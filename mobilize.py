#!/usr/bin/env python3
"""
mobilize — set up a Taskwarrior 2.6.2 station (an Android phone in Termux, or another
Linux machine) from an existing gittw repo, with tw-web on it, and check that it stays
set up.

Commands
  mobilize init [--data URL] [--station NAME] [--platform desktop|mobile]
                [--no-web] [--dry-run] [--yes]
                                  Set up this station, one step at a time: packages,
                                  Taskwarrior 2.6.2, an SSH key, the clone of your
                                  gittw repo and where it pushes, ~/.taskrc, local.rc,
                                  missing state files, PATH, mobilize.rc, tw-web and
                                  (Termux) a launcher, and nb's quick-note buttons. Each step looks first and does
                                  only what's missing, so it's safe to run again.
                                  Nothing is deleted: what's in the way is moved
                                  aside (*.pre-mobilize-<time>). Ends with `check`.
  mobilize check [--json]         Read-only. Is this station still set up?
  mobilize check --hooks [--json] The hook pre-test: run this station's hooks, one at a
                                  time and then together, through real task commands
                                  (add, modify, annotate, done, list) in a sandbox copy
                                  of ~/.task, and time each against no hooks. Never
                                  touches your tasks or your hooks' real files.
  mobilize stations               The stations mobilize.rc knows about.
  mobilize hooks [--set APPS | --all]
                                  Which hooks this station runs. --set need-priority,gittw
                                  writes local.rc (mobilize.hooks, hooks.location, hooks = on)
                                  and builds ~/.task/hooks.d/<station>/: links to just those
                                  apps' hooks. --all goes back to every hook. No option:
                                  rebuild the folder from local.rc (after a pull brings new
                                  hooks) and list it.
  mobilize android [--list]       A phone's own settings, which Termux can't change: run
                                  in the background (Huawei's App launch, battery), lock
                                  in recent apps, the home-screen button, "Display over
                                  other apps". Opens each settings screen and waits for
                                  you; --list just prints the steps for this phone.
  mobilize -e [local]             Edit mobilize.rc (shared by every station), or this
                                  station's local.rc, in $VISUAL / $EDITOR.
  mobilize -h                     This help.

Taskwarrior 2.6.2 is what this is written for. 3.x gets a loud warning, not a refusal:
nothing here is tested with it, but nothing stops you either.

--data is your gittw repo's fetch URL: needed only when ~/.task isn't cloned yet.
Shared settings live in config/mobilize.rc (tracked: every station reads it), this
station's in local.rc (untracked). All under the task dir: $TW_TASK_DIR, else ~/.task.
Exit 0 ok, 1 warnings, 2 failures. Standard library only.
"""
import argparse
import base64
import datetime as dt
import hashlib
import json
import os
import platform
import re
import shlex
import shutil
import socket
import subprocess
import tempfile
import time
import sys
import urllib.request
from pathlib import Path

__version__ = '0.8.0'

OK, DONE, WOULD, SKIP, WARN, FAIL, YOU = 'ok', 'done', 'would', 'skip', 'warn', 'fail', 'you'

TW3_WARNING = '''\
  !!! WARNING: Taskwarrior {v} is installed. This is NOT what the fleet is written for.
  !!! tw-web, gittw, the hooks and mobilize target Taskwarrior 2.6.2 and are not tested
  !!! with 3.x: its data lives in a different format, and sync works differently. Some
  !!! of it may work. You're on your own: keep a backup, and expect surprises.'''

# Facts about the tool, not about you: mobilize.rc (or local.rc) can override each one.
TASK_DEB_URL = 'https://raw.githubusercontent.com/linuxcaffe/gittw/main/task2_2.6.2-3_aarch64.deb'
TASK_DEB_SHA256 = 'bd80e37006fc59a4faaba4a74d99883bc1e4141c711c5fc4775625f667b594a8'
WEB_REPO = 'https://codeberg.org/linuxcaffe/tw-web.git'
WEB_DIR = '~/.local/share/tw-web'
WEB_PORT = 5000
WEB_MODULES = 'flask, flask_cors, flask_sock, tabulate'
TERMUX_PACKAGES = (('python', 'python'), ('git', 'git'), ('openssh', 'ssh'), ('curl', 'curl'))
PLATFORMS = ('desktop', 'mobile')
SSH_ENV = {'GIT_SSH_COMMAND': 'ssh -o BatchMode=yes -o StrictHostKeyChecking=accept-new'}
# Where 2.6.2 looks for a relative include it can't find next to the including file
SYSTEM_RC_DIRS = ('/usr/share/taskwarrior', '/usr/share/doc/task/rc', '/usr/local/share/doc/task/rc')


# ── paths and settings ────────────────────────────────────────────────────────

def task_dir():
    return Path(os.environ.get('TW_TASK_DIR') or Path.home() / '.task').expanduser()


def taskrc_link():
    return Path.home() / '.taskrc'


def local_rc():
    return task_dir() / 'local.rc'


def mobilize_rc():
    return task_dir() / 'config' / 'mobilize.rc'


def is_termux():
    return 'TERMUX_VERSION' in os.environ or os.environ.get('PREFIX', '').startswith('/data/data/com.termux')


def read_rc(path):
    """key = value lines of one rc file (no includes followed); `#` starts a comment, as in 2.6.2."""
    out = {}
    try:
        lines = Path(path).read_text().splitlines()
    except OSError:
        return out
    for line in lines:
        line = line.split('#', 1)[0].strip()
        if '=' in line and not line.startswith('include '):
            key, _, value = line.partition('=')
            out[key.strip()] = value.strip()
    return out


def settings():
    """mobilize.rc, then local.rc on top: this station's word wins."""
    s = read_rc(mobilize_rc())
    s.update(read_rc(local_rc()))
    return s


def _list(value):
    return [v.strip() for v in (value or '').split(',') if v.strip()]


def stations(s=None):
    """{name: {platform, ssh, …}} from the mobilize.station.<name>.<key> lines."""
    s = settings() if s is None else s
    out = {}
    for key, value in s.items():
        m = re.match(r'^mobilize\.station\.([^.]+)\.(\w+)$', key)
        if m:
            out.setdefault(m.group(1), {})[m.group(2)] = value
    return out


def _include_target(raw, including):
    """Where 2.6.2 finds `include <raw>` written in file `including`, or None if nowhere."""
    p = Path(raw).expanduser()
    if p.is_absolute():
        return p if p.exists() else None
    dirs = [Path(including).parent, Path(including).resolve().parent, *map(Path, SYSTEM_RC_DIRS)]
    prefix = os.environ.get('PREFIX')
    if prefix:
        dirs += [Path(prefix) / 'share/doc/task/rc', Path(prefix) / 'share/taskwarrior']
    for d in dirs:
        if (d / p).exists():
            return d / p
    return None


def includes(path, seen=None):
    """Every include reachable from rc file `path`: (file, line number, raw, target or None)."""
    seen = set() if seen is None else seen
    path = Path(path)
    try:
        real = path.resolve()
        if real in seen:
            return []
        seen.add(real)
        lines = path.read_text().splitlines()
    except OSError:
        return []
    out = []
    for n, line in enumerate(lines, 1):
        line = line.split('#', 1)[0].strip()
        if not line.startswith('include '):
            continue
        raw = line[len('include '):].strip()
        target = _include_target(raw, path)
        out.append((path, n, raw, target))
        if target:
            out += includes(target, seen)
    return out


def _expected_path(raw, including):
    """Where a missing include would have to be created (relative: next to the including file)."""
    p = Path(raw).expanduser()
    return p if p.is_absolute() else Path(including).parent / p


# ── small helpers ─────────────────────────────────────────────────────────────

def run(argv, env=None, **kw):
    e = dict(os.environ, **(env or {}))
    return subprocess.run(argv, capture_output=True, text=True, env=e, **kw)


def git(*args, env=None):
    return run(['git', '-C', str(task_dir()), *args], env=env)


def task_version():
    if not shutil.which('task'):
        return None
    r = run(['task', '--version'])
    return r.stdout.strip() if r.returncode == 0 else None


def is_ssh_url(url):
    return url.startswith(('git@', 'ssh://')) or bool(re.match(r'^[\w.-]+@[\w.-]+:', url))


def ssh_host(url):
    m = re.match(r'^(?:ssh://)?(?:[\w.-]+@)?([\w.-]+)[:/]', url)
    return m.group(1) if m else url


def reachable(url):
    """(ok, stderr): can this station read `url` with its key, without any prompt?"""
    r = run(['git', 'ls-remote', url, 'HEAD'], env=SSH_ENV, timeout=60)
    return r.returncode == 0, r.stderr.strip()


def push_urls():
    r = git('remote', 'get-url', '--push', '--all', 'origin')
    return r.stdout.split() if r.returncode == 0 else []


def fetch_url():
    r = git('remote', 'get-url', 'origin')
    return r.stdout.strip() if r.returncode == 0 else ''


def stamp():
    return dt.datetime.now().strftime('%Y%m%d-%H%M%S')


def move_aside(path):
    dest = path.with_name(f'{path.name}.pre-mobilize-{stamp()}')
    path.rename(dest)
    return dest


def ignored(path):
    """Does the gittw repo deliberately not track `path` (a state file, local.rc)?"""
    try:
        rel = Path(path).resolve().relative_to(task_dir().resolve())
    except ValueError:
        return False
    return git('check-ignore', '-q', str(rel)).returncode == 0


def web_dir(s=None):
    s = settings() if s is None else s
    return Path(s.get('mobilize.web.dir') or WEB_DIR).expanduser()


def web_python(d):
    """The interpreter tw-web runs with: its own venv if it has one."""
    venv = d / '.venv' / 'bin' / 'python'
    return str(venv) if venv.exists() else (sys.executable or 'python3')


def web_up(port=WEB_PORT):
    try:
        with socket.create_connection(('127.0.0.1', port), timeout=1):
            return True
    except OSError:
        return False


def append(path, text):
    with open(path, 'a') as f:
        f.write(text)


# ── rc templates ──────────────────────────────────────────────────────────────

def local_rc_text(station, platform):
    hooks = ('hooks = off' if platform == 'mobile' else '# hooks = off')
    return f"""\
# local.rc: this station's differences from the shared .taskrc.
#
# Not tracked by git: every station has its own, and the shared ~/.task/.taskrc includes
# it on its last line, so a setting here wins over the shared one. Keep it short: anything
# every station wants belongs in the shared files. Written by `mobilize init` on
# {dt.date.today().isoformat()}; `mobilize check` reads it. See ~/.task/docs/mobilize_README.md.

# Who this station is. gittw tags its commits with this name, and mobilize.rc's
# mobilize.station.<name>.* lines describe it to the other stations.
gittw.device = {station}

# What kind of station this is: desktop or mobile. Hooks say which kinds they suit
# (a `# tw-platforms:` line); a station runs the ones marked for it (planned).
tw.platforms = {platform}

# A new mobile station starts with hooks off: they run on every command, some call
# desktop tools (nb, hledger), and they haven't been tried here yet. Turn them on
# once `mobilize check --hooks` (planned) says they work on this station.
{hooks}

# Where tw-web lives, if not {WEB_DIR}: say, a development checkout.
# mobilize.web.dir = ~/dev/tw-web
"""


def mobilize_rc_text(pushes, station, platform, ssh=None):
    push = ', '.join(pushes) if pushes else ''
    return f"""\
# mobilize.rc: how this gittw repo's stations are set up, and which stations there are.
#
# Shared: it's tracked in ~/.task, so every station reads the same file. `mobilize init`
# reads it right after cloning, to finish setting up a new station; `mobilize check`
# compares a station with it. A setting only one station needs goes in that station's
# local.rc instead (local.rc is read after this file, so it wins).
#
# Keys are mobilize.<setting>; `#` starts a comment (as everywhere in Taskwarrior's rc
# files, even mid-line, so don't put one inside a value). Written by `mobilize init` on
# {dt.date.today().isoformat()}; edit freely, mobilize keeps what's here and only adds a
# station it doesn't know yet. See ~/.task/docs/mobilize_README.md.

# ── The data repo ─────────────────────────────────────────────────────────────
#
# Where every station pushes ~/.task, comma-separated. Each station must push to the
# place the others fetch from, or its work never reaches them: if you fetch from one
# mirror and push to two, list both. `init` gives a new station's clone these push
# URLs; `check` warns when a station's don't match.
mobilize.data.push = {push}

# ── tw-web ────────────────────────────────────────────────────────────────────
#
# Where tw-web is cloned from. https needs no key, so a station can run tw-web without
# being able to push to it; a station where you develop tw-web switches its own clone
# to SSH. The folder it goes in is per station: mobilize.web.dir in local.rc (default
# {WEB_DIR}).
mobilize.web.repo = {WEB_REPO}

# ── The stations ──────────────────────────────────────────────────────────────
#
# One block per station, named like a host (the name is gittw.device in that station's
# local.rc). `init` adds a block for a station it sets up; nothing else changes them.
#
#   mobilize.station.<name>.platform = desktop | mobile
#       What kind of station it is. Hooks marked for that kind run there (planned).
#   mobilize.station.<name>.ssh = <Host alias or user@address>
#       How the other stations reach it directly over SSH, when they can: to sync with
#       no internet (a laptop tethered to the phone), and to ask it live how many
#       changes it hasn't pushed. A phone's address changes with the network, so an
#       alias in ~/.ssh/config (Host s5 / HostName … / Port 8022) is easier to keep
#       up to date than an address here. Leave it out if nothing reaches it.
#   mobilize.station.<name>.note = <anything>
#       For you: what the station is, where it lives.

mobilize.station.{station}.platform = {platform}
""" + (f'mobilize.station.{station}.ssh = {ssh}\n' if ssh else '')


def station_block(station, platform):
    return f'\nmobilize.station.{station}.platform = {platform}\n'


