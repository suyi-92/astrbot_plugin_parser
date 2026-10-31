import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from aiohttp import web
from bilibili_api import Credential
from bilibili_api.utils import network

from parser_under_test.core.config import BILIBILI_DEFAULTS, ParserConfig
from parser_under_test.core.exception import DownloadException, ParseException
from parser_under_test.core.parsers.bilibili import BilibiliParser
from parser_under_test.core.parsers.bilibili.api import BilibiliAPI, BilibiliVideo
from parser_under_test.core.parsers.bilibili.api_worker import dispatch
from parser_under_test.core.parsers.bilibili.login import (
    BilibiliLogin,
    parse_qr_login_cookies,
)
from parser_under_test.core.parsers.bilibili.streams import candidates, select_streams
from test_resumable import serve


def play_data(quality=80, codec="avc1.640028"):
    return {
        "dash": {
            "duration": 10,
            "video": [
                {
                    "id": quality,
                    "base_url": f"https://v.bilivideo.com/{quality}/video.m4s?signature=PRIVATE",
                    "backup_url": [
                        f"https://b.akamaized.net/{quality}/video.m4s?signature=PRIVATE"
                    ],
                    "codecs": codec,
                    "width": 1920 if quality == 80 else 852,
                    "height": 1080 if quality == 80 else 480,
                    "frame_rate": "30",
                    "bandwidth": 1000000,
                }
            ],
            "audio": [
                {
                    "id": 30280,
                    "base_url": "https://a.bilivideo.com/audio.m4s?signature=PRIVATE",
                    "backup_url": [
                        "https://b.akamaized.net/audio.m4s?signature=PRIVATE"
                    ],
                    "codecs": "mp4a.40.2",
                    "bandwidth": 128000,
                }
            ],
        }
    }


async def test_api_worker_settings_are_isolated_from_other_plugins(cfg):
    before_headers = network.HEADERS.copy()
    before_client = network.get_selected_client()[0]
    before_buvid = network.request_settings.get_enable_auto_buvid()
    api = BilibiliAPI(cfg)
    try:
        assert await api.call("settings") == {
            "client": "aiohttp",
            "user_agent": "curl/8.14.1",
            "auto_buvid": False,
        }
        assert network.HEADERS == before_headers
        assert network.get_selected_client()[0] == before_client
        assert network.request_settings.get_enable_auto_buvid() == before_buvid
        process = api.process
    finally:
        await api.close()
    assert process.returncode is not None


async def test_412_compatibility_uses_real_api_headers_without_auto_buvid(monkeypatch):
    import bilibili_api
    from bilibili_api.video import Video

    # Isolate library globals in this in-process worker unit test.
    settings = network.RequestSettings()
    monkeypatch.setattr(network, "request_settings", settings)
    monkeypatch.setattr(bilibili_api, "request_settings", settings)
    monkeypatch.setattr(network, "HEADERS", network.HEADERS.copy())
    monkeypatch.setattr(network, "session_pool", {k: {} for k in network.session_pool})
    monkeypatch.setattr(
        network, "lazy_settings", {k: {} for k in network.lazy_settings}
    )
    monkeypatch.setattr(network, "selected_client", network.selected_client)
    buvid = AsyncMock(side_effect=AssertionError("auto buvid must be off"))
    monkeypatch.setattr(network, "get_buvid", buvid)
    seen = []

    async def handler(request):
        seen.append(request.headers.get("User-Agent"))
        if seen[-1] != "curl/8.14.1":
            return web.Response(status=412)
        return web.json_response({"code": 0, "data": {"title": "public video"}})

    async with serve(handler) as base:

        async def info(self):
            return await network.Api(url=base, method="GET").result

        monkeypatch.setattr(Video, "get_info", info)
        request = {
            "action": "video",
            "params": {"bvid": "BV11EnQ6CER8", "method": "get_info"},
            "settings": {
                "client": "aiohttp",
                "user_agent": "library-UA",
                "auto_buvid": False,
            },
        }
        try:
            with pytest.raises(Exception) as exc:
                await dispatch(request)
            assert exc.value.status == 412
            request["settings"]["user_agent"] = "curl/8.14.1"
            assert await dispatch(request) == {"title": "public video"}
        finally:
            for pool in network.session_pool.values():
                for client in pool.values():
                    await client.close()
    assert seen == ["library-UA", "curl/8.14.1"]
    buvid.assert_not_awaited()


