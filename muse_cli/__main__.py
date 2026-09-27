"""muse-cli entry point."""
from __future__ import annotations

import argparse
import os
import sys

from . import __version__
from .bridge import run_daemon
from .instance import claim, install_launchd, uninstall_launchd
from .paths import DEFAULT_SESSION


def main() -> None:
    # Subcommands first: `muse-cli setup`, `muse-cli doctor`.
    if len(sys.argv) > 1 and sys.argv[1] in ("setup", "doctor"):
        from .setup import cmd_doctor, cmd_setup
        sys.exit(cmd_setup() if sys.argv[1] == "setup" else cmd_doctor())

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
        # /restart (or the tui-cmd restart op) sets MUSE_CLI_RESTART: re-exec
        # run.sh with the original argv so the new process picks up new code.
        # execv keeps our pid, so the instance lock (keyed by pid) stays ours.
        if os.environ.pop("MUSE_CLI_RESTART", None) == "1":
            root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
            script = os.path.join(root, "run.sh")
            try:
                if os.path.isfile(script):
                    os.execv(script, [script] + sys.argv[1:])
                else:
                    os.execv(sys.executable,
                             [sys.executable, "-m", "muse_cli"] + sys.argv[1:])
            except OSError:
                pass


if __name__ == "__main__":
    main()
