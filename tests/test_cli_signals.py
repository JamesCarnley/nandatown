"""Stopping the CLI must also stop the Town processes it started."""

import os
import shlex
import shutil
import signal
import subprocess
import sys
import threading
import time
import types

import pytest

import nandatown.cli as cli
import nandatown.runner as runner

pytestmark = pytest.mark.skipif(
    os.name != "posix", reason="POSIX signal delivery and process listing")

needs_ps = pytest.mark.skipif(shutil.which("ps") is None, reason="needs ps")

SOURCE_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CLI_ENV = dict(os.environ, PYTHONUNBUFFERED="1",
               PYTHONPATH=os.path.join(SOURCE_ROOT, "src"))


def _processes() -> dict[int, tuple[int, str]]:
    """Every running process: pid -> (parent pid, command line).

    Zombies are left out: they have exited and only await a reap.
    """
    listing = subprocess.run(
        ["ps", "-ww", "-A", "-o", "pid=", "-o", "ppid=", "-o", "stat=",
         "-o", "args="], capture_output=True, text=True, check=True)
    table = {}
    for line in listing.stdout.splitlines():
        fields = line.split(None, 3)
        if len(fields) < 3 or fields[2].startswith("Z"):
            continue
        table[int(fields[0])] = (int(fields[1]),
                                 fields[3] if len(fields) == 4 else "")
    return table


def _descendants(root: int) -> dict[int, str]:
    """The running descendants of root: pid -> command line."""
    table = _processes()
    found: dict[int, str] = {}
    parents = [root]
    while parents:
        parent = parents.pop()
        for pid, (ppid, args) in table.items():
            if ppid == parent and pid not in found:
                found[pid] = args
                parents.append(pid)
    return found


def _still_running(recorded: dict[int, str]) -> dict[int, str]:
    """The recorded processes still running under the same command line."""
    table = _processes()
    return {pid: args for pid, args in recorded.items()
            if pid in table and table[pid][1] == args}


def _serves(args: str, db_path: str) -> bool:
    """Whether a command line is a coordinator serving exactly db_path.

    ps joins argv with spaces, so this relies on db_path having none, as
    pytest's temporary paths do not.
    """
    argv = args.split()
    if "nandatown.coordinator" not in argv or "--db" not in argv:
        return False
    at = argv.index("--db") + 1
    return at < len(argv) and argv[at] == db_path


def _kill(pids) -> None:
    for pid in pids:
        try:
            os.kill(pid, signal.SIGKILL)
        except ProcessLookupError:
            pass


def _require_default(signum: int) -> None:
    # A child inherits an ignored signal, so it could not be stopped by it.
    if signal.getsignal(signum) is signal.SIG_IGN:
        pytest.skip(f"{signal.Signals(signum).name} is ignored here")


@needs_ps
@pytest.mark.parametrize("signum", [signal.SIGTERM, signal.SIGINT],
                         ids=["sigterm", "sigint"])
def test_stopping_test_agent_stops_every_process_it_started(tmp_path,
                                                            signum):
    _require_default(signum)
    process = subprocess.Popen(
        [sys.executable, "-m", "nandatown.cli", "test-agent",
         "--role", "seller", "--wait", "--timeout", "60",
         "--out", str(tmp_path / "runs")],
        env=CLI_ENV, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
        text=True)
    lines: list[str] = []
    waiting = threading.Event()

    def read() -> None:
        for line in process.stdout:
            lines.append(line)
            if "export STATE_DIR=" in line:
                waiting.set()

    threading.Thread(target=read, daemon=True).start()
    started: dict[int, str] = {}
    db_path = None
    try:
        # Credentials are printed only once the coordinator is healthy.
        assert waiting.wait(30), "".join(lines)
        state_dir = next(line.split("=", 1)[1].strip() for line in lines
                         if "export STATE_DIR=" in line)
        db_path = os.path.join(os.path.dirname(state_dir), "town.db")
        # The stock buyer is started just after the credentials are printed.
        deadline = time.monotonic() + 15
        while True:
            started = _descendants(process.pid)
            commands = started.values()
            if (any(_serves(args, db_path) for args in commands)
                    and any("nandatown.participants.buyer" in args
                            for args in commands)):
                break
            assert time.monotonic() < deadline, \
                f"coordinator and buyer not both running: {started}"
            time.sleep(0.1)

        process.send_signal(signum)
        returncode = process.wait(timeout=15)
        deadline = time.monotonic() + 3
        while _still_running(started) and time.monotonic() < deadline:
            time.sleep(0.1)

        assert _still_running(started) == {}, \
            "processes outlived the CLI that started them"
        # The CLI still ends by the signal it was sent, as it did before
        # SIGTERM ran the cleanup; Python ends that way after an unhandled
        # KeyboardInterrupt too.
        assert returncode == -signum
    finally:
        if process.poll() is None:
            started.update(_descendants(process.pid))
            process.kill()
            process.wait(timeout=5)
        leftovers = set(_still_running(started))
        if db_path:
            leftovers.update(pid for pid, (_ppid, args)
                             in _processes().items()
                             if _serves(args, db_path))
        _kill(leftovers)


