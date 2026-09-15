"""Credentials written into an endpoint URL are used, and never recorded.

The agent under test really requires basic authentication here, so a
passing run proves the credentials reached the exact endpoint, and a
bundle, report, receipt or Pulse history that never contains them proves
they went nowhere else.
"""

import contextlib
import json
import os
import re
import shutil
import socket
import stat
import subprocess
import sys
import time
from pathlib import Path

import httpx
import pytest

from nandatown.a2a_adapter import build_agent_card
from nandatown.bundle import load_bundle, verify_bundle
from nandatown.cli import main
from nandatown.path_runner import run_path_test
from nandatown.receipt import make_receipt, verify_receipt
from nandatown.report import render_report
from nandatown.url_credentials import (
    KEY_FILENAME,
    WITHHELD,
    CredentialKeyError,
    Labeller,
    Scrubber,
    has_credentials,
    local_key,
    safe_message,
    withhold,
)

USER, SECRET = "alice", "s3cret-pw"
FIXTURES = Path(__file__).parent / "fixtures"
OLD_BUNDLE = FIXTURES / "credential-url-path-bundle"
OLD_RECEIPT = FIXTURES / "credential-url-path-bundle.receipt.json"
OLD_SECRET = "fixture-s3cret"
LABELLED = re.compile(r"^http://<credentials [0-9a-f]{8}>@127\.0\.0\.1:\d+$")


@pytest.fixture(autouse=True)
def town_home(tmp_path, monkeypatch):
    home = tmp_path / "home"
    monkeypatch.setenv("NANDATOWN_HOME", str(home))
    return home


def free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


