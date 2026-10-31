"""Plugin bridge contract tests, using a local Python process and HTTPS origin.

No Xray download, external proxy or Bilibili request is needed. Actual VLESS
transport interop remains in manual_vless_tunnel.py for explicit maintainer runs.
"""

import asyncio
import datetime
import ipaddress
import json
import ssl
import sys
import textwrap
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import urlsplit

import aiohttp
import pytest
from aiohttp import web
from aiohttp.resolver import DefaultResolver
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID

from parser_under_test.core import vless_proxy as mod
from parser_under_test.core import diagnostics as diag, resumable
from parser_under_test.core.parsers.bilibili.api import BilibiliAPI
from test_vless_proxy import NODE_ID, node


@pytest.fixture
def bridge_core(tmp_path):
    """A real child process that implements only the local HTTP core contract.

    This is not a VLESS implementation. Restrict destinations to loopback and
    reject non-CONNECT methods so a failing test cannot contact an external node.
    """
    path = tmp_path / "local-http-core"
    script = r'''
        import asyncio
        import base64
        import ipaddress
        import json
        import sys

        config = json.load(sys.stdin)
        inbound = config["inbounds"][0]
        account = inbound["settings"]["accounts"][0]
        auth = b"Basic " + base64.b64encode(
            (account["user"] + ":" + account["pass"]).encode()
        )

        async def relay(reader, writer):
            try:
                while data := await reader.read(65536):
                    writer.write(data)
                    await writer.drain()
            finally:
                writer.close()
                await writer.wait_closed()

        async def client(reader, writer):
            remote = None
            try:
                raw = await reader.readuntil(b"\r\n\r\n")
                lines = raw.split(b"\r\n")
                headers = dict(line.split(b":", 1) for line in lines[1:] if b":" in line)
                if headers.get(b"Proxy-Authorization", b"").strip() != auth:
                    writer.write(b'HTTP/1.1 407 Proxy Authentication Required\r\n'
                                 b'Proxy-Authenticate: Basic realm="local-test"\r\n'
                                 b'Content-Length: 0\r\nConnection: close\r\n\r\n')
                    await writer.drain()
                    return
                method, authority, _ = lines[0].split(b" ")
                assert method == b"CONNECT"
                host, port = authority.decode().rsplit(":", 1)
                assert ipaddress.ip_address(host).is_loopback
                remote_reader, remote = await asyncio.open_connection(host, int(port))
                writer.write(b"HTTP/1.1 200 Connection established\r\n\r\n")
                await writer.drain()
                await asyncio.gather(relay(reader, remote), relay(remote_reader, writer))
            except (OSError, ValueError, AssertionError, asyncio.IncompleteReadError):
                pass
            finally:
                if remote:
                    remote.close()
                    await remote.wait_closed()
                writer.close()
                await writer.wait_closed()

        async def main():
            server = await asyncio.start_server(client, "127.0.0.1", inbound["port"])
            async with server:
                await server.serve_forever()

        asyncio.run(main())
    '''
    path.write_text(f"#!{sys.executable}\n" + textwrap.dedent(script))
    path.chmod(0o700)
    return str(path)


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
async def test_bridge_https_auth_redaction_and_shutdown(
    bridge_core, cfg, tmp_path, monkeypatch, transport, security
):
    cert, key = tls_files(tmp_path)
    server_tls = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    server_tls.minimum_version = ssl.TLSVersion.TLSv1_3
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
    params = {"type": transport, "security": security, "sni": "localhost"}
    if security == "reality":
        params.update(pbk="A" * 43, sid="0123456789abcdef", flow="xtls-rprx-vision")
    cfg.proxy = node(**params)
    original = cfg.proxy
    # The config translator remains real; the local test peer only replaces
    # the third-party executable at the subprocess boundary.
    outbound = mod.parse_vless(cfg.proxy)
    assert outbound["streamSettings"]["network"] == transport
    assert outbound["streamSettings"]["security"] == security
    cfg.parser.bilibili.use_proxy = True
    cfg.proxy_xray_path = bridge_core
    cfg.debug_media_info = True
    logs = []
    monkeypatch.setattr(diag.logger, "debug", logs.append)
    monkeypatch.setattr(resumable, "validate_media_url", lambda url: None)
    monkeypatch.setattr(resumable.PublicResolver, "resolve", DefaultResolver.resolve)
    manager = mod.VlessProxy(cfg)
    downloader = resumable.ResumableDownloader(cfg)
    try:
        await manager.start()
        async with aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=15),
            connector=aiohttp.TCPConnector(ssl=client_tls),
            trust_env=False,
        ) as session:
            downloader.client = session
            path = await downloader.download(
                [f"https://127.0.0.1:{target_port}/video?sign=SYNTHETIC-SECRET"],
                downloader.directory / "fixture.mp4",
                resource="fixture:video",
                proxy=manager.endpoint,
            )
            assert path.read_bytes() == body
            response_log = next(line for line in logs if "CDN响应" in line)
            assert "route=vless+xray" in response_log
            assert "socket_peer=loopback proxy_socket=confirmed" in response_log
            for secret in (NODE_ID, "SYNTHETIC-SECRET", "vless://", "http://parser:"):
                assert secret not in "\n".join(logs)
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
        await downloader.close()
        await manager.close()
        await runner.cleanup()


async def test_node_change_and_core_exit_fail_closed(bridge_core, cfg):
    cfg.proxy, cfg.proxy_xray_path = node(), bridge_core
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


async def test_bilibili_worker_receives_http_endpoint_not_vless(bridge_core, cfg):
    cfg.proxy, cfg.proxy_xray_path = node(), bridge_core
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
    bridge_core, tmp_path, monkeypatch, missing_core
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
    raw["proxy_xray_path"] = str(tmp_path / "missing-core") if missing_core else bridge_core
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
            # the node or Bilibili. Actual VLESS interop is a maintainer test.
            assert (await bili.api.call("settings"))["client"] == "aiohttp"
    finally:
        await plugin.terminate()
    assert process is None or process.returncode is not None
    assert plugin.downloader.client.closed
    assert bili.api.process is None
    assert douyin._session is None or douyin._session.closed
