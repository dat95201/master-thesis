"""Module M3 (Budget Guard): keep the cost per bug within a budget and say
why a session stopped.

Each tier has its own share of the session: a number of candidates for the
cheap tier and a number of rounds for the strong tier (enforced by the
Escalation Controller, since they define the policy). On top of them the
guard stops a session
  * when the top tier stalls: r rounds in a row without progress, or
  * before a model call that the remaining budget B (seconds, tokens or USD)
    cannot pay for, judged by the average cost of the tier's calls so far.
In log-only mode it records when it *would* stop and lets the session go on;
the stop is applied afterwards by replay, which separates its effect from
the rest of the bot.
"""

import textwrap

from . import javasrc
from . import memory as M


def cache_split(earlier: list, step: dict, prices: dict) -> dict:
    """Input tokens of a model call that a prompt cache reads and writes,
    under one fixed model of the cache: {read, write}.

    A prompt shorter than `cache_min_prompt_tokens` is not cached. A prefix
    the same tier already sent in this session is read; the rest of the
    prompt is written. So a blind call repeats a prompt the cache holds, and
    a round of a conversation reads the previous prompt and writes what was
    added. Nothing is shared between sessions or tiers.

    The model is a function of the session alone, which the provider's own
    report (gen.tok_in_cached) is not: that depends on what happened to be
    sent in the minutes before. It is the most a cache can give a session."""
    gen = step["gen"] or {}
    tin, requests = gen.get("tok_in", 0), max(1, gen.get("requests", 1))
    least = prices.get("cache_min_prompt_tokens")
    per = tin / requests
    if not least or per < least:
        return {"read": 0, "write": 0}
    prev = [s for s in earlier if s["tier"] == step["tier"]]
    if step["mode"] == "blind":
        write = 0 if prev else per
        return {"read": tin - write, "write": write}
    held = (prev[-1]["gen"] or {}).get("tok_in", 0) if prev else 0
    read = min(held, tin) if held >= least else 0
    return {"read": read, "write": tin - read}


def call_cost(gen: dict, vals: list, tier: dict, prices: dict, cache: dict = None) -> dict:
    """Cost of one model call and of validating its candidates, in the units
    of the proposal: seconds (wall-clock), cpu (validation), tokens, and money
    in two readings: usd, with every input token at the list price, and
    usd_cached, with the split of `cache_split` at the tier's prices for
    cached input and cache writes."""
    gen, cache = gen or {}, cache or {"read": 0, "write": 0}
    tin, tout = gen.get("tok_in", 0), gen.get("tok_out", 0)
    p_in, p_out = tier.get("usd_per_mtok_in", 0.0), tier.get("usd_per_mtok_out", 0.0)
    read, write = cache["read"], cache["write"]
    cpu = sum(v.get("cpu", 0.0) for v in vals)
    val_usd = cpu * prices["usd_per_cpu_hour"] / 3600
    usd = (tin * p_in + tout * p_out) / 1e6
    usd_c = ((tin - read - write) * p_in + read * tier.get("usd_per_mtok_in_cached", p_in)
             + write * tier.get("usd_per_mtok_cache_write", p_in) + tout * p_out) / 1e6
    return {"seconds": gen.get("wall", 0.0) + sum(v.get("wall", 0.0) for v in vals),
            "model_seconds": gen.get("wall", 0.0), "cpu": cpu, "tokens": tin + tout,
            "model_usd": usd, "usd": usd + val_usd,
            "model_usd_cached": usd_c, "usd_cached": usd_c + val_usd, "cache": cache}


class BudgetGuard:
    def __init__(self, r=3, budget=None, unit="seconds", log_only=False):
        self.r, self.budget, self.unit, self.log_only = r, budget, unit, log_only
        self.calls, self.stall, self.spent = 0, 0, 0.0
        self.tier_calls, self.tier_spent = {}, {}
        self.would_stop = None          # (call number, reason), the first time a rule fires

    def step(self, tier: str, top: bool, cost: dict, progress: bool):
        """Record a finished model call that found no plausible patch.
        Returns (stop, reason)."""
        self.calls += 1
        self.spent += cost[self.unit]
        self.tier_calls[tier] = self.tier_calls.get(tier, 0) + 1
        self.tier_spent[tier] = self.tier_spent.get(tier, 0.0) + cost[self.unit]
        if top:
            self.stall = 0 if progress else self.stall + 1
        reason = None
        if top and self.r and self.stall >= self.r:
            reason = f"no progress for {self.r} rounds at the {tier} tier"
        elif self.budget is not None and \
                self.spent + self.tier_spent[tier] / self.tier_calls[tier] > self.budget:
            reason = f"the budget of {self.budget:g} {self.unit} cannot pay for another round"
        if reason and self.would_stop is None:
            self.would_stop = (self.calls, reason)
        return bool(reason) and not self.log_only, reason or ""


def replay(steps: list, r, budget, unit, top="strong"):
    """Apply the guard after the fact to a recorded session. Returns
    (number of model calls kept, reason), or (len(steps), None) if it never fires."""
    g = BudgetGuard(r, budget, unit)
    for i, st in enumerate(steps, 1):
        if any(c["verdict"] == "plausible" for c in st["candidates"]):
            return i, None
        stop, reason = g.step(st["tier"], st["tier"] == top, st["cost"],
                              any(c.get("progress") for c in st["candidates"]))
        if stop:
            return i, reason
    return len(steps), None


def give_up_report(bug: str, session, reason: str, mem: M.EvidenceMemory, steps: list) -> str:
    """The report a session returns when it ends without a plausible patch."""
    tiers = []
    for t in dict.fromkeys(st["tier"] for st in steps):
        n = mem.count(t)
        if any(st["tier"] == t and st["mode"] == "blind" for st in steps):
            tiers.append(f"{t} {n['candidates']} candidates ({n['distinct']} distinct, "
                         f"{n['repeats']} repeat{'s' if n['repeats'] != 1 else ''})")
        else:
            tiers.append(f"{t} {n['candidates']} round{'s' if n['candidates'] != 1 else ''}")
    total = lambda k: sum(st["cost"][k] for st in steps)  # noqa: E731
    lines = [
        f"Give-up report (bug {bug}, session {session})",
        f"Stopped: {reason}",
        f"Tiers:   {'; '.join(tiers)}",
        f"Spent:   {total('seconds'):.0f} s (models {total('model_seconds'):.0f} s, "
        f"validation {total('seconds') - total('model_seconds'):.0f} s), "
        f"{total('tokens') / 1000:.0f}k tokens, ${total('usd'):.2f}",
        "Failures, most frequent first:",
    ]
    rows = mem.failures()
    for sig, cnt, _ in rows[:5]:
        what, err = M.describe(sig)
        lines.append(f"  {cnt}x {what}")
        if err:
            lines.append(f"      {err}")
    if rows:
        ex = rows[0][2]
        nth = sum(1 for t, _, _ in mem.proposals[:ex["pos"] + 1] if t == ex["tier"])
        unit = "candidate" if any(st["tier"] == ex["tier"] and st["mode"] == "blind" for st in steps) else "round"
        lines.append(f"Example patch for the most frequent failure ({ex['tier']} tier, {unit} {nth}):")
        for text in ex["methods"].values():
            lines.append(textwrap.indent(javasrc.dedent(text), "    "))
    return "\n".join(lines)
