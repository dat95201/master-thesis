"""Metrics and statistics (Sections 5.7 and 5.8 of the proposal); writes
results/report-<split>.md.

The bug is the unit of analysis: the sessions of a bug are summed (costs) or
averaged (repair rate) first. The main comparisons use a paired Wilcoxon
test on the per-bug log-ratio with a Holm correction, and bootstrap
intervals over bugs.
"""

import math
import random
import statistics as st
from collections import defaultdict

from . import audit as A
from . import budget as B
from . import config as C

# usd: every input token at the list price; usd_cached: with the prompt-cache
# model of budget.cache_split. Conclusions are read from the one less
# favourable to escalation.
UNITS = ("usd", "usd_cached", "tokens", "seconds", "cpu")
PARTS = ("G_cheap", "G_strong", "V_build", "V_start", "V_test")


# ---------------------------------------------------------------- statistics

def boot_ci(values, fn=st.mean, n=2000, seed=1, alpha=0.05):
    if len(values) < 2:
        return (float("nan"), float("nan"))
    rng = random.Random(seed)
    stats = sorted(fn([rng.choice(values) for _ in values]) for _ in range(n))
    return stats[int(alpha / 2 * n)], stats[int((1 - alpha / 2) * n) - 1]


def wilcoxon_p(logs):
    """Two-sided paired Wilcoxon signed-rank test on per-bug log-ratios."""
    if len(logs) < 6 or not any(logs):
        return float("nan")
    try:
        from scipy.stats import wilcoxon
        return float(wilcoxon(logs).pvalue)
    except (ImportError, ValueError):
        return float("nan")


def ratio_test(a: dict, b: dict):
    """Paired comparison of per-bug quantities a against b (a/b)."""
    bugs = [k for k in a if k in b and a[k] > 0 and b[k] > 0]
    logs = [math.log(a[k] / b[k]) for k in bugs]
    if not logs:
        return {"n": 0}
    lo, hi = boot_ci(logs)
    return {"n": len(logs), "ratio": math.exp(st.mean(logs)), "lo": math.exp(lo), "hi": math.exp(hi),
            "p": wilcoxon_p(logs), "sd_log": st.stdev(logs) if len(logs) > 1 else float("nan"),
            "wins": sum(x < 0 for x in logs) / len(logs)}


def holm(pvals: dict) -> dict:
    items = sorted((p, k) for k, p in pvals.items() if p is not None and not math.isnan(p))
    m, out, run = len(items), {}, 0.0
    for i, (p, k) in enumerate(items):
        run = max(run, min(1.0, (m - i) * p))
        out[k] = run
    return out


def non_inferior(diffs, margin_pp):
    """One-sided 95% bootstrap bound on the mean paired difference in repair rate."""
    if len(diffs) < 2:
        return st.mean(diffs) if diffs else float("nan"), float("nan"), None
    lo, _ = boot_ci(diffs, alpha=0.10)
    return st.mean(diffs), lo, lo > -margin_pp / 100


def fmt(x, d=3):
    if x is None or (isinstance(x, float) and math.isnan(x)):
        return "-"
    if isinstance(x, bool):
        return "yes" if x else "no"
    if isinstance(x, float):
        if abs(x) >= 1000:
            return f"{x:,.0f}"
        return f"{x:.1f}" if abs(x) >= 100 else f"{x:.{d}f}"
    return str(x)


def table(header, rows):
    out = ["| " + " | ".join(header) + " |", "|" + "---|" * len(header)]
    out += ["| " + " | ".join(fmt(c) for c in r) + " |" for r in rows]
    return "\n".join(out)


def pct(x):
    return "-" if x is None or (isinstance(x, float) and math.isnan(x)) else f"{100 * x:.1f}%"


# ---------------------------------------------------------------- sessions

def load(exp, bugs) -> dict:
    """{run name: [session records]} for one experiment."""
    out = defaultdict(list)
    for b in bugs:
        d = C.RESULTS / exp / b
        if d.exists():
            for f in sorted(d.glob("*-s*.json")):
                out[f.name.rsplit("-s", 1)[0]].append(C.read_json(f))
    return out


