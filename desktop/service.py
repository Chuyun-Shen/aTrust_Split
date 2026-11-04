#!/usr/bin/env python3
"""launchd-owned foreground worker. The menu application is an unprivileged client."""
import argparse
import fcntl
import json
import logging
from logging.handlers import RotatingFileHandler
import os
from pathlib import Path
import queue
import signal
import socket
import socketserver
import struct
import subprocess
import sys
import threading
import time

import router_core as core
from version import VERSION
import proxy_routing
import transparent_route
import profiles

LABEL = 'local.atrust-split.service'
SUPPORT = Path('/Library/Application Support/ATrustSplit')
CONFIG = profiles.CONFIG
RUNTIME = Path('/var/run/atrust-split')
SOCKET = RUNTIME / 'control.sock'
STATUS = RUNTIME / 'status.json'
PREFERENCES = core.STATE_DIR / 'preferences.json'
LOG_DIR = Path('/Library/Logs/ATrustSplit')

DEFAULT_PROFILES = profiles.EXAMPLE_PROFILES


def atomic_json(path, value, mode=0o600):
    temp = path.with_suffix('.tmp')
    temp.write_text(json.dumps(value, ensure_ascii=False))
    os.chmod(temp, mode)
    temp.replace(path)


def selected_profile(profiles, gateway, choice='auto'):
    return next((p for p in profiles if gateway in p['gateways'] and
                 choice in ('auto', p['id'])), None)


def validate_config(config):
    if not isinstance(config.get('owner_uid'), int) or config['owner_uid'] < 501:
        raise ValueError('owner_uid must be a normal macOS user')
    profiles.validate_profiles(config.get('profiles', []))
    return config


class ControlHandler(socketserver.StreamRequestHandler):
    def handle(self):
        self.connection.settimeout(2)
        try:
            line = self.rfile.readline(2049)
            if len(line) > 2048:
                raise ValueError('request too long')
            request = json.loads(line)
            command = request.get('command')
            if command not in ('status', 'pause', 'resume', 'retry', 'profile', 'connections'):
                raise ValueError('unsupported command')
            if command == 'profile' and request.get('profile') not in ('auto', 'a', 'b'):
                raise ValueError('invalid profile')
            if command == 'connections':
                native, remote = request.get('native'), request.get('container')
                if native not in (None, 'a', 'b') or remote not in (None, 'a', 'b') or (remote and (not native or native == remote)):
                    raise ValueError('invalid institution assignment')
                if type(request.get('revision')) is not int or request['revision'] < 0:
                    raise ValueError('invalid assignment revision')
            if command != 'status':
                self.server.worker.requests.put_nowait(request)
                self.server.worker.wake.set()
            response = {'ok': True, 'status': self.server.worker.status}
        except Exception as e:
            response = {'ok': False, 'error': type(e).__name__}
        self.wfile.write(json.dumps(response, ensure_ascii=False).encode() + b'\n')


class ControlServer(socketserver.ThreadingUnixStreamServer):
    daemon_threads = True


