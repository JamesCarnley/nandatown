"""Town evidence files are UTF-8 whatever the process locale.

pydantic writes non-ASCII characters, U+2028 included, unescaped into the
JSON records. A text-mode open() without an encoding follows the locale,
so under a non-UTF-8 locale (Windows cp1252, or a POSIX C locale with
UTF-8 mode off) Town could not write such a bundle, and a bundle written
as UTF-8 did not read back. These tests write and read bundles in a child
process whose default text encoding is ASCII.
"""

import codecs
import json
import os
import subprocess
import sys
from pathlib import Path

from nandatown import __version__
from nandatown.bundle import (
    attest_bundle,
    load_bundle,
    verify_bundle,
    write_bundle,
)
from nandatown.evaluator import EVALUATOR_VERSION, evaluate
from nandatown.mirror import mirror_bundle, recover_bundle
from nandatown.receipt import make_receipt, verify_receipt
from nandatown.records import RunRecord, fingerprint

from test_evaluator import clean_events, profile

CARD_NAME = "Agent Zoë — 東京 Desk"
TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
# PYTHONUTF8=0 turns off the UTF-8 mode Python enables in a C locale, and
# PYTHONCOERCECLOCALE=0 stops Linux coercing the C locale to C.UTF-8.
# LC_ALL overrides any LC_CTYPE or LANG the caller has set.
NON_UTF8_LOCALE = {"PYTHONUTF8": "0", "PYTHONCOERCECLOCALE": "0",
                   "LC_ALL": "C", "LANG": "C"}
RECORDS = ["profile.json", "run.json", "intents.jsonl", "events.jsonl"]


def sample_events():
    events = clean_events()
    events[6].detail["note"]["card_name"] = CARD_NAME
    return events


def write_sample_bundle(directory):
    """A Track bundle whose profile, intents and events hold CARD_NAME,
    attested and receipted."""
    p = profile().model_copy(update={"name": f"quote {CARD_NAME}"})
    events = sample_events()
    run = RunRecord(
        run_id="run-1", profile_name=p.name,
        profile_fingerprint=fingerprint(p.model_dump()), created_at=1.0,
        participants=[{"name": "buyer", "role": "buyer"},
                      {"name": "seller", "role": "seller"}],
        releases={"nandatown": __version__, "evaluator": EVALUATOR_VERSION},
    )
    intents = [{"intent_id": "in-1", "run_id": "run-1", "at": 1.0,
                "actor": "seller", "action": "ack",
                "payload": {"card_name": CARD_NAME}}]
    write_bundle(directory, p, run, intents, events,
                 evaluate(p, "run-1", events))
    attest_bundle(directory)
    make_receipt(directory)


def check_bundle(directory, scratch):
    """Read, verify, mirror and recover a bundle; report what was seen."""
    fingerprint_ = load_bundle(directory)["manifest"]["bundle_fingerprint"]
    mirror = os.path.join(scratch, "mirror")
    mirror_bundle(directory, mirror)
    restored = recover_bundle(fingerprint_, [mirror],
                              os.path.join(scratch, "fresh"))
    return {
        "events": [e.model_dump() for e in load_bundle(directory)["events"]],
        "verify": verify_bundle(directory),
        "receipt": verify_receipt(os.path.join(directory, "receipt.json"),
                                  directory),
        "restored": verify_bundle(restored),
        "restored_same": all(
            Path(restored, name).read_bytes()
            == Path(directory, name).read_bytes() for name in RECORDS),
    }


def child_main(action, directory, scratch):
    with open(os.devnull, "w") as probe:
        print(json.dumps({"encoding": probe.encoding}), flush=True)
    if action == "write":
        write_sample_bundle(directory)
    print(json.dumps(check_bundle(directory, scratch)))


def run_under_non_utf8_locale(action, directory, scratch):
    code = ("import sys; sys.path.insert(0, sys.argv[1]);"
            " import test_bundle_encoding as t; t.child_main(*sys.argv[2:])")
    child = subprocess.run(
        [sys.executable, "-c", code, TESTS_DIR, action, str(directory),
         str(scratch)],
        env={**os.environ, **NON_UTF8_LOCALE}, cwd=str(scratch),
        capture_output=True, encoding="utf-8", errors="replace",
        timeout=120)
    lines = child.stdout.splitlines()
    assert lines, child.stderr
    encoding = json.loads(lines[0])["encoding"]
    assert codecs.lookup(encoding).name != "utf-8", encoding
    assert child.returncode == 0, child.stderr
    return json.loads(lines[-1])


def expected_events():
    return json.loads(json.dumps([e.model_dump() for e in sample_events()]))


def test_bundle_written_under_non_utf8_locale_is_utf8(tmp_path):
    written = tmp_path / "written"
    scratch = tmp_path / "child"
    scratch.mkdir()

    seen = run_under_non_utf8_locale("write", written, scratch)

    assert seen["verify"] == [] and seen["receipt"] == []
    assert seen["restored"] == [] and seen["restored_same"]
    assert seen["events"] == expected_events()
    reference = tmp_path / "reference"
    write_sample_bundle(str(reference))
    for name in RECORDS:
        assert (written / name).read_bytes() == \
            (reference / name).read_bytes(), name
    assert CARD_NAME.encode() in (written / "events.jsonl").read_bytes()
    assert CARD_NAME in (written / "report.md").read_text(encoding="utf-8")
    assert verify_bundle(str(written)) == []
    assert verify_receipt(str(written / "receipt.json"), str(written)) == []


def test_utf8_bundle_reads_under_non_utf8_locale(tmp_path):
    directory = tmp_path / "bundle"
    scratch = tmp_path / "child"
    scratch.mkdir()
    write_sample_bundle(str(directory))

    seen = run_under_non_utf8_locale("read", directory, scratch)

    assert seen["verify"] == [] and seen["receipt"] == []
    assert seen["restored"] == [] and seen["restored_same"]
    assert seen["events"] == expected_events()
