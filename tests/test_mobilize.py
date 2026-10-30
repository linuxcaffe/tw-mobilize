"""mobilize: every test runs in a fake home folder with a throwaway gittw "remote" (a bare repo
on disk), so nothing touches ~/.task, ~/.ssh, ~/.bashrc or the network. The data URL is a
plain path, so the SSH key and access steps are skipped; Termux is never detected."""
import importlib.util
import io
import json
import os
import socket
import subprocess
import sys
import time
import shutil
from types import SimpleNamespace
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location("mobilize", ROOT / "mobilize.py")
mobilize = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mobilize)

SEED = {
    '.taskrc': 'data.location=~/.task\nhooks=off\ninclude ~/.task/config/included.rc\n'
               '\n# This station only\ninclude ~/.task/local.rc\n',
    'config/included.rc': 'include ~/.task/config/nag-state.rc\ninclude ~/.task/config/events.rc\n',
    'config/events.rc': 'include holidays.rc\n',
    'config/holidays.rc': 'holiday.x.name=X\nholiday.x.date=20260101\n',
    '.gitignore': 'local.rc\nconfig/nag-state.rc\n',
    'pending.data': '',
    'scripts/tw': '#!/bin/sh\necho tw\n',
}


def sh(*argv, cwd=None):
    r = subprocess.run(argv, cwd=cwd, capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    return r.stdout


@pytest.fixture
def home(tmp_path, monkeypatch):
    h = tmp_path / 'home'
    h.mkdir()
    monkeypatch.setenv('HOME', str(h))
    for var in ('TW_TASK_DIR', 'TASKRC', 'TASKDATA', 'TERMUX_VERSION', 'PREFIX'):
        monkeypatch.delenv(var, raising=False)
    for k, v in (('GIT_AUTHOR_NAME', 't'), ('GIT_AUTHOR_EMAIL', 't@t'),
                 ('GIT_COMMITTER_NAME', 't'), ('GIT_COMMITTER_EMAIL', 't@t'),
                 ('GIT_CONFIG_GLOBAL', '/dev/null')):
        monkeypatch.setenv(k, v)
    monkeypatch.setattr(mobilize.urllib.request, 'urlopen',
                        lambda *a, **k: pytest.fail('a test reached the network'))
    return h


@pytest.fixture
def remote(tmp_path):
    """A bare repo holding a small gittw ~/.task; returns its path (the --data URL)."""
    def make(files=SEED):
        seed = tmp_path / 'seed'
        for rel, text in files.items():
            p = seed / rel
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(text)
        (seed / 'scripts' / 'tw').chmod(0o755)
        sh('git', 'init', '-q', '-b', 'master', cwd=seed)
        sh('git', 'add', '-A', cwd=seed)
        sh('git', 'commit', '-qm', 'seed', cwd=seed)
        bare = tmp_path / 'remote.git'
        sh('git', 'clone', '-q', '--bare', str(seed), str(bare))
        return str(bare)
    return make


def init(*args):
    return mobilize.main(['init', '--station', 't1', '--platform', 'desktop', '--no-web', *args])


def test_init_sets_up_a_new_station(home, remote, capsys):
    url = remote()
    code = init('--data', url)
    out = capsys.readouterr().out
    t = home / '.task'
    assert (t / '.git').exists()
    assert (home / '.taskrc').is_symlink() and os.readlink(home / '.taskrc') == '.task/.taskrc'
    assert mobilize.read_rc(t / 'local.rc')['gittw.device'] == 't1'
    assert (t / 'config' / 'nag-state.rc').exists(), 'a gitignored include target is created empty'
    assert '.task/scripts' in (home / '.bashrc').read_text()
    rc = mobilize.read_rc(t / 'config' / 'mobilize.rc')
    assert rc['mobilize.data.push'] == url
    assert mobilize.stations(rc) == {'t1': {'platform': 'desktop'}}
    assert 'include ~/.task/config/mobilize.rc' in (t / 'config' / 'included.rc').read_text()
    assert ' fail ' not in out
    assert code in (0, 1), out          # 1: PATH isn't set in this process yet


def test_task_reads_the_new_station(home, remote):
    init('--data', remote())
    r = subprocess.run(['task', 'rc.hooks=off', '_get', 'rc.gittw.device', 'rc.tw.platforms'],
                       capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    assert r.stdout.split() == ['t1', 'desktop']


def test_init_commits_the_shared_files_together_and_does_not_push(home, remote):
    url = remote()
    init('--data', url)
    t = home / '.task'
    shown = sh('git', '-C', str(t), 'show', '--stat', '--format=%s', 'HEAD')
    assert shown.startswith('mobilize init: station t1 (desktop)')
    assert 'config/mobilize.rc' in shown and 'config/included.rc' in shown
    assert 'local.rc' not in shown.split('\n', 1)[1].replace('mobilize.rc', '')
    assert sh('git', '-C', str(t), 'status', '--porcelain', '--', 'config') == ''
    assert sh('git', '-C', str(t), 'rev-list', '--count', '@{u}..HEAD').strip() == '1'


def test_init_again_changes_nothing(home, remote, capsys):
    init('--data', remote())
    capsys.readouterr()
    before = {p: p.read_bytes() for p in home.rglob('*') if p.is_file() and '.git' not in p.parts}
    init()
    out = capsys.readouterr().out
    assert ' done ' not in out, out
    after = {p: p.read_bytes() for p in home.rglob('*') if p.is_file() and '.git' not in p.parts}
    assert before == after


def test_dry_run_changes_nothing(home, remote, capsys):
    code = init('--data', remote(), '--dry-run')
    out = capsys.readouterr().out
    assert code == 0
    assert ' would ' in out
    assert list(home.iterdir()) == []


def test_things_in_the_way_are_moved_aside_never_deleted(home, remote, capsys):
    (home / '.taskrc').write_text('old=1\n')
    (home / '.task').mkdir()
    (home / '.task' / 'stray').write_text('keep me')
    init('--data', remote())
    old_rc = list(home.glob('.taskrc.pre-mobilize-*'))
    old_dir = list(home.glob('.task.pre-mobilize-*'))
    assert len(old_rc) == 1 and old_rc[0].read_text() == 'old=1\n'
    assert len(old_dir) == 1 and (old_dir[0] / 'stray').read_text() == 'keep me'


def test_an_empty_task_dir_is_simply_replaced(home, remote):
    (home / '.task').mkdir()
    init('--data', remote())
    assert (home / '.task' / '.git').exists()
    assert not list(home.glob('.task.pre-mobilize-*'))


def test_a_missing_tracked_include_stops_init(home, remote, capsys):
    files = dict(SEED)
    files['config/included.rc'] += 'include ~/.task/config/gone.rc\n'
    code = init('--data', remote(files))
    out = capsys.readouterr().out
    assert code == 2
    assert 'gone.rc, which is missing and tracked' in out
    assert not (home / '.task' / 'config' / 'gone.rc').exists()


def test_no_data_url_and_no_clone_says_what_to_give(home, capsys):
    code = init()
    assert code == 2
    assert '--data' in capsys.readouterr().out


class _Download(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def fake_installer(monkeypatch, body):
    """taskwarrior-2.6's install.sh, as downloaded: a script with this body; returns the URLs asked for."""
    asked = []
    def urlopen(url, timeout=None):
        asked.append(url)
        return _Download(body.encode())
    monkeypatch.setattr(mobilize.urllib.request, 'urlopen', urlopen)
    return asked


def test_taskwarrior_3_gets_a_loud_warning_but_init_carries_on(home, remote, monkeypatch, capsys):
    monkeypatch.setattr(mobilize, 'task_version', lambda: '3.1.0')
    asked = fake_installer(monkeypatch, 'echo "Replace Taskwarrior 3.1.0? (no)"; exit 1\n')   # the owner said no
    code = init('--data', remote())
    out = capsys.readouterr().out
    assert '!!! WARNING: Taskwarrior 3.1.0 is installed' in out
    assert (home / '.task' / '.git').exists(), 'not blocked'
    assert 'NOT 2.6.x' in out, 'check says so too'
    assert code == 1
    assert asked == [mobilize.TASK_INSTALLER]


def _task_init(notes):
    init = mobilize.Init.__new__(mobilize.Init)
    init.dry, init.out = False, lambda *a: None
    init.note = lambda status, step, detail: notes.append((status, detail))
    init.act = lambda step, detail, fn: (fn(), True)[1]
    return init


def test_step_task_runs_the_synced_installer_before_downloading(home, monkeypatch):
    script = home / '.task' / 'scripts' / 'taskwarrior-2.6'
    script.parent.mkdir(parents=True)
    flag = home / 'installed'
    script.write_text(f'touch {flag}\n')
    monkeypatch.setattr(mobilize, 'task_version', lambda: '2.6.2' if flag.exists() else None)
    assert _task_init([]).step_task()               # the home fixture fails any download
    assert flag.exists()


def test_step_task_stops_init_when_nothing_got_installed(home, monkeypatch):
    monkeypatch.setattr(mobilize, 'task_version', lambda: None)
    fake_installer(monkeypatch, 'echo "building 2.6.2 needs: cmake"; exit 1\n')
    with pytest.raises(SystemExit, match='Taskwarrior 2.6.2 is not installed'):
        _task_init([]).step_task()


def test_edit_opens_the_shared_or_the_local_rc(home, remote, monkeypatch, capsys):
    init('--data', remote())
    log = home / 'edited'
    monkeypatch.setenv('EDITOR', f'sh -c \'echo "$0" >> {log}\'')
    monkeypatch.delenv('VISUAL', raising=False)
    capsys.readouterr()
    assert mobilize.main(['-e']) == 0
    assert 'is shared: commit it' in capsys.readouterr().out
    assert mobilize.main(['-e', 'local']) == 0
    assert log.read_text().split() == [str(home / '.task' / 'config' / 'mobilize.rc'),
                                       str(home / '.task' / 'local.rc')]


def test_edit_before_init_says_so(home, capsys):
    assert mobilize.main(['-e']) == 2
    assert 'run `mobilize init` first' in capsys.readouterr().out


def test_mobile_station_starts_with_hooks_off(home, remote):
    mobilize.main(['init', '--data', remote(), '--station', 'p1', '--platform', 'mobile', '--no-web'])
    rc = mobilize.read_rc(home / '.task' / 'local.rc')
    assert rc['hooks'] == 'off' and rc['tw.platforms'] == 'mobile'


def test_a_second_station_is_added_to_mobilize_rc_keeping_the_rest(home, remote):
    init('--data', remote())
    path = home / '.task' / 'config' / 'mobilize.rc'
    path.write_text(path.read_text() + 'mobilize.station.t1.note = my laptop\n')
    (home / '.task' / 'local.rc').unlink()
    mobilize.main(['init', '--station', 't2', '--platform', 'mobile', '--no-web'])
    st = mobilize.stations(mobilize.read_rc(path))
    assert st['t1'] == {'platform': 'desktop', 'note': 'my laptop'}
    assert st['t2'] == {'platform': 'mobile'}


def test_push_urls_from_mobilize_rc_reach_a_new_clone(home, remote, tmp_path):
    url = remote()
    init('--data', url)
    t = home / '.task'
    rc = t / 'config' / 'mobilize.rc'
    rc.write_text(rc.read_text().replace(f'mobilize.data.push = {url}',
                                         f'mobilize.data.push = {url}, /elsewhere/mirror.git'))
    sh('git', '-C', str(t), 'commit', '-qam', 'push to a mirror too')
    assert 'mobilize.rc' in sh('git', '-C', str(t), 'show', '--stat', 'HEAD~1'), 'init committed it'
    sh('git', '-C', str(t), 'push', '-q', url, 'HEAD:master')
    second = tmp_path / 'home2'
    second.mkdir()
    os.environ['HOME'] = str(second)
    mobilize.main(['init', '--data', url, '--station', 't2', '--no-web'])
    pushes = sh('git', '-C', str(second / '.task'), 'remote', 'get-url', '--push', '--all', 'origin').split()
    assert pushes == [url, '/elsewhere/mirror.git']


def test_check_finds_dangling_links_and_orphan_gitlinks(home, remote, capsys):
    init('--data', remote())
    t = home / '.task'
    (t / 'scripts' / 'old').symlink_to('/nowhere/at/all')
    sub = t / 'themes'
    sub.mkdir()
    sh('git', 'init', '-q', cwd=sub)
    (sub / 'a.theme').write_text('x')
    sh('git', 'add', '-A', cwd=sub)
    sh('git', 'commit', '-qm', 'x', cwd=sub)
    sh('git', '-C', str(t), 'add', 'themes')
    capsys.readouterr()
    code = mobilize.main(['check'])
    out = capsys.readouterr().out
    assert code >= 1
    assert 'scripts/old → /nowhere/at/all' in out
    assert 'themes: recorded as a submodule' in out


def test_check_names_another_machines_home(home, remote, capsys):
    files = dict(SEED)
    files['config/included.rc'] += 'include /home/someone/.task/config/x.rc\n'
    init('--data', remote(files))
    capsys.readouterr()
    mobilize.main(['check'])
    assert "another machine's home: /home/someone" in capsys.readouterr().out


def test_check_flags_an_include_naming_this_stations_own_home(home, remote, capsys):
    init('--data', remote())
    inc = home / '.task' / 'config' / 'included.rc'
    inc.write_text(inc.read_text() + f'include {home}/.task/config/events.rc\n')
    capsys.readouterr()
    mobilize.main(['check'])
    assert 'only works on this station' in capsys.readouterr().out


def test_check_without_a_clone_fails(home, capsys):
    assert mobilize.main(['check']) == 2
    assert 'not a git repo' in capsys.readouterr().out


def test_rc_templates_read_back(home):
    rc_path = home / 'm.rc'
    rc_path.write_text(mobilize.mobilize_rc_text(['git@a:x.git', 'git@b:x.git'], 's5', 'mobile', ssh='s5'))
    rc = mobilize.read_rc(rc_path)
    assert mobilize._list(rc['mobilize.data.push']) == ['git@a:x.git', 'git@b:x.git']
    assert mobilize.stations(rc) == {'s5': {'platform': 'mobile', 'ssh': 's5'}}
    (home / 'l.rc').write_text(mobilize.local_rc_text('s5', 'mobile'))
    assert mobilize.read_rc(home / 'l.rc') == {'gittw.device': 's5', 'tw.platforms': 'mobile', 'hooks': 'off'}


def test_launcher_starts_the_server_only_when_it_is_down():
    text = mobilize.launcher_text(Path('/x/tw-web'), '/usr/bin/python')
    assert 'if ! up && ! looping; then' in text
    assert 'cd "/x/tw-web"' in text and '"/usr/bin/python" app.py' in text
    assert text.rstrip().endswith('termux-open-url http://localhost:$port/')


def _free_port():
    with socket.socket() as so:
        so.bind(('127.0.0.1', 0))
        return so.getsockname()[1]


def _wait(cond, secs=10):
    end = time.time() + secs
    while time.time() < end:
        if cond():
            return True
        time.sleep(0.1)
    return False


# Any real shared library: the launcher's LD_PRELOAD must point at a loadable one.
LIBC_STANDIN = next((p for p in ('/lib/x86_64-linux-gnu/libm.so.6', '/lib/aarch64-linux-gnu/libm.so.6',
                                 '/usr/lib/libm.so.6', '/lib64/libm.so.6') if os.path.exists(p)), None)


@pytest.fixture
def launcher(tmp_path):
    if not LIBC_STANDIN:
        pytest.skip('no libm to stand in for termux-exec')
    """The real launcher script, run with sh against a stand-in app.py on a free port."""
    port = _free_port()
    web = tmp_path / 'web'
    web.mkdir()
    (web / 'app.py').write_text(
        'import http.server\n'
        f'http.server.HTTPServer(("127.0.0.1", {port}), http.server.SimpleHTTPRequestHandler).serve_forever()\n')
    bin_ = tmp_path / 'bin'
    bin_.mkdir()
    for name in ('termux-wake-lock', 'termux-wake-unlock', 'termux-open-url'):
        (bin_ / name).write_text(f'#!/bin/sh\necho {name} "$@" >> {tmp_path}/termux.log\n')
        (bin_ / name).chmod(0o755)
    prefix = tmp_path / 'prefix'
    (prefix / 'lib').mkdir(parents=True)
    lib = prefix / 'lib' / 'libtermux-exec-ld-preload.so'
    shutil.copy(LIBC_STANDIN, lib)                   # a real library, so the preload loads quietly
    script = tmp_path / 'tw-web'
    old_prefix, os.environ['PREFIX'] = os.environ.get('PREFIX'), str(prefix)
    old_task, os.environ['TW_TASK_DIR'] = os.environ.get('TW_TASK_DIR'), str(tmp_path / '.task')
    try:
        script.write_text(mobilize.launcher_text(web, sys.executable, port=port))
    finally:
        for k, v in (('PREFIX', old_prefix), ('TW_TASK_DIR', old_task)):
            os.environ.pop(k) if v is None else os.environ.__setitem__(k, v)
    run = tmp_path / 'run'
    env = {k: v for k, v in os.environ.items() if k != 'LD_PRELOAD'}   # bare, as from the widget
    env.update(HOME=str(tmp_path), XDG_RUNTIME_DIR=str(run), PATH=f"{bin_}:{os.environ['PATH']}")
    paths = SimpleNamespace(script=script, port=port, env=env, pidf=run / 'tw-web' / 'tw-web.pid',
                            loopf=run / 'tw-web' / 'tw-web.loop', log=tmp_path / '.cache' / 'tw-web' / 'server.out',
                            termux=tmp_path / 'termux.log', lib=lib, scripts=tmp_path / '.task' / 'scripts')
    paths.go = lambda *a: subprocess.run(['sh', str(script), *a], env=env, capture_output=True, text=True, timeout=30)
    paths.up = lambda: mobilize.web_up(port)
    yield paths
    for f in (paths.pidf, paths.loopf):              # never leave a server or loop behind
        try:
            os.kill(int(f.read_text()), 9)
        except (OSError, ValueError):
            pass


def test_launcher_start_runs_the_server_in_a_loop_that_survives_a_crash(launcher):
    r = launcher.go('--start')
    assert r.returncode == 0, r.stderr
    assert _wait(launcher.up), launcher.log.read_text() if launcher.log.exists() else 'no log'
    assert 'termux-open-url' not in launcher.termux.read_text(), '--start opens no browser'
    assert 'termux-wake-lock' in launcher.termux.read_text()
    first = int(launcher.pidf.read_text())
    os.kill(first, 9)                                # Android (or a crash) kills the server
    assert _wait(lambda: launcher.pidf.exists() and int(launcher.pidf.read_text()) != first and launcher.up())
    assert 'starting it again' in launcher.log.read_text()
    launcher.go('--start')                           # already running: no second loop
    assert launcher.log.read_text().count('mobilize: starting tw-web') == 1


def test_launcher_gives_a_widget_started_server_termux_exec_and_the_fleet_scripts(launcher):
    launcher.go('--start')
    assert _wait(launcher.up)
    env = mobilize.server_environ(launcher.pidf)
    assert env['LD_PRELOAD'] == str(launcher.lib), 'else every #!/usr/bin/env hook fails'
    assert env['PATH'].split(':')[0] == str(launcher.scripts)


def test_every_run_takes_the_wake_lock_again(launcher):
    # 2026-10-23: s5's lock was gone with the server still up, so Termux and sshd slept
    launcher.go('--start')
    assert _wait(launcher.up)
    launcher.go('--start')                           # already running
    assert launcher.termux.read_text().count('termux-wake-lock') == 2


def test_an_0_6_launcher_is_old_too():
    d = Path('/x/tw-web')
    assert mobilize.is_old_launcher(mobilize._launcher_text_0_6(d, '/usr/bin/python3'), d)


def test_an_0_5_launcher_is_old_too():
    d = Path('/x/tw-web')
    assert mobilize.is_old_launcher(mobilize._launcher_text_0_5(d, '/usr/bin/python3'), d)


def test_launcher_loop_does_not_restart_a_server_stopped_on_purpose(launcher):
    launcher.go('--start')
    assert _wait(launcher.up)
    pid = int(launcher.pidf.read_text())
    launcher.pidf.unlink()                           # what tw-web's Stop does, then SIGTERM
    os.kill(pid, 15)
    assert _wait(lambda: not launcher.loopf.exists())
    assert not launcher.up() and 'not restarting' in launcher.log.read_text()
    assert 'termux-wake-unlock' in launcher.termux.read_text()


def test_launcher_without_start_opens_the_browser(launcher):
    launcher.go()
    assert launcher.up()
    assert f'termux-open-url http://localhost:{launcher.port}/' in launcher.termux.read_text()


def test_an_old_launcher_is_recognised_whichever_python_it_named():
    d = Path('/x/tw-web')
    assert mobilize.is_old_launcher(mobilize._launcher_text_0_4(d, '/usr/bin/python'), d)
    assert not mobilize.is_old_launcher(mobilize._launcher_text_0_4(d, '/usr/bin/python') + 'echo mine\n', d)
    assert not mobilize.is_old_launcher(mobilize.launcher_text(d, '/usr/bin/python'), d)


def test_autostart_block_is_added_once_and_rewritten_in_place():
    text = 'export PATH=x\n'
    once = mobilize.with_autostart(text)
    assert once.startswith(text) and mobilize.bashrc_autostart(once) == 'current'
    assert mobilize.with_autostart(once) == once
    stale = once.replace('--start; fi', '--start; fi\necho old')
    assert mobilize.bashrc_autostart(stale) == 'old'
    assert mobilize.with_autostart(stale) == once
    assert mobilize.bashrc_autostart(text) is None


def test_init_on_termux_writes_the_autostart_block_and_replaces_an_old_launcher(home, remote, monkeypatch, capsys):
    monkeypatch.setenv('TERMUX_VERSION', '0.118')
    web = home / 'tw-web'
    web.mkdir()
    (web / 'app.py').write_text('')
    shortcut = home / '.shortcuts' / 'tw-web'
    shortcut.parent.mkdir()
    shortcut.write_text(mobilize._launcher_text_0_4(web, '/data/data/com.termux/files/usr/bin/python'))  # not python3
    monkeypatch.setattr(mobilize, 'web_dir', lambda s=None: web)
    init = mobilize.Init.__new__(mobilize.Init)
    init.dry, init.web, init.worst, init.out = False, True, mobilize.OK, lambda *a: None
    init.note = lambda status, step, detail: None
    init.act = lambda step, detail, fn: (fn(), True)[1]
    (web / 'tw-logo.png').write_bytes(b'PNG logo')
    assert init.step_launcher() and init.step_icon() and init.step_autostart()
    icon = home / '.shortcuts' / 'icons' / 'tw-web.png'
    assert icon.read_bytes() == b'PNG logo'
    icon.write_bytes(b'mine')
    assert init.step_icon() and icon.read_bytes() == b'mine', 'an icon already there is kept'
    assert shortcut.read_text() == mobilize.launcher_text(web, mobilize.web_python(web))
    assert mobilize.bashrc_autostart((home / '.bashrc').read_text()) == 'current'
    (home / '.task').mkdir(exist_ok=True)
    (home / '.task' / 'local.rc').write_text('mobilize.web.autostart = no\n')
    assert not mobilize.autostart_wanted()


def test_init_replaces_the_launcher_without_touching_the_file_a_running_loop_reads(home, remote, monkeypatch):
    # 2026-10-23: init rewrote ~/.shortcuts/tw-web in place under s5's running `sh … --loop`; sh reads
    # its script as it goes, so when the loop ended it would read on in the new text. A new file
    # renamed over the old leaves the running shell its own copy.
    monkeypatch.setenv('TERMUX_VERSION', '0.118')
    web = home / 'tw-web'
    web.mkdir()
    (web / 'app.py').write_text('')
    shortcut = home / '.shortcuts' / 'tw-web'
    shortcut.parent.mkdir()
    shortcut.write_text(mobilize._launcher_text_0_6(web, mobilize.web_python(web)))
    running = open(shortcut)                         # what the loop's sh holds open
    monkeypatch.setattr(mobilize, 'web_dir', lambda s=None: web)
    init = mobilize.Init.__new__(mobilize.Init)
    init.dry, init.web, init.worst, init.out = False, True, mobilize.OK, lambda *a: None
    init.note = lambda status, step, detail: None
    init.act = lambda step, detail, fn: (fn(), True)[1]
    assert init.step_launcher()
    assert shortcut.read_text() == mobilize.launcher_text(web, mobilize.web_python(web))
    assert running.read() == mobilize._launcher_text_0_6(web, mobilize.web_python(web))
    assert os.access(shortcut, os.X_OK)
    running.close()


def _termux_init(notes):
    init = mobilize.Init.__new__(mobilize.Init)
    init.dry, init.web, init.worst, init.out = False, True, mobilize.OK, lambda *a: None
    init.note = lambda status, step, detail: notes.append((status, detail))
    init.act = lambda step, detail, fn: (fn(), True)[1]
    return init


def test_init_on_termux_gives_nb_its_quick_note_buttons_once_nb_is_installed(home, tmp_path, monkeypatch):
    monkeypatch.setenv('TERMUX_VERSION', '0.118')
    bin_ = tmp_path / 'bin'
    bin_.mkdir()
    monkeypatch.setenv('PATH', str(bin_))
    notes = []
    init = _termux_init(notes)
    scripts = mobilize.nb_scripts()
    assert init.step_nb() and not any(p.exists() for p in scripts), 'no nb: nothing written'
    assert notes[-1][0] == mobilize.SKIP

    for tool in ('nb', 'termux-dialog'):
        (bin_ / tool).write_text('#!/bin/sh\n')
        (bin_ / tool).chmod(0o755)
    notes.clear()
    assert init.step_nb()
    for path, text in scripts.items():
        assert path.read_text() == text and os.access(path, os.X_OK)
    icons = [home / '.shortcuts' / 'icons' / f'{name}.png' for name in ('nb-note', 'nb-quick')]
    for icon in icons:
        assert icon.read_bytes().startswith(b'\x89PNG\r\n\x1a\n'), "nb's logo, where Termux:Widget looks"
    assert not any(s == mobilize.YOU for s, _ in notes), 'termux-dialog is there'
    icons[0].write_bytes(b'mine')

    mine = home / 'bin' / 'termux-url-opener'
    mine.write_text('#!/bin/sh\necho my own opener\n')
    (bin_ / 'termux-dialog').unlink()
    notes.clear()
    assert init.step_nb()
    assert mine.read_text() == '#!/bin/sh\necho my own opener\n', 'one edited by hand is left alone'
    assert icons[0].read_bytes() == b'mine', 'an icon already there is kept'
    assert [s for s, _ in notes] == [mobilize.OK, mobilize.OK, mobilize.WARN, mobilize.OK, mobilize.OK, mobilize.YOU]
    assert 'termux-api' in notes[-1][1]


def test_nb_scripts_are_valid_shell():
    for path, text in mobilize.nb_scripts().items():
        shell = 'bash' if text.startswith('#!/data/data/com.termux/files/usr/bin/bash') else 'sh'
        r = subprocess.run([shell, '-n'], input=text, capture_output=True, text=True)
        assert r.returncode == 0, f'{path.name}: {r.stderr}'


def test_init_on_termux_gives_hledger_web_a_button_once_it_is_installed(home, tmp_path, monkeypatch):
    monkeypatch.setenv('TERMUX_VERSION', '0.118')
    prefix = tmp_path / 'usr'
    monkeypatch.setenv('PREFIX', str(prefix))
    notes = []
    init = _termux_init(notes)
    launcher = home / '.shortcuts' / 'hledger-web'
    icon = home / '.shortcuts' / 'icons' / 'hledger-web.png'
    assert init.step_hledger_web() and not launcher.exists(), 'no hledger-web: nothing written'
    assert notes[-1][0] == mobilize.SKIP

    debian = prefix / 'var' / 'lib' / 'proot-distro' / 'containers' / 'debian' / 'rootfs' / 'usr' / 'bin'
    debian.mkdir(parents=True)
    (debian / 'hledger-web').write_text('')
    notes.clear()
    assert init.step_hledger_web()
    assert launcher.read_text() == mobilize.HLEDGER_WEB_TEXT and os.access(launcher, os.X_OK)
    assert icon.read_bytes().startswith(b'\x89PNG\r\n\x1a\n'), "hledger's logo, where Termux:Widget looks"
    assert mobilize.hledger_web_where() == 'debian'

    launcher.write_text('#!/bin/sh\necho mine\n')
    icon.write_bytes(b'mine')
    notes.clear()
    assert init.step_hledger_web()
    assert launcher.read_text() == '#!/bin/sh\necho mine\n', 'one edited by hand is left alone'
    assert icon.read_bytes() == b'mine', 'an icon already there is kept'
    assert [s for s, _ in notes] == [mobilize.WARN, mobilize.OK]

    (prefix / 'bin').mkdir()
    (prefix / 'bin' / 'hledger-web').write_text('')
    assert mobilize.hledger_web_where() == 'termux', 'a native one (a future Termux package) comes first'


def test_hledger_web_launcher_is_valid_shell():
    r = subprocess.run(['sh', '-n'], input=mobilize.HLEDGER_WEB_TEXT, capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    assert f'port={mobilize.HLEDGER_WEB_PORT}' in mobilize.HLEDGER_WEB_TEXT


def test_init_on_termux_gives_t_its_quick_button_once_t_is_there(home, tmp_path, monkeypatch):
    monkeypatch.setenv('TERMUX_VERSION', '0.118')
    bin_ = tmp_path / 'bin'
    bin_.mkdir()
    monkeypatch.setenv('PATH', str(bin_))
    notes = []
    init = _termux_init(notes)
    script = home / '.shortcuts' / 'tasks' / 't-quick'
    icons = home / '.shortcuts' / 'icons'
    assert init.step_t_quick() and not script.exists(), 'no t: nothing written'
    assert notes[-1][0] == mobilize.SKIP

    t = home / '.task' / 'scripts' / 't'
    t.parent.mkdir(parents=True, exist_ok=True)
    t.write_text('#!/bin/sh\n')
    notes.clear()
    assert init.step_t_quick()
    assert script.read_text() == mobilize.T_QUICK_TEXT and os.access(script, os.X_OK)
    for name in ('.t-quick-in.png', '.t-quick-out.png', 't-quick.png'):
        assert (icons / name).read_bytes().startswith(b'\x89PNG\r\n\x1a\n'), name
    assert (icons / 't-quick.png').read_bytes() == (icons / '.t-quick-out.png').read_bytes(), 'starts red'
    assert notes[-1][0] == mobilize.YOU and 'termux-api' in notes[-1][1]

    script.write_text('#!/bin/sh\necho mine\n')
    (bin_ / 'termux-dialog').write_text('#!/bin/sh\n')
    (bin_ / 'termux-dialog').chmod(0o755)
    notes.clear()
    assert init.step_t_quick()
    assert script.read_text() == '#!/bin/sh\necho mine\n', 'one edited by hand is left alone'
    assert [s for s, _ in notes] == [mobilize.WARN, mobilize.OK, mobilize.OK, mobilize.OK]


def test_t_quick_is_valid_bash():
    r = subprocess.run(['bash', '-n'], input=mobilize.T_QUICK_TEXT, capture_output=True, text=True)
    assert r.returncode == 0, r.stderr


def test_nb_quick_notes_go_to_mobile_and_old_scripts_are_replaced(home, tmp_path, monkeypatch):
    for path, text in mobilize.nb_scripts().items():
        if path.name != 'termux-url-opener':
            assert '${to}add' in text and 'NB_AUTO_SYNC=0' in text, path.name
    monkeypatch.setenv('TERMUX_VERSION', '0.118')
    bin_ = tmp_path / 'bin'
    bin_.mkdir()
    for tool in ('nb', 'termux-dialog', 'termux-toast'):
        (bin_ / tool).write_text('#!/bin/sh\n')
        (bin_ / tool).chmod(0o755)
    monkeypatch.setenv('PATH', str(bin_))
    old = '#!/bin/sh\n# an nb-quick an older mobilize wrote\n'
    monkeypatch.setitem(mobilize.OLD_SCRIPTS, 'nb-quick',
                        {mobilize.hashlib.sha256(old.encode()).hexdigest()})
    quick = home / '.shortcuts' / 'tasks' / 'nb-quick'
    note = home / '.shortcuts' / 'nb-note'
    quick.parent.mkdir(parents=True)
    quick.write_text(old)
    note.write_text('#!/bin/sh\necho mine\n')
    notes = []
    assert _termux_init(notes).step_nb()
    assert quick.read_text() == mobilize.nb_scripts()[quick], 'an untouched old one is replaced'
    assert note.read_text() == '#!/bin/sh\necho mine\n', 'an edited one is left alone'


def test_init_on_termux_gives_nb_m_sync_and_turns_nb_auto_sync_off(home, tmp_path, monkeypatch):
    monkeypatch.setenv('TERMUX_VERSION', '0.118')
    bin_ = tmp_path / 'bin'
    bin_.mkdir()
    monkeypatch.setenv('PATH', f'{bin_}:/usr/bin:/bin')
    notes = []
    init = _termux_init(notes)
    script = home / '.shortcuts' / 'tasks' / 'm-sync'
    icons = home / '.shortcuts' / 'icons'
    assert init.step_m_sync() and not script.exists(), 'no nb: nothing written'
    assert notes[-1][0] == mobilize.SKIP

    calls = tmp_path / 'nb-calls'
    (bin_ / 'nb').write_text(f'#!/bin/sh\necho "$@" >> {calls}\n[ "$1" = settings ] && echo 1\nexit 0\n')
    (bin_ / 'nb').chmod(0o755)
    notes.clear()
    assert init.step_m_sync()
    assert script.read_text() == mobilize.M_SYNC_TEXT and os.access(script, os.X_OK)
    for name in ('.m-sync-ok.png', '.m-sync-offline.png', '.m-sync-error.png', 'm-sync.png'):
        assert (icons / name).read_bytes().startswith(b'\x89PNG\r\n\x1a\n'), name
    assert (icons / 'm-sync.png').read_bytes() == (icons / '.m-sync-offline.png').read_bytes(), 'amber until it syncs'
    assert 'set auto_sync 0' in calls.read_text()
    assert notes[-1][0] == mobilize.YOU and 'termux-api' in notes[-1][1]

    script.write_text('#!/bin/sh\necho mine\n')
    notes.clear()
    assert init.step_m_sync()
    assert script.read_text() == '#!/bin/sh\necho mine\n', 'one edited by hand is left alone'
    assert notes[0][0] == mobilize.WARN


def test_m_sync_schedule(home, tmp_path, monkeypatch):
    monkeypatch.setenv('TERMUX_VERSION', '0.118')
    bin_ = tmp_path / 'bin'
    bin_.mkdir()
    monkeypatch.setenv('PATH', f'{bin_}:/usr/bin:/bin')
    calls, pending = tmp_path / 'calls', tmp_path / 'pending'
    (bin_ / 'termux-job-scheduler').write_text(
        f'#!/bin/sh\necho "$@" >> {calls}\n[ "$1" = --pending ] && cat {pending}\nexit 0\n')
    (bin_ / 'termux-job-scheduler').chmod(0o755)
    job = home / '.termux' / 'm-sync-job'
    notes = []
    init = _termux_init(notes)

    pending.write_text('No jobs found\n')
    init.schedule_m_sync(job)
    assert (f'--job-id 7301 --script {job} --period-ms 900000 --network any --persisted true'
            in calls.read_text())

    pending.write_text(f'Pending Job 7301: {job}    (periodic: 900000ms) (persisted) (battery not low)\n')
    calls.write_text('')
    notes.clear()
    init.schedule_m_sync(job)
    assert calls.read_text() == '--pending\n' and notes[-1][0] == mobilize.OK, 'already there: left alone'

    monkeypatch.setattr(mobilize, 'settings', lambda: {'mobilize.msync.every': '30'})
    init.schedule_m_sync(job)
    assert '--period-ms 1800000' in calls.read_text(), 'a new period replaces it'

    monkeypatch.setattr(mobilize, 'settings', lambda: {'mobilize.msync.every': 'off'})
    init.schedule_m_sync(job)
    assert '--cancel --job-id 7301' in calls.read_text()


def test_m_sync_every():
    assert mobilize.m_sync_every({}) == 15
    assert mobilize.m_sync_every({'mobilize.msync.every': '5'}) == 15, "Android's floor"
    assert mobilize.m_sync_every({'mobilize.msync.every': '60m'}) == 60
    assert mobilize.m_sync_every({'mobilize.msync.every': 'off'}) == 0
    assert mobilize.m_sync_every({'mobilize.msync.every': 'often'}) == 15


def test_an_untouched_older_m_sync_is_replaced(home, tmp_path, monkeypatch):
    monkeypatch.setenv('TERMUX_VERSION', '0.118')
    bin_ = tmp_path / 'bin'
    bin_.mkdir()
    (bin_ / 'nb').write_text('#!/bin/sh\nexit 0\n')
    (bin_ / 'nb').chmod(0o755)
    monkeypatch.setenv('PATH', f'{bin_}:/usr/bin:/bin')
    old = '#!/bin/sh\n# an m-sync an older mobilize wrote\n'
    monkeypatch.setitem(mobilize.OLD_SCRIPTS, 'm-sync', {mobilize.hashlib.sha256(old.encode()).hexdigest()})
    script = home / '.shortcuts' / 'tasks' / 'm-sync'
    script.parent.mkdir(parents=True)
    script.write_text(old)
    assert _termux_init([]).step_m_sync()
    assert script.read_text() == mobilize.M_SYNC_TEXT
    job = home / '.termux' / 'm-sync-job'
    assert job.read_text() == mobilize.M_SYNC_JOB_TEXT and os.access(job, os.X_OK)


def test_init_collects_the_phone_data_in_mobile(home, monkeypatch):
    monkeypatch.setenv('TERMUX_VERSION', '0.118')
    monkeypatch.delenv('NB_DIR', raising=False)
    notes = []
    init = _termux_init(notes)
    init.station = 's5'
    assert init.step_collect() and notes[-1][0] == mobilize.SKIP, 'no shared mobile notebook yet'

    mobile = home / '.nb' / 'mobile'
    subprocess.run(['git', 'init', '-q', str(mobile)], check=True)
    subprocess.run(['git', '-C', str(mobile), 'remote', 'add', 'origin', 'git@x:y.git'], check=True)
    (mobile / 'accounts.journal').write_text('account Assets:cash\n')
    old = home / '.hledger.journal'
    old.write_text('\n2026-10-30 Gas\n    Expenses:auto:fuel  50\n    assets:cash\n')
    t = home / '.task' / 'scripts' / 't'
    t.parent.mkdir(parents=True, exist_ok=True)
    t.write_text('#!/bin/sh\n')
    clock = home / '.task' / 'time' / 'tw.timeclock'
    clock.parent.mkdir(parents=True)
    clock.write_text('i 2026/10/30 09:00:00 work\n')
    notes.clear()
    assert init.step_collect()
    journal = mobile / 's5.journal'
    text = journal.read_text()
    assert 'include accounts.journal' in text and text.rstrip().endswith('assets:cash'), 'the entries moved'
    assert old.is_symlink() and old.resolve() == journal.resolve()
    assert list(home.glob('.hledger.journal.pre-mobilize-*')), 'the old file is kept aside'
    assert notes[-1][0] == mobilize.WARN and 'clocked in' in notes[-1][1], "t's files wait for a clock-out"
    assert 'timelog.file' not in mobilize.read_rc(mobilize.local_rc())

    clock.write_text('i 2026/10/30 09:00:00 work\no 2026/10/30 10:00:00\n')
    notes.clear()
    assert init.step_collect()
    rc = mobilize.read_rc(mobilize.local_rc())
    assert rc['timelog.file'] == '~/.nb/mobile/s5.timeclock'
    assert rc['timelog.timedot_file'] == '~/.nb/mobile/s5.timedot'
    notes.clear()
    assert init.step_collect() and all(s == mobilize.OK for s, _ in notes), notes
    assert journal.read_text() == text, 'nothing moved twice'


def test_m_sync_is_valid_bash():
    r = subprocess.run(['bash', '-n'], input=mobilize.M_SYNC_TEXT, capture_output=True, text=True)
    assert r.returncode == 0, r.stderr


@pytest.mark.skipif(not shutil.which('nb'), reason='needs nb')
def test_m_sync_between_two_stations(tmp_path):
    """Two stations, one bare remote: notes travel both ways, a note changed on both sides ends
    as the remote's version plus a conflict copy (never markers), and a notebook whose history
    is unrelated to the remote's is refused without touching the remote."""
    bin_ = tmp_path / 'bin'
    bin_.mkdir()
    for tool in ('bash', 'python3', 'hledger'):
        if shutil.which(tool):
            (bin_ / tool).symlink_to(shutil.which(tool))
    (bin_ / 'nb').symlink_to(os.path.realpath(shutil.which('nb')))
    script = tmp_path / 'm-sync'
    script.write_text(mobilize.M_SYNC_TEXT)
    remote = tmp_path / 'remote.git'
    subprocess.run(['git', 'init', '-q', '--bare', str(remote)], check=True)
    (tmp_path / 'tmp').mkdir()

    def station(name):
        h = tmp_path / f'h{name}'
        (h / '.task').mkdir(parents=True)
        (h / '.task' / 'local.rc').write_text(f'gittw.device={name}\n')
        (h / '.gitconfig').write_text('[user]\n\tname = t\n\temail = t@t\n')
        env = dict(os.environ, HOME=str(h), NB_DIR=str(h / 'nb'), M_SYNC_BIN=str(bin_),
                   M_SYNC_LOG=str(h / 'log'), TMPDIR=str(tmp_path / 'tmp'), NB_AUTO_SYNC='0')
        nb = lambda *a: subprocess.run(['nb', *a], env=env, stdin=subprocess.DEVNULL,
                                        capture_output=True, text=True)
        sync = lambda: subprocess.run(['bash', str(script)], env=env, capture_output=True, text=True)
        nb('init')
        return h / 'nb', nb, sync

    a, nb_a, sync_a = station('A')
    nb_a('notebooks', 'add', 'mob')
    for args in (('branch', '-m', 'mob'), ('remote', 'add', 'origin', remote.as_uri()),
                 ('push', '-q', '-u', 'origin', 'mob')):
        subprocess.run(['git', '-C', str(a / 'mob'), *args], check=True, capture_output=True)
    b, nb_b, sync_b = station('B')
    nb_b('notebooks', 'add', 'mob', remote.as_uri(), 'mob')
    assert (b / 'mob' / '.git').exists()

    nb_a('mob:add', '--title', 'From A', '--content', 'a')
    (b / 'mob' / 'plain.md').write_text('saved by another app\n')
    for sync in (sync_a, sync_b, sync_a):
        r = sync()
        assert r.returncode == 0 and '✓' in r.stdout, r.stdout + r.stderr
    r = subprocess.run(['bash', str(script), '--quiet'], env=dict(os.environ, HOME=str(tmp_path / 'hB'),
                       NB_DIR=str(b), M_SYNC_BIN=str(bin_), M_SYNC_LOG=str(tmp_path / 'qlog'),
                       TMPDIR=str(tmp_path / 'tmp')), capture_output=True, text=True)
    assert r.returncode == 0 and 'mob ✓' in r.stdout, "a scheduled (--quiet) run"
    assert {p.name for p in (a / 'mob').glob('*.md')} == {'from_a.md', 'plain.md'}
    assert not (a / 'home' / '.m-sync.json').exists(), 'home has no remote: not synced'

    if shutil.which('hledger'):
        journal = b / 'mob' / 'B.journal'
        remote_head = lambda: subprocess.run(['git', '-C', str(remote), 'rev-parse', 'mob'],
                                             capture_output=True, text=True).stdout
        journal.write_text('2026-10-30 Gas\n    expenses:fuel  50\n    assets:cash\n')
        assert sync_b().returncode == 0, 'a journal that parses is pushed'
        journal.write_text('2026-10-30 Gas\n    expenses:fuel  50\n    assets:cash  -40\n')
        before = remote_head()
        r = sync_b()
        assert r.returncode == 1 and 'B.journal fails hledger check' in r.stdout, r.stdout
        assert remote_head() == before, "one that doesn't stays here"
        journal.write_text('2026-10-30 Gas\n    expenses:fuel  50\n    assets:cash  -50\n')
        assert sync_b().returncode == 0, 'fixed, it goes'
        sync_a()
        assert (a / 'mob' / 'B.journal').read_text().endswith('-50\n')

    (a / 'mob' / 'from_a.md').write_text('A edit\n')
    (b / 'mob' / 'from_a.md').write_text('B edit\n')
    sync_a()
    r = sync_b()
    assert r.returncode == 1 and 'from_a.conflict-B.md' in r.stdout
    assert (b / 'mob' / 'from_a.md').read_text() == 'A edit\n'
    assert (b / 'mob' / 'from_a.conflict-B.md').read_text() == 'B edit\n'
    sync_a()
    assert (a / 'mob' / 'from_a.conflict-B.md').exists(), 'the copy travels'
    status = json.loads((a / 'mob' / '.m-sync.json').read_text())
    assert status['state'] == 'conflict' and status['conflicts'] == ['from_a.conflict-B.md']
    for d in (a, b):
        assert '<<<<<<<' not in ''.join(p.read_text() for p in (d / 'mob').glob('*.md'))
        st = subprocess.run(['git', '-C', str(d / 'mob'), 'status', '--porcelain'],
                            capture_output=True, text=True).stdout
        assert st == '', 'the status file stays out of git'

    c, nb_c, sync_c = station('C')
    nb_c('notebooks', 'add', 'mob')
    for args in (('branch', '-m', 'mob'), ('remote', 'add', 'origin', remote.as_uri()),
                 ('fetch', '-q', 'origin'), ('branch', '-q', '-u', 'origin/mob')):
        subprocess.run(['git', '-C', str(c / 'mob'), *args], check=True, capture_output=True)
    head = lambda: subprocess.run(['git', '-C', str(remote), 'rev-parse', 'mob'],
                                  capture_output=True, text=True).stdout
    before = head()
    r = sync_c()
    assert r.returncode == 1 and 'nothing in common' in r.stdout
    assert head() == before, 'the remote is untouched'


def test_ssh_urls():
    assert mobilize.is_ssh_url('git@codeberg.org:me/data.git')
    assert mobilize.is_ssh_url('ssh://git@host:22/x.git')
    assert not mobilize.is_ssh_url('https://codeberg.org/linuxcaffe/tw-web.git')
    assert not mobilize.is_ssh_url('/tmp/remote.git')
    assert mobilize.ssh_host('git@github.com:me/x.git') == 'github.com'


def test_waiting_for_a_key_under_tw_noninteract_asks_for_a_terminal(home, monkeypatch, capsys):
    monkeypatch.setenv('TW_NOINTERACT', '1')
    monkeypatch.setattr(mobilize, 'reachable', lambda url: (False, 'git@x: Permission denied (publickey).'))
    i = mobilize.Init(station='t1', out=lambda *a: None)
    assert i._wait_for_access('access', 'git@codeberg.org:me/data.git') is False
    err = capsys.readouterr().err
    sig = json.loads(err.strip().splitlines()[-1])
    assert sig['type'] == 'tw_needs_pty' and 'codeberg.org' in sig['reason']


# ── the hook pre-test ─────────────────────────────────────────────────────────

HOOKS = {
    'on-add_good.sh': '#!/bin/sh\nread t; echo "$t"\n',
    'on-add_bad.sh': '#!/bin/sh\nread t; echo "this hook refuses"; exit 1\n',
    'on-launch_slow.sh': '#!/bin/sh\nsleep 0.5\n',
    'on-add_writer.sh': '#!/bin/sh\nread t; touch "$HOME/.task/config/touched"; echo "$t"\n',
    'on-modify_good.sh': '#!/bin/sh\nread a; read b; echo "$b"\n',
}


@pytest.fixture
def station(home):
    t = home / '.task'
    (t / 'hooks').mkdir(parents=True)
    (t / 'config').mkdir()
    (t / 'scripts').mkdir()
    (t / '.taskrc').write_text(f'data.location=~/.task\nconfidential.path={home}/.task/config\n')
    (home / '.taskrc').symlink_to(t / '.taskrc')
    for name, text in HOOKS.items():
        (t / 'hooks' / name).write_text(text)
        (t / 'hooks' / name).chmod(0o755)
    (t / 'hooks' / 'on-add_off.sh').write_text('#!/bin/sh\nexit 1\n')
    return t


def test_pretest_judges_each_hook_and_all_together(station, capsys):
    rows = {h: (st, n) for h, st, r, tot, n in mobilize.hook_pretest()}
    assert rows['on-add_good.sh'][0] == 'ok'
    assert rows['on-modify_good.sh'][0] == 'ok'
    assert rows['on-add_bad.sh'][0] == 'fail' and 'this hook refuses' in rows['on-add_bad.sh'][1]
    assert rows['on-launch_slow.sh'][0] == 'warn' and 'every read' in rows['on-launch_slow.sh'][1]
    assert rows['(all together)'][0] == 'fail', 'the bad hook spoils the chain'
    assert rows['on-add_off.sh'] == ('skip', 'not executable: off')


def test_pretest_never_touches_the_real_task_dir(station):
    before = sorted(p.relative_to(station) for p in station.rglob('*'))
    mobilize.hook_pretest()
    assert sorted(p.relative_to(station) for p in station.rglob('*')) == before
    assert not (station / 'config' / 'touched').exists()
    assert not list(station.glob('*.data'))


def test_pretest_sandbox_points_real_home_paths_inside_it(station, tmp_path):
    t, env = mobilize._sandbox(tmp_path / 'sb')
    assert env['HOME'] == str(tmp_path / 'sb' / 'home') and env['TASKRC'] == str(t / '.taskrc')
    assert f'confidential.path={tmp_path}/sb/home/.task/config' in (t / '.taskrc').read_text()
    assert (t / 'scripts').is_symlink(), 'big read-only folders are linked, not copied'
    assert env['PYTHONDONTWRITEBYTECODE'] == '1', 'so a hook importing from scripts/ leaves no __pycache__ there'


def test_check_hooks_cli(station, capsys):
    code = mobilize.main(['check', '--hooks', '--json'])
    data = json.loads(capsys.readouterr().out)
    assert code == 2
    assert {d['hook'] for d in data} >= {'on-add_good.sh', '(all together)'}


# ── choosing a station's hooks ────────────────────────────────────────────────

def test_hooks_set_builds_the_station_folder_and_writes_local_rc(home, remote, capsys):
    files = dict(SEED)
    files.update({'hooks/on-add_need-priority.py': '#!/bin/sh\nread t; echo "$t"\n',
                  'hooks/on-exit_need-priority.py': '#!/bin/sh\n',
                  'hooks/on-exit_gittw.py': '#!/bin/sh\n',
                  'hooks/on-exit_recurrence.py': '#!/bin/sh\n',
                  'hooks/on-modify_need-priority-extra.py': '#!/bin/sh\n'})
    files['.gitignore'] += 'hooks.d/\n'
    init('--data', remote(files))
    t = home / '.task'
    assert mobilize.main(['hooks', '--set', 'need-priority,gittw']) == 0
    folder = t / 'hooks.d' / 't1'
    assert sorted(p.name for p in folder.iterdir()) == ['on-add_need-priority.py', 'on-exit_gittw.py',
                                                        'on-exit_need-priority.py']
    assert os.readlink(folder / 'on-exit_gittw.py') == '../../hooks/on-exit_gittw.py'
    rc = mobilize.read_rc(t / 'local.rc')
    assert rc['mobilize.hooks'] == 'need-priority, gittw'
    assert rc['hooks.location'] == '~/.task/hooks.d/t1' and rc['hooks'] == 'on'
    r = subprocess.run(['task', '_get', 'rc.hooks.location'], capture_output=True, text=True)
    assert r.stdout.strip() == str(t / 'hooks.d' / 't1'), 'task reads it (and expands the ~)'


def test_hooks_set_again_replaces_lines_and_drops_stale_links(home, remote):
    files = dict(SEED)
    files.update({'hooks/on-exit_gittw.py': '#!/bin/sh\n', 'hooks/on-add_time-defaults.py': '#!/bin/sh\n'})
    init('--data', remote(files))
    mobilize.main(['hooks', '--set', 'gittw'])
    mobilize.main(['hooks', '--set', 'time-defaults'])
    t = home / '.task'
    assert [p.name for p in (t / 'hooks.d' / 't1').iterdir()] == ['on-add_time-defaults.py']
    text = (t / 'local.rc').read_text()
    assert text.count('mobilize.hooks') == 1 and text.count('hooks.location') == 1


def test_hooks_all_goes_back_to_every_hook(home, remote):
    files = dict(SEED)
    files['hooks/on-exit_gittw.py'] = '#!/bin/sh\n'
    init('--data', remote(files))
    mobilize.main(['hooks', '--set', 'gittw'])
    assert mobilize.main(['hooks', '--all']) == 0
    rc = mobilize.read_rc(home / '.task' / 'local.rc')
    assert 'hooks.location' not in rc and 'mobilize.hooks' not in rc
    assert not (home / '.task' / 'hooks.d').exists() or not list((home / '.task' / 'hooks.d').iterdir())


def test_hooks_names_an_app_with_no_hook(home, remote, capsys):
    init('--data', remote())
    capsys.readouterr()
    assert mobilize.main(['hooks', '--set', 'nonesuch']) == 1
    assert "no hook in" in capsys.readouterr().out


def test_check_says_when_the_folder_is_out_of_step(home, remote, capsys):
    files = dict(SEED)
    files['hooks/on-exit_gittw.py'] = '#!/bin/sh\n'
    init('--data', remote(files))
    mobilize.main(['hooks', '--set', 'gittw'])
    (home / '.task' / 'hooks' / 'on-add_gittw.py').write_text('#!/bin/sh\n')
    capsys.readouterr()
    mobilize.main(['check'])
    assert 'out of step with mobilize.hooks' in capsys.readouterr().out


def test_pretest_tests_the_chosen_hooks_only(station, home):
    (station / 'local.rc').write_text('gittw.device = t1\n')
    (station / '.taskrc').write_text((station / '.taskrc').read_text() + 'include ~/.task/local.rc\n')
    mobilize.main(['hooks', '--set', 'good'])
    rows = {r[0] for r in mobilize.hook_pretest()}
    assert 'on-add_good.sh' in rows and 'on-add_bad.sh' not in rows


# ── git identity and gittw's merge driver ─────────────────────────────────────

GITTW = Path.home() / 'dev' / 'gittw' / 'gittw.py'


def test_check_warns_when_this_station_cannot_commit(home, remote, monkeypatch, capsys):
    init('--data', remote())
    for k in ('GIT_AUTHOR_NAME', 'GIT_AUTHOR_EMAIL', 'GIT_COMMITTER_NAME', 'GIT_COMMITTER_EMAIL'):
        monkeypatch.delenv(k)
    monkeypatch.setenv('EMAIL', '')
    capsys.readouterr()
    mobilize.main(['check'])
    assert 'no name/email for commits here' in capsys.readouterr().out


def test_init_without_an_identity_says_how_to_set_it(home, remote, monkeypatch, capsys):
    url = remote()
    for k in ('GIT_AUTHOR_NAME', 'GIT_AUTHOR_EMAIL', 'GIT_COMMITTER_NAME', 'GIT_COMMITTER_EMAIL'):
        monkeypatch.delenv(k)
    monkeypatch.setenv('EMAIL', '')
    init('--data', url)
    out = capsys.readouterr().out
    assert 'you   git name' in out and 'config user.email' in out


@pytest.mark.skipif(not GITTW.exists(), reason='needs ~/dev/gittw')
def test_init_configures_gittws_merge_driver(home, remote, capsys):
    files = dict(SEED)
    files['scripts/gittw'] = GITTW.read_text()
    init('--data', remote(files))
    t = home / '.task'
    r = subprocess.run(['git', '-C', str(t), 'config', 'merge.twdata.driver'], capture_output=True, text=True)
    assert 'merge-data' in r.stdout
    assert 'pending.data merge=twdata' in (t / '.gitattributes').read_text()
    assert '.gitattributes' in sh('git', '-C', str(t), 'show', '--stat', '--format=', 'HEAD'), 'committed with the rest'


# ── android ───────────────────────────────────────────────────────────────────

HUAWEI = {'maker': 'HUAWEI', 'model': 'EML-L09', 'release': '10', 'emui': 'EmotionUI_10.0.0'}


def test_android_steps_for_a_huawei_on_android_10():
    steps = mobilize.android_steps(HUAWEI, {'com.termux'}, False)
    keys = [k for k, *_ in steps]
    assert keys == ['background', 'power', 'recents', 'widget', 'overlay', 'try'], 'no phantom killer before 12'
    _, title, lines, screens, _ = steps[0]
    assert 'App launch' in title and screens == [mobilize.HUAWEI_APP_LAUNCH, mobilize.APP_DETAILS]
    assert any('Manage all automatically" ON' in l for l in lines)
    assert 'Install Termux:Widget' in steps[3][2][0]
    assert steps[4][3] == [mobilize.OVERLAY] and steps[-1][4] is False


def test_android_steps_for_a_samsung_on_android_14():
    steps = mobilize.android_steps({'maker': 'samsung', 'model': 'x', 'release': '14', 'emui': ''},
                                   {'com.termux', 'com.termux.widget'}, True)
    assert [k for k, *_ in steps][:3] == ['background', 'phantom', 'power']
    assert any('Sleeping apps' in l for l in steps[0][2])
    assert 'is installed' in steps[4][2][0] and steps[-1][4] is True
    unknown = mobilize.android_steps(HUAWEI, None, False)
    assert 'is installed' in unknown[3][2][0], 'pm could not list: say nothing false'


def test_android_documents_step_only_with_nb_and_says_what_it_knows():
    assert 'documents' not in [k for k, *_ in mobilize.android_steps(HUAWEI, {'com.termux'}, False)]
    key, title, lines, screens, done = mobilize.android_steps(HUAWEI, {'com.termux'}, False, ['mobile'])[-1]
    assert key == 'documents' and done is False and screens == []
    assert any('Install Fossify Documents' in l for l in lines) and any('.nb/mobile' in l for l in lines)
    *_, lines, _, done = mobilize.documents_step({'org.fossify.documents.debug'}, [])
    assert done is None, 'the folder grant is the app\'s own: mobilize cannot see it'
    assert 'The fork is installed' in lines[0] and any(l.endswith('.nb → "Use this folder" → Allow. Termux shows up there by itself.') for l in lines)
    *_, lines, _, done = mobilize.documents_step({'org.fossify.documents'}, ['mobile'])
    assert 'Fossify Documents is installed' in lines[0] and not any('status above' in l for l in lines)
    *_, lines, _, done = mobilize.documents_step(None, ['mobile'])
    assert done is None and not any('installed' in l for l in lines), 'pm could not list: say nothing false'


def test_shared_notebooks_are_the_ones_with_a_remote(tmp_path):
    for name, remote in (('home', False), ('mobile', True), ('old', True)):
        d = tmp_path / name
        subprocess.run(['git', 'init', '-q', str(d)], check=True)
        if remote:
            subprocess.run(['git', '-C', str(d), 'remote', 'add', 'origin', 'git@x:y.git'], check=True)
    (tmp_path / 'old' / '.archived').write_text('')
    (tmp_path / 'plain').mkdir()
    assert mobilize.shared_notebooks(tmp_path) == ['mobile']
    assert mobilize.shared_notebooks(tmp_path / 'missing') == []


def _android_env(monkeypatch, am_output):
    monkeypatch.setenv('TERMUX_VERSION', '0.118')
    monkeypatch.setattr(mobilize, 'android_info', lambda: HUAWEI)
    monkeypatch.setattr(mobilize, 'android_packages', lambda: {'com.termux', 'com.termux.widget'})
    monkeypatch.setattr(mobilize, 'web_up', lambda port=5000: False)
    monkeypatch.setattr(mobilize, 'station_notebooks', lambda: None)
    monkeypatch.setattr(mobilize.sys.stdin, 'isatty', lambda: True)
    calls = []
    def fake_run(argv, env=None, **kw):
        calls.append(argv)
        out = am_output(argv) if argv[:2] == ['am', 'start'] else ''
        return subprocess.CompletedProcess(argv, 0, out, '')
    monkeypatch.setattr(mobilize, 'run', fake_run)
    return calls


def test_android_walk_opens_each_screen_and_falls_back_when_one_is_missing(home, monkeypatch):
    def am(argv):
        if 'com.huawei.systemmanager/.startupmgr.ui.StartupNormalAppListActivity' in argv:
            return 'Error: Activity class {…} does not exist.'
        return 'Starting: Intent { … }'
    calls = _android_env(monkeypatch, am)
    answers = iter(['', '', '', '', '', '', '', ''])      # open the screen, then Enter, at each step
    out = []
    assert mobilize.android(out=out.append, ask=lambda prompt: next(answers)) == 0
    opened = [c[2:] for c in calls if c[:2] == ['am', 'start']]
    assert opened == [mobilize.HUAWEI_APP_LAUNCH, mobilize.APP_DETAILS, mobilize.OVERLAY]
    assert 'Android 10 (EmotionUI_10.0.0)' in out[0]


def test_android_walk_stops_on_q_and_list_asks_nothing(home, monkeypatch):
    calls = _android_env(monkeypatch, lambda argv: 'Starting')
    assert mobilize.android(out=lambda *a: None, ask=lambda prompt: 'q') == 0
    assert not [c for c in calls if c[:2] == ['am', 'start']]
    out = []
    assert mobilize.android(list_only=True, out=out.append,
                            ask=lambda prompt: pytest.fail('--list asked')) == 0
    assert any('Display over other apps' in l for l in out)


def test_android_off_termux_does_nothing(home, capsys):
    assert mobilize.android(ask=lambda prompt: pytest.fail('asked')) == 0
    assert 'Nothing to do here' in capsys.readouterr().out


# ── sshd ──────────────────────────────────────────────────────────────────────

def test_sshd_settings_and_the_check(home, remote, monkeypatch):
    init('--data', remote())
    monkeypatch.setenv('TERMUX_VERSION', '0.118')
    monkeypatch.setattr(mobilize, 'sshd_clash', lambda prefix=None: [])
    monkeypatch.setattr(mobilize.shutil, 'which', lambda name, *a, **k: '/x/' + name)
    real_run = mobilize.run
    conf = {'text': 'port 8022\npasswordauthentication yes\nkbdinteractiveauthentication no\n'}
    monkeypatch.setattr(mobilize, 'run', lambda argv, env=None, **kw: (
        subprocess.CompletedProcess(argv, 0, conf['text'], '') if argv[:2] == ['sshd', '-T']
        else real_run(argv, env, **kw)))
    assert mobilize.sshd_settings()['passwordauthentication'] == 'yes'
    rows = [r for r in mobilize.checks() if r[0] == 'sshd']
    assert rows == [('sshd', mobilize.WARN, rows[0][2])] and 'passwordauthentication yes' in rows[0][2]
    conf['text'] = 'passwordauthentication no\nkbdinteractiveauthentication no\n'
    assert [r for r in mobilize.checks() if r[0] == 'sshd'] == [('sshd', mobilize.OK, 'keys only')]


def test_sshd_clash_finds_a_hand_started_sshd_beside_the_service(tmp_path, monkeypatch):
    svc = tmp_path / 'var' / 'service' / 'sshd'
    (svc / 'supervise').mkdir(parents=True)
    (svc / 'supervise' / 'pid').write_text('10\n')
    monkeypatch.setattr(mobilize, 'run', lambda argv, env=None, **kw:
                        subprocess.CompletedProcess(argv, 0, '10\n11\n', ''))
    assert mobilize.sshd_clash(tmp_path) == ['11']
    (svc / 'down').write_text('')
    assert mobilize.sshd_clash(tmp_path) == [], 'service off (down file): a hand-started sshd is fine'
    assert mobilize.sshd_clash(tmp_path / 'nowhere') == []


def test_check_warns_when_the_running_server_lacks_termux_exec(home, remote, monkeypatch):
    init('--data', remote())
    monkeypatch.setenv('TERMUX_VERSION', '0.118')
    web = home / 'tw-web'
    web.mkdir()
    (web / 'app.py').write_text('')
    monkeypatch.setattr(mobilize, 'web_dir', lambda s=None: web)
    monkeypatch.setattr(mobilize, 'web_up', lambda port=5000: True)
    monkeypatch.setattr(mobilize, 'sshd_settings', lambda: {})
    monkeypatch.setattr(mobilize, 'sshd_clash', lambda prefix=None: [])
    (home / '.shortcuts').mkdir()
    (home / '.shortcuts' / 'tw-web').write_text(mobilize.launcher_text(web, mobilize.web_python(web)))
    monkeypatch.setattr(mobilize, 'server_environ', lambda pidfile=None: {'PATH': '/usr/bin'})
    assert any(n == 'tw-web' and s == mobilize.WARN and 'LD_PRELOAD' in d for n, s, d in mobilize.checks())
    monkeypatch.setattr(mobilize, 'server_environ', lambda pidfile=None: {'LD_PRELOAD': '/p/lib.so'})
    assert not any('LD_PRELOAD' in d for n, s, d in mobilize.checks())


# ── tw-web comes from tw -I tw-web (0.8.2) ───────────────────────────────────

def _web_init(notes):
    init = mobilize.Init.__new__(mobilize.Init)
    init.dry, init.web, init.worst, init.out = False, True, mobilize.OK, lambda *a: None
    init.note = lambda status, step, detail: notes.append((status, detail))
    init.act = lambda step, detail, fn: (notes.append(('act', detail)), fn(), True)[2]
    return init


def test_step_web_runs_tw_install_tw_web(home, monkeypatch):
    web = home / '.local' / 'share' / 'tw-web'
    monkeypatch.setattr(mobilize, 'web_dir', lambda s=None: web)
    tw = home / '.task' / 'scripts' / 'tw'
    tw.parent.mkdir(parents=True)
    log = home / 'tw.log'
    tw.write_text(f'#!/bin/sh\necho "$*" >> {log}\nmkdir -p {web} && touch {web}/app.py\n')
    tw.chmod(0o755)
    notes = []
    assert _web_init(notes).step_web()
    assert log.read_text() == '-I tw-web\n'
    assert ('act', f'tw -I tw-web (into {web})') in notes


def test_step_web_leaves_a_working_tw_web_alone(home, monkeypatch):
    web = home / 'dev' / 'tw-web'
    web.mkdir(parents=True)
    (web / 'app.py').write_text('')
    monkeypatch.setattr(mobilize, 'web_dir', lambda s=None: web)
    monkeypatch.setattr(mobilize, 'run', lambda argv, **k: subprocess.CompletedProcess(argv, 0, '', ''))
    notes = []
    assert _web_init(notes).step_web()
    assert notes == [(mobilize.OK, f'{web} (its Python packages import)')]


def test_step_web_fails_when_tw_does(home, monkeypatch):
    monkeypatch.setattr(mobilize, 'web_dir', lambda s=None: home / 'nope')
    tw = home / '.task' / 'scripts' / 'tw'
    tw.parent.mkdir(parents=True)
    tw.write_text('#!/bin/sh\nexit 1\n')
    tw.chmod(0o755)
    with pytest.raises(SystemExit, match='tw -I tw-web failed'):
        _web_init([]).step_web()
