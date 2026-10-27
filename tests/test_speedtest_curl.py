import asyncio
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock
from urllib.parse import urlsplit

import aiohttp
import pytest
from aiohttp import web

from test_bilibili import play_data
from test_resumable import serve
from test_speedtest import speed

VALIDATE_MEDIA_URL = speed.validate_media_url


@pytest.fixture
def local_curl(monkeypatch):
    monkeypatch.setattr(speed, "validate_media_url", lambda url: None)

    async def resolve(url):
        parsed = urlsplit(url)
        return f"{parsed.hostname}:{parsed.port}:127.0.0.1"

    monkeypatch.setattr(speed, "curl_resolve", resolve)


@pytest.mark.usefixtures("local_curl")
@pytest.mark.parametrize("client", ["aiohttp", "curl"])
async def test_rate_matches_independent_byte_count_and_clock(client):
    block = b"x" * 65536

    async def handler(request):
        response = web.StreamResponse(headers={"Content-Length": str(16 * len(block))})
        await response.prepare(request)
        for _ in range(16):
            await asyncio.sleep(0.01)
            await response.write(block)
        return response

    async with serve(handler) as base:
        async with aiohttp.ClientSession() as session:
            start = time.perf_counter()
            report = await speed.sample(session, base, client=client)
            elapsed = time.perf_counter() - start
    assert report["received_bytes"] == 16 * len(block)
    assert report["result"] == "complete"
    assert report["effective_mib_s"] == pytest.approx(1 / elapsed, rel=0.05)


@pytest.mark.usefixtures("local_curl")
@pytest.mark.parametrize("status", [200, 567])
async def test_curl_ignores_environment_and_curlrc_keeps_url_private(
    status, monkeypatch, tmp_path, capsys
):
    (tmp_path / ".curlrc").write_text('limit-rate = "1"\nheader = "Cookie: LEAK"\n')
    monkeypatch.setenv("CURL_HOME", str(tmp_path))
    monkeypatch.setenv("ALL_PROXY", "http://127.0.0.1:1")
    monkeypatch.setenv("http_proxy", "http://127.0.0.1:1")
    calls = []
    create = speed.asyncio.create_subprocess_exec

    async def capture(*args, **kwargs):
        assert "SECRET" not in repr(args)
        calls.append(args)
        return await create(*args, **kwargs)

    monkeypatch.setattr(speed.asyncio, "create_subprocess_exec", capture)

    async def handler(request):
        assert request.raw_path == "/media?signature=SECRET&v=1"
        assert request.headers["User-Agent"] == speed.MEDIA_HEADERS["User-Agent"]
        assert request.headers["Referer"] == speed.MEDIA_HEADERS["Referer"]
        assert "Cookie" not in request.headers
        assert "Authorization" not in request.headers
        assert "Range" not in request.headers
        return web.Response(status=status, body=b"x" * 65536)

    async with serve(handler) as base:
        result = await speed.sample(
            None, base + "/media?signature=SECRET&v=1", client="curl", seconds=2
        )
    assert len(calls) == 1
    assert result["http"] == status
    assert result["result"] == ("complete" if status == 200 else "HTTP567")
    assert "SECRET" not in capsys.readouterr().out


@pytest.mark.usefixtures("local_curl")
@pytest.mark.parametrize("mode", ["disconnect", "byte-limit", "time-limit"])
async def test_curl_incomplete_and_bounded_samples_reap_process(mode, monkeypatch):
    processes = []
    create = speed.asyncio.create_subprocess_exec

    async def capture(*args, **kwargs):
        process = await create(*args, **kwargs)
        processes.append(process)
        return process

    monkeypatch.setattr(speed.asyncio, "create_subprocess_exec", capture)

    async def handler(request):
        response = web.StreamResponse(headers={"Content-Length": str(4 * 1024**2)})
        await response.prepare(request)
        await response.write(b"x" * 65536)
        await asyncio.sleep(0.05)
        if mode == "disconnect":
            request.transport.abort()
        elif mode == "time-limit":
            await asyncio.sleep(0.1)
        return response

    async with serve(handler) as base:
        report = await speed.sample(
            None,
            base,
            client="curl",
            max_bytes=32768 if mode == "byte-limit" else 1024**2,
            seconds=0.08 if mode == "time-limit" else 2,
        )
    assert (
        report["result"]
        == {
            "disconnect": "curl-exit-18",
            "byte-limit": "sample-byte-limit",
            "time-limit": "sample-time-limit",
        }[mode]
    )
    assert report["received_bytes"] > 0
    assert processes and all(p.returncode is not None for p in processes)


@pytest.mark.usefixtures("local_curl")
async def test_curl_cancellation_reaps_process(monkeypatch):
    connected, release = asyncio.Event(), asyncio.Event()
    processes = []
    create = speed.asyncio.create_subprocess_exec

    async def capture(*args, **kwargs):
        process = await create(*args, **kwargs)
        processes.append(process)
        return process

    monkeypatch.setattr(speed.asyncio, "create_subprocess_exec", capture)

    async def handler(request):
        response = web.StreamResponse(headers={"Content-Length": "1048576"})
        await response.prepare(request)
        await response.write(b"x" * 4096)
        connected.set()
        await release.wait()
        return response

    async with serve(handler) as base:
        task = asyncio.create_task(speed.sample(None, base, client="curl"))
        try:
            await asyncio.wait_for(connected.wait(), 3)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(task, 3)
        finally:
            release.set()
    assert processes and all(p.returncode is not None for p in processes)


