"""What the seller's evidence says when no ack of its work was accepted.

A stale fence is correct fencing, and a killed process is a real
failure, but the work behind the missing acknowledgement really
happened. These tests pin which redeliveries carry an application
record and which must not.
"""

import time

import pytest
from fastapi.testclient import TestClient

from nandatown.client import TownClient
from nandatown.participants import seller

from test_participants import ADMIN, make_town, quote_profile


class Killed(Exception):
    """Stand-in for the process dying before its acknowledgement lands."""


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


def run_seller(app, run_id, tokens, state_dir, ack=None, deadline=3.0):
    client = TownClient("http://testserver", run_id, http=TestClient(app))
    if ack is not None:
        client.ack = ack(client.ack)
    seller.run(client, "seller", tokens["seller"], str(state_dir), "none",
               deadline_seconds=deadline)


def test_fenced_application_is_reported_on_redelivery(tmp_path):
    """A fenced applied acknowledgement plus a journal-backed
    redelivery still yields exactly one application record.

    The seller applied the work once; its acknowledgement lost the race
    with the lease. The record must carry the application it really
    performed, not blame the seller for the town's timing.
    """
    app, admin, run_id, tokens = make_town(tmp_path, lease=0.5)
    send_quote_request(app, run_id, tokens)
    seller_dir = tmp_path / "seller"
    seller_dir.mkdir()

    def stall_once(inner):
        stalled = []

        def slow_ack(message_id, fence, status, note=None):
            # Stall once between the apply and its acknowledgement, so the
            # lease ends first and the town fences the applied ack.
            if not stalled and (note or {}).get("applied"):
                stalled.append(True)
                time.sleep(0.7)
            return inner(message_id, fence, status, note)

        return slow_ack

    run_seller(app, run_id, tokens, seller_dir, ack=stall_once)

    events, notes = seller_notes(admin, run_id)
    assert [e for e in events if e["kind"] == "stale_fence_rejected"], events
    applied = [n for n in notes if n.get("applied")]
    assert len(applied) == 1, notes
    assert applied[0]["duplicate"] is True
    assert applied[0]["total_cents"] == 3990


def test_crash_between_apply_and_ack_is_reported(tmp_path):
    """A seller killed after applying but before its acknowledgement
    lands still reports the application on redelivery.

    Nothing observed the failure, so nothing could have written the mark
    afterwards: it has to have been committed with the application.
    """
    app, admin, run_id, tokens = make_town(tmp_path, lease=0.5)
    send_quote_request(app, run_id, tokens)
    seller_dir = tmp_path / "seller"
    seller_dir.mkdir()

    def die(inner):
        def dying_ack(message_id, fence, status, note=None):
            raise Killed(message_id)

        return dying_ack

    with pytest.raises(Killed):
        run_seller(app, run_id, tokens, seller_dir, ack=die)

    # The dead process still holds the lease; wait for it to end so the
    # town redelivers the request to the restarted seller.
    time.sleep(0.6)
    run_seller(app, run_id, tokens, seller_dir)

    events, notes = seller_notes(admin, run_id)
    applied = [n for n in notes if n.get("applied")]
    assert len(applied) == 1, notes
    assert applied[0]["duplicate"] is True
    assert applied[0]["total_cents"] == 3990


def test_repeated_fences_still_yield_one_application_record(tmp_path):
    """Two acknowledgements refused in a row: the seller keeps carrying
    the application until one is accepted, and the record ends with
    exactly one.
    """
    app, admin, run_id, tokens = make_town(tmp_path, lease=0.5)
    send_quote_request(app, run_id, tokens)
    seller_dir = tmp_path / "seller"
    seller_dir.mkdir()

    def stall_twice(inner):
        stalls = []

        def slow_ack(message_id, fence, status, note=None):
            if len(stalls) < 2 and (note or {}).get("applied"):
                stalls.append(True)
                time.sleep(0.7)
            return inner(message_id, fence, status, note)

        return slow_ack

    run_seller(app, run_id, tokens, seller_dir, ack=stall_twice, deadline=4.0)

    events, notes = seller_notes(admin, run_id)
    fences = [e for e in events if e["kind"] == "stale_fence_rejected"]
    assert len(fences) >= 2, events
    applied = [n for n in notes if n.get("applied")]
    assert len(applied) == 1, notes


