"""Fetch the pinned public tun2socks release; never import local VPN assets."""
import hashlib
import io
from pathlib import Path
import platform
import urllib.request
import zipfile

VERSION = 'v2.7.0'
SHA256 = '7c5ebfe2ffb60ecf6e958cc5bbf3e06e74b8b33575ffbb4ba4f6f785a647f1ad'
BINARY_SHA256 = '1b7e27ddecb688bc2ec56f7d13f6a3f8aba09538918ae255566a290fab78f16b'
URL = f'https://github.com/xjasonlyu/tun2socks/releases/download/{VERSION}/tun2socks-darwin-arm64.zip'


def main():
    if platform.system() != 'Darwin' or platform.machine() != 'arm64':
        raise RuntimeError('The desktop build currently supports Apple Silicon macOS only')
    path = Path(__file__).parent / 'vendor/tun2socks'
    if path.is_file() and hashlib.sha256(path.read_bytes()).hexdigest() == BINARY_SHA256:
        path.chmod(0o755)
        print('Verified cached tun2socks v2.7.0 darwin-arm64 executable SHA-256')
        return
    request = urllib.request.Request(URL, headers={'User-Agent': 'atrust-split-source-build'})
    with urllib.request.urlopen(request, timeout=60) as response:
        archive = response.read()
    if hashlib.sha256(archive).hexdigest() != SHA256:
        raise RuntimeError('tun2socks release SHA-256 mismatch; nothing installed')
    with zipfile.ZipFile(io.BytesIO(archive)) as bundle:
        candidates = [name for name in bundle.namelist() if Path(name).name == 'tun2socks-darwin-arm64']
        if len(candidates) != 1:
            raise RuntimeError('Unexpected tun2socks release contents')
        executable = bundle.read(candidates[0])
    temporary = path.with_suffix('.tmp')
    temporary.write_bytes(executable)
    temporary.chmod(0o755)
    temporary.replace(path)
    print('Verified tun2socks v2.7.0 darwin-arm64 release SHA-256')


if __name__ == '__main__':
    main()
