"""Disabled pump.fun worker. Reconciles the side ledger, then waits.

It does not read the network, does not publish a candidate, and does not
heartbeat. A heartbeat would make health.py treat this task as a pipeline agent.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from agents.common import Agent
from pump_exec import Ledger


def ledger_path(db_path: str) -> Path:
    return Path(db_path).with_name("pump_orders.db")


class PumpLive(Agent):
    name = "pump_live"

    async def run(self):
        path = ledger_path(self.ctx.db.path)
        # Absent file stays absent. Opening it is what creates the side ledger.
        if path.exists():
            try:
                book = Ledger(path)
                try:
                    book.reconcile()
                finally:
                    book.close()
            except Exception:
                self.log.exception("pump ledger reconcile failed closed")
        while True:
            await asyncio.sleep(3600)
