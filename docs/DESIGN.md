# Design notes

How the code realises the proposal (`report/main.pdf`), and the decisions that
are not obvious from reading it. Section and table numbers refer to the
proposal.

## 1. One session

```
session.run_session
  └─ bot.TieredBot                       one object per session
       ├─ controller.Controller   M2     which tier proposes next (Step, Algorithm 1)
       ├─ memory.EvidenceMemory   M1     verdicts, failing tests, signatures
       ├─ budget.BudgetGuard      M3     stuck / out of budget
       └─ a validator             M0     AuditedValidator (experiments) or LiveValidator
            └─ substrate.Substrate       warm runtime and cold path
```

`run_session` drives the models; the bot never calls a model itself. For each
model call it calls `bot.begin_call(gen)` and then `bot.validate(methods)` once
per candidate. `validate` returns what the model is told (`feedback`) and what
happens next (`same`, `step_up`, `stop`). This keeps the bot usable from any
agent loop, and it is what makes replay exact: a policy is replayed by running
the same code on recorded answers.

A candidate is a mapping `{method key: new method text}` for the faulty
methods. Its key is the hash of the normalised methods (`javasrc.normalise`:
layout and comments removed, local variables renamed in order of appearance),
so two answers that differ only in those are one candidate.

## 2. M0, the Warm Substrate

**Cold path** (`Substrate.cold`, `tools/AuditRunner.java`): write the patched
file into the checkout, build with Defects4J, start a new JVM, run the test
classes. This is the reference: its verdict is the verdict of record
(`settings.substrate.reference`). The checkout is restored afterwards, class
files included.

**Warm path** (`Substrate.warm`, `tools/WarmRunner.java`): one JVM per session.
For each candidate it compiles the one patched file with `javax.tools` against
the project's class path, then loads the result in a **fresh class loader**
placed in front of the project's classes, and runs the test classes in it.

Why a fresh class loader and not class redefinition: redefinition
(`Instrumentation.redefineClasses`) cannot add or remove methods, fields or
nested classes, and it keeps the static state of the previous candidate. A new
loader has neither limit: every candidate starts from freshly initialised
classes, at the price of loading the project's classes again. The JVM, the JIT
and the compiler stay warm.

What can still leak from one candidate to the next, and what is done:

| Leak | Handling |
|---|---|
| system properties, default locale and time zone | saved before and restored after each candidate |
| threads left running | interrupted after each candidate |
| `System.exit`, a hang, an exhausted heap | the runtime is killed and started again; the restart is charged to that candidate |
| output on stdout/stderr | redirected to a sink while tests run |
| anything else (files, native state) | **calibration**, below; and every candidate of the experiments is audited on both paths |

**Calibration** (`Substrate.warm_ok`): before the first candidate of a bug, the
faulty program itself is run on the warm path. If its outcome per class
differs from the recorded cold outcome, the warm path is not trusted for that
bug and every candidate falls back to the cold path (and is charged its
price). The decision is cached in `cache/audit/<bug>/_warm.json`. The start of
the runtime plus this run is the warm session start-up, charged once per
session.

**Schedule** (`substrate.schedule`): triggering classes first; then classes
that rejected earlier candidates of the session, most rejections first; then
the other classes that reach the faulty methods, fastest first. Classes that
do not reach the faulty methods are skipped. A candidate that passes the
schedule is run once on the whole suite; that run is not charged to the
session.

**Compiler options.** `-source`/`-target` follow the class-file version of the
project's own build, the source encoding is detected (UTF-8, else
ISO-8859-1), and annotation processing is off.

## 3. M1, the Evidence Memory

One memory per session, shared by both tiers. Per candidate key it keeps the
verdict, the failing class and test, the failure signature and the tier that
first proposed it.

**Signature**: (verdict, failing test, exception type, message with line
numbers and object identities removed); for a compile error, the first error
with the position removed. A candidate makes *progress* when it is new and
fails in a way the session has not seen. A repeat, or new code that fails
exactly as an earlier candidate did, makes none. Progress is read from executed
tests, never from a model's account.

**Two rules that make replay exact** (Section 5.5):

1. *Reported failure.* What a model is told about a rejected candidate is the
   first failing class in the **static** order, not in the session's current
   order. It is then a function of the candidate alone, so a strong session
   reads the same feedback whether or not a cheap tier ran before it. The
   session's order only decides what is charged.
2. *Cross-tier repeats.* When the strong tier proposes a candidate that only
   the cheap tier had proposed, it gets the stored failure as ordinary
   feedback, with no notice that it is a repeat. A tier is told about a repeat
   only when it repeats itself. The answer still comes from the memory, so no
   test runs.