class Worker:
    def __init__(self, config):
        self.overlay = proxy_routing.Overlay()
        self.bridge = transparent_route.Bridge()
        self.container_route_error = ''
        self.config = config
        self.assignment = None
        self.pending_assignment = None
        self.routes_restored = False
        self.overlay_applied = False
        self.profiles = config['profiles']
        self.requests = queue.Queue(maxsize=32)
        self.wake = threading.Event()
        self.stop = threading.Event()
        self.split = None
        self.servers = []
        self.status = {}
        self.preferences = {'enabled': False, 'profile': 'auto'}
        if PREFERENCES.exists():
            p = json.loads(PREFERENCES.read_text())
            if isinstance(p.get('enabled'), bool) and p.get('profile') in ('auto', 'a', 'b'):
                self.preferences = p
        uid = config['owner_uid']
        core.CLASH_CONFIG = Path(f'/Library/Application Support/clash-verge-service/users/{uid}/runtime/config.yaml')
        core.CLASH_SOCKET = f'/var/run/clash-verge-service/users/{uid}/verge-mihomo.sock'
        self.retry_at = 0
        self.failures = 0
        self.last_health = 0
        self.health_failures = 0
        self.last_health_success = None
        self.clash_ready = False
        self.last_error = ''

    def publish(self, phase, message, profile=None):
        previous = (self.status.get('phase'), self.status.get('message'))
        self.status = {
            'version': VERSION, 'pid': os.getpid(), 'updated_at': time.time(),
            'phase': phase, 'message': message, 'enabled': self.preferences['enabled'],
            'selection': self.preferences['profile'],
            'container_profile': self.preferences.get('container_profile'),
            'container_route_error': self.container_route_error,
            'profile': profile['id'] if profile else None,
            'profile_name': profile['name'] if profile else None,
            'short_name': profile['short_name'] if profile else None,
            'profile_id': profile['id'] if profile else None,
            'assignment': self.assignment,
            'routes_restored': self.routes_restored,
            'gateway': core.gateway_host(), 'clash_ready': self.clash_ready,
            'managed_routes': len(self.split.state['added']) if self.split else 0,
            'tunnel': self.split.active['name'] if self.split else None,
            'last_error': self.last_error,
            'health_failures': self.health_failures,
            'last_health_success': self.last_health_success,
        }
        atomic_json(STATUS, self.status, 0o644)
        if previous != (phase, message):
            logging.info('%s: %s', phase, message)

    def cleanup(self):
        if self.split:
            with self.split.lock:
                self.split.accepting = False
        for server in self.servers:
            server.shutdown()
            server.server_close()
        self.servers = []
        if self.split:
            with self.split.lock:
                core.restore(self.split.state)
            self.split = None
        elif core.STATE.exists():
            core.restore(json.loads(core.STATE.read_text()))
        self.clash_ready = False
        self.health_failures = 0

    def activate(self, profile):
        split = core.Split(profile)
        if split.active['gateway'] not in profile['gateways']:
            raise RuntimeError('aTrust gateway changed during startup')
        response = core.upstream(core.make_query(profile['probe']), split.active['name'])
        if not core.related_addresses(response)[0]:
            raise RuntimeError('机构 DNS 预检未返回地址')
        servers, started = [], []
        try:
            # Bind both protocols before publishing a usable split. A failed
            # bind must never leave a half-installed session for the next tick.
            for cls in (core.UDP, core.TCP):
                server = cls(('127.0.0.1', core.PORT), core.Handler)
                server.split = split
                servers.append(server)
            for server in servers:
                threading.Thread(target=server.serve_forever, daemon=True).start()
                started.append(server)
            split.reconcile()
            split.answer(core.make_query(profile['probe']))
            split.setup_resolver()
        except Exception as error:
            with split.lock:
                split.accepting = False
            for server in started:
                server.shutdown()
            for server in servers:
                server.server_close()
            core.restore(split.state)
            raise RuntimeError('分流安装未完成，已回滚') from error
        self.split, self.servers = split, servers
        self.last_health = time.monotonic()
        self.health_failures = 0
        self.last_health_success = time.time()
        self.failures = 0
        self.patch_clash()

    def patch_clash(self):
        self.overlay_applied = False
        try:
            self.clash_ready = self.overlay.apply(self.split.profile if self.split else None,
                                                   self.preferences.get('container_profile'))
            self.overlay_applied = True
        except Exception as e:
            self.clash_ready = False
            logging.warning('Clash 暂不可用，将重试：%s', type(e).__name__)

    def process_requests(self):
        changed = False
        while True:
            try:
                request = self.requests.get_nowait()
            except queue.Empty:
                break
            command = request['command']
            if command in ('pause', 'resume'):
                self.preferences['enabled'] = command == 'resume'
                changed = True
            elif command == 'connections':
                if self.bridge.school != request['container']:
                    self.bridge.stop()
                self.preferences.update(enabled=bool(request['native']),
                                        profile=request['native'] or 'auto',
                                        container_profile=request['container'])
                self.pending_assignment = {'native': request['native'],
                                           'container': request['container'],
                                           'revision': request['revision']}
                self.assignment = None
                self.routes_restored = False
                changed = True
            elif command == 'profile':
                self.preferences['profile'] = request['profile']
                changed = True
            elif command == 'retry':
                # A recheck must not tear down working campus routes or reload Clash.
                self.last_health = 0
            self.retry_at = 0
        if changed:
            atomic_json(PREFERENCES, self.preferences)

    def tick(self):
        self.routes_restored = False
        self._tick()
        remote = self.preferences.get('container_profile')
        profile = next((p for p in self.profiles if p['id'] == remote), None)
        try:
            self.bridge.sync(profile)
            self.container_route_error = ''
        except Exception as error:
            self.container_route_error = str(error)[:200]
            logging.warning('容器透明路由暂不可用：%s', type(error).__name__)
        self.patch_clash()
        native = self.preferences['profile'] if self.preferences['enabled'] else None
        # Applied acknowledges route and overlay convergence, not queue receipt.
        native_ready = (not native or (self.split and self.split.profile['id'] == native)
                        or (not self.split and self.status.get('phase') == 'logged-out'
                            and self.status.get('profile_id') == native))
        overlay_ready = self.overlay_applied and (self.clash_ready or (not self.split and not remote))
        if self.pending_assignment and native_ready and overlay_ready and not self.container_route_error:
            self.assignment = dict(self.pending_assignment)
        self.routes_restored = bool(not native and not remote and not self.split
                                    and not self.container_route_error and overlay_ready)
        if self.status:
            self.status.update(assignment=self.assignment, routes_restored=self.routes_restored,
                               container_route_error=self.container_route_error)
            atomic_json(STATUS, self.status, 0o644)

    def _tick(self):
        self.process_requests()
        if self.split and not self.split.accepting:
            self.cleanup()
        if not self.preferences['enabled']:
            self.cleanup()
            self.publish('paused', '已暂停，使用 aTrust 原有路由')
            return
        gateway = core.gateway_host()
        profile = selected_profile(self.profiles, gateway, self.preferences['profile'])
        if not profile:
            self.cleanup()
            self.publish('waiting', '等待匹配的 aTrust 登录地址')
            return
        try:
            active = core.tunnel()
        except RuntimeError:
            # No aTrust tunnel at all: the native client is logged out (or not
            # running). The menubar app uses this phase to prompt for login.
            self.cleanup()
            self.publish('logged-out', f"{profile['short_name']} 未登录，请在 aTrust 中登录", profile)
            return
        if self.split and self.split.active != active and self.split.profile['id'] == profile['id'] \
                and self.split.retarget(active):
            # Same utun after a line switch: re-check DNS soon instead of rebuilding.
            self.last_health = 0
        if self.split and (self.split.active != active or self.split.profile['id'] != profile['id']):
            self.cleanup()
            self.retry_at = time.monotonic() + 5
        if not self.split:
            if time.monotonic() < self.retry_at:
                self.publish('waiting', '等待 aTrust 隧道就绪，将自动重试', profile)
                return
            self.publish('starting', '正在检查隧道并应用分流', profile)
            try:
                self.activate(profile)
            except (OSError, struct.error) as error:
                # Typical right after wake: the tunnel exists before campus DNS
                # answers. Wait quietly instead of logging a failure traceback.
                self.failures += 1
                self.retry_at = time.monotonic() + min(60, 5 * self.failures)
                self.last_error = 'VPN DNS: ' + type(error).__name__
                if self.failures in (1, 5):
                    logging.info('校内 DNS 尚未就绪（%s 次），稍后重试：%s', self.failures, type(error).__name__)
                self.publish('waiting', 'aTrust 已连接，等待校内 DNS 就绪', profile)
                return
        self.split.reconcile()
        self.split.prune()
        self.patch_clash()
        if time.monotonic() - self.last_health > (10 if self.health_failures else 30):
            self.last_health = time.monotonic()
            try:
                result = core.upstream(core.make_query(profile['probe']), self.split.active['name'])
                if not core.related_addresses(result)[0]:
                    raise ValueError('VPN DNS health check returned no address')
            except (OSError, ValueError, struct.error) as error:
                # The tunnel identity was checked above. A DNS timeout is not
                # evidence that its routes should be removed or broadened.
                self.health_failures += 1
                self.last_error = 'VPN DNS: ' + type(error).__name__
                if self.health_failures in (1, 3):
                    logging.warning('校内 DNS 探测失败 %s 次，保留分流并复查：%s',
                                    self.health_failures, type(error).__name__)
            else:
                if self.health_failures:
                    logging.info('校内 DNS 探测已恢复，分流期间保持原有规则')
                self.health_failures = 0
                self.last_health_success = time.time()
        if self.health_failures:
            phase = 'checking' if self.health_failures < 3 else 'degraded'
            message = '校内 DNS 暂未响应，保留分流并复查' if phase == 'checking' else '校内 DNS 持续不可用，请检查 aTrust；分流规则已保留'
            self.publish(phase, message, profile)
            return
        self.last_error = ''
        self.publish('active', '分流运行中' if self.clash_ready else '分流运行中 · 等待 Clash', profile)

    def run(self):
        profiles.require_configured(self.profiles)
        if core.STATE.exists():
            self.cleanup()
        if SOCKET.exists():
            SOCKET.unlink()
        server = ControlServer(str(SOCKET), ControlHandler)
        server.worker = self
        os.chown(SOCKET, self.config['owner_uid'], -1)
        os.chmod(SOCKET, 0o600)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        for sig in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
            signal.signal(sig, lambda *_: (self.stop.set(), self.wake.set()))
        try:
            while not self.stop.is_set():
                try:
                    self.tick()
                except Exception as e:
                    logging.exception('本轮分流失败，恢复后自动重试')
                    self.last_error = f'{type(e).__name__}: {str(e)[:160]}'
                    try:
                        self.cleanup()
                    except Exception:
                        logging.exception('恢复未完成，将保留日志并重试')
                    self.failures += 1
                    self.retry_at = time.monotonic() + min(60, 5 * self.failures)
                    self.publish('retrying', '连接暂不可用，正在等待重试')
                self.wake.wait(3)
                self.wake.clear()
        finally:
            server.shutdown()
            server.server_close()
            self.cleanup()
            self.bridge.stop()
            self.overlay.restore()
            self.publish('stopped', '后台服务已停止，配置已恢复')
            if SOCKET.exists():
                SOCKET.unlink()


