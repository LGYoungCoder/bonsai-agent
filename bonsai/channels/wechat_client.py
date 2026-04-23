"""Minimal client for Tencent's iLink WeChat bot protocol.

Talks to https://ilinkai.weixin.qq.com directly over HTTPS. Text-only for
this first cut — media upload/download uses AES-ECB envelope which is out
of scope here. Tokens persist to a project-local file so restarts skip QR.

Protocol endpoints:
  GET  /ilink/bot/get_bot_qrcode?bot_type=3   → QR id + image URL
  GET  /ilink/bot/get_qrcode_status?qrcode=…  → scan / confirm lifecycle
  POST /ilink/bot/getupdates                  → long-poll inbound msgs
  POST /ilink/bot/sendmessage                 → outbound (text now, media later)
  POST /ilink/bot/sendtyping                  → typing indicator
"""
from __future__ import annotations

import base64
import hashlib
import json
import logging
import os
import struct
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable
from urllib.parse import quote

import httpx

log = logging.getLogger(__name__)

API = "https://ilinkai.weixin.qq.com"
CDN_BASE = "https://novac2c.cdn.weixin.qq.com/c2c"
CHANNEL_VERSION = "2.1.8"


class WxSessionExpired(RuntimeError):
    """Raised when iLink returns errcode=-14 session-timeout on a clean
    buf — meaning bot_token itself is invalid and user must re-scan QR."""
MSG_USER = 1
MSG_BOT = 2
ITEM_TEXT = 1
ITEM_IMAGE = 2
ITEM_FILE = 4
ITEM_VIDEO = 5
STATE_FINISH = 2
_MEDIA_KEYS = {"image_item": ".jpg", "video_item": ".mp4",
                "file_item": "", "voice_item": ".silk"}


def _uin() -> str:
    """Random per-request X-WECHAT-UIN header."""
    return base64.b64encode(str(struct.unpack(">I", os.urandom(4))[0]).encode()).decode()


@dataclass
class WxToken:
    bot_token: str = ""
    ilink_bot_id: str = ""
    updates_buf: str = ""
    login_time: str = ""

    @classmethod
    def load(cls, path: Path) -> "WxToken":
        if not path.exists():
            return cls()
        d = json.loads(path.read_text(encoding="utf-8"))
        return cls(
            bot_token=d.get("bot_token", ""),
            ilink_bot_id=d.get("ilink_bot_id", ""),
            updates_buf=d.get("updates_buf", ""),
            login_time=d.get("login_time", ""),
        )

    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(self.__dict__, ensure_ascii=False, indent=2),
                         encoding="utf-8")


@dataclass
class QrLogin:
    qr_id: str
    qr_image_url: str
    status: str = "pending"
    bot_token: str = ""
    ilink_bot_id: str = ""


