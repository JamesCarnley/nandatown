"""JSON Lines records keep strings that hold Unicode line separators.

JSON allows U+2028, U+2029 and U+0085 unescaped inside a string, and
the event writer records them that way. str.splitlines() also treats
each one as a line boundary, so a reader using it cuts a single record
in two. These tests record such strings through the real writers and
real runs, then read, verify, receipt, prove, mirror and recover them.
"""

import json
import os
import sys
import threading
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import httpx
import pytest

from nandatown import __version__
from nandatown.a2a_adapter import build_agent_card
from nandatown.bundle import load_bundle, verify_bundle, write_bundle
from nandatown.cli import main
from nandatown.evaluator import EVALUATOR_VERSION, evaluate
from nandatown.mirror import mirror_bundle, recover_bundle
from nandatown.path_profiles import QUOTE_INTENT_FIELDS
from nandatown.path_runner import run_path_test
from nandatown.receipt import render_proof, verify_receipt
from nandatown.records import RunRecord, fingerprint
from nandatown.runner import run_town
from nandatown.sim.runner import run_lab
from nandatown.sim.validators import evaluate_scenario

from test_evaluator import clean_events, profile

SEPARATORS = {"U+2028": "\u2028", "U+2029": "\u2029", "U+0085": "\u0085"}
ALL_SEPARATORS = "".join(SEPARATORS.values())
each_separator = pytest.mark.parametrize(
    "separator", list(SEPARATORS.values()), ids=list(SEPARATORS))
SUBJECT = "http://testserver"

# A standard-library Track seller, like examples/byoa_seller.py, whose
# processed acknowledgement note also carries the JSON object in argv[1].
NOTED_SELLER = '''
import json, os, sys, time, urllib.request

TOWN, RUN = os.environ["TOWN_URL"], os.environ["RUN_ID"]
EXTRA_NOTE = json.loads(sys.argv[1])
session = None


def call(method, path, body=None):
    data = None if body is None else json.dumps(body).encode()
    request = urllib.request.Request(f"{TOWN}/runs/{RUN}{path}", data=data,
                                     method=method)
    request.add_header("Content-Type", "application/json")
    if session:
        request.add_header("X-Town-Session", session)
    with urllib.request.urlopen(request, timeout=10) as response:
        return (None if response.status == 204
                else json.loads(response.read() or b"{}"))


session = call("POST", "/join", {"name": os.environ["NAME"],
                                 "token": os.environ["TOKEN"]})["session"]
deadline = time.time() + float(os.environ.get("DEADLINE", "45"))
replies = {}
while time.time() < deadline:
    call("GET", "/inbox/notify?wait=0.4")
    claim = call("POST", "/inbox/claim")
    if claim is None or claim["kind"] != "quote_request":
        continue
    message_id = claim["message_id"]
    ack = {"message_id": message_id, "fence": claim["fence"],
           "status": "processed", "note": {"duplicate": True}}
    if message_id not in replies:
        total = claim["body"]["quantity"] * claim["body"]["unit_price_cents"]
        replies[message_id] = {
            "message_id": "r-" + message_id.removeprefix("q-"),
            "to": claim["from"], "kind": "quote_response",
            "body": {"request_id": message_id, "total_cents": total}}
        ack["note"] = {"applied": True, "total_cents": total, **EXTRA_NOTE}
    call("POST", "/messages", replies[message_id])
    call("POST", "/inbox/ack", ack)
'''


def recorded_bytes(directory, name="events.jsonl"):
    return (Path(directory) / name).read_bytes()


def track_bundle(directory, events, intents):
    p = profile()
    run = RunRecord(
        run_id="run-1", profile_name=p.name,
        profile_fingerprint=fingerprint(p.model_dump()), created_at=1.0,
        participants=[{"name": "buyer", "role": "buyer"},
                      {"name": "seller", "role": "seller"}],
        releases={"nandatown": __version__, "evaluator": EVALUATOR_VERSION},
    )
    write_bundle(str(directory), p, run, intents, events,
                 evaluate(p, "run-1", events))
    return str(directory)


