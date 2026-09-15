"""Concurrent use of one keystore, as when several runs share a Town home.

Each test starts real processes. A small patch in each process widens the
windows the keystore must protect, generating a key and writing the
registry, so a race that only sometimes shows up in practice shows up
every time here.
"""

import errno
import json
import os
import stat
import subprocess
import sys
import time
from pathlib import Path

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from nandatown import identity_portable
from nandatown.bundle import attest_bundle, verify_bundle
from nandatown.identity_portable import (
    OPERATOR_NAME,
    IdentityError,
    Keystore,
    resolve_file,
)
from nandatown.records import fingerprint
from nandatown.sim.runner import run_lab

PROCESSES = 6

# Runs in each process before its work: every key generation and registry
# write takes long enough for the other processes to arrive in between.
WIDEN_WINDOWS = """
import json, os, sys, time
import nandatown.identity_portable as identity_portable

_Key = identity_portable.Ed25519PrivateKey
class _SlowKey:
    from_private_bytes = staticmethod(_Key.from_private_bytes)
    @staticmethod
    def generate():
        time.sleep(0.2)
        return _Key.generate()
identity_portable.Ed25519PrivateKey = _SlowKey

_dump = json.dump
def _slow_dump(*args, **kwargs):
    time.sleep(0.1)
    return _dump(*args, **kwargs)
json.dump = _slow_dump

_read_registry = identity_portable.Keystore._registry
def _slow_registry(self):
    registry = _read_registry(self)
    time.sleep(0.1)
    return registry
identity_portable.Keystore._registry = _slow_registry

gate = os.environ["GATE"]
while not os.path.exists(gate):
    time.sleep(0.005)
"""