# The CLI with subprocess.Popen wrapped so that SIGTERM arrives just as the
# Nth Town process has started: Popen has returned, but the runner has not
# yet recorded the process for cleanup.
_SIGTERM_AS_A_PROCESS_STARTS = """
import os, signal, subprocess, sys
import nandatown.cli as cli

target, record = int(sys.argv[1]), sys.argv[2]
real_popen, started = subprocess.Popen, []

def popen(*args, **kwargs):
    process = real_popen(*args, **kwargs)
    if kwargs.get("start_new_session"):  # the runner's Town processes
        started.append(process.pid)
        if len(started) == target:
            with open(record, "w") as f:
                f.write(str(process.pid))
            os.kill(os.getpid(), signal.SIGTERM)
    return process

subprocess.Popen = popen
sys.exit(cli.main(sys.argv[3:]))
"""


@needs_ps
@pytest.mark.parametrize("target", [1, 2, 3],
                         ids=["coordinator", "seller", "buyer"])
def test_sigterm_as_a_town_process_starts_still_stops_it(tmp_path, target):
    _require_default(signal.SIGTERM)
    record = tmp_path / "started.pid"
    # Participants that neither join nor exit when the coordinator stops,
    # so one the runner had not recorded would keep running.
    idle = (f"cmd:{shlex.quote(sys.executable)}"
            " -c 'import time; time.sleep(60)'")
    process = subprocess.Popen(
        [sys.executable, "-c", _SIGTERM_AS_A_PROCESS_STARTS, str(target),
         str(record), "run", "quote-clean", "--out", str(tmp_path / "runs"),
         "--agent", f"seller={idle}", "--agent", f"buyer={idle}"],
        env=CLI_ENV, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    pid = None
    try:
        returncode = process.wait(timeout=30)
        assert record.exists(), f"process {target} never started"
        pid = int(record.read_text())
        # Seconds after it started, the pid cannot have been reused.
        deadline = time.monotonic() + 3
        while pid in _processes() and time.monotonic() < deadline:
            time.sleep(0.1)

        running = pid in _processes()
        assert not running, \
            "a process started as SIGTERM arrived outlived the CLI"
        assert returncode == -signal.SIGTERM
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=5)
        if pid is not None and pid in _processes():
            _kill([pid])


def test_runner_holds_stop_signals_while_a_process_starts():
    # A recording handler stands in for the CLI's, so the test process is
    # never exposed to SIGTERM's default disposition.
    seen = []

    def handler(_signum, _frame):
        seen.append("handled")

    previous = signal.signal(signal.SIGTERM, handler)
    try:
        with runner._stop_signals_held():
            signal.raise_signal(signal.SIGTERM)
            seen.append("process recorded")
        assert seen == ["process recorded", "handled"]
        assert signal.getsignal(signal.SIGTERM) is handler
    finally:
        signal.signal(signal.SIGTERM, previous)


class _Interrupted(Exception):
    """Stands in for KeyboardInterrupt, which would stop pytest itself."""


class _Unwound(BaseException):
    """A caller's own handler unwinding, which SystemExit really does."""


def test_a_stop_signal_while_handlers_are_restored_leaves_both_restored(
        monkeypatch):
    # SIGINT is raised as soon as the guard has put SIGINT's handler back,
    # before SIGTERM's, the moment a real SIGINT could land. Recording
    # handlers stand in for the caller's, so neither signal meets its
    # default disposition in the test process.
    seen = []

    def on_sigint(_signum, _frame):
        seen.append("SIGINT")
        raise _Interrupted

    def on_sigterm(_signum, _frame):
        seen.append("SIGTERM")

    def set_handler(signum, handler):
        previous = signal.signal(signum, handler)
        if handler is on_sigint:
            signal.raise_signal(signal.SIGINT)
        return previous

    instrumented = types.ModuleType("signal")
    instrumented.__dict__.update(vars(signal))
    instrumented.signal = set_handler
    previous = {signum: signal.signal(signum, handler) for signum, handler
                in [(signal.SIGINT, on_sigint), (signal.SIGTERM, on_sigterm)]}
    mask = signal.pthread_sigmask(signal.SIG_BLOCK, [])
    try:
        monkeypatch.setattr(runner, "signal", instrumented)
        with pytest.raises(_Interrupted):
            with runner._stop_signals_held():
                pass
        assert seen == ["SIGINT"]
        # A later SIGTERM reaches the caller's handler, not the guard's.
        signal.raise_signal(signal.SIGTERM)
        assert seen == ["SIGINT", "SIGTERM"]
        assert signal.getsignal(signal.SIGINT) is on_sigint
        assert signal.getsignal(signal.SIGTERM) is on_sigterm
        assert signal.pthread_sigmask(signal.SIG_BLOCK, []) == mask
    finally:
        for signum, handler in previous.items():
            signal.signal(signum, handler)


