"""Local regressions for the production truncation/changed-range failure."""

import asyncio
import hashlib
import json
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from aiohttp import web

from parser_under_test.core.exception import DownloadException
from parser_under_test.core.parsers.bilibili.streams import candidates
from parser_under_test.core.resumable import digest_file
from parser_under_test.core import resumable as mod
from test_resumable import seed, serve


@pytest.fixture
async def dl(cfg, monkeypatch):
    # Local servers are intentionally allowed only inside these tests.
    monkeypatch.setattr(mod, "validate_media_url", lambda url: None)
    downloader = mod.ResumableDownloader(cfg)
    try:
        yield downloader
    finally:
        await downloader.close()


def test_preferred_official_cdn_preserves_candidates_and_security():
    akam = "https://upos-hz-mirrorakam.akamaized.net/video?sign=akam"
    cosov = "https://upos-sz-mirrorcosov.bilivideo.com/video?sign=cosov"
    other = "https://upos-sz-mirrorcos.bilivideo.com/video?sign=other"
    pcdn = "https://a.mcdn.bilivideo.com/video?sign=pcdn"
    raw = {
        "base_url": akam,
        "backup_url": [other, cosov, pcdn, "https://bilivideo.com.evil.test/x", cosov],
    }
    assert candidates(raw, True) == [cosov, akam, other]
    assert candidates(raw, False) == [cosov, akam, other, pcdn]
    # Priority never manufactures a host or a signed URL absent from the API.
    assert candidates({"base_url": akam, "backup_url": [other]}, True) == [akam, other]


@pytest.mark.parametrize("retry_times, succeeds", [(2, True), (1, False)])
async def test_truncation_switches_once_then_mismatched_etag_restarts_within_budget(
    dl, retry_times, succeeds
):
    dl.cfg.download_retry_times = retry_times
    calls = []
    original, replacement = b"abcdef", b"UVWXYZ"

    async def handler(request):
        calls.append((request.path, request.headers.get("Range")))
        if request.path == "/primary":
            response = web.StreamResponse(
                headers={"Content-Length": "6", "ETag": '"original"'}
            )
            await response.prepare(request)
            await response.write(original[:3])
            await asyncio.sleep(0.03)
            request.transport.abort()
            return response
        if request.headers.get("Range"):
            assert request.headers["If-Range"] == '"original"'
            return web.Response(
                status=206,
                body=replacement[3:],
                headers={"ETag": '"replacement"', "Content-Range": "bytes 3-5/6"},
            )
        return web.Response(body=replacement, headers={"ETag": '"replacement"'})

    validated = []

    async def validate(part):
        validated.append(part.read_bytes())
        assert part.read_bytes() == replacement

    async with serve(handler) as base:
        path = dl.directory / "production.mp4"
        operation = dl.download(
            [base + "/primary?sign=SECRET", base + "/backup?sign=SECRET"],
            path,
            resource="r",
            validate=validate,
        )
        if succeeds:
            await operation
            assert path.read_bytes() == replacement
            assert digest_file(path) == hashlib.sha256(replacement).hexdigest()
            assert await dl.cached(path, "r")
            assert validated == [replacement]
            assert not list(dl.directory.glob("*.part*"))
        else:
            with pytest.raises(DownloadException, match="changed-range"):
                await operation
            assert not path.exists()
            assert not Path(str(path) + ".complete.json").exists()
            assert not list(dl.directory.glob("*.part*"))
            assert validated == []
    assert calls == [
        ("/primary", None),
        ("/backup", "bytes=3-"),
    ] + ([("/backup", None)] if succeeds else [])


@pytest.mark.parametrize(
    "changed", ["etag", "total", "modified", "missing-modified", "added-modified"]
)
async def test_cross_cdn_validator_difference_discards_old_bytes(dl, changed):
    seen = []
    body = b"UVWXYZA" if changed == "total" else b"UVWXYZ"
    old_modified = "Wed, 21 Oct 2015 07:28:00 GMT"
    new_modified = old_modified
    if changed == "modified":
        new_modified = "Thu, 22 Oct 2015 07:28:00 GMT"
    elif changed == "missing-modified":
        new_modified = ""

    async def handler(request):
        seen.append(request.headers.get("Range"))
        headers = {"ETag": '"new"' if changed == "etag" else '"same"'}
        if new_modified:
            headers["Last-Modified"] = new_modified
        if request.headers.get("Range"):
            headers["Content-Range"] = f"bytes 3-{len(body) - 1}/{len(body)}"
            return web.Response(status=206, body=body[3:], headers=headers)
        return web.Response(body=body, headers=headers)

    async with serve(handler) as base:
        path = seed(dl, "https://old.bilivideo.com/source", b"abc")
        meta_path = Path(str(path) + ".part.json")
        meta = json.loads(meta_path.read_text())
        meta["modified"] = "" if changed == "added-modified" else old_modified
        meta_path.write_text(json.dumps(meta))
        await dl.download([base + "/backup"], path, resource="r")
    assert seen == ["bytes=3-", None]
    assert path.read_bytes() == body


