# Design: from the proposal to the code

This file maps each mechanism and experiment of the proposal (`report/`,
Chapters 4 and 5) to the code, and records the choices the proposal leaves
open. Section numbers refer to the proposal.

## 1. Measurement

**Test runner** (`tools/AuditRunner.java`, `d4j.run_classes`). Tests are
ordered and charged per *test class*. The runner executes classes one after
another in one JVM with JUnit 4 and writes, per class: pass/fail, wall time,
process CPU time, and the first failure (test method, exception, message, the
line in the test class). A class that runs longer than
`runner.class_timeout_s` is a failure (a patch that hits the limit is
rejected). If the JVM dies inside a class, that class counts as failed and
the remaining classes run in a new JVM. JVM start-up is the process time
minus the time of the classes.

**Build.** A candidate is written into the faulty checkout, the compiled
classes of the modified class are deleted, and `defects4j compile` builds it.
Its wall and CPU time are the build cost.

**Audit** (`audit.py`). Every distinct patch is compiled once and run on
*every* class of the suite. The cost of any ordering is then charged from the
audit: build + JVM start-up + the classes the ordering would run up to its
first failure (`audit.charge`). A random tenth of the audits (by hash) are run
twice more, in reverse and in a shuffled order; disagreements are stored in
`order_check` (order dependence check). Audits are cached in
`cache/audit/<bug>/<key>.json` and shared by all configurations. Each bug has
a compute ledger; a bug that passes its CPU cap is dropped and reported.

**Cost unit.** Orderings learn and are charged in `runner.cost_unit` (CPU
seconds by default, as in Chapter 2). Every record also keeps wall time, and
model calls keep latency and tokens. Money = tokens × price + validation CPU
hours × price (`settings.prices`).

## 2. Bug selection (Section 5.1) — `bugs.py`

Rules, checked in this order, cheapest first:

1. one modified class and one changed file (Defects4J metadata);
2. both versions check out and compile;
3. the fix changes only the bodies of existing methods: replacing the changed
   methods of the faulty file by their fixed versions must give the fixed file
   token for token (`javasrc.faulty_methods`). These are the *faulty methods*
   given to the model;
4. the suite (`tests.all`) runs in under `max_suite_seconds` on the fixed
   program;
5. *flakiness*: a class is kept only if it passes all `flaky_runs` (3) runs on
   the fixed program;
6. *reproducible*: each triggering class (a class containing a trigger test)
   is kept and fails on the faulty program;
7. at least `min_test_classes` (10) classes remain.

The run on the faulty program is the *failing CI run*: it gives ARC's start
times and the triggering classes. One extra run of the faulty program with a
probe statement at the start of each faulty method
(`System.setProperty("vlayer.hit", "1")`, placed after `this(...)`/`super(...)`
in constructors) gives the classes that reach the faulty methods, used by
Coverage. If the probe does not compile, Coverage falls back to all classes.

`select` shuffles the active bugs of each project with the fixed seed, takes
them round robin over projects, and stops at 120 accepted bugs. The split is
random but also round robin over projects: 20 dev, 100 eval, the first 50 of
eval for E3.

## 3. Module M1: Repeat Guard (Section 4.4)

`javasrc.normalise`: tokens of the method (comments and layout gone), local
variables renamed `v0, v1, ...` in order of first appearance. "Local" covers
method parameters, locals, loop and catch variables, lambda parameters and
try resources; identifiers after `.` and names followed by `(` are never
renamed. Semantically equivalent code (`a+b` vs `b+a`) is not merged. The
candidate key is the hash of its normalised methods. If the method cannot be
parsed, only comments and layout are removed (always safe).

A repeat gets the stored result and costs no validation. With the notice on
(E3, `repair`), the model is told which round proposed it and why it was
rejected. Without the notice (E2), the model gets the same feedback as the
first time, so the conversation is the same as without the cache.

Known limitation: a field used without `this.` before a local with the same
name is declared in the same method would be renamed with it. This needs both
names in the same method and only matters if two patches differ only there.

## 4. Module M2: Test Prioritiser and the orderings (Sections 4.5, 5.2)

