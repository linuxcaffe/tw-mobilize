"""mobilize: every test runs in a fake home folder with a throwaway gittw "remote" (a bare repo
on disk), so nothing touches ~/.task, ~/.ssh, ~/.bashrc or the network. The data URL is a
plain path, so the SSH key and access steps are skipped; Termux is never detected."""
import importlib.util
import json
import os
import subprocess
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
    assert 'if ! curl -s -o /dev/null http://127.0.0.1:5000/' in text
    assert 'cd /x/tw-web && nohup /usr/bin/python app.py' in text
    assert text.rstrip().endswith('termux-open-url http://localhost:5000/')


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
