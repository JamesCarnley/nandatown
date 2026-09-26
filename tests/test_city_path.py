"""Literal City 7e32887 wire vectors; no EVM verification is implied."""

import asyncio
import base64
import copy
import json
import time
from contextlib import asynccontextmanager
from pathlib import Path

import pytest

from nandatown.bundle import load_bundle, verify_bundle
from nandatown.path_profiles import get_path_profile
from nandatown.path_runner import evaluate_path, path_evaluator_version, run_path_test
from nandatown.receipt import verify_receipt


def test_city_profile_is_registered_and_cannot_enter_quote_runner(tmp_path):
    profile = get_path_profile("city-a2a-protocol@0.1")
    assert profile.capability == "city-a2a-structured-task"
    assert path_evaluator_version(profile) == "path-city-a2a-protocol-0.1"
    assert profile.fingerprint() == "sha256:e6a1cc01584de3547a76ddc3b2bbac6d366258bc8603a26a1dad2ebd5c5212cc"
    assert profile.expected["required_stages"] == [
        "pinned_card", "structured_send", "acceptance_task", "exact_retry",
        "terminal_task",
    ]
    with pytest.raises(ValueError, match="nandatown.city_path"):
        run_path_test("http://127.0.0.1:1/", str(tmp_path), profile.ref)


def wire(value):
    return json.dumps(value, separators=(",", ":"), ensure_ascii=False).encode()


def b64(value):
    return base64.b64encode(value).decode("ascii")


# Structurally signed envelopes deliberately do not claim valid EVM signatures.
# These literal schemas follow City wire.ts, service.ts and schema.ts at 7e32887.
def envelope(kind):
    statement = {
        "request": {
            "kind": "request", "version": "0.1",
            "service": {"method": "erc8004", "agent": {
                "chainId": 11155111, "registry": "0x" + "11" * 20,
                "agentId": "7"}},
            "caller": {"method": "eip155-eoa", "chainId": 11155111,
                       "address": "0x" + "22" * 20},
            "interactionId": "0x" + "ab" * 32,
            "profileBasis": {
                "blockNumber": "9123456", "blockHash": "0x" + "aa" * 32,
                "agentOwner": "0x" + "33" * 20,
                "agentUriDigest": "0x" + "bb" * 32,
                "registrationDigest": "0x" + "cc" * 32,
                "cardDigest": "0x" + "dd" * 32,
                "receiptSigner": "0x" + "44" * 20},
            "createdAt": "2026-09-26T12:00:00Z",
            "deadline": "2026-09-26T13:00:00Z",
            "input": {"version": "0.1", "capability": "evening-plan",
                      "city": "Chicago", "area": "The Loop",
                      "timeWindow": {"start": "2026-10-02T18:00:00-05:00",
                                     "end": "2026-10-02T22:00:00-05:00",
                                     "timeZone": "America/Chicago"},
                      "budget": {"currency": "USD", "minorUnits": "8500"},
                      "transport": ["walk"], "preferences": []}},
        "acceptance": {"kind": "acceptance", "version": "0.1",
                       "requestDigest": "0x" + "11" * 32,
                       "acceptanceId": "0x" + "22" * 32,
                       "acceptedAt": "2026-09-26T12:01:00Z",
                       "deadline": "2026-09-26T13:00:00Z"},
        "completion": {"kind": "completion", "version": "0.1",
                       "acceptanceDigest": "0x" + "33" * 32,
                       "recordedAt": "2026-09-26T12:02:00Z",
                       "outcome": "completed", "answerDigest": "0x" + "44" * 32},
    }[kind]
    return {"version": "0.1", "scheme": "eip712-eoa",
            "signer": {"method": "eip155-eoa", "chainId": 11155111,
                       "address": "0x" + ("22" if kind == "request" else "44") * 20},
            "payloadBase64": b64(wire(statement)), "signature": "0x" + "11" * 65}


def replace_statement(signed, field, value):
    payload = json.loads(base64.b64decode(signed["payloadBase64"]))
    payload[field] = value
    signed["payloadBase64"] = b64(wire(payload))
    return signed


REQUEST = wire({
    "jsonrpc": "2.0", "id": "rpc-request-7", "method": "message/send",
    "params": {"message": {"kind": "message", "role": "user",
                           "messageId": "client-message-1", "parts": [{
                               "kind": "data", "data": {
                                   "type": "org.nandacity.city-request", "version": "0.1",
                                   "envelope": envelope("request")}}]},
               "configuration": {"blocking": False, "acceptedOutputModes": ["application/json"]}}})


