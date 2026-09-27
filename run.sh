#!/usr/bin/env bash
# Launch muse-cli. Self-bootstraps a project-local virtualenv on first run
# (your system python is Homebrew-managed and refuses global pip installs).
cd "$(dirname "$0")"
if [ ! -d .venv ]; then
  echo "creating .venv and installing dependencies..."
  python3 -m venv .venv
  .venv/bin/pip install -q -r requirements.txt
fi
exec .venv/bin/python -m muse_cli "$@"
