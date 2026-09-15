"""What the documentation says about replaying historical evidence.

Each test pins one claim the README or architecture notes make, against
bundles an older Town really wrote, so the words cannot drift from what
`verify` and `receipt` do.
"""

import json
import shutil
from pathlib import Path

import pytest

from nandatown.bundle import SHIPPED_EVALUATOR_VERSIONS, load_bundle, verify_bundle
from nandatown.evaluator import EVALUATOR_VERSION, EVALUATOR_VERSIONS, evaluate
from nandatown.receipt import make_receipt, replay_disclosures

FIXTURES = Path(__file__).parent / "fixtures"
EARLY_020 = FIXTURES / "track-0.2.0-early" / "quote-clean"
LATER_020 = FIXTURES / "track-0.2.0" / "quote-clean-stock"
OLDER_LAB = FIXTURES / "historical-lab-0.2.0-voting"


def test_a_later_0_2_0_track_bundle_replays_under_its_own_rules():
    assert verify_bundle(str(LATER_020)) == []


def test_the_first_0_2_0_builds_differ_from_replay_in_one_note_only():
    """The documented exception, exactly as documented."""
    bundle = load_bundle(str(EARLY_020))
    replayed = evaluate(bundle["profile"], bundle["run"].run_id,
                        bundle["events"], version="0.2.0")
    recorded = {s.name: s for s in bundle["result"].stages}
    differing = {s.name for s in replayed.stages
                 if (s.status, s.note) != (recorded[s.name].status,
                                           recorded[s.name].note)}

    assert bundle["result"].evaluator_version == "0.2.0"
    assert replayed.verdict == bundle["result"].verdict
    assert all(s.status == recorded[s.name].status for s in replayed.stages)
    assert differing == {"portable_identity"}
    assert any("evaluator replay mismatch" in p
               for p in verify_bundle(str(EARLY_020)))


def test_a_receipt_refuses_an_early_0_2_0_bundle(tmp_path, monkeypatch):
    monkeypatch.setenv("NANDATOWN_HOME", str(tmp_path / "home"))
    copy = tmp_path / "early"
    shutil.copytree(EARLY_020, copy)

    with pytest.raises(ValueError, match="evaluator replay mismatch"):
        make_receipt(str(copy))


def test_a_version_this_town_cannot_replay_is_accepted_with_a_disclosure(
        tmp_path, monkeypatch):
    """Only where replay is impossible does a receipt rest on anything less."""
    monkeypatch.setenv("NANDATOWN_HOME", str(tmp_path / "home"))
    copy = tmp_path / "lab"
    shutil.copytree(OLDER_LAB, copy)

    path = make_receipt(str(copy))

    payload = json.loads(Path(path).read_text())["payload"]
    assert replay_disclosures(payload), payload["limitations"]


def test_a_track_version_this_town_replays_carries_no_disclosure(
        tmp_path, monkeypatch):
    monkeypatch.setenv("NANDATOWN_HOME", str(tmp_path / "home"))
    copy = tmp_path / "later"
    shutil.copytree(LATER_020, copy)

    path = make_receipt(str(copy))

    payload = json.loads(Path(path).read_text())["payload"]
    assert replay_disclosures(payload) == []


def test_every_earlier_track_version_this_town_replays_is_listed_as_shipped():
    """The shipped list claims to name every version main recorded."""
    earlier = set(EVALUATOR_VERSIONS) - {EVALUATOR_VERSION}
    assert earlier <= SHIPPED_EVALUATOR_VERSIONS["track"]
