import argparse
import json
import os
import shlex
import socket
import subprocess
import sys
import time

import httpx
import pytest
from fastapi.testclient import TestClient

from nandatown.a2a_adapter import build_a2a_app, build_agent_card
from nandatown.bundle import load_bundle, verify_bundle
from nandatown.path_profiles import PATH_PROFILES, get_path_profile
from nandatown.path_runner import evaluate_path, run_path_test
from nandatown.records import TownEvent, fingerprint
from nandatown.report import render_report

SUBJECT = "http://testserver"
MISSING_REQUEST_ID = object()


def client(defect=None):
    return TestClient(build_a2a_app(SUBJECT, defect=defect))


def fulfillment_id_client(returned_request_id):
    """In-process A2A service whose fulfillment ID may differ from its order."""
    def handler(request):
        if request.method == "GET":
            return httpx.Response(200, json=build_agent_card(SUBJECT))

        message = json.loads(request.content)
        text = message["params"]["message"]["parts"][0]["text"]
        order = json.loads(text)
        fulfillment = {"total_cents": 3990}
        if returned_request_id is not MISSING_REQUEST_ID:
            fulfillment["request_id"] = (
                order["request_id"]
                if returned_request_id == "matching"
                else returned_request_id
            )
        task = {
            "id": "task-1",
            "kind": "task",
            "status": {"state": "completed"},
            "artifacts": [{"parts": [{"kind": "text",
                                         "text": json.dumps(fulfillment)}]}],
        }
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": 1,
                                         "result": task})

    return httpx.Client(base_url=SUBJECT,
                        transport=httpx.MockTransport(handler))


def stage(result, name):
    return {s.name: s for s in result.stages}[name]


def statuses(result):
    return {s.name: s.status for s in result.stages}