async def test_redirect_cannot_reuse_last_modified_identity_from_original_url(dl):
    calls = []
    modified = "Wed, 21 Oct 2015 07:28:00 GMT"

    async def handler(request):
        calls.append((request.path, request.headers.get("Range")))
        if request.path == "/source":
            return web.Response(status=302, headers={"Location": "/different-cdn"})
        if request.headers.get("Range"):
            return web.Response(
                status=206,
                body=b"XYZ",
                headers={"Last-Modified": modified, "Content-Range": "bytes 3-5/6"},
            )
        return web.Response(body=b"UVWXYZ", headers={"Last-Modified": modified})

    async with serve(handler) as base:
        path = seed(dl, base + "/source", b"abc", etag="")
        meta_path = Path(str(path) + ".part.json")
        meta = json.loads(meta_path.read_text())
        meta["modified"] = modified
        meta_path.write_text(json.dumps(meta))
        await dl.download([base + "/source"], path, resource="r")
    assert path.read_bytes() == b"UVWXYZ"
    assert calls == [
        ("/source", "bytes=3-"),
        ("/different-cdn", "bytes=3-"),
        ("/source", None),
        ("/different-cdn", None),
    ]


async def test_timeout_rotates_immediately_and_resumes_matching_representation(dl):
    dl.cfg.download_timeout = 0.05
    calls = []

    async def handler(request):
        calls.append((request.path, request.headers.get("Range")))
        if request.path == "/slow":
            response = web.StreamResponse(
                headers={"Content-Length": "6", "ETag": '"same"'}
            )
            await response.prepare(request)
            await response.write(b"abc")
            await asyncio.sleep(0.15)
            return response
        assert request.headers["If-Range"] == '"same"'
        return web.Response(
            status=206,
            body=b"def",
            headers={"ETag": '"same"', "Content-Range": "bytes 3-5/6"},
        )

    async with serve(handler) as base:
        result = await dl.download(
            [base + "/slow", base + "/fast"], dl.directory / "timeout", resource="r"
        )
    assert result.read_bytes() == b"abcdef"
    assert calls == [("/slow", None), ("/fast", "bytes=3-")]


@pytest.mark.parametrize("status", [403, 404, 500, 503, 567])
async def test_http_candidate_failures_rotate_with_bounded_budget(dl, status):
    calls = []

    async def handler(request):
        calls.append(request.path)
        return (
            web.Response(status=status)
            if request.path == "/bad"
            else web.Response(body=b"complete")
        )

    async with serve(handler) as base:
        path = await dl.download(
            [base + "/bad", base + "/good"], dl.directory / "http", resource="r"
        )
    assert calls == ["/bad", "/good"]
    assert path.read_bytes() == b"complete"


@pytest.mark.parametrize("status", [412, 429])
async def test_rate_limit_stops_without_candidate_or_refresh_storm(dl, status):
    refresh, calls = AsyncMock(), []

    async def handler(request):
        calls.append(request.path)
        return web.Response(status=status)

    async with serve(handler) as base:
        with pytest.raises(DownloadException, match="受限"):
            await dl.download(
                [base + "/primary", base + "/backup"],
                dl.directory / "limited",
                resource="r",
                refresh=refresh,
            )
    assert calls == ["/primary"]
    refresh.assert_not_awaited()


async def test_expiry_is_remembered_when_last_backup_fails_without_auth_status(dl):
    calls, refresh = [], AsyncMock()

    async def handler(request):
        calls.append(request.path)
        status = {"/expired": 403, "/unavailable": 503}.get(request.path)
        return web.Response(status=status) if status else web.Response(body=b"fresh")

    async with serve(handler) as base:
        refresh.return_value = [base + "/fresh"]
        result = await dl.download(
            [base + "/expired", base + "/unavailable"],
            dl.directory / "refresh",
            resource="r",
            refresh=refresh,
        )
    assert result.read_bytes() == b"fresh"
    assert calls == ["/expired", "/unavailable", "/fresh"]
    refresh.assert_awaited_once()


async def test_all_truncated_candidates_fail_with_redacted_bounded_diagnostics(
    dl, monkeypatch
):
    calls, logs = [], []
    monkeypatch.setattr(mod.logger, "warning", logs.append)

    async def handler(request):
        assert (
            "Cookie" not in request.headers and "Authorization" not in request.headers
        )
        calls.append(request.path)
        response = web.StreamResponse(headers={"Content-Length": "6"})
        await response.prepare(request)
        await response.write(b"abc")
        await asyncio.sleep(0.03)
        request.transport.abort()
        return response

    async with serve(handler) as base:
        path = dl.directory / "never-complete.mp4"
        with pytest.raises(DownloadException, match="ClientPayloadError") as exc:
            await dl.download(
                [base + "/primary?sign=SECRET", base + "/backup?token=SECRET"],
                path,
                resource="r",
                headers={"Cookie": "SESSDATA=SECRET", "Authorization": "SECRET"},
            )
    assert calls == ["/primary", "/backup", "/primary"]
    assert not path.exists()
    assert not Path(str(path) + ".complete.json").exists()
    assert len(logs) == dl.cfg.download_retry_times + 1
    assert "SECRET" not in str(logs) + str(exc.value)
    assert all("?" not in entry and "http" not in entry for entry in logs)
