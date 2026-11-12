"""Paths, settings and the environment needed to call Defects4J and Java."""

import glob
import hashlib
import json
import os
import shutil
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
D4J_HOME = Path(os.getenv("D4J_HOME", ROOT / "benchmarks" / "defects4j"))
D4J_BIN = D4J_HOME / "framework" / "bin" / "defects4j"
D4J_PROJECTS = D4J_HOME / "framework" / "projects"
TOOLS = ROOT / "tools"

SETTINGS_FILE = ROOT / "settings.json"
LOCK_FILE = ROOT / "settings.lock.json"

DATA = ROOT / "data"          # bug selection and per-bug facts
CACHE = ROOT / "cache"        # model answers and audits, shared by all configurations
RESULTS = ROOT / "results"    # recorded sessions and the analysis report
WORK = ROOT / "work"          # Defects4J checkouts and the compiled runners (scratch)

# Defects4J is validated on Java 11. A JDK is needed, not a JRE: the Warm
# Substrate compiles candidates with the compiler of the running JVM.
_JAVA11_CANDIDATES = [
    os.getenv("JAVA_HOME", ""),
    "/opt/homebrew/opt/openjdk@11/libexec/openjdk.jdk/Contents/Home",
    "/usr/local/opt/openjdk@11/libexec/openjdk.jdk/Contents/Home",
    *sorted(glob.glob("/usr/lib/jvm/java-11-openjdk*")),
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
    """Environment for Defects4J, Ant and the test runners."""
    env = dict(os.environ)
    jh = java_home()
    if jh:
        env["JAVA_HOME"] = jh
        env["PATH"] = f"{jh}/bin:{env.get('PATH', '')}"
    env["PATH"] = f"{D4J_BIN.parent}:{env['PATH']}"
    env["TZ"] = "America/Los_Angeles"          # Defects4J runs its tests in this zone
    env.pop("JAVA_TOOL_OPTIONS", None)         # would change every JVM we time
    perl5 = Path.home() / "perl5" / "lib" / "perl5"  # cpanm --local-lib default
    if perl5.exists() and str(perl5) not in env.get("PERL5LIB", ""):
        env["PERL5LIB"] = f"{perl5}:{env['PERL5LIB']}" if env.get("PERL5LIB") else str(perl5)
    return env


def java_bin(name: str = "java") -> str:
    jh = java_home()
    return str(Path(jh) / "bin" / name) if jh else (shutil.which(name) or name)


def relocate(path: str) -> str:
    """Paths recorded in data/bugs/*/info.json are absolute. If the project
    folder was moved, map them onto the current root."""
    if not path or os.path.exists(path):
        return path
    for marker in ("/work/", "/benchmarks/"):
        if marker in path:
            return str(ROOT) + path[path.index(marker):]
    return path


def relocate_cp(cp: str) -> str:
    return os.pathsep.join(relocate(p) for p in cp.split(os.pathsep))


# ---------------------------------------------------------------- settings

def load_settings() -> dict:
    return json.loads(SETTINGS_FILE.read_text())


def settings_hash() -> str:
    return hashlib.sha256(SETTINGS_FILE.read_bytes()).hexdigest()[:16]


def tier(settings: dict, name: str) -> dict:
    """The settings of one tier ('cheap' is the first tier, 'strong' the last)."""
    for t in settings["tiers"]:
        if t["name"] == name:
            return t
    raise KeyError(f"no tier named {name} in settings.json")


def check_frozen(bugs):
    """Pre-registration: warn when evaluation bugs are run with settings that were not frozen."""
    if not set(bugs) & set(read_json(DATA / "split.json", {}).get("eval", [])):
        return
    if not LOCK_FILE.exists():
        print("!! settings.json is not frozen (run `python -m tierbot freeze` after the dev set).")
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
