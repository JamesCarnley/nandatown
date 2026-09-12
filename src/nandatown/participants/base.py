"""Shared participant machinery: durable journal and inbox loop.

The journal is the participant's durable record of work already
processed. Duplicate delivery is possible by design, so the journal is
what lets a participant recognize work it already handled and apply an
effect exactly once on its own side.

A journal row also carries whether the town has recorded that
application yet. The mark is written with the application itself, in the
same statement, so a participant that dies before acknowledging still
finds it on the next delivery. Only an accepted acknowledgement clears
it.
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
                " at REAL NOT NULL,"
                " unreported INTEGER NOT NULL DEFAULT 0)"
            )
            self._migrate(conn)

    @classmethod
    def _migrate(cls, conn) -> None:
        """Bring a journal created by an earlier release up to date.

        CREATE TABLE IF NOT EXISTS leaves an existing table alone, so a
        journal an operator keeps across an upgrade arrives without the
        mark. Its rows read as reported, which is what they are: those
        applications were acknowledged under the older code, or are gone
        with the run that made them.
        """
        columns = {row[1] for row in
                   conn.execute("PRAGMA table_info(processed)")}
        if "unreported" not in columns:
            conn.execute("ALTER TABLE processed ADD COLUMN unreported"
                         " INTEGER NOT NULL DEFAULT 0")

    def _conn(self):
        conn = sqlite3.connect(self.path, timeout=10.0)
        return conn

    def seen(self, message_id: str) -> bool:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT 1 FROM processed WHERE message_id=?", (message_id,)
            ).fetchone()
        return row is not None

    def record(self, message_id: str, result: dict[str, Any],
               unreported: bool = False) -> None:
        """Write the application and whether an ack of it was accepted.

        Both facts land in one statement on purpose: a participant that
        dies between applying the work and acknowledging it must still
        learn, on the next delivery, that it never saw an acknowledgement
        of this application accepted.

        That is what the mark means, and it is weaker than "the record
        lacks the application": the acknowledgement may have been
        accepted and the participant died before hearing so. Only the
        town can tell those apart.
        """
        with self._conn() as conn:
            conn.execute(
                "INSERT OR IGNORE INTO processed (message_id, result_json,"
                " at, unreported) VALUES (?,?,?,?)",
                (message_id, json.dumps(result), time.time(),
                 1 if unreported else 0),
            )

    def get(self, message_id: str) -> dict[str, Any] | None:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT result_json FROM processed WHERE message_id=?",
                (message_id,),
            ).fetchone()
        return json.loads(row[0]) if row else None

    def unreported(self, message_id: str) -> bool:
        """Whether this participant still has to report this application.

        False covers three cases and does not distinguish them: an ack
        was accepted, this journal has never seen the message, or the row
        predates the mark. True says only that this participant never saw
        an ack of it accepted, which is weaker than the town holding no
        record: the ack may have been accepted and the process died
        before hearing so. Only the town can tell those apart.
        """
        with self._conn() as conn:
            row = conn.execute(
                "SELECT unreported FROM processed WHERE message_id=?",
                (message_id,),
            ).fetchone()
        return bool(row and row[0])

    def mark_reported(self, message_id: str) -> None:
        """The record now carries this application; stop reporting it."""
        with self._conn() as conn:
            conn.execute(
                "UPDATE processed SET unreported=0 WHERE message_id=?",
                (message_id,),
            )


Handler = Callable[[dict[str, Any]], tuple[str, dict[str, Any], list[dict[str, Any]]]]
AckAccepted = Callable[[dict[str, Any], dict[str, Any]], None]


def run_loop(client: TownClient, handler: Handler,
             until: Callable[[], bool], poll_interval: float = 0.3,
             on_ack_accepted: AckAccepted | None = None) -> None:
    """Wait for a wake-up hint, then always check the durable inbox.

    The hint is never the only copy of the work: a lost wake-up must not
    lose inbox work, so the loop claims on every pass regardless of the
    hint. The handler returns (ack_status, note, replies); replies are
    sent before the acknowledgement so a crash after sending is
    recoverable through redelivery and the journal.

    on_ack_accepted, when given, is called after an acknowledgement the
    town accepted. That is the only signal a participant gets that an
    assertion of its own reached the record.
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
            # protects against a second application. A refusal needs no
            # bookkeeping; anything this ack asserted stays unreported
            # until one is accepted.
            #
            # A stale fence says only "this fence can no longer
            # acknowledge". Today that always means the ack never landed,
            # because the sole retry is on a 503 the town raises before
            # it commits. If a retry could ever follow a lost success
            # response, it would be fenced too, and reading that as
            # "never landed" would report the application a second time.
            continue
        if on_ack_accepted is not None:
            on_ack_accepted(claim, note)
