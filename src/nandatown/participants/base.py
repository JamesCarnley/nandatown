"""Shared participant machinery: durable journal and inbox loop.

The journal is the participant's durable record of work already
processed. Duplicate delivery is possible by design, so the journal is
what lets a participant recognize work it already handled and apply an
effect exactly once on its own side.

The journal also remembers which of those applications the town has not
recorded yet. An acknowledgement refused as a stale fence is correct
fencing, but the work behind it really happened, so the participant must
still be able to report it when the work comes back.
"""

from __future__ import annotations

import json
import sqlite3
import time
from typing import Any, Callable

from ..client import StaleFenceError, TownClient


class Journal:
    def __init__(self, path: str):
        self.path = path
        with self._conn() as conn:
            conn.execute(
                "CREATE TABLE IF NOT EXISTS processed ("
                " message_id TEXT PRIMARY KEY, result_json TEXT NOT NULL,"
                " at REAL NOT NULL)"
            )
            conn.execute(
                "CREATE TABLE IF NOT EXISTS unreported ("
                " message_id TEXT PRIMARY KEY, fence TEXT NOT NULL,"
                " at REAL NOT NULL)"
            )

    def _conn(self):
        conn = sqlite3.connect(self.path, timeout=10.0)
        return conn

    def seen(self, message_id: str) -> bool:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT 1 FROM processed WHERE message_id=?", (message_id,)
            ).fetchone()
        return row is not None

    def record(self, message_id: str, result: dict[str, Any]) -> None:
        with self._conn() as conn:
            conn.execute(
                "INSERT OR IGNORE INTO processed (message_id, result_json, at)"
                " VALUES (?,?,?)",
                (message_id, json.dumps(result), time.time()),
            )

    def get(self, message_id: str) -> dict[str, Any] | None:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT result_json FROM processed WHERE message_id=?",
                (message_id,),
            ).fetchone()
        return json.loads(row[0]) if row else None

    def mark_unreported(self, message_id: str, fence: str) -> None:
        """Remember an application the town never recorded: the fence it
        was acknowledged under died with the lease."""
        with self._conn() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO unreported (message_id, fence, at)"
                " VALUES (?,?,?)",
                (message_id, fence, time.time()),
            )

    def unreported(self, message_id: str) -> bool:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT 1 FROM unreported WHERE message_id=?", (message_id,)
            ).fetchone()
        return row is not None

    def clear_unreported(self, message_id: str) -> None:
        """The application reached the record; stop re-reporting it."""
        with self._conn() as conn:
            conn.execute("DELETE FROM unreported WHERE message_id=?",
                         (message_id,))


Handler = Callable[[dict[str, Any]], tuple[str, dict[str, Any], list[dict[str, Any]]]]
AckOutcome = Callable[[dict[str, Any], dict[str, Any], bool], None]


def run_loop(client: TownClient, handler: Handler,
             until: Callable[[], bool], poll_interval: float = 0.3,
             on_ack: AckOutcome | None = None) -> None:
    """Wait for a wake-up hint, then always check the durable inbox.

    The hint is never the only copy of the work: a lost wake-up must not
    lose inbox work, so the loop claims on every pass regardless of the
    hint. The handler returns (ack_status, note, replies); replies are
    sent before the acknowledgement so a crash after sending is
    recoverable through redelivery and the journal.

    on_ack, when given, is told the outcome of every acknowledgement as
    on_ack(claim, note, accepted). A refusal is the only place a
    participant learns that an assertion it made never reached the
    record, which is what lets the next delivery carry it instead.
    """
    while not until():
        client.notify(wait=poll_interval)
        claim = client.claim()
        if claim is None:
            continue
        status, note, replies = handler(claim)
        for reply in replies:
            client.send(**reply)
        try:
            client.ack(claim["message_id"], claim["fence"], status, note)
        except StaleFenceError:
            # The lease ran out: the town will redeliver, and the journal
            # protects against a second application.
            if on_ack is not None:
                on_ack(claim, note, False)
            continue
        if on_ack is not None:
            on_ack(claim, note, True)
