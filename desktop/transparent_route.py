"""Owned /32 system routes into an isolated aTrust SOCKS exit.

Applications keep their original destination addresses. Only explicit targets
from the root-owned institution profile enter this dedicated interface.
"""
import json
import os
from pathlib import Path
import re
import signal
import socket
import subprocess
import sys
import time

import connections
import router_core as core

JOURNAL = core.STATE_DIR / 'container-routes.json'
BINARY = Path(getattr(sys, '_MEIPASS', Path(__file__).parent)) / 'vendor/tun2socks'
ADDRESS = '169.254.210.1'


def targets(profile):
    if not profile:
        return []
    if profile.get('id') not in connections.SCHOOLS:
        raise ValueError('未知容器机构')
    result = profile.get('fixed_ips', [])
    for address in result:
        if core.ipaddress.ip_address(address).version != 4 or not core.eligible(address):
            raise ValueError('透明路由仅接受 IPv4 单地址')
    return list(dict.fromkeys(result))


def link_alive(record):
    if record.get('boot') != core.boot_identity():
        return False
    try:
        if socket.if_nametoindex(record['name']) != record['index']:
            return False
    except OSError:
        return False
    result = core.command(['/sbin/ifconfig', record['name']], False)
    return result.returncode == 0 and bool(re.search(r'\binet ' + re.escape(ADDRESS) + r'\b', result.stdout))


def owned_process(record):
    if record.get('boot') != core.boot_identity():
        return False
    pid = record.get('pid')
    if not isinstance(pid, int) or pid < 2:
        return False
    result = core.command(['/bin/ps', '-p', str(pid), '-o', 'lstart=', '-o', 'comm='], False)
    return bool(record.get('process_identity')) and result.stdout.strip() == record['process_identity']


class Bridge:
    def __init__(self):
        self.process = None
        self.record = None
        self.school = None
        self.log = None

    def stop(self):
        record = self.record
        if record is None and JOURNAL.exists():
            record = json.loads(JOURNAL.read_text())
        if record:
            if link_alive(record):
                for address in record.get('targets', []):
                    network = address + '/32'
                    if core.owned_host_route(network, record):
                        core.command(core.route_args('delete', network, record['name']), False)
            # PID reuse must never kill an unrelated process. The stored full
            # start time and executable path are checked before recovery kills.
        if self.process and self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=5)
        elif record and owned_process(record):
            os.kill(record['pid'], signal.SIGTERM)
            deadline = time.monotonic() + 5
            while owned_process(record) and time.monotonic() < deadline:
                time.sleep(0.1)
            if owned_process(record):
                raise RuntimeError('旧透明路由进程尚未退出，保留恢复记录')
        JOURNAL.unlink(missing_ok=True)
        if self.log:
            self.log.close()
        self.process = self.record = self.school = self.log = None

    def sync(self, profile):
        wanted = targets(profile)
        school = profile['id'] if wanted else None
        if not school:
            self.stop()
            return
        if self.school != school or not self.process or self.process.poll() is not None:
            self.stop()
            self.start(profile)
        if not self.record or not link_alive(self.record):
            self.stop()
            raise RuntimeError('容器透明路由接口发生变化')
        for address in list(self.record['targets']):
            if address not in wanted:
                network = address + '/32'
                if core.owned_host_route(network, self.record):
                    core.command(core.route_args('delete', network, self.record['name']))
                self.record['targets'].remove(address)
                connections.atomic_json(JOURNAL, self.record)
        for address in wanted:
            self.add(address)

    def start(self, profile):
        school = profile['id']
        if not BINARY.is_file() or not os.access(BINARY, os.X_OK):
            raise RuntimeError('应用缺少透明路由组件，请重新安装')
        listing = core.command(['/sbin/ifconfig']).stdout
        if re.search(r'\binet ' + re.escape(ADDRESS) + r'\b', listing):
            raise RuntimeError('透明路由接口地址已被其他程序使用')
        device = next((f'utun{i}' for i in range(210, 220)
                       if not re.search(r'^utun' + str(i) + r':', listing, re.M)), None)
        if device is None:
            raise RuntimeError('没有可用的透明路由接口')
        JOURNAL.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.log = open(core.STATE_DIR / 'container-tun.log', 'a')
        try:
            self.process = subprocess.Popen([str(BINARY), '--device', device, '--proxy',
                f"socks5://127.0.0.1:{connections.SCHOOLS[school]['socks_port']}",
                '--interface', 'lo0', '--mtu', '1500', '--loglevel', 'warn'],
                stdin=subprocess.DEVNULL, stdout=self.log, stderr=self.log)
            # Journal before touching routes; early failures still terminate
            # the owned child even when recording its identity fails.
            identity = core.command(['/bin/ps', '-p', str(self.process.pid), '-o', 'lstart=', '-o', 'comm='], False).stdout.strip()
            self.record = {'pid': self.process.pid, 'process_identity': identity, 'name': device,
                           'index': -1, 'boot': core.boot_identity(), 'targets': []}
            connections.atomic_json(JOURNAL, self.record)
            deadline = time.monotonic() + 8
            while core.command(['/sbin/ifconfig', device], False).returncode:
                if self.process.poll() is not None or time.monotonic() > deadline:
                    raise RuntimeError('透明路由接口启动失败')
                time.sleep(0.1)
            core.command(['/sbin/ifconfig', device, 'inet', ADDRESS, ADDRESS,
                          'netmask', '255.255.255.255', 'up'])
            self.record['index'] = socket.if_nametoindex(device)
            connections.atomic_json(JOURNAL, self.record)
            self.school = school
        except Exception:
            self.stop()
            raise

    def add(self, address):
        network = address + '/32'
        exact = [r for r in core.routes_v4() if r['net'] == network and 'I' not in r['flags']]
        if exact:
            if address in self.record['targets'] and core.owned_host_route(network, self.record):
                return
            raise RuntimeError('目标已有其他程序的路由，未覆盖：' + address)
        if address not in self.record['targets']:
            self.record['targets'].append(address)
            connections.atomic_json(JOURNAL, self.record)
        core.command(core.route_args('add', network, self.record['name'], managed=True))
