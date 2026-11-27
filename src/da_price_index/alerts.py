"""Minimal webhook alerting (Slack or Discord incoming webhooks)."""

from __future__ import annotations

import logging
from urllib.parse import urlparse

import httpx

from da_price_index.config import settings

logger = logging.getLogger(__name__)

# Discord rejects messages over 2000 characters; Slack's limit is far higher.
_MAX_MESSAGE_CHARS = 1900


def send_alert(message: str, webhook_url: str | None = None) -> bool:
    """Post `message` to the configured webhook. Never raises.

    Returns True if the webhook accepted it. With no webhook configured the
    message is only logged, so a missing URL can't crash the job it reports on.
    """
    url = webhook_url or settings.alert_webhook_url
    if not url:
        logger.warning("No alert webhook configured; alert not sent: %s", message)
        return False

    text = message if len(message) <= _MAX_MESSAGE_CHARS else message[:_MAX_MESSAGE_CHARS] + "…"
    host = urlparse(url).hostname or ""
    payload = {"content": text} if "discord" in host else {"text": text}

    try:
        response = httpx.post(url, json=payload, timeout=10.0)
        response.raise_for_status()
        return True
    except httpx.HTTPError as e:
        logger.error("Failed to deliver alert to webhook: %s", e)
        return False
