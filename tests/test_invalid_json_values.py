"""Invalid JSON values at the coordinator's HTTP boundary.

Town cannot store NaN, Infinity, a number too large for a double such as
1e999, or a string or key with an unpaired surrogate as JSON, yet
Python's json module accepts all of them. A participant that sent one used to be accepted and
stored; exporting the run's events then failed, and the run ended in a
runner traceback with no evidence bundle. The coordinator now refuses
such a request body, and a refusal of a joined participant's action is
recorded as an intent plus an event, like any other refused action.
"""

from __future__ import annotations

import json
import os
import shlex
import sys

import pytest
from fastapi.testclient import TestClient

from nandatown.bundle import load_bundle, verify_bundle, write_bundle
from nandatown.cli import main
from nandatown.coordinator import build_app
from nandatown.evaluator import EVALUATOR_VERSION, evaluate
from nandatown.records import RunRecord, TestProfile, fingerprint
from nandatown.report import render_report

from test_evaluator import clean_events, ev
from test_evaluator import profile as quote_profile

ADMIN = {"X-Town-Admin": "secret"}
REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
INVALID_JSON_SELLER = os.path.join(REPO_ROOT, "tests", "fixtures",
                                   "invalid_json_seller.py")

# Every literal Python's json module turns into a non-finite float:
# the three non-standard constants, and numbers that overflow a double.
LITERALS = ["NaN", "Infinity", "-Infinity", "1e999", "-1e999"]

NUMBER_SENDS = {
    "body": '{"message_id": "q-1", "to": "seller",'
            ' "kind": "quote_request",'
            ' "body": {"sku": "widget", "quantity": %(literal)s}}',
    "nested_body": '{"message_id": "q-1", "to": "seller",'
                   ' "kind": "quote_request",'
                   ' "body": {"lines": [{"sku": "widget",'
                   ' "prices": [1995, %(literal)s]}]}}',
}

NUMBER_ACKS = {
    "note": '{"message_id": "q-1", "fence": "%(fence)s",'
            ' "status": "processed",'
            ' "note": {"applied": true, "confidence": %(literal)s}}',
    "nested_note": '{"message_id": "q-1", "fence": "%(fence)s",'
                   ' "status": "processed",'
                   ' "note": {"applied": true,'
                   ' "scores": [1, {"deep": [%(literal)s]}]}}',
    # Not a field the town reads: the parser refuses it all the same.
    "extra_field": '{"message_id": "q-1", "fence": "%(fence)s",'
                   ' "status": "processed", "note": {"applied": true},'
                   ' "retry_after": %(literal)s}',
}

# Unpaired surrogates as JSON escapes, in values and keys.
SURROGATE_SENDS = {
    "body_value": '{"message_id": "q-1", "to": "seller",'
                  ' "kind": "quote_request",'
                  ' "body": {"sku": "wid\\ud800get", "quantity": 2}}',
    "body_key": '{"message_id": "q-1", "to": "seller",'
                ' "kind": "quote_request",'
                ' "body": {"sku": "widget", "\\udc00": 2}}',
    "message_id": '{"message_id": "q-\\ud800", "to": "seller",'
                  ' "kind": "quote_request", "body": {"sku": "widget"}}',
}

SURROGATE_ACKS = {
    "note_value": '{"message_id": "q-1", "fence": "%(fence)s",'
                  ' "status": "processed",'
                  ' "note": {"applied": true, "label": "\\ud800"}}',
    "note_key": '{"message_id": "q-1", "fence": "%(fence)s",'
                ' "status": "processed",'
                ' "note": {"applied": true, "\\udbff": "x"}}',
    "nested_note": '{"message_id": "q-1", "fence": "%(fence)s",'
                   ' "status": "processed",'
                   ' "note": {"applied": true,'
                   ' "lines": [{"sku": ["\\udfff"]}]}}',
    # The raw bytes of a surrogate, which the standard parser also
    # decodes into an unpaired surrogate rather than refusing.
    "raw_bytes": '{"message_id": "q-1", "fence": "%(fence)s",'
                 ' "status": "processed",'
                 ' "note": {"applied": true, "label": "\ud800"}}',
}

