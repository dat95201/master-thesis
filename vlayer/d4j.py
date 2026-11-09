"""Defects4J adapter: checkout, metadata, compile, run test classes, mutants."""


import re
import resource
import shutil
import subprocess
import tempfile
import time
from pathlib import Path

from . import config as C

RUNNER_DIR = C.WORK / "_runner"
# Defects4J runs every project's tests with its own JUnit 4 (some projects only
# ship JUnit 3); it goes first on the runner's class path for the same reason.
JUNIT = C.D4J_PROJECTS / "lib" / "junit-4.12-hamcrest-1.3.jar"


# ---------------------------------------------------------------- processes

def _child_cpu() -> float:
    r = resource.getrusage(resource.RUSAGE_CHILDREN)
    return r.ru_utime + r.ru_stime


def run(cmd, cwd=None, timeout=None, env=None):
    """Run a command; return (returncode, output, wall_s, cpu_s)."""
    t0, c0 = time.perf_counter(), _child_cpu()
    try:
        p = subprocess.run(cmd, cwd=cwd, env=env or C.tool_env(), timeout=timeout,
                           stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                           errors="replace")
        code, out = p.returncode, p.stdout
    except subprocess.TimeoutExpired as e:
        code, out = -9, (e.stdout or "") if isinstance(e.stdout, str) else ""
    return code, out, time.perf_counter() - t0, _child_cpu() - c0


def d4j(args, cwd=None, timeout=1800):
    return run([str(C.D4J_BIN)] + args, cwd=cwd, timeout=timeout)


# ---------------------------------------------------------------- metadata (no checkout)

def projects() -> list:
    return sorted(p.name for p in C.D4J_PROJECTS.iterdir()
                  if (p / "active-bugs.csv").exists())


def active_bugs(pid: str) -> list:
    lines = (C.D4J_PROJECTS / pid / "active-bugs.csv").read_text().splitlines()[1:]
    return [int(l.split(",")[0]) for l in lines if l.strip()]


def _meta_lines(pid, bid, folder, suffix=""):
    p = C.D4J_PROJECTS / pid / folder / f"{bid}{suffix}"
    return [l.strip() for l in p.read_text().splitlines() if l.strip()] if p.exists() else []


def modified_classes(pid, bid) -> list:
    return _meta_lines(pid, bid, "modified_classes", ".src")


def loaded_classes(pid, bid) -> list:
    return _meta_lines(pid, bid, "loaded_classes", ".src")


def patch_file_count(pid, bid) -> int:
    p = C.D4J_PROJECTS / pid / "patches" / f"{bid}.src.patch"
    return p.read_text(errors="replace").count("diff --git") if p.exists() else 0


def trigger_tests(pid, bid) -> list:
    """[(test 'Class::method', first error line)] from Defects4J's trigger_tests file."""
    p = C.D4J_PROJECTS / pid / "trigger_tests" / str(bid)
    out, cur = [], None
    for line in p.read_text(errors="replace").splitlines() if p.exists() else []:
        if line.startswith("--- "):
            cur = line[4:].strip()
            out.append([cur, ""])
        elif cur and not out[-1][1] and line.strip():
            out[-1][1] = line.strip()
    return [tuple(x) for x in out]


# ---------------------------------------------------------------- working copies

def checkout(pid, bid, version, workdir: Path) -> bool:
    if (workdir / ".defects4j.config").exists():
        return True
    if workdir.exists():
        shutil.rmtree(workdir)
    workdir.parent.mkdir(parents=True, exist_ok=True)
    code, out, _, _ = d4j(["checkout", "-p", pid, "-v", f"{bid}{version}", "-w", str(workdir)])
    if code != 0:
        print(out[-2000:])
    return code == 0


def export(workdir: Path, prop: str) -> str:
    code, out, _, _ = d4j(["export", "-p", prop, "-w", str(workdir)])
    # export prints the value on stdout; status lines go to stderr (merged here)
    lines = [l for l in out.splitlines() if not re.match(r"^(Running|Determine|  - |\s*$)", l)
             and "..." not in l]
    return "\n".join(lines).strip() if code == 0 else ""


def compile_project(workdir: Path):
    """defects4j compile. Returns (ok, wall_s, cpu_s, error_text)."""
    code, out, wall, cpu = d4j(["compile", "-w", str(workdir)], timeout=1800)
    if code == 0:
        return True, wall, cpu, ""
    return False, wall, cpu, compile_error(out)


def compile_error(out: str) -> str:
    lines = out.splitlines()
    for i, l in enumerate(lines):
        if "error:" in l:
            block = [re.sub(r"^\s*\[javac\]\s?", "", x) for x in lines[i:i + 6]]
            block[0] = re.sub(r"^.*/([^/]+\.java):(\d+):", r"\1:\2:", block[0])
            return "\n".join(x.rstrip() for x in block if x.strip())
    return "\n".join(lines[-6:])


def drop_class_files(bin_dir: Path, fqcn: str):
    """Delete compiled classes of a top-level class so Ant recompiles it."""
    pkg, _, simple = fqcn.rpartition(".")
    d = bin_dir / pkg.replace(".", "/")
    if d.exists():
        for f in d.glob(f"{simple}.class"):
            f.unlink()
        for f in d.glob(f"{simple}$*.class"):
            f.unlink()


# ---------------------------------------------------------------- test runner