async def test_worker_sanitizes_exception(cfg):
    api = BilibiliAPI(cfg)
    try:
        with pytest.raises(ParseException) as exc:
            await api.call("not-supported", token="SECRET")
        assert "SECRET" not in str(exc.value)
    finally:
        await api.close()


def test_default_old_config_migration_and_schema():
    douyin = {"__template_key": "douyin", "enable": True, "cookies": "unchanged"}
    bili = {
        "__template_key": "bilibili",
        "enable": True,
        "cookies": "local-only",
        "video_codecs": "HEV",
    }
    config = ParserConfig([bili, douyin])
    for key, value in BILIBILI_DEFAULTS.items():
        assert getattr(config.bilibili, key) == value
    assert config.bilibili.video_codec_list == ["HEV"]
    assert douyin == {
        "__template_key": "douyin",
        "enable": True,
        "cookies": "unchanged",
    }
    root = Path(__file__).resolve().parents[1]
    schema = json.loads((root / "_conf_schema.json").read_text())
    items = schema["parsers_template"]["templates"]["bilibili"]["items"]
    template = json.loads((root / "default_template.json").read_text())[0]
    for key, value in BILIBILI_DEFAULTS.items():
        assert items[key]["default"] == template[key] == value


async def test_ai_summary_disabled_skips_extra_api_calls(cfg):
    parser = BilibiliParser(cfg, SimpleNamespace())
    parser.login._credential = object()
    parser.mycfg.fetch_ai_summary = False
    video = SimpleNamespace(
        get_cid=AsyncMock(side_effect=AssertionError("CID API must not be called")),
        get_ai_conclusion=AsyncMock(
            side_effect=AssertionError("AI summary API must not be called")
        ),
    )
    try:
        summary = await parser._get_ai_summary(video, SimpleNamespace(index=0))
        assert summary == ""
        video.get_cid.assert_not_awaited()
        video.get_ai_conclusion.assert_not_awaited()
    finally:
        await parser.close_session()


def test_actual_quality_ceiling_and_codec_identity():
    data = play_data(32)
    data["dash"]["video"] += play_data(80)["dash"]["video"]
    high = select_streams(data, 116, ["AVC"], False, 10)
    low = select_streams(data, 32, ["AVC"], False, 10)
    assert high.video.info["quality"] == 80  # No invented 1080P60.
    assert low.video.info["height"] == 480
    assert high.identity != low.identity
    anonymous = select_streams(play_data(32), 116, ["AVC"], False, 10)
    assert anonymous.video.info["quality"] == 32
    hevc = select_streams(play_data(80, "hvc1.1.6.L120.90"), 80, ["HEV"], False, 10)
    assert hevc.identity != high.identity
    assert "PRIVATE" not in str(high.video.info) + high.identity


def test_official_urls_preserved_and_pcdn_blocked():
    main = "https://upos-sz-mirrorcosov.bilivideo.com/x?sign=original"
    backup = "https://upos-hz-mirrorakam.akamaized.net/y?sign=backup"
    assert candidates({"base_url": main, "backup_url": [backup, main]}, False) == [
        backup,
        main,
    ]
    pcdn = "https://a.mcdn.bilivideo.com/x?sign=pcdn"
    assert candidates({"baseUrl": pcdn, "backupUrl": [backup]}, True) == [backup]
    with pytest.raises(DownloadException, match="block_pcdn"):
        candidates({"base_url": pcdn}, True)
    assert candidates({"base_url": pcdn}, False) == [pcdn]


