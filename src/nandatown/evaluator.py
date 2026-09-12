"""The pinned stage evaluator.

Each stage is a separate claim with a separate failure boundary.
Acceptance, claiming, receipt, processing, response, and semantic
correctness are always judged separately; an HTTP success response never
becomes proof that the agent understood or completed the task. Missing
evidence stays missing: it is reported as Not enough evidence, never
inferred.
"""

from __future__ import annotations

import time

from .records import (
    EvidenceResult,
    StageResult,
    TestProfile,
    TownEvent,
    canonical_json,
    fingerprint,
    json_type,
)

EVALUATOR_VERSION = "0.4.0"
# Recorded bundles replay under the rules that produced them. 0.2.0 took
# the first accepted quote response; it neither counted responses nor
# checked which request a response named. 0.3.0 added those checks but
# read any truthy acknowledgement flag as a yes, and judged only the
# first accepted request.
LEGACY_EVALUATOR_VERSION = "0.2.0"
CORRELATION_EVALUATOR_VERSION = "0.3.0"
EVALUATOR_VERSIONS = (LEGACY_EVALUATOR_VERSION,
                      CORRELATION_EVALUATOR_VERSION,
                      EVALUATOR_VERSION)

REQUEST_KIND = "quote_request"
RESPONSE_KIND = "quote_response"
# The quote.read skill: a quote_response carries the request id. The town
# records the body's request_id on message_accepted: verbatim while it is a
# short string, otherwise as a bounded digest (JSON type, JSON text length,
# fingerprint of the full value) under CORRELATION_DIGEST_FIELD.
CORRELATION_FIELD = "request_id"
CORRELATION_DIGEST_FIELD = "request_id_digest"
JSON_TYPES = ("string", "number", "boolean", "null", "array", "object")
# A stage note shows at most this many characters of a recorded value.
NOTE_VALUE_CHARS = 80


def _passed(name: str, evidence: list[str], note: str = "") -> StageResult:
    return StageResult(name=name, status="passed", evidence=evidence, note=note)


def _failed(name: str, evidence: list[str], note: str) -> StageResult:
    return StageResult(name=name, status="failed", evidence=evidence, note=note)


def _missing(name: str, note: str) -> StageResult:
    return StageResult(name=name, status="not_enough_evidence", evidence=[],
                       note=note)


def _response_mismatch(responses: list[TownEvent],
                       requests: list[TownEvent]) -> tuple[str, str] | None:
    """Why the accepted quote responses cannot stand as the one answer to
    the accepted request, as (status, note). None when they can, or when
    there is nothing to judge yet (that stays missing).

    Each distinct message identity is accepted once; an idempotent resend
    of the same identity and content is a replay, not a second response.
    """
    request_id = requests[0].subject if requests else None
    if len(responses) > 1 and len(requests) > 1:
        # Not the one exchange the profile expects, but not a seller that
        # answered one request twice either: say what was accepted.
        return "failed", (f"{len(requests)} quote requests and"
                          f" {len(responses)} distinct quote responses were"
                          " accepted; the profile expects exactly one of each"
                          " (an idempotent resend of one identity is not"
                          " counted)")
    if len(responses) > 1:
        return "failed", (f"{len(responses)} distinct quote responses were"
                          " accepted, expected one (an idempotent resend of"
                          " one identity is not counted)")
    if not responses or request_id is None:
        return None
    detail = responses[0].detail
    if CORRELATION_FIELD in detail:
        named = detail[CORRELATION_FIELD]
        if isinstance(named, str) and named == request_id:
            return None
        shape, shown = ("string" if isinstance(named, str) else "other",
                        _show_value(named))
    elif CORRELATION_DIGEST_FIELD in detail:
        digest = detail[CORRELATION_DIGEST_FIELD]
        if (isinstance(digest, dict) and digest.get("type") == "string"
                and digest.get("fingerprint") == fingerprint(request_id)):
            return None
        shape, shown = _show_digest(digest)
    else:
        return "not_enough_evidence", (
            "the quote response carries no request_id, so it is not shown"
            " to answer the accepted request")
    accepted = _show_value(request_id)
    if shape == "string":
        return "failed", (f"the quote response names request {shown}, not"
                          f" the accepted request {accepted}")
    if shape == "malformed":
        return "failed", (f"the quote response's recorded request_id digest"
                          f" {shown} is malformed and names no request; the"
                          f" accepted request is {accepted}")
    return "failed", (f"the quote response's request_id is {shown}, which is"
                      " not a string and names no request; the accepted"
                      f" request is {accepted}")


