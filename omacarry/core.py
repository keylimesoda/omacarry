"""Carry a local Omarchy plugin fork across upstream updates.

The problem this solves: ``omarchy plugin update`` fetches ``origin`` and does
``git merge --ff-only FETCH_HEAD``. A fork with any local commit or dirty file
can never fast-forward, so upstream updates stop arriving.

The model:

  upstream remote  the real project (where a PR would go)
  origin remote    the user's fork, when there is one
  carry branch     the checked-out branch holding the local delta
  baseline         the upstream commit the delta was last sitting on

``rebaseline`` replays the delta onto the newest upstream commit, ``deploy``
puts the plugin checkout in ~/.config/omarchy/plugins onto that result (the
directory the shell loads), ``resume`` hands the plugin back to normal
``omarchy plugin update`` once the delta is upstream.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path

# ---------------------------------------------------------------- locations


def plugins_dir() -> Path:
    """Plugin checkout directory; override for tests with OMARCHY_PLUGINS_DIR."""
    return Path(os.environ.get("OMARCHY_PLUGINS_DIR") or Path.home() / ".config/omarchy/plugins")


def config_dir() -> Path:
    return Path(os.environ.get("OMACARRY_CONFIG_DIR") or Path.home() / ".config/omacarry")


def state_file() -> Path:
    return config_dir() / "state.json"


def log_path() -> Path:
    return config_dir() / "hook.log"


class GitError(RuntimeError):
    pass


# ---------------------------------------------------------------- git


def git(repo, *args, check: bool = True, ignore_fail: bool = False, env: dict | None = None):
    run_env = None
    if env:
        run_env = dict(os.environ, **env)
    proc = subprocess.run(
        ("git", "-C", str(repo), *args), capture_output=True, text=True, env=run_env
    )
    out, err = proc.stdout.strip(), proc.stderr.strip()
    if proc.returncode and check and not ignore_fail:
        raise GitError(f"git {' '.join(args)}: {err or out or f'exit {proc.returncode}'}")
    return proc.returncode, out, err


def repo_key(url: str) -> str:
    """Transport-independent identity of a repo, for fork-vs-upstream checks."""
    url = url.strip().rstrip("/")
    if url.startswith("file://"):
        return "fs:" + str(Path(url[7:]).resolve())
    if url.endswith(".git"):
        url = url[:-4]
    m = re.match(r"^(?:ssh://|https?://|git://)?(?:[^@/]+@)?([^/]+)[:/](.+)$", url)
    if m:
        return f"{m.group(1).lower()}/{m.group(2).lstrip('/').lstrip('~').lower()}"
    if url.startswith(("/", "~", "./")):
        return "fs:" + str(Path(url).expanduser().resolve())
    return url.lower()


def clone_shaped(url: str) -> bool:
    """Could this URL be cloned? Host plus owner/name, not a docs page."""
    key = repo_key(url)
    if key.startswith("fs:"):
        return (key[3:] / ".git").exists() or (Path(key[3:]) / "manifest.json").exists()
    return len(key.split("/")) >= 3


# ---------------------------------------------------------------- state


def load_state() -> dict:
    try:
        raw = json.loads(state_file().read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        raw = {}
    if not isinstance(raw, dict):
        raw = {}
    raw.setdefault("plugins", {})
    return raw


def save_state(state: dict) -> None:
    path = state_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=2, sort_keys=True) + "\n")
    tmp.replace(path)


def plugin_state(plugin_id: str) -> dict:
    return load_state()["plugins"].setdefault(plugin_id, {})


def save_plugin_state(plugin_id: str, entry: dict) -> None:
    state = load_state()
    state["plugins"][plugin_id] = entry
    save_state(state)


def drop_plugin_state(plugin_id: str) -> None:
    state = load_state()
    state["plugins"].pop(plugin_id, None)
    save_state(state)


# ---------------------------------------------------------------- model


@dataclass
class Result:
    ok: bool
    action: str
    plugin: str
    message: str
    details: list = field(default_factory=list)
    files: list = field(default_factory=list)
    changed: bool = True  # did this touch the checkout at all

    def as_dict(self) -> dict:
        return {
            "ok": self.ok,
            "action": self.action,
            "plugin": self.plugin,
            "message": self.message,
            "details": self.details,
            "files": self.files,
        }


@dataclass
class Plugin:
    id: str
    path: Path
    repo: Path
    branch: str | None
    head: str
    state: str                      # stock|ffable|dirty|carrying|diverged|detached|no-upstream
    upstream_remote: str | None
    upstream_ref: str | None
    remotes: dict
    origin_url: str
    upstream_candidate: str | None
    ahead: int
    behind: int
    dirty: list
    untracked: list
    delta: list                     # commits of mine not in upstream (newest first)
    new_upstream: list              # upstream commits I do not have
    merged_upstream: bool           # my delta is already patch-equivalent upstream
    baseline: str | None
    needs_attention: bool
    rebasing: bool = False          # a stopped rebase waiting on me
    note: str = ""

    def as_dict(self) -> dict:
        d = self.__dict__.copy()
        d["path"] = str(self.path)
        d["repo"] = str(self.repo)
        return d


def discover() -> list[str]:
    root = plugins_dir()
    if not root.is_dir():
        return []
    found = []
    for entry in sorted(root.iterdir()):
        if not entry.is_dir():
            continue
        if (entry / "manifest.json").is_file() and (entry / ".git").exists():
            found.append(entry.name)
    return found


def manifest_field(plugin_id: str, key: str) -> str | None:
    try:
        data = json.loads((plugins_dir() / plugin_id / "manifest.json").read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return None
    value = data.get(key)
    return value if isinstance(value, str) else None


def remotes_of(repo: Path) -> dict:
    _, out, _ = git(repo, "remote", check=False)
    result = {}
    for name in out.splitlines():
        _, url, _ = git(repo, "remote", "get-url", name, check=False, ignore_fail=True)
        result[name] = url
    return result


def resolve_ref(repo: Path, ref: str) -> bool:
    _, out, _ = git(repo, "rev-parse", "--verify", "--quiet", ref, check=False, ignore_fail=True)
    return bool(out)


def has_remote_refs(repo: Path, remote: str) -> bool:
    _, out, _ = git(repo, "for-each-ref", "--count=1", f"refs/remotes/{remote}/",
                    check=False, ignore_fail=True)
    return bool(out)


def ensure_fetched(repo: Path, remote: str) -> None:
    """A freshly added remote has no tracking refs; fetch once so it can resolve."""
    if remote and not has_remote_refs(repo, remote):
        git(repo, "fetch", "--quiet", remote, check=False, ignore_fail=True)


def default_branch_of(repo: Path, remote: str) -> str | None:
    _, out, _ = git(
        repo, "symbolic-ref", "--quiet", "--short", f"refs/remotes/{remote}/HEAD",
        check=False, ignore_fail=True,
    )
    if out:
        return out.split("/", 1)[-1]
    for guess in ("main", "master"):
        if resolve_ref(repo, f"{remote}/{guess}"):
            return guess
    return None


def inspect_plugin(plugin_id: str, entry: dict | None = None) -> Plugin:
    path = plugins_dir() / plugin_id
    entry = entry if entry is not None else plugin_state(plugin_id)
    repo = Path(entry.get("repo") or path)
    remotes = remotes_of(repo)
    origin_url = remotes.get("origin", "")

    _, branch, _ = git(repo, "symbolic-ref", "--quiet", "--short", "HEAD",
                       check=False, ignore_fail=True)
    _, head, _ = git(repo, "rev-parse", "HEAD", check=False, ignore_fail=True)

    # Upstream: explicit config wins, then a remote actually named "upstream",
    # then origin itself (an unforked plugin updated in place).
    upstream_remote = entry.get("upstream_remote")
    upstream_ref = entry.get("upstream_ref")
    if upstream_ref and not resolve_ref(repo, upstream_ref):
        upstream_ref = None
    if not upstream_ref:
        if upstream_remote and upstream_remote in remotes:
            ensure_fetched(repo, upstream_remote)
            br = default_branch_of(repo, upstream_remote)
            upstream_ref = f"{upstream_remote}/{br}" if br else None
        elif "upstream" in remotes:
            upstream_remote = "upstream"
            ensure_fetched(repo, "upstream")
            br = default_branch_of(repo, "upstream")
            upstream_ref = f"upstream/{br}" if br else None
        else:
            upstream_remote = "origin" if "origin" in remotes else None
            if upstream_remote:
                ensure_fetched(repo, upstream_remote)
                br = branch or default_branch_of(repo, upstream_remote)
                cand = f"{upstream_remote}/{br}" if br else None
                upstream_ref = cand if resolve_ref(repo, cand) else None
                if not upstream_ref:
                    br = default_branch_of(repo, upstream_remote)
                    upstream_ref = f"{upstream_remote}/{br}" if br else None
    if upstream_ref and not upstream_remote:
        # A ref was recorded earlier without its remote; the remote is its prefix,
        # and resume needs that pair to know whether origin is a fork.
        upstream_remote = upstream_ref.rsplit("/", 1)[0]

    # A manifest homepage that is a different repo than origin is almost always
    # the real project the fork came from; surface it rather than guess a remote.
    candidate = None
    for key in ("repository", "homepage", "source"):
        value = manifest_field(plugin_id, key)
        if not value or not (value.startswith("http") or value.startswith("git@")):
            continue
        if not clone_shaped(value):
            continue
        if origin_url and repo_key(value) != repo_key(origin_url):
            candidate = value
            break

    # A different source in the manifest is evidence that origin may be a fork.
    # Require confirmation before unattended updates rather than following the fork.
    if candidate and upstream_remote == "origin" and not entry.get("upstream_ref") and not entry.get("upstream_remote"):
        upstream_ref = None
        upstream_remote = None

    ahead = behind = 0
    delta: list[str] = []
    new_upstream: list[str] = []
    merged = False
    if upstream_ref:
        _, counts, _ = git(repo, "rev-list", "--left-right", "--count",
                           f"{upstream_ref}...HEAD", check=False, ignore_fail=True)
        parts = counts.split()
        if len(parts) == 2:
            behind, ahead = int(parts[0]), int(parts[1])
        delta = log_lines(repo, f"{upstream_ref}..HEAD")
        new_upstream = log_lines(repo, f"HEAD..{upstream_ref}")
        if delta:
            _, cherry, _ = git(repo, "cherry", upstream_ref, "HEAD",
                               check=False, ignore_fail=True)
            merged = bool(cherry) and not any(
                line.startswith("+") for line in cherry.splitlines()
            )

    dirty, untracked = worktree_dirty(repo)
    rebasing = rebase_in_progress(repo)

    baseline = entry.get("baseline")
    if not baseline and upstream_ref:
        _, base, _ = git(repo, "merge-base", "HEAD", upstream_ref,
                         check=False, ignore_fail=True)
        baseline = base or None

    if rebasing:
        status = "rebasing"
        note = "a rebase is stopped on a conflict - resolve it, then continue"
    elif not upstream_ref:
        status = "no-upstream"
        note = "no upstream ref resolved; set one with: omacarry upstream <id> <url>"
    elif branch is None:
        status = "detached"
        note = "detached HEAD; check out your carry branch first"
    elif merged:
        status = "merged"
        note = "your delta is already upstream; normal updates can resume"
    elif ahead and behind:
        status = "diverged"
        note = f"{ahead} of your commits, {behind} upstream commits waiting"
    elif ahead:
        status = "carrying"
        note = f"{ahead} of your commits on top of upstream"
    elif dirty or untracked:
        # Uncommitted work outranks a plain fast-forward: a dirty checkout must
        # not be handed to Omarchy's ff-only updater, which refuses or, if the
        # touched files differ, silently carries half the change.
        status = "dirty"
        note = ("uncommitted local changes"
                + (f", {behind} upstream commits waiting" if behind else ", upstream is current")
                + " - pass --force to carry them through a stash")
    elif behind:
        status = "ffable"
        note = f"{behind} upstream commits, no delta of yours to carry"
    else:
        status = "stock"
        note = "in sync with upstream"

    if status in ("diverged", "carrying") and (dirty or untracked):
        note += f"; {len(dirty) + len(untracked)} uncommitted file(s)"

    needs = status in ("diverged", "carrying", "dirty", "detached", "no-upstream",
                       "rebasing") or bool(dirty or untracked)

    return Plugin(
        id=plugin_id, path=path, repo=repo, branch=branch or None, head=head,
        state=status, upstream_remote=upstream_remote, upstream_ref=upstream_ref,
        remotes=remotes, origin_url=origin_url, upstream_candidate=candidate,
        ahead=ahead, behind=behind, dirty=dirty, untracked=untracked,
        delta=delta, new_upstream=new_upstream, merged_upstream=merged,
        baseline=baseline, needs_attention=needs, rebasing=rebasing, note=note,
    )


def log_lines(repo: Path, rev_range: str) -> list[str]:
    _, out, _ = git(repo, "log", "--format=%h %s", rev_range, check=False, ignore_fail=True)
    return out.splitlines()


def worktree_dirty(repo: Path) -> tuple[list, list]:
    _, out, _ = git(repo, "status", "--porcelain", "-uall", check=False, ignore_fail=True)
    dirty, untracked = [], []
    for line in out.splitlines():
        code, name = line[:2], line[3:]
        if "?" in code:
            untracked.append(name)
        elif code.strip():
            dirty.append(name)
    return dirty, untracked


def scan(all_plugins: bool = True) -> list[Plugin]:
    entries = load_state()["plugins"]
    out = []
    for pid in discover():
        if not all_plugins and pid not in entries:
            continue
        out.append(inspect_plugin(pid, entries.get(pid)))
    return out


# ---------------------------------------------------------------- actions


def set_upstream(plugin_id: str, url: str) -> Result:
    entry = plugin_state(plugin_id)
    repo = Path(entry.get("repo") or plugins_dir() / plugin_id)
    if not (repo / ".git").exists():
        return Result(False, "upstream", plugin_id, f"{repo} is not a git checkout")
    remotes = remotes_of(repo)
    existing = remotes.get("upstream")
    if existing and repo_key(existing) != repo_key(url):
        return Result(
            False, "upstream", plugin_id,
            f"remote 'upstream' already points at {existing}; remove it first",
        )
    target = "upstream"
    if not existing:
        if remotes.get("origin") and repo_key(remotes["origin"]) == repo_key(url):
            target = "origin"  # this checkout's origin already IS the project
        else:
            git(repo, "remote", "add", "upstream", url)
    entry["upstream_remote"] = target
    entry["repo"] = str(repo)
    save_plugin_state(plugin_id, entry)
    _, _, err = git(repo, "fetch", "--quiet", target, check=False, ignore_fail=True)
    br = default_branch_of(repo, target)
    if br:
        entry["upstream_ref"] = f"{target}/{br}"
        save_plugin_state(plugin_id, entry)
    return Result(
        True, "upstream", plugin_id,
        f"upstream = {url}" + (f" ({target}/{br})" if br else ""),
        details=[err] if err else [],
    )


def fetch(plugin_id: str) -> Result:
    entry = plugin_state(plugin_id)
    pl = inspect_plugin(plugin_id, entry)
    repo = pl.repo
    done = []
    for name in sorted(pl.remotes):
        code, _, err = git(repo, "fetch", "--quiet", name, check=False, ignore_fail=True)
        done.append(f"{name}: {'ok' if code == 0 else 'failed' + (' - ' + err if err else '')}")
    fresh = inspect_plugin(plugin_id, entry)
    return Result(
        all("ok" in line for line in done), "fetch", plugin_id,
        f"{fresh.behind} upstream commit(s) new, delta {len(fresh.delta)}",
        details=done,
    )


def predict_conflicts(pl: Plugin, onto: str) -> tuple[bool, list, list]:
    """Three-way merge prediction without touching the worktree.

    Returns (clean, conflicted_paths, notes). ``--name-only`` output is
    "<tree oid>\\n\\n<conflicted names>\\n\\n<informational messages>", so the
    names are only the block between the first two blank lines.
    """
    base = pl.baseline or pl.head
    code, out, err = git(
        pl.repo, "merge-tree", "--write-tree", "--merge-base", base,
        "--name-only", onto, pl.branch or "HEAD", check=False, ignore_fail=True,
    )
    if code == 0:
        return True, [], []
    if "fatal" in err or "usage" in (err or "").lower():
        return True, [], [f"prediction unavailable: {(err or out).splitlines()[0]}"]
    lines = out.splitlines()
    names: list[str] = []
    rest = lines[1:]
    while rest and rest[0].strip():
        names.append(rest.pop(0))
    notes = [line for line in rest if line.strip()]
    return False, names, notes


def merge_is_clean(repo: Path, head: str, target: str) -> tuple[bool, list, list]:
    """Would ``git merge target`` on ``head`` conflict? merge-tree answers that
    without committing, so it needs no author identity and touches nothing."""
    code, out, err = git(repo, "merge-tree", "--write-tree", "--name-only",
                         head, target, check=False, ignore_fail=True)
    if code == 0:
        return True, [], []
    if code > 1 or not out.strip():
        first = (err or out).splitlines()[0] if (err or out) else "git merge-tree failed"
        return False, [], [f"could not rehearse the merge: {first}"]
    lines = out.splitlines()
    names: list[str] = []
    rest = lines[1:]
    while rest and rest[0].strip():
        names.append(rest.pop(0))
    notes = [line for line in rest if line.strip()]
    return False, names, notes


def delta_has_merges(repo: Path, base: str, head: str) -> bool:
    """Does the delta itself contain merge commits - the "merge upstream into my
    branch to stay current" workflow? ``rebase --onto`` flattens those and
    resolves them a second time, which is not what the branch says."""
    _, out, _ = git(repo, "rev-list", "--min-parents=2", "--max-count=1",
                    f"{base}..{head}", check=False, ignore_fail=True)
    return bool(out.strip())


def replay_strategy(repo: Path, base: str, head: str) -> str:
    """How to move this delta onto new upstream: replay it commit by commit, or
    merge upstream into the branch because the branch already works that way."""
    return "merge" if delta_has_merges(repo, base, head) else "rebase"


def _git_reason(err: str) -> str:
    """The line in git's stderr that says why it stopped - not its progress chatter."""
    lines = [line.strip() for line in (err or "").splitlines() if line.strip()]
    for line in lines:
        if line.startswith(("error:", "fatal:")):
            return line
    for line in reversed(lines):
        if not re.match(r"^(Rebasing|Applying|Auto-merging|warning:)", line):
            return line
    return lines[-1] if lines else "the rehearsal failed"