def start_together(tmp_path, home, bodies):
    """Start one process per body, release them at once, wait for all."""
    gate = tmp_path / "gate"
    env = dict(os.environ, NANDATOWN_HOME=str(home), GATE=str(gate))
    processes = [subprocess.Popen(
        [sys.executable, "-c", WIDEN_WINDOWS + body], env=env,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        for body in bodies]
    time.sleep(0.5)
    gate.touch()
    outcomes = [process.communicate(timeout=120) for process in processes]
    for process, (_out, err) in zip(processes, outcomes):
        assert process.returncode == 0, err
    return [out for out, _err in outcomes]


def key_public(keystore_dir, name):
    private = (Path(keystore_dir) / f"{name}.controller.key").read_text()
    key = Ed25519PrivateKey.from_private_bytes(bytes.fromhex(private.strip()))
    return key.public_key().public_bytes_raw().hex()


def test_concurrent_runs_in_a_fresh_home_all_attest_verifiably(tmp_path):
    """Every run signs with the key it names, and all share one identity."""
    home = tmp_path / "home"
    runs = [tmp_path / f"runs{i}" for i in range(PROCESSES)]
    start_together(tmp_path, home, [
        "from nandatown.cli import main\n"
        f"sys.exit(main(['run', 'voting', '--out', {str(out)!r}]))\n"
        for out in runs])

    bundles = [next(p for p in out.iterdir() if p.is_dir()) for out in runs]
    attestations = [json.loads((b / "attestation.json").read_text())
                    for b in bundles]
    keystore_dir = home / "identity"

    for bundle in bundles:
        assert verify_bundle(str(bundle)) == [], bundle
    assert {a["controller_public"] for a in attestations} \
        == {key_public(keystore_dir, OPERATOR_NAME)}
    registry = json.loads((keystore_dir / "registry.json").read_text())
    assert [entry["name"] for entry in registry.values()] == [OPERATOR_NAME]


def test_an_established_key_and_other_identities_are_never_replaced(
        tmp_path):
    home = tmp_path / "home"
    keystore_dir = home / "identity"
    established = Keystore(str(keystore_dir)).new_identity(OPERATOR_NAME)
    other = Keystore(str(keystore_dir)).new_identity("alice")
    key_path = keystore_dir / f"{OPERATOR_NAME}.controller.key"
    key_before = key_path.read_bytes()

    outputs = start_together(tmp_path, home, [
        "from nandatown.identity_portable import Keystore\n"
        f"ks = Keystore({str(keystore_dir)!r})\n"
        f"print(json.dumps(ks.new_identity({name!r})))\n"
        for name in [OPERATOR_NAME] * 3 + ["bob", "carol"]])

    returned = [json.loads(out) for out in outputs]
    assert all(r == established for r in returned[:3])
    assert key_path.read_bytes() == key_before
    registry_path = str(keystore_dir / "registry.json")
    for identity in [established, other, *returned[3:]]:
        assert resolve_file(registry_path, identity["agent_id"]) \
            == identity["controller_public"]


def test_concurrent_new_identities_all_stay_registered(tmp_path):
    """No process's registry write loses another's, or is read half done."""
    home = tmp_path / "home"
    keystore_dir = home / "identity"
    names = [f"agent{i}" for i in range(PROCESSES)]

    outputs = start_together(tmp_path, home, [
        "from nandatown.identity_portable import Keystore\n"
        f"ks = Keystore({str(keystore_dir)!r})\n"
        f"print(json.dumps(ks.new_identity({name!r})))\n"
        for name in names])

    registry_path = str(keystore_dir / "registry.json")
    for name, out in zip(names, outputs):
        identity = json.loads(out)
        assert identity["controller_public"] == key_public(keystore_dir, name)
        assert resolve_file(registry_path, identity["agent_id"]) \
            == identity["controller_public"]


def test_two_homes_sharing_a_registry_keep_every_entry(tmp_path):
    """A registry symlinked into another keystore is locked where it lives."""
    shared_dir = tmp_path / "shared" / "identity"
    linked_dir = tmp_path / "linked" / "identity"
    shared_dir.mkdir(parents=True)
    linked_dir.mkdir(parents=True)
    (shared_dir / "registry.json").write_text("{}")
    (linked_dir / "registry.json").symlink_to(shared_dir / "registry.json")
    work = [(shared_dir, f"shared{i}") for i in range(3)] \
        + [(linked_dir, f"linked{i}") for i in range(3)]

    outputs = start_together(tmp_path, tmp_path / "home", [
        "from nandatown.identity_portable import Keystore\n"
        f"ks = Keystore({str(directory)!r})\n"
        f"print(json.dumps(ks.new_identity({name!r})))\n"
        for directory, name in work])

    registry = json.loads((shared_dir / "registry.json").read_text())
    assert {json.loads(out)["agent_id"] for out in outputs} == set(registry)
    assert (linked_dir / "registry.json").is_symlink()


def test_a_home_the_race_already_damaged_still_attests(tmp_path):
    """Before this fix, a race could leave a registry naming the operator
    twice while the key file held only one of those keys, and not the one
    read first. Signing follows the key on disk, so the attestation
    verifies, and neither registry entry is removed."""
    keystore_dir = tmp_path / "identity"
    keys = [Ed25519PrivateKey.generate() for _ in range(2)]
    publics = [k.public_key().public_bytes_raw().hex() for k in keys]
    ids = ["did:town:" + fingerprint(p).removeprefix("sha256:")[:24]
           for p in publics]
    keystore_dir.mkdir()
    (keystore_dir / "registry.json").write_text(json.dumps(
        {agent_id: {"name": OPERATOR_NAME, "controller_public": public,
                    "registered_at": 1.0}
         for agent_id, public in zip(ids, publics)},
        indent=2, sort_keys=True))
    on_disk = ids.index(max(ids))  # the entry a registry search reaches last
    (keystore_dir / f"{OPERATOR_NAME}.controller.key").write_text(
        keys[on_disk].private_bytes_raw().hex() + "\n")
    bundle_dir, _ = run_lab("voting", str(tmp_path / "runs"))

    attestation = attest_bundle(bundle_dir,
                                keystore=Keystore(str(keystore_dir)))

    assert attestation["controller_public"] == publics[on_disk]
    assert attestation["payload"]["signer"] == ids[on_disk]
    assert verify_bundle(bundle_dir) == []
    assert set(json.loads((keystore_dir / "registry.json").read_text())) \
        == set(ids)


def test_a_key_left_without_its_registry_entry_is_registered(tmp_path):
    """A process can stop between writing a key and registering it."""
    keystore = Keystore(str(tmp_path / "identity"))
    identity = keystore.new_identity("seller")
    Path(keystore.registry_path).write_text("{}")

    assert keystore.new_identity("seller") == identity
    assert resolve_file(keystore.registry_path, identity["agent_id"]) \
        == identity["controller_public"]


# ---- one process: what the lock cannot see ----------------------------------

def writer_arrives_first(monkeypatch, keystore_dir, name):
    """Another writer, one that takes no lock, puts a key down after this
    process found none and before it publishes its own."""
    other = Ed25519PrivateKey.generate()
    real = Ed25519PrivateKey

    class ArrivesDuringGeneration:
        from_private_bytes = staticmethod(real.from_private_bytes)

        @staticmethod
        def generate():
            (Path(keystore_dir) / f"{name}.controller.key").write_text(
                other.private_bytes_raw().hex() + "\n")
            return real.generate()

    monkeypatch.setattr(identity_portable, "Ed25519PrivateKey",
                        ArrivesDuringGeneration)
    return other.public_key().public_bytes_raw().hex()


@pytest.mark.parametrize("hard_links", [True, False],
                         ids=["hard-links", "no-hard-links"])
def test_a_key_another_writer_put_down_first_is_kept(tmp_path, monkeypatch,
                                                     hard_links):
    keystore_dir = tmp_path / "identity"
    keystore = Keystore(str(keystore_dir))
    if not hard_links:
        def no_links(src, dst):
            raise OSError(errno.EPERM, "Operation not permitted")
        monkeypatch.setattr(os, "link", no_links)
    first = writer_arrives_first(monkeypatch, keystore_dir, "seller")

    identity = keystore.new_identity("seller")

    assert identity["controller_public"] == first
    assert key_public(keystore_dir, "seller") == first
    assert resolve_file(keystore.registry_path, identity["agent_id"]) == first


def test_a_home_without_hard_links_still_creates_identities(tmp_path,
                                                            monkeypatch):
    def no_links(src, dst):
        raise OSError(errno.ENOTSUP, "Operation not supported")
    monkeypatch.setattr(os, "link", no_links)
    keystore = Keystore(str(tmp_path / "identity"))

    identity = keystore.new_identity("seller")

    assert keystore.new_identity("seller") == identity
    assert identity["controller_public"] == key_public(tmp_path / "identity",
                                                       "seller")


def test_a_private_key_a_dead_writer_left_staged_is_removed(tmp_path):
    keystore_dir = tmp_path / "identity"
    keystore = Keystore(str(keystore_dir))
    leftover = keystore_dir / ".staged-0123456789abcdef"
    leftover.write_text(Ed25519PrivateKey.generate().private_bytes_raw().hex())

    keystore.new_identity("seller")

    assert not leftover.exists()
    assert not [p for p in keystore_dir.iterdir()
                if p.name.startswith(".staged-")]


def test_a_key_the_registry_lists_under_another_name_is_refused(tmp_path):
    keystore_dir = tmp_path / "identity"
    keystore = Keystore(str(keystore_dir))
    keystore.new_identity("alice")
    (keystore_dir / "bob.controller.key").write_bytes(
        (keystore_dir / "alice.controller.key").read_bytes())

    with pytest.raises(IdentityError, match="registered as 'alice'"):
        keystore.new_identity("bob")
    with pytest.raises(IdentityError):
        keystore.identity("bob")


def test_a_key_the_registry_does_not_list_is_no_identity(tmp_path):
    keystore = Keystore(str(tmp_path / "identity"))
    keystore.new_identity("seller")
    Path(keystore.registry_path).unlink()

    with pytest.raises(IdentityError):
        keystore.identity("seller")
    with pytest.raises(IdentityError):
        keystore.make_grant("seller", "run-1")


def test_a_damaged_key_file_is_an_identity_error(tmp_path):
    keystore_dir = tmp_path / "identity"
    keystore = Keystore(str(keystore_dir))
    keystore.new_identity("seller")
    (keystore_dir / "seller.controller.key").write_text("not a key\n")

    with pytest.raises(IdentityError, match="does not hold a controller key"):
        keystore.new_identity("seller")


def test_the_registry_keeps_its_permissions_and_its_link(tmp_path):
    keystore_dir = tmp_path / "identity"
    shared = tmp_path / "shared-registry.json"
    shared.write_text("{}")
    shared.chmod(0o600)
    keystore_dir.mkdir()
    (keystore_dir / "registry.json").symlink_to(shared)
    keystore = Keystore(str(keystore_dir))

    identity = keystore.new_identity("seller")

    assert (keystore_dir / "registry.json").is_symlink()
    assert identity["agent_id"] in json.loads(shared.read_text())
    assert stat.S_IMODE(shared.stat().st_mode) == 0o600
