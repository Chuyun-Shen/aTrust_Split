"""One writer composes native DIRECT and isolated SOCKS rules for Clash."""
import copy
import json
from pathlib import Path

import connections
import container_support
import router_core as core

JOURNAL = core.STATE_DIR / 'clash-overlay.json'
PROXY = 'ATrust-Container'


def payload(config, native, remote):
    result = core.clash_payload(config, native) if native else copy.deepcopy(config)
    if not remote:
        return result
    school = connections.SCHOOLS[remote]
    proxies = result.setdefault('proxies', [])
    if any(p.get('name') == PROXY for p in proxies):
        raise ValueError('Clash 中已有同名 ATrust-Container 节点，未覆盖')
    proxies.append({'name': PROXY, 'type': 'socks5', 'server': '127.0.0.1',
                    'port': school['socks_port'], 'udp': False})
    dns = result.setdefault('dns', {})
    if dns.get('fake-ip-filter-mode', 'blacklist') != 'blacklist':
        raise ValueError('Clash fake-ip-filter 配置不兼容')
    dns['enable'] = True
    dns['direct-nameserver-follow-policy'] = True
    for domain in school['domains']:
        dns.setdefault('nameserver-policy', {})['+.' + domain] = [f'127.0.0.1:{container_support.DNS_PORT}']
        for name in (domain, '*.' + domain):
            if name not in dns.setdefault('fake-ip-filter', []):
                dns['fake-ip-filter'].append(name)
    # Login/control traffic always follows the user's ordinary Clash policy.
    # Exact exclusions precede the configured domain suffixes.
    rules = [f'DOMAIN,{host},DIRECT' for host in school['gateways']]
    rules += [f'DOMAIN-SUFFIX,{d},{PROXY}' for d in school['domains']]
    rules += [f"IP-CIDR,{address}/32,DIRECT,no-resolve" for address in school['fixed_ips']]
    result['rules'] = rules + result.get('rules', [])
    return result


class Overlay:
    def __init__(self):
        self.stamp = None

    def restore(self):
        if not JOURNAL.exists():
            return
        if core.CLASH_CONFIG.exists() and Path(core.CLASH_SOCKET).exists():
            import yaml
            config = yaml.safe_load(core.CLASH_CONFIG.read_text())
            core.clash_request(config, '/configs?force=true', {'payload': yaml.safe_dump(config, allow_unicode=True)})
        JOURNAL.unlink()
        self.stamp = None

    def apply(self, native, remote):
        if not native and not remote:
            self.restore()
            return False
        if not core.CLASH_CONFIG.exists() or not Path(core.CLASH_SOCKET).exists():
            self.stamp = None
            return False
        stamp = (native.get('id') if native else None, remote,
                 core.CLASH_CONFIG.stat().st_mtime_ns, Path(core.CLASH_SOCKET).stat().st_ino)
        if stamp == self.stamp:
            return True
        import yaml
        config = yaml.safe_load(core.CLASH_CONFIG.read_text())
        changed = payload(config, native, remote)
        JOURNAL.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        connections.atomic_json(JOURNAL, {'active': True})
        core.clash_request(config, '/configs?force=true', {'payload': yaml.safe_dump(changed, allow_unicode=True)})
        self.stamp = stamp
        return True