def rehearsal(pl: Plugin, target: str, base: str, strategy: str) -> tuple[bool, list, list]:
    """Answer "will this replay cleanly" without touching the real checkout.

    A merge is rehearsed with ``merge-tree``, which is exactly what ``git merge``
    would produce and commits nothing. A linear replay is rehearsed by actually
    doing it in a throwaway worktree: a three-way merge of the whole delta is only
    an approximation there, because a replay conflicts commit by commit.
    """
    if strategy == "merge":
        clean, conflicted, notes = merge_is_clean(pl.repo, pl.head, target)
        if not clean and not conflicted:
            return False, [], notes  # the rehearsal itself failed: say so
        return clean, conflicted, notes

    tmp = Path(tempfile.mkdtemp(prefix="omacarry-rehearse-"))
    tmp.rmdir()  # git worktree add insists the path is new
    code, _, err = git(pl.repo, "worktree", "add", "--detach", "--quiet", str(tmp), pl.head,
                       check=False, ignore_fail=True)
    if code != 0:
        return predict_conflicts(pl, target)  # disposable checkout impossible
    notes: list[str] = []
    try:
        # A rehearsal commits only inside a throwaway tree that is deleted below, so it
        # must not depend on the user having a git identity: invent one for the
        # committer. Authorship of the replayed commits is git's business, not ours.
        code, _, err2 = git(tmp, "-c", "user.name=omacarry",
                            "-c", "user.email=omacarry@omacarry.invalid",
                            "rebase", "--onto", target, base, "HEAD",
                            check=False, ignore_fail=True)
        conflicted = _conflicted_files(tmp) if code != 0 else []
        if code != 0:
            git(tmp, "rebase", "--abort", check=False, ignore_fail=True)
    finally:
        git(pl.repo, "worktree", "remove", "--force", str(tmp), check=False, ignore_fail=True)
        git(pl.repo, "worktree", "prune", check=False, ignore_fail=True)
    if code != 0 and not conflicted:
        return False, [], [f"could not rehearse the replay: {_git_reason(err2 or err)}"]
    if code != 0:
        notes = ["rehearsed as rebase; conflicts came from the replay itself"]
    return code == 0, conflicted, notes


