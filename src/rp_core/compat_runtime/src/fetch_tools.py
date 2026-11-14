# SPDX-License-Identifier: AGPL-3.0-only








from __future__ import annotations

import html
import json
import logging
import os
import re
import threading
import time
from html.parser import HTMLParser
from typing import Any
from urllib.parse import urlparse

import requests
from error_text import format_exception_inline


logger = logging.getLogger(__name__)

FETCH_MAX_BYTES = int(os.getenv("FETCH_MAX_BYTES", "524288"))
FETCH_MAX_CHARS = int(os.getenv("FETCH_MAX_CHARS", "28000"))
FETCH_TIMEOUT = float(os.getenv("FETCH_TIMEOUT", "20"))
FETCH_MIN_INTERVAL_SEC = float(os.getenv("FETCH_MIN_INTERVAL_SEC", "0.6"))
FETCH_MAX_CONCURRENT = max(1, int(os.getenv("FETCH_MAX_CONCURRENT", "2")))
FETCH_MAX_CALLS_PER_TURN = max(1, int(os.getenv("FETCH_MAX_CALLS_PER_TURN", "3")))
FETCH_USER_AGENT = (
    os.getenv("FETCH_USER_AGENT")
    or "RoleplayCore/1.0"
)

_RATE_LOCK = threading.Lock()
_LAST_FETCH_AT = 0.0
_CONCURRENCY = threading.Semaphore(FETCH_MAX_CONCURRENT)

_BLOCKED_SCHEMES = {"file", "ftp", "data", "javascript", "vbscript"}
_URL_RE = re.compile(r"https?://[^\s\]\)\"'<>]+", re.IGNORECASE)


class _HTMLTextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self._chunks: list[str] = []
        self._skip_depth = 0

    def handle_starttag(self, tag: str, attrs) -> None:
        if tag in ("script", "style", "noscript"):
            self._skip_depth += 1

    def handle_endtag(self, tag: str) -> None:
        if tag in ("script", "style", "noscript") and self._skip_depth > 0:
            self._skip_depth -= 1
        if tag in ("p", "br", "div", "li", "h1", "h2", "h3", "h4", "tr"):
            self._chunks.append("\n")

    def handle_data(self, data: str) -> None:
        if self._skip_depth == 0 and data:
            self._chunks.append(data)

    def text(self) -> str:
        return re.sub(r"\n{3,}", "\n\n", "".join(self._chunks)).strip()


def _throttle() -> None:
    global _LAST_FETCH_AT
    with _RATE_LOCK:
        now = time.monotonic()
        wait = FETCH_MIN_INTERVAL_SEC - (now - _LAST_FETCH_AT)
        if wait > 0:
            time.sleep(wait)
        _LAST_FETCH_AT = time.monotonic()


def _extra_headers() -> dict[str, str]:
    raw = (os.getenv("FETCH_EXTRA_HEADERS_JSON") or "").strip()
    if not raw:
        return {}
    try:
        obj = json.loads(raw)
        if isinstance(obj, dict):
            return {str(k): str(v) for k, v in obj.items()}
    except Exception as e:
        logger.warning(f"Invalid FETCH_EXTRA_HEADERS_JSON: {format_exception_inline(e)}")
    return {}


def _auth_headers_for_url(url: str) -> dict[str, str]:
    headers: dict[str, str] = {"User-Agent": FETCH_USER_AGENT, "Accept": "*/*"}
    headers.update(_extra_headers())

    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()
    token = (os.getenv("GITHUB_TOKEN") or os.getenv("GH_TOKEN") or "").strip()
    if not token:
        return headers

    if host in ("api.github.com", "raw.githubusercontent.com", "github.com"):
        headers["Authorization"] = f"Bearer {token}"
        if host == "api.github.com":
            headers["Accept"] = "application/vnd.github+json"
    return headers


def _validate_url(url: str) -> str:
    u = (url or "").strip()
    if not u:
        raise ValueError("url is required")
    parsed = urlparse(u)
    if parsed.scheme not in ("http", "https"):
        raise ValueError(f"unsupported URL scheme: {parsed.scheme or '(none)'}")
    if parsed.scheme in _BLOCKED_SCHEMES:
        raise ValueError(f"blocked scheme: {parsed.scheme}")
    if not parsed.netloc:
        raise ValueError("invalid URL: missing host")
    return u


def _detect_format(url: str, content_type: str, body: bytes) -> str:
    ct = (content_type or "").lower()
    path = (urlparse(url).path or "").lower()
    if "json" in ct or path.endswith(".json"):
        return "json"
    if "markdown" in ct or path.endswith((".md", ".markdown")):
        return "md"
    if "html" in ct or path.endswith((".html", ".htm")):
        return "html"
    if "text/plain" in ct or path.endswith(".txt"):
        return "txt"
    sample = body[:512]
    if sample.strip().startswith((b"{", b"[")):
        return "json"
    if b"<html" in sample.lower() or b"<!doctype html" in sample.lower():
        return "html"
    return "txt"


