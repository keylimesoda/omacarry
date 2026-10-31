"""GTK4 + libadwaita window for omacarry.

One row per git-managed plugin, one verb per row: the next thing to do, with its
reason as the subtitle and a blocker sentence in its place when it cannot run.
Everything else on the row is read-only detail or a switch you set once. Engine
calls run off the main thread, so a fetch never freezes the window.
"""

from __future__ import annotations

import os
import threading

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
gi.require_version("Gdk", "4.0")
from gi.repository import Adw, Gdk, Gio, GLib, Gtk  # noqa: E402

from . import core  # noqa: E402

STATE_LABEL = {
    "stock": "In sync",
    "ffable": "Behind",
    "carrying": "Carrying",
    "diverged": "Diverged",
    "dirty": "Uncommitted",
    "merged": "Merged upstream",
    "rebasing": "Resolving",
    "detached": "Detached HEAD",
    "no-upstream": "No upstream",
}
STATE_CLASS = {
    "stock": "success",
    "ffable": "accent",
    "carrying": "warning",
    "diverged": "warning",
    "dirty": "warning",
    "merged": "success",
    "rebasing": "error",
    "detached": "warning",
    "no-upstream": "warning",
}
CSS = b"""
.summary { font-family: monospace; }
"""


def mono_row(lines: list[str]) -> Adw.ActionRow:
    """One monospace block inside an expander."""
    row = Adw.ActionRow()
    label = Gtk.Label(label="\n".join(lines) if lines else "nothing")
    label.set_xalign(0)
    label.set_selectable(True)
    label.set_wrap(True)
    label.add_css_class("summary")
    label.add_css_class("dim-label")
    row.add_prefix(label)
    return row


