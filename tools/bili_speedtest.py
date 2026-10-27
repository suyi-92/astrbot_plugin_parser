#!/usr/bin/env python3
"""Bounded Bilibili CDN sampling, without AstrBot, media files or QQ uploads.

API settings, media headers and candidate selection mirror the plugin. This is
a throughput probe, not a complete/resumable download or an integrity test.
"""

import argparse
import asyncio
import getpass
import ipaddress
import json
import os
import re
import shutil
import sys
import time
from contextlib import asynccontextmanager
from contextvars import ContextVar
from importlib.metadata import version
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import urljoin, urlsplit

import aiohttp
from aiohttp.resolver import DefaultResolver
from multidict import CIMultiDict

API_UA = "curl/8.14.1"
# Two-hour first part; still verify the actual response size, never repeat a
# small file to pretend it was one continuous large-file transfer.
DEFAULT_BVID = "BV1Ke411W7YG"
CONTROL_BVID = "BV1MsnG6TEYy"
DEFAULT_SECONDS = 120
DEFAULT_MIB = 256
JSON_OUTPUT = ContextVar("bili_speedtest_json", default=False)
OUTPUT_CONTEXT = ContextVar("bili_speedtest_context", default=None)
MEDIA_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; WOW64) AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/55.0.2883.87 UBrowser/6.2.4098.3 Safari/537.36"
    ),
    "Referer": "https://www.bilibili.com/",
    "Origin": "https://www.bilibili.com",
    "Accept-Encoding": "identity",
}
MEDIA_DOMAINS = (
    "bilivideo.com",
    "bilivideo.cn",
    "bilivideo.net",
    "akamaized.net",
    "szbdyd.com",
    "mountaintoys.cn",
    "nexusedgeio.com",
    "ahdohpiechei.com",
)
BLOCKED_PCDN_DOMAINS = (
    "mcdn.bilivideo.com",
    "mcdn.bilivideo.cn",
    "szbdyd.com",
    "mountaintoys.cn",
    "nexusedgeio.com",
    "ahdohpiechei.com",
)
LOW_PRIORITY_DOMAINS = ("upos-sz-mirrorcosov.bilivideo.com",)


class ProbeError(Exception):
    """Only fixed, non-sensitive codes may be passed to this exception."""


def emit(event, **fields):
    fields = {**(OUTPUT_CONTEXT.get() or {}), **fields}
    if JSON_OUTPUT.get():
        print(json.dumps({"event": event, **fields}, ensure_ascii=False), flush=True)
    elif event == "selected_video" and fields.get("diagnose"):
        print(
            f"样本 {fields['bvid']} · q{fields['quality']} · "
            f"{fields['width']}×{fields['height']}",
            flush=True,
        )
    elif event == "cdn_result":
        result = fields["result"]
        label = fields["cdn"]
        if fields.get("diagnose"):
            label += f" [{fields['client']}]"
        if fields["received_bytes"]:
            # Include connection/header time in the user-facing comparison.
            line = f"{label}: {fields['effective_mib_s']:.2f} MiB/s"
            if result not in ("complete", "sample-byte-limit", "sample-time-limit"):
                line += f"（中断：{result}）"
            elif fields.get("small_source"):
                line += f"（小文件样本：{fields['total_bytes'] / 1024**2:.1f} MiB）"
        else:
            reason = (
                "未收到数据" if result in ("complete", "sample-time-limit") else result
            )
            line = f"{label}: 不可用（{reason}）"
        if fields.get("diagnose"):
            line += f" · {fields['received_mib']:.1f} MiB / {fields['elapsed_s']:.1f}s"
        print(line, flush=True)
    elif event == "failed":
        print(f"测速失败：{fields['error']}", flush=True)
    elif event == "stopped" and fields.get("reason") == "user-cancelled":
        print("测速已停止", flush=True)


def safe_error(exc):
    if isinstance(exc, ProbeError):
        return str(exc)
    code = getattr(exc, "code", getattr(exc, "status", None))
    return (
        f"{type(exc).__name__}:{code}" if isinstance(code, int) else type(exc).__name__
    )


def validate_media_url(url):
    try:
        parsed = urlsplit(url)
        host = parsed.hostname or ""
        if (
            parsed.scheme not in ("http", "https")
            or parsed.username
            or parsed.password
            or parsed.port not in (None, 80, 443)
            or not any(host == d or host.endswith("." + d) for d in MEDIA_DOMAINS)
        ):
            raise ValueError
    except (ValueError, TypeError):
        raise ProbeError("unsafe-media-url") from None


