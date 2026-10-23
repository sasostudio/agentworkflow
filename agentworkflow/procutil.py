"""A command run with a time limit that really ends it.

subprocess.run on Windows ends only the direct child at the limit, then waits with NO limit for the output pipes —
a grandchild still holds them (cmd → powershell → Godot · Godot_console.exe → Godot). Measured on a real Godot
project: the machine check hit its 1800 s limit and returned after 9886.6 s, when Godot was ended by hand.

run() is a drop-in for subprocess.run (same CompletedProcess, same TimeoutExpired) that ends the whole tree at the
limit and bounds the read of what is left. Standard library only: selfcheck.py also runs as a plain script."""

import os
import subprocess

DRAIN_SEC = 10  # after the tree is ended: a process outside it (started detached) may still hold a pipe


def kill_tree(p):
    if os.name == "nt":
        subprocess.run(["taskkill", "/PID", str(p.pid), "/T", "/F"], capture_output=True)
    if p.poll() is None:
        p.kill()


def run(args, *, input=None, timeout=None, capture_output=False, **kw):
    if capture_output:
        kw["stdout"] = kw["stderr"] = subprocess.PIPE
    if input is not None:
        kw["stdin"] = subprocess.PIPE
    p = subprocess.Popen(args, **kw)
    try:
        out, err = p.communicate(input, timeout=timeout)
    except subprocess.TimeoutExpired:
        kill_tree(p)
        try:
            out, err = p.communicate(timeout=DRAIN_SEC)
        except subprocess.TimeoutExpired:  # give up on the pipes rather than wait on them again
            out, err = None, None
        raise subprocess.TimeoutExpired(p.args, timeout, output=out, stderr=err) from None
    except BaseException:  # e.g. Ctrl+C: leave nothing running behind
        kill_tree(p)
        raise
    return subprocess.CompletedProcess(p.args, p.returncode, out, err)