def cost(sess, settings, unit, plain=False) -> float:
    """Cost of a session. `plain` prices it as a plain loop would pay: the
    project's build and a new JVM for every candidate, repeats validated
    again (None if the cold path was not audited)."""
    if plain:
        if any(s["cost_plain"] is None for s in sess["steps"]):
            return None
        return sum(s["cost_plain"][unit] for s in sess["steps"])
    total = sum(s["cost"][unit] for s in sess["steps"])
    start = sess.get("warm_start") or {"wall": 0.0, "cpu": 0.0}
    if unit == "seconds":
        total += start["wall"]
    elif unit == "cpu":
        total += start["cpu"]
    elif unit in ("usd", "usd_cached"):
        total += start["cpu"] * settings["prices"]["usd_per_cpu_hour"] / 3600
    return total


def parts(sess, settings, unit="usd", path="warm") -> dict:
    """The five components of Section 2.2 for one session, in usd or seconds."""
    money = unit == "usd"
    cpu_usd = settings["prices"]["usd_per_cpu_hour"] / 3600
    out = dict.fromkeys(PARTS, 0.0)
    for s in sess["steps"]:
        out["G_" + s["tier"]] += s["cost"]["model_usd"] if money else s["cost"]["model_seconds"]
        for c in s["candidates"]:
            v = (c["val"] if path == "warm" or not c.get("val_if_uncached") else c["val_if_uncached"]).get(path)
            if v is None and path == "warm":
                v = c["val"].get("cold")
            if v is None:
                continue
            for part in ("build", "start", "test"):
                out["V_" + part] += v[part]["cpu"] * cpu_usd if money else v[part]["wall"]
    if path == "warm" and sess.get("warm_start"):
        w = sess["warm_start"]
        out["V_start"] += w["cpu"] * cpu_usd if money else w["wall"]
    return out


def cut(sess, settings, r=None, budget="default"):
    """The session with the Budget Guard's stop applied afterwards (replay)."""
    g = settings["budget_guard"]
    keep, reason = B.replay(sess["steps"], g["r"] if r is None else r,
                            g["budget"] if budget == "default" else budget, g["budget_unit"])
    if keep >= len(sess["steps"]):
        return sess
    t = dict(sess)
    t["steps"] = sess["steps"][:keep]
    t["plausible"], t["plausible_tier"], t["stopped"] = False, None, reason
    return t


def per_bug(sessions, fn) -> dict:
    d = defaultdict(float)
    for s in sessions:
        d[s["bug"]] += fn(s)
    return dict(d)


def per_bug_rate(sessions) -> dict:
    d = defaultdict(list)
    for s in sessions:
        d[s["bug"]].append(float(s["plausible"]))
    return {b: st.mean(v) for b, v in d.items()}


def repaired_bugs(sessions) -> int:
    return len({s["bug"] for s in sessions if s["plausible"]})


def n_steps(sess, tier):
    return sum(1 for s in sess["steps"] if s["tier"] == tier)


def n_drawn(sess, tier):
    return sum(len(s["candidates"]) + s.get("unvalidated", 0) for s in sess["steps"] if s["tier"] == tier)


def summary_row(name, sessions, settings, plain=False):
    """One row of a policy table: repair rate and cost per repaired bug."""
    rep = max(1, repaired_bugs(sessions))
    row = [name, len(sessions), pct(sum(s["plausible"] for s in sessions) / max(1, len(sessions))),
           f"{repaired_bugs(sessions)}/{len({s['bug'] for s in sessions})}"]
    for u in UNITS:
        vals = [cost(s, settings, u, plain) for s in sessions]
        row.append(None if any(v is None for v in vals) else sum(vals) / rep)
    return row


SUMMARY_HEAD = ["policy", "sessions", "repair rate", "bugs repaired", "USD / repaired bug",
                "USD / repaired bug, prompt cache", "tokens / repaired bug", "s / repaired bug", "CPU s / repaired bug"]


# ---------------------------------------------------------------- E0 (RQ2)

