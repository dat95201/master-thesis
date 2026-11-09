"""Paths, settings and the environment needed to call Defects4J and Java."""

import hashlib
import json
import os
import shutil
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
D4J_HOME = Path(os.getenv("D4J_HOME", ROOT / "benchmarks" / "defects4j"))
D4J_BIN = D4J_HOME / "framework" / "bin" / "defects4j"
D4J_PROJECTS = D4J_HOME / "framework" / "projects"
MAJOR_HOME = D4J_HOME / "major"
TOOLS = ROOT / "tools"

SETTINGS_FILE = ROOT / "settings.json"
LOCK_FILE = ROOT / "settings.lock.json"

DATA = ROOT / "data"          # bug selection and per-bug facts
CACHE = ROOT / "cache"        # model answers and audits, shared by all configurations
RESULTS = ROOT / "results"    # experiment outputs and the analysis report
WORK = ROOT / "work"          # Defects4J checkouts (scratch)

# Defects4J is validated on Java 11.
_JAVA11_CANDIDATES = [
    os.getenv("JAVA_HOME", ""),
    "/opt/homebrew/opt/openjdk@11/libexec/openjdk.jdk/Contents/Home",
    "/usr/local/opt/openjdk@11/libexec/openjdk.jdk/Contents/Home",
    "/usr/lib/jvm/java-11-openjdk-amd64",
    "/usr/lib/jvm/java-11-openjdk",
]


def load_dotenv():
    """Read KEY=VALUE lines from .env without overriding the real environment."""
    path = ROOT / ".env"
    if not path.exists():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


def java_home() -> str:
    for c in _JAVA11_CANDIDATES:
        if c and (Path(c) / "bin" / "java").exists():
            return c
    return ""


def tool_env() -> dict:
    """Environment for Defects4J, Ant, Major and our test runner."""
    env = dict(os.environ)
    jh = java_home()
    if jh:
        env["JAVA_HOME"] = jh
        env["PATH"] = f"{jh}/bin:{env.get('PATH', '')}"
    env["PATH"] = f"{D4J_BIN.parent}:{env['PATH']}"
    env["TZ"] = "America/Los_Angeles"          # Defects4J runs its tests in this zone
    perl5 = Path.home() / "perl5" / "lib" / "perl5"  # cpanm --local-lib default
    if perl5.exists() and str(perl5) not in env.get("PERL5LIB", ""):
        env["PERL5LIB"] = f"{perl5}:{env['PERL5LIB']}" if env.get("PERL5LIB") else str(perl5)
    return env


def java_bin(name: str = "java") -> str:
    jh = java_home()
    return str(Path(jh) / "bin" / name) if jh else (shutil.which(name) or name)


def load_settings() -> dict:
    return json.loads(SETTINGS_FILE.read_text())


def settings_hash() -> str:
    return hashlib.sha256(SETTINGS_FILE.read_bytes()).hexdigest()[:16]


def check_frozen(split: str):
    """Pre-registration: warn when eval runs use settings that were not frozen."""
    if split != "eval":
        return
    if not LOCK_FILE.exists():
        print("!! settings.json is not frozen (run `python -m vlayer freeze` after the dev set).")
        return
    lock = json.loads(LOCK_FILE.read_text())
    if lock.get("hash") != settings_hash():
        print("!! settings.json changed after it was frozen; the change must be reported.")


def write_json(path: Path, obj):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(obj, indent=1, sort_keys=False))
    tmp.replace(path)


def read_json(path: Path, default=None):
    return json.loads(path.read_text()) if path.exists() else default