@pytest.mark.usefixtures("local_curl")
async def test_curl_revalidates_redirect(monkeypatch, capsys):
    calls = []

    async def handler(request):
        calls.append(request.path)
        return web.Response(
            status=302, headers={"Location": "http://127.0.0.1/admin?SECRET"}
        )

    async with serve(handler) as base:
        monkeypatch.setattr(
            speed,
            "validate_media_url",
            lambda url: None if url == base else VALIDATE_MEDIA_URL(url),
        )
        report = await speed.sample(None, base, client="curl")
    assert calls == ["/"]
    assert report["result"] == "unsafe-media-url"
    assert "SECRET" not in capsys.readouterr().out


async def test_curl_dns_validation_and_config_injection(monkeypatch):
    monkeypatch.setattr(
        speed.DefaultResolver,
        "resolve",
        AsyncMock(return_value=[{"host": "127.0.0.1"}]),
    )
    with pytest.raises(speed.ProbeError, match="non-public-media-address"):
        await speed.curl_resolve("https://a.bilivideo.com/v")
    with pytest.raises(speed.ProbeError, match="unsafe-curl-config"):
        speed.curl_config_value('http://host/\nheader = "Cookie: SECRET"')
    assert speed.curl_config_value('a"b\\c') == '"a\\"b\\\\c"'


async def test_curl_explicit_http_proxy_is_used_and_redacted(monkeypatch, capsys):
    calls = []

    async def handler(request):
        calls.append(request.raw_path)
        assert request.headers["Proxy-Authorization"].startswith("Basic ")
        assert "Cookie" not in request.headers
        assert "Authorization" not in request.headers
        return web.Response(body=b"x" * 4096)

    monkeypatch.setattr(
        speed.PublicResolver, "resolve", AsyncMock(return_value=[{"host": "1.1.1.1"}])
    )
    async with serve(handler) as base:
        proxy = base.replace("http://", "http://demo:PROXYSECRET@")
        report = await speed.sample(
            None,
            "http://a.bilivideo.com/media?sign=SIGNSECRET",
            proxy=proxy,
            client="curl",
        )
    assert calls == ["http://a.bilivideo.com/media?sign=SIGNSECRET"]
    assert report["result"] == "complete"
    output = capsys.readouterr().out
    assert "PROXYSECRET" not in output and "SIGNSECRET" not in output


@pytest.mark.usefixtures("local_curl")
async def test_diagnose_uses_two_sources_and_exact_url_in_both_clients(
    monkeypatch, capsys
):
    calls = []

    async def handler(request):
        calls.append(request.raw_path)
        return web.Response(body=b"x" * 65536)

    async with serve(handler) as base:
        data = play_data()
        data["dash"]["video"][0].update(base_url=base + "/v?sign=SECRET", backup_url=[])
        fetch = AsyncMock(return_value=data)
        close = AsyncMock()
        monkeypatch.setattr(speed, "fetch_video", fetch)
        monkeypatch.setattr(speed, "close_api", close)
        args = SimpleNamespace(
            bvid=speed.DEFAULT_BVID,
            page=2,
            credential_file=None,
            quality=116,
            block_pcdn=False,
            max_cdns=1,
            seconds=2,
            mib=1,
            diagnose=True,
        )
        assert await speed.run(args, None) == 0
    assert calls == ["/v?sign=SECRET"] * 4
    assert [c.args[:2] for c in fetch.await_args_list] == [
        (speed.CONTROL_BVID, 1),
        (speed.DEFAULT_BVID, 2),
    ]
    close.assert_awaited_once()
    lines = capsys.readouterr().out.splitlines()
    assert len(lines) == 6
    assert sum("[aiohttp]" in line for line in lines) == 2
    assert sum("[curl]" in line for line in lines) == 2
    assert all("SECRET" not in line for line in lines)
    assert speed.OUTPUT_CONTEXT.get() is None


@pytest.mark.usefixtures("local_curl")
@pytest.mark.parametrize("risk_position", [1, 2])
async def test_diagnose_stops_every_client_and_source_on_risk(
    risk_position, monkeypatch
):
    calls = []

    async def handler(request):
        calls.append(request.path)
        return web.Response(
            status=412 if len(calls) == risk_position else 200, body=b"x"
        )

    async with serve(handler) as base:
        data = play_data()
        data["dash"]["video"][0].update(base_url=base, backup_url=[])
        fetch = AsyncMock(return_value=data)
        monkeypatch.setattr(speed, "fetch_video", fetch)
        monkeypatch.setattr(speed, "close_api", AsyncMock())
        args = SimpleNamespace(
            bvid=speed.DEFAULT_BVID,
            page=1,
            credential_file=None,
            quality=116,
            block_pcdn=False,
            max_cdns=1,
            seconds=2,
            mib=1,
            diagnose=True,
        )
        assert await speed.run(args, None) == 2
    assert len(calls) == risk_position
    fetch.assert_awaited_once()
