#!/usr/bin/env python3
"""Foreground, reversible macOS aTrust split routing. No aTrust files are edited."""
import argparse
import base64
import copy
import fcntl
import http.client
import ipaddress
import json
import os
from pathlib import Path
import re
import signal
import socket
import socketserver
import struct
import subprocess
import threading
import time
import logging
from urllib.parse import urlsplit

UPSTREAM = '198.18.255.1'
PORT = 15353
RESOLVER_TEXT = f'# atrust-split temporary resolver\nnameserver 127.0.0.1\nport {PORT}\ntimeout 3\n'
STATE_DIR = Path('/var/db/local-atrust-split-service')
STATE = STATE_DIR / 'state.json'
CLASH_CONFIG = Path('/Library/Application Support/clash-verge-service/users/501/runtime/config.yaml')
CLASH_SOCKET = '/var/run/clash-verge-service/users/501/verge-mihomo.sock'


def log(msg):
    logging.getLogger('atrust-split').info(msg)


def command(args, check=True):
    r = subprocess.run(args, text=True, capture_output=True, timeout=12)
    if check and r.returncode:
        raise RuntimeError(f'{args[0]} failed: {r.stderr.strip()[:160]}')
    return r


def tunnel():
    text = command(['/sbin/ifconfig']).stdout
    found = []
    for block in re.split(r'(?=^\S+: flags=)', text, flags=re.M):
        if 'fd53:414e:4746:4f52:' in block and '--> 2.0.0.1' in block:
            name = block.split(':', 1)[0]
            if re.fullmatch(r'utun\d+', name):
                addr = re.search(r'\n\s+inet ([0-9.]+)', block)
                found.append({'name': name, 'index': socket.if_nametoindex(name),
                              'address': addr.group(1) if addr else '',
                              'gateway': gateway_host(), 'boot': boot_identity()})
    if len(found) != 1:
        raise RuntimeError('未检测到唯一的已连接 aTrust 网卡；请先登录 aTrust。')
    return found[0]


def gateway_host():
    try:
        raw = Path('/Library/Application Support/aTrust/var/conf/addr.conf').read_text().strip()
        return (urlsplit(raw if '://' in raw else 'https://' + raw).hostname or '').lower()
    except (OSError, ValueError):
        return ''


_BOOT = None


def boot_identity():
    global _BOOT
    if _BOOT is None:
        _BOOT = command(['/usr/sbin/sysctl', '-n', 'kern.boottime']).stdout.strip()
    return _BOOT


def same_link(left, right):
    return bool(left and right and all(left.get(k) == right.get(k) for k in ['name', 'index', 'boot']))


def normalize_v4(text, host=False):
    if text == 'default':
        return '0.0.0.0/0'
    a, sep, prefix = text.partition('/')
    parts = a.split('.')
    if not all(x.isdigit() for x in parts) or len(parts) > 4:
        raise ValueError(text)
    bits = int(prefix) if sep else (32 if host else len(parts) * 8)
    return str(ipaddress.ip_network('.'.join(parts + ['0'] * (4-len(parts))) + '/' + str(bits), strict=False))


def routes_v4():
    result = []
    for line in command(['/usr/sbin/netstat', '-rn', '-f', 'inet']).stdout.splitlines():
        p = line.split()
        if len(p) < 4 or not re.fullmatch(r'(?:utun|en|lo|bridge)\d+', p[3]):
            continue
        try:
            net = normalize_v4(p[0], 'H' in p[2])
        except ValueError:
            continue
        result.append({'net': net, 'gateway': p[1], 'flags': p[2], 'iface': p[3]})
    return result


def removable(route, iface, wanted):
    return (route['iface'] == iface and route['gateway'] == iface
            and 'I' not in route['flags'] and '1' not in route['flags']
            and route['net'] not in wanted)


def route_args(action, network, iface, managed=False):
    net = ipaddress.ip_network(network)
    args = ['/sbin/route', '-n', action]
    if net.version == 6:
        args += ['-inet6']
    args += ['-host', str(net.network_address)] if net.prefixlen == net.max_prefixlen else ['-net', str(net)]
    args += ['-interface', iface]
    if managed and action == 'add':
        args += ['-proto1']
    return args


def owned_host_route(network, active):
    if not active:
        return False
    ip = str(ipaddress.ip_network(network).network_address)
    cmd = ['/sbin/route', '-n', 'get'] + (['-inet6'] if ':' in ip else []) + [ip]
    text = command(cmd, False).stdout
    return ('PROTO1' in text and re.search(r'interface:\s+' + re.escape(active['name']) + r'\s', text)
            and re.search(r'destination:\s+' + re.escape(ip) + r'\s', text))


