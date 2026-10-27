import asyncio
import hashlib
import json
import os
import time
from contextlib import asynccontextmanager
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from aiohttp import web

from parser_under_test.core import resumable as mod
from parser_under_test.core.exception import DownloadException, SizeLimitException
from parser_under_test.core.resumable import (
    ResumableDownloader,
    digest_file,
    validate_media_url,
)


@asynccontextmanager
async def serve(handler):
    app = web.Application()
    app.router.add_get("/{name:.*}", handler)
    runner = web.AppRunner(app, access_log=None)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        await runner.cleanup()


@pytest.fixture
async def dl(cfg, monkeypatch):
    # Tests intentionally use loopback. Production exposes no such bypass option.
    monkeypatch.setattr(mod, "validate_media_url", lambda url: None)
    downloader = ResumableDownloader(cfg)
    yield downloader
    await downloader.close()


def seed(dl, url, body, *, total=None, etag='"same"'):
    path = dl.directory / "test.mp4"
    Path(str(path) + ".part").write_bytes(body)
    Path(str(path) + ".part.json").write_text(
        json.dumps(
            {
                "resource": "r",
                "url_hash": hashlib.sha256(url.encode()).hexdigest(),
                "total": total or len(body) * 2,
                "etag": etag,
                "modified": "",
            }
        )
    )
    return path


