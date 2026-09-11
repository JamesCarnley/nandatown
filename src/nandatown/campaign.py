"""Campaigns: reliability claims need repetition, not one lucky run.

One run resolving to one verdict certifies luck when the actor is
nondeterministic. A campaign precommits its plan (name, trial count,
seeds, evaluator, publication policy) before the first trial, runs every
trial, and reports the full distribution. Every pass, failure, error,
and missing verdict stays in the record.
"""

from __future__ import annotations

import json
import os
import time
import uuid
from typing import Any

from . import __version__
from .profiles import PROFILES


def _resolve_mode(name: str) -> str:
    if name in PROFILES:
        return "track"
    from .sim.scenario import bundled_scenarios
    if name in bundled_scenarios():
        return "lab"
    raise KeyError(f"{name!r} is neither a track profile nor a lab scenario")


def run_campaign(name: str, trials: int, out_dir: str,
                 seed_base: int = 1000) -> tuple[str, dict[str, Any]]:
    mode = _resolve_mode(name)
    campaign_id = "camp-" + uuid.uuid4().hex[:10]
    campaign_dir = os.path.join(out_dir, campaign_id)
    os.makedirs(campaign_dir, exist_ok=True)

    seeds = [seed_base + i for i in range(trials)] if mode == "lab" else []
    plan = {
        "campaign_id": campaign_id,
        "name": name,
        "mode": mode,
        "trials": trials,
        "seeds": seeds,
        "nandatown_version": __version__,
        "declared_at": time.time(),
        "policy": "every trial is reported: pass, fail, incomplete, error",
    }
    # The plan is committed before the first trial runs.
    with open(os.path.join(campaign_dir, "campaign.json"), "w") as f:
        json.dump(plan, f, indent=2)

    trial_records: list[dict[str, Any]] = []
    for i in range(trials):
        record: dict[str, Any] = {"trial": i + 1}
        try:
            if mode == "lab":
                from .sim.runner import run_lab
                record["seed"] = seeds[i]
                bundle_dir, result = run_lab(name, campaign_dir,
                                             seed=seeds[i])
            else:
                from .runner import run_town
                bundle_dir, result = run_town(name, campaign_dir)
            record["run_id"] = result.run_id
            record["verdict"] = result.verdict
            record["bundle"] = os.path.basename(bundle_dir)
            record["stages"] = {s.name: s.status for s in result.stages}
            run_path = os.path.join(bundle_dir, "run.json")
            if os.path.exists(run_path):
                with open(run_path, encoding="utf-8") as f:
                    run_config = json.load(f).get("config", {})
                if "model" in run_config:
                    record["model"] = run_config["model"]
        except Exception as exc:
            record["verdict"] = "error"
            record["error"] = f"{type(exc).__name__}: {exc}"
        trial_records.append(record)

    verdicts: dict[str, int] = {}
    stage_counts: dict[str, dict[str, int]] = {}
    for record in trial_records:
        verdicts[record["verdict"]] = verdicts.get(record["verdict"], 0) + 1
        for stage, status in record.get("stages", {}).items():
            stage_counts.setdefault(stage, {})
            stage_counts[stage][status] = \
                stage_counts[stage].get(status, 0) + 1

    observed_models = sorted({r["model"] for r in trial_records
                              if "model" in r})
    aggregate = {
        "campaign_id": campaign_id,
        "name": name,
        "mode": mode,
        "trials": trials,
        "verdicts": verdicts,
        "stages": stage_counts,
        "trial_records": trial_records,
        "observed_models": observed_models,
        "model_drift_detected": len(observed_models) > 1,
        "completed_at": time.time(),
    }
    with open(os.path.join(campaign_dir, "aggregate.json"), "w") as f:
        json.dump(aggregate, f, indent=2)
    with open(os.path.join(campaign_dir, "campaign-report.md"), "w") as f:
        f.write(render_campaign_report(plan, aggregate))
    return campaign_dir, aggregate


def render_campaign_report(plan: dict[str, Any],
                           aggregate: dict[str, Any]) -> str:
    lines: list[str] = []
    add = lines.append
    add("NANDA Town Campaign Report")
    add("=" * 40)
    add(f"Campaign:  {plan['campaign_id']}")
    add(f"Target:    {plan['name']} ({plan['mode']})")
    add(f"Trials:    {plan['trials']} (precommitted before the first run)")
    add(f"Policy:    {plan['policy']}")
    add("")
    add("Verdicts:")
    for verdict, count in sorted(aggregate["verdicts"].items()):
        add(f"  {verdict:<12} {count}/{plan['trials']}")
    add("")
    add("Per stage:")
    width = max((len(s) for s in aggregate["stages"]), default=10)
    for stage, counts in aggregate["stages"].items():
        parts = ", ".join(f"{k} {v}" for k, v in sorted(counts.items()))
        add(f"  {stage.ljust(width)}  {parts}")
    add("")
    if aggregate.get("observed_models"):
        models = ", ".join(aggregate["observed_models"])
        if aggregate.get("model_drift_detected"):
            add(f"Model drift DETECTED across trials: {models}. A hosted"
                " model changed underneath a pinned release; treat the"
                " distribution accordingly.")
        else:
            add(f"Observed model, stable across trials: {models}."
                " Hosted models are mutable dependencies; this canary"
                " watches for drift.")
        add("")
    add("The unit of evidence is this distribution, not any single run."
        " Any single result is one scoped observation.")
    return "\n".join(lines) + "\n"
