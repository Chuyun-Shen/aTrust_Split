import json
import os
from pathlib import Path
import plistlib
import pwd
import re
import shlex
import shutil
import signal
import subprocess
import time
import uuid

import router_core as core
import profiles

APP_TARGET = Path('/Applications/ATrust Split.app')
BUNDLE_ID = 'io.github.chuyun-shen.atrust-split'
DAEMON_LABEL = 'local.atrust-split.service'
AGENT_LABEL = 'local.atrust-split.menubar'
CONNECTION_LABEL = 'local.atrust-split.connections'
DAEMON = Path('/Library/LaunchDaemons') / (DAEMON_LABEL + '.plist')
SUPPORT = Path('/Library/Application Support/ATrustSplit')


def run(args, check=True, timeout=30):
    p = subprocess.run(args, text=True, capture_output=True, timeout=timeout)
    if check and p.returncode:
        raise RuntimeError(p.stderr.strip()[:500] or f'{args[0]} failed')
    return p


def job_info(target):
    result = run(['/bin/launchctl', 'print', target], False)
    if result.returncode == 0:
        return result.stdout
    if result.returncode in (3, 113):
        return None
    raise RuntimeError(f'无法查询 {target}: {result.stderr.strip()[:500]}')


def stop_job(target, timeout=40):
    """bootout returns before SIGTERM cleanup and removal have completed."""
    info = job_info(target)
    if info is None:
        return
    match = re.search(r'^\s*pid = (\d+)\s*$', info, re.M)
    pid = int(match[1]) if match else None
    result = run(['/bin/launchctl', 'bootout', target], False)
    deadline = time.monotonic() + timeout
    while True:
        alive = False
        if pid:
            try:
                os.kill(pid, 0)
                alive = True
            except ProcessLookupError:
                pass
        if job_info(target) is None and not alive:
            return
        if time.monotonic() >= deadline:
            raise RuntimeError(f'{target} 未完全退出，未替换应用文件。{result.stderr.strip()[:300]}')
        time.sleep(0.2)


def start_job(domain, path, timeout=12):
    label = plistlib.loads(path.read_bytes())['Label']
    target = f'{domain}/{label}'
    deadline = time.monotonic() + timeout
    while True:
        result = run(['/bin/launchctl', 'bootstrap', domain, str(path)], False)
        info = job_info(target)
        if info is not None and re.search(r'^\s*path = ' + re.escape(str(path)) + r'\s*$', info, re.M):
            return
        if result.returncode not in (5, 37) or time.monotonic() >= deadline:
            raise RuntimeError(f'无法注册 {target}: {result.stderr.strip()[:500] or "服务未出现在 launchd 中"}')
        time.sleep(0.5)


def wait_worker(version, timeout=25):
    import service
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            response = service.control({'command': 'status'})
            status = response.get('status', {})
            info = job_info('system/' + DAEMON_LABEL) or ''
            pid = re.search(r'^\s*pid = (\d+)\s*$', info, re.M)
            if response.get('ok') and status.get('version') == version and pid and \
                    status.get('pid') == int(pid[1]) and status.get('phase') not in (None, 'stopped'):
                return
        except (OSError, ValueError):
            pass
        time.sleep(0.25)
    raise RuntimeError('后台进程未通过启动检查；详情见 /Library/Logs/ATrustSplit/service.log')


def snapshot_files(paths):
    result = {}
    for path in paths:
        stat = path.stat() if path.exists() else None
        result[path] = (path.read_bytes(), stat.st_uid, stat.st_gid, stat.st_mode & 0o777) if stat else None
    return result


def restore_files(snapshot):
    for path, saved in snapshot.items():
        if saved is None:
            path.unlink(missing_ok=True)
        else:
            data, uid, gid, mode = saved
            path.write_bytes(data)
            os.chown(path, uid, gid)
            os.chmod(path, mode)


