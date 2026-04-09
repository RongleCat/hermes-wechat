"""
Hermes Agent — WeChat (iLink) Platform Adapter
===============================================

Connects Hermes Agent to WeChat personal accounts via the iLink Bot API
(the same protocol used by the official openclaw-weixin TypeScript plugin).

Architecture:
  - Long-poll getUpdates loop → inbound messages → agent dispatch
  - sendMessage / sendTyping for outbound text
  - AES-128-ECB CDN upload for image / video / file attachments
  - context_token persisted per (account, user) pair across restarts
  - QR-code login with automatic refresh on expiry

Drop-in instructions
--------------------
1. Copy this file to  hermes-agent/gateway/platforms/weixin.py
2. Patch gateway/config.py  (see INSTALL.md)
3. Patch gateway/run.py     (see INSTALL.md)
4. Install deps:  uv add aiohttp cryptography qrcode pillow

Configuration (config.yaml):
    platforms:
      weixin:
        enabled: true
        extra:
          # Auto-filled by `hermes channels login --channel weixin`
          # Manual override possible:
          # base_url: "https://ilinkai.weixin.qq.com"
          # cdn_base_url: "https://novac2c.cdn.weixin.qq.com/c2c"
          # dm_policy: "open"          # open | allowlist | pairing
          # allow_from: ["xxx@im.wechat"]

Environment variables (alternative / override):
    WEIXIN_TOKEN        Bot token from QR login
    WEIXIN_ACCOUNT_ID   Account ID (ilink_bot_id) from QR login
    WEIXIN_BASE_URL     API base URL (default: https://ilinkai.weixin.qq.com)
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import logging
import mimetypes
import os
import secrets
import struct
import tempfile
import time
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

# ── iLink protocol constants ──────────────────────────────────────────────────

ILINK_BASE_URL      = "https://ilinkai.weixin.qq.com"
CDN_BASE_URL        = "https://novac2c.cdn.weixin.qq.com/c2c"
ILINK_APP_ID        = "bot"
CHANNEL_VERSION     = "2.1.3"          # mirrors package.json version

# iLink-App-ClientVersion: 0x00MMNNPP (2.1.3 → 0x00020103 = 131331)
ILINK_APP_CLIENT_VERSION = (2 << 16) | (1 << 8) | 3

# API endpoints (relative to base_url)
EP_GET_UPDATES   = "ilink/bot/getupdates"
EP_SEND_MESSAGE  = "ilink/bot/sendmessage"
EP_SEND_TYPING   = "ilink/bot/sendtyping"
EP_GET_CONFIG    = "ilink/bot/getconfig"
EP_GET_UPLOAD_URL = "ilink/bot/getuploadurl"
EP_GET_BOT_QR    = "ilink/bot/get_bot_qrcode"
EP_GET_QR_STATUS = "ilink/bot/get_qrcode_status"

# Timeouts
LONG_POLL_TIMEOUT_MS = 35_000
API_TIMEOUT_MS       = 15_000
CONFIG_TIMEOUT_MS    = 10_000
QR_POLL_TIMEOUT_MS   = 35_000

# Retry / backoff
MAX_CONSECUTIVE_FAILURES = 3
RETRY_DELAY_S            = 2
BACKOFF_DELAY_S          = 30
SESSION_EXPIRED_ERRCODE  = -14

# Media type constants (mirrors UploadMediaType)
MEDIA_IMAGE = 1
MEDIA_VIDEO = 2
MEDIA_FILE  = 3
MEDIA_VOICE = 4

# Message type / state
MSG_TYPE_BOT   = 2
MSG_STATE_FINISH = 2

# Item types
ITEM_NONE  = 0
ITEM_TEXT  = 1
ITEM_IMAGE = 2
ITEM_VOICE = 3
ITEM_FILE  = 4
ITEM_VIDEO = 5


# ── optional imports (fail gracefully) ───────────────────────────────────────

try:
    import aiohttp as _aiohttp
    AIOHTTP_AVAILABLE = True
except ImportError:
    AIOHTTP_AVAILABLE = False
    _aiohttp = None  # type: ignore

try:
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
    from cryptography.hazmat.backends import default_backend
    CRYPTO_AVAILABLE = True
except ImportError:
    CRYPTO_AVAILABLE = False


def check_weixin_requirements() -> bool:
    """Check if all required dependencies are installed."""
    return AIOHTTP_AVAILABLE and CRYPTO_AVAILABLE


# ── import Hermes base classes ────────────────────────────────────────────────

import sys as _sys
from pathlib import Path as _Path
_sys.path.insert(0, str(_Path(__file__).resolve().parents[2]))

from gateway.config import Platform, PlatformConfig
from gateway.platforms.base import (
    BasePlatformAdapter,
    MessageEvent,
    MessageType,
    SendResult,
    cache_image_from_bytes,
    cache_document_from_bytes,
)
from hermes_constants import get_hermes_home


# ── AES-128-ECB crypto ────────────────────────────────────────────────────────

def _pkcs7_pad(data: bytes, block_size: int = 16) -> bytes:
    pad_len = block_size - (len(data) % block_size)
    return data + bytes([pad_len] * pad_len)


def _aes128_ecb_encrypt(plaintext: bytes, key: bytes) -> bytes:
    """AES-128-ECB encryption with PKCS7 padding (mirrors aes-ecb.ts)."""
    padded = _pkcs7_pad(plaintext)
    cipher = Cipher(algorithms.AES(key), modes.ECB(), backend=default_backend())
    enc = cipher.encryptor()
    return enc.update(padded) + enc.finalize()


def _aes128_ecb_padded_size(plaintext_size: int) -> int:
    """Compute ciphertext size after PKCS7 padding to 16-byte boundary."""
    return ((plaintext_size + 1 + 15) // 16) * 16


def _aes128_ecb_decrypt(ciphertext: bytes, key: bytes) -> bytes:
    """AES-128-ECB decryption with PKCS7 unpadding."""
    cipher = Cipher(algorithms.AES(key), modes.ECB(), backend=default_backend())
    dec = cipher.decryptor()
    padded = dec.update(ciphertext) + dec.finalize()
    # Remove PKCS7 padding
    pad_len = padded[-1]
    if pad_len < 1 or pad_len > 16:
        return padded  # Not padded, return as-is
    return padded[:-pad_len]


# ── context token persistence ─────────────────────────────────────────────────

class ContextTokenStore:
    """
    In-memory + disk-persistent context token store.
    context_token is issued per-message by iLink and must be echoed on every send.
    """

    def __init__(self, hermes_home: str):
        self._cache: Dict[str, str] = {}          # key = "accountId:userId"
        self._home = Path(hermes_home)

    def _file(self, account_id: str) -> Path:
        p = self._home / "weixin" / "accounts" / f"{account_id}.context-tokens.json"
        p.parent.mkdir(parents=True, exist_ok=True)
        return p

    def restore(self, account_id: str) -> None:
        """Load persisted tokens into memory on startup."""
        f = self._file(account_id)
        if not f.exists():
            return
        try:
            tokens: Dict[str, str] = json.loads(f.read_text("utf-8"))
            count = 0
            for user_id, tok in tokens.items():
                if isinstance(tok, str) and tok:
                    self._cache[f"{account_id}:{user_id}"] = tok
                    count += 1
            logger.info("weixin: restored %d context tokens for account=%s", count, account_id[:8])
        except Exception as e:
            logger.warning("weixin: failed to restore context tokens: %s", e)

    def get(self, account_id: str, user_id: str) -> Optional[str]:
        return self._cache.get(f"{account_id}:{user_id}")

    def set(self, account_id: str, user_id: str, token: str) -> None:
        key = f"{account_id}:{user_id}"
        self._cache[key] = token
        self._persist(account_id)

    def clear_account(self, account_id: str) -> None:
        prefix = f"{account_id}:"
        for k in [k for k in self._cache if k.startswith(prefix)]:
            del self._cache[k]
        f = self._file(account_id)
        if f.exists():
            try:
                f.unlink()
            except Exception:
                pass

    def _persist(self, account_id: str) -> None:
        prefix = f"{account_id}:"
        tokens = {
            k[len(prefix):]: v
            for k, v in self._cache.items()
            if k.startswith(prefix)
        }
        try:
            self._file(account_id).write_text(json.dumps(tokens), "utf-8")
        except Exception as e:
            logger.warning("weixin: failed to persist context tokens: %s", e)


# ── account credential storage ────────────────────────────────────────────────

class WeixinAccountStore:
    """Persist and load bot credentials (token + base_url) per account."""

    def __init__(self, hermes_home: str):
        self._dir = Path(hermes_home) / "weixin" / "accounts"
        self._dir.mkdir(parents=True, exist_ok=True)
        self._index_file = Path(hermes_home) / "weixin" / "accounts.json"

    def _account_file(self, account_id: str) -> Path:
        return self._dir / f"{account_id}.json"

    def load(self, account_id: str) -> Optional[Dict[str, Any]]:
        f = self._account_file(account_id)
        if f.exists():
            try:
                return json.loads(f.read_text("utf-8"))
            except Exception:
                pass
        return None

    def save(self, account_id: str, token: str, base_url: str = "", user_id: str = "") -> None:
        data: Dict[str, Any] = {
            "token": token,
            "saved_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        }
        if base_url:
            data["base_url"] = base_url
        if user_id:
            data["user_id"] = user_id
        f = self._account_file(account_id)
        f.write_text(json.dumps(data, indent=2), "utf-8")
        try:
            f.chmod(0o600)
        except Exception:
            pass
        self._register(account_id)

    def list_ids(self) -> List[str]:
        if self._index_file.exists():
            try:
                ids = json.loads(self._index_file.read_text("utf-8"))
                return [i for i in ids if isinstance(i, str) and i.strip()]
            except Exception:
                pass
        return []

    def _register(self, account_id: str) -> None:
        ids = self.list_ids()
        if account_id not in ids:
            ids.append(account_id)
            self._index_file.write_text(json.dumps(ids, indent=2), "utf-8")


# ── low-level iLink HTTP helpers ──────────────────────────────────────────────

def _random_wechat_uin() -> str:
    """X-WECHAT-UIN: random uint32 → decimal string → base64 (mirrors api.ts)."""
    val = struct.unpack(">I", secrets.token_bytes(4))[0]
    return base64.b64encode(str(val).encode()).decode()


def _build_headers(token: Optional[str], body: str) -> Dict[str, str]:
    h: Dict[str, str] = {
        "Content-Type": "application/json",
        "AuthorizationType": "ilink_bot_token",
        "Content-Length": str(len(body.encode("utf-8"))),
        "X-WECHAT-UIN": _random_wechat_uin(),
        "iLink-App-Id": ILINK_APP_ID,
        "iLink-App-ClientVersion": str(ILINK_APP_CLIENT_VERSION),
    }
    if token:
        h["Authorization"] = f"Bearer {token}"
    return h


def _build_base_info() -> Dict[str, Any]:
    return {"channel_version": CHANNEL_VERSION}


def _ensure_trailing_slash(url: str) -> str:
    return url if url.endswith("/") else url + "/"


async def _api_post(
    session: "_aiohttp.ClientSession",
    base_url: str,
    endpoint: str,
    payload: Dict[str, Any],
    token: Optional[str],
    timeout_ms: int,
) -> Dict[str, Any]:
    url = _ensure_trailing_slash(base_url) + endpoint
    body = json.dumps({**payload, "base_info": _build_base_info()})
    headers = _build_headers(token, body)
    timeout = _aiohttp.ClientTimeout(total=timeout_ms / 1000)
    async with session.post(url, data=body, headers=headers, timeout=timeout) as resp:
        raw = await resp.text()
        if not resp.ok:
            raise RuntimeError(f"iLink {endpoint} HTTP {resp.status}: {raw[:200]}")
        return json.loads(raw)


async def _api_get(
    session: "_aiohttp.ClientSession",
    base_url: str,
    endpoint: str,
    timeout_ms: int,
) -> Dict[str, Any]:
    url = _ensure_trailing_slash(base_url) + endpoint
    headers = {
        "iLink-App-Id": ILINK_APP_ID,
        "iLink-App-ClientVersion": str(ILINK_APP_CLIENT_VERSION),
    }
    timeout = _aiohttp.ClientTimeout(total=timeout_ms / 1000)
    async with session.get(url, headers=headers, timeout=timeout) as resp:
        raw = await resp.text()
        if not resp.ok:
            raise RuntimeError(f"iLink GET {endpoint} HTTP {resp.status}: {raw[:200]}")
        return json.loads(raw)


# ── iLink API wrappers ────────────────────────────────────────────────────────

async def _get_updates(
    session: "_aiohttp.ClientSession",
    base_url: str,
    token: str,
    get_updates_buf: str,
    timeout_ms: int = LONG_POLL_TIMEOUT_MS,
) -> Dict[str, Any]:
    try:
        return await _api_post(
            session, base_url, EP_GET_UPDATES,
            {"get_updates_buf": get_updates_buf},
            token, timeout_ms,
        )
    except asyncio.TimeoutError:
        logger.debug("weixin: getUpdates client timeout after %dms, retrying", timeout_ms)
        return {"ret": 0, "msgs": [], "get_updates_buf": get_updates_buf}


async def _send_message(
    session: "_aiohttp.ClientSession",
    base_url: str,
    token: str,
    to: str,
    text: str,
    context_token: Optional[str],
    client_id: str,
) -> None:
    item_list = [{"type": ITEM_TEXT, "text_item": {"text": text}}] if text else []
    msg = {
        "from_user_id": "",
        "to_user_id": to,
        "client_id": client_id,
        "message_type": MSG_TYPE_BOT,
        "message_state": MSG_STATE_FINISH,
    }
    if item_list:
        msg["item_list"] = item_list
    if context_token:
        msg["context_token"] = context_token
    await _api_post(session, base_url, EP_SEND_MESSAGE, {"msg": msg}, token, API_TIMEOUT_MS)


async def _send_media_message(
    session: "_aiohttp.ClientSession",
    base_url: str,
    token: str,
    to: str,
    text: str,
    media_item: Dict[str, Any],
    context_token: Optional[str],
) -> str:
    """Send text (optional) + one media item. Returns last client_id."""
    items: List[Dict[str, Any]] = []
    if text:
        items.append({"type": ITEM_TEXT, "text_item": {"text": text}})
    items.append(media_item)

    last_cid = ""
    for item in items:
        last_cid = f"hermes-weixin-{uuid.uuid4().hex}"
        msg: Dict[str, Any] = {
            "from_user_id": "",
            "to_user_id": to,
            "client_id": last_cid,
            "message_type": MSG_TYPE_BOT,
            "message_state": MSG_STATE_FINISH,
            "item_list": [item],
        }
        if context_token:
            msg["context_token"] = context_token
        await _api_post(session, base_url, EP_SEND_MESSAGE, {"msg": msg}, token, API_TIMEOUT_MS)

    return last_cid


async def _send_typing(
    session: "_aiohttp.ClientSession",
    base_url: str,
    token: str,
    to_user_id: str,
    typing_ticket: str,
    status: int = 1,  # 1=typing, 2=cancel
) -> None:
    try:
        await _api_post(
            session, base_url, EP_SEND_TYPING,
            {"ilink_user_id": to_user_id, "typing_ticket": typing_ticket, "status": status},
            token, CONFIG_TIMEOUT_MS,
        )
    except Exception as e:
        logger.debug("weixin: sendTyping failed: %s", e)


async def _get_config(
    session: "_aiohttp.ClientSession",
    base_url: str,
    token: str,
    ilink_user_id: str,
    context_token: Optional[str] = None,
) -> Dict[str, Any]:
    payload: Dict[str, Any] = {"ilink_user_id": ilink_user_id}
    if context_token:
        payload["context_token"] = context_token
    return await _api_post(session, base_url, EP_GET_CONFIG, payload, token, CONFIG_TIMEOUT_MS)


# ── CDN upload ────────────────────────────────────────────────────────────────

async def _get_upload_url(
    session: "_aiohttp.ClientSession",
    base_url: str,
    token: str,
    to_user_id: str,
    filekey: str,
    media_type: int,
    rawsize: int,
    rawfilemd5: str,
    filesize: int,
    aeskey_hex: str,
) -> Dict[str, Any]:
    payload = {
        "filekey": filekey,
        "media_type": media_type,
        "to_user_id": to_user_id,
        "rawsize": rawsize,
        "rawfilemd5": rawfilemd5,
        "filesize": filesize,
        "no_need_thumb": True,
        "aeskey": aeskey_hex,
    }
    return await _api_post(session, base_url, EP_GET_UPLOAD_URL, payload, token, API_TIMEOUT_MS)


async def _upload_to_cdn(
    session: "_aiohttp.ClientSession",
    upload_full_url: str,
    filekey: str,
    cdn_base_url: str,
    plaintext: bytes,
    aeskey: bytes,
) -> str:
    """Upload AES-128-ECB-encrypted file to CDN; returns download encrypt_query_param."""
    ciphertext = _aes128_ecb_encrypt(plaintext, aeskey)

    # The upload_full_url is a pre-signed URL from the iLink API
    headers = {"Content-Type": "application/octet-stream"}
    timeout = _aiohttp.ClientTimeout(total=120)
    async with session.put(upload_full_url, data=ciphertext, headers=headers, timeout=timeout) as resp:
        raw = await resp.text()
        logger.debug("CDN upload: status=%d body=%s", resp.status, raw[:200])
        if not resp.ok:
            raise RuntimeError(f"CDN upload failed HTTP {resp.status}: {raw[:200]}")

    # The download param is typically the filekey itself (the CDN stores by filekey)
    # The actual encrypt_query_param comes back in the CDN response body
    try:
        data = json.loads(raw) if raw.strip().startswith("{") else {}
        return data.get("encrypt_query_param") or data.get("download_param") or filekey
    except Exception:
        return filekey


async def _upload_file(
    session: "_aiohttp.ClientSession",
    base_url: str,
    cdn_base_url: str,
    token: str,
    to_user_id: str,
    file_path: str,
    media_type: int,
) -> Dict[str, Any]:
    """Full upload pipeline: read → hash → gen key → getUploadUrl → CDN upload."""
    data = Path(file_path).read_bytes()
    rawsize = len(data)
    rawfilemd5 = hashlib.md5(data).hexdigest()
    filesize = _aes128_ecb_padded_size(rawsize)
    filekey = secrets.token_hex(16)
    aeskey = secrets.token_bytes(16)
    aeskey_hex = aeskey.hex()

    resp = await _get_upload_url(
        session, base_url, token, to_user_id,
        filekey, media_type, rawsize, rawfilemd5, filesize, aeskey_hex,
    )

    upload_full_url = (resp.get("upload_full_url") or "").strip()
    if not upload_full_url:
        raise RuntimeError(f"getUploadUrl returned no upload URL: {resp}")

    download_param = await _upload_to_cdn(session, upload_full_url, filekey, cdn_base_url, data, aeskey)

    return {
        "filekey": filekey,
        "download_param": download_param,
        "aeskey_b64": base64.b64encode(aeskey).decode(),
        "file_size": rawsize,
        "file_size_ciphertext": filesize,
    }


def _detect_media_type(file_path: str) -> Tuple[int, str]:
    """Return (iLink media_type, mime_type) for a file path."""
    ext = Path(file_path).suffix.lower()
    mime = mimetypes.guess_type(file_path)[0] or "application/octet-stream"
    if mime.startswith("image/"):
        return MEDIA_IMAGE, mime
    if mime.startswith("video/"):
        return MEDIA_VIDEO, mime
    return MEDIA_FILE, mime


async def _send_file(
    session: "_aiohttp.ClientSession",
    base_url: str,
    cdn_base_url: str,
    token: str,
    to: str,
    text: str,
    context_token: Optional[str],
    file_path: str,
) -> str:
    """Upload a local file to CDN and send it as a WeChat message. Returns client_id."""
    media_type_int, mime = _detect_media_type(file_path)
    info = await _upload_file(session, base_url, cdn_base_url, token, to, file_path, media_type_int)

    cdn_ref = {
        "encrypt_query_param": info["download_param"],
        "aes_key": info["aeskey_b64"],
        "encrypt_type": 1,
    }

    if media_type_int == MEDIA_IMAGE:
        media_item: Dict[str, Any] = {
            "type": ITEM_IMAGE,
            "image_item": {
                "media": cdn_ref,
                "mid_size": info["file_size_ciphertext"],
            },
        }
    elif media_type_int == MEDIA_VIDEO:
        media_item = {
            "type": ITEM_VIDEO,
            "video_item": {
                "media": cdn_ref,
                "video_size": info["file_size_ciphertext"],
            },
        }
    else:
        media_item = {
            "type": ITEM_FILE,
            "file_item": {
                "media": cdn_ref,
                "file_name": Path(file_path).name,
                "len": str(info["file_size"]),
            },
        }

    return await _send_media_message(
        session, base_url, token, to, text, media_item, context_token
    )


# ── QR login flow ─────────────────────────────────────────────────────────────

async def qr_login(
    hermes_home: str,
    bot_type: str = "3",
    timeout_s: int = 480,
) -> Optional[Dict[str, str]]:
    """
    Interactive QR login flow. Prints QR code URL to stdout.
    Returns {"account_id": ..., "token": ..., "base_url": ..., "user_id": ...}
    or None on failure.
    """
    async with _aiohttp.ClientSession() as session:
        # Step 1: get QR code
        try:
            resp = await _api_get(
                session, ILINK_BASE_URL,
                f"{EP_GET_BOT_QR}?bot_type={bot_type}",
                QR_POLL_TIMEOUT_MS,
            )
        except Exception as e:
            logger.error("weixin QR login: failed to fetch QR code: %s", e)
            return None

        qrcode = resp.get("qrcode", "")
        qrcode_url = resp.get("qrcode_img_content", "")
        if not qrcode:
            logger.error("weixin QR login: no qrcode in response")
            return None

        print("\n请使用微信扫描以下二维码：")
        print(qrcode_url)
        print()

        # Try qrcode-in-terminal
        try:
            import qrcode as _qrcode
            qr = _qrcode.QRCode()
            qr.add_data(qrcode_url)
            qr.make(fit=True)
            # Print ASCII QR code to terminal (requires `uv add qrcode`)
            qr.print_ascii(invert=True)
        except Exception:
            print("（终端中显示二维码失败时请直接访问上方 URL）")

        # Step 2: poll for scan result
        deadline = time.time() + timeout_s
        current_base = ILINK_BASE_URL
        refresh_count = 0

        while time.time() < deadline:
            try:
                status_resp = await _api_get(
                    session, current_base,
                    f"{EP_GET_QR_STATUS}?qrcode={qrcode}",
                    QR_POLL_TIMEOUT_MS,
                )
            except asyncio.TimeoutError:
                await asyncio.sleep(1)
                continue
            except Exception as e:
                logger.warning("weixin QR poll error: %s", e)
                await asyncio.sleep(1)
                continue

            status = status_resp.get("status", "wait")

            if status == "wait":
                print(".", end="", flush=True)

            elif status == "scaned":
                print("\n👀 已扫码，请在微信中确认...")

            elif status == "scaned_but_redirect":
                redirect_host = status_resp.get("redirect_host", "")
                if redirect_host:
                    current_base = f"https://{redirect_host}"
                    logger.info("weixin QR: IDC redirect → %s", current_base)

            elif status == "expired":
                refresh_count += 1
                if refresh_count > 3:
                    print("\n⚠️ 二维码多次过期，请重新运行登录命令")
                    return None
                print(f"\n⏳ 二维码已过期，正在刷新... ({refresh_count}/3)")
                try:
                    resp2 = await _api_get(
                        session, ILINK_BASE_URL,
                        f"{EP_GET_BOT_QR}?bot_type={bot_type}",
                        QR_POLL_TIMEOUT_MS,
                    )
                    qrcode = resp2.get("qrcode", "")
                    qrcode_url = resp2.get("qrcode_img_content", "")
                    print("新二维码：", qrcode_url)
                except Exception as e:
                    logger.error("weixin QR refresh failed: %s", e)
                    return None

            elif status == "confirmed":
                bot_token = status_resp.get("bot_token", "")
                account_id = status_resp.get("ilink_bot_id", "")
                base_url = status_resp.get("baseurl", "") or ILINK_BASE_URL
                user_id = status_resp.get("ilink_user_id", "")
                if not account_id:
                    logger.error("weixin QR login: confirmed but no ilink_bot_id")
                    return None
                print(f"\n✅ 微信连接成功！account_id={account_id}")
                return {
                    "account_id": account_id,
                    "token": bot_token,
                    "base_url": base_url,
                    "user_id": user_id,
                }

            await asyncio.sleep(1)

        print("\n⚠️ 登录超时")
        return None


# ── sync buf persistence ──────────────────────────────────────────────────────

def _sync_buf_path(hermes_home: str, account_id: str) -> Path:
    return Path(hermes_home) / "weixin" / "accounts" / f"{account_id}.sync.json"


def _load_sync_buf(hermes_home: str, account_id: str) -> str:
    f = _sync_buf_path(hermes_home, account_id)
    if f.exists():
        try:
            data = json.loads(f.read_text("utf-8"))
            return data.get("get_updates_buf", "")
        except Exception:
            pass
    return ""


def _save_sync_buf(hermes_home: str, account_id: str, buf: str) -> None:
    f = _sync_buf_path(hermes_home, account_id)
    f.parent.mkdir(parents=True, exist_ok=True)
    try:
        f.write_text(json.dumps({"get_updates_buf": buf}), "utf-8")
    except Exception as e:
        logger.warning("weixin: failed to save sync buf: %s", e)


# ── typing ticket cache ───────────────────────────────────────────────────────

class TypingTicketCache:
    """Per-user typing ticket cache (fetched from getConfig, valid ~10 min)."""

    def __init__(self):
        self._cache: Dict[str, Tuple[str, float]] = {}  # user_id → (ticket, ts)
        self._ttl = 600.0

    def get(self, user_id: str) -> Optional[str]:
        entry = self._cache.get(user_id)
        if entry and time.time() - entry[1] < self._ttl:
            return entry[0]
        return None

    def set(self, user_id: str, ticket: str) -> None:
        self._cache[user_id] = (ticket, time.time())


# ── Platform Adapter ──────────────────────────────────────────────────────────

class WeixinAdapter(BasePlatformAdapter):
    """
    Hermes Agent Platform Adapter for WeChat via the iLink Bot API.

    Message flow:
        connect() → long-poll getUpdates loop (background task)
                  → processOneMessage → self.handle_message(event)
        agent reply → send() / send_image() / send_document()
    """

    MAX_MESSAGE_LENGTH = 4000

    def __init__(self, config: PlatformConfig):
        super().__init__(config, Platform.WEIXIN)

        extra = config.extra or {}
        hermes_home = str(get_hermes_home())

        self._hermes_home = hermes_home
        self._account_store = WeixinAccountStore(hermes_home)
        self._token_store = ContextTokenStore(hermes_home)
        self._typing_cache = TypingTicketCache()

        # Resolve credentials (config → env vars → stored account file)
        self._account_id = (
            extra.get("account_id")
            or os.environ.get("WEIXIN_ACCOUNT_ID", "")
        )
        self._base_url = (
            extra.get("base_url")
            or os.environ.get("WEIXIN_BASE_URL", ILINK_BASE_URL)
        ).rstrip("/")
        self._cdn_base_url = extra.get("cdn_base_url", CDN_BASE_URL).rstrip("/")
        self._token: Optional[str] = (
            extra.get("token")
            or os.environ.get("WEIXIN_TOKEN")
        )

        # If account_id provided but no token, try loading from store
        if self._account_id and not self._token:
            data = self._account_store.load(self._account_id)
            if data:
                self._token = data.get("token")
                stored_url = (data.get("base_url") or "").strip()
                if stored_url:
                    self._base_url = stored_url

        # Policies
        self._dm_policy: str = extra.get("dm_policy", "open")   # open | allowlist | pairing
        self._allow_from: List[str] = extra.get("allow_from", [])
        self._bot_wxid: str = self._account_id  # bot's own WeChat ID

        # Runtime state
        self._session: Optional["_aiohttp.ClientSession"] = None
        self._poll_task: Optional[asyncio.Task] = None
        self._connected = False
        self._fatal_error: Optional[str] = None

        # Dedup: (msg_id → ts)
        self._seen_msgs: Dict[Any, float] = {}

    # ── Status properties ─────────────────────────────────────────────────────

    @property
    def name(self) -> str:
        return "weixin"

    @property
    def is_connected(self) -> bool:
        return self._connected

    @property
    def has_fatal_error(self) -> bool:
        return self._fatal_error is not None

    @property
    def fatal_error_message(self) -> Optional[str]:
        return self._fatal_error

    @property
    def fatal_error_code(self) -> Optional[str]:
        return "CONFIG_ERROR" if self._fatal_error else None

    @property
    def fatal_error_retryable(self) -> bool:
        return False

    # ── Connect / Disconnect ──────────────────────────────────────────────────

    async def connect(self) -> bool:
        if not AIOHTTP_AVAILABLE:
            self._fatal_error = "aiohttp not installed (uv add aiohttp)"
            return False
        if not CRYPTO_AVAILABLE:
            self._fatal_error = "cryptography not installed (uv add cryptography)"
            return False
        if not self._token:
            self._fatal_error = (
                "WeChat token not configured. "
                "Run: hermes channels login --channel weixin"
            )
            return False

        self._session = _aiohttp.ClientSession()

        # Restore persisted context tokens
        if self._account_id:
            self._token_store.restore(self._account_id)

        self._connected = True
        self._poll_task = asyncio.create_task(self._poll_loop(), name="weixin-poll")

        logger.info("weixin: adapter connected (account=%s, base=%s)",
                    self._account_id[:8] if self._account_id else "?", self._base_url)
        return True

    async def disconnect(self) -> None:
        self._connected = False
        if self._poll_task and not self._poll_task.done():
            self._poll_task.cancel()
            try:
                await self._poll_task
            except (asyncio.CancelledError, Exception):
                pass
        if self._session and not self._session.closed:
            await self._session.close()
        logger.info("weixin: adapter disconnected")

    # ── Long-poll loop ────────────────────────────────────────────────────────

    async def _poll_loop(self) -> None:
        """Main getUpdates polling loop. Runs until disconnect()."""
        buf = _load_sync_buf(self._hermes_home, self._account_id)
        next_timeout_ms = LONG_POLL_TIMEOUT_MS
        consecutive_failures = 0

        logger.info("weixin: starting poll loop (account=%s)", self._account_id[:8] if self._account_id else "?")

        while self._connected:
            try:
                resp = await _get_updates(
                    self._session, self._base_url, self._token,  # type: ignore[arg-type]
                    buf, next_timeout_ms,
                )

                # Respect server-suggested timeout
                svr_timeout = resp.get("longpolling_timeout_ms")
                if svr_timeout and svr_timeout > 0:
                    next_timeout_ms = svr_timeout

                # API-level error handling
                ret = resp.get("ret")
                errcode = resp.get("errcode")
                is_error = (ret is not None and ret != 0) or (errcode is not None and errcode != 0)

                if is_error:
                    if errcode == SESSION_EXPIRED_ERRCODE or ret == SESSION_EXPIRED_ERRCODE:
                        logger.error("weixin: session expired, pausing 10 min")
                        await asyncio.sleep(600)
                        consecutive_failures = 0
                        continue

                    consecutive_failures += 1
                    logger.warning(
                        "weixin: getUpdates error ret=%s errcode=%s errmsg=%s (%d/%d)",
                        ret, errcode, resp.get("errmsg", ""), consecutive_failures, MAX_CONSECUTIVE_FAILURES,
                    )
                    if consecutive_failures >= MAX_CONSECUTIVE_FAILURES:
                        logger.error("weixin: %d consecutive failures, backing off %ds",
                                     MAX_CONSECUTIVE_FAILURES, BACKOFF_DELAY_S)
                        consecutive_failures = 0
                        await asyncio.sleep(BACKOFF_DELAY_S)
                    else:
                        await asyncio.sleep(RETRY_DELAY_S)
                    continue

                consecutive_failures = 0

                # Persist sync buf
                new_buf = resp.get("get_updates_buf", "")
                if new_buf:
                    buf = new_buf
                    _save_sync_buf(self._hermes_home, self._account_id, buf)

                # Process messages
                for msg in (resp.get("msgs") or []):
                    asyncio.create_task(self._process_message_safe(msg))

            except asyncio.CancelledError:
                break
            except Exception as e:
                consecutive_failures += 1
                logger.error("weixin: poll error (%d/%d): %s",
                             consecutive_failures, MAX_CONSECUTIVE_FAILURES, e)
                if consecutive_failures >= MAX_CONSECUTIVE_FAILURES:
                    consecutive_failures = 0
                    await asyncio.sleep(BACKOFF_DELAY_S)
                else:
                    await asyncio.sleep(RETRY_DELAY_S)

        logger.info("weixin: poll loop ended")

    # ── Inbound message processing ────────────────────────────────────────────

    async def _process_message_safe(self, msg: Dict[str, Any]) -> None:
        """Wrapper around _process_message that catches and logs exceptions."""
        try:
            await self._process_message(msg)
        except Exception as e:
            logger.error("weixin: unhandled error processing msg from %s: %s",
                         (msg.get("from_user_id") or "?")[:8], e, exc_info=True)

    async def _process_message(self, msg: Dict[str, Any]) -> None:
        """Process one inbound WeixinMessage."""
        from_user_id = msg.get("from_user_id", "")

        # DEBUG: dump raw message structure for media messages (debug-level only)
        item_list = msg.get("item_list") or []
        has_media = any(item.get("type") in (ITEM_IMAGE, ITEM_VIDEO, ITEM_VOICE) for item in item_list)
        if has_media or not any(item.get("type") == ITEM_TEXT for item in item_list):
            logger.debug("weixin: raw_msg from=%s keys=%s items=%s",
                          from_user_id[:8], list(msg.keys()),
                          json.dumps(item_list, ensure_ascii=False)[:1000])

        # Filter self-messages
        if self._account_id and from_user_id == self._account_id:
            return

        # Dedup by message_id
        msg_id = msg.get("message_id")
        if msg_id is not None:
            now = time.time()
            self._seen_msgs = {k: v for k, v in self._seen_msgs.items() if now - v < 300}
            if msg_id in self._seen_msgs:
                return
            self._seen_msgs[msg_id] = now

        # Authorization check
        if not self._is_authorized(from_user_id):
            logger.debug("weixin: dropping unauthorized message from %s", from_user_id[:8])
            return

        # Persist context_token
        ctx_token = msg.get("context_token")
        if ctx_token:
            self._token_store.set(self._account_id, from_user_id, ctx_token)

        # Fetch typing ticket (best-effort, non-blocking)
        asyncio.create_task(self._maybe_fetch_typing_ticket(from_user_id, ctx_token))

        # Extract content
        item_list: List[Dict[str, Any]] = msg.get("item_list") or []
        text = self._extract_text(item_list)
        images: List[str] = []
        media_types: List[str] = []

        # Download media (image, video, file, voice) — including quoted references
        for item in item_list:
            if item.get("type") == ITEM_IMAGE:
                path = await self._download_image(item)
                if path:
                    images.append(path)
                    media_types.append("image")
            elif item.get("type") == ITEM_VIDEO:
                path = await self._download_video(item)
                if path:
                    images.append(path)
                    media_types.append("video")
            elif item.get("type") == ITEM_FILE:
                file_item = item.get("file_item") or {}
                file_name = file_item.get("file_name", "document")
                path = await self._download_file(item)
                if path:
                    images.append(path)
                    media_types.append("document")
                    # Inject filename so agent knows what was sent
                    if not text:
                        text = f"[文件: {file_name}]"
                    else:
                        text = f"[文件: {file_name}]\n{text}"
            elif item.get("type") == ITEM_VOICE:
                # Use transcribed text if available
                voice_text = (item.get("voice_item") or {}).get("text", "")
                if voice_text and not text:
                    text = voice_text

        # Also download media from quoted/referenced messages
        for ref_item in self._extract_ref_items(item_list):
            rtype = ref_item.get("type", 0)
            if rtype == ITEM_IMAGE:
                path = await self._download_image(ref_item)
                if path:
                    images.append(path)
                    media_types.append("image")
            elif rtype == ITEM_FILE:
                fi = ref_item.get("file_item") or {}
                fn = fi.get("file_name", "引用文件")
                path = await self._download_file(ref_item)
                if path:
                    images.append(path)
                    media_types.append("document")
                    # Note: filename already injected by _extract_text → _describe_ref_item,
                    # so no need to add it again here.
            elif rtype == ITEM_VIDEO:
                path = await self._download_video(ref_item)
                if path:
                    images.append(path)
                    media_types.append("video")

        # Build session source + dispatch
        # Detect group chat: iLink uses to_user_id as group ID for group messages,
        # and may include a room_id field. Single-chat: to_user_id == bot's own ID.
        to_user_id = msg.get("to_user_id", "")
        is_group = (
            to_user_id != self._account_id
            and bool(msg.get("room_id") or msg.get("chat_room_id"))
        ) or (msg.get("msg_type") == 1)  # msg_type 1 = group in some iLink versions
        chat_type = "group" if is_group else "dm"
        effective_chat_id = msg.get("room_id") or msg.get("chat_room_id") or from_user_id

        source = self.build_source(
            chat_id=effective_chat_id,
            user_id=from_user_id,
            chat_type=chat_type,
        )
        event = MessageEvent(
            source=source,
            text=text,
            message_type=MessageType.PHOTO if images else MessageType.TEXT,
            message_id=str(msg_id) if msg_id else None,
            media_urls=images,
            media_types=media_types or ["image"] * len(images),
        )

        logger.info("weixin: inbound from=%s text_len=%d images=%d",
                    from_user_id[:8], len(text), len(images))
        await self.handle_message(event)

    # ── Reference / quote helpers ────────────────────────────────────────────

    @staticmethod
    def _describe_ref_item(ref_item: Dict[str, Any]) -> str:
        """Return human-readable description of a quoted/referenced item."""
        itype = ref_item.get("type", 0)
        if itype == ITEM_FILE:
            fi = ref_item.get("file_item") or {}
            name = fi.get("file_name", "文件")
            size = fi.get("len", "?")
            return f"引用文件: {name} ({size}字节)"
        if itype == ITEM_IMAGE:
            ii = ref_item.get("image_item") or {}
            mi = ii.get("media") or {}
            w = ii.get("thumb_width", "?")
            h = ii.get("thumb_height", "?")
            size = mi.get("mid_size", "?")
            return f"引用图片 ({w}x{h}, ~{size}字节)"
        if itype == ITEM_VIDEO:
            return "引用视频"
        if itype == ITEM_VOICE:
            vi = ref_item.get("voice_item") or {}
            vt = vi.get("text", "")
            if vt:
                return f'引用语音: "{vt[:80]}"'
            return "引用语音"
        return ""

    def _extract_ref_items(self, item_list: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """Extract all referenced/quoted media items from item_list.
        
        When a user quotes a message (image/file/video) and adds text,
        iLink puts the original item inside ref_msg.message_item.
        This method pulls those out so they can be downloaded alongside
        the main message content.
        """
        refs: List[Dict[str, Any]] = []
        for item in item_list:
            if item.get("type") != ITEM_TEXT:
                continue
            ref = item.get("ref_msg")
            if not ref:
                continue
            ref_item = ref.get("message_item") or {}
            rtype = ref_item.get("type", 0)
            if rtype in (ITEM_IMAGE, ITEM_VIDEO, ITEM_FILE):
                refs.append(ref_item)
        return refs

    # ── Text extraction ─────────────────────────────────────────────────────

    def _extract_text(self, item_list: List[Dict[str, Any]]) -> str:
        """Extract text body from item_list, including quoted message context."""
        for item in item_list:
            if item.get("type") == ITEM_TEXT:
                text = (item.get("text_item") or {}).get("text", "")
                ref = item.get("ref_msg")
                if not ref:
                    return text
                ref_item = ref.get("message_item") or {}
                rtype = ref_item.get("type", 0)
                # Quoted media → inject description so agent knows what was referenced
                if rtype in (ITEM_IMAGE, ITEM_VIDEO, ITEM_FILE, ITEM_VOICE):
                    desc = self._describe_ref_item(ref_item)
                    prefix = f"[{desc}]\n" if desc else ""
                    return prefix + text
                # Quoted text → prepend context
                parts = []
                if ref.get("title"):
                    parts.append(ref["title"])
                ref_text = self._extract_text([ref_item])
                if ref_text:
                    parts.append(ref_text)
                prefix = f"[引用: {' | '.join(parts)}]\n" if parts else ""
                return prefix + text
        # Voice-to-text fallback
        for item in item_list:
            if item.get("type") == ITEM_VOICE:
                return (item.get("voice_item") or {}).get("text", "")
        return ""

    async def _download_image(self, item: Dict[str, Any]) -> Optional[str]:
        """Download and cache an inbound image item."""
        image_item = item.get("image_item") or {}
        media = image_item.get("media") or {}
        full_url = media.get("full_url", "")
        if not full_url:
            return None
        try:
            async with self._session.get(full_url, timeout=_aiohttp.ClientTimeout(total=30)) as resp:
                if resp.ok:
                    data = await resp.read()
                    # Decrypt AES-128-ECB encrypted CDN data
                    # aes_key is base64(hex_string), e.g. "ZWZhNTVl...==" → b'efa55e7c...' → 16 raw bytes
                    aes_key_b64 = media.get("aes_key", "")
                    if aes_key_b64:
                        import base64
                        hex_encoded = base64.b64decode(aes_key_b64)
                        try:
                            aes_key = bytes.fromhex(hex_encoded.decode())
                        except Exception:
                            aes_key = hex_encoded  # fallback: use as-is
                        data = _aes128_ecb_decrypt(data, aes_key)
                    return cache_image_from_bytes(data, ".jpg")
        except Exception as e:
            logger.warning("weixin: image download failed: %s", e)
        return None

    async def _download_video(self, item: Dict[str, Any]) -> Optional[str]:
        """Download and cache an inbound video item."""
        video_item = item.get("video_item") or {}
        media = video_item.get("media") or {}
        full_url = media.get("full_url", "")
        if not full_url:
            return None
        try:
            async with self._session.get(full_url, timeout=_aiohttp.ClientTimeout(total=120)) as resp:
                if resp.ok:
                    data = await resp.read()
                    # Decrypt AES-128-ECB encrypted CDN data
                    # aes_key is base64(hex_string), e.g. "ZWZhNTVl...==" → b'efa55e7c...' → 16 raw bytes
                    aes_key_b64 = media.get("aes_key", "")
                    if aes_key_b64:
                        import base64
                        hex_encoded = base64.b64decode(aes_key_b64)
                        try:
                            aes_key = bytes.fromhex(hex_encoded.decode())
                        except Exception:
                            aes_key = hex_encoded  # fallback: use as-is
                        data = _aes128_ecb_decrypt(data, aes_key)
                    # Cache as document (video file) for agent processing
                    return cache_document_from_bytes(data, ".mp4")
        except Exception as e:
            logger.warning("weixin: video download failed: %s", e)
        return None

    async def _download_file(self, item: Dict[str, Any]) -> Optional[str]:
        """Download and cache an inbound file item (PDF, doc, etc.)."""
        file_item = item.get("file_item") or {}
        media = file_item.get("media") or {}
        full_url = media.get("full_url", "")
        file_name = file_item.get("file_name", "document")
        if not full_url:
            return None
        try:
            async with self._session.get(full_url, timeout=_aiohttp.ClientTimeout(total=60)) as resp:
                if resp.ok:
                    data = await resp.read()
                    # Decrypt AES-128-ECB encrypted CDN data
                    aes_key_b64 = media.get("aes_key", "")
                    if aes_key_b64:
                        import base64
                        hex_encoded = base64.b64decode(aes_key_b64)
                        try:
                            aes_key = bytes.fromhex(hex_encoded.decode())
                        except Exception:
                            aes_key = hex_encoded  # fallback
                        data = _aes128_ecb_decrypt(data, aes_key)
                    # Use original filename for extension detection
                    return cache_document_from_bytes(data, file_name)
        except Exception as e:
            logger.warning("weixin: file download failed: %s", e)
        return None

    async def _maybe_fetch_typing_ticket(
        self, user_id: str, context_token: Optional[str]
    ) -> None:
        """Fetch and cache typing ticket for a user (best-effort)."""
        if self._typing_cache.get(user_id):
            return
        try:
            cfg = await _get_config(
                self._session, self._base_url,
                self._token, user_id, context_token,  # type: ignore[arg-type]
            )
            ticket_b64 = cfg.get("typing_ticket", "")
            if ticket_b64:
                self._typing_cache.set(user_id, ticket_b64)
        except Exception as e:
            logger.debug("weixin: getConfig failed for %s: %s", user_id[:8], e)

    def _is_authorized(self, sender: str) -> bool:
        if self._dm_policy == "disabled":
            return False
        if self._dm_policy == "allowlist":
            return sender in self._allow_from
        return True  # "open" or "pairing" (pairing handled by Hermes framework)

    # ── Outbound ──────────────────────────────────────────────────────────────

    def _split_text(self, text: str, max_len: int = MAX_MESSAGE_LENGTH) -> List[str]:
        """Split long messages at paragraph boundaries."""
        if len(text) <= max_len:
            return [text]
        chunks: List[str] = []
        remaining = text
        while remaining:
            if len(remaining) <= max_len:
                chunks.append(remaining)
                break
            cut = remaining.rfind("\n\n", 0, max_len)
            if cut < max_len * 0.3:
                cut = remaining.rfind(". ", 0, max_len)
                if cut >= 0:
                    cut += 2
            if cut < max_len * 0.3:
                cut = remaining.rfind(" ", 0, max_len)
            if cut < max_len * 0.3:
                cut = max_len
            chunks.append(remaining[:cut])
            remaining = remaining[cut:].lstrip()
        return chunks

    async def send(
        self,
        chat_id: str,
        content: str,
        reply_to=None,
        metadata=None,
    ) -> SendResult:
        """Send text message; auto-splits if over 4000 chars."""
        if not self._session or not self._token:
            return SendResult(success=False, error="Not connected")

        context_token = self._token_store.get(self._account_id, chat_id)
        last_cid = None

        try:
            for chunk in self._split_text(content):
                cid = f"hermes-weixin-{uuid.uuid4().hex}"
                await _send_message(
                    self._session, self._base_url, self._token,
                    chat_id, chunk, context_token, cid,
                )
                last_cid = cid
            logger.info("weixin: sent text to=%s", chat_id[:8])
            return SendResult(success=True, message_id=last_cid)
        except Exception as e:
            logger.error("weixin: send failed to=%s: %s", chat_id[:8], e)
            return SendResult(success=False, error=str(e))

    async def send_typing(self, chat_id: str, metadata=None) -> None:
        """Send typing indicator (requires typing ticket from getConfig)."""
        if not self._session or not self._token:
            return
        ticket = self._typing_cache.get(chat_id)
        if not ticket:
            return
        await _send_typing(self._session, self._base_url, self._token, chat_id, ticket, 1)

    async def stop_typing(self, chat_id: str) -> None:
        if not self._session or not self._token:
            return
        ticket = self._typing_cache.get(chat_id)
        if not ticket:
            return
        await _send_typing(self._session, self._base_url, self._token, chat_id, ticket, 2)

    async def send_image(
        self,
        chat_id: str,
        image_url: str,
        caption: str,
        reply_to=None,
        metadata=None,
    ) -> SendResult:
        """Send image: local file path or remote URL → upload to CDN → send."""
        if not self._session or not self._token:
            return SendResult(success=False, error="Not connected")

        context_token = self._token_store.get(self._account_id, chat_id)

        try:
            if image_url.startswith(("http://", "https://")):
                # Download remote image to temp file (cleaned up in finally)
                tmp = tempfile.NamedTemporaryFile(suffix=".jpg", delete=False)
                try:
                    async with self._session.get(image_url, timeout=_aiohttp.ClientTimeout(total=30)) as resp:
                        resp.raise_for_status()
                        tmp.write(await resp.read())
                finally:
                    tmp.close()
                file_path = tmp.name
            else:
                file_path = image_url.replace("file://", "")
                if not os.path.isabs(file_path):
                    file_path = os.path.abspath(file_path)

            cid = await _send_file(
                self._session, self._base_url, self._cdn_base_url,
                self._token, chat_id, caption, context_token, file_path,
            )
            logger.info("weixin: sent image to=%s", chat_id[:8])
            return SendResult(success=True, message_id=cid)
        except Exception as e:
            logger.error("weixin: send_image failed to=%s: %s", chat_id[:8], e)
            return SendResult(success=False, error=str(e))
        finally:
            # Clean up temp file from remote URL download
            if image_url.startswith(("http://", "https://")) and 'file_path' in dir() \
                    and os.path.exists(file_path):
                try:
                    os.unlink(file_path)
                except Exception:
                    pass

    async def send_document(
        self,
        chat_id: str,
        path: str,
        caption: str = "",
        metadata=None,
    ) -> SendResult:
        """Send any file (image/video/document) via CDN upload."""
        if not self._session or not self._token:
            return SendResult(success=False, error="Not connected")
        context_token = self._token_store.get(self._account_id, chat_id)
        try:
            cid = await _send_file(
                self._session, self._base_url, self._cdn_base_url,
                self._token, chat_id, caption, context_token, path,
            )
            return SendResult(success=True, message_id=cid)
        except Exception as e:
            logger.error("weixin: send_document failed: %s", e)
            return SendResult(success=False, error=str(e))

    async def send_image_file(
        self,
        chat_id: str,
        path: str,
        caption: str = "",
        reply_to=None,
        metadata=None,
    ) -> SendResult:
        return await self.send_document(chat_id, path, caption, metadata)

    async def get_chat_info(self, chat_id: str) -> dict:
        return {
            "name": chat_id,
            "type": "dm",
            "chat_id": chat_id,
        }

    def format_message(self, content: str) -> str:
        """WeChat does not render Markdown; strip common markers."""
        import re
        content = re.sub(r"\*\*(.*?)\*\*", r"\1", content)
        content = re.sub(r"#{1,6}\s+", "", content)
        content = re.sub(r"\[(.*?)\]\(.*?\)", r"\1", content)
        return content
