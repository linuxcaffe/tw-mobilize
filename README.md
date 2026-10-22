- Project: https://github.com/linuxcaffe/tw-mobilize (not published yet)
- Issues:  https://github.com/linuxcaffe/tw-mobilize/issues

# tw-mobilize

Set up a new Taskwarrior 2.6.2 station (an Android phone in Termux, or another Linux machine) from an
existing [gittw](https://github.com/linuxcaffe/gittw) repo, with [tw-web](https://github.com/linuxcaffe/tw-web)
running on it, and check that it stays set up.

**Status: design and field notes. No code yet.** This README holds what was learned by setting up the
first phone by hand (2026-10-21), so the tool can be written from it. Decisions and progress live in the
plan: `tw:docs/tw-web-android-plan.md`.

## TL;DR

- A station is a device with its own copy of `~/.task`, cloned from your gittw remote. Today: `yoga`
  (laptop, `desktop`) and `s5` (phone, `mobile`).
- **One writer at a time.** The station holding the *feather* may write; the others only read. That keeps
  every sync a fast-forward, so gittw never has to merge task data. The feather lives in gittw (planned).
- **Shared config, local differences:** `~/.taskrc` is a symlink to the tracked `~/.task/.taskrc`, which
  ends with `include ~/.task/local.rc`. `local.rc` is untracked and holds what this station is.
- **Each hook says which platforms it suits** (`# tw-platforms: desktop mobile`); a station runs the hooks
  marked for it, via 2.6.2's `hooks.location` (planned).
- `tw-mobilize init` does the setup, `tw-mobilize check` finds what has drifted (planned).

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

**Reaching the phone from another station** (the feather's direct route, and how Claude Code ran step 4
on `s5`): `pkg install openssh`, add the other station's public key to `~/.ssh/authorized_keys`, run
`sshd` (port 8022); on the other station, a `Host s5` entry with the phone's current address and
`Port 8022`. The address changes with the network; `termux-wake-lock` keeps Termux alive.

On `s5` (2026-10-21) tw-web installed with no compiler (Python 3.13, Werkzeug 3.1.9) and served the same
104 pending tasks as `yoga`.

**Until the feather exists, don't `git push` from a second station.** Its auto-commits stay local;
`git checkout -- pending.data completed.data` throws test writes away.

## Problems found (and what the tool must do about them)

| Found | Cause | Fixed / tool's job |
|---|---|---|
| `Permission denied (publickey)` on clone | the station's key was on neither host | `init` makes the key, prints it, and waits until `ssh -T` works for every push host |
| A station pushing to GitHub only would be invisible | `yoga` fetches from Codeberg and pushes to both | `init` copies the push URLs; `check` compares them with the fetch URL |
| `Could not read include file …/config/nag-state.rc` | gitignored state files (`nag-state.rc`, `tod.rc`, rewritten by hooks) are still included; **a missing include is a hard error in 2.6.2** | `init` creates every *gitignored* include target empty; a missing *tracked* one is a real error and is reported |
| Theme include failed after that | `themes/` was an orphan gitlink, so the theme files were never in the repo | fixed in `~/.task` (tracked as files). `check`: no gitlinks without `.gitmodules` |
| `tw: command not found` | `~/.task/scripts` isn't on Termux's `PATH` | `init` adds the line to `~/.bashrc` once |
| Three scripts point at `yoga` paths | `make-awesome.py`, `tw-sync`, `tw-web` are symlinks to `/home/<user>/dev/…` | `check` lists dangling symlinks |
| `included.rc`/`themes.rc` had `include /home/<user>/.task/…` | installers and `theme-select.py` write absolute paths | rewritten as `~/.task/…` on `yoga`; the writers get fixed in awesome-taskwarrior. `check`: no `/home/<user>` in rc files |

| A read changed `pending.data` on `s5` | 2.6.2 expires `until:` tasks on any command (`export` too, even with `rc.gc=off`); gc and built-in recurrence also write on reads | not a tool bug: the feather's rule (a device without it never commits data; `gittw take` saves local data changes, then resets to the remote). `check` reports uncommitted data changes on a device without the feather |

| Fewer contexts in tw-web on `s5` (`need`, `tod`) | both are written by hooks: `context.need.read` in `need.rc` (tracked, but the hook rewrites it; the committed copy is blank) and `context.tod.read` in `tod.rc` (gitignored). With `hooks=off` nothing fills them | expected until the station runs those hooks; `check` lists contexts that are empty |
| tw-web: "Network error: Failed to fetch", no contexts | Android stopped Termux, so the server died (its log ends 13:42); the page itself came from the service worker's cache | `termux-wake-lock`, Termux set to "unrestricted" battery use, and a launcher that starts the server if it's down. tw-web could say "the tw-web server isn't running" instead |

What carried over without trouble: exec bits (git keeps them), `#!/usr/bin/env python3` shebangs, every
hook's `~/.task` lookup (`Path.home()`, never a hard-coded home), tw-web's runtime folder (no
`XDG_RUNTIME_DIR` in Termux, so its `~/.cache/tw-web` fallback applies), and the Calendar's holidays and
moon (they arrive in the clone as `config/holidays.rc` and `sky.rc`).

## Semi-automating it: `curl` → `tw-mobilize init`

Almost all of the above can be one command. The parts that can't are the ones that need you: putting a
key on a website, installing the app in the browser.

**Bootstrap.** Two lines on a fresh Termux, download-then-run (not `curl | sh`, so you can read it first):

```bash
pkg install python git openssh
curl -fLO https://raw.githubusercontent.com/linuxcaffe/tw-mobilize/main/tw-mobilize
python tw-mobilize init --station s5 --platform mobile --data <your gittw fetch URL>
```

**Shape of the tool:** one Python file, standard library only (it runs before anything else is
installed), every step **idempotent**: it checks first, says what it found, and does only what's missing,
so running it again after a failure or a month later is safe. Nothing private in the tool itself: the data
remote is an argument, so the repo can be public.

| `init` step | Automatic? |
|---|---|
| Termux packages (`python git openssh`) | yes (`pkg install`), once the bootstrap has python |
| Taskwarrior 2.6.2 | yes: fetch the gittw deb, check its pinned sha256, `dpkg -i`, refuse if `task` reports 3.x |
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
| Final `check` | yes |

**`check`** (also on its own, any time, any station): `task` is 2.6.2; every include resolves; no
`/home/<user>` in rc files; `local.rc` names the station; push URLs cover the fetch URL; no dangling
symlinks or orphan gitlinks; each hook in this station's set has its interpreter and the tools it calls
(`nb`, `hledger`); who holds the feather. **`check --hooks`** runs each hook against a throwaway database
with sample input and times it: the pre-test that decides which hooks are `mobile`.

**Same tool on Linux:** `init` skips the Termux-only steps (pkg, deb, widget) when it isn't in Termux, so
a new laptop or desktop station is set up the same way.

## Not here

- **Sync rules and the feather:** gittw (`gittw take` / `give`, the write guard).
- **Hook marks:** in each hook; the registry's `.meta` may mirror them later.
- **tw-web changes** (`termux-open` for file links, a Termux launcher): tw-web.
- Taskwarrior 3.x.

## License

MIT