def read_name(data, pos):
    labels, seen, end = [], set(), None
    for _ in range(128):
        if pos >= len(data) or pos in seen:
            raise ValueError('invalid DNS name')
        seen.add(pos)
        length = data[pos]
        if length & 0xc0 == 0xc0:
            if pos + 1 >= len(data):
                raise ValueError('truncated DNS pointer')
            if end is None:
                end = pos + 2
            pos = ((length & 63) << 8) | data[pos+1]
        elif length == 0:
            return '.'.join(labels).lower(), end if end is not None else pos+1
        else:
            if length > 63 or pos + length + 1 > len(data):
                raise ValueError('invalid DNS label')
            labels.append(data[pos+1:pos+1+length].decode('ascii'))
            pos += length + 1
    raise ValueError('DNS name too long')


def question(data):
    if len(data) < 12 or struct.unpack_from('!H', data, 4)[0] != 1:
        raise ValueError('expected one question')
    name, end = read_name(data, 12)
    if end+4 > len(data):
        raise ValueError('short question')
    qtype, qclass = struct.unpack_from('!HH', data, end)
    return name, qtype, qclass, end+4


def make_query(name, qtype=1):
    wire = b''.join(bytes([len(s)]) + s.encode('ascii') for s in name.split('.')) + b'\0'
    return os.urandom(2) + struct.pack('!5H', 0x100, 1, 0, 0, 0) + wire + struct.pack('!HH', qtype, 1)


def empty_response(query, rcode=0):
    _, _, _, end = question(query)
    flags = 0x8080 | (struct.unpack_from('!H', query, 2)[0] & 0x100) | rcode
    return query[:2] + struct.pack('!5H', flags, 1, 0, 0, 0) + query[12:end]


def parse_response(data):
    name, qtype, qclass, pos = question(data)
    counts = struct.unpack_from('!3H', data, 6)
    records = []
    for i in range(sum(counts)):
        owner, pos = read_name(data, pos)
        typ, cls, ttl, size = struct.unpack_from('!HHIH', data, pos)
        ttl_offset = pos+4
        pos += 10
        end = pos + size
        if end > len(data):
            raise ValueError('short DNS record')
        value = None
        if cls == 1 and typ in (1, 28) and size == (4 if typ == 1 else 16):
            value = socket.inet_ntop(socket.AF_INET if typ == 1 else socket.AF_INET6, data[pos:end])
        elif cls == 1 and typ == 5:
            value, _ = read_name(data, pos)
        records.append((owner, typ, value, ttl_offset, i < counts[0]))
        pos = end
    return records


def related_addresses(data):
    name, _, _, _ = question(data)
    records = parse_response(data)
    related = {name}
    for _ in range(16):
        before = len(related)
        for owner, typ, value, _, answer in records:
            if answer and owner in related and typ == 5 and value:
                related.add(value)
        if before == len(related):
            break
    addresses = {value for owner, typ, value, _, answer in records
                 if answer and owner in related and typ in (1, 28) and value}
    return addresses, related - {name}


def upstream(query, iface, tcp=False, server=UPSTREAM):
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM if tcp else socket.SOCK_DGRAM) as s:
        s.settimeout(3)
        s.setsockopt(socket.IPPROTO_IP, 25, socket.if_nametoindex(iface))  # Darwin IP_BOUND_IF
        s.connect((server, 53))
        if tcp:
            s.sendall(struct.pack('!H', len(query)) + query)
            data = receive(s, struct.unpack('!H', receive(s, 2))[0])
        else:
            s.send(query)
            data = s.recv(65535)
    if data[:2] != query[:2] or question(data)[:3] != question(query)[:3] or not data[2] & 128:
        raise ValueError('DNS response mismatch')
    if not tcp and data[2] & 2:
        return upstream(query, iface, tcp=True, server=server)
    return data


