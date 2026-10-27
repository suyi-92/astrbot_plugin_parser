import asyncio
from re import Match
from typing import ClassVar
import shutil
from pathlib import Path

from bilibili_api.video import VideoQuality
from msgspec import convert

from astrbot.api import logger

from ...config import PluginConfig
from ...data import ImageContent, MediaContent, Platform, SendGroup
from ...exception import DownloadException, DurationLimitException, SizeLimitException
from ...resumable import ResumableDownloader, cache_lock
from ...utils import merge_av, probe_media
from ..base import (
    BaseParser,
    Downloader,
    ParseException,
    handle,
)
from .login import BilibiliLogin
from .api import BilibiliAPI, BilibiliVideo
from .streams import select_streams


class BilibiliParser(BaseParser):
    # 平台信息
    platform: ClassVar[Platform] = Platform(name="bilibili", display_name="B站")

    def __init__(self, config: PluginConfig, downloader: Downloader):
        super().__init__(config, downloader)
        self.mycfg = config.parser.bilibili
        self.headers.update(
            {
                "Referer": "https://www.bilibili.com/",
                "Origin": "https://www.bilibili.com",
            }
        )

        self.video_quality = getattr(
            VideoQuality, str(self.mycfg.video_quality).upper(), VideoQuality._720P
        )
        self.video_codecs = [
            str(c).upper() for c in (self.mycfg.video_codec_list or ["AVC"])
        ]
        self.api = BilibiliAPI(config)
        self.login = BilibiliLogin(config, self.api)
        self.resumable = ResumableDownloader(config)
        self.resumable.cleanup()

    async def close_session(self):
        await self.login.close()
        await self.resumable.close()
        await self.api.close()
        await super().close_session()

    async def parse(self, keyword, searched):
        try:
            return await super().parse(keyword, searched)
        except ParseException:
            raise
        except Exception:
            raise ParseException("B站解析失败，请检查网络和登录状态") from None

    @handle("b23.tv", r"b23\.tv/[A-Za-z\d\._?%&+\-=/#]+")
    @handle("bili2233", r"bili2233\.cn/[A-Za-z\d\._?%&+\-=/#]+")
    async def _parse_short_link(self, searched: Match[str]):
        """解析短链"""
        url = f"https://{searched.group(0)}"
        return await self.parse_with_redirect(url)

    @handle("BV", r"^(?P<bvid>BV[0-9a-zA-Z]{10})(?:\s)?(?P<page_num>\d{1,3})?$")
    @handle(
        "/BV",
        r"bilibili\.com(?:/video)?/(?P<bvid>BV[0-9a-zA-Z]{10})(?:\?p=(?P<page_num>\d{1,3}))?",
    )
    async def _parse_bv(self, searched: Match[str]):
        """解析视频信息"""
        bvid = str(searched.group("bvid"))
        page_num = int(searched.group("page_num") or 1)

        return await self.parse_video(bvid=bvid, page_num=page_num)

    @handle("bm", r"^bm(?P<bvid>BV[0-9a-zA-Z]{10})(?:\s(?P<page_num>\d{1,3}))?$")
    async def _parse_bv_bm(self, searched: Match[str]):
        bvid = searched.group("bvid")
        page = int(searched.group("page_num") or 1)
        video = await self._get_video(bvid=bvid)
        info = await video.get_info()
        pages = info.get("pages") or []
        if page < 1 or page > len(pages):
            raise ParseException("B站分 P 不存在")
        duration = pages[page - 1]["duration"]
        task = self.resumable.start(
            self._download_media(video, bvid, page, duration, audio_only=True)
        )
        return self.result(
            title=f"BiliBili_audio_{bvid}",
            contents=[self.create_audio_content(task, duration)],
            url=f"https://www.bilibili.com/video/{bvid}?p={page}",
        )

    @handle("av", r"^av(?P<avid>\d{6,})(?:\s)?(?P<page_num>\d{1,3})?$")
    @handle(
        "/av",
        r"bilibili\.com(?:/video)?/av(?P<avid>\d{6,})(?:\?p=(?P<page_num>\d{1,3}))?",
    )
    async def _parse_av(self, searched: Match[str]):
        """解析视频信息"""
        avid = int(searched.group("avid"))
        page_num = int(searched.group("page_num") or 1)

        return await self.parse_video(avid=avid, page_num=page_num)

    @handle("/dynamic/", r"bilibili\.com/dynamic/(?P<dynamic_id>\d+)")
    @handle("t.bili", r"t\.bilibili\.com/(?P<dynamic_id>\d+)")
    async def _parse_dynamic(self, searched: Match[str]):
        """解析动态信息"""
        dynamic_id = int(searched.group("dynamic_id"))
        return await self.parse_dynamic(dynamic_id)

    @handle("live.bili", r"live\.bilibili\.com/(?P<room_id>\d+)")
    async def _parse_live(self, searched: Match[str]):
        """解析直播信息"""
        room_id = int(searched.group("room_id"))
        return await self.parse_live(room_id)

    @handle("/favlist", r"favlist\?fid=(?P<fav_id>\d+)")
    async def _parse_favlist(self, searched: Match[str]):
        """解析收藏夹信息"""
        fav_id = int(searched.group("fav_id"))
        return await self.parse_favlist(fav_id)

    @handle("/read/", r"bilibili\.com/read/cv(?P<read_id>\d+)")
    async def _parse_read(self, searched: Match[str]):
        """解析专栏信息"""
        read_id = int(searched.group("read_id"))
        return await self.parse_read_with_opus(read_id)

    @handle("/opus/", r"bilibili\.com/opus/(?P<opus_id>\d+)")
    async def _parse_opus(self, searched: Match[str]):
        """解析图文动态信息"""
        opus_id = int(searched.group("opus_id"))
        return await self.parse_opus(opus_id)

    async def parse_video(
        self,
        *,
        bvid: str | None = None,
        avid: int | None = None,
        page_num: int = 1,
    ):
        """解析视频信息

        Args:
            bvid (str | None): bvid
            avid (int | None): avid
            page_num (int): 页码
        """

        from .video import AIConclusion, VideoInfo

        video = await self._get_video(bvid=bvid, avid=avid)
        # 转换为 msgspec struct
        video_info = convert(await video.get_info(), VideoInfo)
        # 获取简介
        text = f"简介: {video_info.desc}" if video_info.desc else None
        # up
        author = self.create_author(video_info.owner.name, video_info.owner.face)
        # 处理分 p
        page_info = video_info.extract_info_with_page(page_num)

        # 获取 AI 总结（默认提示）
        ai_summary = ""
        if self.login._credential:
            try:
                cid = await video.get_cid(page_info.index)
                ai_conclusion = await video.get_ai_conclusion(cid)
                ai_conclusion = convert(ai_conclusion, AIConclusion)
                ai_summary = ai_conclusion.summary
            except Exception:
                ai_summary = "哔哩哔哩 cookie 未配置或失效, 无法使用 AI 总结"

        url = f"https://bilibili.com/{video_info.bvid}"
        url += f"?p={page_info.index + 1}" if page_info.index > 0 else ""

        # Resolve the actual stream BEFORE any cache lookup.
        video_task = self.resumable.start(
            self._download_media(video, video_info.bvid, page_num, page_info.duration)
        )
        video_content = self.create_video_content(
            video_task,
            page_info.cover,
            page_info.duration,
        )

        return self.result(
            url=url,
            title=page_info.title,
            timestamp=page_info.timestamp,
            text=text,
            author=author,
            contents=[video_content],
            extra={"info": ai_summary},
        )

    async def parse_dynamic(self, dynamic_id: int):
        """解析动态信息

        Args:
            url (str): 动态链接
        """
        from .dynamic import DynamicData

        raw = await self.api.call(
            "dynamic", credential=await self.login.credential, id=dynamic_id
        )
        dynamic_info = convert(raw, DynamicData).item
        author = self.create_author(dynamic_info.name, dynamic_info.avatar)

        # 下载图片
        contents: list[MediaContent] = []
        for image_url in dynamic_info.image_urls:
            img_task = self.downloader.download_img(
                image_url, headers=self.headers, proxy=self.proxy
            )
            contents.append(ImageContent(img_task))

        return self.result(
            title=dynamic_info.title,
            text=dynamic_info.text,
            timestamp=dynamic_info.timestamp,
            author=author,
            contents=contents,
        )

    async def parse_opus(self, opus_id: int):
        """解析图文动态信息

        Args:
            opus_id (int): 图文动态 id
        """
        raw = await self.api.call(
            "opus", credential=await self.login.credential, id=opus_id
        )
        return await self._parse_opus_obj(raw)

    async def parse_read_with_opus(self, read_id: int):
        """解析专栏信息, 使用 Opus 接口
        Args:
            read_id (int): 专栏 id
        """
        raw = await self.api.call(
            "article", credential=await self.login.credential, id=read_id
        )
        return await self._parse_opus_obj(raw)

    async def _parse_opus_obj(self, opus_info: dict):
        """解析图文动态信息
        Args:
            opus_id (int): 图文动态 id
        Returns:
            ParseResult: 解析结果
        """
        from .opus import ImageNode, OpusItem, TextNode

        if not isinstance(opus_info, dict):
            raise ParseException("获取图文动态信息失败")
        # 转换为结构体
        opus_data = convert(opus_info, OpusItem)
        author = self.create_author(*opus_data.name_avatar)
        # 按顺序处理图文内容（参考 parse_read 的逻辑）
        contents: list[MediaContent] = []
        current_text = ""
        for node in opus_data.gen_text_img():
            if isinstance(node, ImageNode):
                contents.append(
                    self.create_graphics_content(
                        node.url, current_text.strip(), node.alt
                    )
                )
                current_text = ""
            elif isinstance(node, TextNode):
                current_text += node.text
        return self.result(
            title=opus_data.title,
            author=author,
            timestamp=opus_data.timestamp,
            contents=contents,
            text=current_text.strip(),
            # 图文动态全是轻媒体，默认发送计划不会渲染卡片，
            # 显式要求先发一张"文字+图片"合一的媒体卡片
            send_groups=(
                [SendGroup(contents=contents, render_card=True, force_merge=False)]
                if contents
                else []
            ),
        )

    async def parse_live(self, room_id: int):
        """解析直播信息

        Args:
            room_id (int): 直播 id

        Returns:
            ParseResult: 解析结果
        """
        from .live import RoomData

        info_dict = await self.api.call(
            "live", credential=await self.login.credential, id=room_id
        )

        room_data = convert(info_dict, RoomData)
        contents: list[MediaContent] = []
        # 下载封面
        if cover := room_data.cover:
            cover_task = self.downloader.download_img(
                cover, headers=self.headers, proxy=self.proxy
            )
            contents.append(ImageContent(cover_task))

        # 下载关键帧
        if keyframe := room_data.keyframe:
            keyframe_task = self.downloader.download_img(
                keyframe, headers=self.headers, proxy=self.proxy
            )
            contents.append(ImageContent(keyframe_task))

        author = self.create_author(room_data.name, room_data.avatar)

        url = f"https://www.bilibili.com/blackboard/live/live-activity-player.html?enterTheRoom=0&cid={room_id}"
        return self.result(
            url=url,
            title=room_data.title,
            text=room_data.detail,
            contents=contents,
            author=author,
        )

    async def parse_favlist(self, fav_id: int):
        """解析收藏夹信息

        Args:
            fav_id (int): 收藏夹 id

        Returns:
            list[GraphicsContent]: 图文内容列表
        """
        from .favlist import FavData

        # 只会取一页，20 个
        fav_dict = await self.api.call(
            "favlist", credential=await self.login.credential, id=fav_id
        )

        if fav_dict["medias"] is None:
            raise ParseException("收藏夹内容为空, 或被风控")

        favdata = convert(fav_dict, FavData)

        return self.result(
            title=favdata.title,
            timestamp=favdata.timestamp,
            author=self.create_author(favdata.info.upper.name, favdata.info.upper.face),
            contents=[
                self.create_graphics_content(fav.cover, fav.desc)
                for fav in favdata.medias
            ],
        )

    async def _get_video(self, *, bvid=None, avid=None) -> BilibiliVideo:
        if not (bvid or avid):
            raise ParseException("avid 和 bvid 至少指定一项")
        return BilibiliVideo(self.api, self.login, bvid=bvid, avid=avid)

    async def _select_streams(self, video, page_index, duration=0):
        data = await video.get_download_url(page_index=page_index)
        selected = select_streams(
            data,
            self.video_quality.value,
            self.video_codecs,
            bool(self.mycfg.block_pcdn),
            duration,
        )
        logger.info(f"B站实际视频流: {selected.video.info}")
        if selected.audio:
            logger.debug(f"B站实际音频流: {selected.audio.info}")
        return selected

    async def extract_download_urls(
        self, video=None, *, bvid=None, avid=None, page_index=0
    ):
        """Compatibility helper; internal media tasks use ALL official candidates."""
        video = video or await self._get_video(bvid=bvid, avid=avid)
        streams = await self._select_streams(video, page_index)
        return streams.video.urls[0], streams.audio.urls[0] if streams.audio else None

    async def _download_media(self, video, bvid, page, duration, audio_only=False):
        try:
            async with asyncio.timeout(max(1, self.mycfg.task_timeout)):
                return await self._download_selected(
                    video, bvid, page, duration, audio_only
                )
        except (DownloadException, asyncio.CancelledError):
            raise
        except TimeoutError:
            raise DownloadException(
                "B站下载/合并达到任务时间上限，请稍后重试"
            ) from None
        except Exception:
            raise DownloadException(
                "B站下载或合并失败，请检查网络、FFmpeg 和磁盘空间"
            ) from None

    async def _download_selected(self, video, bvid, page, duration, audio_only):
        if duration > self.cfg.max_duration:
            raise DurationLimitException
        selected = await self._select_streams(video, page - 1, duration)
        if selected.duration > self.cfg.max_duration:
            raise DurationLimitException
        if audio_only and not selected.audio:
            raise DownloadException("B站未提供独立音频流")
        identity = selected.audio.identity if audio_only else selected.identity
        quality = selected.video.info["quality"]
        suffix = ".m4a" if audio_only else ".mp4"
        output = (
            self.resumable.directory / f"{bvid}-{page}-q{quality}-{identity}{suffix}"
        )
        resource = f"{bvid}:{page}:{identity}"
        async with cache_lock(output):
            if await self.resumable.cached(output, resource):
                return output
            estimated = (
                selected.audio.info["estimated_bytes"]
                if audio_only
                else selected.video.info["estimated_bytes"]
                + (selected.audio.info["estimated_bytes"] if selected.audio else 0)
            )
            if estimated > self.cfg.max_size:
                raise SizeLimitException
            if (
                shutil.disk_usage(output.parent).free
                < self.cfg.max_size * 2 + 16 * 1024 * 1024
            ):
                raise DownloadException(
                    "B站下载空间不足，至少需要约两倍资源大小上限的空闲空间"
                )
            refreshed = False
            refresh_error = None
            refresh_lock = asyncio.Lock()
            current = selected

            async def refresh(kind):
                nonlocal refreshed, current, refresh_error
                async with refresh_lock:
                    if refresh_error:
                        raise refresh_error
                    if not refreshed:
                        refreshed = True
                        try:
                            fresh = await self._select_streams(
                                video, page - 1, duration
                            )
                            if fresh.identity != selected.identity:
                                raise DownloadException(
                                    "B站播放资源已变化，请重新解析；未拼接不同版本"
                                )
                        except Exception:
                            refresh_error = DownloadException(
                                "B站播放资源变化或地址刷新失败，请重新解析"
                            )
                            raise refresh_error from None
                        current = fresh
                    stream = current.audio if kind == "audio" else current.video
                    return stream.urls

            async def validate(path):
                await probe_media(path, strict=True)

            async def download(stream, kind):
                path = output.with_name(output.stem + f".{kind}.m4s")
                return await self.resumable.download(
                    stream.urls,
                    path,
                    resource=resource + ":" + kind,
                    headers=self.headers,
                    proxy=self.proxy,
                    refresh=lambda: refresh(kind),
                    validate=validate,
                )

            paths = []
            merged = output.with_name(output.name + ".part.mp4")
            try:
                if audio_only:
                    paths = [await download(selected.audio, "audio")]
                    await self.resumable.publish(paths[0], output, resource)
                elif selected.audio:
                    tasks = [
                        asyncio.create_task(download(selected.video, "video")),
                        asyncio.create_task(download(selected.audio, "audio")),
                    ]
                    try:
                        paths = list(await asyncio.gather(*tasks))
                    finally:
                        for task in tasks:
                            if not task.done():
                                task.cancel()
                        await asyncio.gather(*tasks, return_exceptions=True)
                    if sum(p.stat().st_size for p in paths) > self.cfg.max_size:
                        raise SizeLimitException
                    await merge_av(v_path=paths[0], a_path=paths[1], output_path=merged)
                    await validate(merged)
                    await self.resumable.publish(merged, output, resource)
                else:
                    paths = [await download(selected.video, "video")]
                    await self.resumable.publish(paths[0], output, resource)
                return output
            finally:
                merged.unlink(missing_ok=True)
                for path in paths:
                    path.unlink(missing_ok=True)
                    Path(str(path) + ".complete.json").unlink(missing_ok=True)
