"""Optional API requests and the media pipeline remain independent."""

import asyncio
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from parser_under_test.core.config import ParserConfig
from parser_under_test.core.data import VideoContent
from parser_under_test.core.exception import DownloadException
from parser_under_test.core.parsers.bilibili import BilibiliParser
from test_bilibili import play_data


async def test_old_config_defaults_summary_off_without_optional_api_calls(cfg):
    old = {"__template_key": "bilibili", "enable": True, "cookies": ""}
    assert "fetch_ai_summary" not in old
    migrated = ParserConfig([old])
    assert migrated.bilibili.fetch_ai_summary is False
    parser = BilibiliParser(cfg, SimpleNamespace())
    parser.mycfg = migrated.bilibili
    parser.login._credential = object()
    video = SimpleNamespace(get_cid=AsyncMock(), get_ai_conclusion=AsyncMock())
    try:
        assert await parser._get_ai_summary(video, SimpleNamespace(index=1)) == ""
        video.get_cid.assert_not_awaited()
        video.get_ai_conclusion.assert_not_awaited()
    finally:
        await parser.close_session()


async def test_enabled_summary_uses_selected_page_cid(cfg):
    parser = BilibiliParser(cfg, SimpleNamespace())
    parser.mycfg.fetch_ai_summary = True
    parser.login._credential = object()
    video = SimpleNamespace(
        get_cid=AsyncMock(return_value=2468),
        get_ai_conclusion=AsyncMock(
            return_value={"model_result": {"summary": "selected page summary"}}
        ),
    )
    try:
        assert await parser._get_ai_summary(video, SimpleNamespace(index=1)) == (
            "AI总结: selected page summary"
        )
        video.get_cid.assert_awaited_once_with(1)
        video.get_ai_conclusion.assert_awaited_once_with(2468)
    finally:
        await parser.close_session()


async def test_enabled_summary_without_login_skips_optional_requests(cfg):
    parser = BilibiliParser(cfg, SimpleNamespace())
    parser.mycfg.fetch_ai_summary = True
    video = SimpleNamespace(get_cid=AsyncMock(), get_ai_conclusion=AsyncMock())
    try:
        assert await parser._get_ai_summary(video, SimpleNamespace(index=0)) == ""
        video.get_cid.assert_not_awaited()
        video.get_ai_conclusion.assert_not_awaited()
    finally:
        await parser.close_session()


async def test_default_summary_preserves_metadata_page_and_download(cfg, monkeypatch):
    parser = BilibiliParser(cfg, SimpleNamespace())
    parser.login._credential = object()
    video = SimpleNamespace(
        get_info=AsyncMock(
            return_value={
                "bvid": "BV11EnQ6CER8",
                "title": "video title",
                "desc": "video description",
                "duration": 30,
                "owner": {"mid": 123, "name": "author", "face": ""},
                "stat": dict.fromkeys(
                    ("view", "danmaku", "reply", "favorite", "coin", "share", "like"),
                    0,
                ),
                "pubdate": 10,
                "ctime": 9,
                "pages": [
                    {"part": "first", "ctime": 10, "duration": 12},
                    {"part": "second", "ctime": 20, "duration": 18},
                ],
            }
        ),
        get_cid=AsyncMock(),
        get_ai_conclusion=AsyncMock(),
    )
    output = cfg.cache_dir / "complete.mp4"
    output.write_bytes(b"completed video")
    download = AsyncMock(return_value=output)
    monkeypatch.setattr(parser, "_get_video", AsyncMock(return_value=video))
    monkeypatch.setattr(parser, "_download_media", download)
    try:
        result = await parser.parse_video(bvid="BV11EnQ6CER8", page_num=2)
        assert result.title == "video title | 分集 - second"
        assert result.timestamp == 20
        assert result.text == "简介: video description"
        assert result.author.name == "author"
        assert result.url.endswith("?p=2")
        assert result.extra["info"] == ""
        assert len(result.contents) == 1
        content = result.contents[0]
        assert isinstance(content, VideoContent) and content.duration == 18
        assert await content.get_path() == output
        download.assert_awaited_once_with(video, "BV11EnQ6CER8", 2, 18)
        video.get_info.assert_awaited_once_with()
        video.get_cid.assert_not_awaited()
        video.get_ai_conclusion.assert_not_awaited()
    finally:
        await parser.close_session()


async def test_bilibili_video_audio_downloads_overlap_before_merge(cfg, monkeypatch):
    parser = BilibiliParser(cfg, SimpleNamespace())
    started = {"video": asyncio.Event(), "audio": asyncio.Event()}
    downloaded = []

    async def download(urls, path, **kwargs):
        kind = "audio" if ".audio." in path.name else "video"
        started[kind].set()
        other = "video" if kind == "audio" else "audio"
        # A serial implementation would time out before starting the other stream.
        await asyncio.wait_for(started[other].wait(), 2)
        path.write_bytes(kind.encode())
        downloaded.append(path)
        return path

    async def merge(*, v_path, a_path, output_path):
        assert v_path.read_bytes() == b"video" and a_path.read_bytes() == b"audio"
        assert len(downloaded) == 2
        output_path.write_bytes(b"complete merged video")

    merge_mock = AsyncMock(side_effect=merge)
    monkeypatch.setattr(parser.resumable, "download", download)
    monkeypatch.setattr("parser_under_test.core.parsers.bilibili.merge_av", merge_mock)
    monkeypatch.setattr("parser_under_test.core.parsers.bilibili.probe_media", AsyncMock())
    try:
        path = await parser._download_media(
            SimpleNamespace(get_download_url=AsyncMock(return_value=play_data())),
            "BV11EnQ6CER8", 1, 10,
        )
        assert path.read_bytes() == b"complete merged video"
        merge_mock.assert_awaited_once()
        assert not list(parser.resumable.directory.glob("*.m4s*"))
    finally:
        await parser.close_session()


@pytest.mark.parametrize("cancel", [False, True], ids=["failure", "cancel"])
async def test_completed_sibling_stream_is_cleaned_on_failure(cfg, monkeypatch, cancel):
    parser = BilibiliParser(cfg, SimpleNamespace())
    completed, pending = asyncio.Event(), asyncio.Event()

    async def download(urls, path, **kwargs):
        if ".video." in path.name:
            path.write_bytes(b"completed video stream")
            Path(str(path) + ".complete.json").write_text("{}")
            completed.set()
            return path
        await completed.wait()
        pending.set()
        if cancel:
            await asyncio.Event().wait()
        raise DownloadException("audio failed")

    merge = AsyncMock()
    monkeypatch.setattr(parser.resumable, "download", download)
    monkeypatch.setattr("parser_under_test.core.parsers.bilibili.merge_av", merge)
    task = asyncio.create_task(parser._download_media(
        SimpleNamespace(get_download_url=AsyncMock(return_value=play_data())),
        "BV11EnQ6CER8", 1, 10,
    ))
    try:
        await asyncio.wait_for(pending.wait(), 2)
        if cancel:
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        else:
            with pytest.raises(DownloadException, match="audio failed"):
                await task
        merge.assert_not_awaited()
        assert not list(parser.resumable.directory.glob("*.m4s*"))
        assert not list(parser.resumable.directory.glob("*.mp4*"))
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        await parser.close_session()