def direct_dns(query):
    # The VPN login/control name must resolve outside its own tunnel.
    route = command(['/sbin/route', '-n', 'get', 'default']).stdout
    match = re.search(r'interface:\s+(en\d+)', route)
    if not match:
        raise RuntimeError('Cannot identify physical default interface for VPN DNS')
    iface = match.group(1)
    dns = command(['/usr/sbin/scutil', '--dns']).stdout
    servers = []
    for block in dns.split('resolver #'):
        if re.search(r'if_index\s*:\s*\d+\s+\(' + re.escape(iface) + r'\)', block):
            servers += re.findall(r'nameserver\[\d+\]\s*:\s*(\d+\.\d+\.\d+\.\d+)\s', block)
    for server in dict.fromkeys(servers + ['223.5.5.5']):
        try:
            return upstream(query, iface, server=server)
        except (OSError, ValueError):
            continue
    raise RuntimeError('VPN control-name DNS failed on physical interface')


def receive(sock, count):
    data = b''
    while len(data) < count:
        chunk = sock.recv(count-len(data))
        if not chunk:
            raise ValueError('short TCP message')
        data += chunk
    return data


def campus(name, profile):
    return any(name == domain or name.endswith('.' + domain) for domain in profile['domains'])


def eligible(address):
    ip = ipaddress.ip_address(address)
    return not (ip.is_loopback or ip.is_link_local or ip.is_multicast or ip.is_unspecified
                or (ip.version == 4 and ip in ipaddress.ip_network('100.64.0.0/10'))
                or (ip.version == 6 and ip in ipaddress.ip_network('fd7a:115c:a1e0::/48')))


def save(state):
    STATE_DIR.mkdir(mode=0o700, exist_ok=True)
    tmp = STATE.with_suffix('.tmp')
    tmp.write_text(json.dumps(state))
    os.chmod(tmp, 0o600)
    tmp.replace(STATE)


def clash_request(config, path, payload=None):
    s = socket.socket(socket.AF_UNIX)
    s.settimeout(12)
    s.connect(CLASH_SOCKET)
    c = http.client.HTTPConnection('localhost', timeout=12)
    c.sock = s
    try:
        headers = {'Authorization': 'Bearer ' + str(config.get('secret', ''))}
        data = None
        if payload is not None:
            data = json.dumps(payload).encode()
            headers['Content-Type'] = 'application/json'
        c.request('PUT' if payload is not None else 'GET', path, body=data, headers=headers)
        r = c.getresponse()
        body = r.read()
        if r.status >= 300:
            raise RuntimeError(f'Clash API returned {r.status}')
        return body
    finally:
        c.close()


def clash_payload(config, profile):
    c = copy.deepcopy(config)
    dns = c.setdefault('dns', {})
    dns['enable'] = True
    for domain in profile['domains']:
        dns.setdefault('nameserver-policy', {})['+.' + domain] = [f'127.0.0.1:{PORT}']
    dns['direct-nameserver-follow-policy'] = True
    mode = dns.get('fake-ip-filter-mode', 'blacklist')
    if mode != 'blacklist':
        raise RuntimeError('Clash fake-ip-filter mode is not blacklist; refusing an ambiguous rewrite')
    filters = dns.setdefault('fake-ip-filter', [])
    for name in [n for d in profile['domains'] for n in [d, '*.' + d]]:
        if name not in filters:
            filters.append(name)
    wanted = [f'IP-CIDR,{ip}/32,DIRECT,no-resolve' for ip in profile['fixed_ips']]
    wanted += [f'DOMAIN-SUFFIX,{d},DIRECT' for d in profile['domains']]
    c['rules'] = wanted + [r for r in c.get('rules', []) if r not in wanted]
    return c


def restore(state):
    errors = []
    # Restore broad forwarding before removing DNS-created host routes.
    try:
        active = tunnel()
    except RuntimeError:
        active = None
    for record in state.get('removed', []):
        if same_link(active, record['tunnel']) and active.get('gateway') == record['tunnel'].get('gateway'):
            r = command(route_args('add', record['net'], active['name']), False)
            if r.returncode and 'File exists' not in r.stderr:
                errors.append('restore route ' + record['net'])
    if errors:
        # Keep both the owned hosts and journal until covering routes recover.
        raise RuntimeError('; '.join(errors))
    for record in reversed(state.get('added', [])):
        if same_link(active, record['tunnel']) and owned_host_route(record['net'], active):
            result = command(route_args('delete', record['net'], active['name']), False)
            if result.returncode:
                errors.append('remove owned route ' + record['net'])
    for resolver in state.get('resolvers', []):
        path = Path(resolver['path'])
        if path.exists() and path.read_text() == RESOLVER_TEXT:
            old = resolver.get('old')
            if old is None:
                path.unlink()
            else:
                path.write_bytes(base64.b64decode(old))
                os.chmod(path, resolver['mode'])
    if state.get('clash_active') and CLASH_CONFIG.exists():
        try:
            import yaml
            c = yaml.safe_load(CLASH_CONFIG.read_text())
            clash_request(c, '/configs?force=true', {'payload': yaml.safe_dump(c, allow_unicode=True)})
        except (FileNotFoundError, ConnectionRefusedError):
            # A stopped core will load its unchanged on-disk config when restarted.
            pass
        except Exception:
            errors.append('Clash 恢复失败；请在 Clash Verge 中重新加载配置')
    command(['/usr/bin/dscacheutil', '-flushcache'], False)
    command(['/usr/bin/killall', '-HUP', 'mDNSResponder'], False)
    if errors:
        raise RuntimeError('; '.join(errors))
    if STATE.exists():
        STATE.unlink()
    log('已恢复 DNS、Clash 运行配置及可恢复的 aTrust 路由。')


