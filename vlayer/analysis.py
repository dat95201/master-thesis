"""Metrics and statistics for E1-E3; writes results/report-<split>.md.

The bug is the unit of analysis: sessions of a bug are summed (costs) or
averaged (repair rate) first. Main comparisons use a paired Wilcoxon test on
the per-bug log-ratio of costs with a Holm correction, and bootstrap
intervals for the geometric-mean ratio.
"""

import math
import random
import statistics as st
from collections import defaultdict

from scipy.stats import wilcoxon

from . import config as C
from .budget_guard import replay

UNITS = ("seconds", "cpu", "tokens", "usd")


# ---------------------------------------------------------------- statistics

def boot_ci(values, fn=st.mean, n=2000, seed=1, alpha=0.05):
    if len(values) < 2:
        return (float("nan"), float("nan"))
    rng = random.Random(seed)
    stats = sorted(fn([rng.choice(values) for _ in values]) for _ in range(n))
    return stats[int(alpha / 2 * n)], stats[int((1 - alpha / 2) * n) - 1]


def ratio_test(a: dict, b: dict, n_boot=2000):
    """Paired comparison of per-bug costs a vs b (a/b). Returns a dict."""
    bugs = [k for k in a if k in b and a[k] > 0 and b[k] > 0]
    logs = [math.log(a[k] / b[k]) for k in bugs]
    if not logs:
        return {"n": 0}
    lo, hi = boot_ci(logs, n=n_boot)
    try:
        p = wilcoxon(logs).pvalue if len(logs) >= 6 and any(logs) else float("nan")
    except ValueError:
        p = float("nan")
    return {"n": len(logs), "ratio": math.exp(st.mean(logs)), "lo": math.exp(lo), "hi": math.exp(hi),
            "p": p, "sd_log": st.stdev(logs) if len(logs) > 1 else float("nan"),
            "wins": sum(x < 0 for x in logs) / len(logs)}


def holm(pvals: dict) -> dict:
    items = sorted((p, k) for k, p in pvals.items() if not math.isnan(p))
    m, out, run = len(items), {}, 0.0
    for i, (p, k) in enumerate(items):
        run = max(run, min(1.0, (m - i) * p))
        out[k] = run
    return out


def fmt(x, d=3):
    if x is None or (isinstance(x, float) and math.isnan(x)):
        return "-"
    return f"{x:.{d}f}" if isinstance(x, float) else str(x)


def table(header, rows):
    out = ["| " + " | ".join(header) + " |", "|" + "---|" * len(header)]
    out += ["| " + " | ".join(fmt(c) for c in r) + " |" for r in rows]
    return "\n".join(out)


# ---------------------------------------------------------------- loading

def load_e1(bugs):
    rows = []
    for b in bugs:
        rows += C.read_json(C.RESULTS / "e1" / f"{b}.json", [])
    return rows


def load_live(exp, bugs):
    out = defaultdict(list)          # arm -> sessions
    for b in bugs:
        d = C.RESULTS / exp / b
        if d.exists():
            for f in sorted(d.glob("*-s*.json")):
                out[f.name.rsplit("-s", 1)[0]].append(C.read_json(f))
    return out


def rcost(rd, unit):
    c = rd["cost"]
    return rd["val"]["cpu"] if unit == "cpu" else c[unit]


def scost(sess, unit, upto=None):
    rounds = sess["rounds"][:upto] if upto else sess["rounds"]
    return sum(rcost(r, unit) for r in rounds)


def val_cost(sess, unit):
    key = "wall" if unit == "seconds" else "cpu"
    return sum(r["val"][key] for r in sess["rounds"])


def per_bug(sessions, fn):
    d = defaultdict(float)
    for s in sessions:
        d[s["bug"]] += fn(s)
    return dict(d)


def per_bug_rate(sessions):
    d = defaultdict(list)
    for s in sessions:
        d[s["bug"]].append(float(s["plausible"]))
    return {b: st.mean(v) for b, v in d.items()}


# ---------------------------------------------------------------- E1

