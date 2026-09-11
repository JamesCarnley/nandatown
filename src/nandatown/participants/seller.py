"""The stock seller: claims quote requests, applies each once, responds.

Scripted crash fault (crash_after_claim): after its first claim the
seller writes a durable crash marker, stalls past its lease, tries to
acknowledge with the now-stale fence to prove the fence holds, and exits
with code 3. The runner restarts it; the journal and the town's
redelivery finish the job on attempt two.
"""

from __future__ import annotations

import os
import sys
import time
from typing import Any

from ..client import StaleFenceError, TownClient
from .base import Journal, run_loop

CRASH_EXIT_CODE = 3


def response_id(request_id: str) -> str:
    return "r-" + request_id.removeprefix("q-")


def build_handler(client: TownClient, journal: Journal):
    def handler(claim: dict[str, Any]):
        if claim["kind"] != "quote_request":
            return "rejected", {"reason": "unknown kind"}, []
        message_id = claim["message_id"]
        if journal.seen(message_id):
            # Already applied: resend the original response (idempotent by
            # message identity, the town returns the original acceptance)
            # and say so, without applying again.
            done = journal.get(message_id)
            note = {"duplicate": True}
            if journal.unreported(message_id):
                # The application happened, but the acknowledgement that
                # carried it was fenced, so the record still lacks it.
                # Report the work actually done rather than let the
                # evidence blame the seller for the lease timing.
                note["applied"] = True
                note["total_cents"] = done["total_cents"]
            return "processed", note, [done["reply"]]
        body = claim["body"]
        total_cents = body["quantity"] * body["unit_price_cents"]
        reply = {
            "message_id": response_id(message_id),
            "to": claim["from"],
            "kind": "quote_response",
            "body": {"request_id": message_id, "sku": body["sku"],
                     "quantity": body["quantity"],
                     "total_cents": total_cents},
        }
        journal.record(message_id, {"reply": reply,
                                    "total_cents": total_cents})
        return "processed", {"applied": True, "total_cents": total_cents}, [reply]

    return handler


def crash_wrapper(client: TownClient, journal: Journal, state_dir: str,
                  lease_seconds: float, inner):
    marker = os.path.join(state_dir, "crashed-once")

    def handler(claim: dict[str, Any]):
        if not os.path.exists(marker):
            with open(marker, "w") as f:
                f.write(claim["fence"])
            time.sleep(lease_seconds + 0.5)
            try:
                client.ack(claim["message_id"], claim["fence"], "processed",
                           {"applied": True, "after_crash_stall": True})
            except StaleFenceError:
                sys.exit(CRASH_EXIT_CODE)
            # The stale fence was wrongly accepted; fail loudly.
            sys.exit(9)
        return inner(claim)

    return handler


def ack_outcome(journal: Journal):
    """Track which applications the town has actually recorded.

    An applied acknowledgement refused as a stale fence never reached
    the evidence, so the application behind it stays unreported and the
    next delivery of that work must carry it. An accepted one is in the
    record, so it must never be claimed a second time.
    """
    def on_ack(claim: dict[str, Any], note: dict[str, Any],
               accepted: bool) -> None:
        message_id = claim["message_id"]
        if not note.get("applied") or not journal.seen(message_id):
            return
        if accepted:
            journal.clear_unreported(message_id)
        else:
            journal.mark_unreported(message_id, claim["fence"])

    return on_ack


def run(client: TownClient, name: str, token: str, state_dir: str,
        fault: str, deadline_seconds: float = 60.0,
        grant_json: str | None = None) -> int:
    client.join_auto(name, token, grant_json)
    journal = Journal(os.path.join(state_dir, "journal.db"))
    handler = build_handler(client, journal)
    if fault == "crash_after_claim":
        lease = float(client.run_context.get("lease_seconds", 5.0))
        handler = crash_wrapper(client, journal, state_dir, lease, handler)
    deadline = time.time() + deadline_seconds
    run_loop(client, handler, until=lambda: time.time() > deadline,
             on_ack=ack_outcome(journal))
    return 0


def main() -> None:
    env = os.environ
    client = TownClient(env["TOWN_URL"], env["RUN_ID"])
    code = run(client, env["NAME"], env["TOKEN"], env["STATE_DIR"],
               env.get("FAULT", "none"),
               deadline_seconds=float(env.get("DEADLINE", "60")),
               grant_json=env.get("TOWN_GRANT"))
    sys.exit(code)


if __name__ == "__main__":
    main()
