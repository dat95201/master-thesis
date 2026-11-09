"""Module M3 (Budget Guard): stop sessions that are stuck or out of budget,
and explain why in a give-up report.

A rejected candidate leaves a failure signature: the rejecting test, the
exception type and its message (no line numbers). A round makes progress
when its candidate is new and fails in a new way. The guard stops when
  * r rounds in a row made no progress, or
  * the remaining budget B cannot pay for another round, judged by the
    average cost of the rounds so far, or
  * the hard round limit R_max is reached.
In log-only mode (E3) it records when it *would* stop and lets the session
go on; the stop is applied afterwards by replay.
"""

import re
from collections import Counter


def signature(verdict: str, fail: dict = None, compile_error: str = "") -> tuple:
    if verdict == "compile_error":
        first = (compile_error or "").splitlines()[0] if compile_error else ""
        return ("compile", re.sub(r"^[^:]*:\d+:\s*", "", first).strip())
    if verdict == "unreadable":
        return ("unreadable",)
    if verdict == "rejected" and fail:
        msg = re.sub(r"\s+", " ", fail.get("msg", "")).strip()
        return (f"{fail['class']}::{fail.get('test', '-')}", fail.get("exc", ""), msg)
    return (verdict,)


def round_cost(gen: dict, val: dict, prices: dict) -> dict:
    """Cost of one round in seconds, tokens and USD."""
    tok = gen.get("tok_in", 0) + gen.get("tok_out", 0)
    usd = (gen.get("tok_in", 0) * prices["usd_per_mtok_in"]
           + gen.get("tok_out", 0) * prices["usd_per_mtok_out"]) / 1e6 \
        + val.get("cpu", 0.0) * prices["usd_per_cpu_hour"] / 3600
    return {"seconds": gen.get("wall", 0.0) + val.get("wall", 0.0), "tokens": tok, "usd": usd}


class BudgetGuard:
    def __init__(self, r=3, budget=None, unit="seconds", r_max=20, log_only=False):
        self.r, self.budget, self.unit, self.r_max = r, budget, unit, r_max
        self.log_only = log_only
        self.rounds = 0
        self.stall = 0
        self.spent = 0.0
        self.signatures = set()
        self.would_stop = None          # (round, reason), first time a rule fires

    def step(self, cost: dict, sig: tuple, repeat: bool, plausible: bool):
        """Record a finished round. Returns (go_on, reason)."""
        self.rounds += 1
        self.spent += cost[self.unit]
        if plausible:
            return False, "plausible patch found"
        progress = (not repeat) and sig not in self.signatures
        self.signatures.add(sig)
        self.stall = 0 if progress else self.stall + 1
        reason = None
        if self.r and self.stall >= self.r:
            reason = f"no progress for {self.r} rounds"
        elif self.budget is not None and self.spent + self.spent / self.rounds > self.budget:
            reason = f"budget of {self.budget} {self.unit} would be exceeded"
        if reason and self.would_stop is None:
            self.would_stop = (self.rounds, reason)
        if self.rounds >= self.r_max:
            return False, f"round limit of {self.r_max} reached"
        if reason and not self.log_only:
            return False, reason
        return True, ""


def replay(rounds: list, r, budget, unit, r_max=20):
    """Apply the guard after the fact to a recorded session.

    Returns the number of rounds it would keep (the session is cut after
    that round) and the reason, or (len(rounds), None) if it never fires.
    """
    g = BudgetGuard(r, budget, unit, r_max)
    for i, rd in enumerate(rounds, 1):
        go_on, reason = g.step(rd["cost"], tuple(rd["signature"]), rd["repeat"],
                               rd["verdict"] == "plausible")
        if not go_on:
            return i, (None if rd["verdict"] == "plausible" else reason)
    return len(rounds), None


def give_up_report(bug: str, session: int, rounds: list, reason: str) -> str:
    """The report a session returns when it ends without a plausible patch."""
    n = len(rounds)
    distinct = len({rd["key"] for rd in rounds if rd.get("key")})
    repeats = sum(1 for rd in rounds if rd["repeat"])
    gen_s = sum(rd["gen"]["wall"] for rd in rounds)
    val_s = sum(rd["val"]["wall"] for rd in rounds)
    tokens = sum(rd["gen"]["tok_in"] + rd["gen"]["tok_out"] for rd in rounds)
    sigs = Counter()
    example = {}
    for rd in rounds:
        s = tuple(rd["signature"])
        sigs[s] += 1
        example.setdefault(s, rd["round"])
    lines = [
        f"Give-up report  (bug {bug}, session {session})",
        f"Stopped:  {reason}",
        f"Rounds:   {n}   distinct patches: {distinct}   repeats: {repeats}",
        f"Spent:    {gen_s + val_s:.0f} s  (model {gen_s:.0f} s, validation {val_s:.0f} s), "
        f"{tokens / 1000:.0f}k tokens",
        "Failures, most frequent first:",
    ]
    for s, cnt in sigs.most_common(5):
        if s[0] == "compile":
            lines.append(f"  {cnt}x  compile error: {s[1][:100]}")
        elif s[0] == "unreadable":
            lines.append(f"  {cnt}x  answer could not be read as the given methods")
        else:
            lines.append(f"  {cnt}x  {s[0].rsplit('.', 1)[-1]}")
            lines.append(f"        {s[1].rsplit('.', 1)[-1]}: {s[2][:100]}")
    if sigs:
        lines.append(f"Example patch for the most frequent failure: round {example[sigs.most_common(1)[0][0]]}")
    return "\n".join(lines)
