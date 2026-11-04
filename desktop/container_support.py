"""Two isolated client homes, with only the selected container running."""
import json
import os
import ipaddress
from pathlib import Path
import secrets
import socket
import ssl
import struct
import subprocess
import sys
import tempfile
import time
import shutil

from urllib.parse import urlsplit
import connections
import profiles

DOCKER = ['/opt/homebrew/bin/docker', '--context', 'colima-atrust-split-desktop']
IMAGE = 'local/atrust-split:desktop'
DNS_PORT = 15354


def docker(*args, check=True, timeout=30, input=None):
    result = subprocess.run(DOCKER + list(args), capture_output=True, timeout=timeout,
                            input=input, text=input is None)
    if check and result.returncode:
        # Docker errors can echo environment values; omit their content.
        raise RuntimeError('容器操作失败：' + args[0])
    return result


def secret_path(school):
    return connections.DIRECTORY / ('vnc-password-' + school)


def ensure(school):
    profiles.require_configured(profiles.load_profiles())
    config = connections.SCHOOLS[school]
    name = config['container']
    found = docker('inspect', '--type', 'container', name, check=False)
    if found.returncode == 0:
        obj = json.loads(found.stdout)[0]
        if (obj['Config'].get('Labels') or {}).get('local.atrust-split.managed') != school:
            raise RuntimeError('已有同名容器不属于本应用：' + name)
        return
    image = json.loads(docker('image', 'inspect', IMAGE).stdout)[0]
    if image['Architecture'] != 'arm64':
        raise RuntimeError('容器镜像架构不匹配')
    password = secret_path(school)
    if not password.exists():
        password.write_text(secrets.token_hex(4) + '\n')
        os.chmod(password, 0o600)
    env = connections.DIRECTORY / (school + '-container.env')
    env.write_text('PASSWORD=' + password.read_text().strip() +
                   '\nUSE_NOVNC=1\nCHROMIUM=1\nVNC_SIZE=1360x840\n')
    os.chmod(env, 0o600)
    for suffix in ('home', 'user'):
        docker('volume', 'create', name + '-' + suffix)
    assets = Path(getattr(sys, '_MEIPASS', Path(__file__).parent)) / 'container'
    copies = [('start.sh', '/usr/local/bin/start.sh'), ('xdg-open', '/usr/bin/xdg-open'),
              ('atrust-browser.desktop', '/usr/share/applications/atrust-browser.desktop')]
    if any(not (assets / file).is_file() for file, target in copies):
        raise RuntimeError('应用缺少容器启动文件，请重新安装')
    docker('create', '--name', name, '--hostname', school + '-atrust',
           '--label', 'local.atrust-split.managed=' + school,
           '--restart', 'no', '--init', '--shm-size', '512m',
           '--ulimit', 'nofile=1048576:1048576', '--device', '/dev/net/tun',
           '--cap-add', 'NET_ADMIN',
           '--sysctl', 'net.ipv4.conf.default.route_localnet=1', '--env-file', str(env),
           '--mount', f'type=volume,source={name}-home,target=/root',
           '--mount', f'type=volume,source={name}-user,target=/home/sangfor',
           '-p', f"127.0.0.1:{config['socks_port']}:1080",
           '-p', f"127.0.0.1:{config['web_port']}:8080",
           '-p', f"127.0.0.1:{config['vnc_port']}:5901", image['Id'])
    try:
        for file, target in copies:
            docker('cp', str(assets / file), name + ':' + target)
        with tempfile.TemporaryDirectory() as temporary:
            policy = Path(temporary) / 'chromium/policies/managed/atrust-split.json'
            policy.parent.mkdir(parents=True)
            shutil.copy2(assets / 'chromium-policy.json', policy)
            docker('cp', temporary + '/.', name + ':/etc')
    except Exception:
        # This is our new, never-started container. Keep its fresh volumes so a
        # retry can reuse them, but do not leave an unpatched container behind.
        docker('rm', name, check=False)
        raise


def open_login(school):
    profiles.require_configured(profiles.load_profiles())
    if connections.plan(connections.read())[1] != school:
        raise RuntimeError('该机构当前没有分配到容器')
    config = connections.SCHOOLS[school]
    docker('exec', '-d', '-e', 'DISPLAY=:1', config['container'],
           '/usr/bin/xdg-open', config['login_url'])