SEND_CASES = [
    pytest.param(template % {"literal": literal}, "non_finite_number",
                 literal, id=f"{literal}-{position}")
    for position, template in NUMBER_SENDS.items() for literal in LITERALS
] + [
    pytest.param(template, "unpaired_surrogate", None,
                 id=f"surrogate-{position}")
    for position, template in SURROGATE_SENDS.items()
]

ACK_CASES = [
    pytest.param(template % {"literal": literal, "fence": "%(fence)s"},
                 "non_finite_number", literal, id=f"{literal}-{position}")
    for position, template in NUMBER_ACKS.items() for literal in LITERALS
] + [
    pytest.param(template, "unpaired_surrogate", None,
                 id=f"surrogate-{position}")
    for position, template in SURROGATE_ACKS.items()
]

BAD_VALUES = [
    pytest.param("NaN", "non_finite_number", "NaN", id="nan"),
    pytest.param('"\\ud800"', "unpaired_surrogate", None, id="surrogate"),
]


def profile() -> dict:
    return TestProfile(
        name="quote-none",
        task={"kind": "quote", "sku": "widget", "quantity": 2,
              "unit_price_cents": 1995, "expected_total_cents": 3990},
        roles={"buyer": "buyer", "seller": "seller"},
        capabilities={"buyer": [], "seller": ["quote.read"]},
        fault="none",
        lease_seconds=5.0,
        evaluator="stage-evaluator",
    ).model_dump()


@pytest.fixture()
def client(tmp_path):
    app = build_app(str(tmp_path / "town.db"), admin_token="secret")
    with TestClient(app) as c:
        yield c


def make_run(client):
    r = client.post("/runs", json={"profile": profile()}, headers=ADMIN)
    assert r.status_code == 200, r.text
    data = r.json()
    run_id = data["run_id"]
    sessions = {}
    for name, token in data["join_tokens"].items():
        j = client.post(f"/runs/{run_id}/join",
                        json={"name": name, "token": token})
        assert j.status_code == 200, j.text
        sessions[name] = {"X-Town-Session": j.json()["session"]}
    return run_id, sessions, data


def post_raw(client, path, text, headers=None):
    """Post a body as written: the test client's own encoder would
    refuse to produce NaN at all. surrogatepass lets a test put the raw
    bytes of a surrogate on the wire."""
    return client.post(path, content=text.encode("utf-8", "surrogatepass"),
                       headers={"Content-Type": "application/json",
                                **(headers or {})})


def strict_loads(text):
    def refuse(literal):
        raise AssertionError(f"export carries non-JSON literal {literal}")
    return json.loads(text, parse_constant=refuse)


def events(client, run_id):
    r = client.get(f"/runs/{run_id}/events", headers=ADMIN)
    assert r.status_code == 200, r.text
    return strict_loads(r.text)["events"]


def intents(client, run_id):
    r = client.get(f"/runs/{run_id}/intents", headers=ADMIN)
    assert r.status_code == 200, r.text
    return strict_loads(r.text)["intents"]


def send_and_claim(client, run_id, sessions):
    sent = client.post(
        f"/runs/{run_id}/messages",
        json={"message_id": "q-1", "to": "seller", "kind": "quote_request",
              "body": {"sku": "widget", "quantity": 2,
                       "unit_price_cents": 1995}},
        headers=sessions["buyer"])
    assert sent.status_code == 202, sent.text
    claimed = client.post(f"/runs/{run_id}/inbox/claim",
                          headers=sessions["seller"])
    assert claimed.status_code == 200, claimed.text
    return claimed.json()


def evidence(problem, literal):
    """What the refusal records: the problem, and a number's literal.
    A refused string is never echoed."""
    return {"problem": problem, **({"literal": literal} if literal else {})}


def assert_refused(response, problem, literal):
    assert response.status_code == 422, response.text
    detail = response.json()["detail"]
    assert detail.pop("reason")
    assert detail == {"error": "invalid_json_value",
                      **evidence(problem, literal)}


