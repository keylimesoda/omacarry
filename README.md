# Om Carry

Keep your Omarchy plugin changes current with upstream. Om Carry updates your
GitHub PR branch or a personal branch first, then installs that branch locally.

## Install

Om Carry requires Omarchy, Python 3, Git, GitHub CLI (`gh`), GTK 4, libadwaita,
and PyGObject. Sign in with `gh auth login` and make sure Git can push to your
fork. Your Git identity must be configured for merges and rebases.

```sh
git clone https://github.com/keylimesoda/omacarry.git ~/Work/omacarry
cd ~/Work/omacarry
./bin/omacarry-install
```

The installer adds **Om Carry** to the application launcher and registers an
Omarchy post-update hook. Both refer to this checkout: keep it in place, or run
the installer again after moving it. To install the launcher without the hook,
run `./bin/omacarry-install --no-hook`. You can also open the app directly with
`./bin/omacarry gui`.

## Using it

Open a plugin's row to enable or pause sync, select an open PR, or choose **Track
branch** for a personal GitHub branch without a PR. If several open PRs share
the same head repository, branch and upstream base, they are treated as one.
Different branches or bases require a selection. **Sync now** runs a check
immediately; the hook checks enabled plugins during a confirmed Omarchy update
that reaches the post-update step.

For each selected branch, Om Carry fetches the upstream and GitHub branches,
prepares the update in a temporary checkout, validates it, and pushes with an
explicit Git lease. Only after the push succeeds does it update and install the
local plugin. It stops for conflicts, unpublished local commits, uncommitted
files, ambiguous PRs, and changes to the GitHub branch during the push. A
pre-update local tip is retained under `refs/omacarry/pre-*`.

The app displays the last result. On failure, **Copy error** copies diagnostic
context, and **Ask agent** opens the configured Omarchy coding agent with that
context. If the GitHub branch updated but installation failed, retry after
resolving the reported problem. Separate development checkouts require manual
attention; Om Carry does not overwrite them.

Om Carry currently supports GitHub-hosted, Git-managed Omarchy shell plugins.
The post-update hook and `carry` command use the remote-first sync; the other
legacy Git commands remain available separately. Run `./bin/omacarry --help`
for command-line usage.

## Update or remove

Pull updates in the checkout and rerun the installer if its location changed.
To remove the launcher, delete `~/.local/share/applications/omacarry.desktop`.
To stop automatic sync, remove
`~/.config/omarchy/hooks/post-update.d/post-update-omacarry.hook`. Plugin state
and logs are stored in `~/.config/omacarry/` and are not removed automatically.

## Development

Run `python3 -m unittest discover -s tests -v`. GUI and desktop integration are
checked inside an isolated `omabox` desktop.
