#!/usr/bin/env python3
"""corun: call partner AI command-line tools for the Corun skill.

Subcommands
  check [--only P ...]       probe partners with their exact model and effort, write the roster,
                             print the declaration
  status                     print the declaration (re-checks first when the roster is stale)
  ask ROLE --prompt-file F   send a task to the first healthy partner for ROLE, falling back
                             along the role's chain; output goes to stdout
  scan DIR                   pre-publish privacy scan of DIR

Exit codes: 0 success; 2 no partner succeeded; 3 the role falls back to a Claude subagent;
4 usage or configuration error.

Python 3.9 standard library only.
"""
import argparse
import concurrent.futures
import contextlib
import datetime
import fcntl
import hashlib
import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
SKILL_DIR = os.path.dirname(HERE)
PROBE_PROMPT = "Reply with exactly the word CORUN, a space, and the sum of 2 and 3. Nothing else."
PROBE_REPLY = "CORUN 5"
GROK_REFUSAL = "permission required:"
AGY_REFUSAL = "cannot prompt for, so it was auto-denied"
STALE_DAYS = 7
RETRY_MINUTES = 60
PROBE_TIMEOUT = 240
ASK_TIMEOUT = 1800
ARGV_PROMPT_LIMIT = 100_000
KEEP_RUNS_DAYS = 7
AGY_PREAMBLE = ("Read files with your file tools. Shell commands are unavailable in this mode, "
                "so do not attempt them.\n\n")

EXIT_OK, EXIT_FAILED, EXIT_CLAUDE, EXIT_USAGE = 0, 2, 3, 4


class ConfigError(Exception):
    pass


# ---------------------------------------------------------------- locations and config

def config_dir():
    base = os.environ.get("XDG_CONFIG_HOME") or os.path.join(os.path.expanduser("~"), ".config")
    return os.path.join(base, "corun")


def roster_path():
    return os.path.join(config_dir(), "roster.json")


def runs_dir():
    base = os.environ.get("XDG_CACHE_HOME") or os.path.join(os.path.expanduser("~"), ".cache")
    return os.path.join(base, "corun", "runs")


def run_dir():
    """A fresh per-call folder for corun's own files (task, reply, stderr). It lives in the
    user's cache folder, which no partner sandbox can write, so a partner cannot redirect
    these writes. Folders older than KEEP_RUNS_DAYS are removed."""
    base = runs_dir()
    os.makedirs(base, mode=0o700, exist_ok=True)
    cutoff = time.time() - KEEP_RUNS_DAYS * 86400
    for name in os.listdir(base):
        path = os.path.join(base, name)
        with contextlib.suppress(OSError):
            if os.path.isdir(path) and not os.path.islink(path) and os.path.getmtime(path) < cutoff:
                shutil.rmtree(path)
    return tempfile.mkdtemp(prefix=time.strftime("%Y%m%d-%H%M%S-"), dir=base)


def under_temp(path):
    """True when path lies in a folder Grok's read-only sandbox may write (temp folders)."""
    real = os.path.realpath(path)
    roots = {os.path.realpath(p) for p in (tempfile.gettempdir(), "/tmp", "/var/tmp")}
    return any(real == r or real.startswith(r + os.sep) for r in roots)


def partner_dir():
    """A fresh folder in the system temp directory where Grok may write."""
    base = os.path.join(tempfile.gettempdir(), "corun")
    os.makedirs(base, exist_ok=True)
    return tempfile.mkdtemp(prefix="grok-", dir=base)


def load_models():
    """Return (models, digest) from the user's config folder if present, else the skill's own."""
    local = os.path.join(config_dir(), "models.json")
    path = local if os.path.isfile(local) else os.path.join(SKILL_DIR, "models.json")
    try:
        with open(path, "rb") as f:
            raw = f.read()
        models = json.loads(raw.decode("utf-8"))
    except (OSError, ValueError) as e:
        raise ConfigError("cannot read %s: %s" % (path, e))
    validate_models(models, path)
    return models, hashlib.sha256(raw).hexdigest()[:16]