@pytest.mark.parametrize("text, problem, literal", SEND_CASES)
def test_send_with_invalid_json_value_is_refused_and_recorded(
        client, text, problem, literal):
    run_id, sessions, _ = make_run(client)

    r = post_raw(client, f"/runs/{run_id}/messages", text, sessions["buyer"])

    assert_refused(r, problem, literal)
    evs = events(client, run_id)
    refused = [(e["observer"], e["subject"], e["detail"]) for e in evs
               if e["kind"] == "invalid_json_value_rejected"]
    assert refused == [("town", "buyer",
                        {"action": "send", **evidence(problem, literal)})]
    assert "message_accepted" not in [e["kind"] for e in evs]
    assert [(i["actor"], i["action"], i["payload"])
            for i in intents(client, run_id)] == [
        ("buyer", "send", {"error": "invalid_json_value",
                           **evidence(problem, literal)})]
    # Nothing was delivered.
    assert client.post(f"/runs/{run_id}/inbox/claim",
                       headers=sessions["seller"]).status_code == 204


@pytest.mark.parametrize("text, problem, literal", ACK_CASES)
def test_ack_with_invalid_json_value_is_refused_and_recorded(
        client, text, problem, literal):
    run_id, sessions, _ = make_run(client)
    claim = send_and_claim(client, run_id, sessions)

    r = post_raw(client, f"/runs/{run_id}/inbox/ack",
                 text % {"fence": claim["fence"]}, sessions["seller"])

    assert_refused(r, problem, literal)
    evs = events(client, run_id)
    refused = [(e["observer"], e["subject"], e["detail"]) for e in evs
               if e["kind"] == "invalid_json_value_rejected"]
    assert refused == [("town", "seller",
                        {"action": "ack", **evidence(problem, literal)})]
    assert "ack_recorded" not in [e["kind"] for e in evs]
    assert [(i["action"], i["payload"]) for i in intents(client, run_id)
            if i["actor"] == "seller"] == [
        ("claim", {}),
        ("ack", {"error": "invalid_json_value",
                 **evidence(problem, literal)})]
    # The refusal leaves the claim alone: a well-formed ack still lands.
    ok = client.post(f"/runs/{run_id}/inbox/ack",
                     json={"message_id": "q-1", "fence": claim["fence"],
                           "status": "processed",
                           "note": {"applied": True}},
                     headers=sessions["seller"])
    assert ok.status_code == 200, ok.text


@pytest.mark.parametrize("bad, problem, literal", BAD_VALUES)
@pytest.mark.parametrize("route", ["unknown_session", "join", "admin_event"])
def test_refusal_without_a_participant_session_writes_no_evidence(
        client, route, bad, problem, literal):
    run_id, sessions, data = make_run(client)
    events_before = events(client, run_id)
    intents_before = intents(client, run_id)

    if route == "unknown_session":
        r = post_raw(client, f"/runs/{run_id}/messages",
                     '{"message_id": "q-1", "to": "seller",'
                     ' "kind": "quote_request", "body": {"sku": %s}}' % bad,
                     {"X-Town-Session": "not-a-session"})
    elif route == "join":
        token = data["join_tokens"]["buyer"]
        r = post_raw(client, f"/runs/{run_id}/join",
                     '{"name": "buyer", "token": "%s",'
                     ' "grant": {"issued_at": %s}}' % (token, bad))
    else:
        r = post_raw(client, f"/runs/{run_id}/events",
                     '{"observer": "runner", "kind": "note",'
                     ' "subject": "x", "detail": {"elapsed": %s}}' % bad,
                     ADMIN)

    assert_refused(r, problem, literal)
    assert events(client, run_id) == events_before
    assert intents(client, run_id) == intents_before


@pytest.mark.parametrize("field, value, problem, literal", [
    pytest.param("lease_seconds", float("inf"), "non_finite_number",
                 "Infinity", id="infinite-lease"),
    pytest.param("name", "quote-\ud800", "unpaired_surrogate", None,
                 id="surrogate-name"),
])
def test_run_creation_refuses_invalid_json_values(
        client, field, value, problem, literal):
    # json.dumps writes both as the standard parser would accept them.
    text = json.dumps({"profile": {**profile(), field: value}})

    r = post_raw(client, "/runs", text, ADMIN)

    assert_refused(r, problem, literal)


