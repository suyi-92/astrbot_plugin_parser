# v1.6.8 Fork 运维与验收

本维护版面向 AstrBot 4.29.3 / Python 3.12 / Debian Docker，继承原项目与 MIT 许可证。
基线：`suyi-92/astrbot_plugin_parser@5286c1e9b6682635eb28dcb5bcfa25e6947a76c6`，与读取时的上游 main 相同。
原版 v1.6.1 发布提交是 `99eb31c76738fe9e865f56697d53146df153b1f2`；二者只有 README 差异。

## 从 main 安装与升级

- 插件代码：`/AstrBot/data/plugins/astrbot_plugin_parser/`
- 持久化目录：`/AstrBot/data/plugin_data/astrbot_plugin_parser/`
- B站新缓存：`/AstrBot/data/plugin_data/astrbot_plugin_parser/cache/bilibili/`
- 本地登录文件：`/AstrBot/data/plugin_data/astrbot_plugin_parser/cookies/bilibili_credential.json`

不要同时安装两个同名插件，不要把代码备份放在 `data/plugins/` 目录内。
不要清空整个插件配置或复制任何真实 Cookie 到 Git。
本次开发、测试只操作隔离测试目录及合成凭证，不连接或修改用户生产容器。

### WebUI 安装（推荐，无需先卸载）

1. 在 WebUI 停用现有万能解析器，备份其代码与插件配置文件。备份文件由管理员妥善保存，勿公开上传。
2. 修改已合并到 `main`。点击「＋ → 从链接安装」，填写
   `https://github.com/suyi-92/astrbot_plugin_parser` 并安装；首次安装和已安装原版均使用此入口。
   AstrBot 4.29.3 的安装器按相同 metadata `name` 识别已有插件，备份并替换代码，保留配置和
   `plugin_data`；安装服务会记录 Fork 为仓库安装来源。无需先卸载，不要选择删除配置或数据。
   若实际部署提示「目录已存在」，先保留现状并检查版本/日志，或按下节仅替换代码。
3. 确认 `metadata.yaml` 中 `name: astrbot_plugin_parser`、`version: v1.6.8`、`repo` 指向本 Fork。
4. WebUI 会安装 `requirements.txt` 中的依赖；另需系统提供 FFmpeg/ffprobe。安装成功后再启用插件。
5. WebUI → 插件配置 → 解析器 → B站解析器，可见 README 列出的配置；全局另有 `debug_media_info` 和 [VLESS 内核配置](VLESS_PROXY.md)。旧配置缺少新增字段时使用默认值。

### 手动替换代码（备用方式）

先在 WebUI 停用插件。以下示例在容器内执行；容器名如不是 `astrbot`，请替换。
代码备份与 staging 必须位于插件扫描目录之外。保留宿主机的 Docker `data` 卷映射。

```sh
docker exec -it astrbot sh
```

容器内：

```sh
mkdir -p /AstrBot/data/plugin-staging /AstrBot/data/plugin-backups
git clone --branch main --single-branch \
  https://github.com/suyi-92/astrbot_plugin_parser.git \
  /AstrBot/data/plugin-staging/astrbot_plugin_parser

# 如果安装目录已存在，将代码移动到唯一备份目录；首次安装跳过这一行。
mv /AstrBot/data/plugins/astrbot_plugin_parser \
  "/AstrBot/data/plugin-backups/parser-before-fork-$(date +%Y%m%d-%H%M%S)"
mv /AstrBot/data/plugin-staging/astrbot_plugin_parser \
  /AstrBot/data/plugins/astrbot_plugin_parser

python --version
python -m pip install -r /AstrBot/data/plugins/astrbot_plugin_parser/requirements.txt
ffmpeg -version
ffprobe -version
```