def rebaseline(
    plugin_id: str,
    onto: str | None = None,
    dry_run: bool = False,
    keep_conflict: bool = False,
    push: bool = False,
    force: bool = False,
) -> Result:
    """Replay the local delta onto the newest upstream commit, in place."""
    entry = plugin_state(plugin_id)
    pl = inspect_plugin(plugin_id, entry)
    repo = pl.repo
    details: list[str] = []

    if pl.state in ("detached", "no-upstream"):
        return Result(False, "rebaseline", plugin_id, pl.note)
    if rebase_in_progress(repo):
        return Result(False, "rebaseline", plugin_id,
                      "a rebase is already stopped here: continue, skip or abort it first")

    target = onto or pl.upstream_ref
    if not target or not resolve_ref(repo, target):
        return Result(False, "rebaseline", plugin_id,
                      f"upstream ref {target or '<unset>'} is not fetched yet")

    for name in sorted(pl.remotes):
        git(repo, "fetch", "--quiet", name, check=False, ignore_fail=True)
    pl = inspect_plugin(plugin_id, entry)
    target = onto or pl.upstream_ref

    _, target_sha, _ = git(repo, "rev-parse", target)
    if pl.baseline and pl.baseline == target_sha and pl.behind == 0:
        return Result(True, "rebaseline", plugin_id,
                      "already on the newest upstream: nothing to carry",
                      details=[f"{target} is still {target_sha[:9]}"], changed=False)

    if (pl.dirty or pl.untracked) and not force:
        return Result(
            False, "rebaseline", plugin_id,
            "worktree is not clean: " + ", ".join((pl.dirty + pl.untracked)[:8]),
            files=pl.dirty + pl.untracked,
            details=["commit or stash them, or turn carry_dirty on for this plugin"],
            changed=False,
        )

    base = pl.baseline or pl.head
    strategy = replay_strategy(repo, base, pl.branch or "HEAD")
    clean, conflicts, notes = rehearsal(pl, target, base, strategy)
    if pl.delta:
        would = (f"would merge {target} into {pl.branch}" if strategy == "merge"
                 else f"would replay {len(pl.delta)} commit(s) onto {target}")
    else:
        would = f"would fast-forward {pl.behind} upstream commit(s)"
    if dry_run:
        return Result(
            clean, "rebaseline", plugin_id,
            would + (" - rehearsed clean" if clean else
                     (f" - {len(conflicts)} file(s) conflict" if conflicts
                      else " - " + (notes[0] if notes else "could not be rehearsed"))),
            details=[f"onto: {target}", f"base: {base}", f"strategy: {strategy}"] + notes,
            files=conflicts, changed=False,
        )
    if not clean and not keep_conflict:
        if conflicts:
            headline = f"{len(conflicts)} file(s) would conflict; nothing changed"
            hint = "resolve in the GUI, or rerun with --keep-conflict to resolve in git"
        else:
            # Not a clash with upstream: git itself refused the rehearsal. Say which.
            headline = (notes[0] if notes else "the rehearsal failed") + "; nothing changed"
            hint = "this is a git setup or plumbing failure, not upstream touching your lines"
        return Result(False, "rebaseline", plugin_id, headline, files=conflicts,
                      changed=False, details=[hint] + notes)

    branch = pl.branch
    stamp = time.strftime("%Y%m%d-%H%M%S")
    safety = f"refs/omacarry/pre-{stamp}"
    git(repo, "update-ref", safety, pl.head)
    details.append(f"safety ref {safety} -> {pl.head[:9]}")

    stash_ref = None
    if pl.dirty or pl.untracked:
        code, out, err = git(repo, "stash", "push", "-u", "-m", f"omacarry {plugin_id} {stamp}",
                             check=False, ignore_fail=True)
        if code != 0:
            git(repo, "update-ref", "-d", safety)
            return Result(False, "rebaseline", plugin_id, f"stash failed: {err or out}")
        stash_ref = "stash@{0}"
        details.append("uncommitted work stashed")

    def restore_stash(res: Result) -> Result:
        if not stash_ref:
            return res
        code, out, err = git(repo, "stash", "pop", check=False, ignore_fail=True)
        if code == 0:
            res.details.append("uncommitted work restored")
            return res
        # The pop conflicted: upstream touched the same lines. Git leaves the
        # worktree half-applied with the stash still stored, which is ambiguous,
        # so go back to the clean rebased HEAD and say where the work is.
        conflicted = _conflicted_files(repo)
        git(repo, "reset", "--hard", "HEAD", check=False, ignore_fail=True)
        res.ok = False
        res.message += (" | your uncommitted edits touch lines upstream also changed; "
                        "they are kept in the stash and the checkout is back on the clean "
                        "rebased HEAD")
        res.files = conflicted or [f for line in (err or out).splitlines()
                                   for f in [line.split(":")[-1].strip()] if f]
        res.details.append(f"apply them by hand: git -C {repo} stash pop")
        return res

    if pl.delta:
        if strategy == "merge":
            # The branch already carries upstream inside it (merge commits in the
            # delta), so keep doing that: flattening those merges would re-resolve
            # conflicts the branch has already resolved.
            code, out, err = git(repo, "merge", "--no-edit", "-m",
                                 f"omacarry: merge {target} into {branch}",
                                 target, check=False, ignore_fail=True)
            if code != 0:
                conflicted = _conflicted_files(repo)
                git(repo, "merge", "--abort", check=False, ignore_fail=True)
                why = (f"merge of {target} conflicted on {len(conflicted)} file(s)" if conflicted
                       else f"merge of {target} could not run: {_git_reason(err)}")
                res = Result(False, "rebaseline", plugin_id,
                             f"{why}; "
                             f"rolled back to {pl.head[:9]}",
                             files=conflicted,
                             details=details + [err.strip() or "the merge failed"] + (
                                 [] if conflicted else
                                 ["git refused the merge; your branch is untouched"] + [
                                     "this delta contains merge commits: resolve it as a merge in "
                                     f"git (git -C {repo} merge {target})"]))
                return restore_stash(res)
            details.append(f"merged {target} into {branch} (my delta contains merge commits)")
        else:
            code, out, err = git(repo, "rebase", "--onto", target, base, branch,
                                 check=False, ignore_fail=True)
            if code != 0:
                conflicted = _conflicted_files(repo)
                if keep_conflict:
                    return Result(False, "rebaseline", plugin_id,
                                  f"rebase stopped: {len(conflicted)} conflicted file(s), "
                                  f"resolve then continue",
                                  files=conflicted,
                                  details=details + [err] if err else details + [f"onto {target}"])
                git(repo, "rebase", "--abort", check=False, ignore_fail=True)
                why = (f"rebase conflicted on {len(conflicted)} file(s)" if conflicted
                       else f"rebase could not run: {_git_reason(err)}")
                extra = ([] if conflicted
                         else ["git refused the replay; your branch is untouched"])
                res = Result(False, "rebaseline", plugin_id,
                             f"{why}; rolled back to {pl.head[:9]}",
                             files=conflicted, details=details + extra)
                return restore_stash(res)
            details.append(f"replayed {len(pl.delta)} commit(s) onto {target}")
    elif pl.behind:
        code, _, err = git(repo, "merge", "--ff-only", target, check=False, ignore_fail=True)
        if code != 0:
            git(repo, "update-ref", "-d", safety)
            res = Result(False, "rebaseline", plugin_id, f"fast-forward failed: {err}")
            return restore_stash(res)
        details.append(f"fast-forwarded {pl.behind} upstream commit(s)")
    else:
        details.append("no delta, nothing to move")

    _, new_head, _ = git(repo, "rev-parse", "HEAD")
    _, base_sha, _ = git(repo, "rev-parse", target)
    entry["repo"] = str(repo)
    entry["carry_branch"] = branch
    entry["upstream_remote"] = pl.upstream_remote
    entry["upstream_ref"] = pl.upstream_ref
    entry["baseline"] = base_sha
    entry["installed"] = entry.get("installed")
    entry["last_rebaseline"] = stamp
    save_plugin_state(plugin_id, entry)

    res = Result(True, "rebaseline", plugin_id,
                 f"HEAD now {new_head[:9]} on {branch}, carrying "
                 f"{len(log_lines(repo, f'{target}..HEAD'))} commit(s)")
    res.details = details
    res = restore_stash(res)

    if push:
        res = _push_carry(plugin_id, res)
    return res