class CarryWindow(Adw.ApplicationWindow):
    def __init__(self, app: Adw.Application):
        super().__init__(application=app, default_width=1000, default_height=760)
        self.set_title("omacarry")
        self.plugins: list[core.Plugin] = []
        self.busy = 0
        self.banner_plugin = ""

        self.overlay = Adw.ToastOverlay()
        self.set_content(self.overlay)
        toolbar = Adw.ToolbarView()
        self.overlay.set_child(toolbar)

        header = Adw.HeaderBar()
        self.headline = Adw.WindowTitle(title="Plugin updates", subtitle="loading")
        header.set_title_widget(self.headline)
        self.spinner = Gtk.Spinner(valign=Gtk.Align.CENTER, margin_end=6)
        self.show_sync = Gtk.Switch(valign=Gtk.Align.CENTER, active=False)
        sync_label = Gtk.Label(label="other plugins", valign=Gtk.Align.CENTER)
        sync_label.add_css_class("dim-label")
        header.pack_end(self.spinner)
        header.pack_end(self.show_sync)
        header.pack_end(sync_label)
        self.auto_button = Gtk.Button(label="Auto pass", valign=Gtk.Align.CENTER)
        self.auto_button.set_tooltip_text(
            "The post-update pass: rebase and install what is predicted clean, let "
            "Omarchy's updater take plugins with no delta of yours, notify the rest")
        header.pack_end(self.auto_button)
        hook_button = Gtk.Button(label="Hook", valign=Gtk.Align.CENTER)
        hook_button.set_tooltip_text("Run the auto pass automatically after every omarchy update")
        header.pack_end(hook_button)
        refresh = Gtk.Button.new_from_icon_name("view-refresh-symbolic")
        refresh.set_tooltip_text("Re-read every plugin checkout")
        header.pack_end(refresh)
        toolbar.add_top_bar(header)

        self.banner = Adw.Banner()
        self.banner.set_button_label("See instructions")
        self.banner.set_revealed(False)
        toolbar.add_top_bar(self.banner)

        self.scrolled = Gtk.ScrolledWindow(vexpand=True, hexpand=True)
        self.scrolled.set_child(Adw.PreferencesPage())
        toolbar.set_content(self.scrolled)

        refresh.connect("clicked", lambda *_: self.refresh())
        self.auto_button.connect("clicked", lambda *_: self.run("auto pass", core.auto_pass))
        hook_button.connect("clicked", lambda *_: self.confirm_hook())
        self.banner.connect("button-clicked", lambda *_: self.open_conflict(self.banner_plugin))
        self.show_sync.connect("state-set", lambda *_: self.refresh_and_allow())

        self.refresh()

    # ------------------------------------------------------------- feedback

    def toast(self, text: str) -> None:
        self.overlay.add_toast(Adw.Toast(title=text, timeout=4))

    def refresh_and_allow(self) -> bool:
        self.refresh()
        return False  # let the switch flip

    def set_busy(self, busy: bool) -> None:
        self.busy += 1 if busy else -1
        busy = self.busy > 0
        if busy:
            self.spinner.start()
        else:
            self.spinner.stop()
        self.auto_button.set_sensitive(not busy)

    def run(self, label: str, action, then=None) -> None:
        """Run an engine call off the UI thread; deliver its Result back on it."""
        self.set_busy(True)

        def worker():
            try:
                result = action()
            except core.GitError as exc:
                result = core.Result(False, label, "-", str(exc))
            except Exception as exc:  # a surprise must not kill the window
                result = core.Result(False, label, "-", f"{type(exc).__name__}: {exc}")
            GLib.idle_add(self._finished, result, then)

        threading.Thread(target=worker, daemon=True).start()

    def _finished(self, result, then) -> bool:
        self.set_busy(False)
        if isinstance(result, list):  # auto pass
            self.show_auto(result)
            self.refresh()
            return False
        self.toast(("* " if result.ok else "! ") + result.message)
        if then:
            then(result)
        self.refresh()
        return False

    def show_auto(self, results: list) -> None:
        needed = [res for res in results if not res.ok]
        if not results:
            self.toast("nothing to carry")
        elif needed:
            self.toast(f"{len(results) - len(needed)} done, {len(needed)} need you")
            for res in needed:
                self.toast(f"! {res.plugin}: {res.message}")
        else:
            self.toast(f"{len(results)} plugin(s) carried and installed")

    def confirm_hook(self) -> None:
        self.confirm(
            "Run the auto pass after every Omarchy update?",
            "Writes omacarry's post-update hook into "
            "~/.config/omarchy/hooks/post-update.d/, naming the launcher it was installed from. "
            "Predicted-clean replays get rebaselined and installed on their own; conflicts notify "
            "you instead.",
            "Install hook",
            lambda: self.run("hook", core.install_hook))

    def confirm(self, title: str, body: str, response: str, on_yes) -> None:
        dialog = Adw.AlertDialog(heading=title, body=body)
        dialog.add_response("cancel", "Cancel")
        dialog.add_response("yes", response)
        dialog.set_response_appearance("yes", Adw.ResponseAppearance.SUGGESTED)
        dialog.set_default_response("cancel")
        dialog.connect("response", lambda _d, answer: on_yes() if answer == "yes" else None)
        dialog.present(self)

    # ------------------------------------------------------------- listing

    def refresh(self) -> None:
        debug("scan start")
        try:
            self.plugins = core.scan()
            debug(f"scan done: {len(self.plugins)} plugins")
        except core.GitError as exc:
            self.toast(f"scan failed: {exc}")
            return

        carrying = [pl for pl in self.plugins if pl.needs_attention or pl.state == "merged"]
        quiet = [pl for pl in self.plugins if pl not in carrying]

        page = Adw.PreferencesPage()
        attention = Adw.PreferencesGroup(
            title="Your plugins",
            description="Discovered automatically; local changes are carried forward when enabled")
        for pl in carrying:
            attention.add(PluginRow(self, pl))
        if not carrying:
            attention.add(Adw.ActionRow(
                title="Nothing to carry",
                subtitle="every plugin checkout is a plain copy of its upstream"))
        page.add(attention)
        if self.show_sync.get_active():
            synced = Adw.PreferencesGroup(title="Other plugins",
                                          description="Omarchy handles these updates")
            for pl in quiet:
                synced.add(PluginRow(self, pl))
            page.add(synced)
        self.scrolled.set_child(page)

        self.headline.set_subtitle(
            f"{len(carrying)} of {len(self.plugins)} plugins carry local work")
        stalled = [pl for pl in self.plugins if pl.rebasing]
        if stalled:
            self.banner_plugin = stalled[0].id
            self.banner.set_title(f"{len(stalled)} plugin(s) stopped mid-rebase")
            self.banner.set_revealed(True)
        else:
            self.banner.set_revealed(False)

    def open_conflict(self, plugin_id: str) -> None:
        if not plugin_id:
            return
        pl = core.inspect_plugin(plugin_id)
        files = core.conflict_files(plugin_id) or core.staged_files(plugin_id)
        body = (f"Repository: {pl.repo}\nBranch: {pl.branch or '(detached)'}\n"
                + ("Files: " + ", ".join(files) + "\n" if files else "")
                + "Resolve the conflict online or ask your Omarchy agent for help. "
                  "If a rebase is already in progress, finish or abort it in Git.")
        dialog = Adw.AlertDialog(heading=f"{plugin_id}: resolution instructions", body=body)
        dialog.add_response("close", "Close")
        dialog.present(self)