@pytest.mark.parametrize("bad, problem, literal", BAD_VALUES)
def test_finished_run_refuses_invalid_json_values_without_writing(
        client, bad, problem, literal):
    run_id, sessions, _ = make_run(client)
    assert client.post(f"/runs/{run_id}/finish",
                       headers=ADMIN).status_code == 200
    events_before = events(client, run_id)
    intents_before = intents(client, run_id)

    r = post_raw(client, f"/runs/{run_id}/messages",
                 '{"message_id": "q-1", "to": "seller",'
                 ' "kind": "quote_request", "body": {"sku": %s}}' % bad,
                 sessions["buyer"])

    assert_refused(r, problem, literal)
    assert events(client, run_id) == events_before
    assert intents(client, run_id) == intents_before


@pytest.mark.parametrize("literal, kept", [
    # 32 characters: named whole.
    pytest.param("1e" + "0" * 27 + "999", "1e" + "0" * 27 + "999",
                 id="32-chars"),
    # 33 characters: cut to 32, ending in the marker.
    pytest.param("1e" + "0" * 28 + "999", "1e" + "0" * 27 + "...",
                 id="33-chars"),
    # 200 KB: still 32.
    pytest.param("9" * 200_000 + ".0", "9" * 29 + "...", id="200k-chars"),
])
def test_refused_literal_is_named_in_at_most_32_characters(
        client, literal, kept):
    """A refused number's literal is named, but a long one is cut: an
    overflowing number costs the response, the intent and the event at
    most 32 characters, plus its full length."""
    run_id, sessions, _ = make_run(client)
    text = NUMBER_SENDS["body"] % {"literal": literal}

    r = post_raw(client, f"/runs/{run_id}/messages", text, sessions["buyer"])

    recorded = {"problem": "non_finite_number", "literal": kept}
    if kept != literal:
        recorded["literal_length"] = len(literal)
    assert r.status_code == 422, r.text
    assert len(r.content) < 1000
    detail = r.json()["detail"]
    assert detail.pop("reason")
    assert detail == {"error": "invalid_json_value", **recorded}
    assert len(detail["literal"]) <= 32
    (event,) = [e for e in events(client, run_id)
                if e["kind"] == "invalid_json_value_rejected"]
    assert event["detail"] == {"action": "send", **recorded}
    assert [(i["actor"], i["action"], i["payload"])
            for i in intents(client, run_id)] == [
        ("buyer", "send", {"error": "invalid_json_value", **recorded})]


def test_valid_json_values_and_lookalike_strings_are_stored_unchanged(
        client):
    run_id, sessions, _ = make_run(client)
    claim = send_and_claim(client, run_id, sessions)
    note_text = ('{"applied": true, "label": "NaN",'
                 ' "words": ["Infinity", "-Infinity"], "ratio": 0.1,'
                 ' "tiny": 5e-324, "huge": 1.7976931348623157e308,'
                 ' "negative_zero": -0.0,'
                 ' "big_int": 100000000000000000000000000000,'
                 ' "nested": [1, 2.5, {"x": -1e-10}],'
                 ' "paired_escape": "\\ud83d\\ude00",'
                 ' "raw_utf8": "café \U0001f600",'
                 ' "escaped_accent": "caf\\u00e9",'
                 ' "clé": "a non-ASCII key",'
                 ' "backslash_u": "\\\\ud800"}')

    r = post_raw(client, f"/runs/{run_id}/inbox/ack",
                 '{"message_id": "q-1", "fence": "%s",'
                 ' "status": "processed", "note": %s}'
                 % (claim["fence"], note_text),
                 sessions["seller"])

    assert r.status_code == 200, r.text
    note = json.loads(note_text)
    evs = events(client, run_id)
    (ack,) = [e for e in evs if e["kind"] == "ack_recorded"]
    assert json.dumps(ack["detail"]["note"]) == json.dumps(note)
    (ack_intent,) = [i for i in intents(client, run_id)
                     if i["action"] == "ack"]
    assert json.dumps(ack_intent["payload"]["note"]) == json.dumps(note)
    assert "invalid_json_value_rejected" not in [e["kind"] for e in evs]