def _conflicted_files(repo: Path) -> list:
    _, out, _ = git(repo, "diff", "--name-only", "--diff-filter=U", check=False, ignore_fail=True)
    return out.splitlines()


# ---------------------------------------------------------------- conflicts


def rebase_in_progress(repo: Path) -> bool:
    gitdir = repo / ".git"
    return (gitdir / "rebase-merge").is_dir() or (gitdir / "rebase-apply").is_dir()


def conflict_files(plugin_id: str) -> list:
    entry = plugin_state(plugin_id)
    return _conflicted_files(inspect_plugin(plugin_id, entry).repo)


def staged_files(plugin_id: str) -> list:
    """Files staged during a stopped rebase: resolved, but not yet continued."""
    entry = plugin_state(plugin_id)
    repo = inspect_plugin(plugin_id, entry).repo
    _, out, _ = git(repo, "diff", "--cached", "--name-only", check=False, ignore_fail=True)
    return out.splitlines()


def conflict_text(plugin_id: str, rel_path: str) -> str:
    entry = plugin_state(plugin_id)
    repo = inspect_plugin(plugin_id, entry).repo
    try:
        return (repo / rel_path).read_text()
    except (FileNotFoundError, IsADirectoryError, UnicodeDecodeError) as exc:
        return f"<cannot read {rel_path}: {exc}>"