def launcher_text(d, py, port=WEB_PORT):
    prefix = os.environ.get('PREFIX', '/data/data/com.termux/files/usr')
    scripts = task_dir() / 'scripts'
    return f"""\
#!{prefix}/bin/sh
# tw-web: start the server if it isn't running, then open it in the browser.
# Written by `mobilize init`, which replaces it when a newer mobilize writes it
# differently (unless you've edited it). Termux:Widget shows it on the home screen.
#
#   ~/.shortcuts/tw-web           start tw-web if needed, then open it in the browser
#   ~/.shortcuts/tw-web --start   start tw-web if needed, nothing else (~/.bashrc runs this)
#
# The server runs in a loop that starts it again when it dies: a crash, or Android
# stopping it. Not when it was stopped on purpose (Settings → Stop server, Ctrl-C, kill),
# and not after 5 quick failures in a row. Android can also stop the loop itself; then
# opening Termux starts it again (the block mobilize writes in ~/.bashrc).
# Server output, and the loop's notes: ~/.cache/tw-web/server.out
#
# Every run takes Termux's wake lock, not only the one that starts the server: Android (or
# the notification's "Release wakelock") can drop it, and then Termux and its sshd sleep
# with the screen. Opening Termux, an ssh login or the widget takes it back.
#
# Started from the widget, it gets a bare environment: no LD_PRELOAD (termux-exec, which makes
# `#!/usr/bin/env python3` work: without it every hook fails "No such file or directory") and
# no fleet scripts on PATH (gittw). A Termux session sets both; this sets them when missing.
if [ -z "$LD_PRELOAD" ]; then
    for lib in {prefix}/lib/libtermux-exec-ld-preload.so {prefix}/lib/libtermux-exec.so; do
        if [ -f "$lib" ]; then export LD_PRELOAD="$lib"; break; fi
    done
fi
case ":$PATH:" in *":{scripts}:"*) ;; *) export PATH="{scripts}:$PATH" ;; esac
port={port}
run=${{XDG_RUNTIME_DIR:+$XDG_RUNTIME_DIR/tw-web}}
run=${{run:-$HOME/.cache/tw-web}}
pidf=$run/tw-web.pid
loopf=$run/tw-web.loop
log=$HOME/.cache/tw-web/server.out

up() {{ curl -s -o /dev/null --max-time 2 http://127.0.0.1:$port/; }}
looping() {{ [ -f "$loopf" ] && kill -0 "$(cat "$loopf")" 2>/dev/null; }}
note() {{ echo "$(date '+%F %T') mobilize: $*" >> "$log"; }}

if [ "$1" = --loop ]; then
    echo $$ > "$loopf"
    cd "{d}" || exit 1
    fails=0
    while :; do
        t0=$(date +%s)
        "{py}" app.py >> "$log" 2>&1 &
        echo $! > "$pidf"
        wait $!
        code=$?
        # tw-web's Stop removes the PID file; SIGTERM/SIGINT mean someone stopped it
        if [ ! -f "$pidf" ] || [ $code -eq 0 ] || [ $code -eq 130 ] || [ $code -eq 143 ]; then
            note "tw-web stopped (exit $code): not restarting"
            break
        fi
        if [ $(( $(date +%s) - t0 )) -lt 30 ]; then fails=$((fails + 1)); else fails=0; fi
        if [ $fails -ge 5 ]; then
            note "tw-web failed 5 times in a row (exit $code): giving up, see above"
            break
        fi
        note "tw-web died (exit $code): starting it again"
        sleep 2
    done
    rm -f "$pidf" "$loopf"
    termux-wake-unlock 2>/dev/null
    exit 0
fi

termux-wake-lock 2>/dev/null
if ! up && ! looping; then
    mkdir -p "$run" "${{log%/*}}"
    note "starting tw-web"
    nohup sh "$0" --loop > /dev/null 2>&1 &
    [ -t 1 ] && echo "tw-web: starting on http://localhost:$port/"
    [ "$1" = --start ] && exit 0
    i=0
    while [ $i -lt 30 ] && ! up; do sleep 0.5; i=$((i + 1)); done
fi
[ "$1" = --start ] && exit 0
termux-open-url http://localhost:$port/
"""


def _launcher_text_0_6(d, py, port=WEB_PORT):
    """The launcher mobilize 0.6.x wrote (the wake lock taken only when it started the server)."""
    prefix = os.environ.get('PREFIX', '/data/data/com.termux/files/usr')
    scripts = task_dir() / 'scripts'
    return f"""\
#!{prefix}/bin/sh
# tw-web: start the server if it isn't running, then open it in the browser.
# Written by `mobilize init`, which replaces it when a newer mobilize writes it
# differently (unless you've edited it). Termux:Widget shows it on the home screen.
#
#   ~/.shortcuts/tw-web           start tw-web if needed, then open it in the browser
#   ~/.shortcuts/tw-web --start   start tw-web if needed, nothing else (~/.bashrc runs this)
#
# The server runs in a loop that starts it again when it dies: a crash, or Android
# stopping it. Not when it was stopped on purpose (Settings → Stop server, Ctrl-C, kill),
# and not after 5 quick failures in a row. Android can also stop the loop itself; then
# opening Termux starts it again (the block mobilize writes in ~/.bashrc).
# Server output, and the loop's notes: ~/.cache/tw-web/server.out
#
# Started from the widget, it gets a bare environment: no LD_PRELOAD (termux-exec, which makes
# `#!/usr/bin/env python3` work: without it every hook fails "No such file or directory") and
# no fleet scripts on PATH (gittw). A Termux session sets both; this sets them when missing.
if [ -z "$LD_PRELOAD" ]; then
    for lib in {prefix}/lib/libtermux-exec-ld-preload.so {prefix}/lib/libtermux-exec.so; do
        if [ -f "$lib" ]; then export LD_PRELOAD="$lib"; break; fi
    done
fi
case ":$PATH:" in *":{scripts}:"*) ;; *) export PATH="{scripts}:$PATH" ;; esac
port={port}
run=${{XDG_RUNTIME_DIR:+$XDG_RUNTIME_DIR/tw-web}}
run=${{run:-$HOME/.cache/tw-web}}
pidf=$run/tw-web.pid
loopf=$run/tw-web.loop
log=$HOME/.cache/tw-web/server.out

up() {{ curl -s -o /dev/null --max-time 2 http://127.0.0.1:$port/; }}
looping() {{ [ -f "$loopf" ] && kill -0 "$(cat "$loopf")" 2>/dev/null; }}
note() {{ echo "$(date '+%F %T') mobilize: $*" >> "$log"; }}

if [ "$1" = --loop ]; then
    echo $$ > "$loopf"
    cd "{d}" || exit 1
    fails=0
    while :; do
        t0=$(date +%s)
        "{py}" app.py >> "$log" 2>&1 &
        echo $! > "$pidf"
        wait $!
        code=$?
        # tw-web's Stop removes the PID file; SIGTERM/SIGINT mean someone stopped it
        if [ ! -f "$pidf" ] || [ $code -eq 0 ] || [ $code -eq 130 ] || [ $code -eq 143 ]; then
            note "tw-web stopped (exit $code): not restarting"
            break
        fi
        if [ $(( $(date +%s) - t0 )) -lt 30 ]; then fails=$((fails + 1)); else fails=0; fi
        if [ $fails -ge 5 ]; then
            note "tw-web failed 5 times in a row (exit $code): giving up, see above"
            break
        fi
        note "tw-web died (exit $code): starting it again"
        sleep 2
    done
    rm -f "$pidf" "$loopf"
    termux-wake-unlock 2>/dev/null
    exit 0
fi

if ! up && ! looping; then
    mkdir -p "$run" "${{log%/*}}"
    termux-wake-lock 2>/dev/null
    note "starting tw-web"
    nohup sh "$0" --loop > /dev/null 2>&1 &
    [ -t 1 ] && echo "tw-web: starting on http://localhost:$port/"
    [ "$1" = --start ] && exit 0
    i=0
    while [ $i -lt 30 ] && ! up; do sleep 0.5; i=$((i + 1)); done
fi
[ "$1" = --start ] && exit 0
termux-open-url http://localhost:$port/
"""


def _launcher_text_0_5(d, py, port=WEB_PORT):
    """The launcher mobilize 0.5.x wrote (no LD_PRELOAD or PATH of its own)."""
    prefix = os.environ.get('PREFIX', '/data/data/com.termux/files/usr')
    return f"""\
#!{prefix}/bin/sh
# tw-web: start the server if it isn't running, then open it in the browser.
# Written by `mobilize init`, which replaces it when a newer mobilize writes it
# differently (unless you've edited it). Termux:Widget shows it on the home screen.
#
#   ~/.shortcuts/tw-web           start tw-web if needed, then open it in the browser
#   ~/.shortcuts/tw-web --start   start tw-web if needed, nothing else (~/.bashrc runs this)
#
# The server runs in a loop that starts it again when it dies: a crash, or Android
# stopping it. Not when it was stopped on purpose (Settings → Stop server, Ctrl-C, kill),
# and not after 5 quick failures in a row. Android can also stop the loop itself; then
# opening Termux starts it again (the block mobilize writes in ~/.bashrc).
# Server output, and the loop's notes: ~/.cache/tw-web/server.out
port={port}
run=${{XDG_RUNTIME_DIR:+$XDG_RUNTIME_DIR/tw-web}}
run=${{run:-$HOME/.cache/tw-web}}
pidf=$run/tw-web.pid
loopf=$run/tw-web.loop
log=$HOME/.cache/tw-web/server.out

up() {{ curl -s -o /dev/null --max-time 2 http://127.0.0.1:$port/; }}
looping() {{ [ -f "$loopf" ] && kill -0 "$(cat "$loopf")" 2>/dev/null; }}
note() {{ echo "$(date '+%F %T') mobilize: $*" >> "$log"; }}

if [ "$1" = --loop ]; then
    echo $$ > "$loopf"
    cd "{d}" || exit 1
    fails=0
    while :; do
        t0=$(date +%s)
        "{py}" app.py >> "$log" 2>&1 &
        echo $! > "$pidf"
        wait $!
        code=$?
        # tw-web's Stop removes the PID file; SIGTERM/SIGINT mean someone stopped it
        if [ ! -f "$pidf" ] || [ $code -eq 0 ] || [ $code -eq 130 ] || [ $code -eq 143 ]; then
            note "tw-web stopped (exit $code): not restarting"
            break
        fi
        if [ $(( $(date +%s) - t0 )) -lt 30 ]; then fails=$((fails + 1)); else fails=0; fi
        if [ $fails -ge 5 ]; then
            note "tw-web failed 5 times in a row (exit $code): giving up, see above"
            break
        fi
        note "tw-web died (exit $code): starting it again"
        sleep 2
    done
    rm -f "$pidf" "$loopf"
    termux-wake-unlock 2>/dev/null
    exit 0
fi

if ! up && ! looping; then
    mkdir -p "$run" "${{log%/*}}"
    termux-wake-lock 2>/dev/null
    note "starting tw-web"
    nohup sh "$0" --loop > /dev/null 2>&1 &
    [ -t 1 ] && echo "tw-web: starting on http://localhost:$port/"
    [ "$1" = --start ] && exit 0
    i=0
    while [ $i -lt 30 ] && ! up; do sleep 0.5; i=$((i + 1)); done
fi
[ "$1" = --start ] && exit 0
termux-open-url http://localhost:$port/
"""


def _launcher_text_0_4(d, py, port=WEB_PORT):
    """The launcher mobilize 0.1.0–0.4.0 wrote: init replaces it when it finds it unedited."""
    prefix = os.environ.get('PREFIX', '/data/data/com.termux/files/usr')
    return f"""\
#!{prefix}/bin/sh
# tw-web: start the server if it isn't running, then open it in the browser.
# Written by `mobilize init`. Termux:Widget shows it on the home screen; it also runs
# from a shell (~/.shortcuts/tw-web). Server output: ~/.cache/tw-web/server.out
termux-wake-lock
if ! curl -s -o /dev/null http://127.0.0.1:{port}/; then
    mkdir -p ~/.cache/tw-web
    cd {d} && nohup {py} app.py >> ~/.cache/tw-web/server.out 2>&1 &
    sleep 4
fi
termux-open-url http://localhost:{port}/
"""


def is_old_launcher(text, d):
    """True when text is a launcher an older mobilize wrote for tw-web in d, whichever Python it named."""
    marker = '\x00PY\x00'
    for older in (_launcher_text_0_4, _launcher_text_0_5, _launcher_text_0_6):
        pattern = re.escape(older(d, marker)).replace(re.escape(marker), r'\S+')
        if re.fullmatch(pattern, text):
            return True
    return False


def server_environ(pidfile=None):
    """The running tw-web server's environment (from its PID file), or None."""
    run_dir = Path(os.environ['XDG_RUNTIME_DIR']) / 'tw-web' if os.environ.get('XDG_RUNTIME_DIR') \
        else Path.home() / '.cache' / 'tw-web'
    try:
        pid = int((pidfile or run_dir / 'tw-web.pid').read_text().strip())
        raw = Path(f'/proc/{pid}/environ').read_bytes()
    except (OSError, ValueError):
        return None
    return dict(item.split('=', 1) for item in raw.decode(errors='replace').split('\0') if '=' in item)


AUTOSTART_BEGIN = '# >>> mobilize: tw-web autostart >>>'
AUTOSTART_END = '# <<< mobilize: tw-web autostart <<<'


def autostart_block():
    return f"""\
{AUTOSTART_BEGIN}
# Written by `mobilize init`. Opening Termux (or ssh into it) starts tw-web if it isn't
# running: Android stops Termux's background work when it likes. Keep it out with
# mobilize.web.autostart = no in local.rc, then delete this block.
if [ -x "$HOME/.shortcuts/tw-web" ]; then "$HOME/.shortcuts/tw-web" --start; fi
{AUTOSTART_END}
"""


def autostart_wanted(s=None):
    s = settings() if s is None else s
    return str(s.get('mobilize.web.autostart', 'yes')).strip().lower() not in ('no', 'off', 'false', '0')


def bashrc_autostart(text):
    """The autostart block in a .bashrc's text: 'current', 'old' (ours, written differently) or None."""
    m = re.search(re.escape(AUTOSTART_BEGIN) + r'.*?' + re.escape(AUTOSTART_END) + r'\n?', text, re.S)
    if not m:
        return None
    return 'current' if m.group(0).rstrip('\n') == autostart_block().rstrip('\n') else 'old'


def with_autostart(text):
    """The .bashrc text with the autostart block in it once, current."""
    pattern = re.escape(AUTOSTART_BEGIN) + r'.*?' + re.escape(AUTOSTART_END) + r'\n?'
    if re.search(pattern, text, re.S):
        return re.sub(pattern, lambda _: autostart_block(), text, count=1, flags=re.S)
    return text + ('' if not text or text.endswith('\n') else '\n') + '\n' + autostart_block()


