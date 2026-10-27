import asyncio
import base64
import json
import subprocess
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from aiohttp import web
from astrbot.core.config.astrbot_config import AstrBotConfig
from astrbot.core.message.components import Plain, Video
from astrbot.core.platform.sources.aiocqhttp.aiocqhttp_message_event import (
    AiocqhttpMessageEvent,
)
from astrbot.core.platform.sources.qqofficial.qqofficial_message_event import (
    QQOfficialMessageEvent,
)
from astrbot.core.star.filter.command import CommandFilter
from astrbot.core.star.filter.permission import PermissionType, PermissionTypeFilter
from astrbot.core.star.star_handler import star_handlers_registry

from parser_under_test.core import config as config_module
from parser_under_test.core import utils
from parser_under_test.core.data import ParseResult, Platform, VideoContent
from parser_under_test.core.download import Downloader
from parser_under_test.core.exception import DownloadException
from parser_under_test.core.parsers.bilibili import BilibiliParser
from parser_under_test.core.parsers.douyin import DouyinParser
from parser_under_test.core.parsers.kuaishou import KuaiShouParser
from parser_under_test.core.sender import MessageSender
from parser_under_test.main import ParserPlugin
from test_resumable import serve


@pytest.fixture
def media(tmp_path):
    video, audio = tmp_path / "video.mp4", tmp_path / "audio.m4a"
    subprocess.run(
        [
            "ffmpeg",
            "-v",
            "error",
            "-f",
            "lavfi",
            "-i",
            "color=c=blue:s=1080x1920:r=30",
            "-t",
            "0.4",
            "-c:v",
            "libx264",
            "-preset",
            "ultrafast",
            "-pix_fmt",
            "yuv420p",
            str(video),
        ],
        check=True,
    )
    subprocess.run(
        [
            "ffmpeg",
            "-v",
            "error",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=1000",
            "-t",
            "0.4",
            "-c:a",
            "aac",
            str(audio),
        ],
        check=True,
    )
    return video, audio


def video_hash(path):
    return subprocess.check_output(
        [
            "ffmpeg",
            "-v",
            "error",
            "-i",
            str(path),
            "-map",
            "0:v:0",
            "-c",
            "copy",
            "-f",
            "hash",
            "-hash",
            "sha256",
            "-",
        ]
    ).strip()


async def test_real_ffmpeg_merge_copies_video_and_audio(media, tmp_path):
    video, audio = media
    old_hash = video_hash(video)
    target = tmp_path / "merged.mp4"
    await utils.merge_av(v_path=video, a_path=audio, output_path=target)
    assert video_hash(target) == old_hash
    probe = await utils.probe_media(target, strict=True)
    assert {s["codec_type"] for s in probe["streams"]} == {"video", "audio"}
    assert not video.exists() and not audio.exists()


async def test_audio_conversion_preserves_1080p_and_original(media, tmp_path):
    video, _ = media
    source = tmp_path / "douyin.mp4"
    subprocess.run(
        [
            "ffmpeg",
            "-v",
            "error",
            "-i",
            str(video),
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=800",
            "-t",
            "0.4",
            "-c:v",
            "copy",
            "-c:a",
            "libmp3lame",
            str(source),
        ],
        check=True,
    )
    before = video_hash(source)
    result = await utils.transcode_video_audio_to_lc(source)
    assert result != source and source.exists()
    assert video_hash(result) == before
    probe = await utils.probe_media(result, strict=True)
    stream = next(s for s in probe["streams"] if s["codec_type"] == "video")
    assert (stream["width"], stream["height"], stream["r_frame_rate"]) == (
        1080,
        1920,
        "30/1",
    )
    assert await utils.transcode_video_audio_to_lc(source) == result
    assert not list(tmp_path.glob("*.part*"))


