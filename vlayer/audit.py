"""Audits: every distinct patch is compiled once and run on every test class.

The cost of any ordering on that patch is then *charged* from the audit:
compilation, JVM start-up, and the classes the ordering would run up to its
first failure. All orderings are charged from the same measurement.
Audits are cached in cache/audit/<bug>/<key>.json and shared by E1, E2, E3.
"""

import hashlib
import random

from . import config as C
from . import d4j
from . import javasrc
from .bugs import ensure_checkout


class CapReached(Exception):
    """The bug used up its compute cap; it is reported and dropped."""


def _ledger_path(bug):
    return C.CACHE / "audit" / bug / "_ledger.json"


def _spend(bug, phase, seconds, cap_hours):
    led = C.read_json(_ledger_path(bug), {})
    led[phase] = led.get(phase, 0.0) + seconds
    C.write_json(_ledger_path(bug), led)
    if cap_hours and led[phase] > cap_hours * 3600:
        raise CapReached(f"{bug}: {phase} cap of {cap_hours} CPU-hours reached")


def audit_file(info, version, new_text, key, settings, phase="live", cap_hours=None,
               rel_src=None, fqcn=None):
    """Audit a whole replacement source file on the given checkout ('b' or 'f').

    By default the file is the modified class; E1 group S4 passes another class.
    """
    path = C.CACHE / "audit" / info["bug"] / f"{key}.json"
    cached = C.read_json(path)
    if cached is not None:
        return cached
    rs = settings["runner"]
    w = ensure_checkout(info, version)
    src = w / (rel_src or info["src_file"])
    original = src.read_text(errors="replace")
    top = (fqcn or info["modified_class"]).split("$")[0]
    bin_dir = w / info["bin_dir"]
    try:
        src.write_text(new_text)
        d4j.drop_class_files(bin_dir, top)
        ok, cw, cc, err = d4j.compile_project(w)
        audit = {"key": key, "version": version, "compiled": ok,
                 "compile": {"wall": cw, "cpu": cc}, "compile_error": err,
                 "jvm": {"wall": 0.0, "cpu": 0.0}, "classes": {}}
        spent = cc
        if ok:
            recs, jvm = d4j.run_classes(w, info["cp_test"][version], info["suite"], "all",
                                        rs["class_timeout_s"])
            audit["jvm"] = jvm
            audit["classes"] = {c: {k: recs[c][k] for k in ("status", "wall", "cpu", "test", "exc", "line", "msg")}
                                for c in info["suite"] if c in recs}
            spent += jvm["cpu"] + sum(r["cpu"] for r in recs.values())
            if _sampled(key, rs["order_check_fraction"]):
                audit["order_check"] = _order_check(w, info, version, audit, rs)
    finally:
        src.write_text(original)
        d4j.drop_class_files(bin_dir, top)
    C.write_json(path, audit)
    _spend(info["bug"], phase, spent, cap_hours)
    return audit


def audit_methods(info, methods: dict, settings, phase="live", cap_hours=None):
    """Audit a candidate given as {method key: new text} on the faulty program."""
    key = javasrc.candidate_key(methods)
    path = C.CACHE / "audit" / info["bug"] / f"{key}.json"
    cached = C.read_json(path)
    if cached is not None:
        return cached
    w = ensure_checkout(info, "b")
    original = (w / info["src_file"]).read_text(errors="replace")
    new_text = javasrc.replace_methods(original, methods)
    return audit_file(info, "b", new_text, key, settings, phase, cap_hours)


def _sampled(key, fraction):
    return int(hashlib.sha256(key.encode()).hexdigest()[:8], 16) / 0xFFFFFFFF < fraction


def _order_check(w, info, version, audit, rs):
    """Run the suite twice more, reversed and shuffled; outcomes must agree."""
    suite = list(info["suite"])
    shuffled = suite[:]
    random.Random(audit["key"]).shuffle(shuffled)
    diffs = []
    for order in (suite[::-1], shuffled):
        recs, _ = d4j.run_classes(w, info["cp_test"][version], order, "all", rs["class_timeout_s"])
        for c in suite:
            a = audit["classes"].get(c, {}).get("status") == "pass"
            b = recs.get(c, {}).get("status") == "pass"
            if a != b:
                diffs.append(c)
    return {"consistent": not diffs, "classes": sorted(set(diffs))}


# ---------------------------------------------------------------- charging

def failed(rec) -> bool:
    return rec["status"] != "pass"


def charge(audit, order, stop_after=None, unit="cpu"):
    """Cost of validating the audited patch with the given class order.

    stop_after: if set, the patch is declared plausible once the first
    `stop_after` classes pass (Coverage skips classes that cannot reach the
    change). Returns a dict with verdict, first failing class, the classes
    run as (class, failed, cost) and the cost in wall and CPU seconds.
    """
    cost = {"wall": audit["compile"]["wall"], "cpu": audit["compile"]["cpu"]}
    out = {"verdict": "compile_error", "fail": None, "run": [], "cost": cost,
           "tests": {"wall": 0.0, "cpu": 0.0}, "overhead": dict(cost), "unsafe": False}
    if not audit["compiled"]:
        return out
    for u in ("wall", "cpu"):
        cost[u] += audit["jvm"][u]
        out["overhead"][u] += audit["jvm"][u]
    out["verdict"] = "plausible"
    for i, c in enumerate(order):
        if stop_after is not None and i >= stop_after:
            break
        r = audit["classes"][c]
        for u in ("wall", "cpu"):
            cost[u] += r[u]
            out["tests"][u] += r[u]
        out["run"].append((c, failed(r), r[unit]))
        if failed(r):
            out["verdict"], out["fail"] = "rejected", c
            return out
    out["unsafe"] = any(failed(r) for r in audit["classes"].values())
    return out


def oracle_order(audit, prefix=(), unit="cpu"):
    """The cheapest order in hindsight (optionally after a fixed prefix)."""
    cls = audit["classes"]
    fails = sorted((c for c in cls if failed(cls[c])), key=lambda c: cls[c][unit])
    pre = list(prefix)
    pre_fail = [c for c in fails if c in pre]
    if pre_fail:
        return [pre_fail[0]]
    rest = [c for c in cls if c not in pre]
    rest_fail = [c for c in fails if c not in pre]
    head = pre + rest_fail[:1]
    return head + [c for c in rest if c not in head]


def plausible(audit) -> bool:
    return audit["compiled"] and not any(failed(r) for r in audit["classes"].values())
