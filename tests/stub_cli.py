#!/usr/bin/env python3
"""Stand-in for the agy, codex and grok CLIs in tests.

Behaviour is set per CLI by the environment variable STUB_<NAME>_MODE:
  ok (default)  reply "CORUN 5" plus the prompt length
  noout         (codex) exit 0 without writing the -o file
  echo          reply with the prompt text itself
  fail          exit 1 with an error on stderr
  perm          print a permission refusal on stderr and exit 0 (Grok's headless behaviour)
  empty         exit 0 with no output
  slow          sleep 30 seconds, then reply
STUB_<NAME>_VERSION sets the --version output. Every call's argv is appended, as JSON,
to the file named by STUB_LOG.
"""
import json
import os
import sys
import time

name = os.path.basename(sys.argv[0])
mode = os.environ.get("STUB_%s_MODE" % name.upper(), "ok")
log = os.environ.get("STUB_LOG")
if log:
    with open(log, "a") as f:
        f.write(json.dumps({"cli": name, "argv": sys.argv[1:], "cwd": os.getcwd()}) + "\n")

if sys.argv[1:] == ["--version"]:
    print(os.environ.get("STUB_%s_VERSION" % name.upper(), "%s 1.0" % name))
    sys.exit(0)

args = sys.argv[1:]
if args and args[-1] == "-":
    prompt = sys.stdin.read()
elif "--prompt-file" in args:
    with open(args[args.index("--prompt-file") + 1]) as f:
        prompt = f.read()
else:
    prompt = next((a[3:] for a in args if a.startswith("-p=")), args[-1] if args else "")
if log:
    with open(log, "a") as f:
        f.write(json.dumps({"cli": name, "prompt": prompt}) + "\n")
reply = "CORUN 5 from %s (prompt %d chars)" % (name, len(prompt))
if mode == "echo":
    reply = prompt

if mode == "fail":
    print("stub failure", file=sys.stderr)
    sys.exit(1)
if mode == "perm":
    print("permission required: Execute `x` was not run, as a headless run cannot ask for approval",
          file=sys.stderr)
    sys.exit(0)
if mode == "empty":
    sys.exit(0)
if mode == "slow":
    time.sleep(30)

if mode == "noout":
    print("codex progress noise")
    sys.exit(0)
if name == "codex" and "-o" in args:
    with open(args[args.index("-o") + 1], "w") as f:
        f.write(reply + "\n")
    print("codex progress noise")
else:
    print(reply)