def section_e0(bugs, settings):
    rows, n, both = defaultdict(lambda: defaultdict(list)), 0, 0
    disagree, fallback, restarts, order_bad, order_n, suite_bad, plaus = [], [], 0, 0, 0, [], 0
    multi, rejected, moved = 0, 0, 0
    head = {"sched": 0.0, "best": 0.0}
    starts = []
    for b in bugs:
        d = C.CACHE / "audit" / b
        base = C.read_json(d / "_warm.json")
        if base is not None:
            (starts if base["ok"] else fallback).append(base["start"] if base["ok"] else b)
        for f in sorted(d.glob("*.json")) if d.exists() else []:
            if f.name.startswith("_"):
                continue
            a = C.read_json(f)
            n += 1
            order = a["classes"]
            for path in A.PATHS:
                ch = A.charge(a, path, order) if A.usable(a.get(path)) else None
                if ch is None:
                    continue
                for part in ("build", "start", "test"):
                    rows[path][part].append(ch[part]["cpu"])
                rows[path]["total"].append(ch["cpu"])
            cold, warm = a.get("cold"), a.get("warm")
            if A.usable(cold) and A.usable(warm):
                both += 1
                if not A.agree(a):
                    disagree.append(f"{b}/{a['key']}")
                restarts += warm.get("restarts", 0)
            if cold:
                if "order_check" in cold:
                    order_n += 1
                    order_bad += not cold["order_check"]["consistent"]
                if "whole_suite" in cold:
                    plaus += 1
                    if not cold["whole_suite"]["passed"]:
                        suite_bad.append(f"{b}/{a['key']}")
            ref = A.reference(a, settings)
            if ref["compiled"]:
                fails = [c for c in order if A.failed(ref["classes"].get(c))]
                if fails:
                    rejected += 1
                    multi += len(fails) > 1
                    unit = settings["runner"]["cost_unit"]
                    head["sched"] += A.charge(a, "cold" if A.usable(cold) else "warm", order)["test"][unit]
                    head["best"] += A.best_order_cost(a, "cold" if A.usable(cold) else "warm", unit)
    out = ["## E0: the warm substrate (RQ2)", ""]
    if not n:
        return "\n".join(out + ["No audits yet.", ""])
    med = lambda v: st.median(v) if v else None  # noqa: E731
    mean = lambda v: st.mean(v) if v else None  # noqa: E731
    body = [[p, len(rows[p]["total"])] + [med(rows[p][k]) for k in ("build", "start", "test", "total")]
            + [mean(rows[p]["total"])] for p in A.PATHS if rows[p]["total"]]
    out += [f"{n} distinct candidates audited; {both} on both paths. Validation of one candidate, "
            "CPU seconds, classes in the static order up to the first failure:", "",
            table(["path", "candidates", "build (median)", "start (median)", "tests (median)",
                   "total (median)", "total (mean)"], body), ""]
    if starts:
        out.append(f"- session start on the warm path (start the runtime, compile the unchanged file, run the "
                   f"scheduled classes once): median {fmt(st.median(s['cpu'] for s in starts))} CPU s, "
                   f"{fmt(st.median(s['wall'] for s in starts))} s wall, over {len(starts)} bugs")
    out += [f"- verdict disagreement between the paths: {len(disagree)} of {both} candidates"
            + (f" ({', '.join(disagree[:8])})" if disagree else ""),
            f"- bugs whose warm path is not trusted (they fall back to the cold path): {len(fallback)}"
            + (f" ({', '.join(fallback[:10])})" if fallback else ""),
            f"- runtimes replaced because a candidate hung or killed the JVM: {restarts}",
            f"- order check: {order_bad} of {order_n} sampled candidates change outcome with the order",
            f"- plausible candidates that fail a class outside the schedule: {len(suite_bad)} of {plaus}"
            + (f" ({', '.join(suite_bad[:8])})" if suite_bad else ""),
            f"- rejected candidates that fail more than one scheduled class: {multi} of {rejected}; "
            f"test time under the static order {fmt(head['sched'], 2)} s against {fmt(head['best'], 2)} s "
            f"for the cheapest rejecting class in hindsight "
            f"(room left for any ordering: {pct(1 - head['best'] / head['sched']) if head['sched'] else '-'})", ""]
    return "\n".join(out)


# ---------------------------------------------------------------- E1 (RQ1)