async def test_ffmpeg_cancel_kills_and_reaps(monkeypatch):
    process = SimpleNamespace(returncode=None)
    started = asyncio.Event()

    async def communicate():
        if process.returncode is None:
            started.set()
            await asyncio.sleep(10)
        return b"", b""

    def kill():
        process.returncode = -9

    process.communicate = AsyncMock(side_effect=communicate)
    process.kill = kill
    monkeypatch.setattr(
        asyncio, "create_subprocess_exec", AsyncMock(return_value=process)
    )
    task = asyncio.create_task(utils.exec_ffmpeg_cmd(["ffmpeg"]))
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert process.returncode == -9 and process.communicate.await_count == 2


@pytest.mark.parametrize("parser_type", [DouyinParser, KuaiShouParser])
async def test_other_parsers_use_compatible_generic_downloader(cfg, parser_type):
    cfg.parser._nodes[parser_type.platform.name] = SimpleNamespace(
        use_proxy=False, cookies="", name=parser_type.platform.name
    )
    downloader = Downloader(cfg)
    parser = parser_type(cfg, downloader)
    count = 0

    async def handler(request):
        nonlocal count
        count += 1
        return web.Response(body=b"original-media-bytes")

    try:
        async with serve(handler) as base:
            contents = [parser.create_video_content(base + "/video") for _ in range(3)]
            paths = await asyncio.gather(*(c.get_path() for c in contents))
            assert all(p.read_bytes() == b"original-media-bytes" for p in paths)
            assert count == 1
            assert not list(cfg.cache_dir.glob("*.part"))
    finally:
        await parser.close_session()
        await downloader.close()
    assert downloader.client.closed and not downloader._tasks


async def test_sender_reports_bili_failure_even_tip_disabled(cfg):
    cfg.show_download_fail_tip = False
    sender = MessageSender(cfg, SimpleNamespace())

    async def fail():
        raise DownloadException("B站媒体下载失败（HTTP567）")

    content = VideoContent(asyncio.create_task(fail()))
    result = ParseResult(platform=Platform("bilibili", "B站"), contents=[content])
    plan = {"light": [], "heavy": [content], "render_card": False, "force_merge": False}
    segments = await sender._build_segments(result, plan)
    assert (
        len(segments) == 1
        and isinstance(segments[0], Plain)
        and "567" in segments[0].text
    )


async def test_sender_never_sends_partial(cfg):
    path = cfg.cache_dir / "unfinished.mp4.part"
    path.write_bytes(b"not-complete")
    content = VideoContent(path)
    result = ParseResult(platform=Platform("bilibili", "B站"), contents=[content])
    segments = await MessageSender(cfg, SimpleNamespace())._build_segments(
        result,
        {"light": [], "heavy": [content], "render_card": False, "force_merge": False},
    )
    assert not any(isinstance(s, Video) for s in segments)
    assert "未发送" in segments[0].text


async def test_qqofficial_upload_and_onebot_keep_file_bytes(media):
    path, _ = media
    http = SimpleNamespace(
        request=AsyncMock(return_value={"file_uuid": "test", "file_info": "test"})
    )
    event = SimpleNamespace(bot=SimpleNamespace(api=SimpleNamespace(_http=http)))
    await QQOfficialMessageEvent.upload_group_and_c2c_media(
        event, str(path), file_type=2, group_openid="test"
    )
    assert (
        base64.b64decode(http.request.call_args.kwargs["json"]["file_data"])
        == path.read_bytes()
    )
    component = MessageSender._video_from_path(path)
    onebot = await AiocqhttpMessageEvent._from_segment_to_dict(component)
    assert onebot["type"] == "video"
    assert onebot["data"]["file"] == path.as_uri()


