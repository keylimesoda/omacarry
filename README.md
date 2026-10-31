# omacarry

Carry your own changes to Omarchy plugins across upstream updates, instead of
losing them when the plugin updates or being frozen off updates entirely.

The situation it exists for: you forked an Omarchy plugin, sent a PR, and are
running your fork. Upstream keeps moving. `omarchy update` refuses to
fast-forward a plugin with local commits, so you stop getting updates; or you
merge upstream badly by hand and lose your work.

## Running the app

Requires Python 3, Git, GTK 4, libadwaita and PyGObject. Run
`bin/omacarry gui` to open the settings window. Run `bin/omacarry hook-install`
once to enable the post-update hook. The hook points to this checkout, so keep
the checkout in place or reinstall the hook if you move it.

## The model

For each plugin with local work:

- **upstream** — the real project (`omacarry upstream <id> <url>` records it once;
  a fork's `origin` may not be it). Plugins are discovered automatically; record an
  upstream only when it cannot be determined safely.
- **carry branch** — the branch holding your delta, usually the PR branch.
- **baseline** — the upstream commit your delta was built on.
- **delta** — `baseline..HEAD`: exactly the commits that are yours.

Every replay is `rebase --onto <newest upstream> <baseline> <carry branch>`: your
delta lifted onto the new upstream head. A safety ref
(`refs/omacarry/pre-<timestamp>`) is written before anything moves, so any
mistake is one `git update-ref` away.

## What happens on `omarchy update`

`omacarry hook-install` registers a post-update hook. After every update it runs
`omacarry auto --notify --log`, which for each non-stock plugin fetches, works
out the state against the *newest* upstream, and acts according to that
plugin's policy:

| state | unattended result |
|---|---|
| in sync, no delta | left to Omarchy's own updater |
| upstream ahead, no delta of mine | fast-forwarded, shell rescanned |
| my delta replays clean | rebaselined, validated, installed, shell rescanned |
| my delta conflicts | **nothing touched**; logged and notified, with guidance for resolving it externally |
| uncommitted work | skipped, unless `carry_dirty` is on (stash → replay → restore) |
| your delta already upstream | reported; `omacarry resume <id>` hands it back to normal updates |
| a rebase already stopped | left exactly as it is |

Nothing that needs judgement happens unattended. A conflict never leaves a
stopped rebase, conflict markers, or a stash in the installed plugin directory:
the auto pass either completes or restores the checkout to where it was and
says so.

## Policy (this is what the window is for)

Per plugin, stored in `~/.config/omacarry/state.json`:

| key | default | meaning |
|---|---|---|
| `auto` | on | rebaseline this plugin during the post-update pass |
| `carry_dirty` | off | carry uncommitted edits through a stash |
| `push` | off | force-with-lease the carry branch to my fork after a clean replay, keeping an open PR current |
| `install` | on | deploy the result into the plugins dir, validate, rescan the shell |
| `branch` | current | which branch carries my delta |

```
omacarry policy <id> --show
omacarry policy <id> --auto off --dirty on --push on
omacarry policy <id> --branch desktop-google-personal
```

The only thing a machine cannot decide is what your fork's upstream actually
is, and which branch carries your work. `omacarry setup` lists forks still
missing that; the window has the same field. Everything else runs by itself.

## Development checkouts

If you keep a repo under `~/Work`, don't work in the plugin directory:

```
omacarry repo omamail ~/Work/omamail
```

The rebase happens in your repo; `omacarry install` (and the update pass)
deploy that branch into `~/.config/omarchy/plugins/<id>`, run
`omarchy plugin validate`, and call the shell's `rescanPlugins`.

## CLI

```
omacarry scan [--needs]        every git-managed plugin and its divergence
omacarry status <id>           one plugin in detail
omacarry plan <id>             what a rebaseline would do, changing nothing
omacarry rebase <id>           replay the delta onto newest upstream
              --keep-conflict  stop in the worktree so you can resolve it
omacarry conflicts <id> [--show PATH]
omacarry resolve <id> <path> --file -     write a resolution, stage it
omacarry continue | skip | abort <id>
omacarry install <id>          validate + rescan the live shell
omacarry push <id>             force-with-lease the carry branch to my fork
omacarry resume <id>           delta is upstream: go back to normal updates
omacarry upstream <id> <url>   record the real upstream of a fork
omacarry repo <id> <path>      work in a dev checkout instead of the plugin dir
omacarry policy <id> [...]     what the unattended pass may do
omacarry setup                 forks that still need an upstream recorded
omacarry hook-install          register the post-update hook
omacarry auto [--notify --log] the post-update pass itself
omacarry gui                   the window
```

When a replay conflicts, the window identifies the checkout, branch, and affected
files so you can resolve it online or with your Omarchy agent.

## Verifying

GUI and update behaviour are verified inside `omabox` boxes (a throwaway Hyprland
+ Omarchy session), never on the user's desktop: a fixture upstream + fork +
installed plugin checkout, then real `omarchy hook post-update` runs against the
live box shell.
