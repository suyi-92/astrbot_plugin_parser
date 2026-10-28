"""Optional, plugin-owned VLESS -> loopback HTTP bridge using Xray.

The node URI stays in the user's existing configuration. Runtime credentials
and Xray JSON only travel over anonymous stdin; no secret config files/logs.
"""

import asyncio
import base64
import hashlib
import ipaddress
import json
import os
import platform
import re
import secrets
import shutil
import socket
import zipfile
from pathlib import Path
from urllib.parse import parse_qsl, unquote, urljoin, urlsplit
from uuid import UUID

import aiohttp
from astrbot.api import logger

from .exception import ParseException
from .resumable import file_lock

XRAY_VERSION = "v26.10.21"
# Digests published by XTLS/Xray-core for this exact release; never use latest
# or execute an unverified binary downloaded from a user-supplied URL.
XRAY_ASSETS = {
    "x86_64": (
        "Xray-linux-64.zip",
        "0eec3ed4163c273d8c8bf821f7d219e257921cf584aa3fb970715e458e631978",
    ),
    "aarch64": (
        "Xray-linux-arm64-v8a.zip",
        "d1304946c486d98e6b886e9b931eaaaebb23835b30e217638037b7f2721d4e9c",
    ),
}
MAX_ARCHIVE = 32 * 1024**2
MAX_BINARY = 128 * 1024**2
START_TIMEOUT = 15


class ProxyError(ParseException):
    """Messages must be fixed strings; never include URI values or core output."""


def is_vless(value):
    return isinstance(value, str) and value.strip().lower().startswith("vless://")


def get_proxy(cfg):
    """Called only by a parser that enabled use_proxy; never fall back to direct."""
    raw = cfg.proxy
    if not is_vless(raw):
        return raw or None
    manager = getattr(cfg, "_proxy_manager", None)
    if manager is None:
        raise ProxyError("VLESS 代理尚未初始化，请保存配置并重载插件")
    return manager.endpoint


def _no_controls(value):
    return not any(ord(c) < 32 or ord(c) == 127 for c in value)


