"""A journal outlives the release that wrote it.

Normal runs get a fresh per-run state directory, but an operator
running an external agent supplies their own and keeps it. Opening such
a journal after an upgrade must work, not crash the participant.
"""

import json
import sqlite3
import time

from nandatown.participants.base import Journal

OLD_RESULT = {"reply": {"message_id": "r-1"}, "total_cents": 3990}


def write_old_journal(path, message_id="q-1", result=OLD_RESULT):
    """A journal exactly as the previous release wrote it: no mark."""
    conn = sqlite3.connect(path)
    with conn:
        conn.execute(
            "CREATE TABLE processed ("
            " message_id TEXT PRIMARY KEY, result_json TEXT NOT NULL,"
            " at REAL NOT NULL)"
        )
        conn.execute(
            "INSERT INTO processed (message_id, result_json, at)"
            " VALUES (?,?,?)",
            (message_id, json.dumps(result), time.time()),
        )
    conn.close()


def test_old_journal_upgrades_in_place(tmp_path):
    """An older journal gains the column and stays readable.

    Its existing rows read as reported: those applications were
    acknowledged under the older code, or are gone with their run.
    """
    path = str(tmp_path / "journal.db")
    write_old_journal(path)

    journal = Journal(path)
    assert journal.seen("q-1") is True
    assert journal.get("q-1") == OLD_RESULT
    assert journal.unreported("q-1") is False

    journal.record("q-2", {"reply": {"message_id": "r-2"},
                           "total_cents": 100}, unreported=True)
    assert journal.unreported("q-2") is True
    journal.mark_reported("q-2")
    assert journal.unreported("q-2") is False
    assert journal.seen("q-1") is True


def test_reopening_a_migrated_journal_keeps_its_marks(tmp_path):
    """The migration runs once and changes nothing on a later open."""
    path = str(tmp_path / "journal.db")
    first = Journal(path)
    first.record("q-1", {"total_cents": 3990}, unreported=True)
    first.record("q-2", {"total_cents": 10})

    reopened = Journal(path)
    assert reopened.unreported("q-1") is True
    assert reopened.unreported("q-2") is False

    reopened.mark_reported("q-1")
    assert Journal(path).unreported("q-1") is False
    assert Journal(path).get("q-1") == {"total_cents": 3990}