`orderings.py`. Each ordering returns the class order and optionally
`stop_after`, and then *observes* only the classes that ran (censoring).

| Name | Rule |
|---|---|
| `default` | `tests.all` order |
| `frtp` | most rejections in the session first; ties in default order |
| `coverage` | classes that reach the faulty methods: the class named after the modified class first, then fastest (CI time) first; then the rest, fastest first. For a change inside the faulty methods, the patch is plausible once the reaching classes pass (`stop_after`) |
| `arc` | Algorithm 1: draw `θ_t ~ Beta(π_t + k_t, 1 − π_t + n_t − k_t)` for each candidate (Thompson sampling; the posterior mean is `K_t = (π_t + k_t)/(1 + n_t)`), `m_t = s_t/n_t` or the CI time, order by `θ_t/m_t` |
| `oracle` | cheapest failing class in hindsight, computed from the audit |
| `+trigger` | prefix: triggering classes first (in the inner order), then the inner order |

**Notation.** The proposal writes `q̂_t` for `K_t` (`ARC.K`) and `ĉ_t` for `m_t`
(`ARC.m`); `α_t = π_t + k_t` and `β_t = 1 − π_t + n_t − k_t` are the Beta parameters
(`ARC._beta`).

**ARC prior.** Three location groups (`orderings.location_group`): classes
whose simple name starts with the modified class's simple name
(`StringUtilsTest`, `StringUtilsTrimTest` for `StringUtils`), other classes of
its package, the rest. Each non-empty group gets its share
`settings.arc.shares` (provisional 0.60 / 0.25 / 0.15), split equally among
its classes; the shares of empty groups are redistributed, so `π_t` sums to
one (`orderings.location_prior`). The prior has the weight of one run. A class
with no CI time is estimated from its file size at the rate observed so far.
Ties are broken by the default order.

**Exploration (censoring).** Early stopping observes a class only when the
order reaches it (a cascading-bandit observation model), so a class whose
estimate is too low is rarely corrected. ARC orders by a posterior draw, not
by `K_t`. Inverse propensity weighting is not used: a deterministic order
reaches a class with probability 0 or 1. The draws of a candidate are seeded
by the session (`build_layer` seeds the ordering with `seed-bug-session`; E1
with `seed-bug-order`) and by the candidate's key (`order(..., key=)`), so
every run is reproducible. Exploration depends on the prior being calibrated:
with too much mass on the "other" group, unrelated classes are often drawn
first, which is why the shares are fitted on the dev set.

**RQ4 variants** (one part changed each):

| Name | Change |
|---|---|
| `arc-nocost` | order by `K_t` only |
| `arc-costonly` | order by `1/m_t` only (fastest first) |
| `arc-flat` | `π_t = 1/N` |
| `arc-noname` | the class-name hint removed (named classes join the package group, which gets both shares), package kept |
| `arc-wrong` | prior computed for a wrong location (a class of another package) |
| `arc-mean` | no exploration: order by the posterior mean `K_t/m_t` |
| `arc-size` | start times from test file size, not the CI run |
| `frtp-cost` | FRTP with ties broken by CI time |
| `arc-cov` | ARC on the classes Coverage selects, with Coverage's stop |

**Coverage safety.** Skipping classes is only valid for a change inside the
faulty methods. E1 groups S3 and S4 change other code, so for them Coverage
runs the remaining classes after the reaching ones. Each simulated plausible
verdict is checked against the audit; the report counts unsafe accepts (must
be 0).

## 5. Module M3: Budget Guard (Section 4.6)

`budget_guard.py`. Failure signature: rejecting test + exception type +
message (compile errors: the first error line without file and line;
unreadable answers: one shared signature). Progress = a new candidate with a
new signature. Stop when `r` rounds in a row make no progress, when
`spent + spent/rounds > budget` (unit `seconds`, `tokens` or `usd`), or at
`R_max = live.rounds`. `log_only` records the first time a rule fires and goes
on. `replay` applies the guard to a recorded session; `give_up_report` builds
the report of Figure 4.3.

## 6. The layer and the loop