def breakdown_rows(sessions, settings, unit, path):
    tot = dict.fromkeys(PARTS, 0.0)
    by_end = defaultdict(float)
    for s in sessions:
        p = parts(s, settings, unit, path)
        for k in PARTS:
            tot[k] += p[k]
        end = f"repaired at the {s['plausible_tier']} tier" if s["plausible"] else "not repaired"
        by_end[end] += sum(p.values())
    total = sum(tot.values()) or 1.0
    return tot, by_end, total


def token_lines(runs, settings):
    """Per tier: how the tokens of the recorded calls divide, and what the
    provider's prompt cache reported against the cache model that is priced."""
    out = []
    for t in settings["tiers"]:
        gens = {}
        for ss in runs.values():
            for x in ss:
                for s in x["steps"]:
                    if s["tier"] == t["name"]:
                        gens[(x["bug"], x["session"], s["call"])] = s
        if not gens:
            continue
        g = lambda k: sum((s["gen"] or {}).get(k, 0) for s in gens.values())  # noqa: E731
        tin, tout = g("tok_in") or 1, g("tok_out") or 1
        read = sum(s["cost"]["cache"]["read"] for s in gens.values())
        write = sum(s["cost"]["cache"]["write"] for s in gens.values())
        out.append(f"- {t['name']} tier, {len(gens)} calls: {tin:,} input and {tout:,} output tokens; "
                   f"reasoning is {pct(g('tok_reason') / tout)} of the output; prompt cache as priced: "
                   f"{pct(read / tin)} of the input read, {pct(write / tin)} written; as the provider "
                   f"reported: {pct(g('tok_in_cached') / tin)} read, {pct(g('tok_in_write') / tin)} written")
    return out + [""] if out else out


def section_e1(bugs, settings):
    runs = load("e1", bugs)
    out = ["## E1: where the cost goes (RQ1)", ""]
    if not runs:
        return "\n".join(out + ["No E1 sessions yet.", ""])
    out += [table(SUMMARY_HEAD, [summary_row(k, v, settings) for k, v in sorted(runs.items())]), ""]
    out += token_lines(runs, settings)
    has_cold = all(s["cost_plain"] is not None for v in runs.values() for x in v for s in x["steps"])
    for unit in ("usd", "seconds"):
        for path in ("warm", "cold") if has_cold else ("warm",):
            body = []
            for name, ss in sorted(runs.items()):
                tot, by_end, total = breakdown_rows(ss, settings, unit, path)
                body.append([name] + [pct(tot[k] / total) for k in PARTS]
                            + [pct(by_end["not repaired"] / total), total])
            label = "on the warm substrate" if path == "warm" else "as a plain loop (cold path, repeats validated again)"
            out += [f"### Cost breakdown in {unit}, {label}", "",
                    table(["sessions of", "model calls, cheap", "model calls, strong", "build", "start-up",
                           "tests", "spent on sessions never repaired", f"total ({unit})"], body), ""]
    return "\n".join(out)


# ---------------------------------------------------------------- E2 (RQ3)

def hindsight(runs, settings):
    """For each session, the switch point that costs least in money, chosen
    with knowledge of the outcome: among those that end with a plausible
    patch if any does."""
    points = {k: v for k, v in runs.items() if k == "strong-only" or k.startswith("fixed-")}
    by_key = defaultdict(list)
    for name, ss in points.items():
        for s in ss:
            by_key[(s["bug"], s["session"])].append(s)
    chosen = []
    for cands in by_key.values():
        ok = [s for s in cands if s["plausible"]] or cands
        chosen.append(min(ok, key=lambda s: cost(s, settings, "usd")))
    return chosen


def break_even(sessions, strong_only, settings):
    """The cheap tier's repair share minus its mean number of candidates
    times the price ratio c1/c2 (sufficient condition of Section 2.3)."""
    cheap_cost = sum(s["cost"]["usd"] for x in sessions for s in x["steps"] if s["tier"] == "cheap")
    drawn = sum(n_drawn(x, "cheap") for x in sessions)
    strong_steps = [s["cost"]["usd"] for x in strong_only for s in x["steps"] if s["tier"] == "strong"]
    if not drawn or not strong_steps or not sessions:
        return None, None
    ratio = (cheap_cost / drawn) / st.mean(strong_steps)
    share = sum(s["plausible_tier"] == "cheap" for s in sessions) / len(sessions)
    return share - drawn / len(sessions) * ratio, ratio