def save_resolution(plugin_id: str, rel_path: str, text: str) -> Result:
    entry = plugin_state(plugin_id)
    repo = inspect_plugin(plugin_id, entry).repo
    target = repo / rel_path
    if not str(target.resolve()).startswith(str(repo.resolve())):
        return Result(False, "resolve", plugin_id, f"refusing to write outside the repo: {rel_path}")
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text)
    left = text.count("<<<<<<<") + text.count(">>>>>>>")
    code, _, err = git(repo, "add", "--", rel_path, check=False, ignore_fail=True)
    if code != 0:
        return Result(False, "resolve", plugin_id, f"git add failed: {err}")
    return Result(left == 0, "resolve", plugin_id,
                  f"{rel_path} staged" + ("" if left == 0 else f" - {left} marker(s) still in it"),
                  details=[] if left == 0 else ["remove the <<<<<<< ======= >>>>>>> markers first"])


def rebase_continue(plugin_id: str) -> Result:
    entry = plugin_state(plugin_id)
    pl = inspect_plugin(plugin_id, entry)
    repo = pl.repo
    if not rebase_in_progress(repo):
        return Result(False, "continue", plugin_id, "no rebase in progress")
    remaining = _conflicted_files(repo)
    if remaining:
        return Result(False, "continue", plugin_id,
                      f"{len(remaining)} file(s) still conflicted", files=remaining)
    code, out, err = git(repo, "rebase", "--continue", check=False, ignore_fail=True,
                         env={"GIT_EDITOR": "true"})
    if code != 0:
        return Result(False, "continue", plugin_id, f"rebase --continue failed: {err or out}",
                      files=_conflicted_files(repo))
    if rebase_in_progress(repo):
        return Result(False, "continue", plugin_id,
                      "rebase stopped again on the next commit",
                      files=_conflicted_files(repo))
    _, base_sha, _ = git(repo, "rev-parse", pl.upstream_ref or "HEAD", check=False, ignore_fail=True)
    entry["baseline"] = base_sha or entry.get("baseline")
    entry["carry_branch"] = entry.get("carry_branch") or pl.branch
    entry["last_rebaseline"] = time.strftime("%Y%m%d-%H%M%S")
    save_plugin_state(plugin_id, entry)
    _, new_head, _ = git(repo, "rev-parse", "HEAD")
    carried = log_lines(repo, f"{pl.upstream_ref or 'upstream'}..HEAD")
    return Result(True, "continue", plugin_id,
                  f"rebase finished: HEAD {new_head[:9]}, carrying {len(carried)} commit(s)",
                  details=[f"next: install {plugin_id}"])


def rebase_skip(plugin_id: str) -> Result:
    """Drop the commit that conflicted (its change is already upstream)."""
    entry = plugin_state(plugin_id)
    pl = inspect_plugin(plugin_id, entry)
    if not rebase_in_progress(pl.repo):
        return Result(False, "skip", plugin_id, "no rebase in progress")
    code, _, err = git(pl.repo, "rebase", "--skip", check=False, ignore_fail=True,
                       env={"GIT_EDITOR": "true"})
    if code != 0:
        return Result(False, "skip", plugin_id, f"rebase --skip failed: {err}",
                      files=_conflicted_files(pl.repo))
    if rebase_in_progress(pl.repo):
        return Result(False, "skip", plugin_id, "rebase stopped on the next commit",
                      files=_conflicted_files(pl.repo))
    return rebase_continue_note(plugin_id, "skipped the conflicting commit")


