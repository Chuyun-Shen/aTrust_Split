"""Reconcile selected schools without storing or moving aTrust credentials."""
import fcntl
import json
import logging
from logging.handlers import RotatingFileHandler
import os
import signal
import threading
import time
import socketserver

import connections
import container_support
import dual_runtime as dual
import native_client
import service
from version import VERSION


class Manager:
    def __init__(self):
        self.stop = threading.Event()
        self.supervisor = dual.Supervisor()
        self.current_remote = None
        self.vm_stopped = False
        self.native = {'phase': 'checking'}
        self.stopped_revision = None

    def ensure_current(self, expected):
        if connections.read() != expected:
            raise RuntimeError('连接选择已更改，正在按新选择重新检查')

    def configure_routing(self, native, remote, revision):
        desired = {'native': native, 'container': remote, 'revision': revision}
        response = service.control({'command': 'status'})
        status = response.get('status', {})
        if not response.get('ok'):
            raise RuntimeError('无法读取后台分流状态')
        if status.get('assignment') == desired:
            return bool(native or remote) or status.get('routes_restored') is True
        response = service.control({'command': 'connections', **desired})
        if not response.get('ok'):
            raise RuntimeError('后台分流服务未接受连接配置，请安装新版应用')
        # Queue acceptance is not an applied assignment. Recheck on next tick,
        # including after a daemon restart with unchanged user choices.
        return False

    def vm_running(self):
        info = dual.run([dual.COLIMA, 'list', '--json'])
        text = info.stdout.strip()
        try:
            records = json.loads(text) if text else []
        except json.JSONDecodeError:
            records = [json.loads(line) for line in text.splitlines() if line.strip()]
        if isinstance(records, dict):
            records = [records]
        if not isinstance(records, list):
            raise RuntimeError('无法确认专用虚拟机状态')
        for record in records:
            if record.get('name') == dual.PROFILE:
                state = record.get('status', '').lower()
                if state not in ('running', 'stopped'):
                    raise RuntimeError('专用虚拟机尚未稳定停止或启动')
                return state == 'running'
        return False

    def stop_vm(self):
        if not self.vm_stopped:
            if self.vm_running():
                container_support.stop_other(None)
                dual.run([dual.COLIMA, 'stop', '--profile', dual.PROFILE], timeout=90)
            if self.vm_running():
                raise RuntimeError('专用虚拟机尚未确认停止')
            self.vm_stopped = True
        self.current_remote = None
        self.supervisor.enabled = False
        self.supervisor.publish('paused', '容器已停止', False)

    def start_remote(self, school, still_current):
        dual.use_school(school)
        self.vm_stopped = False
        if self.current_remote != school:
            self.supervisor = dual.Supervisor()
            self.supervisor.publish('starting', f"正在准备 {connections.SCHOOLS[school]['name']} 容器")
            still_current()
            info = dual.run([dual.COLIMA, 'status', '--profile', dual.PROFILE, '--json'], check=False)
            if info.returncode:
                dual.run([dual.COLIMA, 'start', '--profile', dual.PROFILE,
                          '--activate=false', '--ssh-config=false'], timeout=180)
            still_current()
            container_support.stop_other(school)
            container_support.ensure(school)
            still_current()
            self.current_remote = school
        self.supervisor.tick(enabled=True)

    def tick(self):
        config = connections.read()
        native, remote = connections.plan(config)
        # Missing settings must not turn a default empty choice into logout or
        # VM shutdown of an unrelated existing client.
        connections.require_configured()
        revision = config.get('revision', 0)
        if native is None and self.stopped_revision is None:
            try:
                previous = json.loads(connections.STATUS.read_text())
            except (OSError, ValueError):
                previous = {}
            if (isinstance(previous, dict) and previous.get('phase') == 'ready' and
                    previous.get('revision') == revision and
                    'native' in previous and previous['native'] is None and
                    'container' in previous and previous['container'] is None and
                    isinstance(previous.get('native_status'), dict) and
                    previous['native_status'].get('phase') == 'stopped'):
                # A completed deselection survives manager restart. A manually
                # reopened native app is not logged out a second time.
                self.stopped_revision = revision
                self.native = previous['native_status']
        phase, error = 'applying', ''
        connections.atomic_json(connections.STATUS, {
            'version': VERSION, 'phase': phase, 'native': native, 'container': remote,
            'revision': revision, 'updated_at': time.time(), 'message': '正在切换连接…'})
        if remote is None:
            self.stop_vm()
        else:
            # Check even on process startup: a persisted container may own the
            # institution that is now being assigned to the native client.
            if self.vm_running():
                container_support.stop_other(remote)
            if self.current_remote != remote:
                self.current_remote = None
        self.ensure_current(config)
        try:
            if native is not None or self.stopped_revision != revision:
                self.native = native_client.reconcile(native, still_current=lambda: self.ensure_current(config))
                self.stopped_revision = revision if native is None else None
        except (OSError, ValueError, RuntimeError) as exc:
            self.native = {'phase': 'error', 'school': native, 'message': str(exc)[:200]}
            phase, error = 'error', self.native['message']
        # Failed native logout must not start a duplicate institution.
        self.ensure_current(config)
        if remote and self.native['phase'] != 'error':
            self.start_remote(remote, lambda: self.ensure_current(config))
        self.ensure_current(config)
        applied = self.configure_routing(native, remote, revision)
        if applied and phase != 'error':
            phase = 'ready'
        connections.atomic_json(connections.STATUS, {
            'version': VERSION, 'phase': phase, 'native': native, 'container': remote,
            'native_status': self.native, 'updated_at': time.time(),
            'revision': revision, 'message': error,
        })

    def serve(self):
        connections.DIRECTORY.mkdir(mode=0o700, parents=True, exist_ok=True)
        lock = open(connections.DIRECTORY / 'supervisor.lock', 'w')
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        import router_core as core

        class Resolver:
            def answer(self, query):
                native, remote = connections.plan(connections.read())
                if not remote:
                    return core.empty_response(query, 2)
                return container_support.dns(query, remote)

        servers = []
        for cls in (core.UDP, core.TCP):
            server = cls(('127.0.0.1', container_support.DNS_PORT), core.Handler)
            server.split = Resolver()
            threading.Thread(target=server.serve_forever, daemon=True).start()
            servers.append(server)
        handler = RotatingFileHandler(connections.DIRECTORY / 'service.log', maxBytes=1048576, backupCount=2)
        handler.setFormatter(logging.Formatter('%(asctime)s %(levelname)s %(message)s'))
        logging.basicConfig(level=logging.INFO, handlers=[handler])
        for sig in (signal.SIGTERM, signal.SIGINT):
            signal.signal(sig, lambda *_: self.stop.set())
        while not self.stop.is_set():
            try:
                self.tick()
            except Exception as error:
                # Report failures without marking an unconfirmed shutdown as off.
                logging.warning('连接切换未完成：%s', type(error).__name__)
                connections.atomic_json(connections.STATUS, {
                    'version': VERSION, 'phase': 'error', 'updated_at': time.time(),
                    'message': str(error)[:250], 'native_status': self.native})
            self.stop.wait(5)
        for server in servers:
            server.shutdown()
            server.server_close()
