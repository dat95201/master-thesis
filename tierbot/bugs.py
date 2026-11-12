"""Bug selection and per-bug preparation (Section 5.1 of the proposal).

`prepare` checks one bug against the selection rules and records every fact
the experiments need in data/bugs/<bug>/info.json:

  * the faulty methods (the methods the developer's fix changes), which are
    given to the model and are the only code a candidate may replace,
  * the test suite after removing flaky classes (3 runs on the fixed program),
  * the failing CI run: one run of the suite on the faulty program, which
    gives the classes that fail before any repair and the time of every class,
  * the classes that reach the faulty methods (one instrumented run), which
    are the classes the Warm Substrate schedules.
"""

import csv
import random
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

from . import config as C
from . import d4j
from . import javasrc


def split_bug(bug: str):
    pid, bid = bug.rsplit("-", 1)
    return pid, int(bid)


def workdir(bug: str, version: str) -> Path:
    return C.WORK / bug / version


def info_path(bug: str) -> Path:
    return C.DATA / "bugs" / bug / "info.json"


def load_info(bug: str) -> dict:
    info = C.read_json(info_path(bug))
    if info is None:
        raise SystemExit(f"{bug} is not prepared; run `python -m tierbot prepare --bugs {bug}`")
    for key in ("cp_test", "cp_compile"):       # absolute paths: follow the project folder
        info[key] = {v: C.relocate_cp(cp) for v, cp in info[key].items()}
    return info


def ensure_checkout(info: dict, version: str) -> Path:
    """Re-create a working copy if the scratch directory was deleted."""
    w = workdir(info["bug"], version)
    if not (w / ".defects4j.config").exists() and not (w / info["src_file"]).exists():
        d4j.checkout(info["pid"], info["bid"], version, w)
        d4j.compile_project(w)
    return w


def verify_checkout(info: dict) -> str:
    """'' when the source file in work/<bug>/b still holds the faulty methods
    that info.json records, else what differs. Every candidate is built from
    that file, so a checkout left patched (an interrupted run, an edit by
    hand) would silently shift every verdict of the bug."""
    path = ensure_checkout(info, "b") / info["src_file"]
    if not path.exists():
        return f"{path} is missing"
    have = javasrc.method_texts(javasrc.read_source(path)[0])
    for key, text in info["buggy_methods"].items():
        if have.get(key) != text:
            return f"{info['src_file']}: `{key}` is not the recorded faulty method"
    return ""


def source_path(w: Path, src_dir: str, cls: str) -> str:
    """Source file of a class. '$' marks a nested class, but a class name may
    also contain '$' itself (Gson's `$Gson$Types`), so drop nested parts from
    the right only until the file exists."""
    name = cls.replace(".", "/")
    while True:
        rel = f"{src_dir}/{name}.java"
        simple = name.rsplit("/", 1)[-1]
        if (w / rel).exists() or "$" not in simple[1:]:
            return rel
        name = name[:name.rindex("$")]


def metadata_ok(pid: str, bid: int):
    if len(d4j.modified_classes(pid, bid)) != 1:
        return False, "fix modifies more than one class"
    if d4j.patch_file_count(pid, bid) != 1:
        return False, "fix changes more than one file"
    if not d4j.trigger_tests(pid, bid):
        return False, "no trigger test"
    return True, "ok"