`layer.ValidationLayer.validate(methods, gen)` does, in order: unreadable
check → M1 lookup → M2 order → runner → M2 observe → feedback → M3 step.

* `AuditedRunner` (experiments): audit once, charge the ordering. Also stores
  the oracle cost of the candidate (for the ordering headroom) and whether it
  passes the triggering classes.
* `LiveRunner` (`vlayer repair --live`): really runs the ordered classes in
  one JVM and stops at the first failure. This is how a CI bot uses the layer.

`loop.py` is the ChatRepair-style loop: the first prompt (faulty methods, the
first trigger test and its error from Defects4J's `trigger_tests` file) and
the feedback of Table 5.4, keeping the whole conversation. The answer is read
from the first Java code block; methods are matched to the faulty methods by
signature, then by name and number of parameters. Methods missing from the
answer stay unchanged; an answer with none of them is *unreadable*.

`llm.py` caches answers by (model, temperature, max tokens, session number,
conversation). Arms of the same bug and session therefore share answers until
their conversations differ, and a cached answer is charged with its recorded
latency and tokens. The `mock` model is only for smoke tests.

## 7. Experiments (Sections 5.4, 5.5) — `experiments.py`

**E1, step 1 (`mutants`).** Major (`all.mml`) mutates the faulty file of the
fixed program (S1: lines in the faulty methods; S3: lines in other methods of
the class), the faulty file of the faulty program (S2: faulty methods) and one
other class loaded by the trigger tests (S4, from Defects4J's
`loaded_classes`). Per group, mutants are drawn at random and audited until
`mutants_per_group` (5) compile and fail at least one class, or
`max_audits_per_group` (10) are audited. The developer's fix is audited too.

**E1, step 2 (`e1`).** A session is a random order of the bug's wrong patches
followed by the fix. *Main* sessions use S1+S2, *check* sessions all groups.
`sessions_per_bug` (10) random orders per bug; every ordering, with and
without the trigger prefix, runs on the same orders with a fresh state.
Output per (bug, kind, ordering, trigger): mean cost of rejected patches,
of their test classes only, of the accepted fix, and unsafe accepts.

**E2.** Arms `default`, `frtp`, `coverage`, `arc`; cache on, notice off, no
stop; `live.sessions` (3) sessions per bug.

**E3.** One arm: ARC, cache on, notice on, Budget Guard in log-only mode.

## 8. Analysis (Section 5.6) — `analysis.py`

* Bug is the unit: costs are summed over a bug's sessions, repair rates
  averaged.
* ARC vs FRTP and ARC vs Coverage: paired Wilcoxon on per-bug log-ratios,
  Holm over the two; geometric-mean ratio with a bootstrap 95 % interval.
* RQ1 (Default sessions of E2): `G_new`, `G_rep`, `V_rep`, `V_rej`, `V_acc`;
  ordering headroom `V_rej − V_rej*` (oracle cost per rejected candidate);
  start-up share (build + JVM over validation); repeat rate; cost of failed
  sessions. `V_rep` is what the repeats would have cost if validated again;
  it is exact for Default (fixed order) and approximate for adaptive orders.
* Trigger-pass subset: rejected candidates that pass the triggering classes,
  the only ones whose cost still depends on the order after a trigger step.
* M3 trade-off: replay for `r` = 1..5 and off, with and without the budget:
  share of time saved and plausible sessions lost.
* E3: layer with the replayed stop vs E2 Default (cost per repaired bug, USD
  ratio test, repair-rate non-inferiority with a one-sided 95 % bootstrap
  bound against `analysis.margin_pp`), notice effect vs E2 ARC, median time to
  a plausible patch, budget overrun.
* Dev checks: median rounds (model rule), SD of log-ratios and the smallest
  detectable saving, audit hours per bug (compute budget), and the values the
  dev set fixes: ARC prior shares (`prior_shares`: per wrong patch of S1, S2
  and the rejected E2 candidates, the share of its failing classes in each
  location group, averaged over patches) and the Budget Guard
  (`guard_suggestion`: the smallest `r` whose replay loses at most 5 % of the
  plausible Default sessions; `budget` = 90th percentile of the time to a
  plausible patch).