def parse_vless(uri):
    """Translate the common UUID share-link format; reject ambiguous extras."""
    try:
        if isinstance(uri, str):
            uri = uri.strip()
        if not isinstance(uri, str) or len(uri) > 16384 or not _no_controls(uri):
            raise ValueError
        parsed = urlsplit(uri.strip())
        if (
            parsed.scheme.lower() != "vless"
            or not parsed.hostname
            or parsed.password is not None
            or parsed.path not in ("", "/")
            or not parsed.port
        ):
            raise ValueError
        uid = str(UUID(unquote(parsed.username or "")))
        host = parsed.hostname.encode("idna").decode("ascii")
        if any(c in host for c in " /\\%") or not _no_controls(host):
            raise ValueError
        pairs = parse_qsl(parsed.query, keep_blank_values=True, max_num_fields=48)
        params = dict(pairs)
        if len(params) != len(pairs) or not all(_no_controls(k + v) for k, v in pairs):
            raise ValueError
    except (ValueError, TypeError, UnicodeError):
        raise ProxyError(
            "VLESS 链接格式不正确，需要包含 UUID、节点地址和端口"
        ) from None

    allowed = {
        "type",
        "security",
        "encryption",
        "flow",
        "sni",
        "serverName",
        "fp",
        "fingerprint",
        "alpn",
        "pbk",
        "publicKey",
        "password",
        "sid",
        "shortId",
        "spx",
        "spiderX",
        "pqv",
        "host",
        "path",
        "serviceName",
        "authority",
        "mode",
        "headerType",
        "allowInsecure",
        "insecure",
        "packetEncoding",
    }
    if params.keys() - allowed:
        raise ProxyError(
            "该 VLESS 链接含暂不支持的高级参数，请使用外部客户端的 HTTP 入口"
        )

    def option(*names, default=""):
        values = [params[n] for n in names if n in params]
        if len(set(values)) > 1:
            raise ProxyError("VLESS 链接中有冲突的重复参数")
        return values[0] if values else default

    if option("allowInsecure", "insecure", default="0").lower() not in (
        "",
        "0",
        "false",
    ):
        raise ProxyError("VLESS 内置桥接不关闭 TLS 证书校验，请检查节点证书")
    if option("headerType", default="none") not in ("", "none"):
        raise ProxyError("暂不支持 VLESS 的 TCP HTTP 伪装，请使用外部 HTTP 代理入口")
    if option("packetEncoding") not in ("", "xudp"):
        raise ProxyError("暂不支持该 VLESS packetEncoding 参数")
    transport = option("type", default="tcp")
    transport = {"raw": "tcp", "websocket": "ws", "splithttp": "xhttp"}.get(
        transport, transport
    )
    if transport not in ("tcp", "ws", "grpc", "xhttp", "httpupgrade"):
        raise ProxyError("VLESS 暂支持 TCP/RAW、WS、gRPC、XHTTP 和 HTTPUpgrade")
    security = option("security", default="none")
    if security not in ("none", "tls", "reality"):
        raise ProxyError("VLESS 暂支持 TLS、REALITY 或内网明文传输")
    encryption = option("encryption", default="none") or "none"
    if security == "none" and encryption == "none":
        try:
            private = ipaddress.ip_address(host).is_private
        except ValueError:
            private = host.lower() == "localhost"
        if not private:
            raise ProxyError("公网 VLESS 节点需使用 TLS、REALITY 或 VLESS Encryption")
    flow = option("flow")
    if flow not in ("", "xtls-rprx-vision", "xtls-rprx-vision-udp443"):
        raise ProxyError("不支持该 VLESS flow，请检查节点链接")
    if flow and (transport != "tcp" or security not in ("tls", "reality")):
        raise ProxyError("内置桥接的 Vision flow 仅支持 TCP/RAW + TLS/REALITY")
    stream = {"network": transport, "security": security}
    sni = option("sni", "serverName")
    fp = option("fp", "fingerprint", default="chrome") or "chrome"
    if security == "tls":
        tls = {"allowInsecure": False, "fingerprint": fp}
        if sni:
            tls["serverName"] = sni
        if option("alpn"):
            tls["alpn"] = option("alpn").split(",")
        stream["tlsSettings"] = tls
    elif security == "reality":
        if transport not in ("tcp", "grpc", "xhttp"):
            raise ProxyError("REALITY 仅支持 TCP/RAW、gRPC 或 XHTTP")
        key = option("pbk", "publicKey", "password")
        sid = option("sid", "shortId")
        try:
            if (
                not re.fullmatch(r"[A-Za-z0-9_-]{43}", key)
                or len(base64.urlsafe_b64decode(key + "=")) != 32
            ):
                raise ValueError
            if not sni or not re.fullmatch(r"(?:[0-9a-fA-F]{2}){0,8}", sid):
                raise ValueError
        except ValueError:
            raise ProxyError("REALITY 链接缺少有效的 SNI、pbk 或 sid 参数") from None
        stream["realitySettings"] = {
            "show": False,
            "serverName": sni,
            "fingerprint": fp,
            "publicKey": key,
            "shortId": sid,
            "spiderX": option("spx", "spiderX", default="/"),
        }
        if option("pqv"):
            stream["realitySettings"]["mldsa65Verify"] = option("pqv")
    if transport in ("ws", "xhttp", "httpupgrade"):
        settings = {"path": option("path", default="/") or "/"}
        if option("host"):
            settings["host"] = option("host")
        if transport == "xhttp":
            mode = option("mode", default="auto") or "auto"
            if mode not in ("auto", "packet-up", "stream-up", "stream-one"):
                raise ProxyError("不支持该 XHTTP mode")
            settings["mode"] = mode
        stream[
            {
                "ws": "wsSettings",
                "xhttp": "xhttpSettings",
                "httpupgrade": "httpupgradeSettings",
            }[transport]
        ] = settings
    elif transport == "grpc":
        mode = option("mode", default="gun")
        if mode not in ("", "gun", "multi"):
            raise ProxyError("不支持该 gRPC mode")
        stream["grpcSettings"] = {
            "serviceName": option("serviceName"),
            "multiMode": mode == "multi",
            "authority": option("authority"),
        }
    return {
        "tag": "parser-vless",
        "protocol": "vless",
        "settings": {
            "vnext": [
                {
                    "address": host,
                    "port": parsed.port,
                    "users": [
                        {"id": uid, "encryption": encryption, "flow": flow},
                    ],
                }
            ]
        },
        "streamSettings": stream,
        "mux": {"enabled": False},
    }


def bridge_config(outbound, port, password):
    return {
        "log": {"loglevel": "none", "access": "none", "error": "none"},
        "inbounds": [
            {
                "tag": "parser-http",
                "listen": "127.0.0.1",
                "port": port,
                "protocol": "http",
                "settings": {
                    "accounts": [{"user": "parser", "pass": password}],
                    "allowTransparent": False,
                },
            }
        ],
        # No direct outbound or routing fallback on a broken VLESS connection.
        "outbounds": [outbound],
    }