class PublicResolver(DefaultResolver):
    async def resolve(self, host, port=0, family=0):
        addresses = await super().resolve(host, port, family)
        if not addresses or any(
            not ipaddress.ip_address(a["host"]).is_global for a in addresses
        ):
            raise ProbeError("non-public-media-address")
        return addresses


def select_video(data, quality=116, block_pcdn=False):
    dash = data.get("dash") or {}
    if dash:
        videos = [
            v
            for v in dash.get("video", [])
            if int(v["id"]) <= quality
            and v["id"] not in (125, 126)
            and (
                str(v.get("codecs", "")).lower().startswith(("avc", "h264"))
                or (not v.get("codecs") and v.get("codecid") == 7)
            )
        ]
        if not videos:
            raise ProbeError("no-available-AVC-stream")
        raw = max(videos, key=lambda v: v["id"])
        actual_quality = raw["id"]
    else:
        durl = data.get("durl") or []
        actual_quality = int(data.get("quality") or 0)
        if (
            len(durl) != 1
            or not 0 < actual_quality <= quality
            or data.get("format", "").startswith("flv")
        ):
            raise ProbeError("unsupported-play-response")
        raw = durl[0]
    base = raw.get("base_url") or raw.get("baseUrl") or raw.get("url")
    backups = raw.get("backup_url") or raw.get("backupUrl") or []
    if isinstance(backups, str):
        backups = [backups]
    urls = []
    for url in [base, *backups]:
        if not isinstance(url, str) or not url or url in urls:
            continue
        try:
            validate_media_url(url)
        except ProbeError:
            continue
        host = urlsplit(url).hostname or ""
        if block_pcdn and any(
            host == d or host.endswith("." + d) for d in BLOCKED_PCDN_DOMAINS
        ):
            continue
        urls.append(url)
    urls.sort(key=lambda u: (urlsplit(u).hostname or "") in LOW_PRIORITY_DOMAINS)
    if not urls:
        raise ProbeError("no-safe-official-candidate")
    return {
        "quality": actual_quality,
        "width": raw.get("width", 0),
        "height": raw.get("height", 0),
        "fps": raw.get("frame_rate") or raw.get("frameRate") or "unknown",
        "codec": raw.get("codecs") or "unknown",
        "bandwidth": raw.get("bandwidth", 0),
    }, urls


def configure_api(proxy):
    from bilibili_api import request_settings, select_client
    from bilibili_api.utils.network import HEADERS, request_log

    select_client("aiohttp")
    request_settings.set_enable_auto_buvid(False)
    request_settings.set_enable_bili_ticket(False)
    request_settings.set_wbi_retry_times(1)
    request_settings.set_timeout(20)
    request_settings.set_trust_env(False)
    request_settings.set_proxy(proxy or "")
    HEADERS["User-Agent"] = API_UA
    request_log.set_on(False)


async def fetch_video(bvid, page, proxy, credential_file=None):
    from bilibili_api import Credential
    from bilibili_api.video import Video

    configure_api(proxy)
    cookies = {}
    if credential_file:
        try:
            path = Path(credential_file)
            if path.stat().st_size > 65536:
                raise ValueError
            data = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(data, dict) or not all(
                isinstance(v, str) for v in data.values()
            ):
                raise ValueError
            cookies = {
                k: data[k]
                for k in ("SESSDATA", "bili_jct", "DedeUserID", "buvid3", "buvid4")
                if k in data
            }
        except (OSError, ValueError):
            raise ProbeError("invalid-local-credential-file") from None
    video = Video(bvid=bvid, credential=Credential.from_cookies(cookies))
    # The SDK uses the same view + WBI playurl calls as the plugin, including cid.
    async with asyncio.timeout(45):
        return await video.get_download_url(page_index=page - 1)


async def close_api():
    from bilibili_api.utils.network import session_pool

    clients = {id(c): c for pool in session_pool.values() for c in pool.values()}
    await asyncio.gather(*(c.close() for c in clients.values()), return_exceptions=True)


@asynccontextmanager
async def response_for(session, url, proxy):
    for _ in range(4):
        validate_media_url(url)
        if proxy:
            resolver = PublicResolver()
            try:
                await resolver.resolve(urlsplit(url).hostname, 443)
            finally:
                await resolver.close()
        response = await session.get(
            url, proxy=proxy, headers=MEDIA_HEADERS, allow_redirects=False
        )
        if response.status in (301, 302, 303, 307, 308):
            location = response.headers.get("Location", "")
            response.release()
            if not location:
                raise ProbeError("invalid-redirect")
            url = urljoin(url, location)
            continue
        try:
            yield response
        finally:
            response.release()
        return
    raise ProbeError("redirect-limit")


