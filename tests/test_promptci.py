from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from promptci.engine import (
    HONESTY,
    apply_fixture_change,
    create_fixture,
    diff_project,
    gate_project,
    read_json,
    render_report,
    run_project,
    state_dir,
    write_json,
)


ROOT = Path(__file__).resolve().parents[1]


def snapshot_tree(path: Path) -> dict[str, bytes]:
    return {str(file.relative_to(path)): file.read_bytes() for file in sorted(path.rglob("*")) if file.is_file()}


def test_fixture_generation_is_byte_deterministic(tmp_path: Path) -> None:
    first, second = tmp_path / "first", tmp_path / "second"
    create_fixture(first)
    create_fixture(second)
    assert snapshot_tree(first) == snapshot_tree(second)
    assert "synthetic" in read_json(first / "promptci.json")["project_name"].lower()
    assert set(read_json(first / "fixture_variants.json")["scenarios"]) == {"baseline", "regression", "cost"}


def test_identical_eval_runs_and_reports_are_byte_identical(tmp_path: Path) -> None:
    projects = [tmp_path / "one", tmp_path / "two"]
    for project in projects:
        create_fixture(project, scenario="regression")
        run_project(project)
        write_json(state_dir(project) / "latest-diff.json", diff_project(project))
        gate_project(project)
        report = render_report(project, "md")
        (project / "report.md").write_text(report, encoding="utf-8", newline="\n")
    first, second = projects
    assert (state_dir(first) / "latest-run.json").read_bytes() == (state_dir(second) / "latest-run.json").read_bytes()
    assert (state_dir(first) / "latest-diff.json").read_bytes() == (state_dir(second) / "latest-diff.json").read_bytes()
    assert (state_dir(first) / "latest-gate.json").read_bytes() == (state_dir(second) / "latest-gate.json").read_bytes()
    assert (first / "report.md").read_bytes() == (second / "report.md").read_bytes()


def test_no_change_run_is_a_named_noop(tmp_path: Path) -> None:
    project = tmp_path / "demo"
    create_fixture(project)
    run = run_project(project)
    assert run["status"] == "skipped"
    assert run["reason"] == "SKIPPED: no prompt changes since the last release."
    assert run["executed_suites"] == []


def test_changed_prompt_runs_only_its_affected_suite(tmp_path: Path) -> None:
    project = tmp_path / "demo"
    create_fixture(project)
    assert apply_fixture_change(project, "regression") == ["summary"]
    run = run_project(project)
    assert run["changed_prompts"] == ["summary"]
    assert run["affected_suites"] == ["summary_quality"]
    assert run["executed_suites"] == ["summary_quality"]
    assert "classifier_cost" not in run["executed_results"]


def test_planted_regression_is_attributed_with_before_and_after_scores(tmp_path: Path) -> None:
    project = tmp_path / "demo"
    create_fixture(project)
    apply_fixture_change(project, "regression")
    run_project(project)
    diff = diff_project(project)
    assert len(diff["regressions"]) == 2
    assert {item["prompt"] for item in diff["regressions"]} == {"summary"}
    assert [(item["score_before"], item["score_after"]) for item in diff["regressions"]] == [(1.0, 0.555556), (1.0, 0.5)]
    assert all(item["score_before"] > item["score_after"] for item in diff["regressions"])


def test_cost_and_latency_deltas_match_declared_fixture_profiles(tmp_path: Path) -> None:
    project = tmp_path / "demo"
    create_fixture(project)
    apply_fixture_change(project, "cost")
    run_project(project)
    diff = diff_project(project)
    metrics = diff["metrics"]
    # Baseline cost is 34 summary tokens at $0.000002 plus 18 classifier tokens at $0.000001.
    # The verbose outputs add 18 classifier tokens at the declared classifier rate.
    assert metrics["baseline_cost_usd"] == "0.0000860000"
    assert metrics["candidate_cost_usd"] == "0.0001040000"
    assert metrics["cost_delta_usd"] == "0.0000180000"
    assert metrics["cost_delta_pct"] == "20.93"
    assert metrics["baseline_avg_latency_ms"] == "54.00"
    assert metrics["candidate_avg_latency_ms"] == "58.50"
    assert metrics["latency_delta_ms"] == "4.50"
    assert metrics["latency_delta_pct"] == "8.33"
    assert diff["regressions"] == []


def test_failing_gate_keeps_release_and_names_regression_reason(tmp_path: Path) -> None:
    project = tmp_path / "demo"
    create_fixture(project)
    apply_fixture_change(project, "regression")
    run_project(project)
    release_before = read_json(state_dir(project) / "release.json")
    baseline_before = read_json(state_dir(project) / "baseline.json")
    gate = gate_project(project)
    assert gate["decision"] == "BLOCKED"
    assert gate["reason"] == "BLOCKED: regression threshold exceeded (2 > 0)."
    assert read_json(state_dir(project) / "release.json") == release_before
    assert read_json(state_dir(project) / "baseline.json") == baseline_before