def section_e2(bugs, settings):
    runs = load("e2", bugs)
    out = ["## E2: escalation policies by replay (RQ3)", ""]
    info = {}
    if not runs:
        return "\n".join(out + ["No E2 sessions yet.", ""]), info
    eq = settings["e2"]["equal_attempts"]
    equal = f"equal-{eq['cheap']}-{eq['strong']}"
    main = [p for p in settings["e2"]["policies"] if p in runs] + [p for p in ("step-strict",) if p in runs]
    strong = runs.get("strong-only", [])
    hind = hindsight(runs, settings) if strong else []
    body = [summary_row(p, runs[p], settings) for p in main]
    if hind:
        body.append(summary_row("hindsight", hind, settings))
    out += [table(SUMMARY_HEAD, body), ""]

    c_strong = sum(cost(s, settings, "usd") for s in strong)
    c_hind = sum(cost(s, settings, "usd") for s in hind)
    by_key = {(s["bug"], s["session"]): s for s in strong}
    body = []
    for p in main:
        ss = runs[p]
        if p in ("cheap-only", "strong-only"):
            continue
        margin, ratio = break_even(ss, strong, settings)
        up = [s for s in ss if n_steps(s, "strong")]
        delay = st.mean(sum(x["cost"]["seconds"] for x in s["steps"] if x["tier"] == "cheap") for s in up) if up else None
        c = sum(cost(s, settings, "usd") for s in ss)
        dist = (c - c_hind) / (c_strong - c_hind) if hind and c_strong != c_hind else None
        lost = sum(1 for s in ss if by_key.get((s["bug"], s["session"]), {}).get("plausible") and not s["plausible"])
        body.append([p, pct(sum(s["plausible_tier"] == "cheap" for s in ss) / len(ss)),
                     st.mean(n_drawn(s, "cheap") for s in ss), ratio, margin, delay, pct(dist), lost])
    if body:
        out += ["### Escalating policies", "",
                table(["policy", "repaired at the cheap tier", "cheap candidates / session", "price ratio c1/c2",
                       "break-even margin", "escalation delay (s)", "distance to Hindsight",
                       "repairs of Strong-only lost (must be 0)"], body), ""]

    if "step" in runs and strong:
        tests = {}
        for u in UNITS:
            t = ratio_test(per_bug(runs["step"], lambda s, u=u: cost(s, settings, u)),
                           per_bug(strong, lambda s, u=u: cost(s, settings, u)))
            tests[u] = t
        info["p_step"] = tests["usd"].get("p")
        info["sd_log"] = tests["usd"].get("sd_log")
        info["n_bugs"] = tests["usd"].get("n")
        out += ["### Step against Strong-only, per bug (ratio < 1: Step is cheaper)", "",
                table(["unit", "bugs", "Step / Strong-only", "CI lo", "CI hi", "p (Wilcoxon)", "bugs where Step is cheaper"],
                      [[u, t.get("n"), t.get("ratio"), t.get("lo"), t.get("hi"), t.get("p"), pct(t.get("wins"))]
                       for u, t in tests.items()]), ""]
    if equal in runs and strong:
        ra, rb = per_bug_rate(runs[equal]), per_bug_rate(strong)
        diffs = [ra[b] - rb[b] for b in ra if b in rb]
        mean, lo, ok = non_inferior(diffs, settings["analysis"]["margin_pp"])
        info["sd_rate"] = st.stdev(diffs) if len(diffs) > 1 else None
        out += [f"### Second reading: equal attempts ({eq['cheap']} cheap candidates, then {eq['strong']} strong rounds)", "",
                table(SUMMARY_HEAD, [summary_row(equal, runs[equal], settings)]), "",
                f"- repair rate, {equal} minus Strong-only: mean {fmt(mean)}, one-sided 95% lower bound {fmt(lo)}; "
                f"non-inferior at {settings['analysis']['margin_pp']} points: **{fmt(ok)}**", ""]
    return "\n".join(out), info


