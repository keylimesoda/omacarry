"""Sync an open GitHub PR branch before updating the installed checkout."""
from __future__ import annotations

import json
import re
import subprocess
import tempfile
import time
from pathlib import Path
from urllib.parse import urlsplit
from . import core


def github_login():
    """Return the authenticated GitHub login, or None if it cannot be read."""
    try:
        proc = subprocess.run(['gh', 'api', 'user', '--jq', '.login'],
                              capture_output=True, text=True, timeout=8)
    except (OSError, subprocess.TimeoutExpired):
        return None
    login = proc.stdout.strip()
    return login if proc.returncode == 0 and re.fullmatch(r'[A-Za-z0-9-]+', login) else None


def github_repository(value):
    value = value.strip()
    if value.startswith(('https://', 'http://')):
        url = urlsplit(value)
        if url.scheme != 'https' or url.netloc.lower() != 'github.com' or url.query or url.fragment:
            return None
        value = url.path.strip('/')
    if value.endswith('.git'):
        value = value[:-4]
    return value if re.fullmatch(r'[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+', value) else None


def set_tracked_branch(plugin_id, upstream, base, head_repo, head):
    upstream, head_repo = github_repository(upstream), github_repository(head_repo)
    base, head = base.strip(), head.strip()
    if not upstream:
        return core.Result(False, 'track', plugin_id, 'Enter the upstream GitHub repository (owner/name or GitHub URL)', changed=False)
    if not head_repo:
        return core.Result(False, 'track', plugin_id, 'Enter your GitHub repository (owner/name or GitHub URL)', changed=False)
    if not core.valid_branch_name(base) or not core.valid_branch_name(head):
        return core.Result(False, 'track', plugin_id, 'Enter valid upstream and GitHub branch names', changed=False)
    if upstream.lower() == head_repo.lower() and base == head:
        return core.Result(False, 'track', plugin_id, 'Your branch must differ from the upstream branch', changed=False)
    entry = core.plugin_state(plugin_id)
    entry['tracked_branch'] = {'upstream': upstream, 'base': base,
                               'head_repo': head_repo, 'head': head}
    core.save_plugin_state(plugin_id, entry)
    return core.Result(True, 'track', plugin_id, f'Tracking {head_repo}:{head}', changed=False)


def clear_tracked_branch(plugin_id):
    entry = core.plugin_state(plugin_id)
    entry.pop('tracked_branch', None)
    core.save_plugin_state(plugin_id, entry)
    return core.Result(True, 'track', plugin_id, 'Using open PRs', changed=False)


def discover_prs(plugin_id, trace=None):
    pl = core.inspect_plugin(plugin_id)
    url = pl.remotes.get(pl.upstream_remote or "") or pl.upstream_candidate
    key = core.repo_key(url or "")
    parts = key.split('/')
    if len(parts) != 3 or parts[0] != 'github.com':
        raise ValueError('Cannot identify the GitHub upstream repository')
    repo = '/'.join(parts[1:])
    if trace is not None:
        trace.update({'repository': repo, 'author': '@me', 'state': 'open',
                      'upstream_remote': pl.upstream_remote, 'upstream_ref': pl.upstream_ref})
    proc = subprocess.run(['gh', 'pr', 'list', '--repo', repo, '--author', '@me',
                           '--state', 'open', '--limit', '100', '--json',
                           'number,url,headRefName,headRepository,headRepositoryOwner,baseRefName'],
                          capture_output=True, text=True)
    if proc.returncode:
        raise ValueError('Could not find open PRs: ' + (proc.stderr.strip() or 'GitHub request failed'))
    prs = json.loads(proc.stdout)
    if trace is not None:
        trace['returned_prs'] = len(prs)
        trace['candidates'] = [{'number': pr.get('number'), 'url': pr.get('url'),
                                'head': pr.get('headRefName'), 'base': pr.get('baseRefName')}
                               for pr in prs]
    groups = {}
    for pr in prs:
        owner = (pr.get('headRepositoryOwner') or {}).get('login')
        head_repo = (pr.get('headRepository') or {}).get('name')
        if not owner or not head_repo:
            continue
        key = (f'{owner}/{head_repo}', pr['headRefName'], pr['baseRefName'])
        groups.setdefault(key, []).append(pr)
    if trace is not None:
        trace['matching_groups'] = len(groups)
    return [{'head_repo': k[0], 'head': k[1], 'base': k[2], 'upstream': repo,
             'prs': v, 'key': '|'.join(k)} for k, v in groups.items()]


def sync(plugin_id):
    entry = core.plugin_state(plugin_id)
    entry.pop('last_pr', None)
    core.save_plugin_state(plugin_id, entry)
    trace = {}
    try:
        result = _sync(plugin_id, trace)
    except (ValueError, core.GitError, OSError) as exc:
        result = core.Result(False, 'sync', plugin_id, str(exc), changed=False)
    entry = core.plugin_state(plugin_id)
    entry['last_sync'] = {'ok': result.ok, 'message': result.message,
                          'files': result.files, 'details': result.details, 'discovery': trace, 'time': int(time.time())}
    core.save_plugin_state(plugin_id, entry)
    return result