# ── nb quick notes (Termux) ───────────────────────────────────────────────────
# nb (npm install -g nb.sh) works from Termux's command line; these give it home-screen buttons.
# nb's own `#!/usr/bin/env bash` needs termux-exec, which a widget-started script lacks, so each
# runs nb with bash directly. nb add reads a piped stdin as the note, hence nb-quick's </dev/null.

NB_NOTE_TEXT = r'''#!/data/data/com.termux/files/usr/bin/sh
# nb-note: a quick nb note from the home screen (Termux:Widget opens it in a terminal).
# Asks for a title, which becomes the filename ("Buy milk" -> buy_milk.md; empty -> a
# timestamp name), then one line of text: type it and the note is saved at once, or
# leave it empty to write the note in nano. nb commits it either way.
# Written by `mobilize init`, which leaves it alone once it's edited.
#
# Started from the widget, it gets a bare environment, where nb's `#!/usr/bin/env bash`
# line can fail ("nb: not found"), so run it with bash directly.
nb() { bash /data/data/com.termux/files/usr/bin/nb "$@"; }
export EDITOR="${EDITOR:-nano}"

printf "Title: "; read -r title
printf "Text (empty = open editor): "; read -r text
set --
[ -n "$title" ] && set -- --title "$title"
if [ -n "$text" ]; then nb add "$@" --content "$text"; else nb add "$@"; fi
printf "\nPress Enter to close. "; read -r _
'''

NB_QUICK_TEXT = r'''#!/data/data/com.termux/files/usr/bin/bash
# nb-quick: a new nb note through Android popups, no terminal (Termux:Widget runs
# ~/.shortcuts/tasks/* in the background). Asks for a title, which becomes the filename
# ("Buy milk" -> buy_milk.md; blank -> a timestamp name), then the note text. nb commits it,
# and a toast says how it went. Cancel either popup and nothing is saved.
# Written by `mobilize init`, which leaves it alone once it's edited.
#
#   nb-quick          ask for title and text
#   nb-quick TEXT     ask for the title only, TEXT is the note (~/bin/termux-url-opener
#                     passes what was shared to Termux)
P=/data/data/com.termux/files/usr/bin
# The widget gives a bare environment, where nb's `#!/usr/bin/env bash` can fail.
nb() { bash "$P/nb" "$@"; }
# termux-dialog prints JSON: {"code": -1 (OK) or -2 (cancel), "text": "..."}
ask() {
    "$P/termux-dialog" text "$@" | "$P/python3" -c "import json, sys
d = json.load(sys.stdin)
if d.get(\"code\") != -1: sys.exit(1)
print(d.get(\"text\", \"\"))"
}

title=$(ask -t "New nb note" -i "Title (blank: timestamp name)") || exit 0
if [ -n "$1" ]; then
    text=$1
else
    text=$(ask -m -t "${title:-New note}" -i "Note") || exit 0
fi
if [ -z "$title$text" ]; then "$P/termux-toast" "nb: empty, nothing saved"; exit 0; fi

args=()
[ -n "$title" ] && args+=(--title "$title")
[ -n "$text" ] && args+=(--content "$text")
if out=$(nb add --no-color "${args[@]}" </dev/null 2>&1); then
    "$P/termux-toast" "nb: ${out#Added: }"
else
    "$P/termux-toast" -b red "nb add failed: $(printf %s "$out" | tail -n 2)"
fi
'''

URL_OPENER_TEXT = r'''#!/data/data/com.termux/files/usr/bin/bash
# Android Share -> Termux runs this with the shared text or URL as $1: save it as an nb
# note (nb-quick asks for a title). Shared URLs become plain notes, not nb bookmarks,
# which would need the network to fetch the page.
# Written by `mobilize init`, which leaves it alone once it's edited.
exec "$HOME/.shortcuts/tasks/nb-quick" "$1"
'''


def nb_scripts():
    """The nb quick-note scripts init writes on Termux: path -> text."""
    home = Path.home()
    return {home / '.shortcuts' / 'nb-note': NB_NOTE_TEXT,
            home / '.shortcuts' / 'tasks' / 'nb-quick': NB_QUICK_TEXT,
            home / 'bin' / 'termux-url-opener': URL_OPENER_TEXT}


# nb's logo (github.com/xwmx/nb, docs/images/nb.png), squared and shrunk to 192px: the widget icon
# for nb-note and nb-quick. The npm package ships no image to copy, as tw-web's logo is copied.
NB_ICON_PNG = (
    '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'
    'NptEDrsav+/o8Sfo6YnYTwEQI6CnBfiLG1ot2rEVtbrD4bdCNyO2XysP3UZmd64taeclHb2Foxq4+mNF+4gWQj8pbqp+mRTO'
    'aiB014+Pj6Mfox9cGMDBBFmxfdzvEwjy9RO0D03sBYybEpsHDSUHAejuHvb3G6GkDxNywhu5Cu2AvKF0MmVXw41Lfo4emXIA'
    'eKFAgE+Os8e+u8VuRmx/6QwKm//3owC00isUqrFZBwHyhtIRvRredzR5LAL8IAA6IxiPxQaQ3a/QkLUfdy1+HgD4LAJYGYBD'
    'AJZHAHCVjm5fdeRl9z93wVNCXqrfp33BBbtfHUAJhjDNSqQQ1mEPCC7YFjywB7DblMw8YBU8wF3whCuLGv7elafxaYC400CS'
    'B8oHY0Ag2M6dvSF01AXTKWsdMpGR/HzcHURPtEBVQ26YghMyAvp0RD6G4mHiyR6m2SjaA3gXVcgqbBR8zJe8H3C+KDbm4zJv'
    '3rgfrEdcbCp6eGJ5DXmoIk3HeSfaIP8SFjC/6cs/xGGZbvLnkPhzHZC857cCtyqTJt9NrrWoLNXXNDeZCXrCy0HAp3XyXEh0'
    'n+J5tN/PxhB+5nsYt+qoylcpmfitjqv4PE3J1eLTN/C3H4SiAk94K2ygFyZ4CQAn9HvfewPSQ52QHlu7ypNMZqXX5a82D5ac'
    'xSyVJD9R0ur307TXa7WSsE6yI7vig1hZenlQu3Zozt5zZ7lq4tVabedV60DyjJMgnDN5nkvHnm1d0leJmUbhbuu1y/oiNJ6f'
    '6Lpt67p5gbYrKSkpKSkpKSkpKSkpKSkpKSkpKSkpKSkpKSkpKSkpKX29/gPPSTanzaYIjgAAAABJRU5ErkJggg=='
)


def nb_icon():
    return base64.b64decode(NB_ICON_PNG)


# ── hledger-web (Termux) ──────────────────────────────────────────────────────
# Termux packages hledger (CLI only) but not hledger-web. mobilize doesn't install it: once it's
# there, natively or in a proot-distro Debian, init gives it a home-screen button. Debian trixie
# has no hledger-web; sid does (`proot-distro install debian`, switch its sources to sid,
# `apt install hledger-web`).

HLEDGER_WEB_PORT = 5050   # tw-web has 5000 (and 5001 over Tailscale), nb-web 5001/5002

HLEDGER_WEB_TEXT = r'''#!/data/data/com.termux/files/usr/bin/sh
# hledger-web: start the server if it isn't running, then open it in the browser.
# Written by `mobilize init`, which leaves it alone once it's edited. Termux:Widget shows it.
#
#   ~/.shortcuts/hledger-web           start if needed, then open it in the browser
#   ~/.shortcuts/hledger-web --start   start if needed, nothing else
#   ~/.shortcuts/hledger-web --stop    stop the server
#
# hledger-web from Termux's PATH if it's there, else from the proot-distro Debian (Termux has
# no package for it; Debian sid does). Termux's home is bound into Debian at the same path, so
# Termux's `hledger` and this share the files, include directives and all.
# Journal: $LEDGER_FILE, else ~/.hledger.journal (created empty if missing: hledger-web won't
# start without one). Port 5050, on 127.0.0.1 only. Output: ~/.cache/hledger-web/server.out
mode=$1
port=5050
journal=${LEDGER_FILE:-$HOME/.hledger.journal}
log=$HOME/.cache/hledger-web/server.out
P=/data/data/com.termux/files/usr/bin

up() { "$P/curl" -s -o /dev/null --max-time 2 http://127.0.0.1:$port/; }

if [ "$mode" = --stop ]; then
    pkill -f "hledger-web --serve --port $port" && echo "hledger-web: stopped" || echo "hledger-web: not running"
    exit 0
fi

"$P/termux-wake-lock" 2>/dev/null
if ! up; then
    mkdir -p "${log%/*}"
    [ -e "$journal" ] || touch "$journal"
    echo "$(date '+%F %T') starting hledger-web on $journal" >> "$log"
    set -- --serve --port $port --host 127.0.0.1 --allow=edit -f "$journal"
    if [ -x "$P/hledger-web" ]; then
        nohup "$P/hledger-web" "$@" >> "$log" 2>&1 &
    else
        nohup "$P/proot-distro" login debian --bind "$HOME:$HOME" -- hledger-web "$@" >> "$log" 2>&1 &
    fi
    [ -t 1 ] && echo "hledger-web: starting on http://localhost:$port/"
    i=0
    while [ $i -lt 60 ] && ! up; do sleep 0.5; i=$((i + 1)); done
fi
[ "$mode" = --start ] && exit 0
"$P/termux-open-url" http://localhost:$port/
'''


def hledger_web_where():
    """Where hledger-web is installed on Termux: 'termux', 'debian' (proot-distro), or None."""
    prefix = Path(os.environ.get('PREFIX', '/data/data/com.termux/files/usr'))
    if (prefix / 'bin' / 'hledger-web').exists():
        return 'termux'
    rootfs = prefix / 'var' / 'lib' / 'proot-distro'
    for d in (rootfs / 'containers' / 'debian' / 'rootfs', rootfs / 'installed-rootfs' / 'debian'):
        if (d / 'usr' / 'bin' / 'hledger-web').exists():
            return 'debian'
    return None


# hledger's logo (hledger.org/images/coins2-248.png, drawn by linuxcaffe), squared and shrunk to 192px.
HLEDGER_ICON_PNG = (
    'iVBORw0KGgoAAAANSUhEUgAAAMAAAADACAMAAABlApw1AAAACXBIWXMAAAqdAAAKnQEmlT32AAACc1BMVEVHcEyGbA51XgwFBAAA'
    'AAACAgB3YAwAAAAAAAADAgAOCwEGBACbfBCDaQ2cfRAJBwBCNQYcFgKnhhFaSQl9ZQwKCABpVAqJbg6AZw0UEAEcFgKpiBF8ZAy1'
    'khJDNgahghAxJwShgRCLcA5yWwuRdA++mBSYeg9sVwtNPge5lBO1khOsihIyKAQNCgCRdQ82KwWWeA8OCwEtJASSdQ8yKARoVAqk'
    'hBFsVwsZFAKyjxKBaA25lRN7YgxjUAqujBI4LQWyjxJUQwg0KgWsihJ8ZAzLoxVWRQgeGAJENgaGbA6Ocw5iTwrWrBbOpRV2XwxS'
    'Qgi9mBNiTwpJOwdVRAjBmxRkUAq9mBOefxAyKAQpIAPPpxUuJQTFnhTFnhTFnhRjTwpOPwdSQgguJQRZRwnWrBfXrRfWrBbWrBfY'
    'rhfarxfZrha3kxO4lBPZrxe8lxO5lRPKoxXOpRW9mBO/mhTXrRayjxK+mRSzkBLPpha0kRK2khPHoBXJohXEnRTYrRbAmhTSqRbC'
    'nBStixLVqxarihKxjhKwjRLcsBfUqha7lxO1khOUdw+ujBKggRDRqBaZexCPcg+1kRONcQ6ighGefxCafBDBmxS2kxO7lhSpiBF/'
    'Zg2LcA7MpBWwjhKJbg6XeQ/GnxW6lhO6ZA+SdQ+6lRN8ZAyVeA95YQy7lhPFnhSBaA2QdA+HbA6lhRGkhBG5lBODaQ11XgybfRCn'
    'WQqFaw1lsglvWQtlUQreshdcSQm6lRSdfhCcfRCnhhGXgC+MeTOfhiyniylRQQjGoR1YVUiCcjdsY0CylCS9myBiXEStkCetjBJ4'
    'aju3lyLAnR+0khNHcEycZECsAAAA0XRSTlMA9OYiCQ7tAQUUJxrm6Pc2o1D5vfQuze3uRmzr5fyY8L355PDr/e+/sfjx8Vpa83D4'
    'Po3bZe/k4WT40v3csPmD6NF6/Mf2gXv92vjz9u3OovPmi5X6oOfUx5n6qvz17fjf8vTs////////////////////////////////'
    '////////////////////////////////////////////////////////////////////////////////////////////////////'
    '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'
    'CkYBVQEAuvmuHWvCA5YAAAAASUVORK5CYII='
)


def hledger_icon():
    return base64.b64decode(HLEDGER_ICON_PNG)


# ── t-quick: the timelog from the home screen (Termux) ────────────────────────
# t (~/.task/scripts/t, from the shared ~/.task) clocks in and out of a timeclock file; t-quick
# gives it one-tap Android popups. Status shows as an ongoing notification while you're in
# (Termux:Widget icons only redraw when the widget does, so the green/red icon lags).

