"""Unprivileged supervisor for the institution selected for isolation."""
import fcntl
import json
import logging
from logging.handlers import RotatingFileHandler
import os
from pathlib import Path
import signal
import socket
import subprocess
import threading
import time
from urllib.parse import urlsplit
import profiles

from version import VERSION
import connections
import container_support

SCHOOL = 'a'
GATEWAY = ''
GATEWAY_PORT = 443

SUPPORT = Path.home() / 'Library/Application Support/ATrustDual'
STATUS = SUPPORT / 'status.json'
PREFERENCES = SUPPORT / 'preferences.json'
CONFIG = profiles.CONFIG
COLIMA = '/opt/homebrew/bin/colima'
DOCKER = '/opt/homebrew/bin/docker'
PROFILE = 'atrust-split-desktop'
CONTEXT = 'colima-atrust-split-desktop'
CONTAINER = 'atrust-a'
SOCKS_PORT = 10811
WEB_PORT = 18081
LABEL = 'local.atrust-split.connections'


def use_school(school):
    global SCHOOL, CONTAINER, SOCKS_PORT, WEB_PORT, GATEWAY, GATEWAY_PORT
    config = connections.SCHOOLS[school]
    SCHOOL = school
    CONTAINER, SOCKS_PORT, WEB_PORT = config['container'], config['socks_port'], config['web_port']
    gateway = urlsplit(config['gateway'])
    GATEWAY = gateway.hostname or ''
    GATEWAY_PORT = gateway.port or 443


def atomic_json(path, value):
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    temp = path.with_suffix('.tmp-' + str(os.getpid()))
    temp.write_text(json.dumps(value, ensure_ascii=False) + '\n')
    os.chmod(temp, 0o600)
    temp.replace(path)


def run(args, timeout=20, check=True):
    env = dict(os.environ, PATH='/opt/homebrew/bin:/usr/bin:/bin:/usr/sbin:/sbin')
    p = subprocess.run(args, capture_output=True, text=True, timeout=timeout, env=env)
    if check and p.returncode:
        raise RuntimeError(p.stderr.strip()[-300:] or f'{Path(args[0]).name} exited {p.returncode}')
    return p


def docker(*args, **kwargs):
    return run([DOCKER, '--context', CONTEXT, *args], **kwargs)




def tunnel_routed(output):
    # aTrust keeps its utun interface after logout; only the IPv4 routes it
    # installs on login show that the 隔离机构 session is actually established.
    return any(' dev utun' in line and line.split()[0] not in ('local', 'broadcast')
               for line in output.splitlines())


def set_blackhole(enabled):
    # Only explicit configured targets are blocked while no tunnel exists.
    targets = connections.SCHOOLS[SCHOOL]['fixed_ips']
    if not targets:
        return
    verb = 'replace' if enabled else 'del'
    script = '; '.join(f'ip route {verb} unreachable {ip}/32 metric 4000' for ip in targets)
    docker('exec', CONTAINER, 'sh', '-c', script + (' ' if enabled else '; true'), timeout=10, check=enabled)


def stop_browser():
    # Chromium only exists to complete the web -> client handoff. Once the
    # tunnel is proven up it is dead weight (several hundred MB inside the VM);
    # a later login reopens it through xdg-open. aTrust itself is never touched.
    result = docker('exec', CONTAINER, 'pkill', '-f', '^/usr/lib/chromium/chromium ',
                    timeout=10, check=False)
    if result.returncode == 0:
        logging.info('隔离机构 已连接，关闭容器内登录用的浏览器')
        return True
    return False


def memory_mb():
    try:
        usage = int(docker('exec', CONTAINER, 'cat',
                           '/sys/fs/cgroup/memory.current', timeout=8).stdout.strip())
    except (OSError, ValueError, subprocess.TimeoutExpired, RuntimeError):
        return 0
    return round(usage / 2**20)


def tunnel_linked(output):
    # After a VM restart aTrust restores its interface and routes before it has
    # re-authenticated, so routes alone can describe a dead session. Only its
    # own established :441 connection shows the tunnel is really carrying data.
    for line in output.splitlines():
        parts = line.split()
        if parts and parts[0] == 'ESTAB' and len(parts) > 4:
            peers = [field for field in parts[3:5] if field.endswith(':441')]
            if peers and 'aTrustXtunnel' in parts[-1]:
                return True
    return False


