- Project: https://github.com/linuxcaffe/tw-mobilize
- Issues:  https://github.com/linuxcaffe/tw-mobilize/issues

# tw-mobilize

Set up a new Taskwarrior 2.6.2 station (an Android phone in Termux, or another Linux machine) from an
existing [gittw](https://github.com/linuxcaffe/gittw) repo, with [tw-web](https://github.com/linuxcaffe/tw-web)
running on it, and check that it stays set up.

## ▶ Quick start: a new phone

**In [Termux](https://f-droid.org/packages/com.termux/) (from F-Droid), three lines:**

```bash
pkg install python git openssh curl
curl -fLO https://raw.githubusercontent.com/linuxcaffe/tw-mobilize/main/mobilize.py
python mobilize.py init --station <name> --data <your gittw repo's fetch URL>
```

**`init` installs Taskwarrior 2.6.2, clones your tasks, sets up the station and tw-web, then checks it.**
It stops once to show you an SSH key to add to GitHub/Codeberg, and waits until you have. Safe to run
again: it picks up where it left off. On a station that already has your tasks: `tw -I mobilize`.

**If `pkg` complains first** ("No mirror or mirror group is selected", or `CANNOT LINK EXECUTABLE`):
that's Termux, not mobilize. Run `termux-change-repo` (pick a mirror group near you), then
`pkg upgrade`, then the three lines again. Termux is a rolling release: a long-unused install needs
upgrading before it installs anything.

**Status: 0.2.0, testing.** `mobilize init`, `check`, `stations` and `-e` work and are tested (pytest, in
a fake home folder). `check --hooks` (the hook pre-test) since 0.2.0. Still planned: hook sets per platform. This
README also keeps what was learned setting up the first phone by hand (2026-10-21).

## TL;DR

- A station is a device with its own copy of `~/.task`, cloned from your gittw remote. Today: `yoga`
  (laptop, `desktop`) and `s5` (phone, `mobile`).
- **Sync often, see the others, resolve the rare collision.** Stations sync automatically when online;
  tw-web's Sync advisory shows incoming changes and when each station last synced; a merge driver for
  the task data resolves a collision per task, last write wins. All of that lives in gittw (planned).
- **Shared config, local differences:** `~/.taskrc` is a symlink to the tracked `~/.task/.taskrc`, which
  ends with `include ~/.task/local.rc`. `local.rc` is untracked and holds what this station is.
- **Each hook says which platforms it suits** (`# tw-platforms: desktop mobile`); a station runs the hooks
  marked for it, via 2.6.2's `hooks.location` (planned).
- `mobilize init` does the setup, `mobilize check` finds what has drifted, `mobilize -e` edits
  `mobilize.rc` (`-e local`: this station's `local.rc`).
- **Taskwarrior 3.x gets a loud warning, not a refusal:** nothing here is tested with it, but nothing
  stops you.

## The pieces on a station

| Piece | Where it comes from | Notes |
|---|---|---|
| `task` 2.6.2 | Linux: the distro. Termux: `task2_2.6.2-3_aarch64.deb` from gittw | Termux's own `taskwarrior` package is 3.x: never install it. The deb is repackaged as `task2`, conflicting with it |
| `~/.task` | `git clone` of your gittw remote | data, config, `hooks/`, `scripts/` (with exec bits), themes |
| `~/.taskrc` | symlink to `.task/.taskrc` | the shared config |
| `~/.task/local.rc` | written per station, untracked | `gittw.device`, `tw.platforms`, `hooks=off` until hooks are tested, `hooks.location` later |
| `PATH` | `~/.bashrc`: `export PATH="$HOME/.task/scripts:$PATH"` | gives `tw`, `gittw`, the fleet scripts |
| tw-web | `git clone` of tw-web, `pip install -r requirements.txt`, `python app.py` | serves `localhost:5000` only; open it in the browser and install it as an app |

## Setting up a phone by hand (what worked, 2026-10-21)

Termux from F-Droid. Each step was done once on `s5`; the problems found are in the next section.

```bash
# 1. Taskwarrior 2.6.2 (once)
pkg install python git openssh
curl -fLO https://raw.githubusercontent.com/linuxcaffe/gittw/main/task2_2.6.2-3_aarch64.deb
sha256sum task2_2.6.2-3_aarch64.deb   # bd80e37006fc59a4faaba4a74d99883bc1e4141c711c5fc4775625f667b594a8
dpkg -i task2_2.6.2-3_aarch64.deb && task --version

# 2. An SSH key, added to every host your gittw remote pushes to
ssh-keygen -t ed25519 -C "<station>"
cat ~/.ssh/id_ed25519.pub
#   GitHub, from a machine with gh:  gh ssh-key add - --title <station> <<< "<key>"
#   Codeberg: Settings → SSH / GPG Keys (web)

# 3. The data, pushing where the other stations pull from
git clone <your gittw fetch URL> ~/.task
cd ~/.task
git remote set-url --add --push origin <push URL 1>
git remote set-url --add --push origin <push URL 2>
ln -s .task/.taskrc ~/.taskrc
printf 'gittw.device=<station>\ntw.platforms=mobile\nhooks=off\n' > ~/.task/local.rc
touch config/nag-state.rc config/tod.rc    # see "Problems found"
echo 'export PATH="$HOME/.task/scripts:$PATH"' >> ~/.bashrc
task next                                  # same pending count as the other station

# 4. tw-web
mkdir -p ~/dev && git clone <tw-web URL> ~/dev/tw-web
cd ~/dev/tw-web && pip install -r requirements.txt   # pkg install clang if a build fails
python app.py                                         # then http://localhost:5000 in the browser
```

**Reaching the phone from another station** (the direct sync route with no internet, and how Claude Code ran step 4
on `s5`): `pkg install openssh`, add the other station's public key to `~/.ssh/authorized_keys`, run
`sshd` (port 8022); on the other station, a `Host s5` entry with the phone's current address and
`Port 8022`. The address changes with the network; `termux-wake-lock` keeps Termux alive.

On `s5` (2026-10-21) tw-web installed with no compiler (Python 3.13, Werkzeug 3.1.9) and served the same
104 pending tasks as `yoga`.

**Until gittw's merge driver exists, don't `git push` from a second station.** Its auto-commits stay local;
`git checkout -- pending.data completed.data` throws test writes away.

## Problems found (and what the tool must do about them)

| Found | Cause | Fixed / tool's job |
|---|---|---|
| `pkg`: "No mirror or mirror group is selected", then `curl`: `CANNOT LINK EXECUTABLE … ngtcp2…` (`s5`, 2026-10-22) | no `$PREFIX/etc/termux/chosen_mirrors` link; then packages half-upgraded (a newer libcurl than its ngtcp2), so `pkg` couldn't even test mirrors | Termux's, not ours: `termux-change-repo`, then `pkg upgrade` (fixed `s5`: 112 packages). README quick start says so; `init` repeats it when `pkg install` fails |
| `Permission denied (publickey)` on clone | the station's key was on neither host | `init` makes the key, prints it, and waits until `ssh -T` works for every push host |
| A station pushing to GitHub only would be invisible | `yoga` fetches from Codeberg and pushes to both | `init` copies the push URLs; `check` compares them with the fetch URL |
| `Could not read include file …/config/nag-state.rc` | gitignored state files (`nag-state.rc`, `tod.rc`, rewritten by hooks) are still included; **a missing include is a hard error in 2.6.2** | `init` creates every *gitignored* include target empty; a missing *tracked* one is a real error and is reported |
| Theme include failed after that | `themes/` was an orphan gitlink, so the theme files were never in the repo | fixed in `~/.task` (tracked as files). `check`: no gitlinks without `.gitmodules` |
| `tw: command not found` | `~/.task/scripts` isn't on Termux's `PATH` | `init` adds the line to `~/.bashrc` once |
| **The acid test (`s5`, 2026-10-22):** after moving `~/.task`, `~/.taskrc`, tw-web aside and removing `task2`, the three quick-start lines set the phone up again in one run: deb installed via `apt`, clone with both push URLs from `mobilize.rc`, `local.rc`, state files, `PATH`, tw-web (https, `~/.local/share/tw-web`, pip), launcher; tw-web served the same 104 pending tasks and stayed up after the shell closed | | `check`: 4 warnings, all dangling links (below) |
| Three scripts point at `yoga` paths | `make-awesome.py`, `tw-sync`, `tw-web` (scripts) and `timelog` (hooks) are symlinks to `/home/<user>/dev/…` | `check` lists dangling symlinks |
| `included.rc`/`themes.rc` had `include /home/<user>/.task/…` | installers and `theme-select.py` write absolute paths | rewritten as `~/.task/…` on `yoga`; the writers get fixed in awesome-taskwarrior. `check`: no `/home/<user>` in rc files |

| A read changed `pending.data` on `s5` | 2.6.2 expires `until:` tasks on any command (`export` too, even with `rc.gc=off`); gc and built-in recurrence also write on reads | not a tool bug: the merge driver absorbs expiry; `gc=off` in the shared `.taskrc` with gc run at sync time keeps gc out of reads (2026-10-22). `init` configures the driver in each clone |

| Fewer contexts in tw-web on `s5` (`need`, `tod`) | both are written by hooks: `context.need.read` in `need.rc` (tracked, but the hook rewrites it; the committed copy is blank) and `context.tod.read` in `tod.rc` (gitignored). With `hooks=off` nothing fills them | expected until the station runs those hooks; `check` lists contexts that are empty |
| tw-web: "Network error: Failed to fetch", no contexts | Android stopped Termux, so the server died (its log ends 13:42); the page itself came from the service worker's cache | `termux-wake-lock`, Termux set to "unrestricted" battery use, and a launcher that starts the server if it's down. tw-web could say "the tw-web server isn't running" instead |

| **Hook pre-test, 2026-10-22** (`mobilize check --hooks`, 25 hooks): `yoga` +0.33 s on a read with all hooks, `s5` **+4.3 s on a read and +33 s over five commands**: each Python hook costs ~0.05 s on the laptop, 0.4–0.75 s on the phone (Python start-up) | the phone needs a small hook set; tw-web's reads already run with hooks off, but every write fires the on-launch, on-add/modify and on-exit chains | choose the `mobile` set by these numbers |
| `on-modify_hledger-add.py` fails every `done` in the sandbox | it checks `TW_NOINTERACT` *before* whether the task is a transaction, so under tw-web (which sets it) every completion is refused with `tw_needs_pty` | a bug in tw-hledger-add-hook (since 2026-07-11): check the trigger first |
| `on-modify_tw2nb.py` fails on `s5` | calls `/usr/local/bin/nb` by absolute path | desktop-only (no nb on the phone) |
| The pre-test left `__pycache__/*.pyc` in the real `scripts/` | `scripts/` is linked into the sandbox, not copied | `PYTHONDONTWRITEBYTECODE=1` in the sandbox; `~/.task` now ignores `__pycache__/` and stopped tracking two (`yoga`, 2026-10-22) |

What carried over without trouble: exec bits (git keeps them), `#!/usr/bin/env python3` shebangs, every
hook's `~/.task` lookup (`Path.home()`, never a hard-coded home), tw-web's runtime folder (no
`XDG_RUNTIME_DIR` in Termux, so its `~/.cache/tw-web` fallback applies), and the Calendar's holidays and
moon (they arrive in the clone as `config/holidays.rc` and `sky.rc`).

## In the fleet (planned)

tw-mobilize joins the [awesome-taskwarrior](https://github.com/linuxcaffe/awesome-taskwarrior) registry
and follows its `CONVENTIONS.md` (§7–9):

- **Install:** `tw -I mobilize` (registry name `mobilize`, repo `tw-mobilize`), like `events`/`tw-events`.
  The command goes to `~/.task/scripts/`, which is inside the gittw repo, so **every clone already has
  it**: a new station needs `curl` once; after that it updates with `tw -u`. Docs:
  `~/.task/docs/mobilize_README.md`.
- **Settings:** `~/.task/config/mobilize.rc`, keys `mobilize.<setting>`, included from `included.rc`; the
  tool reads the file directly. The installer doesn't ship it (generated installers overwrite config on
  every `tw -u`): `mobilize` writes it itself, keeps what's there, and adds the include only after the
  file exists, as `events init` does.
- **Tests:** pytest in `tests/`, never touching `~/.task`; `TW_TASK_DIR` respected.
- **The dangling-include rule,** knowingly bent: the shared `.taskrc` includes the untracked `local.rc`,
  and `included.rc` includes the gitignored `nag-state.rc` and `tod.rc`. So a bare `git clone` of a gittw
  repo won't start `task` until `mobilize init` (or `touch`) creates them. `init` is the supported way
  onto a new station; `taskutil config check` reports a missing one.

## Where settings live

The bootstrap runs before anything exists, so settings come from four places:

| Where | Holds | Why there |
|---|---|---|
| **Command-line options** | `--data <fetch URL>`, `--station s5`, `--platform mobile` | needed before the clone exists |
| **`mobilize.rc`** (tracked, travels with the clone) | the recipe shared by every station: push URLs, the list of stations | written once; each new station reads it |
| **`local.rc`** (untracked) | this station: `gittw.device`, `tw.platforms`, `hooks.location`, and overrides such as `mobilize.web.dir` | the only part that differs per device |
| **Built into the tool** | the task2 deb's URL and sha256, Termux package names, tw-web's public repo, tw-web's default folder | facts about the tool, not about you; `mobilize.rc` or `local.rc` can override them |

A sketch of `mobilize.rc` (yours lives in your private gittw repo; nothing private is in the tool):

```
# mobilize.rc: how to set up a station. Shared; `mobilize init` reads it after cloning.
mobilize.data.push = git@codeberg.org:<you>/<data>.git, git@github.com:<you>/<data>.git
mobilize.web.repo  = https://codeberg.org/linuxcaffe/tw-web.git
mobilize.station.yoga.platform = desktop
mobilize.station.s5.platform   = mobile
mobilize.station.s5.ssh        = s5          # Host alias: direct sync route, live status
```

**tw-web's folder** defaults to `~/.local/share/tw-web`: on most stations it's an installed app. A station
where tw-web is a development checkout says so in its own `local.rc` (`yoga`:
`mobilize.web.dir = ~/dev/tw-web`). A shared `~/dev/…` would have been one person's habit made into
everyone's path.

## What it depends on

**Repos:**
- **Your gittw data repo:** private, over SSH. The only place the station needs a key.
- **tw-web:** public, cloned over **https**, so running it needs no key (switch the remote to SSH to push
  from that station).
- **tw-mobilize:** public, fetched once by `curl`; afterwards it comes with the clone.
- **gittw:** hosts the task2 deb. `gittw`, `tw` and the other fleet scripts are already in
  `~/.task/scripts/`, so no awesome-taskwarrior checkout is needed on a station.

**Packages:**

| Kind | Termux | Linux station |
|---|---|---|
| Bootstrap | `python git openssh` (one `pkg install` line) | `git`, `python3`: checked, not installed (that needs sudo) |
| Taskwarrior 2.6.2 | the task2 deb, sha256 checked; 3.x installed: loud warning | the distro's; 3.x: loud warning |
| tw-web's Python packages | `pip install -r requirements.txt` (no compiler needed on `s5`; `clang` only if a build fails) | in a venv: newer Debian/Ubuntu refuse a system-wide `pip install` |
| Opening URLs and files | `termux-open-url`, `termux-open` (`termux-tools`, part of Termux) | `xdg-open` |
| Apps you install | Termux (F-Droid), optionally Termux:Widget, a browser | none |
| Tools that hooks call (`nb`, `hledger`) | not installed; `check` names the hooks that need them | whatever is there |

**Open (2026-10-22):** command name `mobilize` or `tw-mobilize`; whether `mobilize.rc` keeps a list of
stations (it gives live status and the direct route their addresses, but it's one more list to keep up);
tw-web over https by default.

## Semi-automating it: `curl` → `mobilize init`

Almost all of the above can be one command. The parts that can't are the ones that need you: putting a
key on a website, installing the app in the browser.

**Bootstrap.** Two lines on a fresh Termux, download-then-run (not `curl | sh`, so you can read it first):

```bash
pkg install python git openssh
curl -fLO https://raw.githubusercontent.com/linuxcaffe/tw-mobilize/main/mobilize.py
python mobilize.py init --station s5 --platform mobile --data <your gittw fetch URL>
```

**Shape of the tool:** one Python file, standard library only (it runs before anything else is
installed), every step **idempotent**: it checks first, says what it found, and does only what's missing,
so running it again after a failure or a month later is safe. Nothing private in the tool itself: the data
remote is an argument, so the repo can be public.

| `init` step | Automatic? |
|---|---|
| Termux packages (`python git openssh`) | yes (`pkg install`), once the bootstrap has python |
| Taskwarrior 2.6.2 | yes: fetch the gittw deb, check its pinned sha256, `dpkg -i`; a 3.x already installed gets a loud warning |
| SSH key | yes, if there is none |
| Key on GitHub / Codeberg | **you**: it prints the key and the `gh ssh-key add` line for another station, then waits for `ssh -T` to succeed |
| Clone `~/.task`, set push URLs | yes (push URLs taken from `--push`, or asked) |
| `~/.taskrc` symlink | yes; an existing `~/.taskrc` is moved aside, never deleted |
| `local.rc` | yes, from the station name and platform, with `hooks=off` |
| Gitignored include targets | yes, created empty |
| `PATH` in `~/.bashrc` | yes, once |
| tw-web | yes: clone, `pip install` (asks before installing `clang`) |
| Launcher | yes: a Termux:Widget shortcut in `~/.shortcuts/` (the widget app itself is **you**, from F-Droid) |
| Install tw-web as a browser app | **you**: it opens `http://localhost:5000` with `termux-open-url` |
| Commit the shared files it changed (`mobilize.rc`, its include), together; never push | yes |
| Final `check` | yes |

**`check`** (also on its own, any time, any station): `task` is 2.6.2; every include resolves; no
`/home/<user>` in rc files; `local.rc` names the station; push URLs cover the fetch URL; no dangling
symlinks or orphan gitlinks; each hook in this station's set has its interpreter and the tools it calls
(`nb`, `hledger`); the merge driver is configured. **`check --hooks`** runs each hook against a throwaway database
with sample input and times it: the pre-test that decides which hooks are `mobile`.

**Same tool on Linux:** `init` skips the Termux-only steps (pkg, deb, widget) when it isn't in Termux, so
a new laptop or desktop station is set up the same way.

## Not here

- **Sync rules:** gittw (station-tagged commits, last-seen, housekeeping at sync, the merge driver).
- **Hook marks:** in each hook; the registry's `.meta` may mirror them later.
- **tw-web changes** (`termux-open` for file links, a Termux launcher): tw-web.
- Taskwarrior 3.x.

## License

MIT