def section_e1(bugs, settings):
    rows = load_e1(bugs)
    if not rows:
        return "## E1: orderings on mutants\n\nNo E1 results yet.\n", {}
    unit = settings["runner"]["cost_unit"]
    col = f"_{unit}"
    out = ["## E1: orderings on mutants", "",
           f"Validation cost per simulated session ({unit} seconds: rejected patches + the accepted fix), "
           "ratio = ARC / ordering (geometric mean over bugs; < 1 means ARC is cheaper).", ""]
    dev = {}
    for kind in ("main", "check"):
        for trig in (False, True):
            sel = [r for r in rows if r["kind"] == kind and r["trigger"] == trig]
            if not sel:
                continue
            cost = defaultdict(dict)
            rej = defaultdict(dict)
            for r in sel:
                cost[r["ordering"]][r["bug"]] = r["rej" + col] + r["acc" + col]
                rej[r["ordering"]][r["bug"]] = r["rej_tests" + col]
            tests = {o: ratio_test(cost["arc"], cost[o]) for o in cost if o != "arc"}
            adj = holm({o: tests[o]["p"] for o in ("frtp", "coverage") if o in tests and tests[o].get("n")})
            body = []
            for o in sorted(cost, key=lambda o: st.mean(cost[o].values())):
                t = tests.get(o, {})
                body.append([o, st.mean(cost[o].values()), st.mean(rej[o].values()),
                             t.get("ratio"), t.get("lo"), t.get("hi"), t.get("p"), adj.get(o), t.get("wins")])
            out += [f"### {kind} sessions, {'with' if trig else 'without'} trigger step", "",
                    table(["ordering", "mean cost/bug", "tests of rejected", "ARC/ord", "CI lo", "CI hi",
                           "p", "p (Holm)", "ARC wins"], body), ""]
            if kind == "main" and not trig and "frtp" in tests:
                dev["e1_sd_log_arc_frtp"] = tests["frtp"].get("sd_log")
    unsafe = sum(r["unsafe"] for r in rows if r["ordering"] == "coverage" and r["kind"] == "main")
    out.append(f"Coverage declared a wrong main-session patch plausible {unsafe:.1f} times "
               "(mean per session, summed over bugs); this must be 0.\n")
    return "\n".join(out), dev


# ---------------------------------------------------------------- E2 / E3

def breakdown(sessions):
    """RQ1 in wall seconds: G_new, G_rep, V_rep, V_rej, V_acc, headroom, shares."""
    g = {"G_new": 0.0, "G_rep": 0.0, "V_rep": 0.0, "V_rej": 0.0, "V_acc": 0.0,
         "V_rej_star": 0.0, "overhead": 0.0, "rounds": 0, "repeats": 0, "failed_cost": 0.0}
    for s in sessions:
        for r in s["rounds"]:
            g["rounds"] += 1
            if r["repeat"]:
                g["repeats"] += 1
                g["G_rep"] += r["gen"]["wall"]
                # what validating it again would have cost (removed by the cache)
                g["V_rep"] += (r["val_if_uncached"] or {}).get("wall", 0.0)
                continue
            g["G_new"] += r["gen"]["wall"]
            v = r["val"]["wall"]
            if r["verdict"] == "plausible":
                g["V_acc"] += v
            elif r["verdict"] in ("rejected", "compile_error"):
                g["V_rej"] += v
                g["V_rej_star"] += r["extra"].get("oracle_cost", r["val"])["wall"]
            if r.get("overhead"):
                g["overhead"] += r["overhead"]["wall"]
        if not s["plausible"]:
            g["failed_cost"] += scost(s, "seconds")
    g["total"] = g["G_new"] + g["G_rep"] + g["V_rep"] + g["V_rej"] + g["V_acc"]
    return g


def guard_tradeoff(sessions, settings):
    """M3 replay: cost saved against plausible sessions lost, per setting."""
    b = settings["budget_guard"]
    rows = []
    total = sum(scost(s, "seconds") for s in sessions) or 1.0
    n_pl = sum(s["plausible"] for s in sessions)
    for r in (1, 2, 3, 4, 5, None):
        for budget in (None, b["budget"]):
            saved, lost = 0.0, 0
            for s in sessions:
                keep, reason = replay(s["rounds"], r, budget, b["budget_unit"], settings["live"]["rounds"])
                saved += scost(s, "seconds") - scost(s, "seconds", keep)
                if s["plausible"] and s["plausible_round"] > keep:
                    lost += 1
            rows.append([str(r or "off"), str(budget or "off"), saved / total, lost, n_pl])
    return rows


def arm_summary(name, sessions):
    bugs = {s["bug"] for s in sessions}
    repaired = {s["bug"] for s in sessions if s["plausible"]}
    n_pl = sum(s["plausible"] for s in sessions)
    row = [name, len(sessions), n_pl / max(1, len(sessions)), f"{len(repaired)}/{len(bugs)}"]
    for u in ("seconds", "tokens", "usd"):
        row.append(sum(scost(s, u) for s in sessions) / max(1, len(repaired)))
    row.append(sum(val_cost(s, "seconds") for s in sessions) / max(1, len(repaired)))
    return row