def start_reference_agent():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    source_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    environment = dict(os.environ, PYTHONDONTWRITEBYTECODE="1",
                       PYTHONPATH=os.path.join(source_root, "src"))
    process = subprocess.Popen(
        [sys.executable, "-m", "nandatown.cli", "a2a", "serve",
         "--port", str(port)], env=environment,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    url = f"http://127.0.0.1:{port}"
    import httpx

    for _ in range(50):
        try:
            if httpx.get(url + "/.well-known/agent-card.json",
                         trust_env=False).status_code == 200:
                return process, url
        except httpx.HTTPError:
            time.sleep(0.05)
    process.terminate()
    process.wait()
    raise RuntimeError("reference A2A agent did not start")


def test_healthy_agent_passes_the_path(tmp_path):
    bundle_dir, result = run_path_test(SUBJECT, str(tmp_path),
                                       http=client())
    s = statuses(result)
    assert result.verdict == "passed", s
    for name in ["resolution", "agent_card_retrieval",
                 "protocol_invocation", "semantic_result",
                 "duplicate_request"]:
        assert s[name] == "passed", s
    assert s["descriptor_consistency"] == "not_tested"
    assert verify_bundle(bundle_dir) == []
    report = render_report(load_bundle(bundle_dir))
    assert "already-running external agent" in report
    assert "Rerun:" in report
    assert "First broken stage" not in report


def test_pinned_digest_match_passes_descriptor(tmp_path):
    expected = fingerprint(build_agent_card(SUBJECT))
    _, result = run_path_test(SUBJECT, str(tmp_path),
                              pin_card_digest=expected, http=client())
    assert stage(result, "descriptor_consistency").status == "passed"
    assert result.verdict == "passed"


def test_card_mismatch_names_both_digests_and_halts(tmp_path):
    bundle_dir, result = run_path_test(SUBJECT, str(tmp_path),
                                       pin_card_digest="sha256:deadbeef",
                                       http=client())
    s = statuses(result)
    consistency = stage(result, "descriptor_consistency")
    assert consistency.status == "failed"
    assert "expected card sha256:deadbeef" in consistency.note
    assert "observed" in consistency.note
    assert s["protocol_invocation"] == "not_tested"
    assert s["semantic_result"] == "not_tested"
    assert result.verdict == "failed"
    report = render_report(load_bundle(bundle_dir))
    assert "First broken stage: descriptor_consistency" in report


def test_wrong_total_fails_semantics_only(tmp_path):
    _, result = run_path_test(SUBJECT, str(tmp_path),
                              http=client("wrong_total"))
    s = statuses(result)
    assert s["protocol_invocation"] == "passed"
    semantic = stage(result, "semantic_result")
    assert semantic.status == "failed"
    assert "expected total 3990, observed 4090" in semantic.note
    assert result.verdict == "failed"


@pytest.mark.parametrize(
    ("returned_request_id", "passes"),
    [
        ("matching", True),
        ("order-from-an-earlier-run", False),
        ("unrelated-request", False),
        (MISSING_REQUEST_ID, False),
        (None, False),
        (123, False),
        (["order-from-an-earlier-run"], False),
        ({"request_id": "order-from-an-earlier-run"}, False),
    ],
    ids=["matching", "stale-order", "other", "missing", "null",
         "integer", "list", "object"],
)
def test_fulfillment_request_id_must_exactly_match_issued_order_and_replay(
        tmp_path, returned_request_id, passes):
    bundle_dir, result = run_path_test(
        SUBJECT, str(tmp_path), http=fulfillment_id_client(returned_request_id))

    semantic = stage(result, "semantic_result")
    assert verify_bundle(bundle_dir) == []
    if passes:
        assert semantic.status == "passed"
        assert result.verdict == "passed"
        return

    bundle = load_bundle(bundle_dir)
    fulfillment = next(event for event in bundle["events"]
                       if event.kind == "fulfillment_observed"
                       and event.detail["attempt"] == 1)
    assert semantic.status == "failed"
    assert result.verdict == "failed"
    assert f"expected request_id {fulfillment.subject!r}" in semantic.note
    assert (f"observed request_id"
            f" {fulfillment.detail.get('request_id')!r}" in semantic.note)


def test_empty_fulfillment_subject_cannot_establish_request_correlation():
    profile = get_path_profile("a2a-capability-fulfillment@0.1")
    result = evaluate_path(profile, "path-empty-subject", [TownEvent(
        event_id="ev-1", run_id="path-empty-subject", at=0,
        observer="town-requester", kind="fulfillment_observed", subject="",
        detail={"attempt": 1, "total_cents": 3990,
                "request_id": ""},
    )])

    semantic = stage(result, "semantic_result")
    assert semantic.status == "failed"
    assert result.verdict == "failed"
    assert "expected request_id ''" in semantic.note
    assert "observed request_id ''" in semantic.note


def test_duplicate_fulfillment_exposes_idempotency_defect(tmp_path):
    _, result = run_path_test(SUBJECT, str(tmp_path),
                              http=client("duplicate_fulfillment"))
    s = statuses(result)
    assert s["semantic_result"] == "passed"
    duplicate = stage(result, "duplicate_request")
    assert duplicate.status == "failed"
    assert "second distinct fulfillment" in duplicate.note
    assert result.verdict == "failed"


def test_unreachable_endpoint_fails_retrieval_only(tmp_path):
    _, result = run_path_test("http://127.0.0.1:1", str(tmp_path))
    s = statuses(result)
    assert s["resolution"] == "passed"
    assert s["agent_card_retrieval"] == "failed"
    assert s["protocol_invocation"] == "not_tested"
    assert s["semantic_result"] == "not_tested"
    assert result.verdict == "failed"


def test_index_resolution_and_missing_pointer(tmp_path):
    index = tmp_path / "index.json"
    expected = fingerprint(build_agent_card(SUBJECT))
    index.write_text(json.dumps({"agents": {
        "maya-seller": {"url": SUBJECT, "card_digest": expected}}}))

    _, result = run_path_test(None, str(tmp_path), index_file=str(index),
                              agent_name="maya-seller", http=client())
    assert stage(result, "descriptor_consistency").status == "passed"
    assert result.verdict == "passed"

    _, missing = run_path_test(None, str(tmp_path),
                               index_file=str(index),
                               agent_name="ghost", http=client())
    s = statuses(missing)
    resolution = stage(missing, "resolution")
    assert resolution.status == "failed"
    assert "missing card pointer" in resolution.note
    assert s["agent_card_retrieval"] == "not_tested"
    assert missing.verdict == "failed"


@pytest.mark.parametrize("index_json, reason", [
    ([{"agents": {"maya-seller": {"url": SUBJECT}}}],
     "top level must be a JSON object"),
    (None, "top level must be a JSON object"),
    ({"agents": [{"name": "maya-seller", "url": SUBJECT}]},
     '"agents" must be a JSON object'),
    ({"agents": {"maya-seller": f"url {SUBJECT}"}},
     "the entry for this agent must be a JSON object"),
    ({"agents": {"maya-seller": {"url": 5}}},
     'the entry "url" must be a non-empty string'),
    ({"agents": {"maya-seller": {"url": ""}}},
     'the entry "url" must be a non-empty string'),
    ({"agents": {"maya-seller": {"url": SUBJECT, "card_digest": 5}}},
     'the entry "card_digest" must be a non-empty string'),
    ({"agents": {"maya-seller": {"url": SUBJECT, "card_digest": ""}}},
     'the entry "card_digest" must be a non-empty string'),
], ids=["top-level-list", "top-level-null", "agents-list", "entry-string",
        "url-number", "url-empty", "card-digest-number", "card-digest-empty"])
def test_malformed_index_fails_resolution_with_verifiable_bundle(
        tmp_path, index_json, reason):
    index = tmp_path / "index.json"
    index.write_text(json.dumps(index_json))

    bundle_dir, result = run_path_test(
        None, str(tmp_path / "runs"), index_file=str(index),
        agent_name="maya-seller", http=client())

    resolution = stage(result, "resolution")
    assert resolution.status == "failed"
    assert resolution.note == f"malformed index: {reason}"
    assert stage(result, "agent_card_retrieval").status == "not_tested"
    assert result.verdict == "failed"
    assert verify_bundle(bundle_dir) == []


@pytest.mark.parametrize("raw", [
    pytest.param('{"agents": {"maya-seller": {"url": "café"}}}'
                 .encode("latin-1"), id="not-utf8"),
    pytest.param(b"[" * 200_000 + b"]" * 200_000, id="nesting-too-deep"),
])
def test_unreadable_index_fails_resolution_with_verifiable_bundle(
        tmp_path, raw):
    index = tmp_path / "index.json"
    index.write_bytes(raw)

    bundle_dir, result = run_path_test(
        None, str(tmp_path / "runs"), index_file=str(index),
        agent_name="maya-seller", http=client())

    resolution = stage(result, "resolution")
    assert resolution.status == "failed"
    assert resolution.note.startswith("index unreadable: ")
    assert result.verdict == "failed"
    assert verify_bundle(bundle_dir) == []


def test_cli_malformed_index_writes_failed_resolution_bundle(tmp_path,
                                                             capsys):
    from nandatown.cli import main

    index = tmp_path / "index.json"
    index.write_text(json.dumps(
        {"agents": [{"name": "maya-seller", "url": SUBJECT}]}))

    code = main(["test-agent", "--index", str(index), "--agent-name",
                 "maya-seller", "--out", str(tmp_path / "runs")])

    out = capsys.readouterr().out
    assert code == 1
    assert 'malformed index: "agents" must be a JSON object' in out
    assert "0 of 6 path stages passed" in out
    bundle_dir = out.split("Evidence bundle: ", 1)[1].strip()
    resolution = next(s for s in load_bundle(bundle_dir)["result"].stages
                      if s.name == "resolution")
    assert resolution.status == "failed"
    assert verify_bundle(bundle_dir) == []


INVALID_URL_REASON = "invalid endpoint URL: expected an absolute http(s) URL"
INVALID_INDEX_URL_REASON = ('malformed index: the entry "url" must be an'
                            " absolute http(s) URL")
UNUSABLE_URLS = [
    pytest.param("   ", id="blank"),
    pytest.param(" http://127.0.0.1:9", id="leading-space"),
    pytest.param("https://agent.example ", id="trailing-space"),
    pytest.param("http://127.0.0.1:9\n", id="trailing-newline"),
    pytest.param("http://not a url", id="space-in-host"),
    pytest.param("not a url", id="not-a-url"),
    pytest.param("file:///etc/hosts", id="file-scheme"),
    pytest.param("ftp://agent.example", id="ftp-scheme"),
    pytest.param("//agent.example", id="no-scheme"),
    pytest.param("http://", id="no-host"),
    pytest.param("http://[::1", id="unparseable"),
    pytest.param("http://" + "a" * 1_000_000, id="one-megabyte"),
    pytest.param("\t\n", id="whitespace-only"),
    pytest.param("http://127.0.0.1:99999", id="port-99999"),
    pytest.param("http://127.0.0.1:65536", id="port-65536"),
    pytest.param("http://127.0.0.1:0", id="port-zero"),
    pytest.param("http://127.0.0.1:-1", id="port-negative"),
    pytest.param("http://[::1]:99999", id="ipv6-port-99999"),
    pytest.param("https://agent.example:" + "9" * 30, id="port-30-digits"),
    pytest.param("http://xn--.localhost:9", id="empty-a-label"),
    pytest.param("http://xn--a.localhost:9", id="undecodable-a-label"),
]
USABLE_URLS = ["http://10.0.0.5:8940", "https://agent.example",
               "http://127.0.0.1:9", "http://[::1]:8940",
               "https://agent.example:8443/a2a/", "http://127.0.0.1:1",
               "http://127.0.0.1:65535", "http://[::1]:65535",
               "http://127.0.0.1:", "http://xn--caf-dma.localhost:8940"]
MALFORMED_A_LABEL_URLS = ["http://xn--.localhost:9",
                          "http://xn--a.localhost:9"]


def _assert_resolution_refused(bundle_dir, result, reason, problems=()):
    resolution = stage(result, "resolution")
    assert resolution.status == "failed"
    assert resolution.note == reason
    assert stage(result, "agent_card_retrieval").status == "not_tested"
    assert result.verdict == "failed"
    kinds = [event.kind for event in load_bundle(bundle_dir)["events"]]
    assert "card_fetch_failed" not in kinds
    assert "card_retrieved" not in kinds
    assert verify_bundle(bundle_dir) == list(problems)


@pytest.mark.parametrize("url", UNUSABLE_URLS)
def test_unusable_url_fails_resolution_not_card_retrieval(tmp_path, url):
    """An unusable locator is the operator's, not the agent's, failure."""
    bundle_dir, result = run_path_test(url, str(tmp_path / "runs"),
                                       http=client())

    _assert_resolution_refused(bundle_dir, result, INVALID_URL_REASON)


@pytest.mark.parametrize("url", UNUSABLE_URLS)
def test_unusable_index_url_fails_resolution_not_card_retrieval(tmp_path,
                                                               url):
    index = tmp_path / "index.json"
    index.write_text(json.dumps({"agents": {"maya-seller": {"url": url}}}))

    bundle_dir, result = run_path_test(
        None, str(tmp_path / "runs"), index_file=str(index),
        agent_name="maya-seller", http=client())

    _assert_resolution_refused(bundle_dir, result, INVALID_INDEX_URL_REASON)


@pytest.mark.parametrize("url", [
    pytest.param("http://127.0.0.1:9\n", id="trailing-newline"),
    pytest.param("http://" + "a" * 1_000_000, id="one-megabyte"),
])
def test_url_httpx_rejects_fails_resolution_without_traceback(tmp_path,
                                                             url):
    """Without an injected client these reached httpx and raised."""
    bundle_dir, result = run_path_test(url, str(tmp_path / "runs"))

    _assert_resolution_refused(bundle_dir, result, INVALID_URL_REASON)


@pytest.mark.parametrize("url", MALFORMED_A_LABEL_URLS)
def test_malformed_a_label_without_a_client_fails_resolution(tmp_path, url):
    """httpx accepts these; reading the host is what raises.

    The other malformed-label test injects a client, so this is the one
    that proves the real httpx path does not traceback.
    """
    bundle_dir, result = run_path_test(url, str(tmp_path / "runs"))

    _assert_resolution_refused(bundle_dir, result, INVALID_URL_REASON)


@pytest.mark.parametrize("via", ["url", "index"])
@pytest.mark.parametrize("url", MALFORMED_A_LABEL_URLS)
def test_malformed_a_label_is_refused_before_any_request(tmp_path, url, via):
    """Reading the host of these decodes punycode, which raises.

    httpx parses them, so the refusal has to come from evaluating the host
    itself, not from parsing. The subject is never contacted.
    """
    requests = []

    def record(request):
        requests.append(request.url)
        return httpx.Response(200, json=build_agent_card(SUBJECT))

    http = httpx.Client(transport=httpx.MockTransport(record))
    index = tmp_path / "index.json"
    index.write_text(json.dumps({"agents": {"maya-seller": {"url": url}}}))

    if via == "url":
        bundle_dir, result = run_path_test(url, str(tmp_path / "runs"),
                                           http=http)
        reason = INVALID_URL_REASON
    else:
        bundle_dir, result = run_path_test(
            None, str(tmp_path / "runs"), index_file=str(index),
            agent_name="maya-seller", http=http)
        reason = INVALID_INDEX_URL_REASON

    _assert_resolution_refused(bundle_dir, result, reason)
    assert requests == []


@pytest.mark.parametrize("via", ["url", "index"])
@pytest.mark.parametrize("url", USABLE_URLS)
def test_any_absolute_http_url_passes_resolution(tmp_path, url, via):
    """Loopback, LAN and remote endpoints are all valid subjects."""
    kwargs = {}
    subject = url
    if via == "index":
        index = tmp_path / "index.json"
        index.write_text(json.dumps({"agents": {"maya-seller": {"url": url}}}))
        kwargs = {"index_file": str(index), "agent_name": "maya-seller"}
        subject = None

    _, result = run_path_test(subject, str(tmp_path / "runs"),
                              http=client(), **kwargs)

    assert stage(result, "resolution").status == "passed"
    assert stage(result, "agent_card_retrieval").status == "passed"


@pytest.mark.parametrize("url", [
    pytest.param("http://127.0.0.1:99999", id="port-99999"),
    pytest.param("http://127.0.0.1:0", id="port-zero"),
])
def test_out_of_range_port_is_not_charged_to_card_retrieval(tmp_path, url):
    """Without an injected client these failed as the agent's card fetch."""
    bundle_dir, result = run_path_test(url, str(tmp_path / "runs"))

    _assert_resolution_refused(bundle_dir, result, INVALID_URL_REASON)


def _assert_verifiable_with_receipt(bundle_dir, tmp_path):
    from nandatown.identity_portable import Keystore
    from nandatown.receipt import make_receipt

    assert verify_bundle(bundle_dir) == []
    receipt = json.loads(open(make_receipt(
        bundle_dir, keystore=Keystore(str(tmp_path / "keys")))).read())
    assert receipt["payload"]["claim"]["subject"].strip()


def _resolution_subjects(bundle_dir):
    return [event.subject for event in load_bundle(bundle_dir)["events"]
            if event.kind in ("resolution_failed", "resolution_hop")]


@pytest.mark.parametrize("url", ["", "   ", "\t\n"])
def test_blank_url_writes_a_bundle_that_verifies(tmp_path, url):
    """A blank locator must not become the recorded subject name."""
    bundle_dir, result = run_path_test(url, str(tmp_path / "runs"),
                                       http=client())

    assert stage(result, "resolution").status == "failed"
    run = load_bundle(bundle_dir)["run"]
    assert run.participants[1] == {"name": "?", "role": "subject"}
    assert run.config["subject"] in (None, "")
    assert _resolution_subjects(bundle_dir) == ["?"]
    _assert_verifiable_with_receipt(bundle_dir, tmp_path)


@pytest.mark.parametrize("url", [
    pytest.param(5, id="number"),
    pytest.param([SUBJECT], id="list"),
])
def test_non_string_url_fails_resolution_without_crashing(tmp_path, url):
    """An API caller's non-string URL once crashed recording the event."""
    bundle_dir, result = run_path_test(url, str(tmp_path / "runs"),
                                       http=client())

    _assert_resolution_refused(bundle_dir, result, INVALID_URL_REASON)
    assert load_bundle(bundle_dir)["run"].participants[1]["name"] == "?"
    assert _resolution_subjects(bundle_dir) == ["?"]
    _assert_verifiable_with_receipt(bundle_dir, tmp_path)


BLANK_AGENT_NAME_REASON = ("blank agent name: expected a non-blank name to"
                           " look up in the pinned index")
# Whitespace-only: truthy, so no caller-side "is a name given" check can
# stop one before resolution does.
WHITESPACE_AGENT_NAMES = [
    pytest.param("   ", id="spaces"),
    pytest.param("\t", id="tab"),
    pytest.param("\n", id="newline"),
    pytest.param(" \t\r\n", id="mixed"),
    pytest.param("\u00a0\u3000", id="unicode-spaces"),
]


@pytest.mark.parametrize("listed", [True, False], ids=["listed", "unlisted"])
@pytest.mark.parametrize("agent_name", WHITESPACE_AGENT_NAMES)
def test_blank_agent_name_fails_resolution_before_the_index_lookup(
        tmp_path, agent_name, listed):
    """An index may list a blank name, but a run must name its subject.

    Resolving one let a passing receipt name its subject "?".
    """
    index = tmp_path / "index.json"
    index.write_text(json.dumps({"agents": {
        (agent_name if listed else "maya-seller"): {"url": SUBJECT}}}))

    bundle_dir, result = run_path_test(
        None, str(tmp_path / "runs"), index_file=str(index),
        agent_name=agent_name, http=client())

    _assert_resolution_refused(bundle_dir, result, BLANK_AGENT_NAME_REASON)
    run = load_bundle(bundle_dir)["run"]
    assert run.participants[1] == {"name": "?", "role": "subject"}
    assert run.config["subject"] in (None, "")
    assert _resolution_subjects(bundle_dir) == ["?"]
    _assert_verifiable_with_receipt(bundle_dir, tmp_path)


@pytest.mark.parametrize("listed", [True, False], ids=["listed", "unlisted"])
@pytest.mark.parametrize("agent_name", ["", None], ids=["empty", "none"])
def test_empty_agent_name_never_resolves_an_index_entry(tmp_path,
                                                        agent_name, listed):
    """An index listing "" must not let a nameless run resolve.

    The run is either refused before it starts or fails resolution with a
    bundle that verifies; both keep the subject from going unnamed.
    """
    index = tmp_path / "index.json"
    index.write_text(json.dumps({"agents": {
        ("" if listed else "maya-seller"): {"url": SUBJECT}}}))
    runs = tmp_path / "runs"

    try:
        bundle_dir, result = run_path_test(
            None, str(runs), index_file=str(index), agent_name=agent_name,
            http=client())
    except ValueError:
        assert not runs.exists() or not any(runs.iterdir())
        return

    _assert_resolution_refused(bundle_dir, result, BLANK_AGENT_NAME_REASON)
    assert _resolution_subjects(bundle_dir) == ["?"]
    _assert_verifiable_with_receipt(bundle_dir, tmp_path)


@pytest.mark.parametrize("agent_name", [
    pytest.param(["maya-seller"], id="list"),
    pytest.param({"name": "maya-seller"}, id="object"),
    pytest.param(5, id="number"),
])
def test_non_string_agent_name_fails_resolution_without_crashing(
        tmp_path, agent_name):
    """An API caller's non-string name once crashed recording the event."""
    index = tmp_path / "index.json"
    index.write_text(json.dumps({"agents": {"maya-seller": {"url": SUBJECT}}}))

    bundle_dir, result = run_path_test(
        None, str(tmp_path / "runs"), index_file=str(index),
        agent_name=agent_name, http=client())

    _assert_resolution_refused(bundle_dir, result, BLANK_AGENT_NAME_REASON)
    run = load_bundle(bundle_dir)["run"]
    assert run.participants[1] == {"name": "?", "role": "subject"}
    assert run.config["subject"] is None
    assert _resolution_subjects(bundle_dir) == ["?"]
    _assert_verifiable_with_receipt(bundle_dir, tmp_path)


@pytest.mark.parametrize("agent_name, resolves", [
    ("maya-seller", True),
    (" maya-seller ", True),
    ("Zoë", True),
    ("nobody", False),
])
def test_non_blank_agent_name_is_the_resolution_subject_verbatim(
        tmp_path, agent_name, resolves):
    index = tmp_path / "index.json"
    index.write_text(json.dumps({"agents": {
        "maya-seller": {"url": SUBJECT}, " maya-seller ": {"url": SUBJECT},
        "Zoë": {"url": SUBJECT}}}, ensure_ascii=False),
        encoding="utf-8")

    bundle_dir, result = run_path_test(
        None, str(tmp_path / "runs"), index_file=str(index),
        agent_name=agent_name, http=client())

    assert stage(result, "resolution").status == (
        "passed" if resolves else "failed")
    assert _resolution_subjects(bundle_dir) == [agent_name]
    assert load_bundle(bundle_dir)["run"].participants[1]["name"] \
        == agent_name
    assert verify_bundle(bundle_dir) == []


def test_utf8_index_resolves_a_non_ascii_name_whatever_the_locale(tmp_path):
    """Reading the index with the locale's encoding failed it as unreadable."""
    index = tmp_path / "index.json"
    index.write_bytes(json.dumps({"agents": {"Zoë": {"url": SUBJECT}}},
                                 ensure_ascii=False).encode("utf-8"))
    assert "Zoë".encode("utf-8") in index.read_bytes()
    child = (
        "import json, locale, sys\n"
        "from nandatown.path_runner import _Recorder, _resolve\n"
        "encoding = locale.getpreferredencoding(False)\n"
        "assert not sys.flags.utf8_mode\n"
        "assert encoding.replace('-', '').lower() != 'utf8', encoding\n"
        "recorder = _Recorder('path-locale')\n"
        "url, _ = _resolve(recorder, None, sys.argv[1], 'Zo\\u00eb')\n"
        "print(json.dumps({'url': url, 'events': [\n"
        "    [e.kind, e.subject, e.detail.get('reason')]\n"
        "    for e in recorder.events]}))\n")
    source_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    environment = dict(os.environ, PYTHONDONTWRITEBYTECODE="1",
                       PYTHONPATH=os.path.join(source_root, "src"),
                       PYTHONUTF8="0", PYTHONCOERCECLOCALE="0",
                       LC_ALL="C", LANG="C")

    completed = subprocess.run(
        [sys.executable, "-c", child, str(index)], env=environment,
        capture_output=True, text=True, timeout=60)

    assert completed.returncode == 0, completed.stderr
    assert json.loads(completed.stdout) == {
        "url": SUBJECT,
        "events": [["resolution_hop", "Zoë", None]]}


def test_blank_agent_name_is_refused_before_reading_the_index(tmp_path):
    bundle_dir, result = run_path_test(
        None, str(tmp_path / "runs"),
        index_file=str(tmp_path / "missing.json"), agent_name="   ",
        http=client())

    _assert_resolution_refused(bundle_dir, result, BLANK_AGENT_NAME_REASON)


def test_subject_names_are_recorded_unchanged(tmp_path):
    """Pins run records Town already wrote with a usable locator."""
    index = tmp_path / "index.json"
    index.write_text(json.dumps({"agents": {"maya-seller": {"url": SUBJECT}}}))
    runs = str(tmp_path / "runs")
    cases = [
        ((SUBJECT, None, None), SUBJECT, SUBJECT),
        ((None, str(index), "maya-seller"), "maya-seller", "maya-seller"),
        ((None, None, None), "?", None),
    ]
    for (url, index_file, agent_name), name, subject in cases:
        bundle_dir, _ = run_path_test(url, runs, index_file=index_file,
                                      agent_name=agent_name, http=client())
        run = load_bundle(bundle_dir)["run"]
        assert run.participants[1] == {"name": name, "role": "subject"}
        assert run.config["subject"] == subject
        assert verify_bundle(bundle_dir) == []


@pytest.mark.parametrize("argv", [
    pytest.param(["--url", "   "], id="blank-url"),
    pytest.param(["--index", "INDEX", "--agent-name", "   "],
                 id="blank-agent-name"),
    pytest.param(["--index", "BLANK_INDEX", "--agent-name", "   "],
                 id="blank-agent-name-listed"),
])
def test_cli_blank_locator_bundle_passes_nandatown_verify(tmp_path, capsys,
                                                          argv):
    from nandatown.cli import main

    index = tmp_path / "index.json"
    index.write_text(json.dumps({"agents": {"maya-seller": {"url": SUBJECT}}}))
    # Listed under a blank name at a closed loopback port: resolving it
    # would reach card retrieval instead of failing resolution.
    blank_index = tmp_path / "blank-index.json"
    blank_index.write_text(json.dumps(
        {"agents": {"   ": {"url": "http://127.0.0.1:9"}}}))
    paths = {"INDEX": str(index), "BLANK_INDEX": str(blank_index)}
    argv = [paths.get(arg, arg) for arg in argv]

    code = main(["test-agent", *argv, "--out", str(tmp_path / "runs")])

    out = capsys.readouterr().out
    assert code == 1
    assert "Traceback" not in out
    if "--agent-name" in argv:
        assert BLANK_AGENT_NAME_REASON in out
    bundle_dir = out.split("Evidence bundle: ", 1)[1].strip()
    assert main(["verify", bundle_dir]) == 0
    assert "bundle verified" in capsys.readouterr().out


def test_town_driver_fault_is_an_error_not_a_failure(tmp_path,
                                                     monkeypatch):
    import nandatown.a2a_adapter as a2a

    def broken(*args, **kwargs):
        raise TypeError("driver bug: bad argument shape")

    monkeypatch.setattr(a2a, "send_message", broken)
    _, result = run_path_test(SUBJECT, str(tmp_path), http=client())
    invocation = stage(result, "protocol_invocation")
    assert invocation.status == "error"
    assert "Town's own driver malfunctioned" in invocation.note
    assert result.verdict == "error"
    assert stage(result, "semantic_result").status == "not_tested"


def test_profile_is_frozen_and_fingerprinted():
    profile = get_path_profile("a2a-capability-fulfillment@0.1")
    assert profile.ref == "a2a-capability-fulfillment@0.1"
    assert profile.fingerprint().startswith("sha256:")
    with pytest.raises(KeyError):
        get_path_profile("nonsense@9.9")


def test_oversized_card_does_not_reach_path_invocation(tmp_path):
    url = "http://fixture.invalid"
    card = build_agent_card(url)
    card["description"] = "x" * 1_048_576
    methods = []
    def handle(request):
        methods.append(request.method)
        return httpx.Response(200, json=card)
    with httpx.Client(base_url=url, transport=httpx.MockTransport(handle)) as http:
        bundle_dir, result = run_path_test(url, str(tmp_path), http=http)
        assert not http.is_closed
    assert methods == ["GET"]
    assert stage(result, "agent_card_retrieval").status == "failed"
    assert stage(result, "agent_card_retrieval").note == (
        "a2a_response_budget_exceeded: selected local byte budget exceeded for this run")
    assert stage(result, "semantic_result").status == "not_tested"
    bundle = load_bundle(bundle_dir)
    assert bundle["run"].profile_name == "a2a-capability-fulfillment@0.3"
    assert bundle["run"].config["a2a_transport_policy"] == {
        "policy_id": "a2a-bounded-json@0.1",
        "max_response_bytes": 1_048_576,
        "budget_basis": "profile",
        "accept_encoding": "identity",
        "follow_redirects": False,
        "trust_env": "caller_controlled",
        "transport_retries": "caller_controlled",
        "client_ownership": "injected",
        "phase_timeout_seconds": 15.0,
        "total_deadline_seconds": None,
    }
    assert verify_bundle(bundle_dir) == []


def test_old_profile_is_unchanged_and_new_bundles_replay(tmp_path):
    old = get_path_profile("a2a-capability-fulfillment@0.1")
    assert old.fingerprint() == "sha256:80d238c2de68dbe3de577ad88ae5eb742daeaf2628dc7802be2b11e68b8d4b83"
    assert old.limits == {"timeout_seconds": 15.0}
    new = get_path_profile("a2a-capability-fulfillment@0.2")
    assert new.limits == {"timeout_seconds": 15.0, "max_response_bytes": 1_048_576}
    assert old.fingerprint() != new.fingerprint()
    for profile in (old, new):
        with client() as http:
            directory, result = run_path_test(SUBJECT, str(tmp_path), profile_ref=profile.ref, http=http)
        assert result.verdict == "passed"
        bundle = load_bundle(directory)
        assert bundle["run"].profile_fingerprint == profile.fingerprint()
        assert bundle["run"].config["a2a_transport_policy"]["budget_basis"] == (
            "implementation_ceiling" if profile.version == "0.1" else "profile")
        assert verify_bundle(directory) == []


def test_owned_path_keeps_card_session_for_both_logical_requests(tmp_path, monkeypatch):
    import nandatown.a2a_transport as transport
    real_client = httpx.Client
    clients = []
    def handle(request):
        if request.method == "GET":
            return httpx.Response(200, json=build_agent_card(SUBJECT),
                                  headers={"set-cookie": "session=local; Path=/"})
        assert request.headers.get("cookie") == "session=local"
        order = json.loads(json.loads(request.content)["params"]["message"]["parts"][0]["text"])
        return httpx.Response(200, json={"result": {
            "kind": "task", "id": "local", "status": {"state": "completed"},
            "artifacts": [{"parts": [{"kind": "text", "text": json.dumps({
                "request_id": order["request_id"], "total_cents": 3990})}]}]}})
    def client_factory(**kwargs):
        kwargs.pop("transport", None)
        http = real_client(**kwargs, transport=httpx.MockTransport(handle))
        clients.append(http)
        return http
    monkeypatch.setattr(transport.httpx, "Client", client_factory)
    _, result = run_path_test(SUBJECT, str(tmp_path))
    assert result.verdict == "passed"
    assert len(clients) == 1 and clients[0].is_closed


def test_generated_rerun_keeps_explicit_path_profile_when_default_changes(
        tmp_path, monkeypatch):
    """Catches a generated --profile being parsed as the Track flag."""
    fallback = get_path_profile("a2a-capability-fulfillment@0.1").model_copy(
        update={"version": "0.2"})
    monkeypatch.setitem(PATH_PROFILES, fallback.ref, fallback)
    original_add_argument = argparse.ArgumentParser.add_argument

    def different_path_default(parser, *names, **kwargs):
        if "--path-profile" in names:
            kwargs["default"] = fallback.ref
        return original_add_argument(parser, *names, **kwargs)

    monkeypatch.setattr(argparse.ArgumentParser, "add_argument",
                        different_path_default)
    process, url = start_reference_agent()
    try:
        expected_digest = fingerprint(build_agent_card(url))
        bundle_dir, _ = run_path_test(
            url, str(tmp_path),
            profile_ref="a2a-capability-fulfillment@0.1",
            pin_card_digest=expected_digest)
        rerun = load_bundle(bundle_dir)["run"].config["rerun_command"]
        assert url in rerun
        assert "--pin-card-digest " + expected_digest in rerun

        monkeypatch.chdir(tmp_path)
        from nandatown.cli import main

        assert main(shlex.split(rerun)[1:]) == 0
        rerun_bundle = next((tmp_path / "runs").iterdir())
        replayed = load_bundle(str(rerun_bundle))
        assert replayed["run"].profile_name == "a2a-capability-fulfillment@0.1"
        assert replayed["run"].config["pinned_card_digest"] == expected_digest
    finally:
        process.terminate()
        process.wait()


def _write_index(path, url=SUBJECT):
    path.write_text(json.dumps({"agents": {"seller": {"url": url}}}))
    return str(path)


def test_url_and_index_together_are_refused_before_any_contact(
        tmp_path, capsys):
    """Catches a pass credited to --url while --index chose the agent."""
    from nandatown.cli import main

    index = _write_index(tmp_path / "index.json", "http://127.0.0.1:9")
    out = tmp_path / "runs"

    assert main(["test-agent", "--url", "http://127.0.0.1:9",
                 "--index", index, "--agent-name", "seller",
                 "--out", str(out)]) == 2
    assert "either --url or --index" in capsys.readouterr().out
    assert not out.exists()
    with pytest.raises(ValueError, match="either a subject URL or an index"):
        run_path_test("http://127.0.0.1:9", str(out), index_file=index,
                      agent_name="seller")
    assert not out.exists()


def test_index_without_agent_name_is_refused(tmp_path, capsys):
    from nandatown.cli import main

    index = _write_index(tmp_path / "index.json")
    out = tmp_path / "runs"

    assert main(["test-agent", "--index", index, "--out", str(out)]) == 2
    assert "--agent-name" in capsys.readouterr().out
    assert not out.exists()


def test_run_path_test_refuses_index_without_agent_name(tmp_path):
    index = _write_index(tmp_path / "index.json")

    with pytest.raises(ValueError, match="needs an agent name"):
        run_path_test(None, str(tmp_path / "runs"), index_file=index)
    assert not (tmp_path / "runs").exists()


def _shell_argv(command):
    """The argv a POSIX shell gives the recorded command, without running it."""
    shown = subprocess.run(
        ["/bin/sh", "-c", 'nandatown() { printf "%s\\n" "$@"; }\n' + command],
        capture_output=True, text=True, timeout=10)
    return shown.returncode, shown.stdout.splitlines()


@pytest.mark.skipif(not os.path.exists("/bin/sh"), reason="needs /bin/sh")
@pytest.mark.parametrize("via_index", [False, True])
def test_generated_rerun_survives_a_real_shell(tmp_path, via_index):
    """Catches shell metacharacters splitting the rerun into a different test."""
    pin = "sha256:" + "0" * 64
    if via_index:
        index = _write_index(tmp_path / "my index.json")
        subject, locator = None, ["--index", index, "--agent-name", "seller"]
    else:
        subject = SUBJECT + "/agents/x?tenant=alpha&region=eu"
        index, locator = None, ["--url", subject]
    with client() as http:
        bundle_dir, _ = run_path_test(
            subject, str(tmp_path / "runs"),
            profile_ref="a2a-quote-intent@0.2", pin_card_digest=pin,
            index_file=index, agent_name="seller" if via_index else None,
            http=http)
    rerun = load_bundle(bundle_dir)["run"].config["rerun_command"]

    returncode, argv = _shell_argv(rerun)

    assert returncode == 0, rerun
    assert argv == ["test-agent", *locator,
                    "--path-profile", "a2a-quote-intent@0.2",
                    "--pin-card-digest", pin]
