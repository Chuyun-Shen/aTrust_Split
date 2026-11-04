# Third-party components

The MIT license covers this repository's own integration code and documentation.
It does not relicense Sangfor aTrust, Chromium, tun2socks or container image contents.

- [docker-easyconnect](https://github.com/docker-easyconnect/docker-easyconnect):
  container wrapper, WTFPL. The image's startup script is patched during a local
  build; this repository does not redistribute the original image or client.
  The initial reference wrapper revision is
  `e8fc56a7c518d83b6817e16713f765e3c652e3bf`.
- [tun2socks](https://github.com/xjasonlyu/tun2socks): MIT; installed separately.
- [Colima](https://github.com/abiosoft/colima): MIT; installed separately.
- Sangfor aTrust: proprietary software. Obtain/use it under the vendor's and
  your institution's terms. Institutional authentication and device policies
  still apply. No credentials, client binaries or login bypasses are provided.
- Debian and Chromium packages retain their respective licenses within the image.

The pinned ARM64 base image digest in the example is a reference from an existing
local deployment. It is not a claim of an official Sangfor container release or
a verified AMD64 image. Review third-party terms before redistributing images.
