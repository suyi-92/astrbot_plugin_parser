"""Selection describes the returned stream, never the requested quality label."""

import hashlib
import json
from dataclasses import dataclass
from urllib.parse import urlsplit

from ...exception import DownloadException
from ...resumable import validate_media_url

BLOCKED_PCDN_DOMAINS = (
    "mcdn.bilivideo.com",
    "mcdn.bilivideo.cn",
    "szbdyd.com",
    "mountaintoys.cn",
    "nexusedgeio.com",
    "ahdohpiechei.com",
)
LOW_PRIORITY_DOMAINS = ("upos-sz-mirrorcosov.bilivideo.com",)


def _is_pcdn_url(url):
    host = (urlsplit(url).hostname or "").lower()
    return any(host == d or host.endswith("." + d) for d in BLOCKED_PCDN_DOMAINS)


def candidates(raw, block_pcdn):
    base = raw.get("base_url") or raw.get("baseUrl") or raw.get("url")
    backups = raw.get("backup_url") or raw.get("backupUrl") or []
    if isinstance(backups, str):
        backups = [backups]
    result = []
    for url in [base, *backups]:
        if not isinstance(url, str) or not url or url in result:
            continue
        try:
            validate_media_url(url)
        except DownloadException:
            continue
        if not block_pcdn or not _is_pcdn_url(url):
            result.append(url)
    result.sort(key=lambda u: (urlsplit(u).hostname or "") in LOW_PRIORITY_DOMAINS)
    if not result:
        raise DownloadException("B站没有可用的安全 CDN 地址（请检查 block_pcdn 设置）")
    return result


def codec_name(raw):
    codec = str(raw.get("codecs") or "").lower()
    if codec.startswith(("avc", "h264")):
        return "AVC"
    if codec.startswith(("hev", "hvc")):
        return "HEV"
    if codec.startswith(("av01", "av1")):
        return "AV1"
    return {7: "AVC", 12: "HEV", 13: "AV1"}.get(raw.get("codecid"), "UNKNOWN")


@dataclass
class Stream:
    urls: list[str]
    identity: str
    info: dict


@dataclass
class Selection:
    video: Stream
    audio: Stream | None
    duration: float

    @property
    def identity(self):
        return self.video.identity + ("-" + self.audio.identity if self.audio else "")


def make_stream(raw, duration, block_pcdn, quality=None):
    urls = candidates(raw, block_pcdn)
    info = {
        "quality": quality if quality is not None else raw.get("id"),
        "width": raw.get("width", 0),
        "height": raw.get("height", 0),
        "fps": raw.get("frame_rate") or raw.get("frameRate") or "unknown",
        "codec": raw.get("codecs") or codec_name(raw),
        "bandwidth": raw.get("bandwidth", 0),
        "estimated_bytes": int(
            raw.get("size") or (raw.get("bandwidth", 0) * duration / 8)
        ),
    }
    # Include the original media path's digest, but never its signed query or host.
    base = raw.get("base_url") or raw.get("baseUrl") or raw.get("url") or urls[0]
    identity = hashlib.sha256(
        json.dumps(
            {
                **info,
                "path": hashlib.sha256(urlsplit(base).path.encode()).hexdigest(),
                "segments": raw.get("SegmentBase") or raw.get("segment_base"),
            },
            sort_keys=True,
        ).encode()
    ).hexdigest()[:24]
    return Stream(urls, identity, info)


def select_streams(data, max_quality, codecs, block_pcdn, duration):
    dash = data.get("dash") or {}
    duration = float(
        dash.get("duration") or duration or data.get("timelength", 0) / 1000
    )
    if dash:
        videos = [
            v
            for v in dash.get("video", [])
            if int(v["id"]) <= max_quality
            and v["id"] not in (125, 126)
            and codec_name(v) in codecs
        ]
        if not videos:
            raise DownloadException("B站没有符合画质上限和编码设置的视频流")
        raw = max(videos, key=lambda v: (v["id"], -codecs.index(codec_name(v))))
        audios = dash.get("audio") or []
        if not audios:
            raise DownloadException("B站 DASH 音频流缺失，无法合并完整视频")
        audio = max(audios, key=lambda a: a.get("bandwidth", 0))
        return Selection(
            make_stream(raw, duration, block_pcdn),
            make_stream(audio, duration, block_pcdn),
            duration,
        )
    durl = data.get("durl") or []
    quality = int(data.get("quality") or 0)
    if (
        len(durl) != 1
        or not quality
        or quality > max_quality
        or data.get("format", "").startswith("flv")
    ):
        raise DownloadException("B站未返回受支持的完整 MP4/DASH 流或超过画质上限")
    return Selection(
        make_stream(durl[0], duration, block_pcdn, quality), None, duration
    )