def rebase_continue_note(plugin_id: str, lead: str) -> Result:
    entry = plugin_state(plugin_id)
    pl = inspect_plugin(plugin_id, entry)
    _, base_sha, _ = git(pl.repo, "rev-parse", pl.upstream_ref or "HEAD",
                         check=False, ignore_fail=True)
    entry["baseline"] = base_sha or entry.get("baseline")
    entry["last_rebaseline"] = time.strftime("%Y%m%d-%H%M%S")
    save_plugin_state(plugin_id, entry)
    carried = log_lines(pl.repo, f"{pl.upstream_ref or 'upstream'}..HEAD")
    return Result(True, "rebaseline", plugin_id,
                  f"{lead}: carrying {len(carried)} commit(s) on {pl.branch}")


def rebase_abort(plugin_id: str) -> Result:
    entry = plugin_state(plugin_id)
    pl = inspect_plugin(plugin_id, entry)
    if not rebase_in_progress(pl.repo):
        return Result(False, "abort", plugin_id, "no rebase in progress")
    code, _, err = git(pl.repo, "rebase", "--abort", check=False, ignore_fail=True)
    if code != 0:
        return Result(False, "abort", plugin_id, f"rebase --abort failed: {err}")
    _, head, _ = git(pl.repo, "rev-parse", "HEAD")
    return Result(True, "abort", plugin_id, f"back to {head[:9]}, nothing carried forward")


def push(plugin_id: str) -> Result:
    """Update the fork's carry branch so an open PR stays current."""
    res = Result(True, "push", plugin_id, "carry branch pushed")
    return _push_carry(plugin_id, res)


def _push_carry(plugin_id: str, res: Result) -> Result:
    entry = plugin_state(plugin_id)
    pl = inspect_plugin(plugin_id, entry)
    if not pl.branch:
        res.ok = False
        res.message += " | push skipped: detached HEAD"
        return res
    # My fork is the remote called 'fork' when there is one. A checkout whose
    # origin IS the project has nowhere to push until a fork remote is added.
    target = "fork" if "fork" in pl.remotes else "origin"
    if target == "origin" and pl.upstream_remote == "origin":
        res.details.append("push skipped: origin is upstream itself; add your fork with "
                           f"git -C {pl.repo} remote add fork <url>")
        return res
    # The published branch keeps the PR's name even when the local one differs,
    # so push local:published rather than assuming they match.
    published = policy(plugin_id).get("branch") or pl.branch
    code, out, err = git(pl.repo, "push", "--force-with-lease", target,
                         f"{pl.branch}:{published}", check=False, ignore_fail=True)
    if code != 0:
        res.ok = False
        res.message += f" | push to {target} failed: {err or out}"
    else:
        res.message = f"pushed {pl.branch} -> {target}/{published}"
    return res


def deploy(plugin_id: str, rescan: bool = True) -> Result:
    """Make the shell's plugin checkout match the rebased carry branch."""
    entry = plugin_state(plugin_id)
    pl = inspect_plugin(plugin_id, entry)
    details: list[str] = []

    if pl.rebasing:
        return Result(False, "install", plugin_id,
                      "a rebase is still stopped here: resolve it before installing")
    if pl.repo != pl.path:
        prev = pl.head
        branch = entry.get("carry_branch") or pl.branch
        if not branch:
            return Result(False, "install", plugin_id, "carry branch unknown; run rebaseline first")
        code, _, err = git(pl.path, "fetch", "--quiet", str(pl.repo), branch,
                           check=False, ignore_fail=True)
        if code != 0:
            return Result(False, "install", plugin_id, f"fetch from {pl.repo} failed: {err}")
        code, _, err = git(pl.path, "reset", "--hard", "FETCH_HEAD",
                           check=False, ignore_fail=True)
        if code != 0:
            return Result(False, "install", plugin_id, f"reset failed: {err}")
        details.append(f"checkout moved {prev[:9]} -> FETCH_HEAD ({branch} of {pl.repo})")

    if not plugin_valid(pl.path):
        return Result(False, "install", plugin_id,
                      "plugin failed omarchy plugin validate; not rescanning",
                      details=details + [validate_error(pl.path)])

    details.append("omarchy plugin validate: ok")
    if rescan:
        code, out, err = run_shell("shell", "rescanPlugins")
        details.append("shell rescanPlugins: " + ("ok" if code == 0 else f"failed - {err or out}"))

    entry["installed"] = pl.head
    save_plugin_state(plugin_id, entry)
    return Result(True, "install", plugin_id,
                  f"installed {pl.id} at {pl.head[:9]}", details=details)


def resume(plugin_id: str) -> Result:
    """Hand the plugin back to plain omarchy plugin update.

    Omarchy's updater fast-forwards ``origin``, so handing back means: ``origin``
    must be the real project (the fork moves aside under the name ``fork``) and
    the checkout must sit on the upstream branch with no delta of its own.
    """
    entry = plugin_state(plugin_id)
    pl = inspect_plugin(plugin_id, entry)
    if not pl.merged_upstream:
        missing = [line for line in git(pl.repo, "cherry", pl.upstream_ref or "upstream", "HEAD",
                                        check=False, ignore_fail=True)[1].splitlines()
                   if line.startswith("+")]
        return Result(
            False, "resume", plugin_id,
            f"{len(missing)} of your commits are not upstream yet",
            files=missing,
            details=["open a PR for the delta, or keep carrying it"],
        )

    repo = pl.repo
    upstream_url = pl.remotes.get(pl.upstream_remote or "", "")
    stamp = time.strftime("%Y%m%d-%H%M%S")
    git(repo, "update-ref", f"refs/omacarry/pre-resume-{stamp}", pl.head)

    target_branch = default_branch_of(repo, pl.upstream_remote or "origin") or pl.branch or "main"
    code, _, err = git(repo, "checkout", "-B", target_branch, pl.upstream_ref,
                       check=False, ignore_fail=True)
    if code != 0:
        return Result(False, "resume", plugin_id,
                      f"checkout of {target_branch} at {pl.upstream_ref} failed: {err}")

    moved = []
    if pl.upstream_remote and pl.upstream_remote != "origin":
        if "origin" in pl.remotes:
            name = "fork" if "fork" not in pl.remotes else "origin-old"
            code, _, err = git(repo, "remote", "rename", "origin", name,
                               check=False, ignore_fail=True)
            if code != 0:
                return Result(False, "resume", plugin_id,
                              f"could not move the fork remote aside: {err}",
                              details=[f"HEAD is already on {target_branch} at {pl.upstream_ref}"])
            moved.append(f"fork -> remote '{name}' ({pl.origin_url})")
        code, _, err = git(repo, "remote", "rename", pl.upstream_remote, "origin",
                           check=False, ignore_fail=True)
        if code != 0:
            return Result(False, "resume", plugin_id,
                          f"could not promote {pl.upstream_remote} to origin: {err}",
                          details=moved)
        moved.append(f"'{pl.upstream_remote}' -> origin ({upstream_url})")

    carry = entry.get("carry_branch") or pl.branch
    if carry and carry != target_branch:
        git(repo, "branch", "-D", carry, check=False, ignore_fail=True)
        moved.append(f"carry branch '{carry}' deleted")
    drop_plugin_state(plugin_id)
    return Result(True, "resume", plugin_id,
                  f"delta is upstream: {pl.id} now sits on origin/{target_branch} at "
                  f"{pl.upstream_ref}, future updates use omarchy plugin update",
                  details=moved + [f"safety ref refs/omacarry/pre-resume-{stamp}"])