def validate_models(m, path):
    def fail(msg):
        raise ConfigError("%s: %s" % (path, msg))
    if not isinstance(m, dict):
        fail("top level must be an object")
    partners, roles = m.get("partners"), m.get("roles")
    if not isinstance(partners, dict) or not isinstance(roles, dict) or not roles:
        fail("needs 'partners' and 'roles' objects")
    for name, p in partners.items():
        if not isinstance(p, dict) or not all(isinstance(p.get(k), str) for k in
                                              ("label", "cli", "model", "effort")):
            fail("partner %r needs label, cli, model and effort strings" % name)
        if p["cli"] not in ("codex", "agy", "grok"):
            fail("partner %r has unsupported cli %r" % (name, p["cli"]))
    fallbacks = m.get("claude_fallbacks", {})
    if not isinstance(fallbacks, dict):
        fail("'claude_fallbacks' must be an object")
    for name, r in roles.items():
        if not isinstance(r, dict) or not isinstance(r.get("chain"), list) or not r["chain"]:
            fail("role %r needs a non-empty chain" % name)
        if not isinstance(r.get("pick", 1), int) or r.get("pick", 1) < 1:
            fail("role %r has an invalid pick" % name)


def now_iso():
    return datetime.datetime.now(datetime.timezone.utc).replace(microsecond=0).isoformat()


def parse_time(value):
    """ISO timestamp to an aware datetime; naive values are taken as UTC; bad values give None."""
    if not isinstance(value, str):
        return None
    try:
        t = datetime.datetime.fromisoformat(value)
    except ValueError:
        return None
    return t if t.tzinfo else t.replace(tzinfo=datetime.timezone.utc)


def age_minutes(value):
    t = parse_time(value)
    if t is None:
        return float("inf")
    return (datetime.datetime.now(datetime.timezone.utc) - t).total_seconds() / 60


def find_bin(cli, models):
    """CORUN_<CLI>_BIN, then PATH, then known install locations."""
    env = os.environ.get("CORUN_%s_BIN" % cli.upper())
    if env:
        return env if os.access(env, os.X_OK) else None
    found = shutil.which(cli)
    if found:
        return found
    for path in models.get("cli_locations", {}).get(cli, []):
        path = os.path.expanduser(path)
        if os.access(path, os.X_OK):
            return path
    return None


# ---------------------------------------------------------------- running a partner

def run(cmd, cwd, timeout, stdin_text=None):
    """Run a command in its own process group; kill the whole group on timeout."""
    start = time.time()
    try:
        p = subprocess.Popen(cmd, cwd=cwd, stdin=subprocess.PIPE if stdin_text is not None
                             else subprocess.DEVNULL, stdout=subprocess.PIPE,
                             stderr=subprocess.PIPE, start_new_session=True)
    except OSError as e:
        return 127, "", str(e), 0.0
    data = stdin_text.encode("utf-8") if stdin_text is not None else None
    try:
        out, err = p.communicate(input=data, timeout=timeout)
        rc = p.returncode
    except subprocess.TimeoutExpired:
        with contextlib.suppress(OSError):
            os.killpg(p.pid, signal.SIGKILL)
        out, err = p.communicate()
        rc, err = 124, b"timed out after %ds" % timeout
    return (rc, out.decode("utf-8", "replace"), err.decode("utf-8", "replace"),
            round(time.time() - start, 1))


def safe_write(path, text):
    """Write a file without following a symlink. Returns False instead of raising."""
    flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(path, flags, 0o600)
    except OSError:
        return False
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(text)
    return True


def safe_read(path):
    try:
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    except OSError:
        return None
    with os.fdopen(fd, "rb") as f:
        return f.read().decode("utf-8", "replace")


def get_version(binary):
    rc, out, err, _ = run([binary, "--version"], None, 30)
    text = (out or err).strip().splitlines()
    return text[0].strip() if rc == 0 and text else "unknown"


