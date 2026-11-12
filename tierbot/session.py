"""One repair session of the tiered bot.

The cheap tier draws by blind re-sampling: each call sends the first prompt
and asks for n candidates; nothing about earlier failures is added. The
strong tier holds a conversation: one candidate per round, with the failure
fed back. A session ends at the first plausible patch, or when the bot says
stop.

Every policy runs through this one loop: a model alone, a fixed switch,
Step. With recorded answers (`cache_only`) the loop replays a policy on the
candidates of the recorded single-tier sessions, which is experiment E2.
"""

from . import config as C
from . import javasrc
from . import llm as L
from . import prompts
from .bot import AuditedValidator, LiveValidator, TieredBot
from .controller import policy as make_policy


def run_session(info, settings, policy_name, session, handover="cold", guard="off",
                llms=None, validator=None, live=False, verbose=False) -> dict:
    """Run one session and return its record (JSON-serialisable)."""
    llms = llms or L.tiers(settings)
    own_validator = validator is None
    if own_validator:
        validator = LiveValidator(info, settings) if live else AuditedValidator(info, settings)
    try:
        return _run(info, settings, policy_name, session, handover, guard, llms, validator, verbose)
    finally:
        if own_validator:
            validator.close()


def _run(info, settings, policy_name, session, handover, guard, llms, validator, verbose):
    bot = TieredBot(info, settings, validator, make_policy(policy_name, settings), handover, guard, session)
    faulty = [(f["key"], f["name"], f["nparams"]) for f in info["faulty"]]
    note, report, reason, messages = "", None, "", {}
    while True:
        tier = bot.tier
        t = C.tier(settings, tier)
        call = sum(1 for s in bot.steps if s["tier"] == tier)
        if t["mode"] == "blind":                # the same prompt every time, n candidates
            msgs, n = [{"role": "user", "content": prompts.first_prompt(info)}], t["n"]
        else:                                   # a conversation, one candidate per round
            msgs = messages.setdefault(tier, [{"role": "user", "content": prompts.first_prompt(info, note)}])
            n = 1
        answers, gen = llms[tier].draw(msgs, n, (info["bug"], session, tier, call), info)
        bot.begin_call(gen)
        res = None
        for i, answer in enumerate(answers):
            res = bot.validate(javasrc.parse_answer(answer, faulty), last=(i == len(answers) - 1))
            if verbose:
                r = res.record
                v = (r["val"].get("warm") or r["val"].get("cold") or {})
                print(f"    {tier:<6} call {call + 1:>2}.{i + 1}: {res.verdict:<13} "
                      f"val {v.get('wall', 0.0):6.2f}s  {(r['fail'] or {}).get('class', '').rsplit('.', 1)[-1]}")
            if res.next != "same" or res.verdict == "plausible":
                break
        bot.steps[-1]["unvalidated"] = len(answers) - len(bot.steps[-1]["candidates"])
        reason = res.reason
        if res.next == "stop":
            report = res.report
            break
        if res.next == "step_up":
            note = res.note or ""
            if verbose:
                print(f"    -> step up: {res.reason}")
                if note:
                    print("\n".join("       | " + l for l in note.splitlines()))
        elif t["mode"] != "blind":
            msgs += [{"role": "assistant", "content": answers[0]},
                     {"role": "user", "content": res.feedback}]

    last = bot.steps[-1]
    plausible = any(c["verdict"] == "plausible" for c in last["candidates"])
    return {
        "bug": info["bug"], "session": session, "policy": policy_name, "handover": handover,
        "guard": guard, "models": {t["name"]: llms[t["name"]].model for t in settings["tiers"]},
        "plausible": plausible, "plausible_tier": last["tier"] if plausible else None,
        "stopped": reason, "steps": bot.steps, "note": note or None, "report": report,
        "would_stop": list(bot.guard.would_stop) if bot.guard and bot.guard.would_stop else None,
        "warm_start": validator.session_start(),
    }
