import asyncio
import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import aiohttp
import pytest
from aiohttp import web

from parser_under_test.core.constants import COMMON_HEADER
from parser_under_test.core.resumable import MEDIA_DOMAINS
from parser_under_test.core.parsers.bilibili.streams import (
    BLOCKED_PCDN_DOMAINS,
    LOW_PRIORITY_DOMAINS,
    select_streams,
)
from test_bilibili import play_data
from test_resumable import serve

TOOL = Path(__file__).resolve().parents[1] / "tools" / "bili_speedtest.py"
spec = importlib.util.spec_from_file_location("bili_speedtest", TOOL)
speed = importlib.util.module_from_spec(spec)
spec.loader.exec_module(speed)


def test_standalone_headers_and_candidates_match_plugin():
    assert speed.MEDIA_HEADERS["User-Agent"] == COMMON_HEADER["User-Agent"]
    assert speed.MEDIA_DOMAINS == MEDIA_DOMAINS
    assert speed.BLOCKED_PCDN_DOMAINS == BLOCKED_PCDN_DOMAINS
    assert speed.LOW_PRIORITY_DOMAINS == LOW_PRIORITY_DOMAINS
    data = play_data(112)
    data["dash"]["video"].extend(play_data(80)["dash"]["video"])
    data["dash"]["video"].extend(play_data(116, "av01.0.12")["dash"]["video"])
    selected = select_streams(data, 116, ["AVC"], False, 10)
    info, urls = speed.select_video(data)
    assert info["quality"] == selected.video.info["quality"] == 112
    assert urls == selected.video.urls
    assert all("signature=PRIVATE" in url for url in urls)
    assert speed.select_video(data, quality=80)[0]["quality"] == 80


def test_private_worker_compatibility_in_standalone_process():
    code = (
        "import json, runpy; from bilibili_api.utils import network; "
        f"m=runpy.run_path({str(TOOL)!r}); m['configure_api'](None); "
        "print(json.dumps([network.get_selected_client()[0], "
        "network.HEADERS['User-Agent'], network.request_settings.get_enable_auto_buvid(), "
        "network.request_settings.get_trust_env()]))"
    )
    result = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        timeout=15,
        check=True,
    )
    assert json.loads(result.stdout) == ["aiohttp", "curl/8.14.1", False, False]


def test_filter_pcdn_and_unsafe_candidates():
    data = play_data()
    video = data["dash"]["video"][0]
    video["base_url"] = "https://upos-sz-mirrorcosov.bilivideo.com/a?sign=PRIMARY"
    video["backup_url"] = [
        "http://127.0.0.1/private",
        "https://x.szbdyd.com/a",
        "https://a.akamaized.net/b?sign=BACKUP",
    ]
    assert speed.select_video(data, block_pcdn=True)[1] == [
        video["base_url"],
        video["backup_url"][2],
    ]


@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1",
        "http://169.254.169.254",
        "https://evil.test/a",
        "https://a.bilivideo.com:8000/a",
        "https://user:SECRET@a.bilivideo.com/a",
    ],
)
def test_rejects_untrusted_url(url):
    with pytest.raises(speed.ProbeError, match="unsafe-media-url"):
        speed.validate_media_url(url)


async def test_resolver_rejects_private_dns(monkeypatch):
    monkeypatch.setattr(
        speed.DefaultResolver,
        "resolve",
        AsyncMock(return_value=[{"host": "127.0.0.1"}]),
    )
    resolver = speed.PublicResolver()
    try:
        with pytest.raises(speed.ProbeError, match="non-public-media-address"):
            await resolver.resolve("a.bilivideo.com")
    finally:
        await resolver.close()


@pytest.fixture
def local_media(monkeypatch):
    monkeypatch.setattr(speed, "validate_media_url", lambda url: None)


@pytest.mark.usefixtures("local_media")
@pytest.mark.parametrize("status", [403, 503, 567])
async def test_http_errors_redacted(status, capsys):
    async def handler(request):
        return web.Response(status=status)

    async with serve(handler) as base:
        async with aiohttp.ClientSession() as session:
            result = await speed.sample(session, base + "/video?signature=SECRET")
    assert result["http"] == status
    assert result["result"] == f"HTTP{status}"
    assert result["received_mib"] == 0
    assert "SECRET" not in capsys.readouterr().out


