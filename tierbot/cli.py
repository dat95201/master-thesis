"""Command line: python -m tierbot <command> [options]

  env        check Java 11 (a JDK), Defects4J, the test runners and the models
  select     draw the 120 bugs and split them into dev / eval (E3 half, E4 half)
  prepare    check and record facts for the given bugs (--bugs)
  e1         E1: each model alone, live; recorded once (the audits are E0)
  e2         E2: every escalation policy replayed on the recorded sessions
  e3         E3: the strong tier restarted with each handover note, live
  e4         E4: the whole bot against the strong model alone, live
  analyze    metrics and statistics -> results/report-<split>.md
  repair     run the bot on one bug and print the session
  freeze     record the settings hash before the evaluation (pre-registration)
  smoke      a short end-to-end run on one bug with the mock models
"""

import argparse
import os
import shutil
from datetime import datetime

from . import config as C

MOCK = {"cheap": "mock-cheap", "strong": "mock-strong"}


def _bugs(args):
    from .bugs import bugs_for
    return bugs_for(args.split, args.bugs)


def _models(args):
    if getattr(args, "mock", False):
        return dict(MOCK)
    out = {}
    for item in getattr(args, "model", None) or []:
        tier, _, name = item.partition("=")
        out[tier] = name
    return out or None


def _apply_variant(args, settings):
    """A variant changes how a tier draws. It keeps its results apart from
    the pre-registered runs and never touches settings.json."""
    changes = {k: getattr(args, k, None) for k in ("cheap_n", "cheap_mode", "cheap_effort", "strong_budget")}
    if not any(v is not None for v in changes.values()) and not getattr(args, "variant", None):
        return
    if not getattr(args, "variant", None):
        raise SystemExit("give the variant a name with --variant NAME")
    cheap, strong = C.tier(settings, "cheap"), C.tier(settings, "strong")
    if changes["cheap_n"]:
        cheap["n"] = changes["cheap_n"]
    if changes["cheap_mode"]:
        cheap["mode"] = changes["cheap_mode"]
        if cheap["mode"] == "conversation":
            cheap["n"] = 1
    if changes["cheap_effort"]:
        cheap["params"]["reasoning_effort"] = changes["cheap_effort"]
        if changes["cheap_effort"] != "none":       # sampling parameters are refused when the model reasons
            cheap["params"].pop("temperature", None)
    if changes["strong_budget"]:
        strong["budget"] = changes["strong_budget"]
    C.RESULTS = C.ROOT / "results" / "variants" / args.variant
    print(f"variant {args.variant}: cheap tier {cheap['mode']}, n={cheap['n']}, "
          f"reasoning {cheap['params'].get('reasoning_effort', 'default')}; "
          f"strong budget {strong['budget']} -> {C.RESULTS}")


def cmd_env(args, settings):
    from . import d4j
    ok = True
    jh = C.java_home()
    code, out, _, _ = d4j.run([C.java_bin(), "-version"])
    print(f"java      : {out.splitlines()[0] if out else 'missing'}  ({jh or 'JAVA_HOME not found'})")
    ok &= '"11' in out
    code, out, _, _ = d4j.run([C.java_bin(), "--list-modules"])
    has_compiler = "jdk.compiler" in out
    print(f"compiler  : {'ok' if has_compiler else 'MISSING (a JDK is needed, not a JRE)'}")
    ok &= has_compiler
    if d4j.available():
        code, out, _, _ = d4j.d4j(["info", "-p", "Lang"])
        print(f"defects4j : {'ok' if code == 0 else 'FAILED'}  ({C.D4J_BIN})")
        if code != 0:
            print(out[-600:])
        ok &= code == 0
    else:
        print(f"defects4j : {d4j.status()}; only the warm path can run")
        ok = False
    try:
        d4j.build_runner()
        print("runners   : ok (tools/AuditRunner.java, tools/WarmRunner.java compiled)")
    except RuntimeError as e:
        print(f"runners   : FAILED {e}")
        ok = False
    C.load_dotenv()
    has_key = bool(os.getenv("OPENAI_API_KEY"))
    for t in settings["tiers"]:
        print(f"tier      : {t['name']:<6} {t['model']}  ({t['mode']}, n={t['n']}, budget {t['budget']}, "
              f"reasoning {t.get('params', {}).get('reasoning_effort', 'default')})")
    print(f"api       : key {'set' if has_key else 'not set'}, base url {os.getenv('OPENAI_BASE_URL') or 'default'}")
    print("\nall good" if ok else "\nfix the items above (see README, Setup)")


def cmd_prepare(args, settings):
    from .bugs import prepare
    for b in args.bugs:
        info, why = prepare(b, settings, force=args.force)
        extra = f"  suite {len(info['suite'])} classes, faulty {[f['key'] for f in info['faulty']]}" if info else ""
        print(f"  {b:<20} {'ACCEPT' if info else 'reject'}  {why}{extra}")


def cmd_select(args, settings):
    from .bugs import select
    s = settings["bugs"]
    select(settings, args.n_dev or s["n_dev"], args.n_eval or s["n_eval"], args.n_e3 or s["n_e3"],
           args.projects, args.jobs)


def _experiment(name):
    def cmd(args, settings):
        from . import experiments as E
        bugs = _bugs(args)
        if not args.mock:
            C.check_frozen(bugs)
        getattr(E, f"run_{name}")(bugs, settings, args.jobs, _models(args))
    return cmd


cmd_e1, cmd_e2, cmd_e3, cmd_e4 = (_experiment(n) for n in ("e1", "e2", "e3", "e4"))


