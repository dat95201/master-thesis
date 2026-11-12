#!/usr/bin/env bash
# =============================================================
# One-time setup: Python environment + Defects4J 3.0.1.
#   bash setup.sh
# =============================================================
set -e

ROOT="$(cd "$(dirname "$0")" && pwd)"
D4J_DIR="$ROOT/benchmarks/defects4j"
# Release 3.0.1 plus seven maintenance commits: the snapshot the 120 bugs in
# data/ were selected and prepared with.
D4J_COMMIT="8c16da8230843cdc918eaf4ddb449637f02b83c6"

echo "==> [1/4] Checking system tools"
MISSING=0
for cmd in git svn perl cpanm python3; do
  if command -v "$cmd" >/dev/null 2>&1; then echo "    ok      : $cmd"; else echo "    MISSING : $cmd"; MISSING=1; fi
done
# Defects4J 3 needs Java 11, and tierbot needs the JDK (it compiles patches in process).
JAVA11=""
for home in "$JAVA_HOME" \
            "$(brew --prefix openjdk@11 2>/dev/null)/libexec/openjdk.jdk/Contents/Home" \
            /usr/lib/jvm/java-11-openjdk*; do
  if [ -n "$home" ] && [ -x "$home/bin/javac" ] && "$home/bin/java" -version 2>&1 | grep -q '"11'; then
    JAVA11="$home"; break
  fi
done
if [ -n "$JAVA11" ]; then
  echo "    ok      : Java 11 JDK ($JAVA11)"
else
  echo "    MISSING : Java 11 JDK"; MISSING=1
fi
if [ "$MISSING" = "1" ]; then
  echo ""
  echo "Install on macOS with Homebrew:"
  echo "    brew install git subversion perl cpanminus openjdk@11 python@3.11"
  echo "(Linux: install the same packages; Java must be a JDK 11.)"
  exit 1
fi

echo ""
echo "==> [2/4] Python virtual environment"
python3 -m venv "$ROOT/.venv"
"$ROOT/.venv/bin/pip" install -q --upgrade pip
"$ROOT/.venv/bin/pip" install -q -r "$ROOT/requirements.txt"
echo "    done -> $ROOT/.venv"

echo ""
echo "==> [3/4] Defects4J 3.0.1 (${D4J_COMMIT:0:8})"
mkdir -p "$ROOT/benchmarks"
if [ -x "$D4J_DIR/framework/bin/defects4j" ]; then
  echo "    already present -> $D4J_DIR"
else
  git clone https://github.com/rjust/defects4j.git "$D4J_DIR"
  git -C "$D4J_DIR" checkout -q "$D4J_COMMIT"
fi

echo ""
echo "==> [4/4] Defects4J init (downloads the project repositories; 10-30 min)"
cd "$D4J_DIR"
cpanm --local-lib="$HOME/perl5" --installdeps .
if [ -n "$(ls -A "$D4J_DIR/project_repos" 2>/dev/null | grep -v README)" ]; then
  echo "    project repositories already downloaded"
else
  ./init.sh
fi

echo ""
echo "============================================================"
echo "Done. tierbot finds Java 11, Defects4J and the Perl modules by itself."
echo "Check everything with:"
echo "    .venv/bin/python -m tierbot env"
echo "    .venv/bin/python -m tierbot smoke"
echo "============================================================"