def test_accepted_application_is_not_reported_twice(tmp_path):
    """When the applied acknowledgement was accepted, a later duplicate
    delivery is only a duplicate. Re-reporting the application there
    would read as two applications of the same work.
    """
    app, admin, run_id, tokens = make_town(tmp_path,
                                           fault="duplicate_delivery")
    send_quote_request(app, run_id, tokens)
    seller_dir = tmp_path / "seller"
    seller_dir.mkdir()

    run_seller(app, run_id, tokens, seller_dir)

    events, notes = seller_notes(admin, run_id)
    assert [e for e in events if e["kind"] == "duplicate_offered"], events
    applied = [n for n in notes if n.get("applied")]
    duplicates = [n for n in notes if n.get("duplicate")]
    assert len(applied) == 1, notes
    assert len(duplicates) == 1, notes
    assert "applied" not in duplicates[0]


def test_crash_after_an_accepted_ack_is_not_two_applications(tmp_path):
    """The town recorded the application; only the seller forgot.

    An unreported mark means this seller never saw an acknowledgement
    accepted, which is not the same as the town holding no record. Here
    the acknowledgement was accepted and the process died before the
    mark could be cleared, so re-reporting on the duplicate delivery
    would make one application read as two.
    """
    app, admin, run_id, tokens = make_town(tmp_path,
                                           fault="duplicate_delivery")
    send_quote_request(app, run_id, tokens)
    seller_dir = tmp_path / "seller"
    seller_dir.mkdir()

    def die_after_accept(inner):
        def ack(message_id, fence, status, note=None):
            result = inner(message_id, fence, status, note)
            assert result["recorded"] is True
            raise Killed("the acknowledgement landed; the mark did not")

        return ack

    with pytest.raises(Killed):
        run_seller(app, run_id, tokens, seller_dir, ack=die_after_accept)
    run_seller(app, run_id, tokens, seller_dir)

    events, notes = seller_notes(admin, run_id)
    assert [e for e in events if e["kind"] == "duplicate_offered"], events
    applied = [n for n in notes if n.get("applied")]
    assert len(applied) == 1, notes
    duplicates = [n for n in notes if n.get("duplicate")]
    assert len(duplicates) == 1, notes
    assert "applied" not in duplicates[0], notes


def test_a_run_ending_that_way_still_passes(tmp_path):
    """The evidence the evaluator reads, not just the notes.

    A false second application shows up as a failed run, so the buyer's
    side is completed here and the recorded events are evaluated.
    """
    from nandatown.evaluator import evaluate
    from nandatown.records import TownEvent

    app, admin, run_id, tokens = make_town(tmp_path,
                                           fault="duplicate_delivery")
    send_quote_request(app, run_id, tokens)
    seller_dir = tmp_path / "seller"
    seller_dir.mkdir()

    def die_after_accept(inner):
        def ack(message_id, fence, status, note=None):
            result = inner(message_id, fence, status, note)
            raise Killed("the acknowledgement landed; the mark did not")

        return ack

    with pytest.raises(Killed):
        run_seller(app, run_id, tokens, seller_dir, ack=die_after_accept)
    run_seller(app, run_id, tokens, seller_dir)

    buyer = TownClient("http://testserver", run_id, http=TestClient(app))
    buyer.join("buyer", tokens["buyer"])
    claim, deadline = None, time.time() + 5
    while claim is None and time.time() < deadline:
        buyer.notify(wait=0.2)
        claim = buyer.claim()
    assert claim is not None
    buyer.ack(claim["message_id"], claim["fence"], "processed",
              {"correct": claim["body"]["total_cents"] == 3990,
               "total_cents": claim["body"]["total_cents"]})

    events, _ = seller_notes(admin, run_id)
    result = evaluate(quote_profile("duplicate_delivery"), run_id,
                      [TownEvent.model_validate(e) for e in events])
    detail = [(s.name, s.status, s.note) for s in result.stages]
    processed = next(s for s in result.stages if s.name == "processed")
    assert "applied 2 times" not in (processed.note or ""), detail
    assert processed.status == "passed", detail
