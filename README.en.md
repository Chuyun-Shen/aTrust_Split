# aTrust Split

[简体中文](README.md) | **English**

**Let aTrust and Clash take their own routes.**

A routing tool for macOS: send selected internal IPs through aTrust while other traffic follows your existing Clash rules. Start with one native aTrust client; add the optional [second aTrust session](#dual-atrust) when you need another institution.

**Experimental CLI · MIT · macOS 13+ · Python 3.11+**

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

## Scope

- Manages selected IPv4 addresses and recognized aTrust routes only. Automatic domain discovery, DNS management, IPv6, startup services, and Windows are not currently supported.
- A second aTrust session is optional. Its container tunnel supports IPv4 TCP only.
- This is an unofficial project. Authenticate through the official client; the repository contains no accounts, login data, or official client binaries.
- Local tests pass. This open-source implementation still needs real-network validation on a separate test machine. See the [validation record](docs/VALIDATION.md) (Chinese).

[Routing and recovery (Chinese)](docs/DETAILS.md) · [Contributing](CONTRIBUTING.md) · [Third-party notices](THIRD_PARTY_NOTICES.md)