T_QUICK_TEXT = r'''#!/data/data/com.termux/files/usr/bin/bash
# t-quick: clock in and out of the timelog (t, ~/.task/scripts/t) through Android popups, no
# terminal (Termux:Widget runs ~/.shortcuts/tasks/* in the background). One tap, one list:
#   out: your recent accounts (most recent first) to clock in to, or Other... for a new one
#        ("account description", like `t in`)
#   in:  Clock out, Clock out with a note..., Comment..., or another account to switch to
# While you're in, an ongoing notification says so (account, since when) with Out and Menu
# buttons; clocking out removes it. The widget icon turns green (in) or red (out), shown the
# next time the widget redraws. A toast says what t did. Written by `mobilize init`, which
# leaves it alone once it's edited.
#
#   t-quick             the menu
#   t-quick out         clock out (the notification's Out button)
#   t-quick --refresh   bring the notification and icon in line with t (say after `t in` in a shell)
P=/data/data/com.termux/files/usr/bin
export PATH="$P:$PATH"
T=$HOME/.task/scripts/t
SELF=$HOME/.shortcuts/tasks/t-quick
ICONS=$HOME/.shortcuts/icons

# t's own `#!/usr/bin/env bash` can fail in the widget's bare environment; no stdin (t's
# prompts take the default), no colours
t() { "$P/bash" "$T" "$@" </dev/null 2>&1 | sed 's/\x1b\[[0-9;]*m//g'; }
toast() { "$P/termux-toast" "$@"; }
# termux-dialog prints JSON: {"code": -1 (OK) or -2 (cancel), "text": "..."}; a sheet gives
# the chosen item as text
dialog() {
    "$P/termux-dialog" "$@" | "$P/python3" -c "import json, sys
d = json.load(sys.stdin)
if d.get(\"code\") == -2 or not d.get(\"text\"): sys.exit(1)
print(d[\"text\"])"
}

# t's status: "IN  acct  desc" + "    since 18:26:24  (1h20m elapsed)", or "OUT  last: acct  at …"
read_status() {
    local status; status=$(t)
    state=${status%% *}
    account=$(printf '%s\n' "$status" | head -1 | awk '$1 == "IN" {print $2} $1 == "OUT" {print $3}')
    [ "$state" = IN ] || state=OUT   # "No timelog entries": no file yet (t in creates it)
    since=$(printf '%s\n' "$status" | sed -n 2p | awk '{print substr($2, 1, 5)}')   # HH:MM
}
read_status
file=$(t version | sed -n 's/^\(file\|timelog\): *//p' | head -1)
recent() { grep '^i ' "$file" 2>/dev/null | awk '{print $4}' | tac | awk '!seen[$0]++' | head -n 7; }

refresh() {
    read_status
    if [ "$state" = IN ]; then
        "$P/termux-notification" -i t-quick --ongoing --alert-once --priority low --icon timer \
            -t "IN  $account" -c "since $since" \
            --action "$P/bash $SELF" \
            --button1 Out --button1-action "$P/bash $SELF out" \
            --button2 Menu --button2-action "$P/bash $SELF"
        cp -f "$ICONS/.t-quick-in.png" "$ICONS/t-quick.png" 2>/dev/null
    else
        "$P/termux-notification-remove" t-quick
        cp -f "$ICONS/.t-quick-out.png" "$ICONS/t-quick.png" 2>/dev/null
    fi
}

# do_t: run t, toast its first line (red when it failed), then refresh
do_t() {
    local out
    if out=$(t "$@") && ! printf '%s' "$out" | grep -qi '^\(not clocked\|specify\|usage\)'; then
        toast "$(printf '%s\n' "$out" | grep -v '^$' | tail -n 1)"
    else
        toast -b red "t $1: $(printf '%s\n' "$out" | grep -v '^$' | tail -n 1)"
    fi
}

clock_in() {   # "account description..."
    local acct=${1%% *} desc=""
    [ "$acct" != "$1" ] && desc=${1#* }
    [ "$state" = IN ] && t out >/dev/null
    do_t in "$acct" "$desc"
}

case "$1" in
    --refresh) refresh; exit 0 ;;
    out) do_t out; refresh; exit 0 ;;
esac

OTHER="Other account..."
if [ "$state" = IN ]; then
    items="Clock out,Clock out with a note...,Comment..."
    for a in $(recent); do [ "$a" != "$account" ] && items+=",→ $a"; done
    items+=",→ $OTHER"
    pick=$(dialog sheet -t "IN  $account, since $since" -v "$items") || { refresh; exit 0; }
else
    items=""
    for a in $(recent); do items+="${items:+,}$a"; done
    items+="${items:+,}$OTHER"
    pick=$(dialog sheet -t "OUT  (last: ${account:-none})" -v "$items") || { refresh; exit 0; }
fi
pick=${pick#→ }

case "$pick" in
    "Clock out") do_t out ;;
    "Clock out with a note...")
        note=$(dialog text -t "Clock out of $account" -i "Note") || exit 0
        do_t out "$note" ;;
    "Comment...")
        note=$(dialog text -t "Comment ($account)" -i "Comment") || exit 0
        [ -n "$note" ] && do_t comment "$note" ;;
    "$OTHER")
        new=$(dialog text -t "Clock in" -i "account:sub description") || exit 0
        [ -n "$new" ] && clock_in "$new" ;;
    *) clock_in "$pick" ;;
esac
refresh
'''

# t-quick's icons: a white t on green (in) or red (out). t-quick copies one over
# icons/t-quick.png as the state changes; the two masters sit beside it as dotfiles.
T_ICON_IN_PNG = (
    'iVBORw0KGgoAAAANSUhEUgAAAMAAAADACAMAAABlApw1AAAASFBMVEVHcEwunk8unk8unk8unk8unk8unk8unk8unk8unk8unk8u'
    'nk8unk8unk////9ru4L8/v3J59KXz6hNrGlVsHDk8+mv27x2wIy0PzxFAAAADXRSTlMA7hZpjUG9CVEroNPZzf+8xwAABFlJREFU'
    'eNrtndmS4yAMRQGz2lG3O2kn//+nU7PV1HTZiRdJcLt8H/PECQLJAiRjTp06derbKbmSO9/HwVoisnaIve9ycQlg6CX7gZY1+Fya'
    'xXDBW1oj64Nr7p8PnrbJh3ZmIuVIexRzauK/3zf6Pwy156F4Oipf6g0/XIhDl1DHdjriU5egh6+PwD58ZYRMMspKO48lKVmFHcn1'
    'JKnegVqPkh25SPKKDvjvF52E1JOWeokdtZCmCqz5SJmRJ215VvOPpK/ItxCcpRqyDnL58i/leuPnIQhUUwF8/McJao//KEGh+irg'
    '4z9C4KgN7fUHyTYCYHf65EitKILEb7yRXaaWlEE3oP1bUaLWtHEh980B9MALYPsycNSiHKIH2OcNMrWpDG1AG4yobxagh3Rhm92Z'
    'bRjAAq/gtev4WAxxndVDM6I4dv44vs3pgxGgkw3i5AFeTUHXPEAnGkUrADyfgg4A4OkUEAAASSYSVQCeJBsvEAAXwShIBWA5IvIg'
    'AF4uE6EDsLSTBhiAIPYlrAQQxXJZSgDzNpSBALJULkULIEplQ7UA5mwoQAEEofMMNQDPGIje3v9pdvzj9D4v1pB0fzpuftT/ESz8'
    'fuNM0gVBgDcBgMC3BOoAeL58XB0Ay+cF6gB89QQFDqCwBUKVADKbG6sE8GUVD3AAA1tCqBIAsYWitQAS17leLQDHlRGqBVC4Pidr'
    'AWSurHQtgI7ra6YWgJc421b7IqMvp94RECAyOeJqAIPE6bwmgP1WAAQIQCdASwDnIq4NAO8H4D0xfCzkAQE85y2VGgCdxE0/TYAs'
    'cVdUE6BI3DbWBHASDzY0AZIRCIY0Adhyo5UABpFHY4oAXuTGtCJAFrlzrwhQRN6NKQIkkVcDegBW5umnHoCXeTyvBxBkni7pATj2'
    'a9PKAN/wvhD8jS0WTzDNAkzyXoArMzEP8CabkeAMhxYA3rkBstTN3Y/5K2dKt78ZbOi+cGnuoWBBLPvQdelA8noT3oOYbOjJkevH'
    '/fp4MF26TGIvOG4bLl9+cnoxtq+aaf0x936AIveKaWkVswJcJAtSPRQAguBLvqVoiBVA8i3lFhvaC9DJ1oS5jdIAychOwVUYoBMv'
    'yzPJArx6VH98Cj5HSYBXb+pZ4olREOB1nSSGz4LbJAawpj4MR47uPsoArKnswZPnvb1G2AOwrlQY06n34z5xA6yss8VXX+j2uN4/'
    'pmkcmQDWFqlCr/CEX2MLv8oZfJ05/Ep/+LUW4atd4tcbxa/4il9zF7/qMXzdafzK3/i11/Gr3+P3H8DvAIHfgwO/Cwp+Hxr8TkAG'
    'vheTwe+Ghd+PDL8jnIHvyWfwuyIa+L6UBr8zqIHvzSppR0rdcQ1+f2ID3yGaHaGr0+0du0v67x0Ju0/9L0sKR7xzDMnUV8r7GGJu'
    'YfR/52GrLfnQzuj/BhnBr/PR1gdnGlUq2T971Tj4XJJpXsmV3Pk+DvbnlFg7xN53uTiAoZ86dUpbPwBjISdxzqGdhQAAAABJRU5E'
    'rkJggg=='
)
T_ICON_OUT_PNG = (
    'iVBORw0KGgoAAAANSUhEUgAAAMAAAADACAMAAABlApw1AAAATlBMVEVHcEzIOC7IOC7IOC7IOC7IOC7IOC7IOC7IOC7IOC7IOC7I'
    'OC7IOC7IOC7////Zcmv//f3xy8jSWlLlnZj66+rcfXbrtbH12tjPTkXhk40pb230AAAADXRSTlMAvWkmNu4WTgmNoNPZCr8a0AAA'
    'BExJREFUeNrtnQmOpDAMRQkkcQLlLrr2uf9FR7NppBZUUWA7+S3+CfKIg53FdtPs2rVr17dTip7a3LshBGYOYXB9bsnHBDD0jvLA'
    '8xoyddViRMqBlyhkitV9eZ/5PWVfz0wkcrxGjhLw6Cth6DJvVe7KDZ8OLKEDlbGdluXUJujh2yOID98YgVhHRmvBB9ZS8AYBQ8+a'
    '6iOo9RjZUXSsLxeBP7/qJKSerdRr/FE7tlQHaz5aZpTZWlnU/B3by8kthBi4hEKEXL7yS9lzOXnw8UsQlB3/doLS499KUH782wg6'
    'rkHr/0WR69Baf5BCJQBhpU92XIscSPwmG9kR1yQC/QGt/xUlrk1vLuS+OoAeeAG8vwwi16iI6AHWeQPiOkXQBvSGEfXVAvQwe4BN'
    'e4NQMUAAXsFL1/G2GOI8qatlRLHt/nH8mNKnIECrG8TpA7yagrZ6gFY1ijYAeD4FLQDA0ylgAADW9AEmAE98wQEC4KC4kTcBmN/g'
    'ZxCArHcSYQMw9yclGABS2wkbATi1sywjgGkbIiAA0jpLsQJwWqehVgBTNuShALzSfYYZQBYMRB/H/5oGuB+nJRqSrj+OO36s1Uny'
    'kI7AAEhuCZQByHLncWUAgpwXKAPw1RN0cACdWCBUCIDE3FghgC+reIADGMQOhAoBsFgoWgogSd3rlQKIUrF0KQAvtZ0sBUBSp9Kl'
    'AFqp3UwpgKxxt222I+Mvt94OEMAJOeJiAIPG7bwlQPhWAAwIwDtATQD7Ii4NAO8H4D0xfCyUAQGy5CuVEgCtxks/SwDSeCtqCeA1'
    'XhtbAkSNhA1LgNQoBEOWAGJno4UABpWkMUOArPJi2hCAVNLGDAE6lbwxQ4CkkjVgBxB0Uj/tALJO3oMdAOmkLtkBRPFn08YA3/C9'
    'kOKLrbtyLC3oCS6TABd9LyB1MjEN8KF7IiH5I71PAxzVf6JSNvQ5Of7R6PW3gA3dpmdgvBpYkIgNnWfuI8fzQ92CRGzoyZXr/fbj'
    'ehV6dJnUMjge8wDjKHZLnBWLYVwsrrk7vSymuVUsCnDQzCW+GgCQYibfXDQkCqCZS/mODa0FaHVrwjxGbYDU6E7BWRmgVS/Lc9EF'
    'eJVUv30KTqMmwKucepF4YlQEeF0nSSCkO13UAJbUh5E4ozuPOgBLKnvI7O4ft1EDYFndRaFb7+vtIg2wsM6WXH2h0/V8+7xfxlEI'
    'YGmRKvQKT/g1tvCrnMHXmcOv9IdfaxG+2iV+vVH8iq/4NXfxqx7D153Gr/yNX3sdv/o9fv8B/A4Q+D048Lug4Pehwe8E1MD3Ymrw'
    'u2Hh9yPD7wjXwPfka/C7IjbwfSkb/M6gDXxvVk07susUjd6fuIHvEC2O0Jbp9o7dJf1PgITdp/63JdEW7+woNeW1lqGO0f9l8O/a'
    'Uvb1jP5fkEF5mY8OmWJTqVJH+VlW45CpS031StFTm3s3hF9TEsLg+tySjwBD37Vrl7V+As8VJ7bSs16TAAAAAElFTkSuQmCC'
)


# ── init ──────────────────────────────────────────────────────────────────────