class PluginRow(Adw.ExpanderRow):
    """One plugin: at most one verb, one switch, everything else behind them."""

    def __init__(self, window: CarryWindow, pl: core.Plugin):
        super().__init__(title=pl.id)
        self.window = window
        self.pid = pl.id
        want = core.policy(pl.id)
        verb, what, blocker = core.describe_action(pl)
        self.strategy = (core.replay_strategy(pl.repo, pl.baseline or pl.head, pl.head)
                         if pl.behind else "rebase")
        upstream = pl.remotes.get(pl.upstream_remote or "", pl.upstream_ref or "unknown")
        if pl.state in ("no-upstream", "detached", "rebasing") or blocker or ((pl.dirty or pl.untracked) and not want["carry_dirty"]):
            status = "Needs attention"
        elif not want["auto"]:
            status = "Paused"
        elif pl.state in ("stock", "merged"):
            status = "Up to date"
        else:
            status = "Enabled"
        summary = f"{len(pl.delta)} local commit(s) · upstream {upstream}"
        self.set_subtitle((blocker or what) if status == "Needs attention" else summary)

        chip = Gtk.Label(label=status)
        chip.add_css_class("success" if status in ("Enabled", "Up to date") else "warning" if status == "Needs attention" else "dim-label")
        chip.add_css_class("caption")
        self.add_suffix(chip)

        loop = Gtk.Switch(valign=Gtk.Align.CENTER, active=bool(want["auto"]))
        loop.set_tooltip_text("Enable automatic updates after Omarchy updates")
        loop.connect("state-set", self.set_in_loop)
        self.add_suffix(loop)

        if verb:
            action = Gtk.Button(label=verb)
            action.set_valign(Gtk.Align.CENTER)
            action.add_css_class("suggested-action")
            if blocker:
                action.set_sensitive(False)
                action.set_tooltip_text(blocker)
            action.connect("clicked", lambda *_: self.run_verb(verb))
            self.add_suffix(action)

        headline = Adw.ActionRow(title=verb or "Nothing to do", subtitle=blocker or what)
        self.add_row(headline)
        self.add_row(self.works_in_row(pl))

        technical = Adw.ExpanderRow(title="Technical details")
        lines = [f"branch      {pl.branch or '(detached)'} at {pl.head[:9]}"]
        if pl.baseline:
            lines.append(f"baseline    {pl.baseline[:9]}")
        lines.append(f"origin      {pl.origin_url or '-'}")
        lines += [f"remote {name}  {url}" for name, url in pl.remotes.items() if name != "origin"]
        shown_ref = pl.upstream_ref or "(none)"
        lines.append(f"upstream    {shown_ref}")
        lines.append(f"note        {pl.note}")
        technical.add_row(mono_row(lines))
        if pl.delta:
            technical.add_row(mono_row(["my delta"] + [f"  {line}" for line in pl.delta]))
        if pl.new_upstream:
            shown = pl.new_upstream[:40]
            technical.add_row(mono_row([f"upstream new ({pl.behind})"] + [f"  {line}" for line in shown]
                                  + ([f"  ... {pl.behind - 40} more"] if pl.behind > 40 else [])))
        if pl.dirty or pl.untracked:
            technical.add_row(mono_row(["uncommitted"] + [f"  M {name}" for name in pl.dirty]
                                  + [f"  ? {name}" for name in pl.untracked]))

        self.add_row(technical)
        extra = self.secondary_actions(pl, want)
        if extra:
            self.add_row(extra)
        self.add_row(self.policy_section(pl, want))

    # ------------------------------------------------------------------ verbs

    def run_verb(self, verb: str) -> None:
        if verb == "Resolve":
            self.window.open_conflict(self.pid)
        elif verb == "Record upstream":
            UrlDialog(self.window, core.inspect_plugin(self.pid)).present(self.window)
        elif verb == "Resume normal updates":
            self.window.confirm(
                f"Hand {self.pid} back to Omarchy?",
                "origin becomes the real upstream project, your fork moves to the remote 'fork', "
                "and the carry branch is deleted. Future updates come from omarchy plugin update.",
                "Resume normal updates",
                lambda: self.window.run("resume", lambda: core.resume(self.pid)))
        else:
            self.window.run("carry", lambda: core.carry(self.pid), then=self.after_carry)

    def after_carry(self, res) -> None:
        """A rehearsed conflict is not a dead end: show the way through it."""
        if res.ok or not res.files:
            return
        pl = core.inspect_plugin(self.pid)
        body = (f"Repository: {pl.repo}\nBranch: {pl.branch or '(detached)'}\n"
                f"Conflicting files: {', '.join(res.files)}\n\n"
                "Your checkout was not changed. Resolve the conflict online or ask your "
                "Omarchy agent for help, then update the plugin again.")
        dialog = Adw.AlertDialog(heading=f"{self.pid}: resolution instructions", body=body)
        dialog.add_response("close", "Close")
        dialog.present(self.window)

    # ----------------------------------------------------------- detail parts

    def set_in_loop(self, _switch, state) -> bool:
        self.window.run("policy", lambda: core.set_policy(self.pid, auto=state))
        return False  # let the switch move on screen; the engine write runs alongside

    def works_in_row(self, pl: core.Plugin) -> Adw.ActionRow:
        row = Adw.ActionRow(title="Works in", subtitle=str(pl.repo))
        if pl.repo != pl.path:
            back = Gtk.Button(label="Back to the plugins dir")
            back.set_valign(Gtk.Align.CENTER)
            back.connect("clicked",
                         lambda *_: self.window.run("repo", lambda: core.clear_repo(self.pid)))
            row.add_suffix(back)
        change = Gtk.Button(label="Use a dev checkout")
        change.set_valign(Gtk.Align.CENTER)
        change.set_tooltip_text("Do the rebase in your own repo; install deploys it to the plugins dir")
        change.connect("clicked",
                       lambda *_: RepoDialog(self.window, core.inspect_plugin(self.pid))
                       .present(self.window))
        row.add_suffix(change)
        return row

    def secondary_actions(self, pl: core.Plugin, want: dict):
        """Things you would not press on your own, so they sit one level down."""
        row = Adw.ActionRow(title="Other actions")
        plan = Gtk.Button(label="Plan")
        plan.set_valign(Gtk.Align.CENTER)
        plan.set_tooltip_text("Rehearse the replay in a throwaway worktree; change nothing")
        plan.connect("clicked", lambda *_: self.window.run(
            "plan", lambda: core.rebaseline(self.pid, dry_run=True)))
        row.add_suffix(plan)
        if pl.repo != pl.path:
            install = Gtk.Button(label="Install")
            install.set_valign(Gtk.Align.CENTER)
            install.set_tooltip_text("Deploy this branch into the plugins dir, validate, rescan")
            install.connect("clicked", lambda *_: self.window.run("install",
                                                                  lambda: core.deploy(self.pid)))
            row.add_suffix(install)
        if not want["push"] and ("fork" in pl.remotes or (pl.upstream_remote
                                                          and pl.upstream_remote != "origin")):
            push = Gtk.Button(label="Push once")
            push.set_valign(Gtk.Align.CENTER)
            push.set_tooltip_text("force-with-lease this branch to my fork, this time only")
            push.connect("clicked", lambda *_: self.window.run("push", lambda: core.push(self.pid)))
            row.add_suffix(push)
        return row

    def policy_section(self, pl: core.Plugin, want: dict) -> Adw.ExpanderRow:
        """The switches you set once and forget; in-the-loop lives on the row."""
        section = Adw.ExpanderRow(title="When omarchy update runs",
                                  subtitle="what may happen to this plugin with nobody there")
        for key, title, subtitle in (
            ("carry_dirty", "Carry uncommitted work",
             "stash it, replay, restore it; a clash stays in the stash, never half-applied"),
            ("push", "Keep my fork current",
             "force-with-lease this branch to my fork after a clean replay, so an open PR stays "
             "reviewable"),
            ("install", "Install the result",
             "validate the checkout and rescan the shell"),
        ):
            row = Adw.SwitchRow(title=title, subtitle=subtitle, active=want[key])
            row.connect("notify::active", self.policy_toggled(key))
            section.add_row(row)
        branch = Adw.EntryRow(title="Publish under this branch name (blank = the current one)")
        branch.set_text(want.get("branch") or "")
        apply_btn = Gtk.Button(label="Set")
        apply_btn.set_valign(Gtk.Align.CENTER)
        apply_btn.connect("clicked", lambda *_: self.window.run(
            "policy", lambda: core.set_policy(self.pid, branch=branch.get_text().strip())))
        branch.add_suffix(apply_btn)
        section.add_row(branch)
        return section

    def policy_toggled(self, key: str):
        def handler(row, _pspec) -> None:
            if getattr(row, "writing", False):
                return  # the refresh below rebuilds rows; don't echo that
            row.writing = True
            self.window.run("policy", lambda: core.set_policy(self.pid, **{key: row.get_active()}))
            row.writing = False
        return handler