def _asserted(note: object, field: str) -> bool | None:
    """The boolean a participant asserted for field, or None for no
    assertion this evaluator can read.

    A flag counts only when it is a boolean. Anything else says nothing
    about the work: read as truthiness, the string "false" and the
    number 1 both mean yes, which is how a participant could deny doing
    something and be recorded as having done it.
    """
    if not isinstance(note, dict):
        return None
    value = note.get(field)
    return value if isinstance(value, bool) else None


def _unreadable_flags(acks: list[TownEvent], field: str) -> list[TownEvent]:
    """Acknowledgements that state field as something other than a
    boolean. Their authors meant to say something; the note says what
    was recorded so an operator can see what to fix."""
    return [a for a in acks
            if isinstance(a.detail.get("note"), dict)
            and field in a.detail["note"]
            and not isinstance(a.detail["note"][field], bool)]


def _flag_note(acks: list[TownEvent], field: str, what: str) -> str:
    shown = _show_value(acks[0].detail["note"][field])
    return (f"{what} records {field} as {shown}, which is not a boolean"
            " and states nothing about the work")


def _show_value(value: object) -> str:
    """A recorded value for a stage note: its JSON text when short (JSON
    null for null), otherwise its first NOTE_VALUE_CHARS characters, its
    length and its fingerprint. Notes stay small whatever was recorded."""
    text = canonical_json(value)
    if not isinstance(value, str):
        text = ("JSON null" if value is None
                else f"a JSON {json_type(value)} {text}")
    if len(text) <= NOTE_VALUE_CHARS:
        return text
    return (f"{text[:NOTE_VALUE_CHARS]}… ({len(canonical_json(value))}"
            f" JSON characters, {fingerprint(value)[:23]}…)")


def _show_digest(digest: object) -> tuple[str, str]:
    """(shape, text) for a recorded request_id digest; shape is string,
    other or malformed."""
    if not (isinstance(digest, dict) and digest.get("type") in JSON_TYPES
            and type(digest.get("json_length")) is int
            and 0 <= digest["json_length"] < 10 ** 15
            and isinstance(digest.get("fingerprint"), str)):
        return "malformed", _show_value(digest)
    if digest["type"] == "null":
        return "other", "JSON null"
    return ("string" if digest["type"] == "string" else "other",
            f"a JSON {digest['type']} of {digest['json_length']} JSON"
            f" characters ({digest['fingerprint'][:23]}…)")


