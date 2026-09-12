"""What the Track evaluator will and will not read as an assertion.

Two things a run could previously get away with: acknowledging with a
flag that is not a boolean, so a string saying "false" counted as yes;
and leaving an accepted request unanswered, which the verdict ignored
because only the first request was ever looked at.
"""

import pytest

from nandatown.evaluator import (
    CORRELATION_EVALUATOR_VERSION,
    EVALUATOR_VERSION,
    LEGACY_EVALUATOR_VERSION,
    evaluate,
)

from test_evaluator import clean_events, ev, profile, stage

# Truthy in Python, and none of them an assertion that the work was done.
NOT_A_YES = [
    pytest.param("false", id="string-false"),
    pytest.param("true", id="string-true"),
    pytest.param(1, id="integer-one"),
    pytest.param(1.0, id="float-one"),
    pytest.param("no", id="string-no"),
    pytest.param([False], id="list"),
    pytest.param({"applied": True}, id="object"),
]
HISTORICAL = [LEGACY_EVALUATOR_VERSION, CORRELATION_EVALUATOR_VERSION]


UNCHANGED = object()


def events_with(seller_note=UNCHANGED, buyer_note=UNCHANGED):
    """The clean run with one or both acknowledgement notes replaced.

    A sentinel rather than None, because None is one of the notes worth
    testing.
    """
    replacement = {"seller": seller_note, "buyer": buyer_note}
    out = []
    for event in clean_events():
        note = replacement.get(event.observer, UNCHANGED)
        if event.kind == "ack_recorded" and note is not UNCHANGED:
            event = event.model_copy(update={
                "detail": dict(event.detail, note=note)})
        out.append(event)
    return out


@pytest.mark.parametrize("value", NOT_A_YES)
def test_a_non_boolean_applied_flag_proves_no_application(value):
    events = events_with(seller_note={"applied": value, "total_cents": 3990})

    result = evaluate(profile(), "run-1", events)

    processed = stage(result, "processed")
    assert processed.status == "not_enough_evidence", processed
    assert result.verdict != "passed"


@pytest.mark.parametrize("value", NOT_A_YES)
def test_a_non_boolean_correct_flag_is_no_assertion(value):
    events = events_with(buyer_note={"correct": value, "total_cents": 3990})

    result = evaluate(profile(), "run-1", events)

    correct = stage(result, "correct")
    assert correct.status == "not_enough_evidence", correct
    assert result.verdict != "passed"


@pytest.mark.parametrize("value", NOT_A_YES)
@pytest.mark.parametrize("version", HISTORICAL)
def test_historical_evaluators_still_read_those_runs_their_own_way(value,
                                                                   version):
    """A recorded bundle replays under the rules that produced it."""
    events = events_with(seller_note={"applied": value, "total_cents": 3990},
                         buyer_note={"correct": value, "total_cents": 3990})

    result = evaluate(profile(), "run-1", events, version=version)

    assert result.evaluator_version == version
    assert result.verdict == "passed"


def test_real_booleans_are_unchanged():
    assert evaluate(profile(), "run-1", clean_events()).verdict == "passed"

    refused = evaluate(profile(), "run-1", events_with(
        seller_note={"applied": False, "total_cents": 3990}))
    assert stage(refused, "processed").status == "not_enough_evidence"

    wrong = evaluate(profile(), "run-1", events_with(
        buyer_note={"correct": False, "total_cents": 4000}))
    assert stage(wrong, "correct").status == "failed"
    assert wrong.verdict == "failed"


def two_requests_one_answered():
    """The buyer asked twice; only the first was ever taken up."""
    events = clean_events()
    return events[:4] + [
        ev(5, "message_accepted", "q-2", kind="quote_request",
           sender="buyer", to="seller"),
    ] + events[4:]


def test_an_accepted_request_nobody_answered_leaves_the_run_incomplete():
    result = evaluate(profile(), "run-1", two_requests_one_answered())

    assert result.verdict == "incomplete", [
        (s.name, s.status, s.note) for s in result.stages]
    for name in ("claimed", "received", "processed", "response"):
        assert stage(result, name).status == "not_enough_evidence", name
        assert "q-2" in stage(result, name).note, name
    # The first request was answered, and the evidence still says so.
    assert stage(result, "accepted").status == "passed"


@pytest.mark.parametrize("version", HISTORICAL)
def test_historical_evaluators_still_pass_that_run(version):
    result = evaluate(profile(), "run-1", two_requests_one_answered(),
                      version=version)

    assert result.evaluator_version == version
    assert result.verdict == "passed"


# The fault stages count rather than assert, and a count read with >=
# raises on a string instead of answering.
NOT_A_COUNT = [
    pytest.param("many", id="string"),
    pytest.param(None, id="null"),
    pytest.param([1], id="list"),
    pytest.param({"n": 1}, id="object"),
    pytest.param(True, id="boolean"),
]
COUNTERS = [("tool_error", "tool_errors", "tool_error_survived"),
            ("context_truncation", "context_truncations",
             "truncation_survived")]


@pytest.mark.parametrize("value", NOT_A_COUNT)
@pytest.mark.parametrize("fault, field, stage_name", COUNTERS)
def test_a_non_integer_count_is_no_report(value, fault, field, stage_name):
    """A participant can send this, so it must not end the run.

    The note is unvalidated, so a subject reaching the coordinator can
    put anything here. Comparing a string with >= raises, which would
    surface as Town failing rather than the subject saying nothing.
    """
    events = events_with(
        seller_note={"applied": True, "total_cents": 3990, field: value})

    result = evaluate(profile(fault), "run-1", events)

    assert stage(result, stage_name).status == "not_enough_evidence"


@pytest.mark.parametrize("fault, field, stage_name", COUNTERS)
def test_a_real_count_still_reports(fault, field, stage_name):
    events = events_with(
        seller_note={"applied": True, "total_cents": 3990, field: 2})

    result = evaluate(profile(fault), "run-1", events)

    assert stage(result, stage_name).status == "passed"


@pytest.mark.parametrize("note", [5, "correct", ["correct"], None, True])
def test_a_note_that_is_not_an_object_carries_no_assertion(note):
    """The coordinator refuses these, so only a crafted bundle has one.

    It should replay to an honest verdict rather than raise, because a
    raise here is reported as Town failing on its own evidence.
    """
    result = evaluate(profile(), "run-1", events_with(buyer_note=note))

    assert stage(result, "correct").status in ("not_enough_evidence",
                                               "not_tested")
    assert result.verdict != "passed"


def test_a_request_for_someone_else_is_not_the_sellers_to_answer():
    """These stages judge the seller, so only its own requests count."""
    events = clean_events()
    events = events[:4] + [
        ev(5, "message_accepted", "q-2", kind="quote_request",
           sender="buyer", to="auditor"),
    ] + events[4:]

    result = evaluate(profile(), "run-1", events)

    assert result.verdict == "passed", [
        (s.name, s.status, s.note) for s in result.stages]


def test_a_response_naming_another_accepted_request_says_which_it_judged():
    """Both requests were accepted; the note must not deny that."""
    events = two_requests_one_answered()
    events = [e.model_copy(update={"detail": dict(e.detail,
                                                  request_id="q-2")})
              if e.kind == "message_accepted" and e.subject == "r-1"
              else e
              for e in events]

    note = stage(evaluate(profile(), "run-1", events), "response").note

    assert "the first accepted request" in note, note