def _asset_url_allowed(url):
    parsed = urlsplit(url)
    host = parsed.hostname or ""
    return (
        parsed.scheme == "https"
        and not parsed.username
        and not parsed.password
        and (host == "github.com" or host.endswith(".githubusercontent.com"))
        and parsed.port in (None, 443)
    )


async def download_xray(target, asset, digest):
    """Bounded, checksum-pinned installation; only extract the single executable."""
    target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    token = secrets.token_hex(8)
    archive = target.parent / f".xray-{token}.zip.part"
    binary = target.parent / f".xray-{token}.bin.part"
    url = f"https://github.com/XTLS/Xray-core/releases/download/{XRAY_VERSION}/{asset}"
    try:
        checksum, count = hashlib.sha256(), 0
        timeout = aiohttp.ClientTimeout(total=180, connect=15, sock_read=60)
        async with (
            asyncio.timeout(180),
            aiohttp.ClientSession(timeout=timeout, trust_env=False) as session,
        ):
            for _ in range(5):
                if not _asset_url_allowed(url):
                    raise ProxyError("Xray 下载重定向地址不受信任")
                async with session.get(url, allow_redirects=False) as response:
                    if response.status in (301, 302, 303, 307, 308):
                        url = urljoin(url, response.headers.get("Location", ""))
                        continue
                    if response.status != 200:
                        raise ProxyError(
                            "Xray 官方下载失败，请稍后重载或填写已有内核路径"
                        )
                    if (
                        response.content_length
                        and response.content_length > MAX_ARCHIVE
                    ):
                        raise ProxyError("Xray 下载文件超过体积上限")
                    with archive.open("xb") as output:
                        async for block in response.content.iter_chunked(65536):
                            count += len(block)
                            if count > MAX_ARCHIVE:
                                raise ProxyError("Xray 下载文件超过体积上限")
                            checksum.update(block)
                            output.write(block)
                    break
            else:
                raise ProxyError("Xray 下载重定向次数过多")
        if checksum.hexdigest() != digest:
            raise ProxyError("Xray 文件 SHA256 校验失败，已拒绝安装")
        with zipfile.ZipFile(archive) as zipped:
            entry = zipped.getinfo("xray")
            if not 0 < entry.file_size <= MAX_BINARY or entry.is_dir():
                raise ProxyError("Xray 压缩包内容不正确")
            with zipped.open(entry) as source, binary.open("xb") as output:
                shutil.copyfileobj(source, output, length=65536)
                output.flush()
                os.fsync(output.fileno())
        binary.chmod(0o700)
        os.replace(binary, target)
    except asyncio.CancelledError:
        raise
    except ProxyError:
        raise
    except Exception:
        raise ProxyError(
            "Xray 下载或安装失败，请检查容器网络、磁盘权限或填写已有内核路径"
        ) from None
    finally:
        archive.unlink(missing_ok=True)
        binary.unlink(missing_ok=True)


async def find_xray(cfg):
    configured = (getattr(cfg, "proxy_xray_path", "") or "").strip()
    if configured:
        executable = shutil.which(configured)
        if not executable:
            raise ProxyError("配置的 Xray 路径不存在或不可执行，请填写容器内路径")
        return executable
    if executable := shutil.which("xray"):
        return executable
    arch = {"amd64": "x86_64", "arm64": "aarch64"}.get(
        platform.machine().lower(), platform.machine().lower()
    )
    if platform.system() != "Linux" or arch not in XRAY_ASSETS:
        raise ProxyError("自动安装仅支持 Linux amd64/arm64，请填写已有 Xray 内核路径")
    target = Path(cfg.data_dir) / "proxy_bin" / f"xray-{XRAY_VERSION}-{arch}"
    async with file_lock(target):
        if target.is_file() and os.access(target, os.X_OK):
            return str(target)
        if not getattr(cfg, "proxy_xray_auto_download", False):
            raise ProxyError(
                "未找到 Xray；请开启“允许自动下载 Xray 内核”或填写内核路径，然后重载插件"
            )
        logger.info("VLESS：正在下载并校验官方 Xray 内核，首次安装最多等待 180 秒")
        await download_xray(target, *XRAY_ASSETS[arch])
    return str(target)


