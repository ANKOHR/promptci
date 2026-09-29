"""Command line interface for PromptCI."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Sequence

from .engine import (
    HONESTY,
    apply_fixture_change,
    create_fixture,
    diff_project,
    gate_project,
    initialize_project,
    render_report,
    run_project,
    state_dir,
    write_json,
)


def _project_arg(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--dir", type=Path, default=Path.cwd(), help="Project directory (default: current directory).")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="promptci",
        description="CI/CD for prompt changes. " + HONESTY,
        epilog=HONESTY,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    fixtures = subparsers.add_parser("fixtures", help="Create the synthetic fixture project.", description=HONESTY)
    fixtures.add_argument("--out", type=Path, required=True, help="Output project directory.")
    fixtures.add_argument("--scenario", choices=("baseline", "regression", "cost"), default="baseline", help="Optional planted prompt change to start with.")

    init = subparsers.add_parser("init", help="Create a release baseline from the current prompt set.", description=HONESTY)
    _project_arg(init)

    change = subparsers.add_parser("change", help="Apply one named synthetic prompt change to the fixture project.", description=HONESTY)
    _project_arg(change)
    change.add_argument("--scenario", choices=("baseline", "regression", "cost"), required=True)

    run = subparsers.add_parser("run", help="Detect prompt changes and evaluate only affected suites.", description=HONESTY)
    _project_arg(run)

    diff = subparsers.add_parser("diff", help="Compare the latest candidate with the stored baseline.", description=HONESTY)
    _project_arg(diff)

    gate = subparsers.add_parser("gate", help="Apply configured release rules and update baseline only on pass.", description=HONESTY)
    _project_arg(gate)

    report = subparsers.add_parser("report", help="Write a deterministic Markdown or HTML change report.", description=HONESTY)
    _project_arg(report)
    report.add_argument("--format", choices=("md", "html"), required=True)
    report.add_argument("--out", type=Path, help="Output file (default: promptci-report.<format> in the project).")
    return parser


def _print_diff(project: Path, diff: dict) -> None:
    print(f"Changed prompts: {', '.join(diff['changed_prompts']) if diff['changed_prompts'] else 'none'}")
    print(f"Affected suites: {', '.join(diff['affected_suites']) if diff['affected_suites'] else 'none'}")
    print(f"Run: {diff['status']} ({diff['reason']})")
    for case in diff["cases"]:
        print(
            f"CASE {case['suite']}/{case['case_id']} prompt={case['prompt']} "
            f"score {case['score_before']:.6f} -> {case['score_after']:.6f} "
            f"delta={case['score_delta']:+.6f} cost_delta_usd={case['cost_delta_usd']} "
            f"latency_delta_ms={case['latency_delta_ms']:+d}"
        )
    for case in diff["regressions"]:
        print(
            f"REGRESSION prompt={case['prompt']} case={case['suite']}/{case['case_id']} "
            f"score_before={case['score_before']:.6f} score_after={case['score_after']:.6f}"
        )
    metrics = diff["metrics"]
    print(f"COST {metrics['baseline_cost_usd']} -> {metrics['candidate_cost_usd']} USD; delta {metrics['cost_delta_usd']} USD ({metrics['cost_delta_pct']}%)")
    print(f"LATENCY average {metrics['baseline_avg_latency_ms']} -> {metrics['candidate_avg_latency_ms']} ms; delta {metrics['latency_delta_ms']} ms ({metrics['latency_delta_pct']}%)")


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        if args.command == "fixtures":
            create_fixture(args.out.resolve(), args.scenario)
            print(f"Created synthetic fixture project at {args.out.resolve()}")
            print(HONESTY)
            return 0
        project = args.dir.resolve()
        if args.command == "init":
            initialize_project(project)
            print(f"Initialized release baseline in {state_dir(project)}")
            return 0
        if args.command == "change":
            changed = apply_fixture_change(project, args.scenario)
            print(f"Applied synthetic {args.scenario} scenario to prompts: {', '.join(changed)}")
            return 0
        if args.command == "run":
            run = run_project(project)
            print(run["reason"])
            print(f"Changed prompts: {', '.join(run['changed_prompts']) if run['changed_prompts'] else 'none'}")
            print(f"Executed suites: {', '.join(run['executed_suites']) if run['executed_suites'] else 'none'}")
            print(HONESTY)
            return 0
        if args.command == "diff":
            diff = diff_project(project)
            write_json(state_dir(project) / "latest-diff.json", diff)
            _print_diff(project, diff)
            print(HONESTY)
            return 0
        if args.command == "gate":
            gate = gate_project(project)
            print(gate["reason"])
            if gate.get("baseline_updated"):
                print(f"Release record and baseline updated: {gate['release_id']}")
            print(HONESTY)
            return 1 if gate["decision"] == "BLOCKED" else 0
        if args.command == "report":
            report_text = render_report(project, args.format)
            destination = args.out if args.out is not None else project / f"promptci-report.{args.format}"
            if not destination.is_absolute():
                destination = project / destination
            destination.parent.mkdir(parents=True, exist_ok=True)
            with destination.open("w", encoding="utf-8", newline="\n") as stream:
                stream.write(report_text)
                if not report_text.endswith("\n"):
                    stream.write("\n")
            print(f"Wrote {args.format} report to {destination.resolve()}")
            print(HONESTY)
            return 0
    except (OSError, ValueError, KeyError, json.JSONDecodeError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