class Init:
    """Each step looks first and does only what's missing. A step returns False to stop."""

    def __init__(self, data=None, station=None, platform=None, web=True, dry=False, yes=False, out=print):
        self.data, self.station, self.platform = data, station, platform
        self.web, self.dry, self.yes, self.out = web, dry, yes, out
        self.worst = OK
        self.shared = []      # tracked files in the repo this run changed: committed together at the end

    def note(self, status, step, detail):
        self.out(f'  {status:5} {step:<11} {detail}')
        if status == FAIL or (status in (WARN, YOU) and self.worst != FAIL):
            self.worst = FAIL if status == FAIL else WARN

    def act(self, step, detail, fn):
        """Do it, or say it would be done (--dry-run)."""
        if self.dry:
            self.note(WOULD, step, detail)
            return True
        fn()
        self.note(DONE, step, detail)
        return True

    def interactive(self):
        """May init stop and ask? Never under TW_NOINTERACT (a client with no terminal: CONVENTIONS §1)."""
        return sys.stdin.isatty() and not self.yes and not os.environ.get('TW_NOINTERACT')

    # The steps, in order

    def step_identity(self):
        rc = read_rc(local_rc())
        self.station = self.station or rc.get('gittw.device')
        self.platform = self.platform or rc.get('tw.platforms') or ('mobile' if is_termux() else 'desktop')
        if not self.station:
            host = socket.gethostname().split('.')[0]
            if host and host != 'localhost':
                self.station = host
        if not self.station:
            self.note(FAIL, 'station', "no name for this station: give --station (the hostname here is 'localhost')")
            return False
        if self.platform not in PLATFORMS:
            self.note(FAIL, 'station', f"--platform must be one of {', '.join(PLATFORMS)}")
            return False
        self.note(OK, 'station', f'{self.station} ({self.platform}{", Termux" if is_termux() else ""})')
        return True

    def step_packages(self):
        if not is_termux():
            missing = [c for c in ('git', 'python3') if not shutil.which(c)]
            if missing:
                self.note(FAIL, 'packages', f"missing: {', '.join(missing)} (install them with your package manager)")
                return False
            self.note(OK, 'packages', 'git, python3')
            return True
        missing = [p for p, cmd in TERMUX_PACKAGES if not shutil.which(cmd)]
        if not missing:
            self.note(OK, 'packages', ', '.join(p for p, _ in TERMUX_PACKAGES))
            return True
        def install():
            r = subprocess.run(['pkg', 'install', '-y', *missing])
            if r.returncode:
                raise SystemExit(f"mobilize: pkg install {' '.join(missing)} failed. If Termux said no mirror is "
                                 "selected, or CANNOT LINK EXECUTABLE: termux-change-repo, then pkg upgrade, "
                                 "then init again")
        return self.act('packages', f"pkg install {' '.join(missing)}", install)

    def step_task(self):
        v = task_version()
        if v and v.startswith('2.6.'):
            self.note(OK, 'task', v)
            return True
        if v:
            self.note(WARN, 'task', f'Taskwarrior {v}: not 2.6.x, see the warning below. Carrying on.')
            self.out(TW3_WARNING.format(v=v))
            return True
        if not is_termux():
            self.note(FAIL, 'task', 'Taskwarrior is not installed: install 2.6.x from your distribution')
            return False
        s = settings()
        url = s.get('mobilize.task.deb') or TASK_DEB_URL
        if not s.get('mobilize.task.deb') and platform.machine() not in ('aarch64', 'arm64'):
            self.note(FAIL, 'task', f'the task2 deb is built for aarch64; this device is {platform.machine()}. '
                                    'Build Taskwarrior 2.6.2 for it, or name your own deb in mobilize.rc '
                                    '(mobilize.task.deb, mobilize.task.sha256)')
            return False
        want = s.get('mobilize.task.sha256') or TASK_DEB_SHA256
        def install():
            cache = Path.home() / '.cache' / 'mobilize'
            cache.mkdir(parents=True, exist_ok=True)
            deb = cache / url.rsplit('/', 1)[-1]
            with urllib.request.urlopen(url, timeout=120) as r:
                data = r.read()
            got = hashlib.sha256(data).hexdigest()
            if got != want:
                raise SystemExit(f'mobilize: {url} has sha256 {got}, expected {want}: not installing it')
            deb.write_bytes(data)
            # apt installs the deb's dependencies too (libgnutls, libuuid…); dpkg -i would just fail
            argv = ['apt', 'install', '-y', str(deb)] if shutil.which('apt') else ['dpkg', '-i', str(deb)]
            r = subprocess.run(argv)
            if r.returncode or not (task_version() or '').startswith('2.6.'):
                raise SystemExit(f"mobilize: {' '.join(argv[:2])} failed, or task still is not 2.6.x")
        return self.act('task', f'install Taskwarrior 2.6.2 ({url.rsplit("/", 1)[-1]}, sha256 checked)', install)

    def _data_url(self):
        return fetch_url() if (task_dir() / '.git').exists() else self.data

    def step_key(self):
        url = self._data_url()
        if not url or not is_ssh_url(url):
            self.note(SKIP, 'ssh key', 'the data repo is not reached over SSH')
            return True
        key = Path.home() / '.ssh' / 'id_ed25519'
        if key.exists():
            self.note(OK, 'ssh key', str(key))
            return True
        def make():
            key.parent.mkdir(mode=0o700, exist_ok=True)
            argv = ['ssh-keygen', '-t', 'ed25519', '-C', self.station, '-f', str(key)]
            if not self.interactive():
                argv += ['-N', '']
            if subprocess.run(argv).returncode:
                raise SystemExit('mobilize: ssh-keygen failed')
        return self.act('ssh key', f'make {key} (ed25519, comment {self.station})', make)

    def _wait_for_access(self, step, url):
        """Can we read `url`? If the key isn't on that host yet, say how to add it and wait."""
        while True:
            ok, err = reachable(url)
            if ok:
                self.note(OK, step, f'{ssh_host(url)} accepts this station')
                return True
            if 'Permission denied' not in err and 'publickey' not in err:
                self.note(FAIL, step, f'{url}: {err.splitlines()[-1] if err else "unreachable"}')
                return False
            pub = (Path.home() / '.ssh' / 'id_ed25519.pub')
            self.note(YOU, step, f'{ssh_host(url)} does not know this station\'s key yet. Add it:')
            self.out(f'          {pub.read_text().strip() if pub.exists() else "(no public key)"}')
            self.out(f'          GitHub, from a station with gh:  gh ssh-key add - --title {self.station} <<< "<the key>"')
            self.out('          Codeberg: Settings → SSH / GPG Keys (web)')
            if os.environ.get('TW_NOINTERACT'):
                # A client with no terminal (tw-web): ask it to rerun this in one (CONVENTIONS §2)
                print(json.dumps({'type': 'tw_needs_pty', 'cmd': os.environ.get('TW_PREFLIGHT_CMD', ''),
                                  'reason': f'mobilize init waits for the key to be added on {ssh_host(url)}'}),
                      file=sys.stderr)
                return False
            if not self.interactive():
                return False
            try:
                input('          Press Enter once it is added (Ctrl-C to stop) ')
            except (EOFError, KeyboardInterrupt):
                self.out('')
                return False

    def step_clone(self):
        d = task_dir()
        if (d / '.git').exists():
            self.note(OK, 'clone', f'{d} is a git repo (origin {fetch_url() or "none"})')
            return True
        if not self.data:
            self.note(FAIL, 'clone', f'{d} is not cloned yet: give --data <your gittw fetch URL>')
            return False
        if is_ssh_url(self.data) and not self.dry and not self._wait_for_access('access', self.data):
            return False
        def clone():
            if d.exists():
                if any(d.iterdir()):
                    self.note(DONE, 'clone', f'moved the old {d} aside to {move_aside(d)}')
                else:
                    d.rmdir()
            r = run(['git', 'clone', self.data, str(d)], env=SSH_ENV)
            if r.returncode:
                raise SystemExit(f'mobilize: git clone failed: {r.stderr.strip()}')
        return self.act('clone', f'git clone {self.data} {d}', clone)

    def step_push(self):
        want = _list(settings().get('mobilize.data.push'))
        if not want:
            self.note(SKIP, 'push', 'mobilize.rc names no push URLs: origin pushes to where it fetches from')
            return True
        # With no pushurl set, git reports the fetch URL; the first --add --push replaces it.
        explicit = git('config', '--get-all', 'remote.origin.pushurl').stdout.split()
        effective = explicit or [fetch_url()]
        missing = [] if set(want) <= set(effective) else [u for u in want if u not in explicit]
        if not missing:
            self.note(OK, 'push', ', '.join(ssh_host(u) for u in want))
        else:
            def add():
                for u in missing:
                    git('remote', 'set-url', '--add', '--push', 'origin', u)
            self.act('push', f"origin also pushes to {', '.join(missing)}", add)
        if not self.dry:
            for u in want:
                if is_ssh_url(u) and u != fetch_url():
                    ok, err = reachable(u)
                    if not ok:
                        self.note(WARN, 'push', f'{ssh_host(u)} refuses this station: add its key there')
        return True

    def step_git_identity(self):
        """This station's commits need a name and email (gittw sync, the auto-commit hook): without
        one, sync stops and the hook fails silently. Set for the ~/.task clone only."""
        if git('var', 'GIT_AUTHOR_IDENT').returncode == 0:
            self.note(OK, 'git name', git('var', 'GIT_AUTHOR_IDENT').stdout.rsplit('>', 1)[0] + '>')
            return True
        if self.dry:
            self.note(WOULD, 'git name', 'ask for a name and email for commits from this station')
            return True
        if not self.interactive():
            self.note(YOU, 'git name', f'no name/email for commits here: git -C {task_dir()} config user.name '
                                       "'Your Name'; git -C {task_dir()} config user.email you@example.com")
            return True
        try:
            name = input('          Your name, for commits from this station: ').strip()
            email = input('          Your email: ').strip()
        except (EOFError, KeyboardInterrupt):
            self.out('')
            name = email = ''
        if name and email:
            git('config', 'user.name', name)
            git('config', 'user.email', email)
            self.note(DONE, 'git name', f'{name} <{email}> (for {task_dir()} only)')
        else:
            self.note(YOU, 'git name', 'skipped: gittw sync and the auto-commit hook will fail until it is set')
        return True

    def step_merge_driver(self):
        """gittw's merge driver for the task data is configured per clone (.git/config)."""
        g = task_dir() / 'scripts' / 'gittw'
        if not g.exists():
            self.note(SKIP, 'merge', 'no gittw in scripts/ (tw -I gittw)')
            return True
        if git('config', 'merge.twdata.driver').returncode == 0:
            self.note(OK, 'merge', 'gittw merges the task data by UUID')
            return True
        attrs = task_dir() / '.gitattributes'
        before = attrs.read_text() if attrs.exists() else None
        def setup():
            r = run([sys.executable or 'python3', str(g), 'setup'], env={'TW_TASK_DIR': str(task_dir())})
            if r.returncode:
                raise SystemExit(f'mobilize: gittw setup failed: {(r.stderr or r.stdout).strip()}')
        self.act('merge', 'gittw setup (merge the task data by UUID)', setup)
        if not self.dry and (attrs.read_text() if attrs.exists() else None) != before:
            self.shared.append(attrs)
        return True

    def step_taskrc(self):
        link, shared = taskrc_link(), task_dir() / '.taskrc'
        if not shared.exists():
            self.note(WARN, '.taskrc', f'{shared} is not in the repo: on the station where ~/.taskrc is a '
                                       'real file, `gittw init` offers to move it in')
            return True
        if link.is_symlink() and link.resolve() == shared.resolve():
            self.note(OK, '.taskrc', f'{link} → {os.readlink(link)}')
            return True
        def make():
            if link.exists() or link.is_symlink():
                self.note(DONE, '.taskrc', f'moved the old {link} aside to {move_aside(link)}')
            link.symlink_to(os.path.relpath(shared, link.parent))
        return self.act('.taskrc', f'{link} → {os.path.relpath(shared, link.parent)}', make)

    def step_local_rc(self):
        path = local_rc()
        if path.exists():
            name = read_rc(path).get('gittw.device')
            if name and name != self.station:
                self.note(WARN, 'local.rc', f'says this station is {name}, not {self.station}: left as it is')
            elif not name:
                self.note(WARN, 'local.rc', 'has no gittw.device line')
            else:
                self.note(OK, 'local.rc', f'{path}')
        else:
            self.act('local.rc', f'write {path} (gittw.device = {self.station}, tw.platforms = {self.platform}'
                                 f'{", hooks = off" if self.platform == "mobile" else ""})',
                     lambda: path.write_text(local_rc_text(self.station, self.platform)))
        shared = task_dir() / '.taskrc'
        if shared.exists() and not any(t and t.resolve() == path.resolve() or raw.endswith('local.rc')
                                       for f, n, raw, t in includes(shared) if f.resolve() == shared.resolve()):
            self.act('local.rc', f'{shared} includes it, last',
                     lambda: append(shared, '\n# This station only (untracked; must exist, even empty): last, '
                                            'so it wins\ninclude ~/.task/local.rc\n'))
            self.shared.append(shared)
        return True

    def step_state_files(self):
        """Includes of files gittw doesn't track (rewritten by hooks): create them empty."""
        created, broken = [], []
        for f, n, raw, target in includes(taskrc_link()):
            if target:
                continue
            want = _expected_path(raw, f)
            (created if ignored(want) else broken).append((f, n, raw, want))
        for f, n, raw, want in created:
            self.act('state', f'{want} (gitignored, included by {f.name}:{n}): created empty',
                     lambda w=want: (w.parent.mkdir(parents=True, exist_ok=True), w.touch()))
        for f, n, raw, want in broken:
            self.note(FAIL, 'includes', f'{f}:{n} includes {raw}, which is missing and tracked: '
                                        'the repo is missing a file')
        if not created and not broken:
            self.note(OK, 'includes', 'every include resolves')
        return not broken or self.dry

    def step_hooks(self):
        """A station that chose its hooks gets its hooks.d folder back (it's gitignored, so a clone lacks it)."""
        if not _list(read_rc(local_rc()).get('mobilize.hooks')):
            self.note(SKIP, 'hooks', 'this station runs every hook (or none: hooks = off); choose with mobilize hooks --set')
            return True
        if self.dry:
            self.note(WOULD, 'hooks', 'rebuild the hooks.d folder from mobilize.hooks')
            return True
        lines = []
        hooks(out=lines.append)
        self.note(OK, 'hooks', lines[0] if lines else '')
        return True

    def step_path(self):
        scripts = task_dir() / 'scripts'
        bashrc = Path.home() / '.bashrc'
        shown = '$HOME/.task/scripts' if scripts == Path.home() / '.task' / 'scripts' else str(scripts)
        try:
            text = bashrc.read_text()
        except OSError:
            text = ''
        if shown in text or str(scripts) in text:
            self.note(OK, 'PATH', f'{bashrc.name} puts {shown} on PATH')
            return True
        return self.act('PATH', f'{bashrc} puts {shown} on PATH (new shells; now: source ~/.bashrc)',
                        lambda: append(bashrc, f'\n# Taskwarrior fleet scripts (tw, gittw, mobilize…)\n'
                                               f'export PATH="{shown}:$PATH"\n'))

    def step_mobilize_rc(self):
        path = mobilize_rc()
        if not path.exists():
            pushes = push_urls() or ([fetch_url()] if fetch_url() else [])
            self.act('mobilize.rc', f'write {path} (push URLs from origin, station {self.station})',
                     lambda: (path.parent.mkdir(parents=True, exist_ok=True),
                              path.write_text(mobilize_rc_text(pushes, self.station, self.platform))))
            self.shared.append(path)
        elif self.station not in stations(read_rc(path)):
            self.act('mobilize.rc', f'add station {self.station} ({self.platform})',
                     lambda: append(path, station_block(self.station, self.platform)))
            self.shared.append(path)
        else:
            self.note(OK, 'mobilize.rc', f'knows {self.station}')
        inc = task_dir() / 'config' / 'included.rc'
        host = inc if inc.exists() else task_dir() / '.taskrc'
        if host.exists() and not any(raw.endswith('mobilize.rc') for f, n, raw, t in includes(host)):
            self.act('mobilize.rc', f'{host.name} includes it',
                     lambda: append(host, 'include ~/.task/config/mobilize.rc\n'))
            self.shared.append(host)
        return True

    def step_commit(self):
        """Shared files change together or not at all: an include committed without its file would
        stop task on every other station. Committed here, never pushed (gittw sync pushes)."""
        if not self.shared:
            return True
        rels = sorted({str(p.resolve().relative_to(task_dir().resolve())) for p in self.shared})
        def commit():
            git('add', '--', *rels)
            r = git('commit', '-q', '-m', f'mobilize init: station {self.station} ({self.platform})',
                    '-m', 'Changed: ' + ', '.join(rels), '--', *rels)
            if r.returncode:
                raise SystemExit(f'mobilize: git commit failed: {(r.stderr or r.stdout).strip()}')
        return self.act('commit', f"{', '.join(rels)} (not pushed: gittw sync does that)", commit)

    def step_web(self):
        if not self.web:
            self.note(SKIP, 'tw-web', '--no-web')
            return True
        s = settings()
        d, repo = web_dir(s), s.get('mobilize.web.repo') or WEB_REPO
        if (d / 'app.py').exists():
            self.note(OK, 'tw-web', f'{d}')
        elif d.exists() and any(d.iterdir()):
            self.note(FAIL, 'tw-web', f'{d} exists and is not tw-web')
            return False
        else:
            def clone():
                d.parent.mkdir(parents=True, exist_ok=True)
                r = run(['git', 'clone', repo, str(d)])
                if r.returncode:
                    raise SystemExit(f'mobilize: git clone {repo} failed: {r.stderr.strip()}')
            self.act('tw-web', f'git clone {repo} {d}', clone)
        if self.dry and not (d / 'app.py').exists():
            self.note(WOULD, 'tw-web', 'install its Python packages')
            return True
        py = web_python(d)
        if run([py, '-c', f'import {WEB_MODULES}']).returncode == 0:
            self.note(OK, 'tw-web', f'its Python packages import ({py})')
            return True
        def install():
            nonlocal py
            if not is_termux() and run([py, '-m', 'pip', 'install', '--dry-run', '-q', 'pip']).returncode:
                venv = d / '.venv'
                if run([sys.executable or 'python3', '-m', 'venv', str(venv)]).returncode:
                    raise SystemExit(f'mobilize: python3 -m venv {venv} failed (Debian/Ubuntu: apt install python3-venv)')
                py = str(venv / 'bin' / 'python')
            r = subprocess.run([py, '-m', 'pip', 'install', '-q', '-r', str(d / 'requirements.txt')])
            if r.returncode:
                hint = ' (Termux: pkg install clang, then run init again)' if is_termux() else ''
                raise SystemExit(f'mobilize: pip install failed{hint}')
        return self.act('tw-web', f'pip install -r {d / "requirements.txt"}', install)

    def step_launcher(self):
        if not self.web or not is_termux():
            self.note(SKIP, 'launcher', 'Termux only' if self.web else '--no-web')
            return True
        d = web_dir()
        path = Path.home() / '.shortcuts' / 'tw-web'
        text = launcher_text(d, web_python(d))
        def write():
            # a new file renamed over the old: a running `sh … --loop` reads its script as it goes,
            # and must keep reading the copy it started with (0.6.3)
            path.parent.mkdir(exist_ok=True)
            new = path.with_name(path.name + '.new')
            new.write_text(text)
            new.chmod(0o755)
            os.replace(new, path)
        if path.exists():
            have = path.read_text()
            if have == text:
                self.note(OK, 'launcher', f'{path}')
            elif is_old_launcher(have, d):
                self.act('launcher', f'{path}: the newer launcher', write)
            else:
                self.note(WARN, 'launcher', f'{path} (edited by hand: left as it is)')
            return True
        self.act('launcher', f'{path} (Termux:Widget, or run it from a shell)', write)
        self.note(YOU, 'launcher', 'for a home-screen button and the Android settings: mobilize android')
        return True

    def step_icon(self):
        """The widget's icon: tw-web's menu logo, where Termux:Widget looks (icons/<script>.png)."""
        if not self.web or not is_termux():
            self.note(SKIP, 'icon', 'Termux only' if self.web else '--no-web')
            return True
        icon, logo = Path.home() / '.shortcuts' / 'icons' / 'tw-web.png', web_dir() / 'tw-logo.png'
        if icon.exists():
            self.note(OK, 'icon', f'{icon}')
            return True
        if not logo.exists():
            self.note(SKIP, 'icon', f'no {logo} to use')
            return True
        self.act('icon', f"{icon} (tw-web's logo; re-add the widget to see it)",
                 lambda: (icon.parent.mkdir(parents=True, exist_ok=True), shutil.copyfile(logo, icon)))
        return True

    def step_autostart(self):
        if not self.web or not is_termux():
            self.note(SKIP, 'autostart', 'Termux only' if self.web else '--no-web')
            return True
        if not autostart_wanted():
            self.note(SKIP, 'autostart', 'mobilize.web.autostart = no')
            return True
        bashrc = Path.home() / '.bashrc'
        try:
            text = bashrc.read_text()
        except OSError:
            text = ''
        state = bashrc_autostart(text)
        if state == 'current':
            self.note(OK, 'autostart', f'{bashrc.name} starts tw-web when Termux opens')
            return True
        return self.act('autostart', f'{bashrc}: start tw-web when Termux opens'
                                     + (' (block rewritten)' if state else ''),
                        lambda: bashrc.write_text(with_autostart(text)))

    def step_nb(self):
        """nb's quick-note buttons, once nb is installed: nb-note (a terminal), nb-quick
        (Android popups, no terminal) and Share -> Termux."""
        if not is_termux():
            self.note(SKIP, 'nb', 'Termux only')
            return True
        if not shutil.which('nb'):
            self.note(SKIP, 'nb', 'nb not installed (npm install -g nb.sh, then init again)')
            return True
        for path, text in nb_scripts().items():
            if path.exists():
                if path.read_text(errors='replace') == text:
                    self.note(OK, 'nb', f'{path}')
                else:
                    self.note(WARN, 'nb', f'{path} (not the one mobilize writes: left as it is)')
                continue
            def write(path=path, text=text):
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(text)
                path.chmod(0o755)
            self.act('nb', f'{path}', write)
        for name in ('nb-note', 'nb-quick'):
            icon = Path.home() / '.shortcuts' / 'icons' / f'{name}.png'
            if icon.exists():
                self.note(OK, 'nb', f'{icon}')
                continue
            self.act('nb', f"{icon} (nb's logo; re-add the widget to see it)",
                     lambda icon=icon: (icon.parent.mkdir(parents=True, exist_ok=True), icon.write_bytes(nb_icon())))
        if not shutil.which('termux-dialog'):
            self.note(YOU, 'nb', "nb-quick's popups need Termux:API: pkg install termux-api")
        return True

    def step_hledger_web(self):
        """hledger-web's home-screen button, once hledger-web is installed (mobilize doesn't
        install it: see HLEDGER_WEB_TEXT)."""
        if not is_termux():
            self.note(SKIP, 'hledger-web', 'Termux only')
            return True
        where = hledger_web_where()
        if not where:
            self.note(SKIP, 'hledger-web', 'not installed (in Termux, or in proot-distro Debian sid)')
            return True
        path = Path.home() / '.shortcuts' / 'hledger-web'
        if path.exists():
            if path.read_text(errors='replace') == HLEDGER_WEB_TEXT:
                self.note(OK, 'hledger-web', f'{path}')
            else:
                self.note(WARN, 'hledger-web', f'{path} (not the one mobilize writes: left as it is)')
        else:
            def write():
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(HLEDGER_WEB_TEXT)
                path.chmod(0o755)
            self.act('hledger-web', f'{path} (port {HLEDGER_WEB_PORT}, hledger-web from {where})', write)
        icon = Path.home() / '.shortcuts' / 'icons' / 'hledger-web.png'
        if icon.exists():
            self.note(OK, 'hledger-web', f'{icon}')
        else:
            self.act('hledger-web', f"{icon} (hledger's logo; re-add the widget to see it)",
                     lambda: (icon.parent.mkdir(parents=True, exist_ok=True), icon.write_bytes(hledger_icon())))
        return True

    def step_t_quick(self):
        """t-quick: clock in/out of t's timelog through Android popups, once t is in ~/.task/scripts."""
        if not is_termux():
            self.note(SKIP, 't-quick', 'Termux only')
            return True
        if not (Path.home() / '.task' / 'scripts' / 't').exists():
            self.note(SKIP, 't-quick', 'no ~/.task/scripts/t')
            return True
        path = Path.home() / '.shortcuts' / 'tasks' / 't-quick'
        if path.exists():
            if path.read_text(errors='replace') == T_QUICK_TEXT:
                self.note(OK, 't-quick', f'{path}')
            else:
                self.note(WARN, 't-quick', f'{path} (not the one mobilize writes: left as it is)')
        else:
            def write():
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(T_QUICK_TEXT)
                path.chmod(0o755)
            self.act('t-quick', f'{path}', write)
        icons = Path.home() / '.shortcuts' / 'icons'
        for name, data in (('.t-quick-in.png', T_ICON_IN_PNG), ('.t-quick-out.png', T_ICON_OUT_PNG),
                           ('t-quick.png', T_ICON_OUT_PNG)):
            icon = icons / name
            if icon.exists():
                self.note(OK, 't-quick', f'{icon}')
                continue
            self.act('t-quick', f'{icon}' + (" (re-add the widget to see it)" if name == 't-quick.png' else ''),
                     lambda icon=icon, data=data: (icon.parent.mkdir(parents=True, exist_ok=True),
                                                   icon.write_bytes(base64.b64decode(data))))
        if not shutil.which('termux-dialog'):
            self.note(YOU, 't-quick', "its popups need Termux:API: pkg install termux-api")
        return True

    def run(self):
        steps = [self.step_identity, self.step_packages, self.step_task, self.step_key, self.step_clone,
                 self.step_push, self.step_git_identity, self.step_merge_driver, self.step_taskrc, self.step_local_rc, self.step_state_files, self.step_hooks, self.step_path,
                 self.step_mobilize_rc, self.step_commit, self.step_web, self.step_launcher,
                 self.step_icon, self.step_autostart, self.step_nb, self.step_hledger_web, self.step_t_quick]
        self.out(f'mobilize init{" (dry run: nothing changes)" if self.dry else ""}')
        for step in steps:
            try:
                if not step():
                    self.out('mobilize: stopped. Fix that, then run `mobilize init` again: '
                             'it picks up where it left off.')
                    return 2
            except SystemExit as e:
                self.note(FAIL, step.__name__[5:], str(e).replace('mobilize: ', ''))
                return 2
        if self.dry:
            return 0
        self.out('')
        code = check(out=self.out)
        if is_termux():
            self.out("\nAndroid's own settings (background running, the home-screen button, "
                     '"Display over other apps"): mobilize android')
        if self.web and is_termux() and not web_up():
            self.out('Start tw-web: ~/.shortcuts/tw-web (or the Termux:Widget button)')
        return max(code, {OK: 0, WARN: 1, FAIL: 2}[self.worst])