class RepoDialog(Adw.Dialog):
    """Where this plugin is worked on: a dev checkout, or the plugins dir itself."""

    def __init__(self, window: CarryWindow, pl: core.Plugin):
        super().__init__(title=f"Where {pl.id} is worked on", content_width=560,
                         content_height=240)
        self.window = window
        self.pid = pl.id
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12, margin_start=18,
                      margin_end=18, margin_top=12, margin_bottom=12)
        info = Gtk.Label(label=f"currently: {pl.repo}\n"
                               "The replay happens in that checkout; installing deploys its "
                               "branch into the plugins dir the shell loads.")
        info.set_xalign(0)
        self.entry = Adw.EntryRow(title="path to your dev checkout")
        self.entry.set_text(str(pl.repo))
        actions = Gtk.Box(spacing=8, halign=Gtk.Align.END)
        cancel = Gtk.Button(label="Cancel")
        cancel.connect("clicked", lambda *_: self.close())
        use = Gtk.Button(label="Work here")
        use.add_css_class("suggested-action")
        use.connect("clicked", self.on_save)
        back = Gtk.Button(label="Use the plugins dir")
        back.connect("clicked", self.on_back)
        actions.pack_end(use)
        actions.pack_end(cancel)
        actions.pack_start(back)
        box.append(info)
        box.append(self.entry)
        box.append(actions)
        self.set_child(box)

    def on_save(self, *_):
        path = self.entry.get_text().strip()
        if not path:
            self.window.toast("no path given")
            return
        self.close()
        self.window.run("repo", lambda: core.set_repo(self.pid, path))

    def on_back(self, *_):
        self.close()
        self.window.run("repo", lambda: core.clear_repo(self.pid))