def test_mp4_backup_and_quality_ceiling():
    raw = {
        "quality": 32,
        "format": "mp4",
        "durl": [
            {
                "url": "https://a.bilivideo.com/a.mp4",
                "backup_url": ["https://b.akamaized.net/b.mp4"],
                "size": 100,
            }
        ],
    }
    assert len(select_streams(raw, 80, ["AVC"], False, 10).video.urls) == 2
    raw["quality"] = 80
    with pytest.raises(DownloadException):
        select_streams(raw, 32, ["AVC"], False, 10)


async def test_old_480p_cache_not_used_for_1080p(cfg, monkeypatch):
    (cfg.cache_dir / "BV11EnQ6CER8-1.mp4").write_bytes(b"old480")
    parser = BilibiliParser(cfg, SimpleNamespace())
    video = SimpleNamespace(get_download_url=AsyncMock(return_value=play_data(80)))
    downloads = []

    async def download(urls, path, **kwargs):
        downloads.append(path)
        path.write_bytes(b"new1080")
        return path

    async def merge(**kwargs):
        kwargs["output_path"].write_bytes(b"new1080-merged")

    monkeypatch.setattr(parser.resumable, "download", download)
    monkeypatch.setattr("parser_under_test.core.parsers.bilibili.merge_av", merge)
    monkeypatch.setattr(
        "parser_under_test.core.parsers.bilibili.probe_media", AsyncMock()
    )
    try:
        path = await parser._download_media(video, "BV11EnQ6CER8", 1, 10)
        assert "-q80-" in path.name and path.read_bytes() == b"new1080-merged"
        assert len(downloads) == 2
        assert (cfg.cache_dir / "BV11EnQ6CER8-1.mp4").read_bytes() == b"old480"
        assert await parser._download_media(video, "BV11EnQ6CER8", 1, 10) == path
        assert len(downloads) == 2
    finally:
        await parser.close_session()


async def test_recent_cache_preflight_skips_playurl(cfg):
    parser = BilibiliParser(cfg, SimpleNamespace())
    parser.login._credential = Credential(
        sessdata="synthetic-session", dedeuserid="12345"
    )
    bvid, page = "BV11EnQ6CER8", 1
    resource = f"{bvid}:{page}:synthetic-stream"
    cached = parser.resumable.directory / "cached-q80-synthetic.mp4"
    part = cached.with_name(cached.name + ".part")
    part.write_bytes(b"verified synthetic media")
    await parser.resumable.publish(part, cached, resource)
    await parser._write_video_cache_index(bvid, page, cached, resource)
    try:
        found = await parser._find_cached_video(bvid, page)
        assert found == cached
    finally:
        await parser.close_session()


async def test_cache_preflight_isolated_by_bilibili_account(cfg):
    parser = BilibiliParser(cfg, SimpleNamespace())
    bvid, page = "BV11EnQ6CER8", 1
    parser.login._credential = Credential(
        sessdata="synthetic-session-a", dedeuserid="111"
    )
    index_a = parser._cache_index_path(bvid, page)
    resource = f"{bvid}:{page}:synthetic-stream"
    cached = parser.resumable.directory / "cached-q80-account-a.mp4"
    part = cached.with_name(cached.name + ".part")
    part.write_bytes(b"account-scoped media")
    await parser.resumable.publish(part, cached, resource)
    await parser._write_video_cache_index(bvid, page, cached, resource)
    parser.login._credential = Credential(
        sessdata="synthetic-session-b", dedeuserid="222"
    )
    try:
        assert parser._cache_index_path(bvid, page) != index_a
        assert await parser._find_cached_video(bvid, page) is None
    finally:
        await parser.close_session()


async def test_missing_audio_does_not_merge(cfg, monkeypatch):
    data = play_data()
    data["dash"]["audio"] = []
    parser = BilibiliParser(cfg, SimpleNamespace())
    merge = AsyncMock()
    monkeypatch.setattr("parser_under_test.core.parsers.bilibili.merge_av", merge)
    try:
        with pytest.raises(DownloadException, match="音频"):
            await parser._download_media(
                SimpleNamespace(get_download_url=AsyncMock(return_value=data)),
                "BV11EnQ6CER8",
                1,
                10,
            )
        merge.assert_not_awaited()
    finally:
        await parser.close_session()


