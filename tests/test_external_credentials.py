import json
import os
import shlex
import signal
import subprocess
import sys

import pytest

from nandatown.cli import _print_join_credentials

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
EXAMPLE = os.path.join(REPO_ROOT, "examples", "byoa_seller.py")


def _stop_cli(cli: subprocess.Popen) -> None:
    """Interrupt, then kill. SIGINT lets run_town's finally stop the
    coordinator it started in its own session; SIGKILL would orphan it."""
    if cli.poll() is not None:
        return
    cli.send_signal(signal.SIGINT)
    try:
        cli.wait(timeout=10)
    except subprocess.TimeoutExpired:
        cli.kill()
        cli.wait(timeout=5)


@pytest.mark.parametrize("argv", [
    ["run", "quote-clean", "--agent", "seller=external"],
    ["test-agent", "--role", "seller", "--wait", "--timeout", "30"],
], ids=["run-agent-external", "test-agent-wait"])
def test_cli_prints_join_credentials_through_a_pipe_while_waiting(
        tmp_path, argv):
    """An operator (or CI) reads the credentials from the CLI's piped
    stdout and starts the agent from them while the run is waiting."""
    import queue
    import threading
    import time

    env = {k: v for k, v in os.environ.items() if k != "PYTHONUNBUFFERED"}
    env["NANDATOWN_HOME"] = str(tmp_path / "home")
    cli = subprocess.Popen(
        [sys.executable, "-m", "nandatown.cli", *argv,
         "--out", str(tmp_path / "runs")],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
        env=env)
    lines: queue.Queue = queue.Queue()

    def read() -> None:
        for line in cli.stdout:
            lines.put(line)
        lines.put(None)

    threading.Thread(target=read, daemon=True).start()
    seen: list[str] = []
    exports: dict[str, str] = {}
    agent = None
    try:
        deadline = time.monotonic() + 20
        while True:
            try:
                line = lines.get(
                    timeout=max(deadline - time.monotonic(), 0.01))
            except queue.Empty:
                pytest.fail("no join credentials on the CLI's piped stdout"
                            " within 20 s; output so far:\n"
                            + "".join(seen))
            if line is None:
                pytest.fail("the CLI ended before handing out join"
                            " credentials:\n" + "".join(seen))
            seen.append(line)
            if line.strip().startswith("export "):
                key, _, value = shlex.split(line)[1].partition("=")
                exports[key] = value
            if line.strip().startswith("then join"):
                break
        assert cli.poll() is None, "credentials arrived after the run ended"
        assert set(exports) == {"TOWN_URL", "RUN_ID", "NAME", "TOKEN",
                                "STATE_DIR", "DEADLINE"}
        assert exports["NAME"] == "seller"
        # How long the run will wait is a fact about the run, and an
        # outside agent has no other way to learn it. FAULT is not handed
        # over: an outside agent is the subject, not one of this town's
        # scripted participants.
        assert float(exports["DEADLINE"]) > 0
        assert "FAULT" not in exports
        agent = subprocess.Popen(
            [sys.executable, EXAMPLE], env={**os.environ, **exports},
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        exit_code = cli.wait(timeout=60)
    finally:
        _stop_cli(cli)
        if agent is not None and agent.poll() is None:
            agent.kill()
            agent.wait(timeout=5)
    while (line := lines.get(timeout=5)) is not None:
        seen.append(line)
    output = "".join(seen)
    assert exit_code == 0, output
    assert "Verdict:   PASSED" in output


def test_printed_join_credentials_survive_a_shell_round_trip(capsys):
    """Values a shell would split or unquote (a path with a space, a
    TOWN_GRANT JSON) come back exactly from the printed export lines."""
    grant = json.dumps({
        "grant": {"agent_id": "seller", "permissions": ["join", "send"],
                  "note": "it's $HOME and `pwd`"},
        "grant_signature": "c2lnbmF0dXJl",
        "session_private": "a b\\c"})
    env = {"TOWN_URL": "http://127.0.0.1:8123", "RUN_ID": "run-1",
           "NAME": "seller", "TOKEN": "tok123",
           "STATE_DIR": "/tmp/town state/seller", "TOWN_GRANT": grant}

    _print_join_credentials("seller", env)

    exports: dict[str, str] = {}
    for line in capsys.readouterr().out.splitlines():
        words = shlex.split(line)
        if words[:1] == ["export"]:
            assert len(words) == 2, line
            key, _, value = words[1].partition("=")
            exports[key] = value
    assert list(exports) == list(env)
    assert exports == env
