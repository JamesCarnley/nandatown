import hashlib
import json
import os
import re
import shutil
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from nandatown.a2a_adapter import build_a2a_app, build_agent_card
from nandatown.bundle import attest_bundle, load_bundle, verify_bundle
from nandatown.cli import main
from nandatown.identity_portable import Keystore
from nandatown.path_runner import STRICT_PATH_EVALUATOR_VERSION, run_path_test
from nandatown.receipt import (
    DEFAULT_LIMITATIONS,
    _bundle_receipt_fields,
    make_receipt,
    render_proof,
    verify_receipt,
)
from nandatown.records import fingerprint
from nandatown.runner import run_town
from nandatown.sim.runner import run_lab
from nandatown.sim.validators import LAB_EVALUATOR_VERSION


def passed_bundle(tmp_path):
    bundle_dir, result = run_path_test(
        "http://testserver", str(tmp_path),
        http=TestClient(build_a2a_app("http://testserver")))
    assert result.verdict == "passed"
    return bundle_dir


def complete_passed_bundle(tmp_path):
    url = "http://testserver"
    bundle_dir, result = run_path_test(
        url, str(tmp_path),
        pin_card_digest=fingerprint(build_agent_card(url)),
        http=TestClient(build_a2a_app(url)))
    assert result.verdict == "passed"
    assert all(stage.status == "passed" for stage in result.stages)
    return bundle_dir


def failed_bundle(tmp_path):
    bundle_dir, result = run_path_test(
        "http://testserver", str(tmp_path / "bad"),
        http=TestClient(build_a2a_app("http://testserver",
                                      defect="wrong_total")))
    assert result.verdict == "failed"
    return bundle_dir


def test_receipt_is_sanitized_signed_and_verifiable(tmp_path):
    bundle_dir = passed_bundle(tmp_path)
    path = make_receipt(bundle_dir)
    assert verify_receipt(path, bundle_dir=bundle_dir) == []
    with open(path) as f:
        receipt = json.load(f)
    payload = receipt["payload"]
    assert payload["claim"]["capability"] == "quote"
    assert payload["claim"]["verdict"] == "passed"
    assert payload["observer"].startswith("did:town:")
    assert "semantic_result" in payload["coverage"]["tested"]
    assert payload["limitations"]
    text = json.dumps(receipt)
    assert "widget" not in text
    assert "sku" not in text
    assert "unit_price_cents" not in text
    assert "quantity" not in text
    assert "request_id" not in text


def test_tampered_receipt_is_caught(tmp_path):
    bundle_dir = passed_bundle(tmp_path)
    path = make_receipt(bundle_dir)
    with open(path) as f:
        receipt = json.load(f)
    receipt["payload"]["claim"]["verdict"] = "passed-forever"
    with open(path, "w") as f:
        json.dump(receipt, f)
    problems = verify_receipt(path, bundle_dir=bundle_dir)
    assert any("signature" in p for p in problems)


def test_proof_renders_only_from_passing_fresh_evidence(tmp_path):
    bundle_dir = complete_passed_bundle(tmp_path)
    ok, text = render_proof(bundle_dir)
    assert ok, text
    assert "TOWN-TESTED" in text
    assert "observed to pass for release basis sha256:" in text
    assert "narrow and expiring" in text

    bad_dir = failed_bundle(tmp_path)
    ok, text = render_proof(bad_dir)
    assert not ok
    assert "No Town Proof" in text
    assert "verdict is failed" in text


def test_stale_evidence_refuses_a_badge(tmp_path):
    bundle_dir = complete_passed_bundle(tmp_path)
    path = make_receipt(bundle_dir)
    ok, text = render_proof(bundle_dir, freshness_days=0.0)
    assert not ok
    assert "freshness" in text
    assert os.path.exists(path)


