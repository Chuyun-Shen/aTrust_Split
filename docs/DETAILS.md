# 分流与恢复

## 两层分流

1. **Clash**：指定 IP 优先命中 DIRECT；TUN 模式还需排除这些 IP。
2. **系统路由**：本工具为指定 IP 建立 aTrust `/32` 路由，并收窄同一原生接口上已识别的宽泛 IPv4 路由。

两层都需要配置。DIRECT 的含义是交给系统路由；只设置 DIRECT，不能阻止 aTrust 的宽泛路由接管其他流量。

`atrust-split clash` 输出 JSON，可作为 YAML 读取。单 aTrust 示例等价于：

```yaml
rules:
  - IP-CIDR,10.101.0.10/32,DIRECT,no-resolve
tun:
  route-exclude-address:
    - 10.101.0.10/32
```

在客户端的持久化覆写/扩展中将精确规则放在 MATCH 和其他宽泛规则前面；TUN 排除地址追加到原列表。检查最终配置，避免订阅更新后丢失。项目不自动改写 Clash 文件，也不读取订阅或控制 API。

保持 Rule 模式。Global 模式、DIRECT 固定出口接口、应用自带其他代理或强制绑定网卡，均需单独检查。直接访问 IP 不依赖机构 DNS；业务若要求域名、HTTPS Host/SNI 或 DNS 发现，需要另外配置正确解析，且目标应落在白名单内。

参考：[Mihomo 路由规则](https://wiki.metacubex.one/config/rules/)、[TUN 排除地址](https://wiki.metacubex.one/config/inbound/tun/)。

## 检查

```sh
.venv/bin/atrust-split doctor
route -n get 10.101.0.10 # 换成自己的业务 IP
route -n get default
```

业务目标应使用识别出的 aTrust 接口。再检查真实业务、Clash 连接日志和一个原来可用的代理网站。

网关、业务目标、TCP 端口可达与账号认证是不同事实。缺少一次 TCP 响应不能单独证明“未登录”；掉线应结合官方客户端日志和授权业务探测判断。

## 路由归属与恢复

原生检测要求官方网关匹配，并同时满足已知 Sangfor 接口特征与点对点地址。特征来自一个 macOS 部署，不保证所有版本；识别失败时拒绝猜测。

每次改动前记录 root 私有 journal；新增主机路由带 PROTO1 标记，保存接口名、索引、地址、启动标识和网关。既有主机路由不被收为本项目所有；同地址已属于其他接口时拒绝覆盖。只移除可确认的原生接口路由，不清空路由表。

正常退出先恢复仍属于同一会话的原生路由，再删除自己新增的路由。恢复失败会保留 journal。重启、网关变化或接口复用后不重放旧会话路由；`recover` 与路由进程共用锁。

恢复状态位于 `/var/db/atrust-split/`。停止分流后，官方 aTrust 可能重新接管宽泛网段；需要退出 VPN 时在官方客户端登出。停止使用本工具时，手动移除 Clash 覆写中的精确条目。本项目不是全局断网保护开关。

## 第二个 aTrust 的实现

可选容器只发布回环 SOCKS/noVNC。SOCKS guard 仅接受白名单 IPv4 TCP，检查目标路由指向容器 aTrust 接口，并在连接前用 SO_BINDTODEVICE 绑定该接口。Mac 的 tun2socks 通过独立 utun 接入这些目标，代理出口绑定 lo0。

Docker 命令使用普通用户，路由命令使用 sudo。容器和数据卷有归属标签；配置变化需明确重建。Ctrl-C 会恢复路由并停止自己的 tun2socks。SIGKILL 后需 `recover`；若留下子进程，核对后手动停止，不凭过期 PID 杀进程。
