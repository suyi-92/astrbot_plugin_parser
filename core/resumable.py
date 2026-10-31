"""Bounded, resumable downloads for Bilibili's official media candidates only.

The ordinary Downloader is deliberately not routed through this implementation.
Receipts contain hashes and HTTP validators, never URLs or credentials.
"""

import asyncio
import hashlib
import ipaddress
import json
import os
import re
import time
import weakref
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import urljoin, urlsplit

import aiohttp
from aiohttp.resolver import DefaultResolver

from astrbot.api import logger

from .diagnostics import TransferProgress, note, proxy_route, response_peer, stage
from .exception import DownloadException, SizeLimitException, ZeroSizeException

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
_LOCKS = weakref.WeakValueDictionary()
_ACTIVE = set()


def file_lock(path):
    key = str(path.resolve())
    lock = _LOCKS.get(key)
    if lock is None:
        lock = _LOCKS[key] = asyncio.Lock()
    return lock


@asynccontextmanager
async def cache_lock(path):
    # All owners live in AstrBot's event loop. terminate() awaits them before reload.
    async with file_lock(path):
        _ACTIVE.add(path)
        try:
            yield
        finally:
            _ACTIVE.discard(path)


def digest_file(path):
    with path.open("rb") as source:
        return hashlib.file_digest(source, "sha256").hexdigest()


def write_json(path, data):
    tmp = path.with_name(path.name + ".tmp")
    try:
        with tmp.open("w", encoding="utf-8") as file:
            json.dump(data, file)
            file.flush()
            os.fsync(file.fileno())
        tmp.replace(path)
    finally:
        tmp.unlink(missing_ok=True)


def read_json(path):
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


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
    except ValueError:
        raise DownloadException("B站媒体地址未通过安全检查") from None


class PublicResolver(DefaultResolver):
    async def resolve(self, host, port=0, family=0):
        addresses = await super().resolve(host, port, family)
        if not addresses or any(
            not ipaddress.ip_address(a["host"]).is_global for a in addresses
        ):
            raise OSError("non-public CDN address")
        return addresses


class Retryable(Exception):
    def __init__(self, code, *, rotate=False, refresh=False):
        self.code, self.rotate, self.refresh = code, rotate, refresh