async def test_70mb_disconnect_at_40mb_uses_range(dl):
    # Reproduce the reported exact byte counts, without any remote API.
    total, cut = 73745233, 43122045
    block = bytes(range(256)) * 4096
    body = (block * (total // len(block) + 1))[:total]
    ranges = []

    async def handler(request):
        ranges.append(request.headers.get("Range"))
        if len(ranges) == 1:
            response = web.StreamResponse(
                headers={"Content-Length": str(total), "ETag": '"same"'}
            )
            await response.prepare(request)
            for start in range(0, cut, len(block)):
                await response.write(body[start : min(start + len(block), cut)])
            await asyncio.sleep(0.03)
            request.transport.abort()
            return response
        offset = int(request.headers["Range"][6:-1])
        assert request.headers["If-Range"] == '"same"'
        return web.Response(
            status=206,
            body=body[offset:],
            headers={
                "Content-Range": f"bytes {offset}-{total - 1}/{total}",
                "ETag": '"same"',
            },
        )

    async with serve(handler) as base:
        path = await dl.download(
            [base + "/video?sign=SECRET"], dl.directory / "70mb.mp4", resource="r"
        )
    assert len(ranges) == 2
    assert ranges[0] is None
    assert int(ranges[1][6:-1]) == cut
    assert path.stat().st_size == total
    assert digest_file(path) == hashlib.sha256(body).hexdigest()
    assert not Path(str(path) + ".part").exists()
    assert await dl.cached(path, "r")
    assert "SECRET" not in Path(str(path) + ".complete.json").read_text()


async def test_primary_567_backup_success(dl):
    calls = []

    async def handler(request):
        calls.append(request.path)
        return (
            web.Response(status=567)
            if request.path == "/main"
            else web.Response(body=b"official-backup")
        )

    async with serve(handler) as base:
        result = await dl.download(
            [base + "/main", base + "/backup"], dl.directory / "x", resource="r"
        )
    assert result.read_bytes() == b"official-backup"
    assert calls == ["/main", "/backup"]


async def test_unreachable_and_backup_failure_redacted(dl, monkeypatch):
    logs = []
    monkeypatch.setattr(mod.logger, "warning", logs.append)

    async def handler(request):
        return web.Response(status=503)

    async with serve(handler) as base:
        with pytest.raises(DownloadException, match="官方备用节点") as exc:
            await dl.download(
                [
                    "http://127.0.0.1:1/private?token=SECRET",
                    base + "/backup?sign=SECRET",
                ],
                dl.directory / "x",
                resource="r",
                headers={"Cookie": "SESSDATA=SECRET"},
            )
    assert not (dl.directory / "x").exists()
    assert "SECRET" not in str(exc.value) + str(logs)
    assert all("?" not in message for message in logs)


async def test_ignored_range_200_truncates(dl):
    body = b"new-full-content"

    async def handler(request):
        assert request.headers["Range"] == "bytes=3-"
        return web.Response(body=body, headers={"ETag": '"new"'})

    async with serve(handler) as base:
        path = seed(dl, base, b"old", total=100)
        await dl.download([base], path, resource="r")
    assert path.read_bytes() == body


@pytest.mark.parametrize(
    "kind",
    ["416", "wrong-start", "changed-total", "changed-etag", "malformed", "bad-length"],
)
async def test_invalid_range_safely_restarts(dl, kind):
    body, ranges = b"abcdef", []

    async def handler(request):
        ranges.append(request.headers.get("Range"))
        if len(ranges) > 1:
            return web.Response(body=body)
        headers = {"Content-Range": "bytes 3-5/6", "ETag": '"same"'}
        if kind == "416":
            return web.Response(status=416, headers={"Content-Range": "bytes */6"})
        if kind == "wrong-start":
            headers["Content-Range"] = "bytes 0-2/6"
        if kind == "changed-total":
            headers["Content-Range"] = "bytes 3-5/7"
        if kind == "changed-etag":
            headers["ETag"] = '"different"'
        if kind == "malformed":
            headers["Content-Range"] = "nonsense"
        if kind == "bad-length":
            headers["Content-Range"] = "bytes 3-4/6"
        return web.Response(status=206, body=body[3:], headers=headers)

    async with serve(handler) as base:
        path = seed(dl, base, b"abc")
        await dl.download([base], path, resource="r")
    assert ranges == ["bytes=3-", None]
    assert path.read_bytes() == body


@pytest.mark.parametrize("etag", ['"same"', 'W/"weak"', ""])
async def test_cross_cdn_only_matching_strong_etag_can_resume(dl, etag):
    received = []

    async def handler(request):
        received.append(request.headers.get("Range"))
        if received[-1]:
            return web.Response(
                status=206,
                body=b"def",
                headers={"Content-Range": "bytes 3-5/6", "ETag": etag},
            )
        return web.Response(body=b"abcdef")

    async with serve(handler) as base:
        path = seed(dl, "https://old.bilivideo.com/asset?old=1", b"abc", etag=etag)
        await dl.download([base], path, resource="r")
    assert received == (["bytes=3-"] if etag == '"same"' else [None])
    assert path.read_bytes() == b"abcdef"


async def test_cancel_keeps_only_partial_and_reload_resumes(dl, cfg):
    ready = asyncio.Event()

    async def handler(request):
        if request.headers.get("Range"):
            return web.Response(
                status=206,
                body=b"def",
                headers={"ETag": '"same"', "Content-Range": "bytes 3-5/6"},
            )
        response = web.StreamResponse(headers={"Content-Length": "6", "ETag": '"same"'})
        await response.prepare(request)
        await response.write(b"abc")
        ready.set()
        await asyncio.sleep(0.2)
        return response

    async with serve(handler) as base:
        path = dl.directory / "cancel"
        task = dl.start(dl.download([base], path, resource="r"))
        await ready.wait()
        await asyncio.sleep(0.02)
        await dl.close()
        assert task.cancelled()
        assert not path.exists()
        assert Path(str(path) + ".part").read_bytes() == b"abc"
        next_dl = ResumableDownloader(cfg)
        try:
            assert (
                await next_dl.download([base], path, resource="r")
            ).read_bytes() == b"abcdef"
        finally:
            await next_dl.close()


@pytest.mark.parametrize("length_known", [True, False])
async def test_size_limit_cleans_partial(dl, length_known):
    dl.cfg.source_max_size = 0.001

    async def handler(request):
        if length_known:
            return web.Response(body=b"x" * 4096)
        response = web.StreamResponse()
        await response.prepare(request)
        await response.write(b"x" * 4096)
        await response.write_eof()
        return response

    async with serve(handler) as base:
        path = dl.directory / "big"
        with pytest.raises(SizeLimitException):
            await dl.download([base], path, resource="r")
        assert not path.exists()
        assert not list(dl.directory.glob("*.part*"))


async def test_chunked_without_content_length(dl):
    async def handler(request):
        response = web.StreamResponse()
        await response.prepare(request)
        await response.write(b"abc")
        await response.write(b"def")
        return response

    async with serve(handler) as base:
        assert (
            await dl.download([base], dl.directory / "chunked", resource="r")
        ).read_bytes() == b"abcdef"


async def test_concurrent_cache_single_writer_and_tamper_detection(dl):
    count = 0

    async def handler(request):
        nonlocal count
        count += 1
        await asyncio.sleep(0.03)
        return web.Response(body=b"complete")

    async with serve(handler) as base:
        path = dl.directory / "same"
        values = await asyncio.gather(
            *[dl.download([base], path, resource="r") for _ in range(5)]
        )
        assert count == 1 and values == [path] * 5
        path.write_bytes(b"corrupt!")
        assert not await dl.cached(path, "r")
        await dl.download([base], path, resource="r")
        assert count == 2


async def test_expired_url_refresh_once_with_shared_attempt_budget(dl):
    refresh = AsyncMock()
    calls = []

    async def handler(request):
        calls.append(request.path)
        return (
            web.Response(body=b"fresh")
            if request.path == "/fresh"
            else web.Response(status=403)
        )

    async with serve(handler) as base:
        refresh.return_value = [base + "/fresh"]
        path = await dl.download(
            [base + "/old"], dl.directory / "x", resource="r", refresh=refresh
        )
    assert path.read_bytes() == b"fresh" and refresh.await_count == 1
    assert calls == ["/old", "/fresh"]


async def test_429_does_not_rotate_or_refresh(dl):
    refresh = AsyncMock()
    calls = []

    async def handler(request):
        calls.append(1)
        return web.Response(status=429)

    async with serve(handler) as base:
        with pytest.raises(DownloadException, match="受限"):
            await dl.download(
                [base, base + "/backup"],
                dl.directory / "x",
                resource="r",
                refresh=refresh,
            )
    assert len(calls) == 1
    refresh.assert_not_awaited()


async def test_task_deadline_is_bounded(dl):
    dl.options.task_timeout = 0.02

    async def handler(request):
        await asyncio.sleep(1.1)
        return web.Response(body=b"late")

    async with serve(handler) as base:
        with pytest.raises(DownloadException, match="时间上限"):
            await dl.download([base], dl.directory / "x", resource="r")
    assert not (dl.directory / "x").exists()


def test_cleanup_expired_and_budget(dl):
    path = dl.directory / "expired.part"
    path.write_bytes(b"x")
    meta = Path(str(path) + ".json")
    meta.write_text("{}")
    for p in (path, meta):
        os.utime(p, (time.time() - 90000, time.time() - 90000))
    dl.cleanup()
    assert not path.exists() and not meta.exists()
    dl.cfg.source_max_size = 0.000001
    for i in range(3):
        (dl.directory / f"{i}.part").write_bytes(b"x" * 4)
    dl.cleanup()
    assert sum(p.stat().st_size for p in dl.directory.glob("*.part")) <= 4.2


@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1/x",
        "http://[::1]/x",
        "http://169.254.169.254/x",
        "file:///etc/passwd",
        "https://evil.example/x",
        "https://bilivideo.com.evil.example/x",
        "https://user:password@a.bilivideo.com/x",
        "https://a.bilivideo.com:22/x",
    ],
)
def test_url_security(url):
    with pytest.raises(DownloadException):
        validate_media_url(url)