`python` 必须是 AstrBot 使用的 Python 3.12；若镜像使用虚拟环境，改用该环境的 Python。
若没有 pip/git/FFmpeg，在自定义 Dockerfile 中提供它们，避免重建容器后丢失；Debian 系镜像可安装 `git ffmpeg` 系统包。
不要修改系统 site-packages 中的 bilibili-api 源码。
启用插件后先匿名测试一个公开视频，再按需 `/blogin`。失败时查看域名/错误码和实际流信息，不公开带 Cookie 的配置或完整 URL。

### 后续升级

推荐 WebUI 使用 Fork 仓库来源更新，也可再次从上述链接安装；不要切换回市场中的上游来源。
在 Git 安装方式中，可停用插件后执行：

```sh
cd /AstrBot/data/plugins/astrbot_plugin_parser
git fetch origin main:refs/remotes/origin/main
git switch main
git pull --ff-only origin main
python -m pip install -r requirements.txt
```

若本地还没有 `main`，使用 `git switch -c main --track origin/main`。
若有本地修改，先备份、检查差异；不要用 `reset --hard` 覆盖未知修改。
重新启用插件。原有 Cookie、模型、聊天和数据库路径都不变。

## 回滚到原版 v1.6.1

如果已配置 `vless://`，先改为空（直连）或可用的外部 HTTP 代理入口并保存，再执行下面步骤；旧版不支持直接使用 VLESS。
自动安装的 `plugin_data/astrbot_plugin_parser/proxy_bin/` 可保留，旧版不会加载它，不需要删除任何登录凭证。

1. 停用插件，保存当前 Fork 的代码备份（移出 `data/plugins/`）。
2. 首选恢复升级前的代码备份到原安装目录；或重新克隆上游并检出以下固定提交：

```sh
git clone https://github.com/Zhalslar/astrbot_plugin_parser.git \
  /AstrBot/data/plugin-staging/astrbot_plugin_parser-v161
cd /AstrBot/data/plugin-staging/astrbot_plugin_parser-v161
git checkout --detach 99eb31c76738fe9e865f56697d53146df153b1f2
```

3. 将当前代码目录移到一个新的备份位置，再把上述目录改名移动到
   `/AstrBot/data/plugins/astrbot_plugin_parser/`；不要同时留两个可加载目录。
4. 安装原版的 `requirements.txt`，在 WebUI 启用插件。原版不具备本 Fork 的 412/567/续传修复。
5. **保留全部持久化数据与配置**；不要为回滚执行 `/blogout`。新缓存目录与旧命名隔离，原版不会当成 BV 缓存复用。
   如果需要释放缓存空间，仅在停止插件后清理确认无用的缓存；不要删除 `cookies/` 或 AstrBot 数据库。

## 下载行为与边界

每个 B站解析器拥有一个 API worker 和一个媒体会话。API 调用在 worker 中串行执行，所有 bilibili-api 全局设置仅作用于这个子进程。
没有对其他插件的 `HEADERS`、`select_client` 或请求设置做临时猴子补丁。退出登录会结束 worker 以清除其持有的旧凭证；新调用重新启动 worker 并读取当前本地登录态。
子进程增加少量启动延时和内存；容器必须允许创建 Python 子进程。API 超时后直接报错，不做激进重试。

每条流的下载尝试共享 `1 + download_retry_times` 的预算，不是每个 CDN 都再获得一整套重试。
每次最多 4 个重定向链请求；默认最多 3 次下载尝试，因此候选很多时未必遍历完全部地址；错误提示会说明“备用节点已尝试或次数已用尽”。
链接刷新由音视频共享，最多一次，且只能使用同一实际流的官方地址。风控和普通传输故障分开处理。
403/404/410 等可以尝试官方备用节点，412/429 立即停止。截断响应与超时立即切换官方候选，不在相同失败节点重复耗尽预算。不会伪装登录权限、替换 CDN 主机名或循环轮换 UA/IP。