def section_e2(bugs, settings):
    arms = load_live("e2", bugs)
    if not arms:
        return "## E2: orderings, live\n\nNo E2 results yet.\n", {}
    unit = "cpu" if settings["runner"]["cost_unit"] == "cpu" else "seconds"
    out = ["## E2: orderings, live", "",
           table(["arm", "sessions", "repair rate", "bugs repaired", "s / repaired bug",
                  "tokens / repaired bug", "USD / repaired bug", "validation s / repaired bug"],
                 [arm_summary(a, s) for a, s in sorted(arms.items())]), ""]
    dev = {}
    if "arc" in arms:
        v = {a: per_bug(s, lambda x: val_cost(x, unit)) for a, s in arms.items()}
        tests = {a: ratio_test(v["arc"], v[a]) for a in v if a != "arc"}
        adj = holm({a: tests[a]["p"] for a in ("frtp", "coverage") if a in tests and tests[a].get("n")})
        rate = {a: per_bug_rate(s) for a, s in arms.items()}
        body = []
        for a, t in tests.items():
            diff = [rate[a].get(b, 0) - rate["default"].get(b, 0) for b in rate[a]] if "default" in rate else []
            body.append([a, t.get("n"), t.get("ratio"), t.get("lo"), t.get("hi"), t.get("p"), adj.get(a),
                         st.mean(diff) if diff else None])
        out += [f"### Validation cost ({unit}) of ARC against each ordering, per bug", "",
                table(["vs", "bugs", "ARC/ord", "CI lo", "CI hi", "p", "p (Holm)",
                       "repair rate - Default"], body), ""]
        if "frtp" in tests:
            dev["e2_sd_log_arc_frtp"] = tests["frtp"].get("sd_log")
        # candidates that pass the triggering classes: the only ones a trigger step leaves to the order
        vkey = "wall" if unit == "seconds" else "cpu"
        body = []
        for a, ss in sorted(arms.items()):
            rd = [r for s in ss for r in s["rounds"]
                  if not r["repeat"] and r["verdict"] == "rejected" and r["extra"].get("trigger_pass")]
            spent = sum(r["val"][vkey] for r in rd)
            best = sum(r["extra"]["oracle_cost"][vkey] for r in rd)
            body.append([a, len(rd), spent, (spent - best) / spent if spent else None])
        out += ["### Rejected candidates that pass the triggering classes", "",
                table(["arm", "candidates", f"validation ({unit})", "headroom share"], body), ""]

    if "default" in arms:
        ss = arms["default"]
        g = breakdown(ss)
        tot = g["total"] or 1.0
        val = g["V_rep"] + g["V_rej"] + g["V_acc"]
        out += ["### RQ1: where the cost goes (Default sessions, wall seconds)", "",
                table(["component", "seconds", "share"],
                      [[k, g[k], g[k] / tot] for k in ("G_new", "G_rep", "V_rep", "V_rej", "V_acc")]), "",
                f"- ordering headroom (V_rej - V_rej*): {fmt(g['V_rej'] - g['V_rej_star'], 1)} s "
                f"= {fmt((g['V_rej'] - g['V_rej_star']) / tot)} of the total",
                f"- start-up share of validation (build + JVM): {fmt(g['overhead'] / val if val else None)}",
                f"- repeat rate: {fmt(g['repeats'] / max(1, g['rounds']))}",
                f"- cost of failed sessions: {fmt(g['failed_cost'] / tot)} of the total", ""]
        rounds = [len(s["rounds"]) for s in ss]
        dev["median_rounds_default"] = st.median(rounds) if rounds else None
        out += ["### Module M3 (Budget Guard) replayed on Default sessions", "",
                table(["r", "budget", "share of time saved", "plausible lost", "plausible sessions"],
                      guard_tradeoff(ss, settings)), ""]
    return "\n".join(out), dev


