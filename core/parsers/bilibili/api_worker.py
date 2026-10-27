"""Standalone, private bilibili-api worker. Do not import AstrBot here."""

import asyncio
import json
import sys


def configure(settings):
    from bilibili_api import request_settings, select_client
    from bilibili_api.utils.network import HEADERS, request_log

    client = settings.get("client", "aiohttp")
    if client not in ("aiohttp", "httpx", "curl_cffi"):
        raise ValueError("unsupported client")
    select_client(client)
    request_settings.set_enable_auto_buvid(bool(settings.get("auto_buvid", False)))
    request_settings.set_wbi_retry_times(1)  # No hidden risk-control retry loop.
    request_settings.set_enable_bili_ticket(False)
    request_settings.set_timeout(settings.get("timeout", 15))
    request_settings.set_proxy(settings.get("proxy", ""))
    request_settings.set_trust_env(False)
    if client == "curl_cffi":
        request_settings.set("impersonate", "chrome131")
    HEADERS["User-Agent"] = settings.get("user_agent") or "curl/8.14.1"
    request_log.set_on(False)


async def dispatch(request):
    from bilibili_api import Credential

    configure(request["settings"])
    credential = Credential.from_cookies(request.get("cookies") or {})
    params = request.get("params") or {}
    action = request["action"]
    if action == "video":
        from bilibili_api.video import Video

        video = Video(
            bvid=params.get("bvid"), aid=params.get("avid"), credential=credential
        )
        method = params["method"]
        if method == "get_info":
            return await video.get_info()
        if method == "get_cid":
            return await video.get_cid(params["page_index"])
        if method == "get_ai_conclusion":
            return await video.get_ai_conclusion(params["cid"])
        if method == "get_download_url":
            return await video.get_download_url(page_index=params["page_index"])
    elif action == "dynamic":
        from bilibili_api.dynamic import Dynamic

        return await Dynamic(params["id"], credential).get_info()
    elif action == "opus":
        from bilibili_api.opus import Opus

        return await Opus(params["id"], credential).get_info()
    elif action == "article":
        from bilibili_api.article import Article

        opus = await Article(params["id"], credential=credential).turn_to_opus()
        return await opus.get_info()
    elif action == "live":
        from bilibili_api.live import LiveRoom

        return await LiveRoom(
            room_display_id=params["id"], credential=credential
        ).get_room_info()
    elif action == "favlist":
        from bilibili_api.favorite_list import get_video_favorite_list_content

        return await get_video_favorite_list_content(
            params["id"], credential=credential
        )
    elif action == "credential_refresh":
        if credential.has_ac_time_value() and credential.has_bili_jct():
            if await credential.check_refresh():
                await credential.refresh()
        return credential.get_cookies()
    elif action == "settings":  # Offline health check; no secrets in this reply.
        from bilibili_api.utils.network import (
            HEADERS,
            get_selected_client,
            request_settings,
        )

        return {
            "client": get_selected_client()[0],
            "user_agent": HEADERS["User-Agent"],
            "auto_buvid": request_settings.get_enable_auto_buvid(),
        }
    raise ValueError("unsupported operation")


async def main():
    try:
        while line := await asyncio.to_thread(sys.stdin.buffer.readline):
            try:
                request = json.loads(line)
                async with asyncio.timeout(request["settings"].get("timeout", 15)):
                    response = {"data": await dispatch(request)}
            except Exception as exc:
                code = getattr(exc, "code", getattr(exc, "status", None))
                response = {
                    "error": True,
                    "code": code if isinstance(code, int) else None,
                }
            sys.stdout.write(json.dumps(response, ensure_ascii=False) + "\n")
            sys.stdout.flush()
            del response
            request = None
    finally:
        from bilibili_api.utils.network import session_pool

        for pool in session_pool.values():
            for client in pool.values():
                await client.close()


if __name__ == "__main__":
    asyncio.run(main())
