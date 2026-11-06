"""bus.py — in-process async pub/sub between agents (STEP 5). Agents talk only through this bus and db.

Topics (publisher -> subscriber):
  candidate  Scanner  -> Auditor    Scanner OUTPUT JSON, verdict "pass" only
  audited    Auditor  -> Analyst    Auditor OUTPUT JSON, passed=true only (+ "scanner": Scanner JSON)
  signal     Analyst  -> Executor   Analyst OUTPUT JSON, passed=true only (+ "ctx": scanner/auditor/market)
  fill       Executor -> Risk       Executor OUTPUT JSON, status "bought" (+ "positionId"); Risk owns exits from here
  notify     any      -> notifier   {"text", "mint"?, "buttons"?: [{"text","url"} | {"text","callback"}]}
  control    notifier -> agents     {"cmd": "bought"|"skip"|"sold"|"close"|"pause"|"resume", "mint"?}
  ui         Auditor  -> app (SSE)  {"type": "audit_layer", ...}; no subscriber until the SSE step (shim)

There is no "order" topic: Executor sizes entries, Risk closes PAPER positions itself (fees.py both sides).
"""
from __future__ import annotations

import asyncio
import logging
from collections import defaultdict

log = logging.getLogger("desk.bus")

TOPICS = ("candidate", "audited", "signal", "fill", "notify", "control", "ui")


class Bus:
    def __init__(self, maxsize: int = 1000):
        self._subs: dict[str, list[asyncio.Queue]] = defaultdict(list)
        self.maxsize = maxsize
        self.sent: dict[str, int] = defaultdict(int)

    def subscribe(self, topic: str) -> asyncio.Queue:
        if topic not in TOPICS:
            raise ValueError(f"unknown bus topic {topic!r}")
        q: asyncio.Queue = asyncio.Queue(self.maxsize)
        self._subs[topic].append(q)
        return q

    def publish(self, topic: str, msg: dict) -> None:
        if topic not in TOPICS:
            raise ValueError(f"unknown bus topic {topic!r}")
        self.sent[topic] += 1
        for q in self._subs.get(topic, []):
            try:
                q.put_nowait(msg)
            except asyncio.QueueFull:
                log.warning("bus queue full on %s; dropping", topic)