def plugin_valid(path: Path) -> bool:
    proc = subprocess.run(("omarchy", "plugin", "validate", str(path)),
                          capture_output=True, text=True)
    return proc.returncode == 0


def validate_error(path: Path) -> str:
    proc = subprocess.run(("omarchy", "plugin", "validate", str(path)),
                          capture_output=True, text=True)
    return (proc.stderr or proc.stdout).strip()


def run_shell(*args):
    """Talk to the running omarchy-shell; OMARCHY_SHELL_BIN overrides it."""
    binary = os.environ.get("OMARCHY_SHELL_BIN") or "omarchy-shell"
    proc = subprocess.run((binary, *args), capture_output=True, text=True)
    return proc.returncode, proc.stdout.strip(), proc.stderr.strip()


# ---------------------------------------------------------------- auto pass


HOOK_TEMPLATE = """#!/usr/bin/env bash
# omacarry: carry local plugin changes across the Omarchy update that just ran.
#
# Non-interactive and non-fatal: plugins whose replay is predicted clean get
# rebaselined and installed here; anything that conflicts (or carries
# uncommitted work) is left untouched and a notification opens the window.
#
# Installed by: omacarry hook-install  (source: {source})
set -u

OMACARRY={launcher}
if [[ ! -x $OMACARRY ]]; then
  printf '%s\\n' "omacarry: launcher missing at $OMACARRY; Omarchy update can continue" >&2
  exit 0
fi

"$OMACARRY" auto --notify --log
exit 0
"""


def install_hook(hook_file: str | None = None) -> Result:
    """Register the post-update hook so Omarchy updates run the auto pass."""
    launcher = Path(__file__).resolve().parent.parent / "bin" / "omacarry"
    if hook_file:
        source = Path(hook_file)
        if not source.is_file():
            return Result(False, "hook", "-", f"hook file not found: {source}")
    else:
        source = launcher
        if not launcher.is_file():
            return Result(False, "hook", "-", f"launcher not found: {launcher}")
        generated = config_dir() / "post-update-omacarry.hook"
        generated.parent.mkdir(parents=True, exist_ok=True)
        generated.write_text(HOOK_TEMPLATE.format(launcher=str(launcher), source=str(source)))
        generated.chmod(0o755)
        source = generated
    proc = subprocess.run(("omarchy", "hook", "install", "post-update", str(source)),
                          capture_output=True, text=True)
    out = (proc.stdout + proc.stderr).strip()
    return Result(proc.returncode == 0, "hook", "-", out or "installed",
                  details=[f"source: {source}", f"launcher: {launcher}"])


def normal_update(plugin_id: str) -> Result:
    """Plain upstream update, for plugins with no delta of their own."""
    proc = subprocess.run(("omarchy", "plugin", "update", plugin_id, "--yes"),
                          capture_output=True, text=True)
    out = (proc.stdout + proc.stderr).strip()
    return Result(proc.returncode == 0, "update", plugin_id,
                  out.splitlines()[-1] if out else "no change",
                  details=out.splitlines()[:-1])


POLICY_KEYS = ("auto", "carry_dirty", "push", "install")
POLICY_DEFAULTS = {"auto": True, "carry_dirty": False, "push": False, "install": True}


def policy(plugin_id: str) -> dict:
    """What the unattended pass may do to this plugin, default per POLICY_DEFAULTS."""
    entry = plugin_state(plugin_id)
    merged = dict(POLICY_DEFAULTS)
    for key in POLICY_KEYS:
        if key in entry:
            merged[key] = bool(entry[key])
    if entry.get("branch"):
        merged["branch"] = entry["branch"]
    return merged


def set_policy(plugin_id: str, **changes) -> Result:
    entry = plugin_state(plugin_id)
    applied = []
    for key, value in changes.items():
        if key not in POLICY_KEYS and key != "branch":
            return Result(False, "policy", plugin_id, f"unknown policy key: {key}")
        if key in POLICY_KEYS:
            entry[key] = bool(value)
            applied.append(f"{key}={'on' if value else 'off'}")
        else:
            if value and not valid_branch_name(str(value)):
                return Result(False, "policy", plugin_id, f"invalid branch name: {value}")
            entry["branch"] = value or None
            applied.append(f"branch={value or '(current branch)'}")
    save_plugin_state(plugin_id, entry)
    return Result(True, "policy", plugin_id, "; ".join(applied) or "no change")


def valid_branch_name(name: str) -> bool:
    code, _, _ = git(".", "check-ref-format", "--branch", name, check=False, ignore_fail=True)
    return code == 0


def carry(plugin_id: str) -> Result:
    """The one verb: bring my branch onto the newest upstream, then push and
    install it if that plugin's policy says so. The post-update pass, the window
    and the CLI all run this same thing."""
    want = policy(plugin_id)
    pl = inspect_plugin(plugin_id, plugin_state(plugin_id))
    if pl.state == "stock":
        return Result(True, "carry", plugin_id, "nothing of yours to carry", changed=False)
    if pl.state == "ffable":
        # Nothing of mine to carry: Omarchy's own updater is the right tool.
        return normal_update(plugin_id)
    res = rebaseline(plugin_id, force=want["carry_dirty"])
    if not res.ok:
        return Result(False, "carry", plugin_id, res.message,
                      files=res.files, details=res.details, changed=res.changed)
    if not res.changed:
        return Result(True, "carry", plugin_id, res.message, details=res.details, changed=False)
    steps = list(res.details)
    if want["push"]:
        pushed = push(plugin_id)
        steps += [pushed.message] + (pushed.details or [])
        if not pushed.ok:
            return Result(False, "carry", plugin_id,
                          f"updated, but the push failed: {pushed.message}",
                          details=steps)
    if want["install"]:
        dep = deploy(plugin_id)
        steps += dep.details
        if not dep.ok:
            return Result(False, "carry", plugin_id,
                          f"updated, but the install failed: {dep.message}",
                          files=dep.files, details=steps)
        return Result(True, "carry", plugin_id, f"updated and installed: {dep.message}",
                      details=steps)
    return Result(True, "carry", plugin_id,
                  f"updated, not installed (install is off): {res.message}", details=steps)