def test_cli_receipt_verify_proof(tmp_path, capsys):
    bundle_dir = complete_passed_bundle(tmp_path)
    assert main(["receipt", bundle_dir]) == 0
    assert main(["verify-receipt", f"{bundle_dir}/receipt.json",
                 "--bundle", bundle_dir]) == 0
    assert main(["proof", bundle_dir]) == 0
    out = capsys.readouterr().out
    assert "TOWN-TESTED" in out
    assert "commitment is not truth" in out


@pytest.mark.parametrize("freshness_days", [float("nan"), float("inf"), -1.0],
                         ids=["nan", "infinite", "negative"])
def test_proof_rejects_invalid_freshness_domain(tmp_path, freshness_days):
    bundle_dir = complete_passed_bundle(tmp_path)

    ok, text = render_proof(bundle_dir, freshness_days=freshness_days)

    assert not ok
    assert "freshness days must be a finite non-negative number" in text


@pytest.mark.parametrize("document", [[], {"payload": []},
                                         {"payload": {},
                                          "controller_public": [],
                                          "signature": []}],
                         ids=["list", "payload-list", "non-string-signature"])
def test_malformed_receipt_is_reported_not_raised(tmp_path, document):
    receipt_path = tmp_path / "receipt.json"
    receipt_path.write_text(json.dumps(document))

    try:
        problems = verify_receipt(str(receipt_path))
    except Exception as exc:  # pragma: no cover - the assertion is the contract
        pytest.fail(f"malformed receipt raised {type(exc).__name__}: {exc}")

    assert problems
    assert any("receipt" in problem for problem in problems)


def test_symlinked_receipt_is_rejected_before_read(tmp_path):
    target = tmp_path / "target.json"
    target.write_text("{}")
    receipt_path = tmp_path / "receipt.json"
    receipt_path.symlink_to(target)

    problems = verify_receipt(str(receipt_path))

    assert any("not a regular file" in problem for problem in problems), problems


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="FIFO unavailable")
def test_fifo_receipt_is_rejected_before_open(tmp_path, monkeypatch):
    receipt_path = tmp_path / "receipt.json"
    os.mkfifo(receipt_path)

    def forbidden_open(*_args, **_kwargs):
        pytest.fail("verify_receipt attempted to open a FIFO")

    monkeypatch.setattr("builtins.open", forbidden_open)

    problems = verify_receipt(str(receipt_path))

    assert any("not a regular file" in problem for problem in problems), problems


def test_detached_verification_rejects_signed_sparse_payload(tmp_path):
    keys = Keystore(str(tmp_path / "keys"))
    identity = keys.new_identity("reviewer")
    payload = {"observer": identity["agent_id"]}
    receipt = {
        "payload": payload,
        "signature": keys.sign("reviewer", payload),
        "controller_public": identity["controller_public"],
    }
    receipt_path = tmp_path / "receipt.json"
    receipt_path.write_text(json.dumps(receipt))

    problems = verify_receipt(str(receipt_path))

    assert any("payload" in problem and "missing" in problem
               for problem in problems), problems


@pytest.mark.parametrize(
    ("change", "expected"),
    [
        ("claim-extra", "claim has unexpected fields"),
        ("evidence-extra", "evidence has unexpected fields"),
        ("payload-extra", "payload has unexpected fields"),
        ("receipt-extra", "receipt has unexpected fields"),
        ("missing-limitations", "payload is missing fields"),
    ],
)
def test_signed_receipt_rejects_unrecognized_or_missing_claim_shape(
        tmp_path, change, expected):
    bundle_dir = complete_passed_bundle(tmp_path)
    keys = Keystore(str(tmp_path / "keys"))
    receipt_path = make_receipt(
        bundle_dir, keystore=keys, signer="reviewer")
    with open(receipt_path) as stream:
        receipt = json.load(stream)
    if change == "claim-extra":
        receipt["payload"]["claim"]["universally_safe"] = True
    elif change == "evidence-extra":
        receipt["payload"]["evidence"]["independently_observed"] = True
    elif change == "payload-extra":
        receipt["payload"]["endorsement"] = True
    elif change == "receipt-extra":
        receipt["endorsement"] = True
    else:
        receipt["payload"].pop("limitations")
    receipt["signature"] = keys.sign("reviewer", receipt["payload"])
    with open(receipt_path, "w") as stream:
        json.dump(receipt, stream)

    detached = verify_receipt(receipt_path)
    bundle_aware = verify_receipt(receipt_path, bundle_dir)

    assert any(expected in problem for problem in detached), detached
    assert any(expected in problem for problem in bundle_aware), bundle_aware