def task(state="submitted"):
    acceptance = envelope("acceptance")
    value = {"kind": "task", "id": "task-1", "contextId": "context-1",
             "status": {"state": state, "timestamp": "2026-09-26T12:01:00Z"},
             "history": [json.loads(REQUEST)["params"]["message"]],
             "metadata": {"org.nandacity": {
                 "subset": "a2a-0.3-jsonrpc-loopback",
                 "pollingAuthentication": "none-loopback-only",
                 "interactionId": "0x" + "ab" * 32, "acceptance": acceptance}}}
    if state == "submitted":
        value["status"]["message"] = {
            "kind": "message", "role": "agent", "messageId": "status-1",
            "parts": [{"kind": "data", "data": {
                "type": "org.nandacity.city-status", "version": "0.1",
                "state": "accepted", "acceptance": acceptance}}]}
    if state == "completed":
        completion = envelope("completion")
        value["metadata"]["org.nandacity"]["completion"] = completion
        value["artifacts"] = [{"artifactId": "artifact-1", "parts": [{
            "kind": "data", "data": {"type": "org.nandacity.city-result", "version": "0.1",
                                      "answerBase64": "e30=", "completion": completion}}]}]
    return value


@asynccontextmanager
async def owned_server(change=None, *, raw_card=None, slow=False, redirect=False,
                       missing_eof=False, delay=0):
    calls = []
    active = set()
    card = None

    async def handler(reader, writer):
        active.add(asyncio.current_task())
        try:
            headers = await reader.readuntil(b"\r\n\r\n")
            first = headers.split(b"\r\n")[0]
            size = next((int(line.split(b":", 1)[1]) for line in headers.split(b"\r\n")
                         if line.lower().startswith(b"content-length:")), 0)
            body = await reader.readexactly(size)
            calls.append((first, body))
            if delay:
                await asyncio.sleep(delay)
            if first.startswith(b"GET "):
                payload = raw_card if raw_card is not None else card
                status = 200
            else:
                request = json.loads(body)
                phase = "send" if len(calls) == 2 else "retry" if len(calls) == 3 else "poll"
                value = {"jsonrpc": "2.0", "id": request["id"],
                         "result": task("completed" if phase == "poll" else "submitted")}
                value = change(phase, value) if change else value
                payload = value if isinstance(value, bytes) else wire(value)
                status = 200
            if redirect:
                writer.write(b"HTTP/1.1 302 Found\r\nLocation: http://127.0.0.1:1/elsewhere\r\nContent-Length: 0\r\n\r\n")
            elif missing_eof:
                writer.write(b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n")
                writer.write(f"{len(payload):x}\r\n".encode() + payload + b"\r\n")
                await writer.drain()
                await asyncio.sleep(10)
            elif slow:
                writer.write(f"HTTP/1.1 {status} OK\r\nContent-Length: {len(payload)}\r\n\r\n".encode())
                # A body byte arrives regularly: inactivity timeout is not enough.
                for byte in payload:
                    writer.write(bytes([byte]))
                    await writer.drain()
                    await asyncio.sleep(0.1)
            else:
                writer.write(f"HTTP/1.1 {status} OK\r\nContent-Type: application/json\r\nContent-Length: {len(payload)}\r\nConnection: close\r\n\r\n".encode())
                writer.write(payload)
                await writer.drain()
        except (ConnectionError, asyncio.IncompleteReadError):
            pass
        finally:
            writer.close()
            await writer.wait_closed()
            active.discard(asyncio.current_task())

    server = await asyncio.start_server(handler, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    endpoint = f"http://127.0.0.1:{port}/"
    card = wire({"protocolVersion": "0.3.0", "name": "Synthetic City",
                 "description": "Synthetic test only", "url": endpoint,
                 "preferredTransport": "JSONRPC", "version": "0.1.0",
                 "capabilities": {"streaming": False, "pushNotifications": False,
                                  "stateTransitionHistory": False},
                 "defaultInputModes": ["application/json"],
                 "defaultOutputModes": ["application/json"],
                 "skills": [{"id": "evening-plan", "name": "Evening Plan",
                             "description": "Synthetic", "tags": ["city"]}]})
    try:
        yield endpoint, endpoint + "cards/7.json", card, calls
    finally:
        server.close()
        await server.wait_closed()
        for pending in list(active):
            pending.cancel()
        await asyncio.gather(*active, return_exceptions=True)


async def observe(tmp_path, server, **kwargs):
    from nandatown.city_path import run_city_path
    endpoint, card_url, card, _ = server
    return await run_city_path(
        subject_url=endpoint, card_url=card_url,
        pinned_card=kwargs.pop("pinned_card", card), request_bytes=REQUEST,
        out_dir=str(tmp_path / "bundles"), observer_key_dir=str(tmp_path / "observer"),
        observer_name="synthetic-town-observer", **kwargs)


@pytest.mark.parametrize(("kind", "field", "value"), [
    ("acceptance", "requestDigest", "x"),
    ("acceptance", "acceptanceId", "0x" + "AB" * 32),
    ("acceptance", "acceptedAt", "2026-02-29T12:00:00Z"),
    ("acceptance", "deadline", "2026-09-26T25:00:00Z"),
    ("acceptance", "extra", "unrecognized"),
    ("completion", "acceptanceDigest", "x"),
    ("completion", "answerDigest", "0x1234"),
    ("completion", "recordedAt", "2026-09-26T12:02:00.000Z"),
    ("completion", "recordedAt", "2026-13-26T12:02:00Z"),
    ("completion", "recordedAt", "2026-09-26T12:60:00Z"),
    ("completion", "recordedAt", "2026-09-26T12:02:60Z"),
    ("completion", "recordedAt", True),
    ("completion", "extra", "unrecognized"),
])
def test_malformed_statement_shapes_cannot_receive_pass(tmp_path, kind, field, value):
    def change(phase, response):
        task_value = response["result"]
        metadata = task_value["metadata"]["org.nandacity"]
        if kind in metadata:
            changed = replace_statement(copy.deepcopy(metadata[kind]), field, value)
            metadata[kind] = changed
            if kind == "acceptance" and "message" in task_value["status"]:
                task_value["status"]["message"]["parts"][0]["data"][kind] = changed
            elif kind == "completion":
                task_value["artifacts"][0]["parts"][0]["data"][kind] = changed
        return response

    async def exercise():
        async with owned_server(change) as server:
            directory, result = await observe(tmp_path, server)
            failed = 2 if kind == "acceptance" else 4
            assert result.verdict == "failed"
            assert result.stages[failed].status == "failed"
            assert all(s.status == "passed" for s in result.stages[:failed])
            assert verify_bundle(directory) == []
            assert verify_receipt(str(Path(directory) / "receipt.json"), directory) == []
    asyncio.run(exercise())


def test_observer_shutdown_error_does_not_blame_completed_service(tmp_path, monkeypatch):
    import httpx
    original = httpx.AsyncClient.__aexit__

    async def close_then_fail(self, *args):
        await original(self, *args)
        raise RuntimeError("synthetic observer shutdown failure")

    monkeypatch.setattr(httpx.AsyncClient, "__aexit__", close_then_fail)

    async def exercise():
        async with owned_server() as server:
            directory, result = await observe(tmp_path, server)
            assert result.verdict == "error"
            assert [s.status for s in result.stages] == ["passed"] * 4 + ["error"]
            assert verify_bundle(directory) == []
            assert verify_receipt(str(Path(directory) / "receipt.json"), directory) == []
    asyncio.run(exercise())


def test_owned_http_produces_native_replayable_receipt(tmp_path, monkeypatch):
    # This must not consult environment proxy configuration.
    monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:1/")
    monkeypatch.setenv("ALL_PROXY", "http://127.0.0.1:1/")
    async def exercise():
        async with owned_server() as server:
            started = time.time()
            directory, result = await observe(tmp_path, server)
            assert result.verdict == "passed"
            assert [stage.status for stage in result.stages] == ["passed"] * 5
            assert verify_bundle(directory) == []
            assert verify_receipt(str(Path(directory) / "receipt.json"), directory) == []
            bundle = load_bundle(directory)
            assert started <= bundle["run"].created_at <= bundle["events"][1].at
            assert bundle["run"].config["subject"] == server[0]
            assert bundle["run"].participants[0]["role"] == "observer"
            replay = evaluate_path(bundle["profile"], bundle["run"].run_id, bundle["events"])
            assert replay.model_dump(exclude={"evaluated_at"}) == result.model_dump(exclude={"evaluated_at"})
            assert server[3][1][1] == REQUEST == server[3][2][1]
            assert json.loads(server[3][3][1])["params"] == {"id": "task-1"}
            receipt = json.loads((Path(directory) / "receipt.json").read_text())
            assert any("not Ethereum authorization" in text for text in receipt["payload"]["limitations"])
            assert any("not global exactly-once" in text for text in receipt["payload"]["limitations"])
            assert "replay-unchecked" not in str(receipt)
    asyncio.run(exercise())


@pytest.mark.parametrize(("phase", "field", "value", "broken_stage"), [
    ("send", "rpc_id", None, "structured_send"),
    ("send", "id", "", "acceptance_task"),
    ("send", "contextId", "", "acceptance_task"),
    ("retry", "id", "different", "exact_retry"),
    ("retry", "contextId", "different", "exact_retry"),
    ("retry", "acceptance", None, "exact_retry"),
    ("poll", "id", "different", "terminal_task"),
    ("poll", "contextId", "different", "terminal_task"),
    ("poll", "acceptance", None, "terminal_task"),
    ("poll", "artifacts", [], "terminal_task"),
    ("poll", "state", "failed", "terminal_task"),
])
def test_changed_or_missing_protocol_fields_cannot_pass(tmp_path, phase, field, value, broken_stage):
    def change(current, response):
        if current == phase:
            if field == "rpc_id":
                response.pop("id")
            elif field == "acceptance":
                response["result"]["metadata"]["org.nandacity"]["acceptance"] = value
            elif field == "state":
                response["result"]["status"]["state"] = value
            else:
                response["result"][field] = value
        return response
    async def exercise():
        async with owned_server(change) as server:
            directory, result = await observe(tmp_path, server)
            statuses = {stage.name: stage.status for stage in result.stages}
            assert result.verdict == "failed"
            assert statuses[broken_stage] == "failed"
            index = [stage.name for stage in result.stages].index(broken_stage)
            assert all(stage.status == "not_tested" for stage in result.stages[index + 1:])
            assert verify_bundle(directory) == []
    asyncio.run(exercise())


@pytest.mark.parametrize("raw", [b'{"x":1,"x":2}', b'{"x":NaN}', b'{"x":1e999}',
                                 b'{"x":"\\ud800"}', b'\xff', b'[' * 33 + b'0' + b']' * 33])
def test_bad_card_json_is_retained_without_breaking_bundle_writes(tmp_path, raw):
    async def exercise():
        async with owned_server(raw_card=raw) as server:
            directory, result = await observe(tmp_path, server)
            assert result.verdict == "failed"
            assert [s.status for s in result.stages] == ["failed"] + ["not_tested"] * 4
            assert verify_bundle(directory) == []
            exchange = load_bundle(directory)["events"][1]
            assert base64.b64decode(exchange.detail["response_base64"]) == raw
    asyncio.run(exercise())


def test_wrong_pinned_card_stops_before_invocation(tmp_path):
    async def exercise():
        async with owned_server() as server:
            pinned = json.loads(server[2])
            pinned["name"] = "Different pinned card"
            directory, result = await observe(tmp_path, server, pinned_card=wire(pinned))
            assert result.verdict == "failed"
            assert len(server[3]) == 1
            assert verify_bundle(directory) == []
    asyncio.run(exercise())


def test_no_events_is_incomplete_not_passed():
    result = evaluate_path(get_path_profile("city-a2a-protocol@0.1"), "empty", [])
    assert result.verdict == "incomplete"
    assert result.stages[0].status == "not_enough_evidence"
    assert all(s.status == "not_tested" for s in result.stages[1:])


@pytest.mark.parametrize("variant", ["acceptance-echo", "completion-echo", "result-type",
                                    "result-base64", "rpc-error", "interaction", "large-surrogate"])
def test_envelope_mismatches_and_unsafe_echoes_never_pass(tmp_path, variant):
    def change(phase, response):
        if phase != "poll":
            return response
        result = response["result"]
        if variant == "rpc-error":
            return {"jsonrpc": "2.0", "id": response["id"], "error": {"code": -32010, "message": "unavailable"}}
        if variant == "acceptance-echo":
            result["metadata"]["org.nandacity"]["acceptance"]["signature"] = "0x" + "22" * 65
        if variant == "completion-echo":
            result["metadata"]["org.nandacity"]["completion"] = envelope("acceptance")
        if variant == "result-type":
            result["artifacts"][0]["parts"][0]["data"]["type"] = "other"
        if variant == "result-base64":
            result["artifacts"][0]["parts"][0]["data"]["answerBase64"] = "?"
        if variant == "interaction":
            result["metadata"]["org.nandacity"]["interactionId"] = "other"
        if variant == "large-surrogate":
            return b'{"jsonrpc":"2.0","id":"' + b"x" * 200_000 + b'\\ud800"}'
        return response
    async def exercise():
        async with owned_server(change) as server:
            directory, result = await observe(tmp_path, server)
            assert result.verdict == "failed"
            assert result.stages[-1].status == "failed"
            assert verify_bundle(directory) == []
    asyncio.run(exercise())


def test_twenty_polls_exhaust_without_claiming_completion(tmp_path):
    def change(phase, response):
        if phase == "poll":
            response["result"] = task("working")
        return response
    async def exercise():
        async with owned_server(change) as server:
            directory, result = await observe(tmp_path, server)
            assert result.verdict == "incomplete"
            assert len(server[3]) == 23  # card, send, retry, twenty polls
            assert result.stages[-1].status == "not_enough_evidence"
            assert verify_bundle(directory) == []
    asyncio.run(exercise())


@pytest.mark.parametrize("options", [{"slow": True}, {"missing_eof": True}])
def test_five_second_deadline_covers_slow_body_and_stream_eof(tmp_path, options):
    async def exercise():
        async with owned_server(**options) as server:
            started = time.monotonic()
            directory, result = await observe(tmp_path, server)
            duration = time.monotonic() - started
            assert 4.8 <= duration < 6.5
            assert result.verdict == "incomplete"
            assert [s.status for s in result.stages] == ["not_enough_evidence"] + ["not_tested"] * 4
            assert len(server[3]) == 1
            assert verify_bundle(directory) == []
    asyncio.run(exercise())


def test_cancellation_does_not_sign_or_create_a_bundle(tmp_path):
    async def exercise():
        async with owned_server(slow=True) as server:
            pending = asyncio.create_task(observe(tmp_path, server))
            while not server[3]:
                await asyncio.sleep(0.01)
            pending.cancel()
            with pytest.raises(asyncio.CancelledError):
                await pending
            assert not (tmp_path / "bundles").exists()
            assert not (tmp_path / "observer").exists()
    asyncio.run(exercise())


def test_redirect_is_recorded_but_never_followed(tmp_path):
    async def exercise():
        async with owned_server(redirect=True) as server:
            directory, result = await observe(tmp_path, server)
            assert result.verdict == "failed"
            assert len(server[3]) == 1
            assert load_bundle(directory)["events"][1].detail["status_code"] == 302
            assert verify_bundle(directory) == []
    asyncio.run(exercise())


@pytest.mark.parametrize("url", ["http://localhost:8080/", "http://example.com/",
                                 "http://127.0.0.1:8080", "http://127.0.0.1:8080/a/../",
                                 "http://user:pass@127.0.0.1:8080/", "http://127.0.0.1:8080/?x=1",
                                 "http://127.1:8080/", "http://2130706433:8080/"])
def test_nonliteral_or_ambiguous_urls_are_rejected_before_http(tmp_path, url):
    from nandatown.city_path import run_city_path
    with pytest.raises(ValueError, match="URL"):
        asyncio.run(run_city_path(subject_url=url, card_url=url, pinned_card=b"{}", request_bytes=REQUEST,
                                  out_dir=str(tmp_path / "bundles"), observer_key_dir=str(tmp_path / "observer"),
                                  observer_name="synthetic-observer"))
    assert not (tmp_path / "observer").exists()


@pytest.mark.parametrize("which", ["card", "response", "total"])
def test_byte_budgets_bound_retained_evidence(tmp_path, which):
    def change(phase, response):
        if which == "response":
            return b" " * 1_048_577
        if which == "total":
            response["result"] = task("working")
            response["result"]["padding"] = "x" * 900_000
        return response
    async def exercise():
        async with owned_server(change, raw_card=b" " * 65_537 if which == "card" else None) as server:
            directory, result = await observe(tmp_path, server)
            assert result.verdict == "incomplete"
            events = load_bundle(directory)["events"][1:]
            recorded = sum(len(base64.b64decode(e.detail[key])) for e in events
                           for key in ("request_base64", "response_base64"))
            assert recorded <= 4_194_304
            assert events[-1].detail["outcome"] == "byte_limit"
            assert verify_bundle(directory) == []
    asyncio.run(exercise())


def test_tampered_transcript_and_claimed_result_fail_native_verification(tmp_path):
    async def exercise():
        async with owned_server() as server:
            directory, result = await observe(tmp_path, server)
            bundle = load_bundle(directory)
            events = copy.deepcopy(bundle["events"])
            raw = json.loads(base64.b64decode(events[-1].detail["response_base64"]))
            raw["result"]["id"] = "tampered"
            events[-1].detail["response_base64"] = b64(wire(raw))
            assert evaluate_path(bundle["profile"], result.run_id, events).verdict == "failed"
            events_file = Path(directory) / "events.jsonl"
            events_file.write_text("".join(e.model_dump_json() + "\n" for e in events))
            assert any("hash mismatch" in problem for problem in verify_bundle(directory))
        async with owned_server() as server:
            directory, _ = await observe(tmp_path, server)
            result_file = Path(directory) / "result.json"
            value = json.loads(result_file.read_text())
            value["stages"][0]["status"] = "failed"
            result_file.write_text(json.dumps(value))
            assert verify_receipt(str(Path(directory) / "receipt.json"), directory)
    asyncio.run(exercise())


def test_terminal_already_seen_on_first_send_cannot_change(tmp_path):
    def change(phase, response):
        response["result"] = task("completed")
        if phase in {"retry", "poll"}:
            response["result"]["artifacts"][0]["parts"][0]["data"]["answerBase64"] = "W10="
        return response
    async def exercise():
        async with owned_server(change) as server:
            directory, result = await observe(tmp_path, server)
            assert result.verdict == "failed"
            assert result.stages[3].status == "failed"
            assert len(server[3]) == 3
            assert verify_bundle(directory) == []
    asyncio.run(exercise())


def test_thirty_second_run_deadline_covers_many_individually_timely_responses(tmp_path):
    def change(phase, response):
        response["result"] = task("working")
        return response
    async def exercise():
        async with owned_server(change, delay=1.5) as server:
            started = time.monotonic()
            directory, result = await observe(tmp_path, server)
            assert 29 <= time.monotonic() - started < 31.5
            assert result.verdict == "incomplete"
            assert len(server[3]) < 23
            assert verify_bundle(directory) == []
    asyncio.run(exercise())


@pytest.mark.parametrize("field", ["state", "status", "metadata", "parts", "envelope"])
def test_wrong_container_types_are_subject_failures_not_replay_crashes(tmp_path, field):
    def change(phase, response):
        if phase == "send":
            value = response["result"]
            if field == "state":
                value["status"]["state"] = {}
            elif field == "parts":
                value["status"]["message"]["parts"] = {}
            elif field == "envelope":
                value["metadata"]["org.nandacity"]["acceptance"] = []
            else:
                value[field] = []
        return response
    async def exercise():
        async with owned_server(change) as server:
            directory, result = await observe(tmp_path, server)
            assert result.verdict == "failed"
            assert result.stages[2].status == "failed"
            assert verify_bundle(directory) == []
    asyncio.run(exercise())


def test_town_driver_error_is_not_blame_on_subject(tmp_path, monkeypatch):
    import httpx

    def broken(*args, **kwargs):
        raise RuntimeError("synthetic internal bug")
    monkeypatch.setattr(httpx.AsyncClient, "stream", broken)
    async def exercise():
        async with owned_server() as server:
            directory, result = await observe(tmp_path, server)
            assert result.verdict == "error"
            assert result.stages[0].status == "error"
            assert all(s.status == "not_tested" for s in result.stages[1:])
            assert not server[3]
            assert verify_bundle(directory) == []
    asyncio.run(exercise())


def test_module_cli_requires_explicit_keys_and_runs_native_pipeline(tmp_path):
    import os
    import sys

    async def exercise():
        async with owned_server() as server:
            card_file = tmp_path / "card.json"
            request_file = tmp_path / "request.json"
            card_file.write_bytes(server[2])
            request_file.write_bytes(REQUEST)
            argv = [sys.executable, "-m", "nandatown.city_path", "--subject-url", server[0],
                    "--card-url", server[1], "--pinned-card", str(card_file),
                    "--request", str(request_file), "--out-dir", str(tmp_path / "cli-bundles"),
                    "--observer-name", "cli-observer"]
            env = {"PATH": os.path.dirname(sys.executable) + ":/usr/bin:/bin",
                   "NANDATOWN_HOME": str(tmp_path / "disposable-home")}
            missing = await asyncio.create_subprocess_exec(*argv, env=env, stdout=asyncio.subprocess.PIPE,
                                                           stderr=asyncio.subprocess.PIPE)
            _, error = await missing.communicate()
            assert missing.returncode == 2
            assert b"--observer-key-dir" in error
            assert not server[3]
            process = await asyncio.create_subprocess_exec(*argv, "--observer-key-dir", str(tmp_path / "cli-key"),
                                                           env=env, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
            output, error = await process.communicate()
            assert process.returncode == 0, error.decode()
            summary = json.loads(output)
            assert summary["verdict"] == "passed"
            assert verify_bundle(summary["bundle"]) == []
            assert verify_receipt(summary["receipt"], summary["bundle"]) == []
    asyncio.run(exercise())


@pytest.mark.parametrize("which", ["request-limit", "malformed-city"])
def test_invalid_caller_input_fails_before_network_or_keys(tmp_path, which):
    from nandatown.city_path import run_city_path

    async def exercise():
        async with owned_server() as server:
            request = REQUEST
            if which == "request-limit":
                request = b" " * 1_048_577
            else:
                raw = json.loads(REQUEST)
                signed = raw["params"]["message"]["parts"][0]["data"]["envelope"]
                statement = json.loads(base64.b64decode(signed["payloadBase64"]))
                statement["input"]["city"] = {}
                signed["payloadBase64"] = b64(wire(statement))
                request = wire(raw)
            with pytest.raises(ValueError):
                await run_city_path(subject_url=server[0], card_url=server[1], pinned_card=server[2],
                                    request_bytes=request, out_dir=str(tmp_path / "bundles"),
                                    observer_key_dir=str(tmp_path / "observer"), observer_name="test-observer")
            assert not server[3]
            assert not (tmp_path / "observer").exists()
    asyncio.run(exercise())


def test_rehashed_transcript_still_cannot_borrow_recorded_pass(tmp_path):
    from nandatown.bundle import attest_bundle, write_bundle
    from nandatown.identity_portable import Keystore

    async def exercise():
        async with owned_server() as server:
            directory, _ = await observe(tmp_path, server)
            bundle = load_bundle(directory)
            raw = json.loads(base64.b64decode(bundle["events"][-1].detail["response_base64"]))
            raw["result"]["artifacts"] = []
            bundle["events"][-1].detail["response_base64"] = b64(wire(raw))
            write_bundle(directory, bundle["profile"], bundle["run"], [], bundle["events"], bundle["result"], mode="path")
            attest_bundle(directory, keystore=Keystore(str(tmp_path / "observer")), signer="synthetic-town-observer")
            assert any("replay mismatch" in problem for problem in verify_bundle(directory))
            assert verify_receipt(str(Path(directory) / "receipt.json"), directory)
    asyncio.run(exercise())


def test_exactly_exhausted_total_budget_does_not_record_or_send_another_request(tmp_path):
    response_bytes = 0
    server_state = None

    def change(phase, response):
        nonlocal response_bytes
        response["result"] = task("working")
        used = len(server_state[2]) + sum(len(body) for _, body in server_state[3]) + response_bytes
        limit = min(1_048_576, 4_194_304 - used)
        raw = wire(response)
        assert len(raw) < limit
        raw += b" " * (limit - len(raw))
        response_bytes += len(raw)
        return raw

    async def exercise():
        nonlocal server_state
        async with owned_server(change) as server:
            server_state = server
            directory, result = await observe(tmp_path, server)
            assert result.verdict == "incomplete"
            events = load_bundle(directory)["events"][1:]
            assert sum(len(base64.b64decode(e.detail[key])) for e in events
                       for key in ("request_base64", "response_base64")) == 4_194_304
            assert len(server[3]) == 5  # card plus four full responses; no further send
            assert verify_bundle(directory) == []
    asyncio.run(exercise())