def stop_other(school):
    # Listing all containers distinguishes an absent managed name from a daemon
    # failure; neither a failed inspect nor a failed stop is a safety signal.
    names = set(docker('ps', '-a', '--format', '{{.Names}}').stdout.splitlines())
    for key, config in connections.SCHOOLS.items():
        name = config['container']
        if key == school or name not in names:
            continue
        obj = json.loads(docker('inspect', '--type', 'container', name).stdout)[0]
        if (obj['Config'].get('Labels') or {}).get('local.atrust-split.managed') != key:
            raise RuntimeError('拒绝停止未知容器')
        docker('update', '--restart=no', name)
        if obj['State']['Running']:
            docker('stop', '-t', '15', name, timeout=25)
        stopped = json.loads(docker('inspect', '--type', 'container', name).stdout)[0]
        if stopped['State']['Running']:
            raise RuntimeError('容器尚未停止')


def receive(sock, count, deadline=None):
    result = b''
    while len(result) < count:
        if deadline is not None:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError('业务探测超时')
            sock.settimeout(remaining)
        chunk = sock.recv(count - len(result))
        if not chunk:
            raise ConnectionError('SOCKS 连接已关闭')
        result += chunk
    return result


def connect(port, host, target_port, timeout=6):
    # IPv4 literal probes avoid depending on DNS for an SSH-only target.
    sock = socket.create_connection(('127.0.0.1', port), timeout=timeout)
    try:
        sock.settimeout(timeout)
        sock.sendall(b'\x05\x01\x00')
        if receive(sock, 2) != b'\x05\x00':
            raise ConnectionError('SOCKS 握手失败')
        try:
            ip = ipaddress.ip_address(host)
            address = (b'\x01' if ip.version == 4 else b'\x04') + ip.packed
        except ValueError:
            encoded = host.encode('idna')
            address = b'\x03' + bytes([len(encoded)]) + encoded
        sock.sendall(b'\x05\x01\x00' + address + struct.pack('!H', target_port))
        response = receive(sock, 4)
        if response[:3] != b'\x05\x00\x00':
            raise ConnectionError('容器出口无法连接目标')
        kind = response[3]
        if kind not in (1, 3, 4):
            raise ConnectionError('无效的 SOCKS 响应')
        size = 4 if kind == 1 else 16 if kind == 4 else receive(sock, 1)[0]
        receive(sock, size + 2)
        return sock
    except Exception:
        sock.close()
        raise


def dns(query, school):
    # The client DNS stub supports UDP inside the container. socat is already
    # shipped by the image; no credentials or extra packages are required.
    import router_core as core
    config = connections.SCHOOLS[school]
    name, typ, cls, _ = core.question(query)
    if cls != 1 or not any(name == d or name.endswith('.' + d) for d in config['domains']):
        return core.empty_response(query, 5)
    if name in config['gateways']:
        return core.direct_dns(query)
    if typ in (28, 64, 65):  # Container proxy integration is explicitly IPv4.
        return core.empty_response(query)
    result = docker('exec', '-i', config['container'], 'socat', '-T', '2', '-',
                    'UDP4-DATAGRAM:198.18.255.1:53', timeout=5, input=query)
    answer = result.stdout
    if len(answer) < 12 or answer[:2] != query[:2] or core.question(answer)[:3] != (name, typ, cls):
        raise ValueError('容器 DNS 返回无效响应')
    return answer


def gateway_matches(address, school):
    """Accept configured gateway hosts, independent of URL scheme/path/port."""
    try:
        parsed = urlsplit(address if '://' in address else 'https://' + address)
        return bool(parsed.hostname) and parsed.hostname.lower() in connections.SCHOOLS[school]['gateways']
    except ValueError:
        return False


def probe(school):
    import router_core as core
    config = connections.SCHOOLS[school]
    target = config['probe']
    protocol = target['protocol']
    if protocol not in ('ssh', 'tls'):
        raise ValueError('不支持的业务探测协议')
    try:
        address = ipaddress.ip_address(target['host'])
    except ValueError:
        response = dns(core.make_query(target['host']), school)
        addresses = sorted(a for a in core.related_addresses(response)[0]
                           if ipaddress.ip_address(a).version == 4)
        if not addresses:
            raise ConnectionError('机构 DNS 尚未返回 IPv4 业务地址')
        address = ipaddress.ip_address(addresses[0])
    if address.version != 4:
        raise ValueError('业务探测仅支持 IPv4')
    deadline = time.monotonic() + 10
    with connect(config['socks_port'], str(address), target['port']) as sock:
        if protocol == 'ssh':
            # A listening TCP socket or HTTP server must not pass SSH health.
            banner = b''
            while b'\n' not in banner and len(banner) < 256:
                banner += receive(sock, 1, deadline)
            if not banner.startswith(b'SSH-') or not banner.endswith(b'\n'):
                raise ConnectionError('业务服务器未返回 SSH 标识')
            return True
        # Both certificate trust and hostname are verified; no credentials sent.
        with ssl.create_default_context().wrap_socket(sock, server_hostname=target['host']):
            return True