def evaluate(profile: TestProfile, run_id: str, events: list[TownEvent],
             version: str = EVALUATOR_VERSION) -> EvidenceResult:
    if version not in EVALUATOR_VERSIONS:
        raise ValueError(f"unsupported Track evaluator version {version!r}")
    # Only the current rules require a flag to be a boolean. Earlier ones
    # read truthiness, and a bundle they recorded still replays that way.
    strict_flags = version == EVALUATOR_VERSION
    seller = next((n for n, r in profile.roles.items() if r == "seller"), "seller")
    buyer = next((n for n, r in profile.roles.items() if r == "buyer"), "buyer")

    def find(ekind: str, **conds) -> list[TownEvent]:
        out = []
        for e in events:
            if e.kind != ekind:
                continue
            if "observer" in conds and e.observer != conds["observer"]:
                continue
            if "subject" in conds and e.subject != conds["subject"]:
                continue
            ok = True
            for key, val in conds.items():
                if key in ("observer", "subject"):
                    continue
                if e.detail.get(key) != val:
                    ok = False
                    break
            if ok:
                out.append(e)
        return out

    accepted_req = find("message_accepted", kind=REQUEST_KIND)
    request_id = accepted_req[0].subject if accepted_req else None

    stages: list[StageResult] = []

    # accepted: the town committed the request before reporting success.
    if accepted_req:
        stages.append(_passed("accepted", [accepted_req[0].event_id]))
    else:
        stages.append(_missing("accepted", "no accepted quote request"))

    # claimed: a seller claimed the request under a lease.
    claims = find("message_claimed", subject=request_id) if request_id else []
    if claims:
        stages.append(_passed("claimed", [c.event_id for c in claims]))
    else:
        stages.append(_missing("claimed", "the request was never claimed"))

    # received: the seller acknowledged the request through a valid fence.
    seller_acks = (find("ack_recorded", observer=seller, subject=request_id)
                   if request_id else [])
    received = [a for a in seller_acks
                if a.detail.get("status") in ("received", "processed")]
    if received:
        stages.append(_passed("received", [received[0].event_id]))
    else:
        stages.append(_missing("received",
                               "no acknowledged receipt by the seller"))

    # processed: the seller applied the task exactly once on its own side.
    processed = [a for a in seller_acks if a.detail.get("status") == "processed"]
    applied = [a for a in processed
               if (_asserted(a.detail.get("note"), "applied") is True
                   if strict_flags
                   else a.detail.get("note", {}).get("applied"))]
    unreadable = (_unreadable_flags(processed, "applied") if strict_flags
                  else [])
    if not processed:
        stages.append(_missing("processed", "no processed acknowledgement"))
    elif len(applied) == 1:
        stages.append(_passed("processed", [applied[0].event_id]))
    elif len(applied) == 0 and unreadable:
        stages.append(_missing(
            "processed",
            _flag_note(unreadable, "applied",
                       "a processed acknowledgement")))
    elif len(applied) == 0:
        stages.append(_missing("processed",
                               "processed acknowledgements carry no"
                               " application record"))
    else:
        stages.append(_failed("processed", [a.event_id for a in applied],
                              f"applied {len(applied)} times, expected once"))

    # response: the quote response was accepted and reached the buyer.
    accepted_resp = find("message_accepted", kind=RESPONSE_KIND)
    response_id = accepted_resp[0].subject if accepted_resp else None
    buyer_claims = (find("message_claimed", subject=response_id,
                         claimant=buyer) if response_id else [])
    mismatch = (None if version == LEGACY_EVALUATOR_VERSION
                else _response_mismatch(accepted_resp, accepted_req))
    if mismatch is not None and mismatch[0] == "failed":
        # Several requests are part of why several responses fail.
        cited = (accepted_req if len(accepted_req) > 1
                 and len(accepted_resp) > 1 else [])
        stages.append(_failed("response",
                              [e.event_id for e in cited + accepted_resp],
                              mismatch[1]))
    elif mismatch is not None:
        stages.append(_missing("response", mismatch[1]))
    elif accepted_resp and buyer_claims:
        stages.append(_passed("response", [accepted_resp[0].event_id,
                                           buyer_claims[0].event_id]))
    else:
        stages.append(_missing("response",
                               "no quote response accepted and claimed by"
                               " the buyer"))

    # correct: the buyer's own assertion about the total.
    buyer_acks = (find("ack_recorded", observer=buyer, subject=response_id)
                  if response_id else [])
    if mismatch is not None:
        # The assertion may concern any of the responses in question.
        buyer_acks = [a for r in accepted_resp
                      for a in find("ack_recorded", observer=buyer,
                                    subject=r.subject)]
    verdict_acks = [a for a in buyer_acks
                    if "correct" in a.detail.get("note", {})]
    unreadable_verdicts = (_unreadable_flags(verdict_acks, "correct")
                           if strict_flags else [])
    if strict_flags:
        verdict_acks = [a for a in verdict_acks
                        if _asserted(a.detail.get("note"), "correct")
                        is not None]
    if not verdict_acks and unreadable_verdicts:
        stages.append(_missing(
            "correct",
            _flag_note(unreadable_verdicts, "correct",
                       "the buyer's acknowledgement")))
    elif verdict_acks and mismatch is not None and mismatch[0] == "failed":
        stages.append(_failed(
            "correct", [a.event_id for a in verdict_acks],
            "the buyer's assertion cannot establish the answer to the"
            f" accepted request: {mismatch[1]}"))
    elif (verdict_acks and mismatch is not None
          and verdict_acks[0].detail["note"]["correct"]):
        stages.append(_missing(
            "correct", "the buyer's assertion concerns a response not shown"
                       f" to answer the accepted request: {mismatch[1]}"))
    elif verdict_acks:
        note = verdict_acks[0].detail["note"]
        if note["correct"]:
            stages.append(_passed("correct", [verdict_acks[0].event_id]))
        else:
            stages.append(_failed(
                "correct", [verdict_acks[0].event_id],
                f"buyer observed total {note.get('total_cents')} against"
                f" expected {profile.task.expected_total_cents}"))
    else:
        stages.append(_missing("correct", "the buyer made no correctness"
                                          " assertion"))

    # Fault checks apply only when the profile names the fault.
    fault = profile.fault
    if fault == "crash_after_claim":
        ended_early = (find("claim_expired", subject=request_id)
                       + find("stale_fence_rejected", subject=request_id)
                       if request_id else [])
        reclaimed = [c for c in claims if c.detail.get("attempt", 1) >= 2]
        restarts = find("participant_restarted")
        if ended_early and reclaimed:
            evidence = ([e.event_id for e in ended_early]
                        + [reclaimed[0].event_id]
                        + [r.event_id for r in restarts])
            stages.append(_passed("recovered_after_restart", evidence))
        else:
            stages.append(_missing("recovered_after_restart",
                                   "no lease end followed by redelivery"))
        fences = (find("stale_fence_rejected", subject=request_id)
                  if request_id else [])
        if fences:
            stages.append(_passed("stale_fence_rejected",
                                  [f.event_id for f in fences]))
        else:
            stages.append(_missing("stale_fence_rejected",
                                   "no stale fence was rejected"))
    elif fault == "duplicate_delivery":
        offered = find("duplicate_offered")
        recognized = [a for a in seller_acks
                      if (_asserted(a.detail.get("note"), "duplicate") is True
                          if strict_flags
                          else a.detail.get("note", {}).get("duplicate"))]
        if offered and recognized and len(applied) == 1:
            stages.append(_passed("duplicate_recognized",
                                  [offered[0].event_id,
                                   recognized[0].event_id]))
        else:
            stages.append(_missing("duplicate_recognized",
                                   "no duplicate offer recognized exactly"
                                   " once"))
    elif fault == "drop_wakeup":
        suppressed = find("notify_suppressed")
        if suppressed and claims:
            stages.append(_passed("wakeup_loss_tolerated",
                                  [suppressed[0].event_id,
                                   claims[0].event_id]))
        else:
            stages.append(_missing("wakeup_loss_tolerated",
                                   "no suppressed wake-up followed by a"
                                   " claim"))
    elif fault == "lost_ack":
        dropped = find("ack_dropped")
        if dropped and processed:
            stages.append(_passed("ack_retry_survived",
                                  [dropped[0].event_id,
                                   processed[0].event_id]))
        else:
            stages.append(_missing("ack_retry_survived",
                                   "no dropped acknowledgement followed by"
                                   " a recorded retry"))
    elif fault == "tool_error":
        errored = [e for e in events if e.kind == "ack_recorded"
                   and e.detail.get("note", {})
                   .get("tool_errors", 0) >= 1]
        if errored and processed:
            stages.append(_passed(
                "tool_error_survived",
                [e.event_id for e in errored[:4]],
                "a lost tool result was noticed, retried, and the task"
                " still completed"))
        else:
            stages.append(_missing(
                "tool_error_survived",
                "no participant reported recovering from a lost tool"
                " result"))
    elif fault == "context_truncation":
        truncated = [e for e in events if e.kind == "ack_recorded"
                     and e.detail.get("note", {})
                     .get("context_truncations", 0) >= 1]
        if truncated and processed:
            stages.append(_passed(
                "truncation_survived",
                [e.event_id for e in truncated[:4]],
                "the agents reported losing context and still completed"))
        else:
            stages.append(_missing(
                "truncation_survived",
                "no participant reported a context truncation"))

    verified = find("portable_identity_verified")
    if verified:
        agent_ids = sorted({e.detail.get("agent_id", "?")
                            for e in verified})
        stages.append(StageResult(
            name="portable_identity", status="passed",
            evidence=[e.event_id for e in verified[:4]],
            note="run grants verified against pinned controller keys"
                 f" for {', '.join(agent_ids)}"))
    else:
        stages.append(StageResult(
            name="portable_identity", status="not_tested",
            note="this run used short-lived join tokens; rerun with"
                 " --identity for grant-based portable identity"))

    cascade_unreached(stages)
    return EvidenceResult(run_id=run_id, evaluator_version=version,
                          stages=stages, verdict=stage_verdict(stages),
                          evaluated_at=time.time())


def cascade_unreached(stages: list[StageResult]) -> None:
    """After the first failed or errored stage, an inconclusive later
    stage was simply never reached: report it not_tested, never let a
    broken boundary masquerade as many independent inconclusives."""
    broken = False
    for stage in stages:
        if broken and stage.status == "not_enough_evidence":
            stage.status = "not_tested"
            stage.note = ("not reached: an earlier boundary broke"
                          + (f" ({stage.note})" if stage.note else ""))
        if stage.status in ("failed", "error"):
            broken = True


def stage_verdict(stages: list[StageResult]) -> str:
    """ERROR means the town malfunctioned; it outranks blaming the
    subject. Missing evidence never becomes a pass."""
    applicable = [s for s in stages if s.status != "not_tested"]
    if any(s.status == "error" for s in applicable):
        return "error"
    if any(s.status == "failed" for s in applicable):
        return "failed"
    if applicable and all(s.status == "passed" for s in applicable):
        return "passed"
    return "incomplete"
