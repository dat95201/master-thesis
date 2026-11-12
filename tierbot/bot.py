"""The tiered repair bot: four modules behind one call.

    bot.begin_call(gen)                 # a model call of the current tier starts
    result = bot.validate(candidate)
    result.verdict    # plausible, rejected, compile_error, unreadable or repeat
    result.evidence   # the failing test and its failure signature
    result.next       # same (tier), step_up or stop
    result.note       # the handover note, when next is step_up
    result.report     # the give-up report, when next is stop without a patch

Every candidate of either tier passes through the Evidence Memory (M1) and
the Warm Substrate (M0); the Escalation Controller (M2) decides which tier
proposes next; the Budget Guard (M3) may stop the session.

Two validators run the tests:
  * AuditedValidator audits the candidate once on the cold and on the warm
    path (cached) and charges the session's schedule from the audit. The
    experiments use it, so that every policy is charged from one measurement.
  * LiveValidator really runs the scheduled classes on the warm path and
    stops at the first failure: how the bot runs in CI (`tierbot repair --live`).
"""

from dataclasses import dataclass, field

from . import audit as A
from . import config as C
from . import javasrc
from . import prompts
from .budget import BudgetGuard, call_cost, give_up_report
from .bugs import ensure_checkout
from .controller import Controller, Policy, handover_note
from .memory import EvidenceMemory
from .substrate import Substrate


@dataclass
class Result:
    verdict: str
    evidence: dict
    next: str
    note: str = None
    report: str = None
    feedback: str = ""
    reason: str = ""
    record: dict = field(default_factory=dict)


# ---------------------------------------------------------------- validators

class AuditedValidator:
    def __init__(self, info, settings):
        self.info, self.settings = info, settings
        self.auditor = A.Auditor(info, settings)

    def validate(self, methods: dict, order: list) -> dict:
        audit = self.auditor.audit(methods)
        v = A.record_verdict(audit, self.settings)
        ref = A.reference(audit, self.settings)
        return {"verdict": v,
                "fail": A.reported_failure(audit, self.info, self.settings) if v == "rejected" else None,
                "compile_error": ref["compile_error"] if v == "compile_error" else "",
                "cost": {p: A.charge(audit, p, order) for p in A.PATHS},
                "agree": A.agree(audit)}

    def session_start(self) -> dict:
        """What a session pays once on the warm path: starting the runtime
        and running the scheduled tests of the faulty program in it."""
        base = self.auditor.sub.baseline
        return dict(base["start"]) if base and base["ok"] else {"wall": 0.0, "cpu": 0.0}

    def close(self):
        self.auditor.close()


class LiveValidator:
    def __init__(self, info, settings):
        self.info, self.settings = info, settings
        self.sub = Substrate(info, settings)
        self.original, _ = javasrc.read_source(ensure_checkout(info, "b") / info["src_file"])
        self.path = "warm" if self.sub.warm_ok() else "cold"
        if self.path == "cold":
            print(f"    warm path not trusted for {info['bug']} ({self.sub.baseline['reason']}); using the cold path")

    def _run(self, text, tag, classes):
        if self.path == "warm":
            return self.sub.warm(text, tag, classes, "stop")
        return self.sub.cold(text, classes, "stop")

    def validate(self, methods: dict, order: list) -> dict:
        key = javasrc.candidate_key(methods)
        text = javasrc.replace_methods(self.original, methods)
        view = self._run(text, key, order)
        cost = A.charge({self.path: view}, self.path, order)
        v = A.verdict(view)
        fail = None
        if v == "plausible" and self.settings["substrate"]["confirm_plausible"]:
            # the whole-suite run that any proposed patch receives in CI: it can
            # refute the candidate, but it is not charged to the session
            rest = [c for c in self.info["suite"] if c not in set(order)]
            more = self._run(text, key + "-rest", rest) if rest else None
            if more is not None:
                extra = A.charge({self.path: more}, self.path, rest)
                cost["confirm"] = {u: extra[u] for u in ("wall", "cpu")}
                if extra["fail"]:
                    v, view, cost["fail"] = "rejected", more, extra["fail"]
        if v == "rejected":
            r = view["classes"][cost["fail"]]
            fail = {"class": cost["fail"], "test": r.get("test", "-"), "exc": r.get("exc", ""),
                    "line": r.get("line", 0), "msg": r.get("msg", "")}
        return {"verdict": v, "fail": fail,
                "compile_error": view["compile_error"] if v == "compile_error" else "",
                "cost": {self.path: cost}, "agree": True}

    def session_start(self) -> dict:
        base = self.sub.baseline
        return dict(base["start"]) if self.path == "warm" else {"wall": 0.0, "cpu": 0.0}

    def close(self):
        self.sub.close()


# ---------------------------------------------------------------- the bot

