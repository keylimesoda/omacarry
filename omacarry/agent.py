"""Prepare a focused handoff to Omarchy's configured coding agent."""
from __future__ import annotations

import json
import shlex
import subprocess
from datetime import datetime, timezone
from pathlib import Path

from . import core


def diagnostic_prompt(plugin_id: str) -> str:
    pl = core.inspect_plugin(plugin_id)
    entry = core.plugin_state(plugin_id)
    last = entry.get('last_sync') or {}
    when = last.get('time')
    try:
        timestamp = datetime.fromtimestamp(when, timezone.utc).isoformat() if when else 'unknown'
    except (ValueError, OSError, OverflowError, TypeError):
        timestamp = 'unknown'
    pr = entry.get('last_pr') or {}
    files = last.get('files') or []
    details = last.get('details') or []
    discovery = last.get('discovery')
    remotes = getattr(pl, 'remotes', {}) or {}
    remote_summary = ', '.join(f"{name}={core.repo_key(url)}" for name, url in remotes.items()) or 'none'
    code, status, _ = core.git(pl.repo, 'status', '--short', '--branch', check=False)
    current_status = status if code == 0 else 'unavailable'
    checkout = shlex.quote(str(pl.repo))
    repo = (discovery or {}).get('repository')
    command = (shlex.join(['gh', 'pr', 'list', '--repo', repo, '--author', '@me',
                          '--state', 'open', '--limit', '100', '--json',
                          'number,url,headRefName,headRepository,headRepositoryOwner,baseRefName'])
               if repo else 'Repository was not recorded; resolve it from the remote mapping first')
    # Avoid passing remote URLs, which can contain embedded credentials. The agent
    # can inspect local Git configuration if it needs more information.
    return '\n'.join([
        'Help me diagnose and resolve this omacarry plugin sync failure.',
        'The intended workflow is to update the selected GitHub PR or personal branch from upstream, '
        'then sync and install the local plugin. Preserve all local and remote work; '
        'inspect the situation before making changes.',
        f'Plugin: {plugin_id}',
        f'Installed plugin directory: {pl.path}',
        f'Working checkout: {pl.repo}',
        f'Local branch: {pl.branch or "(detached)"}',
        f'Local commit: {pl.head or "unknown"}',
        f'Upstream ref: {pl.upstream_ref or "unknown"}',
        f'Current local Git status (may differ from failure time):\n{current_status}',
        f'Current remote identities: {remote_summary}',
        f'Current resolved upstream remote: {getattr(pl, "upstream_remote", None) or "unknown"}',
        'Discovery at failure time: ' + (json.dumps(discovery, sort_keys=True) if discovery is not None else 'not recorded by that version'),
        f'Discovery query to repeat: {command}',
        'Read-only checks: gh api user --jq .login; '
        f'git -C {checkout} status --short --branch; git -C {checkout} remote -v',
        'For missing PRs, compare the authenticated GitHub account, searched repository, '
        'PR author, head repository/branch, and base repository/branch. Inspect the code '
        'and actual GitHub results; do not assume no PR exists just because the query returned none.',
        f'PR repository and branch: {pr.get("head_repo", "unknown")}:{pr.get("head", "unknown")}',
        f'PR upstream and base: {pr.get("upstream", "unknown")}:{pr.get("base", "unknown")}',
        'PR links: ' + (', '.join(pr.get('urls') or []) or 'none recorded'),
        f'Chosen PR selection: {entry.get("pr_selection") or "automatic"}',
        f'Failure time (UTC): {timestamp}',
        f'Last error: {last.get("message") or "not recorded"}',
        'Conflicting or affected files: ' + (', '.join(map(str, files)) if files else 'none recorded'),
        'Additional details: ' + ('; '.join(map(str, details)) if details else 'none recorded'),
        f'omacarry source: {Path(__file__).resolve().parent.parent}',
        f'omacarry state file: {core.state_file()}',
        f'omacarry hook log: {core.log_path()}',
        'Use the recorded state and log as context, identify the root cause, and fix the issue when safe. '
        'Report any blocked step and preserve unpublished changes and remote branches.',
    ])


def launch(plugin_id: str) -> core.Result:
    try:
        prompt = diagnostic_prompt(plugin_id)
        pl = core.inspect_plugin(plugin_id)
        proc = subprocess.run(['omarchy', 'agent', 'prompt', prompt], cwd=pl.repo,
                              capture_output=True, text=True)
    except (OSError, core.GitError) as exc:
        return core.Result(False, 'agent', plugin_id, f'Could not launch the agent: {exc}', changed=False)
    if proc.returncode:
        return core.Result(False, 'agent', plugin_id,
                           'Could not launch the agent: ' + (proc.stderr.strip() or proc.stdout.strip() or f'exit {proc.returncode}'),
                           changed=False)
    return core.Result(True, 'agent', plugin_id, 'Opened the default Omarchy agent', changed=False)
