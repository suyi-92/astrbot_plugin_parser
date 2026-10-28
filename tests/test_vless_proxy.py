import asyncio
import hashlib
import io
import json
import stat
import sys
import zipfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock
from urllib.parse import urlencode

import pytest
from aiohttp import web

from parser_under_test.core import vless_proxy as mod
from parser_under_test.core.parsers.base import BaseParser
from parser_under_test.core.parsers.bilibili.api import BilibiliAPI
from test_resumable import serve

NODE_ID = "00000000-0000-4000-8000-000000000001"


def node(**params):
    values = {"type": "tcp", "security": "tls", "encryption": "none", **params}
    return f"vless://{NODE_ID}@node.example:443?{urlencode(values)}#PRIVATE-NAME"


@pytest.mark.parametrize(
    "transport", ["tcp", "raw", "ws", "grpc", "xhttp", "httpupgrade"]
)
def test_common_links_map_to_one_vless_outbound(transport):
    outbound = mod.parse_vless(node(type=transport, sni="sni.example"))
    assert outbound["protocol"] == "vless"
    user = outbound["settings"]["vnext"][0]
    assert user["address"] == "node.example" and user["users"][0]["id"] == NODE_ID
    assert outbound["streamSettings"]["tlsSettings"]["allowInsecure"] is False
    assert outbound["streamSettings"]["tlsSettings"]["serverName"] == "sni.example"
    config = mod.bridge_config(outbound, 12345, "SYNTHETIC-LOCAL-PASS")
    assert config["inbounds"][0]["listen"] == "127.0.0.1"
    assert config["inbounds"][0]["settings"]["accounts"]
    assert len(config["outbounds"]) == 1
    assert config["outbounds"][0]["protocol"] != "freedom"
    assert "PRIVATE-NAME" not in json.dumps(config)


def test_reality_and_encoded_transport_values():
    outbound = mod.parse_vless(
        node(
            security="reality",
            pbk="A" * 43,
            sid="0123",
            sni="sni.example",
            flow="xtls-rprx-vision",
            fp="chrome",
            spx="/path?a=b",
        )
    )
    reality = outbound["streamSettings"]["realitySettings"]
    assert reality["publicKey"] == "A" * 43
    assert reality["shortId"] == "0123" and reality["spiderX"] == "/path?a=b"
    ws = mod.parse_vless(node(type="ws", path="/ws?ed=2048", host="ws.example"))
    assert ws["streamSettings"]["wsSettings"] == {
        "path": "/ws?ed=2048",
        "host": "ws.example",
    }
    grpc = mod.parse_vless(node(type="grpc", serviceName="my/service", mode="multi"))
    assert grpc["streamSettings"]["grpcSettings"]["multiMode"] is True
    assert grpc["streamSettings"]["grpcSettings"]["serviceName"] == "my/service"
    ipv6 = mod.parse_vless(f"vless://{NODE_ID}@[::1]:12345?security=none")
    assert ipv6["settings"]["vnext"][0]["address"] == "::1"


@pytest.mark.parametrize(
    "link",
    [
        "vless://PRIVATE",
        node(type="kcp"),
        node(extra="SECRET"),
        node(security="reality", pbk="SECRET", sni="sni.example"),
        node(security="reality", pbk="A" * 43, sni="sni.example", sid="abc"),
        node(allowInsecure="1"),
        node(security="none"),
        node(type="ws", flow="xtls-rprx-vision"),
        node(sni="a", serverName="b"),
        node(path="/x\r\nCookie: SECRET"),
        node().replace("type=tcp", "type=tcp&type=ws"),
    ],
)
def test_unsupported_and_malformed_links_fail_without_secrets(link):
    with pytest.raises(mod.ProxyError) as error:
        mod.parse_vless(link)
    assert "SECRET" not in str(error.value)
    assert NODE_ID not in str(error.value)
    assert "node.example" not in str(error.value)


async def test_http_and_disabled_parsers_need_no_core(cfg, monkeypatch):
    find = AsyncMock(side_effect=AssertionError("must not install"))
    monkeypatch.setattr(mod, "find_xray", find)
    cfg.proxy = "http://127.0.0.1:7890"
    manager = mod.VlessProxy(cfg)
    await manager.start()
    assert mod.get_proxy(cfg) == cfg.proxy
    cfg.proxy = node()
    await manager.start()  # use_proxy remains false
    find.assert_not_awaited()
    parser = BaseParser(cfg, None)
    monkeypatch.setattr(
        BaseParser, "platform", SimpleNamespace(name="bilibili"), raising=False
    )
    assert parser.proxy is None
    await manager.close()


async def test_missing_core_blocks_proxy_requests_and_never_forwards_uri(
    cfg, monkeypatch
):
    cfg.proxy = node()
    cfg.parser.bilibili.use_proxy = True
    manager = mod.VlessProxy(cfg)
    monkeypatch.setattr(
        mod, "find_xray", AsyncMock(side_effect=mod.ProxyError("未找到 Xray"))
    )
    with pytest.raises(mod.ProxyError, match="未找到 Xray"):
        await manager.start()
    api = BilibiliAPI(cfg)
    try:
        with pytest.raises(mod.ProxyError, match="未找到 Xray"):
            await api.call("settings")
        assert api.process is None
        parser = BaseParser(cfg, None)
        monkeypatch.setattr(
            BaseParser, "platform", SimpleNamespace(name="bilibili"), raising=False
        )
        with pytest.raises(mod.ProxyError):
            _ = parser.proxy
    finally:
        await api.close()
        await manager.close()