def assert_mirrors_and_recovers(directory, tmp_path):
    fingerprint_ = load_bundle(directory)["manifest"]["bundle_fingerprint"]
    mirror = str(tmp_path / "mirror")
    mirror_bundle(directory, mirror)
    restored = recover_bundle(fingerprint_, [mirror], str(tmp_path / "fresh"))
    assert verify_bundle(restored) == []
    assert recorded_bytes(restored) == recorded_bytes(directory)
    return restored


@each_separator
def test_track_bundle_reads_strings_holding_line_separators(
        tmp_path, separator):
    memo = f"first{separator}second"
    events = clean_events()
    events[6].detail["note"]["memo"] = memo
    intents = [{"intent_id": "in-1", "run_id": "run-1", "at": 1.0,
                "actor": "seller", "action": "ack",
                "payload": {"message_id": "q-1", "memo": memo}}]

    directory = track_bundle(tmp_path / "bundle", events, intents)

    assert separator.encode() in recorded_bytes(directory)
    bundle = load_bundle(directory)
    assert [e.model_dump() for e in bundle["events"]] == \
        [e.model_dump() for e in events]
    assert bundle["intents"][0].payload["memo"] == memo
    assert verify_bundle(directory) == []


def test_jsonl_records_still_tolerate_crlf_and_blank_lines(tmp_path):
    events = clean_events()
    intents = [{"intent_id": "in-1", "run_id": "run-1", "at": 1.0,
                "actor": "buyer", "action": "send", "payload": {}}]
    directory = track_bundle(tmp_path / "bundle", events, intents)
    for name in ("intents.jsonl", "events.jsonl"):
        path = Path(directory) / name
        path.write_bytes(path.read_bytes().replace(b"\n", b"\r\n") + b"\r\n")

    bundle = load_bundle(directory)

    assert [e.model_dump() for e in bundle["events"]] == \
        [e.model_dump() for e in events]
    assert len(bundle["intents"]) == 1


@each_separator
def test_lab_bundle_with_line_separators_verifies_mirrors_and_recovers(
        tmp_path, separator):
    source, _ = run_lab("voting", str(tmp_path / "runs"))
    original = load_bundle(source)
    events = original["events"]
    label = f"town{separator}hall"
    events[0].detail["label"] = label
    result = evaluate_scenario(original["profile"], original["run"].run_id,
                               events)
    directory = str(tmp_path / "lab")
    write_bundle(directory, original["profile"], original["run"],
                 [i.model_dump() for i in original["intents"]], events,
                 result, mode="lab")

    assert separator.encode() in recorded_bytes(directory)
    assert load_bundle(directory)["events"][0].detail["label"] == label
    assert verify_bundle(directory) == []
    restored = assert_mirrors_and_recovers(directory, tmp_path)
    assert load_bundle(restored)["events"][0].detail["label"] == label


class SeparatorAgent:
    """A well-formed A2A quote agent whose card name and task ids hold
    the given separator characters."""

    def __init__(self, base_url, separators):
        self.base_url = base_url
        self.separators = separators
        self.tasks = {}

    def card(self):
        card = build_agent_card(self.base_url)
        card["name"] = f"Acme{self.separators}Seller"
        return card

    def rpc(self, envelope):
        params = envelope.get("params", {})
        if envelope.get("method") == "tasks/get":
            task = self.tasks[params["id"]]
        else:
            parts = params["message"]["parts"]
            order = json.loads(next(p["text"] for p in parts
                                    if p.get("kind") == "text"))
            quote = {"request_id": order["request_id"],
                     "total_cents": order["quantity"]
                     * order["unit_price_cents"]}
            if any(f in order for f in ("color", "merchant_id", "currency")):
                quote.update({f: order[f] for f in QUOTE_INTENT_FIELDS
                              if f in order})
            task_id = f"task{self.separators}{len(self.tasks) + 1}"
            task = {"id": task_id, "kind": "task",
                    "status": {"state": "completed"},
                    "artifacts": [{"parts": [{"kind": "text",
                                              "text": json.dumps(quote)}]}]}
            self.tasks[task_id] = task
        return {"jsonrpc": "2.0", "id": envelope.get("id"), "result": task}

    def handle(self, request):
        if request.method == "GET":
            return httpx.Response(200, json=self.card())
        return httpx.Response(200, json=self.rpc(json.loads(request.content)))


