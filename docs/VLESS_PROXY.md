# VLESS 节点配置（v1.6.4 起）

适用环境：AstrBot 4.29.3、Python 3.12、Debian/Docker。
VLESS 不是 aiohttp/httpx 原生支持的 HTTP 代理协议。本插件用一个受生命周期管理的 Xray 子进程，将节点转换为容器内的 HTTP 代理入口；不改 AstrBot 或系统 Python 源码。

## 在截图的配置页填写

| 配置 | 填写方法 | 默认值 |
| --- | --- | --- |
| 代理地址 `proxy` | 从客户端导出单个节点的完整 `vless://…` 分享链接，原样粘贴 | 空，直连 |
| 允许自动下载 Xray 内核 `proxy_xray_auto_download` | 没有容器内已有 Xray 时开启 | 关闭 |
| 已有 Xray 内核路径 `proxy_xray_path` | 自动下载时留空；已有内核可填 `/usr/local/bin/xray` 等容器内绝对路径 | 空 |
| 解析器 → B站解析器 → 使用代理 `use_proxy` | 开启 | 关闭 |
| 其他解析器 → 使用代理 | 仅需要经过该节点的平台开启 | 沿用现有值 |

保存后**重载插件**。不需要重新登录 B站，不用删除旧插件、Cookie 或缓存。
修改节点后也要重载，避免会话继续沿用旧代理。
只打开「使用代理」且代理地址留空，仍然直连；不会凭空生成节点。

没有已有内核时，开启自动下载便可由插件安装，**不必额外部署代理容器、改 Docker 端口映射或设置宿主机系统代理**。
日志出现 `VLESS 本地代理入口已就绪` 表示本地入口已启动；它不是远端节点连通性检测。发送一个新的视频链接检查实际解析/下载结果。

## 内核安装和生命周期

