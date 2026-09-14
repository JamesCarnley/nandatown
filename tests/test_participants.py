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


def quote_profile(fault="none", lease=5.0):
    return TestProfile(
        name=f"quote-{fault}",
        task={"kind": "quote", "sku": "widget", "quantity": 2,
              "unit_price_cents": 1995, "expected_total_cents": 3990},
        roles={"buyer": "buyer", "seller": "seller"},
        capabilities={"buyer": [], "seller": ["quote.read"]},
        fault=fault, lease_seconds=lease, evaluator="stage-evaluator",
    )


def make_town(tmp_path, fault="none", lease=5.0):
    app = build_app(str(tmp_path / "town.db"), admin_token="secret")
    admin = TestClient(app)
    p = quote_profile(fault, lease)
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

    # The seller sends its response before acknowledging the request, so the
    # buyer can return first; wait, bounded, for the seller thread's ack.
    deadline = time.monotonic() + 5.0
    while True:
        events = admin.get(f"/runs/{run_id}/events",
                           headers=ADMIN).json()["events"]
        seller_acks = [e for e in events
                       if e["kind"] == "ack_recorded"
                       and e["observer"] == "seller"
                       and e["detail"]["note"].get("applied")]
        if seller_acks or time.monotonic() > deadline:
            break
        time.sleep(0.05)
    buyer_acks = [e for e in events
                  if e["kind"] == "ack_recorded" and e["observer"] == "buyer"]
    assert buyer_acks, events
    note = buyer_acks[-1]["detail"]["note"]
    assert note["correct"] is True
    assert note["total_cents"] == 3990
    assert len(seller_acks) == 1, \
        (f"seller recorded {len(seller_acks)} applied acks within 5 s,"
         f" expected 1: {events}")