`.part` 只在有明确版本验证器时续传；媒体长度与本地偏移必须匹配 `Content-Range`。
不同 CDN 的强 ETag 和长度相同，且来自同一 API 流标识时才接续；否则重下。
验证器缺失或 CDN 不支持 Range 时无法保证节省已下载流量，安全重下是有意行为。
无 Content-Length 的流以正常结束、逐块体积限制及 ffprobe 检查作为完成条件，不把连接异常视为完成。
平台未提供源文件哈希时无法证明端到端逐字节完整性；SHA-256 用于检测已完成本地缓存的变化，ffprobe 扫描包结构。

同一请求在单个 AstrBot 进程内通过请求维度锁完成预检、下载和索引发布；输出另有路径锁，完成文件和元数据原子发布，缓存命中复核大小和 SHA-256。
30 分钟索引按 BV、分 P、画质上限、编码偏好和不可逆账号指纹隔离，不持久化 Cookie、SESSDATA 或签名 URL。路径必须为安全缓存文件名；过期、文件缺失、哈希变化和配置变化均回退正常选流。完整缓存的清理仍由 cache_expire_hours 控制。
插件停止会取消并等待自身媒体任务、扫码请求及 FFmpeg 子进程。
**不支持多个 AstrBot 容器同时写同一个插件缓存卷**；各实例应使用独立数据目录。

`source_max_size` 单位沿用 MiB，分离音视频之和也受限；合并输出超过上限不会发送。
典型合并峰值约为输入 + 输出两份，音频兼容转换还会保留原成片和转换成片。
磁盘预检不是空间预留；多个并发视频仍可能耗尽空间，这时会明确报错。
B站完整缓存由独立 TTL 管理；普通平台继续原来的 `clean_cron` 管理。残片和未合并流按年龄及不活跃字节预算清理。

## 仅限制文件大小，不限制媒体时长

v1.6.6 起，「资源最大时长（分钟）」支持自由整数输入，`0` 表示不限媒体时长，负数也按不限处理；正数仍会限制时长，但没有原来的 60 分钟输入上限。
如果只按大小限制，将「资源最大大小」保持为所需数值（例如 `130`），将「资源最大时长」设为 `0`，保存并重载。
升级不会覆盖旧配置的 `27` 等数值，旧用户需主动改为 `0`；新安装和字段缺失时默认 `0`。
大小限制、下载连接/读取超时、单次下载请求上限与 B站整个任务上限仍按原设置执行；这里的时长指视频/音频播放长度。
回滚至 v1.6.5 或更旧版本前，将此字段改回合适的正数，旧版会把 `0` 当作 0 秒上限而非不限。

## 登录与退出

`/blogin`、`/登录B站`、`/blogout`、`/退出B站`、`/bstatus` 均有 AstrBot ADMIN 过滤器。
`/blogout` 同时清除内存 Credential、配置字段、凭证文件和未完成二维码；重复执行安全。
请求世代检查防止退出之后旧二维码响应、旧 refresh 响应写回凭证。
凭证使用同目录临时文件、`0600` 权限、fsync 和原子替换；权限/磁盘错误返回安全提示。
`/bstatus` 只表示本地存在包含 SESSDATA 的凭证，**不表示服务器端一定有效**。
匿名模式无需 Cookie，也不会为匿名解析发起登录验证请求。

## 下载慢与上传慢的区分

v1.6.3 将现有 `debug_media_info` 开关扩展为阶段诊断，无需增加配置字段。
先更新到 main 的最新 Fork，在插件配置顶部将日志等级设为 DEBUG，打开底部
“发送前记录本地视频信息（DEBUG）”，保存。诊断行以 `[解析诊断 请求编号]` 开头；
同一消息的解析、并行下载和发送任务共享编号，不记录 Cookie、完整媒体地址或异常详情。

