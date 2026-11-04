"""Institution data lives only in the root-owned, locally installed settings."""
import copy
import ipaddress
import json
from pathlib import Path
import re
from urllib.parse import urlsplit

CONFIG = Path('/Library/Application Support/ATrustSplit/settings.json')
EXAMPLE_PROFILES = [
    {'id': 'a', 'name': '机构 A', 'short_name': 'A',
     'domains': ['a.example.org'], 'fixed_ips': ['10.101.0.10'],
     'gateways': ['vpn.a.example.org', 'vpn-alt.a.example.org'],
     'probe': 'intranet.a.example.org', 'login_url': 'https://vpn.a.example.org/portal/',
     'business_probe': {'host': '10.101.0.10', 'port': 22, 'protocol': 'ssh', 'name': '业务服务'}},
    {'id': 'b', 'name': '机构 B', 'short_name': 'B',
     'domains': ['b.example.org'], 'fixed_ips': [],
     'gateways': ['vpn.b.example.org', 'vpn-alt.b.example.org'],
     'probe': 'intranet.b.example.org', 'login_url': 'https://vpn.b.example.org/portal/',
     'business_probe': {'host': 'intranet.b.example.org', 'port': 443, 'protocol': 'tls', 'name': '业务服务'}},
]


def hostname(value):
    if not isinstance(value, str) or not 1 <= len(value) <= 253 or value != value.lower():
        raise ValueError('Hostnames must be lowercase DNS names')
    if any(not re.fullmatch(r'[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?', label) for label in value.split('.')):
        raise ValueError('Invalid hostname')
    return value


def within(host, domains):
    return any(host == domain or host.endswith('.' + domain) for domain in domains)


def target(value):
    if not isinstance(value, str) or '/' in value:
        raise ValueError('Business targets must be individual IPv4 addresses')
    address = ipaddress.ip_address(value)
    if address.version != 4 or address.is_loopback or address.is_link_local or address.is_multicast or any(
            address in ipaddress.ip_network(net) for net in ('0.0.0.0/8', '100.64.0.0/10', '198.18.0.0/15', '240.0.0.0/4')):
        raise ValueError('Unsupported business IPv4 address')
    return value


def validate_profiles(values):
    fields = {'id', 'name', 'short_name', 'domains', 'fixed_ips', 'gateways', 'probe', 'login_url', 'business_probe'}
    if not isinstance(values, list) or len(values) != 2:
        raise ValueError('Configure exactly two institution slots: a and b')
    ids, addresses, scopes, gateways = set(), set(), [], set()
    for entry in values:
        if not isinstance(entry, dict) or set(entry) != fields or entry['id'] not in ('a', 'b') or entry['id'] in ids:
            raise ValueError('Each profile must have a unique a/b id and the documented fields')
        ids.add(entry['id'])
        for key in ('name', 'short_name'):
            if not isinstance(entry[key], str) or not entry[key].strip() or any(ord(c) < 32 for c in entry[key]):
                raise ValueError('Profile names must be nonempty text')
        for key in ('domains', 'gateways'):
            names = entry[key]
            if not isinstance(names, list) or not names or len(names) != len(set(names)):
                raise ValueError(key + ' must be a nonempty list without duplicates')
            for name in names:
                hostname(name)
        if any(within(domain, scopes) or any(within(old, [domain]) for old in scopes) for domain in entry['domains']):
            raise ValueError('Institution domain scopes must not overlap')
        scopes.extend(entry['domains'])
        if gateways & set(entry['gateways']):
            raise ValueError('A gateway cannot belong to both institutions')
        gateways.update(entry['gateways'])
        fixed = entry['fixed_ips']
        if not isinstance(fixed, list) or len(fixed) > 128:
            raise ValueError('fixed_ips must list at most 128 individual IPv4 addresses')
        for address in fixed:
            target(address)
            if address in addresses:
                raise ValueError('Business targets must be unique across institutions')
            addresses.add(address)
        hostname(entry['probe'])
        if not within(entry['probe'], entry['domains']) or entry['probe'] in entry['gateways']:
            raise ValueError('DNS health probe must be a business hostname in the institution scope')
        raw = entry['login_url']
        if not isinstance(raw, str) or any(c.isspace() or ord(c) < 32 for c in raw):
            raise ValueError('Invalid login_url')
        url = urlsplit(raw)
        if url.scheme != 'https' or url.hostname not in entry['gateways'] or url.username or url.password or url.query or url.fragment:
            raise ValueError('login_url must be an HTTPS gateway URL without credentials, query or fragment')
        if url.port is not None and not 1 <= url.port <= 65535:
            raise ValueError('Invalid login URL port')
        probe = entry['business_probe']
        if not isinstance(probe, dict) or set(probe) != {'host', 'port', 'protocol', 'name'}:
            raise ValueError('Invalid business_probe fields')
        if type(probe['port']) is not int or not 1 <= probe['port'] <= 65535 or probe['protocol'] not in ('ssh', 'tls'):
            raise ValueError('Business probe requires a TCP port and ssh/tls protocol')
        if not isinstance(probe['name'], str) or not probe['name'].strip():
            raise ValueError('Business probe needs a display name')
        host = probe['host']
        if host not in fixed:
            hostname(host)
            if not within(host, entry['domains']) or host in entry['gateways']:
                raise ValueError('Business probe must be a fixed target or an institution business hostname')
    if gateways & addresses:
        raise ValueError('A gateway cannot be a business target')
    return values


def load_profiles(path=None):
    source = CONFIG if path is None else Path(path)
    if not source.exists():
        if path is not None:
            raise FileNotFoundError(source)
        return copy.deepcopy(EXAMPLE_PROFILES)
    if path is None and (source.is_symlink() or source.stat().st_uid != 0 or source.stat().st_mode & 0o022):
        raise ValueError('Installed settings must be root-owned and not writable by other users')
    value = json.loads(source.read_text())
    if not isinstance(value, dict) or type(value.get('version')) is not int or value['version'] != 1:
        raise ValueError('Settings version must be 1')
    return validate_profiles(value.get('profiles'))


def require_configured(values):
    validate_profiles(values)
    reserved = ('example.org', 'example.com', 'example.net', 'invalid')
    for entry in values:
        names = entry['domains'] + entry['gateways'] + [entry['probe'], entry['business_probe']['host']]
        if any(within(name, reserved) for name in names):
            raise ValueError('Replace all example institution data in your local settings before connecting')
