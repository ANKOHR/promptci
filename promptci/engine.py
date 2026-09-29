"""Deterministic fixture project, runner, diff, gate, and report operations."""

from __future__ import annotations

import hashlib
import html
import json
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path
from typing import Any


HONESTY = (
    "HONESTY BOUNDARY: PromptCI uses deterministic fixture providers with declared "
    "cost and latency profiles and synthetic eval cases. It makes no real model API "
    "calls, reads no live provider data, and reports no real API costs."
)

BASE_PROMPTS: dict[str, dict[str, str]] = {
    "summary": {
        "version": "1.0",
        "text": "Summarize the subject and preserve its key detail. [fixture:summary:complete]",
    },
    "classifier": {
        "version": "1.0",
        "text": "Return only the category label. [fixture:classify:compact]",
    },
}

REGRESSION_PROMPT = {
    "version": "2.0",
    "text": "Summarize the subject and omit its key detail. [fixture:summary:omit-detail]",
}
COST_PROMPT = {
    "version": "2.0",
    "text": "Return the category label with a short explanation. [fixture:classify:verbose]",
}

PROVIDERS: dict[str, Any] = {
    "summary_fixture": {
        "script": "summary_join",
        "cost_per_token_usd": "0.000002",
        "latency_profile_ms": {"base": 40, "per_token": 2},
        "behavior": {
            "omit_detail_marker": "[fixture:summary:omit-detail]",
            "complete_marker": "[fixture:summary:complete]",
        },
    },
    "classifier_fixture": {
        "script": "label_echo",
        "cost_per_token_usd": "0.000001",
        "latency_profile_ms": {"base": 25, "per_token": 1},
        "behavior": {
            "verbose_marker": "[fixture:classify:verbose]",
            "verbose_suffix": "This synthetic category follows the declared fixture routing rule.",
        },
    },
}

SUITES: dict[str, Any] = {
    "summary_quality": {
        "prompt": "summary",
        "provider": "summary_fixture",
        "cases": [
            {
                "id": "summary_mira",
                "input": {"subject": "Mira Chen won the debate", "key_detail": "by a unanimous vote"},
                "expected_output": "Mira Chen won the debate by a unanimous vote",
            },
            {
                "id": "summary_orbit",
                "input": {"subject": "The team reached orbit", "key_detail": "after a six-hour flight"},
                "expected_output": "The team reached orbit after a six-hour flight",
            },
        ],
    },
    "classifier_cost": {
        "prompt": "classifier",
        "provider": "classifier_fixture",
        "cases": [
            {"id": "classify_login", "input": {"text": "I cannot sign in to my account", "label": "support"}, "expected_output": "support"},
            {"id": "classify_invoice", "input": {"text": "The latest invoice has the wrong amount", "label": "finance"}, "expected_output": "finance"},
        ],
    },
}


def fixture_variants() -> dict[str, Any]:
    return {
        "synthetic": True,
        "scenarios": {
            "baseline": {"summary": BASE_PROMPTS["summary"], "classifier": BASE_PROMPTS["classifier"]},
            "regression": {"summary": REGRESSION_PROMPT},
            "cost": {"classifier": COST_PROMPT},
        },
    }


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    data = json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    with path.open("w", encoding="utf-8", newline="\n") as stream:
        stream.write(data)


def read_json(path: Path) -> Any:
    with path.open("r", encoding="utf-8") as stream:
        return json.load(stream)


def project_file(project: Path, relative: str) -> Path:
    return project / relative


def state_dir(project: Path) -> Path:
    config = read_json(project / "promptci.json")
    return project / config.get("state_dir", ".promptci")


def load_project(project: Path) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any], dict[str, Any]]:
    config = read_json(project / "promptci.json")
    prompts = read_json(project_file(project, config.get("prompts_file", "prompts.json")))
    providers = read_json(project_file(project, config.get("providers_file", "providers.json")))
    suites = read_json(project_file(project, config.get("suites_file", "suites.json")))
    for suite_name, suite in suites.items():
        if suite["prompt"] not in prompts:
            raise ValueError(f"Suite {suite_name} references missing prompt {suite['prompt']}")
        if suite["provider"] not in providers:
            raise ValueError(f"Suite {suite_name} references missing provider {suite['provider']}")
    return config, prompts, providers, suites


