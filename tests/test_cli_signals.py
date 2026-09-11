"""Stopping the CLI must also stop the Town processes it started."""

import os
import shutil
import signal
import subprocess
import sys
import threading
import time

import pytest

import nandatown.cli as cli

pytestmark = pytest.mark.skipif(
    os.name != "posix", reason="POSIX signal delivery and process listing")

SOURCE_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _coordinator_pids(db_path: str) -> list[int]:
    """Coordinator processes serving exactly this database."""
    listing = subprocess.run(["ps", "-ww", "-A", "-o", "pid=", "-o", "args="],
                             capture_output=True, text=True, check=True)
    pids = []
    for line in listing.stdout.splitlines():
        pid, _, args = line.strip().partition(" ")
        if "nandatown.coordinator" in args and f"--db {db_path} " in args:
            pids.append(int(pid))
    return pids


def _require_default(signum: int) -> None:
    # A child inherits an ignored signal, so it could not be stopped by it.
    if signal.getsignal(signum) is signal.SIG_IGN:
        pytest.skip(f"{signal.Signals(signum).name} is ignored here")


@pytest.mark.skipif(shutil.which("ps") is None, reason="needs ps")
@pytest.mark.parametrize("signum", [signal.SIGTERM, signal.SIGINT],
                         ids=["sigterm", "sigint"])
def test_stopping_test_agent_stops_its_coordinator(tmp_path, signum):
    _require_default(signum)
    env = dict(os.environ, PYTHONUNBUFFERED="1",
               PYTHONPATH=os.path.join(SOURCE_ROOT, "src"))
    process = subprocess.Popen(
        [sys.executable, "-m", "nandatown.cli", "test-agent",
         "--role", "seller", "--wait", "--timeout", "60",
         "--out", str(tmp_path / "runs")],
        env=env, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
        text=True)
    lines: list[str] = []
    waiting = threading.Event()

    def read() -> None:
        for line in process.stdout:
            lines.append(line)
            if "export STATE_DIR=" in line:
                waiting.set()

    threading.Thread(target=read, daemon=True).start()
    db_path = None
    try:
        # Credentials are printed only once the coordinator is healthy.
        assert waiting.wait(30), "".join(lines)
        state_dir = next(line.split("=", 1)[1].strip() for line in lines
                         if "export STATE_DIR=" in line)
        db_path = os.path.join(os.path.dirname(state_dir), "town.db")
        assert _coordinator_pids(db_path), "coordinator not found"

        process.send_signal(signum)
        returncode = process.wait(timeout=15)
        deadline = time.monotonic() + 3
        while _coordinator_pids(db_path) and time.monotonic() < deadline:
            time.sleep(0.1)

        assert _coordinator_pids(db_path) == [], \
            "the coordinator outlived the CLI that started it"
        if signum == signal.SIGTERM:
            assert returncode == 128 + signal.SIGTERM
        else:
            assert returncode != 0
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=5)
        for pid in _coordinator_pids(db_path) if db_path else []:
            try:
                os.kill(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass


def test_sigterm_handling_is_scoped_to_the_command(monkeypatch):
    _require_default(signal.SIGTERM)
    before = signal.getsignal(signal.SIGTERM)
    seen = {}

    def command(_args):
        handler = signal.getsignal(signal.SIGTERM)
        assert callable(handler), "SIGTERM would skip run cleanup"
        with pytest.raises(SystemExit) as stop:
            handler(signal.SIGTERM, None)
        seen["code"] = stop.value.code
        return 0

    monkeypatch.setattr(cli, "cmd_run", command)
    assert cli.main(["run", "quote-clean"]) == 0
    assert seen["code"] == 128 + signal.SIGTERM
    assert signal.getsignal(signal.SIGTERM) is before
