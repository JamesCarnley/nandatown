"""What the seller's evidence says when its own ack was fenced.

A stale fence is correct fencing, but the work behind the refused
acknowledgement really happened. These tests pin which redeliveries
carry an application record and which must not.
"""

import time

from fastapi.testclient import TestClient

from nandatown.client import TownClient
from nandatown.participants import seller

from test_participants import ADMIN, make_town


def send_quote_request(app, run_id, tokens):
    buyer_client = TownClient("http://testserver", run_id,
                              http=TestClient(app))
    buyer_client.join("buyer", tokens["buyer"])
    buyer_client.send(message_id="q-1", to="seller", kind="quote_request",
                      body={"sku": "widget", "quantity": 2,
                            "unit_price_cents": 1995})


def seller_notes(admin, run_id):
    events = admin.get(f"/runs/{run_id}/events", headers=ADMIN).json()["events"]
    notes = [e["detail"]["note"] for e in events
             if e["kind"] == "ack_recorded" and e["observer"] == "seller"]
    return events, notes


def test_fenced_application_is_reported_on_redelivery(tmp_path):
    """A fenced applied acknowledgement plus a journal-backed
    redelivery still yields exactly one application record.

    The seller applied the work once; its acknowledgement lost the race
    with the lease. The record must carry the application it really
    performed, not blame the seller for the town's timing.
    """
    app, admin, run_id, tokens = make_town(tmp_path, lease=0.5)
    send_quote_request(app, run_id, tokens)

    seller_client = TownClient("http://testserver", run_id,
                               http=TestClient(app))
    inner_ack = seller_client.ack
    stalled = []

    def slow_ack(message_id, fence, status, note=None):
        # Stall once between the apply and its acknowledgement, so the
        # lease ends first and the town fences the applied ack.
        if not stalled and (note or {}).get("applied"):
            stalled.append(True)
            time.sleep(0.7)
        return inner_ack(message_id, fence, status, note)

    seller_client.ack = slow_ack
    seller_dir = tmp_path / "seller"
    seller_dir.mkdir()
    seller.run(seller_client, "seller", tokens["seller"], str(seller_dir),
               "none", deadline_seconds=3.0)

    events, notes = seller_notes(admin, run_id)
    assert [e for e in events if e["kind"] == "stale_fence_rejected"], events
    applied = [n for n in notes if n.get("applied")]
    assert len(applied) == 1, notes
    assert applied[0]["duplicate"] is True
    assert applied[0]["total_cents"] == 3990


def test_accepted_application_is_not_reported_twice(tmp_path):
    """When the applied acknowledgement was accepted, a later duplicate
    delivery is only a duplicate. Re-reporting the application there
    would read as two applications of the same work.
    """
    app, admin, run_id, tokens = make_town(tmp_path,
                                           fault="duplicate_delivery")
    send_quote_request(app, run_id, tokens)

    seller_client = TownClient("http://testserver", run_id,
                               http=TestClient(app))
    seller_dir = tmp_path / "seller"
    seller_dir.mkdir()
    seller.run(seller_client, "seller", tokens["seller"], str(seller_dir),
               "none", deadline_seconds=3.0)

    events, notes = seller_notes(admin, run_id)
    assert [e for e in events if e["kind"] == "duplicate_offered"], events
    applied = [n for n in notes if n.get("applied")]
    duplicates = [n for n in notes if n.get("duplicate")]
    assert len(applied) == 1, notes
    assert len(duplicates) == 1, notes
    assert "applied" not in duplicates[0]