def test_holding_unblocks_stop_signals_when_blocking_them_raises(
        monkeypatch):
    # Blocking runs any other pending Python handler, which may raise after
    # the mask has changed; processes started later must not inherit
    # blocked stop signals.
    def block_then_raise(how, signals):
        previous = signal.pthread_sigmask(how, signals)
        if how == signal.SIG_BLOCK and signals:
            raise _Interrupted
        return previous

    instrumented = types.ModuleType("signal")
    instrumented.__dict__.update(vars(signal))
    instrumented.pthread_sigmask = block_then_raise
    previous = {signum: signal.getsignal(signum)
                for signum in (signal.SIGINT, signal.SIGTERM)}
    mask = signal.pthread_sigmask(signal.SIG_BLOCK, [])
    try:
        monkeypatch.setattr(runner, "signal", instrumented)
        with pytest.raises(_Interrupted):
            with runner._stop_signals_held():
                pass
        assert signal.pthread_sigmask(signal.SIG_BLOCK, []) == mask
    finally:
        signal.pthread_sigmask(signal.SIG_SETMASK, mask)
        for signum, handler in previous.items():
            signal.signal(signum, handler)


def test_holding_restores_caller_handlers_when_blocking_them_raises(
        monkeypatch):
    # The block that puts the caller's handlers back is what makes holding
    # them safe, so a failure to block first must not skip it: an embedding
    # caller that catches the error would otherwise keep the recording
    # handlers and silently lose every later cancellation.
    def block_then_raise(how, signals):
        previous = signal.pthread_sigmask(how, signals)
        if how == signal.SIG_BLOCK and signals:
            raise _Interrupted
        return previous

    instrumented = types.ModuleType("signal")
    instrumented.__dict__.update(vars(signal))
    instrumented.pthread_sigmask = block_then_raise
    delivered = []
    callers = {signum: (lambda s, _frame: delivered.append(s))
               for signum in (signal.SIGINT, signal.SIGTERM)}
    previous = {signum: signal.getsignal(signum) for signum in callers}
    mask = signal.pthread_sigmask(signal.SIG_BLOCK, [])
    try:
        for signum, handler in callers.items():
            signal.signal(signum, handler)
        monkeypatch.setattr(runner, "signal", instrumented)
        with pytest.raises(_Interrupted):
            with runner._stop_signals_held():
                pass

        for signum, handler in callers.items():
            assert signal.getsignal(signum) is handler
        for signum in callers:
            signal.raise_signal(signum)
        assert delivered == [signal.SIGINT, signal.SIGTERM]
    finally:
        signal.pthread_sigmask(signal.SIG_SETMASK, mask)
        for signum, handler in previous.items():
            signal.signal(signum, handler)


def _failing_mask_module(instrumented_signal=None):
    """A signal module whose blocking fails before it takes effect.

    The restores then run unblocked, which is the path the fix adds and
    the one where a stop signal can arrive between two of them.
    """
    def refuse_to_block(how, signals):
        if how == signal.SIG_BLOCK and signals:
            raise _Interrupted
        return signal.pthread_sigmask(how, signals)

    module = types.ModuleType("signal")
    module.__dict__.update(vars(signal))
    module.pthread_sigmask = refuse_to_block
    if instrumented_signal is not None:
        module.signal = instrumented_signal
    return module