class UrlDialog(Adw.Dialog):
    def __init__(self, window: CarryWindow, pl: core.Plugin):
        super().__init__(title=f"Upstream repo for {pl.id}", content_width=560,
                         content_height=240)
        self.window = window
        self.pid = pl.id
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12, margin_start=18,
                      margin_end=18, margin_top=12, margin_bottom=12)
        info = Gtk.Label(label=f"origin is {pl.origin_url}\nThis is the repo a PR should go to:")
        info.set_xalign(0)
        self.entry = Adw.EntryRow(title="git URL")
        if pl.upstream_candidate:
            self.entry.set_text(pl.upstream_candidate)
        actions = Gtk.Box(spacing=8, halign=Gtk.Align.END)
        cancel = Gtk.Button(label="Cancel")
        cancel.connect("clicked", lambda *_: self.close())
        save = Gtk.Button(label="Add as 'upstream'")
        save.add_css_class("suggested-action")
        save.connect("clicked", self.on_save)
        actions.pack_end(save)
        actions.pack_end(cancel)
        box.append(info)
        box.append(self.entry)
        box.append(actions)
        self.set_child(box)

    def on_save(self, *_):
        url = self.entry.get_text().strip()
        if not url:
            self.window.toast("no URL given")
            return
        self.close()
        self.window.run("upstream", lambda: core.set_upstream(self.pid, url))


