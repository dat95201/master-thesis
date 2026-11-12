"""Audits: every distinct candidate is compiled once and run on every
scheduled test class, on the cold path and on the warm path (experiment E0).

From the audit, the validation cost of any policy on that candidate is
*charged*: compilation, start-up where it applies, and the classes run up to
the first failure in the order the session would use. All policies are thus
charged from the same measurement.

An audit also fixes what is reported about a candidate. A candidate may fail
several classes, and the order decides which failure is met first; so that
what a model is told depends on its candidate alone, the reported failure is
the first failing class in the static order (substrate.scheduled).

Audits are cached in cache/audit/<bug>/<key>.json and shared by every
experiment. The verdict of record is the cold one (settings.substrate.reference).
"""

import hashlib
import random

from . import config as C
from . import d4j
from . import javasrc
from .bugs import ensure_checkout
from .substrate import UNITS, Substrate, scheduled

PATHS = ("cold", "warm")


class CapReached(Exception):
    """The bug used up its compute cap; it is reported and dropped."""


def audit_path(bug, key):
    return C.CACHE / "audit" / bug / f"{key}.json"


def _ledger_path(bug):
    return C.CACHE / "audit" / bug / "_ledger.json"


def _spend(bug, seconds, cap_hours):
    led = C.read_json(_ledger_path(bug), {})
    led["cpu"] = led.get("cpu", 0.0) + seconds
    led["audits"] = led.get("audits", 0) + 1
    C.write_json(_ledger_path(bug), led)
    if cap_hours and led["cpu"] > cap_hours * 3600:
        raise CapReached(f"{bug}: audit cap of {cap_hours} CPU-hours reached")


def _sampled(key, fraction):
    return int(hashlib.sha256(key.encode()).hexdigest()[:8], 16) / 0xFFFFFFFF < fraction


class Auditor:
    """Audits the candidates of one bug. It holds the bug's substrate, so one
    live runtime serves many candidates, as it does in a session."""

    def __init__(self, info: dict, settings: dict):
        self.info, self.settings = info, settings
        self.sub = Substrate(info, settings)
        self.paths = [p for p in settings["substrate"]["paths"] if p in PATHS]
        if "cold" in self.paths and not d4j.available():
            raise RuntimeError(f"the cold path needs the Defects4J command line, which {d4j.status()} "
                               "(run setup.sh); or set substrate.paths to [\"warm\"] in settings.json")
        self.original = None

    def close(self):
        self.sub.close()

    def audit(self, methods: dict) -> dict:
        """Audit a candidate given as {method key: new text}."""
        info, rs = self.info, self.settings["runner"]
        key = javasrc.candidate_key(methods)
        path = audit_path(info["bug"], key)
        audit = C.read_json(path)
        if audit is not None and all(p in audit for p in self.paths):
            return audit
        if self.original is None:
            self.original, _ = javasrc.read_source(ensure_checkout(info, "b") / info["src_file"])
        text = javasrc.replace_methods(self.original, methods)
        audit = audit or {"key": key, "bug": info["bug"], "methods": methods,
                          "classes": list(self.sub.classes)}
        spent = 0.0
        if "cold" in self.paths and "cold" not in audit:
            audit["cold"] = self.sub.cold(text, then=lambda out, rerun: self._checks(key, out, rerun))
            spent += _cpu(audit["cold"])
        if "warm" in self.paths and "warm" not in audit:
            if self.sub.warm_ok():
                audit["warm"] = self.sub.warm(text, key)
                spent += _cpu(audit["warm"])
            elif "cold" in self.paths:      # not trusted for this bug: it falls back to the cold path
                audit["warm"] = {"fallback": self.sub.baseline["reason"]}
            else:
                raise RuntimeError(f"the warm path cannot be used for {info['bug']} "
                                   f"({self.sub.baseline['reason']}) and the cold path is off")
        C.write_json(path, audit)
        _spend(info["bug"], spent, rs.get("cap_cpu_hours"))
        return audit

    def _checks(self, key, cold, rerun):
        """Two checks made while the patched build is in place."""
        classes = list(self.sub.classes)
        if _sampled(key, self.settings["runner"]["order_check_fraction"]):
            # outcomes must not depend on the order: run reversed and shuffled
            shuffled = classes[:]
            random.Random(key).shuffle(shuffled)
            diffs = set()
            for order in (classes[::-1], shuffled):
                again = rerun(order)
                diffs |= {c for c in classes if failed(cold["classes"].get(c)) != failed(again.get(c))}
            cold["order_check"] = {"consistent": not diffs, "classes": sorted(diffs)}
        if verdict(cold) == "plausible" and self.settings["substrate"]["confirm_plausible"]:
            # skipping the classes that never reach the faulty methods must not hide a failure
            rest = [c for c in self.info["suite"] if c not in set(classes)]
            again = rerun(rest) if rest else {}
            bad = sorted(c for c in rest if failed(again.get(c)))
            cold["whole_suite"] = {"passed": not bad, "failing": bad}


