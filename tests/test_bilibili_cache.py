import asyncio
import json
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from bilibili_api import Credential
from bilibili_api.video import VideoQuality

from parser_under_test.core.exception import DownloadException, DurationLimitException
from parser_under_test.core.parsers.bilibili import BilibiliParser
from parser_under_test.core.parsers.bilibili.cache import INDEX_TTL, VideoCacheKey
from test_bilibili import play_data

BVID = "BV11EnQ6CER8"


@pytest.fixture
async def media(cfg, monkeypatch):
    parser = BilibiliParser(cfg, SimpleNamespace())
    video = SimpleNamespace(get_download_url=AsyncMock(return_value=play_data()))
    downloads = []

    async def download(urls, path, **kwargs):
        downloads.append(path)
        await asyncio.sleep(0)
        path.write_bytes(b"verified-stream")
        return path

    async def merge(**kwargs):
        kwargs["output_path"].write_bytes(b"complete-verified-mp4")

    monkeypatch.setattr(parser.resumable, "download", download)
    monkeypatch.setattr("parser_under_test.core.parsers.bilibili.merge_av", merge)
    monkeypatch.setattr("parser_under_test.core.parsers.bilibili.probe_media", AsyncMock())
    yield parser, video, downloads
    await parser.close_session()


async def fetch(media, page=1):
    parser, video, _ = media
    return await parser._download_media(video, BVID, page, 10)


async def test_verified_cache_skips_playurl_and_download(media):
    parser, video, downloads = media
    first = await fetch(media)
    assert await fetch(media) == first
    video.get_download_url.assert_awaited_once_with(page_index=0)
    assert len(downloads) == 2
    index = parser._video_cache_key(BVID, 1).index_path(parser.resumable.directory)
    entry = json.loads(index.read_text())
    assert entry["file"] == first.name
    # A hit never slides expiration forward and indefinitely masks API changes.
    created = entry["created_at"]
    await fetch(media)
    assert json.loads(index.read_text())["created_at"] == created
    assert "signature" not in index.read_text()


@pytest.mark.parametrize("damage", ["expired", "missing", "sha256", "receipt", "malformed", "future", "nan", "huge"])
async def test_invalid_cache_falls_back_to_playurl(media, damage):
    parser, video, downloads = media
    path = await fetch(media)
    index = parser._video_cache_key(BVID, 1).index_path(parser.resumable.directory)
    entry = json.loads(index.read_text())
    if damage == "expired":
        entry["created_at"] = time.time() - INDEX_TTL - 1
    elif damage == "future":
        entry["created_at"] = time.time() + 100
    elif damage == "nan":
        entry["created_at"] = float("nan")
    elif damage == "huge":
        entry["created_at"] = 10 ** 400
    elif damage == "missing":
        path.unlink()
    elif damage == "sha256":
        path.write_bytes(b"X" * path.stat().st_size)
    elif damage == "receipt":
        Path(str(path) + ".complete.json").unlink()
    elif damage == "malformed":
        index.write_text("[]")
    if damage != "malformed":
        index.write_text(json.dumps(entry))
    result = await fetch(media)
    assert result.read_bytes() == b"complete-verified-mp4"
    assert video.get_download_url.await_count == 2
    assert len(downloads) == (4 if damage in {"missing", "sha256", "receipt"} else 2)


@pytest.mark.parametrize("setting", ["account", "session", "quality", "codec", "codec_order", "page"])
async def test_request_dimensions_do_not_reuse_preflight(media, setting):
    parser, video, downloads = media
    parser.login._credential = Credential(sessdata="first-secret", dedeuserid="123456")
    first = await fetch(media)
    page = 1
    if setting == "account":
        parser.login._credential = Credential(sessdata="second-secret", dedeuserid="789012")
    elif setting == "session":
        parser.login._credential = Credential(sessdata="new-login", dedeuserid="123456")
    elif setting == "quality":
        parser.video_quality = VideoQuality._720P
        video.get_download_url.return_value = play_data(64)
    elif setting == "codec":
        parser.video_codecs = ["HEV"]
        video.get_download_url.return_value = play_data(80, "hvc1.1.6.L120.90")
    elif setting == "codec_order":
        parser.video_codecs = ["AVC", "HEV"]
    elif setting == "page":
        page = 2
    second = await fetch(media, page)
    assert second != first
    assert video.get_download_url.await_count == 2
    assert len(downloads) == 4
    for index in parser.resumable.directory.glob("*.index.json"):
        contents = index.read_text() + index.name
        for secret in ("first-secret", "second-secret", "new-login", "123456", "789012", "SESSDATA", "PRIVATE"):
            assert secret not in contents


async def test_720p_cache_cannot_satisfy_1080p_request(media):
    parser, video, downloads = media
    parser.video_quality = VideoQuality._720P
    video.get_download_url.return_value = play_data(64)
    low = await fetch(media)
    parser.video_quality = VideoQuality._1080P
    video.get_download_url.return_value = play_data(80)
    high = await fetch(media)
    assert "-q64-" in low.name and "-q80-" in high.name
    assert high != low and len(downloads) == 4
    assert video.get_download_url.await_count == 2