# A receipt rests on the bundle it names. Signing or verifying it against a
# bundle that fails verification must refuse; only an older evaluator
# version (replay impossible, everything else checked) is accepted, and
# the output says so.


def _edit_json(path, change):
    path = Path(path)
    document = json.loads(path.read_text())
    change(document)
    path.write_text(json.dumps(document))


def _rehash(directory, *names):
    bundle = Path(directory)
    manifest_path = bundle / "manifest.json"
    manifest = json.loads(manifest_path.read_text())
    for name in names:
        manifest["files"][name] = "sha256:" + hashlib.sha256(
            (bundle / name).read_bytes()).hexdigest()
    manifest["bundle_fingerprint"] = fingerprint(manifest["files"])
    manifest_path.write_text(json.dumps(manifest))


def _edit_records_in_place(directory):
    """Change the recorded observations without touching the manifest."""
    bundle = Path(directory)
    events_path = bundle / "events.jsonl"
    events = [json.loads(line)
              for line in events_path.read_text().splitlines()]
    for event in events:
        if event["kind"] == "fulfillment_observed":
            event["detail"]["total_cents"] = 999999
    events_path.write_text("".join(json.dumps(e) + "\n" for e in events))
    _edit_json(bundle / "profile.json",
               lambda profile: profile["expected"].update(total_cents=1))
    (bundle / "intents.jsonl").write_text("")


def _relabel_failed_as_passed(directory):
    def relabel(result):
        result["verdict"] = "passed"
        for stage in result["stages"]:
            stage["status"] = "passed"
            stage["note"] = ""
    _edit_json(Path(directory) / "result.json", relabel)


def _sign_unverified_receipt(directory, keys):
    """Sign what a bundle says about itself without verifying it, as
    `receipt` did before it checked bundle integrity."""
    fields = _bundle_receipt_fields(load_bundle(str(directory)))
    identity = keys.new_identity("reviewer")
    payload = {"claim": fields["claim"], "observer": identity["agent_id"],
               "window": fields["window"], "coverage": fields["coverage"],
               "limitations": DEFAULT_LIMITATIONS,
               "evidence": fields["evidence"]}
    path = Path(directory) / "receipt.json"
    path.write_text(json.dumps({
        "payload": payload,
        "signature": keys.sign("reviewer", payload),
        "controller_public": identity["controller_public"],
    }))
    return str(path)


def _relabel_evaluator(directory, old_version):
    """The same records as recorded by an older evaluator release:
    labelled consistently, rehashed and re-attested."""
    bundle = Path(directory)
    _edit_json(bundle / "result.json",
               lambda result: result.update(evaluator_version=old_version))
    _edit_json(bundle / "run.json",
               lambda run: run["releases"].update(evaluator=old_version))
    _edit_json(bundle / "manifest.json",
               lambda manifest: manifest.update(evaluator_version=old_version))
    _rehash(directory, "result.json", "run.json")
    attest_bundle(str(directory))


def test_bundle_edited_after_receipt_no_longer_verifies(tmp_path, capsys):
    bundle_dir = complete_passed_bundle(tmp_path)
    path = make_receipt(bundle_dir)
    _edit_records_in_place(bundle_dir)

    problems = verify_receipt(path, bundle_dir=bundle_dir)

    for name in ("profile.json", "intents.jsonl", "events.jsonl"):
        assert any(f"{name} hash mismatch" in p for p in problems), problems
    # Without a bundle, verification still means only key commitment.
    assert verify_receipt(path) == []
    assert main(["verify-receipt", path, "--bundle", bundle_dir]) == 1
    out = capsys.readouterr().out
    assert "events.jsonl hash mismatch" in out
    assert "receipt verifies" not in out