def prepare(bug: str, settings: dict, force=False):
    """Check one bug and record its facts. Returns (info or None, reason)."""
    if not force and info_path(bug).exists():
        return load_info(bug), "ok (cached)"
    sel, rs = settings["bugs"], settings["runner"]
    pid, bid = split_bug(bug)
    ok, why = metadata_ok(pid, bid)
    if not ok:
        return None, why

    wb, wf = workdir(bug, "b"), workdir(bug, "f")
    for v, w in (("b", wb), ("f", wf)):
        if not d4j.checkout(pid, bid, v, w):
            return None, f"checkout {v} failed"
        if not d4j.compile_project(w)[0]:
            return None, f"compile {v} failed"

    mod = d4j.modified_classes(pid, bid)[0]
    src_dir = d4j.export(wb, "dir.src.classes")
    rel_src = source_path(wb, src_dir, mod)
    if not (wb / rel_src).exists():
        return None, f"source file not found: {rel_src}"
    buggy_text, _ = javasrc.read_source(wb / rel_src)
    fixed_text, _ = javasrc.read_source(wf / rel_src)
    keys, why = javasrc.faulty_methods(buggy_text, fixed_text)
    if keys is None:
        return None, why
    bmeth, fmeth = javasrc.method_texts(buggy_text), javasrc.method_texts(fixed_text)
    found = {m.key: m for m in javasrc.find_methods(buggy_text)}

    tests_all = d4j.export(wb, "tests.all").split()
    cp_b, cp_f = d4j.export(wb, "cp.test"), d4j.export(wf, "cp.test")
    timeout = rs["class_timeout_s"]

    # flaky check: every class must pass on the fixed program in every run
    stable = set(tests_all)
    fixed_runs = []
    for i in range(rs["flaky_runs"]):
        recs, jvm = d4j.run_classes(wf, cp_f, tests_all, "all", timeout)
        total = jvm["wall"] + sum(r["wall"] for r in recs.values())
        if i == 0 and total > sel["max_suite_seconds"]:
            return None, f"suite takes {total:.0f} s"
        stable &= {c for c, r in recs.items() if r["status"] == "pass"}
        fixed_runs.append(round(total, 2))
    flaky = sorted(set(tests_all) - stable)

    # the failing CI run: the suite on the faulty program
    ci, ci_jvm = d4j.run_classes(wb, cp_b, tests_all, "all", timeout)
    triggers = d4j.trigger_tests(pid, bid)
    trig_classes = sorted({t.split("::")[0] for t, _ in triggers})
    for tc in trig_classes:
        if tc not in stable:
            return None, f"triggering class {tc} is not stable on the fixed program"
        if ci.get(tc, {}).get("status") == "pass":
            return None, f"triggering class {tc} passes on the faulty program"
    suite = [c for c in tests_all if c in stable]
    if len(suite) < sel["min_test_classes"]:
        return None, f"only {len(suite)} stable test classes"

    bin_dir = d4j.export(wb, "dir.bin.classes")
    covering = coverage(wb, rel_src, keys, mod, bin_dir, cp_b, suite, timeout)

    test_dir = d4j.export(wb, "dir.src.tests")
    info = {
        "bug": bug, "pid": pid, "bid": bid,
        "modified_class": mod, "src_file": rel_src, "test_dir": test_dir, "bin_dir": bin_dir,
        "cp_test": {"b": cp_b, "f": cp_f},
        "cp_compile": {"b": d4j.export(wb, "cp.compile"), "f": d4j.export(wf, "cp.compile")},
        "faulty": [{"key": k, "name": found[k].name, "nparams": found[k].nparams} for k in keys],
        "buggy_methods": {k: bmeth[k] for k in keys},
        "fixed_methods": {k: fmeth[k] for k in keys},   # never shown to the model
        "trigger_tests": [{"test": t, "error": e} for t, e in triggers],
        "triggering_classes": trig_classes,
        "loaded_classes": d4j.loaded_classes(pid, bid),
        "suite": suite, "flaky": flaky,
        "ci": {c: {"wall": ci[c]["wall"], "cpu": ci[c]["cpu"], "status": ci[c]["status"]}
               for c in suite if c in ci},
        "ci_jvm": ci_jvm,
        "fixed_suite_seconds": fixed_runs,
        "covering": covering,
    }
    C.write_json(info_path(bug), info)
    return load_info(bug), "ok"


def coverage(w, rel_src, keys, mod, bin_dir, cp, suite, timeout):
    """One instrumented run of the faulty program: which classes reach the
    faulty methods. Returns None if the probes could not be compiled."""
    src = w / rel_src
    original, enc = javasrc.read_source(src)
    try:
        javasrc.write_source(src, javasrc.insert_probes(original, keys), enc)
        d4j.drop_class_files(w / bin_dir, mod.split("$")[0])
        ok, _, _, _ = d4j.compile_project(w)
        if not ok:
            return None
        recs, _ = d4j.run_classes(w, cp, suite, "all", timeout, probe=True)
        return [c for c in suite if recs.get(c, {}).get("hit")]
    finally:
        javasrc.write_source(src, original, enc)
        d4j.drop_class_files(w / bin_dir, mod.split("$")[0])
        d4j.compile_project(w)