1. 保留缓存和登录状态，等待超过防抖间隔（例如配置 60 秒就等超过 60 秒），再次发送同一链接。
2. 查看缓存预检/命中日志。30 分钟内命中且完整性复核通过时跳过播放地址请求、CDN 下载和合并，仍会获取视频信息并上传至消息平台。
3. 再选择一个未解析过的视频测试首次下载；不要清空整个插件数据目录来制造冷缓存。
4. 在 WebUI 日志中搜索 `解析诊断`，保留该编号从开始至结束的行。
   也可在自己的 Docker 主机实时筛选（`astrbot` 换成实际容器名）：

```sh
docker logs --since 2m -f astrbot 2>&1 | grep --line-buffered '解析诊断'
```

如果慢在“平台上传/发送”，另保留 AstrBot 的 `Starting chunked upload`、
`Prepared ... upload parts`、`Chunked upload completed` 三类日志，帮助区分上传准备与分片传输。
不要复制包含预签名地址、访问令牌或 Cookie 的完整调试响应。

| 日志阶段 | 可判断的耗时 |
| --- | --- |
| B站视频信息 / B站AI摘要 / B站播放地址 | API 请求和可能的本地凭证刷新；登录后还可能请求 AI 摘要 |
| B站API代理路由 / B站API返回 / B站扫码响应 | 请求实际选择的 route、use_proxy 开关、配置类型；worker_proxy 表示独立 API 进程内实际启用状态，不是出口 IP 检测 |
| 成片缓存锁等待 / 成片缓存校验 | 同资源并发等待、磁盘读取与 SHA-256 校验 |
| CDN请求 / CDN响应 | 候选域名、请求次数、Range 起始偏移、收到响应头的等待时间，以及 route / proxy_socket 代理连接证据 |
| 下载进度 | `video`/`audio` 的已收 MiB、总 MiB、该日志间隔的 KiB/s；接收数据时约每 10 秒一条，完成另记一条 |
| CDN重试 | 安全错误码、该次请求耗时及内存中已接收偏移；不表示残片一定能续传 |
| 流接收完成 | received_bytes 是本次实收量；avg_mib_s 是本次实收量除以请求至接收写入完成耗时（含响应头等待），不把续传的旧字节计入速度 |
| 完整性校验 / 缓存写入 | ffprobe、哈希与磁盘写入 |
| FFmpeg合并（copy） | 合并容器的时间，不重新编码视频 |
| 音频兼容检查/处理（视频copy） | 检查音频，必要时只转换音频 |
| 平台上传/发送 | 等待 AstrBot 适配器返回的总时间，包含上传、平台处理和发送；不能据此细分到 QQ 内部阶段 |
| 平台上传路由 | source=adapter / plugin_proxy_applied=off：插件代理未接管平台上传，不等于适配器或系统一定没有其他代理 |

