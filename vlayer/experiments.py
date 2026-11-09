"""E1 (orderings on mutants), E2 (orderings, live) and E3 (whole layer, live)."""

import random
import traceback
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

from . import audit as A
from . import config as C
from . import d4j
from . import javasrc
from . import orderings
from .bugs import ensure_checkout, load_info
from .llm import LLM
from .loop import run_session


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
    except Exception as e:  # noqa: BLE001
        traceback.print_exc()
        return f"  {bug}: ERROR {e}"


# ================================================================ E1: mutants

def _lines(text, start, end):
    return text.count("\n", 0, start) + 1, text.count("\n", 0, end) + 1


def _spans(text, keys=None, exclude=None):
    out = []
    for m in javasrc.find_methods(text):
        if (keys is None or m.key in keys) and (exclude is None or m.key not in exclude):
            out.append(_lines(text, m.start, m.end))
    return out


def _in(line, spans):
    return any(a <= line <= b for a, b in spans)


def mutants_for_bug(bug, settings):
    """Generate mutants in four groups and audit them until each group has
    `mutants_per_group` that compile and fail at least one test."""
    out_path = C.DATA / "bugs" / bug / "mutants.json"
    done = C.read_json(out_path)
    if done is not None:
        return f"  {bug}: mutants already done"
    info = load_info(bug)
    e1 = settings["e1"]
    cap = e1["cap_cpu_hours"]
    rng = random.Random(f"{settings['seed']}-{bug}")
    keys = [f["key"] for f in info["faulty"]]
    wb, wf = ensure_checkout(info, "b"), ensure_checkout(info, "f")
    mdir = C.WORK / bug / "mutants"

    def gen(w, version, rel, tag):
        return d4j.generate_mutants(w, w / rel, info["cp_compile"][version] + ":" + str(w / info["bin_dir"]),
                                    mdir / tag)

    fixed_text = (wf / info["src_file"]).read_text(errors="replace")
    buggy_text = (wb / info["src_file"]).read_text(errors="replace")
    m_fixed = gen(wf, "f", info["src_file"], "fixed")
    m_buggy = gen(wb, "b", info["src_file"], "buggy")
    groups = {
        "S1": ("f", None, [m for m in m_fixed if _in(m["line"], _spans(fixed_text, keys))]),
        "S2": ("b", None, [m for m in m_buggy if _in(m["line"], _spans(buggy_text, keys))]),
        "S3": ("f", None, [m for m in m_fixed if _in(m["line"], _spans(fixed_text, exclude=keys))]),
    }
    # S4: another class loaded by the failing tests
    src_dir = info["src_file"][: -len(info["modified_class"].split("$")[0].replace(".", "/") + ".java")]
    others = [c for c in info["loaded_classes"] if c != info["modified_class"] and "$" not in c]
    rng.shuffle(others)
    for other in others:
        rel = f"{src_dir}{other.replace('.', '/')}.java"
        if (wf / rel).exists():
            ms = gen(wf, "f", rel, "other")
            if ms:
                groups["S4"] = ("f", (rel, other), ms)
                break

    result = {"groups": {}, "fix": None}
    for g, (version, other, pool) in groups.items():
        pool = pool[:]
        rng.shuffle(pool)
        chosen, audited = [], 0
        for m in pool:
            if len(chosen) >= e1["mutants_per_group"] or audited >= e1["max_audits_per_group"]:
                break
            text = Path(m["path"]).read_text(errors="replace")
            tag = "b" if version == "b" else ("o-" + other[1] if other else "f")
            key = f"mut-{tag}-{m['id']}"
            kw = {"rel_src": other[0], "fqcn": other[1]} if other else {}
            a = A.audit_file(info, version, text, key, settings, "e1", cap, **kw)
            audited += 1
            if a["compiled"] and not A.plausible(a):
                chosen.append({"key": key, "id": m["id"], "op": m["op"], "line": m["line"]})
        result["groups"][g] = {"chosen": chosen, "audited": audited, "pool": len(pool)}
    fix = A.audit_methods(info, info["fixed_methods"], settings, "e1", cap)
    result["fix"] = fix["key"]
    C.write_json(out_path, result)
    d4j.cleanup(mdir)
    sizes = {g: len(v["chosen"]) for g, v in result["groups"].items()}
    return f"  {bug}: mutants {sizes}, fix plausible={A.plausible(fix)}"


