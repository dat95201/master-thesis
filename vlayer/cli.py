"""Command line: python -m vlayer <command> [options]

  env        check Java 11, Defects4J, Major, the test runner and the model
  prepare    check and record facts for the given bugs (--bugs)
  select     draw the 120 bugs and split them into dev / eval / E3
  mutants    E1 step 1: generate and audit the mutated patches
  e1         E1 step 2: simulate every ordering on the same mutant sessions
  e2         E2: the repair loop with each ordering, live
  e3         E3: the whole layer, live
  analyze    metrics and statistics -> results/report-<split>.md
  repair     run the full layer on one bug and print the session
  freeze     record the settings hash before the evaluation (pre-registration)
  smoke      a short end-to-end run on one bug with the mock model
"""

import argparse
from datetime import datetime

from . import config as C


def _bugs(args):
    from .bugs import bugs_for
    return bugs_for(args.split, args.bugs)


def cmd_env(args, settings):
    from . import d4j
    ok = True
    jh = C.java_home()
    code, out, _, _ = d4j.run([C.java_bin(), "-version"])
    print(f"java      : {out.splitlines()[0] if out else 'missing'}  ({jh or 'JAVA_HOME not found'})")
    ok &= '"11' in out
    code, out, _, _ = d4j.d4j(["info", "-p", "Lang"])
    print(f"defects4j : {'ok' if code == 0 else 'FAILED'}  ({C.D4J_BIN})")
    if code != 0:
        print(out[-600:])
    ok &= code == 0
    major = C.MAJOR_HOME / "bin" / "major"
    print(f"major     : {'ok' if major.exists() else 'missing'}  ({major})")
    try:
        d4j.build_runner()
        print("runner    : ok (tools/AuditRunner.java compiled)")
    except RuntimeError as e:
        print(f"runner    : FAILED {e}")
        ok = False
    C.load_dotenv()
    import os
    m = settings["model"]["name"]
    has_key = bool(os.getenv("OPENAI_API_KEY"))
    print(f"model     : {m}  (API key {'set' if has_key else 'not set'}, "
          f"base url {os.getenv('OPENAI_BASE_URL') or 'default'})")
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


def cmd_mutants(args, settings):
    from .experiments import run_mutants
    C.check_frozen(args.split)
    run_mutants(_bugs(args), settings, args.jobs)


def cmd_e1(args, settings):
    from .experiments import run_e1
    C.check_frozen(args.split)
    run_e1(_bugs(args), settings, args.jobs, not args.no_variants)


def cmd_e2(args, settings):
    from .experiments import run_live
    C.check_frozen(args.split)
    run_live("e2", _bugs(args), settings, args.jobs)


def cmd_e3(args, settings):
    from .experiments import run_live
    C.check_frozen(args.split)
    if not args.bugs and args.split == "eval":
        args.split = "e3"            # E3 runs on its 50-bug part of the evaluation set
    bugs = _bugs(args)
    run_live("e3", bugs, settings, args.jobs)


def cmd_analyze(args, settings):
    from .analysis import run
    run(args.split if not args.bugs else "custom", _bugs(args), settings)


def cmd_repair(args, settings):
    from .bugs import load_info, prepare
    from .loop import run_session
    if args.model:
        settings["model"]["name"] = args.model
    if prepare(args.bug, settings)[0] is None:
        raise SystemExit(f"{args.bug} does not pass the selection rules")
    info = load_info(args.bug)
    arm = {"ordering": args.ordering, "cache": True, "notice": True, "guard": "on"}
    print(f"Repairing {args.bug} with the full layer ({args.ordering}, "
          f"{'live ordered runs' if args.live else 'audited runs'}), model {settings['model']['name']}")
    res = run_session(info, settings, arm, args.session, live=args.live, verbose=True)
    print("\nPLAUSIBLE PATCH FOUND in round", res["plausible_round"] if res["plausible"] else "")
    if res["report"]:
        print("\n" + res["report"])


def cmd_freeze(args, settings):
    lock = {"hash": C.settings_hash(), "frozen_at": datetime.now().isoformat(timespec="seconds"),
            "settings": settings}
    C.write_json(C.LOCK_FILE, lock)
    print(f"settings frozen: {lock['hash']} -> {C.LOCK_FILE}")


def cmd_smoke(args, settings):
    """Everything on one bug, small numbers, mock model, in smoke/."""
    from .analysis import run as analyze
    from .bugs import prepare
    from .experiments import run_e1, run_live, run_mutants
    for name in ("DATA", "CACHE", "RESULTS"):
        setattr(C, name, C.ROOT / "smoke" / name.lower())
    settings["model"]["name"] = "mock"
    settings["live"]["sessions"] = 1
    settings["e1"].update({"mutants_per_group": 2, "max_audits_per_group": 3, "sessions_per_bug": 3})
    bug = args.bug
    print(f"[1/5] prepare {bug}")
    info, why = prepare(bug, settings)
    print(f"      {why}")
    if info is None:
        raise SystemExit("choose another bug for the smoke test")
    print("[2/5] mutants")
    run_mutants([bug], settings)
    print("[3/5] E1 simulation")
    run_e1([bug], settings, variants=True)
    print("[4/5] E2 and E3 with the mock model")
    run_live("e2", [bug], settings)
    run_live("e3", [bug], settings)
    print("[5/5] analysis")
    analyze("smoke", [bug], settings)


def main(argv=None):
    ap = argparse.ArgumentParser(prog="python -m vlayer", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)

    def with_bugs(p, default="eval"):
        p.add_argument("--split", default=default, choices=["dev", "eval", "e3"],
                       help="bug set from data/split.json (default %(default)s)")
        p.add_argument("--bugs", nargs="+", help="explicit bugs, e.g. Lang-6 Cli-17 (overrides --split)")
        p.add_argument("--jobs", type=int, default=1, help="bugs processed in parallel")
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
    with_bugs(sub.add_parser("mutants"))
    p = with_bugs(sub.add_parser("e1"))
    p.add_argument("--no-variants", action="store_true", help="skip the RQ4 variants of ARC")
    with_bugs(sub.add_parser("e2"))
    with_bugs(sub.add_parser("e3"))
    with_bugs(sub.add_parser("analyze"))
    p = sub.add_parser("repair")
    p.add_argument("--bug", required=True)
    p.add_argument("--ordering", default="arc")
    p.add_argument("--live", action="store_true", help="really run the ordered classes (no audit)")
    p.add_argument("--session", type=int, default=0)
    p.add_argument("--model", help="override settings.model.name (e.g. mock)")
    sub.add_parser("freeze")
    p = sub.add_parser("smoke")
    p.add_argument("--bug", default="Cli-17", help="a bug with a fast suite")

    args = ap.parse_args(argv)
    settings = C.load_settings()
    globals()[f"cmd_{args.cmd}"](args, settings)


if __name__ == "__main__":
    main()