def recover_install(backup, snapshot, uid, home):
    """Keep the failed bundle for diagnosis, then put back the prior install."""
    for target in (f'gui/{uid}/{CONNECTION_LABEL}', f'gui/{uid}/{AGENT_LABEL}', 'system/' + DAEMON_LABEL):
        stop_job(target)
    if core.STATE.exists():
        core.restore(json.loads(core.STATE.read_text()))
    if backup:
        if APP_TARGET.exists():
            APP_TARGET.rename(backup.with_name(backup.stem + '-failed-' + uuid.uuid4().hex[:8] + '.app'))
        backup.rename(APP_TARGET)
    restore_files(snapshot)
    if DAEMON.exists():
        start_job('system', DAEMON)
    for label in (CONNECTION_LABEL, AGENT_LABEL):
        path = home / 'Library/LaunchAgents' / (label + '.plist')
        if path.exists():
            start_job(f'gui/{uid}', path)


def make_root_owned(path):
    for parent, dirs, files in os.walk(path):
        os.chown(parent, 0, 0)
        os.chmod(parent, 0o755)
        for name in files + dirs:
            p = Path(parent) / name
            os.chown(p, 0, 0, follow_symlinks=False)
            if not p.is_symlink() and p.is_file():
                os.chmod(p, 0o755 if p.stat().st_mode & 0o111 else 0o644)


def legacy_workers(home):
    script = str(home / 'Downloads/atrust-split/atrust_split.py')
    matches = []
    for line in run(['/bin/ps', '-axo', 'pid=,command=']).stdout.splitlines():
        pieces = line.strip().split(None, 1)
        if len(pieces) != 2:
            continue
        try:
            args = shlex.split(pieces[1])
        except ValueError:
            continue
        if args and Path(args[0]).name.startswith('python') and script in args and 'run' in args:
            matches.append(int(pieces[0]))
    return matches


def stop_legacy(home):
    pids = legacy_workers(home)
    if not pids:
        if Path('/var/db/local-atrust-split/state.json').exists():
            raise RuntimeError('旧版恢复日志仍存在。请先运行旧版 restore.command，再安装。')
        return
    print('正在停止旧前台分流，并等待它恢复原始配置…', flush=True)
    for pid in pids:
        os.kill(pid, signal.SIGTERM)
    deadline = time.monotonic() + 25
    while time.monotonic() < deadline:
        if not legacy_workers(home):
            break
        time.sleep(0.5)
    else:
        raise RuntimeError('旧版分流未正常退出；已停止安装，未强制结束进程。')
    if Path('/var/db/local-atrust-split/state.json').exists():
        raise RuntimeError('旧版恢复尚未完成，请先运行旧版 restore.command。')


def write_user_plist(home, label, payload):
    if os.geteuid() == 0:
        raise RuntimeError('User startup files must be written without root privileges')
    folder = home / 'Library/LaunchAgents'
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / (label + '.plist')
    temporary = path.with_name(path.name + '.tmp-' + uuid.uuid4().hex)
    with temporary.open('xb') as stream:
        stream.write(plistlib.dumps(payload))
    os.chmod(temporary, 0o644)
    temporary.replace(path)
    return path


def initialize_user():
    """Run as the installing user; root never writes/chowns user-controlled paths."""
    import connections
    uid = os.geteuid()
    if uid == 0:
        raise RuntimeError('User setup must run as the logged-in user')
    home = Path(pwd.getpwuid(uid).pw_dir)
    folder = home / 'Library/Application Support/ATrustDual'
    folder.mkdir(mode=0o700, parents=True, exist_ok=True)
    path = folder / 'connections.json'
    if path.exists():
        connections.validate(json.loads(path.read_text()))
    else:
        connections.atomic_json(path, connections.DEFAULT)
    write_user_plist(home, AGENT_LABEL, {
        'Label': AGENT_LABEL,
        'ProgramArguments': [str(APP_TARGET / 'Contents/MacOS/ATrustSplit')],
        'RunAtLoad': True, 'ProcessType': 'Interactive'})
    write_user_plist(home, CONNECTION_LABEL, {
        'Label': CONNECTION_LABEL,
        'ProgramArguments': [str(APP_TARGET / 'Contents/Resources/runtime/atrust-splitd'), 'dual-serve'],
        'RunAtLoad': True, 'KeepAlive': True, 'ThrottleInterval': 15,
        'ProcessType': 'Background',
        'EnvironmentVariables': {'PATH': '/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin'},
        'StandardOutPath': str(folder / 'launchd.log'),
        'StandardErrorPath': str(folder / 'launchd.log')})


