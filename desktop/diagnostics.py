"""Read-only, allowlisted report. Never reads credentials, browser data or proxy config."""
import json
from pathlib import Path
import time

from version import VERSION

NATIVE_STATUS = Path('/var/run/atrust-split/status.json')
CONTAINER_STATUS = Path.home() / 'Library/Application Support/ATrustDual/status.json'
FIELDS = {'version', 'pid', 'phase', 'message', 'enabled', 'updated_at',
          'profile', 'short_name', 'clash_ready', 'managed_routes', 'tunnel',
          'health_failures', 'last_health_success', 'last_checked_at',
          'last_success_at', 'consecutive_failures', 'socks_port', 'web_port'}


def read_status(path, now):
    try:
        raw = json.loads(path.read_text())
        if not isinstance(raw, dict):
            raise ValueError('invalid status')
        value = {k: v for k, v in raw.items() if k in FIELDS and
                 (v is None or isinstance(v, (str, bool, int, float)))}
        stamp = value.get('updated_at')
        value['status_age_seconds'] = round(max(0, now - stamp), 1) if isinstance(stamp, (int, float)) else None
        return value
    except (OSError, ValueError) as error:
        return {'available': False, 'reason': type(error).__name__}


def collect():
    now = time.time()
    return {'app_version': VERSION, 'collected_at': now,
            'native': read_status(NATIVE_STATUS, now),
            'container': read_status(CONTAINER_STATUS, now),
            'connections': __import__('connections').read(),
            'routes': {'business_tcp': 'explicit configured IPv4 targets via system route',
                       'web_proxy': 'Clash'},
            'note': '状态快照，不是新的连通性测试；不含密码、订阅、登录令牌或日志全文。'}
