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

from . import agent, core, prsync  # noqa: E402

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
.plugin-status { border-radius: 999px; padding: 4px 10px; font-weight: 600; background: alpha(currentColor, 0.10); }
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
        self.auto_button = Gtk.Button(label="Sync all", valign=Gtk.Align.CENTER)
        self.auto_button.set_tooltip_text(
            "Sync enabled PR branches and install their updates")
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
            "Open PR branches are updated on GitHub first, then installed locally. "
            "Conflicts and other errors need your attention.",
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
    """One discovered plugin, its PR selection and last sync result."""

    def __init__(self, window: CarryWindow, pl: core.Plugin):
        super().__init__(title=pl.id)
        self.window, self.pid = window, pl.id
        entry = core.plugin_state(pl.id)
        want = core.policy(pl.id)
        last = entry.get("last_sync")
        if last:
            self.set_subtitle(last.get("message", ""))
            label = ("Needs attention" if not last.get("ok") else
                     "Synced" if want["auto"] else "Paused")
        else:
            self.set_subtitle("No sync attempted yet")
            label = "Enabled" if want["auto"] else "Paused"
        chip = Gtk.Label(label=label, valign=Gtk.Align.CENTER)
        chip.add_css_class("success" if label in ("Synced", "Enabled") else "warning" if label == "Needs attention" else "dim-label")
        chip.add_css_class("caption")
        chip.add_css_class("plugin-status")
        self.add_suffix(chip)

        switch = Adw.SwitchRow(title="Automatic sync", subtitle="Sync after Omarchy updates", active=bool(want["auto"]))
        switch.connect("notify::active", lambda row, _: window.run("policy", lambda: core.set_policy(pl.id, auto=row.get_active())))
        self.add_row(switch)
        tracked = entry.get("tracked_branch")
        if tracked:
            description = (f"{tracked.get('upstream')}:{tracked.get('base')} ← "
                           f"{tracked.get('head_repo')}:{tracked.get('head')}")
            branch_row = Adw.ActionRow(title="Tracked GitHub branch", subtitle=description)
            edit = Gtk.Button(label="Change", valign=Gtk.Align.CENTER)
            edit.connect("clicked", lambda *_: TrackBranchDialog(window, pl).present(window))
            branch_row.add_suffix(edit)
            clear = Gtk.Button(label="Use PRs", valign=Gtk.Align.CENTER)
            clear.connect("clicked", lambda *_: window.run("track", lambda: prsync.clear_tracked_branch(pl.id)))
            branch_row.add_suffix(clear)
            self.add_row(branch_row)
        else:
            self.selection = Adw.ComboRow(title="Upstream and PR branch", subtitle="Looking for open PRs…")
            self.add_row(self.selection)
            track_row = Adw.ActionRow(title="Personal branch", subtitle="Keep a GitHub branch updated without a PR")
            track = Gtk.Button(label="Track branch", valign=Gtk.Align.CENTER)
            track.connect("clicked", lambda *_: TrackBranchDialog(window, pl).present(window))
            track_row.add_suffix(track)
            self.add_row(track_row)
        if not pl.upstream_ref:
            upstream_row = Adw.ActionRow(title="Upstream repository", subtitle="Choose the project your PR targets")
            upstream_button = Gtk.Button(label="Set upstream", valign=Gtk.Align.CENTER)
            upstream_button.connect("clicked", lambda *_: UrlDialog(window, core.inspect_plugin(pl.id)).present(window))
            upstream_row.add_suffix(upstream_button)
            self.add_row(upstream_row)
        self.choices = []
        self.loading = True
        if not tracked:
            threading.Thread(target=self.load_prs, daemon=True).start()
        sync = Adw.ActionRow(title="Sync now", subtitle="Update the GitHub branch, then install it locally")
        button = Gtk.Button(label="Sync now", valign=Gtk.Align.CENTER)
        button.add_css_class("suggested-action")
        button.connect("clicked", lambda *_: window.run("sync", lambda: prsync.sync(pl.id)))
        sync.add_suffix(button)
        self.add_row(sync)
        if last and not last.get("ok"):
            details = last.get("message", "")
            if last.get("files"):
                details += "\nFiles: " + ", ".join(last["files"])
            error_row = Adw.ActionRow(title="Last sync error", subtitle=details)
            copy_button = Gtk.Button(label="Copy error", valign=Gtk.Align.CENTER)
            copy_button.set_tooltip_text("Copy the error and diagnostic context")
            copy_button.connect("clicked", self.copy_error)
            error_row.add_suffix(copy_button)
            help_button = Gtk.Button(label="Ask agent", valign=Gtk.Align.CENTER)
            help_button.set_tooltip_text("Open your default Omarchy agent with the sync failure details")
            help_button.connect("clicked", lambda *_: window.run("agent", lambda: agent.launch(pl.id)))
            error_row.add_suffix(help_button)
            self.add_row(error_row)

    def copy_error(self, *_):
        try:
            prompt = agent.diagnostic_prompt(self.pid)
            self.get_display().get_clipboard().set(prompt)
        except (core.GitError, OSError, RuntimeError) as exc:
            self.window.toast(f"Could not copy error: {exc}")
            return
        self.window.toast("Error details copied")

    def load_prs(self):
        try:
            choices = prsync.discover_prs(self.pid)
            GLib.idle_add(self.show_prs, choices, None)
        except Exception as exc:
            GLib.idle_add(self.show_prs, [], str(exc))

    def show_prs(self, choices, error):
        self.choices = choices
        labels = [f"{p['upstream']}:{p['base']} ← {p['head_repo']}:{p['head']}  ·  {len(p['prs'])} PR(s)" for p in choices]
        self.selection.set_model(Gtk.StringList.new(labels))
        selected = core.plugin_state(self.pid).get("pr_selection")
        if choices:
            index = next((i for i, p in enumerate(choices) if p['key'] == selected), None)
            self.selection.set_selected(index if index is not None else (0 if len(choices) == 1 else Gtk.INVALID_LIST_POSITION))
            self.selection.set_subtitle("Choose a PR branch" if len(choices) > 1 and index is None else "Open pull requests")
            self.selection.connect("notify::selected", self.select_pr)
        else:
            self.selection.set_subtitle(error or "No open PR found")
            self.selection.set_sensitive(False)
        self.loading = False
        return False

    def select_pr(self, row, _):
        index = row.get_selected()
        if self.loading or index >= len(self.choices):
            return
        entry = core.plugin_state(self.pid)
        entry['pr_selection'] = self.choices[index]['key']
        core.save_plugin_state(self.pid, entry)
        row.set_subtitle("Open pull requests")


