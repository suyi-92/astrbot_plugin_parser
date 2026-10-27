import asyncio
import io
import json
import os
import tempfile
import time
from collections.abc import AsyncGenerator

import qrcode
from bilibili_api import Credential
from aiohttp import ClientSession, ClientTimeout

from ...exception import ParseException

from astrbot.api import logger

from ...config import PluginConfig

QR_GENERATE_URL = "https://passport.bilibili.com/x/passport-login/web/qrcode/generate"
QR_POLL_URL = "https://passport.bilibili.com/x/passport-login/web/qrcode/poll"

# 轮询接口返回的状态码
QR_STATUS_SUCCESS = 0
QR_STATUS_SCAN = 86101
QR_STATUS_CONFIRM = 86090
QR_STATUS_EXPIRED = 86038

CREDENTIAL_KEYS = ("SESSDATA", "bili_jct", "DedeUserID")


def parse_qr_login_cookies(
    payload: dict, set_cookie_headers: list[str] | None = None
) -> dict[str, str]:
    """从扫码成功的轮询响应中提取登录 Cookies

    旧接口把 Cookies 拼在 `url` 查询参数里, 新接口改为通过 `Set-Cookie` 响应头下发,
    两种格式都解析。
    """
    cookies: dict[str, str] = {}

    # 1) url 查询参数: 部分返回以 `&amp;` 拼接, 参数名会残留 `amp;` 前缀
    query = (payload.get("url") or "").partition("?")[2]
    for chunk in query.split("&"):
        name, _, value = chunk.partition("=")
        name = name.strip().removeprefix("amp;")
        if name in CREDENTIAL_KEYS and value:
            cookies.setdefault(name, value.strip())

    # 2) Set-Cookie 响应头
    for header in set_cookie_headers or []:
        name, _, value = header.partition("=")
        name = name.strip()
        if name in CREDENTIAL_KEYS and value:
            cookies.setdefault(name, value.split(";")[0].strip())

    return cookies


