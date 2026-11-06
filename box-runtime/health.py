"""health.py — STEP 12 desk health for the hourly "desk-health" routine.

CLI (from /workspace/desk):
  .venv/bin/python health.py check          # JSON: actions + notify texts (no side effects)
  .venv/bin/python health.py check --apply  # same, then perform restarts
  .venv/bin/python health.py apply          # check + apply
  .venv/bin/python health.py restart-desk   # force subprocess re-launch of main.py

Stale heartbeat (> STALE_S): restart that agent (or the desk process) and produce
  "Restarted {Agent}, down {n} min"
>_WS_RECONNECT_ALERT reconnects in the last hour -> alert text for Telegram.

Restart path:
  - process dead (no live pid / main heartbeat stale with no process): subprocess re-launch main.py
  - process alive, agent stale: write db setting _restart_agents; main.watch_restarts cancels+restarts that task
    (scanner/auditor/analyst/executor/risk/publisher/telegram are all restartable in-process)
  - process alive but HUNG (main beat > HUNG_S, publisher/telegram beat > EXTRA_KILL_S, or a restart request
    not picked up for RESTART_PENDING_S): SIGTERM/SIGKILL the pid and relaunch main.py (self-heals in one check)
  - autopush dead (no live data/autopush.pid): subprocess re-launch scripts/autopush.sh
  - autopush stuck (pid alive but no PUSHED for > AUTOPUSH_STUCK_S while publish/ is fresh):
      kill + relaunch scripts/autopush.sh
"""
from __future__ import annotations

import argparse
import json
import os
import re
import signal
import subprocess
import sys
import time
from pathlib import Path

from config import DB_PATH, ROOT
from db import DB

STALE_S = 120  # 2 min
WS_RECONNECT_ALERT = 10
AUTOPUSH_STUCK_S = 240  # 4 min without PUSHED while publish is fresh (phone offline at 600s)
PUBLISH_FRESH_S = 180  # publish/state.json mtime considered "desk is writing"
PID_FILE = Path(os.environ.get("DESK_PID_FILE", ROOT / "data" / "desk.pid"))
AUTOPUSH_PID_FILE = Path(os.environ.get("AUTOPUSH_PID_FILE", ROOT / "data" / "autopush.pid"))
AUTOPUSH_LOG = Path(os.environ.get("AUTOPUSH_LOG", ROOT / "logs" / "autopush.log"))
PUBLISH_DIR = Path(os.environ.get("DESK_PUBLISH", ROOT / "publish"))
RESTART_KEY = "_restart_agents"
RESTART_TS_KEY = "_restart_agents_ts"
HUNG_S = 180          # main heartbeat this old while the pid is alive -> desk is hung: kill + relaunch
EXTRA_KILL_S = 480    # publisher/telegram this old (in-process restart already tried) -> kill + relaunch
RESTART_PENDING_S = 90  # a restart request main never picked up -> its watch_restarts loop is dead
CONFIRM_S = 60        # CLI apply re-checks for up to this long before killing (VM resume makes beats look old)
AGENTS = ("scanner", "auditor", "analyst", "executor", "risk", "main")

_PUSHED_RE = re.compile(r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}).*\bPUSHED\b")


def display_name(agent: str) -> str:
    """Telegram wording: Restarted Scanner / Main / …"""
    return (agent or "?").strip().capitalize()