With these two rules and a cold handover, the strong tier's conversation after
a step-up is identical to its conversation in a Strong-only session. This is
Proposition 2.2 (no lost repairs), and `tests/test_tierbot.py` checks it.

## 4. M2, the Escalation Controller

A policy is data (`controller.Policy`): which tiers, the budget of each, and
when the cheap tier hands over.

| Policy | Cheap tier | Step-up |
|---|---|---|
| `cheap-only` | 20 candidates | never |
| `strong-only` | none | at once |
| `fixed-K` | K candidates | after K candidates |
| `step` | up to 20 | after the first call in which at most 1 candidate made progress |
| `step-strict` | up to 20 | after the first call in which none did |
| `equal-K-R` | K candidates | after K; the strong tier gets R rounds |
| `hindsight` | — | computed in the analysis from the recorded sessions, not a run |

The stall stop of the **strong** tier belongs to M3, not to Step. Step alone
never stops the top tier early; that is what the no-lost-repairs guarantee
needs.

**Handover notes** (`controller.handover_note`): `cold` (nothing), `evidence`
(the verified failures with counts; none of the first model's text),
`evidence+patches` (plus the shortest candidate behind each signature),
`transcript` (every candidate and its result, in order). The note goes between
the task and the final instruction of the first prompt, so a cold prompt is
byte-identical to the prompt of a Strong-only session.

## 5. M3, the Budget Guard

Stops a session when the top tier made no progress for `r` rounds, or before
a model call that the remaining budget cannot pay for, judged by the average
cost of the tier's calls so far. In the experiments it runs in `log` mode: it
records where it would have stopped, and `budget.replay` cuts the recorded
session there afterwards. Its effect on the repair rate and on cost is
therefore measured apart from the rest.

When it stops, `budget.give_up_report` writes what was tried, the verified
failures and where the budget went.

## 6. Audits, charges and replay

**Audit** (`audit.Auditor`): in the experiments every distinct candidate runs
*all* scheduled classes on *both* paths, once, and the result is stored in
`cache/audit/<bug>/<key>.json`. Nothing is stopped early. From one audit the
harness can price the candidate under any order and any path:

```
audit.charge(audit, path, order) -> {wall, cpu, build, start, test, fail, ran}
```

This is why E2 needs neither a model nor a test run: a policy changes which
candidates are proposed and in which order the classes run, and both are
looked up.

A session step records two prices: `cost` (the bot: warm path, repeats
answered from memory) and `cost_plain` (a plain loop: build and a new JVM for
every candidate, repeats validated again). RQ5 compares the bot with
Strong-only priced both ways.

**Model answers** are cached under `cache/llm/` by
(model, parameters, n, messages, index of the draw). `e2` opens the cache in
`cache_only` mode; a missing answer raises `CacheMiss`, which means the replay
left the recorded table of draws and is a defect of the harness, never a
result.

**Units.** Every cost is kept in four units: dollars, tokens, wall seconds,
CPU seconds. Prices are in `settings.json`.

## 7. Data

`data/bugs/<bug>/info.json`:

| Field | Meaning |
|---|---|
| `bug`, `pid`, `bid` | identity |
| `modified_class`, `src_file` | the one source file the developer's fix changes |
| `faulty`, `buggy_methods`, `fixed_methods` | the methods the fix changes, before and after |
| `trigger_tests`, `triggering_classes` | the tests that fail on the faulty program |
| `suite` | every test class, in a fixed order |
| `covering` | the classes that reach the faulty methods |
| `ci` | per class on the fixed program: wall, CPU, status |
| `flaky` | classes excluded because their outcome changed between runs |
| `cp_test`, `cp_compile`, `bin_dir`, `test_dir` | build facts; recorded absolute paths are re-rooted at this checkout when read |

`data/selection.csv` logs every bug that was checked. 122 bugs pass the rules;
the set is the first 120 in the seeded draw order, and `data/split.json`
splits them.

`results/<exp>/<bug>/<policy>[+<handover>]-s<k>.json` is one session:
its steps (tier, call, tokens and latency, candidates with verdict, progress,
signature and both prices), the handover note, the give-up report, and where
the Budget Guard would have stopped.

## 8. Settings and registration

`settings.json` holds every value the evaluation depends on. Values still to
be fixed on the development set carry a `_tbd` note with the rule that fixes
them (Table 5.6); `analyze --split dev` evaluates those rules. `freeze` then
writes `settings.lock.json`; evaluation commands warn if the settings were not
frozen or changed afterwards.

## 9. What is not covered

- The cold path needs a working Defects4J installation; the warm path needs
  only the checkout and a Java 11 JDK.
- Test classes are the unit of scheduling and of verdicts. A class that hangs
  is cut off after `runner.class_timeout_s`.
- Only patches that replace the bodies of the given methods in one file.
