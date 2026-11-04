# aTrust Split

**让 aTrust 与 Clash 各走各路。**

macOS 分流工具：指定内网 IP 走 aTrust，其他流量继续使用原来的 Clash 规则。默认只用一个本机 aTrust；需要第二个机构时，再加[双 aTrust 扩展](docs/DUAL_ATRUST.md)。

**实验版 CLI · MIT · macOS 13+ · Python 3.11+**

## 1. 安装

准备官方 macOS aTrust 和已配置好的 Clash/Mihomo。默认模式不需要 Docker。

```sh
git clone https://github.com/Chuyun-Shen/aTrust_Split.git
cd aTrust_Split
python3 -m venv .venv
.venv/bin/python -m pip install -e .
cp config.example.toml config.local.toml
```

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

## 范围

- 只管理指定 IPv4 与已识别的 aTrust 路由。域名自动发现、DNS、IPv6、开机自启动和 Windows 暂不支持。
- 双 aTrust 为可选扩展，容器通道仅支持 IPv4 TCP。
- 非官方项目；使用者在官方界面完成认证，仓库不含账号、登录数据或官方客户端二进制。
- 本地测试通过，当前开源版本尚需独立测试机的实际网络验收，见[验证记录](docs/VALIDATION.md)。

[双 aTrust 扩展](docs/DUAL_ATRUST.md) · [分流与恢复](docs/DETAILS.md) · [AI 安装助手](skills/atrust-split-setup/SKILL.md) · [参与开发](CONTRIBUTING.md) · [第三方说明](THIRD_PARTY_NOTICES.md)
