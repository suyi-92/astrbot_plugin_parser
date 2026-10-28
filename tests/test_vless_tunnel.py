"""Real local Xray tunnels with synthetic keys, no remote node/API required."""

import asyncio
import base64
import datetime
import ipaddress
import json
import os
import socket
import ssl
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import urlencode, urlsplit

import aiohttp
import pytest
from aiohttp import web
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa, x25519
from cryptography.x509.oid import NameOID

from parser_under_test.core import vless_proxy as mod
from parser_under_test.core.parsers.bilibili.api import BilibiliAPI
from test_vless_proxy import NODE_ID, node


@pytest.fixture
def xray():
    path = os.environ.get("XRAY_TEST_BINARY")
    if not path:
        pytest.skip("Set XRAY_TEST_BINARY to test the actual local VLESS tunnel")
    assert Path(path).is_file(), "XRAY_TEST_BINARY is not a file"
    return path


def tls_files(tmp_path):
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "localhost")])
    now = datetime.datetime.now(datetime.timezone.utc)
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(minutes=1))
        .not_valid_after(now + datetime.timedelta(days=1))
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
        .add_extension(
            x509.SubjectAlternativeName(
                [
                    x509.DNSName("localhost"),
                    x509.IPAddress(ipaddress.ip_address("127.0.0.1")),
                ]
            ),
            critical=False,
        )
        .sign(key, hashes.SHA256())
    )
    cert_path, key_path = tmp_path / "cert.pem", tmp_path / "key.pem"
    cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    key_path.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    key_path.chmod(0o600)
    return cert_path, key_path


@asynccontextmanager
async def core_process(binary, config):
    process = await asyncio.create_subprocess_exec(
        binary,
        "run",
        "-config",
        "stdin:",
        "-format",
        "json",
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL,
    )
    try:
        process.stdin.write(json.dumps(config).encode())
        await process.stdin.drain()
        process.stdin.close()
        port = config["inbounds"][0]["port"]
        async with asyncio.timeout(5):
            while True:
                assert process.returncode is None, "local Xray server failed"
                try:
                    _, writer = await asyncio.open_connection("127.0.0.1", port)
                    writer.close()
                    await writer.wait_closed()
                    break
                except OSError:
                    await asyncio.sleep(0.02)
        yield process
    finally:
        if process.returncode is None:
            process.terminate()
        try:
            await asyncio.wait_for(process.wait(), 3)
        except TimeoutError:
            process.kill()
            await process.wait()


@pytest.mark.parametrize(
    "transport,security",
    [
        ("tcp", "tls"),
        ("ws", "tls"),
        ("grpc", "tls"),
        ("xhttp", "tls"),
        ("httpupgrade", "tls"),
        ("tcp", "reality"),
    ],
)
async def test_actual_vless_tunnel_https_auth_and_shutdown(
    xray,
    cfg,
    tmp_path,
    monkeypatch,
    transport,
    security,
):
    cert, key = tls_files(tmp_path)
    # Xray defaults to bundled CA roots, not SSL_CERT_FILE. Add this test CA to
    # the client's trust pool without disabling certificate verification.
    original_config = mod.bridge_config

    def trust_test_ca(*args):
        config = original_config(*args)
        stream_settings = config["outbounds"][0]["streamSettings"]
        if stream_settings["security"] == "tls":
            stream_settings["tlsSettings"]["certificates"] = [
                {"certificateFile": str(cert), "usage": "verify"}
            ]
        return config

    monkeypatch.setattr(mod, "bridge_config", trust_test_ca)
    server_tls = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    server_tls.minimum_version = ssl.TLSVersion.TLSv1_3
    server_tls.set_alpn_protocols(["h2", "http/1.1"])
    server_tls.load_cert_chain(cert, key)
    client_tls = ssl.create_default_context(cafile=str(cert))
    body = bytes(range(256)) * 8192
    calls = []

    async def handler(request):
        calls.append(request.path)
        assert "Proxy-Authorization" not in request.headers
        return web.Response(body=body)

    app = web.Application()
    app.router.add_get("/video", handler)
    runner = web.AppRunner(app, access_log=None)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0, ssl_context=server_tls)
    await site.start()
    target_port = site._server.sockets[0].getsockname()[1]
    with socket.socket() as reserve:
        reserve.bind(("127.0.0.1", 0))
        node_port = reserve.getsockname()[1]
    params = {"type": transport, "security": security, "sni": "localhost"}
    settings_key = {
        "ws": "wsSettings",
        "grpc": "grpcSettings",
        "xhttp": "xhttpSettings",
        "httpupgrade": "httpupgradeSettings",
    }.get(transport)
    stream = {"network": transport, "security": security}
    if settings_key:
        stream[settings_key] = (
            {"serviceName": "test"} if transport == "grpc" else {"path": "/test"}
        )
        params["serviceName" if transport == "grpc" else "path"] = (
            "test" if transport == "grpc" else "/test"
        )
    if security == "tls":
        stream["tlsSettings"] = {
            "certificates": [{"certificateFile": str(cert), "keyFile": str(key)}]
        }
    else:
        reality_key = x25519.X25519PrivateKey.generate()

        def encode(raw):
            return base64.urlsafe_b64encode(raw).decode().rstrip("=")

        private = encode(reality_key.private_bytes_raw())
        public = encode(reality_key.public_key().public_bytes_raw())
        stream["realitySettings"] = {
            "dest": f"127.0.0.1:{target_port}",
            "serverNames": ["localhost"],
            "privateKey": private,
            "shortIds": ["0123456789abcdef"],
        }
        params.update(pbk=public, sid="0123456789abcdef", flow="xtls-rprx-vision")
    server = {
        "log": {"loglevel": "none"},
        "inbounds": [
            {
                "listen": "127.0.0.1",
                "port": node_port,
                "protocol": "vless",
                "settings": {
                    "decryption": "none",
                    "clients": [{"id": NODE_ID, "flow": params.get("flow", "")}],
                },
                "streamSettings": stream,
            }
        ],
        # Xray 26.10 blocks private destinations by default on server inbounds.
        # This synthetic server permits only this test's loopback HTTPS port.
        "outbounds": [
            {
                "protocol": "freedom",
                "settings": {
                    "finalRules": [
                        {
                            "action": "allow",
                            "network": "tcp",
                            "ip": ["127.0.0.1/32"],
                            "port": str(target_port),
                        },
                    ]
                },
            }
        ],
    }
    cfg.proxy = f"vless://{NODE_ID}@127.0.0.1:{node_port}?{urlencode(params)}"
    original = cfg.proxy
    cfg.parser.bilibili.use_proxy = True
    cfg.proxy_xray_path = xray
    manager = mod.VlessProxy(cfg)
    try:
        async with core_process(xray, server):
            await manager.start()
            async with aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=15)
            ) as session:
                async with session.get(
                    f"https://127.0.0.1:{target_port}/video",
                    proxy=manager.endpoint,
                    ssl=client_tls,
                ) as response:
                    assert await response.read() == body
                endpoint = urlsplit(manager.endpoint)
                async with session.get(
                    "http://parser-probe.invalid/",
                    proxy=f"http://127.0.0.1:{endpoint.port}",
                ) as response:
                    assert response.status == 407
            assert calls == ["/video"]
            assert cfg.proxy == original
            process = manager.process
            await manager.close()
            assert process.returncode is not None
            with pytest.raises(mod.ProxyError):
                mod.get_proxy(cfg)
    finally:
        await manager.close()
        await runner.cleanup()