def fake_core(tmp_path, *, hang=False):
    path = tmp_path / "fake-xray"
    path.write_text(
        f"#!{sys.executable}\nimport json, sys, time\n"
        "data=json.load(sys.stdin)\n"
        "print('PRIVATE CORE OUTPUT ' + repr(data), file=sys.stderr, flush=True)\n"
        + ("time.sleep(60)\n" if hang else "sys.exit(23)\n")
    )
    path.chmod(0o700)
    return str(path)


async def test_core_failure_and_cancel_are_redacted_and_reaped(
    cfg, tmp_path, monkeypatch, capsys
):
    cfg.proxy, cfg.proxy_xray_path = node(), fake_core(tmp_path)
    cfg.parser.bilibili.use_proxy = True
    manager = mod.VlessProxy(cfg)
    with pytest.raises(mod.ProxyError):
        await manager.start()
    assert manager.process is None
    assert "PRIVATE CORE OUTPUT" not in capsys.readouterr().err
    await manager.close()

    cfg.proxy_xray_path = fake_core(tmp_path, hang=True)
    manager = mod.VlessProxy(cfg)
    pending = asyncio.create_task(manager.start())
    for _ in range(100):
        if manager.process:
            break
        await asyncio.sleep(0.01)
    process = manager.process
    assert process is not None
    await manager.close()
    assert pending.cancelled() and process.returncode is not None
    assert manager.process is None
    await manager.close()  # idempotent


def archive_bytes():
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w") as zipped:
        zipped.writestr("xray", b"SYNTHETIC EXECUTABLE")
        zipped.writestr("../../do-not-extract", b"not extracted")
    return stream.getvalue()


async def test_installer_checksum_atomic_files_and_concurrent_calls(cfg, monkeypatch):
    body = archive_bytes()
    calls = []

    async def handler(request):
        calls.append(request.path)
        return web.Response(body=body)

    original_get = mod.aiohttp.ClientSession.get
    async with serve(handler) as base:
        # Only the test fixture substitutes a local URL; production uses a fixed
        # official release URL and a compiled-in checksum.
        monkeypatch.setattr(
            mod.aiohttp.ClientSession,
            "get",
            lambda self, url, **kwargs: original_get(self, base, **kwargs),
        )
        monkeypatch.setattr(mod.shutil, "which", lambda _: None)
        monkeypatch.setattr(mod.platform, "system", lambda: "Linux")
        monkeypatch.setattr(mod.platform, "machine", lambda: "x86_64")
        monkeypatch.setitem(
            mod.XRAY_ASSETS, "x86_64", ("fixture.zip", hashlib.sha256(body).hexdigest())
        )
        cfg.proxy_xray_auto_download = True
        paths = await asyncio.gather(mod.find_xray(cfg), mod.find_xray(cfg))
    assert paths[0] == paths[1] and len(calls) == 1
    target = Path(paths[0])
    assert target.read_bytes() == b"SYNTHETIC EXECUTABLE"
    assert stat.S_IMODE(target.stat().st_mode) == 0o700
    assert not list(target.parent.glob("*.part"))
    assert not (cfg.data_dir / "do-not-extract").exists()


async def test_installer_rejects_wrong_digest_and_cleans_parts(cfg, monkeypatch):
    body = archive_bytes()

    async def handler(request):
        return web.Response(body=body)

    original_get = mod.aiohttp.ClientSession.get
    async with serve(handler) as base:
        monkeypatch.setattr(
            mod.aiohttp.ClientSession,
            "get",
            lambda self, url, **kwargs: original_get(self, base, **kwargs),
        )
        target = cfg.data_dir / "proxy_bin" / "xray"
        with pytest.raises(mod.ProxyError, match="SHA256"):
            await mod.download_xray(target, "fixture.zip", "0" * 64)
    assert not target.exists()
    assert list(target.parent.iterdir()) == []


async def test_installer_needs_explicit_opt_in(cfg, monkeypatch):
    monkeypatch.setattr(mod.shutil, "which", lambda _: None)
    monkeypatch.setattr(mod.platform, "system", lambda: "Linux")
    monkeypatch.setattr(mod.platform, "machine", lambda: "x86_64")
    download = AsyncMock(side_effect=AssertionError("download not authorized"))
    monkeypatch.setattr(mod, "download_xray", download)
    with pytest.raises(mod.ProxyError, match="允许自动下载"):
        await mod.find_xray(cfg)
    download.assert_not_awaited()


