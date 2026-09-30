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
    # Agent companion heartbeat: `muse-cli report --agent <id>
    # --task "..." --status "..."` drops ~/.muse/activity/<id>.json so the
    # TUI shows live agent presence (see muse_cli/activity.py).
    if len(sys.argv) > 1 and sys.argv[1] == "report":
        from .activity import mark_done, report
        rp = argparse.ArgumentParser(
            prog="muse-cli report",
            description="Heartbeat agent activity to the TUI dashboard.")
        rp.add_argument("--agent", required=True,
                        help="stable agent id, e.g. side-chat:<id>")
        rp.add_argument("--task", default="", help="what the task is")
        rp.add_argument("--status", default="",
                        help="what you are doing right now")
        rp.add_argument("--todo", default="",
                        help="your todo filename under ~/.muse/todos/")
        rp.add_argument("--label", default="",
                        help="display name, e.g. your side-chat's title")
        rp.add_argument("--state", default="", choices=("working", "waiting",
                        "stalled", "done", "failed"),
                        help="lifecycle state; 'waiting' means blocked on "
                             "the user (say why with --reason)")
        rp.add_argument("--reason", default="",
                        help="why waiting/stalled/failed/done")
        rp.add_argument("--done", action="store_true",
                        help="mark finished: stays visible as done ~5 min")
        rargs = rp.parse_args(sys.argv[2:])
        if rargs.done:
            print(mark_done(rargs.agent, reason=rargs.reason))
        else:
            print(report(rargs.agent, task=rargs.task, status=rargs.status,
                         todo=rargs.todo, label=rargs.label,
                         state=rargs.state, reason=rargs.reason))
        return
    # Subcommands: `muse-cli setup`, `muse-cli doctor`, `muse-cli pair`,
    # `muse-cli unpair`.
    if len(sys.argv) > 1 and sys.argv[1] in ("setup", "doctor", "pair", "unpair"):
        from .setup import cmd_doctor, cmd_pair, cmd_setup, cmd_unpair
        cmds = {"setup": cmd_setup, "doctor": cmd_doctor,
                "pair": cmd_pair, "unpair": cmd_unpair}
        sys.exit(cmds[sys.argv[1]]())

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
