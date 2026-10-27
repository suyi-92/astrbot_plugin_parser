# B站代理配置与独立 CDN 测速

## 在 AstrBot 配置代理

全局「代理地址」填入真实的 HTTP 代理服务入口，例如 `http://代理主机:7890`，再打开「B站解析器 → 使用代理」并保存。
地址为空时仍是直连；单独打开开关不会创建代理。用户名/密码认证格式为 `http://用户名:密码@代理主机:端口`，特殊字符须按 URL 编码。
这里需要支持 HTTPS CONNECT 的 HTTP 代理端口，不是订阅链接、`ss://`、`vmess://` 或 `socks5://` 链接；可以使用代理软件的 HTTP/mixed 入口。
代理认证信息只在本机配置，分享截图时遮盖。

API 客户端保持 `aiohttp`、API User-Agent 保持 `curl/8.14.1`、自动 buvid 关闭。
配置影响本插件启用了「使用代理」的解析器；B站 API 和媒体下载都会使用该入口。
**QQ 官方上传适配器不读取这个插件的代理设置，B站代理不会自动改变 QQ 上传线路。**

Docker 地址选择：

| 代理服务所在位置 | 代理地址 |
| --- | --- |
| 可访问的独立香港/其他服务器 | `http://实际地址:HTTP端口` |
| 同一个自定义 Docker 网络的代理容器 | `http://代理服务名:HTTP端口` |
| Linux Docker 宿主机 | `http://host.docker.internal:HTTP端口`，需要下列映射 |
| AstrBot 同一容器，或 Linux host 网络且代理在宿主机 | 才可以使用 `http://127.0.0.1:HTTP端口` |

宿主机代理场景可在 Compose 的 AstrBot 服务下添加（重建容器后生效）：

```yaml
extra_hosts:
  - "host.docker.internal:host-gateway"
```