def test_receipt_refuses_failed_bundle_relabelled_as_passed(tmp_path, capsys):
    bundle_dir = failed_bundle(tmp_path)
    _relabel_failed_as_passed(bundle_dir)
    receipt_path = Path(bundle_dir) / "receipt.json"

    with pytest.raises(ValueError, match="result.json hash mismatch"):
        make_receipt(bundle_dir)
    assert not receipt_path.exists()
    assert main(["receipt", bundle_dir]) == 1
    assert "result.json hash mismatch" in capsys.readouterr().out
    assert not receipt_path.exists()

    unverified = _sign_unverified_receipt(
        bundle_dir, Keystore(str(tmp_path / "keys")))
    assert verify_receipt(unverified) == []
    assert main(["verify-receipt", unverified, "--bundle", bundle_dir]) == 1
    out = capsys.readouterr().out
    assert "result.json hash mismatch" in out
    assert "receipt verifies" not in out


def _break_fingerprint(directory):
    _edit_json(Path(directory) / "manifest.json",
               lambda m: m.update(bundle_fingerprint="sha256:" + "0" * 64))


def _break_binding(directory):
    _edit_json(Path(directory) / "run.json",
               lambda run: run.update(run_id="other-run"))
    _rehash(directory, "run.json")


def _break_replay(directory):
    events_path = Path(directory) / "events.jsonl"
    events = [json.loads(line)
              for line in events_path.read_text().splitlines()]
    for event in events:
        if event["kind"] == "fulfillment_observed":
            event["detail"]["total_cents"] = 4090
    events_path.write_text("".join(json.dumps(e) + "\n" for e in events))
    _rehash(directory, "events.jsonl")
    attest_bundle(str(directory))


def _break_attestation(directory):
    _edit_json(Path(directory) / "attestation.json",
               lambda attestation: attestation.update(signature="00"))


def _unsupported_evaluator(directory):
    _edit_json(Path(directory) / "profile.json",
               lambda profile: profile.update(evaluator="unknown@9"))
    _rehash(directory, "profile.json")


@pytest.mark.parametrize(
    ("breaker", "expected"),
    [(_break_fingerprint, "bundle fingerprint mismatch"),
     (_break_binding, "run and result name different run ids"),
     (_break_replay, "evaluator replay mismatch"),
     (_break_attestation, "attestation signature does not verify"),
     (_unsupported_evaluator, "unsupported path evaluator")],
    ids=["fingerprint", "binding", "replay", "attestation", "evaluator"],
)
def test_receipts_refuse_bundles_that_fail_verification(
        tmp_path, breaker, expected):
    bundle_dir = complete_passed_bundle(tmp_path)
    breaker(bundle_dir)
    assert any(expected in p for p in verify_bundle(bundle_dir))

    with pytest.raises(ValueError, match=expected):
        make_receipt(bundle_dir)
    unverified = _sign_unverified_receipt(
        bundle_dir, Keystore(str(tmp_path / "keys")))
    problems = verify_receipt(unverified, bundle_dir)

    assert any(expected in p for p in problems), problems


def test_intact_failed_and_partial_bundles_still_get_receipts(
        tmp_path, capsys):
    for bundle_dir in (failed_bundle(tmp_path), passed_bundle(tmp_path)):
        assert main(["receipt", bundle_dir]) == 0
        assert main(["verify-receipt", f"{bundle_dir}/receipt.json",
                     "--bundle", bundle_dir]) == 0
        out = capsys.readouterr().out
        assert "receipt verifies" in out
        assert "not checked" not in out


def _historical_path_bundle(tmp_path):
    bundle_dir = complete_passed_bundle(tmp_path)
    _relabel_evaluator(bundle_dir, "path-0.1")
    return bundle_dir, "path-0.1", STRICT_PATH_EVALUATOR_VERSION