async def test_astrbot_4293_full_lifecycle_admin_commands_and_config(
    tmp_path, monkeypatch
):
    root = Path(__file__).resolve().parents[1]
    schema = json.loads((root / "_conf_schema.json").read_text())
    raw = AstrBotConfig(str(tmp_path / "plugin.json"), schema=schema)
    raw["parsers_template"] = [
        {
            "__template_key": "bilibili",
            "enable": True,
            "use_proxy": False,
            "cookies": "",
        }
    ]
    raw.save_config()
    monkeypatch.setattr(
        config_module,
        "get_astrbot_plugin_data_path",
        lambda: str(tmp_path / "plugin_data"),
    )
    monkeypatch.setattr(
        config_module, "get_astrbot_plugin_path", lambda: str(root.parent)
    )
    context = SimpleNamespace(
        get_config=lambda: {"admins_id": ["admin"], "timezone": "Asia/Shanghai"}
    )
    plugin = ParserPlugin(context, raw)
    try:
        # Actual bundled font/assets and registered AstrBot handlers; no fake imports.
        await plugin.initialize()
        parser = plugin._get_parser_by_type(BilibiliParser)
        assert await parser.api.call("settings") == {
            "client": "aiohttp",
            "user_agent": "curl/8.14.1",
            "auto_buvid": False,
        }
        for method in ("login_bilibili", "logout_bilibili", "status_bilibili"):
            handler = next(
                h
                for h in star_handlers_registry
                if h.handler == getattr(ParserPlugin, method)
            )
            permission = next(
                f for f in handler.event_filters if isinstance(f, PermissionTypeFilter)
            )
            assert permission.permission_type == PermissionType.ADMIN
            assert not permission.filter(SimpleNamespace(is_admin=lambda: False), raw)
            assert permission.filter(SimpleNamespace(is_admin=lambda: True), raw)
        handler = next(
            h
            for h in star_handlers_registry
            if h.handler == ParserPlugin.logout_bilibili
        )
        command = next(f for f in handler.event_filters if isinstance(f, CommandFilter))
        assert command.command_name == "blogout" and "退出B站" in command.alias
        event = SimpleNamespace(plain_result=lambda text: text)
        assert "没有" in (await anext(plugin.status_bilibili(event)))
        for _ in range(2):
            assert "没有" in (await anext(plugin.logout_bilibili(event)))
        assert (
            json.loads((tmp_path / "plugin.json").read_text(encoding="utf-8-sig"))[
                "parsers_template"
            ][0]["cookies"]
            == ""
        )
    finally:
        await plugin.terminate()
    assert parser.api.process is None
    assert plugin.downloader.client.closed


async def test_sender_close_cancels_inflight_conversion(cfg, monkeypatch):
    sender = MessageSender(cfg, SimpleNamespace())
    started, cancelled = asyncio.Event(), asyncio.Event()

    async def pending(*args):
        started.set()
        try:
            await asyncio.sleep(10)
        finally:
            cancelled.set()

    monkeypatch.setattr(sender, "_send_parse_result", pending)
    task = asyncio.create_task(sender.send_parse_result(None, None))
    await started.wait()
    await sender.close()
    assert task.cancelled() and cancelled.is_set() and not sender.tasks
    with pytest.raises(DownloadException, match="已停止"):
        await sender.send_parse_result(None, None)


async def test_generic_av_failure_cancels_sibling(cfg, monkeypatch):
    downloader = Downloader(cfg)
    cancelled = asyncio.Event()

    async def video():
        try:
            await asyncio.sleep(10)
        finally:
            cancelled.set()

    async def audio():
        await asyncio.sleep(0)
        raise DownloadException("audio failed")

    monkeypatch.setattr(
        downloader, "download_video", lambda *a, **k: asyncio.create_task(video())
    )
    monkeypatch.setattr(
        downloader, "download_audio", lambda *a, **k: asyncio.create_task(audio())
    )
    try:
        with pytest.raises(DownloadException):
            await downloader.download_av_and_merge(
                "video", "audio", output_path=cfg.cache_dir / "out.mp4"
            )
        assert cancelled.is_set()
    finally:
        await downloader.close()