def section_e3(bugs, settings, margin_pp):
    e3 = load_live("e3", bugs).get("layer", [])
    if not e3:
        return "## E3: whole layer, live\n\nNo E3 results yet.\n"
    e2 = load_live("e2", bugs)
    b = settings["budget_guard"]
    cut = []          # E3 sessions with the Budget Guard's stop applied (replay)
    for s in e3:
        keep, _ = replay(s["rounds"], b["r"], b["budget"], b["budget_unit"], settings["live"]["rounds"])
        t = dict(s)
        t["rounds"] = s["rounds"][:keep]
        t["plausible"] = s["plausible"] and s["plausible_round"] <= keep
        cut.append(t)
    rows = [arm_summary("E3 layer (notice, no stop)", e3), arm_summary("E3 layer + Budget Guard", cut)]
    for a in ("default", "arc"):
        if a in e2:
            rows.append(arm_summary(f"E2 {a} (same bugs)", [s for s in e2[a] if s["bug"] in {x["bug"] for x in e3}]))
    out = ["## E3: whole layer, live", "",
           table(["configuration", "sessions", "repair rate", "bugs repaired", "s / repaired bug",
                  "tokens / repaired bug", "USD / repaired bug", "validation s / repaired bug"], rows), ""]
    if "default" in e2:
        rl, rd = per_bug_rate(cut), per_bug_rate(e2["default"])
        diff = [rl[k] - rd[k] for k in rl if k in rd]
        if diff:
            lo, _ = boot_ci(diff, alpha=0.10)          # one-sided 95 %
            ok = "too few bugs" if math.isnan(lo) else lo > -margin_pp / 100
            out.append(f"- repair rate, layer - Default: mean {fmt(st.mean(diff))}, one-sided 95 % lower "
                       f"bound {fmt(lo)}; non-inferior at a {margin_pp} pp margin: **{ok}**")
        cl = per_bug(cut, lambda s: scost(s, "usd"))
        cd = per_bug([s for s in e2["default"] if s["bug"] in cl], lambda s: scost(s, "usd"))
        t = ratio_test(cl, cd)
        if t.get("n"):
            out.append(f"- USD per bug, layer / Default: {fmt(t['ratio'])} [{fmt(t['lo'])}, {fmt(t['hi'])}], "
                       f"p = {fmt(t['p'])}")
    ttp = []
    for s in cut:
        if s["plausible"]:
            ttp.append(sum(r["cost"]["seconds"] for r in s["rounds"][:s["plausible_round"]]))
    if ttp:
        out.append(f"- median time to a plausible patch: {fmt(st.median(ttp), 1)} s")
    if b["budget"]:
        over = lambda ss: sum(scost(s, b["budget_unit"]) > b["budget"] for s in ss) / max(1, len(ss))  # noqa: E731
        out.append(f"- budget overrun: {fmt(over(cut))} of layer sessions"
                   + (f", {fmt(over(e2['default']))} of Default sessions" if "default" in e2 else ""))
    reports = [s["report"] for s in e3 if s.get("report")]
    if reports:
        out += ["", "Example give-up report:", "", "```", reports[0], "```"]
    return "\n".join(out) + "\n"


# ---------------------------------------------------------------- dev checks

def section_dev(bugs, dev, settings):
    out = ["## Development-set checks", ""]
    mr = dev.get("median_rounds_default")
    out.append(f"- median rounds per Default session: {fmt(mr, 1)} "
               f"(rule: at least 3, otherwise choose a smaller model)")
    for k in ("e1_sd_log_arc_frtp", "e2_sd_log_arc_frtp"):
        if dev.get(k) is not None:
            sd = dev[k]
            out.append(f"- SD of per-bug log-ratio ARC/FRTP ({k[:2].upper()}): {fmt(sd)} "
                       f"(planned power assumes <= 0.15; smallest detectable saving at n=100, "
                       f"power 0.8: {fmt(1 - math.exp(-2.8 * sd / math.sqrt(100)))})")
    spent = defaultdict(float)
    for b in bugs:
        for phase, sec in C.read_json(C.CACHE / "audit" / b / "_ledger.json", {}).items():
            spent[phase] += sec
    for phase, sec in spent.items():
        out.append(f"- audit CPU time, {phase}: {sec / 3600:.2f} h over {len(bugs)} bugs "
                   f"({sec / max(1, len(bugs)) / 60:.1f} min per bug)")
    return "\n".join(out) + "\n"


def run(split, bugs, settings):
    margin = settings["analysis"]["margin_pp"]
    e1, dev1 = section_e1(bugs, settings)
    e2, dev2 = section_e2(bugs, settings)
    e3 = section_e3(bugs, settings, margin)
    dev = section_dev(bugs, {**dev1, **dev2}, settings)
    text = "\n".join([f"# Results ({split}, {len(bugs)} bugs, settings {C.settings_hash()})", "",
                      dev, e1, e2, e3])
    path = C.RESULTS / f"report-{split}.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    print(text)
    print(f"\n-> {path}")