async def test_simultaneous_cold_requests_share_playurl_download_and_atomic_index(media):
    parser, video, downloads = media
    results = await asyncio.gather(*(fetch(media) for _ in range(8)))
    assert len(set(results)) == 1
    video.get_download_url.assert_awaited_once()
    assert len(downloads) == 2
    assert len(list(parser.resumable.directory.glob("*.index.json"))) == 1
    assert not list(parser.resumable.directory.glob("*.tmp"))
    assert await parser.resumable.cached(
        results[0], json.loads(next(parser.resumable.directory.glob("*.index.json")).read_text())["resource"]
    )


async def test_request_lock_is_shared_across_parser_instances(media, cfg, monkeypatch):
    parser, video, downloads = media
    other = BilibiliParser(cfg, SimpleNamespace())
    monkeypatch.setattr(other.resumable, "download", parser.resumable.download)
    try:
        first, second = await asyncio.gather(
            fetch(media), other._download_media(video, BVID, 1, 10)
        )
        assert first == second
        assert len(downloads) == 2
        video.get_download_url.assert_awaited_once()
    finally:
        await other.close_session()


@pytest.mark.parametrize("poison", ["traversal", "absolute", "backslash", "wrong_resource", "symlink", "receipt_symlink", "index_symlink"])
async def test_poisoned_index_cannot_read_unrelated_file(media, tmp_path, poison):
    parser, video, _ = media
    path = await fetch(media)
    key = parser._video_cache_key(BVID, 1)
    index = key.index_path(parser.resumable.directory)
    entry = json.loads(index.read_text())
    outside = tmp_path / "unrelated.mp4"
    outside.write_bytes(b"outside-must-not-be-used")
    if poison == "traversal":
        entry["file"] = "../../unrelated.mp4"
    elif poison == "absolute":
        entry["file"] = str(outside)
    elif poison == "backslash":
        entry["file"] = "..\\unrelated.mp4"
    elif poison == "wrong_resource":
        entry["resource"] += ":wrong"
    elif poison == "symlink":
        path.unlink()
        path.symlink_to(outside)
    elif poison == "receipt_symlink":
        receipt = Path(str(path) + ".complete.json")
        receipt.unlink()
        receipt.symlink_to(outside)
    elif poison == "index_symlink":
        outside.write_text(json.dumps(entry))
        index.unlink()
        index.symlink_to(outside)
    if poison not in {"index_symlink"}:
        index.write_text(json.dumps(entry))
    # Stop at the normal API fallback, before any download can touch poisoned paths.
    video.get_download_url.side_effect = DownloadException("normal fallback")
    with pytest.raises(DownloadException, match="normal fallback"):
        await fetch(media)
    assert video.get_download_url.await_count == 2
    assert outside.exists()


async def test_cache_hit_still_enforces_current_size_and_duration_limits(media, cfg):
    parser, video, _ = media
    path = await fetch(media)
    cfg.max_duration = 5
    with pytest.raises(DurationLimitException):
        await fetch(media)
    video.get_download_url.assert_awaited_once()
    cfg.max_duration = 900
    cfg.max_size = path.stat().st_size - 1
    video.get_download_url.side_effect = DownloadException("size fallback")
    with pytest.raises(DownloadException, match="size fallback"):
        await fetch(media)


async def test_account_change_during_cache_validation_does_not_return_cache(media, monkeypatch):
    parser, _, _ = media
    await fetch(media)
    cached = parser.resumable.cached

    async def changed(*args):
        result = await cached(*args)
        parser.login._credential = Credential(sessdata="other-account", dedeuserid="999")
        return result

    monkeypatch.setattr(parser.resumable, "cached", changed)
    with pytest.raises(DownloadException, match="账号或下载设置已变化"):
        await fetch(media)


@pytest.mark.parametrize("bvid,page", [("../BV11EnQ6CER8", 1), (BVID, 0), (BVID, True)])
def test_cache_key_rejects_unsafe_inputs(bvid, page):
    with pytest.raises(DownloadException):
        VideoCacheKey.create(bvid, page, 80, ["AVC"], None)


@pytest.mark.parametrize("link", ["output", "receipt"])
async def test_symlink_fallback_redownloads_without_touching_target(media, tmp_path, link):
    parser, video, downloads = media
    path = await fetch(media)
    outside = tmp_path / "protected"
    receipt = Path(str(path) + ".complete.json")
    if link == "output":
        # The receipt remains fully valid for the symlink's bytes, so an unsafe
        # ordinary cached() fallback would return the symlink without downloading.
        outside.write_bytes(path.read_bytes())
        path.unlink()
        path.symlink_to(outside)
    else:
        outside.write_bytes(receipt.read_bytes())
        receipt.unlink()
        receipt.symlink_to(outside)
    before = outside.read_bytes(), outside.stat().st_mtime_ns
    assert await fetch(media) == path
    assert not path.is_symlink() and not receipt.is_symlink()
    assert video.get_download_url.await_count == 2
    assert len(downloads) == 4
    assert (outside.read_bytes(), outside.stat().st_mtime_ns) == before