async def test_dns_private_address_rejected(monkeypatch):
    monkeypatch.setattr(
        mod.DefaultResolver, "resolve", AsyncMock(return_value=[{"host": "10.0.0.1"}])
    )
    resolver = mod.PublicResolver()
    try:
        with pytest.raises(OSError, match="non-public"):
            await resolver.resolve("cdn.bilivideo.com")
    finally:
        await resolver.close()


async def test_request_total_timeout_resumes_in_new_request(dl):
    dl.cfg.download_timeout = 0.05
    ranges = []

    async def handler(request):
        ranges.append(request.headers.get("Range"))
        if len(ranges) > 1:
            return web.Response(
                status=206,
                body=b"def",
                headers={"Content-Range": "bytes 3-5/6", "ETag": '"same"'},
            )
        response = web.StreamResponse(headers={"Content-Length": "6", "ETag": '"same"'})
        await response.prepare(request)
        await response.write(b"abc")
        await asyncio.sleep(1.2)
        return response

    async with serve(handler) as base:
        path = await dl.download([base], dl.directory / "slow", resource="r")
    assert ranges == [None, "bytes=3-"]
    assert path.read_bytes() == b"abcdef"


@pytest.mark.parametrize("changed", [False, True])
async def test_last_modified_same_url_resume_or_restart(dl, changed):
    modified = "Wed, 21 Oct 2015 07:28:00 GMT"
    seen = []

    async def handler(request):
        seen.append(request.headers.get("Range"))
        if seen[-1]:
            assert request.headers["If-Range"] == modified
            return web.Response(
                status=206,
                body=b"def",
                headers={
                    "Content-Range": "bytes 3-5/6",
                    "Last-Modified": modified
                    if not changed
                    else "Thu, 22 Oct 2015 07:28:00 GMT",
                },
            )
        return web.Response(body=b"abcdef")

    async with serve(handler) as base:
        path = seed(dl, base, b"abc", etag="")
        meta_path = Path(str(path) + ".part.json")
        metadata = json.loads(meta_path.read_text())
        metadata["modified"] = modified
        meta_path.write_text(json.dumps(metadata))
        await dl.download([base], path, resource="r")
    assert seen == (["bytes=3-", None] if changed else ["bytes=3-"])
    assert path.read_bytes() == b"abcdef"


async def test_redirect_revalidated_and_cookie_not_forwarded(dl, monkeypatch):
    seen = []

    async def handler(request):
        assert (
            "Cookie" not in request.headers and "Authorization" not in request.headers
        )
        seen.append(request.path)
        return web.Response(
            status=302, headers={"Location": "http://169.254.169.254/credentials"}
        )

    async with serve(handler) as base:

        def validate(url):
            if not url.startswith(base):
                validate_media_url(url)

        monkeypatch.setattr(mod, "validate_media_url", validate)
        with pytest.raises(DownloadException, match="安全检查"):
            await dl.download(
                [base],
                dl.directory / "x",
                resource="r",
                headers={"Cookie": "SESSDATA=SECRET", "Authorization": "Bearer SECRET"},
            )
    assert seen == ["/"]