@contextmanager
def serve_separator_agent(separators):
    """Serve SeparatorAgent over loopback HTTP from a standard-library
    server, so the Path CLI talks to it through a real socket."""

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass

        def _send(self, document):
            body = json.dumps(document, ensure_ascii=False).encode("utf-8")
            self.send_response(200)
            self.send_header("content-type", "application/json")
            self.send_header("content-length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            self._send(self.server.agent.card())

        def do_POST(self):
            length = int(self.headers.get("content-length", 0))
            self._send(self.server.agent.rpc(json.loads(
                self.rfile.read(length))))

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    url = f"http://127.0.0.1:{server.server_address[1]}"
    server.agent = SeparatorAgent(url, separators)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield url
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


@each_separator
def test_path_bundle_with_separator_in_card_name_proves_and_recovers(
        tmp_path, separator):
    agent = SeparatorAgent(SUBJECT, separator)
    with httpx.Client(base_url=SUBJECT,
                      transport=httpx.MockTransport(agent.handle)) as http:
        directory, result = run_path_test(
            SUBJECT, str(tmp_path / "runs"), http=http,
            pin_card_digest=fingerprint(agent.card()))

    assert result.verdict == "passed", \
        [(s.name, s.status, s.note) for s in result.stages]
    assert all(stage.status == "passed" for stage in result.stages)
    assert f"Acme{separator}Seller".encode() in recorded_bytes(directory)
    assert verify_bundle(directory) == []
    ok, text = render_proof(directory)
    assert ok, text
    receipt = os.path.join(directory, "receipt.json")
    assert verify_receipt(receipt, directory) == []
    restored = assert_mirrors_and_recovers(directory, tmp_path)
    assert verify_receipt(os.path.join(restored, "receipt.json"),
                          restored) == []


def test_path_cli_run_against_agent_with_separators_in_card_name(
        tmp_path, capsys):
    with serve_separator_agent(ALL_SEPARATORS) as url:
        code = main(["test-agent", "--url", url, "--out", str(tmp_path)])
    out = capsys.readouterr().out
    assert code == 0, out
    directory = out.split("Evidence bundle: ")[1].strip()
    assert f"Acme{ALL_SEPARATORS}Seller".encode() in recorded_bytes(directory)
    assert os.path.isfile(os.path.join(directory, "report.md"))
    assert os.path.isfile(os.path.join(directory, "attestation.json"))
    assert main(["verify", directory]) == 0


def test_track_run_whose_seller_note_holds_line_separators(tmp_path):
    memo = f"Acme{ALL_SEPARATORS}Seller"
    script = tmp_path / "noted_seller.py"
    script.write_text(NOTED_SELLER)
    seller = [sys.executable, str(script), json.dumps({"memo": memo})]

    bundle_dir, result = run_town("quote-clean", str(tmp_path / "runs"),
                                  external={"seller": seller})

    assert result.verdict == "passed", \
        [(s.name, s.status, s.note) for s in result.stages]
    assert memo.encode() in recorded_bytes(bundle_dir)
    bundle = load_bundle(bundle_dir)
    seller_acks = [e for e in bundle["events"]
                   if e.kind == "ack_recorded" and e.observer == "seller"]
    assert seller_acks[0].detail["note"]["memo"] == memo
    assert os.path.isfile(os.path.join(bundle_dir, "report.md"))
    assert verify_bundle(bundle_dir) == []
