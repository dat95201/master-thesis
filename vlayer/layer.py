"""The validation layer: one call between the repair loop and the test runner.

    result = layer.validate(candidate, gen)
    result.verdict    # plausible, rejected, compile_error, unreadable or repeat
    result.feedback   # text to show the model in the next round
    result.go_on      # false when the session should stop
    result.report     # the give-up report, when the session stops without a patch

Two runners execute the ordered tests:
  * LiveRunner really runs the classes in order and stops at the first
    failure (how the layer is used in a CI bot; `vlayer repair`).
  * AuditedRunner audits the patch once (every class, cached) and charges the
    ordering from the audit (how E2/E3 compare configurations fairly).
"""

from dataclasses import dataclass, field
from pathlib import Path

from . import audit as A
from . import d4j
from . import javasrc
from .budget_guard import BudgetGuard, give_up_report, round_cost, signature
from .bugs import ensure_checkout
from .repeat_guard import RepeatGuard

UNREADABLE = ("Your answer could not be read as the given method(s). "
              "Return the complete fixed method(s) in one Java code block.")


@dataclass
class Result:
    verdict: str
    feedback: str
    go_on: bool
    report: str = None
    record: dict = field(default_factory=dict)


def _fail_detail(info, cls, rec):
    return {"class": cls, "test": rec.get("test", "-"), "exc": rec.get("exc", ""),
            "line": rec.get("line", 0), "msg": rec.get("msg", "")}


class AuditedRunner:
    def __init__(self, info, settings, phase="live", cap_hours=None):
        self.info, self.settings = info, settings
        self.unit = settings["runner"]["cost_unit"]
        self.phase, self.cap = phase, cap_hours

    def run(self, methods, ordering):
        audit = A.audit_methods(self.info, methods, self.settings, self.phase, self.cap)
        if ordering.needs_audit:
            prefix = self.info["triggering_classes"] if "+trigger" in ordering.name else ()
            order, stop = A.oracle_order(audit, prefix, self.unit), None
        else:
            order, stop = ordering.order(in_faulty=True, key=audit["key"])
        out = A.charge(audit, order, stop, self.unit)
        out["compile_error"] = audit.get("compile_error", "")
        out["fail"] = _fail_detail(self.info, out["fail"], audit["classes"][out["fail"]]) if out["fail"] else None
        # facts used only by the analysis (headroom, trigger-pass subset)
        oracle = A.charge(audit, A.oracle_order(audit, (), self.unit), None, self.unit)
        trig = self.info["triggering_classes"]
        out["extra"] = {
            "audit_key": audit["key"],
            "oracle_cost": oracle["cost"],
            "trigger_pass": audit["compiled"] and all(
                audit["classes"].get(c, {}).get("status") == "pass" for c in trig),
            "audit_plausible": A.plausible(audit),
        }
        return out


class LiveRunner:
    def __init__(self, info, settings):
        self.info, self.settings = info, settings
        self.unit = settings["runner"]["cost_unit"]

    def run(self, methods, ordering):
        info = self.info
        w = ensure_checkout(info, "b")
        src = w / info["src_file"]
        original = src.read_text(errors="replace")
        top = info["modified_class"].split("$")[0]
        order, stop = ordering.order(in_faulty=True, key=javasrc.candidate_key(methods))
        classes = order[:stop] if stop is not None else order
        try:
            src.write_text(javasrc.replace_methods(original, methods))
            d4j.drop_class_files(w / info["bin_dir"], top)
            ok, cw, cc, err = d4j.compile_project(w)
            out = {"verdict": "compile_error", "fail": None, "run": [], "compile_error": err,
                   "cost": {"wall": cw, "cpu": cc}, "tests": {"wall": 0.0, "cpu": 0.0},
                   "overhead": {"wall": cw, "cpu": cc}, "extra": {}}
            if not ok:
                return out
            recs, jvm = d4j.run_classes(w, info["cp_test"]["b"], classes, "stop",
                                        self.settings["runner"]["class_timeout_s"])
            for u in ("wall", "cpu"):
                out["cost"][u] += jvm[u]
                out["overhead"][u] += jvm[u]
            out["verdict"] = "plausible"
            for c in classes:
                if c not in recs:
                    break
                r = recs[c]
                bad = r["status"] != "pass"
                for u in ("wall", "cpu"):
                    out["cost"][u] += r[u]
                    out["tests"][u] += r[u]
                out["run"].append((c, bad, r[self.unit]))
                if bad:
                    out["verdict"], out["fail"] = "rejected", _fail_detail(info, c, r)
                    break
            return out
        finally:
            src.write_text(original)
            d4j.drop_class_files(w / info["bin_dir"], top)


