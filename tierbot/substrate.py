"""Module M0 (Warm Substrate): make one validation cheap.

The project is built once. For each candidate only the patched source file
is compiled, by the compiler of a JVM that stays alive for the session
(tools/WarmRunner.java), and the result is loaded there through a fresh
class loader, so the build and the JVM start-up are paid once per session.

The module also holds the two things the rest of the bot needs from
validation:

  * the test schedule: the classes that failed on the faulty program, then
    the classes that rejected earlier candidates, then the other classes
    that reach the faulty methods, fastest first; classes that never reach
    the faulty methods are skipped;
  * the cold path (the project's own build and a new JVM for the candidate),
    which is the reference every warm verdict is checked against and the
    fallback when the warm path cannot be trusted.
"""

import os
import queue
import shutil
import subprocess
import tempfile
import threading
import time
from pathlib import Path

from . import config as C
from . import d4j
from . import javasrc
from .bugs import ensure_checkout, verify_checkout

UNITS = ("wall", "cpu")


# ---------------------------------------------------------------- the schedule

def scheduled(info: dict, unit="cpu") -> list:
    """The static order: classes that fail on the faulty program first, then
    the other classes that reach the faulty methods, fastest first."""
    suite = list(info["suite"])
    pos = {c: i for i, c in enumerate(suite)}
    trig = [c for c in suite if c in set(info["triggering_classes"])]
    cov = info.get("covering")
    rest = [c for c in (suite if cov is None else cov) if c in pos and c not in set(trig)]
    rest.sort(key=lambda c: (info["ci"].get(c, {}).get(unit, 0.0), pos[c]))
    return trig + rest


def schedule(info: dict, rejections: dict = None, unit="cpu") -> list:
    """The order for the next candidate. `rejections` counts, per class, the
    earlier candidates of the session it rejected (kept by the Evidence
    Memory); those classes move in front of the ones that never rejected."""
    static = scheduled(info, unit)
    if not rejections:
        return static
    trig = set(info["triggering_classes"])
    pos = {c: i for i, c in enumerate(static)}
    head = [c for c in static if c in trig]
    stored = sorted((c for c in static if c not in trig and rejections.get(c, 0) > 0),
                    key=lambda c: (-rejections[c], pos[c]))
    return head + stored + [c for c in static if c not in trig and c not in set(stored)]


# ---------------------------------------------------------------- compiler options

def class_release(info: dict) -> str:
    """The -source/-target level the project's build used, read from the
    class file of the modified class (major version 50 = Java 6, ...)."""
    w = C.WORK / info["bug"] / "b"
    f = w / info["bin_dir"] / (info["modified_class"].split("$")[0].replace(".", "/") + ".class")
    try:
        major = int.from_bytes(f.read_bytes()[6:8], "big")
    except OSError:
        return "-"
    return str(max(major - 44, 6)) if 49 <= major <= 55 else "-"


def _scratch(info) -> Path:
    d = C.WORK / info["bug"] / "warm"
    d.mkdir(parents=True, exist_ok=True)
    return d


# ---------------------------------------------------------------- the live runtime

class RuntimeDied(Exception):
    pass