async def test_node_change_and_core_exit_fail_closed(xray, cfg):
    cfg.proxy, cfg.proxy_xray_path = node(), xray
    cfg.parser.bilibili.use_proxy = True
    manager = mod.VlessProxy(cfg)
    try:
        await manager.start()
        cfg.proxy = node(sni="changed.example")
        with pytest.raises(mod.ProxyError, match="改变"):
            mod.get_proxy(cfg)
        cfg.proxy = node()
        manager.process.kill()
        await manager.process.wait()
        with pytest.raises(mod.ProxyError, match="退出"):
            mod.get_proxy(cfg)
    finally:
        await manager.close()


async def test_bilibili_worker_receives_http_endpoint_not_vless(xray, cfg):
    cfg.proxy, cfg.proxy_xray_path = node(), xray
    cfg.parser.bilibili.use_proxy = True
    manager = mod.VlessProxy(cfg)
    await manager.start()
    api = BilibiliAPI(cfg)
    recorded = []

    class Writer:
        def write(self, value):
            recorded.append(json.loads(value))

        async def drain(self):
            pass

    reader = asyncio.StreamReader()
    reader.feed_data(b'{"data": true}\n')
    api.process = SimpleNamespace(returncode=None, stdin=Writer(), stdout=reader)
    try:
        assert await api.call("settings") is True
        assert recorded[0]["settings"]["proxy"] == manager.endpoint
        assert "vless://" not in json.dumps(recorded)
        assert NODE_ID not in json.dumps(recorded)
    finally:
        api.process = None
        await api.close()
        await manager.close()


@pytest.mark.parametrize("missing_core", [False, True])
async def test_astrbot_vless_lifecycle_and_direct_parser_isolation(
    xray, tmp_path, monkeypatch, missing_core
):
    from astrbot.core.config.astrbot_config import AstrBotConfig
    from parser_under_test.core import config as config_module
    from parser_under_test.core.parsers.bilibili import BilibiliParser
    from parser_under_test.core.parsers.douyin import DouyinParser
    from parser_under_test.main import ParserPlugin

    root = Path(__file__).resolve().parents[1]
    raw = AstrBotConfig(
        str(tmp_path / "plugin.json"),
        schema=json.loads((root / "_conf_schema.json").read_text()),
    )
    raw["proxy"] = node()
    raw["proxy_xray_path"] = str(tmp_path / "missing-core") if missing_core else xray
    raw["parsers_template"] = [
        {"__template_key": name, "enable": True, "use_proxy": proxy, "cookies": ""}
        for name, proxy in [("bilibili", True), ("douyin", False)]
    ]
    monkeypatch.setattr(
        config_module, "get_astrbot_plugin_data_path", lambda: str(tmp_path / "data")
    )
    monkeypatch.setattr(
        config_module, "get_astrbot_plugin_path", lambda: str(root.parent)
    )
    context = SimpleNamespace(get_config=lambda: {"timezone": "Asia/Shanghai"})
    plugin = ParserPlugin(context, raw)
    process = None
    try:
        await plugin.initialize()
        bili = plugin._get_parser_by_type(BilibiliParser)
        douyin = plugin._get_parser_by_type(DouyinParser)
        assert douyin.proxy is None
        assert douyin.session is not None
        if missing_core:
            with pytest.raises(mod.ProxyError, match="路径"):
                await bili.api.call("settings")
        else:
            process = plugin.proxy_manager.process
            assert bili.proxy == plugin.proxy_manager.endpoint
            # Starts the real isolated Python worker; this method does not dial
            # the node or Bilibili. Remote routing is covered by tunnel tests.
            assert (await bili.api.call("settings"))["client"] == "aiohttp"
    finally:
        await plugin.terminate()
    assert process is None or process.returncode is not None
    assert plugin.downloader.client.closed
    assert bili.api.process is None
    assert douyin._session is None or douyin._session.closed
