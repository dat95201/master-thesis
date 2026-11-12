"""Module M2 (Escalation Controller): decide which model proposes next.

The cheap tier tries first, and the tests say whether it is getting
anywhere. The rule, Step (Algorithm 1 of the proposal): after each call of
the cheap tier, count the candidates that made progress, that is, new
candidates that failed in a way the session had not seen. A call in which at
most one candidate made progress has stalled. The session steps up to the
strong tier after a stalled call, or when the cheap tier's budget of
candidates is spent, and the controller writes a handover note from the
Evidence Memory.

The same state machine runs the baselines the rule is compared with (each
model alone, a switch after a fixed number of candidates), so every policy
goes through one code path.
"""

import re
import textwrap
from dataclasses import dataclass

from . import config as C
from . import javasrc
from . import memory as M

HANDOVERS = ("cold", "evidence", "evidence+patches", "transcript")


@dataclass
class Policy:
    name: str
    cheap: str = "step"         # none | all | fixed | step
    k: int = 0                  # fixed: step up after k cheap candidates
    stall_new_max: int = 1      # step: a call stalls when at most this many candidates made progress
    cheap_budget: int = 20      # B1, candidates
    strong_rounds: int = 10     # R2, rounds; 0 = the session ends at the cheap tier


def policy(name: str, settings: dict) -> Policy:
    """cheap-only | strong-only | fixed-K | step | step-strict | equal-K-R"""
    b1, r2 = C.tier(settings, "cheap")["budget"], C.tier(settings, "strong")["budget"]
    n = C.tier(settings, "cheap")["n"]
    if name == "cheap-only":
        return Policy(name, "all", cheap_budget=b1, strong_rounds=0)
    if name == "strong-only":
        return Policy(name, "none", cheap_budget=0, strong_rounds=r2)
    if name == "step":
        return Policy(name, "step", stall_new_max=settings["step"]["stall_new_max"],
                      cheap_budget=b1, strong_rounds=r2)
    if name == "step-strict":       # variant: a call stalls only when no candidate made progress
        return Policy(name, "step", stall_new_max=0, cheap_budget=b1, strong_rounds=r2)
    m = re.fullmatch(r"fixed-(\d+)", name) or re.fullmatch(r"equal-(\d+)-(\d+)", name)
    if m:
        k = int(m.group(1))
        if k % n or k > b1:
            raise ValueError(f"{name}: the switch point must be a multiple of {n} and at most {b1}")
        if k == 0:
            return Policy(name, "none", cheap_budget=0, strong_rounds=r2)
        return Policy(name, "fixed", k=k, cheap_budget=k,
                      strong_rounds=int(m.group(2)) if m.lastindex == 2 else r2)
    raise ValueError(f"unknown policy {name}")


class Controller:
    def __init__(self, pol: Policy):
        self.policy = pol
        self.tier = "strong" if pol.cheap == "none" else "cheap"
        self.drawn = {"cheap": 0, "strong": 0}
        self.new_in_call = 0

    def candidate(self, progress: bool):
        """One candidate of the current tier was judged and is not plausible."""
        self.drawn[self.tier] += 1
        self.new_in_call += int(progress)

    def call_done(self):
        """A model call ended without a plausible candidate.
        Returns (next, reason): same | step_up | stop."""
        p, new = self.policy, self.new_in_call
        self.new_in_call = 0
        if self.tier == "strong":
            if self.drawn["strong"] >= p.strong_rounds:
                return "stop", f"the strong tier used its {p.strong_rounds} rounds"
            return "same", ""
        drawn = self.drawn["cheap"]
        spent = drawn >= p.cheap_budget
        if p.cheap == "all" or p.strong_rounds == 0:
            return ("stop", f"the cheap tier used its {p.cheap_budget} candidates") if spent else ("same", "")
        if p.cheap == "fixed":
            if drawn < p.k:
                return "same", ""
            why = f"fixed switch after {p.k} candidates"
        elif new <= p.stall_new_max:
            why = f"the cheap tier stalled ({new} candidate{'s' if new != 1 else ''} of its last call made progress)"
        elif spent:
            why = f"the cheap tier used its {p.cheap_budget} candidates"
        else:
            return "same", ""
        self.tier = "strong"
        return "step_up", why


# ---------------------------------------------------------------- handover

def _code(entry) -> str:
    return "\n\n".join(javasrc.dedent(t) for t in entry["methods"].values())


def handover_note(kind: str, mem: M.EvidenceMemory, info: dict, tier="cheap") -> str:
    """What the strong tier is told about the cheap tier's work.

    cold               nothing
    evidence           the verified failures: how many candidates were tried,
                       each distinct failure signature with its count, the
                       compiler errors; none of the first model's text
    evidence+patches   the same, with the shortest candidate behind each signature
    transcript         every candidate of the first model and how it failed, in order
    """
    if kind == "cold":
        return ""
    if kind not in HANDOVERS:
        raise ValueError(f"unknown handover {kind}; choose from {HANDOVERS}")
    n = mem.count(tier)
    head = f"{n['candidates']} candidate{'s' if n['candidates'] != 1 else ''} from the first model, none plausible"
    if kind == "transcript":
        lines = [f"Transcript of the first model ({head})"]
        index = {}
        for i, (t, key, rep) in enumerate((p for p in mem.proposals if p[0] == tier), 1):
            if key is None:
                lines.append(f"Candidate {i}: the answer could not be read as the given methods")
            elif rep:
                lines.append(f"Candidate {i}: the same as candidate {index[key]}")
            else:
                index[key] = i
                e = mem.entries[key]
                what, err = M.describe(e["signature"])
                lines += [f"Candidate {i}:", textwrap.indent(_code(e), "    "),
                          f"  -> {what}" + (f": {err}" if err else "")]
        return "\n".join(lines)
    lines = [f"Handover note ({head})", "Verified failures, most frequent first:"]
    for sig, cnt, example in mem.failures():
        if not any(mem.entries[k]["tier"] == tier for k in mem.signatures[sig]["keys"]):
            continue
        what, err = M.describe(sig)
        lines.append(f"  {cnt}x {what}")
        if err:
            lines.append(f"      {err}")
        if kind == "evidence+patches":
            lines += ["      shortest candidate that failed this way:",
                      textwrap.indent(_code(example), "        ")]
    tail = []
    if n["repeats"]:
        tail.append(f"{n['repeats']} candidate{'s' if n['repeats'] != 1 else ''} repeated an earlier one.")
    if n["unreadable"]:
        tail.append(f"{n['unreadable']} answer{'s' if n['unreadable'] != 1 else ''} could not be read.")
    if not mem.passed_trigger() and info.get("triggering_classes"):
        # verdicts are per test class, so this is what the tests have shown
        names = [c.rsplit(".", 1)[-1] for c in info["triggering_classes"]]
        tail.append("No candidate passed " + (names[0] if len(names) == 1 else "all of " + ", ".join(names)) + ".")
    if tail:
        lines.append(" ".join(tail))
    return "\n".join(lines)