class Split:
    def __init__(self, profile, dry=False):
        self.profile = profile
        self.dry = dry
        self.lock = threading.RLock()
        self.active = tunnel()
        self.permanent = set(self.profile['fixed_ips']) | {UPSTREAM}
        self.addresses = set(self.permanent)
        self.expiry = {}
        self.state = {'removed': [], 'added': [], 'clash_active': False}
        self.clash_stamp = None
        self.accepting = True
        self.last_prune = 0

    def add_address(self, address):
        if not eligible(address):
            raise RuntimeError('DNS result conflicts with a local or Tailscale address')
        self.addresses.add(address)
        net = str(ipaddress.ip_network(address))
        record = {'net': net, 'tunnel': self.active}
        if record in self.state['added']:
            return
        if self.dry:
            return
        # A pre-existing exact route stays owned by its original creator.
        if ':' not in address:
            exact = [r for r in routes_v4() if r['net'] == net and 'I' not in r['flags']]
            if exact:
                if any(r['iface'] != self.active['name'] for r in exact):
                    raise RuntimeError('Existing host route belongs to another interface: ' + address)
                return
        self.state['added'].append(record)
        save(self.state)  # journal before mutation
        r = command(route_args('add', net, self.active['name'], managed=True), False)
        if r.returncode:
            self.state['added'].remove(record)
            save(self.state)
            raise RuntimeError('Cannot add aTrust host route: ' + address)

    def retarget(self, new):
        """Follow an aTrust line switch on the same utun without a rebuild.

        A gateway failover keeps the interface but assigns a new virtual
        address. Only that case is adopted; a new interface, reboot or VPN
        host still requires a full restore and activation.
        """
        with self.lock:
            if not same_link(new, self.active) or new['gateway'] != self.active['gateway']:
                return False
            old = self.active
            for record in self.state['removed'] + self.state['added']:
                if record['tunnel'] == old:
                    record['tunnel'] = new
            self.active = new
            if not self.dry:
                save(self.state)
            log(f"aTrust 线路切换：{old['address']} -> {new['address']}，沿用现有分流。")
            return True

    def reconcile(self):
        with self.lock:
            current_tunnel = tunnel()
            if current_tunnel != self.active and not self.retarget(current_tunnel):
                raise RuntimeError('aTrust session changed')
            # Recheck routes because aTrust may recreate them after a reconnect.
            current = routes_v4()
            actual = {(r['net'], r['iface']) for r in current}
            self.state['added'] = [r for r in self.state['added'] if ':' in r['net'] or
                                   (r['net'], r['tunnel']['name']) in actual]
            for a in sorted(self.addresses):
                self.add_address(a)
            wanted = {str(ipaddress.ip_network(a)) for a in self.addresses}
            removed = 0
            for r in current:
                if not removable(r, self.active['name'], wanted):
                    continue
                record = {'net': r['net'], 'tunnel': self.active}
                if record not in self.state['removed']:
                    self.state['removed'].append(record)
                    if not self.dry:
                        save(self.state)
                if not self.dry:
                    command(route_args('delete', r['net'], self.active['name']))
                removed += 1
            if removed:
                log(f'{"计划移除" if self.dry else "已移除"} {removed} 条 aTrust 广泛 IPv4 路由；保留指定目标。')

    def prune(self):
        now = time.time()
        if now - self.last_prune < 120:
            return
        self.last_prune = now
        busy = command(['/usr/sbin/netstat', '-an', '-p', 'tcp'], False).stdout
        with self.lock:
            for ip, expiry in list(self.expiry.items()):
                if expiry > now or ip in self.permanent or ip in busy:
                    continue
                net = str(ipaddress.ip_network(ip))
                for record in list(self.state['added']):
                    if record['net'] == net and same_link(record['tunnel'], self.active):
                        if owned_host_route(net, self.active):
                            command(route_args('delete', net, self.active['name']), False)
                        self.state['added'].remove(record)
                self.addresses.discard(ip)
                del self.expiry[ip]
            if not self.dry:
                save(self.state)

    def answer(self, query):
        name, typ, cls, _ = question(query)
        if not campus(name, self.profile) or cls != 1:
            return empty_response(query, 5)
        if name in self.profile['gateways']:
            return direct_dns(query)
        # HTTPS/SVCB address hints can bypass A/AAAA routing; let clients use A/AAAA.
        if typ in (64, 65):
            return empty_response(query)
        active = tunnel()
        if active != self.active:
            raise RuntimeError('VPN session changed; retry after service transition')
        data = upstream(query, active['name'])
        if name not in self.profile['gateways']:
            addresses, aliases = related_addresses(data)
            if aliases and typ in (1, 28) and not addresses:
                for alias in sorted(aliases)[:8]:
                    extra = upstream(make_query(alias, typ), active['name'])
                    addresses.update(related_addresses(extra)[0])
            with self.lock:
                if not self.accepting:
                    raise RuntimeError('DNS service is stopping')
                if active != self.active:
                    raise RuntimeError('VPN session changed')
                if len(self.addresses | addresses) > 4096:
                    raise RuntimeError('DNS route limit exceeded')
                for address in addresses:
                    self.add_address(address)
                    self.expiry[address] = time.time() + 3600
        b = bytearray(data)
        b[3] &= ~0x20  # do not claim authenticated data after local processing
        for _, typ, _, offset, _ in parse_response(data):
            if typ != 41:  # OPT TTL has a different meaning
                ttl = struct.unpack_from('!I', b, offset)[0]
                struct.pack_into('!I', b, offset, min(ttl, 60))
        return bytes(b)

    def setup_resolver(self):
        for domain in self.profile['domains']:
            path = Path('/etc/resolver') / domain
            if path.is_symlink():
                raise RuntimeError('Resolver is a symlink; refusing to replace it')
            self.state.setdefault('resolvers', []).append({
                'path': str(path), 'old': base64.b64encode(path.read_bytes()).decode() if path.exists() else None,
                'mode': path.stat().st_mode & 0o777 if path.exists() else 0o644})
            save(self.state)
            path.parent.mkdir(exist_ok=True)
            path.write_text(RESOLVER_TEXT)
            os.chmod(path, 0o644)
        command(['/usr/bin/dscacheutil', '-flushcache'], False)
        command(['/usr/bin/killall', '-HUP', 'mDNSResponder'], False)

    def patch_clash(self):
        if not CLASH_CONFIG.exists() or not Path(CLASH_SOCKET).exists():
            return False
        import yaml
        # Socket inode detects a restarted core even when config mtime is unchanged.
        stamp = (CLASH_CONFIG.stat().st_mtime_ns, Path(CLASH_SOCKET).stat().st_ino)
        if stamp == self.clash_stamp:
            return True
        c = yaml.safe_load(CLASH_CONFIG.read_text())
        changed = clash_payload(c, self.profile)
        self.state['clash_active'] = True
        save(self.state)
        clash_request(c, '/configs?force=true', {'payload': yaml.safe_dump(changed, allow_unicode=True)})
        self.clash_stamp = stamp
        log('已临时接入 Clash 校内域名 DNS；订阅文件保持原样。')
        return True


class UDP(socketserver.ThreadingUDPServer):
    daemon_threads = True


class TCP(socketserver.ThreadingTCPServer):
    daemon_threads = True
    allow_reuse_address = True


class Handler(socketserver.BaseRequestHandler):
    def handle(self):
        query = None
        try:
            if isinstance(self.request, tuple):
                query, sock = self.request
            else:
                sock = self.request
                sock.settimeout(5)
                query = receive(sock, struct.unpack('!H', receive(sock, 2))[0])
            reply = self.server.split.answer(query)
        except Exception as e:
            log('DNS request failed: ' + type(e).__name__)
            try:
                reply = empty_response(query, 2)
            except Exception:
                return
        if isinstance(self.request, tuple):
            sock.sendto(reply, self.client_address)
        else:
            sock.sendall(struct.pack('!H', len(reply)) + reply)