def run_as_user(uid, *arguments, timeout=30):
    return run(['/bin/launchctl', 'asuser', str(uid), '/usr/bin/sudo', '-u',
                pwd.getpwuid(uid).pw_name, '-H', *arguments], timeout=timeout)


def install(args, uid, home):
    import service
    settings_path = getattr(args, 'settings', None)
    if settings_path:
        config = {'version': 1, 'owner_uid': uid, 'profiles': profiles.load_profiles(settings_path)}
    elif service.CONFIG.exists():
        config = json.loads(service.CONFIG.read_text())
        config['owner_uid'] = uid
    else:
        raise RuntimeError('首次安装需要 --settings /absolute/path/to/config.local.json')
    profiles.require_configured(config['profiles'])
    service.validate_config(config)
    source = Path(args.app or '').resolve()
    info_path = source / 'Contents/Info.plist'
    if not info_path.is_file() or plistlib.loads(info_path.read_bytes()).get('CFBundleIdentifier') != BUNDLE_ID:
        raise RuntimeError('安装来源不是 ATrust Split 应用包')
    binary = source / 'Contents/Resources/runtime/atrust-splitd'
    if not binary.is_file():
        raise RuntimeError('后台程序缺失')
    SUPPORT.mkdir(mode=0o755, parents=True, exist_ok=True)
    stage = None
    if source != APP_TARGET:
        if APP_TARGET.exists():
            old = plistlib.loads((APP_TARGET / 'Contents/Info.plist').read_bytes())
            if old.get('CFBundleIdentifier') != BUNDLE_ID:
                raise RuntimeError('Applications 中已有不同的同名应用，未覆盖')
        stage = APP_TARGET.with_name('.ATrustSplit-staging-' + uuid.uuid4().hex + '.app')
        shutil.copytree(source, stage, symlinks=True)
        make_root_owned(stage)
        run(['/usr/bin/codesign', '--verify', '--deep', '--strict', str(stage)])
    stop_legacy(home)
    print('正在等待旧服务完成退出和网络配置恢复…', flush=True)
    stop_job(f'gui/{uid}/{CONNECTION_LABEL}')
    stop_job(f'gui/{uid}/{AGENT_LABEL}')
    stop_job('system/' + DAEMON_LABEL)
    # If an earlier service stopped with a pending recovery journal, restore it first.
    core.CLASH_CONFIG = Path(f'/Library/Application Support/clash-verge-service/users/{uid}/runtime/config.yaml')
    core.CLASH_SOCKET = f'/var/run/clash-verge-service/users/{uid}/verge-mihomo.sock'
    if core.STATE.exists():
        core.restore(json.loads(core.STATE.read_text()))
    import transparent_route
    import proxy_routing
    transparent_route.Bridge().stop()
    proxy_routing.Overlay().restore()
    snapshot = snapshot_files([DAEMON, service.CONFIG])
    backup = None
    try:
        if stage:
            if APP_TARGET.exists():
                folder = SUPPORT / 'Backups'
                folder.mkdir(exist_ok=True)
                destination = folder / ('ATrust Split-' + time.strftime('%Y%m%d-%H%M%S') + '-' + uuid.uuid4().hex[:8] + '.app')
                APP_TARGET.rename(destination)
                backup = destination
            stage.rename(APP_TARGET)
        configure_daemon(uid, config)
        start_job('system', DAEMON)
        wait_worker(service.VERSION)
    except Exception as error:
        print(f'安装未完成：{error}\n正在恢复升级前的服务…', flush=True)
        try:
            recover_install(backup, snapshot, uid, home)
        except Exception as recovery_error:
            raise RuntimeError(f'{error}\n恢复服务也未完成：{recovery_error}') from error
        raise RuntimeError(f'{error}\n已还原升级前的应用和服务配置。') from error
    run_as_user(uid, str(APP_TARGET / 'Contents/Resources/runtime/atrust-splitd'), 'user-setup')
    for label in (CONNECTION_LABEL, AGENT_LABEL):
        agent = home / 'Library/LaunchAgents' / (label + '.plist')
        try:
            start_job(f'gui/{uid}', agent)
        except RuntimeError as error:
            raise RuntimeError(f'后台已安装，但用户启动项未启动：{error}') from error
    print('安装完成。后台服务已通过启动检查，可以关闭终端。', flush=True)
    print('菜单栏应用：/Applications/ATrust Split.app', flush=True)