# ---------------------------------------------------------------- E3 (RQ4)

def strong_stats(sessions):
    up = [s for s in sessions if n_steps(s, "strong")]
    cands = [c for s in up for x in s["steps"] if x["tier"] == "strong" for c in x["candidates"]]
    return {
        "sessions": len(up),
        "rounds": st.mean(n_steps(s, "strong") for s in up) if up else None,
        "repaired": sum(s["plausible"] for s in up) / len(up) if up else None,
        "tokens": st.mean(sum(x["cost"]["tokens"] for x in s["steps"] if x["tier"] == "strong") for s in up) if up else None,
        "refuted": sum(bool(c.get("refuted_again")) for c in cands) / len(cands) if cands else None,
        "anchored": sum(c.get("repeat_of") == "cheap" for c in cands) / len(cands) if cands else None,
        "up": up,
    }


def section_e3(bugs, settings, p_step=None):
    runs = load("e3", bugs)
    out = ["## E3: what the handover note is worth (RQ4)", ""]
    if not runs:
        return "\n".join(out + ["No E3 sessions yet.", ""])
    names = {"step": "cold"}
    names.update({f"step+{h}": h for h in settings["e3"]["handovers"]})
    stats = {names[k]: strong_stats(v) for k, v in runs.items() if k in names}
    cold = stats.get("cold")
    tests, pvals = {}, ({"step vs strong-only": p_step} if p_step is not None else {})
    if cold:
        base = per_bug(cold["up"], lambda s: n_steps(s, "strong") / 1.0)
        nb = per_bug(cold["up"], lambda s: 1.0)
        for h, s in stats.items():
            if h == "cold":
                continue
            a, na = per_bug(s["up"], lambda x: n_steps(x, "strong") / 1.0), per_bug(s["up"], lambda x: 1.0)
            tests[h] = ratio_test({b: a[b] / na[b] for b in a}, {b: base[b] / nb[b] for b in base})
            pvals[h] = tests[h].get("p")
    adj = holm(pvals)
    body = []
    for h in ["cold"] + list(settings["e3"]["handovers"]):
        if h not in stats:
            continue
        s, t = stats[h], tests.get(h, {})
        body.append([h, s["sessions"], s["rounds"], pct(s["repaired"]), s["tokens"], pct(s["refuted"]),
                     pct(s["anchored"]), t.get("ratio"), t.get("p"), adj.get(h)])
    out += ["Sessions that step up under Step; the strong tier is started with each note.", "",
            table(["handover", "sessions", "strong rounds", "repaired within budget", "strong tokens",
                   "refuted-again rate", "anchoring rate", "rounds / cold", "p", "p (Holm)"], body), ""]
    if cold and len(stats) > 1:
        margin = settings["analysis"]["margin_pp"] / 100
        ok = {h: s for h, s in stats.items() if h != "cold" and s["repaired"] is not None
              and s["repaired"] >= cold["repaired"] - margin}
        per_rep = lambda s: (s["rounds"] * s["sessions"]) / max(1, repaired_bugs(s["up"]))  # noqa: E731
        best = min(ok, key=lambda h: per_rep(ok[h])) if ok else None
        pick = best if best and per_rep(ok[best]) < per_rep(cold) else "cold"
        out += [f"- rule for E4 (fewest strong rounds per repaired bug, repair rate within "
                f"{settings['analysis']['margin_pp']} points of cold; otherwise cold): **{pick}** "
                f"(set `e4.handover` in settings.json; now `{settings['e4']['handover']}`)", ""]
    return "\n".join(out)


# ---------------------------------------------------------------- E4 (RQ5)

def guard_tradeoff(sessions, settings):
    g = settings["budget_guard"]
    total = sum(cost(s, settings, "usd") for s in sessions) or 1.0
    n_pl = sum(s["plausible"] for s in sessions)
    rows = []
    for r in (1, 2, 3, 4, 5, 0):
        for budget in (None, g["budget"]):
            cuts = [cut(s, settings, r, budget) for s in sessions]
            saved = total - sum(cost(s, settings, "usd") for s in cuts)
            rows.append([str(r or "off"), str(budget or "off"), pct(saved / total),
                         n_pl - sum(s["plausible"] for s in cuts), n_pl])
    return rows