`elapsed_s` 是该阶段耗时。音视频并行、父子阶段重叠，不能把全部阶段时间简单相加。
没有新数据时不会伪造进度；看最近的请求/响应记录，随后由读取超时和有限重试处理。
“平台上传/发送结束”也不代表收件人的 QQ 客户端已下载完；聊天文件卡片上的速度是客户端接收速度。
先依据日志定位再调整参数，不要把所有超时一律调大。诊断结束后关闭开关并恢复原日志等级。
要确认 VLESS 是否用于下载，查看同一请求的 `route=vless+xray` 和 CDN 响应 `proxy_socket=confirmed`；详细解释见 [代理路由诊断](VLESS_PROXY.md#确认请求是否经过代理)。
若缓存命中，没有发起 CDN 下载便不会出现 CDN 连接日志；用另一个未缓存的视频复测，保留原缓存和登录凭证。

QQ 官方[富媒体文档](https://bot.q.qq.com/wiki/develop/api-v2/server-inter/message/rich-media.html)
当前列出视频软限制 30 MB、硬限制 200 MB，超过软限制转为文件类型上传。
例如 71.51 MB 的 MP4 显示文件卡片符合该规则。`音频以文件形式上传` 只影响音频；
视频仍作为 `Video` 组件交给适配器，诊断行会标出 `component=Video`。
将 `source_max_size` 调小只会拒绝大文件，不会自动压缩；如需要更小视频，可主动选择较低画质或较短视频，
但 720P 等画质也不保证文件低于 30 MB。最终展示仍由 QQ 决定。

## QQ 画质排查

在 `debug_media_info=true` 且 AstrBot 日志等级 DEBUG 时，找“上传前本地视频”。
记录宽高、帧率、编码及字节数；不要把本地视频的签名下载地址贴到公开 Issue。
也可以在容器内对本地缓存运行：

```sh
ffprobe -v error -select_streams v:0 \
  -show_entries stream=width,height,r_frame_rate,codec_name \
  -show_entries format=size -of json /绝对路径/视频.mp4
```

本 Fork 保持抖音解析逻辑；音频转 AAC-LC 使用视频 copy。
测试检查了真实 1080×1920 / 30 FPS 合成视频在合并、音频转换前后的压缩视频流哈希相同。
AstrBot 4.29.3 的 `qqofficial_message_event.py` 本地小文件上传使用原字节 base64；大文件转给 `QQOfficialChunkedUploader` 分片，无视频缩放步骤。
OneBot v11 的视频组件保持文件 URI。生产环境 NapCat 需能访问相同文件或正确挂载目录。
这些结论不代表已经实测 QQ 服务端的最终文件；平台可能转码、限额或拒绝格式，插件无法指定最终保存分辨率。

## 自动化测试与线上验收分界

测试入口：`python -m pytest -q`；依赖：`requirements.txt`、`requirements-test.txt`，系统 `ffmpeg`/`ffprobe`。
CI 工作流在 Python 3.12 下覆盖 bilibili-api-python 17.4.1 与 17.4.2，安装 AstrBot 4.29.3。
v1.6.2 基线曾在 bilibili-api-python 17.4.1 与 17.4.2 下分别通过 106 项测试；后续新增诊断、测速和 VLESS 用例。
常规完整 pytest 使用本地可控 HTTP/HTTPS 服务与认证 CONNECT 子进程，不依赖外部节点或 Xray 下载。原真实 Xray TLS/REALITY 测试完整保留在 `tests/manual_vless_tunnel.py`，由维护者提供内核后单独运行：

```sh
XRAY_TEST_BINARY=/绝对路径/xray python -m pytest -q tests/manual_vless_tunnel.py
```

手动命令缺少内核会失败，不把跳过当作成功；真实 B站测速同样由维护者单独执行。最新执行结果以对应提交的测试记录为准。
v1.6.4 本地在 Python 3.12 / AstrBot 4.29.3 / bilibili-api-python 17.4.2 / Xray 26.10.21 下执行完整套件，结果 **193 passed**（真实隧道用例没有跳过）；Python 语法、Ruff F/E9、Shell 语法和差异空白检查通过。
v1.6.5 在相同环境执行完整套件，结果 **217 passed**（真实 Xray 下载与连接证据用例没有跳过）；上述语法、静态及差异检查也通过。
v1.6.6 在相同环境执行完整套件，结果 **232 passed**；新增媒体时长不限/自定义上限、配置保存及大小限制回归测试，语法、静态、Shell 和差异检查通过。
v1.6.8 在 Python 3.12.14 / AstrBot 4.29.3 下分别安装 bilibili-api-python 17.4.1 和 17.4.2，完整本地套件均 **300 passed、0 skipped**（启用 `-W error::ResourceWarning`）；缓存回归 40 项、CDN 故障切换回归 19 项全部通过。两个环境 compileall、全路径 Ruff F/E9、Bash 语法及差异检查通过。真实 Xray 手动套件与外网 CDN/QQ 发送未在本次执行。
启用 `-W error::ResourceWarning` 后没有资源泄漏警告；仅 AstrBot 自身 `audioop` 的 Python 3.13 弃用提示。
测试没有调用真实 B站、没有扫码真实账号、没有向 QQ 或 NapCat 发送消息。
GitHub Actions 的远端执行状态需在仓库 Actions 中查看，本地通过不等于远端 CI 已运行。

| 验证领域 | 自动化方式 |
| --- | --- |
| HTTP 412 配置 | 本地服务按 UA 返回 412/200，经过真实 bilibili-api Api 请求路径；另启动真实 worker 验证主进程配置不变 |
| 567/不可达/备用失败 | 本地 HTTP 状态模拟、无监听端口，检查次数及清晰错误 |
| 大文件续传 | 73,745,233 bytes 流在 43,122,045 bytes 处中断，验证 Range 精确偏移及最终 SHA-256 |
| Range 异常 | 200 忽略、416、错误起点/总长度/ETag/格式/长度、跨 CDN 验证器限制 |
| 取消/并发/清理 | 停止下载器、重新实例化继续、并发单写入、缓存篡改检测、过期和体积清理 |
| 大小和未知长度 | 已知/未知 Content-Length 超限、正常 chunked 结束 |
| 可选媒体时长限制 | 0/负数不限、超过 60 分钟的输入、旧值保留、JSON 往返、两小时 B站音视频、信息/播放双重时长检查、大小限制与 yt-dlp 共用上限 |
| 缓存与画质 | 旧 BV 缓存隔离、480P/1080P/编码区别、MP4 上限、刷新后变化拒绝 |
| 合并和发送 | 真 FFmpeg copy 哈希对比、音频转换保留分辨率、取消回收、下载失败提示、拒发 `.part` |
| 登录退出 | Mock 两种扫码 Cookie 响应、0600、重复退出、配置保存、迟到扫码/刷新阻断 |
| AstrBot/QQ/OneBot | 真实 4.29.3 导入/初始化/注销、真实权限过滤器、Mock QQ 上传字节对比、OneBot URI |
| 阶段诊断 | 并发请求编号隔离与子任务继承、取消传递、关闭时无输出、速度计算、567/206/缓存日志脱敏、71 MiB 文件仍为 Video 组件、等待适配器返回计时 |
| 其他平台 | 原有 Cookie/QZone/JSON 测试，抖音/快手经过真实通用下载器的本地模拟回归 |
| VLESS 代理 | 常规 CI：合成节点解析、SHA256 安装、认证 CONNECT、本地 HTTPS、失败/取消清理及 AstrBot 启停；手动：真实 Xray 本地 TLS/REALITY 隧道；均不连接用户节点 |

上线后仍应由管理员依次验证：匿名短视频 → 登录后相同视频实际画质 → 大文件断流 → QQ/NapCat 发送 → `/blogout` → 再次匿名解析。
如需要排查版本问题，先记录 commit、Python/AstrBot/bilibili-api 版本、实际流信息、域名与错误码。
不要提供 Cookie、SESSDATA、Access Token、完整签名 URL、二维码或生产数据库。

## 已知剩余风险

- B站风控、账号权限、地区及 CDN 策略会变化；默认 UA 只是一种兼容配置，不是长期通行保证。
- 新 CDN 域名不在安全名单时会被拒绝，需要维护者核实官方来源后添加；代理会影响 DNS 和路径，应仅使用信任的管理员代理。
- ETag/Last-Modified 缺失、签名过期、资源更新和 Range 不支持可能触发安全重下或需要重新解析。
- 真实扫码/刷新、完整 Docker 运维、真实 B站大视频和 QQ/NapCat 网络传输未由这些本地测试验证。
- 合成媒体测试及包结构探测不能覆盖所有容器/编码组合；QQ 对 HEVC/AV1 等的接受程度及服务端压缩由平台决定，AVC 通常更便于排查。
- API 子进程有额外资源开销；严格禁用子进程的宿主环境不适用。缓存锁以单进程单数据目录为边界。
- VLESS 需要额外 Xray 进程；自动安装需容器能访问 GitHub。节点高级扩展、出口线路及真实 CDN 速度需在用户环境验证，QQ 上传不随插件代理切换。
