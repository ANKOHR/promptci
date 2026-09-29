# PromptCI

PromptCI treats a prompt like code. Change it, and the pipeline tells you what broke, what got slower, and what got more expensive before it ships.

It detects prompt changes against a release record, runs only linked eval suites, compares deterministic case results with a stored baseline, and applies named release rules. The included fixture project is entirely synthetic.

## Quickstart

Python 3.10 or newer is required. Install PromptCI and the test extra with:

```text
python -m pip install -e ".[test]"
```

Create a clean fixture project and see the no-change skip:

```text
promptci fixtures --out demo
promptci run --dir demo
```

Try the planted summary regression:

```text
promptci change --dir demo --scenario regression
promptci run --dir demo
promptci diff --dir demo
promptci gate --dir demo
promptci report --dir demo --format md
```

The regression gate exits with status 1 and names the blocking rule. The report is written to `demo/promptci-report.md`. Try the cost change in a fresh project with `promptci fixtures --out cost-demo --scenario cost`; its gate blocks on the configured cost budget. Passing gates update `.promptci/release.json` and `.promptci/baseline.json`. To use an existing project instead, define `promptci.json`, `prompts.json`, `providers.json`, and `suites.json`, then run `promptci init` to seed its first release baseline.

## HONESTY BOUNDARY

Evaluation runs against deterministic fixture providers with declared cost and latency profiles, not real model APIs. There are no real model calls, no live provider data, and no real costs. Eval cases and provider outputs are synthetic. Token counts, scores, costs, and latency are computed locally from the fixture definitions. PromptCI is a prompt change management demonstration, not a production readiness claim.

Every CLI command and generated report repeats this boundary. No provider credentials or network access are used by the runner.

## Architecture

```text
prompt files + versions
          |
          v
hash against release record ---- no changes ----> named skip
          |
          v
prompt-to-suite map -> affected suites only -> scripted fixture providers
          |                                      |
          +----------------- baseline <----------+ per-case results
                             |
                             v
                 score, cost, latency diff
                             |
                             v
                 named release gate decision
                    |                 |
                   pass              block
                    |                 |
             write release and     keep prior
             new baseline          release state
```

The project files are human-readable JSON. `.promptci/latest-run.json` stores the candidate result, `latest-diff.json` stores case and aggregate comparisons, `latest-gate.json` stores the release decision, and `release.json` records the last accepted prompt hashes and versions. A failed gate never changes the accepted baseline.

## Fixture provider reference

`providers.json` declares each scripted provider, its cost per token, latency profile, and prompt markers. `summary_fixture` uses the local `summary_join` script. It joins the subject and detail unless the prompt includes `[fixture:summary:omit-detail]`. The planted regression therefore drops expected words and lowers expected-token recall.

| Provider | Script | Cost per token | Latency profile | Prompt behavior |
| --- | --- | ---: | --- | --- |
| `summary_fixture` | `summary_join` | `$0.000002` | 40 ms base + 2 ms per token | `[fixture:summary:omit-detail]` drops the detail; otherwise joins subject and detail. |
| `classifier_fixture` | `label_echo` | `$0.000001` | 25 ms base + 1 ms per token | `[fixture:classify:verbose]` adds a fixed synthetic suffix. |

`classifier_fixture` returns the input label. The verbose marker keeps the score unchanged while increasing output tokens, declared token cost, and computed latency.

These are short deterministic Python scripts, not model adapters. They run offline and do not call a provider service. Fixture variants are stored in `fixture_variants.json`; use `promptci change --scenario regression|cost|baseline` to apply one.

## Scoring and comparison formula

- The fixture tokenizer is whitespace splitting. Input tokens are counted from input values joined in their declared order, and output tokens from the scripted output.
- Per-case score is expected-token recall: matching expected whitespace tokens divided by the number of expected whitespace tokens. Matching is case-insensitive and duplicate tokens count separately. An empty expected output scores 1 only when output is also empty.
- Case cost is `(input tokens + output tokens) * provider cost_per_token_usd`.
- Case latency is `base milliseconds + per_token milliseconds * total tokens` from the provider's declared latency profile.
- Cost delta compares the sum across all suites in the candidate with the sum in the accepted baseline. Latency delta compares average per-case latency across all suites. Unaffected suites retain their baseline result in the candidate.
- The same project files produce byte-identical result JSON and reports. No random number generator or seed is used.

## Gate rule catalogue

Rules are configured in `promptci.json` under `gate` and checked in this order:

1. **Regression count:** block when the number of cases with a lower score exceeds `max_regressions`.
2. **Cost increase:** block when aggregate cost increase as a percentage of baseline cost exceeds `max_cost_increase_pct`.
3. **Latency increase:** block when aggregate average latency increase as a percentage of baseline average latency exceeds `max_latency_increase_pct`.
4. **Pass:** accept when no rule is exceeded. Write a new release record and replace the stored baseline with the candidate results.

The first exceeded rule is the named gate reason. A no-change run is skipped and does not create a release. Defaults in the fixture project are zero allowed regressions, a 10 percent cost increase, and a 20 percent latency increase. Gate thresholds are inclusive at the limit: a value equal to the configured threshold passes.

## Commands

```text
promptci fixtures --out DIR [--scenario baseline|regression|cost]
promptci init [--dir DIR]
promptci change --dir DIR --scenario baseline|regression|cost
promptci run [--dir DIR]
promptci diff [--dir DIR]
promptci gate [--dir DIR]
promptci report --dir DIR --format md|html [--out FILE]
```

`promptci run` is change triggered. It compares prompt content and version hashes with the last accepted release and evaluates only suites linked to changed prompt IDs. A no-change run prints `SKIPPED: no prompt changes since the last release.` The run still writes a deterministic candidate file so the diff and report commands can explain the skip.

## Development

```text
python -m pytest
```

The test suite checks deterministic fixture generation, trigger and affected-suite selection, planted regression attribution, hand-computed cost and latency deltas, gate behavior, report contents, and CLI flows. It uses only CPU and the Python standard library at runtime.
