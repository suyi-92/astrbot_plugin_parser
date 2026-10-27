# main.py

import asyncio
import re

from astrbot.api import logger
from astrbot.api.event import filter
from astrbot.api.star import Context, Star
from astrbot.core import AstrBotConfig
from astrbot.core.message.components import At, Image, Json, Plain, Reply
from astrbot.core.platform.astr_message_event import AstrMessageEvent
from astrbot.core.platform.message_type import MessageType
from astrbot.core.platform.sources.aiocqhttp.aiocqhttp_message_event import (
    AiocqhttpMessageEvent,
)

from .core.arbiter import ArbiterContext, EmojiLikeArbiter
from .core.clean import CacheCleaner
from .core.config import PluginConfig
from .core.debounce import Debouncer
from .core.diagnostics import note, stage, trace_scope
from .core.download import Downloader
from .core.exception import ParseException
from .core.parsers import BaseParser, BilibiliParser
from .core.parsers.bilibili.login import BilibiliLogin
from .core.render import Renderer
from .core.sender import MessageSender
from .core.utils import extract_json_url


class ParserPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.cfg = PluginConfig(config, context=context)
        # 渲染器
        self.renderer = Renderer(self.cfg)
        # 下载器
        self.downloader = Downloader(self.cfg)
        # 防抖器
        self.debouncer = Debouncer(self.cfg)
        # 仲裁器
        self.arbiter = EmojiLikeArbiter()
        # 消息发送器
        self.sender = MessageSender(self.cfg, self.renderer)
        # 缓存清理器
        self.cleaner = CacheCleaner(self.cfg)
        # 关键词 -> Parser 映射
        self.parser_map: dict[str, BaseParser] = {}
        self._offline_bili_login = None
        # 关键词 -> 正则 列表
        self.key_pattern_list: list[tuple[str, re.Pattern[str]]] = []

    async def initialize(self):
        """加载、重载插件时触发"""
        # 加载渲染器资源
        await asyncio.to_thread(Renderer.load_resources)
        # 注册解析器
        self._register_parser()

    async def terminate(self):
        """插件卸载时触发"""
        await self.sender.close()
        # 关所有解析器里的会话 (去重后的实例)
        unique_parsers = set(self.parser_map.values())
        for parser in unique_parsers:
            await parser.close_session()
        if self._offline_bili_login:
            await self._offline_bili_login.close()
        await self.downloader.close()
        # 关缓存清理器
        await self.cleaner.stop()

    def _register_parser(self):
        """注册解析器（以 parser.enable 为唯一启用来源）"""
        # 所有 Parser 子类
        all_subclass = BaseParser.get_all_subclass()
        enabled_platforms = set(self.cfg.parser.enabled_platforms())

        enabled_classes: list[type[BaseParser]] = []
        enabled_names: list[str] = []
        for cls in all_subclass:
            platform_name = cls.platform.name

            if platform_name not in enabled_platforms:
                logger.debug(f"[parser] 平台未启用或未配置: {platform_name}")
                continue

            enabled_classes.append(cls)
            enabled_names.append(platform_name)

            # 一个平台一个 parser 实例
            parser = cls(self.cfg, self.downloader)

            # 关键词 → parser
            for keyword, _ in cls._key_patterns:
                self.parser_map[keyword] = parser

        logger.debug(f"启用平台: {'、'.join(enabled_names) if enabled_names else '无'}")

        # -------- 关键词-正则表（统一生成） --------
        patterns: list[tuple[str, re.Pattern[str]]] = []

        for cls in enabled_classes:
            for kw, pat in cls._key_patterns:
                patterns.append((kw, re.compile(pat) if isinstance(pat, str) else pat))

        # 长关键词优先，避免短词抢匹配
        patterns.sort(key=lambda x: -len(x[0]))

        self.key_pattern_list = patterns

        logger.debug(f"[parser] 关键词-正则对已生成: {[kw for kw, _ in patterns]}")

    def _get_parser_by_type(self, parser_type):
        for parser in self.parser_map.values():
            if isinstance(parser, parser_type):
                return parser
        raise ValueError(f"未找到类型为 {parser_type} 的 parser 实例")

    def _get_bilibili_login(self):
        for parser in self.parser_map.values():
            if isinstance(parser, BilibiliParser):
                return parser.login
        # Administrators can still remove local credentials when parsing is disabled.
        if self._offline_bili_login is None:
            self._offline_bili_login = BilibiliLogin(self.cfg)
        return self._offline_bili_login

    @filter.event_message_type(filter.EventMessageType.ALL)
    async def on_message(self, event: AstrMessageEvent):
        """消息的统一入口"""
        umo = event.unified_msg_origin

        # 白名单
        if self.cfg.whitelist and umo not in self.cfg.whitelist:
            return

        # 黑名单
        if self.cfg.blacklist and umo in self.cfg.blacklist:
            return

        # 消息链
        chain = event.get_messages()
        if not chain:
            return

        text = event.message_str

        # 指定机制：专门@其他bot的消息不解析
        self_id = event.get_self_id()
        mentioned_ids: set[str] = set()
        for seg in chain:
            if isinstance(seg, At):
                mentioned_ids.add(str(seg.qq))
            elif isinstance(seg, Plain):
                mentioned_ids.update(re.findall(r"<@!?([^>\s]+)>", seg.text))
        if (
            self.cfg.require_at_in_group
            and not isinstance(event, AiocqhttpMessageEvent)
            and event.get_message_type() == MessageType.GROUP_MESSAGE
            and self_id not in mentioned_ids
        ):
            return
        if (
            mentioned_ids
            and self_id not in mentioned_ids
            and "qq_official" not in mentioned_ids
        ):
            return

        # 卡片解析：扫描整条消息链，兼容 @ + JSON 卡片等组合消息。
        for seg in chain:
            if not isinstance(seg, Json):
                continue
            parsed_url = extract_json_url(seg.data)
            logger.debug(f"解析Json组件: {parsed_url}")
            if parsed_url:
                text = parsed_url
                break

        # 引用解析
        reply_seg = next((seg for seg in chain if isinstance(seg, Reply)), None)
        if self.cfg.enable_reply_parse and reply_seg and reply_seg.chain:
            reply_texts = []
            for seg in reply_seg.chain:
                if isinstance(seg, Plain):
                    reply_texts.append(seg.text)
                elif isinstance(seg, Json):
                    reply_texts.append(extract_json_url(seg.data))
            if reply_texts:
                reply_text = "".join(t for t in reply_texts if t)
                if reply_text:
                    text = reply_text

        if not text:
            return

        # 核心匹配逻辑 ：关键词 + 正则双重判定，汇集了所有解析器的正则对。
        keyword: str = ""
        searched: re.Match[str] | None = None
        for kw, pat in self.key_pattern_list:
            if kw not in text:
                continue
            if m := pat.search(text):
                keyword, searched = kw, m
                break
        if searched is None:
            return
        logger.debug(f"匹配结果: {keyword}, {searched}")

        # 仲裁机制
        if isinstance(event, AiocqhttpMessageEvent) and not event.is_private_chat():
            raw = event.message_obj.raw_message
            if not isinstance(raw, dict):
                logger.warning(f"Unexpected raw_message type: {type(raw)}")
                return
            is_win = await self.arbiter.compete(
                bot=event.bot,
                ctx=ArbiterContext(
                    message_id=int(raw["message_id"]),
                    msg_time=int(raw["time"]),
                    self_id=int(raw["self_id"]),
                ),
            )
            if not is_win:
                logger.debug("Bot在仲裁中输了, 跳过解析")
                return
            logger.debug("Bot在仲裁中胜出, 准备解析...")

        # 基于link防抖
        link = searched.group(0)
        if self.debouncer.hit_link(umo, link):
            logger.warning(f"[链接防抖] 链接 {link} 在防抖时间内，跳过解析")
            return

        with trace_scope(self.cfg.debug_media_info):
            with stage(self.cfg.debug_media_info, "解析到发送总流程"):
                await self._handle_match(event, keyword, searched)

    async def _handle_match(self, event, keyword, searched):
        # Keep parsing and its child media/send tasks under one diagnostic trace.
        try:
            with stage(self.cfg.debug_media_info, "解析信息"):
                parse_res = await self.parser_map[keyword].parse(keyword, searched)
        except ParseException as exc:
            await event.send(event.plain_result(str(exc)))
            return

        # 基于资源ID防抖
        resource_id = parse_res.get_resource_id()
        if self.debouncer.hit_resource(event.unified_msg_origin, resource_id):
            note(self.cfg.debug_media_info, "资源防抖命中，跳过发送")
            logger.warning(f"[资源防抖] 资源 {resource_id} 在防抖时间内，跳过发送")
            return

        # 发送
        await self.sender.send_parse_result(event, parse_res)

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("开启解析")
    async def open_parser(self, event: AstrMessageEvent):
        """开启当前会话的解析"""
        umo = event.unified_msg_origin
        self.cfg.remove_blacklist(umo)
        yield event.plain_result("当前会话的解析已开启")

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("关闭解析")
    async def close_parser(self, event: AstrMessageEvent):
        """关闭当前会话的解析"""
        umo = event.unified_msg_origin
        self.cfg.add_blacklist(umo)
        yield event.plain_result("当前会话的解析已关闭")

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("登录B站", alias={"blogin", "登录b站"})
    async def login_bilibili(self, event: AstrMessageEvent):
        """扫码登录B站"""
        login = self._get_bilibili_login()
        try:
            qrcode = await login.login_with_qrcode()
            yield event.chain_result([Image.fromBytes(qrcode)])
            async for msg in login.check_qr_state():
                yield event.plain_result(msg)
        except ParseException as exc:
            yield event.plain_result(str(exc))

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("blogout", alias={"退出B站", "退出b站"})
    async def logout_bilibili(self, event: AstrMessageEvent):
        """只清除本插件本地登录态，不注销B站服务器端其他设备。"""
        login = self._get_bilibili_login()
        try:
            existed = await login.logout()
            yield event.plain_result(
                "已清除本插件的B站本地凭证及扫码状态"
                if existed
                else "当前没有B站本地登录凭证，扫码状态已清除"
            )
        except ParseException as exc:
            yield event.plain_result(str(exc))

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("bstatus")
    async def status_bilibili(self, event: AstrMessageEvent):
        """只报告本地凭证是否存在，不验证服务器端会话。"""
        present = self._get_bilibili_login().has_local_credential()
        yield event.plain_result(
            "存在B站本地登录凭证（未验证服务器端有效性）"
            if present
            else "没有B站本地登录凭证，使用匿名解析"
        )

    async def parse_url(self, url: str):
        """供其他插件调用的公开解析 API：传入 URL 解析并返回 ParseResult 对象"""
        for kw, pat in self.key_pattern_list:
            if kw in url and (m := pat.search(url)):
                return await self.parser_map[kw].parse(kw, m)
        raise ValueError(f"未匹配到支持的解析器: {url}")

    async def parse_and_send(self, event: AstrMessageEvent, url: str) -> bool:
        """供其他插件调用的公开方法：解析指定 URL 并直接发送至 event 对应会话"""
        with trace_scope(self.cfg.debug_media_info):
            with stage(self.cfg.debug_media_info, "解析到发送总流程"):
                with stage(self.cfg.debug_media_info, "解析信息"):
                    result = await self.parse_url(url)
                return await self.sender.send_parse_result(event, result)

    @filter.llm_tool(name="parse_media_url")
    async def llm_parse_media_url(self, event: AstrMessageEvent, url: str) -> str:
        """
        解析用户提供的视频、图集或音频链接并自动发送解析结果。

        Args:
            url(string): 待解析的视频或图文等媒体链接
        """
        try:
            await self.parse_and_send(event, url)
            return "链接解析成功并已发送"
        except Exception as e:
            logger.error(f"[LLM Tool] 解析链接失败: {type(e).__name__}")
            return "解析链接失败，请检查网络、下载限制和平台登录状态"
