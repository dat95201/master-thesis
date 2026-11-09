# vlayer: a cost-aware validation layer for LLM-based program repair

Code and experiments for the thesis proposal *Cutting the Cost of LLM-Based
Program Repair with a Cost-Aware Validation Layer* (CO5109, HCMUT). The
proposal itself is in [`report/`](report/) (`report/main.pdf`).

An LLM repair loop pays twice in every round: one model call to propose a
patch and one test run to check it. The validation layer sits between the
loop and the project's test runner and removes three wastes:

| Module | Waste it removes | File |
|---|---|---|
| **M1 Repeat Guard** | the same patch validated again | [`vlayer/repeat_guard.py`](vlayer/repeat_guard.py), normalisation in [`vlayer/javasrc.py`](vlayer/javasrc.py) |
| **M2 Test Prioritiser** (rule **ARC**) | passing tests run before the one that rejects a wrong patch | [`vlayer/orderings.py`](vlayer/orderings.py) |
| **M3 Budget Guard** | sessions that go on after the model is stuck | [`vlayer/budget_guard.py`](vlayer/budget_guard.py) |

The loop talks to the layer through one call ([`vlayer/layer.py`](vlayer/layer.py)):

```python
result = layer.validate(candidate, gen)
result.verdict    # plausible, rejected, compile_error, unreadable or repeat
result.feedback   # text to show the model in the next round
result.go_on      # false when the session should stop
result.report     # the give-up report, when it stops without a patch
```

How each part of the proposal maps to the code is in
[`docs/DESIGN.md`](docs/DESIGN.md).

---

## 1. Layout

```
thesis/
├── README.md             this file
├── docs/DESIGN.md        proposal -> code: mechanisms, experiments, outputs
├── settings.json         every pre-registered value (bugs, ARC, M3, model, prices)
├── setup.sh              one-time setup (Python env + Defects4J + Major)
├── requirements.txt
├── .env.example          API key / base URL of the model (copy to .env)
├── tools/AuditRunner.java  JUnit runner: per test class pass/fail, wall + CPU time, first failure
├── vlayer/               the Python package
│   ├── cli.py            `python -m vlayer <command>`
│   ├── config.py         paths, settings, Java/Defects4J environment
│   ├── d4j.py            Defects4J adapter: checkout, compile, run classes, Major mutants
│   ├── bugs.py           bug selection and per-bug preparation
│   ├── javasrc.py        find / replace / normalise Java methods, coverage probes, read answers
│   ├── audit.py          audit a patch once, charge any ordering from the audit
│   ├── repeat_guard.py   M1
│   ├── orderings.py      M2: Default, FRTP, Coverage (PIT rule), ARC, Oracle, ARC variants, trigger prefix
│   ├── budget_guard.py   M3 and the give-up report
│   ├── layer.py          the validation layer, LiveRunner and AuditedRunner
│   ├── llm.py            OpenAI-compatible client with a shared answer cache (+ a mock model)
│   ├── loop.py           the conversational repair loop (ChatRepair style)
│   ├── experiments.py    E1 (mutants + simulation), E2, E3
│   └── analysis.py       metrics, Wilcoxon + Holm, bootstrap, report
├── tests/test_layer.py   unit tests (no Defects4J or model needed)
├── report/               LaTeX source of the proposal
├── benchmarks/defects4j/ Defects4J 3.x + Major            (setup.sh, git-ignored)
├── data/                 selection log, split, per-bug facts, mutant lists   (created)
├── cache/                model answers and audits, shared by all configs  (created, git-ignored)
├── results/              E1/E2/E3 outputs and report-<split>.md            (created)
└── work/                 Defects4J checkouts (scratch, git-ignored)
```

---

## 2. Setup

Requirements: macOS or Linux, **Java 11** (Defects4J is validated on it),
git, svn, perl + cpanminus, Python 3.10+.

```bash
brew install git subversion perl cpanminus openjdk@11 python@3.11   # macOS
cd thesis
bash setup.sh                      # venv, Defects4J clone, Perl modules, ./init.sh (10-30 min)
cp .env.example .env               # put the API key here (not needed for the smoke test)
.venv/bin/python -m vlayer env     # checks Java 11, Defects4J, Major, the runner, the model
```

You do not need to edit `PATH`: `vlayer` finds Java 11 (Homebrew or
`JAVA_HOME`), sets `TZ=America/Los_Angeles` as Defects4J requires, and adds
`~/perl5` to `PERL5LIB` when it calls Defects4J.

The model is any OpenAI-compatible endpoint: set `OPENAI_API_KEY` (and
`OPENAI_BASE_URL` for OpenRouter, vLLM or Ollama) in `.env`, and the model
name in `settings.json` → `model.name`.

Run the unit tests:

```bash
.venv/bin/python -m unittest discover -s tests -v
```

---

## 3. Quick start: the smoke test (under a minute, no API key)

```bash
.venv/bin/python -m vlayer smoke            # default bug Cli-17 (fast suite)
```

