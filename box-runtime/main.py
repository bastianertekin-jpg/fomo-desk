"""main.py — one asyncio loop: agents + telegram + Plan B writer + heartbeats.

  .venv/bin/python main.py                 # run the PAPER desk
  .venv/bin/python main.py --smoke 20      # brief run, publish once, exit (analyst forced to rules, no Telegram, no Jev)
  flags: --offline (no network), --no-telegram

Pipeline (STEP 5): Scanner -candidate-> Auditor -audited-> Analyst -signal-> Executor -fill-> Risk (exits).

STEP 12: daily() fires at desk.report_time rollover (build night report → notify → clear skips).
health.py can request in-process agent restarts via db `_restart_agents` (watch_restarts).
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import time

import httpx

from agents.analyst import Analyst
from agents.auditor import Auditor
from agents.common import Ctx
from agents.executor import Executor
from agents.pump_live import PumpLive
from agents.risk import Risk
from agents.scanner import Scanner
from api import PlanBWriter
from bus import Bus
from config import DB_PATH, load_config, setup_logging
from db import DB, next_report
import health
import report
from telegram import Telegram

log = logging.getLogger("desk.main")

AGENT_CTORS = {
    "scanner": Scanner,
    "auditor": Auditor,
    "analyst": Analyst,
    "executor": Executor,
    "risk": Risk,
}


async def heartbeats(ctx: Ctx):
    """Main heartbeat + event-loop lag probe. Never dies on a db hiccup (a dead main beat looks like a hang)."""
    while True:
        try:
            ctx.db.beat("main", "ok", f"up {int(time.time() - ctx.state['started'])}s · {ctx.cfg['desk']['mode']}")
        except Exception as e:
            log.warning("main heartbeat write failed: %s", type(e).__name__)
        every = float(ctx.cfg.get("heartbeat_every_s") or 30)
        t0 = time.monotonic()
        await asyncio.sleep(every)
        lag = time.monotonic() - t0 - every
        if lag > 5:
            log.warning("event loop lag %.1fs (something blocked the loop)", lag)


async def balance_minutely(ctx: Ctx):
    """STEP 4: balance(ts, value) every minute -> Home chart. value = PAPER equity in USD."""
    while True:
        await asyncio.sleep(ctx.cfg.get("balance_every_s", 60))
        try:
            ctx.db.add_balance(ctx.db.equity_usd())
        except Exception:
            log.exception("balance write failed")


async def sync_settings(ctx: Ctx, overrides: dict | None, every: float = 10):
    """STEP 9: re-read db settings (CLI / app / another channel may write them) into the live cfg + pause state."""
    while True:
        await asyncio.sleep(every)
        try:
            new = load_config(ctx.db, overrides=overrides)
        except Exception as e:
            log.warning("settings sync failed: %s", type(e).__name__)
            continue
        for k, v in new.items():
            ctx.cfg[k] = v
        ctx.state["paused"] = bool(new.get("paused", False))
        ctx.state["paused_agents"] = set(new.get("paused_agents") or [])


async def daily(ctx: Ctx):
    """STEP 12: at each report_time rollover, build the night report, notify, clear skips.

    Leaves `_last_report_url` for the Grok Bot night-report routine (Page write is not done here).
    """
    while True:
        rt = (ctx.cfg.get("desk") or {}).get("report_time") or "08:00"
        wait = next_report(report_time=rt) - time.time()
        if wait > 0:
            await asyncio.sleep(wait)
        try:
            data = report.build_night_report(ctx.db, ctx.cfg)
            ctx.bus.publish("notify", {"text": data["telegram_text"]})
            n = report.clear_skips(ctx.db)
            log.info("night report notified · cleared %s skips · page url left for routine", n)
        except Exception:
            log.exception("night report failed")
        # step past the boundary so the next wait targets the following report_time
        await asyncio.sleep(2)


async def _stop_task(task: asyncio.Task, timeout: float = 10.0):
    """Cancel a task and wait (bounded) for it to finish.

    Uses asyncio.wait, NOT `await task` inside try/except CancelledError: the old code swallowed
    CancelledError there, so when amain's shutdown cancelled watch_restarts mid-restart, watch_restarts
    ignored it and kept running forever -> shutdown never finished, heartbeats/publisher/telegram were
    already cancelled, process stayed alive half-dead (the "freeze").
    """
    task.cancel()
    done, _ = await asyncio.wait({task}, timeout=timeout)
    if not done:
        log.warning("task %s did not stop within %ss after cancel", task.get_name(), timeout)


def _start(registry: dict, name: str, agent):
    task = asyncio.create_task(agent.guarded(), name=name)
    registry[name] = {"task": task, "agent": agent}
    return task


async def watch_restarts(ctx: Ctx, registry: dict):
    """STEP 12: health.py writes `_restart_agents`; cancel + restart those tasks in-process.

    Pipeline agents, publisher and telegram are all in `registry`. The SAME agent object is restarted
    (keeps its bus subscriptions; recreating subscribed a second queue that then filled up and dropped).
    """
    while True:
        await asyncio.sleep(5)
        try:
            names = health.pop_restart_requests(ctx.db)
        except Exception:
            log.exception("restart request read failed")
            continue
        for name in names:
            entry = registry.get(name)
            if not entry:
                log.warning("restart requested for unknown agent %s", name)
                continue
            await _stop_task(entry["task"])
            stop = getattr(entry["agent"], "on_restart", None)
            if stop:
                try:
                    stop()
                except Exception:
                    log.exception("on_restart %s failed", name)
            _start(registry, name, entry["agent"])
            try:
                ctx.db.beat(name, "ok", "restarted by health")
            except Exception:
                pass
            log.info("restarted agent %s in-process", name)


async def supervise(registry: dict):
    """Restart any registry task that ended on its own (returned or crashed past its guard)."""
    while True:
        await asyncio.sleep(15)
        for name, entry in list(registry.items()):
            t = entry["task"]
            if t.done() and not t.cancelled() and t.exception() is not None:
                log.warning("agent %s task crashed (%r); restarting", name, t.exception())
                _start(registry, name, entry["agent"])


async def amain(args):
    setup_logging()
    db = DB(DB_PATH)
    overrides = {"analyst.mode": "rules"} if (args.smoke or args.offline) else None
    cfg = load_config(db, overrides=overrides)
    bus = Bus()
    http = None if args.offline else httpx.AsyncClient(headers={"User-Agent": "fomo-desk/0.1"}, follow_redirects=True)
    ctx = Ctx(cfg=cfg, db=db, bus=bus, http=http, offline=args.offline)
    ctx.state["paused"] = bool(cfg.get("paused", False))
    ctx.state["paused_agents"] = set(cfg.get("paused_agents") or [])  # STEP 9 "pause Scanner"
    ctx.state["no_paid_api"] = bool(args.smoke)  # smoke never calls the paid Jev API
    ctx.state["started"] = time.time()

    if not args.smoke:
        health.write_pid()

    executor = Executor(ctx)  # seeds paper balance if empty
    workers = [Scanner(ctx), Auditor(ctx), Analyst(ctx), Risk(ctx), executor]
    writer = PlanBWriter(ctx)
    log.info("desk start %s analyst=%s handle=%s", cfg["desk"]["mode"], cfg["analyst"]["mode"], cfg["desk"]["fomo_handle"])

    # Restartable workers (pipeline agents + publisher + telegram) live in `registry` and are NOT awaited by
    # the core gather below: cancelling one for a restart must never look like a desk crash.
    registry: dict = {}
    for a in workers:
        _start(registry, a.name, a)
    # Disabled. Reconciles its own file only; it is not a pipeline agent and it does not heartbeat.
    _start(registry, "pump_live", PumpLive(ctx))
    _start(registry, "publisher", writer)
    if not args.no_telegram and not args.smoke:
        _start(registry, "telegram", Telegram(ctx))
    core = [asyncio.create_task(heartbeats(ctx), name="heartbeats"),
            asyncio.create_task(balance_minutely(ctx), name="balance"),
            asyncio.create_task(daily(ctx), name="daily"),
            asyncio.create_task(sync_settings(ctx, overrides), name="settings"),
            asyncio.create_task(watch_restarts(ctx, registry), name="watch_restarts"),
            asyncio.create_task(supervise(registry), name="supervise")]

    try:
        if args.smoke:
            await asyncio.sleep(args.smoke)
            db.add_balance(db.equity_usd())
            log.info("smoke: bus traffic %s", dict(bus.sent))
            writer.publish_once()
            log.info("smoke done; published to %s", writer.root)
        else:
            # core loops never return; if one crashes, exit so health.py relaunches a clean process
            done, _ = await asyncio.wait(core, return_when=asyncio.FIRST_COMPLETED)
            for t in done:
                if not t.cancelled() and t.exception():
                    log.error("core task %s crashed: %r; exiting for relaunch", t.get_name(), t.exception())
    finally:
        everything = core + [e["task"] for e in registry.values()]
        for t in everything:
            t.cancel()
        await asyncio.wait(everything, timeout=10)
        for e in registry.values():
            stop = getattr(e["agent"], "on_restart", None)
            if stop:
                try:
                    stop()
                except Exception:
                    pass
        if http:
            try:
                await asyncio.wait_for(http.aclose(), 5)
            except Exception:
                pass
        if not args.smoke:
            health.clear_pid()


def cli():
    ap = argparse.ArgumentParser()
    ap.add_argument("--smoke", type=int, default=0, help="run N seconds (rules mode, no Telegram), publish, exit")
    ap.add_argument("--offline", action="store_true")
    ap.add_argument("--no-telegram", action="store_true")
    asyncio.run(amain(ap.parse_args()))


if __name__ == "__main__":
    cli()