class TrackBranchDialog(Adw.Dialog):
    """Explicitly select the two GitHub branches for a personal change."""

    def __init__(self, window: CarryWindow, pl: core.Plugin):
        super().__init__(title=f"Track a branch for {pl.id}", content_width=560, content_height=500)
        self.window, self.pid = window, pl.id
        saved = core.plugin_state(pl.id).get("tracked_branch") or {}
        upstream_url = pl.remotes.get(pl.upstream_remote or "") or pl.upstream_candidate or ""
        upstream = core.repo_key(upstream_url)
        if upstream.startswith("github.com/"):
            upstream = upstream[len("github.com/"):]
        else:
            upstream = ""
        # A fork may be called origin when the real project is upstream.
        fork_url = pl.remotes.get("fork", "")
        if not fork_url and pl.upstream_remote != "origin":
            fork_url = pl.remotes.get("origin", "")
        fork = core.repo_key(fork_url)
        fork = fork[len("github.com/"):] if fork.startswith("github.com/") else ""
        base = (pl.upstream_ref or "").split("/", 1)[-1]
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8, margin_start=18,
                      margin_end=18, margin_top=12, margin_bottom=12)
        note = Gtk.Label(label="Choose the upstream branch and your GitHub branch. Sync updates your branch first, then installs it locally.", wrap=True, xalign=0)
        box.append(note)
        self.fields = {}
        for key, title, default in (
            ("upstream", "Upstream GitHub repository", upstream),
            ("base", "Upstream branch", base),
            ("head_repo", "Your GitHub repository", fork),
            ("head", "Your GitHub branch", pl.branch or ""),
        ):
            row = Adw.EntryRow(title=title)
            row.set_text(saved.get(key, default))
            self.fields[key] = row
            box.append(row)
        self.error = Gtk.Label(wrap=True, xalign=0, visible=False)
        self.error.add_css_class("error")
        box.append(self.error)
        actions = Gtk.Box(spacing=8, halign=Gtk.Align.END, margin_top=8)
        cancel = Gtk.Button(label="Cancel")
        cancel.connect("clicked", lambda *_: self.close())
        save = Gtk.Button(label="Track branch")
        save.add_css_class("suggested-action")
        save.connect("clicked", self.on_save)
        actions.append(cancel)
        actions.append(save)
        box.append(actions)
        self.set_child(box)
        self.suggested_login = None
        self.last_fork_suggestion = None
        if not self.fields["head_repo"].get_text().strip():
            self.fields["upstream"].connect("changed", self.suggest_fork)
            threading.Thread(target=self.load_github_login, daemon=True).start()

    def load_github_login(self):
        login = prsync.github_login()
        GLib.idle_add(self.on_github_login, login)

    def on_github_login(self, login):
        self.suggested_login = login
        self.suggest_fork()
        return False

    def suggest_fork(self, *_):
        current = self.fields["head_repo"].get_text().strip()
        if not self.suggested_login or (current and current != self.last_fork_suggestion):
            return
        upstream = prsync.github_repository(self.fields["upstream"].get_text())
        if upstream:
            self.last_fork_suggestion = f"{self.suggested_login}/{upstream.split('/', 1)[1]}"
            self.fields["head_repo"].set_text(self.last_fork_suggestion)

    def on_save(self, *_):
        values = {key: row.get_text().strip() for key, row in self.fields.items()}
        result = prsync.set_tracked_branch(self.pid, **values)
        if not result.ok:
            self.error.set_text(result.message)
            self.error.set_visible(True)
            return
        self.close()
        self.window.toast(result.message)
        self.window.refresh()


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
            version="0.1.0",
            comments=("Carry your own changes to Omarchy shell plugins across upstream updates: "
                      "rebase them onto the newest upstream commit, install the result, and hand "
                      "the plugin back to normal updates once your change is merged."),
        ).present(self.window)


def run(argv=None) -> int:
    return CarryApp().run(argv if argv is not None else [])