It runs every stage on one bug with small numbers and the **mock model**
(rounds 1 and 2 return the faulty method, the second time reformatted, so it
is a repeat; round 3 returns the developer's fix). Outputs go to `smoke/`
(git-ignored), and the report is `smoke/results/report-smoke.md`.

To see the layer on one bug as a CI bot would use it, really running the
ordered test classes and stopping at the first failure:

```bash
.venv/bin/python -m vlayer repair --bug Cli-17 --live --model mock
.venv/bin/python -m vlayer repair --bug Lang-6 --live          # with the real model
```

---

## 4. Running the study

Every command takes `--split dev|eval` (bugs from `data/split.json`) or
`--bugs Lang-6 Cli-17 ...`, and `--jobs N` to process bugs in parallel.
Every stage is resumable: finished bugs and sessions are skipped, audits and
model answers are cached. Keep `--jobs` at most half the CPU cores, because
test times are the measurement.

### Step 0. Select the bugs (once)

```bash
.venv/bin/python -m vlayer select --jobs 4
```

Bugs are drawn at random, round robin over the 17 projects, and kept if they
pass every rule: the fix changes one file and only the bodies of existing
methods; the triggering classes fail on the faulty program and pass on the
fixed one; at least 10 test classes stay after removing classes that do not
pass 3 runs on the fixed program; the suite runs in under 5 minutes. It stops
at 120 bugs and writes `data/split.json` (20 dev, 100 eval, 50 of them for
E3). Every decision is logged in `data/selection.csv`; facts per bug are in
`data/bugs/<bug>/info.json`.

### Step 1. Development set: fix the settings

```bash
.venv/bin/python -m vlayer mutants --split dev
.venv/bin/python -m vlayer e1      --split dev
.venv/bin/python -m vlayer e2      --split dev
.venv/bin/python -m vlayer analyze --split dev     # -> results/report-dev.md
```

The *Development-set checks* section of the report gives what the proposal
fixes on the dev set: the median rounds per session (the model rule: at
least 3, otherwise pick a smaller model), the SD of the per-bug log-ratio
(power), and the audit time per bug (compute budget). Set the final values in
`settings.json` (`arc` weights, `budget_guard.r` and `budget`, `model.name`,
`analysis.margin_pp`), then freeze them:

```bash
.venv/bin/python -m vlayer freeze        # writes settings.lock.json (hash + time)
```

Eval runs warn if `settings.json` was not frozen or changed afterwards.

### Step 2. E1: orderings on mutants (RQ2, RQ4; no model)

```bash
.venv/bin/python -m vlayer mutants --split eval --jobs 4   # Major mutants S1-S4, audited
.venv/bin/python -m vlayer e1      --split eval            # simulation, seconds
```

### Step 3. E2: orderings, live (RQ1, RQ2)

```bash
.venv/bin/python -m vlayer e2 --split eval --jobs 4
```

100 bugs × 3 sessions × 4 orderings (Default, FRTP, Coverage, ARC), up to 20
rounds each. The Repeat Guard and the Budget Guard are evaluated on these
sessions by replay in the analysis.

### Step 4. E3: the whole layer, live (RQ3)

```bash
.venv/bin/python -m vlayer e3 --split eval --jobs 4        # runs on the 50 E3 bugs
```

ARC with the repeat notice on; the Budget Guard only logs when it would stop,
and its stop is replayed in the analysis.

### Step 5. Analysis

```bash
.venv/bin/python -m vlayer analyze --split eval            # -> results/report-eval.md
```

---

## 5. Outputs

| Path | Content |
|---|---|
| `data/selection.csv` | every bug tried, accepted or not, and why |
| `data/split.json` | dev / eval / e3 bug lists |
| `data/bugs/<bug>/info.json` | suite, flaky classes, CI times, triggering and covering classes, faulty methods |
| `data/bugs/<bug>/mutants.json` | chosen mutants per group S1-S4 and the developer's fix |
| `cache/audit/<bug>/<key>.json` | one audit: compile time, JVM start-up, every class's result and time |
| `cache/llm/**.json` | model answers with their latency and tokens |
| `results/e1/<bug>.json` | mean session cost per ordering, setting (trigger step or not) and session kind |
| `results/e2/<bug>/<arm>-s<k>.json`, `results/e3/...` | one session: every round with verdict, costs, signature, failure |
| `results/report-<split>.md` | all tables for RQ1-RQ4 |

## 6. Troubleshooting

| Symptom | Likely cause |
|---|---|
| `defects4j : FAILED ... String/Interpolate.pm` | Perl modules missing: `cd benchmarks/defects4j && cpanm --local-lib=~/perl5 --installdeps .` |
| many bugs rejected with "triggering class ... passes on the faulty program" | wrong Java version; `vlayer env` must show 11 |
| `test runner did not start` | read the printed Java error; usually a broken checkout: delete `work/<bug>` and rerun |
| a bug is reported as `dropped (... cap ... reached)` | its audits used the CPU cap of `settings.json` (`e1.cap_cpu_hours`, `live.cap_cpu_hours`) |
| model errors | check `.env` and `model.name`; `vlayer env` shows what is used |