# ---------------------------------------------------------------- selection

SELECTION_LOG = C.DATA / "selection.csv"
SPLIT_FILE = C.DATA / "split.json"


def _prepare_job(args):
    bug, settings = args
    try:
        info, why = prepare(bug, settings)
    except Exception as e:  # noqa: BLE001
        info, why = None, f"error: {e}"
    return bug, info is not None, why


def select(settings: dict, n_dev: int, n_eval: int, n_e3: int, projects=None, jobs=1):
    """Draw bugs at random, spread evenly over projects (round robin), until
    n_dev + n_eval pass every rule. Resumable: decisions are logged."""
    rng = random.Random(settings["seed"])
    want = n_dev + n_eval
    log = {}
    if SELECTION_LOG.exists():
        with SELECTION_LOG.open() as f:
            for row in csv.DictReader(f):
                log[row["bug"]] = row
    queues = {}
    for pid in sorted(projects or d4j.projects()):
        bids = d4j.active_bugs(pid)
        rng.shuffle(bids)
        queues[pid] = [f"{pid}-{b}" for b in bids]

    order = []                               # round robin over projects
    while any(queues.values()):
        for pid in sorted(queues):
            if queues[pid]:
                order.append(queues[pid].pop(0))

    pos = {b: i for i, b in enumerate(order)}
    accepted = [b for b in order if log.get(b, {}).get("accepted") == "1"]
    todo = [b for b in order if b not in log]

    def next_batch():
        """The next bugs to check: in draw order until `want` are accepted;
        after that, only undecided bugs drawn before the last one kept (e.g.
        a bug whose log row was removed to check it again), since they come
        first in the sample."""
        if len(accepted) < want:
            return todo[:jobs]
        cutoff = pos[sorted(accepted, key=pos.get)[want - 1]]
        return [b for b in todo[:jobs] if pos[b] < cutoff]

    SELECTION_LOG.parent.mkdir(parents=True, exist_ok=True)
    new_file = not SELECTION_LOG.exists()
    with SELECTION_LOG.open("a", newline="") as f:
        w = csv.DictWriter(f, fieldnames=["bug", "accepted", "reason"])
        if new_file:
            w.writeheader()
        while batch := next_batch():
            todo = [b for b in todo if b not in batch]
            if jobs > 1:
                with ProcessPoolExecutor(jobs) as ex:
                    results = list(ex.map(_prepare_job, [(b, settings) for b in batch]))
            else:
                results = [_prepare_job((b, settings)) for b in batch]
            for bug, ok, why in results:
                w.writerow({"bug": bug, "accepted": int(ok), "reason": why})
                f.flush()
                print(f"  {bug:<20} {'ACCEPT' if ok else 'reject'}  {why}")
                if ok:
                    accepted.append(bug)
    accepted = sorted(accepted, key=pos.get)[:want]
    if len(accepted) < want:
        print(f"!! only {len(accepted)} bugs pass the rules (wanted {want})")

    # split: dev and eval spread over projects; the evaluation set is halved for E3 and E4
    shuffled = accepted[:]
    rng.shuffle(shuffled)
    by_pid = {}
    for b in shuffled:
        by_pid.setdefault(split_bug(b)[0], []).append(b)
    rr = []
    while any(by_pid.values()):
        for pid in sorted(by_pid):
            if by_pid[pid]:
                rr.append(by_pid[pid].pop(0))
    dev, ev = rr[:n_dev], rr[n_dev:]
    split = {"dev": sorted(dev), "eval": sorted(ev), "e3": sorted(ev[:n_e3]), "e4": sorted(ev[n_e3:])}
    C.write_json(SPLIT_FILE, split)
    print(f"split: {len(split['dev'])} dev, {len(split['eval'])} eval "
          f"({len(split['e3'])} for E3, {len(split['e4'])} for E4) -> {SPLIT_FILE}")
    return split


def bugs_for(split: str = None, bugs=None) -> list:
    if bugs:
        return list(bugs)
    s = C.read_json(SPLIT_FILE)
    if s is None:
        raise SystemExit("no data/split.json; run `python -m tierbot select` or pass --bugs")
    if split == "e4" and "e4" not in s:
        return sorted(set(s["eval"]) - set(s["e3"]))
    return s[split]