async def test_audio_failure_cancels_video_no_merge(cfg, monkeypatch):
    parser = BilibiliParser(cfg, SimpleNamespace())
    cancelled = asyncio.Event()

    async def download(urls, path, **kwargs):
        if ".audio." in path.name:
            await asyncio.sleep(0.01)
            raise DownloadException("audio failed")
        try:
            await asyncio.sleep(10)
        finally:
            cancelled.set()

    merge = AsyncMock()
    monkeypatch.setattr(parser.resumable, "download", download)
    monkeypatch.setattr("parser_under_test.core.parsers.bilibili.merge_av", merge)
    try:
        with pytest.raises(DownloadException, match="audio failed"):
            await parser._download_media(
                SimpleNamespace(get_download_url=AsyncMock(return_value=play_data())),
                "BV11EnQ6CER8",
                1,
                10,
            )
        assert cancelled.is_set()
        merge.assert_not_awaited()
        assert not list(parser.resumable.directory.glob("*.mp4"))
    finally:
        await parser.close_session()


async def test_login_logout_twice_and_permissions(cfg, monkeypatch):
    login = BilibiliLogin(cfg)
    login._qr_request = AsyncMock(
        side_effect=[
            (
                {
                    "url": "https://passport.bilibili.com/qrcode/test",
                    "qrcode_key": "synthetic-key",
                },
                [],
            ),
            (
                {"code": 0, "refresh_token": "synthetic-refresh"},
                [
                    "SESSDATA=synthetic-session; Path=/",
                    "bili_jct=synthetic-csrf; Path=/",
                    "DedeUserID=123; Path=/",
                ],
            ),
        ]
    )
    assert (await login.login_with_qrcode()).startswith(b"\x89PNG")
    assert "成功" in (await anext(login.check_qr_state()))
    assert login._credential.has_sessdata()
    assert login.credential_file.stat().st_mode & 0o777 == 0o600
    assert login._qr_key is None
    cfg.parser.bilibili.cookies = "SESSDATA=synthetic-config"
    assert await login.logout()
    assert not await login.logout()
    assert await login.credential is None and not login.has_local_credential()
    assert not login.credential_file.exists() and cfg.parser.bilibili.cookies == ""
    assert cfg.save_config.call_count == 2


async def test_late_qr_and_refresh_cannot_restore_logged_out_credential(cfg):
    api = SimpleNamespace(call=AsyncMock(), reset=AsyncMock())
    login = BilibiliLogin(cfg, api)
    login._qr_key = "old-key"
    reached, release = asyncio.Event(), asyncio.Event()

    async def late(*args):
        reached.set()
        await release.wait()
        return {"code": 0}, ["SESSDATA=synthetic-late"]

    login._qr_request = late

    async def consume():
        return [msg async for msg in login.check_qr_state()]

    task = asyncio.create_task(consume())
    await reached.wait()
    await login.logout()
    release.set()
    assert await task == []
    assert not login.credential_file.exists()
    assert await login.credential is None
    api.reset.assert_awaited_once()


async def test_anonymous_no_cookie_needs_no_validation_request(cfg):
    api = SimpleNamespace(call=AsyncMock(return_value={"title": "public"}))
    login = BilibiliLogin(cfg, api)
    assert await login.credential is None
    api.call.assert_not_awaited()
    video = BilibiliVideo(api, login, bvid="BV11EnQ6CER8")
    assert await video.get_info() == {"title": "public"}
    assert api.call.call_args.kwargs["credential"] is None


async def test_malformed_cookie_file_and_logout_save_error_safe(cfg):
    login = BilibiliLogin(cfg)
    login.credential_file.parent.mkdir(exist_ok=True)
    login.credential_file.write_text("not-json")
    assert await login.credential is None
    cfg.save_config.side_effect = OSError("SECRET")
    with pytest.raises(ParseException) as exc:
        await login.logout()
    assert "SECRET" not in str(exc.value)
    assert login._credential is None and not login.credential_file.exists()