class WxBotClient:
    def __init__(self, token_file: Path) -> None:
        self._tf = token_file
        self.t = WxToken.load(token_file)
        self._last_errcode: int = 0   # for log-on-change throttling
        self._consec_err: int = 0     # for backoff on repeated errcodes

    # ─────────── login ───────────

    def login_qr_start(self) -> QrLogin:
        r = httpx.get(f"{API}/ilink/bot/get_bot_qrcode",
                       params={"bot_type": 3}, timeout=10)
        r.raise_for_status()
        d = r.json()
        return QrLogin(qr_id=d["qrcode"], qr_image_url=d.get("qrcode_img_content", ""))

    def login_qr_poll(self, qr_id: str) -> QrLogin:
        """One-shot poll. Returns pending / scanned / confirmed / expired."""
        r = httpx.get(f"{API}/ilink/bot/get_qrcode_status",
                       params={"qrcode": qr_id}, timeout=60)
        r.raise_for_status()
        s = r.json()
        status = s.get("status", "pending") or "pending"
        out = QrLogin(qr_id=qr_id, qr_image_url="", status=status)
        if status == "confirmed":
            out.bot_token = s.get("bot_token", "")
            out.ilink_bot_id = s.get("ilink_bot_id", "")
            self.t.bot_token = out.bot_token
            self.t.ilink_bot_id = out.ilink_bot_id
            # 新 token,旧的 updates_buf 肯定不属于这个 session,清掉免得首轮白白 -14。
            self.t.updates_buf = ""
            self.t.login_time = time.strftime("%Y-%m-%d %H:%M:%S")
            self.t.save(self._tf)
        return out

    def login_qr_blocking(self, on_qr: Callable[[str, str], None],
                          on_status: Callable[[str], None] | None = None,
                          poll_interval: float = 2.0) -> QrLogin:
        """Synchronous QR login — runs QR callback once, then polls status."""
        qr = self.login_qr_start()
        on_qr(qr.qr_id, qr.qr_image_url)
        last_status = ""
        while True:
            time.sleep(poll_interval)
            cur = self.login_qr_poll(qr.qr_id)
            if cur.status != last_status:
                if on_status:
                    on_status(cur.status)
                last_status = cur.status
            if cur.status == "confirmed":
                return cur
            if cur.status == "expired":
                raise RuntimeError("二维码过期,请重新扫码")

    # ─────────── messaging ───────────

    @property
    def logged_in(self) -> bool:
        return bool(self.t.bot_token)

    def _headers(self) -> dict:
        h = {
            "Content-Type": "application/json",
            "AuthorizationType": "ilink_bot_token",
            "X-WECHAT-UIN": _uin(),
        }
        if self.t.bot_token:
            h["Authorization"] = f"Bearer {self.t.bot_token}"
        return h

    def _post(self, ep: str, body: dict, timeout: float = 15) -> dict:
        r = httpx.post(f"{API}/{ep}", json=body, headers=self._headers(), timeout=timeout)
        # 401/403 = bot_token 真的被 iLink 废掉了(很少见,一般得重扫码)。
        if r.status_code in (401, 403):
            self.t.bot_token = ""
            self.t.save(self._tf)
            raise WxSessionExpired(
                f"iLink 拒绝当前 bot_token(HTTP {r.status_code}),请重新扫码登录。")
        r.raise_for_status()
        return r.json()

    def get_updates(self, timeout: float = 30) -> list[dict]:
        body = {"get_updates_buf": self.t.updates_buf or "",
                "base_info": {"channel_version": CHANNEL_VERSION}}
        try:
            resp = self._post("ilink/bot/getupdates", body, timeout=timeout + 5)
        except httpx.ReadTimeout:
            return []
        errcode = resp.get("errcode")
        if errcode:
            errmsg = resp.get("errmsg") or ""
            # 每次 errcode 都 print 出来 — 节流只会掩盖「-14 持续发生」这种致命情况。
            # ga 也是无节流直接 print,用户可接受偶发刷屏。
            print(f"[wechat] getUpdates err: errcode={errcode} errmsg={errmsg!r}", flush=True)
            if errcode == -14:
                # -14 = 轮询游标过期。按协议清空 buf 重新建 session,token 保留。
                self.t.updates_buf = ""
                self.t.save(self._tf)
            self._last_errcode = errcode
            # 服务端立即报错就不走长轮询 → 按连续次数小退避防 flood。
            self._consec_err += 1
            time.sleep(min(30.0, 2.0 ** min(self._consec_err, 4)))
            return []
        self._last_errcode = 0
        self._consec_err = 0
        nb = resp.get("get_updates_buf", "")
        if nb:
            self.t.updates_buf = nb
            self.t.save(self._tf)
        return resp.get("msgs") or []

    def send_text(self, to_user_id: str, text: str, context_token: str = "",
                  *, retries: int = 2) -> dict:
        """Send a plain text message.
        Checks errcode in the response (iLink returns business errors as
        {errcode: N, errmsg: ...} with HTTP 200), retries transient ones
        with backoff, raises RuntimeError if all retries fail. Previously
        the runner treated all 200s as success, so context-token-expired
        / rate-limited sends silently vanished → user sees no reply.
        """
        last_resp: dict = {}
        for attempt in range(retries + 1):
            msg = {
                "from_user_id": "",
                "to_user_id": to_user_id,
                "client_id": f"bonsai-{uuid.uuid4().hex[:16]}",
                "message_type": MSG_BOT,
                "message_state": STATE_FINISH,
                "item_list": [{"type": ITEM_TEXT, "text_item": {"text": text}}],
            }
            if context_token:
                msg["context_token"] = context_token
            resp = self._post(
                "ilink/bot/sendmessage",
                {"msg": msg, "base_info": {"channel_version": CHANNEL_VERSION}},
            )
            last_resp = resp
            errcode = resp.get("errcode")
            if not errcode:
                return resp                                  # success
            # Transient: retry with small backoff. Permanent (e.g. -14
            # context-token expired): retry once without the token.
            log.warning("send_text errcode=%s errmsg=%r attempt=%d/%d",
                        errcode, resp.get("errmsg", ""),
                        attempt + 1, retries + 1)
            if attempt < retries:
                if errcode in (-14, -50010) and context_token:
                    context_token = ""                       # drop expired
                time.sleep(0.8 * (attempt + 1))
        raise RuntimeError(
            f"send_text failed after {retries + 1} attempts: "
            f"errcode={last_resp.get('errcode')} errmsg={last_resp.get('errmsg')!r}")

    def send_typing(self, to_user_id: str, typing_ticket: str = "",
                    cancel: bool = False) -> dict:
        return self._post("ilink/bot/sendtyping", {
            "to_user_id": to_user_id,
            "typing_ticket": typing_ticket,
            "typing_status": 2 if cancel else 1,
            "base_info": {"channel_version": CHANNEL_VERSION},
        })

    # ─────────── media send (image / file / video) ───────────

    def _enc(self, raw: bytes, aes_key: bytes) -> bytes:
        """AES-ECB + PKCS7. Lazy import so non-channels installs don't need it."""
        from Crypto.Cipher import AES
        pad = 16 - (len(raw) % 16)
        return AES.new(aes_key, AES.MODE_ECB).encrypt(raw + bytes([pad] * pad))

    def _dec(self, ct: bytes, aes_key: bytes) -> bytes:
        from Crypto.Cipher import AES
        pt = AES.new(aes_key, AES.MODE_ECB).decrypt(ct)
        return pt[:-pt[-1]]

    def _upload(self, filekey: str, upload_param: str, raw: bytes,
                aes_key: bytes, upload_url: str = "",
                timeout: float = 120) -> dict:
        url = upload_url.strip() if upload_url else (
            f"{CDN_BASE}/upload?encrypted_query_param={quote(upload_param)}&filekey={filekey}"
        )
        data = self._enc(raw, aes_key)
        last_err: Exception | None = None
        for attempt in range(3):
            try:
                r = httpx.post(url, content=data,
                                headers={"Content-Type": "application/octet-stream"},
                                timeout=timeout)
                if 400 <= r.status_code < 500:
                    msg = r.headers.get("x-error-message") or r.text[:300]
                    raise RuntimeError(f"CDN upload client error {r.status_code}: {msg}")
                if r.status_code != 200:
                    raise RuntimeError(
                        f"CDN upload server error: "
                        f"{r.headers.get('x-error-message') or r.status_code}")
                eq = r.headers.get("x-encrypted-param", "")
                if not eq:
                    raise RuntimeError("CDN upload missing x-encrypted-param header")
                return {
                    "encrypt_query_param": eq,
                    "aes_key": base64.b64encode(aes_key.hex().encode()).decode(),
                    "encrypt_type": 1,
                }
            except Exception as e:
                last_err = e
                if "client error" in str(e) or attempt >= 2:
                    break
                log.warning("CDN upload retry %d: %s", attempt + 1, e)
        raise last_err  # type: ignore[misc]

    def _send_media(self, to_user_id: str, file_path: Path,
                    media_type: int, item_type: int, item_key: str,
                    context_token: str = "") -> dict:
        fp = Path(file_path)
        raw = fp.read_bytes()
        filekey = uuid.uuid4().hex
        aes_key = os.urandom(16)
        ciphertext_size = ((len(raw) // 16) + 1) * 16
        body = {
            "filekey": filekey,
            "media_type": media_type,
            "to_user_id": to_user_id,
            "rawsize": len(raw),
            "rawfilemd5": hashlib.md5(raw).hexdigest(),
            "filesize": ciphertext_size,
            "no_need_thumb": True,
            "aeskey": aes_key.hex(),
            "base_info": {"channel_version": CHANNEL_VERSION},
        }
        resp = self._post("ilink/bot/getuploadurl", body)
        upload_param = resp.get("upload_param", "")
        upload_url = resp.get("upload_full_url", "")
        if not (upload_param or upload_url):
            raise RuntimeError(f"getuploadurl failed: {resp}")
        media = self._upload(filekey, upload_param, raw,
                              aes_key=aes_key, upload_url=upload_url)
        item: dict = {"media": media}
        if item_key == "file_item":
            item.update({"file_name": fp.name, "len": str(len(raw))})
        elif item_key == "image_item":
            item.update({"mid_size": ciphertext_size})
        elif item_key == "video_item":
            item.update({"video_size": ciphertext_size})
        msg = {
            "from_user_id": "",
            "to_user_id": to_user_id,
            "client_id": f"bonsai-{uuid.uuid4().hex[:16]}",
            "message_type": MSG_BOT,
            "message_state": STATE_FINISH,
            "item_list": [{"type": item_type, item_key: item}],
        }
        if context_token:
            msg["context_token"] = context_token
        return self._post("ilink/bot/sendmessage",
                          {"msg": msg, "base_info": {"channel_version": CHANNEL_VERSION}})

    def send_file(self, to_user_id: str, file_path: Path, context_token: str = "") -> dict:
        return self._send_media(to_user_id, file_path, 3, ITEM_FILE, "file_item", context_token)

    def send_image(self, to_user_id: str, file_path: Path, context_token: str = "") -> dict:
        return self._send_media(to_user_id, file_path, 1, ITEM_IMAGE, "image_item", context_token)

    def send_video(self, to_user_id: str, file_path: Path, context_token: str = "") -> dict:
        return self._send_media(to_user_id, file_path, 2, ITEM_VIDEO, "video_item", context_token)

    # ─────────── media receive ───────────

    def download_media(self, msg: dict, save_dir: Path) -> list[Path]:
        """Decrypt and save every media item in `msg` to `save_dir`. Returns
        the list of saved paths (empty if no media / all failed)."""
        save_dir.mkdir(parents=True, exist_ok=True)
        saved: list[Path] = []
        for item in msg.get("item_list", []):
            for key, ext in _MEDIA_KEYS.items():
                sub = item.get(key)
                if not sub:
                    continue
                media = sub.get("media") or {}
                eq = media.get("encrypt_query_param")
                if not eq:
                    continue
                ak_b = media.get("aes_key") or sub.get("aeskey", "")
                if not ak_b:
                    continue
                try:
                    aes_key = (bytes.fromhex(base64.b64decode(ak_b).decode())
                               if media.get("aes_key") else bytes.fromhex(ak_b))
                    ct = httpx.get(f"{CDN_BASE}/download?encrypted_query_param={quote(eq)}",
                                    timeout=60).content
                    pt = self._dec(ct, aes_key)
                    fname = sub.get("file_name") or f"{uuid.uuid4().hex[:8]}{ext or '.bin'}"
                    path = save_dir / fname
                    path.write_bytes(pt)
                    saved.append(path)
                    log.info("media saved: %s (%d bytes)", path.name, len(pt))
                except Exception as e:
                    log.error("media decrypt failed (%s): %s", key, e)
                break   # one media per item
        return saved

    # ─────────── helpers for parsing inbound ───────────

    @staticmethod
    def extract_text(msg: dict) -> str:
        return "\n".join(
            it.get("text_item", {}).get("text", "")
            for it in msg.get("item_list", [])
            if it.get("type") == ITEM_TEXT and it.get("text_item")
        )

    @staticmethod
    def is_user_msg(msg: dict) -> bool:
        return msg.get("message_type") == MSG_USER
