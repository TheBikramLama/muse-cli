#!/usr/bin/env bash
# Idempotent: init repo (if needed), commit, and push to the muse-cli remote.
set -euo pipefail
cd "$(dirname "$0")"

if [ ! -d .git ]; then
  git init -q
fi
git add -A
if ! git diff --cached --quiet; then
  git commit -q -m "muse-cli: initial scaffold — TUI terminal bridge replacing muse-runner"
  echo "committed."
else
  echo "nothing to commit."
fi
git branch -M main
if ! git remote get-url origin >/dev/null 2>&1; then
  git remote add origin git@github.com:TheBikramLama/muse-cli.git
fi
git push -u origin main