def build_command(spec, binary, prompt, project, work):
    """Return (cmd, cwd, stdin_text) for one partner call. Partners never get write access
    to the project: Codex runs read-only with the user's config (MCP servers, plugins) ignored,
    Antigravity runs in plan mode with its terminal sandbox, and Grok runs in its read-only
    kernel sandbox with MCP tools denied, working in its own temp folder."""
    cli, model, effort = spec["cli"], spec["model"], spec["effort"]
    if cli == "codex":
        cmd = [binary, "exec", "--ignore-user-config", "-m", model,
               "-c", "model_reasoning_effort=%s" % effort, "-s", "read-only",
               "--skip-git-repo-check", "--ephemeral", "-C", project,
               "-o", os.path.join(work, "last_message.txt"), "-"]
        return cmd, project, prompt
    if cli == "agy":
        prompt = AGY_PREAMBLE + prompt
        cmd = [binary, "--model", model, "--mode", "plan", "--sandbox", "--disable-slash-commands"]
        if len(prompt) > ARGV_PROMPT_LIMIT:
            task = os.path.join(work, "task.md")
            safe_write(task, prompt)
            cmd += ["--add-dir", work]
            prompt = AGY_PREAMBLE + "Read the task in %s and carry it out exactly." % task
        return cmd + ["-p=" + prompt], project, None
    if cli == "grok":
        pdir = partner_dir()
        task = os.path.join(work, "task.md")
        safe_write(task, "Your working folder is %s and you may write files only there. "
                         "The project folder to read is %s.\n\n%s" % (pdir, project, prompt))
        cmd = [binary, "--always-approve", "--sandbox", "read-only", "--deny", "MCPTool(*)",
               "-m", model, "--reasoning-effort", effort, "--cwd", pdir,
               "--prompt-file", task]
        return cmd, pdir, None
    raise ConfigError("unsupported cli %r" % cli)


def call_partner(spec, binary, prompt, project, timeout):
    """Run one partner on a prompt. Returns (ok, text, error, seconds, workdir, refused).
    refused is True when the partner works but refused a tool call this task needed; such a
    failure says nothing about the partner's health."""
    work = run_dir()
    cmd, cwd, stdin_text = build_command(spec, binary, prompt, project, work)
    rc, out, err, secs = run(cmd, cwd, timeout, stdin_text)
    text = out
    if spec["cli"] == "codex":
        text = safe_read(os.path.join(work, "last_message.txt")) or ""
    safe_write(os.path.join(work, "reply.txt"), text)
    safe_write(os.path.join(work, "stderr.txt"), err)
    if rc == 124:
        return False, text, "timed out after %ss" % timeout, secs, work, False
    if spec["cli"] == "grok" and any(l.startswith(GROK_REFUSAL) for l in err.splitlines()):
        return False, text, "a tool call was refused for lack of approval", secs, work, True
    if spec["cli"] == "agy" and AGY_REFUSAL in err and not text.strip():
        return False, text, "a tool call was refused for lack of approval", secs, work, True
    if rc != 0:
        return False, text, "exit %s: %s" % (rc, last_line(err) or last_line(out)), secs, work, False
    if not text.strip():
        return False, text, "empty reply (%s)" % (last_line(err) or "no output"), secs, work, False
    return True, text, "", secs, work, False


def last_line(s):
    lines = [l for l in (s or "").strip().splitlines() if l.strip()]
    return lines[-1][:200] if lines else ""


# ---------------------------------------------------------------- roster

@contextlib.contextmanager
def roster_lock():
    os.makedirs(config_dir(), exist_ok=True)
    with open(os.path.join(config_dir(), "roster.lock"), "a") as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(f, fcntl.LOCK_UN)


def load_roster():
    try:
        with open(roster_path(), encoding="utf-8") as f:
            r = json.load(f)
    except (OSError, ValueError):
        return None
    if not isinstance(r, dict) or not isinstance(r.get("partners"), dict) \
            or not isinstance(r.get("roles"), dict):
        return None
    if not all(isinstance(p, dict) for p in r["partners"].values()):
        return None
    if not all(isinstance(v, list) and all(isinstance(n, str) for n in v)
               for v in r["roles"].values()):
        return None
    return r


def save_roster(roster):
    fd, tmp = tempfile.mkstemp(prefix="roster.", suffix=".tmp", dir=config_dir())
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(roster, f, indent=2)
    os.replace(tmp, roster_path())


def resolve_roles(models, partners):
    fallbacks = models.get("claude_fallbacks", {})
    roles = {}
    for role, spec in models["roles"].items():
        picked = []
        for name in spec["chain"]:
            if name in fallbacks or partners.get(name, {}).get("ok"):
                picked.append(name)
            if len(picked) == spec.get("pick", 1):
                break
        roles[role] = picked
    return roles