def _cpu(view) -> float:
    return view["compile"]["cpu"] + view.get("jvm", {}).get("cpu", 0.0) \
        + sum(r["cpu"] for r in view["classes"].values())


# ---------------------------------------------------------------- reading an audit

def failed(rec) -> bool:
    return rec is None or rec["status"] != "pass"


def killed(rec) -> bool:
    """Did this class hang or kill the JVM it ran in?"""
    return rec is not None and (rec["status"] == "timeout" or rec.get("exc") == "JVMCrash")


def usable(view) -> bool:
    return view is not None and "fallback" not in view


def verdict(view) -> str:
    """plausible, rejected or compile_error, on one path."""
    if not view["compiled"]:
        return "compile_error"
    if any(failed(r) for r in view["classes"].values()):
        return "rejected"
    return "plausible"


def reference(audit: dict, settings: dict = None) -> dict:
    """The view whose verdict is the verdict of record: the cold path when it
    was run, otherwise the warm path."""
    prefer = (settings or {}).get("substrate", {}).get("reference", "cold")
    for p in (prefer,) + PATHS:
        if usable(audit.get(p)):
            return audit[p]
    raise KeyError(f"audit {audit.get('key')} has no usable path")


def record_verdict(audit: dict, settings: dict = None) -> str:
    v = verdict(reference(audit, settings))
    if v == "plausible" and not audit.get("cold", {}).get("whole_suite", {}).get("passed", True):
        return "rejected"       # a skipped class fails: the selection was not safe here
    return v


def reported_failure(audit: dict, info: dict, settings: dict = None):
    """What is reported about a rejected candidate: its first failing class
    in the static order, with the first failing test of that class."""
    view = reference(audit, settings)
    if not view["compiled"]:
        return None
    for c in audit.get("classes") or scheduled(info):
        r = view["classes"].get(c)
        if failed(r):
            r = r or d4j.crashed_record("the class did not run")
            return {"class": c, "test": r.get("test", "-"), "exc": r.get("exc", ""),
                    "line": r.get("line", 0), "msg": r.get("msg", "")}
    extra = audit.get("cold", {}).get("whole_suite", {}).get("failing") or []
    if extra:
        return {"class": extra[0], "test": "-", "exc": "", "line": 0,
                "msg": "this class fails; it is outside the scheduled classes"}
    return None


def agree(audit: dict) -> bool:
    """Do the two paths give the same verdict and the same failing classes?"""
    cold, warm = audit.get("cold"), audit.get("warm")
    if not usable(cold) or not usable(warm):
        return True
    if verdict(cold) != verdict(warm):
        return False
    return {c for c, r in cold["classes"].items() if failed(r)} == \
           {c for c, r in warm["classes"].items() if failed(r)}


# ---------------------------------------------------------------- charging

def zero() -> dict:
    return {"wall": 0.0, "cpu": 0.0, "build": {"wall": 0.0, "cpu": 0.0},
            "start": {"wall": 0.0, "cpu": 0.0}, "test": {"wall": 0.0, "cpu": 0.0}}


def charge(audit: dict, path: str, order: list):
    """What validating the audited candidate costs on `path` when the classes
    run in `order` and the run stops at the first failing class.

    Returns {wall, cpu, build, start, test, fail, ran}: `build` is the
    project's build (cold) or the one-file compilation (warm); `start` is the
    JVM start-up of the candidate (cold) or of the runtime that replaces one
    the candidate hung or killed (warm); `test` the classes that ran; `fail`
    the class that stopped the run. A bug whose warm path is not trusted is
    charged at the cold price. None if the path was not audited.
    """
    view = audit.get(path)
    if path == "warm" and view is not None and not usable(view):
        view = audit.get("cold")
    if not usable(view):
        return None
    out = zero()
    out.update({"fail": None, "ran": 0})
    for u in UNITS:
        out["build"][u] = view["compile"][u]
    if view["compiled"]:
        if "jvm" in view:
            for u in UNITS:
                out["start"][u] = view["jvm"][u]
        for c in order:
            r = view["classes"].get(c)
            if r is None:
                continue
            out["ran"] += 1
            for u in UNITS:
                out["test"][u] += r[u]
            if failed(r):
                out["fail"] = c
                break
        if out["fail"] and view.get("restarts") and killed(view["classes"][out["fail"]]):
            for u in UNITS:
                out["start"][u] += view["restart"][u] / view["restarts"]
    for u in UNITS:
        out[u] = out["build"][u] + out["start"][u] + out["test"][u]
    return out


def best_order_cost(audit: dict, path: str, unit="cpu") -> float:
    """Test time of the cheapest rejection in hindsight: the least any order
    of the scheduled classes can pay for this candidate."""
    view = audit.get(path)
    if not usable(view) or not view["compiled"]:
        return 0.0
    fails = [r[unit] for r in view["classes"].values() if failed(r)]
    return min(fails) if fails else sum(r[unit] for r in view["classes"].values())
