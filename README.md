# aTrust Split

**简体中文** | [English](README.en.md)

**让 aTrust 与 Clash 各走各路。**

macOS 分流工具：指定内网 IP 走 aTrust，其他流量继续使用原来的 Clash 规则。默认只用一个本机 aTrust；需要第二个机构时，再加[双 aTrust 扩展](#dual-atrust)。

**实验版 CLI · MIT · macOS 13+ · Python 3.11+**

## 用 AI 助手安装

将下面这段提示发给能操作本机终端的 AI agent：

```text
请帮我安装并配置 https://github.com/Chuyun-Shen/aTrust_Split 。
先阅读 README，检查本机兼容性，并在项目内创建显示名称为 atrust-split 的 .venv 环境。
默认配置一个本机 aTrust 与 Clash 分流；缺少官方网关、业务 IP 或 Clash 模式时再询问我。
仅在我需要第二个 aTrust 时启用双机构扩展。保留现有 Clash 订阅、节点和无关规则，修改前备份；已有路由管理器时先检查兼容与恢复方式。
账号登录、扫码和 MFA 由我在官方界面完成。完成后验证内网业务和原有 Clash 网络，并告诉我停止与恢复的方法；未验证的步骤请明确说明。
```

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

## 范围

- 只管理指定 IPv4 与已识别的 aTrust 路由。域名自动发现、DNS、IPv6、开机自启动和 Windows 暂不支持。
- 双 aTrust 为可选扩展，容器通道仅支持 IPv4 TCP。
- 非官方项目；使用者在官方界面完成认证，仓库不含账号、登录数据或官方客户端二进制。
- 本地测试通过，当前开源版本尚需独立测试机的实际网络验收，见[验证记录](docs/VALIDATION.md)。

[分流与恢复](docs/DETAILS.md) · [参与开发](CONTRIBUTING.md) · [第三方说明](THIRD_PARTY_NOTICES.md)