def curl_config_value(value):
    # URLs/proxy credentials travel over stdin, never argv, logs or a temp file.
    if any(ord(c) < 32 or ord(c) == 127 for c in value):
        raise ProbeError("unsafe-curl-config")
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


async def curl_resolve(url):
    parsed = urlsplit(url)
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    resolver = PublicResolver()
    try:
        async with asyncio.timeout(15):
            addresses = await resolver.resolve(parsed.hostname, port)
    finally:
        await resolver.close()
    ips = list(dict.fromkeys(a["host"] for a in addresses))
    # Pin the validated public results so curl cannot resolve to an internal IP
    # between validation and connection. An explicit proxy controls its own DNS.
    return f"{parsed.hostname}:{port}:" + ",".join(
        f"[{ip}]" if ":" in ip else ip for ip in ips
    )


async def stop_curl(process):
    if process.returncode is None:
        try:
            process.terminate()
        except ProcessLookupError:
            pass
    try:
        # Drain the pipe as well as waiting, including when its buffer is full.
        await asyncio.wait_for(process.communicate(), 2)
    except TimeoutError:
        if process.returncode is None:
            try:
                process.kill()
            except ProcessLookupError:
                pass
        await process.communicate()


class CurlBody:
    def __init__(self, process):
        self.process = process

    async def read(self, size):
        async with asyncio.timeout(60):
            chunk = await self.process.stdout.read(size)
        if not chunk:
            code = await self.process.wait()
            if code:
                raise ProbeError(f"curl-exit-{code}")
        return chunk


async def curl_headers(process):
    for _ in range(5):
        try:
            async with asyncio.timeout(60):
                raw = await process.stdout.readuntil(b"\r\n\r\n")
        except asyncio.IncompleteReadError:
            code = await process.wait()
            raise ProbeError(
                f"curl-exit-{code}" if code else "invalid-curl-headers"
            ) from None
        except asyncio.LimitOverrunError:
            raise ProbeError("oversized-curl-headers") from None
        lines = raw.decode("iso-8859-1").split("\r\n")
        match = re.fullmatch(r"HTTP/1\.[01] (\d{3})(?: .*)?", lines[0])
        if not match:
            raise ProbeError("invalid-curl-headers")
        status = int(match[1])
        if 100 <= status < 200 and status != 101:
            continue
        headers = CIMultiDict()
        for line in lines[1:]:
            if line:
                key, sep, value = line.partition(":")
                if not sep or not key:
                    raise ProbeError("invalid-curl-headers")
                headers.add(key.strip(), value.strip())
        return status, headers
    raise ProbeError("too-many-interim-responses")


