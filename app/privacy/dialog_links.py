"""Opaque links for Telegram. Decryption alone never grants access to VK."""
from __future__ import annotations

import json
import re
from urllib.parse import urlsplit

from app.core.config import settings
from app.privacy import crypto

_PATH = "/secure-dialog/"
_AAD_CLIENT = "manager-dialog-link"
_AAD_LABEL = "v1"
_VK_LINK = re.compile(r"https?://(?:www\.)?vk\.(?:com|ru)/gim(?P<group>\d+)\?sel=(?P<peer>\d+)(?!\d)")
_TOKEN = re.compile(r"[A-Za-z0-9_-]{40,512}\Z")


def group_id() -> str:
    value = settings.vk_group_id.removeprefix("club").removeprefix("public")
    if not value.isdigit():
        raise ValueError("VK group is not configured")
    return value


def origin() -> str:
    value = settings.dialog_links_origin.rstrip("/")
    url = urlsplit(value)
    if url.scheme != "https" or not url.netloc or url.path or url.query or url.fragment or url.username:
        raise ValueError("Dialog link origin must be an HTTPS origin")
    return value


def make_link(peer_id: int) -> str:
    if not 0 < int(peer_id) < 2**63:
        raise ValueError("Invalid dialog identifier")
    plain = json.dumps({"v": 1, "group": group_id(), "peer": int(peer_id)}, separators=(",", ":"))
    token = crypto.encrypt(plain, client_key=_AAD_CLIENT, label=_AAD_LABEL)
    token = token.replace("+", "-").replace("/", "_").rstrip("=")
    return origin() + _PATH + token


def destination(token: str) -> str:
    """Call only after checking the current authenticated user's permission."""
    if not _TOKEN.fullmatch(token):
        raise ValueError("Invalid dialog link")
    try:
        blob = token.replace("-", "+").replace("_", "/")
        blob += "=" * (-len(blob) % 4)
        data = json.loads(crypto.decrypt(blob, client_key=_AAD_CLIENT, label=_AAD_LABEL))
        if data.get("v") != 1 or data.get("group") != group_id():
            raise ValueError()
        peer = data["peer"]
        if type(peer) is not int or not 0 < peer < 2**63:
            raise ValueError()
    except Exception:
        raise ValueError("Invalid dialog link") from None
    return f"https://vk.com/gim{group_id()}?sel={peer}"


def protect_text(text: str) -> str:
    """Protect new messages and legacy queued cards at the Telegram boundary.

    No plaintext fallback if encryption is unavailable. Delivery can retry once
    configuration is repaired. Existing messages already sent are not modified.
    """
    if not settings.dialog_links_enabled:
        return text
    peers = set()
    def replace(match):
        if match["group"] != group_id():
            raise ValueError("Unexpected VK dialog group")
        peers.add(match["peer"])
        return make_link(int(match["peer"]))
    protected = _VK_LINK.sub(replace, text)
    # Some legacy cards repeat the identifier in a caption or CLI example.
    # Do this before returning; the original ID must not accompany its new link.
    for peer in peers:
        protected = re.sub(rf"(?<![\w-]){re.escape(peer)}(?![\w-])", "[клиент]", protected)
    return protected


def protect_payload(payload: dict) -> dict:
    """Text/caption/URL only: never rewrite Telegram routing or callback IDs."""
    if not settings.dialog_links_enabled:
        return payload
    peers = set()
    def collect(value, key=""):
        if isinstance(value, dict):
            for k, v in value.items():
                collect(v, k)
        elif isinstance(value, list):
            for v in value:
                collect(v)
        elif isinstance(value, str) and key in {"text", "caption", "url"}:
            peers.update(m["peer"] for m in _VK_LINK.finditer(value))
    collect(payload)
    def visit(value, key=""):
        if isinstance(value, dict):
            return {k: visit(v, k) for k, v in value.items()}
        if isinstance(value, list):
            return [visit(v) for v in value]
        if isinstance(value, str) and key in {"text", "caption", "url"}:
            result = protect_text(value)
            if key != "url":
                for peer in peers:
                    result = re.sub(rf"(?<![\w-]){re.escape(peer)}(?![\w-])", "[клиент]", result)
            return result
        return value
    return visit(payload)
