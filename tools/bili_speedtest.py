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
import sys
import time
from contextlib import asynccontextmanager
from importlib.metadata import version
from pathlib import Path
from urllib.parse import urljoin, urlsplit

import aiohttp
from aiohttp.resolver import DefaultResolver

API_UA = "curl/8.14.1"
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
    print(json.dumps({"event": event, **fields}, ensure_ascii=False), flush=True)


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


async def sample(session, url, *, proxy=None, seconds=45, max_bytes=32 * 1024**2):
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
            async with response_for(session, url, proxy) as response:
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
        "cdn": host,
        "served_cdn": served_host,
        "http": status,
        "result": result,
        "headers_s": round(body_start - start, 3) if body_start else None,
        "elapsed_s": round(elapsed, 3),
        "received_mib": round(received / 1024**2, 3),
        "received_bytes": received,
        "total_bytes": total_bytes,
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
    started = time.monotonic()
    try:
        data = await fetch_video(args.bvid, args.page, proxy, args.credential_file)
    finally:
        await close_api()
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
        connector=aiohttp.TCPConnector(resolver=PublicResolver()),
        timeout=aiohttp.ClientTimeout(total=None, connect=15, sock_read=60),
        cookie_jar=aiohttp.DummyCookieJar(),
        auto_decompress=False,
        trust_env=False,
    ) as session:
        for url in urls[: args.max_cdns]:
            report = await sample(
                session,
                url,
                proxy=proxy,
                seconds=args.seconds,
                max_bytes=args.mib * 1024**2,
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
    emit("finished", note="仅测B站视频CDN；限时/限量采样不证明整片稳定，也不包含QQ上传")
    return 0 if usable else 1


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="B站官方 CDN 单连接测速；Ctrl+C 终止，不保存视频。"
    )
    parser.add_argument("bvid", nargs="?", default="BV1MsnG6TEYy")
    parser.add_argument("--page", type=int, default=1)
    parser.add_argument(
        "--quality", type=int, choices=(16, 32, 64, 80, 112, 116), default=116
    )
    parser.add_argument(
        "--seconds", type=int, choices=range(5, 121), metavar="5..120", default=45
    )
    parser.add_argument(
        "--mib", type=int, choices=range(1, 65), metavar="1..64", default=32
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


if __name__ == "__main__":
    sys.exit(main())
