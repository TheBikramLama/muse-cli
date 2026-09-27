"""muse-cli entry point."""
from __future__ import annotations

import argparse

from . import __version__
from .bridge import run_daemon
from .instance import claim, install_launchd, uninstall_launchd
from .paths import DEFAULT_SESSION


def main() -> None:
    p = argparse.ArgumentParser(prog="muse-cli", description="Full-screen TUI terminal bridge for Muse.")
    p.add_argument("--daemon", action="store_true",
                   help="run the bridge without the TUI (plain stdout logging)")
    p.add_argument("--session", default=DEFAULT_SESSION, metavar="NAME",
                   help="instance session (default: main). Run parallel TUIs "
                        "with different sessions; queue items, messages and "
                        "replies route per session.")
    p.add_argument("--install-launchd", action="store_true",
                   help="install the --daemon bridge as a macOS LaunchAgent")
    p.add_argument("--uninstall-launchd", action="store_true",
                   help="remove the LaunchAgent again")
    p.add_argument("--version", action="version", version=f"muse-cli {__version__}")
    args = p.parse_args()
    if args.install_launchd:
        install_launchd()
    elif args.uninstall_launchd:
        uninstall_launchd()
    elif args.daemon:
        claim("daemon", args.session)
        run_daemon(args.session)
    else:
        claim("tui", args.session)
        from .app import MuseCliApp
        MuseCliApp(session=args.session).run()


if __name__ == "__main__":
    main()
