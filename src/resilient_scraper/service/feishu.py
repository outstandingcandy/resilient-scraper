"""Feishu (Lark) bot client for login verification notifications.

Sends interactive card messages with login screenshots to a Feishu group chat,
and polls replies for verification codes submitted by human operators.
"""

import json
import logging
import re
import threading
import time
from typing import Any

import requests

from resilient_scraper.service.config import FeishuSettings

logger = logging.getLogger("resilient_scraper.service.feishu")

# Throttle: suppress duplicate alerts for the same context_key within this window.
_ALERT_DEDUP_SECONDS = 30


class FeishuClient:
    """Feishu bot API client with token caching, image upload, and reply polling."""

    def __init__(self, settings: FeishuSettings) -> None:
        self._settings = settings
        self._token: str = ""
        self._token_expires_at: float = 0.0
        self._token_lock = threading.Lock()
        self._last_alert_time: dict[str, float] = {}
        self._chat_id: str = ""  # P2P chat_id, resolved from first send response
        self._session = requests.Session()
        self._session.headers["Content-Type"] = "application/json; charset=utf-8"

        # Auto-resolve receive_id if not explicitly configured
        if not self._settings.receive_id:
            self._auto_resolve_receive_id()

    # ------------------------------------------------------------------
    # Token management
    # ------------------------------------------------------------------

    def _refresh_token(self) -> str:
        """Fetch a new tenant_access_token (valid for 2 hours)."""
        url = f"{self._settings.api_base}/open-apis/auth/v3/tenant_access_token/internal"
        resp = self._session.post(
            url,
            json={
                "app_id": self._settings.app_id,
                "app_secret": self._settings.app_secret,
            },
            timeout=10,
        )
        resp.raise_for_status()
        data = resp.json()
        if data.get("code") != 0:
            raise RuntimeError(f"Feishu token error: {data}")
        token = data["tenant_access_token"]
        expire = data.get("expire", 7200)
        self._token = token
        # Refresh 5 minutes before expiry
        self._token_expires_at = time.time() + expire - 300
        logger.info("Feishu token refreshed (expires in %ds)", expire)
        return token

    def _get_token(self) -> str:
        """Return a valid tenant_access_token, refreshing if needed."""
        if self._token and time.time() < self._token_expires_at:
            return self._token
        with self._token_lock:
            # Double-check after acquiring lock
            if self._token and time.time() < self._token_expires_at:
                return self._token
            return self._refresh_token()

    def _auth_headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self._get_token()}"}

    def _auto_resolve_receive_id(self) -> None:
        """Auto-detect the notification recipient from the bot's contact scope.

        Queries the Feishu contacts API for users visible to this bot and
        picks the first one. This way the user only needs to configure
        app_id and app_secret — no need to manually look up their open_id.
        """
        try:
            url = f"{self._settings.api_base}/open-apis/contact/v3/scopes"
            resp = self._session.get(url, headers=self._auth_headers(), timeout=10)
            resp.raise_for_status()
            data = resp.json()
            user_ids = data.get("data", {}).get("user_ids", [])
            if user_ids:
                self._settings.receive_id = user_ids[0]
                logger.info(
                    "Feishu receive_id auto-resolved: %s", self._settings.receive_id,
                )
            else:
                logger.warning(
                    "Feishu: no users in bot contact scope, "
                    "set FEISHU_RECEIVE_ID manually or add a user to the bot's visibility"
                )
        except Exception as e:
            logger.error("Feishu: failed to auto-resolve receive_id: %s", e)

    # ------------------------------------------------------------------
    # Image upload
    # ------------------------------------------------------------------

    def upload_image(self, image_bytes: bytes) -> str:
        """Upload a PNG image to Feishu and return the image_key.

        Args:
            image_bytes: Raw PNG image data.

        Returns:
            Feishu image_key string.
        """
        url = f"{self._settings.api_base}/open-apis/im/v1/images"
        resp = requests.post(
            url,
            headers=self._auth_headers(),
            files={"image": ("screenshot.png", image_bytes, "image/png")},
            data={"image_type": "message"},
            timeout=30,
        )
        resp.raise_for_status()
        data = resp.json()
        if data.get("code") != 0:
            raise RuntimeError(f"Feishu image upload error: {data}")
        image_key = data["data"]["image_key"]
        logger.info("Feishu image uploaded: %s (%d bytes)", image_key, len(image_bytes))
        return image_key

    # ------------------------------------------------------------------
    # Message sending
    # ------------------------------------------------------------------

    def send_login_alert(
        self,
        image_key: str,
        platform: str,
        context_key: str,
        phase: str,
        task_id: int,
    ) -> str | None:
        """Send an interactive card message with a login screenshot.

        Args:
            image_key: Feishu image_key from upload_image().
            platform: Human-readable platform name (e.g. "小红书").
            context_key: Account/task identifier.
            phase: "qr_scan" or "sms_verification".
            task_id: Database task ID.

        Returns:
            Message ID on success, None if throttled or failed.
        """
        # Dedup throttle
        now = time.time()
        dedup_key = f"{context_key}:{phase}"
        last = self._last_alert_time.get(dedup_key, 0)
        if now - last < _ALERT_DEDUP_SECONDS:
            logger.debug("Feishu alert throttled for %s (%.0fs ago)", dedup_key, now - last)
            return None
        self._last_alert_time[dedup_key] = now

        if phase == "sms_verification":
            title = f"短信验证: {platform}"
            instruction = "请直接回复本消息，发送验证码（纯数字）"
            phase_label = "SMS 验证码"
        else:
            title = f"登录验证: {platform}"
            instruction = "请用 APP 扫描下方二维码"
            phase_label = "QR 扫码"

        card = {
            "config": {"wide_screen_mode": True},
            "header": {
                "title": {"tag": "plain_text", "content": f"🔐 {title}"},
                "template": "red",
            },
            "elements": [
                {
                    "tag": "div",
                    "fields": [
                        {"is_short": True, "text": {"tag": "lark_md", "content": f"**账号:** {context_key}"}},
                        {"is_short": True, "text": {"tag": "lark_md", "content": f"**任务:** #{task_id}"}},
                        {"is_short": True, "text": {"tag": "lark_md", "content": f"**类型:** {phase_label}"}},
                    ],
                },
                {"tag": "hr"},
                {"tag": "img", "img_key": image_key, "alt": {"tag": "plain_text", "content": "screenshot"}},
                {"tag": "hr"},
                {
                    "tag": "note",
                    "elements": [
                        {"tag": "plain_text", "content": f"👉 {instruction}"},
                    ],
                },
            ],
        }

        id_type = self._settings.receive_id_type
        url = f"{self._settings.api_base}/open-apis/im/v1/messages?receive_id_type={id_type}"
        body = {
            "receive_id": self._settings.receive_id,
            "msg_type": "interactive",
            "content": json.dumps(card),
        }
        try:
            resp = self._session.post(
                url, headers=self._auth_headers(), json=body, timeout=10,
            )
            resp.raise_for_status()
            data = resp.json()
            if data.get("code") != 0:
                logger.error("Feishu send error: %s", data)
                return None
            message_id = data["data"]["message_id"]
            # Cache chat_id from response (needed for polling messages in P2P)
            if not self._chat_id:
                self._chat_id = data["data"].get("chat_id", "")
            logger.info(
                "Feishu alert sent: %s (task=%d, phase=%s, msg=%s, chat=%s)",
                context_key, task_id, phase, message_id, self._chat_id,
            )
            return message_id
        except Exception as e:
            logger.error("Feishu send failed: %s", e)
            return None

    def send_text(self, text: str) -> bool:
        """Send a plain text message to the configured recipient.

        Args:
            text: Message text content.

        Returns:
            True on success.
        """
        id_type = self._settings.receive_id_type
        url = f"{self._settings.api_base}/open-apis/im/v1/messages?receive_id_type={id_type}"
        body = {
            "receive_id": self._settings.receive_id,
            "msg_type": "text",
            "content": json.dumps({"text": text}),
        }
        try:
            resp = self._session.post(
                url, headers=self._auth_headers(), json=body, timeout=10,
            )
            resp.raise_for_status()
            data = resp.json()
            if data.get("code") != 0:
                logger.warning("Feishu send_text error: %s", data)
                return False
            return True
        except Exception as e:
            logger.warning("Feishu send_text failed: %s", e)
            return False

    # ------------------------------------------------------------------
    # Reply polling & verification code extraction
    # ------------------------------------------------------------------

    def get_replies(self, message_id: str, since_timestamp: float) -> str | None:
        """Poll the chat for new user messages containing a verification code.

        Uses the chat message list API (works for both P2P and group chats),
        since the per-message replies API is not available in P2P conversations.

        Args:
            message_id: The alert message ID (unused but kept for interface stability).
            since_timestamp: Only consider messages newer than this (epoch seconds).

        Returns:
            Extracted verification code string, or None.
        """
        if not self._chat_id:
            logger.debug("Feishu: no chat_id yet, skipping poll")
            return None

        url = f"{self._settings.api_base}/open-apis/im/v1/messages"
        # start_time is in seconds (string), API returns newest first
        params = {
            "container_id_type": "chat",
            "container_id": self._chat_id,
            "page_size": 10,
            "sort_type": "ByCreateTimeDesc",
            "start_time": str(int(since_timestamp)),
        }
        try:
            resp = self._session.get(
                url, headers=self._auth_headers(), params=params, timeout=10,
            )
            resp.raise_for_status()
            data = resp.json()
            if data.get("code") != 0:
                logger.debug("Feishu get messages error: %s", data)
                return None

            items = data.get("data", {}).get("items", [])
            for item in items:
                # Skip bot's own messages
                sender = item.get("sender", {})
                if sender.get("sender_type") == "app":
                    continue

                # Only text messages
                if item.get("msg_type") != "text":
                    continue

                # Extract text content
                try:
                    content = json.loads(item.get("body", {}).get("content", "{}"))
                    text = content.get("text", "").strip()
                except (json.JSONDecodeError, AttributeError):
                    continue

                code = self.extract_verification_code(text)
                if code:
                    logger.info("Feishu verification code found: %s", code)
                    return code

            return None
        except Exception as e:
            logger.debug("Feishu get messages failed: %s", e)
            return None

    @staticmethod
    def extract_verification_code(text: str) -> str | None:
        """Extract a 4-8 digit verification code from text.

        Handles:
            - Pure digits: "123456"
            - With prefix: "code: 123456", "验证码 123456"
            - Ignores 11-digit phone numbers

        Args:
            text: Raw reply text.

        Returns:
            Code string if found, None otherwise.
        """
        text = text.strip()

        # Pure digits (4-8 chars) — most common case
        if re.fullmatch(r"\d{4,8}", text):
            return text

        # Look for digit sequences of 4-8 chars in longer text
        matches = re.findall(r"\d{4,8}", text)
        for match in matches:
            # Skip 11-digit phone numbers that partially matched
            # Check surrounding context for longer digit sequences
            start = text.find(match)
            before = text[max(0, start - 1) : start]
            after = text[start + len(match) : start + len(match) + 1]
            if before.isdigit() or after.isdigit():
                continue
            return match

        return None
