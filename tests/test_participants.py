import threading
import time

import httpx
from fastapi.testclient import TestClient

from nandatown.client import TownClient
from nandatown.coordinator import build_app
from nandatown.participants import buyer, seller
from nandatown.participants.base import Journal
from nandatown.records import TestProfile

ADMIN = {"X-Town-Admin": "secret"}


def test_journal_is_durable_across_reopen(tmp_path):
    path = str(tmp_path / "journal.db")
    j = Journal(path)
    assert j.seen("q-1") is False
    j.record("q-1", {"total_cents": 3990})
    j2 = Journal(path)
    assert j2.seen("q-1") is True
    assert j2.get("q-1") == {"total_cents": 3990}
    j2.record("q-1", {"total_cents": 1})
    assert j2.get("q-1") == {"total_cents": 3990}


def test_send_retries_one_503():
    calls = []

    def responder(request):
        calls.append(request.url.path)
        if len(calls) == 1:
            return httpx.Response(503, json={"detail": {"error": "ack_lost"}})
        return httpx.Response(202, json={"message_id": "q-1",
                                         "accepted_at": 1.0, "replay": False})

    http = httpx.Client(transport=httpx.MockTransport(responder),
                        base_url="http://town")
    client = TownClient("http://town", "run-x", http=http)
    client.session = "ses-test"
    out = client.send("q-1", "seller", "quote_request", {"quantity": 2})
    assert out["replay"] is False
    assert len(calls) == 2


def make_town(tmp_path, fault="none", lease=5.0):
    app = build_app(str(tmp_path / "town.db"), admin_token="secret")
    admin = TestClient(app)
    p = TestProfile(
        name=f"quote-{fault}",
        task={"kind": "quote", "sku": "widget", "quantity": 2,
              "unit_price_cents": 1995, "expected_total_cents": 3990},
        roles={"buyer": "buyer", "seller": "seller"},
        capabilities={"buyer": [], "seller": ["quote.read"]},
        fault=fault, lease_seconds=lease, evaluator="stage-evaluator",
    )
    r = admin.post("/runs", json={"profile": p.model_dump()}, headers=ADMIN)
    data = r.json()
    return app, admin, data["run_id"], data["join_tokens"]


def test_buyer_and_seller_complete_clean_run(tmp_path):
    app, admin, run_id, tokens = make_town(tmp_path)

    seller_client = TownClient("http://testserver", run_id,
                               http=TestClient(app))
    seller_dir = tmp_path / "seller"
    seller_dir.mkdir()
    t = threading.Thread(
        target=seller.run,
        args=(seller_client, "seller", tokens["seller"], str(seller_dir),
              "none"),
        kwargs={"deadline_seconds": 10.0},
        daemon=True,
    )
    t.start()

    buyer_client = TownClient("http://testserver", run_id,
                              http=TestClient(app))
    buyer_dir = tmp_path / "buyer"
    buyer_dir.mkdir()
    code = buyer.run(buyer_client, "buyer", tokens["buyer"], str(buyer_dir),
                     deadline_seconds=10.0)
    assert code == buyer.EXIT_CORRECT

    events = admin.get(f"/runs/{run_id}/events", headers=ADMIN).json()["events"]
    buyer_acks = [e for e in events
                  if e["kind"] == "ack_recorded" and e["observer"] == "buyer"]
    assert buyer_acks, events
    note = buyer_acks[-1]["detail"]["note"]
    assert note["correct"] is True
    assert note["total_cents"] == 3990
    seller_acks = [e for e in events
                   if e["kind"] == "ack_recorded" and e["observer"] == "seller"
                   and e["detail"]["note"].get("applied")]
    assert len(seller_acks) == 1


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