class BilibiliLogin:
    """Local credentials with generation-checked QR/refresh replies."""

    def __init__(self, config: PluginConfig, api=None):
        self.cfg = config
        self.api = api
        self.credential_file = config.data_dir / "cookies" / "bilibili_credential.json"
        self.options = getattr(config.parser, "bilibili", None)
        self.raw_cookies = (self.options.cookies if self.options else "") or ""
        self._credential: Credential | None = None
        self._qr_key: str | None = None
        self._generation = 0
        self._initialized = False
        self._next_refresh = 0
        self._requests = set()
        self._closed = False

    def _save_credential(self):
        if self._credential is None or not self._credential.has_sessdata():
            return
        path = self.credential_file
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = None
        try:
            fd, tmp = tempfile.mkstemp(prefix=".bilibili-", dir=path.parent)
            with os.fdopen(fd, "w", encoding="utf-8") as file:
                os.fchmod(file.fileno(), 0o600)
                json.dump(
                    {k: v for k, v in self._credential.get_cookies().items() if v}, file
                )
                file.flush()
                os.fsync(file.fileno())
            os.replace(tmp, path)
        except OSError:
            raise ParseException("B站凭证保存失败，请检查目录权限及磁盘空间") from None
        finally:
            if tmp and os.path.exists(tmp):
                os.unlink(tmp)

    def _load_credential(self):
        try:
            cookies = json.loads(self.credential_file.read_text(encoding="utf-8"))
            cookies["SESSDATA"] = cookies.get("SESSDATA") or cookies.get("sessdata", "")
            cookies["DedeUserID"] = cookies.get("DedeUserID") or cookies.get(
                "dedeuserid", ""
            )
            credential = Credential.from_cookies(cookies)
            if credential.has_sessdata():
                self._credential = credential
                self.credential_file.chmod(0o600)
        except FileNotFoundError:
            pass
        except (OSError, ValueError, TypeError, AttributeError):
            logger.warning("B站本地凭证无法读取；继续匿名解析，请管理员检查凭证文件")

    @staticmethod
    def _cookies_to_dict(cookies_str):
        result = {}
        for item in cookies_str.split(";"):
            name, sep, value = item.strip().partition("=")
            if sep and name:
                result[name] = value
        return result

    def _init_local(self):
        if self._initialized:
            return
        self._initialized = True
        if self.raw_cookies:
            credential = Credential.from_cookies(
                self._cookies_to_dict(self.raw_cookies)
            )
            if credential.has_sessdata():
                self._credential = credential
        if self._credential is None:
            self._load_credential()

    @property
    async def credential(self):
        if self._closed:
            return None
        self._init_local()
        credential, generation = self._credential, self._generation
        if (
            credential
            and self.api
            and credential.has_ac_time_value()
            and credential.has_bili_jct()
            and time.monotonic() >= self._next_refresh
        ):
            self._next_refresh = time.monotonic() + 300
            try:
                cookies = await self.api.call(
                    "credential_refresh", credential=credential
                )
                if generation != self._generation:
                    return self._credential
                self._credential = Credential.from_cookies(cookies)
                self._save_credential()
            except ParseException:
                logger.warning("B站凭证刷新暂不可用，继续使用本地凭证")
        return self._credential

    def has_local_credential(self):
        self._init_local()
        return bool(self._credential and self._credential.has_sessdata())

    async def _cancel_qr(self):
        self._generation += 1
        self._qr_key = None
        pending = list(self._requests)
        for task in pending:
            task.cancel()
        await asyncio.gather(*pending, return_exceptions=True)

    async def close(self):
        self._closed = True
        await self._cancel_qr()
        self._credential = None  # Unload does NOT delete persisted credentials.

    async def logout(self):
        existed = (
            self.has_local_credential()
            or bool(self.raw_cookies)
            or self.credential_file.exists()
        )
        # Invalidate before the first await; late QR and refresh replies cannot write.
        self._credential = None
        self._initialized = True
        self.raw_cookies = ""
        if self.options:
            self.options.cookies = ""
        await self._cancel_qr()
        if self.api:
            await self.api.reset()
        failed = False
        try:
            self.credential_file.unlink(missing_ok=True)
        except OSError:
            failed = True
        try:
            self.cfg.save_config()
        except Exception:
            failed = True
        if failed:
            raise ParseException(
                "B站内存凭证及扫码已清除，但本地文件删除或配置保存失败；请检查权限后重试"
            )
        return existed

    async def _qr_request(self, url, params=None):
        async def request():
            options = self.options
            if options is None:
                raise ParseException("请先在插件配置中添加B站解析器")
            headers = {
                "User-Agent": options.api_user_agent,
                "Referer": "https://www.bilibili.com/",
            }
            async with ClientSession(
                headers=headers, timeout=ClientTimeout(total=15)
            ) as session:
                async with session.get(
                    url,
                    params=params,
                    allow_redirects=False,
                    proxy=self.cfg.proxy if options.use_proxy else None,
                ) as response:
                    if response.status != 200:
                        raise ParseException(
                            f"B站扫码请求失败（HTTP {response.status}），请稍后重试"
                        )
                    result = await response.json()
                    if result.get("code") != 0:
                        raise ParseException("B站扫码接口暂不可用，请稍后重试")
                    return result.get("data") or {}, response.headers.getall(
                        "Set-Cookie", []
                    )

        task = asyncio.create_task(request(), name="bilibili-qr")
        self._requests.add(task)
        try:
            return await task
        except asyncio.CancelledError:
            raise
        except ParseException:
            raise
        except Exception:
            raise ParseException("B站扫码请求失败或超时，请稍后重试") from None
        finally:
            self._requests.discard(task)

    async def login_with_qrcode(self) -> bytes:
        await self._cancel_qr()
        generation = self._generation
        payload, _ = await self._qr_request(QR_GENERATE_URL)
        if generation != self._generation or self._closed:
            raise ParseException("B站扫码登录已取消")
        url, key = payload.get("url"), payload.get("qrcode_key")
        if not url or not key:
            raise ParseException("获取B站登录二维码失败")
        self._qr_key = key
        qr = qrcode.QRCode(
            error_correction=qrcode.constants.ERROR_CORRECT_M, box_size=10, border=2
        )
        qr.add_data(url)
        qr.make(fit=True)
        buffer = io.BytesIO()
        qr.make_image(fill_color="black", back_color="white").save(buffer, format="PNG")
        return buffer.getvalue()

    async def check_qr_state(self) -> AsyncGenerator[str, None]:
        key, generation = self._qr_key, self._generation
        if not key:
            yield "未找到二维码，请重新生成"
            return
        pending = True
        try:
            for _ in range(30):
                if generation != self._generation or self._closed:
                    return
                payload, headers = await self._qr_request(
                    QR_POLL_URL, {"qrcode_key": key}
                )
                if generation != self._generation or self._closed:
                    return
                status = payload.get("code")
                if status == QR_STATUS_SUCCESS:
                    cookies = parse_qr_login_cookies(payload, headers)
                    if not cookies.get("SESSDATA"):
                        yield "扫码成功但未获取到本地凭证，请重新登录"
                        return
                    self._credential = Credential(
                        sessdata=cookies["SESSDATA"],
                        bili_jct=cookies.get("bili_jct", ""),
                        dedeuserid=cookies.get("DedeUserID", ""),
                        ac_time_value=payload.get("refresh_token") or "",
                    )
                    self._initialized = True
                    self._save_credential()
                    # Old configured cookies must not override this login on reload.
                    if self.raw_cookies:
                        self.raw_cookies = ""
                        self.cfg.parser.bilibili.cookies = ""
                        try:
                            self.cfg.save_config()
                        except Exception:
                            yield "B站凭证已保存，但旧 Cookie 配置未能保存为空，请检查配置权限"
                            return
                    self._qr_key = None
                    yield "B站登录成功，本地凭证已保存"
                    return
                if status == QR_STATUS_CONFIRM and pending:
                    yield "二维码已扫描，请确认登录"
                    pending = False
                elif status == QR_STATUS_EXPIRED:
                    yield "二维码过期，请重新生成"
                    return
                await asyncio.sleep(2)
            yield "二维码登录超时，请重新生成"
        finally:
            if generation == self._generation:
                self._qr_key = None