# ── check ─────────────────────────────────────────────────────────────────────

def _hook_tools(path):
    """Desktop tools a hook calls by name, that this station lacks."""
    try:
        text = path.read_text(errors='replace')
    except OSError:
        return []
    return [t for t in ('nb', 'hledger', 'notify-send', 'xdg-open')
            if re.search(r"['\"]%s['\"]" % re.escape(t), text) and not shutil.which(t)]


def sshd_settings():
    """sshd's effective settings (sshd -T), lower-case keys; {} when there's no sshd or it can't say."""
    if not shutil.which('sshd'):
        return {}
    try:
        r = run(['sshd', '-T'], timeout=10)
    except (OSError, subprocess.SubprocessError):
        return {}
    pairs = (line.split(None, 1) for line in r.stdout.splitlines() if ' ' in line)
    return {k.lower(): v.strip() for k, v in pairs} if r.returncode == 0 else {}


def sshd_clash(prefix=None):
    """termux-services runs sshd (enabled: no `down` file) while another sshd, started by hand,
    holds the port: the service then fails every second. Returns the hand-started pids."""
    svc = Path(prefix or os.environ.get('PREFIX', '')) / 'var' / 'service' / 'sshd'
    if not svc.is_dir() or (svc / 'down').exists():
        return []
    try:
        supervised = (svc / 'supervise' / 'pid').read_text().strip()
    except OSError:
        supervised = ''
    r = run(['pgrep', '-x', 'sshd'])
    return [pid for pid in r.stdout.split() if pid != supervised] if r.returncode == 0 else []


