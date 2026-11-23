#!/usr/bin/env python3
"""Pre-publish privacy scanner for public skill repositories.

Scans a directory tree for personal information, machine-specific paths,
forbidden files, and leaked credentials using only the Python 3.9 standard
library.
"""

from dataclasses import dataclass
import fnmatch
import getpass
import io
import os
import re
import subprocess
import sys
from typing import List, Optional, Pattern, Sequence, Tuple

FILE_SIZE_LIMIT = 2 * 1024 * 1024
BINARY_SNIFF_SIZE = 8 * 1024
ALLOW_MARKER = "corun-scan: allow"

FORBIDDEN_EXACT_NAMES = frozenset({"roster.json", "auth.json", ".env"})
FORBIDDEN_GLOB_PATTERNS = ("*.pem", "*.key", "*.p12", "id_rsa*", "id_ed25519*", ".env.*")
# Binary files that cannot hold readable personal text; any other unreadable file is reported.
SAFE_BINARY_EXTENSIONS = frozenset({".png", ".jpg", ".jpeg", ".gif", ".webp", ".ico"})

HOME_PATH_ALLOWLIST = frozenset(
    {"you", "user", "USER", "example", "runner", "shared", "Shared"}
)

EMAIL_EXEMPT_DOMAINS = frozenset(
    {"example.com", "example.org", "users.noreply.github.com"}
)

Finding = Tuple[str, int, str, str]


@dataclass(frozen=True)
class _RuleSet:
    """Compiled rule patterns for a single scan invocation."""

    unix_home: Pattern[str]
    win_home: Pattern[str]
    current_user: Optional[Pattern[str]]
    git_email: Optional[Pattern[str]]
    email: Pattern[str]
    secrets: Tuple[Tuple[str, Pattern[str]], ...]


def mask_secret(value: str) -> str:
    """Mask a matched secret to its first six characters followed by ***."""
    return value[:6] + "***"


def is_forbidden_filename(filename: str) -> bool:
    """Return True if a filename matches any forbidden-file rule."""
    if filename in FORBIDDEN_EXACT_NAMES:
        return True
    for pattern in FORBIDDEN_GLOB_PATTERNS:
        if fnmatch.fnmatchcase(filename, pattern):
            return True
    return False


def is_exempt_email(address: str) -> bool:
    """Return True if an email address is allowed by the email rule."""
    local_part, domain = address.lower().rsplit("@", 1)
    if domain in EMAIL_EXEMPT_DOMAINS:
        return True
    if domain == "anthropic.com" and local_part == "noreply":
        return True
    return False


def get_current_username() -> Optional[str]:
    """Return the current OS username if available and at least 3 chars long."""
    try:
        username = getpass.getuser()
    except Exception:
        return None
    if not username or len(username) < 3:
        return None
    return username