class ConflictDialog(Adw.Dialog):
    """A stopped rebase: edit each conflicted file, stage, then continue/skip/abort."""

    def __init__(self, window: CarryWindow, plugin_id: str, files: list[str]):
        super().__init__(title=f"Resolve {plugin_id}", content_width=900, content_height=640)
        self.window = window
        self.pid = plugin_id
        self.views: dict[str, Gtk.TextView] = {}

        root = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8, margin_start=12,
                       margin_end=12, margin_top=8, margin_bottom=8)
        note = Gtk.Label(label="Fix the <<<<<<< markers, Save & stage, then Continue.")
        note.add_css_class("dim-label")
        note.set_xalign(0)
        root.append(note)

        self.tabs = Gtk.Notebook()
        self.tabs.set_vexpand(True)
        root.append(self.tabs)
        self.load(files)

        bar = Gtk.CenterBox()
        left = Gtk.Box(spacing=8)
        abort = Gtk.Button(label="Abort rebase")
        abort.add_css_class("flat")
        abort.connect("clicked", lambda *_: self.act(core.rebase_abort, closing=True))
        skip = Gtk.Button(label="Skip commit")
        skip.set_tooltip_text("This commit's change is already upstream - drop it")
        skip.add_css_class("flat")
        skip.connect("clicked", lambda *_: self.act(core.rebase_skip))
        left.append(abort)
        left.append(skip)
        right = Gtk.Box(spacing=8)
        cont = Gtk.Button(label="Continue")
        cont.add_css_class("suggested-action")
        cont.connect("clicked", lambda *_: self.act(
            lambda pid: (self.save_all(), core.rebase_continue(pid))[1]))
        stage = Gtk.Button(label="Save & stage")
        stage.connect("clicked", lambda *_: self.act(self.save_all, keep_open=True))
        right.append(stage)
        right.append(cont)
        bar.set_start_widget(left)
        bar.set_end_widget(right)
        root.append(bar)
        self.set_child(root)

    def load(self, files: list[str]) -> None:
        for path in files:
            view = Gtk.TextView(monospace=True, wrap_mode=Gtk.WrapMode.CHAR)
            buffer = view.get_buffer()
            buffer.set_text(core.conflict_text(self.pid, path))
            scrolled = Gtk.ScrolledWindow(hexpand=True, vexpand=True)
            scrolled.set_child(view)
            self.tabs.append_page(scrolled, Gtk.Label(label=path))
            self.views[path] = view

    def text_of(self, view: Gtk.TextView) -> str:
        buffer = view.get_buffer()
        return buffer.get_text(buffer.get_start_iter(), buffer.get_end_iter(), True)

    def save_all(self, _pid: str | None = None) -> core.Result:
        results = [core.save_resolution(self.pid, path, self.text_of(view))
                   for path, view in self.views.items()]
        bad = [res for res in results if not res.ok]
        return bad[0] if bad else core.Result(True, "resolve", self.pid,
                                             f"{len(results)} file(s) staged")

    def act(self, action, closing: bool = False, keep_open: bool = False) -> None:
        def done(result) -> None:
            if isinstance(result, list):
                return
            self.window.toast(("* " if result.ok else "! ") + result.message)
            remaining = core.conflict_files(self.pid)
            if keep_open or remaining and not closing:
                if remaining:
                    self.reload(remaining)
            else:
                self.close()
            self.window.refresh()

        # Engine calls take the plugin id; the buffer-backed ones do not need it.
        self.window.run("resolve", lambda: action(self.pid), then=done)

    def reload(self, files: list[str]) -> None:
        for page in range(self.tabs.get_n_pages() - 1, -1, -1):
            self.tabs.remove_page(page)
        self.views.clear()
        self.load(files)


def debug(message: str) -> None:
    if os.environ.get("OMACARRY_DEBUG"):
        import sys
        print(f"omacarry: {message}", file=sys.stderr, flush=True)


class CarryApp(Adw.Application):
    def __init__(self):
        super().__init__(application_id="org.omacarry.App",
                         flags=Gio.ApplicationFlags.DEFAULT_FLAGS)
        self.window: CarryWindow | None = None

    def do_startup(self):  # type: ignore[override]
        Adw.Application.do_startup(self)
        provider = Gtk.CssProvider()
        provider.load_from_data(CSS)
        Gtk.StyleContext.add_provider_for_display(
            Gdk.Display.get_default(), provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)
        for name, callback in (("hook", self.on_hook), ("about", self.on_about)):
            action = Gio.SimpleAction.new(name, None)
            action.connect("activate", callback)
            self.add_action(action)
        menu = Gio.Menu()
        menu.append("Set up post-update hook", "app.hook")
        menu.append("About omacarry", "app.about")
        self.set_menubar(menu)

    def do_activate(self, *args):  # type: ignore[override]
        debug("activate")
        if self.window is None:
            self.window = CarryWindow(self)
            debug("window built")
        self.window.present()
        debug("presented")

    def on_hook(self, *_):
        if self.window:
            self.window.confirm_hook()

    def on_about(self, *_):
        Adw.AboutDialog(
            application_name="omacarry",
            developer_name="ric",
            version="0.1",
            comments=("Carry your own changes to Omarchy shell plugins across upstream updates: "
                      "rebase them onto the newest upstream commit, install the result, and hand "
                      "the plugin back to normal updates once your change is merged."),
        ).present(self.window)


def run(argv=None) -> int:
    return CarryApp().run(argv if argv is not None else [])
