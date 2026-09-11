"""A Track timeout too short for Town's own counterparts is refused.

The runner carves each stock participant's DEADLINE out of the wait
timeout. Below the floor Town's buyer or seller would give up before it
could do its part, and the agent under test would be reported INCOMPLETE
for a run that could never have been valid.
"""

import math
import os
import shlex
import sys

import pytest

import nandatown.runner as runner_module
from nandatown.cli import main
from nandatown.runner import run_town

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
EXAMPLE = os.path.join(REPO_ROOT, "examples", "byoa_seller.py")
EXAMPLE_CMD = " ".join(shlex.quote(p) for p in [sys.executable, EXAMPLE])

# Values that leave the stock buyer no usable budget, or are no timeout.
UNUSABLE = [19.9, 15.0, 10.0, 5.0, 0.0, -1.0, math.nan, math.inf]


@pytest.fixture
def reject_startup(monkeypatch):
    def fail_if_started():
        raise AssertionError("a refused timeout reached port allocation")

    monkeypatch.setattr(runner_module, "_free_port", fail_if_started)


def test_minimum_is_derived_from_the_counterpart_deadlines():
    assert runner_module.MIN_WAIT_TIMEOUT == 20.0
    assert runner_module.MIN_WAIT_TIMEOUT == (
        runner_module.BUYER_DEADLINE_MARGIN + runner_module.MIN_BUYER_BUDGET)
    assert runner_module.MIN_BUYER_BUDGET > 0
    # The seller's margin is the smaller one: it outlives the buyer.
    assert (runner_module.SELLER_DEADLINE_MARGIN
            < runner_module.BUYER_DEADLINE_MARGIN)


@pytest.mark.parametrize("timeout", UNUSABLE)
def test_run_town_refuses_a_timeout_that_starves_town_counterparts(
        tmp_path, reject_startup, timeout):
    out_dir = tmp_path / "not-created"

    with pytest.raises(ValueError, match=r"at least 20 s"):
        run_town("quote-clean", str(out_dir), wait_timeout=timeout)

    assert not out_dir.exists()


@pytest.mark.parametrize("join", [["--cmd", EXAMPLE_CMD], ["--wait"]])
@pytest.mark.parametrize("role", ["seller", "buyer"])
@pytest.mark.parametrize("timeout", ["10", "5", "19.9"])
def test_test_agent_refuses_a_short_timeout_as_usage_error(
        tmp_path, capsys, reject_startup, join, role, timeout):
    out_dir = tmp_path / "not-created"

    code = main(["test-agent", "--role", role, *join, "--timeout", timeout,
                 "--out", str(out_dir)])

    out = capsys.readouterr().out
    assert code == 2, out
    assert f"--timeout {float(timeout):g} s is too short" in out
    assert "at least 20 s" in out
    # True whichever role is under test: with --role buyer Town starts
    # no stock buyer.
    assert "stock buyer" not in out
    assert "export TOKEN" not in out  # refused before any credentials
    assert not out_dir.exists()


def test_test_agent_help_names_the_minimum(capsys):
    with pytest.raises(SystemExit) as exit_info:
        main(["test-agent", "--help"])
    assert exit_info.value.code == 0
    help_text = " ".join(capsys.readouterr().out.split())
    assert (f"minimum {runner_module.MIN_WAIT_TIMEOUT:g}"
            in help_text), help_text
    assert "stock buyer" not in help_text


def test_the_minimum_timeout_runs_and_gives_the_buyer_a_budget(
        tmp_path, monkeypatch):
    deadlines = {}
    spawn = runner_module._spawn_participant

    def record_deadline(command, url, run_id, name, token, state_dir,
                        fault, deadline, **kwargs):
        deadlines[name] = float(deadline)
        return spawn(command, url, run_id, name, token, state_dir, fault,
                     deadline, **kwargs)

    monkeypatch.setattr(runner_module, "_spawn_participant",
                        record_deadline)

    _, result = run_town("quote-clean", str(tmp_path),
                         wait_timeout=runner_module.MIN_WAIT_TIMEOUT)

    detail = [(s.name, s.status, s.note) for s in result.stages]
    assert result.verdict == "passed", detail
    assert deadlines == {"buyer": runner_module.MIN_BUYER_BUDGET,
                         "seller": 15.0}


def test_test_agent_at_the_minimum_passes_a_correct_seller(tmp_path, capsys):
    code = main(["test-agent", "--role", "seller", "--cmd", EXAMPLE_CMD,
                 "--timeout", "20", "--out", str(tmp_path)])

    out = capsys.readouterr().out
    assert code == 0, out
    assert "Verdict:   PASSED" in out