def test_holding_restores_every_handler_when_one_restore_unwinds(monkeypatch):
    # Restoring unblocked means a stop signal can land between two
    # restores and run the handler just put back. If that handler
    # unwinds, the handlers still to be restored must not be abandoned:
    # they would keep recording into a block that has already ended, and
    # nothing would ever put them back.
    calls = []
    real_signal = signal.signal

    def restore_then_unwind(signum, handler):
        result = real_signal(signum, handler)
        calls.append(signum)
        # The first two calls install the guard's own handlers; the third
        # is the first restore, standing in for the arriving signal.
        if len(calls) == 3:
            raise _Unwound("the restored handler ran and unwound")
        return result

    delivered = []
    callers = {signum: (lambda s, _frame: delivered.append(s))
               for signum in (signal.SIGINT, signal.SIGTERM)}
    previous = {signum: signal.getsignal(signum) for signum in callers}
    mask = signal.pthread_sigmask(signal.SIG_BLOCK, [])
    try:
        for signum, handler in callers.items():
            signal.signal(signum, handler)
        monkeypatch.setattr(runner, "signal",
                            _failing_mask_module(restore_then_unwind))
        with pytest.raises(_Unwound):
            with runner._stop_signals_held():
                pass

        for signum, handler in callers.items():
            assert signal.getsignal(signum) is handler
        for signum in callers:
            signal.raise_signal(signum)
        assert delivered == [signal.SIGINT, signal.SIGTERM]
        assert signal.pthread_sigmask(signal.SIG_BLOCK, []) == mask
    finally:
        signal.pthread_sigmask(signal.SIG_SETMASK, mask)
        for signum, handler in previous.items():
            signal.signal(signum, handler)


def test_holding_restores_the_rest_when_one_restore_raises(monkeypatch):
    """A restore that fails outright still leaves the others done."""
    calls = []
    real_signal = signal.signal

    def restore_then_fail(signum, handler):
        result = real_signal(signum, handler)
        calls.append(signum)
        if len(calls) == 3:
            raise OSError("cannot set this handler")
        return result

    callers = {signum: (lambda s, _frame: None)
               for signum in (signal.SIGINT, signal.SIGTERM)}
    previous = {signum: signal.getsignal(signum) for signum in callers}
    mask = signal.pthread_sigmask(signal.SIG_BLOCK, [])
    try:
        for signum, handler in callers.items():
            signal.signal(signum, handler)
        monkeypatch.setattr(runner, "signal",
                            _failing_mask_module(restore_then_fail))
        with pytest.raises(OSError, match="cannot set this handler"):
            with runner._stop_signals_held():
                pass

        # The one that raised keeps whatever the failing call left; the
        # rest are the caller's again.
        assert signal.getsignal(signal.SIGTERM) is callers[signal.SIGTERM]
        assert signal.pthread_sigmask(signal.SIG_BLOCK, []) == mask
    finally:
        signal.pthread_sigmask(signal.SIG_SETMASK, mask)
        for signum, handler in previous.items():
            signal.signal(signum, handler)


def test_holding_leaves_a_callers_blocked_stop_signal_blocked():
    mask = signal.pthread_sigmask(signal.SIG_BLOCK, [signal.SIGTERM])
    try:
        with runner._stop_signals_held():
            pass
        assert signal.SIGTERM in signal.pthread_sigmask(signal.SIG_BLOCK, [])
    finally:
        signal.pthread_sigmask(signal.SIG_SETMASK, mask)


def test_sigterm_handling_is_scoped_to_the_command(monkeypatch):
    _require_default(signal.SIGTERM)
    before = signal.getsignal(signal.SIGTERM)
    seen = {}

    def command(_args):
        seen["handler"] = signal.getsignal(signal.SIGTERM)
        return 0

    monkeypatch.setattr(cli, "cmd_run", command)
    assert cli.main(["run", "quote-clean"]) == 0
    assert callable(seen["handler"]), "SIGTERM would skip run cleanup"
    assert signal.getsignal(signal.SIGTERM) is before


def test_sigterm_ends_the_cli_by_signal_after_cleanup(monkeypatch):
    # The handler is called directly and the re-delivery is recorded, so no
    # SIGTERM ever reaches the test process.
    _require_default(signal.SIGTERM)
    before = signal.getsignal(signal.SIGTERM)
    events: list = []

    def command(_args):
        handler = signal.getsignal(signal.SIGTERM)
        try:
            handler(signal.SIGTERM, None)
        finally:
            events.append("cleanup")

    def raise_signal(signum):
        events.append((signum, signal.getsignal(signum)))

    monkeypatch.setattr(cli, "cmd_run", command)
    monkeypatch.setattr(signal, "raise_signal", raise_signal)
    with pytest.raises(SystemExit) as stop:
        cli.main(["run", "quote-clean"])
    # Cleanup first, then SIGTERM again under its default disposition; the
    # exit status is only a fallback should that not end the process.
    assert events == ["cleanup", (signal.SIGTERM, signal.SIG_DFL)]
    assert stop.value.code == 128 + signal.SIGTERM
    assert signal.getsignal(signal.SIGTERM) is before
