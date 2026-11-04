"""User-owned connection choices. Checked means requested, not authenticated."""
import copy
import fcntl
import json
import os
from pathlib import Path
import time
from urllib.parse import urlsplit, urlunsplit

import profiles

DIRECTORY = Path.home() / 'Library/Application Support/ATrustDual'
PATH = DIRECTORY / 'connections.json'
STATUS = DIRECTORY / 'connections-status.json'
PROFILES = profiles.load_profiles()
SCHOOLS = {}
for profile in PROFILES:
    key = profile['id']
    login = urlsplit(profile['login_url'])
    SCHOOLS[key] = {
        'name': profile['name'], 'short_name': profile['short_name'],
        'login_url': profile['login_url'],
        'gateway': urlunsplit((login.scheme, login.netloc, '', '', '')),
        'gateways': list(profile['gateways']), 'domains': list(profile['domains']),
        'fixed_ips': list(profile['fixed_ips']), 'container': 'atrust-' + key,
        'socks_port': 10811 if key == 'a' else 10812,
        'web_port': 18081 if key == 'a' else 18082,
        'vnc_port': 15911 if key == 'a' else 15912,
        'probe': copy.deepcopy(profile['business_probe']),
    }
DEFAULT = {'version': 1, 'enabled': [], 'native_preference': 'a'}


def validate(value):
    if not isinstance(value, dict) or type(value.get('version')) is not int or value['version'] != 1:
        raise ValueError('连接配置 version 必须为 1')
    enabled = value.get('enabled')
    if (not isinstance(enabled, list) or
            any(not isinstance(x, str) or x not in SCHOOLS for x in enabled) or
            len(set(enabled)) != len(enabled)):
        raise ValueError('enabled 只能包含 a / b，且不可重复')
    if not isinstance(value.get('native_preference'), str) or value['native_preference'] not in SCHOOLS:
        raise ValueError('native_preference 必须为 a 或 b')
    revision = value.get('revision', 0)
    if type(revision) is not int or not 0 <= revision <= 9223372036854775807:
        raise ValueError('revision 必须为非负的 64 位整数')
    return value


def require_configured():
    profiles.require_configured(PROFILES)


def read():
    return validate(json.loads(PATH.read_text())) if PATH.exists() else copy.deepcopy(DEFAULT)


def atomic_json(path, value):
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    temp = path.with_name(path.name + '.tmp-' + str(os.getpid()))
    with open(temp, 'w') as stream:
        os.chmod(temp, 0o600)
        json.dump(value, stream, ensure_ascii=False, indent=2)
        stream.write('\n')
    temp.replace(path)


def update(school=None, enabled=None, native=None):
    PATH.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    with open(PATH.with_suffix('.lock'), 'a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        value = read()
        old_plan = plan(value)
        if school is not None:
            if not isinstance(school, str) or school not in SCHOOLS or not isinstance(enabled, bool):
                raise ValueError('无效的机构选择')
            value['enabled'] = [s for s in SCHOOLS if (s == school and enabled) or
                                (s != school and s in value['enabled'])]
        elif enabled is not None:
            raise ValueError('enabled 必须与机构一起指定')
        if native is not None:
            value['native_preference'] = native
        validate(value)
        if plan(value) != old_plan:
            value['revision'] = time.time_ns()
        if value['enabled']:
            require_configured()
        atomic_json(PATH, value)
    return value


def plan(value):
    validate(value)
    chosen = value['enabled']
    native = (value['native_preference'] if len(chosen) == 2 else chosen[0]) if chosen else None
    remote = next((s for s in chosen if s != native), None)
    return native, remote
