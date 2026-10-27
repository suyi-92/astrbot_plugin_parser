import asyncio
import hashlib
import json
import re
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from aiohttp import web
from astrbot.core.message.components import Plain, Video

from parser_under_test.core import diagnostics as diag
from parser_under_test.core import resumable, sender as sender_module
from parser_under_test.core.data import ParseResult, Platform, VideoContent
from parser_under_test.core.sender import MessageSender
from parser_under_test.main import ParserPlugin
from test_resumable import serve


@pytest.fixture
def messages(monkeypatch):
    captured = []
    monkeypatch.setattr(diag.logger, "debug", lambda message: captured.append(message))
    return captured


def test_diagnostics_disabled_and_error_redaction(messages):
    with diag.trace_scope(False), diag.stage(False, "disabled"):
        diag.note(False, "hidden")
    assert messages == []
    with pytest.raises(RuntimeError):
        with diag.trace_scope(True), diag.stage(True, "测试阶段"):
            raise RuntimeError("https://cdn.invalid/?sign=SECRET Cookie=SESSDATA")
    output = "\n".join(messages)
    assert "elapsed_s=" in output and "failed error=RuntimeError" in output
    assert "SECRET" not in output and "SESSDATA" not in output
    assert "https://" not in output and diag._TRACE.get() == "-"


async def test_trace_isolated_and_inherited_by_child_tasks(messages):
    ready = asyncio.Event()

    async def child():
        await ready.wait()
        diag.note(True, "child")

    with diag.trace_scope(True):
        first_id = diag._TRACE.get()
        first = asyncio.create_task(child())
    with diag.trace_scope(True):
        second_id = diag._TRACE.get()
        second = asyncio.create_task(child())
    assert first_id != second_id and diag._TRACE.get() == "-"
    ready.set()
    await asyncio.gather(first, second)
    assert any(first_id in line for line in messages)
    assert any(second_id in line for line in messages)


async def test_timed_stage_preserves_cancellation(messages):
    started = asyncio.Event()

    async def pending():
        with diag.trace_scope(True), diag.stage(True, "等待阶段"):
            started.set()
            await asyncio.Event().wait()

    task = asyncio.create_task(pending())
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert "result=cancelled" in messages[-1]
    assert diag._TRACE.get() == "-"


def test_progress_reports_interval_rate_and_unknown_total(messages, monkeypatch):
    clock = [0.0]
    monkeypatch.setattr(diag, "time", SimpleNamespace(monotonic=lambda: clock[0]))
    progress = diag.TransferProgress(True, "video", "cdn.example", 1024, None)
    clock[0] = 9
    progress.update(2048)
    assert not messages
    clock[0] = 10
    progress.update(11264)
    assert "rate_kib_s=1.0" in messages[-1]
    assert "total_mib=unknown" in messages[-1]
    clock[0] = 12
    progress.update(13312, force=True)
    assert len(messages) == 2 and "rate_kib_s=1.0" in messages[-1]


async def test_download_diagnostics_resume_backup_and_cache_without_secrets(
    cfg, messages, monkeypatch
):
    cfg.debug_media_info = True
    monkeypatch.setattr(resumable, "validate_media_url", lambda url: None)
    downloader = resumable.ResumableDownloader(cfg)
    body, cut, requests = b"test-data" * 4096, 1024, []

    async def handler(request):
        requests.append(request.path)
        assert "Cookie" not in request.headers
        if request.path == "/bad":
            return web.Response(status=567)
        assert request.headers["Range"] == f"bytes={cut}-"
        return web.Response(
            status=206,
            body=body[cut:],
            headers={
                "Content-Range": f"bytes {cut}-{len(body) - 1}/{len(body)}",
                "ETag": '"same"',
            },
        )

    try:
        async with serve(handler) as base:
            urls = [base + "/bad?sign=SECRET", base + "/good?sign=SECRET"]
            path = downloader.directory / "video.mp4"
            Path(str(path) + ".part").write_bytes(body[:cut])
            Path(str(path) + ".part.json").write_text(
                json.dumps(
                    {
                        "resource": "r:video",
                        "total": len(body),
                        "etag": '"same"',
                        "url_hash": hashlib.sha256(urls[1].encode()).hexdigest(),
                    }
                )
            )
            with diag.trace_scope(True):
                await downloader.download(
                    urls,
                    path,
                    resource="r:video",
                    headers={"Cookie": "SESSDATA=SECRET"},
                )
                await downloader.download(urls, path, resource="r:video")
            assert path.read_bytes() == body
    finally:
        await downloader.close()
    output = "\n".join(messages)
    assert requests == ["/bad", "/good"]
    for marker in (
        "status=567",
        "status=206",
        "resume_bytes=1024",
        "下载进度",
        "流缓存命中",
    ):
        assert marker in output
    assert "SECRET" not in output and "SESSDATA" not in output
    assert "?sign" not in output and "http://" not in output