def get_git_email(root: str) -> Optional[str]:
    """Return git config user.email if available and non-empty."""
    try:
        completed = subprocess.run(
            ["git", "config", "user.email"],
            cwd=root,
            capture_output=True,
            text=True,
            check=False,
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if completed.returncode != 0:
        return None
    email = completed.stdout.strip()
    return email if email else None


def _build_secret_rules() -> Tuple[Tuple[str, Pattern[str]], ...]:
    """Compile secret detection patterns without matching their own source."""
    return (
        (
            "openai-key",
            re.compile(
                "(?:"
                + "sk-"
                + "(?:proj|svcacct|admin)-[A-Za-z0-9_-]{20,}"
                + "|"
                + "sk-"
                + "[A-Za-z0-9]{20}"
                + "T3BlbkFJ"
                + "[A-Za-z0-9]{20}"
                + ")"
            ),
        ),
        (
            "anthropic-key",
            re.compile(
                "sk-"
                + "ant-"
                + "[a-z0-9]{2,8}-[A-Za-z0-9_-]{20,}"
            ),
        ),
        (
            "google-key",
            re.compile("AI" + "za" + "[0-9A-Za-z_-]{35}"),
        ),
        (
            "xai-key",
            re.compile("xai" + "-" + "[A-Za-z0-9]{20,}"),
        ),
        (
            "github-token",
            re.compile(
                "(?:"
                + "gh"
                + "[pousr]_[A-Za-z0-9]{36,}"
                + "|"
                + "github"
                + "_pat_[A-Za-z0-9_]{20,}"
                + ")"
            ),
        ),
        (
            "aws-key",
            re.compile(
                "(?:A3T[A-Z0-9]|"
                + "AKIA|ASIA|ABIA|ACCA)"
                + "[A-Z2-7]{16}"
            ),
        ),
        (
            "private-key",
            re.compile(
                "-----"
                + "BEGIN"
                + "[ A-Z0-9_-]{0,100}"
                + "PRIVATE"
                + " KEY"
            ),
        ),
        (
            "jwt",
            re.compile(
                "ey"
                + "J"
                + r"[A-Za-z0-9_-]{10,}\."
                + "ey"
                + "J"
                + "[A-Za-z0-9_-]{10,}"
            ),
        ),
    )


def _build_rules(root: str) -> _RuleSet:
    """Build all static and environment-derived scanning rules."""
    unix_home = re.compile(
        r"(?<![A-Za-z0-9])"
        + "(?:/"
        + "Users"
        + "|/"
        + "home"
        + ")"
        + r"/(?P<name>[A-Za-z0-9._-]+)"
    )
    win_home = re.compile(
        r"(?<![A-Za-z0-9])[A-Za-z]:\\+"
        + "Users"
        + r"\\+(?P<name>[A-Za-z0-9._-]+)"
    )

    username = get_current_username()
    current_user_pattern: Optional[Pattern[str]] = None
    if username is not None:
        # Only in identifying contexts (after / \\ ~ or next to @), so a user name that is
        # also an ordinary word ("max", "test") does not flag prose.
        name = re.escape(username)
        current_user_pattern = re.compile(
            r"(?:(?<=[/\\~])" + name + r"(?![\w.-])|(?<![\w.-])" + name + r"(?=@))",
            re.IGNORECASE,
        )

    git_email = get_git_email(root)
    git_email_pattern: Optional[Pattern[str]] = None
    if git_email is not None:
        git_email_pattern = re.compile(re.escape(git_email), re.IGNORECASE)

    email_pattern = re.compile(
        r"(?<![A-Za-z0-9._%+-])"
        + r"[A-Za-z0-9._%+-]+@"
        + r"(?:[A-Za-z0-9-]+\.)+[A-Za-z]{2,}\b"
    )

    return _RuleSet(
        unix_home=unix_home,
        win_home=win_home,
        current_user=current_user_pattern,
        git_email=git_email_pattern,
        email=email_pattern,
        secrets=_build_secret_rules(),
    )


def read_text_file(path: str) -> Optional[str]:
    """Read a file as UTF-8 text, or return None if binary, oversized, or unreadable."""
    try:
        if os.path.getsize(path) > FILE_SIZE_LIMIT:
            return None
        with open(path, "rb") as handle:
            head = handle.read(BINARY_SNIFF_SIZE)
            if b"\x00" in head:
                return None
            remaining_budget = FILE_SIZE_LIMIT - len(head) + 1
            tail = handle.read(remaining_budget)
    except OSError:
        return None

    data = head + tail
    if len(data) > FILE_SIZE_LIMIT:
        return None
    return data.decode("utf-8", errors="replace")


def scan_line(
    relpath: str, lineno: int, line: str, rules: _RuleSet
) -> List[Finding]:
    """Scan a single text line and return all findings on that line."""
    findings: List[Finding] = []
    for rule_name, pattern in rules.secrets:
        for match in pattern.finditer(line):
            findings.append(
                (relpath, lineno, rule_name, mask_secret(match.group(0)))
            )
    # The allow marker exempts identity rules on its line, never secret rules.
    if ALLOW_MARKER in line:
        return findings

    for pattern in (rules.unix_home, rules.win_home):
        for match in pattern.finditer(line):
            if match.group("name") not in HOME_PATH_ALLOWLIST:
                findings.append((relpath, lineno, "home-path", match.group(0)))

    if rules.current_user is not None:
        for match in rules.current_user.finditer(line):
            findings.append((relpath, lineno, "current-user", match.group(0)))

    if rules.git_email is not None:
        for match in rules.git_email.finditer(line):
            findings.append((relpath, lineno, "git-email", match.group(0)))

    for match in rules.email.finditer(line):
        matched_text = match.group(0)
        if not is_exempt_email(matched_text):
            findings.append((relpath, lineno, "email", matched_text))

    return findings


def _normalize_relpath(full_path: str, root: str) -> str:
    """Return a forward-slash relative path from root to full_path."""
    relpath = os.path.relpath(full_path, root)
    if os.sep != "/":
        relpath = relpath.replace(os.sep, "/")
    return relpath


def _scan_directory(root: str) -> Tuple[List[Finding], int]:
    """Walk root recursively, returning sorted findings and scanned file count."""
    if not os.path.isdir(root):
        raise NotADirectoryError(f"Not a directory: {root}")

    rules = _build_rules(root)
    findings: List[Finding] = []
    scanned_files = 0

    for dirpath, dirnames, filenames in os.walk(
        root, topdown=True, followlinks=False
    ):
        dirnames[:] = sorted(name for name in dirnames if name != ".git")
        for filename in sorted(filenames):
            full_path = os.path.join(dirpath, filename)
            relpath = _normalize_relpath(full_path, root)

            if is_forbidden_filename(filename):
                findings.append((relpath, 0, "forbidden-file", filename))

            findings.extend(scan_line(relpath, 0, relpath, rules))
            text = read_text_file(full_path)
            if text is None:
                if os.path.splitext(filename)[1].lower() not in SAFE_BINARY_EXTENSIONS:
                    findings.append((relpath, 0, "unscanned-file",
                                     "binary, oversized or unreadable"))
                continue

            scanned_files += 1
            for lineno, raw_line in enumerate(io.StringIO(text), start=1):
                line = raw_line.rstrip("\r\n")
                findings.extend(scan_line(relpath, lineno, line, rules))

    findings.sort(key=lambda item: (item[0], item[1], item[2], item[3]))
    return findings, scanned_files


def scan(root: str) -> List[Finding]:
    """Scan a directory tree and return sorted (relpath, lineno, rule, match) tuples."""
    findings, _ = _scan_directory(root)
    return findings


def format_finding(finding: Finding) -> str:
    """Format a finding tuple as a single output line."""
    relpath, lineno, rule, matched = finding
    return f"{relpath}:{lineno}: {rule}: {matched}"


def main(argv: Optional[Sequence[str]] = None) -> int:
    """CLI entrypoint for scan.py."""
    args = sys.argv[1:] if argv is None else list(argv)
    if len(args) != 1:
        print("usage: python3 scan.py <directory>", file=sys.stderr)
        return 2

    target = args[0]
    if not os.path.isdir(target):
        print(f"scan: not a directory: {target}", file=sys.stderr)
        return 2

    findings, scanned_files = _scan_directory(target)
    if not findings:
        print(f"scan: clean ({scanned_files} files)")
        return 0

    for finding in findings:
        print(format_finding(finding))
    return 1


if __name__ == "__main__":
    sys.exit(main())