def describe_action(pl: Plugin) -> tuple:
    """The one thing this plugin needs: (verb, what-will-happen, blocker).

    The window shows a button only when there is a verb, and says plainly what it
    would do; a blocker explains why the verb is unavailable instead of leaving a
    dead button.
    """
    want = policy(pl.id)
    if pl.state == "stock":
        return None, "nothing of yours here: Omarchy's updater handles this plugin", None
    if pl.state == "no-upstream":
        return "Record upstream", "which project does this come from? until that is known " \
                                  "it cannot be updated", None
    if pl.rebasing:
        return "Resolve", "a rebase is stopped in this checkout: finish it or abort it", None
    if pl.merged_upstream:
        return "Resume normal updates", "your commits are upstream already: hand this plugin " \
                                        "back to plain omarchy plugin update", None
    if not pl.branch:
        return None, "HEAD is detached; check out a branch before it can be carried", None
    if pl.behind == 0:
        mine = (f"carrying {len(pl.delta)} commit(s) on {pl.branch}" if pl.delta
                else f"{pl.branch} sits at upstream")
        extra = f", {len(pl.dirty + pl.untracked)} file(s) uncommitted" \
            if (pl.dirty or pl.untracked) else ""
        return None, f"{mine}; upstream has not moved since{extra}", None
    strategy = replay_strategy(pl.repo, pl.baseline or pl.head, pl.head)
    what = f"{strategy} {pl.behind} upstream commit(s) into {pl.branch}"
    if want["push"] and ("fork" in pl.remotes or pl.upstream_remote != "origin"):
        what += f", push to fork/{policy(pl.id).get('branch') or pl.branch}"
    what += ", install and rescan the shell" if want["install"] else ", leave the shell alone"
    if not want["auto"]:
        return None, what + " - but this plugin is opted out of the unattended pass", None
    blocker = None
    if (pl.dirty or pl.untracked) and not want["carry_dirty"]:
        blocker = f"{len(pl.dirty + pl.untracked)} file(s) uncommitted block this: commit them, " \
                  f"or turn on Carry uncommitted work"
    return ("Update my branch" if pl.delta else "Update it"), what, blocker


def clear_repo(plugin_id: str) -> Result:
    """Stop working in a dev checkout: the plugins dir becomes the checkout again."""
    entry = plugin_state(plugin_id)
    if "repo" not in entry:
        return Result(True, "repo", plugin_id, "already works in the plugins dir")
    previous = entry.pop("repo")
    for key in ("upstream_ref", "upstream_remote", "baseline"):
        entry.pop(key, None)
    save_plugin_state(plugin_id, entry)
    return Result(True, "repo", plugin_id,
                  f"back to the plugins dir as the checkout (was {previous})",
                  details=["upstream is resolved again on the next scan"])


def set_repo(plugin_id: str, path: str) -> Result:
    """Point a plugin's work at a development checkout; the plugins dir then just
    runs whatever that branch says (rebase upstream-side, install local-side)."""
    repo = Path(path).expanduser().resolve()
    if not (repo / ".git").exists():
        return Result(False, "repo", plugin_id, f"not a git checkout: {repo}")
    pl = inspect_plugin(plugin_id)
    if repo == pl.path:
        return Result(False, "repo", plugin_id, "that is the installed checkout itself")
    entry = plugin_state(plugin_id)
    entry["repo"] = str(repo)
    for key in ("upstream_ref", "upstream_remote", "baseline"):
        entry.pop(key, None)  # remotes differ per checkout; re-resolve them
    save_plugin_state(plugin_id, entry)
    fresh = inspect_plugin(plugin_id, entry)
    return Result(True, "repo", plugin_id,
                  f"{pl.id} works in {repo} (branch {fresh.branch}, "
                  f"upstream {fresh.upstream_ref or 'unset'})",
                  details=["the plugins dir is deployed from there on install"])


def plugins_with_upstream() -> list[dict]:
    """Plugins with local work whose upstream is not settled: the one thing a
    human has to say. A fork whose origin is a copy of the project can be
    guessed at; a checkout with no candidate at all cannot be updated at all."""
    missing = []
    for pl in scan():
        if pl.upstream_ref or pl.state == "stock":
            continue
        missing.append({"id": pl.id, "origin": pl.origin_url or "(none)",
                        "candidate": pl.upstream_candidate, "note": pl.note})
    return missing


def auto_pass(notify=None) -> list[Result]:
    """The post-update behaviour, unattended.

    Each plugin's policy says what may be done without anyone there: replay the
    delta and install it (auto), carry uncommitted edits through a stash
    (carry_dirty), keep the fork's PR branch current (push), deploy into the
    plugins dir (install). Anything that would need a human - a real conflict, a
    detached HEAD, an unrecorded upstream, a stopped rebase - is reported, never
    guessed at.
    """
    results: list[Result] = []
    for pl in scan():
        want = policy(pl.id)
        # Decide against the newest upstream, not the last-fetched one: a remote
        # tracking ref left over from an earlier run can make a plugin that has
        # moved ahead look perfectly in sync, and then nothing ever updates.
        fetch(pl.id)
        pl = inspect_plugin(pl.id, plugin_state(pl.id))
        if pl.state == "stock":
            continue
        if not want["auto"]:
            results.append(Result(True, "auto", pl.id, "left alone: auto is off for this plugin"))
            continue
        if pl.state == "merged":
            # Handing a plugin back to normal updates rewrites its remotes and
            # deletes the carry branch: worth reporting, not worth doing alone.
            results.append(Result(True, "auto", pl.id,
                                  "your changes are upstream - run: omacarry resume "
                                  f"{pl.id}, then omarchy plugin update {pl.id}"))
            continue
        if pl.state in ("detached", "no-upstream", "rebasing"):
            results.append(Result(False, "auto", pl.id, pl.note))
            continue
        # carry() rehearses before touching the checkout, so a conflict here
        # costs nothing and the failure says what needs a human.
        res = carry(pl.id)
        if res.ok:
            results.append(Result(True, "auto", pl.id, res.message,
                                  details=res.details, changed=res.changed))
        else:
            results.append(Result(False, "auto", pl.id, f"{res.message} - needs a human",
                                  files=res.files, details=res.details, changed=res.changed))
    if notify:
        for res in results:
            notify(res)
    return results


def append_hook_log(results: list[Result]) -> None:
    path = log_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as handle:
        for res in results:
            handle.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {res.action} {res.plugin}: "
                         f"{'ok' if res.ok else 'attention'} - {res.message}\n")
            for line in res.details:
                handle.write(f"    {line}\n")
            for line in res.files:
                handle.write(f"    conflict: {line}\n")
