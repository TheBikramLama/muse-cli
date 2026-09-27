#!/usr/bin/env bash
# Launch muse-cli from the project directory.
cd "$(dirname "$0")"
exec python3 -m muse_cli "$@"