代理还须监听容器可达的宿主机接口，而非仅监听宿主机 `127.0.0.1`；访问范围限制为需要的容器。
参考 [Docker 网络文档](https://docs.docker.com/compose/how-tos/networking/) 与 [aiohttp 代理说明](https://docs.aiohttp.org/en/stable/client_advanced.html#proxy-support)。

## 在其他 VPS 一行测速

需要 Bash、curl、Python 3.11+ 和 venv。Debian/Ubuntu 若缺少 venv，可先安装 `python3-venv`。
在准备比较的每台 VPS 上运行相同命令：

```bash
curl -fLsS --connect-timeout 15 --max-time 60 https://raw.githubusercontent.com/suyi-92/astrbot_plugin_parser/main/tools/bili_speedtest.sh | bash -s --
```

不传 BV 参数时，默认使用长视频 `BV1Ke411W7YG` 的第 1 P；[视频页面](https://www.bilibili.com/video/BV1Ke411W7YG/)列出的单 P 时长为 2:05:06。
仍可在命令末尾加 BV 号指定自己的样本。原来的 `BV1MsnG6TEYy` 匿名流只有约 13.4 MiB，单独调大读取上限不会把它变成长文件。
以每次 API 和 CDN 实际返回为准：如果响应资源不足采样上限，结果会标记「小文件样本」，不会反复下载小文件来凑样本量。

在末尾加 `--ask-proxy` 可隐藏输入 HTTP 代理地址并测试该线路，命令历史中不必出现代理密码。
也支持显式环境变量 `BILI_TEST_PROXY`；未配置时忽略环境中 `HTTP_PROXY` 等变量，使用直连。
代理参数只影响测速中的 B站请求，不影响前面的 GitHub/PyPI 工具下载。
命令只从 Fork 和 PyPI 获取工具/依赖；不使用第三方镜像绕过下载失败。

启动器在独立临时目录创建虚拟环境，安装 bilibili-api-python 17.4.2 与 aiohttp 3.x；不会修改系统 Python、AstrBot 依赖、Cookie 或插件配置。
启动准备时间不计入测速，正常安装的提示/弃用警告不显示；安装失败仍会简短报错。
首次运行会安静准备依赖，再开始逐个采样；需要等待节点采样结束才输出该节点的一行速度。
完成、失败或 Ctrl+C 退出后清理临时目录；可直接按 **Ctrl+C** 停止。
已有这些依赖时，也可以直接运行仓库中的 `python tools/bili_speedtest.py`。

默认参数：最高画质 116、只选 AVC、匿名、每节点最多 120 秒/读取 256 MiB、最多 4 个候选，串行单连接采样。
候选只来自 B站播放 API，沿用插件的优先级，不替换域名或拼接签名。每候选只尝试一次，不做下载重试或续传；遇 412/429 立即停止整个测试。
采样上限按单个连续响应计数，不跨多次下载累计；每个节点到时或达到字节上限即停止读取。
默认最多读取约 1 GiB 的媒体有效数据（4 个候选均达到 256 MiB），最长采样时间约 480 秒，另加 API 获取地址最多 45 秒；Python 依赖准备不包括在此时间内。
媒体数据读取后丢弃，不生成完整视频，不消耗生产缓存，也不运行 FFmpeg 或发送 QQ 消息。

可选参数：`--seconds 180`、`--mib 512`、`--quality 80`、`--page 1`、`--max-cdns 2`、`--block-pcdn`、`--json`。
秒数限制 5–300，采样量 1–1024 MiB，候选数 1–4；更长测试会增加流量，仍不代表整个大文件能稳定下载。

默认不会读取本机任何登录凭证，所以实际可能只有 480P；必须比较输出的实际画质、编码、总长度，不能将匿名结果当作已登录 1080P+ 的等价测量。
只有在自己的受信任服务器上需要同登录态对比时，才显式添加 `--credential-file /本机/凭证文件.json`。
该文件只读，不刷新、不保存、不打印凭证；不要为测速把生产 Cookie 复制给不受信任的机器。

## 如何读输出

默认只输出各 CDN 一行：`域名: 数值 MiB/s`，速度包含连接和响应头等待时间。
没有进度行、设置说明或 JSON。HTTP 错误会显示「不可用（HTTP567）」等短原因；断流时保留速度并标记「中断」，不把有速度的失败请求当成成功。
只有实际资源比采样上限小，才附加「小文件样本：实际大小」，避免把短样本当成持续带宽。

需要详细排查时，在命令末尾添加 `--json` 恢复以下字段和进度日志：

| 字段/事件 | 含义 |
| --- | --- |
| `settings` | 实际 API 客户端、UA、buvid 开关、依赖版本；代理仅显示 enabled/direct |
| `selected_video` | API 实际返回并选中的画质、宽高、帧率、编码、码率及候选数量 |
| `headers_s` | 请求开始到响应头的时间，包含连接建立等开销 |
| `speed_mib_s` | 收到响应头之后的平均读取速度，MiB/s；与插件的 KiB/s 需乘/除 1024 比较 |
| `effective_mib_s` | 包含等待响应头的整个请求平均速度，MiB/s |
| `received_bytes` / `total_bytes` | 实际读取量 / 资源总长度；无长度响应正常读完后按实际读取量记录，尚未知时为 null |
| `small_source` | 已知资源长度是否不足本次采样上限；正常读完的无长度响应按实际读取量判断 |
| `sample-time-limit` / `sample-byte-limit` | 达到主动采样上限，不代表网络错误或整片下载成功 |
| `ClientPayloadError` / `ServerDisconnectedError` | 本次采样中实际遇到响应体异常/连接断开；即使有速度也需看错误 |
| `HTTP403` / `HTTP567` / `HTTP503` | 当前节点请求失败，继续下一个官方候选 |
| `complete` | HTTP 响应正常读完；没有执行媒体完整性校验 |

约 `0.1 MiB/s` 相当于 `102 KiB/s`；`1 MiB/s` 相当于约 `8.39 Mbps`。
优先比较同一 BV、分 P、实际画质/编码、相同代理模式及相近时段的数据。每台 VPS 都重新获取自己的官方播放地址；不同出口可能获得不同节点。
短采样无法证明整片无断流，不要把 5 MiB 的高速结果直接外推到 100 MiB 全程。
普通网站 ping 或 Speedtest 的结果也不能代替实际 CDN 结果；本工具不测 QQ 上传速度。

日志只输出域名、非敏感流信息、测量值及简短错误码，不输出完整下载 URL、Cookie、签名或代理密码。
测试依赖本地模拟 HTTP 服务，覆盖单响应连续读取 256 MiB、小文件不循环凑量、精简输出与 JSON 切换、安装警告隐藏/失败提示、567/备用地址、短响应体、采样上限、取消、HTTP 代理、412/429 停止、安全 URL 检查及日志脱敏。
长视频页面的时长已查阅；开发环境无法解析 B站 API 域名，尚未在这里实测该长视频的匿名媒体总大小和真实速度，工具会以用户 VPS 的实际响应检查大小并给出结果。