def down_min(age_s: float) -> int:
    """Whole minutes down, at least 1 once past the stale threshold."""
    return max(1, int(age_s // 60))


def pid_alive(pid: int | None) -> bool:
    if not pid or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def read_pid(path: Path = PID_FILE) -> int | None:
    try:
        return int(path.read_text().strip())
    except (OSError, ValueError):
        return None


def write_pid(pid: int | None = None, path: Path = PID_FILE):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(str(pid or os.getpid()))


def clear_pid(path: Path = PID_FILE):
    try:
        path.unlink()
    except FileNotFoundError:
        pass


def ws_reconnects_last_hour(db: DB, now: float | None = None) -> int:
    """Count timestamps in settings._ws_reconnects that fall inside the last hour."""
    now = time.time() if now is None else now
    raw = db.get_setting("_ws_reconnects", []) or []
    if isinstance(raw, (int, float)):
        # scanner may have written a bare count in an older build
        return int(raw)
    return sum(1 for t in raw if isinstance(t, (int, float)) and now - float(t) < 3600)


def last_pushed_age_s(log_path: Path = AUTOPUSH_LOG, now: float | None = None) -> float | None:
    """Seconds since the last 'PUSHED' line in autopush.log, or None if never seen."""
    now = time.time() if now is None else now
    try:
        text = log_path.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return None
    last_ts = None
    for line in text.splitlines():
        m = _PUSHED_RE.match(line)
        if not m:
            continue
        try:
            last_ts = time.mktime(time.strptime(m.group(1), "%Y-%m-%d %H:%M:%S"))
        except ValueError:
            continue
    if last_ts is None:
        return None
    return now - last_ts


def publish_mtime_age_s(publish_dir: Path = PUBLISH_DIR, now: float | None = None) -> float | None:
    """Age of the newest publish/d/*/state.json mtime, or None if missing."""
    now = time.time() if now is None else now
    newest = None
    try:
        for p in publish_dir.glob("d/*/state.json"):
            try:
                mt = p.stat().st_mtime
            except OSError:
                continue
            if newest is None or mt > newest:
                newest = mt
    except OSError:
        return None
    if newest is None:
        return None
    return now - newest


def autopush_stuck(
    *,
    now: float | None = None,
    log_path: Path = AUTOPUSH_LOG,
    publish_dir: Path = PUBLISH_DIR,
    stuck_s: float = AUTOPUSH_STUCK_S,
    fresh_s: float = PUBLISH_FRESH_S,
) -> tuple[bool, float | None, float | None]:
    """True when pid-alive caller should restart: no PUSHED for stuck_s while publish is fresh.

    Returns (stuck, pushed_age_s, publish_age_s).
    """
    now = time.time() if now is None else now
    pushed_age = last_pushed_age_s(log_path, now=now)
    pub_age = publish_mtime_age_s(publish_dir, now=now)
    if pub_age is None or pub_age > fresh_s:
        return False, pushed_age, pub_age
    if pushed_age is None:
        # Never pushed, but publish is being written — treat as stuck past stuck_s of "start"
        # only if the log itself is old enough (mtime) or missing PUSHED with an old log.
        try:
            log_age = now - log_path.stat().st_mtime
        except OSError:
            log_age = stuck_s + 1
        return log_age > stuck_s, pushed_age, pub_age
    return pushed_age > stuck_s, pushed_age, pub_age


def check(db: DB, now: float | None = None, pid_path: Path = PID_FILE,
         autopush_pid_path: Path = AUTOPUSH_PID_FILE,
         autopush_log_path: Path = AUTOPUSH_LOG,
         publish_dir: Path = PUBLISH_DIR) -> dict:
    """Read heartbeats + _ws_reconnects -> structured actions + notify texts. No side effects."""
    now = time.time() if now is None else now
    actions: list[dict] = []
    notifies: list[str] = []
    alerts: list[str] = []

    beats = {h["agent"]: h for h in db.q("SELECT agent, ts FROM heartbeats")}
    # Known agents that should be beating; also anything already in the table.
    names = list(dict.fromkeys([*AGENTS, *beats.keys()]))
    process_alive = pid_alive(read_pid(pid_path))
    main_row = beats.get("main")
    main_age = (now - float(main_row["ts"])) if main_row else None
    main_stale = main_row is None or (main_age is not None and main_age > STALE_S)

    hung: list[str] = []
    if process_alive:
        if main_age is not None and main_age > HUNG_S:
            hung.append(f"main heartbeat {int(main_age)}s old")
        for extra in ("publisher", "telegram"):
            row = beats.get(extra)
            if row is not None and now - float(row["ts"]) > EXTRA_KILL_S:
                hung.append(f"{extra} heartbeat {int(now - float(row['ts']))}s old")
        try:
            pending = db.get_setting(RESTART_KEY, []) or []
            pts = db.get_setting(RESTART_TS_KEY, None)
        except Exception:
            pending, pts = [], None
        if pending and pts and now - float(pts) > RESTART_PENDING_S:
            hung.append(f"restart request {pending} not picked up for {int(now - float(pts))}s")

    if hung:
        age = max([main_age or 0.0] + [now - float(beats[x]["ts"]) for x in ("publisher", "telegram") if x in beats])
        n = down_min(age)
        actions.append({"type": "kill_relaunch_desk", "agent": "main", "down_min": n, "age_s": age,
                        "reasons": hung, "pid": read_pid(pid_path)})
        notifies.append(f"Restarted Desk (was hung), down {n} min")
    elif main_stale and not process_alive:
        age = main_age if main_age is not None else STALE_S + 1
        n = down_min(age)
        text = f"Restarted {display_name('main')}, down {n} min"
        actions.append({"type": "restart_desk", "agent": "main", "down_min": n, "age_s": age})
        notifies.append(text)
    else:
        for agent in names:
            if agent == "main" and process_alive and not main_stale:
                continue
            row = beats.get(agent)
            if row is None:
                # never beat: only flag known pipeline agents (not publisher/telegram extras)
                if agent not in AGENTS:
                    continue
                age = STALE_S + 1
            else:
                age = now - float(row["ts"])
                if age <= STALE_S:
                    continue
            n = down_min(age)
            text = f"Restarted {display_name(agent)}, down {n} min"
            if agent == "main":
                actions.append({"type": "restart_desk", "agent": "main", "down_min": n, "age_s": age})
            else:
                actions.append({"type": "restart_agent", "agent": agent, "down_min": n, "age_s": age})
            notifies.append(text)

    n_ws = ws_reconnects_last_hour(db, now)
    if n_ws > WS_RECONNECT_ALERT:
        alerts.append(f"Scanner websocket reconnected {n_ws} times in the last hour (>{WS_RECONNECT_ALERT})")

    autopush_alive = pid_alive(read_pid(autopush_pid_path))
    stuck, pushed_age, pub_age = autopush_stuck(
        now=now, log_path=autopush_log_path, publish_dir=publish_dir,
    )
    if not autopush_alive:
        actions.append({"type": "restart_autopush", "agent": "autopush", "reason": "dead"})
        notifies.append("Restarted Autopush")
    elif stuck:
        actions.append({
            "type": "restart_autopush",
            "agent": "autopush",
            "reason": "stuck",
            "pushed_age_s": pushed_age,
            "publish_age_s": pub_age,
        })
        notifies.append("Restarted Autopush (stuck: no PUSHED while publish fresh)")

    return {
        "ok": not actions and not alerts,
        "stale_s": STALE_S,
        "hung": hung,
        "process_alive": process_alive,
        "autopush_alive": autopush_alive,
        "autopush_stuck": bool(autopush_alive and stuck),
        "autopush_pushed_age_s": pushed_age,
        "publish_age_s": pub_age,
        "ws_reconnects_1h": n_ws,
        "actions": actions,
        "notifies": notifies,
        "alerts": alerts,
        "telegram_texts": notifies + alerts,
    }


def request_agent_restarts(db: DB, agents: list[str]):
    """Ask the running desk process to cancel+recreate these agent tasks."""
    cur = list(db.get_setting(RESTART_KEY, []) or [])
    if not cur:
        db.set_setting(RESTART_TS_KEY, time.time())
    for a in agents:
        if a and a not in cur and a != "main":
            cur.append(a)
    db.set_setting(RESTART_KEY, cur)


def pop_restart_requests(db: DB) -> list[str]:
    cur = list(db.get_setting(RESTART_KEY, []) or [])
    if cur:
        db.set_setting(RESTART_KEY, [])
        db.set_setting(RESTART_TS_KEY, None)
    return cur


def relaunch_desk(cwd: Path = ROOT) -> dict:
    """Subprocess re-launch of main.py when the desk process is dead."""
    pid = read_pid()
    if pid_alive(pid):
        return {"launched": False, "reason": "already_running", "pid": pid}
    log = cwd / "logs" / "run.out"
    log.parent.mkdir(parents=True, exist_ok=True)
    py = cwd / ".venv" / "bin" / "python"
    if not py.exists():
        py = Path(sys.executable)
    cmd = [str(py), str(cwd / "main.py")]
    with open(log, "a") as out:
        proc = subprocess.Popen(cmd, cwd=str(cwd), stdout=out, stderr=subprocess.STDOUT,
                                start_new_session=True)
    return {"launched": True, "pid": proc.pid, "cmd": cmd}


def _kill_pid(pid: int, grace_s: float = 2.0) -> bool:
    """SIGTERM then SIGKILL a process (and its group if it is a session leader)."""
    if not pid_alive(pid):
        return False
    try:
        os.killpg(pid, signal.SIGTERM)
    except OSError:
        try:
            os.kill(pid, signal.SIGTERM)
        except OSError:
            return False
    deadline = time.time() + grace_s
    while time.time() < deadline:
        if not pid_alive(pid):
            return True
        time.sleep(0.1)
    try:
        os.killpg(pid, signal.SIGKILL)
    except OSError:
        try:
            os.kill(pid, signal.SIGKILL)
        except OSError:
            pass
    return not pid_alive(pid)


def relaunch_autopush(cwd: Path = ROOT, force: bool = False) -> dict:
    """Subprocess re-launch of scripts/autopush.sh when the autopusher is dead or stuck."""
    pid = read_pid(AUTOPUSH_PID_FILE)
    if pid_alive(pid):
        if not force:
            return {"launched": False, "reason": "already_running", "pid": pid}
        _kill_pid(pid)
        clear_pid(AUTOPUSH_PID_FILE)
        # brief pause so the exiting trap's pid clear / port-less script settles
        time.sleep(0.3)
    log = cwd / "logs" / "autopush.log"
    log.parent.mkdir(parents=True, exist_ok=True)
    script = cwd / "scripts" / "autopush.sh"
    if not script.exists():
        return {"launched": False, "reason": "missing_script", "path": str(script)}
    with open(log, "a") as out:
        proc = subprocess.Popen(
            ["bash", str(script)],
            cwd=str(cwd),
            stdout=out,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
    # Script writes data/autopush.pid itself; give it a moment via caller.
    return {"launched": True, "pid": proc.pid, "cmd": ["bash", str(script)], "forced": force}


def kill_relaunch_desk(db: DB | None = None, pid_path: Path = PID_FILE, cwd: Path = ROOT) -> dict:
    """The desk pid is alive but hung: SIGTERM (then SIGKILL) it and launch a fresh main.py."""
    pid = read_pid(pid_path)
    out: dict = {"killed_pid": pid}
    if pid and pid == os.getpid():
        return {**out, "killed": False, "launched": False, "reason": "refusing to kill self"}
    if pid_alive(pid):
        out["killed"] = _kill_pid(pid, grace_s=8.0)
        if not out["killed"]:
            return {**out, "launched": False, "reason": "kill failed"}
    clear_pid(pid_path)
    if db is not None:
        try:
            db.set_setting(RESTART_KEY, [])
            db.set_setting(RESTART_TS_KEY, None)
        except Exception:
            pass
    return {**out, **relaunch_desk(cwd)}


def apply(db: DB, result: dict | None = None, now: float | None = None, confirm_s: float = 0) -> dict:
    """Perform restarts described by check().

    confirm_s > 0 (CLI): before killing a hung desk, re-check every 5s for up to confirm_s; if the
    heartbeats come back (e.g. the box just resumed from a pause) nothing is killed.
    """
    result = result or check(db, now=now)
    applied = []
    if confirm_s and any(a["type"] == "kill_relaunch_desk" for a in result["actions"]):
        deadline = time.time() + confirm_s
        while time.time() < deadline:
            time.sleep(5)
            result = check(db)
            if not any(a["type"] == "kill_relaunch_desk" for a in result["actions"]):
                result["confirm_note"] = "heartbeats recovered during confirm window; no kill"
                break
    if any(a["type"] == "kill_relaunch_desk" for a in result["actions"]):
        applied.append({"type": "kill_relaunch_desk", **kill_relaunch_desk(db)})
        ap_actions = [a for a in result["actions"] if a["type"] == "restart_autopush"]
        if ap_actions:
            force = any(a.get("reason") == "stuck" for a in ap_actions)
            applied.append({"type": "relaunch_autopush", **relaunch_autopush(force=force)})
        return {**result, "applied": applied}
    agent_names = [a["agent"] for a in result["actions"] if a["type"] == "restart_agent"]
    if agent_names:
        if result.get("process_alive"):
            request_agent_restarts(db, agent_names)
            applied.append({"type": "request_agent_restarts", "agents": agent_names})
        else:
            # process dead: one desk relaunch covers everyone
            pass
    if any(a["type"] == "restart_desk" for a in result["actions"]) or (
            agent_names and not result.get("process_alive")):
        applied.append({"type": "relaunch_desk", **relaunch_desk()})
    ap_actions = [a for a in result["actions"] if a["type"] == "restart_autopush"]
    if ap_actions:
        force = any(a.get("reason") == "stuck" for a in ap_actions)
        applied.append({"type": "relaunch_autopush", **relaunch_autopush(force=force)})
    return {**result, "applied": applied}


def cli(argv: list[str] | None = None):
    ap = argparse.ArgumentParser(prog="health.py", description="STEP 12 desk health")
    ap.add_argument("cmd", choices=("check", "apply", "restart-desk"), help="check | apply | restart-desk")
    ap.add_argument("--apply", action="store_true", help="with check: also perform restarts")
    ap.add_argument("--db", default=str(DB_PATH))
    args = ap.parse_args(argv)
    db = DB(args.db)
    if args.cmd == "restart-desk":
        out = relaunch_desk()
    elif args.cmd == "apply" or (args.cmd == "check" and args.apply):
        out = apply(db, confirm_s=CONFIRM_S)
    else:
        out = check(db)
    print(json.dumps(out, indent=2, default=str))
    return 0 if out.get("ok", True) or args.cmd in ("apply", "restart-desk") or args.apply else 1


if __name__ == "__main__":
    raise SystemExit(cli())