def fix_match(sessions):
    out = {}
    for tier in ("cheap", "strong"):
        pl = [c for s in sessions if s["plausible_tier"] == tier
              for c in s["steps"][-1]["candidates"] if c["verdict"] == "plausible"]
        out[tier] = (sum(bool(c.get("matches_fix")) for c in pl), len(pl))
    return out


def section_e4(bugs, settings):
    runs = load("e4", bugs)
    out = ["## E4: the whole system (RQ5)", ""]
    name = "step" if settings["e4"]["handover"] == "cold" else f"step+{settings['e4']['handover']}"
    bot, strong = runs.get(name, []), runs.get("strong-only", [])
    if not bot or not strong:
        return "\n".join(out + ["No E4 sessions yet.", ""])
    guarded = [cut(s, settings) for s in bot]
    rows = [summary_row("Strong-only, plain loop (cold path, no memory)", strong, settings, plain=True),
            summary_row("Strong-only, same validation layer", strong, settings),
            summary_row(f"bot: Step, handover {settings['e4']['handover']}, no stop", bot, settings),
            summary_row("bot with the Budget Guard (replayed)", guarded, settings)]
    out += [table(SUMMARY_HEAD, rows), ""]
    ra, rb = per_bug_rate(guarded), per_bug_rate(strong)
    mean, lo, ok = non_inferior([ra[b] - rb[b] for b in ra if b in rb], settings["analysis"]["margin_pp"])
    out.append(f"- repair rate, bot minus Strong-only: mean {fmt(mean)}, one-sided 95% lower bound {fmt(lo)}; "
               f"non-inferior at {settings['analysis']['margin_pp']} points: **{fmt(ok)}**")
    for u in ("usd", "seconds"):
        t = ratio_test(per_bug(guarded, lambda s, u=u: cost(s, settings, u)),
                       per_bug(strong, lambda s, u=u: cost(s, settings, u)))
        if t.get("n"):
            out.append(f"- {u} per bug, bot / Strong-only: {fmt(t['ratio'])} [{fmt(t['lo'])}, {fmt(t['hi'])}], "
                       f"p = {fmt(t['p'])}")
    failed_share = lambda ss: sum(cost(s, settings, "usd") for s in ss if not s["plausible"]) / \
        (sum(cost(s, settings, "usd") for s in ss) or 1.0)  # noqa: E731
    out.append(f"- cost of failed sessions: {pct(failed_share(guarded))} of the bot's spend, "
               f"{pct(failed_share(strong))} of Strong-only's")
    fm = fix_match(bot)
    out.append("- plausible patches equal to the developer's fix after normalising: "
               + ", ".join(f"{t} tier {k}/{n}" for t, (k, n) in fm.items()))
    out += ["", "### The Budget Guard replayed on the bot's sessions", "",
            table(["r", "budget", "share of USD saved", "plausible sessions lost", "plausible sessions"],
                  guard_tradeoff(bot, settings)), ""]
    reports = [s["report"] for s in bot if s.get("report")]
    if reports:
        out += ["Example give-up report:", "", "```", reports[0], "```", ""]
    return "\n".join(out)


# ---------------------------------------------------------------- development-set checks