def probe(name, spec, models):
    binary = find_bin(spec["cli"], models)
    entry = {"label": spec["label"], "cli": spec["cli"], "model": spec["model"],
             "effort": spec["effort"], "checked_at": now_iso()}
    if not binary:
        entry.update(ok=False, version="", error="%s not installed" % spec["cli"], seconds=0)
        return name, entry
    entry["version"] = get_version(binary)
    ok, text, error, secs, _, _ = call_partner(spec, binary, PROBE_PROMPT, tempfile.gettempdir(),
                                               PROBE_TIMEOUT)
    if ok and PROBE_REPLY not in text:
        ok, error = False, "unexpected reply: %s" % last_line(text)
    entry.update(ok=ok, error=error, seconds=secs)
    return name, entry


def check(models, digest, only=None):
    names = [n for n in models["partners"] if not only or n in only]
    with concurrent.futures.ThreadPoolExecutor(max_workers=len(names) or 1) as pool:
        results = list(pool.map(lambda n: probe(n, models["partners"][n], models), names))
    with roster_lock():
        roster = load_roster() or {"partners": {}, "roles": {}}
        for name, entry in results:
            roster["partners"][name] = entry
        for name in list(roster["partners"]):
            if name not in models["partners"]:
                del roster["partners"][name]
        if not only or "checked_at" not in roster:
            roster["checked_at"] = now_iso()
            roster["models_digest"] = digest
            roster["models_updated"] = models.get("updated", "")
        roster["roles"] = resolve_roles(models, roster["partners"])
        save_roster(roster)
    return roster


def mark_failed(models, name, error):
    with roster_lock():
        roster = load_roster()
        if roster is None or name not in roster["partners"]:
            return
        roster["partners"][name].update(ok=False, error=error, checked_at=now_iso())
        roster["roles"] = resolve_roles(models, roster["partners"])
        save_roster(roster)


def stale_reason(roster, models, digest):
    if not roster:
        return "no usable roster"
    if roster.get("models_digest") != digest:
        return "models.json changed"
    if set(roster["partners"]) != set(models["partners"]):
        return "partner list changed"
    checked = parse_time(roster.get("checked_at"))
    if checked is None:
        return "roster has no check time"
    days = (datetime.datetime.now(datetime.timezone.utc) - checked).days
    if days >= STALE_DAYS:
        return "roster is %d days old" % days
    return ""


def declaration(roster, models):
    fallbacks = models.get("claude_fallbacks", {})
    rows = []
    for role, names in roster["roles"].items():
        for name in names:
            if name in fallbacks:
                rows.append((role, fallbacks[name], "same-family fallback"))
            else:
                p = roster["partners"][name]
                rows.append((role, "%s (%s, %s)" % (p["label"], p["model"], p["effort"]),
                             "PASS %ss" % p.get("seconds", "?")))
    w = max([len(r[1]) for r in rows] + [24]) + 2
    lines = ["Corun roster, checked %s (models.json %s)"
             % (roster.get("checked_at", "?"), models.get("updated", "")), "",
             "%-10s%-*s%s" % ("ROLE", w, "ASSIGNED (model, effort)", "STATUS")]
    lines += ["%-10s%-*s%s" % (r[0], w, r[1], r[2]) for r in rows]
    bad = ["unavailable: %s (%s): %s" % (p.get("label", n), n, p.get("error", ""))
           for n, p in roster["partners"].items() if not p.get("ok")]
    return "\n".join(lines + ([""] + bad if bad else []))


def fresh_roster(models, digest):
    roster = load_roster()
    reason = stale_reason(roster, models, digest)
    if reason:
        print("corun: re-checking partners (%s)" % reason, file=sys.stderr)
        roster = check(models, digest)
        print(declaration(roster, models), file=sys.stderr)
    return roster


# ---------------------------------------------------------------- ask