def checks():
    """(name, status, detail) for this station. Read-only."""
    out = []
    add = lambda name, status, detail: out.append((name, status, detail))
    d = task_dir()

    v = task_version()
    if v and v.startswith('2.6.'):
        add('task', OK, v)
    elif v:
        add('task', WARN, f'{v}: NOT 2.6.x. Untested with the fleet; use at your own risk')
    else:
        add('task', FAIL, 'not installed')

    if not (d / '.git').exists():
        add('repo', FAIL, f'{d} is not a git repo: mobilize init --data <URL>')
        return out
    fetch, pushes = fetch_url(), push_urls()
    want = _list(read_rc(mobilize_rc()).get('mobilize.data.push'))
    if want and sorted(set(want)) != sorted(set(pushes)):
        add('push', WARN, f"origin pushes to {', '.join(pushes) or 'nothing'}; mobilize.rc says {', '.join(want)}")
    elif fetch and fetch not in pushes and pushes:
        add('push', WARN, f'origin fetches from {ssh_host(fetch)} but does not push there: the other stations '
                          'may never see this one\'s work')
    else:
        add('push', OK, ', '.join(ssh_host(u) for u in pushes) or 'origin')

    link, shared = taskrc_link(), d / '.taskrc'
    if link.is_symlink() and shared.exists() and link.resolve() == shared.resolve():
        add('.taskrc', OK, f'{link} → {os.readlink(link)}')
    elif not link.exists():
        add('.taskrc', FAIL, f'{link} is missing')
    else:
        add('.taskrc', WARN, f'{link} is not a link to {shared}: its settings are this station\'s alone')

    if git('var', 'GIT_AUTHOR_IDENT').returncode:
        add('git name', WARN, "no name/email for commits here: gittw sync and the auto-commit hook fail "
                              f"(git -C {d} config user.name …; user.email …)")
    if (d / 'scripts' / 'gittw').exists() and git('config', 'merge.twdata.driver').returncode:
        add('merge', WARN, 'gittw merge driver not configured in this clone: gittw setup')

    rc = read_rc(local_rc())
    name = rc.get('gittw.device')
    if not local_rc().exists():
        add('local.rc', FAIL, f'{local_rc()} is missing (task will not start if .taskrc includes it)')
    elif not name:
        add('local.rc', WARN, 'no gittw.device: this station has no name')
    else:
        add('local.rc', OK, f"{name} ({rc.get('tw.platforms', 'no tw.platforms')})")

    bad = [(f, n, raw) for f, n, raw, t in includes(link) if not t]
    for f, n, raw in bad:
        add('includes', FAIL, f'{f}:{n} includes {raw}, which is missing (task will not start)')
    if not bad:
        add('includes', OK, 'every include resolves')
    home = str(Path.home())
    foreign = [(f, n, raw) for f, n, raw, t in includes(link)
               if raw.startswith('/') and not raw.startswith(home) and not raw.startswith('/usr/')]
    for f, n, raw in foreign:
        add('includes', WARN, f'{f}:{n} names another machine\'s home: {raw} (write ~/.task/… instead)')
    own = [(f, n, raw) for f, n, raw, t in includes(link) if raw.startswith(home + '/') and ignored(f) is False
           and str(f).startswith(str(task_dir()))]
    for f, n, raw in own:
        add('includes', WARN, f'{f}:{n}: {raw} only works on this station; the other stations need '
                              f'~{raw[len(home):]} (installers write absolute paths: put it back)')

    links = [p for sub in ('scripts', 'hooks') for p in sorted((d / sub).glob('*'))
             if p.is_symlink() and not p.exists()]
    for p in links:
        add('links', WARN, f'{p.relative_to(d)} → {os.readlink(p)}, which is not on this station')
    if not links:
        add('links', OK, 'no dangling symlinks in scripts/ or hooks/')
    gitlinks = [line.split('\t', 1)[1] for line in git('ls-files', '-s').stdout.splitlines()
                if line.startswith('160000 ')]
    if gitlinks and not (d / '.gitmodules').exists():
        add('repo', WARN, f"{', '.join(gitlinks)}: recorded as a submodule with no .gitmodules, so a clone gets it empty")

    if str(d / 'scripts') not in os.environ.get('PATH', '').split(os.pathsep):
        add('PATH', WARN, f'{d / "scripts"} is not on PATH here (tw, gittw, mobilize)')
    else:
        add('PATH', OK, f'{d / "scripts"}')

    st = stations()
    if not mobilize_rc().exists():
        add('mobilize.rc', WARN, f'{mobilize_rc()} is missing: mobilize init writes it')
    elif name and name not in st:
        add('mobilize.rc', WARN, f'does not list this station ({name}): mobilize init adds it')
    else:
        add('mobilize.rc', OK, f"{len(st)} station{'s' if len(st) != 1 else ''}: {', '.join(sorted(st))}")

    s = settings()
    hooks_on = s.get('hooks', 'on').lower() not in ('off', 'no', '0', 'false')
    hdir = Path(s.get('hooks.location') or d / 'hooks').expanduser()
    lacking = [(p.name, _hook_tools(p)) for p in sorted(hdir.glob('on-*')) if os.access(p, os.X_OK)]
    lacking = [(n, t) for n, t in lacking if t]
    for n, tools in lacking:
        add('hooks', WARN if hooks_on else OK, f"{n} calls {', '.join(tools)}, not on this station"
            + ('' if hooks_on else ' (hooks are off)'))
    if not lacking:
        add('hooks', OK, f"{'on' if hooks_on else 'off'}; none calls a missing tool")
    apps = _list(s.get('mobilize.hooks'))
    if apps and name:
        want = {f.name for f in hook_files(apps)}
        have = {p.name for p in station_hooks_dir(name).glob('on-*') if p.exists()}
        if want != have:
            add('hooks', WARN, f"{station_hooks_dir(name)} is out of step with mobilize.hooks "
                               f"({', '.join(apps)}): run `mobilize hooks`")
        else:
            add('hooks', OK, f"{len(want)} chosen: {', '.join(apps)}")

    r = run(['task', 'rc.hooks=off', 'rc.context=none', '_show']) if v else None
    if r and r.returncode == 0:
        empty = sorted(m.group(1) for m in re.finditer(r'^context\.([^.=]+)(?:\.read)?=\s*$', r.stdout, re.M))
        if empty:
            add('contexts', WARN, f"empty here: {', '.join(empty)} (hooks fill some of them)")

    wd = web_dir(s)
    if (wd / 'app.py').exists():
        add('tw-web', OK, f"{wd}{', running' if web_up() else ', not running'}")
        if is_termux():
            launcher = Path.home() / '.shortcuts' / 'tw-web'
            if not launcher.exists():
                add('launcher', WARN, f'no {launcher}: mobilize init writes it')
            elif is_old_launcher(launcher.read_text(), wd):
                add('launcher', WARN, f'{launcher} is an older one: mobilize init replaces it')
            env = server_environ() if web_up() else None
            if env is not None and not env.get('LD_PRELOAD'):
                add('tw-web', WARN, 'the running server has no LD_PRELOAD (termux-exec): every hook fails '
                                    '"No such file or directory". Stop it (Settings → Stop server) and start '
                                    'it again with the launcher from mobilize 0.6.0 or later')
            try:
                state = bashrc_autostart((Path.home() / '.bashrc').read_text())
            except OSError:
                state = None
            if autostart_wanted(s) and state != 'current':
                add('autostart', WARN, ('~/.bashrc has an older autostart block' if state else
                                        'opening Termux won\'t start tw-web if Android stopped it') + ': mobilize init')
    else:
        add('tw-web', OK if not is_termux() else WARN, f'not installed at {wd}')

    if is_termux():
        conf = sshd_settings()
        if conf:
            pw = [k for k in ('passwordauthentication', 'kbdinteractiveauthentication') if conf.get(k) == 'yes']
            if pw:
                add('sshd', WARN, f"accepts passwords ({', '.join(pw)} yes): make it keys only "
                                  '(README: "sshd on a phone")')
            else:
                add('sshd', OK, 'keys only')
        hand = sshd_clash()
        if hand:
            add('sshd', WARN, f"started by hand (pid {', '.join(hand)}) while termux-services also runs it: "
                              'the service fails on port 8022. Use one: `sv-disable sshd`, or kill the hand-started one')

    dirty = [line[3:] for line in git('status', '--porcelain').stdout.splitlines()
             if line[3:].endswith('.data')]
    if dirty:
        add('data', OK, f"uncommitted changes to {', '.join(dirty)}")
    return out


# ── the hook pre-test ─────────────────────────────────────────────────────────

PRETEST_SLOW_READ_MS = 300      # a hook adding more than this to every read is slow on this station
PRETEST_TIMEOUT = 20
PRETEST_NOISE_MS = 40           # less than this is run-to-run noise
PRETEST_LINKED = ('scripts', 'awesome-taskwarrior', 'docs', 'themes', 'templates')   # read-only: linked, not copied


def _sandbox(root):
    """A throwaway home holding a copy of this station's ~/.task (no data, no git), and the
    environment that keeps every hook inside it: HOME, TW_TASK_DIR, TASKRC, TASKDATA, and the
    places hooks write outside ~/.task (LEDGER_FILE, NB_DIR, TW_SPOOL_DIR)."""
    real, home = task_dir(), root / 'home'
    t = home / '.task'
    skip = shutil.ignore_patterns('.git', '*.data', 'logs', 'time', '__pycache__', *PRETEST_LINKED)
    shutil.copytree(real, t, symlinks=True, ignore=skip)
    for name in PRETEST_LINKED:
        if (real / name).exists():
            (t / name).symlink_to(real / name)
    for name in ('logs', 'time'):
        (t / name).mkdir(exist_ok=True)
    # Absolute paths to the real home, in the copied settings, point into the sandbox instead
    real_home = str(Path.home()) + '/'
    for f in [t / '.taskrc', t / 'local.rc', *(t / 'config').glob('*.rc')]:
        if f.is_file() and not f.is_symlink():
            text = f.read_text(errors='replace')
            if real_home in text:
                f.write_text(text.replace(real_home, str(home) + '/'))
    (home / '.taskrc').symlink_to(t / '.taskrc')
    env = dict(os.environ, HOME=str(home), TW_TASK_DIR=str(t), TASKRC=str(t / '.taskrc'),
               LEDGER_FILE=str(root / 'sandbox.journal'), NB_DIR=str(root / 'nb'),
               TW_SPOOL_DIR=str(root / 'spool'), TW_NOINTERACT='1', TW_CLIENT='mobilize-pretest',
               PYTHONDONTWRITEBYTECODE='1',   # scripts/ is linked, not copied: no __pycache__ in the real one
               GIT_CEILING_DIRECTORIES=str(root))
    return t, env


def _pretest_run(hooks_dir, data, env):
    """The sequence, in a fresh database: (ms per command, first error or '', all ok?)."""
    data.mkdir(parents=True)
    env = dict(env, TASKDATA=str(data))
    base = ['task', 'rc.confirmation=no', 'rc.gc=on', 'rc.recurrence.confirmation=no']
    base += ([f'rc.hooks.location={hooks_dir}', 'rc.hooks=on'] if hooks_dir else ['rc.hooks=off'])
    steps = [('add', ['add', 'project:pretest', '+mtest', 'due:tomorrow', '--', 'mobilize hook pre-test']),
             ('modify', ['1', 'modify', '+mtest2', 'priority:M']),
             ('annotate', ['1', 'annotate', '--', 'checked by mobilize']),
             ('done', ['1', 'done']),
             ('list', ['list'])]
    times, error = {}, ''
    for name, args in steps:
        t0 = time.monotonic()
        try:
            r = subprocess.run(base + args, env=env, capture_output=True, text=True,
                               stdin=subprocess.DEVNULL, timeout=PRETEST_TIMEOUT)
        except subprocess.TimeoutExpired:
            return times, f'{name}: no answer within {PRETEST_TIMEOUT} s (waiting for input?)', False
        times[name] = (time.monotonic() - t0) * 1000
        text = r.stdout + r.stderr
        bad = [l for l in text.splitlines()
               if re.search(r'Hook Error|Traceback|Error:|error:|No such file', l)]
        if not error and (bad or (r.returncode and name != 'list')):
            error = f"{name}: {(bad or text.strip().splitlines() or ['exit %d' % r.returncode])[-1].strip()}"
    r = subprocess.run(['task', 'rc.hooks=off', 'status:completed', 'count'], env=dict(env, TASKDATA=str(data)),
                       capture_output=True, text=True)
    if not error and r.stdout.strip() != '1':
        error = f'the test task did not end up completed ({r.stdout.strip() or r.stderr.strip()})'
    return times, error, not error


def hook_pretest():
    """[(hook, status, added ms on a read, added ms in all, note)], the last row for all together."""
    s = settings()
    hdir = Path(s.get('hooks.location') or task_dir() / 'hooks').expanduser()
    names = sorted(p.name for p in hdir.glob('on-*') if p.is_file() and os.access(p, os.X_OK))
    off = sorted(p.name for p in hdir.glob('on-*') if p.is_file() and not os.access(p, os.X_OK))
    rows = []
    with tempfile.TemporaryDirectory(prefix='mobilize-pretest-') as tmp:
        root = Path(tmp)
        t, env = _sandbox(root)
        assert str(t).startswith(tmp) and t != task_dir()
        try:    # the station's folder inside the sandbox copy (hooks.d links are relative, so they resolve there)
            sand_hooks = t / hdir.relative_to(task_dir())
        except ValueError:
            sand_hooks = hdir
        # The baseline: no hooks, fastest of three runs (the first warms the caches)
        base = {}
        for n in range(3):
            times, err, ok = _pretest_run(None, root / f'data-baseline-{n}', env)
            if not ok:
                return [('(no hooks)', FAIL, 0, 0, f'task itself fails in the sandbox: {err}')]
            base = {k: min(v, base.get(k, v)) for k, v in times.items()}
        def row(label, hooks_dir, data):
            times, err, ok = _pretest_run(hooks_dir, data, env)
            read = max(0.0, times.get('list', 0) - base['list']) if 'list' in times else 0
            total = max(0.0, sum(times.get(k, 0) - base[k] for k in base if k in times))
            status = FAIL if not ok else WARN if read > PRETEST_SLOW_READ_MS else OK
            note = err or (f'slow: +{read:.0f} ms on every read' if status == WARN else '')
            return (label, status, read, total, note)
        for i, name in enumerate(names):
            one = root / f'one-{i}'
            one.mkdir()
            (one / name).symlink_to(sand_hooks / name)
            rows.append(row(name, one, root / f'data-{i}'))
        if names:
            rows.append(row('(all together)', sand_hooks, root / 'data-all'))
    rows += [(name, SKIP, 0, 0, 'not executable: off') for name in off]
    return rows


def pretest(as_json=False, out=print):
    rows = hook_pretest()
    if as_json:
        out(json.dumps([{'hook': h, 'status': st, 'read_ms': round(r), 'total_ms': round(t), 'note': n}
                        for h, st, r, t, n in rows], indent=2))
    else:
        station = read_rc(local_rc()).get('gittw.device', 'this station')
        out(f'mobilize check --hooks  ({station}; added time vs. no hooks: on a read, and over '
            'add+modify+annotate+done+list)')
        for h, st, r, t, n in rows:
            fmt = lambda ms, w: (f'+{ms:.0f} ms' if ms >= PRETEST_NOISE_MS else '~0').rjust(w)
            times = f'{fmt(r, 9)} {fmt(t, 10)}' if st != SKIP else ' ' * 20
            out(f'  {st:5} {h:<36} {times}  {n}')
    worst = {r[1] for r in rows}
    return 2 if FAIL in worst else 1 if WARN in worst else 0


# ── android ───────────────────────────────────────────────────────────────────
# What mobilize can't do from inside Termux: Android's own settings. Termux may not read
# them (app ops, battery lists, overlay: all need permissions only adb has), so `mobilize
# android` walks through them and opens each settings screen with `am start`.