def _historical_lab_bundle(tmp_path):
    bundle_dir, _ = run_lab("voting", str(tmp_path))
    _relabel_evaluator(bundle_dir, "lab-0.2.5")
    return bundle_dir, "lab-0.2.5", LAB_EVALUATOR_VERSION


# A Lab bundle recorded by nandatown at cb19e0e, kept verbatim; see its
# .provenance.json. It is a fixture, not an adoption.
GENUINE_HISTORICAL_LAB = (Path(__file__).parent / "fixtures"
                          / "historical-lab-0.2.0-voting")


def _genuine_historical_lab_bundle(tmp_path):
    """A bundle an older Town really wrote, not a relabelled modern one.

    Relabelling cannot stand in for this. A bundle written today records
    every field today's model has, so reading it back adds nothing; only
    a document written before those fields existed shows whether the
    profile binding survives the model gaining them.
    """
    directory = tmp_path / "genuine-historical"
    shutil.copytree(GENUINE_HISTORICAL_LAB, directory)
    return str(directory), "lab-0.2.0", LAB_EVALUATOR_VERSION


def test_genuine_historical_bundle_is_bound_to_the_document_it_recorded():
    """The fixture must actually exercise the drift, or it proves nothing."""
    bundle = load_bundle(str(GENUINE_HISTORICAL_LAB))
    recorded = bundle["run"].profile_fingerprint

    assert recorded == fingerprint(bundle["profile_document"])
    assert recorded != fingerprint(bundle["profile"].model_dump())
    assert set(bundle["profile"].model_dump()) - set(
        bundle["profile_document"]) == {"plugin_files", "adaptations"}


def test_genuine_historical_bundle_verifies_apart_from_its_evaluator(
        tmp_path):
    bundle_dir, old, local = _genuine_historical_lab_bundle(tmp_path)

    assert verify_bundle(bundle_dir) == [
        f"evaluator version differs: bundle {old}, local {local};"
        " reproducibility not checked"]


def test_a_rewritten_historical_profile_is_still_refused(tmp_path):
    """Rehashing hides the edit from the manifest, not from the binding."""
    bundle_dir, _, _ = _genuine_historical_lab_bundle(tmp_path)
    _edit_json(Path(bundle_dir) / "profile.json",
               lambda profile: profile.update(name="something-else"))
    _rehash(bundle_dir, "profile.json")

    assert "run profile fingerprint does not match profile" in verify_bundle(
        bundle_dir)
    with pytest.raises(ValueError, match="does not match profile"):
        make_receipt(bundle_dir)


def test_an_unmodelled_field_added_to_a_profile_is_refused(tmp_path):
    """The model would drop this field; the recorded document would not."""
    bundle_dir, _, _ = _genuine_historical_lab_bundle(tmp_path)
    _edit_json(Path(bundle_dir) / "profile.json",
               lambda profile: profile.update(smuggled="payload"))
    _rehash(bundle_dir, "profile.json")

    assert "run profile fingerprint does not match profile" in verify_bundle(
        bundle_dir)


@pytest.mark.parametrize("historical_bundle",
                         [_historical_path_bundle, _historical_lab_bundle,
                          _genuine_historical_lab_bundle],
                         ids=["path", "lab", "genuine-lab"])
def test_historical_evaluator_bundle_gets_receipt_with_disclosure(
        tmp_path, capsys, historical_bundle):
    bundle_dir, old, local = historical_bundle(tmp_path)
    assert verify_bundle(bundle_dir) == [
        f"evaluator version differs: bundle {old}, local {local};"
        " reproducibility not checked"]
    disclosure = f"evaluator replay not checked: bundle {old}, local {local}"

    path = make_receipt(bundle_dir)
    assert verify_receipt(path, bundle_dir) == []

    assert main(["receipt", bundle_dir]) == 0
    assert disclosure in capsys.readouterr().out
    assert main(["verify-receipt", path, "--bundle", bundle_dir]) == 0
    out = capsys.readouterr().out
    assert "receipt verifies" in out
    assert disclosure in out
    assert main(["verify-receipt", path]) == 0
    assert "not checked" not in capsys.readouterr().out
    # Town Proof still requires a replay under the local evaluator.
    assert main(["proof", bundle_dir]) == 1
    assert "evaluator version differs" in capsys.readouterr().out