def test_participant_sending_invalid_json_gets_an_honest_verifiable_bundle(
        tmp_path, capsys):
    """A real Track run: the seller's acknowledgements, one with an
    unpaired surrogate key and one with NaN, are refused and recorded,
    and the run still ends in a normal, verifiable bundle instead of a
    runner traceback."""
    out = tmp_path / "runs"
    command = shlex.join([sys.executable, INVALID_JSON_SELLER])

    code = main(["test-agent", "--role", "seller", "--cmd", command,
                 "--out", str(out)])

    printed = capsys.readouterr().out
    (bundle_name,) = [n for n in os.listdir(out) if n.startswith("run-")]
    bundle_dir = str(out / bundle_name)
    assert f"Evidence bundle: {bundle_dir}" in printed
    assert code == 1
    assert verify_bundle(bundle_dir) == []
    bundle = load_bundle(bundle_dir)
    result = bundle["result"]
    statuses = {s.name: s.status for s in result.stages}
    assert result.verdict == "incomplete", statuses
    assert statuses["response"] == "passed"
    assert statuses["correct"] == "passed"
    assert statuses["received"] == "not_enough_evidence"
    assert statuses["processed"] == "not_enough_evidence"
    refusals = [
        {"action": "ack", "problem": "unpaired_surrogate"},
        {"action": "ack", "problem": "non_finite_number", "literal": "NaN"},
    ]
    refused = [e for e in bundle["events"]
               if e.kind == "invalid_json_value_rejected"]
    assert all(e.observer == "town" and e.subject == "seller"
               and e.detail in refusals for e in refused)
    assert {e.detail["problem"] for e in refused} == {
        "unpaired_surrogate", "non_finite_number"}
    assert not [e for e in bundle["events"] if e.kind == "ack_recorded"
                and e.observer == "seller"]
    seller_acks = [i.payload for i in bundle["intents"]
                   if i.actor == "seller" and i.action == "ack"]
    assert seller_acks
    assert all(p in [{"error": "invalid_json_value",
                      **{k: v for k, v in r.items() if k != "action"}}
                     for r in refusals] for p in seller_acks)
    # The report says why the stages are inconclusive.
    line = f"Refused as invalid JSON values: {len(refused)}."
    assert line in printed
    with open(os.path.join(bundle_dir, "report.md")) as f:
        assert line in f.read()


def write_quote_bundle(tmp_path, events):
    p = quote_profile()
    run = RunRecord(
        run_id="run-1", profile_name=p.name,
        profile_fingerprint=fingerprint(p.model_dump()), created_at=1.0,
        participants=[{"name": "buyer", "role": "buyer"},
                      {"name": "seller", "role": "seller"}],
        # The evaluator release must follow whichever evaluator writes the
        # result, or verify reports a release mismatch after a version bump.
        releases={"nandatown": "0.2.0", "evaluator": EVALUATOR_VERSION,
                  "python": "3.11"})
    out = str(tmp_path / "bundle")
    write_bundle(out, p, run, [], events,
                 evaluate(p, "run-1", events))
    assert verify_bundle(out) == []
    return load_bundle(out)


def test_report_counts_refusals_of_invalid_json_values(tmp_path):
    events = [e for e in clean_events()
              if not (e.kind == "ack_recorded" and e.observer == "seller")]
    events += [
        ev(100, "invalid_json_value_rejected", "seller", action="ack",
           problem="non_finite_number", literal="NaN"),
        ev(101, "invalid_json_value_rejected", "seller", action="ack",
           problem="unpaired_surrogate"),
        ev(102, "grant_permission_denied", "seller", permission="send"),
    ]

    report = render_report(write_quote_bundle(tmp_path, events))

    assert "Verdict:   INCOMPLETE" in report
    assert " Refused by grant permissions: 1." in report
    assert " Refused as invalid JSON values: 2." in report
    with open(tmp_path / "bundle" / "report.md") as f:
        assert " Refused as invalid JSON values: 2." in f.read()


def test_report_without_refusals_does_not_mention_them(tmp_path):
    report = render_report(write_quote_bundle(tmp_path, clean_events()))

    assert "Refused" not in report