async def test_large_video_remains_video_and_upload_timing_waits_for_adapter(
    cfg, messages, monkeypatch
):
    cfg.debug_media_info = True
    cfg.single_heavy_render_card = False
    cfg.forward_threshold = 2
    path = cfg.cache_dir / "large.mp4"
    with path.open("wb") as target:
        target.truncate(71 * 1024**2)
    monkeypatch.setattr(
        sender_module, "transcode_video_audio_to_lc", AsyncMock(return_value=path)
    )
    monkeypatch.setattr(
        sender_module,
        "probe_media",
        AsyncMock(
            return_value={
                "streams": [{"codec_type": "video", "width": 1920, "height": 1080}]
            }
        ),
    )
    entered, release = asyncio.Event(), asyncio.Event()

    async def send(chain):
        assert len(chain) == 1 and isinstance(chain[0], Video)
        entered.set()
        await release.wait()

    event = SimpleNamespace(send=send, chain_result=lambda segments: segments)
    sender = MessageSender(cfg, SimpleNamespace())
    result = ParseResult(
        platform=Platform("bilibili", "B站"), contents=[VideoContent(path)]
    )
    try:
        with diag.trace_scope(True):
            task = asyncio.create_task(sender.send_parse_result(event, result))
        await asyncio.wait_for(entered.wait(), 2)
        assert any("平台上传/发送 开始" in line for line in messages)
        assert not any("平台上传/发送 结束" in line for line in messages)
        release.set()
        await task
    finally:
        release.set()
        await sender.close()
    assert any("平台上传/发送 结束" in line for line in messages)
    assert any("component=Video" in line for line in messages)
    assert any("bytes=74448896" in line for line in messages)


async def test_message_entry_keeps_parse_and_send_in_same_trace(cfg, messages):
    cfg.debug_media_info = True
    cfg.whitelist = []
    cfg.blacklist = []
    cfg.require_at_in_group = False
    cfg.enable_reply_parse = False
    result = ParseResult(
        platform=Platform("bilibili", "B站"), url="https://bilibili.com/BV1VGJB67Eus"
    )

    async def parse(*args):
        diag.note(True, "parse-marker")
        return result

    async def send(*args):
        diag.note(True, "send-marker")

    plugin = ParserPlugin.__new__(ParserPlugin)
    plugin.cfg = cfg
    plugin.parser_map = {"BV": SimpleNamespace(parse=parse)}
    plugin.key_pattern_list = [("BV", re.compile(r"BV[0-9A-Za-z]{10}"))]
    plugin.debouncer = SimpleNamespace(
        hit_link=lambda *args: False, hit_resource=lambda *args: False
    )
    plugin.sender = SimpleNamespace(send_parse_result=send)
    event = SimpleNamespace(
        unified_msg_origin="test",
        message_str="BV1VGJB67Eus",
        get_messages=lambda: [Plain("BV1VGJB67Eus")],
        get_self_id=lambda: "bot",
    )
    await plugin.on_message(event)
    markers = [line for line in messages if "-marker" in line]
    assert len(markers) == 2
    assert markers[0].split("]")[0] == markers[1].split("]")[0]
    assert "[解析诊断 -]" not in markers[0]
    assert diag._TRACE.get() == "-"