class VlessProxy:
    def __init__(self, cfg):
        self.cfg = cfg
        cfg._proxy_manager = self
        self.process = None
        self._endpoint = None
        self._source_hash = None
        self._starting = None
        self._lock = asyncio.Lock()
        self.closed = False
        self.error = "VLESS 代理尚未就绪，请保存配置并重载插件"

    @property
    def endpoint(self):
        if self.closed:
            raise ProxyError("VLESS 代理已停止，请重载插件")
        if self.process is not None and self.process.returncode is not None:
            raise ProxyError("Xray 代理进程已退出，请检查内核及节点配置后重载插件")
        if self._endpoint is None:
            raise ProxyError(self.error)
        if (
            self._source_hash
            != hashlib.sha256(self.cfg.proxy.strip().encode()).digest()
        ):
            raise ProxyError("VLESS 节点配置已改变，请重载插件后再解析")
        return self._endpoint

    async def start(self):
        if not is_vless(self.cfg.proxy) or not any(
            p.use_proxy for p in self.cfg.parser
        ):
            return
        async with self._lock:
            if self.closed:
                raise ProxyError("VLESS 代理已停止")
            if self._endpoint and self.process and self.process.returncode is None:
                return
            self._starting = asyncio.current_task()
            try:
                outbound = parse_vless(self.cfg.proxy)
                executable = await find_xray(self.cfg)
                with socket.socket() as reserve:
                    reserve.bind(("127.0.0.1", 0))
                    port = reserve.getsockname()[1]
                password = secrets.token_urlsafe(24)
                config = bridge_config(outbound, port, password)
                # Shield process creation so cancellation cannot orphan a child
                # between OS spawn and the Process object being returned.
                spawning = asyncio.create_task(
                    asyncio.create_subprocess_exec(
                        executable,
                        "run",
                        "-config",
                        "stdin:",
                        "-format",
                        "json",
                        stdin=asyncio.subprocess.PIPE,
                        stdout=asyncio.subprocess.DEVNULL,
                        stderr=asyncio.subprocess.DEVNULL,
                        env={
                            k: v
                            for k, v in os.environ.items()
                            if not k.lower().startswith(
                                ("xray.", "xray_", "v2ray.", "v2ray_")
                            )
                        },
                    )
                )
                try:
                    self.process = await asyncio.shield(spawning)
                except asyncio.CancelledError:
                    self.process = await spawning
                    raise
                async with asyncio.timeout(START_TIMEOUT):
                    self.process.stdin.write(json.dumps(config).encode())
                    await self.process.stdin.drain()
                    self.process.stdin.close()
                    await self._wait_ready(port)
                self._endpoint = f"http://parser:{password}@127.0.0.1:{port}"
                self._source_hash = hashlib.sha256(
                    self.cfg.proxy.strip().encode()
                ).digest()
                logger.info("VLESS 本地代理入口已就绪；节点连通性以实际解析结果为准")
            except asyncio.CancelledError:
                await self._stop()
                raise
            except ProxyError as exc:
                self.error = str(exc)
                await self._stop()
                raise
            except Exception:
                self.error = (
                    "Xray 启动失败或超时，请检查内核版本、执行权限和 VLESS 节点参数"
                )
                await self._stop()
                raise ProxyError(self.error) from None
            finally:
                self._starting = None

    async def _wait_ready(self, port):
        while True:
            if self.process.returncode is not None:
                raise ProxyError(
                    "Xray 启动失败，请检查节点参数和内核版本；详细输出已隐藏以保护凭证"
                )
            writer = None
            try:
                async with asyncio.timeout(0.5):
                    reader, writer = await asyncio.open_connection("127.0.0.1", port)
                    # No credentials / external connection: HTTP auth is checked
                    # locally by Xray before dialing any requested destination.
                    writer.write(
                        b"HEAD http://parser-probe.invalid/ HTTP/1.1\r\nHost: parser-probe.invalid\r\nConnection: close\r\n\r\n"
                    )
                    await writer.drain()
                    status = await reader.readline()
                    if status.startswith(b"HTTP/1.1 407") or status.startswith(
                        b"HTTP/1.0 407"
                    ):
                        await asyncio.sleep(0.05)
                        if self.process.returncode is None:
                            return
            except (OSError, TimeoutError):
                pass
            finally:
                if writer:
                    writer.close()
                    await writer.wait_closed()
            await asyncio.sleep(0.05)

    async def _stop(self):
        self._endpoint = None
        process, self.process = self.process, None
        if process is not None:
            if process.returncode is None:
                try:
                    process.terminate()
                except ProcessLookupError:
                    pass
            try:
                await asyncio.wait_for(process.wait(), 3)
            except TimeoutError:
                if process.returncode is None:
                    try:
                        process.kill()
                    except ProcessLookupError:
                        pass
                await process.wait()

    async def close(self):
        self.closed = True
        starting = self._starting
        if starting and starting is not asyncio.current_task():
            starting.cancel()
            await asyncio.gather(starting, return_exceptions=True)
        await self._stop()