@pytest.mark.parametrize("with_length", [True, False])
async def test_installer_rejects_oversize_and_cleans_parts(
    cfg, monkeypatch, with_length
):
    async def handler(request):
        if with_length:
            return web.Response(body=b"x" * 65)
        response = web.StreamResponse()
        await response.prepare(request)
        await response.write(b"x" * 65)
        await response.write_eof()
        return response

    original_get = mod.aiohttp.ClientSession.get
    monkeypatch.setattr(mod, "MAX_ARCHIVE", 64)
    async with serve(handler) as base:
        monkeypatch.setattr(
            mod.aiohttp.ClientSession,
            "get",
            lambda self, url, **kwargs: original_get(self, base, **kwargs),
        )
        target = cfg.data_dir / "proxy_bin" / "xray"
        with pytest.raises(mod.ProxyError, match="体积上限"):
            await mod.download_xray(target, "fixture.zip", "0" * 64)
    assert not target.exists()
    assert list(target.parent.iterdir()) == []


async def test_installer_cancel_removes_partial_and_does_not_publish(cfg, monkeypatch):
    sent, release = asyncio.Event(), asyncio.Event()

    async def handler(request):
        response = web.StreamResponse()
        await response.prepare(request)
        await response.write(b"partial archive")
        sent.set()
        await release.wait()
        return response

    original_get = mod.aiohttp.ClientSession.get
    async with serve(handler) as base:
        monkeypatch.setattr(
            mod.aiohttp.ClientSession,
            "get",
            lambda self, url, **kwargs: original_get(self, base, **kwargs),
        )
        target = cfg.data_dir / "proxy_bin" / "xray"
        task = asyncio.create_task(mod.download_xray(target, "fixture.zip", "0" * 64))
        try:
            await asyncio.wait_for(sent.wait(), 3)
            for _ in range(100):
                if list(target.parent.glob("*.part")):
                    break
                await asyncio.sleep(0.01)
            assert list(target.parent.glob("*.part"))
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        finally:
            release.set()
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
    assert not target.exists()
    assert list(target.parent.iterdir()) == []


@pytest.mark.parametrize("enabled", [True, False])
async def test_qr_request_uses_bridge_only_when_enabled(cfg, monkeypatch, enabled):
    from parser_under_test.core import diagnostics as diag
    from parser_under_test.core.parsers.bilibili import login

    cfg.debug_media_info = True
    logs = []
    monkeypatch.setattr(diag.logger, "debug", logs.append)
    cfg.proxy = node()
    cfg.parser.bilibili.use_proxy = enabled
    endpoint = "http://parser:synthetic@127.0.0.1:12345"
    cfg._proxy_manager = SimpleNamespace(
        endpoint=endpoint, _endpoint=endpoint, process=SimpleNamespace(returncode=None)
    )
    calls = []

    class Session:
        def __init__(self, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        def get(self, url, **kwargs):
            calls.append(kwargs)
            return self

        status = 200
        headers = SimpleNamespace(getall=lambda *args: [])

        async def json(self):
            return {"code": 0, "data": {"ok": True}}

    monkeypatch.setattr(login, "ClientSession", Session)
    manager = login.BilibiliLogin(cfg)
    try:
        assert await manager._qr_request("https://passport.bilibili.com/test") == (
            {"ok": True},
            [],
        )
        assert calls[0]["proxy"] == (endpoint if enabled else None)
        assert NODE_ID not in json.dumps(calls)
        assert "vless://" not in json.dumps(calls)
        output = "\n".join(logs)
        assert f"route={'vless+xray' if enabled else 'direct'}" in output
        assert "socket_peer=unavailable" in output
        assert "synthetic" not in output and NODE_ID not in output
    finally:
        await manager.close()


def test_official_asset_redirect_allowlist():
    assert mod._asset_url_allowed(
        "https://release-assets.githubusercontent.com/a?token=PRIVATE"
    )
    assert not mod._asset_url_allowed("http://github.com/a")
    assert not mod._asset_url_allowed("https://github.com.evil.test/a")
    assert not mod._asset_url_allowed("https://127.0.0.1/a")


async def test_old_config_defaults_and_raw_link_survives_save(tmp_path, monkeypatch):
    from astrbot.core.config.astrbot_config import AstrBotConfig
    from parser_under_test.core import config as config_module

    root = Path(__file__).resolve().parents[1]
    raw = AstrBotConfig(
        str(tmp_path / "plugin.json"),
        schema=json.loads((root / "_conf_schema.json").read_text()),
    )
    raw["proxy"] = node()
    raw["parsers_template"] = [
        {"__template_key": "bilibili", "enable": True, "use_proxy": True, "cookies": ""}
    ]
    del raw["proxy_xray_path"]
    del raw["proxy_xray_auto_download"]
    monkeypatch.setattr(
        config_module, "get_astrbot_plugin_data_path", lambda: str(tmp_path / "data")
    )
    cfg = config_module.PluginConfig(raw, SimpleNamespace(get_config=lambda: {}))
    manager = mod.VlessProxy(cfg)
    cfg.save_config()
    saved = json.loads((tmp_path / "plugin.json").read_text(encoding="utf-8-sig"))
    assert saved["proxy"] == node()
    assert saved["proxy_xray_path"] == "" and saved["proxy_xray_auto_download"] is False
    assert "_proxy_manager" not in saved
    await manager.close()
