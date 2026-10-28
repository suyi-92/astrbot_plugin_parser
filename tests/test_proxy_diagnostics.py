import asyncio
import base64
from types import SimpleNamespace

import aiohttp
import pytest
from aiohttp import web
from aiohttp.resolver import DefaultResolver
from bilibili_api import Credential

from parser_under_test.core import diagnostics as diag, resumable
from parser_under_test.core.parsers.bilibili.api import BilibiliAPI
from test_resumable import serve
from test_vless_proxy import NODE_ID, node


@pytest.fixture
def messages(monkeypatch):
    captured = []
    monkeypatch.setattr(diag.logger, "debug", captured.append)
    return captured


@pytest.mark.parametrize(
    "enabled,configured,actual,route",
    [
        (False, "", None, "direct"),
        (True, "", None, "direct"),
        (False, node(), None, "direct"),
        (True, node(), None, "direct"),
        (True, "http://SECRET:PASS@secret.invalid:1234", None, "direct"),
        (True, node(), "http://SECRET:PASS@secret.invalid:1234", "http-proxy"),
        (True, node(), node(), "other-proxy"),
    ],
)
def test_log_reports_actual_argument_not_just_config(
    cfg, enabled, configured, actual, route
):
    cfg.parser.bilibili.use_proxy = enabled
    cfg.proxy = configured
    output = diag.proxy_route(cfg, actual)
    assert f"route={route} " in output
    assert f"use_proxy={'on' if enabled else 'off'}" in output
    for secret in ("SECRET", "PASS", "secret.invalid", NODE_ID, "node.example", "://"):
        assert secret not in output


@pytest.mark.parametrize("returncode,expected", [(None, "running"), (23, "stopped")])
def test_owned_bridge_route_requires_endpoint_match(cfg, returncode, expected):
    cfg.proxy = node()
    cfg.parser.bilibili.use_proxy = True
    endpoint = "http://parser:PRIVATE@127.0.0.1:12345"
    cfg._proxy_manager = SimpleNamespace(
        _endpoint=endpoint, process=SimpleNamespace(returncode=returncode)
    )
    output = diag.proxy_route(cfg, endpoint)
    assert "route=vless+xray" in output and f"xray={expected}" in output
    assert "PRIVATE" not in output and "12345" not in output
    assert "route=http-proxy" in diag.proxy_route(cfg, "http://127.0.0.1:23456")


@pytest.mark.parametrize(
    "peer,proxy,expected",
    [
        (
            None,
            "http://127.0.0.1:12345",
            "socket_peer=unavailable proxy_socket=unknown",
        ),
        (
            ("127.0.0.1", 12345),
            "http://SECRET:PASS@127.0.0.1:12345",
            "socket_peer=loopback proxy_socket=confirmed",
        ),
        (
            ("127.0.0.1", 23456),
            "http://127.0.0.1:12345",
            "socket_peer=loopback proxy_socket=mismatch",
        ),
        (
            ("::1", 12345, 0, 0),
            "http://[::1]:12345",
            "socket_peer=loopback proxy_socket=confirmed",
        ),
        (
            ("1.1.1.1", 443),
            "https://SECRET:PASS@1.1.1.1",
            "socket_peer=public proxy_socket=confirmed",
        ),
        (
            ("1.1.1.1", 12345),
            "http://SECRET:PASS@secret.invalid:12345",
            "socket_peer=public proxy_socket=unknown",
        ),
        (("192.168.1.2", 443), None, "socket_peer=private proxy_socket=not_used"),
        (("127.0.0.1", 12345), None, "socket_peer=loopback proxy_socket=not_used"),
        (
            ("127.0.0.1", 12345),
            "http://SECRET:PASS@localhost:bad",
            "socket_peer=loopback proxy_socket=unknown",
        ),
    ],
)
def test_socket_evidence_is_redacted_and_does_not_infer_missing_peer(
    peer, proxy, expected
):
    connection = (
        SimpleNamespace(transport=SimpleNamespace(get_extra_info=lambda name: peer))
        if peer
        else None
    )
    assert diag.response_peer(SimpleNamespace(connection=connection), proxy) == expected