@pytest.mark.parametrize(
    ("breaker", "expected"),
    [(_edit_records_in_place, "events.jsonl hash mismatch"),
     (_break_attestation, "attestation signature does not verify")],
    ids=["records", "attestation"],
)
def test_historical_evaluator_bundle_still_refuses_integrity_failures(
        tmp_path, capsys, breaker, expected):
    bundle_dir, _, _ = _historical_path_bundle(tmp_path)
    path = make_receipt(bundle_dir)
    breaker(bundle_dir)

    assert any(expected in p for p in verify_receipt(path, bundle_dir))
    with pytest.raises(ValueError, match=expected):
        make_receipt(bundle_dir)
    assert main(["receipt", bundle_dir]) == 1
    assert expected in capsys.readouterr().out


def _forge_evaluator_version(directory, version):
    """Relabel a bundle as passed under an evaluator version: labelled
    consistently and rehashed, with the attestation dropped, so no key is
    needed. Only a replay would expose the relabelled verdict."""
    bundle = Path(directory)
    _relabel_failed_as_passed(directory)
    _edit_json(bundle / "result.json",
               lambda result: result.update(evaluator_version=version))
    _edit_json(bundle / "run.json",
               lambda run: run["releases"].update(evaluator=version))
    _edit_json(bundle / "manifest.json",
               lambda manifest: manifest.update(evaluator_version=version))
    _rehash(directory, "result.json", "run.json")
    (bundle / "attestation.json").unlink(missing_ok=True)


def _failed_path_bundle(tmp_path):
    return failed_bundle(tmp_path), "path"


def _lab_bundle(tmp_path):
    bundle_dir, _ = run_lab("voting", str(tmp_path))
    return bundle_dir, "lab"


def _track_bundle(tmp_path):
    bundle_dir, _ = run_town("quote-clean", str(tmp_path))
    return bundle_dir, "track"


@pytest.mark.parametrize(
    ("make_bundle", "version"),
    [(_failed_path_bundle, "zzz-any"),
     (_failed_path_bundle, "path-9.0"),
     (_failed_path_bundle, "lab-0.2.5"),
     (_lab_bundle, "zzz-any"),
     (_lab_bundle, "lab-9.0.0"),
     (_track_bundle, "zzz-any")],
    ids=["path-made-up", "path-future", "path-other-mode", "lab-made-up",
         "lab-future", "track-made-up"],
)
def test_receipts_refuse_unrecognised_evaluator_versions(
        tmp_path, capsys, make_bundle, version):
    bundle_dir, mode = make_bundle(tmp_path)
    _forge_evaluator_version(bundle_dir, version)
    receipt_path = Path(bundle_dir) / "receipt.json"
    reason = f"unrecognised evaluator version {version} for {mode} bundles"
    # verify reports the same single difference to every other caller.
    problems = verify_bundle(bundle_dir)
    assert len(problems) == 1, problems
    assert problems[0].startswith(
        f"evaluator version differs: bundle {version}, local ")

    with pytest.raises(ValueError, match=re.escape(reason)):
        make_receipt(bundle_dir)
    assert not receipt_path.exists()
    assert main(["receipt", bundle_dir]) == 1
    out = capsys.readouterr().out
    assert reason in out
    assert "receipt written" not in out
    assert "not checked" not in out
    assert not receipt_path.exists()

    unverified = _sign_unverified_receipt(
        bundle_dir, Keystore(str(tmp_path / "keys")))
    assert verify_receipt(unverified) == []
    assert any(reason in p for p in verify_receipt(unverified, bundle_dir))
    assert main(["verify-receipt", unverified, "--bundle", bundle_dir]) == 1
    out = capsys.readouterr().out
    assert reason in out
    assert "receipt verifies" not in out
    assert "not checked" not in out
