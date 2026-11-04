# aTrust Split

[简体中文](README.md) | **English**

**Let aTrust and Clash take their own routes.**

A routing tool for macOS: send selected internal IPs through aTrust while other traffic follows your existing Clash rules. Start with one native aTrust client; add the optional [second aTrust session](#dual-atrust) when you need another institution.

**Experimental CLI · MIT · macOS 13+ · Python 3.11+**

For menu-bar connection selection, use the [desktop app](#desktop): select institution A, B, both, or neither, and choose which runs natively when both are selected. Only generic examples are shipped; institution and business-server details stay in local configuration.

## 1. Install

Install the official macOS aTrust client and configure Clash/Mihomo. Docker is not required for the default setup.

```sh
git clone https://github.com/Chuyun-Shen/aTrust_Split.git
cd aTrust_Split
python3 -m venv --prompt atrust-split .venv
.venv/bin/python -m pip install -e .
cp config.example.toml config.local.toml
```

The virtual environment lives in the project's `.venv/` directory and displays `(atrust-split)` when activated. The commands below work without activating it first.

## 2. Set your gateway and target IPs

Edit `config.local.toml`, replacing the example gateway and IP with your own:

```toml
version = 1

[native]
name = "My institution"
gateway_hosts = ["vpn.example.org"]
targets = ["10.101.0.10"]

[routing]
poll_seconds = 2
```

Set `gateway_hosts` to the official login gateway hostnames and `targets` to individual service IPv4 addresses that should use aTrust. Gateways and service targets are separate; entire private subnets are not accepted.

```sh
.venv/bin/atrust-split check
.venv/bin/atrust-split plan
```

## 3. Configure Clash

```sh
.venv/bin/atrust-split clash
```

Merge the generated `rules` at the **beginning** of your persistent Clash rule overrides. If TUN is enabled, also append the generated `tun.route-exclude-address` entries. Preserve existing lists, use **Rule mode**, and check the effective configuration. See [routing and recovery](docs/DETAILS.md) (Chinese) for details.

## 4. Log in and start routing

Log in through the official aTrust client and confirm that internal services work, then run:

```sh
sudo .venv/bin/atrust-split routes
```

Keep the terminal open and verify both internal services and sites you normally access through Clash. Press **Ctrl-C** to stop and restore routes. After an abnormal exit, run:

```sh
sudo .venv/bin/atrust-split recover
```

Run `routes` again after the native session changes. If an older ATrust Split background service is installed, stop its routing manager normally before using this CLI.

<a id="dual-atrust"></a>

## Two aTrust sessions (optional)

Use the native aTrust client for institution A and aTrust inside an isolated Linux container for institution B. Each keeps its own authentication session. The second set of target IPs uses a separate tunnel, so individual applications do not need SOCKS settings.

### 1. Install the extra dependencies

Complete the Python setup above, then run:

```sh
brew install colima docker
cp config.dual.example.toml config.dual.local.toml
```

Install a **v2** executable for your Mac's architecture from the [official tun2socks releases](https://github.com/xjasonlyu/tun2socks/releases). Verify its checksum and note its absolute path.

Edit `config.dual.local.toml`: keep institution A's `native` settings, then fill in institution B's `container`, an authorized service `probe`, and the dedicated utun settings. The two IP lists must not overlap. The example image is ARM64; Intel Macs require a separately selected and verified AMD64 digest.

### 2. Start the container and log in

Run these commands as your normal user. The Colima profile must be a new, dedicated environment. If that name already exists, inspect it first or choose another name and update `context` in the configuration accordingly.

```sh
.venv/bin/atrust-split --config config.dual.local.toml check
colima start --profile atrust-split --arch aarch64 --vm-type vz --runtime docker --cpu 2 --memory 3 --disk 20 --activate=false --ssh-config=false
.venv/bin/atrust-split --config config.dual.local.toml build
.venv/bin/atrust-split --config config.dual.local.toml up
.venv/bin/atrust-split --config config.dual.local.toml login
```

The first build downloads dependencies. Set `dns` to match your actual Colima network. In the container desktop, enter `container.login_url` into the official aTrust client and log in. Complete browser authentication in Chromium inside the container as well. Keep the Mac's aTrust client logged in to institution A.

### 3. Enable routing for both institutions

If single-session routing is running, stop it with Ctrl-C first. Check institution B's authorized service port, merge the new Clash fragment as described above, then start routing:

```sh
.venv/bin/atrust-split --config config.dual.local.toml probe
.venv/bin/atrust-split --config config.dual.local.toml clash
sudo .venv/bin/atrust-split --config config.dual.local.toml routes --tun2socks /absolute/path/to/tun2socks
```

Replace the path in the last command with your actual installation path. Verify services at both institutions and your usual Clash connections. `probe` only checks TCP connectivity; the container tunnel does not support ping.

To stop, press Ctrl-C in the routing terminal, then run `.venv/bin/atrust-split --config config.dual.local.toml down`. To return to one aTrust session, remove the second IP list from your Clash overrides and restart with the original `config.local.toml`. After changing container settings, stop routing, then run `down --remove` followed by `up`. Data volumes are retained, but you may need to authenticate again.

<a id="desktop"></a>

## Menu-bar desktop app

`desktop/` contains the standalone **macOS 13+, Apple Silicon** app. The existing CLI remains available; do not run both routing managers together. The desktop app integrates with the **Clash Verge/Mihomo service** and is built locally with ad-hoc signing, not notarized.

### Build and configure

Install Xcode Command Line Tools and Python 3.11+, then run from the repository root:

```sh
brew install colima docker
python3 -m venv --prompt atrust-split-desktop desktop/.venv
desktop/.venv/bin/python -m pip install -r desktop/requirements.txt
bash desktop/build.sh
cp desktop/config.example.json desktop/config.local.json
```

The build downloads and verifies a pinned upstream tun2socks release. The app is written to `desktop/dist/ATrust Split.app`; building does not install services or change networking.

Edit both `profiles` in `desktop/config.local.json`, keeping IDs `a` and `b`:

- `name` / `short_name`: display labels.
- `domains`, `gateways`, `login_url`: business-domain scopes, official gateway hostnames, and full HTTPS portal URL.
- `fixed_ips`: explicit individual IPv4 system-route targets; may be empty, must not overlap between institutions.
- `probe`: a DNS health-check hostname within the business-domain scope, not a login gateway.
- `business_probe`: an authorized service with `host`, `port`, `protocol` (`ssh` or `tls`), and `name`. SSH checks the server banner; TLS verifies certificate trust and hostname. Neither sends account credentials.

After replacing every example address:

```sh
bash desktop/install.command "$PWD/desktop/config.local.json"
```

Installation requires administrator authorization. Institution data is copied only to root-managed `/Library/Application Support/ATrustSplit/settings.json`, never bundled into the app. Reinstall with the same command after editing profiles. An existing same-named app with a different bundle identity is not overwritten.

No connection is selected initially. One selection runs natively; two selections use native and isolated clients. Choose the native institution under Settings. Clearing both logs out the native session, stops the dedicated VM, restores managed routes and the Clash overlay, then closes the official client window after confirmation. A subsequently reopened client is not repeatedly closed.

Selections are stored in `~/Library/Application Support/ATrustDual/connections.json`. Quitting the menu-bar app leaves background services running; uninstall is available in Settings. SSH configuration is not modified; account credentials are not stored or moved.

### Prepare the optional container

Before selecting both institutions, run as your normal user. Do not reuse a same-named VM that serves another purpose:

```sh
colima start --profile atrust-split-desktop --arch aarch64 --vm-type vz --runtime docker --cpu 2 --memory 3 --disk 20 --activate=false --ssh-config=false
docker --context colima-atrust-split-desktop build --platform linux/arm64 \
  --build-arg BASE_IMAGE=hagb/docker-atrust@sha256:978d613b9d863a80ebaf9ea3e9cf75f2c8dfc39cadd28cac41f865a1a0eed5ca \
  --tag local/atrust-split:desktop --file desktop/container/Dockerfile desktop
```

Select both institutions and authenticate through the appropriate container desktop. Each institution has separate data volumes; changing locations may require another login. Container domains use a dedicated Clash SOCKS node; configured IPv4 targets use owned `/32` transparent routes. The container path supports IPv4 TCP only.

**Local trust boundary:** SOCKS, VNC and noVNC bind only to loopback, but SOCKS has no user authentication. Other accounts or untrusted processes on the same Mac are not isolated. Use only on a trusted single-user host; do not expose these ports.

Real authentication, business access and system-route recovery require your own end-to-end acceptance checks. A successful build or probe is not a substitute. Review [third-party terms](THIRD_PARTY_NOTICES.md) before using or distributing images.

## Scope

- The CLI manages selected IPv4 addresses and recognized aTrust routes only; automatic domain discovery, DNS management, IPv6, startup services and Windows are not supported. The desktop app additionally provides DNS routing for configured business domains and launchd services.
- A second aTrust session is optional. Its container tunnel supports IPv4 TCP only.
- This is an unofficial project. Authenticate through the official client; the repository contains no accounts, login data, or official client binaries.

[Routing and recovery (Chinese)](docs/DETAILS.md) · [Third-party notices](THIRD_PARTY_NOTICES.md)
