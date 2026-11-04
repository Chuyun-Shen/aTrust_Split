---
name: atrust-split-setup
description: 配置或排查本仓库的 macOS aTrust 与 Clash 分流；默认一个本机 aTrust，需要第二个机构时再启用容器扩展。
---

# aTrust Split 安装助手

定位本仓库并读取 README.md。默认使用 `config.example.toml`：一个本机官方 aTrust，与现有 Clash 共存。只有用户需要第二个 aTrust 时，才按 README 的“双 aTrust（可选）”章节使用双机构示例。

- 确认 macOS、Python、官方 aTrust、Clash 模式和既有路由管理器。单 aTrust 不要求 Docker、Colima 或 tun2socks。
- 获取官方网关主机名、明确的业务 IPv4 单地址。配置写入被忽略的 `*.local.toml`；密码、扫码、MFA 由用户在官方客户端完成。
- 先执行 `check`、`doctor`、`plan`。生成 Clash 片段后，在持久化覆写中合并精确 DIRECT 规则和 TUN 排除项，保留已有配置并核对实际生效结果。
- 按用户授权启动 `routes`。旧路由管理器需正常恢复/停止后再切换；原机导出或代码审阅任务不切换网络。
- 同时验证内网业务、原有 Clash 网站、实际接口和 Ctrl-C 恢复。异常恢复参见 `docs/DETAILS.md`；不清空路由表。

第二个 aTrust 的网页认证留在容器浏览器内。容器通道只支持 IPv4 TCP，两组目标不能重叠。容器运行、TCP 可达与账号认证需分别判断，不仅凭接口或一次探测推断登录状态。

交付说明实际配置、已做检查、未验证项和恢复命令。实机验收范围见 `docs/VALIDATION.md`；不把本地单元测试写成网络验收。
