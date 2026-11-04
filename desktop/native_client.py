"""Small adapter to the installed aTrust client's own loopback service.

Uses the same status/logout/online requests as aTrust's local tray. Credentials
stay in the official client. TLS relaxation is confined to its loopback socket.
"""
import http.client
import json
from pathlib import Path
import ssl
from urllib.parse import urlsplit

PORT_FILE = Path('/Library/Application Support/aTrust/var/run/httpserver')


def request(action, state=None):
    if action not in ('status', 'logout', 'online'):
        raise ValueError('unsupported native action')
    details = (state or {}).get('data', {})
    addr = details.get('addr', '')
    data = {'type': 'cs', 'lang': 'zh-CN', 'guid': details.get('sdpcguid', ''),
            'addr': addr, 'token': '', 'sdpTraceId': '',
            'data': {'type': 'force'} if action == 'logout' else {}}
    port = int(PORT_FILE.read_text().strip())
    if not 1024 <= port <= 65535:
        raise ValueError('invalid aTrust loopback port')
    conn = http.client.HTTPSConnection('127.0.0.1', port, timeout=12,
                                      context=ssl._create_unverified_context())
    try:
        conn.request('POST', '/v1/service/' + action, body=json.dumps(data), headers={
            'Content-Type': 'application/json', 'Referer': addr + '/portal/' if addr else ''})
        response = conn.getresponse()
        result = json.loads(response.read(1024 * 1024))
        if response.status != 200 or result.get('code') != 0:
            # Never log the body: the official service can include account data.
            raise RuntimeError(f'aTrust {action} 未完成（代码 {result.get("code", response.status)}）')
        return result.get('data', {})
    finally:
        conn.close()


def host(state):
    return urlsplit(state.get('data', {}).get('addr', '')).hostname or ''


def disconnected(state):
    # Offline sessions can resume; require actual logout before reassignment.
    return state.get('status') in ('logout', 'unlogin')


def reconcile(school, still_current=None):
    from connections import SCHOOLS, require_configured
    require_configured()
    if school is not None:
        if not isinstance(school, str) or school not in SCHOOLS:
            raise ValueError('无效的机构选择')
    def confirm_current():
        if still_current is not None:
            still_current()
    state = request('status')
    if school is None:
        if not disconnected(state):
            confirm_current()
            request('logout', state)
            state = request('status')
        if not disconnected(state):
            raise RuntimeError('aTrust 尚未确认断开')
        return {'phase': 'stopped', 'school': None, 'message': '本机已断开'}
    config = SCHOOLS[school]
    if host(state) not in config['gateways']:
        if not disconnected(state):
            confirm_current()
            request('logout', state)
            if not disconnected(request('status')):
                raise RuntimeError('原机构仍在断开，稍后自动重试')
        return {'phase': 'login-required', 'school': school,
                'message': f"请在本机 aTrust 登录 {config['login_url']}"}
    if state.get('status') == 'offline':
        confirm_current()
        request('online', state)
        state = request('status')
    online = state.get('status') == 'online'
    return {'phase': 'online' if online else 'login-required', 'school': school,
            'message': '本机已登录' if online else f"请在本机 aTrust 登录 {config['login_url']}"}
