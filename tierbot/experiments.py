"""The five experiments (Table 5.1 of the proposal).

  E0  the warm substrate: every distinct candidate is audited on the cold
      path and on the warm path. No command of its own: the audits are made
      as the sessions below run, and `analyze` reports them.
  E1  single-tier sessions, live: each model alone, recorded once.
  E2  escalation policies by replay: every policy is run again on the
      recorded answers and audits; no model call.
  E3  handover, live: the strong tier is started again with each note in
      the sessions that step up.
  E4  the whole system, live, on bugs that were not used to choose the note.

A session of policy P with handover H is stored in
results/<exp>/<bug>/<P>[+<H>]-s<k>.json. Everything is resumable.
"""

import traceback
from concurrent.futures import ProcessPoolExecutor

from . import audit as A
from . import config as C
from . import llm as L
from .bot import AuditedValidator
from .bugs import load_info
from .controller import policy as make_policy
from .session import run_session


def _for_each_bug(fn, bugs, jobs, *args):
    """Run fn(bug, *args) for each bug, in parallel processes if jobs > 1."""
    if jobs > 1:
        with ProcessPoolExecutor(jobs) as ex:
            for msg in ex.map(_safe, [(fn, b, args) for b in bugs]):
                print(msg)
    else:
        for b in bugs:
            print(_safe((fn, b, args)))


def _safe(job):
    fn, bug, args = job
    try:
        return fn(bug, *args)
    except A.CapReached as e:
        return f"  {bug}: dropped ({e})"
    except L.CacheMiss as e:
        return f"  {bug}: REPLAY DEFECT, {e}"
    except Exception as e:  # noqa: BLE001
        traceback.print_exc()
        return f"  {bug}: ERROR {e}"


def run_name(policy, handover):
    return policy if handover == "cold" else f"{policy}+{handover}"


def sessions_for_bug(bug, settings, exp, runs, cache_only=False, models=None):
    """runs = [(policy, handover, guard)]; one validator serves the bug, so
    its live runtime and its audits are shared by every run."""
    info = load_info(bug)
    llms = L.tiers(settings, cache_only, models)
    out_dir = C.RESULTS / exp / bug
    todo = [(s, p, h, g) for s in range(settings["sessions"]) for p, h, g in runs
            if not (out_dir / f"{run_name(p, h)}-s{s}.json").exists()]
    if not todo:
        return f"  {bug}: {exp} done"
    validator = AuditedValidator(info, settings)
    try:
        for s, p, h, g in todo:
            res = run_session(info, settings, p, s, h, g, llms, validator)
            C.write_json(out_dir / f"{run_name(p, h)}-s{s}.json", res)
    finally:
        validator.close()
    return f"  {bug}: {exp} sessions run {len(todo)}"


def replay_policies(settings) -> list:
    """The policies of E2. Hindsight is not run: it is the cheapest of the
    fixed switch points, chosen per session in the analysis."""
    n, b1 = C.tier(settings, "cheap")["n"], C.tier(settings, "cheap")["budget"]
    eq = settings["e2"]["equal_attempts"]
    names = [p for p in settings["e2"]["policies"] if p != "hindsight"]
    names += [f"fixed-{k}" for k in range(n, b1 + 1, n)]             # switch points for Hindsight
    names += ["step-strict", f"equal-{eq['cheap']}-{eq['strong']}"]   # a variant and the second reading
    ok = []
    for name in dict.fromkeys(names):
        try:
            make_policy(name, settings)
            ok.append(name)
        except ValueError as e:        # e.g. fixed-5 when a variant draws 3 candidates per call
            print(f"  skipping {e}")
    return ok


def run_e1(bugs, settings, jobs=1, models=None):
    runs = [("cheap-only", "cold", "off"), ("strong-only", "cold", "off")]
    _for_each_bug(sessions_for_bug, bugs, jobs, settings, "e1", runs, False, models)


def run_e2(bugs, settings, jobs=1, models=None):
    runs = [(p, "cold", "off") for p in replay_policies(settings)]
    _for_each_bug(sessions_for_bug, bugs, jobs, settings, "e2", runs, True, models)


def run_e3(bugs, settings, jobs=1, models=None):
    runs = [("step", "cold", "off")] + [("step", h, "off") for h in settings["e3"]["handovers"]]
    _for_each_bug(sessions_for_bug, bugs, jobs, settings, "e3", runs, False, models)


def run_e4(bugs, settings, jobs=1, models=None):
    runs = [("step", settings["e4"]["handover"], "log"), ("strong-only", "cold", "log")]
    _for_each_bug(sessions_for_bug, bugs, jobs, settings, "e4", runs, False, models)