class TieredBot:
    def __init__(self, info, settings, validator, pol: Policy, handover="cold", guard="off", session=0):
        self.info, self.settings, self.validator = info, settings, validator
        self.handover, self.session = handover, session
        self.memory = EvidenceMemory(info, settings["runner"]["cost_unit"])
        self.controller = Controller(pol)
        g = settings["budget_guard"]
        self.guard = None if guard == "off" else BudgetGuard(
            g["r"], g["budget"], g["budget_unit"], log_only=(guard == "log"))
        self.fix_key = javasrc.candidate_key(info["fixed_methods"])
        self.steps = []

    @property
    def tier(self) -> str:
        return self.controller.tier

    def begin_call(self, gen: dict):
        """A model call of the current tier starts; `gen` is its latency and tokens."""
        t = C.tier(self.settings, self.tier)
        self.steps.append({"tier": self.tier, "mode": t["mode"], "call": sum(
            1 for s in self.steps if s["tier"] == self.tier), "gen": gen, "candidates": []})

    # ------------------------------------------------------------ main call
    def validate(self, methods, last=True) -> Result:
        """Judge one candidate ({method key: new text}, or None for an answer
        that could not be read). `last` marks the last candidate of the call."""
        step, tier, mem = self.steps[-1], self.tier, self.memory
        rec = {"key": None, "verdict": "unreadable", "repeat": False, "repeat_of": None,
               "progress": False, "signature": ["unreadable"], "fail": None,
               "val": {p: A.zero() for p in A.PATHS}, "val_if_uncached": None, "agree": True}
        feedback = prompts.UNREADABLE
        if methods is None:
            mem.unreadable(tier)
        else:
            key = mem.key(methods)
            hit, own_nth = mem.lookup(key), mem.nth_by(key, tier)
            if hit is None:
                out = self.validator.validate(methods, mem.schedule())
                entry, progress = mem.add(key, tier, out["verdict"], out["fail"],
                                          out["compile_error"], methods, {"cost": out["cost"]})
                rec.update({"progress": progress, "agree": out["agree"],
                            "val": {p: out["cost"].get(p) for p in A.PATHS}})
            else:
                entry = mem.repeat(key, tier)
                rec.update({"repeat": True, "repeat_of": hit["tier"],
                            "val_if_uncached": hit["extra"].get("cost")})
            rec.update({"key": key, "verdict": entry["verdict"], "signature": list(entry["signature"]),
                        "fail": entry["fail"], "matches_fix": key == self.fix_key})
            if entry["verdict"] != "plausible":
                # did another tier already fail in exactly this way?
                rec["refuted_again"] = any(mem.entries[k]["tier"] != tier
                                           for k in mem.signatures[entry["signature"]]["keys"])
            if entry["verdict"] == "plausible":
                feedback = ""
            else:
                feedback, why = prompts.describe_failure(self.info, entry["verdict"], entry["fail"],
                                                         entry["compile_error"])
                if own_nth is not None:
                    # The tier repeats itself: say so. A repeat of the other tier's
                    # candidate is answered from the memory but reported as a plain
                    # failure, so a tier's prompts never depend on the other tier.
                    feedback = prompts.repeat_notice(own_nth, why)
        step["candidates"].append(rec)
        shown = "repeat" if rec["repeat"] else rec["verdict"]
        evidence = {"test": rec["fail"], "signature": rec["signature"]} if rec["verdict"] != "plausible" else None

        if rec["verdict"] == "plausible":
            self._close_step(step)
            return Result(shown, None, "stop", reason="plausible patch found", record=rec)
        self.controller.candidate(rec["progress"])
        if not last:
            return Result(shown, evidence, "same", feedback=feedback, record=rec)

        self._close_step(step)
        nxt, why = self.controller.call_done()
        if self.guard is not None:
            stop, g_why = self.guard.step(tier, tier == self.settings["tiers"][-1]["name"], step["cost"],
                                          any(c["progress"] for c in step["candidates"]))
            if stop and nxt != "stop":
                nxt, why = "stop", g_why
        note = report = None
        if nxt == "step_up":
            note = handover_note(self.handover, mem, self.info, tier)
            step["step_up"] = why
        elif nxt == "stop":
            report = give_up_report(self.info["bug"], self.session, why, mem, self.steps)
        return Result(shown, evidence, nxt, note, report, feedback, why, rec)

    def _close_step(self, step):
        """Cost of the finished call: the bot's own (warm) price, and the price
        of a plain loop, which builds and starts a JVM for every candidate and
        validates repeats again."""
        t, prices = C.tier(self.settings, step["tier"]), self.settings["prices"]
        cands = step["candidates"]
        own = [c["val"].get("warm") or c["val"].get("cold") or A.zero() for c in cands]
        plain = [(c["val_if_uncached"] or c["val"]).get("cold") for c in cands]
        step["cost"] = call_cost(step["gen"], own, t, prices)
        step["cost_plain"] = None if any(v is None for v in plain) else call_cost(step["gen"], plain, t, prices)
