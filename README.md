- Project: https://github.com/linuxcaffe/tw-mobilize
- Issues:  https://github.com/linuxcaffe/tw-mobilize/issues

# tw-mobilize

Set up a new Taskwarrior 2.6.2 station (an Android phone in Termux, or another Linux machine) from an
existing [gittw](https://github.com/linuxcaffe/gittw) repo, with [tw-web](https://github.com/linuxcaffe/tw-web)
running on it, and check that it stays set up.

**Two ways to use tw-web on a phone; this tool is the second:**
- **Connected:** the phone's browser opens the laptop's tw-web over [Tailscale](https://tailscale.com)
  (tw-web's README, "Network access"). Nothing to install on the phone but Tailscale, but the laptop must
  be on and both must be online.
- **On the phone itself (mobilize):** the phone is a station of its own: Taskwarrior, your tasks and
  tw-web all run in Termux, work offline, and sync with the other stations through gittw when online.

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

**Status: 0.8.2, in daily use on one phone** (`s5`, since 2026-10-22). Commands: `init`, `check`
(`--hooks`: the hook pre-test), `hooks` (each station's chosen hooks), `android` (the phone's own
settings, step by step), `stations`, `-e`; tested with pytest in a fake home folder. The Termux launcher
restarts tw-web when it dies, and opening Termux starts it. With nb installed, `init` also adds
home-screen buttons for quick nb notes (0.7.0), and with hledger-web installed, a button for it (0.7.2); and t-quick clocks in and out of the timelog (0.8.0). The task2 deb comes from [taskwarrior-2.6](https://github.com/linuxcaffe/taskwarrior-2.6) (0.8.1), tw-web from `tw -I tw-web` (0.8.2; `mobilize.web.repo` is no longer read). This README also keeps what was learned
setting up the first phone (2026-10-21/22): "Problems found" is the record.

## TL;DR

- A station is a device with its own copy of `~/.task`, cloned from your gittw remote. Today: `yoga`
  (laptop, `desktop`) and `s5` (phone, `mobile`).
- **Sync often, see the others, resolve the rare collision.** tw-web syncs automatically when online
  (shortly after a change, and every 15 minutes); its Sync dialog shows incoming changes and when each
  station last synced; gittw's merge driver resolves a collision per task, last write wins, and retries a
  pull or push the phone's network drops.
- **Shared config, local differences:** `~/.taskrc` is a symlink to the tracked `~/.task/.taskrc`, which
  ends with `include ~/.task/local.rc`. `local.rc` is untracked and holds what this station is.
- **Each station chooses its hooks:** `mobilize hooks --set need-priority,time-defaults,gittw` writes
  `mobilize.hooks`, `hooks.location` and `hooks = on` into `local.rc`, and builds `~/.task/hooks.d/<station>/`
  (links to just those apps' hooks; gitignored) for 2.6.2's `hooks.location`. `mobilize hooks` rebuilds it
  after a pull brings new hooks, `--all` goes back to every hook, and `check --hooks` tests the chosen set.
  An app installed on one station is installed on all (it's in the shared `~/.task`); `tw -l` marks
  `[~]` one whose hooks this station doesn't run, and `platforms=` in an app's `.meta` notes one that
  needs a desktop (tw 2.18.0).
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
| `~/.task/local.rc` | written per station, untracked | `gittw.device`, `tw.platforms`, `hooks=off` until hooks are tested, then `mobilize.hooks` and `hooks.location` (`mobilize hooks --set`) |
| `PATH` | `~/.bashrc`: `export PATH="$HOME/.task/scripts:$PATH"` | gives `tw`, `gittw`, the fleet scripts |
| tw-web | `tw -I tw-web` (0.8.2: a `git clone`, Codeberg else GitHub, and `pip install -r requirements.txt`), `python app.py` | serves `localhost:5000` only; open it in the browser and install it as an app |
| Launcher | `~/.shortcuts/tw-web` (Termux) | starts tw-web in a restart loop, then opens the browser; `--start` only starts it |
| Autostart | a marked block in `~/.bashrc` (Termux) | opening Termux starts tw-web if it isn't running |
| nb quick notes | `~/.shortcuts/nb-note`, `~/.shortcuts/tasks/nb-quick`, `~/bin/termux-url-opener` (Termux, once `nb` is installed) | home-screen buttons for [nb](https://github.com/xwmx/nb) (`npm install -g nb.sh`): `nb-note` asks in a terminal, `nb-quick` in Android popups (`pkg install termux-api`), and Share → Termux saves what was shared. The title becomes the filename, so a file browser shows real names. nb's logo is their widget icon (`~/.shortcuts/icons/`) |
| hledger-web button | `~/.shortcuts/hledger-web` (Termux, once hledger-web is installed) | mobilize doesn't install hledger-web: Termux's `hledger` is CLI only, and Debian trixie dropped hledger-web, so today it comes from a proot-distro Debian switched to sid (`apt install hledger-web`); a native one on Termux's PATH is used first. Serves `localhost:5050` (tw-web has 5000) on `$LEDGER_FILE` or `~/.hledger.journal`, with Termux's home bound into Debian at the same path; `--start`, `--stop`. hledger's logo is its widget icon |
| t-quick | `~/.shortcuts/tasks/t-quick` (Termux, once `~/.task/scripts/t` is there) | the timelog ([t](https://github.com/linuxcaffe/t)) in Android popups, no terminal: one list to clock in to a recent account (or a new one), or, while in, to clock out (with a note), comment or switch. While you're in, an ongoing notification shows the account and start time, with Out and Menu buttons; a toast says what t did. The icon is a green or red t (`icons/t-quick.png`, swapped from `.t-quick-in.png`/`.t-quick-out.png`), but Termux:Widget only redraws it with the widget, so the notification is the live status. `t-quick --refresh` syncs both after a `t in`/`t out` in a shell. Needs `termux-api` |

## Keeping tw-web running on Android

Android stops apps' background work when it likes: a server that ran fine for ten minutes is
gone, and the page (served from the browser's cache) says "Network error: Failed to fetch".
Three things help; mobilize does the first two.

1. **The launcher keeps it going.** `~/.shortcuts/tw-web` takes Termux's wake lock and runs the
   server in a loop that starts it again when it dies (a crash, or Android killing just the
   server). It doesn't restart a server stopped on purpose (tw-web's Settings → Stop server,
   Ctrl-C, `kill`), and gives up after 5 quick failures in a row. Each start, death and restart
   is a line in `~/.cache/tw-web/server.out`.
2. **Opening Termux starts it.** When Android kills Termux, the loop goes too. `mobilize init`
   writes a marked block to `~/.bashrc` that runs `~/.shortcuts/tw-web --start`, so opening
   Termux (or ssh into it) brings tw-web back, and takes the wake lock again (0.6.2): Android, or
   the notification's "Release wakelock", can drop it while tw-web runs, and then Termux and its
   sshd sleep with the screen (s5, 2026-10-23). Don't want it: `mobilize.web.autostart = no` in `local.rc`, then
   delete the block (between the `>>> mobilize: tw-web autostart >>>` lines).
3. **Tell Android to leave Termux alone (you):** the next section, step by step.

`mobilize check` warns when the launcher is the old one (no restart loop) or `~/.bashrc` lacks
the autostart block.

## The Android side, step by step (you)

**`mobilize android`** walks through these on the phone: for each step it says what to set,
offers to open the settings screen (`am start`), and waits for you; `--list` just prints the
steps for this phone. It can't check them for you: Termux may not read Android's battery, app
launch or overlay settings (that takes adb). It does see whether Termux:Widget is installed and
whether tw-web answers.

What mobilize can't do from inside Termux. Worked out on `s5` (Huawei P20, EMUI, Android 10),
2026-10-22; menu names differ between makers and versions, so the generic route comes first.

1. **Let Termux run in the background.**
   - Most phones: Settings → Apps → Termux → Battery: **Unrestricted** (not "Optimized").
   - **Huawei (EMUI):** Settings → Battery → **App launch**. Leave **"Manage all
     automatically" on** (that switch is for every app; off, they all run unchecked). Instead
     switch off **Termux's own** toggle in the list: a "Manage manually" dialog appears with
     **Auto-launch, Secondary launch, Run in background**; turn all three on. Do the same for
     Termux:Widget and Termux:API if installed. The three switches only exist in that dialog.
   - **Samsung:** keep Termux off "Sleeping apps" / "Deep sleeping apps".
   - **Android 12 and later**, any maker: the "phantom process killer" stops processes Termux
     started. Android 14+: Developer options → **Disable child process restrictions**; 12–13:
     `adb shell device_config put activity_manager max_phantom_processes 2147483647` from a computer.
   - Other makers: dontkillmyapp.com.
   - **Power saving mode off** while you rely on tw-web: battery saver (and Huawei's Ultra power
     saving) stops background apps whatever the per-app settings say. On again when you don't
     need tw-web is fine: opening Termux or tapping the widget brings it back.
2. **Lock Termux in recent apps** (Huawei and others that clear it): open Termux, open the
   recent-apps view (the □ button; with gestures, swipe up from the bottom edge and pause; with
   the fingerprint-sensor navigation key, swipe across the sensor), and drag Termux's card (the
   preview of its black terminal screen) down until a padlock shows.
3. **Add the widget:** install Termux:Widget (F-Droid, same source as Termux). Long-press the
   home screen → Widgets → Termux:Widget → **Termux shortcut** → `tw-web` (or the list widget,
   which shows everything in `~/.shortcuts`; tap its refresh icon if it's empty).
   Its icon is tw-web's menu logo: `mobilize init` copies `tw-logo.png` to
   `~/.shortcuts/icons/tw-web.png`, where Termux:Widget looks. A widget added before the icon
   was there keeps the plain one: remove it and add it again.
4. **Allow "Display over other apps" for Termux** (Android 10+): Termux asks the first time the
   widget runs, because a widget tap opens a Termux session from the background. EMUI hides the
   setting (Settings search finds nothing); open it straight from Termux:

   ```
   am start -a android.settings.action.MANAGE_OVERLAY_PERMISSION -d package:com.termux
   ```

   It lets Termux show its window, nothing more.
5. **Tap the widget.** It starts tw-web and opens the browser; Termux's notification then says
   "wake lock held". `~/.cache/tw-web/server.out` logs each start, death and restart
   (`mobilize:` lines).

## sshd on a phone: a development aid

Nothing in the scheme needs it: stations never talk to each other, only to the git remotes
(over ssh *out*, with the station's own key). `sshd` (port 8022) is how another machine works on
the phone: running `mobilize init`, reading logs. Keep it or not; if kept, **keys only**:

```
printf '\nPasswordAuthentication no\nKbdInteractiveAuthentication no\n' >> $PREFIX/etc/ssh/sshd_config
sshd -t && pkill -HUP -x sshd      # reload the running one in place
```

Check from another machine: `ssh -v -o PubkeyAuthentication=no <phone> true` must list only
`publickey`. `mobilize check` on the phone warns while sshd accepts passwords, and when a
hand-started sshd and the termux-services one are both set to run. It dies with Termux like everything else. Better than starting it by hand: let termux-services
run it, so it comes back by itself (`pkg install termux-services`, then `pkill -x sshd; sv-enable sshd`);
its log is then in `$PREFIX/var/log/sv/sshd/current`.

**Keeping ssh reachable** (it dropped every few minutes on `s5`):
- **Tailscale on the phone** (the Tailscale app, signed in to the same tailnet): `ssh` to its tailnet
  name or 100.x address works on any network, wifi or mobile data, with no router settings. On mobile
  data the traffic may go through a Tailscale relay, and the phone's network still drops a connection
  now and then. On the same wifi only:
- Settings → Wi-Fi → ⋮ / Configure → Advanced → **Keep Wi-Fi on during sleep: Always**
  (Huawei's default turns wifi off with the screen).
- **Wi-Fi+ off** (Huawei; others call it "Adaptive Wi-Fi" or "Switch to mobile data"): it moves the
  phone to mobile data, and its wifi address is gone.
- A fixed address for the phone in your router (a DHCP reservation), so `ssh <phone>` finds it
  (not needed with Tailscale).
- Termux holds a wake lock while tw-web runs (the launcher takes it), which keeps sshd from sleeping too. If you use
termux-services (`sv-enable sshd`) instead, don't also start it by hand: the hand-started one
holds port 8022 and the service fails every second ("Address already in use"), and in an ssh
command `sv` needs `SVDIR=$PREFIX/var/service`.

## Setting up a phone by hand (what worked, 2026-10-21)

Termux from F-Droid. Each step was done once on `s5`; the problems found are in the next section.

```bash
# 1. Taskwarrior 2.6.2 (once)
pkg install python git openssh
curl -fLO https://raw.githubusercontent.com/linuxcaffe/taskwarrior-2.6/main/termux/task2_2.6.2-3_aarch64.deb
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
`Port 8022`. The wifi address changes with the network (Tailscale's doesn't); `termux-wake-lock` keeps Termux alive.

On `s5` (2026-10-21) tw-web installed with no compiler (Python 3.13, Werkzeug 3.1.9) and served the same
104 pending tasks as `yoga`.

## Problems found (and what the tool must do about them)

| Found | Cause | Fixed / tool's job |
|---|---|---|
| `pkg`: "No mirror or mirror group is selected", then `curl`: `CANNOT LINK EXECUTABLE … ngtcp2…` (`s5`, 2026-10-22) | no `$PREFIX/etc/termux/chosen_mirrors` link; then packages half-upgraded (a newer libcurl than its ngtcp2), so `pkg` couldn't even test mirrors | Termux's, not ours: `termux-change-repo`, then `pkg upgrade` (fixed `s5`: 112 packages). README quick start says so; `init` repeats it when `pkg install` fails |
| `gittw sync` on `s5`: `git commit` exit 128 (2026-10-22) | no git name/email on the phone: it had never committed (and the auto-commit hook had been failing silently) | `init` asks for them (for the `~/.task` clone only), `check` warns; `init` also runs `gittw setup` (the merge driver is per clone) |
| `Permission denied (publickey)` on clone | the station's key was on neither host | `init` makes the key, prints it, and waits until `ssh -T` works for every push host |
| A station pushing to GitHub only would be invisible | `yoga` fetches from Codeberg and pushes to both | `init` copies the push URLs; `check` compares them with the fetch URL |
| `Could not read include file …/config/nag-state.rc` | gitignored state files (`nag-state.rc`, `tod.rc`, rewritten by hooks) are still included; **a missing include is a hard error in 2.6.2** | `init` creates every *gitignored* include target empty; a missing *tracked* one is a real error and is reported |
| Theme include failed after that | `themes/` was an orphan gitlink, so the theme files were never in the repo | fixed in `~/.task` (tracked as files). `check`: no gitlinks without `.gitmodules` |
| `tw: command not found` | `~/.task/scripts` isn't on Termux's `PATH` | `init` adds the line to `~/.bashrc` once |
| **The acid test (`s5`, 2026-10-22):** after moving `~/.task`, `~/.taskrc`, tw-web aside and removing `task2`, the three quick-start lines set the phone up again in one run: deb installed via `apt`, clone with both push URLs from `mobilize.rc`, `local.rc`, state files, `PATH`, tw-web (https, `~/.local/share/tw-web`, pip), launcher; tw-web served the same 104 pending tasks and stayed up after the shell closed | | `check`: 4 warnings, all dangling links (below) |
| Three scripts point at `yoga` paths | `make-awesome.py`, `tw-sync`, `tw-web` (scripts) and `timelog` (hooks) are symlinks to `/home/<user>/dev/…` | `check` lists dangling symlinks |
| `included.rc`/`themes.rc` had `include /home/<user>/.task/…` | installers and `theme-select.py` write absolute paths | rewritten as `~/.task/…` on `yoga`; the writers fixed (make-awesome 4.15.0 installers, theme-select 0.6.3, smart-nag 1.3.3). `check`: no `/home/<user>` in rc files |
| A read changed `pending.data` on `s5` | 2.6.2 expires `until:` tasks on any command (`export` too, even with `rc.gc=off`); gc and built-in recurrence also write on reads | not a tool bug: the merge driver absorbs expiry; `gc=off` in the shared `.taskrc` with gc run at sync time keeps gc out of reads (2026-10-22). `init` configures the driver in each clone |
| Fewer contexts in tw-web on `s5` (`need`, `tod`) | both are written by hooks: `context.need.read` in `need.rc` (tracked, but the hook rewrites it; the committed copy is blank) and `context.tod.read` in `tod.rc` (gitignored). With `hooks=off` nothing fills them | expected until the station runs those hooks; `check` lists contexts that are empty |
| tw-web: "Network error: Failed to fetch", no contexts | Android stopped Termux, so the server died (its log ends 13:42); the page itself came from the service worker's cache | 0.5.0: the launcher runs it in a restart loop with the wake lock, and `~/.bashrc` starts it when Termux opens (see "Keeping tw-web running on Android"); battery "Unrestricted" is you. It happened again in the offline test of 2026-10-22, after ~10 minutes; sshd died with it. tw-web now says "the tw-web server isn't answering", with Retry and an "Open Termux" button that starts it again |
| s5's 0.4.0 launcher kept as "edited by hand" | it named `…/bin/python` (the bootstrap's interpreter); init now computes `python3` | 0.5.1 recognises an old launcher whichever Python it names |
| Huawei: the App launch switches weren't there | they appear only in the dialog when one app is set to manual; the top "Manage all automatically" switch sets every app to manual | "The Android side, step by step" |
| The widget needs "Display over other apps", and EMUI's Settings search can't find it | Android 10+ blocks background activity starts; a widget tap is one | `am start … MANAGE_OVERLAY_PERMISSION -d package:com.termux` opens it (step 4) |
| Termux:API app installed, `termux-job-scheduler` missing | the app and its command-line package (`pkg install termux-api`) are separate | needed for scheduled sync (planned) |
| sshd on s5 accepted passwords | Termux's default | keys only since 2026-10-22 (section above); a `sv restart` from ssh failed (`SVDIR`) and then fought the hand-started sshd for the port |
| **Widget → tw-web, 2026-10-22 12:59:** restart loop + server up, tasks intact | | the offline test follows |
| ssh to `s5` dropped every few minutes ("Connection reset" in the handshake, then timeouts) | wifi asleep with the screen off, and a hand-started sshd | wifi kept on during sleep, Wi-Fi+ off, sshd under termux-services ("Keeping ssh reachable") |
| Completing a task in tw-web (widget-started): `Failed to execute '…/hooks.d/s5/on-modify_need-priority.py': No such file or directory` | Termux:Widget runs the launcher in a bare environment: no `LD_PRELOAD` (termux-exec, which makes `#!/usr/bin/env python3` work on Android) and no `~/.task/scripts` on `PATH`. Over ssh and in a Termux session both are set, so the pre-test passed. `.bashrc` can't help: the widget doesn't start bash | 0.6.0: the launcher sets both when missing; `check` reads the running server's environment and warns |
| **Hook pre-test, 2026-10-22** (`mobilize check --hooks`, 25 hooks): `yoga` +0.33 s on a read with all hooks, `s5` **+4.3 s on a read and +33 s over five commands**: each Python hook costs ~0.05 s on the laptop, 0.4–0.75 s on the phone (Python start-up) | the phone needs a small hook set; tw-web's reads already run with hooks off, but every write fires the on-launch, on-add/modify and on-exit chains | choose the `mobile` set by these numbers |
| `s5` left stopped mid-rebase, every later sync failing (2026-10-22) | `tw -u` on both stations wrote `config/.tw_manifest` with each one's absolute paths: a conflict the merge driver doesn't handle | gittw 0.3.2 undoes a conflict it can't merge and names the file; tw 2.18.0 writes the manifest with `~/.task/…` paths, so it's the same on every station |
| Sync on `s5` failed showing only "Pulling..." | the phone's mobile network aborted the connection mid-pull ("Software caused connection abort"); tw-web showed only gittw's progress | gittw 0.3.3 retries twice; tw-web's Sync box shows the errors too |
| `tw -l` on `s5` listed yoga's installs | it does: `~/.task` is shared, so every app's files are on both; what runs differs | tw 2.18.0 marks `[~]` what isn't at work on this station, and why |
| `on-modify_hledger-add.py` fails every `done` in the sandbox | it checks `TW_NOINTERACT` *before* whether the task is a transaction, so under tw-web (which sets it) every completion is refused with `tw_needs_pty` | a bug in tw-hledger-add-hook (since 2026-07-11): check the trigger first |
| `on-modify_tw2nb.py` fails on `s5` | calls `/usr/local/bin/nb` by absolute path | desktop-only (no nb on the phone) |
| The pre-test left `__pycache__/*.pyc` in the real `scripts/` | `scripts/` is linked into the sandbox, not copied | `PYTHONDONTWRITEBYTECODE=1` in the sandbox; `~/.task` now ignores `__pycache__/` and stopped tracking two (`yoga`, 2026-10-22) |

What carried over without trouble: exec bits (git keeps them), `#!/usr/bin/env python3` shebangs, every
hook's `~/.task` lookup (`Path.home()`, never a hard-coded home), tw-web's runtime folder (no
`XDG_RUNTIME_DIR` in Termux, so its `~/.cache/tw-web` fallback applies), and the Calendar's holidays and
moon (they arrive in the clone as `config/holidays.rc` and `sky.rc`).

## In the fleet

tw-mobilize is in the [awesome-taskwarrior](https://github.com/linuxcaffe/awesome-taskwarrior) registry
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
| **Built into the tool** | the task2 deb's URL and sha256, Termux package names, tw-web's default folder (its repo is `tw -I tw-web`'s) | facts about the tool, not about you; `mobilize.rc` or `local.rc` can override them |

A sketch of `mobilize.rc` (yours lives in your private gittw repo; nothing private is in the tool):

```
# mobilize.rc: how to set up a station. Shared; `mobilize init` reads it after cloning.
mobilize.data.push = git@codeberg.org:<you>/<data>.git, git@github.com:<you>/<data>.git
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

**Decided (2026-10-22):** the command is `mobilize` (repo `tw-mobilize`); `mobilize.rc` keeps a commented
list of stations; tw-web is cloned over https.

## What `mobilize init` does

The quick start's three lines are download-then-run (not `curl | sh`, so you can read `mobilize.py`
first). The steps that can't be automated are the ones that need you: putting a key on a website, the
phone's own settings, installing the app in the browser.

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
| tw-web | yes: `tw -I tw-web` (clone, `pip install`) |
| Android's own settings (background running, recents lock, widget, overlay) | **you**, with `mobilize android` opening each screen; `init` ends by pointing at it |
| Launcher | yes: a Termux:Widget shortcut in `~/.shortcuts/` that restarts tw-web when it dies (the widget app itself is **you**, from F-Droid); an unedited older launcher is replaced |
| Autostart | yes: a marked block in `~/.bashrc`, so opening Termux starts tw-web (`mobilize.web.autostart = no` leaves it out) |
| nb quick notes | yes, once `nb` is installed: three scripts and nb's logo as their icons (see "The pieces"); a script that's there and different, say your own `termux-url-opener`, is left as it is. `termux-api` for the popups is **you** |
| hledger-web button | yes, once hledger-web is installed: the launcher and hledger's logo as its icon; one that's there and different is left as it is. Installing hledger-web is **you** |
| t-quick | yes, once `t` is in `~/.task/scripts`: the script and its three icons; an edited script is left as it is. `termux-api` is **you** |
| Install tw-web as a browser app | **you**: it opens `http://localhost:5000` with `termux-open-url` |
| Commit the shared files it changed (`mobilize.rc`, its include), together; never push | yes |
| Final `check` | yes |

**`check`** (also on its own, any time, any station): `task` is 2.6.2; every include resolves; no
`/home/<user>` in rc files; `local.rc` names the station; push URLs cover the fetch URL; no dangling
symlinks or orphan gitlinks; each hook in this station's set has its interpreter and the tools it calls
(`nb`, `hledger`); the merge driver is configured; on Termux, the launcher is current, `~/.bashrc` has the autostart block, and sshd is keys only with no port clash. **`check --hooks`** runs each hook against a throwaway database
with sample input and times it: the pre-test that decides which hooks are `mobile`.

**Same tool on Linux:** `init` skips the Termux-only steps (pkg, deb, widget) when it isn't in Termux, so
a new laptop or desktop station is set up the same way.

## Not here

- **Sync rules:** gittw (station-tagged commits, last-seen, housekeeping at sync, the merge driver).
- **What an app suits:** `platforms=` in its registry `.meta` (awesome-taskwarrior); `tw -l` reads it.
- **tw-web's side** (the "server isn't answering" banner, automatic sync, `termux-open` for file links,
  the update pill): tw-web.
- Taskwarrior 3.x.

## License

MIT