def session_state():
    """Classify the selected container's authenticated session."""
    address = docker('exec', CONTAINER, 'cat', '/usr/share/sangfor/.aTrust/var/conf/addr.conf', check=False).stdout.strip()
    if not container_support.gateway_matches(address, SCHOOL):
        return 'login-required'
    routes = docker('exec', CONTAINER, 'ip', '-4', 'route', 'show', 'table', 'all', timeout=10)
    if tunnel_routed(routes.stdout):
        # Blocks only matter while there is no tunnel at all; while a stale
        # session is being re-authenticated they must stay out of the way.
        if 'unreachable' in routes.stdout:
            set_blackhole(False)
        links = docker('exec', CONTAINER, 'ss', '-Htanp', timeout=10)
        if tunnel_linked(links.stdout):
            return 'tunnel-up'
        # Routes without a live :441 link: either restored routes awaiting
        # re-authentication or a session whose lines are all out. The
        # supervisor tells them apart by whether this container ran a tunnel.
        return 'link-down'
    set_blackhole(True)
    # Network reachability is checked from the Mac: the VM's outbound path can
    # stall for seconds on a first SYN, which would misreport a logout as offline.
    try:
        socket.create_connection((GATEWAY, GATEWAY_PORT), timeout=5).close()
    except OSError:
        return 'offline'
    return 'login-required'