@pytest.mark.usefixtures("local_media")
async def test_size_and_time_caps_are_not_download_success():
    async def handler(request):
        if request.path == "/large":
            return web.Response(body=b"x" * 1024**2)
        response = web.StreamResponse()
        await response.prepare(request)
        await response.write(b"x" * 4096)
        await asyncio.sleep(0.1)
        return response

    async with serve(handler) as base:
        async with aiohttp.ClientSession() as session:
            size = await speed.sample(session, base + "/large", max_bytes=32768)
            timed = await speed.sample(session, base + "/slow", seconds=0.03)
    assert size["result"] == "sample-byte-limit"
    assert size["received_mib"] == round(32768 / 1024**2, 3)
    assert timed["result"] == "sample-time-limit"
    assert timed["received_mib"] > 0


@pytest.mark.usefixtures("local_media")
async def test_payload_disconnect_reports_partial_measurement(capsys):
    async def handler(request):
        response = web.StreamResponse(headers={"Content-Length": "65536"})
        await response.prepare(request)
        await response.write(b"x" * 32768)
        await asyncio.sleep(0.01)
        request.transport.abort()
        return response

    async with serve(handler) as base:
        async with aiohttp.ClientSession() as session:
            result = await speed.sample(session, base + "/video?token=SECRET")
    assert result["result"] == "ClientPayloadError"
    assert result["received_mib"] > 0
    output = capsys.readouterr().out
    assert "SECRET" not in output
    assert "中断：ClientPayloadError" in output
    assert len(output.splitlines()) == 1


@pytest.mark.usefixtures("local_media")
async def test_invalid_206_is_rejected():
    async def handler(request):
        return web.Response(
            status=206, body=b"bad", headers={"Content-Range": "bytes 3-5/6"}
        )

    async with serve(handler) as base:
        async with aiohttp.ClientSession() as session:
            result = await speed.sample(session, base)
    assert result["result"] == "invalid-content-range"


async def test_redirect_target_revalidated(monkeypatch):
    async def handler(request):
        return web.Response(
            status=302, headers={"Location": "http://127.0.0.1/admin?secret=PRIVATE"}
        )

    original = speed.validate_media_url
    async with serve(handler) as base:
        monkeypatch.setattr(
            speed,
            "validate_media_url",
            lambda url: None if url == base else original(url),
        )
        async with aiohttp.ClientSession() as session:
            result = await speed.sample(session, base)
    assert result["result"] == "unsafe-media-url"


@pytest.mark.usefixtures("local_media")
@pytest.mark.parametrize("status", [412, 429, 567])
async def test_stops_on_risk_but_measures_official_backup_after_567(
    monkeypatch, status, capsys
):
    calls = []

    async def handler(request):
        calls.append(request.path)
        return (
            web.Response(status=status)
            if request.path == "/main"
            else web.Response(body=b"x" * 4096)
        )

    async with serve(handler) as base:
        data = play_data()
        data["dash"]["video"][0].update(
            base_url=base + "/main?sign=SECRET",
            backup_url=[base + "/backup?sign=SECRET"],
        )
        monkeypatch.setattr(speed, "fetch_video", AsyncMock(return_value=data))
        monkeypatch.setattr(speed, "close_api", AsyncMock())
        args = SimpleNamespace(
            bvid="BV1MsnG6TEYy",
            page=1,
            credential_file=None,
            quality=116,
            block_pcdn=False,
            max_cdns=4,
            seconds=1,
            mib=1,
        )
        rc = await speed.run(args, None)
    assert calls == (["/main", "/backup"] if status == 567 else ["/main"])
    assert rc == (0 if status == 567 else 2)
    assert "SECRET" not in capsys.readouterr().out