- 查找顺序：明确填写的可执行文件 → 容器 PATH 中的 `xray` → 插件已下载的固定版本 → 经开关允许后下载。
- 自动下载目前支持 Linux amd64 / arm64，使用 [XTLS/Xray-core v26.10.21 官方发布](https://github.com/XTLS/Xray-core/releases/tag/v26.10.21)，核对代码内固定的 SHA256。不会追随 `latest` 自动升级。
- 首次下载最多 180 秒，压缩包上限 32 MiB，解出的单个可执行文件上限 128 MiB。失败时移除临时文件；校验成功后原子安装、设为 `0700`。
- 安装位置为 `/AstrBot/data/plugin_data/astrbot_plugin_parser/proxy_bin/`，属于持久化数据。无需把二进制提交 Git。官方内核保留其自身 [MPL-2.0 许可证](https://github.com/XTLS/Xray-core/blob/main/LICENSE)；插件继续使用 MIT。
- 若容器无法访问 GitHub，可自行从该官方版本安装适配架构的 Xray，再填写容器内路径。路径不是宿主机路径、不是目录，也不能附加命令参数。
- 内置入口仅监听 `127.0.0.1` 的随机端口，带每次启动重新生成的认证。只由勾选「使用代理」的解析器使用；没有直连备用出口。
- 正常重载、卸载或初始化取消时停止并回收该子进程。内核退出后请求会明确失败，需检查配置并重载；不无限重启或轮换节点。

## 支持范围

接受包含 UUID、地址、端口的标准 VLESS 单节点分享链接，支持 IPv4、IPv6、域名和 URL 编码参数。
常见映射包括 `type`、`security`、`encryption`、`sni`、`fp`、`flow`、`pbk`、`sid`、`spx`、`alpn`、`path`、`host`、`serviceName`、`authority` 和 `mode`。

| 传输 | 可配安全层 | 本次真实本地测试 |
| --- | --- | --- |
| TCP / RAW | TLS、REALITY | TLS；REALITY + `xtls-rprx-vision` |
| WS / WebSocket | TLS | TLS |
| gRPC | TLS、REALITY | TLS |
| XHTTP / SplitHTTP | TLS、REALITY | TLS（auto） |
| HTTPUpgrade | TLS | TLS |

Vision flow 目前仅支持 TCP/RAW + TLS/REALITY。TLS 始终验证证书；不支持 `allowInsecure=1`。
明文 `security=none` + `encryption=none` 仅接受明确的内网/回环 IP 或 localhost；公网节点需 TLS、REALITY 或节点配置所要求的 VLESS Encryption。
VLESS Encryption、REALITY gRPC/XHTTP、ML-DSA 扩展的参数可交给内核校验，但本次没有对应远端实测，不能据此承诺所有组合可用。

不接收订阅链接、VMess/SS/Trojan 链接；暂不支持 KCP、TCP HTTP 伪装和 XHTTP `extra` 等高级扩展。
未知参数或冲突参数会明确报错，避免悄悄丢弃关键设置。复杂节点仍可由外部 Xray/sing-box 等客户端提供 HTTP 入口，再填写 `http://主机:端口`。

## 安全、作用范围与速度

节点链接包含认证信息，仍保存在 AstrBot 原有插件配置中，配置文件及备份应由管理员保管，勿公开截图、上传 Git 或发给他人。
运行时配置通过匿名标准输入交给内核，不使用包含节点链接的进程参数或临时节点配置文件。内核详细输出被抑制；插件错误仅显示固定提示，不输出 UUID、密钥、完整节点链接或签名媒体 URL。
没有新增聊天设置节点的命令，也不更改 `/blogout` 的行为。

B站 API、扫码登录、音视频下载都使用所选入口。未开启代理的平台保持直连。下载大小、时限、重试、续传、缓存及 URL 安全校验保持原有语义；经代理的目标 DNS 解析由受信任的节点负责。
**QQ 官方上传适配器不读取本插件代理设置。换 VLESS 节点只可能改善 B站等解析器的访问，不能据此保证 QQ 上传变快。**
VPS → VLESS 节点 → CDN 的任一段都可能限速；优先用同一资源、同一画质、相近时间比较直连和节点。

独立 `tools/bili_speedtest.sh` 仍只支持 HTTP 代理入口，不能把 VLESS 分享链接传给 `--proxy` / `--ask-proxy`。
本功能管理的随机本地入口也不适合跨容器或跨机器共享；需要独立脚本测速时，使用自己管理的客户端 HTTP 入口。

## 常见提示与回滚

| 提示 / 现象 | 处理 |
| --- | --- |
| 未找到 Xray | 开启自动下载或填容器内可执行文件路径，然后重载 |
| 官方下载失败 / SHA256 校验失败 | 检查容器访问 GitHub、磁盘和权限；不要跳过校验，必要时手动安装官方内核 |
| 含暂不支持的高级参数 | 使用外部客户端 HTTP 入口；不要删参数碰运气 |
| 启动失败 / 内核进程已退出 | 检查内核版本、架构、执行权限、节点参数，重载后重试 |
| 节点配置已改变 | 保存并重载插件 |
| 入口就绪但下载超时 | 入口就绪不等于节点可用；检查节点有效性、SNI / REALITY 参数、出口到目标 CDN 的连接 |
| 代理失败时其他平台还能用 | 未启用代理的解析器仍可工作；启用代理的平台不会自动直连 |

停用节点：清空代理地址或关闭相关解析器的「使用代理」，保存并重载。
回滚到 v1.6.3 或上游旧版前，先清空 VLESS 地址或换成外部 HTTP 入口，再按 [运维说明](FORK_MAINTENANCE.md) 回滚代码。
不需要删除持久化凭证、数据库或模型配置；已下载内核可留在 `proxy_bin`，旧版不会运行它。

## 测试边界

本地测试使用合成 UUID、证书和 REALITY 密钥，启动真实 Xray 客户端/服务端，经认证 HTTP CONNECT 转发 HTTPS 内容，验证正文、认证隔离、进程退出和关闭回收。
还覆盖安装哈希错误、大小限制、取消清理、旧配置默认值、HTTP 兼容、B站 worker/扫码代理入口及 AstrBot 生命周期。
测试入口：`XRAY_TEST_BINARY=/绝对路径/xray python -m pytest -q`；未提供内核路径时对应真实隧道测试会跳过。
这些测试不连接用户的节点，不证明特定 VPS/CDN 的吞吐量；实际 B站和 QQ 效果需用户环境验证。
本次完整套件结果为 **193 passed**，使用 AstrBot 4.29.3、Python 3.12、bilibili-api-python 17.4.2 和真实 Xray 26.10.21，未跳过真实隧道用例。
官方压缩包的下载、哈希校验与安装也在隔离目录完成验证；测试环境需要宿主提供的网络出口，因此单次安装验证注入该出口，不代表用户容器直连 GitHub 一定可用。插件安装器本身直连官方源，失败时可使用已有内核路径。