@contextlib.contextmanager
def running_auth_agent(password=SECRET):
    """A real localhost A2A agent that answers only alice:<password>."""
    port = free_port()
    root = Path(__file__).resolve().parents[1]
    process = subprocess.Popen(
        [sys.executable, str(FIXTURES / "basic_auth_a2a_agent.py"),
         str(port), USER, password],
        env=dict(os.environ, PYTHONPATH=str(root / "src")),
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    base = f"127.0.0.1:{port}"
    for _ in range(100):
        try:
            httpx.get(f"http://{base}/", trust_env=False, timeout=1)
            break
        except httpx.HTTPError:
            time.sleep(0.05)
    try:
        yield base
    finally:
        process.terminate()
        process.wait(timeout=10)


@pytest.fixture
def auth_agent():
    with running_auth_agent() as base:
        yield base


def files_containing(directory, needle):
    return sorted(str(p.relative_to(directory)) for p in Path(directory).rglob("*")
                  if p.is_file() and needle.encode() in p.read_bytes())


def bundle_of(capsys, argv):
    code = main(argv)
    out = capsys.readouterr().out
    return code, out, out.rsplit("Evidence bundle: ", 1)[1].split()[0]


# ---- recognising and labelling credentials ---------------------------------

LABEL = Labeller(b"k" * 32)


@pytest.mark.parametrize("url, host_part", [
    ("http://alice:s3cret-pw@127.0.0.1:9/x", "@127.0.0.1:9/x"),
    ("https://s3cret-pw@api.example/v1?x=1", "@api.example/v1?x=1"),
    ("http://alice:s3cret-pw@[::1]:9/", "@[::1]:9/"),
    ("HTTPS://alice:s3cret-pw@h", "@h"),
    # httpx allows these in user information and sends them.
    ("http://alice:Qu0te'Pw@127.0.0.1:9", "@127.0.0.1:9"),
    ('http://alice:Dq"Pw@127.0.0.1:9', "@127.0.0.1:9"),
    ("http://alice:An<gl>e@127.0.0.1:9", "@127.0.0.1:9"),
    ("http://alice:Sp ace@127.0.0.1:9", "@127.0.0.1:9"),
    # httpx cannot parse these, and what it cannot parse is the password.
    ("http://alice:Hash#Pw1@127.0.0.1:9", "@127.0.0.1:9"),
    ("http://alice:Slash/Pw2@127.0.0.1:9/x", "@127.0.0.1:9/x"),
])
def test_credentials_are_found_where_httpx_finds_them(url, host_part):
    labelled = LABEL.label(url)

    assert has_credentials(url)
    assert re.fullmatch(r"(?i)https?://<credentials [0-9a-f]{8}>"
                        + re.escape(host_part), labelled), labelled
    assert withhold(url).endswith(f"{WITHHELD}{host_part}")


@pytest.mark.parametrize("url", [
    "http://127.0.0.1:9/x", "http://[::1]:9", "agent-name", None,
    "http://@h",           # httpx sends no credentials for this
    "http://h/users/a@b",  # an "@" in the path is not user information
    "http://<credentials 1a2b3c4d>@h",
])
def test_a_url_without_credentials_is_left_as_written(url):
    assert not has_credentials(url)
    if isinstance(url, str) and "credentials" not in url:
        assert LABEL.label(url) == url


def test_the_same_credentials_have_one_label_however_they_are_written():
    assert LABEL.label_for("http://alice:p@ss@h") == LABEL.label_for(
        "http://alice:p%40ss@h/elsewhere")
    # httpx sends "tok" with an empty password either way.
    assert LABEL.label_for("http://tok@h") == LABEL.label_for("http://tok:@h")
    assert LABEL.label_for("http://alice:one@h") != LABEL.label_for(
        "http://alice:two@h")
    assert LABEL.label(LABEL.label("http://alice:one@h")) == LABEL.label(
        "http://alice:one@h")


def test_withholding_covers_raw_and_labelled_credentials():
    raw = "http://alice:s3cret-pw@127.0.0.1:9"

    assert withhold(raw) == f"http://{WITHHELD}@127.0.0.1:9"
    assert withhold(LABEL.label(raw)) == f"http://{WITHHELD}@127.0.0.1:9"


def test_only_a_registered_locators_exact_credentials_are_replaced():
    scrubber = Scrubber(LABEL)
    scrubber.register('http://alice:Dq"Pw@127.0.0.1:9')

    # As written, and as httpx quotes it back in its own error messages.
    assert SECRET_FREE(scrubber('http://alice:Dq"Pw@127.0.0.1:9'))
    assert "<credentials " in scrubber(
        "for url 'http://alice:Dq%22Pw@127.0.0.1:9/x'")
    # An agent's own words are recorded as it said them.
    for text in ('the password Dq"Pw alone',
                 "see https://example.com,ops@example.org",
                 "contact admin@example.com"):
        assert scrubber(text) == text


def SECRET_FREE(text):
    return "Dq" not in text


def test_registering_a_locator_without_credentials_never_loads_the_key():
    def refuse():
        raise AssertionError("the key was loaded")

    scrubber = Scrubber(Labeller(refuse))
    scrubber.register("http://127.0.0.1:9")
    scrubber.register(None)

    assert not scrubber
    assert scrubber("anything at all") == "anything at all"


def test_a_key_that_cannot_be_used_withholds_rather_than_fails():
    def unwritable():
        raise PermissionError("read-only home")

    assert Labeller(unwritable).label("http://alice:pw@h") == (
        f"http://{WITHHELD}@h")


def test_a_message_that_could_quote_a_password_is_not_repeated():
    unparseable = "http://alice:Hash#Pw1@127.0.0.1:9"

    assert "Hash" not in safe_message(unparseable, "Invalid port: 'Hash'")
    assert safe_message("http://127.0.0.1:9", "Invalid port: 'x'") == (
        "Invalid port: 'x'")


def test_the_key_is_private_and_every_process_agrees_on_it(town_home):
    script = ("import sys; from nandatown.url_credentials import local_key;"
              " sys.stdout.write(local_key().hex())")
    processes = [subprocess.Popen([sys.executable, "-c", script],
                                  stdout=subprocess.PIPE, env=os.environ)
                 for _ in range(8)]
    keys = {p.communicate(timeout=30)[0] for p in processes}

    assert len(keys) == 1
    path = town_home / KEY_FILENAME
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert local_key().hex().encode() in keys


def test_the_key_is_created_where_hard_links_are_not_supported(
        town_home, monkeypatch):
    def no_links(source, destination):
        raise OSError(45, "Operation not supported")

    monkeypatch.setattr(os, "link", no_links)

    key = local_key()

    assert len(key) == 32 and local_key() == key
    assert stat.S_IMODE((town_home / KEY_FILENAME).stat().st_mode) == 0o600


def test_a_corrupt_key_is_named_and_never_silently_replaced(town_home):
    town_home.mkdir(parents=True)
    (town_home / KEY_FILENAME).write_bytes(b"short")

    with pytest.raises(CredentialKeyError, match="remove it"):
        local_key()


# ---- Path ------------------------------------------------------------------

def test_credentials_reach_the_endpoint_and_nothing_records_them(
        tmp_path, capsys, auth_agent):
    url = f"http://{USER}:{SECRET}@{auth_agent}"

    code, out, bundle = bundle_of(capsys, [
        "test-agent", "--url", url, "--out", str(tmp_path / "runs")])

    assert code == 0 and "PASSED" in out, out
    assert SECRET not in out
    assert files_containing(bundle, SECRET) == []
    run = load_bundle(bundle)["run"]
    assert LABELLED.match(run.config["subject"]), run.config["subject"]
    assert "<operator-supplied-url>" in run.config["rerun_command"]
    assert "url" in run.config["rerun_required_inputs"]
    assert verify_bundle(bundle) == []

    assert main(["receipt", bundle]) == 0
    receipt_out = capsys.readouterr().out
    assert "nothing private leaves the bundle" not in receipt_out
    receipt = json.loads((Path(bundle) / "receipt.json").read_text())
    assert receipt["payload"]["claim"]["subject"] == (
        f"http://{WITHHELD}@{auth_agent}")
    assert SECRET not in json.dumps(receipt)
    assert verify_receipt(str(Path(bundle) / "receipt.json"), bundle) == []


def test_the_url_without_its_credentials_is_a_different_endpoint(
        tmp_path, capsys, auth_agent):
    """Stripping the credentials would have tested something else."""
    code, out, _ = bundle_of(capsys, [
        "test-agent", "--url", f"http://{auth_agent}",
        "--out", str(tmp_path / "runs")])

    assert code == 1 and "a2a_http_status_401" in out, out


def test_wrong_credentials_fail_without_being_recorded(
        tmp_path, capsys, auth_agent):
    wrong = "not-the-password"

    code, out, bundle = bundle_of(capsys, [
        "test-agent", "--url", f"http://{USER}:{wrong}@{auth_agent}",
        "--out", str(tmp_path / "runs")])

    assert code == 1 and "a2a_http_status_401" in out, out
    assert wrong not in out
    assert files_containing(bundle, wrong) == []


def test_an_error_quoting_the_url_is_recorded_without_its_credentials(
        tmp_path):
    """An unexpected failure is recorded as its own text, which can quote
    the request URL, credentials and all."""
    url = f"http://{USER}:{SECRET}@127.0.0.1:9"

    def handler(request):
        if request.method == "GET":
            return httpx.Response(200, json=build_agent_card(url))
        raise RuntimeError(f"upstream refused {request.url}")

    with httpx.Client(base_url=url,
                      transport=httpx.MockTransport(handler)) as http:
        bundle, _ = run_path_test(url, str(tmp_path / "runs"), http=http)

    assert files_containing(bundle, SECRET) == []
    reasons = [e.detail.get("reason", "") for e in load_bundle(bundle)["events"]]
    assert any("upstream refused http://<credentials " in r
               for r in reasons), reasons


def test_index_credentials_are_used_and_never_recorded(
        tmp_path, capsys, auth_agent):
    index = tmp_path / "index.json"
    index.write_text(json.dumps({"agents": {"seller": {
        "url": f"http://{USER}:{SECRET}@{auth_agent}"}}}))

    code, out, bundle = bundle_of(capsys, [
        "test-agent", "--index", str(index), "--agent-name", "seller",
        "--out", str(tmp_path / "runs")])

    assert code == 0 and "PASSED" in out, out
    assert SECRET not in out
    assert files_containing(bundle, SECRET) == []
    # The index still holds the credentials, so its rerun is exact.
    assert "--index" in load_bundle(bundle)["run"].config["rerun_command"]


def test_two_sets_of_credentials_are_recorded_as_different_subjects(
        tmp_path):
    subjects = []
    for password in ("one", "two", "one"):
        bundle, _ = run_path_test(f"http://{USER}:{password}@127.0.0.1:9",
                                  str(tmp_path / password))
        subjects.append(load_bundle(bundle)["run"].config["subject"])

    assert subjects[0] != subjects[1]
    assert subjects[0] == subjects[2]


@pytest.mark.parametrize("password", ["Qu0te'Pw", 'Dq"Pw', "An<gl>e"])
def test_a_password_with_quotes_or_brackets_is_used_and_never_recorded(
        tmp_path, capsys, password):
    """httpx sends these; recognising credentials by pattern missed them."""
    with running_auth_agent(password) as base:
        code, out, bundle = bundle_of(capsys, [
            "test-agent", "--url", f"http://{USER}:{password}@{base}",
            "--out", str(tmp_path / "runs")])
        assert code == 0 and "PASSED" in out, out
        assert main(["receipt", bundle]) == 0
        out += capsys.readouterr().out

    assert password not in out
    assert files_containing(bundle, password) == []


@pytest.mark.parametrize("password", ["Hash#Pw1", "Slash/Pw2", "Q?Pw3"])
def test_credentials_in_a_url_httpx_cannot_parse_are_not_recorded(
        tmp_path, capsys, password):
    """The URL fails resolution, and the bundle is the kind people share."""
    url = f"http://{USER}:{password}@127.0.0.1:9"

    code, out, bundle = bundle_of(capsys, [
        "test-agent", "--url", url, "--out", str(tmp_path / "runs")])
    assert main(["a2a", "test", url]) == 1
    out += capsys.readouterr().out

    assert code == 1 and "invalid endpoint URL" in out, out
    fragment = password[:4]
    assert fragment not in out
    assert files_containing(bundle, fragment) == []


def test_an_agents_own_words_are_recorded_as_it_said_them(
        tmp_path, town_home):
    """Only the operator's credentials are withheld, never an agent's text."""
    name = "Quotes: see https://example.com,ops@example.org"

    def handler(request):
        card = build_agent_card("http://127.0.0.1:9")
        return httpx.Response(200, json=dict(card, name=name))

    with httpx.Client(base_url="http://127.0.0.1:9",
                      transport=httpx.MockTransport(handler)) as http:
        bundle, _ = run_path_test("http://127.0.0.1:9",
                                  str(tmp_path / "runs"), http=http)

    names = [e.detail.get("name") for e in load_bundle(bundle)["events"]
             if e.kind == "card_retrieved"]
    assert names == [name]
    assert not (town_home / KEY_FILENAME).exists()


def test_a_read_only_home_does_not_stop_a_run(tmp_path, town_home):
    """A home that already holds an identity can attest without writing,
    and credentials must not be what makes the run need to."""
    run_path_test("http://127.0.0.1:9", str(tmp_path / "first"))
    town_home.chmod(0o500)
    try:
        bundle, _ = run_path_test(f"http://{USER}:{SECRET}@127.0.0.1:9",
                                  str(tmp_path / "runs"))
    finally:
        town_home.chmod(0o700)

    assert files_containing(bundle, SECRET) == []
    assert WITHHELD in load_bundle(bundle)["run"].config["subject"]


def test_a_corrupt_key_withholds_credentials_rather_than_fail(
        tmp_path, town_home):
    town_home.mkdir(parents=True)
    (town_home / KEY_FILENAME).write_bytes(b"short")

    bundle, _ = run_path_test(f"http://{USER}:{SECRET}@127.0.0.1:9",
                              str(tmp_path / "runs"))

    assert files_containing(bundle, SECRET) == []
    assert WITHHELD in load_bundle(bundle)["run"].config["subject"]


def test_a_run_without_credentials_never_creates_the_key(tmp_path, town_home):
    run_path_test("http://127.0.0.1:9", str(tmp_path / "runs"))

    assert not (town_home / KEY_FILENAME).exists()


# ---- evidence recorded before credentials were withheld --------------------

@pytest.fixture
def old_bundle(tmp_path):
    directory = tmp_path / "old-bundle"
    shutil.copytree(OLD_BUNDLE, directory)
    return directory


def test_a_new_receipt_over_old_evidence_withholds_its_credentials(
        old_bundle):
    before = {p.name: p.read_bytes() for p in old_bundle.iterdir()}

    path = make_receipt(str(old_bundle))

    receipt = json.loads(Path(path).read_text())
    assert OLD_SECRET not in json.dumps(receipt)
    assert WITHHELD in receipt["payload"]["claim"]["subject"]
    assert verify_receipt(path, str(old_bundle)) == []
    # The evidence itself is not rewritten.
    assert {p.name: p.read_bytes() for p in old_bundle.iterdir()
            if p.name != "receipt.json"} == before
    assert verify_bundle(str(old_bundle)) == []


def test_a_receipt_issued_before_this_still_verifies(old_bundle, tmp_path):
    receipt = tmp_path / "old-receipt.json"
    shutil.copy(OLD_RECEIPT, receipt)

    assert verify_receipt(str(receipt), str(old_bundle)) == []


def test_reports_of_old_evidence_withhold_its_credentials(old_bundle):
    report = render_report(load_bundle(str(old_bundle)))

    assert OLD_SECRET not in report
    assert WITHHELD in report


