import json
import math
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from aiohttp import web
from astrbot.core.config.astrbot_config import AstrBotConfig

from parser_under_test.core import config as config_module
from parser_under_test.core.download import Downloader
from parser_under_test.core.exception import DurationLimitException, SizeLimitException
from parser_under_test.core.parsers.bilibili import BilibiliParser
from parser_under_test.core.utils import generate_file_name
from test_bilibili import play_data
from test_resumable import serve


@pytest.fixture
def make_config(tmp_path, monkeypatch):
    root = Path(__file__).resolve().parents[1]
    schema = json.loads((root / "_conf_schema.json").read_text())
    monkeypatch.setattr(
        config_module, "get_astrbot_plugin_data_path", lambda: str(tmp_path / "data")
    )
    monkeypatch.setattr(
        config_module, "get_astrbot_plugin_path", lambda: str(root.parent)
    )

    def create(minutes=0):
        raw = AstrBotConfig(str(tmp_path / "plugin.json"), schema=schema)
        if minutes is None:  # Existing partial configuration, before defaults.
            raw.pop("source_max_minute", None)
        else:
            raw["source_max_minute"] = minutes
        raw["source_max_size"] = 1
        raw["parsers_template"] = [
            {
                "__template_key": "bilibili",
                "enable": True,
                "use_proxy": False,
                "cookies": "",
                "video_quality": "_1080P_60",
                "video_codec_list": ["AVC"],
            }
        ]
        cfg = config_module.PluginConfig(raw, SimpleNamespace(get_config=lambda: {}))
        return cfg, raw, schema

    return create


@pytest.mark.parametrize("minutes", [None, 0, -1, 27, 120, 1440])
def test_unlimited_and_custom_duration_roundtrip_keeps_old_values(
    make_config, minutes, tmp_path
):
    cfg, raw, schema = make_config(minutes)
    expected = 0 if minutes is None else minutes
    assert cfg.source_max_minute == expected
    assert cfg.max_duration == (expected * 60 if expected > 0 else math.inf)
    assert schema["source_max_minute"]["type"] == "int"
    assert schema["source_max_minute"]["default"] == 0
    assert "slider" not in schema["source_max_minute"]
    # The in-memory infinity must never become non-standard JSON in AstrBot.
    json.dumps(dict(raw), allow_nan=False)
    cfg.save_config()
    saved = json.loads((tmp_path / "plugin.json").read_text(encoding="utf-8-sig"))
    assert saved["source_max_minute"] == expected
    assert "max_duration" not in saved
    assert cfg.max_size == 1024**2
    assert cfg.download_timeout == schema["download_timeout"]["default"]
    assert cfg.parser.bilibili.task_timeout == 900


@pytest.mark.parametrize(
    "minutes,info_duration,audio_only,rejected",
    [
        (0, 7200, False, False),
        (0, 7200, True, False),
        (120, 7200, False, False),
        (27, 7200, False, True),
        (27, 1, False, True),
    ],
)
async def test_long_bilibili_media_and_both_duration_checks(
    make_config, monkeypatch, minutes, info_duration, audio_only, rejected
):
    cfg, _, _ = make_config(minutes)
    data = play_data()
    data["dash"]["duration"] = 7200
    # Synthetic two-hour metadata with small, permitted streams. No network or
    # two-hour media encoding is needed to exercise the duration gate.
    for stream in data["dash"]["video"] + data["dash"]["audio"]:
        stream["size"] = 1024
    video = SimpleNamespace(get_download_url=AsyncMock(return_value=data))
    parser = BilibiliParser(cfg, SimpleNamespace())
    downloaded = []

    async def download(urls, path, **kwargs):
        downloaded.append(path)
        path.write_bytes(b"synthetic-stream")
        return path

    async def merge(**kwargs):
        kwargs["output_path"].write_bytes(b"synthetic-merged")

    monkeypatch.setattr(parser.resumable, "download", download)
    monkeypatch.setattr("parser_under_test.core.parsers.bilibili.merge_av", merge)
    monkeypatch.setattr(
        "parser_under_test.core.parsers.bilibili.probe_media", AsyncMock()
    )
    try:
        if rejected:
            with pytest.raises(DurationLimitException):
                await parser._download_media(
                    video, "BV11EnQ6CER8", 1, info_duration, audio_only
                )
            assert not downloaded
            if info_duration > cfg.max_duration:
                video.get_download_url.assert_not_awaited()
            else:
                video.get_download_url.assert_awaited_once()
        else:
            path = await parser._download_media(
                video, "BV11EnQ6CER8", 1, info_duration, audio_only
            )
            assert path.exists() and path.stat().st_size > 0
            assert len(downloaded) == (1 if audio_only else 2)
    finally:
        await parser.close_session()


async def test_unlimited_duration_still_rejects_large_bilibili_stream(
    make_config, monkeypatch
):
    cfg, _, _ = make_config(0)
    parser = BilibiliParser(cfg, SimpleNamespace())
    data = play_data()
    data["dash"]["duration"] = 7200
    data["dash"]["video"][0]["size"] = cfg.max_size + 1
    video = SimpleNamespace(get_download_url=AsyncMock(return_value=data))
    download = AsyncMock()
    monkeypatch.setattr(parser.resumable, "download", download)
    try:
        with pytest.raises(SizeLimitException):
            await parser._download_media(video, "BV11EnQ6CER8", 1, 7200)
        download.assert_not_awaited()
    finally:
        await parser.close_session()


async def test_unlimited_duration_keeps_generic_download_size_limit(make_config):
    cfg, _, _ = make_config(0)
    downloader = Downloader(cfg)

    async def handler(request):
        return web.Response(body=b"x" * (cfg.max_size + 1))

    try:
        async with serve(handler) as base:
            with pytest.raises(SizeLimitException):
                await downloader.streamd(base, file_name="too-large.mp4")
        assert not (cfg.cache_dir / "too-large.mp4").exists()
        assert not (cfg.cache_dir / "too-large.mp4.part").exists()
    finally:
        await downloader.close()


@pytest.mark.parametrize("minutes", [0, 27])
async def test_ytdlp_duration_gate_uses_the_shared_unlimited_bound(
    make_config, monkeypatch, minutes
):
    cfg, _, _ = make_config(minutes)
    downloader = Downloader(cfg)
    url = "https://www.youtube.com/watch?v=abcdefghijk"
    cached = cfg.cache_dir / generate_file_name(url, ".mp4")
    cached.write_bytes(b"existing-media")
    monkeypatch.setattr(
        downloader,
        "ytdlp_extract_info",
        AsyncMock(return_value=SimpleNamespace(duration=7200)),
    )
    try:
        if minutes == 0:
            assert await downloader.ytdlp_download_video(url) == cached
        else:
            with pytest.raises(DurationLimitException):
                await downloader.ytdlp_download_video(url)
    finally:
        await downloader.close()
