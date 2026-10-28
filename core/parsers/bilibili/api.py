"""A small private worker keeps bilibili-api's process globals out of AstrBot.

Only JSON travels over anonymous pipes. Credentials are never command arguments,
environment variables, temporary files or log messages. Calls are serialized; a
cancelled/timed-out RPC kills the worker so its late reply cannot satisfy a new RPC.
"""

import asyncio
import json
import sys
from pathlib import Path

from ...exception import ParseException
from ...vless_proxy import get_proxy


class BilibiliAPI:
    def __init__(self, cfg):
        self.cfg = cfg
        self.process = None
        self.lock = asyncio.Lock()
        self.closed = False
        self.generation = 0

    async def close(self):
        self.closed = True
        await self._stop()

    async def reset(self):
        """Forget any worker-held credentials, including an in-flight operation."""
        self.generation += 1
        await self._stop()

    async def _stop(self):
        process, self.process = self.process, None
        if process is not None:
            if process.returncode is None:
                try:
                    process.kill()
                except ProcessLookupError:
                    pass
            await process.wait()

    async def call(self, action, *, credential=None, **params):
        generation = self.generation
        async with self.lock:
            if self.closed:
                raise ParseException("B站解析器已停止")
            if generation != self.generation:
                raise ParseException("B站旧请求已被退出登录取消，请重新解析")
            options = self.cfg.parser.bilibili
            request = {
                "action": action,
                "params": params,
                "cookies": credential.get_cookies() if credential else {},
                "settings": {
                    "client": options.api_client,
                    "user_agent": options.api_user_agent,
                    "auto_buvid": options.auto_buvid,
                    "timeout": self.cfg.common_timeout,
                    "proxy": (get_proxy(self.cfg) or "") if options.use_proxy else "",
                },
            }
            try:
                async with asyncio.timeout(self.cfg.common_timeout + 5):
                    if self.process is None or self.process.returncode is not None:
                        self.process = await asyncio.create_subprocess_exec(
                            sys.executable,
                            "-u",
                            str(Path(__file__).with_name("api_worker.py")),
                            stdin=asyncio.subprocess.PIPE,
                            stdout=asyncio.subprocess.PIPE,
                            stderr=asyncio.subprocess.DEVNULL,
                            limit=16 * 1024 * 1024,
                        )
                    process = self.process
                    process.stdin.write(json.dumps(request).encode() + b"\n")
                    await process.stdin.drain()
                    line = await process.stdout.readline()
                    response = json.loads(line)
            except asyncio.CancelledError:
                await self._stop()
                raise
            except Exception:
                await self._stop()
                raise ParseException("B站 API 请求失败或超时，请稍后重试") from None
            if "error" in response:
                # The worker only returns an integer status, never exception text.
                code = response.get("code")
                if code in (412, 429, -412, -352, -509):
                    raise ParseException(
                        f"B站请求受限（{code}），请稍后重试并检查 API 配置"
                    )
                suffix = f"（{code}）" if isinstance(code, int) else ""
                raise ParseException(f"B站 API 请求失败{suffix}，请检查网络或登录状态")
            return response["data"]


class BilibiliVideo:
    """Only the four video operations used by this parser, with fresh credentials."""

    def __init__(self, api, login, *, bvid=None, avid=None):
        self.api, self.login = api, login
        self.ids = {"bvid": bvid, "avid": avid}

    async def _call(self, method, **params):
        return await self.api.call(
            "video",
            credential=await self.login.credential,
            method=method,
            **self.ids,
            **params,
        )

    async def get_info(self):
        return await self._call("get_info")

    async def get_cid(self, page_index):
        return await self._call("get_cid", page_index=page_index)

    async def get_ai_conclusion(self, cid):
        return await self._call("get_ai_conclusion", cid=cid)

    async def get_download_url(self, page_index=0):
        return await self._call("get_download_url", page_index=page_index)
