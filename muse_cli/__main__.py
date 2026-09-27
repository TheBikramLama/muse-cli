"""muse-cli entry point."""
from __future__ import annotations

import argparse

from . import __version__
from .bridge import run_daemon


def main() -> None:
    p = argparse.ArgumentParser(prog="muse-cli", description="Full-screen TUI terminal bridge for Muse.")
    p.add_argument("--daemon", action="store_true",
                   help="run the bridge without the TUI (plain stdout logging)")
    p.add_argument("--version", action="version", version=f"muse-cli {__version__}")
    args = p.parse_args()
    if args.daemon:
        run_daemon()
    else:
        from .app import MuseCliApp
        MuseCliApp().run()


if __name__ == "__main__":
    main()