def test_passing_gate_writes_release_record_and_new_baseline(tmp_path: Path) -> None:
    project = tmp_path / "demo"
    create_fixture(project)
    apply_fixture_change(project, "cost")
    config = read_json(project / "promptci.json")
    config["gate"]["max_cost_increase_pct"] = 30
    write_json(project / "promptci.json", config)
    run_project(project)
    gate = gate_project(project)
    assert gate["decision"] == "PASS"
    assert gate["baseline_updated"] is True
    release = read_json(state_dir(project) / "release.json")
    baseline = read_json(state_dir(project) / "baseline.json")
    assert release["prompt_versions"]["classifier"] == "2.0"
    assert baseline["results"] == read_json(state_dir(project) / "latest-run.json")["candidate_results"]


def test_cost_budget_gate_blocks_with_named_reason(tmp_path: Path) -> None:
    project = tmp_path / "demo"
    create_fixture(project, scenario="cost")
    run_project(project)
    gate = gate_project(project)
    assert gate["decision"] == "BLOCKED"
    assert gate["reason"] == "BLOCKED: cost increase exceeds budget (20.93% > 10.00%)."


def test_latency_budget_gate_blocks_with_named_reason(tmp_path: Path) -> None:
    project = tmp_path / "demo"
    create_fixture(project, scenario="cost")
    config = read_json(project / "promptci.json")
    config["gate"]["max_cost_increase_pct"] = 30
    config["gate"]["max_latency_increase_pct"] = 5
    write_json(project / "promptci.json", config)
    run_project(project)
    gate = gate_project(project)
    assert gate["decision"] == "BLOCKED"
    assert gate["reason"] == "BLOCKED: latency increase exceeds budget (8.33% > 5.00%)."


def test_report_retains_before_after_comparison_after_a_pass(tmp_path: Path) -> None:
    project = tmp_path / "demo"
    create_fixture(project)
    apply_fixture_change(project, "cost")
    config = read_json(project / "promptci.json")
    config["gate"]["max_cost_increase_pct"] = 30
    write_json(project / "promptci.json", config)
    run_project(project)
    assert gate_project(project)["decision"] == "PASS"
    report = render_report(project, "md")
    assert "Decision: **PASS**" in report
    assert "0.0000090000" in report
    assert "20.93%" in report


def test_report_has_change_deltas_regressions_gate_and_honesty(tmp_path: Path) -> None:
    project = tmp_path / "demo"
    create_fixture(project)
    apply_fixture_change(project, "regression")
    run_project(project)
    gate_project(project)
    report = render_report(project, "md")
    for required in ("Change summary", "Per-case deltas", "summary_mira", "Regressions", "Decision: **BLOCKED**", "1.000000 before", HONESTY):
        assert required in report
    html_report = render_report(project, "html")
    assert "<table>" in html_report
    assert "HONESTY BOUNDARY" in html_report


def cli(*args: str) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env["PYTHONPATH"] = str(ROOT) + os.pathsep + env.get("PYTHONPATH", "")
    return subprocess.run(
        [sys.executable, "-m", "promptci", *args],
        cwd=ROOT,
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )


def test_cli_smoke_all_subcommands_and_failure_exit(tmp_path: Path) -> None:
    project = tmp_path / "cli-demo"
    result = cli("fixtures", "--out", str(project))
    assert result.returncode == 0, result.stderr
    assert HONESTY in result.stdout
    for command in (
        ("init", "--dir", str(project)),
        ("run", "--dir", str(project)),
        ("diff", "--dir", str(project)),
        ("gate", "--dir", str(project)),
    ):
        result = cli(*command)
        assert result.returncode == 0, result.stderr
    result = cli("report", "--dir", str(project), "--format", "md")
    assert result.returncode == 0, result.stderr
    assert (project / "promptci-report.md").exists()
    result = cli("change", "--dir", str(project), "--scenario", "regression")
    assert result.returncode == 0, result.stderr
    assert cli("run", "--dir", str(project)).returncode == 0
    assert cli("diff", "--dir", str(project)).returncode == 0
    result = cli("gate", "--dir", str(project))
    assert result.returncode == 1
    assert "BLOCKED: regression threshold exceeded" in result.stdout
    result = cli("report", "--dir", str(project), "--format", "html", "--out", "reports/final.html")
    assert result.returncode == 0, result.stderr
    assert (project / "reports" / "final.html").exists()
    help_result = cli("--help")
    assert help_result.returncode == 0
    assert "no real model API calls" in help_result.stdout


@pytest.mark.parametrize("scenario,expected_suite", [("regression", "summary_quality"), ("cost", "classifier_cost")])
def test_fixture_scenarios_change_only_linked_suite(tmp_path: Path, scenario: str, expected_suite: str) -> None:
    project = tmp_path / scenario
    create_fixture(project, scenario=scenario)
    run = run_project(project)
    assert run["executed_suites"] == [expected_suite]
