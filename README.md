# aTrust Split

**简体中文** | [English](README.en.md)

**让 aTrust 与 Clash 各走各路。**

macOS 分流工具：指定内网 IP 走 aTrust，其他流量继续使用原来的 Clash 规则。默认只用一个本机 aTrust；需要第二个机构时，再加[双 aTrust 扩展](#dual-atrust)。

**实验版 CLI · MIT · macOS 13+ · Python 3.11+**

需要菜单栏勾选要连接的 aTrust？使用[桌面版](#desktop)：支持机构 A／B 单选、双选、全部断开，以及双连接时切换本机机构。仓库只含通用示例，机构和业务服务器资料由使用者在本机填写。

## 1. 安装

准备官方 macOS aTrust 和已配置好的 Clash/Mihomo。默认模式不需要 Docker。

```sh
git clone https://github.com/Chuyun-Shen/aTrust_Split.git
cd aTrust_Split
python3 -m venv --prompt atrust-split .venv
.venv/bin/python -m pip install -e .
cp config.example.toml config.local.toml
```

虚拟环境保存在项目内的 `.venv/`，激活后显示为 `(atrust-split)`；下面的命令可直接运行，无须先激活。

## 2. 填写网关和 IP

编辑 `config.local.toml`，把示例网关和 IP 换成自己的：

```toml
version = 1

[native]
name = "我的机构"
gateway_hosts = ["vpn.example.org"]
targets = ["10.101.0.10"]

[routing]
poll_seconds = 2
```

`gateway_hosts` 填官方登录网关主机名；`targets` 填需要走 aTrust 的业务 IPv4 单地址。两者不能混用，也不接受整个私网网段。

```sh
.venv/bin/atrust-split check
.venv/bin/atrust-split plan
```

## 3. 配置 Clash

```sh
.venv/bin/atrust-split clash
```

将输出的 `rules` 合并到 Clash 持久化覆写规则的**前面**。开启 TUN 时，还要追加输出的 `tun.route-exclude-address`。保留原来的列表，使用 **Rule 模式**，检查最终生效配置。具体含义见[分流与恢复](docs/DETAILS.md)。

## 4. 登录并启动

在官方 aTrust 客户端正常登录，确认内网可用，再运行：

```sh
sudo .venv/bin/atrust-split routes
```

保持终端打开，分别检查内网业务和原有 Clash 网站。按 **Ctrl-C** 停止并恢复路由；异常退出后运行：

```sh
sudo .venv/bin/atrust-split recover
```

原生会话变化后需重新运行 `routes`。已有旧版 ATrust Split 后台服务时，先正常停止它的路由管理，再使用此 CLI。

<a id="dual-atrust"></a>

## 双 aTrust（可选）

本机 aTrust 登录机构 A，隔离 Linux 容器里的 aTrust 登录机构 B，各自保留认证会话。第二组指定 IP 通过独立通道访问，业务应用无需单独设置 SOCKS。

### 1. 准备额外依赖

先完成上面的 Python 安装，再运行：

```sh
brew install colima docker
cp config.dual.example.toml config.dual.local.toml
```

从 [tun2socks 官方 Releases](https://github.com/xjasonlyu/tun2socks/releases) 安装匹配 Mac 架构的 **v2** 可执行文件，核验摘要并记下绝对路径。

编辑 `config.dual.local.toml`：保留机构 A 的 `native` 配置，填写机构 B 的 `container`、授权业务探测 `probe` 和独立 utun 参数。两组 IP 不能重叠；示例镜像为 ARM64，Intel 需另选并验证 AMD64 摘要。

### 2. 启动容器并登录

以下命令使用普通用户运行。Colima profile 必须为新的专用环境；已有同名环境时先检查，或换名并同步修改配置中的 `context`。

```sh
.venv/bin/atrust-split --config config.dual.local.toml check
colima start --profile atrust-split --arch aarch64 --vm-type vz --runtime docker --cpu 2 --memory 3 --disk 20 --activate=false --ssh-config=false
.venv/bin/atrust-split --config config.dual.local.toml build
.venv/bin/atrust-split --config config.dual.local.toml up
.venv/bin/atrust-split --config config.dual.local.toml login
```

首次构建需要下载依赖；配置中的 `dns` 应与 Colima 实际网络一致。在容器桌面的官方 aTrust 中填写 `container.login_url` 并登录；网页认证也在容器 Chromium 内完成。Mac 上的 aTrust 继续登录机构 A。

### 3. 启用双机构分流

如单 aTrust 分流正在运行，先 Ctrl-C 停止。检查机构 B 的授权业务端口，将新的 Clash 片段按前述方法合并，再启动：

```sh
.venv/bin/atrust-split --config config.dual.local.toml probe
.venv/bin/atrust-split --config config.dual.local.toml clash
sudo .venv/bin/atrust-split --config config.dual.local.toml routes --tun2socks /absolute/path/to/tun2socks
```

把最后一行路径换成实际安装位置。分别验证两边业务和原有 Clash 网站；`probe` 仅检查 TCP 可达，容器通道不支持 ping。

停止时先在路由终端 Ctrl-C，再运行 `.venv/bin/atrust-split --config config.dual.local.toml down`。退回单 aTrust 时移除 Clash 中第二组 IP 条目，使用原 `config.local.toml` 重新启动即可。更改容器配置后，先停止路由，再 `down --remove`、`up`；数据卷保留，但可能需要重新认证。

<a id="desktop"></a>

## 菜单栏桌面版

`desktop/` 是独立的 macOS 13+、Apple Silicon 桌面版源码；原有 CLI 保留。桌面版面向官方 aTrust 与 **Clash Verge/Mihomo 服务模式**，不要与 CLI 路由管理同时运行。未公证，需在自己的 Mac 上构建。

### 1. 构建应用

先安装 Xcode Command Line Tools、Python 3.11+，再在仓库根目录运行：

```sh
brew install colima docker
python3 -m venv --prompt atrust-split-desktop desktop/.venv
desktop/.venv/bin/python -m pip install -r desktop/requirements.txt
bash desktop/build.sh
cp desktop/config.example.json desktop/config.local.json
```

构建脚本从上游下载并核验固定版本 tun2socks，产物位于 `desktop/dist/ATrust Split.app`；构建不会安装服务或修改网络。

### 2. 填写本机配置并安装

编辑 `desktop/config.local.json` 的两个 `profiles`，保留 `id` 为 `a`、`b`：

- `name`、`short_name`：菜单显示名称。
- `domains`、`gateways`、`login_url`：业务域名、官方网关主机名与完整 HTTPS 登录门户 URL。
- `fixed_ips`：明确需要系统路由的 IPv4 单地址，可为空；不接受网段，两机构不得重叠。
- `probe`：机构业务域名内用于检查 DNS 的主机名，不填登录网关。
- `business_probe`：自己有权访问的业务服务，含 `host`、`port`、`protocol`（`ssh` 或 `tls`）、`name`。SSH 检查协议标识；TLS 验证证书及主机名，不发送账号凭据。

所有示例地址替换完毕后：

```sh
bash desktop/install.command "$PWD/desktop/config.local.json"
```

安装需管理员授权。机构配置只复制到本机 root 管理的 `/Library/Application Support/ATrustSplit/settings.json`，不打包进应用；更改机构资料后使用上述命令重新安装。现有不同标识的同名应用不会被覆盖。

默认不连接。菜单勾选一个机构时在本机运行；两个都勾选时，一个在本机、另一个在容器；“设置 → 双连接时本机运行”选择位置。取消全部勾选会注销本机会话、停止专用虚拟机、恢复受管理路由及 Clash 覆盖配置，确认完成后正常退出官方客户端窗口。手动重新打开的客户端不会被反复关闭。

连接选择保存在本机 `~/Library/Application Support/ATrustDual/connections.json`。退出菜单栏不停止后台；卸载入口在“设置”中。应用不修改 SSH 配置，不存储或迁移机构账号密码。

### 3. 双连接前准备专用容器

以下命令以普通用户运行；不要复用已有其他用途的同名 Colima 环境：

```sh
colima start --profile atrust-split-desktop --arch aarch64 --vm-type vz --runtime docker --cpu 2 --memory 3 --disk 20 --activate=false --ssh-config=false
docker --context colima-atrust-split-desktop build --platform linux/arm64 \
  --build-arg BASE_IMAGE=hagb/docker-atrust@sha256:978d613b9d863a80ebaf9ea3e9cf75f2c8dfc39cadd28cac41f865a1a0eed5ca \
  --tag local/atrust-split:desktop --file desktop/container/Dockerfile desktop
```

在菜单同时勾选两个机构，打开对应容器桌面完成官方认证。两机构使用独立数据卷；换位置可能需要重新登录。容器域名经 Clash 专用 SOCKS 节点，明确配置的 IPv4 目标经受管理的 `/32` 透明路由；容器通道仅支持 IPv4 TCP。

**本机信任边界：** SOCKS、VNC、noVNC 只绑定 loopback，但 SOCKS 不做用户认证，不能隔离同一 Mac 的其他账户或不可信进程。只在可信单用户环境使用；不要开放端口或将此配置用于多用户共享主机。

真实机构认证、双端业务连通与系统路由恢复仍须由使用者实机验收，不能以构建或探测端口通过替代。容器镜像与官方客户端须遵守[第三方条款](THIRD_PARTY_NOTICES.md)。

## 范围

- CLI 只管理指定 IPv4 与已识别的 aTrust 路由；不支持域名自动发现、DNS 管理、IPv6、开机自启动和 Windows。桌面版另外提供按已配置业务域名的 DNS 分流与 launchd 后台。
- 双 aTrust 为可选扩展，容器通道仅支持 IPv4 TCP。
- 非官方项目；使用者在官方界面完成认证，仓库不含账号、登录数据或官方客户端二进制。

[分流与恢复](docs/DETAILS.md) · [第三方说明](THIRD_PARTY_NOTICES.md)