def _decode_body(body: bytes, content_type: str) -> str:
    charset = "utf-8"
    m = re.search(r"charset=([^\s;]+)", content_type or "", flags=re.IGNORECASE)
    if m:
        charset = m.group(1).strip("\"'")
    try:
        return body.decode(charset, errors="replace")
    except Exception:
        return body.decode("utf-8", errors="replace")


def _html_to_text(raw_html: str) -> str:
    parser = _HTMLTextExtractor()
    try:
        parser.feed(raw_html)
        parser.close()
        text = parser.text()
    except Exception:
        text = re.sub(r"<[^>]+>", " ", raw_html)
    return html.unescape(re.sub(r"[ \t]+\n", "\n", text)).strip()


def _format_body(text: str, fmt: str) -> str:
    fmt = (fmt or "auto").lower()
    if fmt == "json":
        try:
            obj = json.loads(text)
            return json.dumps(obj, ensure_ascii=False, indent=2)
        except Exception:
            return text
    if fmt == "html":
        return _html_to_text(text)

    return text


def fetch_url(
    url: str,
    *,
    format: str = "auto",
    max_chars: int | None = None,
    timeout: float | None = None,
) -> dict[str, Any]:
    """
    Fetch a public HTTP(S) URL. Returns a structured dict for tool consumers.
    """
    validated = _validate_url(url)
    max_chars = max(500, int(max_chars or FETCH_MAX_CHARS))
    timeout = float(timeout or FETCH_TIMEOUT)

    _CONCURRENCY.acquire()
    try:
        _throttle()
        resp = requests.get(
            validated,
            headers=_auth_headers_for_url(validated),
            timeout=timeout,
            allow_redirects=True,
            stream=True,
        )
        resp.raise_for_status()

        chunks: list[bytes] = []
        total = 0
        for chunk in resp.iter_content(chunk_size=16384):
            if not chunk:
                continue
            chunks.append(chunk)
            total += len(chunk)
            if total > FETCH_MAX_BYTES:
                break
        body = b"".join(chunks)
        truncated_bytes = total > FETCH_MAX_BYTES

        content_type = resp.headers.get("Content-Type", "")
        detected = _detect_format(validated, content_type, body)
        fmt = (format or "auto").lower()
        if fmt == "auto":
            fmt = detected

        text = _format_body(_decode_body(body, content_type), fmt)
        truncated_chars = len(text) > max_chars
        if truncated_chars:
            text = text[:max_chars] + "\n\n[TRUNCATED]"

        return {
            "ok": True,
            "url": validated,
            "final_url": str(resp.url),
            "status_code": resp.status_code,
            "content_type": content_type,
            "format": fmt,
            "chars": len(text),
            "truncated_bytes": truncated_bytes,
            "truncated_chars": truncated_chars,
            "content": text,
        }
    except requests.HTTPError as e:
        code = e.response.status_code if e.response is not None else None
        return {"ok": False, "url": validated, "error": f"HTTP {code}: {e}"}
    except Exception as e:
        return {"ok": False, "url": validated, "error": str(e)}
    finally:
        _CONCURRENCY.release()


def fetch_url_tool(
    url: str,
    format: str = "auto",
    max_chars: int | None = None,
) -> str:
    """String wrapper for LLM tool loops."""
    result = fetch_url(url, format=format, max_chars=max_chars)
    if not result.get("ok"):
        return f"[fetch_url] Error: {result.get('error', 'unknown')}"
    header = (
        f"[fetch_url] {result.get('final_url')} "
        f"({result.get('format')}, {result.get('chars')} chars"
    )
    if result.get("truncated_bytes") or result.get("truncated_chars"):
        header += ", truncated"
    header += ")\n---\n"
    return header + (result.get("content") or "")


def extract_urls_from_text(text: str, *, limit: int = 5) -> list[str]:
    if not text:
        return []
    seen: set[str] = set()
    out: list[str] = []
    for m in _URL_RE.finditer(text):
        u = m.group(0).rstrip(".,;)")
        if u not in seen:
            seen.add(u)
            out.append(u)
        if len(out) >= limit:
            break
    return out


def fetch_urls_from_search_evidence(
    evidence: str,
    *,
    max_urls: int | None = None,
    max_chars_each: int | None = None,
) -> str:
    """
    Supplement search-agent evidence by fetching a few result URLs.
    Respects the same rate limits as fetch_url.
    """
    urls = extract_urls_from_text(evidence, limit=max_urls or 2)
    if not urls:
        return ""
    blocks: list[str] = []
    for u in urls:
        try:
            blocks.append(fetch_url_tool(u, format="auto", max_chars=max_chars_each or 6000))
        except Exception as e:
            blocks.append(f"[fetch_url] {u} failed: {e}")
    return "\n\n".join(blocks)