class ValidationLayer:
    def __init__(self, info, ordering, runner, settings, repeat_guard: RepeatGuard = None,
                 budget_guard: BudgetGuard = None):
        self.info, self.ordering, self.runner = info, ordering, runner
        self.settings = settings
        self.repeat_guard = repeat_guard
        self.budget_guard = budget_guard
        self.r_max = settings["live"]["rounds"]
        self.rounds = []

    # ------------------------------------------------------------ feedback
    def _test_line(self, cls, line):
        if not line:
            return ""
        f = Path(ensure_checkout(self.info, "b")) / self.info["test_dir"] / (
            cls.split("$")[0].replace(".", "/") + ".java")
        try:
            return f.read_text(errors="replace").splitlines()[line - 1].strip()
        except (OSError, IndexError):
            return ""

    def _feedback(self, out):
        if out["verdict"] == "compile_error":
            return (f"Your fix does not compile:\n{out['compile_error']}\n"
                    f"Please provide a corrected version."), f"it does not compile: {out['compile_error'][:200]}"
        f = out["fail"]
        simple = f["class"].rsplit(".", 1)[-1]
        src = self._test_line(f["class"], f["line"])
        where = f"  (line {f['line']}: {src})" if src else ""
        err = f"{f['exc'].rsplit('.', 1)[-1]}: {f['msg']}".rstrip(": ")
        text = (f"Your fix is still incorrect. It fails this test:\n"
                f"    Test:  {simple}::{f['test']}{where}\n"
                f"    Error: {err}\n"
                f"Please provide a different fix.")
        return text, f"test {simple}::{f['test']} fails with {err}"

    # ------------------------------------------------------------ main call
    def validate(self, methods, gen: dict) -> Result:
        n = len(self.rounds) + 1
        key, repeat, out = None, False, None
        val = {"wall": 0.0, "cpu": 0.0}
        val_if_uncached = None
        if methods is None:
            verdict, feedback = "unreadable", UNREADABLE
            sig = signature("unreadable")
        else:
            key = javasrc.candidate_key(methods)
            hit = self.repeat_guard.lookup(key) if self.repeat_guard else None
            if hit:
                repeat = True
                stored = hit[1]
                verdict, sig = stored["verdict"], stored["signature"]
                val_if_uncached = stored["cost"]
                feedback = self.repeat_guard.feedback(key, stored["feedback"])
                out = {"fail": stored["fail"], "extra": stored["extra"]}
            else:
                out = self.runner.run(methods, self.ordering)
                self.ordering.observe(out["run"])
                verdict = out["verdict"]
                val = dict(out["cost"])
                sig = signature(verdict, out["fail"], out.get("compile_error", ""))
                feedback, why = ("", "") if verdict == "plausible" else self._feedback(out)
                if self.repeat_guard:
                    self.repeat_guard.store(key, n, {
                        "verdict": verdict, "signature": sig, "feedback": feedback, "why": why,
                        "cost": val, "fail": out["fail"], "extra": out.get("extra", {})})

        cost = round_cost(gen, val, self.settings["prices"])
        if self.budget_guard:
            go_on, reason = self.budget_guard.step(cost, sig, repeat, verdict == "plausible")
        else:
            go_on = verdict != "plausible" and n < self.r_max
            reason = "" if go_on else ("plausible patch found" if verdict == "plausible"
                                       else f"round limit of {self.r_max} reached")
        record = {
            "round": n, "key": key, "repeat": repeat, "verdict": verdict,
            "signature": list(sig), "gen": gen, "val": val, "cost": cost,
            "val_if_uncached": val_if_uncached,
            "tests": out.get("tests") if out and not repeat else None,
            "overhead": out.get("overhead") if out and not repeat else None,
            "fail": out.get("fail") if out else None,
            "extra": out.get("extra", {}) if out else {},
        }
        if self.budget_guard and self.budget_guard.would_stop:
            record["would_stop"] = list(self.budget_guard.would_stop)
        self.rounds.append(record)
        report = None
        if not go_on and verdict != "plausible":
            report = give_up_report(self.info["bug"], 0, self.rounds, reason)
        shown = "repeat" if repeat else verdict
        return Result(shown, feedback, go_on, report, record)
