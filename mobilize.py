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

__version__ = '0.7.1'

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
    'iVBORw0KGgoAAAANSUhEUgAAAMAAAADACAMAAABlApw1AAAAwFBMVEX////+///9///8//7+/v79/v78/v78/v38/f30+/rd'
    '8vDF5uWl4tah2tWWz9Bx1r1uwL4vzZ8vwaAkzZoky5oczZckyZoeyZgjxpojwZocwpcjvZojvJoju5ojuZojt5sht5oXuZZB'
    'rqoiqJwhtJohspohqpsgr5ogqJsWspUWqZYfopoYo5cen5oenpoenZsUnpYjlZ0em5selJsdlZsRl5UXkJgcjZsTjZceiZwc'
    'ipsbh5sRiZcbhJsRg5YTfphzFOhAAAALm0lEQVR42u2cCXfavBKG5RUvYBuyEFzKYkgTh5KwJqQk7f//V3e02TKbZe6X28t3'
    '9J7T0zY4MI9HI41GYxBSUlJSUlJSUlJSUlJSUlJSUlJSUlJSUlJSUlJSUlJSUlL6h6Xp9o507ZLst6R/+H96/xHy/B155MeX'
    'IR15yWCYpulQUCf0LsUHmuYkaa/X+47V7X4DdUDDpqGZFwFQQ37aHwx6jKHLGYY+vHQZAHUCMMgIGED9QgB0FGKAwWBnGA1D'
    'ZFyIBxoEoId9cJkAPvPAhQJYyG0N+yA2ghjB5QBAEPj9lGt4gQDYB/VGSJUM+Ex6QQDikltPu8wFlwSA9FqN5KCOUU/5YnZR'
    'AOKixsNYAfxNAEygAP4qwPdLBuj9KwC+XzIAdYEC+FsAvX8BQE8B/PcyTcuydMsyzwLABIcANFM3QJb5tQUX07H1/DNt2/xH'
    'AExdeFdcTbXOoLCc3SKso+1ZT7J716Ny8H/ETy4vsTCCIoBO/83e1iVviwyjYvnxYK2v8B6ajT/FD6Jk8ryYPT9Pkij0Xfh8'
    'Sx6AlYhEABs+xPEaYbPZiUF3zWYY1snNsau4wUReEBUV+AUCbL4fjdbr9eLlZUo0W8+SyKMvSQMMCgAWrv4GzXgIphPdttvt'
    'OG43Q5/Byd7/xm+uj0yRnpUwTRgpfkKMJ3p+nk4noOlsBAgylc68SjfIALBT/bAzjO86nc4d1+3t9W37pt2sG9KFeB2579t3'
    '0BvTEmv+0UBONsDA/NcFEQeYUobZqC7vAVohYgBwU7xwOOxQCQCg6+ubm2bdQXKnIQ444P0X0Vuu5SZid8BGTgR3f5EDMAIM'
    'MJrMInSOB2oINTrc/D2C2+srQPAIpgRAwAF+iQAJH/5eslksViKB4ILJaBaV18sZQB4DNnJDwfx9gGuM0EAydWACsN0DeE0c'
    'iG4YqI3V63y1WnGEHRcQgtI5IwcY9DBADXmtYYceGOwQ3HKCq6urm9CVmCREAIHgdQ4AYH+wWS1XGOCYC4AgKLtRGQC4AAAc'
    '5H+L+YnHcRcAAQwjoxzAPwiAPQBwmyWxn3uAmF8EmDz+9EtmjByAeAD53W+k0vtt3/6CCwhB2WRkmm7yG89ChSAmHgD7P5j9'
    '3AVrrNlsOhEAJj8TpGmlAOMMwB90u/mhEygm52ewBIDlBRcAgVtKYEGYbvkasBUATNTI7V9h41dJQta55Hn9PMkJRrPG6bEq'
    'AqSB2+oKp2bxsNOCBTgM4E/zDta0ggsgDsonU1M4Dg0+siHkIv91xQEWq80qaXhsaYAFNFlPRBfgiJcDGLRaeakaZtKo7rn8'
    'Otfzw2ZcAACC8kVZ8JH/O/eAO39dUoDlYjMPPLJ+apBN4/dzo9lEcMHpkzsKMGZRPOAlos6wFXp0NbXtmk3tdOsMISOol6/J'
    'GktHHTsHSFD0QRZlsP/1Fac9msMzXc2CEROAD3gYw1SqyQGQMydaYIm7xHzbygaJqddg6XKDu7YAcNV0NdmjZUf0QLRZUs03'
    'c38vd9Z1FOUEk9HJWGMAnGBA6ysteFdjb601cY7UbF8XBtEZAG9vmyVLizYQDvupv67pySyzf1bnqZMEAM6qYWvp4LtwKDuD'
    'dRoTZC648eWS3h2AJc3r5pAVHTQOLp5NxOX4JMDTDgBeDo6GjUEIrgUXaGcA0MR0/hEdSfpNDeUugKXgxIdwAD4RUftPZGqY'
    '4KrgAuMsACDASZ11zKw8jk9PpBnAmOcTia6fzDQN5MdXYhTo8gCFrBpnFPrRD/GyMTQZnVrzdwEGXa/MohoKb7IxdHUl2eNC'
    'AcSkdOkdD04TOcmUEeB8yJAGSOulQ8Kig4iPoUDOBbsAy4/g1OSCcBBMJaahHAAT9IcJ2Q+XuaAR52HclGuU2gXYJMapKo2G'
    'orMAUpmOG0sDF+RhLDeGGMCvbFt2egKma9lUDuB+zAjAAY4pcTt1MQpu5Jp0dgA+ktPYNpmGpnxXY58GuOcAEAEy61JN89tZ'
    'FEguBWRzk21slh/+6QhwUGPNsmp5gPGg5UoNB5gjmu1sJm2ezncPAkAEnN6nCACTdTkAJeinkeS6WhhDchNpBkAIfpdMQecC'
    'NCQzG/gtYR6SWoyLAG9lqw0HmFYDkM0LdOTlKd2NFHYBYJOUzb1VAe5pEMt3jrrNHCCs5gEg+B2VMWcA0woA4wQhudIz3D2I'
    'Yl6fOAOgXhICBGD6XAHgvkoMY4IwG0Ny86gA8Ks8BASAaSUAXR7g5jqrcckkE0KR6+3X3C1zNQV4rgBwDwChNACZR3mZsSrA'
    '+0dpDFcEeMgAZE8pDRTG13x733SlAbYZAJIAoHXGrweQWslEgN9R6S9UBCAEVQGyYjvkElpVAFsC4KUiwH2FINYZwC0BkI8B'
    'QiAPQM/MvgIAT6O80vu1AM9yAA8MQP4UmAHgUzP5dWBLCCoAvFQCeGgZ1VZidmYmvxJ/IQAmGN/L5kImLm/xYntFgG1FgOcK'
    'AFWy0Tg774BsVA7gDwXYVgF4kQF4eGAETw3JZ1DwfoADXLX9rwR4qQKQJvI7MgBgZ9+3sjuyygCLqgD3fVe2UNhs81O/dtOV'
    '3BOfCTCVBCAuaMhWJWJ+bHndlq5KnAfwIg3wQMeQXF0o5gev17F0XejP+2dFgEVVgPtU5sDC0txWfnJ850n2Tfh/tpUBFpUA'
    'qAvK+8pqKIizs+92U/qAAwA+vxoAR4FROoC4AzBAHMitHWcA8P6DSgB4IjJKzwdiof3AqwTwWRFgUQmArgWupp+23x/e8Q6K'
    '27jCCc3/BgDnpKfuqYG81l0nB5B9AhYDfH4RwI8fhTEEG7PjVRvcTxRnPSw4hDWtEsBnRYBFJQBGELlHGk91g9nPCLADDFQN'
    '4FMWYMPboCQACi4AAnxSv39WibsYfGY/BYhlTupNh0oEYD9yrKNXO4EAoJOfGMWrTQs3RTuGAMD2luP+OA1xn4qtZy3TpmWT'
    'Xom40Ah1WxLwBJv/gwN8Yg/ki+KRq7MhBACNbARoB66szzIAVmCBnU0/7bNuFU03bIMOE6/RKnQz3sZ1mTzUZf1CUQ6Q8B4i'
    '9+jVfpQDRAevdlknUoQ9gAmekoCXiMbj/iAdJ4HYL1QPW4Vuxo7MANJMFLz/YWL2Qxh/8m7eeYDEvni4ev66oXrNewFnpBdt'
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

    def run(self):
        steps = [self.step_identity, self.step_packages, self.step_task, self.step_key, self.step_clone,
                 self.step_push, self.step_git_identity, self.step_merge_driver, self.step_taskrc, self.step_local_rc, self.step_state_files, self.step_hooks, self.step_path,
                 self.step_mobilize_rc, self.step_commit, self.step_web, self.step_launcher,
                 self.step_icon, self.step_autostart, self.step_nb]
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
