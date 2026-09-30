#!/usr/bin/env bash
# Launch muse-cli. Self-bootstraps a project-local virtualenv on first run
# (your system python is Homebrew-managed and refuses global pip installs).
cd "$(dirname "$0")"
if [ ! -d .venv ]; then
  echo "creating .venv and installing dependencies..."
  python3 -m venv .venv
  .venv/bin/pip install -q -r requirements.txt
fi
# Auto-update: fast-forward to the latest origin before launching the TUI,
# so a fresh start never runs stale code. Skipped for subcommands
# (setup/doctor/mcp/...) and fails safe: dirty tree, no network or no git
# just launches as-is.
case "$1" in
  setup|doctor|pair|unpair|mcp|exec|report) ;;
  *)
    if command -v git >/dev/null 2>&1 && [ -d .git ]; then
      GIT_SSH_COMMAND="ssh -o BatchMode=yes" \
        git pull --ff-only -q >/dev/null 2>&1 || true
    fi
    ;;
esac
exec .venv/bin/python -m muse_cli "$@"