def build_runner() -> Path:
    cls = RUNNER_DIR / "AuditRunner.class"
    src = C.TOOLS / "AuditRunner.java"
    if cls.exists() and cls.stat().st_mtime >= src.stat().st_mtime:
        return RUNNER_DIR
    RUNNER_DIR.mkdir(parents=True, exist_ok=True)
    code, out, _, _ = run([C.java_bin("javac"), "-cp", str(JUNIT), "-d", str(RUNNER_DIR), str(src)])
    if code != 0:
        raise RuntimeError(f"cannot compile AuditRunner:\n{out}")
    return RUNNER_DIR


def _parse_runner_output(path: Path):
    started, records = [], {}
    if not path.exists():
        return started, records
    for line in path.read_text(errors="replace").splitlines():
        f = line.split("\t")
        if f[0] == "START":
            started.append(f[1])
        elif f[0] == "CLASS" and len(f) >= 12:
            records[f[1]] = {
                "status": f[2], "wall": float(f[3]) / 1000, "cpu": float(f[4]) / 1000,
                "n_run": int(f[5]), "n_fail": int(f[6]), "test": f[7], "exc": f[8],
                "line": int(f[9]) if f[9].isdigit() else 0, "msg": f[10], "hit": f[11] == "1",
            }
    return started, records


def run_classes(workdir: Path, cp_test: str, classes: list, mode="all",
                class_timeout=60.0, probe=False):
    """Run test classes in the given order in one JVM.

    mode "all" runs every class (restarting the JVM after a crash or timeout);
    mode "stop" stops at the first failing class.
    Returns (records {class: dict}, jvm {wall, cpu}) where jvm is the start-up
    and shut-down overhead of the first JVM (process time minus class time).
    """
    runner = build_runner()
    records, jvm, todo = {}, None, list(classes)
    while todo:
        with tempfile.TemporaryDirectory() as tmp:
            lst, out = Path(tmp) / "classes.txt", Path(tmp) / "out.tsv"
            lst.write_text("\n".join(todo) + "\n")
            cmd = [C.java_bin(), "-Xmx2g", "-cp", f"{runner}:{JUNIT}:{cp_test}", "AuditRunner",
                   str(lst), str(out), mode, str(class_timeout)]
            if probe:
                from .javasrc import PROBE_PROPERTY
                cmd.append(PROBE_PROPERTY)
            budget = class_timeout * len(todo) + 120
            _, log, wall, cpu = run(cmd, cwd=workdir, timeout=budget)
            started, recs = _parse_runner_output(out)
        if not started:
            print(f"    test runner did not start:\n{log[-800:]}")
        if jvm is None:
            jvm = {"wall": max(0.0, wall - sum(r["wall"] for r in recs.values())),
                   "cpu": max(0.0, cpu - sum(r["cpu"] for r in recs.values()))}
        records.update(recs)
        crashed = [c for c in started if c not in recs]
        for c in crashed:          # the JVM died inside this class
            records[c] = {"status": "fail", "wall": 0.0, "cpu": 0.0, "n_run": 0, "n_fail": 1,
                          "test": "-", "exc": "JVMCrash", "line": 0,
                          "msg": "the test JVM stopped while running this class", "hit": False}
        if mode == "stop" and any(r["status"] != "pass" for r in records.values()):
            break
        todo = [c for c in todo if c not in records]
        if todo and not started:   # nothing ran at all: give up on the rest
            for c in todo:
                records[c] = {"status": "error", "wall": 0.0, "cpu": 0.0, "n_run": 0, "n_fail": 1,
                              "test": "-", "exc": "RunnerError", "line": 0,
                              "msg": "the test runner did not start", "hit": False}
            break
    return records, jvm or {"wall": 0.0, "cpu": 0.0}


# ---------------------------------------------------------------- mutants (Major)

def generate_mutants(workdir: Path, src_file: Path, cp_compile: str, outdir: Path) -> list:
    """Run Major on one source file and export every mutant as a source file.

    Returns [{id, op, line, method, path}] where path is the mutated file.
    """
    if outdir.exists():
        shutil.rmtree(outdir)
    (outdir / "bin").mkdir(parents=True)
    env = C.tool_env()
    env["MML"] = f"mml:{C.MAJOR_HOME / 'mml' / 'all.mml.bin'}"
    env["EXPORT"] = "export.mutants"
    code, out, _, _ = run([str(C.MAJOR_HOME / "bin" / "major"), "-cp", cp_compile,
                           "-d", str(outdir / "bin"), str(src_file)], cwd=outdir, env=env)
    log = outdir / "mutants.log"
    if code != 0 or not log.exists():
        print(f"    Major failed on {src_file.name}: {out[-500:]}")
        return []
    rel = None
    mutants = []
    for line in log.read_text(errors="replace").splitlines():
        parts = line.split(":", 4)
        if len(parts) < 5:
            continue
        mid, op, rest = parts[0], parts[1], parts[4]
        loc_line = rest.split(":", 3)          # location, line, offset, description
        if len(loc_line) < 3 or not loc_line[1].isdigit():
            continue
        mdir = outdir / "mutants" / mid
        if rel is None:
            found = list(mdir.rglob("*.java")) if mdir.exists() else []
            rel = found[0].relative_to(mdir) if found else None
        if rel is None:
            continue
        mutants.append({"id": int(mid), "op": op, "location": loc_line[0],
                        "line": int(loc_line[1]), "path": str(mdir / rel)})
    return mutants


def cleanup(path: Path):
    if path.exists():
        shutil.rmtree(path, ignore_errors=True)