async def test_http_proxy_is_used_without_media_cookies(monkeypatch, capsys):
    calls = []

    async def handler(request):
        calls.append(request.raw_path)
        assert request.headers.get("Cookie") is None
        assert request.headers.get("Authorization") is None
        assert request.headers["Proxy-Authorization"].startswith("Basic ")
        assert request.headers["User-Agent"] == COMMON_HEADER["User-Agent"]
        return web.Response(body=b"x" * 4096)

    monkeypatch.setattr(
        speed.PublicResolver, "resolve", AsyncMock(return_value=[{"host": "1.1.1.1"}])
    )
    async with serve(handler) as base:
        proxy = base.replace("http://", "http://demo:PROXYSECRET@")
        async with aiohttp.ClientSession(
            cookie_jar=aiohttp.DummyCookieJar()
        ) as session:
            result = await speed.sample(
                session, "http://a.bilivideo.com/media?sign=SIGNSECRET", proxy=proxy
            )
    assert result["result"] == "complete"
    assert len(calls) == 1
    output = capsys.readouterr().out
    assert "PROXYSECRET" not in output and "SIGNSECRET" not in output


@pytest.mark.usefixtures("local_media")
async def test_cancellation_propagates_without_finishing():
    ready = asyncio.Event()

    async def handler(request):
        response = web.StreamResponse()
        await response.prepare(request)
        await response.write(b"x")
        ready.set()
        await asyncio.sleep(0.05)
        return response

    async with serve(handler) as base:
        async with aiohttp.ClientSession() as session:
            task = asyncio.create_task(speed.sample(session, base))
            await ready.wait()
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task


def test_invalid_proxy_does_not_leak_password(monkeypatch, capsys):
    monkeypatch.setenv("BILI_TEST_PROXY", "socks5://user:SECRET@localhost:1080")
    assert speed.main([]) == 1
    output = capsys.readouterr().out
    assert "proxy-must-be-http-endpoint" in output and "SECRET" not in output


async def test_explicit_credential_is_read_only(monkeypatch, tmp_path):
    from bilibili_api import video

    path = tmp_path / "credential.json"
    path.write_text(
        json.dumps({"SESSDATA": "SYNTHETIC", "ac_time_value": "NOT_REFRESHED"})
    )
    before = path.read_bytes()
    received = []

    class Video:
        def __init__(self, *, bvid, credential):
            received.append(credential.get_cookies())

        async def get_download_url(self, *, page_index):
            return play_data()

    monkeypatch.setattr(video, "Video", Video)
    monkeypatch.setattr(speed, "configure_api", lambda proxy: None)
    await speed.fetch_video("BV1MsnG6TEYy", 1, None, path)
    assert received[0]["SESSDATA"] == "SYNTHETIC"
    assert path.read_bytes() == before