def _session_cost(info, settings, name, trigger, patches, fix, seed):
    """Validation cost of one simulated session under one ordering."""
    unit = settings["runner"]["cost_unit"]
    o = orderings.make(name, info, settings, random.Random(f"{settings['seed']}-{info['bug']}-{seed}"), trigger)
    tot = {"rej_wall": 0.0, "rej_cpu": 0.0, "rej_tests_cpu": 0.0, "rej_tests_wall": 0.0,
           "acc_wall": 0.0, "acc_cpu": 0.0, "unsafe": 0}
    for audit, in_faulty in patches + [(fix, True)]:
        if o.needs_audit:
            prefix = info["triggering_classes"] if trigger else ()
            order, stop = A.oracle_order(audit, prefix, unit), None
        else:
            order, stop = o.order(in_faulty, key=audit["key"])
        ch = A.charge(audit, order, stop, unit)
        o.observe(ch["run"])
        if audit is fix:
            tot["acc_wall"] += ch["cost"]["wall"]
            tot["acc_cpu"] += ch["cost"]["cpu"]
        else:
            tot["rej_wall"] += ch["cost"]["wall"]
            tot["rej_cpu"] += ch["cost"]["cpu"]
            tot["rej_tests_wall"] += ch["tests"]["wall"]
            tot["rej_tests_cpu"] += ch["tests"]["cpu"]
            tot["unsafe"] += int(ch["verdict"] == "plausible")
    return tot


def simulate_bug(bug, settings, names):
    info = load_info(bug)
    mut = C.read_json(C.DATA / "bugs" / bug / "mutants.json")
    if mut is None:
        return f"  {bug}: no mutants (run `vlayer mutants` first)"
    load = lambda k: C.read_json(C.CACHE / "audit" / bug / f"{k}.json")  # noqa: E731
    fix = load(mut["fix"])
    if fix is None or not A.plausible(fix):
        return f"  {bug}: developer fix is not plausible in the audit; skipped"
    pats = {g: [(load(m["key"]), g in ("S1", "S2")) for m in v["chosen"]]
            for g, v in mut["groups"].items()}
    kinds = {"main": pats.get("S1", []) + pats.get("S2", []),
             "check": sum(pats.values(), [])}
    n = settings["e1"]["sessions_per_bug"]
    rows = []
    for kind, patches in kinds.items():
        if not patches:
            continue
        rng = random.Random(f"{settings['seed']}-{bug}-{kind}")
        perms = [rng.sample(patches, len(patches)) for _ in range(n)]
        for name in names:
            for trigger in (False, True):
                tots = [_session_cost(info, settings, name, trigger, p, fix, s)
                        for s, p in enumerate(perms)]
                row = {k: sum(t[k] for t in tots) / n for k in tots[0]}
                row.update({"bug": bug, "kind": kind, "ordering": name, "trigger": trigger,
                            "n_patches": len(patches)})
                rows.append(row)
    C.write_json(C.RESULTS / "e1" / f"{bug}.json", rows)
    return f"  {bug}: simulated {len(rows)} rows"


def run_mutants(bugs, settings, jobs=1):
    _for_each_bug(mutants_for_bug, bugs, jobs, settings)


def run_e1(bugs, settings, jobs=1, variants=True):
    names = orderings.MAIN + (orderings.VARIANTS if variants else [])
    _for_each_bug(simulate_bug, bugs, jobs, settings, names)


# ================================================================ E2 / E3: live

def e2_arms(settings):
    return {a: {"ordering": a, "cache": True, "notice": False, "guard": "off"}
            for a in settings["live"]["e2_arms"]}


E3_ARMS = {"layer": {"ordering": "arc", "cache": True, "notice": True, "guard": "log"}}


def live_bug(bug, settings, exp, arms):
    info = load_info(bug)
    llm = LLM(settings)
    out_dir = C.RESULTS / exp / bug
    done = 0
    for s in range(settings["live"]["sessions"]):
        for name, arm in arms.items():
            path = out_dir / f"{name}-s{s}.json"
            if path.exists():
                continue
            res = run_session(info, settings, arm, s, llm)
            C.write_json(path, res)
            done += 1
    return f"  {bug}: {exp} sessions run {done}"


def run_live(exp, bugs, settings, jobs=1):
    arms = e2_arms(settings) if exp == "e2" else E3_ARMS
    _for_each_bug(live_bug, bugs, jobs, settings, exp, arms)
