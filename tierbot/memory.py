"""Module M1 (Evidence Memory): what the tests have shown, kept for the whole
session and across the switch between models.

  * Verdicts. A candidate is normalised (comments, layout and local variable
    names removed, see javasrc.normalise). A candidate whose normal form is
    already in the memory is a repeat: it gets the stored verdict and no test
    is run, whichever tier proposed it first.
  * Failing tests. Every class that has rejected a candidate is kept and
    scheduled first for later candidates (substrate.schedule).
  * Failure signatures. Each distinct signature is kept with a count and an
    example patch. The Escalation Controller reads progress from them; the
    handover note and the give-up report are written from them.
"""

import re
from collections import Counter

from . import javasrc
from . import substrate


def signature(verdict: str, fail: dict = None, compile_error: str = "") -> tuple:
    """How a candidate failed: the rejecting test, the exception type and its
    message, without line numbers; for a candidate that does not compile, the
    first compiler error."""
    if verdict == "compile_error":
        first = (compile_error or "").splitlines()[0] if compile_error else ""
        return ("compile", re.sub(r"^[^:]*:\d+:\s*", "", first).strip())
    if verdict == "unreadable":
        return ("unreadable",)
    if verdict == "rejected" and fail:
        msg = re.sub(r"\s+", " ", fail.get("msg", "")).strip()
        msg = re.sub(r"@[0-9a-f]{5,}", "@", msg)            # object identities change between runs
        return (f"{fail['class']}::{fail.get('test', '-')}", fail.get("exc", ""), msg)
    return (verdict,)


class EvidenceMemory:
    def __init__(self, info: dict, unit="cpu"):
        self.info, self.unit = info, unit
        self.entries = {}               # candidate key -> entry
        self.proposals = []             # every proposal in order: (tier, key or None, repeat)
        self.rejections = Counter()     # test class -> distinct candidates it rejected
        self.signatures = {}            # signature -> {count, tier, keys}

    # ------------------------------------------------------------ verdicts
    @staticmethod
    def key(methods: dict) -> str:
        return javasrc.candidate_key(methods)

    def lookup(self, key):
        return self.entries.get(key)

    def nth_by(self, key, tier):
        """The position (1-based) among the tier's own proposals at which it
        first proposed this candidate, or None if it never did."""
        n = 0
        for t, k, _ in self.proposals:
            if t == tier:
                n += 1
                if k == key:
                    return n
        return None

    def schedule(self) -> list:
        """The order of the test classes for the next new candidate."""
        return substrate.schedule(self.info, self.rejections, self.unit)

    def add(self, key, tier, verdict, fail=None, compile_error="", methods=None, extra=None):
        """Store a validated candidate. Returns (entry, progress): progress is
        true when the candidate fails in a way the session has not seen."""
        sig = signature(verdict, fail, compile_error)
        entry = {"key": key, "tier": tier, "pos": len(self.proposals), "verdict": verdict,
                 "signature": sig, "fail": fail, "compile_error": compile_error,
                 "methods": methods or {}, "extra": extra or {}}
        self.entries[key] = entry
        self.proposals.append((tier, key, False))
        progress = False
        if verdict != "plausible":
            progress = sig not in self.signatures
            s = self.signatures.setdefault(sig, {"count": 0, "tier": tier, "keys": []})
            s["count"] += 1
            s["keys"].append(key)
            if verdict == "rejected" and fail:
                self.rejections[fail["class"]] += 1
        return entry, progress

    def repeat(self, key, tier):
        """A candidate the memory already holds: no test is run, no progress."""
        self.proposals.append((tier, key, True))
        return self.entries[key]

    def unreadable(self, tier):
        """An answer that could not be read as the given methods."""
        self.proposals.append((tier, None, False))

    # ------------------------------------------------------------ what the session knows
    def count(self, tier=None) -> dict:
        ps = [p for p in self.proposals if tier is None or p[0] == tier]
        return {"candidates": len(ps),
                "distinct": len({k for _, k, rep in ps if k and not rep}),
                "repeats": sum(1 for _, _, rep in ps if rep),
                "unreadable": sum(1 for _, k, _ in ps if k is None)}

    def failures(self) -> list:
        """Distinct failure signatures, most frequent first:
        [(signature, count, entry of the shortest example patch)]."""
        rows = []
        for sig, s in self.signatures.items():
            example = min((self.entries[k] for k in s["keys"]), key=_size)
            rows.append((sig, s["count"], example))
        rows.sort(key=lambda r: (-r[1], r[2]["pos"]))
        return rows

    def seen_by(self, sig, tier) -> bool:
        """Did a candidate of `tier` already fail with this signature?"""
        s = self.signatures.get(tuple(sig))
        return bool(s) and any(self.entries[k]["tier"] == tier for k in s["keys"])

    def passed_trigger(self) -> bool:
        """Did any candidate get past the tests that fail on the faulty program?"""
        trig = set(self.info["triggering_classes"])
        return any(e["verdict"] == "plausible" or
                   (e["verdict"] == "rejected" and e["fail"] and e["fail"]["class"] not in trig)
                   for e in self.entries.values())


def _size(entry) -> int:
    return sum(len(t) for t in entry["methods"].values()) or 10 ** 9


def describe(sig) -> tuple:
    """A signature as two lines of text: (what failed, the error)."""
    sig = tuple(sig)
    if sig[0] == "compile":
        return ("compile error: " + re.sub(r"^error:\s*", "", sig[1])[:160], "")
    if sig[0] == "unreadable":
        return ("answer could not be read as the given methods", "")
    if len(sig) < 3:
        return (sig[0], "")
    cls, _, test = sig[0].partition("::")
    return (f"{cls.rsplit('.', 1)[-1]}.{test}", f"{sig[1].rsplit('.', 1)[-1]}: {sig[2][:160]}".rstrip(": "))
