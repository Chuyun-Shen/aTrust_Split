# 可选扩展：第二个 aTrust

先按 README 理解单 aTrust 与 Clash 分流。需要同时访问另一个机构时，保留本机官方 aTrust，在独立 Linux 容器中运行第二个官方客户端。两者分别认证，浏览器回调也分别留在对应环境。

容器侧通过 `tun2socks → 本机回环 SOCKS → 容器 aTrust` 建立 IPv4 TCP 通道，业务应用无需逐一配置 SOCKS。只为第二组指定 IP 增加 `/32` 路由。

## 1. 准备额外依赖与配置

```sh
brew install colima docker
cp config.dual.example.toml config.dual.local.toml
```

从 [xjasonlyu/tun2socks Releases](https://github.com/xjasonlyu/tun2socks/releases) 获取匹配 Mac 架构的 **v2** 可执行文件，核验发布摘要并记录安装绝对路径。参数参见[上游用法](https://github.com/xjasonlyu/tun2socks/wiki/Examples)。

编辑 `config.dual.local.toml`：保留你已经验证的 `native` 名单，填写第二个机构的 `container` 与 `probe`，以及独立 utun 参数。两组 IP 必须不重叠。示例固定镜像为 ARM64；Intel 需要单独选择并验证 AMD64 镜像摘要。

```sh
.venv/bin/atrust-split --config config.dual.local.toml check
.venv/bin/atrust-split --config config.dual.local.toml doctor
.venv/bin/atrust-split --config config.dual.local.toml plan
```

## 2. 建立隔离环境

以下命令用于**新的专用 profile**。已有同名环境时先检查，或配置其他名称与 Docker context，不覆盖它：

```sh
colima start --profile atrust-split --arch aarch64 --vm-type vz --runtime docker --cpu 2 --memory 3 --disk 20 --activate=false --ssh-config=false
.venv/bin/atrust-split --config config.dual.local.toml build
.venv/bin/atrust-split --config config.dual.local.toml up
.venv/bin/atrust-split --config config.dual.local.toml login
```

这些命令使用普通用户运行。首次构建需要下载依赖；示例 VM DNS 为 `192.168.5.2`，应核对本机 Colima 实际网络。

在打开的容器桌面里，将 `container.login_url` 填入 Linux aTrust 客户端并正常登录；跳到网页时在容器 Chromium 内完成，不把认证回调搬到 Mac 浏览器。账号密码、扫码或 MFA 由使用者本人完成。

`status` 只报告容器状态；`probe` 验证你配置的授权业务 TCP 端口，不等于验证账号权限：

```sh
.venv/bin/atrust-split --config config.dual.local.toml status
.venv/bin/atrust-split --config config.dual.local.toml probe
```

## 3. 启用两组分流

先在原路由终端 Ctrl-C 正常停止单 aTrust 分流。用完整双机构配置重新生成片段，并按 [分流说明](DETAILS.md) 合并两组 DIRECT / TUN 排除规则：

```sh
.venv/bin/atrust-split --config config.dual.local.toml clash > .local/clash-bypass.json
sudo .venv/bin/atrust-split --config config.dual.local.toml routes --tun2socks /absolute/path/to/tun2socks
```

验证两边实际业务、路由接口与原有 Clash 网站；不要用 ping 验收 TCP 通道。此扩展尚需独立测试机的端到端验证，见 [验证记录](VALIDATION.md)。

## 停止或退回单 aTrust

1. 路由终端 Ctrl-C，恢复本项目管理的路由并停止自己的 tun2socks。
2. `.venv/bin/atrust-split --config config.dual.local.toml down`，保留登录数据卷。
3. 需要退回单 aTrust 时，保留原 `config.local.toml`，从 Clash 覆写中移除第二组 IP 条目，再运行默认配置的 `routes`。

修改容器相关配置后，先停止路由，再执行 `down --remove` 和 `up`；数据卷保留，但仍可能需要重新认证。项目不会自动删除登录数据卷。
