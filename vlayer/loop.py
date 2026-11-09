"""The conversational repair loop (ChatRepair style).

The first prompt gives the faulty methods, a failing test and its error.
After each round the layer's feedback is sent back. A session ends at the
first plausible patch, or when the layer says to stop (round limit, or the
Budget Guard when it is not in log-only mode).
"""

import textwrap

from . import javasrc
from . import orderings
from .budget_guard import BudgetGuard
from .layer import AuditedRunner, LiveRunner, ValidationLayer
from .llm import LLM
from .repeat_guard import RepeatGuard


def first_prompt(info) -> str:
    n = len(info["buggy_methods"])
    what = "method contains" if n == 1 else f"{n} methods contain"
    methods = "\n\n".join(textwrap.indent(textwrap.dedent(t), "    ")
                          for t in info["buggy_methods"].values())
    trig = info["trigger_tests"][0]
    test = trig["test"].rsplit(".", 1)[-1]
    target = "the complete fixed method" if n == 1 else "all the complete fixed methods"
    return (f"The following Java {what} a bug:\n\n{methods}\n\n"
            f"It makes this test fail:\n"
            f"    Test:  {test}\n"
            f"    Error: {trig['error']}\n\n"
            f"Return {target} in one Java code block.")


def build_layer(info, settings, arm: dict, live=False):
    """arm = {ordering, cache, notice, guard} with guard in off|log|on."""
    ordering = orderings.make(arm["ordering"], info, settings)
    runner = LiveRunner(info, settings) if live else AuditedRunner(
        info, settings, phase="live", cap_hours=settings["live"]["cap_cpu_hours"])
    rg = RepeatGuard(notice=arm.get("notice", False)) if arm.get("cache", True) else None
    bg = None
    if arm.get("guard", "off") != "off":
        b = settings["budget_guard"]
        bg = BudgetGuard(b["r"], b["budget"], b["budget_unit"], settings["live"]["rounds"],
                         log_only=arm["guard"] == "log")
    return ValidationLayer(info, ordering, runner, settings, rg, bg)


def run_session(info, settings, arm: dict, session: int, llm: LLM = None, live=False, verbose=False):
    llm = llm or LLM(settings)
    layer = build_layer(info, settings, arm, live)
    faulty = [(f["key"], f["name"], f["nparams"]) for f in info["faulty"]]
    messages = [{"role": "user", "content": first_prompt(info)}]
    report = None
    while True:
        answer, gen = llm.chat(messages, session, info)
        methods = javasrc.parse_answer(answer, faulty)
        res = layer.validate(methods, gen)
        if verbose:
            r = res.record
            print(f"    round {r['round']:>2}: {res.verdict:<13} gen {gen['wall']:.1f}s "
                  f"val {r['val']['wall']:.1f}s  {(r['fail'] or {}).get('class', '')}")
        if res.verdict == "plausible" or not res.go_on:
            report = res.report
            break
        messages += [{"role": "assistant", "content": answer},
                     {"role": "user", "content": res.feedback}]
    rounds = layer.rounds
    plausible_round = next((r["round"] for r in rounds if r["verdict"] == "plausible"), None)
    if report:
        report = report.replace("session 0", f"session {session}")
    return {
        "bug": info["bug"], "arm": arm, "session": session,
        "plausible": plausible_round is not None, "plausible_round": plausible_round,
        "rounds": rounds, "report": report,
        "would_stop": layer.budget_guard.would_stop if layer.budget_guard else None,
    }