def cmd_analyze(args, settings):
    from .analysis import run
    run(args.split if not args.bugs else "custom", _bugs(args), settings)


def cmd_repair(args, settings):
    from . import llm as L
    from .bugs import load_info
    from .session import run_session
    info = load_info(args.bug)
    llms = L.tiers(settings, models=_models(args))
    print(f"Repairing {args.bug}: policy {args.policy}, handover {args.handover}, "
          f"{'live scheduled runs' if args.live else 'audited runs'}, "
          f"models {', '.join(f'{k}={v.model}' for k, v in llms.items())}")
    res = run_session(info, settings, args.policy, args.session, args.handover, "on", llms,
                      live=args.live, verbose=True)
    if res["plausible"]:
        print(f"\nPLAUSIBLE PATCH FOUND at the {res['plausible_tier']} tier")
    if res["report"]:
        print("\n" + res["report"])


def cmd_freeze(args, settings):
    lock = {"hash": C.settings_hash(), "frozen_at": datetime.now().isoformat(timespec="seconds"),
            "settings": settings}
    C.write_json(C.LOCK_FILE, lock)
    print(f"settings frozen: {lock['hash']} -> {C.LOCK_FILE}")


def cmd_smoke(args, settings):
    """Every stage on one prepared bug, with the mock models, in smoke/."""
    from . import d4j
    from . import experiments as E
    from .analysis import run as analyze
    from .bugs import info_path
    src = info_path(args.bug)
    if not src.exists():
        raise SystemExit(f"{args.bug} is not prepared (choose one of data/bugs/, or run `prepare`)")
    for name in ("DATA", "CACHE", "RESULTS"):
        setattr(C, name, C.ROOT / "smoke" / name.lower())
    shutil.rmtree(C.ROOT / "smoke", ignore_errors=True)
    dst = info_path(args.bug)
    dst.parent.mkdir(parents=True)
    shutil.copy2(src, dst)
    if not d4j.available():
        print(f"Defects4J {d4j.status()}: the smoke test audits on the warm path only")
        settings["substrate"]["paths"] = ["warm"]
    bug = [args.bug]
    print("[1/5] E1: each mock model alone (every candidate is audited: E0)")
    E.run_e1(bug, settings, models=MOCK)
    print("[2/5] E2: the policies replayed on the recorded sessions")
    E.run_e2(bug, settings, models=MOCK)
    print("[3/5] E3: the strong tier restarted with each handover note")
    E.run_e3(bug, settings, models=MOCK)
    print("[4/5] E4: the whole bot")
    E.run_e4(bug, settings, models=MOCK)
    print("[5/5] analysis")
    analyze("smoke", bug, settings)


def main(argv=None):
    ap = argparse.ArgumentParser(prog="python -m tierbot", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)

    def with_bugs(p, default="eval"):
        p.add_argument("--split", default=default, choices=["dev", "eval", "e3", "e4"],
                       help="bug set from data/split.json (default %(default)s)")
        p.add_argument("--bugs", nargs="+", help="explicit bugs, e.g. Lang-59 Cli-12 (overrides --split)")
        p.add_argument("--jobs", type=int, default=1, help="bugs processed in parallel")
        return p

    def with_models(p):
        p.add_argument("--model", action="append", metavar="TIER=NAME",
                       help="override a tier's model, e.g. --model cheap=qwen2.5-coder:7b")
        p.add_argument("--mock", action="store_true", help="use the scripted mock models (no API)")
        return with_variant(p)

    def with_variant(p):
        g = p.add_argument_group("variants (development set only; results go to results/variants/<name>/)")
        g.add_argument("--variant", metavar="NAME", help="name of the variant run")
        g.add_argument("--cheap-n", type=int, help="candidates per cheap call (1, 3, 5 or 10)")
        g.add_argument("--cheap-mode", choices=["blind", "conversation"], help="how the cheap tier draws")
        g.add_argument("--cheap-effort", choices=["none", "low", "medium"],
                       help="reasoning effort of the cheap tier")
        g.add_argument("--strong-budget", type=int, help="rounds of the strong tier")
        return p

    sub.add_parser("env")
    p = sub.add_parser("prepare")
    p.add_argument("--bugs", nargs="+", required=True)
    p.add_argument("--force", action="store_true")
    p = sub.add_parser("select")
    p.add_argument("--n-dev", type=int)
    p.add_argument("--n-eval", type=int)
    p.add_argument("--n-e3", type=int)
    p.add_argument("--projects", nargs="+")
    p.add_argument("--jobs", type=int, default=1)
    with_models(with_bugs(sub.add_parser("e1")))
    with_models(with_bugs(sub.add_parser("e2")))
    with_models(with_bugs(sub.add_parser("e3"), default="e3"))
    with_models(with_bugs(sub.add_parser("e4"), default="e4"))
    with_variant(with_bugs(sub.add_parser("analyze")))
    p = with_models(sub.add_parser("repair"))
    p.add_argument("--bug", required=True)
    p.add_argument("--policy", default="step", help="step, strong-only, cheap-only, fixed-K (default step)")
    p.add_argument("--handover", default="cold", help="cold, evidence, evidence+patches or transcript")
    p.add_argument("--live", action="store_true",
                   help="really run the scheduled classes and stop at the first failure (no audit)")
    p.add_argument("--session", type=int, default=0)
    sub.add_parser("freeze")
    p = sub.add_parser("smoke")
    p.add_argument("--bug", default="Cli-12", help="a prepared bug with a fast suite")

    args = ap.parse_args(argv)
    settings = C.load_settings()
    _apply_variant(args, settings)
    globals()[f"cmd_{args.cmd}"](args, settings)


if __name__ == "__main__":
    main()