def ask(models, digest, role, prompt, project, exclude, partner, timeout):
    if role not in models["roles"]:
        print("corun: unknown role %r; roles: %s" % (role, ", ".join(models["roles"])),
              file=sys.stderr)
        return EXIT_USAGE
    if partner and partner not in models["partners"]:
        print("corun: unknown partner %r" % partner, file=sys.stderr)
        return EXIT_USAGE
    roster = fresh_roster(models, digest)
    fallbacks = models.get("claude_fallbacks", {})
    chain = [partner] if partner else models["roles"][role]["chain"]
    for name in chain:
        if name in fallbacks:
            # Claude fallbacks are never excluded: a second same-family reviewer is a second
            # fresh subagent.
            print("corun: role %s falls back to a %s (same-family). Run the task as that "
                  "subagent." % (role, fallbacks[name]), file=sys.stderr)
            return EXIT_CLAUDE
        if name in exclude:
            continue
        spec = models["partners"].get(name)
        if spec is None:
            print("corun: skipping %r in the %s chain: not a partner in models.json"
                  % (name, role), file=sys.stderr)
            continue
        binary = find_bin(spec["cli"], models)
        if not binary:
            continue
        if spec["cli"] == "grok" and under_temp(project):
            print("corun: skipping %s: the project is in a temp folder, which Grok's sandbox can "
                  "write" % name, file=sys.stderr)
            continue
        state = roster["partners"].get(name, {})
        if not state.get("ok") and age_minutes(state.get("checked_at")) >= RETRY_MINUTES:
            print("corun: %s failed earlier (%s), re-checking it" % (name, state.get("error", "")),
                  file=sys.stderr)
            roster = check(models, digest, only=[name])
            state = roster["partners"][name]
        elif state.get("ok") and get_version(binary) != state.get("version"):
            print("corun: %s changed version, re-checking it" % name, file=sys.stderr)
            roster = check(models, digest, only=[name])
            state = roster["partners"][name]
        if not state.get("ok"):
            continue
        print("corun: role=%s partner=%s (%s, %s, %s)"
              % (role, name, spec["label"], spec["model"], spec["effort"]), file=sys.stderr)
        ok, text, error, secs, work, refused = call_partner(spec, binary, prompt, project, timeout)
        print("corun: %s %s in %ss; files in %s" % (name, "ok" if ok else "FAILED: " + error,
                                                    secs, work), file=sys.stderr)
        if ok:
            sys.stdout.write(text if text.endswith("\n") else text + "\n")
            return EXIT_OK
        if not refused:
            mark_failed(models, name, error)
    print("corun: no partner succeeded for role %s" % role, file=sys.stderr)
    return EXIT_FAILED


# ---------------------------------------------------------------- command line

def main(argv=None):
    ap = argparse.ArgumentParser(prog="corun", description="Partner AI calls for the Corun skill.")
    sub = ap.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("check", help="probe partners and print the declaration")
    c.add_argument("--only", nargs="+", help="probe only these partners")
    sub.add_parser("status", help="print the declaration")
    a = sub.add_parser("ask", help="send a task to a role")
    a.add_argument("role")
    a.add_argument("--prompt-file", required=True, help="file holding the task ('-' for stdin)")
    a.add_argument("--project", default=os.getcwd(), help="project folder partners may read")
    a.add_argument("--exclude", nargs="*", default=[], help="partners to skip")
    a.add_argument("--partner", help="use exactly this partner")
    a.add_argument("--timeout", type=int, default=ASK_TIMEOUT)
    s = sub.add_parser("scan", help="pre-publish privacy scan")
    s.add_argument("dir")
    args = ap.parse_args(argv)

    if args.cmd == "scan":
        sys.path.insert(0, HERE)
        import scan as scanner
        return scanner.main([args.dir])

    try:
        models, digest = load_models()
        if args.cmd == "check":
            print(declaration(check(models, digest, only=args.only), models))
            return EXIT_OK
        if args.cmd == "status":
            print(declaration(fresh_roster(models, digest), models))
            return EXIT_OK
        try:
            if args.prompt_file == "-":
                prompt = sys.stdin.read()
            else:
                with open(args.prompt_file, encoding="utf-8", errors="replace") as f:
                    prompt = f.read()
        except OSError as e:
            print("corun: cannot read prompt file: %s" % e, file=sys.stderr)
            return EXIT_USAGE
        prompt = prompt.replace("\x00", "")
        if not prompt.strip():
            print("corun: empty prompt", file=sys.stderr)
            return EXIT_USAGE
        project = os.path.abspath(args.project)
        if not os.path.isdir(project):
            print("corun: project folder not found: %s" % project, file=sys.stderr)
            return EXIT_USAGE
        return ask(models, digest, args.role, prompt, project, set(args.exclude),
                   args.partner, args.timeout)
    except ConfigError as e:
        print("corun: %s" % e, file=sys.stderr)
        return EXIT_USAGE


if __name__ == "__main__":
    sys.exit(main())