class WarmRuntime:
    """One JVM that stays alive (tools/WarmRunner.java)."""

    def __init__(self, info: dict, settings: dict):
        self.timeout = settings["runner"]["class_timeout_s"]
        self.dir = Path(tempfile.mkdtemp(prefix="jvm-", dir=_scratch(info)))
        w = ensure_checkout(info, "b")
        _, self.encoding = javasrc.read_source(w / info["src_file"])
        cp_compile = info["cp_compile"]["b"]
        if str(w / info["bin_dir"]) not in cp_compile.split(":"):
            cp_compile = f"{w / info['bin_dir']}:{cp_compile}"
        runner = d4j.build_runner()
        cmd = [C.java_bin(), "-Xmx2g", "-Dapple.awt.UIElement=true",
               "-cp", f"{runner}:{d4j.JUNIT}", "WarmRunner",
               info["cp_test"]["b"], cp_compile, class_release(info), self.encoding,
               str(self.timeout), str(self.dir)]
        self.log = open(self.dir / "runtime.log", "w")
        t0 = time.perf_counter()
        self.proc = subprocess.Popen(cmd, cwd=w, env=C.tool_env(), stdin=subprocess.PIPE,
                                     stdout=subprocess.PIPE, stderr=self.log, text=True,
                                     errors="replace")
        self.lines = queue.Queue()
        threading.Thread(target=self._pump, daemon=True).start()
        self.cpu_ms, self.n = 0.0, 0
        ready = self._reply(60)
        if ready is None or ready[0] != "@@READY":
            self.close(keep_log=True)
            raise RuntimeDied(f"the live runtime did not start (see {self.dir / 'runtime.log'})")
        self.cpu_ms = float(ready[2])
        self.start = {"wall": time.perf_counter() - t0, "cpu": self.cpu_ms / 1000}

    def _pump(self):
        for line in self.proc.stdout:
            if line.startswith("@@"):
                self.lines.put(line.rstrip("\n").split("\t"))
        self.lines.put(None)

    def _reply(self, timeout):
        try:
            return self.lines.get(timeout=timeout)
        except queue.Empty:
            return None

    def run(self, source, classes_dir, classes: list, mode="all"):
        """Compile `source` (None: do not compile) into `classes_dir` (None:
        the program as built), then run `classes` in a fresh class loader.

        Returns (compile, records, alive): compile is None or {ok, wall, cpu,
        error}; records has the classes that ran; alive is False when the
        JVM has to be replaced (a class hung, or the JVM died).
        """
        self.n += 1
        lst, out = self.dir / f"classes-{self.n}.txt", self.dir / f"out-{self.n}.tsv"
        lst.write_text("\n".join(classes) + "\n")
        cmd = ["RUN", str(self.n), str(source or "-"), str(classes_dir or "-"), str(lst), str(out), mode]
        reply = None
        if self._send("\t".join(cmd)):
            reply = self._reply(self.timeout * max(1, len(classes)) + 120)
        started, recs, comp = d4j.parse_runner_output(out)
        for c in started:               # the JVM died inside this class
            if c not in recs:
                recs[c] = d4j.crashed_record()
        alive = reply is not None and reply[0] == "@@DONE" and reply[2] in ("ok", "compile_error")
        if reply is not None and len(reply) > 3:
            self.cpu_ms = float(reply[3])
        if not alive:
            self.close()
        return comp, recs, alive

    def _send(self, line: str) -> bool:
        """Write one command, unbuffered, so a dead JVM is noticed here."""
        if self.proc.poll() is not None:
            return False
        try:
            os.write(self.proc.stdin.fileno(), (line + "\n").encode())
            return True
        except (BrokenPipeError, OSError):
            return False

    def close(self, keep_log=False):
        if self._send("QUIT"):
            try:
                self.proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                pass
        if self.proc.poll() is None:
            self.proc.kill()
        self.proc.wait()
        for stream in (self.proc.stdin, self.proc.stdout):
            try:
                stream.close()
            except OSError:
                pass
        if not self.log.closed:
            self.log.close()
        if not keep_log:
            shutil.rmtree(self.dir, ignore_errors=True)


# ---------------------------------------------------------------- one bug