@pytest.mark.parametrize("enabled", [False, True])
async def test_real_worker_acknowledges_applied_proxy_without_secrets(
    cfg, messages, enabled
):
    cfg.debug_media_info = True
    cfg.proxy = node()
    cfg.parser.bilibili.use_proxy = enabled
    endpoint = "http://parser:PRIVATE@127.0.0.1:12345"
    cfg._proxy_manager = SimpleNamespace(
        endpoint=endpoint, _endpoint=endpoint, process=SimpleNamespace(returncode=None)
    )
    api = BilibiliAPI(cfg)
    try:
        # Offline action runs a real worker and configures the real library, but
        # deliberately makes no network call to this synthetic proxy endpoint.
        await api.call("settings", credential=Credential(sessdata="SESSION-SECRET"))
    finally:
        await api.close()
    output = "\n".join(messages)
    assert f"worker_proxy={'on' if enabled else 'off'} result=ok" in output
    assert f"route={'vless+xray' if enabled else 'direct'}" in output
    for secret in ("PRIVATE", "SESSION-SECRET", NODE_ID, "127.0.0.1", "node.example"):
        assert secret not in output


async def test_disabled_diagnostics_do_not_log_worker_routes(cfg, messages):
    cfg.debug_media_info = False
    api = BilibiliAPI(cfg)
    try:
        await api.call("settings")
    finally:
        await api.close()
    assert not messages


@pytest.mark.parametrize("stream", ["video", "audio"])
async def test_real_http_proxy_download_logs_actual_peer(
    cfg, messages, monkeypatch, stream
):
    cfg.debug_media_info = True
    cfg.parser.bilibili.use_proxy = True
    body = bytes(range(256)) * 8192
    proxy_calls, target_calls = [], []
    monkeypatch.setattr(resumable, "validate_media_url", lambda url: None)
    # Local fixtures only; production still rejects internal media destinations.
    monkeypatch.setattr(resumable.PublicResolver, "resolve", DefaultResolver.resolve)

    async def origin(request):
        target_calls.append(request.path)
        assert "Proxy-Authorization" not in request.headers
        return web.Response(body=body)

    async with aiohttp.ClientSession(trust_env=False) as forwarding:

        async def proxy(request):
            proxy_calls.append(request.raw_path)
            assert (
                request.headers["Proxy-Authorization"]
                == "Basic " + base64.b64encode(b"SECRET:PASS").decode()
            )
            async with forwarding.get(request.raw_path) as upstream:
                response = web.StreamResponse(
                    headers={"Content-Length": str(len(body))}
                )
                await response.prepare(request)
                async for chunk in upstream.content.iter_chunked(32768):
                    await response.write(chunk)
                await response.write_eof()
                return response

        downloader = resumable.ResumableDownloader(cfg)
        try:
            async with serve(origin) as base, serve(proxy) as gateway:
                cfg.proxy = gateway.replace("://", "://SECRET:PASS@")
                url = base + "/media?signature=SIGNATURE-SECRET"
                path = await downloader.download(
                    [url],
                    downloader.directory / stream,
                    resource=f"r:{stream}",
                    proxy=cfg.proxy,
                )
                assert path.read_bytes() == body
                assert proxy_calls == [url] and target_calls == ["/media"]
        finally:
            await downloader.close()
    output = "\n".join(messages)
    response_log = next(line for line in messages if "CDN响应" in line)
    assert f"stream={stream}" in response_log
    assert (
        "route=http-proxy" in response_log and "proxy_socket=confirmed" in response_log
    )
    assert "received_bytes=2097152" in output and "avg_mib_s=" in output
    for secret in ("SECRET", "PASS", "signature", "http://"):
        assert secret not in output


async def test_missing_worker_ack_is_unknown_not_confirmed(cfg, messages):
    cfg.debug_media_info = True
    reader = asyncio.StreamReader()
    reader.feed_data(b'{"data": true}\n')

    class Writer:
        def write(self, value):
            pass

        async def drain(self):
            pass

    api = BilibiliAPI(cfg)
    api.process = SimpleNamespace(returncode=None, stdin=Writer(), stdout=reader)
    try:
        assert await api.call("settings") is True
        assert "worker_proxy=unknown" in messages[-1]
    finally:
        api.process = None
        await api.close()
