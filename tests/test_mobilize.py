"""mobilize: every test runs in a fake home folder with a throwaway gittw "remote" (a bare repo
on disk), so nothing touches ~/.task, ~/.ssh, ~/.bashrc or the network. The data URL is a
plain path, so the SSH key and access steps are skipped; Termux is never detected."""
import importlib.util
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


def test_taskwarrior_3_gets_a_loud_warning_but_init_carries_on(home, remote, monkeypatch, capsys):
    monkeypatch.setattr(mobilize, 'task_version', lambda: '3.1.0')
    code = init('--data', remote())
    out = capsys.readouterr().out
    assert '!!! WARNING: Taskwarrior 3.1.0 is installed' in out
    assert (home / '.task' / '.git').exists(), 'not blocked'
    assert 'NOT 2.6.x' in out, 'check says so too'
    assert code == 1


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


def _android_env(monkeypatch, am_output):
    monkeypatch.setenv('TERMUX_VERSION', '0.118')
    monkeypatch.setattr(mobilize, 'android_info', lambda: HUAWEI)
    monkeypatch.setattr(mobilize, 'android_packages', lambda: {'com.termux', 'com.termux.widget'})
    monkeypatch.setattr(mobilize, 'web_up', lambda port=5000: False)
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