class Substrate:
    """The validation substrate of one bug: a live runtime for the warm path
    and the project's own build for the cold path."""

    def __init__(self, info: dict, settings: dict):
        self.info, self.settings = info, settings
        why = verify_checkout(info)
        if why:
            raise RuntimeError(
                f"work/{info['bug']}/b is not the faulty program of {info['bug']} ({why}). "
                f"Remove that folder; the next run checks the bug out again.")
        self.unit = settings["runner"]["cost_unit"]
        self.limit = settings["substrate"]["candidates_per_runtime"]
        self.classes = scheduled(info, self.unit)
        self.tmp = Path(tempfile.mkdtemp(prefix="cand-", dir=_scratch(info)))
        self.runtime, self.used, self.encoding = None, 0, "UTF-8"
        self.baseline = C.read_json(self._baseline_file())   # {ok, reason, start}

    def _baseline_file(self) -> Path:
        return C.CACHE / "audit" / self.info["bug"] / "_warm.json"

    # ------------------------------------------------------------ warm path
    def _patched(self, text: str, tag: str) -> Path:
        d = self.tmp / tag
        d.mkdir(parents=True, exist_ok=True)
        f = d / Path(self.info["src_file"]).name
        javasrc.write_source(f, text, self.encoding)
        return f

    def _start(self, calibrate: bool) -> dict:
        """Start a runtime; returns what the start cost. With `calibrate`, the
        unchanged file is compiled in the runtime and the scheduled classes
        run once, as at the start of a session: this warms the runtime up, and
        the warm path is trusted for this bug only if every class ends as in
        the failing CI run."""
        self.close_runtime()
        self.runtime, self.used = WarmRuntime(self.info, self.settings), 0
        self.encoding = self.runtime.encoding
        start = dict(self.runtime.start)
        if not calibrate:
            return start
        t0 = time.perf_counter()
        text, _ = javasrc.read_source(ensure_checkout(self.info, "b") / self.info["src_file"])
        out = self._run(text, "base", self.classes, "all")
        reason = ""
        if not out["compiled"]:
            reason = "the unchanged file does not compile in the live runtime: " + out["compile_error"][:200]
        else:
            diff = [c for c in self.classes
                    if (out["classes"].get(c, {}).get("status") == "pass")
                    != (self.info["ci"].get(c, {}).get("status") == "pass")]
            if diff:
                reason = "outcome differs from the failing CI run: " + ", ".join(diff[:5])
        start["wall"] += time.perf_counter() - t0
        start["cpu"] += out["compile"]["cpu"] + out["restart"]["cpu"] \
            + sum(r["cpu"] for r in out["classes"].values())
        if self.baseline is None:           # the first calibration of a bug decides
            self.baseline = {"ok": not reason, "reason": reason, "start": start}
            C.write_json(self._baseline_file(), self.baseline)
        return start

    def warm_ok(self) -> bool:
        """Can the warm path be trusted for this bug? Calibrated once."""
        if self.baseline is None:
            try:
                self._start(calibrate=True)
            except RuntimeDied as e:
                self.baseline = {"ok": False, "reason": str(e), "start": {"wall": 0.0, "cpu": 0.0}}
                C.write_json(self._baseline_file(), self.baseline)
        return self.baseline["ok"]

    def warm(self, text: str, tag: str, classes=None, mode="all") -> dict:
        """Validate a whole replacement source file on the warm path."""
        if self.runtime is None or self.used >= self.limit:
            self._start(calibrate=True)     # as a session starts: calibrated and warmed up
        if self.runtime is None:            # the faulty program itself killed the runtime
            self._start(calibrate=False)
        self.used += 1
        return self._run(text, tag, list(classes or self.classes), mode)

    def _run(self, text: str, tag: str, classes: list, mode: str) -> dict:
        src = self._patched(text, tag)
        patch = src.parent / "classes"
        out = {"compiled": False, "compile": {"wall": 0.0, "cpu": 0.0}, "compile_error": "",
               "classes": {}, "restarts": 0, "restart": {"wall": 0.0, "cpu": 0.0}}
        rt = self.runtime
        comp, recs, alive = rt.run(src, patch, classes, mode)
        if comp is None:                    # the runtime died before it compiled
            comp = {"ok": False, "wall": 0.0, "cpu": 0.0, "error": "error: the live runtime stopped"}
        out["compiled"], out["compile_error"] = comp["ok"], comp["error"]
        out["compile"] = {"wall": comp["wall"], "cpu": comp["cpu"]}
        out["classes"].update(recs)
        while not alive:
            # A class hung or killed the JVM. The session needs a new runtime;
            # its start is charged to this candidate. The compiled patch is kept.
            out["restarts"] += 1
            for u in UNITS:
                out["restart"][u] += rt.start[u]
            self.runtime = None
            todo = [c for c in classes if c not in out["classes"]]
            if not comp["ok"] or not todo or _stopped(out, mode):
                break
            self.runtime, self.used = WarmRuntime(self.info, self.settings), 1
            rt = self.runtime
            _, recs, alive = rt.run(None, patch, todo, mode)
            if not recs:
                break
            out["classes"].update(recs)
        if not alive:
            self.runtime = None
        shutil.rmtree(src.parent, ignore_errors=True)
        out["classes"] = {c: _slim(out["classes"][c]) for c in classes if c in out["classes"]}
        return out

    # ------------------------------------------------------------ cold path
    def cold(self, text: str, classes=None, mode="all", then=None) -> dict:
        """Validate a whole replacement source file as a plain loop does: the
        project's own build, then a new JVM for this candidate.

        `then(out, rerun)` is called while the patched build is still in
        place; `rerun(classes)` runs more classes on it (the order check and
        the whole-suite check of an audit)."""
        info = self.info
        classes = list(classes or self.classes)
        timeout = self.settings["runner"]["class_timeout_s"]
        w = ensure_checkout(info, "b")
        src = w / info["src_file"]
        original, enc = javasrc.read_source(src)
        top = info["modified_class"].split("$")[0]
        built = _class_files(w / info["bin_dir"], top)
        stash = Path(tempfile.mkdtemp(prefix="built-", dir=_scratch(info)))
        for f in built:
            shutil.copy2(f, stash / f.name)
        try:
            javasrc.write_source(src, text, enc)
            d4j.drop_class_files(w / info["bin_dir"], top)
            ok, cw, cc, err = d4j.compile_project(w)
            out = {"compiled": ok, "compile": {"wall": cw, "cpu": cc}, "compile_error": err,
                   "jvm": {"wall": 0.0, "cpu": 0.0}, "classes": {}}
            if ok:
                recs, jvm = d4j.run_classes(w, info["cp_test"]["b"], classes, mode, timeout)
                out["jvm"] = jvm
                out["classes"] = {c: _slim(recs[c]) for c in classes if c in recs}
                if then is not None:
                    then(out, lambda cs: {c: _slim(r) for c, r in d4j.run_classes(
                        w, info["cp_test"]["b"], cs, "all", timeout)[0].items()})
            return out
        finally:                           # the checkout is left exactly as it was built
            javasrc.write_source(src, original, enc)
            d4j.drop_class_files(w / info["bin_dir"], top)
            for f in built:
                shutil.copy2(stash / f.name, f)
            shutil.rmtree(stash, ignore_errors=True)

    # ------------------------------------------------------------ life cycle
    def close_runtime(self):
        if self.runtime is not None:
            self.runtime.close()
            self.runtime = None

    def close(self):
        self.close_runtime()
        shutil.rmtree(self.tmp, ignore_errors=True)


def _class_files(bin_dir: Path, fqcn: str) -> list:
    pkg, _, simple = fqcn.rpartition(".")
    d = bin_dir / pkg.replace(".", "/")
    return sorted(d.glob(f"{simple}.class")) + sorted(d.glob(f"{simple}$*.class")) if d.exists() else []


def _stopped(out, mode):
    return mode == "stop" and any(r["status"] != "pass" for r in out["classes"].values())


def _slim(rec):
    return {k: rec[k] for k in ("status", "wall", "cpu", "test", "exc", "line", "msg")}
