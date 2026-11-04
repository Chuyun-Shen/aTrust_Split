# Third-party components

The MIT license covers this repository's own integration code and documentation.
It does not relicense Sangfor aTrust, Chromium, tun2socks or container image contents.

- [docker-easyconnect](https://github.com/docker-easyconnect/docker-easyconnect):
  container wrapper, WTFPL. The CLI patches its startup script during a local
  build; `desktop/container/start.sh` contains an adapted wrapper under the
  upstream WTFPL terms. Official images and client binaries are not committed.
  The initial reference wrapper revision is
  `e8fc56a7c518d83b6817e16713f765e3c652e3bf`.
- [tun2socks](https://github.com/xjasonlyu/tun2socks): MIT. Installed separately for
  the CLI; the desktop build downloads v2.7.0 from upstream with SHA-256 verification
  and includes its license from `desktop/vendor/tun2socks-LICENSE` in the local app.
- [Colima](https://github.com/abiosoft/colima): MIT; installed separately.
- Sangfor aTrust: proprietary software. Obtain/use it under the vendor's and
  your institution's terms. Institutional authentication and device policies
  still apply. No credentials, client binaries or login bypasses are provided.
- Debian and Chromium packages retain their respective licenses within the image.
- Python and [PyYAML](https://pyyaml.org/): their respective PSF/MIT licenses apply.
- [PyInstaller](https://pyinstaller.org/): GPL with its bootloader distribution
  exception; used only to build the locally packaged desktop runtime.

The pinned ARM64 base image digest in the example is a reference from an existing
local deployment. It is not a claim of an official Sangfor container release or
a verified AMD64 image. Review third-party terms before redistributing images.