async def test_index_unlink_failure_remains_optional_fallback(media, monkeypatch):
    parser, video, _ = media
    await fetch(media)
    index = parser._video_cache_key(BVID, 1).index_path(parser.resumable.directory)
    index.write_text("invalid-index")
    original_unlink = Path.unlink

    def cannot_unlink(path, *args, **kwargs):
        if path == index:
            raise PermissionError("PRIVATE-filesystem-detail")
        return original_unlink(path, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", cannot_unlink)
    await fetch(media)
    assert video.get_download_url.await_count == 2
    assert json.loads(index.read_text())["version"] == 1


async def test_regular_cache_cleanup_removes_index_and_falls_back(media, cfg):
    import os

    parser, video, downloads = media
    path = await fetch(media)
    for item in parser.resumable.directory.iterdir():
        os.utime(item, (time.time() - 7200, time.time() - 7200))
    cfg.parser.bilibili.cache_expire_hours = 1
    parser.resumable.cleanup()
    assert not path.exists()
    assert not list(parser.resumable.directory.glob("*.index.json"))
    await fetch(media)
    assert video.get_download_url.await_count == 2
    assert len(downloads) == 4


async def test_cancelling_cache_lock_owner_allows_waiter_to_finish(media, monkeypatch):
    parser, video, _ = media
    entered, gate = asyncio.Event(), asyncio.Event()
    original_download = parser.resumable.download

    async def held_download(*args, **kwargs):
        entered.set()
        await gate.wait()
        return await original_download(*args, **kwargs)

    monkeypatch.setattr(parser.resumable, "download", held_download)
    first = asyncio.create_task(fetch(media))
    await entered.wait()
    second = asyncio.create_task(fetch(media))
    await asyncio.sleep(0)
    first.cancel()
    with pytest.raises(asyncio.CancelledError):
        await first
    gate.set()
    path = await second
    assert path.read_bytes() == b"complete-verified-mp4"
    assert not list(parser.resumable.directory.glob("*.tmp"))
    assert await fetch(media) == path
    assert video.get_download_url.await_count == 2


@pytest.mark.parametrize("changed_during", ["select", "download", "validate", "publish"])
async def test_account_change_cannot_leave_wrong_scope_completed_media(media, monkeypatch, changed_during):
    parser, video, downloads = media
    first_account = Credential(sessdata="first-account", dedeuserid="111")
    second_account = Credential(sessdata="second-account", dedeuserid="222")
    parser.login._credential = first_account
    original_download = parser.resumable.download
    original_publish = parser.resumable.publish

    if changed_during == "select":
        async def select(**kwargs):
            parser.login._credential = second_account
            return play_data()
        video.get_download_url.side_effect = select
    elif changed_during == "download":
        async def download(*args, **kwargs):
            result = await original_download(*args, **kwargs)
            parser.login._credential = second_account
            return result
        monkeypatch.setattr(parser.resumable, "download", download)
    elif changed_during == "validate":
        async def validate(path, **kwargs):
            parser.login._credential = second_account
        monkeypatch.setattr("parser_under_test.core.parsers.bilibili.probe_media", validate)
    elif changed_during == "publish":
        async def publish(*args, **kwargs):
            await original_publish(*args, **kwargs)
            parser.login._credential = second_account
        monkeypatch.setattr(parser.resumable, "publish", publish)

    with pytest.raises(DownloadException, match="账号或下载设置已变化"):
        await fetch(media)
    directory = parser.resumable.directory
    assert not list(directory.glob("*.mp4"))
    assert not list(directory.glob("*.mp4.complete.json"))
    assert not list(directory.glob("*.index.json"))
    assert not list(directory.glob("*.m4s*"))

    # Switching back to A must perform an actual fresh download rather than use
    # a wrongly published, A-scoped completed artifact from the failed attempt.
    before = len(downloads)
    parser.login._credential = first_account
    video.get_download_url.side_effect = None
    monkeypatch.setattr(parser.resumable, "download", original_download)
    monkeypatch.setattr(parser.resumable, "publish", original_publish)
    monkeypatch.setattr("parser_under_test.core.parsers.bilibili.probe_media", AsyncMock())
    await fetch(media)
    assert len(downloads) == before + 2
    assert video.get_download_url.await_count == 2


async def test_account_aba_generation_change_is_not_hidden_by_same_cookies(media, monkeypatch):
    parser, video, _ = media
    parser.login._credential = Credential(sessdata="first-account", dedeuserid="111")
    old_key = parser._video_cache_key(BVID, 1)

    async def select(**kwargs):
        # Logout and login can return to the exact same cookies while a request is
        # awaiting the API. Generation is checked even though its durable key is A.
        parser.login._generation += 2
        return play_data()

    video.get_download_url.side_effect = select
    with pytest.raises(DownloadException, match="账号或下载设置已变化"):
        await fetch(media)
    assert not list(parser.resumable.directory.glob("*.mp4*"))
    new_key = parser._video_cache_key(BVID, 1)
    assert old_key != new_key
    assert old_key.fingerprint == new_key.fingerprint
    assert old_key.index_path(parser.resumable.directory) == new_key.index_path(parser.resumable.directory)