def test_qr_cookies_legacy_and_header_formats():
    cookies = parse_qr_login_cookies(
        {"url": "https://passport.bilibili.com/?SESSDATA=a%2Cb&amp;bili_jct=c"},
        ["DedeUserID=123; Secure"],
    )
    assert cookies == {"SESSDATA": "a%2Cb", "bili_jct": "c", "DedeUserID": "123"}


async def test_late_refresh_after_logout_cannot_save(cfg):
    reached, release = asyncio.Event(), asyncio.Event()

    async def refreshing(*args, **kwargs):
        reached.set()
        await release.wait()
        return {"SESSDATA": "synthetic-stale", "bili_jct": "synthetic-csrf"}

    api = SimpleNamespace(call=refreshing, reset=AsyncMock())
    login = BilibiliLogin(cfg, api)
    login._initialized = True
    login._credential = Credential(
        sessdata="synthetic-session",
        bili_jct="synthetic-csrf",
        ac_time_value="synthetic-token",
    )
    task = asyncio.create_task(login.credential)
    await reached.wait()
    await login.logout()
    release.set()
    assert await task is None
    assert not login.credential_file.exists()


async def test_disabled_parser_logout_still_clears_disk(cfg):
    from parser_under_test.main import ParserPlugin

    cfg.parser._nodes.clear()
    plugin = object.__new__(ParserPlugin)
    plugin.cfg, plugin.parser_map, plugin._offline_bili_login = cfg, {}, None
    login = plugin._get_bilibili_login()
    login._credential = Credential(sessdata="synthetic")
    login._initialized = True
    login._save_credential()
    event = SimpleNamespace(plain_result=lambda text: text)
    assert "已清除" in await anext(plugin.logout_bilibili(event))
    assert not login.credential_file.exists()


async def test_refresh_changed_quality_never_hands_lower_stream_to_sibling(
    cfg, monkeypatch
):
    parser = BilibiliParser(cfg, SimpleNamespace())
    video = SimpleNamespace(
        get_download_url=AsyncMock(side_effect=[play_data(80), play_data(32)])
    )
    errors = []

    async def download(urls, path, *, refresh, **kwargs):
        try:
            await refresh()
        except DownloadException as exc:
            errors.append(str(exc))
            raise
        raise AssertionError("changed stream must not escape refresh guard")

    monkeypatch.setattr(parser.resumable, "download", download)
    try:
        with pytest.raises(DownloadException, match="刷新失败"):
            await parser._download_media(video, "BV11EnQ6CER8", 1, 10)
        assert len(errors) == 2
        assert video.get_download_url.await_count == 2
    finally:
        await parser.close_session()


async def test_failed_merge_never_publishes_or_leaks_stderr(cfg, monkeypatch):
    parser = BilibiliParser(cfg, SimpleNamespace())

    async def download(urls, path, **kwargs):
        path.write_bytes(b"complete-stream")
        return path

    async def broken_merge(**kwargs):
        kwargs["output_path"].write_bytes(b"half-merged")
        raise RuntimeError("signed-url?token=SECRET")

    monkeypatch.setattr(parser.resumable, "download", download)
    monkeypatch.setattr(
        "parser_under_test.core.parsers.bilibili.merge_av", broken_merge
    )
    try:
        with pytest.raises(DownloadException, match="合并失败") as exc:
            await parser._download_media(
                SimpleNamespace(get_download_url=AsyncMock(return_value=play_data())),
                "BV11EnQ6CER8",
                1,
                10,
            )
        assert "SECRET" not in str(exc.value)
        assert not list(parser.resumable.directory.iterdir())
    finally:
        await parser.close_session()


async def test_logout_invalidates_queued_worker_calls(cfg):
    api = BilibiliAPI(cfg)
    await api.lock.acquire()
    task = asyncio.create_task(
        api.call("settings", credential=Credential(sessdata="synthetic-old"))
    )
    await asyncio.sleep(0)
    await api.reset()
    api.lock.release()
    try:
        with pytest.raises(ParseException, match="退出登录取消"):
            await task
        assert api.process is None
        assert (await api.call("settings"))["client"] == "aiohttp"
    finally:
        await api.close()
