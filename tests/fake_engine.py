"""Stand-in for `python -m agentworkflow ...` in supervisor tests (v4 §6). No AI, no git.

argv: run --dir D   |   resume --run D
Behaviour per start comes from D/script.json: a list consumed one entry per process start.
  "complete"  -> state COMPLETE, heartbeat, exit 0
  "help"      -> state HELP, exit 3
  "crash"     -> state RUNNING (as a dying engine leaves it), exit 1
  "stall"     -> state RUNNING, one heartbeat, then hang forever (a frozen main thread)
  "awaiting"  -> state AWAITING_APPROVAL, exit 3
  "qa"        -> state RUNNING with a DEVELOPMENT -> QA transition, keeps beating 1.5 s, then COMPLETE
"""
import json
import os
import sys
import time
from pathlib import Path


def write(p, obj):
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(obj), encoding="utf-8")
    tmp.replace(p)


def main():
    args = sys.argv[1:]
    d = Path(args[args.index("--dir") + 1] if "--dir" in args else args[args.index("--run") + 1])
    if args[0] == "run":
        print(f"run: {d}", flush=True)
    script = json.loads((d / "script.json").read_text(encoding="utf-8"))
    starts = d / "starts.txt"
    n = int(starts.read_text()) if starts.exists() else 0
    starts.write_text(str(n + 1))
    act = script[min(n, len(script) - 1)]
    state = {"run_id": d.name, "status": "RUNNING", "stage": "DEVELOPMENT", "pause_reason": None}
    hb = {"epoch": time.time(), "pid": os.getpid(), "stage": "DEVELOPMENT", "calls": []}
    write(d / "heartbeat.json", hb)
    if act == "complete":
        state["status"] = "COMPLETE"
        write(d / "state.json", state)
        return 0
    if act == "help":
        state.update(status="HELP", pause_reason="가짜 HELP")
        write(d / "state.json", state)
        return 3
    if act == "awaiting":
        state["status"] = "AWAITING_APPROVAL"
        write(d / "state.json", state)
        return 3
    if act == "qa":
        state["history"] = [{"from": "DEVELOPMENT", "to": "QA", "note": "TODO 전부 체크"}]
        write(d / "state.json", state)
        for _ in range(6):
            time.sleep(0.25)
            write(d / "heartbeat.json", {**hb, "epoch": time.time()})
        state["status"] = "COMPLETE"
        write(d / "state.json", state)
        return 0
    write(d / "state.json", state)
    if act == "crash":
        return 1
    if act == "stall":
        while True:
            time.sleep(1)
    return 9


if __name__ == "__main__":
    sys.exit(main())
