"""Bounded, synthetic City/A2A observation and deterministic native Path replay.

Only this module owns HTTP. The evaluator reads retained body bytes, never a
caller-supplied verdict. Envelope *shape* is checked, not EVM cryptography.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import calendar
import hashlib
import json
import math
import re
import sys
import time
import uuid
from pathlib import Path
from typing import Any

import httpx

from . import __version__
from .bundle import attest_bundle, write_bundle
from .evaluator import stage_verdict
from .identity_portable import Keystore
from .path_profiles import PathProfile, get_path_profile
from .receipt import make_receipt
from .records import EvidenceResult, RunRecord, StageResult, TownEvent, canonical_json, fingerprint

PROFILE_REF = "city-a2a-protocol@0.1"
EVALUATOR_VERSION = "path-city-a2a-protocol-0.1"
SOURCE_BASE = "17fbc7902be49683aee5f7610a0a1dcf8c803b3e"
STAGES = ("pinned_card", "structured_send", "acceptance_task", "exact_retry", "terminal_task")
LIMITATIONS = [
    "Synthetic same-host observer selected by demo policy; not independent operators or official Town accreditation.",
    "Protocol shape and one exact retry only; not Ethereum authorization, EIP-712 validity, or ownership verification.",
    "No certification of truthful venues, answer quality, or semantic task success.",
    "One observed retry is not global exactly-once execution.",
    "Replay evaluates retained observer records, not an independent rerun or proof the observer told the truth.",
]


class _Finding(Exception):
    def __init__(self, note: str, status: str = "failed"):
        super().__init__(note)
        self.status = status


def _require(condition: bool, note: str) -> None:
    if not condition:
        raise _Finding(note)


def _object(value: Any) -> dict:
    _require(isinstance(value, dict), "expected a JSON object")
    return value


def _text(value: Any, maximum: int = 256) -> bool:
    return isinstance(value, str) and 0 < len(value) <= maximum


def _digest(value: Any) -> bool:
    return isinstance(value, str) and re.fullmatch(r"0x[0-9a-f]{64}", value) is not None


def _utc_second(value: Any) -> bool:
    if not isinstance(value, str) or re.fullmatch(
        r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z", value
    ) is None:
        return False
    year, month, day, hour, minute, second = map(int, re.findall(r"[0-9]+", value))
    # City accepts proleptic Gregorian year 0000; datetime cannot represent it.
    return (1 <= month <= 12 and 1 <= day <= calendar.monthrange(year, month)[1]
            and hour < 24 and minute < 60 and second < 60)


def _strict_json(raw: bytes) -> Any:
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError("duplicate key")
            result[key] = value
        return result

    def constant(_value):
        raise ValueError("non-finite number")

    try:
        value = json.loads(raw.decode("utf-8"), object_pairs_hook=pairs, parse_constant=constant)
        stack = [(value, 0)]
        while stack:
            item, depth = stack.pop()
            if isinstance(item, (dict, list)):
                if depth >= 32:
                    raise ValueError("excessive JSON nesting")
                children = list(item.keys()) + list(item.values()) if isinstance(item, dict) else item
                stack.extend((child, depth + 1) for child in children)
            elif isinstance(item, str):
                item.encode("utf-8")
            elif isinstance(item, float) and not math.isfinite(item):
                raise ValueError("non-finite number")
        return value
    except (ValueError, UnicodeError, RecursionError):
        raise _Finding("invalid, ambiguous, or excessively nested JSON") from None


def _base64(value: Any, maximum: int) -> bytes:
    _require(isinstance(value, str) and len(value) <= 4 * ((maximum + 2) // 3),
             "missing or oversized Base64 bytes")
    try:
        raw = base64.b64decode(value, validate=True)
    except (ValueError, binascii.Error):
        raise _Finding("invalid Base64 bytes") from None
    _require(len(raw) <= maximum and base64.b64encode(raw).decode("ascii") == value,
             "noncanonical or oversized Base64 bytes")
    return raw


def _encoded(raw: bytes) -> str:
    return base64.b64encode(raw).decode("ascii")


def _url(value: Any) -> str:
    # Literal addresses only: no DNS, credentials, query, fragments, escapes,
    # ambiguous authority spelling, dot segments or URL normalization.
    _require(isinstance(value, str), "URL must be an exact literal loopback URL")
    match = re.fullmatch(r"http://(?:127\.0\.0\.1|\[::1\]):([1-9][0-9]{0,4})(/[A-Za-z0-9._~/-]*)", value)
    _require(match is not None, "URL must be an exact literal loopback URL")
    _require(int(match[1]) <= 65535 and not any(p in {".", ".."} for p in value.split("/")[3:])
             and str(httpx.URL(value)) == value, "noncanonical loopback URL")
    return value


def _envelope(value: Any, kind: str) -> dict:
    value = _object(value)
    _require(set(value) == {"version", "scheme", "signer", "payloadBase64", "signature"}
             and value["version"] == "0.1" and value["scheme"] == "eip712-eoa", "invalid City envelope shape")
    signer = _object(value["signer"])
    _require(set(signer) == {"method", "chainId", "address"}
             and signer["method"] == "eip155-eoa"
             and type(signer["chainId"]) is int and 0 < signer["chainId"] <= 2**53 - 1
             and isinstance(signer["address"], str)
             and re.fullmatch(r"0x[0-9a-f]{40}", signer["address"]) is not None,
             "invalid City signer shape")
    _require(isinstance(value["signature"], str)
             and re.fullmatch(r"0x[0-9a-f]{130}", value["signature"]) is not None,
             "invalid City signature shape (signature not verified)")
    statement = _object(_strict_json(_base64(value["payloadBase64"], 16_384 if kind == "request" else 4_096)))
    _require(statement.get("kind") == kind and statement.get("version") == "0.1", "wrong City statement kind")
    return statement


def _request(raw: bytes) -> tuple[dict, dict]:
    rpc = _object(_strict_json(raw))
    _require(set(rpc) == {"jsonrpc", "id", "method", "params"}
             and rpc["jsonrpc"] == "2.0" and rpc["method"] == "message/send",
             "expected structured message/send JSON-RPC request")
    _require(_text(rpc["id"]) or (type(rpc["id"]) is int and abs(rpc["id"]) <= 2**53 - 1), "invalid RPC id")
    params = _object(rpc["params"])
    message = _object(params.get("message"))
    _require(message.get("kind") == "message" and message.get("role") == "user"
             and _text(message.get("messageId")), "invalid City user message")
    parts = message.get("parts")
    _require(isinstance(parts, list) and len(parts) == 1, "expected one structured data part")
    part = _object(parts[0])
    data = _object(part.get("data"))
    _require(part.get("kind") == "data" and data.get("type") == "org.nandacity.city-request"
             and data.get("version") == "0.1", "wrong City request data type")
    config = _object(params.get("configuration", {}))
    _require(config.get("blocking", False) is False, "blocking send is outside this profile")
    statement = _envelope(data.get("envelope"), "request")
    _require(_text(statement.get("interactionId")), "missing City interaction ID")
    task_input = _object(statement.get("input"))
    _require(task_input.get("version") == "0.1" and task_input.get("capability") == "evening-plan"
             and task_input.get("city") in ("Chicago", "Boston"), "unsupported City request input shape")
    return rpc, statement


def _card(raw: bytes, subject_url: str, capability: str) -> dict:
    card = _object(_strict_json(raw))
    _require(card.get("url") == subject_url and card.get("protocolVersion") == "0.3.0"
             and card.get("preferredTransport") == "JSONRPC", "card descriptor or endpoint mismatch")
    _require(all(isinstance(card.get(key), list) and "application/json" in card[key]
                 for key in ("defaultInputModes", "defaultOutputModes")), "card lacks structured JSON modes")
    _require(isinstance(card.get("skills"), list) and any(isinstance(skill, dict) and skill.get("id") == capability
                                                       for skill in card["skills"]), "card lacks requested capability")
    return card


def _task(value: Any, request: dict) -> tuple[dict, dict]:
    task = _object(value)
    _require(task.get("kind") == "task" and _text(task.get("id")) and _text(task.get("contextId")),
             "missing task or context identity")
    state = _object(task.get("status")).get("state")
    _require(isinstance(state, str) and state in {"submitted", "working", "completed"},
             "task failed or has unsupported/incomplete state")
    metadata = _object(_object(task.get("metadata")).get("org.nandacity"))
    _require(metadata.get("subset") == "a2a-0.3-jsonrpc-loopback"
             and metadata.get("pollingAuthentication") == "none-loopback-only"
             and metadata.get("interactionId") == request["interactionId"], "City task metadata mismatch")
    acceptance = _envelope(metadata.get("acceptance"), "acceptance")
    _require(set(acceptance) == {"kind", "version", "requestDigest", "acceptanceId", "acceptedAt", "deadline"}
             and all(_digest(acceptance.get(key)) for key in ("requestDigest", "acceptanceId"))
             and all(_utc_second(acceptance.get(key)) for key in ("acceptedAt", "deadline")),
             "invalid acceptance statement shape")
    status_message = task["status"].get("message")
    if status_message is not None:
        message = _object(status_message)
        parts = message.get("parts")
        _require(message.get("kind") == "message" and message.get("role") == "agent"
                 and _text(message.get("messageId")) and isinstance(parts, list) and len(parts) == 1,
                 "invalid City status message")
        part = _object(parts[0])
        data = _object(part.get("data"))
        _require(part.get("kind") == "data" and data.get("type") == "org.nandacity.city-status"
                 and data.get("version") == "0.1" and data.get("acceptance") == metadata["acceptance"],
                 "status acceptance mismatch")
    return task, metadata


def _stable(task: dict, metadata: dict, original: dict, original_metadata: dict) -> None:
    _require(task["id"] == original["id"] and task["contextId"] == original["contextId"]
             and metadata["acceptance"] == original_metadata["acceptance"], "changed task, context, or acceptance on retry/poll")
    if original["status"]["state"] == "completed":
        _require(task["status"]["state"] == "completed"
                 and task.get("artifacts") == original.get("artifacts")
                 and metadata.get("completion") == original_metadata.get("completion"),
                 "previously completed task or result changed")


def _terminal(task: dict, metadata: dict) -> None:
    artifacts = task.get("artifacts")
    _require(isinstance(artifacts, list) and len(artifacts) == 1, "expected one City result artifact")
    artifact = _object(artifacts[0])
    parts = artifact.get("parts")
    _require(_text(artifact.get("artifactId")) and isinstance(parts, list) and len(parts) == 1, "invalid City artifact")
    part = _object(parts[0])
    data = _object(part.get("data"))
    _require(part.get("kind") == "data" and data.get("type") == "org.nandacity.city-result"
             and data.get("version") == "0.1", "missing City result envelope")
    _base64(data.get("answerBase64"), 1_048_576)
    completion = _envelope(data.get("completion"), "completion")
    _require(set(completion) == {"kind", "version", "outcome", "acceptanceDigest", "recordedAt", "answerDigest"}
             and completion.get("outcome") == "completed"
             and all(_digest(completion.get(key)) for key in ("acceptanceDigest", "answerDigest"))
             and _utc_second(completion.get("recordedAt"))
             and data["completion"] == metadata.get("completion"), "completion envelope mismatch")


def evaluate_city_path(profile: PathProfile, run_id: str, events: list[TownEvent]) -> EvidenceResult:
    """Pure replay of retained bodies and bounded observer transport records."""
    if profile.fingerprint() != get_path_profile(PROFILE_REF).fingerprint():
        raise ValueError("City evaluator requires its exact fixed profile")
    stages = [StageResult(name=name, status="not_tested") for name in STAGES]
    current = 0
    used_events = []
    cursor = 1
    byte_count = 0
    last_end = 0.0
    limits = profile.limits

    def pass_stage():
        stages[current].status = "passed"
        stages[current].evidence = list(used_events)

    def exchange(phase: str, url: str, request: bytes, rpc_id=None) -> Any:
        nonlocal cursor, byte_count, last_end
        if cursor >= len(events):
            raise _Finding("stage not reached or observation incomplete", "not_enough_evidence")
        event = events[cursor]
        cursor += 1
        used_events.append(event.event_id)
        if event.kind == "city_driver_error":
            raise _Finding("Town observer malfunction; not a subject failure", "error")
        detail = event.detail
        _require(event.kind == "city_http" and detail.get("phase") == phase and event.subject == url,
                 "unexpected exchange order or endpoint")
        _require(detail.get("method") == ("GET" if phase == "card" else "POST"), "unexpected HTTP method")
        sent = _base64(detail.get("request_base64"), int(limits["max_message_bytes"]))
        raw = _base64(detail.get("response_base64"), int(limits["max_card_bytes"] if phase == "card" else limits["max_message_bytes"]))
        not_sent = (detail.get("request_attempted") is False and sent == b"" and raw == b""
                    and detail.get("outcome") in ("byte_limit", "run_limit"))
        _require(not_sent or (detail.get("request_attempted") is True and sent == request), "request bytes changed")
        byte_count += len(sent) + len(raw)
        _require(byte_count <= limits["max_http_bytes"], "recorded HTTP byte budget exceeded")
        start, end = detail.get("started_seconds"), detail.get("ended_seconds")
        _require(all(type(value) in (int, float) and math.isfinite(value) for value in (start, end))
                 and last_end <= start <= end, "invalid exchange clock")
        last_end = end
        outcome = detail.get("outcome")
        if outcome == "driver_error":
            raise _Finding("Town observer malfunction; not a subject failure", "error")
        if outcome in {"timeout", "byte_limit", "run_limit"}:
            raise _Finding("bounded observation incomplete: " + outcome, "not_enough_evidence")
        if outcome == "transport_error":
            raise _Finding("HTTP transport failed")
        _require(outcome == "complete" and end - start <= limits["response_seconds"]
                 and end <= limits["run_seconds"], "response missing EOF or exceeds deadline")
        _require(detail.get("status_code") == 200, "unexpected HTTP status; redirects are not followed")
        decoded = _strict_json(raw)
        if phase == "card":
            return raw
        rpc = _object(decoded)
        _require(set(rpc) == {"jsonrpc", "id", "result"} and rpc["jsonrpc"] == "2.0"
                 and type(rpc["id"]) is type(rpc_id) and rpc["id"] == rpc_id,
                 "uncorrelated or unsuccessful JSON-RPC response")
        return rpc["result"]

    try:
        if not events:
            raise _Finding("no City observation", "not_enough_evidence")
        begin = events[0]
        used_events.append(begin.event_id)
        _require(begin.kind == "city_run" and all(e.run_id == run_id for e in events), "invalid City run evidence")
        config = begin.detail
        subject_url, card_url = _url(config.get("subject_url")), _url(config.get("card_url"))
        _require(begin.subject == subject_url, "observer subject mismatch")
        pinned = _base64(config.get("pinned_card_base64"), int(limits["max_card_bytes"]))
        request_bytes = _base64(config.get("request_base64"), int(limits["max_message_bytes"]))
        request, statement = _request(request_bytes)
        _card(pinned, subject_url, statement["input"]["capability"])
        observed = exchange("card", card_url, b"")
        _card(observed, subject_url, statement["input"]["capability"])
        _require(observed == pinned, "observed card differs from independently pinned exact bytes")
        pass_stage()
        current = 1
        response = exchange("send", subject_url, request_bytes, request["id"])
        pass_stage()
        current = 2
        original, original_metadata = _task(response, statement)
        if original["status"]["state"] == "completed":
            _terminal(original, original_metadata)
        pass_stage()
        current = 3
        retry, retry_metadata = _task(exchange("retry", subject_url, request_bytes, request["id"]), statement)
        _stable(retry, retry_metadata, original, original_metadata)
        if retry["status"]["state"] == "completed":
            _terminal(retry, retry_metadata)
        pass_stage()
        current = 4
        for poll in range(int(limits["max_polls"])):
            poll_request = {"jsonrpc": "2.0", "id": f"city-poll-{poll + 1}", "method": "tasks/get", "params": {"id": original["id"]}}
            observed, metadata = _task(exchange("poll", subject_url, canonical_json(poll_request).encode(), poll_request["id"]), statement)
            _stable(observed, metadata, original, original_metadata)
            _stable(observed, metadata, retry, retry_metadata)
            if observed["status"]["state"] == "completed":
                _terminal(observed, metadata)
                if cursor + 1 == len(events) and events[cursor].kind == "city_driver_error":
                    used_events.append(events[cursor].event_id)
                    raise _Finding("Town observer malfunction; not a subject failure", "error")
                _require(cursor == len(events), "unexpected evidence after terminal task")
                pass_stage()
                break
        else:
            raise _Finding("poll budget exhausted without completion", "not_enough_evidence")
    except _Finding as finding:
        stages[current].status = finding.status
        stages[current].note = str(finding)
        stages[current].evidence = list(used_events)
    return EvidenceResult(run_id=run_id, evaluator_version=EVALUATOR_VERSION, stages=stages,
                          verdict=stage_verdict(stages), evaluated_at=time.time())


async def run_city_path(*, subject_url: str, card_url: str, pinned_card: bytes,
                        request_bytes: bytes, out_dir: str,
                        observer_key_dir: str, observer_name: str) -> tuple[str, EvidenceResult]:
    """Observe exact caller-supplied synthetic bytes. Never use a default key."""
    profile = get_path_profile(PROFILE_REF)
    limits = profile.limits
    try:
        _url(subject_url)
        _url(card_url)
        _require(isinstance(pinned_card, bytes) and len(pinned_card) <= limits["max_card_bytes"], "pinned card too large")
        _require(isinstance(request_bytes, bytes) and len(request_bytes) <= limits["max_message_bytes"], "request too large")
        request, statement = _request(request_bytes)
        parsed_card = _card(pinned_card, subject_url, statement["input"]["capability"])
        _require(_text(observer_key_dir, 4096) and _text(observer_name)
                 and observer_name.isprintable() and observer_name != subject_url,
                 "explicit observer keystore and distinct printable observer name required")
    except _Finding as finding:
        raise ValueError(str(finding)) from None
    run_id = "city-path-" + uuid.uuid4().hex[:12]
    started = time.time()
    clock_start = time.monotonic()
    events: list[TownEvent] = []
    byte_count = 0

    def emit(kind, subject, detail):
        events.append(TownEvent(event_id=f"{run_id}-e{len(events) + 1}", run_id=run_id,
                                at=time.time(), observer=observer_name, kind=kind, subject=subject, detail=detail))

    emit("city_run", subject_url, {"subject_url": subject_url, "card_url": card_url,
                                 "pinned_card_base64": _encoded(pinned_card), "request_base64": _encoded(request_bytes)})

    async def exchange(client, phase, url, request_body):
        nonlocal byte_count
        body = bytearray()
        detail = {"phase": phase, "method": "GET" if phase == "card" else "POST",
                  "request_base64": "", "request_attempted": False, "status_code": None,
                  "started_seconds": time.monotonic() - clock_start, "outcome": "complete"}
        budget = min(int(limits["max_card_bytes"] if phase == "card" else limits["max_message_bytes"]),
                     int(limits["max_http_bytes"]) - byte_count - len(request_body))
        remaining = limits["run_seconds"] - detail["started_seconds"]
        try:
            if budget < 0 or remaining <= 0:
                detail["outcome"] = "run_limit" if remaining <= 0 else "byte_limit"
                return
            detail["request_base64"] = _encoded(request_body)
            detail["request_attempted"] = True
            byte_count += len(request_body)
            async with asyncio.timeout(min(limits["response_seconds"], remaining)):
                async with client.stream(detail["method"], url, content=request_body,
                                         headers={"Content-Type": "application/json", "Accept-Encoding": "identity"}) as response:
                    detail["status_code"] = response.status_code
                    async for chunk in response.aiter_raw():
                        room = budget - len(body)
                        body.extend(chunk[:room])
                        byte_count += min(len(chunk), room)
                        if len(chunk) > room:
                            detail["outcome"] = "byte_limit"
                            break
        except TimeoutError:
            detail["outcome"] = "timeout"
        except httpx.HTTPError:
            detail["outcome"] = "transport_error"
        except Exception:
            detail["outcome"] = "driver_error"
        finally:
            detail["response_base64"] = _encoded(bytes(body))
            detail["ended_seconds"] = time.monotonic() - clock_start
            emit("city_http", url, detail)

    def status(index):
        return evaluate_city_path(profile, run_id, events).stages[index].status

    try:
        async with httpx.AsyncClient(trust_env=False, follow_redirects=False, timeout=limits["response_seconds"]) as client:
            await exchange(client, "card", card_url, b"")
            if status(0) == "passed":
                await exchange(client, "send", subject_url, request_bytes)
                if status(2) == "passed":
                    await exchange(client, "retry", subject_url, request_bytes)
                    if status(3) == "passed":
                        first = _strict_json(_base64(events[2].detail["response_base64"], int(limits["max_message_bytes"])))
                        for poll in range(int(limits["max_polls"])):
                            body = canonical_json({"jsonrpc": "2.0", "id": f"city-poll-{poll + 1}", "method": "tasks/get",
                                                   "params": {"id": first["result"]["id"]}}).encode()
                            await exchange(client, "poll", subject_url, body)
                            result = evaluate_city_path(profile, run_id, events)
                            if result.verdict != "incomplete" or events[-1].detail["outcome"] != "complete":
                                break
                            await asyncio.sleep(0.05)
    except asyncio.CancelledError:
        # No signing or bundle creation after external cancellation.
        raise
    except Exception:
        emit("city_driver_error", subject_url, {"reason": "internal observer error"})

    result = evaluate_city_path(profile, run_id, events)
    source_files = {name: "sha256:" + hashlib.sha256(Path(__file__).with_name(name).read_bytes()).hexdigest()
                    for name in ("city_path.py", "path_profiles.py", "path_runner.py")}
    run = RunRecord(run_id=run_id, profile_name=profile.ref, profile_fingerprint=profile.fingerprint(),
                    created_at=started, participants=[{"name": observer_name, "role": "observer"},
                                                     {"name": subject_url, "role": "subject"}],
                    releases={"nandatown": __version__, "evaluator": EVALUATOR_VERSION,
                              "python": sys.version.split()[0], "town_source_base": SOURCE_BASE,
                              "city_observer_sources": fingerprint(source_files)},
                    config={"mode": "path", "subject": subject_url, "card_url": card_url,
                            "pinned_card_digest": fingerprint(parsed_card), "source_files": source_files,
                            "pinned_card_bytes_sha256": "sha256:" + hashlib.sha256(pinned_card).hexdigest(),
                            "synthetic": True, "limitations": LIMITATIONS})
    directory = str(Path(out_dir) / run_id)
    write_bundle(directory, profile, run, [], events, result, mode="path")
    keystore = Keystore(observer_key_dir)
    attest_bundle(directory, keystore=keystore, signer=observer_name)
    make_receipt(directory, keystore=keystore, signer=observer_name, limitations=LIMITATIONS)
    return directory, result


def main(argv=None) -> int:
    import argparse

    parser = argparse.ArgumentParser(description=__doc__)
    for flag in ("subject-url", "card-url", "pinned-card", "request", "out-dir", "observer-key-dir", "observer-name"):
        parser.add_argument("--" + flag, required=True)
    args = parser.parse_args(argv)

    def read_bounded(path, limit):
        with open(path, "rb") as stream:
            content = stream.read(limit + 1)
        if len(content) > limit:
            raise ValueError("input file exceeds profile byte limit")
        return content

    try:
        directory, result = asyncio.run(run_city_path(
            subject_url=args.subject_url, card_url=args.card_url,
            pinned_card=read_bounded(args.pinned_card, 65_536),
            request_bytes=read_bounded(args.request, 1_048_576), out_dir=args.out_dir,
            observer_key_dir=args.observer_key_dir, observer_name=args.observer_name))
    except (ValueError, OSError) as exc:
        parser.error(str(exc))
    print(json.dumps({"bundle": directory, "receipt": str(Path(directory) / "receipt.json"), "verdict": result.verdict}))
    return 0 if result.verdict == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