class ResumableDownloader:
    def __init__(self, cfg):
        self.cfg = cfg
        self.options = cfg.parser.bilibili
        self.directory = cfg.cache_dir / "bilibili"
        self.directory.mkdir(parents=True, exist_ok=True)
        self.client = None
        self.tasks = set()
        self.closed = False
        self.cleaner = None

    def start(self, coro):
        if self.closed:
            coro.close()
            raise DownloadException("B站下载器已停止")
        task = asyncio.create_task(coro, name="bilibili-media")
        self.tasks.add(task)
        task.add_done_callback(self.tasks.discard)
        # Observe detached failures (e.g. debounce drops the result) without logging secrets.
        task.add_done_callback(lambda t: None if t.cancelled() else t.exception())
        return task

    async def close(self):
        self.closed = True
        tasks = list(self.tasks)
        if self.cleaner:
            tasks.append(self.cleaner)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        if self.client:
            await self.client.close()

    async def _session(self):
        if self.closed:
            raise DownloadException("B站下载器已停止")
        if self.client is None:
            self.client = aiohttp.ClientSession(
                connector=aiohttp.TCPConnector(resolver=PublicResolver()),
                timeout=aiohttp.ClientTimeout(
                    total=self.cfg.download_timeout,
                    connect=max(1, self.options.connect_timeout),
                    sock_read=max(1, self.options.read_timeout),
                ),
                auto_decompress=False,
                cookie_jar=aiohttp.DummyCookieJar(),
                trust_env=False,
            )
            self.cleaner = asyncio.create_task(
                self._clean_loop(), name="bilibili-cache-cleaner"
            )
        return self.client

    async def _clean_loop(self):
        while True:
            self.cleanup()
            await asyncio.sleep(3600)

    def cleanup(self):
        """Bound abandoned partial storage to 4 * source_max_size as well as age."""
        now = time.time()
        partials = []
        try:
            entries = list(self.directory.iterdir())
        except OSError:
            logger.warning("B站缓存清理无法读取目录，请检查权限")
            return
        for path in entries:
            if any(path.name.startswith(active.name) for active in _ACTIVE):
                continue
            try:
                if not path.is_file() or path.is_symlink():
                    continue
                stat = path.stat()
                age = now - stat.st_mtime
                partial = (
                    ".part" in path.name or ".tmp" in path.name or ".m4s" in path.name
                )
                hours = (
                    self.options.partial_expire_hours
                    if partial
                    else self.options.cache_expire_hours
                )
                if age > max(1, hours) * 3600:
                    path.unlink(missing_ok=True)
                elif path.name.endswith((".part", ".m4s")):
                    partials.append((path, stat.st_size, stat.st_mtime))
            except OSError:
                logger.warning("B站缓存文件清理失败，请检查权限")
        budget = self.cfg.source_max_size * 1024 * 1024 * 4
        size = sum(item[1] for item in partials)
        for path, file_size, _ in sorted(partials, key=lambda item: item[2]):
            if size <= budget:
                break
            try:
                path.unlink(missing_ok=True)
                size -= file_size
                Path(str(path) + ".json").unlink(missing_ok=True)
                Path(str(path) + ".complete.json").unlink(missing_ok=True)
            except OSError:
                logger.warning("B站残片清理失败，请检查权限")

    @staticmethod
    def _discard(path):
        Path(str(path) + ".part").unlink(missing_ok=True)
        Path(str(path) + ".part.json").unlink(missing_ok=True)

    async def cached(self, path, resource):
        receipt = read_json(Path(str(path) + ".complete.json"))
        try:
            if (
                receipt.get("resource") == resource
                and path.stat().st_size == receipt.get("size")
                and 0 < path.stat().st_size <= self.cfg.source_max_size * 1024 * 1024
                and await asyncio.to_thread(digest_file, path) == receipt.get("sha256")
            ):
                path.touch()
                Path(str(path) + ".complete.json").touch()
                return True
        except OSError:
            pass
        return False

    async def publish(self, part, path, resource):
        size = part.stat().st_size
        if size == 0:
            raise ZeroSizeException
        if size > self.cfg.source_max_size * 1024 * 1024:
            raise SizeLimitException
        receipt = {
            "resource": resource,
            "size": size,
            "sha256": await asyncio.to_thread(digest_file, part),
        }
        # No await between atomic file promotion and receipt publication.
        part.replace(path)
        write_json(Path(str(path) + ".complete.json"), receipt)

    @asynccontextmanager
    async def _response(self, url, headers, proxy):
        session = await self._session()
        for _ in range(4):
            validate_media_url(url)
            # Proxies resolve remotely; also reject locally resolved internal hosts.
            if proxy:
                resolver = PublicResolver()
                try:
                    await resolver.resolve(urlsplit(url).hostname, 443)
                finally:
                    await resolver.close()
            response = await session.get(
                url, headers=headers, proxy=proxy, allow_redirects=False
            )
            if response.status in (301, 302, 303, 307, 308):
                location = response.headers.get("Location", "")
                response.release()
                if not location:
                    raise Retryable("redirect", rotate=True)
                url = urljoin(url, location)
                continue
            try:
                yield response
            finally:
                response.release()
            return
        raise Retryable("redirect-limit", rotate=True)

    async def download(
        self,
        urls,
        path,
        *,
        resource,
        headers=None,
        proxy=None,
        refresh=None,
        validate=None,
    ):
        """1 + download_retry_times attempts in total, including CDN switches/refresh.

        Same-address resumes need a strong ETag or Last-Modified plus total length.
        Across addresses only equal strong ETags + equal totals permit appending.
        Otherwise the partial is discarded before writing a new representation.
        """
        self.cleanup()
        debug = self.cfg.debug_media_info
        waiting = time.monotonic()
        note(debug, "流缓存锁等待")
        try:
            async with asyncio.timeout(max(1, self.options.task_timeout)):
                async with cache_lock(path):
                    note(
                        debug,
                        f"流缓存锁就绪 elapsed_s={time.monotonic() - waiting:.2f}",
                    )
                    with stage(debug, "流缓存校验"):
                        cached = await self.cached(path, resource)
                    if cached:
                        note(debug, f"流缓存命中 bytes={path.stat().st_size}")
                        return path
                    return await self._download(
                        list(dict.fromkeys(urls)),
                        path,
                        resource,
                        headers,
                        proxy,
                        refresh,
                        validate,
                    )
        except TimeoutError:
            raise DownloadException("B站下载达到任务时间上限，可稍后重试") from None
        except OSError:
            raise DownloadException("B站缓存写入失败，请检查磁盘空间及权限") from None

    async def _download(self, urls, path, resource, headers, proxy, refresh, validate):
        debug = self.cfg.debug_media_info
        stream = "audio" if resource.endswith(":audio") else "video"
        part, meta_path = Path(str(path) + ".part"), Path(str(path) + ".part.json")
        max_bytes = self.cfg.source_max_size * 1024 * 1024
        index, refreshed, last_error = 0, False, "no-candidate"
        failures = {}
        # Never forward credentials to a media host or a redirect target.
        request_headers = {
            k: v
            for k, v in (headers or {}).items()
            if k.lower() in ("user-agent", "referer", "origin")
        }
        request_headers["Accept-Encoding"] = "identity"
        for attempt in range(max(0, self.cfg.download_retry_times) + 1):
            if not urls:
                break
            url = urls[index % len(urls)]
            host = urlsplit(url).hostname or "invalid"
            url_hash = hashlib.sha256(url.encode()).hexdigest()
            meta = read_json(meta_path)
            offset = part.stat().st_size if part.exists() else 0
            etag = meta.get("etag") if isinstance(meta.get("etag"), str) else ""
            strong = len(etag) >= 2 and etag.startswith('"') and etag.endswith('"')
            same_url = meta.get("url_hash") == url_hash
            resumable = (
                meta.get("resource") == resource
                and isinstance(meta.get("total"), int)
                and meta["total"] > 0
                and (strong or (same_url and meta.get("modified")))
            )
            if offset and (not resumable or offset > meta.get("total", 0)):
                logger.info(f"B站 CDN {host}: 无法验证断点版本，从头下载")
                self._discard(path)
                meta, offset = {}, 0
            if offset > max_bytes:
                self._discard(path)
                raise SizeLimitException
            send_headers = request_headers.copy()
            if offset:
                send_headers["Range"] = f"bytes={offset}-"
                send_headers["If-Range"] = etag if strong else meta["modified"]
            started = time.monotonic()
            received = 0
            route = proxy_route(self.cfg, proxy) if debug else ""
            note(
                debug,
                f"CDN请求 stream={stream} cdn={host} "
                f"attempt={attempt + 1}/{self.cfg.download_retry_times + 1} resume_bytes={offset} {route}",
            )
            try:
                async with self._response(url, send_headers, proxy) as response:
                    status = response.status
                    peer = response_peer(response, proxy) if debug else ""
                    note(
                        debug,
                        f"CDN响应 stream={stream} cdn={host} status={status} "
                        f"headers_s={time.monotonic() - started:.2f} {route} {peer}",
                    )
                    if status in (412, 429):
                        raise DownloadException(
                            f"B站 CDN 请求受限（{status}），请稍后重试"
                        )
                    if status == 416:
                        # A complete partial cannot be trusted merely from a 416 reply.
                        self._discard(path)
                        raise Retryable("HTTP416")
                    if status in (401, 403, 410, 567) or status >= 500:
                        raise Retryable(
                            f"HTTP{status}",
                            rotate=True,
                            refresh=status in (401, 403, 410),
                        )
                    if status not in (200, 206):
                        raise DownloadException(f"B站 CDN 下载失败（HTTP {status}）")
                    if (
                        response.headers.get("Content-Encoding", "identity").lower()
                        != "identity"
                    ):
                        raise DownloadException("B站 CDN 返回了无法安全续传的编码")
                    total = response.content_length
                    new_etag = response.headers.get("ETag", "")
                    modified = response.headers.get("Last-Modified", "")
                    expected_end = None
                    if status == 206:
                        match = re.fullmatch(
                            r"bytes (\d+)-(\d+)/(\d+)",
                            response.headers.get("Content-Range", ""),
                        )
                        if not match:
                            self._discard(path)
                            raise Retryable("invalid-range")
                        start, end, total = map(int, match.groups())
                        valid = (
                            start == offset
                            and start <= end < total
                            and (
                                response.content_length is None
                                or response.content_length == end - start + 1
                            )
                        )
                        if offset:
                            valid = valid and total == meta.get("total")
                            valid = valid and (
                                new_etag == etag
                                if strong
                                else modified == meta.get("modified")
                            )
                            if (
                                meta.get("modified")
                                and modified
                                and modified != meta["modified"]
                            ):
                                valid = False
                            if etag and new_etag and new_etag != etag:
                                valid = False
                        if not valid:
                            self._discard(path)
                            raise Retryable("changed-range")
                        expected_end = end + 1
                    else:
                        # Server ignored Range / If-Range changed: TRUNCATE, never append.
                        if offset:
                            logger.info(f"B站 CDN {host}: Range 返回 200，重新写入")
                        offset = 0
                    if total is not None and total > max_bytes:
                        raise SizeLimitException
                    meta = {
                        "resource": resource,
                        "url_hash": url_hash,
                        "total": total,
                        "etag": new_etag,
                        "modified": modified,
                    }
                    progress = TransferProgress(debug, stream, host, offset, total)
                    # Truncate before replacing metadata, including across a crash/reload.
                    with part.open("ab" if offset else "wb") as output:
                        write_json(meta_path, meta)
                        async for chunk in response.content.iter_chunked(256 * 1024):
                            if offset + len(chunk) > max_bytes:
                                raise SizeLimitException
                            output.write(chunk)
                            offset += len(chunk)
                            received += len(chunk)
                            progress.update(offset)
                        output.flush()
                        os.fsync(output.fileno())
                    if expected_end is not None and offset != expected_end:
                        raise Retryable("incomplete-range")
                    if total is not None and offset != total:
                        raise Retryable("incomplete-payload")
                    if not offset:
                        raise ZeroSizeException
                    progress.update(offset, force=True)
                    note(
                        debug,
                        f"流接收完成 stream={stream} bytes={offset} "
                        f"attempt_s={time.monotonic() - started:.2f} "
                        f"received_bytes={received} "
                        f"avg_mib_s={received / max(time.monotonic() - started, 0.001) / 1024**2:.3f} {route}",
                    )
                if validate:
                    try:
                        with stage(debug, f"{stream}完整性校验"):
                            await validate(part)
                    except DownloadException:
                        self._discard(path)
                        raise
                with stage(debug, f"{stream}缓存写入"):
                    await self.publish(part, path, resource)
                meta_path.unlink(missing_ok=True)
                return path
            except (SizeLimitException, ZeroSizeException):
                self._discard(path)
                raise
            except (aiohttp.ClientError, TimeoutError, Retryable) as exc:
                last_error = (
                    exc.code if isinstance(exc, Retryable) else type(exc).__name__
                )
                logger.warning(
                    f"B站 CDN {host}: {last_error} ({attempt + 1}/{self.cfg.download_retry_times + 1})"
                )
                note(
                    debug,
                    f"CDN重试 stream={stream} cdn={host} error={last_error} "
                    f"attempt_s={time.monotonic() - started:.2f} saved_bytes={offset}",
                )
                failures[url_hash] = failures.get(url_hash, 0) + 1
                # Truncated payloads indicate this CDN connection is unhealthy;
                # fail over immediately rather than spending another retry on it.
                rotate = isinstance(exc, Retryable) and exc.rotate
                if (
                    rotate
                    or isinstance(exc, aiohttp.ClientPayloadError)
                    or failures[url_hash] >= 2
                ):
                    index += 1
                # Refresh only expired/auth candidates, once, within the original budget.
                if (
                    isinstance(exc, Retryable)
                    and exc.refresh
                    and refresh
                    and not refreshed
                    and index >= len(urls)
                    and attempt < self.cfg.download_retry_times
                ):
                    refreshed = True
                    urls = list(dict.fromkeys(await refresh()))
                    index = 0
                if attempt < self.cfg.download_retry_times:
                    await asyncio.sleep(min(2**attempt, 8))
        raise DownloadException(
            f"B站媒体下载失败（{last_error}），官方备用节点已尝试或重试次数已用尽"
        ) from None
