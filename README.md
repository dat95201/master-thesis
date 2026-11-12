# tierbot

A tiered repair bot for Java, and the harness that evaluates it.

The bot repairs a bug that has a failing test. It asks a cheap model first and
steps up to a strong model only when the tests show that the session has
stalled. Four modules do the work:

| Module | What it does | Code |
|---|---|---|
| M0 Warm Substrate | compiles the patched file in process and runs the tests in a JVM that stays alive, so the build and the start-up are paid once per session | `tierbot/substrate.py`, `tools/WarmRunner.java` |
| M1 Evidence Memory | keeps every verdict, failing test and failure signature; answers repeats without a test run; spans both models | `tierbot/memory.py` |
| M2 Escalation Controller | the rule Step: move up a tier after a verified stall, and write the handover note | `tierbot/controller.py` |
| M3 Budget Guard | stops a session that is stuck or out of budget and says why | `tierbot/budget.py` |

The proposal is in `report/` (`report/main.pdf`). The design notes for the
code are in `docs/DESIGN.md`.

## Setup

Needs macOS or Linux with `git`, `svn`, `perl`, `cpanm`, Python 3.10+ and a
**Java 11 JDK** (Defects4J 3 requires Java 11; the bot uses the JDK's compiler).

```bash
bash setup.sh                         # Python environment + Defects4J 3.0.1 (pinned commit)
cp .env.example .env                  # then put the API key in .env
.venv/bin/python -m tierbot env       # checks Java, Defects4J, runners, models
.venv/bin/python -m tierbot smoke     # every stage on one bug with mock models, no API
```

`smoke` writes only to `smoke/`. It takes a minute or two.

## The bugs

`data/` holds the 120 selected Defects4J bugs and nothing else:

- `data/selection.csv`: every bug that was checked, and why it was kept or dropped.
- `data/split.json`: 20 development bugs (`dev`) and 100 evaluation bugs
  (`eval`), the latter halved into `e3` and `e4`.
- `data/bugs/<bug>/info.json`: the facts recorded for a bug: the faulty
  methods, the triggering tests, the classes that reach the faulty methods,
  the time of every test class on the fixed program, flaky classes.

The checkouts under `work/` are scratch. `prepare` rebuilds them:

```bash
.venv/bin/python -m tierbot prepare --bugs Cli-12 Lang-59     # or any of the 120
```

`select` is how the set was drawn: bugs in a seeded random order, spread over
the projects, each checked against the rules of the proposal (Section 5.1)
until 120 pass. It resumes from `data/selection.csv`; with that log in place it
checks nothing again and only rewrites `data/split.json` (same seed, same
split). Without the log it checks out and runs a few hundred bugs.

## Running the bot on one bug

```bash
.venv/bin/python -m tierbot repair --bug Cli-12 --live
.venv/bin/python -m tierbot repair --bug Cli-12 --live --policy strong-only
.venv/bin/python -m tierbot repair --bug Cli-12 --live --handover evidence
.venv/bin/python -m tierbot repair --bug Cli-12 --mock          # scripted models, no API
```

`--live` validates the way a deployed bot does: scheduled test classes on the
warm path, stopping at the first failure. Without it every candidate is
audited on both paths, as in the experiments. Policies: `step` (default),
`cheap-only`, `strong-only`, `fixed-K`, `step-strict`, `equal-K-R`. Handovers:
`cold` (default), `evidence`, `evidence+patches`, `transcript`.

## The experiments

| | Question | Command | Model calls |
|---|---|---|---|
| E0 | does the warm path give the same verdicts as build-and-restart, and what does each cost? | part of `e1` (every candidate is audited on both paths) | none |
| E1 | where does the cost go, for each model alone? | `e1` | yes, recorded once |
| E2 | which escalation policy is cheapest per repaired bug? | `e2` | none: replay |
| E3 | what is the handover note worth to the strong model? | `e3` | strong tier only |
| E4 | does the whole bot lower the cost per repaired bug? | `e4` | yes |

The order of work:

```bash
# 1. development set: fix the provisional settings
.venv/bin/python -m tierbot e1 --split dev --jobs 4
.venv/bin/python -m tierbot e2 --split dev
.venv/bin/python -m tierbot analyze --split dev      # prints the checks that fix each setting
#    variants, development set only (results/variants/<name>/); each takes
#    e1, e2 and analyze with the same variant options:
.venv/bin/python -m tierbot e1 --split dev --variant r15 --strong-budget 15    # is R2 = 10 enough?
.venv/bin/python -m tierbot e1 --split dev --variant n10 --cheap-n 10          # also 1 and 3
.venv/bin/python -m tierbot e1 --split dev --variant conv --cheap-mode conversation
.venv/bin/python -m tierbot analyze --split dev --variant r15 --strong-budget 15

# 2. registration: edit settings.json as the checks say, then
.venv/bin/python -m tierbot freeze

# 3. evaluation set
.venv/bin/python -m tierbot e1 --jobs 4
.venv/bin/python -m tierbot e2
.venv/bin/python -m tierbot e3 --jobs 4
.venv/bin/python -m tierbot e4 --jobs 4
.venv/bin/python -m tierbot analyze                  # -> results/report-eval.md
```

Every command is resumable: model answers and audits are cached, and a session
that is already recorded is skipped. `e2` never calls a model; it runs the bot
again on the recorded answers and fails loudly if a policy asks for an answer
that was not recorded.

Add `--mock` to any experiment to run it with the scripted models, or
`--model cheap=<name>` / `--model strong=<name>` to try another model through
the same OpenAI-compatible endpoint.

## Layout

```
tierbot/            the bot and the harness
  substrate.py        M0: test schedule, warm runtime, cold path
  memory.py           M1: Evidence Memory
  controller.py       M2: policies, Step, handover notes
  budget.py           M3: Budget Guard, give-up report, cost of a call
  bot.py              one session of the bot; audited and live validators
  session.py          the loop that drives the models
  llm.py              model access, answer cache, mock models
  prompts.py          prompts and feedback
  audit.py            audits on both paths, and what a candidate is charged
  experiments.py      E1 to E4
  analysis.py         metrics, statistics, report
  bugs.py, d4j.py     bug selection and preparation, Defects4J
  javasrc.py          Java source handling
  config.py, cli.py
tools/              AuditRunner.java (cold path), WarmRunner.java (warm path)
tests/              unit tests: python -m unittest discover -s tests
data/               the 120 bugs
settings.json       every setting of the evaluation, with the rule that fixes it
report/             the proposal (LaTeX)
docs/DESIGN.md      design notes
```

Created when the experiments run, and not under version control except
`results/`: `benchmarks/` (Defects4J), `work/` (checkouts, compiled runners),
`cache/` (model answers, audits), `results/` (sessions, reports), `smoke/`.

## Tests

```bash
.venv/bin/python -m unittest discover -s tests       # no Java, no network
.venv/bin/python -m tierbot smoke                    # Java and one checkout
```
