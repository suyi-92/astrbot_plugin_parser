
<div align="center">

![:name](https://count.getloli.com/@astrbot_plugin_parser?name=astrbot_plugin_parser&theme=minecraft&padding=6&offset=0&align=top&scale=1&pixelated=1&darkmode=auto)

# astrbot_plugin_parser

_✨ 链接解析器 · suyi-92 Fork 维护版 v1.6.3 ✨_

[![License](https://img.shields.io/badge/License-MIT-green.svg)](https://opensource.org/licenses/MIT)
[![Python 3.12](https://img.shields.io/badge/Python-3.12-blue.svg)](https://www.python.org/)
[![AstrBot](https://img.shields.io/badge/AstrBot-4.29.3-orange.svg)](https://github.com/Soulter/AstrBot)
[![GitHub](https://img.shields.io/badge/作者-Zhalslar-blue)](https://github.com/Zhalslar)

</div>

## 📖 介绍

内置解析器适配矩阵（`—` 表示当前内置实现已禁用）：

| 平台 | 触发的消息形态 | 视频 | 图集 | 音频 | 备注 |
| --- | --- | :---: | :---: | :---: | --- |
| B 站 | `av` / `BV` / `bmBV`、视频/动态/直播/收藏夹/专栏/Opus 链接、短链 | ✅ | ✅ | ✅ | `bmBV` 提取音频；多 P 视频可在编号后指定分 P |
| 抖音 | 分享短链、电脑端链接、`aweme_id`、作品 ID | ✅ | ✅ | ❌ | 支持视频与图文作品，部分内容需要 Cookie |
| 快手 | 标准链接、`v.kuaishou.com` 短链、`chenzhongtech.com` 链接 | ✅ | ✅ | ❌ | 支持视频与图集，登录态可提高解析成功率 |
| QQ 空间 | `h5.qzone.qq.com/ugc/share`、`mobile.qzone.qq.com/l` 公开分享链接/卡片 | ✅ | ✅ | ❌ | 默认关闭；可选 SnowLuma 自动凭证或手动 Cookie，保留公开页面回退 |
| 微信视频号 | `weixin.qq.com/sph` 短链、带 `token`/`eid` 的预览长链 | ✅ | ❌ | ❌ | 短链需要腾讯元宝 Cookie；预览长链自带凭证时可直接解析 |
| 微博 | 博文、视频、`show`、文章及分享链接 | ✅ | ✅ | ❌ | Cookie 可提升受限内容和登录态页面的可用性 |
| 小红书 | 标准链接、`xhslink.com` / `xhslink.cn` 短链、卡片 | ✅ | ✅ | ❌ | 支持视频笔记与图文笔记，建议配置 Cookie |
| 小黑盒 | 社区帖子、分享链接、游戏详情链接/卡片 | ✅ | ✅ | ❌ | 支持图文混排；可配置正文、原图和视频发送方式 |
| 知乎 | 专栏、问题、回答、想法链接/卡片 | ✅ | ✅ | ❌ | 解析正文中的图片和视频，登录态可改善受限页面访问 |
| TikTok | 标准链接、`vt.tiktok.com` / `vm.tiktok.com` 短链 | ✅ | ✅ | ❌ | 照片集使用图文接口解析，失败时会在视频与图文路径间回退 |
| YouTube | 视频、Shorts、`youtu.be` 短链；`ym` + 链接 | ✅ | ❌ | ✅ | 普通链接下载视频；`ym` 前缀提取音频，通常需要 Cookie |
| Metube | YouTube 视频、Shorts、`youtu.be` 短链 | ✅ | ❌ | ❌ | 需要独立 Metube 服务；启用后接管 YouTube 视频路由，`ym` 音频仍由 YouTube 解析器处理 |
| Instagram | `p` / `reel` / `reels` / `tv` / `share` 链接 | ✅ | ✅ | ❌ | 支持帖子、图集和 Reels；Cookie 可提高私有或受限内容成功率 |
| Twitter / X | `twitter.com` / `x.com` 状态链接 | ✅ | ✅ | ❌ | 通过解析服务提取推文视频、图片和引用内容 |
| AcFun | `ac` 号或稿件链接 | ✅ | ❌ | ❌ | 下载并合并 HLS 视频流，Cookie 为可选项 |
| 网易云音乐 | 歌曲链接、`163cn.tv` 短链、MP3/外链播放地址 | ❌ | ❌ | ✅ | 支持歌曲元数据、封面和音频直链，部分资源受版权或登录态限制 |
| NGA | `ngabbs.com` / `nga.178.com` / `bbs.nga.cn` 主题链接 | ❌ | ✅ | ❌ | 返回主题首帖文本与图片；登录可见内容需要 Cookie |
| CPP 无差别同人站 | `icp.red` 短链、展品 `/d/`、图文 `/w/` 链接 | ❌ | ✅ | ❌ | 未登录时展品试阅会退化为仅封面；正文显示长度可配置 |

本插件目标：凡是链接皆可解析！尽请期待更新（如果可以,请提交PR）

---

## 🎨 效果图

插件默认启用 PIL 实现的通用媒体卡片渲染，效果图如下

<div align="center">

<img src="https://raw.githubusercontent.com/fllesser/nonebot-plugin-parser/refs/heads/resources/resources/renderdamine/video.png" width="160" />
<img src="https://raw.githubusercontent.com/fllesser/nonebot-plugin-parser/refs/heads/resources/resources/renderdamine/9_pic.png" width="160" />
<img src="https://raw.githubusercontent.com/fllesser/nonebot-plugin-parser/refs/heads/resources/resources/renderdamine/4_pic.png" width="160" />
<img src="https://raw.githubusercontent.com/fllesser/nonebot-plugin-parser/refs/heads/resources/resources/renderdamine/repost_video.png" width="160" />
<img src="https://raw.githubusercontent.com/fllesser/nonebot-plugin-parser/refs/heads/resources/resources/renderdamine/repost_2_pic.png" width="160" />

</div>

---

## 💿 Fork 安装、升级与回滚

本仓库维护自 [Zhalslar/astrbot_plugin_parser](https://github.com/Zhalslar/astrbot_plugin_parser)，
基线为 `5286c1e9b6682635eb28dcb5bcfa25e6947a76c6`（v1.6.1，与上游相同，发布提交之后只有 README 修改）。
保留插件内部名称 `astrbot_plugin_parser`、原作者署名与 [MIT 许可证](LICENSE)。
本次版本为 **v1.6.3 Fork**，没有恢复 v1.6.1 已禁用的平台；默认模板中的残留 JMComic 条目已去除。

目标环境：AstrBot **4.29.3**、Python **3.12**、Debian/Docker；需要 `ffmpeg` 和 `ffprobe`。
完整的部署、回滚和验收说明见 [Fork 运维说明](docs/FORK_MAINTENANCE.md)。

1. 先备份插件代码及插件配置，停用插件；保持 `/AstrBot/data/plugin_data/astrbot_plugin_parser/` 原样。
2. 修改已合并到本 Fork 的 **main**。在 AstrBot WebUI 点击「＋ → 从链接安装」，填写
   `https://github.com/suyi-92/astrbot_plugin_parser`，点击安装。不要从市场安装上游同名插件覆盖 Fork。
3. **AstrBot 4.29.3 无需先卸载原版**：链接安装会根据相同的 `name: astrbot_plugin_parser`
   替换旧代码，保留插件配置与 `plugin_data`，并记录新的仓库安装来源。不要勾选删除配置或数据。
   如部署版本提示目录已存在，先停止操作并查看报错，可使用运维说明中的手动代码替换步骤。
4. WebUI 安装会处理 `requirements.txt`；手动安装时使用 AstrBot 的 Python 环境安装依赖。
   确认 FFmpeg/ffprobe 可执行，再启用/重载插件。
5. 查看版本是否为 v1.6.3、`repo` 是否指向本 Fork。以后通过这个 Fork 更新，无需修改 site-packages。

回滚时停用插件，恢复备份的**插件代码**，或检出上游 v1.6.1 发布提交
`99eb31c76738fe9e865f56697d53146df153b1f2`。重新安装该版本依赖并启用。
不要删除 Cookie、插件持久化数据、聊天记录、AstrBot 数据库或模型配置；回滚不需要 `/blogout`。

## ⚙️ B站配置与工作方式

在 WebUI → 本插件配置 → **解析器 → B站解析器** 中修改下列新增字段。
旧配置缺失新字段时会自动使用默认值，不要求清空配置，不修改其他平台配置。

| 字段 | 默认值 | 作用 |
| --- | --- | --- |
| `api_client` | `aiohttp` | API 客户端，可选 `aiohttp` / `httpx` / `curl_cffi` |
| `api_user_agent` | `curl/8.14.1` | B站 API 与扫码请求 UA；不轮换 UA |
| `auto_buvid` | `false` | 是否由 bilibili-api 自动获取 buvid |
| `connect_timeout` | `15` | CDN 建连超时秒数 |
| `read_timeout` | `60` | CDN 连续无数据的超时秒数 |
| `task_timeout` | `900` | 整个下载任务上限，含锁等待、播放地址获取/刷新、下载、退避、校验和合并 |
| `partial_expire_hours` | `24` | 不活跃残片/未合并流保留小时数 |
| `cache_expire_hours` | `24` | B站完整缓存按最后使用时间保留小时数 |

全局 `debug_media_info=false`：开启后以 DEBUG 记录交给适配器前的本地视频信息，并输出带同一请求编号的
`解析诊断` 日志，涵盖信息获取、AI 摘要、缓存命中、CDN 进度/续传、校验、合并及平台上传/发送耗时。
日志等级也需设为 DEBUG；不开启时不输出新增诊断日志。复测步骤见[耗时排查](docs/FORK_MAINTENANCE.md#下载慢与上传慢的区分)。

- **412 兼容性**：默认组合来源于部分 VPS 的成功排查，不保证所有网络永久有效。
  bilibili-api 的客户端、请求头、buvid 设置是进程全局状态；本 Fork 用一个惰性启动的专用 Python 子进程执行 API 调用，
  先配置再请求，不修改 AstrBot 主进程中其他插件的设置。扫码 HTTP 会话也由本插件单独持有。
  子进程只通过匿名管道接收凭证，不使用命令行参数、环境变量或临时凭证文件传递；退出登录、取消请求、重载会结束相关子进程。
  依赖范围限定在 `bilibili-api-python >=17.4.1,<17.5.0`。
- **CDN 567/403/5xx**：从 API 的 `base_url`/`baseUrl`/`url` 和 `backup_url`/`backupUrl` 提取完整候选地址，视频/音频都可切换。
  已知问题节点 `upos-sz-mirrorcosov.bilivideo.com` 降低优先级，失败切换不依赖这个域名。
  不拼接域名、不复用其他节点的签名。`block_pcdn=true` 时排除已识别的 P/MCDN，全部被排除就报错，**不会回退到被屏蔽节点**。
- **续传**：`.part` 与 `.part.json` 留存断点；检查 206 的起点、终点、总长度和验证器。
  同地址需要强 ETag 或 Last-Modified 与已知总长度；跨地址需要相同强 ETag、相同总长度和相同流标识。
  无法确认、验证器变化、无 Content-Length/可用验证器时安全重下；200 忽略 Range 时截断重写；416 丢弃残片后重新请求。
  无长度响应必须正常 EOF、通过实时大小限制与 ffprobe 包结构检查。
  完成后写 SHA-256/大小校验记录，再供缓存复用；本地哈希不等于平台提供的源端完整性签名。
- **次数和时限**：每条流总共最多 `1 + download_retry_times` 次下载尝试（每次最多 4 个重定向链请求），CDN 切换和刷新后的请求都消耗该预算；退避 1、2、4…秒，上限 8 秒。
  鉴权/过期地址失败且候选耗尽时，整个音视频任务最多刷新一次官方播放数据。刷新后流身份变化会要求重新解析，不降画质拼接。
  `download_timeout` 仍是单次下载请求总上限；超时保留可验证断点，下一次请求可继续。
  因此它不再要求大文件在一次请求内传完；同时受新的整个任务上限约束。412/429 等明确受限响应立即停止，不轮换节点重试。
- **大小、画质和缓存**：`source_max_size` 仍以 MiB 计，限制单流、音视频之和及最终文件；`source_max_minute` 在下载前检查。
  码率预估超限会报错，不通过偷偷降清晰度规避限制。画质设置是**上限**，实际画质取决于 API 与账号权限。
  INFO 记录实际画质 ID（32=480P、64=720P、80=1080P、116=1080P60）、宽高、帧率、编码和估算字节数。
  新缓存位于 `cache/bilibili/`，标识包含实际画质、编码、媒体路径摘要和音频流；先选流再查缓存。
  旧的 `BVxxx-1.mp4` 不复用，留给原有清理周期自然失效。
- **合并与磁盘**：两条流都完成才运行 FFmpeg `-c copy`。完整性检查后原子发布最终文件，失败/取消不发送 `.part`。
  非 AAC-LC 音频兼容处理仍保持 `-c:v copy`，保存原文件和经过验证的转换文件。
  开始下载前要求至少 `2 × source_max_size + 16 MiB` 空闲空间；输入之和与输出分别限额，成功后清除中间流。
  启动时、后续下载前及有下载会话时每小时清理过期文件，并将不活跃残片/未合并流控制在约 `4 × source_max_size` 内；活跃任务不删除。
  B站独立缓存不被通用 `clean_cron` 粗暴删除，完整缓存由 `cache_expire_hours` 管理。
- **失败提示与日志**：B站下载/合并失败始终发送简短提示（即使关闭 `show_download_fail_tip`）。媒体日志只含域名、错误类型/状态码、非敏感流信息；不记录签名 URL、Cookie 或 Token。
  CDN URL 只允许已知媒体域、HTTP(S)、80/443 端口，并检查每次重定向及直接连接的 DNS 公网地址；代理须由管理员信任并正确配置。

### 抖音和 QQ 最终画质

本 Fork 没有改动抖音解析算法。`core/sender.py` 不缩放视频，必要的音频转换使用视频流 copy。
对 AstrBot 4.29.3 的 QQ 官方适配器源码检查及本地 Mock 上传测试显示：该发送路径读取本地文件并以 base64 或分片上传，没有视频缩放步骤；OneBot v11 保持原视频文件引用。
如果发送前本地文件为 1080×1920/30 FPS，而 QQ 保存的视频为 480P，仍需在真实 QQ 会话对比服务端结果。
**插件无法控制 QQ 服务端二次压缩，也没有验证 QQ 最终下载一定保持原画质。**

QQ 官方[富媒体文档](https://bot.q.qq.com/wiki/develop/api-v2/server-inter/message/rich-media.html)
当前标明视频软限制 30 MB、硬限制 200 MB，超过软限制会按文件类型上传。
因此大 MP4 显示为文件卡片不代表插件改用了 File 组件；诊断日志会记录实际提交的组件类型。
插件的 `source_max_size` 是拒绝下载的上限，不会把视频压缩到该大小；不自动转码以绕过平台规则。

## 🎉 指令

|   指令   |         权限          |        说明        |
| :------: | :-------------------: |  :---------------: |
| 开启解析 |      ADMIN            |     开启当前会话的解析功能      |
| 关闭解析 |      ADMIN            |    关闭当前会话的解析功能      |
| `/blogin` / `/登录B站` | ADMIN | 扫码获取 B站本地凭证 |
| `/blogout` / `/退出B站` | ADMIN | 清除本插件内存、配置 Cookie、凭证文件及未完成扫码；可重复执行 |
| `/bstatus` | ADMIN | 只报告本地凭证是否存在，不验证服务器端有效性 |

---

退出只清除本插件本地登录态，**不会注销 B站服务器端所有设备**。凭证文件为
`data/plugin_data/astrbot_plugin_parser/cookies/bilibili_credential.json`，原子保存并设置 `0600`；文件不存在不会导致退出失败。
新请求立即使用退出后的匿名身份；重载只关闭会话，不删除持久化凭证。管理员才可使用登录/退出/状态命令。

## 🧪 自动化测试

```sh
python3.12 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt -r requirements-test.txt
.venv/bin/python -m pytest -q
.venv/bin/python -m compileall -q main.py core tests
```

测试使用本地 HTTP 服务、合成凭证和 Mock，不需要真实 B站账号或 API。
包含精确复现 73,745,233 bytes 文件在 43,122,045 bytes 断流后的 Range 续传与 SHA-256 对比、CDN 失败切换、错误 Range/416、取消、限额、并发、低画质缓存隔离、扫码退出竞争，以及真实 FFmpeg 视频流 copy 和 AstrBot 4.29.3 生命周期测试。
完整测试范围、限制和线上验收步骤见 [运维说明](docs/FORK_MAINTENANCE.md)。

## 🧠 插件工作流程

当插件运行后，每一条消息的处理流程如下：

1. **消息接收**  
   监听所有消息事件，获取消息链与原始文本内容  
   - 支持普通文本、链接、卡片（Json 组件）

2. **基础过滤**  
   - 跳过已被禁用的会话  
   - 跳过空消息  
   - 若消息首段为 `@` 且目标不是本 Bot，则不解析

3. **链接提取与匹配**  
   - 若为卡片消息，先从 Json 中提取 URL  
   - 使用「关键词 + 正则」双重匹配，定位对应解析器  
   - 未匹配到解析规则则直接退出

4. **仲裁判定（Emoji Like Arbiter）**  
   - 仅在 `aiocqhttp` 平台生效  
   - 通过固定表情进行 Bot 间仲裁  
   - 未胜出的 Bot 自动放弃解析

5. **防抖判定（Link Debouncer）**  
   - 对同一会话内的相同链接进行时间窗口限制  
   - 命中防抖规则则跳过解析，避免短时间重复处理

6. **内容解析**  
   - 调用对应平台解析器获取媒体信息  
   - 生成统一的 `ParseResult` 数据结构

7. **媒体下载与消息构建**  
   - 下载视频 / 图片 / 音频 / 文件  
   - 根据配置决定音频发送方式  
   - 可按配置提示下载失败项

8. **卡片渲染（可选）**  
   - 在非简洁模式或无直传媒体时生成媒体卡片  
   - 使用 PIL 渲染并缓存图片

9. **消息合并与发送**  
    - 当消息段数量超过阈值时自动合并为转发消息  
    - 最终将结果发送到对应会话

---

## 🧩 扩展

插件支持自定义解析器，通过继承 `BaseParser` 类并实现 `platform`, `handle` 即可。

示例解析器请看 [示例解析器](https://github.com/Zhalslar/astrbot_plugin_parser/blob/main/core/parsers/example.py)

---

## 🎉 致谢

本项目初始版本改自 [nonebot-plugin-parser](https://github.com/fllesser/nonebot-plugin-parser) 插件，请前往原仓库给作者点个Star!

## 🤪 注意

- 有问题想第一时间交流，可前往 Astrbot 插件开发群：460973561（不点star不给进）
- 如需更丰富的解析器，这里推荐一个另外维护的分支：https://github.com/huawubot/astrbot_plugin_parser