def configure_daemon(uid, config):
    import service
    make_root_owned(APP_TARGET)
    service.CONFIG.write_text(json.dumps(config, ensure_ascii=False, indent=2) + '\n')
    os.chmod(service.CONFIG, 0o644)
    os.chown(service.CONFIG, 0, 0)
    service.LOG_DIR.mkdir(mode=0o755, parents=True, exist_ok=True)
    daemon_payload = {
        'Label': DAEMON_LABEL,
        'ProgramArguments': [str(APP_TARGET / 'Contents/Resources/runtime/atrust-splitd'), 'serve'],
        'RunAtLoad': True, 'KeepAlive': True, 'ThrottleInterval': 15, 'ExitTimeOut': 30,
        'ProcessType': 'Background', 'Umask': 0o022,
        'WorkingDirectory': str(SUPPORT),
        'EnvironmentVariables': {'PATH': '/usr/bin:/bin:/usr/sbin:/sbin', 'LC_ALL': 'C', 'LANG': 'C'},
        'StandardOutPath': str(service.LOG_DIR / 'launchd.log'),
        'StandardErrorPath': str(service.LOG_DIR / 'launchd.log'),
    }
    DAEMON.write_bytes(plistlib.dumps(daemon_payload))
    os.chown(DAEMON, 0, 0)
    os.chmod(DAEMON, 0o644)
    run(['/usr/bin/plutil', '-lint', str(DAEMON)])


def uninstall(uid, home):
    stop_job(f'gui/{uid}/{CONNECTION_LABEL}')
    colima = shutil.which('colima', path='/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin')
    if colima:
        run_as_user(uid, colima, 'stop', '--profile', 'atrust-split-desktop', timeout=100)
    stop_job('system/' + DAEMON_LABEL)
    core.CLASH_CONFIG = Path(f'/Library/Application Support/clash-verge-service/users/{uid}/runtime/config.yaml')
    core.CLASH_SOCKET = f'/var/run/clash-verge-service/users/{uid}/verge-mihomo.sock'
    if core.STATE.exists():
        core.restore(json.loads(core.STATE.read_text()))
    import transparent_route
    import proxy_routing
    transparent_route.Bridge().stop()
    proxy_routing.Overlay().restore()
    stop_job(f'gui/{uid}/{AGENT_LABEL}')
    disabled = SUPPORT / 'Disabled'
    disabled.mkdir(parents=True, exist_ok=True)
    for path in [DAEMON, home / 'Library/LaunchAgents' / (AGENT_LABEL + '.plist'),
                 home / 'Library/LaunchAgents' / (CONNECTION_LABEL + '.plist')]:
        if path.exists():
            dest = disabled / (time.strftime('%Y%m%d-%H%M%S-') + path.name)
            path.rename(dest)
    print('后台服务和菜单栏自启动已卸载；网络配置已恢复。应用、配置和日志保留，方便重新安装。')


def main(args):
    if os.geteuid() != 0:
        raise RuntimeError('请运行安装/卸载 command 文件，在本机 sudo 提示中输入管理员密码')
    uid = args.uid or int(os.environ.get('SUDO_UID', '0'))
    account = pwd.getpwuid(uid)
    home = Path(account.pw_dir)
    if uid < 501 or (args.home and Path(args.home).resolve() != home.resolve()):
        raise RuntimeError('无法确认登录用户')
    if args.action == 'install':
        install(args, uid, home)
    else:
        uninstall(uid, home)