def _sync(plugin_id, trace=None):
    pl = core.inspect_plugin(plugin_id)
    if pl.repo != pl.path:
        raise ValueError('Separate development checkout needs manual attention before sync')
    if pl.rebasing or not pl.branch:
        raise ValueError('Finish the current Git operation and check out the PR branch')
    if pl.dirty or pl.untracked:
        raise ValueError('Uncommitted local files: commit or stash them before syncing')
    configured = core.plugin_state(plugin_id).get('tracked_branch')
    if configured:
        # Explicitly selected branches do not depend on finding an open PR.
        pr = dict(configured, prs=[])
        if trace is not None:
            trace.update({'mode': 'tracked_branch', **configured})
    else:
        choices = discover_prs(plugin_id, trace=trace)
        if not choices:
            raise ValueError('No open PR found; local changes were left alone')
        chosen = core.plugin_state(plugin_id).get('pr_selection')
        matches = [p for p in choices if p['key'] == chosen] if chosen else choices
        if not matches:
            raise ValueError('The selected PR is no longer open; select an open PR before syncing')
        if len(matches) != 1:
            raise ValueError('Multiple PR branches or upstream bases found; select one before syncing')
        pr = matches[0]
    entry = core.plugin_state(plugin_id)
    entry['last_pr'] = {key: pr[key] for key in ('upstream', 'base', 'head_repo', 'head')}
    entry['last_pr']['urls'] = [item['url'] for item in pr['prs'] if item.get('url')]
    core.save_plugin_state(plugin_id, entry)
    upstream_url = f"https://github.com/{pr['upstream']}.git"
    fork_url = f"https://github.com/{pr['head_repo']}.git"
    with tempfile.TemporaryDirectory(prefix='omacarry-pr-') as tmp:
        work = Path(tmp) / 'work'
        proc = subprocess.run(['git', 'clone', '--quiet', '--no-local', str(pl.repo), str(work)],
                              capture_output=True, text=True)
        if proc.returncode:
            raise ValueError('Could not prepare sync: ' + proc.stderr.strip())
        core.git(work, 'fetch', '--quiet', upstream_url, f"+refs/heads/{pr['base']}:refs/omacarry/upstream")
        core.git(work, 'fetch', '--quiet', fork_url, f"+refs/heads/{pr['head']}:refs/omacarry/pr")
        _, old, _ = core.git(work, 'rev-parse', 'refs/omacarry/pr')
        _, upstream, _ = core.git(work, 'rev-parse', 'refs/omacarry/upstream')
        code, _, _ = core.git(work, 'merge-base', '--is-ancestor', pl.head, old, check=False)
        if code:
            raise ValueError('Local branch has commits not on the GitHub branch; push or reconcile them first')
        core.git(work, 'checkout', '--quiet', '--detach', old)
        _, base, _ = core.git(work, 'merge-base', old, upstream)
        strategy = core.replay_strategy(work, base, old)
        if strategy == 'merge':
            args = ('merge', '--no-edit', upstream)
        else:
            args = ('rebase', '--onto', upstream, base, 'HEAD')
        code, out, err = core.git(work, *args, check=False)
        if code:
            files = core._conflicted_files(work)
            msg = ('Upstream conflicts with the GitHub branch; resolve it online or with your Omarchy agent'
                   if files else 'Could not update GitHub branch: ' + core._git_reason(err or out))
            return core.Result(False, 'sync', plugin_id, msg, files=files, changed=False)
        _, new, _ = core.git(work, 'rev-parse', 'HEAD')
        if not core.plugin_valid(work):
            raise ValueError('Updated plugin failed validation: ' + core.validate_error(work))
        if new != old:
            code, out, err = core.git(work, 'push', f'--force-with-lease=refs/heads/{pr["head"]}:{old}',
                                      fork_url, f'{new}:refs/heads/{pr["head"]}', check=False)
            if code:
                raise ValueError('GitHub branch was not updated: ' + core._git_reason(err or out))
        # Recheck after network operations: never discard newly-created local work.
        dirty, untracked = core.worktree_dirty(pl.repo)
        _, current, _ = core.git(pl.repo, 'rev-parse', 'HEAD')
        _, branch, _ = core.git(pl.repo, 'symbolic-ref', '--quiet', '--short', 'HEAD', check=False)
        if dirty or untracked or current != pl.head or branch != pl.branch:
            return core.Result(False, 'sync', plugin_id, 'GitHub branch updated, but local checkout changed during sync; install skipped', changed=new != old)
        code, out, err = core.git(pl.repo, 'fetch', '--quiet', fork_url,
                                  f'refs/heads/{pr["head"]}', check=False)
        if code:
            return core.Result(False, 'sync', plugin_id,
                               'GitHub branch updated, but fetching it locally failed: ' + core._git_reason(err or out),
                               changed=new != old)
        _, fetched, _ = core.git(pl.repo, 'rev-parse', 'FETCH_HEAD')
        if fetched != new:
            return core.Result(False, 'sync', plugin_id, 'GitHub branch changed during sync; local install skipped', changed=new != old)
        # Fetch can take time too; check again immediately before reset so edits
        # made during that fetch are not discarded.
        dirty, untracked = core.worktree_dirty(pl.repo)
        _, current, _ = core.git(pl.repo, 'rev-parse', 'HEAD')
        _, branch, _ = core.git(pl.repo, 'symbolic-ref', '--quiet', '--short', 'HEAD', check=False)
        if dirty or untracked or current != pl.head or branch != pl.branch:
            return core.Result(False, 'sync', plugin_id,
                               'GitHub branch updated, but local checkout changed during sync; install skipped',
                               changed=new != old)
        try:
            core.git(pl.repo, 'update-ref', f'refs/omacarry/pre-{time.time_ns()}', current)
            core.git(pl.repo, 'reset', '--hard', new)
        except core.GitError as exc:
            return core.Result(False, 'sync', plugin_id,
                               'GitHub branch updated, but local checkout could not be updated: ' + str(exc),
                               changed=new != old)
        dep = core.deploy(plugin_id)
        if not dep.ok:
            return core.Result(False, 'sync', plugin_id, 'GitHub branch updated, but local installation failed: ' + dep.message)
        return core.Result(True, 'sync', plugin_id, f"Synced {pr['head']} and installed locally", changed=new != old or current != new)