def section_dev(bugs, settings, info):
    out = ["## Development-set checks (Table 5.6)", ""]
    e1, e2 = load("e1", bugs), load("e2", bugs)
    cheap, strong = e1.get("cheap-only", []), e1.get("strong-only", [])
    if cheap and strong:
        rc = per_bug_rate(cheap)
        share = st.mean(rc.values())
        failed = [b for b, r in rc.items() if r == 0]
        rounds = per_bug(strong, lambda s: n_steps(s, "strong") / 1.0)
        cnt = per_bug(strong, lambda s: 1.0)
        multi = [b for b in failed if b in rounds and rounds[b] / cnt[b] > 1]
        keep = 0.2 <= share <= 0.8 and (not failed or len(multi) >= len(failed) / 3)
        out.append(f"- model pair: the cheap tier repairs {pct(share)} of the bugs within its budget (rule: 20% to 80%); "
                   f"the strong tier needs more than one round on {len(multi)} of the {len(failed)} bugs the cheap "
                   f"tier never repairs (rule: at least a third): **{'keep the pair' if keep else 'see Section 5.1'}**")
        calls = [x for s in cheap for x in s["steps"] if not x.get("unvalidated")]
        if calls:
            distinct = st.mean(len({c["key"] for c in x["candidates"] if c["key"]}) / len(x["candidates"]) for x in calls)
            out.append(f"- candidates per cheap call: {pct(distinct)} of the candidates of a call are distinct "
                       f"after normalising (rule: at least half at the chosen n)")
        pl = [s for s in strong if s["plausible"]]
        if pl:
            times = sorted(sum(x["cost"]["seconds"] for x in s["steps"]) for s in pl)
            b90 = st.quantiles(times, n=100)[89] if len(times) > 1 else times[0]
            out.append(f"- session budget B: 90th percentile of the time to a plausible patch in the Strong-only "
                       f"sessions = {b90:.0f} s (set {settings['budget_guard']['budget']})")
            late = sum(n_steps(s, "strong") > 10 for s in pl) / len(pl)
            out.append(f"- strong budget R2: {pct(late)} of the repaired Strong-only sessions needed more than "
                       f"10 rounds (rule: under 5%, otherwise 15)")
    if cheap and "step" in e2:
        by = {(s["bug"], s["session"]): s for s in e2["step"]}
        won = [s for s in cheap if s["plausible"]]
        lost = [s for s in won if by.get((s["bug"], s["session"]), {}).get("plausible_tier") != "cheap"]
        if won:
            out.append(f"- stall rule of the cheap tier: Step gives up {pct(len(lost) / len(won))} of the cheap tier's "
                       f"repairs (rule: at most 5%, otherwise a call stalls only when no candidate makes progress)")
    for name in ("step", "strong-only"):
        ss = e2.get(name, [])
        pl = [s for s in ss if s["plausible"]]
        if not pl:
            continue
        pick = next((r for r in (1, 2, 3, 4, 5)
                     if sum(not cut(s, settings, r, None)["plausible"] for s in pl) <= 0.05 * len(pl)), None)
        out.append(f"- stall count r of the strong tier, on the {name} sessions: smallest r that loses at most "
                   f"5% of the plausible sessions = {pick or 'none of 1-5'} (set {settings['budget_guard']['r']})")
    if info.get("sd_log") is not None and not math.isnan(info["sd_log"]):
        sd, n = info["sd_log"], max(1, info.get("n_bugs") or 1)
        out.append(f"- SD of the per-bug log-ratio Step / Strong-only (USD): {fmt(sd)} (assumed at most 0.15); "
                   f"smallest saving detected with power 0.8 at {n} bugs: {pct(1 - math.exp(-2.8 * sd / math.sqrt(n)))}")
    if info.get("sd_rate") is not None:
        out.append(f"- SD of the per-bug repair-rate difference (equal attempts): {fmt(info['sd_rate'])} "
                   f"(assumed at most 0.24)")
    spent = [C.read_json(C.CACHE / "audit" / b / "_ledger.json", {}) for b in bugs]
    cpu, aud = sum(l.get("cpu", 0.0) for l in spent), sum(l.get("audits", 0) for l in spent)
    if aud:
        out.append(f"- audits: {aud} candidates, {cpu / 3600:.2f} CPU-hours over {len(bugs)} bugs "
                   f"({cpu / len(bugs) / 60:.1f} min per bug; cap {settings['runner']['cap_cpu_hours']} h)")
    if len(out) == 2:
        out.append("No sessions yet.")
    return "\n".join(out) + "\n"


def run(split, bugs, settings):
    e2, info = section_e2(bugs, settings)
    text = "\n".join([
        f"# Results ({split}, {len(bugs)} bugs, settings {C.settings_hash()})", "",
        section_dev(bugs, settings, info), section_e0(bugs, settings), section_e1(bugs, settings),
        e2, section_e3(bugs, settings, info.get("p_step")), section_e4(bugs, settings)])
    path = C.RESULTS / f"report-{split}.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    print(text)
    print(f"\n-> {path}")
