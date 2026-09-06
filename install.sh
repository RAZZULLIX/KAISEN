#!/usr/bin/env bash
# KAISEN installer — works on plain AND "externally managed" Pythons
# (PEP 668: Ubuntu 24.04/26.04, Debian 12+, Fedora 38+ ...).
#
# Strategy: create a .venv in the project root and install into it.  The
# system Python is never touched.  Override the interpreter with PYTHON=...
# (e.g. PYTHON=python3.13 ./install.sh).
set -euo pipefail
cd "$(dirname "$0")"

PY="${PYTHON:-python3}"
if ! command -v "$PY" >/dev/null 2>&1; then
  echo "error: $PY not found — install Python 3.10+ (apt install python3) and retry" >&2
  exit 1
fi

if [ ! -x .venv/bin/python ]; then
  echo "[kaisen] creating .venv with $PY ..."
  if ! "$PY" -m venv .venv; then
    cat >&2 <<'EOF'
error: could not create a virtualenv.
On Debian/Ubuntu the venv module needs an extra package:
  sudo apt install python3-venv
then retry.  (Or point PYTHON= at an interpreter that has it.)
EOF
    exit 1
  fi
  # Minimal distros may ship venv without pip — bootstrap if needed.
  if ! ./.venv/bin/python -m pip --version >/dev/null 2>&1; then
    echo "[kaisen] bootstrapping pip inside .venv ..."
    "$PY" -m ensurepip --upgrade || \
      curl -fsSL https://bootstrap.pypa.io/get-pip.py | ./.venv/bin/python
  fi
fi

PIP="./.venv/bin/python -m pip"
echo "[kaisen] installing requirements into .venv ..."
$PIP install --upgrade pip >/dev/null 2>&1 || true
$PIP install -r requirements.txt

cat <<EOF

[kaisen] ready.  Start the dashboard:

    ./.venv/bin/python main.py

then open http://127.0.0.1:8080

(On Windows/WSL the same layout works with 'py -3 -m venv .venv' if you
prefer; install.sh is for POSIX hosts.)
EOF
