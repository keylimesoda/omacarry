"""Command-line front end for the omacarry engine.

Same engine the GUI and the post-update hook use, so scripted and interactive
answers never diverge.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

from . import core

STATE_MARK = {
    "stock": "in sync",
    "ffable": "behind",
    "carrying": "carrying",
    "diverged": "diverged",
    "dirty": "dirty",
    "merged": "merged upstream",
    "rebasing": "resolving",
    "detached": "detached",
    "no-upstream": "no upstream",
}


def print_plugin(pl, verbose: bool) -> None:
    mark = STATE_MARK.get(pl.state, pl.state)
    line = (f"{pl.id:<28} {mark:<15} delta {len(pl.delta):<3} new {pl.behind:<4} "
            f"dirty {len(pl.dirty) + len(pl.untracked):<3}")
    print(line)
    print(f"{'':<28} branch {pl.branch or '(detached)'}  upstream {pl.upstream_ref or '-'}")
    want = core.policy(pl.id)
    print(f"{'':<28} unattended: " + "  ".join(
        f"{key}={'on' if want[key] else 'off'}" for key in core.POLICY_KEYS))
    verb, what, blocker = core.describe_action(pl)
    print(f"{'':<28} {'do: ' + verb if verb else 'nothing to do'} - {what}")
    if blocker:
        print(f"{'':<28} blocked: {blocker}")
    if pl.origin_url:
        print(f"{'':<28} origin  {pl.origin_url}")
    if pl.upstream_candidate:
        print(f"{'':<28} candidate upstream {pl.upstream_candidate} "
              f"(set with: omacarry upstream {pl.id} <url>)")
    if verbose:
        print(f"{'':<28} {pl.note}")
        for entry in pl.delta:
            print(f"    mine    {entry}")
        for entry in pl.new_upstream[:12]:
            print(f"    upstream {entry}")
        if pl.behind > 12:
            print(f"    ... {pl.behind - 12} more upstream commits")
        for entry in pl.dirty + pl.untracked:
            print(f"    uncommitted {entry}")


def report(res: core.Result) -> int:
    print(f"{'ok  ' if res.ok else 'need'} {res.action} {res.plugin}: {res.message}")
    for line in res.details:
        print(f"     {line}")
    for line in res.files:
        print(f"     ! {line}")
    return 0 if res.ok else 1


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        prog="omacarry",
        description="Carry local changes to Omarchy plugins across upstream updates.",
    )
    sub = parser.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("scan", help="list every git-managed plugin and its divergence")
    p.add_argument("--json", action="store_true")
    p.add_argument("--needs", action="store_true", help="only plugins with local work")

    p = sub.add_parser("status", help="detail one plugin")
    p.add_argument("plugin")
    p.add_argument("--json", action="store_true")

    p = sub.add_parser("plan", help="show what a rebaseline would do, changing nothing")
    p.add_argument("plugin")

    p = sub.add_parser("fetch", help="fetch remotes for one plugin")
    p.add_argument("plugin")

    p = sub.add_parser("carry", help="the one verb: update my branch onto upstream, push and install per policy")
    p.add_argument("plugin")

    p = sub.add_parser("rebase", help="replay my delta onto the newest upstream commit")
    p.add_argument("plugin")
    p.add_argument("--onto", help="upstream ref to land on (default: resolved upstream)")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--keep-conflict", action="store_true",
                   help="leave the rebase stopped in the worktree to resolve by hand")
    p.add_argument("--push", action="store_true", help="force-with-lease the carry branch to origin")
    p.add_argument("--force", action="store_true", help="carry uncommitted work via a stash")

    p = sub.add_parser("install", help="validate the checkout and rescan the shell")
    p.add_argument("plugin")
    p.add_argument("--no-rescan", action="store_true")

    p = sub.add_parser("resume", help="delta is upstream: drop the carry and go back to normal updates")
    p.add_argument("plugin")

    p = sub.add_parser("upstream", help="record the real upstream repo for a fork")
    p.add_argument("plugin")
    p.add_argument("url")

    p = sub.add_parser("conflicts", help="list conflicted files of a stopped rebase")
    p.add_argument("plugin")
    p.add_argument("--show", metavar="PATH", help="print the conflicted file contents")

    p = sub.add_parser("resolve", help="write a resolution for one conflicted file and stage it")
    p.add_argument("plugin")
    p.add_argument("path")
    p.add_argument("--file", metavar="FILE", help="take the resolution from FILE ('-' = stdin)")

    for name, help_text in (
        ("continue", "finish the stopped rebase"),
        ("skip", "drop the commit that conflicted (already upstream)"),
        ("abort", "abandon the rebase and go back"),
    ):
        p = sub.add_parser(name, help=help_text)
        p.add_argument("plugin")

    p = sub.add_parser("policy", help="what the unattended update pass may do per plugin")
    p.add_argument("plugin")
    p.add_argument("--auto", choices=["on", "off"],
                   help="rebaseline this plugin automatically after an update (default on)")
    p.add_argument("--dirty", choices=["on", "off"],
                   help="carry uncommitted work through a stash (default off)")
    p.add_argument("--push", choices=["on", "off"],
                   help="update my fork's branch after a clean replay, so a PR stays current "
                        "(default off)")
    p.add_argument("--install", choices=["on", "off"],
                   help="deploy the result into the plugins dir and rescan the shell (default on)")
    p.add_argument("--branch", help="which branch carries my delta")
    p.add_argument("--show", action="store_true", help="print the current policy and exit")

    p = sub.add_parser("repo", help="do the work in a dev checkout; install deploys it to the plugins dir")
    p.add_argument("plugin")
    p.add_argument("path")

    sub.add_parser("setup", help="list forks that still need their real upstream recorded")

    p = sub.add_parser("push", help="force-with-lease the carry branch to my fork")
    p.add_argument("plugin")

    sub.add_parser("hook-install", help="install the post-update hook into ~/.config/omarchy/hooks")

    p = sub.add_parser("auto", help="non-interactive pass for a post-update hook")
    p.add_argument("--notify", action="store_true", help="desktop-notify each plugin needing attention")
    p.add_argument("--log", action="store_true", help="append the pass to ~/.config/omacarry/hook.log")

    sub.add_parser("gui", help="open the window")

    args = parser.parse_args(argv)

    if args.cmd == "scan":
        plugins = core.scan()
        if args.needs:
            plugins = [pl for pl in plugins if pl.needs_attention]
        if args.json:
            print(json.dumps([pl.as_dict() for pl in plugins], indent=2))
        else:
            if not plugins:
                print("nothing to carry: every plugin is a plain upstream checkout")
            for pl in plugins:
                print_plugin(pl, verbose=False)
        return 0

    if args.cmd == "status":
        pl = core.inspect_plugin(args.plugin)
        if args.json:
            print(json.dumps(pl.as_dict(), indent=2))
        else:
            print_plugin(pl, verbose=True)
        return 0

    if args.cmd == "plan":
        res = core.rebaseline(args.plugin, dry_run=True)
        return report(res)

    if args.cmd == "fetch":
        return report(core.fetch(args.plugin))

    if args.cmd == "carry":
        return report(core.carry(args.plugin))

    if args.cmd == "rebase":
        res = core.rebaseline(
            args.plugin, onto=args.onto, dry_run=args.dry_run,
            keep_conflict=args.keep_conflict, push=args.push, force=args.force,
        )
        return report(res)

    if args.cmd == "install":
        return report(core.deploy(args.plugin, rescan=not args.no_rescan))

    if args.cmd == "resume":
        return report(core.resume(args.plugin))

    if args.cmd == "upstream":
        return report(core.set_upstream(args.plugin, args.url))

    if args.cmd == "conflicts":
        files = core.conflict_files(args.plugin)
        if not files:
            print("no rebase conflict in flight")
            return 0
        if args.show:
            print(core.conflict_text(args.plugin, args.show))
            return 0
        for name in files:
            print(name)
        return 1

    if args.cmd == "resolve":
        if not args.file:
            parser.error("resolve needs --file FILE (use '-' for stdin)")
        text = sys.stdin.read() if args.file == "-" else Path(args.file).read_text()
        return report(core.save_resolution(args.plugin, args.path, text))

    if args.cmd == "continue":
        return report(core.rebase_continue(args.plugin))

    if args.cmd == "skip":
        return report(core.rebase_skip(args.plugin))

    if args.cmd == "abort":
        return report(core.rebase_abort(args.plugin))

    if args.cmd == "policy":
        chosen = (args.auto, args.dirty, args.push, args.install, args.branch)
        if args.show or not any(chosen):
            want = core.policy(args.plugin)
            print(f"{args.plugin}: "
                  + "  ".join(f"{key}={'on' if want[key] else 'off'}"
                              for key in core.POLICY_KEYS)
                  + f"  branch={want.get('branch') or '(current)'}")
            return 0
        changes = {}
        for flag, key in ((args.auto, "auto"), (args.dirty, "carry_dirty"),
                          (args.push, "push"), (args.install, "install")):
            if flag:
                changes[key] = flag == "on"
        if args.branch:
            changes["branch"] = args.branch
        return report(core.set_policy(args.plugin, **changes))

    if args.cmd == "repo":
        return report(core.set_repo(args.plugin, args.path))

    if args.cmd == "setup":
        missing = core.plugins_with_upstream()
        if not missing:
            print("every fork has its real upstream recorded")
        for item in missing:
            print(f"{item['id']}: origin is {item['origin']}")
            if item["candidate"]:
                print(f"    record it:  omacarry upstream {item['id']} {item['candidate']}")
            else:
                print(f"    no upstream found ({item['note']}) - record it: "
                      f"omacarry upstream {item['id']} <url>")
        return 0

    if args.cmd == "push":
        return report(core.push(args.plugin))

    if args.cmd == "hook-install":
        return report(core.install_hook())

    if args.cmd == "auto":
        notify = None
        if args.notify:
            launcher = str(Path(core.__file__).resolve().parent.parent / "bin" / "omacarry")

            def notify(res):
                if res.ok:
                    return
                subprocess.run(("omarchy", "notification", "send",
                                "-u", "normal", "-g", "warning-symbolic",
                                f"Plugin fork needs you: {res.plugin}", res.message,
                                "--exec", launcher, "gui"), check=False)
        results = core.auto_pass(notify=notify)
        if args.log:
            core.append_hook_log(results)
        if not results:
            print("nothing to carry")
        for res in results:
            report(res)
        return 0 if all(res.ok for res in results) else 1

    if args.cmd == "gui":
        from .gui import run as run_gui
        return run_gui(sys.argv[:1])

    return 2


if __name__ == "__main__":
    raise SystemExit(main())