def getprop(name):
    try:
        r = run(['getprop', name], timeout=5)
    except (OSError, subprocess.SubprocessError):
        return ''
    return r.stdout.strip() if r.returncode == 0 else ''


def android_info():
    return {'maker': getprop('ro.product.manufacturer'), 'model': getprop('ro.product.model'),
            'release': getprop('ro.build.version.release'), 'emui': getprop('ro.build.version.emui')}


def android_packages():
    """Installed package names, or None when pm can't list them (Android 11+ may hide some)."""
    try:
        r = run(['pm', 'list', 'packages'], timeout=20)
    except (OSError, subprocess.SubprocessError):
        return None
    if r.returncode != 0:
        return None
    return {line.split(':', 1)[1].strip() for line in r.stdout.splitlines() if line.startswith('package:')}


def _major(release):
    m = re.match(r'(\d+)', release or '')
    return int(m.group(1)) if m else 0


APP_DETAILS = ['-a', 'android.settings.APPLICATION_DETAILS_SETTINGS', '-d', 'package:com.termux']
HUAWEI_APP_LAUNCH = ['-n', 'com.huawei.systemmanager/.startupmgr.ui.StartupNormalAppListActivity']
OVERLAY = ['-a', 'android.settings.action.MANAGE_OVERLAY_PERMISSION', '-d', 'package:com.termux']


def android_steps(info, packages, up):
    """The walk: (key, title, lines, screens to try in order, done?) for this phone. done is
    True/False when mobilize can tell, None when it can't (most of Android's settings)."""
    maker = (info.get('maker') or '').lower()
    huawei = maker in ('huawei', 'honor') or bool(info.get('emui'))
    have = (lambda name: None) if packages is None else (lambda name: name in packages)
    steps = []
    if huawei:
        steps.append(('background', 'Let Termux run in the background (Huawei: App launch)', [
            'Leave "Manage all automatically" ON: that switch is for every app.',
            "Switch off Termux's OWN toggle in the list: a \"Manage manually\" dialog appears.",
            'Turn on Auto-launch, Secondary launch and Run in background, then OK.',
            'Do the same for Termux:Widget and Termux:API if they are listed.'],
            [HUAWEI_APP_LAUNCH, APP_DETAILS], None))
    else:
        lines = ['Battery: Unrestricted (not "Optimized").']
        if maker == 'samsung':
            lines.append('Samsung: keep Termux off "Sleeping apps" and "Deep sleeping apps" '
                         '(Settings → Battery → Background usage limits).')
        lines.append('Other makers have their own killers: dontkillmyapp.com')
        steps.append(('background', 'Let Termux run in the background', lines, [APP_DETAILS], None))
    if _major(info.get('release')) >= 12:
        steps.append(('phantom', 'Android 12+: the phantom process killer', [
            'It stops processes Termux started, tw-web among them.',
            'Android 14+: Developer options → Disable child process restrictions.',
            'Android 12–13: from a computer with adb:',
            '  adb shell device_config put activity_manager max_phantom_processes 2147483647'], [], None))
    steps.append(('power', 'Power saving mode: off while you rely on tw-web', [
        "Battery saver stops background apps and cuts their network, whatever the per-app settings say.",
        'Quick settings (pull down from the top), or Settings → Battery. Turning it back on is fine when',
        "you don't need tw-web: opening Termux or tapping the widget brings it back."], [], None))
    steps.append(('recents', 'Lock Termux in recent apps', [
        'Open the recent-apps view: the □ button; with gestures, swipe up from the bottom edge',
        'and pause; with the fingerprint-sensor key, swipe across the sensor.',
        "Drag Termux's card (its black terminal screen) down until a padlock shows."], [], None))
    widget = have('com.termux.widget')
    steps.append(('widget', 'Add the tw-web button to the home screen', [
        'Install Termux:Widget from F-Droid (same source as Termux).' if widget is False else
        'Termux:Widget is installed.',
        'Long-press the home screen → Widgets → Termux:Widget → Termux shortcut → tw-web.'], [], None))
    steps.append(('overlay', 'Allow "Display over other apps" for Termux', [
        'A widget tap opens Termux from the background; Android 10+ needs this for that.',
        'It lets Termux show its window, nothing more. Some phones call it "Floating windows".'],
        [OVERLAY], None))
    steps.append(('try', 'Tap the tw-web button', [
        'It starts tw-web and opens the browser; Termux then says "wake lock held".',
        'Each start, death and restart is logged in ~/.cache/tw-web/server.out.'], [], up))
    return steps


def open_screen(screens):
    """Open the first settings screen that exists. am's exit code is 0 even when the activity
    is missing, so its output decides."""
    for extra in screens:
        try:
            r = run(['am', 'start', *extra], timeout=15)
        except (OSError, subprocess.SubprocessError):
            continue
        said = r.stdout + r.stderr
        if r.returncode == 0 and 'Error' not in said and 'Exception' not in said:
            return True
    return False


def android(list_only=False, out=print, ask=input):
    if not is_termux():
        out('mobilize android: for a phone, in Termux. Nothing to do here.')
        return 0
    info = android_info()
    steps = android_steps(info, android_packages(), web_up())
    out(f"mobilize android: {info['maker'] or 'Android'} {info['model']}, Android {info['release'] or '?'}"
        + (f" ({info['emui']})" if info['emui'] else ''))
    out("Android's own settings, which Termux can't change or read: you set them, mobilize opens")
    out('each screen. Menu names differ between phones; README: "The Android side, step by step".')
    interactive = not list_only and sys.stdin.isatty() and not os.environ.get('TW_NOINTERACT')
    if not list_only and os.environ.get('TW_NOINTERACT'):
        print(json.dumps({'type': 'tw_needs_pty', 'cmd': os.environ.get('TW_PREFLIGHT_CMD', ''),
                          'reason': 'mobilize android opens settings screens and waits for you'}), file=sys.stderr)
    for n, (key, title, lines, screens, done) in enumerate(steps, 1):
        mark = {True: ' [done]', False: ' [not yet]', None: ''}[done]
        out(f'\n{n}. {title}{mark}')
        for line in lines:
            out(f'   {line}')
        if not interactive or done:
            continue
        try:
            if screens:
                answer = ask('   Open the settings screen? [Y/n/q] ').strip().lower()
                if answer == 'q':
                    return 0
                if answer in ('', 'y', 'yes'):
                    if not open_screen(screens):
                        out("   Couldn't open it here: find it in Settings (the README has routes).")
            if ask('   Enter when done (q to stop) ').strip().lower() == 'q':
                return 0
        except (EOFError, KeyboardInterrupt):
            out('')
            return 0
    out('\nAll there is. Run it again any time: `mobilize android` (`--list` just prints).')
    return 0


def check(as_json=False, out=print):
    rows = checks()
    if as_json:
        out(json.dumps([{'check': n, 'status': s, 'detail': d} for n, s, d in rows], indent=2))
    else:
        out('mobilize check')
        for n, s, d in rows:
            out(f'  {s:5} {n:<11} {d}')
    worst = {s for _, s, _ in rows}
    return 2 if FAIL in worst else 1 if WARN in worst else 0


def edit(which='shared', out=print):
    """Open mobilize.rc (or local.rc) in the user's editor. The editor setting is split, not
    run through a shell, so EDITOR='code --wait' works and nothing else is interpreted."""
    path = local_rc() if which == 'local' else mobilize_rc()
    if not path.exists():
        out(f'{path} does not exist yet: run `mobilize init` first')
        return 2
    editor = os.environ.get('VISUAL') or os.environ.get('EDITOR') or ('nano' if shutil.which('nano') else 'vi')
    code = subprocess.run([*shlex.split(editor), str(path)]).returncode
    if code == 0 and which != 'local':
        out(f'{path.name} is shared: commit it (gittw sync) so the other stations see the change')
    return code


def hook_files(apps, hdir=None):
    """The hook files in ~/.task/hooks belonging to apps (names like need-priority, or file names)."""
    hdir = hdir or task_dir() / 'hooks'
    out = []
    for p in sorted(hdir.glob('on-*')):
        for app in apps:
            if p.name == app or re.match(r'^on-(add|modify|exit|launch)[_-]%s\.[a-z]+$' % re.escape(app), p.name):
                out.append(p)
                break
    return out


def station_hooks_dir(station):
    return task_dir() / 'hooks.d' / station


def set_local(updates, comment=None):
    """Set keys in local.rc: replace a key's line where it is, else append (with a comment once)."""
    path = local_rc()
    lines = path.read_text().splitlines() if path.exists() else []
    left = dict(updates)
    out = []
    for line in lines:
        key = line.split('#', 1)[0].split('=', 1)[0].strip()
        if '=' in line.split('#', 1)[0] and key in left:
            if left[key] is not None:
                out.append(f'{key} = {left[key]}')
            left.pop(key)
            continue
        out.append(line)
    new = [f'{k} = {v}' for k, v in left.items() if v is not None]
    if new:
        out += ([''] + [f'# {comment}'] if comment else ['']) + new
    path.write_text('\n'.join(out) + '\n')


def hooks(set_=None, all_=False, out=print):
    rc = read_rc(local_rc())
    station = rc.get('gittw.device')
    if not station:
        out(f'{local_rc()} names no station (gittw.device): run `mobilize init` first')
        return 2
    folder = station_hooks_dir(station)
    shown = '~/' + str(folder.relative_to(Path.home())) if str(folder).startswith(str(Path.home())) else str(folder)
    if all_:
        set_local({'mobilize.hooks': None, 'hooks.location': None})
        if folder.exists():
            shutil.rmtree(folder)
        out(f'{station} runs every hook in {task_dir() / "hooks"} again (hooks.location removed from local.rc)')
        return 0
    if set_ is not None:
        apps = _list(set_)
        set_local({'mobilize.hooks': ', '.join(apps), 'hooks.location': shown, 'hooks': 'on'},
                  comment='Which hooks this station runs: `mobilize hooks --set …` writes these three lines')
        rc = read_rc(local_rc())
    apps = _list(rc.get('mobilize.hooks'))
    if not apps:
        out(f'{station} runs every hook in {task_dir() / "hooks"} (no mobilize.hooks in local.rc). '
            'Choose some: mobilize hooks --set need-priority,gittw')
        return 0
    files = hook_files(apps)
    found = {a for a in apps for f in files if f.name == a or re.search(r'[_-]%s\.' % re.escape(a), f.name)}
    folder.mkdir(parents=True, exist_ok=True)
    want = {f.name for f in files}
    for p in folder.iterdir():
        if p.is_symlink() and p.name not in want:
            p.unlink()
    for f in files:
        link = folder / f.name
        target = os.path.relpath(f, folder)
        if not (link.is_symlink() and os.readlink(link) == target):
            if link.is_symlink() or link.exists():
                link.unlink()
            link.symlink_to(target)
    out(f'{station} runs {len(files)} hook{"s" if len(files) != 1 else ""} from {shown}:')
    for f in files:
        out(f'  {f.name}{"" if os.access(f, os.X_OK) else "  (not executable: off)"}')
    missing = [a for a in apps if a not in found]
    for a in missing:
        out(f'  warn: no hook in {task_dir() / "hooks"} belongs to {a!r}')
    if not ignored(folder / 'x'):
        out(f'  warn: {task_dir() / "hooks.d"} is not in .gitignore: add `hooks.d/` there (one station\'s choice)')
    if read_rc(local_rc()).get('hooks', 'on').lower() in ('off', 'no', '0', 'false'):
        out('  note: hooks = off in local.rc, so none of them runs yet')
    return 1 if missing else 0


def show_stations(out=print):
    st = stations()
    if not st:
        out(f'No stations in {mobilize_rc()}')
        return 1
    me = read_rc(local_rc()).get('gittw.device')
    for name in sorted(st):
        info = st[name]
        extra = ', '.join(f'{k} {v}' for k, v in sorted(info.items()) if k != 'platform')
        out(f"{'*' if name == me else ' '} {name:<12} {info.get('platform', '?'):<8} {extra}")
    return 0


# ── command line ──────────────────────────────────────────────────────────────

def main(argv=None):
    p = argparse.ArgumentParser(prog='mobilize', description=__doc__.split('\n\n')[0].strip(),
                                epilog=__doc__.split('\n\n', 1)[1],
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument('--version', action='version', version=f'mobilize {__version__}')
    p.add_argument('-e', '--edit', nargs='?', const='shared', choices=('shared', 'local'), metavar='local',
                   help='edit mobilize.rc (shared), or `-e local` for this station\'s local.rc')
    sub = p.add_subparsers(dest='cmd')
    i = sub.add_parser('init', help='set up this station (safe to run again)')
    i.add_argument('--data', help="your gittw repo's fetch URL (when ~/.task isn't cloned yet)")
    i.add_argument('--station', help='this station\'s name (default: local.rc, else the hostname)')
    i.add_argument('--platform', choices=PLATFORMS, help='default: mobile in Termux, else desktop')
    i.add_argument('--no-web', dest='web', action='store_false', help='leave tw-web out')
    i.add_argument('--dry-run', action='store_true', help='say what would change; change nothing')
    i.add_argument('--yes', action='store_true', help='never ask (an SSH key gets no passphrase)')
    c = sub.add_parser('check', help='is this station still set up? (read-only)')
    c.add_argument('--json', action='store_true')
    c.add_argument('--hooks', action='store_true', help='the hook pre-test (a sandbox; takes a while)')
    sub.add_parser('stations', help='the stations mobilize.rc knows about')
    h = sub.add_parser('hooks', help='which hooks this station runs')
    g = h.add_mutually_exclusive_group()
    g.add_argument('--set', metavar='APPS', help='comma-separated app names (need-priority,gittw,…)')
    g.add_argument('--all', action='store_true', help='every hook again')
    d = sub.add_parser('android', help="walk through Android's settings for Termux (opens each screen)")
    d.add_argument('--list', action='store_true', help='just print the steps')
    a = p.parse_args(argv)
    if a.edit:
        return edit(a.edit)
    if a.cmd == 'init':
        return Init(a.data, a.station, a.platform, a.web, a.dry_run, a.yes).run()
    if a.cmd == 'stations':
        return show_stations()
    if a.cmd == 'hooks':
        return hooks(a.set, a.all)
    if a.cmd == 'android':
        return android(a.list)
    if a.cmd == 'check':
        return pretest(a.json) if a.hooks else check(a.json)
    p.print_help()
    return 0


if __name__ == '__main__':
    sys.exit(main())