@pytest.mark.parametrize(
    "phase,exit_code", [("probe", 0), ("probe", 1), ("probe", 130), ("install", 1)]
)
def test_launcher_cleans_environment_and_installer_scratch(tmp_path, phase, exit_code):
    """Reproduce installers leaving files in TMPDIR, also on failure/cancel exit."""
    bin_dir = tmp_path / "bin"
    scratch = tmp_path / "scratch"
    bin_dir.mkdir()
    scratch.mkdir()
    sentinel = tmp_path / "existing-config"
    sentinel.write_text("preserve")
    curl = bin_dir / "curl"
    curl.write_text(
        f"#!{sys.executable}\nimport sys\nfrom pathlib import Path\n"
        "Path(sys.argv[sys.argv.index('-o')+1]).write_text('mock probe')\n"
    )
    curl.chmod(0o755)
    python = bin_dir / "python3"
    python.write_text(
        f"#!{sys.executable}\nimport os, sys, shutil\nfrom pathlib import Path\n"
        "args=sys.argv[1:]\n"
        "if args[0]=='-c': sys.exit(0)\n"
        "if args[:2]==['-m','venv']:\n"
        "    print('VENV_NOISE', file=sys.stderr)\n"
        "    dest=Path(args[2])/'bin'/'python'; dest.parent.mkdir(parents=True)\n"
        "    shutil.copyfile(sys.argv[0], dest); dest.chmod(0o755); sys.exit(0)\n"
        "if args[:2]==['-m','pip']:\n"
        "    print('DEPRECATION_INSTALL_NOISE', file=sys.stderr)\n"
        "    (Path(os.environ['TMPDIR'])/'installer-leftovers').mkdir()\n"
        f"    sys.exit({exit_code if phase == 'install' else 0})\n"
        "assert args[-2:]==['--seconds', '15']\n"
        f"sys.exit({exit_code})\n"
    )
    python.chmod(0o755)
    env = {
        **os.environ,
        "PATH": str(bin_dir) + os.pathsep + os.environ["PATH"],
        "TMPDIR": str(scratch),
        "BILI_SPEEDTEST_REF": "main",
    }
    result = subprocess.run(
        ["bash", "-s", "--", "--seconds", "15"],
        input=TOOL.with_suffix(".sh").read_text(),
        env=env,
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert result.returncode == exit_code, result.stdout + result.stderr
    assert not list(scratch.iterdir())
    assert sentinel.read_text() == "preserve"
    assert result.stdout == ""
    if phase == "install":
        assert "测速依赖安装失败" in result.stderr
        assert "INSTALL_NOISE" not in result.stderr
    else:
        assert result.stderr == ""


def test_quiet_result_only_uses_effective_speed(capsys):
    speed.emit("settings", bvid="BV1Ke411W7YG")
    speed.emit("cdn_start", cdn="a.bilivideo.com")
    speed.emit("progress", received_mib=1)
    speed.emit(
        "cdn_result",
        cdn="a.bilivideo.com",
        result="sample-byte-limit",
        received_bytes=256 * 1024**2,
        effective_mib_s=3.251,
        speed_mib_s=5.22,
        small_source=False,
    )
    speed.emit("cdn_result", cdn="b.bilivideo.com", result="HTTP567", received_bytes=0)
    speed.emit("finished", note="done")
    assert capsys.readouterr().out.splitlines() == [
        "a.bilivideo.com: 3.25 MiB/s",
        "b.bilivideo.com: 不可用（HTTP567）",
    ]


def test_large_default_and_json_opt_in_restore_output_mode(monkeypatch, capsys):
    calls = []

    async def run(args, proxy):
        calls.append(args)
        speed.emit("settings", bvid=args.bvid)
        speed.emit(
            "cdn_result",
            cdn="a.bilivideo.com",
            result="sample-time-limit",
            received_bytes=1024**2,
            effective_mib_s=0.1,
            small_source=False,
        )
        return 0

    monkeypatch.setattr(speed, "run", run)
    monkeypatch.delenv("BILI_TEST_PROXY", raising=False)
    assert speed.main(["--json"]) == 0
    records = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert [r["event"] for r in records] == ["settings", "cdn_result"]
    assert calls[0].bvid == "BV1Ke411W7YG"
    assert calls[0].seconds == 120 and calls[0].mib == 256
    assert not speed.JSON_OUTPUT.get()
    assert speed.main([]) == 0
    assert capsys.readouterr().out == "a.bilivideo.com: 0.10 MiB/s\n"


@pytest.mark.usefixtures("local_media")
async def test_256mib_is_one_continuous_large_response(capsys):
    calls = []
    block = b"x" * 1024**2

    async def handler(request):
        calls.append(request.path)
        assert "Range" not in request.headers
        response = web.StreamResponse(headers={"Content-Length": str(256 * len(block))})
        await response.prepare(request)
        for _ in range(256):
            await response.write(block)
        return response

    async with serve(handler) as base:
        async with aiohttp.ClientSession() as session:
            result = await speed.sample(session, base + "/large?signature=PRIVATE")
    assert calls == ["/large"]
    assert result["received_bytes"] == 256 * 1024**2
    assert result["result"] == "sample-byte-limit"
    assert not result["small_source"]
    output = capsys.readouterr().out
    assert len(output.splitlines()) == 1 and "MiB/s" in output
    assert "PRIVATE" not in output and '"event"' not in output


@pytest.mark.usefixtures("local_media")
@pytest.mark.parametrize("length_known", [True, False])
async def test_small_source_is_not_repeated_or_presented_as_large(length_known, capsys):
    calls = []
    block = b"x" * 1024**2

    async def handler(request):
        calls.append(request.path)
        headers = {"Content-Length": str(13 * len(block))} if length_known else {}
        response = web.StreamResponse(headers=headers)
        await response.prepare(request)
        for _ in range(13):
            await response.write(block)
        return response

    async with serve(handler) as base:
        async with aiohttp.ClientSession() as session:
            result = await speed.sample(session, base)
    assert len(calls) == 1
    assert result["small_source"]
    assert result["received_bytes"] == 13 * 1024**2
    output = capsys.readouterr().out
    assert len(output.splitlines()) == 1 and "小文件样本：13.0 MiB" in output