def control(request):
    with socket.socket(socket.AF_UNIX) as s:
        s.settimeout(3)
        s.connect(str(SOCKET))
        s.sendall(json.dumps(request).encode() + b'\n')
        data = b''
        while b'\n' not in data and len(data) < 32768:
            chunk = s.recv(4096)
            if not chunk:
                break
            data += chunk
    if not data.endswith(b'\n'):
        raise ValueError('后台返回了不完整的状态')
    response = json.loads(data)
    if not isinstance(response, dict):
        raise ValueError('后台返回了无效的状态')
    return response


def report_control_error(error):
    if isinstance(error, TimeoutError):
        message = '后台响应超时，请稍后重试；这不表示 VPN 已退出登录。'
    elif isinstance(error, (FileNotFoundError, ConnectionRefusedError)):
        message = '后台服务尚未启动，可从菜单选择“安装或修复后台服务”。'
    elif isinstance(error, PermissionError):
        message = '当前用户无权访问后台服务，请使用安装此应用的 Mac 账号。'
    else:
        message = '后台操作未完成，请稍后重试或查看服务日志。'
    print(message, file=sys.stderr)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('action', choices=['serve', 'status', 'pause', 'resume', 'retry', 'select', 'inspect', 'install', 'uninstall',
                                         'dual-serve', 'dual-start', 'dual-stop', 'dual-status', 'dual-check', 'diagnostics', 'connections', 'school', 'prefer-native', 'container-login', 'user-setup'])
    parser.add_argument('--school', choices=['a', 'b'])
    parser.add_argument('--enabled', choices=['yes', 'no'])
    parser.add_argument('--app')
    parser.add_argument('--uid', type=int)
    parser.add_argument('--home')
    parser.add_argument('--settings')
    parser.add_argument('--profile', choices=['auto', 'a', 'b'])
    args = parser.parse_args()
    if args.action in ('connections', 'school', 'prefer-native', 'container-login'):
        import connections
        if args.action == 'container-login':
            if not args.school:
                parser.error('container-login requires --school')
            import container_support
            return container_support.open_login(args.school)
        if args.action == 'school':
            if not args.school or not args.enabled:
                parser.error('school requires --school and --enabled')
            value = connections.update(school=args.school, enabled=args.enabled == 'yes')
        elif args.action == 'prefer-native':
            if not args.school:
                parser.error('prefer-native requires --school')
            value = connections.update(native=args.school)
        else:
            value = connections.read()
        print(json.dumps(value, ensure_ascii=False, indent=2))
        return
    if args.action == 'diagnostics':
        import diagnostics
        print(json.dumps(diagnostics.collect(), ensure_ascii=False, indent=2))
        return
    if args.action.startswith('dual-'):
        import dual_runtime
        try:
            return dual_runtime.main(args.action)
        except (OSError, ValueError, RuntimeError) as error:
            if args.action == 'dual-serve':
                raise
            report_control_error(error)
            raise SystemExit(1)
    if args.action == 'user-setup':
        import install_tools
        return install_tools.initialize_user()
    if args.action in ('install', 'uninstall'):
        import install_tools
        try:
            return install_tools.main(args)
        except (RuntimeError, OSError, ValueError, subprocess.TimeoutExpired) as error:
            print(f'安装/卸载未完成：{error}', file=sys.stderr)
            raise SystemExit(1)
    if args.action in ('status', 'pause', 'resume', 'retry', 'select'):
        request = {'command': args.action}
        if args.action == 'select':
            if not args.profile:
                parser.error('select requires --profile')
            request = {'command': 'profile', 'profile': args.profile}
        try:
            response = control(request)
            if not response.get('ok'):
                raise RuntimeError('后台未接受请求')
        except (OSError, ValueError, RuntimeError) as error:
            report_control_error(error)
            raise SystemExit(1)
        print(json.dumps(response, ensure_ascii=False, indent=2))
        return
    if args.action == 'inspect':
        configured = profiles.load_profiles()
        profiles.require_configured(configured)
        profile = selected_profile(configured, core.gateway_host())
        print(json.dumps({'version': VERSION, 'gateway': core.gateway_host(), 'profile': profile,
                          'tunnel': core.tunnel()}, ensure_ascii=False, indent=2))
        if profile:
            split = core.Split(profile, dry=True)
            split.reconcile()
            for name in [profile['probe'], core.gateway_host()]:
                data = split.answer(core.make_query(name))
                print(name, sorted(core.related_addresses(data)[0]))
        return
    if os.geteuid() != 0:
        parser.error('后台 worker 必须由 launchd 以 root 运行')
    core.STATE_DIR.mkdir(mode=0o700, parents=True, exist_ok=True)
    RUNTIME.mkdir(mode=0o755, parents=True, exist_ok=True)
    lock = open(core.STATE_DIR / 'lock', 'w')
    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    LOG_DIR.mkdir(mode=0o755, parents=True, exist_ok=True)
    handler = RotatingFileHandler(LOG_DIR / 'service.log', maxBytes=1048576, backupCount=3)
    handler.setFormatter(logging.Formatter('%(asctime)s %(levelname)s %(message)s'))
    logging.basicConfig(level=logging.INFO, handlers=[handler])
    config = validate_config(json.loads(CONFIG.read_text()))
    profiles.require_configured(config['profiles'])
    Worker(config).run()


if __name__ == '__main__':
    main()