@asynccontextmanager
async def curl_response_for(session, url, proxy, seconds):
    """One curl process per hop; same headers, no retries/parallelism/Range."""
    for _ in range(4):
        validate_media_url(url)
        resolve = await curl_resolve(url)
        options = [
            ("url", url),
            ("proxy", proxy or ""),
            ("noproxy", "" if proxy else "*"),
            ("resolve", resolve),
            *(("header", f"{key}: {value}") for key, value in MEDIA_HEADERS.items()),
        ]
        config = "\n".join(
            f"{key} = {curl_config_value(value)}" for key, value in options
        )
        process = await asyncio.create_subprocess_exec(
            "curl",
            "--disable",
            "--silent",
            "--http1.1",
            "--globoff",
            "--path-as-is",
            "--include",
            "--suppress-connect-headers",
            "--no-buffer",
            "--proto",
            "=http,https",
            "--connect-timeout",
            "15",
            "--max-time",
            str(seconds),
            "--retry",
            "0",
            "--config",
            "-",
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        try:
            process.stdin.write(config.encode())
            await process.stdin.drain()
            process.stdin.close()
            status, headers = await curl_headers(process)
            if status in (301, 302, 303, 307, 308):
                location = headers.get("Location")
                if not location:
                    raise ProbeError("invalid-redirect")
                url = urljoin(url, location)
                continue
            lengths = headers.getall("Content-Length", [])
            if lengths and (len(set(lengths)) != 1 or not lengths[0].isdigit()):
                raise ProbeError("invalid-content-length")
            yield SimpleNamespace(
                status=status,
                headers=headers,
                content_length=int(lengths[0]) if lengths else None,
                url=SimpleNamespace(host=urlsplit(url).hostname),
                content=CurlBody(process),
            )
            return
        finally:
            await stop_curl(process)
    raise ProbeError("redirect-limit")


async def sample(
    session,
    url,
    *,
    proxy=None,
    seconds=DEFAULT_SECONDS,
    max_bytes=DEFAULT_MIB * 1024**2,
    client="aiohttp",
):
    start = time.monotonic()
    body_start = None
    received = 0
    status = None
    total_bytes = None
    result = "complete"
    host = urlsplit(url).hostname
    served_host = host
    last_report = start
    emit("cdn_start", cdn=host, seconds=seconds, max_mib=max_bytes / 1024**2)
    try:
        async with asyncio.timeout(seconds):
            context = (
                response_for(session, url, proxy)
                if client == "aiohttp"
                else curl_response_for(session, url, proxy, seconds)
            )
            async with context as response:
                status = response.status
                body_start = time.monotonic()
                served_host = response.url.host
                if status not in (200, 206):
                    raise ProbeError(f"HTTP{status}")
                if (
                    response.headers.get("Content-Encoding", "identity").lower()
                    != "identity"
                ):
                    raise ProbeError("unexpected-content-encoding")
                total_bytes = response.content_length
                if status == 206:
                    match = re.fullmatch(
                        r"bytes 0-(\d+)/(\d+)",
                        response.headers.get("Content-Range", ""),
                    )
                    if not match:
                        raise ProbeError("invalid-content-range")
                    end, total = map(int, match.groups())
                    total_bytes = total
                    if end >= total or (
                        response.content_length is not None
                        and response.content_length != end + 1
                    ):
                        raise ProbeError("invalid-content-range")
                    if end + 1 < total:
                        result = "partial-response"
                while received < max_bytes:
                    chunk = await response.content.read(
                        min(256 * 1024, max_bytes - received)
                    )
                    if not chunk:
                        if (
                            response.content_length is not None
                            and received != response.content_length
                        ):
                            raise ProbeError("incomplete-payload")
                        if total_bytes is None:
                            total_bytes = received
                        break
                    received += len(chunk)
                    now = time.monotonic()
                    if now - last_report >= 10:
                        emit(
                            "progress",
                            cdn=host,
                            received_mib=round(received / 1024**2, 2),
                            speed_mib_s=round(
                                received / max(now - body_start, 1e-6) / 1024**2, 3
                            ),
                        )
                        last_report = now
                else:
                    result = "sample-byte-limit"
    except TimeoutError as exc:
        result = (
            "sample-time-limit"
            if time.monotonic() - start >= seconds
            else safe_error(exc)
        )
    except (aiohttp.ClientError, ProbeError, OSError) as exc:
        result = safe_error(exc)
    elapsed = time.monotonic() - start
    body_seconds = max(time.monotonic() - body_start, 1e-6) if body_start else elapsed
    report = {
        "client": client,
        "cdn": host,
        "served_cdn": served_host,
        "http": status,
        "result": result,
        "headers_s": round(body_start - start, 3) if body_start else None,
        "elapsed_s": round(elapsed, 3),
        "received_mib": round(received / 1024**2, 3),
        "received_bytes": received,
        "total_bytes": total_bytes,
        "small_source": total_bytes is not None and total_bytes < max_bytes,
        "speed_mib_s": round(received / max(body_seconds, 1e-6) / 1024**2, 3),
        "effective_mib_s": round(received / max(elapsed, 1e-6) / 1024**2, 3),
    }
    emit("cdn_result", **report)
    return report


async def run(args, proxy):
    emit(
        "settings",
        bvid=args.bvid,
        page=args.page,
        proxy="enabled" if proxy else "direct",
        login="local-file" if args.credential_file else "anonymous",
        api_client="aiohttp",
        api_user_agent=API_UA,
        auto_buvid=False,
        bilibili_api=version("bilibili-api-python"),
        aiohttp=version("aiohttp"),
    )
    diagnose = getattr(args, "diagnose", False)
    if diagnose and not shutil.which("curl"):
        raise ProbeError("curl-not-installed")
    targets = (
        list(dict.fromkeys([CONTROL_BVID, args.bvid])) if diagnose else [args.bvid]
    )
    usable = False
    try:
        for bvid in targets:
            token = OUTPUT_CONTEXT.set({"bvid": bvid, "diagnose": diagnose})
            try:
                result = await run_video(args, proxy, bvid, diagnose)
            finally:
                OUTPUT_CONTEXT.reset(token)
            if result == 2:
                return 2
            usable |= result == 0
    finally:
        await close_api()
    emit("finished", note="仅测B站视频CDN；限时/限量采样不证明整片稳定，也不包含QQ上传")
    return 0 if usable else 1


async def run_video(args, proxy, bvid, diagnose):
    started = time.monotonic()
    # The short reference always uses its first part, even for a custom --page.
    page = 1 if diagnose and bvid == CONTROL_BVID else args.page
    data = await fetch_video(bvid, page, proxy, args.credential_file)
    info, urls = select_video(data, args.quality, args.block_pcdn)
    emit(
        "selected_video",
        **info,
        api_s=round(time.monotonic() - started, 3),
        candidates=len(urls),
        testing=min(len(urls), args.max_cdns),
    )
    usable = False
    async with aiohttp.ClientSession(
        connector=aiohttp.TCPConnector(resolver=PublicResolver(), force_close=diagnose),
        timeout=aiohttp.ClientTimeout(total=None, connect=15, sock_read=60),
        cookie_jar=aiohttp.DummyCookieJar(),
        auto_decompress=False,
        trust_env=False,
    ) as session:
        for url in urls[: args.max_cdns]:
            # Reuse the exact official URL, not a second API result. Sequential
            # clients do not compete for bandwidth or retry a risk response.
            for client in ("aiohttp", "curl") if diagnose else ("aiohttp",):
                report = await sample(
                    session,
                    url,
                    proxy=proxy,
                    seconds=args.seconds,
                    max_bytes=args.mib * 1024**2,
                    client=client,
                )
                if report["http"] in (412, 429):
                    emit("stopped", reason="platform-rate-or-risk-limit")
                    return 2
                usable |= report["received_bytes"] > 0 and report["result"] in (
                    "complete",
                    "partial-response",
                    "sample-byte-limit",
                    "sample-time-limit",
                )
    return 0 if usable else 1


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="B站官方 CDN 单连接测速；Ctrl+C 终止，不保存视频。"
    )
    parser.add_argument("bvid", nargs="?", default=DEFAULT_BVID)
    parser.add_argument("--page", type=int, default=1)
    parser.add_argument(
        "--quality", type=int, choices=(16, 32, 64, 80, 112, 116), default=116
    )
    parser.add_argument(
        "--seconds",
        type=int,
        choices=range(5, 301),
        metavar="5..300",
        default=DEFAULT_SECONDS,
    )
    parser.add_argument(
        "--mib",
        type=int,
        choices=range(1, 1025),
        metavar="1..1024",
        default=DEFAULT_MIB,
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="输出详细 JSON 诊断；默认只显示各 CDN 的速度/失败原因",
    )
    parser.add_argument(
        "--diagnose",
        action="store_true",
        help="对比原短视频与当前样本；各官方地址用 aiohttp/curl 各测一次，保持简短输出",
    )
    parser.add_argument("--max-cdns", type=int, choices=range(1, 5), default=4)
    parser.add_argument("--block-pcdn", action="store_true")
    parser.add_argument(
        "--ask-proxy",
        action="store_true",
        help="隐藏输入 HTTP 代理；也可用 BILI_TEST_PROXY 环境变量",
    )
    parser.add_argument(
        "--credential-file", help="可选：只读本机插件凭证 JSON，不刷新或保存凭证"
    )
    args = parser.parse_args(argv)
    if not re.fullmatch(r"BV[0-9A-Za-z]{10}", args.bvid) or args.page < 1:
        parser.error("需要有效 BV 号和大于零的分 P")
    output_token = JSON_OUTPUT.set(args.json)
    try:
        proxy = (
            getpass.getpass("HTTP 代理地址（输入隐藏）: ")
            if args.ask_proxy
            else os.environ.get("BILI_TEST_PROXY", "")
        )
        if proxy:
            try:
                parsed = urlsplit(proxy)
                if (
                    parsed.scheme != "http"
                    or not parsed.hostname
                    or parsed.path not in ("", "/")
                    or parsed.query
                    or parsed.fragment
                ):
                    raise ValueError
                _ = parsed.port
            except ValueError:
                raise ProbeError("proxy-must-be-http-endpoint") from None
        return asyncio.run(run(args, proxy or None))
    except KeyboardInterrupt:
        emit("stopped", reason="user-cancelled")
        return 130
    except Exception as exc:
        emit("failed", error=safe_error(exc))
        return 1
    finally:
        JSON_OUTPUT.reset(output_token)


if __name__ == "__main__":
    sys.exit(main())