def prompt_hashes(prompts: dict[str, Any]) -> dict[str, str]:
    hashes: dict[str, str] = {}
    for name, prompt in sorted(prompts.items()):
        canonical = json.dumps(prompt, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        hashes[name] = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    return hashes


def prompt_versions(prompts: dict[str, Any]) -> dict[str, str]:
    return {name: prompt.get("version", "unspecified") for name, prompt in sorted(prompts.items())}


def _word_tokens(text: str) -> list[str]:
    return [token.casefold() for token in text.split()]


def _score(expected: str, actual: str) -> float:
    """Score is expected-token recall: matching expected whitespace tokens / expected tokens."""
    wanted = _word_tokens(expected)
    found = _word_tokens(actual)
    if not wanted:
        return 1.0 if not found else 0.0
    remaining = list(found)
    matched = 0
    for token in wanted:
        if token in remaining:
            remaining.remove(token)
            matched += 1
    return round(matched / len(wanted), 6)


def _provider_output(script: str, prompt_text: str, data: dict[str, Any], behavior: dict[str, Any]) -> str:
    if script == "summary_join":
        subject = str(data.get("subject", ""))
        if behavior["omit_detail_marker"] in prompt_text:
            return subject
        detail = str(data.get("key_detail", ""))
        return " ".join(part for part in (subject, detail) if part).strip()
    if script == "label_echo":
        output = str(data.get("label", ""))
        if behavior["verbose_marker"] in prompt_text:
            output = " ".join(part for part in (output, behavior["verbose_suffix"]) if part)
        return output
    raise ValueError(f"Unknown fixture provider script: {script}")


def _case_result(case: dict[str, Any], prompt: dict[str, Any], provider: dict[str, Any]) -> dict[str, Any]:
    output = _provider_output(provider["script"], prompt["text"], case["input"], provider["behavior"])
    input_text = " ".join(str(value) for value in case["input"].values())
    input_tokens = len(input_text.split())
    output_tokens = len(output.split())
    total_tokens = input_tokens + output_tokens
    cost = Decimal(str(provider["cost_per_token_usd"])) * total_tokens
    latency_profile = provider["latency_profile_ms"]
    latency = int(latency_profile["base"] + latency_profile["per_token"] * total_tokens)
    return {
        "case_id": case["id"],
        "output": output,
        "expected_output": case["expected_output"],
        "score": _score(case["expected_output"], output),
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "total_tokens": total_tokens,
        "cost_usd": _decimal_string(cost),
        "latency_ms": latency,
    }


def _decimal_string(value: Decimal) -> str:
    return format(value.quantize(Decimal("0.0000000001"), rounding=ROUND_HALF_UP), "f")


def evaluate_suites(
    prompts: dict[str, Any], providers: dict[str, Any], suites: dict[str, Any], suite_names: list[str]
) -> dict[str, list[dict[str, Any]]]:
    result: dict[str, list[dict[str, Any]]] = {}
    for suite_name in sorted(suite_names):
        suite = suites[suite_name]
        provider = providers[suite["provider"]]
        prompt = prompts[suite["prompt"]]
        result[suite_name] = [_case_result(case, prompt, provider) for case in suite["cases"]]
    return result


def initialize_fixture_state(project: Path) -> None:
    baseline_results = evaluate_suites(BASE_PROMPTS, PROVIDERS, SUITES, sorted(SUITES))
    hashes = prompt_hashes(BASE_PROMPTS)
    versions = prompt_versions(BASE_PROMPTS)
    state = state_dir(project)
    write_json(state / "baseline.json", {
        "schema_version": 1,
        "prompt_hashes": hashes,
        "prompt_versions": versions,
        "results": baseline_results,
    })
    write_json(state / "release.json", {
        "schema_version": 1,
        "release_id": "fixture-baseline",
        "prompt_hashes": hashes,
        "prompt_versions": versions,
    })


def create_fixture(project: Path, scenario: str = "baseline") -> None:
    project.mkdir(parents=True, exist_ok=True)
    config = {
        "project_name": "Synthetic PromptCI Demo",
        "synthetic_fixture_project": True,
        "prompts_file": "prompts.json",
        "providers_file": "providers.json",
        "suites_file": "suites.json",
        "variants_file": "fixture_variants.json",
        "state_dir": ".promptci",
        "gate": {
            "max_regressions": 0,
            "max_cost_increase_pct": 10,
            "max_latency_increase_pct": 20,
        },
    }
    variants = fixture_variants()
    prompts = json.loads(json.dumps(BASE_PROMPTS))
    if scenario == "regression":
        prompts["summary"] = REGRESSION_PROMPT
    elif scenario == "cost":
        prompts["classifier"] = COST_PROMPT
    elif scenario != "baseline":
        raise ValueError(f"Unknown fixture scenario: {scenario}")
    write_json(project / "promptci.json", config)
    write_json(project / "prompts.json", prompts)
    write_json(project / "providers.json", PROVIDERS)
    write_json(project / "suites.json", SUITES)
    write_json(project / "fixture_variants.json", variants)
    initialize_fixture_state(project)


def apply_fixture_change(project: Path, scenario: str) -> list[str]:
    config, prompts, _, _ = load_project(project)
    variants_path = project_file(project, config.get("variants_file", "fixture_variants.json"))
    variants = read_json(variants_path)
    if scenario not in variants.get("scenarios", {}):
        raise ValueError(f"Unknown fixture scenario: {scenario}")
    for name, prompt in variants["scenarios"][scenario].items():
        prompts[name] = prompt
    write_json(project_file(project, config.get("prompts_file", "prompts.json")), prompts)
    return sorted(variants["scenarios"][scenario])


def initialize_project(project: Path) -> None:
    _, prompts, providers, suites = load_project(project)
    hashes = prompt_hashes(prompts)
    versions = prompt_versions(prompts)
    results = evaluate_suites(prompts, providers, suites, sorted(suites))
    state = state_dir(project)
    write_json(state / "baseline.json", {
        "schema_version": 1,
        "prompt_hashes": hashes,
        "prompt_versions": versions,
        "results": results,
    })
    write_json(state / "release.json", {
        "schema_version": 1,
        "release_id": f"release-{hashlib.sha256(json.dumps(hashes, sort_keys=True).encode()).hexdigest()[:8]}",
        "prompt_hashes": hashes,
        "prompt_versions": versions,
    })


def run_project(project: Path) -> dict[str, Any]:
    config, prompts, providers, suites = load_project(project)
    state = state_dir(project)
    release_path = state / "release.json"
    baseline_path = state / "baseline.json"
    if not release_path.exists() or not baseline_path.exists():
        raise ValueError("Project has no release baseline. Run `promptci init` first.")
    release = read_json(release_path)
    baseline = read_json(baseline_path)
    current_hashes = prompt_hashes(prompts)
    changed = sorted(name for name, digest in current_hashes.items() if release["prompt_hashes"].get(name) != digest)
    affected = sorted(name for name, suite in suites.items() if suite["prompt"] in changed)
    candidate = json.loads(json.dumps(baseline["results"]))
    executed_results = evaluate_suites(prompts, providers, suites, affected)
    candidate.update(executed_results)
    if not changed:
        status = "skipped"
        reason = "SKIPPED: no prompt changes since the last release."
    elif not affected:
        status = "skipped"
        reason = "SKIPPED: changed prompts have no linked eval suites."
    else:
        status = "executed"
        reason = f"RUN: {len(changed)} changed prompt(s), {len(affected)} affected suite(s)."
    run_hash = hashlib.sha256(json.dumps({"hashes": current_hashes, "affected": affected}, sort_keys=True).encode()).hexdigest()
    run = {
        "schema_version": 1,
        "run_id": run_hash[:12],
        "status": status,
        "reason": reason,
        "changed_prompts": changed,
        "prompt_versions": prompt_versions(prompts),
        "affected_suites": affected,
        "executed_suites": sorted(executed_results),
        "executed_results": executed_results,
        "baseline_release_id": release.get("release_id"),
        "baseline_prompt_versions": baseline.get("prompt_versions", {}),
        "baseline_results": baseline["results"],
        "candidate_results": candidate,
    }
    write_json(state / "latest-run.json", run)
    return run


def _all_cases(results: dict[str, list[dict[str, Any]]]) -> list[dict[str, Any]]:
    return [case for suite_name in sorted(results) for case in results[suite_name]]


def _sum_cost(results: dict[str, list[dict[str, Any]]]) -> Decimal:
    return sum((Decimal(case["cost_usd"]) for case in _all_cases(results)), Decimal("0"))


def _avg_latency(results: dict[str, list[dict[str, Any]]]) -> Decimal:
    cases = _all_cases(results)
    if not cases:
        return Decimal("0")
    return Decimal(sum(case["latency_ms"] for case in cases)) / Decimal(len(cases))


def _percent_delta(before: Decimal, after: Decimal) -> Decimal:
    if before == 0:
        return Decimal("0") if after == 0 else Decimal("Infinity")
    return ((after - before) / before) * Decimal("100")


def _percent_string(value: Decimal) -> str:
    return "infinity" if value.is_infinite() else f"{value.quantize(Decimal('0.01'), rounding=ROUND_HALF_UP):f}"


def diff_project(project: Path) -> dict[str, Any]:
    config, prompts, _, suites = load_project(project)
    state = state_dir(project)
    run_path = state / "latest-run.json"
    if not run_path.exists():
        raise ValueError("No run result found. Run `promptci run` first.")
    run = read_json(run_path)
    baseline = read_json(state / "baseline.json")
    before_results = run.get("baseline_results", baseline["results"])
    after_results = run["candidate_results"]
    cases: list[dict[str, Any]] = []
    regressions: list[dict[str, Any]] = []
    for suite_name in run["affected_suites"]:
        before_by_id = {case["case_id"]: case for case in before_results[suite_name]}
        after_by_id = {case["case_id"]: case for case in after_results[suite_name]}
        for case_id in sorted(before_by_id):
            before, after = before_by_id[case_id], after_by_id[case_id]
            change = {
                "suite": suite_name,
                "case_id": case_id,
                "prompt": suites[suite_name]["prompt"],
                "score_before": before["score"],
                "score_after": after["score"],
                "score_delta": round(after["score"] - before["score"], 6),
                "cost_before_usd": before["cost_usd"],
                "cost_after_usd": after["cost_usd"],
                "cost_delta_usd": _decimal_string(Decimal(after["cost_usd"]) - Decimal(before["cost_usd"])),
                "latency_before_ms": before["latency_ms"],
                "latency_after_ms": after["latency_ms"],
                "latency_delta_ms": after["latency_ms"] - before["latency_ms"],
            }
            cases.append(change)
            if change["score_after"] < change["score_before"]:
                regressions.append(change)
    baseline_cost = _sum_cost(before_results)
    candidate_cost = _sum_cost(after_results)
    baseline_latency = _avg_latency(before_results)
    candidate_latency = _avg_latency(after_results)
    cost_delta = candidate_cost - baseline_cost
    latency_delta = candidate_latency - baseline_latency
    return {
        "schema_version": 1,
        "run_id": run["run_id"],
        "status": run["status"],
        "reason": run["reason"],
        "changed_prompts": run["changed_prompts"],
        "changed_prompt_versions": {name: prompts[name].get("version", "unspecified") for name in run["changed_prompts"]},
        "affected_suites": run["affected_suites"],
        "cases": cases,
        "regressions": regressions,
        "metrics": {
            "baseline_cost_usd": _decimal_string(baseline_cost),
            "candidate_cost_usd": _decimal_string(candidate_cost),
            "cost_delta_usd": _decimal_string(cost_delta),
            "cost_delta_pct": _percent_string(_percent_delta(baseline_cost, candidate_cost)),
            "baseline_avg_latency_ms": f"{baseline_latency.quantize(Decimal('0.01'), rounding=ROUND_HALF_UP):f}",
            "candidate_avg_latency_ms": f"{candidate_latency.quantize(Decimal('0.01'), rounding=ROUND_HALF_UP):f}",
            "latency_delta_ms": f"{latency_delta.quantize(Decimal('0.01'), rounding=ROUND_HALF_UP):f}",
            "latency_delta_pct": _percent_string(_percent_delta(baseline_latency, candidate_latency)),
        },
    }


def gate_project(project: Path) -> dict[str, Any]:
    config, prompts, _, _ = load_project(project)
    state = state_dir(project)
    diff = diff_project(project)
    run = read_json(state / "latest-run.json")
    if run["status"] == "skipped":
        gate = {"run_id": run["run_id"], "decision": "SKIPPED", "reason": "SKIPPED: no changed prompt suites to gate.", "rules": []}
        write_json(state / "latest-gate.json", gate)
        return gate
    rules = config.get("gate", {})
    max_regressions = int(rules.get("max_regressions", 0))
    max_cost_pct = Decimal(str(rules.get("max_cost_increase_pct", "Infinity")))
    max_latency_pct = Decimal(str(rules.get("max_latency_increase_pct", "Infinity")))
    regressions = len(diff["regressions"])
    cost_pct = Decimal(diff["metrics"]["cost_delta_pct"]) if diff["metrics"]["cost_delta_pct"] != "infinity" else Decimal("Infinity")
    latency_pct = Decimal(diff["metrics"]["latency_delta_pct"]) if diff["metrics"]["latency_delta_pct"] != "infinity" else Decimal("Infinity")
    failures: list[str] = []
    if regressions > max_regressions:
        failures.append(f"BLOCKED: regression threshold exceeded ({regressions} > {max_regressions}).")
    if cost_pct > max_cost_pct:
        failures.append(f"BLOCKED: cost increase exceeds budget ({_percent_string(cost_pct)}% > {_percent_string(max_cost_pct)}%).")
    if latency_pct > max_latency_pct:
        failures.append(f"BLOCKED: latency increase exceeds budget ({_percent_string(latency_pct)}% > {_percent_string(max_latency_pct)}%).")
    if failures:
        gate = {"run_id": run["run_id"], "decision": "BLOCKED", "reason": failures[0], "all_reasons": failures, "rules": rules}
    else:
        gate = {"run_id": run["run_id"], "decision": "PASS", "reason": "PASS: regression, cost, and latency rules are within configured thresholds.", "all_reasons": [], "rules": rules}
        hashes = prompt_hashes(prompts)
        versions = prompt_versions(prompts)
        candidate = run["candidate_results"]
        write_json(state / "baseline.json", {
            "schema_version": 1,
            "prompt_hashes": hashes,
            "prompt_versions": versions,
            "results": candidate,
        })
        release_id = f"release-{hashlib.sha256(json.dumps(hashes, sort_keys=True).encode()).hexdigest()[:8]}"
        write_json(state / "release.json", {
            "schema_version": 1,
            "release_id": release_id,
            "prompt_hashes": hashes,
            "prompt_versions": versions,
        })
        gate["release_id"] = release_id
        gate["baseline_updated"] = True
    write_json(state / "latest-gate.json", gate)
    return gate


def _gate_for_report(project: Path) -> dict[str, Any]:
    path = state_dir(project) / "latest-gate.json"
    if path.exists():
        gate = read_json(path)
        run_path = state_dir(project) / "latest-run.json"
        if run_path.exists() and gate.get("run_id") == read_json(run_path).get("run_id"):
            return gate
    return {"decision": "NOT RUN", "reason": "NOT RUN: use `promptci gate` to evaluate release rules."}


def render_report(project: Path, format_name: str) -> str:
    diff = diff_project(project)
    gate = _gate_for_report(project)
    changes = ", ".join(diff["changed_prompts"]) if diff["changed_prompts"] else "none"
    suites = ", ".join(diff["affected_suites"]) if diff["affected_suites"] else "none"
    metrics = diff["metrics"]
    regressions = diff["regressions"]
    if format_name == "md":
        lines = [
            "# PromptCI change report",
            "",
            HONESTY,
            "",
            "## Change summary",
            "",
            f"- Changed prompts: {changes}.",
            f"- Affected suites: {suites}.",
            f"- Run status: {diff['status']} ({diff['reason']})",
            "",
            "## Per-case deltas",
            "",
            "| Suite | Case | Prompt | Score before | Score after | Delta | Cost delta (USD) | Latency delta (ms) |",
            "| --- | --- | --- | ---: | ---: | ---: | ---: | ---: |",
        ]
        if diff["cases"]:
            for case in diff["cases"]:
                lines.append(f"| {case['suite']} | {case['case_id']} | {case['prompt']} | {case['score_before']:.6f} | {case['score_after']:.6f} | {case['score_delta']:+.6f} | {case['cost_delta_usd']} | {case['latency_delta_ms']:+d} |")
        else:
            lines.append("| none | none | none | 0.000000 | 0.000000 | +0.000000 | 0.0000000000 | +0 |")
        lines += [
            "",
            "## Aggregate cost and latency",
            "",
            f"- Cost: {metrics['baseline_cost_usd']} USD to {metrics['candidate_cost_usd']} USD, delta {metrics['cost_delta_usd']} USD ({metrics['cost_delta_pct']}%).",
            f"- Average latency: {metrics['baseline_avg_latency_ms']} ms to {metrics['candidate_avg_latency_ms']} ms, delta {metrics['latency_delta_ms']} ms ({metrics['latency_delta_pct']}%).",
            "",
            "## Regressions",
            "",
        ]
        if regressions:
            for case in regressions:
                lines.append(f"- Prompt `{case['prompt']}` caused a score regression in `{case['suite']}/{case['case_id']}`: {case['score_before']:.6f} before, {case['score_after']:.6f} after.")
        else:
            lines.append("- None.")
        lines += ["", "## Release gate", "", f"- Decision: **{gate['decision']}**", f"- Reason: {gate['reason']}", ""]
        return "\n".join(lines)
    if format_name == "html":
        row_html = "".join(
            "<tr>" + "".join(f"<td>{html.escape(str(value))}</td>" for value in (
                case["suite"], case["case_id"], case["prompt"], f"{case['score_before']:.6f}",
                f"{case['score_after']:.6f}", f"{case['score_delta']:+.6f}", case["cost_delta_usd"],
                f"{case['latency_delta_ms']:+d}",
            )) + "</tr>"
            for case in diff["cases"]
        ) or '<tr><td colspan="8">No changed cases</td></tr>'
        regression_html = "".join(
            f"<li>Prompt {html.escape(case['prompt'])} regressed {html.escape(case['suite'])}/{html.escape(case['case_id'])}: {case['score_before']:.6f} before, {case['score_after']:.6f} after.</li>"
            for case in regressions
        ) or "<li>None.</li>"
        return "\n".join([
            "<!doctype html>", '<html lang="en"><head><meta charset="utf-8"><title>PromptCI change report</title></head><body>',
            "<h1>PromptCI change report</h1>", f"<p><strong>{html.escape(HONESTY)}</strong></p>",
            "<h2>Change summary</h2>", f"<p>Changed prompts: {html.escape(changes)}. Affected suites: {html.escape(suites)}. Run status: {html.escape(diff['status'])} ({html.escape(diff['reason'])})</p>",
            "<h2>Per-case deltas</h2>", '<table><thead><tr><th>Suite</th><th>Case</th><th>Prompt</th><th>Score before</th><th>Score after</th><th>Delta</th><th>Cost delta USD</th><th>Latency delta ms</th></tr></thead>', f"<tbody>{row_html}</tbody></table>",
            "<h2>Aggregate cost and latency</h2>", f"<p>Cost: {metrics['baseline_cost_usd']} USD to {metrics['candidate_cost_usd']} USD, delta {metrics['cost_delta_usd']} USD ({metrics['cost_delta_pct']}%).</p>",
            f"<p>Average latency: {metrics['baseline_avg_latency_ms']} ms to {metrics['candidate_avg_latency_ms']} ms, delta {metrics['latency_delta_ms']} ms ({metrics['latency_delta_pct']}%).</p>",
            f"<h2>Regressions</h2><ul>{regression_html}</ul>", f"<h2>Release gate</h2><p>Decision: <strong>{html.escape(gate['decision'])}</strong><br>Reason: {html.escape(gate['reason'])}</p>",
            "</body></html>",
        ])
    raise ValueError(f"Unsupported report format: {format_name}")