class Supervisor:
    def __init__(self):
        self.stop = threading.Event()
        self.connected = False
        self.last_probe = None
        self.last_phase = None
        self.last_error = ''
        self.enabled = True
        self.failures = 0
        self.last_success_at = None
        self.last_checked_at = None
        self.check_request = None
        self.session = ''
        self.browser_closed = False
        self.started = None         # container StartedAt, changes on every restart
        self.linked_start = None    # StartedAt of the run that last had a live tunnel
        self.routed = False         # aTrust routes present, whether or not a :441 link is up
        try:
            # Survives a supervisor restart (e.g. an upgrade) while the container keeps running.
            self.linked_start = json.loads(STATUS.read_text()).get('linked_start')
        except (OSError, ValueError, AttributeError):
            pass
        self.link_down_since = None

    def publish(self, phase, message, enabled=None):
        school = connections.SCHOOLS[SCHOOL]
        message = message.replace('隔离机构', school['name']).replace('业务目标', school['probe']['name'])
        atomic_json(STATUS, {'version': VERSION, 'phase': phase, 'message': message,
                    'enabled': self.enabled if enabled is None else enabled,
                    'updated_at': time.time(), 'pid': os.getpid(),
                    'gateway': GATEWAY, 'school': SCHOOL, 'socks_port': SOCKS_PORT,
                    'web_port': WEB_PORT, 'last_error': self.last_error,
                    'last_checked_at': self.last_checked_at,
                    'last_success_at': self.last_success_at,
                    'consecutive_failures': self.failures,
                    'session': self.session, 'linked_start': self.linked_start, 'memory_mb': memory_mb() if self.enabled else 0})
        if self.last_phase != phase:
            logging.info('%s: %s', phase, message)
            self.last_phase = phase

    def refresh_session(self):
        try:
            session = session_state()
            self.routed = session in ('tunnel-up', 'link-down')
            if session == 'tunnel-up':
                self.linked_start = self.started
            elif session == 'link-down':
                # Only a container that already carried this session is merely
                # reconnecting; after a restart aTrust must re-authenticate.
                session = 'reconnecting' if self.linked_start == self.started else 'login-required'
            self.session = session
        except (OSError, RuntimeError, subprocess.TimeoutExpired) as error:
            self.session = ''
            self.routed = False
            logging.warning('无法判断 隔离机构 登录状态：%s', type(error).__name__)

    def tick(self, enabled=None):
        if not CONFIG.exists():
            self.publish('not-configured', '隔离机构 尚未安装', False)
            return
        profiles.require_configured(profiles.load_profiles())
        prefs = json.loads(PREFERENCES.read_text()) if PREFERENCES.exists() else {'enabled': True}
        self.enabled = prefs.get('enabled', True) if enabled is None else enabled
        if prefs.get('check_requested_at') != self.check_request:
            self.check_request = prefs.get('check_requested_at')
            self.last_probe = None
        if not self.enabled:
            if self.last_phase != 'paused':
                self.publish('stopping', '正在停止 隔离机构', False)
                run([COLIMA, 'stop', '--profile', PROFILE], timeout=90)
            self.connected = False
            self.last_probe = None
            self.failures = 0
            self.last_error = ''
            self.session = ''
            self.browser_closed = False
            self.publish('paused', '隔离机构 已停止', False)
            return
        info = run([COLIMA, 'status', '--profile', PROFILE, '--json'], check=False)
        if info.returncode:
            self.publish('starting', '正在启动 隔离机构')
            run([COLIMA, 'start', '--profile', PROFILE, '--activate=false', '--ssh-config=false'], timeout=180)
        result = docker('inspect', '--format', '{{.State.Running}} {{.State.StartedAt}}', CONTAINER, check=False)
        if result.returncode:
            raise RuntimeError('无法读取 隔离机构 容器状态，请检查 Colima')
        running, _, self.started = result.stdout.strip().partition(' ')
        if running != 'true':
            self.publish('starting', '正在启动 隔离机构 aTrust')
            docker('start', CONTAINER, timeout=30)
            self.last_probe = None
        if self.last_probe is None or time.monotonic() - self.last_probe >= (60 if self.connected else 10):
            self.refresh_session()
            if not self.routed:
                self.connected = False
            # aTrust opens its :441 link on demand, so an idle session shows
            # routes but no link; only real traffic can tell it from a dead one.
            if self.routed:
                try:
                    container_support.probe(SCHOOL)
                    self.connected = True
                    self.failures = 0
                    self.last_success_at = time.time()
                    self.last_error = ''
                    self.session = 'tunnel-up'
                    self.linked_start = self.started
                    if not self.browser_closed:
                        self.browser_closed = stop_browser()
                except (OSError, ConnectionError) as e:
                    self.connected = False
                    self.failures += 1
                    self.last_error = type(e).__name__
                    self.refresh_session()
            else:
                self.browser_closed = False
                # Without an authenticated tunnel a probe would only stall in
                # the VM's user-mode network.
                self.failures += 1
                self.last_error = 'no ' + SCHOOL.upper() + ' tunnel'
            self.last_checked_at = time.time()
            self.last_probe = time.monotonic()
        if self.session != 'reconnecting':
            self.link_down_since = None
        elif self.link_down_since is None:
            self.link_down_since = time.monotonic()
        if self.connected:
            self.publish('connected', '隔离机构 已连接 · 业务目标 可达')
        elif self.session == 'login-required' and self.failures >= 2:
            self.publish('login-required', '隔离机构 未登录，请在 隔离机构 登录界面输入密码')
        elif self.session == 'reconnecting' and self.failures >= 2:
            if time.monotonic() - self.link_down_since < 600:
                self.publish('reconnecting', '隔离机构 线路中断，aTrust 正在自动重连')
            else:
                self.publish('reconnecting', '隔离机构 线路中断已超过 10 分钟；若仍未恢复，请重新登录 隔离机构')
        elif self.session == 'offline' and self.failures >= 2:
            self.publish('offline', '无法连接 配置的登录网关，等待网络恢复')
        elif self.failures < 3:
            self.publish('checking', '业务目标 暂未响应，正在复查')
        else:
            self.publish('unreachable', '业务目标 暂不可达，请检查网络或 隔离机构 登录状态')

    def serve(self):
        SUPPORT.mkdir(mode=0o700, parents=True, exist_ok=True)
        lock = open(SUPPORT / 'supervisor.lock', 'w')
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        handler = RotatingFileHandler(SUPPORT / 'service.log', maxBytes=1048576, backupCount=2)
        handler.setFormatter(logging.Formatter('%(asctime)s %(levelname)s %(message)s'))
        logging.basicConfig(level=logging.INFO, handlers=[handler])
        for sig in (signal.SIGTERM, signal.SIGINT):
            signal.signal(sig, lambda *_: self.stop.set())
        while not self.stop.is_set():
            try:
                self.tick()
            except Exception as e:
                self.last_error = f'{type(e).__name__}: {str(e)[:300]}'
                logging.exception('隔离机构 supervisor will retry')
                self.publish('retrying', '隔离机构 环境暂不可用，将自动重试')
            self.stop.wait(10)
        self.publish('stopped', '隔离机构 后台监控已停止')


def main(action):
    if os.geteuid() == 0:
        raise RuntimeError('隔离机构 环境应由当前用户运行，无需 sudo')
    if action == 'dual-serve':
        from connection_manager import Manager
        Manager().serve()
    elif action == 'dual-status':
        print(STATUS.read_text() if STATUS.exists() else '{"phase":"not-configured"}')
    elif action in ('dual-start', 'dual-stop', 'dual-check'):
        prefs = json.loads(PREFERENCES.read_text()) if PREFERENCES.exists() else {'enabled': True}
        if action != 'dual-check':
            prefs['enabled'] = action == 'dual-start'
        prefs['check_requested_at'] = time.time_ns()
        atomic_json(PREFERENCES, prefs)
        print('隔离机构 preference updated')
    else:
        raise ValueError('Unknown 隔离机构 action')
