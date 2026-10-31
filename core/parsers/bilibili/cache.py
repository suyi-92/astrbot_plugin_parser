"""Short-lived pointers to verified completed media, without URLs or credentials."""

import hashlib
import json
import math
import re
import time
from dataclasses import dataclass
from pathlib import Path

from astrbot.api import logger

from ...exception import DownloadException
from ...resumable import read_json, write_json

INDEX_TTL = 30 * 60
_BVID = re.compile(r"BV[0-9A-Za-z]{10}\Z")
_IDENTITY = r"[0-9a-f]{24}(?:-[0-9a-f]{24})?"


@dataclass(frozen=True)
class VideoCacheKey:
    bvid: str
    page: int
    fingerprint: str
    # Transient guard only; not part of filenames or persisted fingerprints. This
    # detects logout/login ABA changes while allowing reuse after plugin reload.
    generation: int | None = None

    @classmethod
    def create(cls, bvid, page, quality, codecs, credential, *, generation=None):
        # IDs supplied by metadata are validated too: neither filenames nor resource
        # identities may contain caller-controlled path separators.
        if not isinstance(bvid, str) or not _BVID.fullmatch(bvid):
            raise DownloadException("B站视频标识无效")
        if not isinstance(page, int) or isinstance(page, bool) or page < 1:
            raise DownloadException("B站分 P 标识无效")
        cookies = credential.get_cookies() if credential else {}
        # Hash all account-identifying material, including the login session. A new
        # login/entitlement cannot inherit an old preflight entry for the same UID.
        account = hashlib.sha256(json.dumps(
            [cookies.get("DedeUserID") or cookies.get("dedeuserid") or "",
             cookies.get("SESSDATA") or cookies.get("sessdata") or ""],
            separators=(",", ":"),
        ).encode()).hexdigest()
        fingerprint = hashlib.sha256(json.dumps(
            {"version": 1, "bvid": bvid, "page": page,
             "quality": int(quality), "codecs": list(codecs), "account": account},
            sort_keys=True, separators=(",", ":"),
        ).encode()).hexdigest()
        return cls(bvid, page, fingerprint, generation)

    def index_path(self, directory):
        return directory / f"{self.bvid}-{self.page}-{self.fingerprint}.index.json"

    def filename(self, quality, identity):
        return f"{self.bvid}-{self.page}-q{quality}-{identity}-{self.fingerprint}.mp4"

    def resource(self, identity):
        return f"{self.bvid}:{self.page}:{identity}:{self.fingerprint}"

    def valid_entry(self, entry):
        filename, resource = entry.get("file"), entry.get("resource")
        if not isinstance(filename, str) or not isinstance(resource, str):
            return False
        prefix = re.escape(f"{self.bvid}-{self.page}-q")
        suffix = re.escape(f"-{self.fingerprint}.mp4")
        match = re.fullmatch(prefix + r"\d+-(" + _IDENTITY + ")" + suffix, filename)
        return bool(match and resource == self.resource(match.group(1)))


class VideoCacheIndex:
    def __init__(self, downloader):
        self.downloader = downloader

    @staticmethod
    def _invalidate(index):
        try:
            index.unlink(missing_ok=True)
        except OSError:
            logger.warning("B站无效缓存索引无法清理，继续正常解析")

    async def find(self, key):
        """Caller holds the request key lock through lookup/download/publication."""
        index = key.index_path(self.downloader.directory)
        if index.is_symlink():
            self._invalidate(index)
            return None
        entry = read_json(index)
        created = entry.get("created_at")
        try:
            age = (
                time.time() - created
                if isinstance(created, (int, float)) and not isinstance(created, bool)
                else math.inf
            )
            fresh = math.isfinite(age) and 0 <= age <= INDEX_TTL
        except OverflowError:
            fresh = False
        if entry.get("version") != 1 or not fresh or not key.valid_entry(entry):
            self._invalidate(index)
            return None
        path = self.downloader.directory / entry["file"]
        receipt = Path(str(path) + ".complete.json")
        # Never follow a poisoned cache pointer/receipt outside the cache directory.
        if path.is_symlink() or receipt.is_symlink():
            self._invalidate(index)
            return None
        try:
            if (
                path.resolve().parent == self.downloader.directory.resolve()
                and path.stat().st_size <= self.downloader.cfg.max_size
                and await self.downloader.cached(path, entry["resource"])
            ):
                return path
        except OSError:
            pass
        self._invalidate(index)
        return None

    def store(self, key, path, resource):
        entry = {"version": 1, "file": path.name, "resource": resource,
                 "created_at": time.time()}
        if not key.valid_entry(entry):
            return
        try:
            write_json(key.index_path(self.downloader.directory), entry)
        except OSError:
            # This optimization is optional; a verified download remains usable.
            logger.warning("B站缓存索引无法写入，请检查目录权限和磁盘空间")
