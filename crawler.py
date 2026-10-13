from __future__ import annotations

import argparse
import hashlib
import ipaddress
import json
import os
import re
import socket
import subprocess
import sys
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, Callable, Iterable
from urllib.parse import parse_qsl, quote, urlencode, urljoin, urlsplit, urlunsplit
from urllib.robotparser import RobotFileParser
from xml.etree import ElementTree

import app

try:
    import httpx
except ImportError:  # pragma: no cover - surfaced as a configuration error at runtime
    httpx = None

try:
    import trafilatura
except ImportError:  # pragma: no cover - fallback keeps basic HTML usable
    trafilatura = None


BRAVE_API_KEY = os.getenv("BRAVE_SEARCH_API_KEY", "")
USER_AGENT = os.getenv(
    "CRAWLER_USER_AGENT",
    "ZhiyuRAGBot/1.0 (private research crawler; respects robots.txt)",
)
MAX_PAGES = max(1, min(int(os.getenv("CRAWLER_MAX_PAGES", "200")), 1000))
REQUEST_TIMEOUT = max(3.0, float(os.getenv("CRAWLER_REQUEST_TIMEOUT", "20")))
HOST_DELAY = max(0.0, float(os.getenv("CRAWLER_HOST_DELAY", "3")))
HTML_MAX_BYTES = 5 * 1024 * 1024
PDF_MAX_BYTES = 20 * 1024 * 1024
MAX_REDIRECTS = 5
MAX_SEARCH_CALLS = 50
MIN_TEXT_LENGTH = 200
VERSION_RETENTION_DAYS = 180
VERSION_RETENTION_COUNT = 20
LOCK_TTL_HOURS = 4

ALLOWED_CONTENT_TYPES = {
    "text/html",
    "application/xhtml+xml",
    "application/pdf",
    "text/plain",
    "text/markdown",
    "application/xml",
    "text/xml",
    "application/rss+xml",
    "application/atom+xml",
}
TRACKING_PARAMS = {
    "fbclid",
    "gclid",
    "dclid",
    "msclkid",
    "mc_cid",
    "mc_eid",
    "ref_src",
    "spm",
}
BLOCKED_HOSTS = {
    "localhost",
    "localhost.localdomain",
    "metadata.google.internal",
    "metadata.azure.internal",
}


class CrawlerError(RuntimeError):
    pass


class BusyError(CrawlerError):
    pass


class FetchError(CrawlerError):
    pass


@dataclass
class FetchResult:
    url: str
    status_code: int
    content_type: str
    content: bytes
    headers: dict[str, str]
    not_modified: bool = False


class _TextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.title_parts: list[str] = []
        self._ignored = 0
        self._in_title = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in {"script", "style", "noscript", "svg", "canvas", "nav", "footer"}:
            self._ignored += 1
        if tag == "title":
            self._in_title = True
        if tag in {"p", "div", "section", "article", "li", "br", "h1", "h2", "h3", "h4"}:
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in {"script", "style", "noscript", "svg", "canvas", "nav", "footer"} and self._ignored:
            self._ignored -= 1
        if tag == "title":
            self._in_title = False
        if tag in {"p", "div", "section", "article", "li", "h1", "h2", "h3", "h4"}:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        if self._ignored:
            return
        if self._in_title:
            self.title_parts.append(data)
        self.parts.append(data)

    def result(self) -> tuple[str, str]:
        return normalize_text(" ".join(self.parts)), normalize_text(" ".join(self.title_parts))


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def parse_utc(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def json_list(value: Any) -> list[str]:
    if isinstance(value, list):
        return [str(item) for item in value]
    if isinstance(value, str):
        try:
            decoded = json.loads(value)
        except json.JSONDecodeError:
            return []
        return [str(item) for item in decoded] if isinstance(decoded, list) else []
    return []


def init_crawler_db() -> None:
    app.init_db()
    with app.connect_db() as connection:
        columns = {row["name"] for row in connection.execute("PRAGMA table_info(documents)")}
        additions = {
            "source_type": "TEXT NOT NULL DEFAULT 'upload'",
            "source_url": "TEXT",
            "content_hash": "TEXT",
            "updated_at": "TEXT",
        }
        for name, definition in additions.items():
            if name not in columns:
                connection.execute(f"ALTER TABLE documents ADD COLUMN {name} {definition}")
        connection.execute("UPDATE documents SET updated_at = created_at WHERE updated_at IS NULL")
        connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS crawl_sources (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                keywords_json TEXT NOT NULL,
                allowed_domains_json TEXT NOT NULL,
                feed_urls_json TEXT NOT NULL DEFAULT '[]',
                search_lang TEXT NOT NULL DEFAULT 'zh-hans',
                country TEXT NOT NULL DEFAULT 'CN',
                max_pages INTEGER NOT NULL DEFAULT 200,
                enabled INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS crawl_candidates (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                source_id INTEGER NOT NULL REFERENCES crawl_sources(id) ON DELETE CASCADE,
                canonical_url TEXT NOT NULL,
                domain TEXT NOT NULL,
                title TEXT NOT NULL DEFAULT '',
                snippet TEXT NOT NULL DEFAULT '',
                status TEXT NOT NULL DEFAULT 'pending',
                discovered_at TEXT NOT NULL,
                decided_at TEXT,
                UNIQUE(source_id, canonical_url)
            );

            CREATE TABLE IF NOT EXISTS web_pages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                canonical_url TEXT NOT NULL UNIQUE,
                domain TEXT NOT NULL,
                document_id INTEGER REFERENCES documents(id) ON DELETE SET NULL,
                title TEXT NOT NULL DEFAULT '',
                content_type TEXT,
                etag TEXT,
                last_modified TEXT,
                content_hash TEXT,
                approved_once INTEGER NOT NULL DEFAULT 0,
                first_seen_at TEXT NOT NULL,
                last_seen_at TEXT NOT NULL,
                last_fetched_at TEXT,
                status TEXT NOT NULL DEFAULT 'pending',
                error TEXT,
                http_status INTEGER
            );

            CREATE TABLE IF NOT EXISTS source_pages (
                source_id INTEGER NOT NULL REFERENCES crawl_sources(id) ON DELETE CASCADE,
                page_id INTEGER NOT NULL REFERENCES web_pages(id) ON DELETE CASCADE,
                discovered_at TEXT NOT NULL,
                PRIMARY KEY(source_id, page_id)
            );

            CREATE TABLE IF NOT EXISTS web_versions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                page_id INTEGER NOT NULL REFERENCES web_pages(id) ON DELETE CASCADE,
                content_hash TEXT NOT NULL,
                title TEXT NOT NULL DEFAULT '',
                content TEXT NOT NULL,
                content_type TEXT NOT NULL,
                fetched_at TEXT NOT NULL,
                UNIQUE(page_id, content_hash)
            );

            CREATE TABLE IF NOT EXISTS crawl_jobs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                trigger_type TEXT NOT NULL,
                source_id INTEGER REFERENCES crawl_sources(id) ON DELETE SET NULL,
                status TEXT NOT NULL,
                started_at TEXT NOT NULL,
                finished_at TEXT,
                discovered INTEGER NOT NULL DEFAULT 0,
                fetched INTEGER NOT NULL DEFAULT 0,
                added INTEGER NOT NULL DEFAULT 0,
                updated INTEGER NOT NULL DEFAULT 0,
                unchanged INTEGER NOT NULL DEFAULT 0,
                skipped INTEGER NOT NULL DEFAULT 0,
                failed INTEGER NOT NULL DEFAULT 0,
                pending_review INTEGER NOT NULL DEFAULT 0,
                message TEXT
            );

            CREATE TABLE IF NOT EXISTS crawl_lock (
                id INTEGER PRIMARY KEY CHECK(id = 1),
                owner TEXT NOT NULL,
                acquired_at TEXT NOT NULL
            );

            CREATE INDEX IF NOT EXISTS idx_candidates_status ON crawl_candidates(status, discovered_at);
            CREATE INDEX IF NOT EXISTS idx_pages_fetched ON web_pages(last_fetched_at);
            CREATE INDEX IF NOT EXISTS idx_versions_page ON web_versions(page_id, fetched_at DESC);
            CREATE INDEX IF NOT EXISTS idx_jobs_started ON crawl_jobs(started_at DESC);
            """
        )


def normalize_domain(value: str) -> str:
    value = value.strip().lower()
    if not value:
        raise ValueError("可信域名不能为空")
    if "://" in value:
        parsed = urlsplit(value)
        value = parsed.hostname or ""
    else:
        value = value.split("/", 1)[0]
        if ":" in value and not value.startswith("["):
            value = value.rsplit(":", 1)[0]
    value = value.strip(".")
    if not value or value in BLOCKED_HOSTS or "." not in value:
        raise ValueError(f"无效的公开域名：{value or '(空)'}")
    try:
        value = value.encode("idna").decode("ascii")
    except UnicodeError as exc:
        raise ValueError(f"无效域名：{value}") from exc
    try:
        address = ipaddress.ip_address(value)
        if not address.is_global:
            raise ValueError("可信域名不能是非公网 IP 地址")
        return value
    except ValueError as exc:
        if "非公网" in str(exc):
            raise
    labels = value.split(".")
    if len(value) > 253 or any(
        not label or len(label) > 63 or not re.fullmatch(r"[a-z0-9](?:[a-z0-9-]*[a-z0-9])?", label)
        for label in labels
    ):
        raise ValueError(f"无效域名：{value}")
    return value


def normalize_url(value: str, assume_https: bool = False) -> str:
    value = value.strip()
    if assume_https and "://" not in value:
        value = "https://" + value
    parsed = urlsplit(value)
    if parsed.scheme.lower() not in {"http", "https"}:
        raise ValueError("只允许 HTTP 或 HTTPS URL")
    if parsed.username or parsed.password:
        raise ValueError("URL 不得包含用户名或密码")
    host = (parsed.hostname or "").strip(".").lower()
    if not host:
        raise ValueError("URL 缺少域名")
    try:
        host = host.encode("idna").decode("ascii")
    except UnicodeError as exc:
        raise ValueError("URL 域名无效") from exc
    try:
        port = parsed.port
    except ValueError as exc:
        raise ValueError("URL 端口无效") from exc
    if port not in {None, 80, 443}:
        raise ValueError("只允许公开网站的 80/443 端口")
    netloc = host
    if port and not ((parsed.scheme.lower() == "http" and port == 80) or (parsed.scheme.lower() == "https" and port == 443)):
        netloc = f"{host}:{port}"
    path = re.sub(r"/{2,}", "/", parsed.path or "/")
    path = quote(path, safe="/%:@!$&'()*+,;=-._~")
    clean_query = []
    for key, value_part in parse_qsl(parsed.query, keep_blank_values=True):
        lowered = key.lower()
        if lowered.startswith("utm_") or lowered in TRACKING_PARAMS:
            continue
        clean_query.append((key, value_part))
    clean_query.sort()
    return urlunsplit((parsed.scheme.lower(), netloc, path, urlencode(clean_query, doseq=True), ""))


def domain_allowed(host: str, domains: Iterable[str]) -> bool:
    host = host.lower().strip(".")
    return any(host == domain or host.endswith("." + domain) for domain in domains)


def validate_public_url(
    value: str,
    resolver: Callable[..., list[tuple[Any, ...]]] = socket.getaddrinfo,
) -> str:
    url = normalize_url(value)
    host = (urlsplit(url).hostname or "").lower()
    if host in BLOCKED_HOSTS or host.endswith(".localhost"):
        raise ValueError("禁止访问本机或元数据地址")
    try:
        literal = ipaddress.ip_address(host.strip("[]"))
        addresses = [literal]
    except ValueError:
        try:
            records = resolver(host, None, type=socket.SOCK_STREAM)
        except OSError as exc:
            raise ValueError(f"域名解析失败：{host}") from exc
        addresses = []
        for record in records:
            try:
                addresses.append(ipaddress.ip_address(record[4][0]))
            except (ValueError, IndexError):
                continue
    if not addresses or any(not address.is_global for address in addresses):
        raise ValueError("目标解析到了非公网地址，已阻止访问")
    return url


def normalize_text(text: str) -> str:
    text = text.replace("\x00", "").replace("\r\n", "\n").replace("\r", "\n")
    text = re.sub(r"[\t ]+", " ", text)
    text = re.sub(r" *\n *", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def decode_body(content: bytes, content_type: str) -> str:
    charset_match = re.search(r"charset=([\w.-]+)", content_type, flags=re.I)
    encodings = [charset_match.group(1)] if charset_match else []
    encodings.extend(["utf-8", "gb18030", "big5"])
    for encoding in encodings:
        try:
            return content.decode(encoding)
        except (UnicodeDecodeError, LookupError):
            continue
    return content.decode("utf-8", errors="replace")


def extract_html(content: bytes, url: str, content_type: str = "text/html") -> tuple[str, str]:
    markup = decode_body(content, content_type)
    if trafilatura is not None:
        extracted = trafilatura.extract(
            markup,
            url=url,
            include_comments=False,
            include_tables=True,
            include_links=False,
            output_format="txt",
            favor_precision=True,
        )
        metadata = trafilatura.extract_metadata(markup, default_url=url)
        title = normalize_text(getattr(metadata, "title", "") or "") if metadata else ""
        if extracted:
            return normalize_text(extracted), title
    fallback = _TextExtractor()
    fallback.feed(markup)
    return fallback.result()


def page_title_from_url(url: str) -> str:
    parsed = urlsplit(url)
    name = Path(parsed.path).name
    return name or parsed.hostname or "网页资料"


class SafeFetcher:
    def __init__(self, client: Any | None = None, resolver: Callable[..., list[tuple[Any, ...]]] = socket.getaddrinfo):
        if client is None and httpx is None:
            raise CrawlerError("缺少 httpx，请执行：pip install -r requirements.txt")
        self.client = client or httpx.Client(
            headers={"User-Agent": USER_AGENT, "Accept": "text/html,application/pdf,text/plain,application/xml;q=0.8,*/*;q=0.2"},
            timeout=httpx.Timeout(REQUEST_TIMEOUT),
            follow_redirects=False,
            verify=True,
        )
        self.resolver = resolver
        self._host_locks: dict[str, threading.Lock] = {}
        self._host_last: dict[str, float] = {}
        self._state_lock = threading.Lock()
        self._robots_cache: dict[str, tuple[float, RobotFileParser | None]] = {}

    def close(self) -> None:
        close = getattr(self.client, "close", None)
        if close:
            close()

    def _host_lock(self, host: str) -> threading.Lock:
        with self._state_lock:
            return self._host_locks.setdefault(host, threading.Lock())

    def _request_once(self, url: str, headers: dict[str, str], max_bytes: int) -> FetchResult:
        host = urlsplit(url).hostname or ""
        with self._host_lock(host):
            elapsed = time.monotonic() - self._host_last.get(host, 0.0)
            if elapsed < HOST_DELAY:
                time.sleep(HOST_DELAY - elapsed)
            try:
                with self.client.stream("GET", url, headers=headers) as response:
                    status = int(response.status_code)
                    response_headers = {key.lower(): value for key, value in response.headers.items()}
                    response_type = response_headers.get("content-type", "").split(";", 1)[0].strip().lower()
                    effective_limit = max_bytes
                    if response_type != "application/pdf":
                        effective_limit = min(effective_limit, HTML_MAX_BYTES)
                    declared = response_headers.get("content-length")
                    if declared and int(declared) > effective_limit:
                        raise FetchError("响应体超过大小限制")
                    body = bytearray()
                    if status != 304:
                        for block in response.iter_bytes():
                            body.extend(block)
                            if len(body) > effective_limit:
                                raise FetchError("响应体超过大小限制")
                self._host_last[host] = time.monotonic()
                content_type = response_headers.get("content-type", "").split(";", 1)[0].strip().lower()
                return FetchResult(url, status, content_type, bytes(body), response_headers, status == 304)
            except FetchError:
                self._host_last[host] = time.monotonic()
                raise
            except Exception as exc:
                self._host_last[host] = time.monotonic()
                if httpx is not None and isinstance(exc, httpx.HTTPError):
                    raise FetchError(f"网络请求失败：{exc}") from exc
                raise

    def _request_with_retry(self, url: str, headers: dict[str, str], max_bytes: int) -> FetchResult:
        last_error: Exception | None = None
        for attempt in range(3):
            try:
                result = self._request_once(url, headers, max_bytes)
                if result.status_code not in {429, 500, 502, 503, 504}:
                    return result
                retry_after = result.headers.get("retry-after", "")
                try:
                    delay = min(60.0, max(0.0, float(retry_after)))
                except ValueError:
                    delay = float(2 ** attempt)
                last_error = FetchError(f"网站返回 HTTP {result.status_code}")
                if attempt < 2:
                    time.sleep(delay)
            except FetchError as exc:
                last_error = exc
                if attempt < 2:
                    time.sleep(float(2 ** attempt))
        raise last_error or FetchError("请求失败")

    def _robots_allowed(self, url: str) -> bool:
        parsed = urlsplit(url)
        origin = f"{parsed.scheme}://{parsed.netloc}"
        now = time.monotonic()
        with self._state_lock:
            cached = self._robots_cache.get(origin)
        if cached and cached[0] > now:
            parser = cached[1]
            return bool(parser and parser.can_fetch(USER_AGENT, url))

        robots_url = origin + "/robots.txt"
        try:
            validate_public_url(robots_url, self.resolver)
            result = self._request_with_retry(robots_url, {}, 512 * 1024)
            if result.status_code in {401, 403} or result.status_code >= 500:
                parser = None
            elif result.status_code == 404:
                parser = RobotFileParser()
                parser.parse([])
            elif 200 <= result.status_code < 300:
                parser = RobotFileParser()
                parser.set_url(robots_url)
                parser.parse(decode_body(result.content, result.headers.get("content-type", "text/plain")).splitlines())
            else:
                parser = None
        except (CrawlerError, ValueError):
            parser = None
        with self._state_lock:
            self._robots_cache[origin] = (now + 24 * 3600, parser)
        return bool(parser and parser.can_fetch(USER_AGENT, url))

    def fetch(
        self,
        value: str,
        allowed_domains: Iterable[str],
        approved_url: str | None = None,
        conditional_headers: dict[str, str] | None = None,
        check_robots: bool = True,
        max_bytes: int | None = None,
    ) -> FetchResult:
        current = normalize_url(value)
        approved_url = normalize_url(approved_url) if approved_url else None
        domains = list(allowed_domains)
        headers = conditional_headers or {}
        for redirect_index in range(MAX_REDIRECTS + 1):
            current = validate_public_url(current, self.resolver)
            host = urlsplit(current).hostname or ""
            if not domain_allowed(host, domains) and current != approved_url:
                raise FetchError("目标或重定向地址不在可信域名中")
            if check_robots and not self._robots_allowed(current):
                raise FetchError("robots.txt 未允许抓取此地址")
            result = self._request_with_retry(current, headers, max_bytes or PDF_MAX_BYTES)
            if result.status_code in {301, 302, 303, 307, 308}:
                location = result.headers.get("location")
                if not location:
                    raise FetchError("重定向响应缺少 Location")
                if redirect_index >= MAX_REDIRECTS:
                    raise FetchError("重定向次数超过限制")
                current = urljoin(current, location)
                continue
            if result.status_code == 304:
                return result
            if not 200 <= result.status_code < 300:
                raise FetchError(f"网站返回 HTTP {result.status_code}")
            content_type = result.content_type
            if content_type not in ALLOWED_CONTENT_TYPES:
                if current.lower().endswith(".pdf"):
                    result.content_type = "application/pdf"
                else:
                    raise FetchError(f"不支持的内容类型：{content_type or '未知'}")
            limit = PDF_MAX_BYTES if result.content_type == "application/pdf" else HTML_MAX_BYTES
            if len(result.content) > limit:
                raise FetchError("响应体超过此内容类型的大小限制")
            result.url = current
            return result
        raise FetchError("重定向次数超过限制")


class BraveSearchClient:
    endpoint = "https://api.search.brave.com/res/v1/web/search"

    def __init__(self, api_key: str = BRAVE_API_KEY, client: Any | None = None):
        self.api_key = api_key
        if client is None and httpx is None:
            raise CrawlerError("缺少 httpx，请执行：pip install -r requirements.txt")
        self.client = client or httpx.Client(timeout=httpx.Timeout(REQUEST_TIMEOUT), follow_redirects=False)
        self._last_request = 0.0

    def close(self) -> None:
        close = getattr(self.client, "close", None)
        if close:
            close()

    def search(self, query: str, country: str, search_lang: str) -> list[dict[str, str]]:
        if not self.api_key:
            raise CrawlerError("尚未配置 BRAVE_SEARCH_API_KEY")
        elapsed = time.monotonic() - self._last_request
        if elapsed < 1.0:
            time.sleep(1.0 - elapsed)
        last_error: Exception | None = None
        for attempt in range(3):
            try:
                response = self.client.get(
                    self.endpoint,
                    headers={"Accept": "application/json", "X-Subscription-Token": self.api_key},
                    params={
                        "q": query[:600],
                        "count": 20,
                        "freshness": "pw",
                        "country": country.upper(),
                        "search_lang": search_lang.lower(),
                    },
                )
                self._last_request = time.monotonic()
                if response.status_code in {429, 500, 502, 503, 504}:
                    retry_after = response.headers.get("retry-after", "")
                    try:
                        delay = min(60.0, float(retry_after))
                    except ValueError:
                        delay = float(2 ** attempt)
                    last_error = CrawlerError(f"Brave Search 返回 HTTP {response.status_code}")
                    if attempt < 2:
                        time.sleep(delay)
                    continue
                response.raise_for_status()
                data = response.json()
                results = []
                for item in data.get("web", {}).get("results", []):
                    if item.get("url"):
                        results.append(
                            {
                                "url": str(item["url"]),
                                "title": str(item.get("title", "")),
                                "snippet": str(item.get("description", "")),
                            }
                        )
                return results
            except Exception as exc:
                if httpx is not None and isinstance(exc, httpx.HTTPError):
                    last_error = CrawlerError(f"Brave Search 请求失败：{exc}")
                else:
                    last_error = exc
                if attempt < 2:
                    time.sleep(float(2 ** attempt))
        raise CrawlerError(str(last_error or "Brave Search 请求失败"))


def validate_source_payload(payload: dict[str, Any], partial: bool = False) -> dict[str, Any]:
    result: dict[str, Any] = {}
    if not partial or "name" in payload:
        name = str(payload.get("name", "")).strip()
        if not name or len(name) > 100:
            raise ValueError("主题名称不能为空且不能超过 100 字")
        result["name"] = name
    for field, max_items in (("keywords", 20), ("allowed_domains", 20), ("feed_urls", 20)):
        if partial and field not in payload:
            continue
        values = payload.get(field, [])
        if not isinstance(values, list):
            raise ValueError(f"{field} 必须是数组")
        cleaned = []
        for value in values:
            item = str(value).strip()
            if item and item not in cleaned:
                cleaned.append(item)
        if len(cleaned) > max_items:
            raise ValueError(f"{field} 最多允许 {max_items} 项")
        if field == "allowed_domains":
            cleaned = [normalize_domain(item) for item in cleaned]
        elif field == "feed_urls":
            cleaned = [normalize_url(item, assume_https=True) for item in cleaned]
        elif any(len(item) > 200 for item in cleaned):
            raise ValueError("单个关键词不能超过 200 字")
        result[field] = cleaned
    if not partial or "search_lang" in payload:
        result["search_lang"] = str(payload.get("search_lang", "zh-hans")).strip().lower()[:20] or "zh-hans"
    if not partial or "country" in payload:
        country = str(payload.get("country", "CN")).strip().upper()
        if not re.fullmatch(r"[A-Z]{2}", country):
            raise ValueError("country 必须是两位国家代码")
        result["country"] = country
    if not partial or "max_pages" in payload:
        try:
            requested_max = int(payload.get("max_pages", MAX_PAGES))
        except (TypeError, ValueError) as exc:
            raise ValueError("max_pages 必须是整数") from exc
        result["max_pages"] = max(1, min(requested_max, MAX_PAGES))
    if not partial or "enabled" in payload:
        result["enabled"] = 1 if bool(payload.get("enabled", True)) else 0
    return result


def serialize_source(row: Any) -> dict[str, Any]:
    data = dict(row)
    data["keywords"] = json_list(data.pop("keywords_json"))
    data["allowed_domains"] = json_list(data.pop("allowed_domains_json"))
    data["feed_urls"] = json_list(data.pop("feed_urls_json"))
    data["enabled"] = bool(data["enabled"])
    return data


def create_source(payload: dict[str, Any]) -> dict[str, Any]:
    init_crawler_db()
    data = validate_source_payload(payload)
    if not data["keywords"] and not data["feed_urls"]:
        raise ValueError("至少填写一个关键词或订阅地址")
    if not data["allowed_domains"]:
        raise ValueError("至少填写一个可信域名")
    now = utc_now()
    with app.connect_db() as connection:
        cursor = connection.execute(
            """
            INSERT INTO crawl_sources(
                name, keywords_json, allowed_domains_json, feed_urls_json,
                search_lang, country, max_pages, enabled, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                data["name"], json.dumps(data["keywords"], ensure_ascii=False),
                json.dumps(data["allowed_domains"]), json.dumps(data["feed_urls"]),
                data["search_lang"], data["country"], data["max_pages"], data["enabled"], now, now,
            ),
        )
        source_id = int(cursor.lastrowid)
        row = connection.execute("SELECT * FROM crawl_sources WHERE id = ?", (source_id,)).fetchone()
    return serialize_source(row)


def list_sources() -> list[dict[str, Any]]:
    init_crawler_db()
    with app.connect_db() as connection:
        rows = connection.execute(
            """
            SELECT s.*,
                   (SELECT COUNT(*) FROM source_pages sp WHERE sp.source_id = s.id) AS pages,
                   (SELECT COUNT(*) FROM crawl_candidates c WHERE c.source_id = s.id AND c.status = 'pending') AS pending
            FROM crawl_sources s ORDER BY s.id DESC
            """
        ).fetchall()
    return [serialize_source(row) for row in rows]


def update_source(source_id: int, payload: dict[str, Any]) -> dict[str, Any]:
    init_crawler_db()
    data = validate_source_payload(payload, partial=True)
    if not data:
        raise ValueError("没有可更新的字段")
    assignments = []
    values: list[Any] = []
    json_fields = {"keywords", "allowed_domains", "feed_urls"}
    for name, value in data.items():
        column = f"{name}_json" if name in json_fields else name
        assignments.append(f"{column} = ?")
        values.append(json.dumps(value, ensure_ascii=False) if name in json_fields else value)
    assignments.append("updated_at = ?")
    values.extend([utc_now(), source_id])
    with app.connect_db() as connection:
        cursor = connection.execute(
            f"UPDATE crawl_sources SET {', '.join(assignments)} WHERE id = ?", values
        )
        if not cursor.rowcount:
            raise ValueError("采集主题不存在")
        row = connection.execute("SELECT * FROM crawl_sources WHERE id = ?", (source_id,)).fetchone()
        serialized = serialize_source(row)
        if not serialized["keywords"] and not serialized["feed_urls"]:
            raise ValueError("至少保留一个关键词或订阅地址")
        if not serialized["allowed_domains"]:
            raise ValueError("至少保留一个可信域名")
    return serialized


def delete_source(source_id: int) -> bool:
    init_crawler_db()
    with app.connect_db() as connection:
        cursor = connection.execute("DELETE FROM crawl_sources WHERE id = ?", (source_id,))
        return cursor.rowcount > 0


def list_jobs(limit: int = 30) -> list[dict[str, Any]]:
    init_crawler_db()
    limit = max(1, min(int(limit), 100))
    with app.connect_db() as connection:
        rows = connection.execute(
            "SELECT * FROM crawl_jobs ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
    return [dict(row) for row in rows]


def list_candidates(status: str = "pending", limit: int = 100) -> list[dict[str, Any]]:
    init_crawler_db()
    if status not in {"pending", "approved", "rejected", "all"}:
        raise ValueError("候选状态无效")
    params: list[Any] = []
    where = ""
    if status != "all":
        where = "WHERE c.status = ?"
        params.append(status)
    params.append(max(1, min(int(limit), 500)))
    with app.connect_db() as connection:
        rows = connection.execute(
            f"""
            SELECT c.*, s.name AS source_name FROM crawl_candidates c
            JOIN crawl_sources s ON s.id = c.source_id
            {where} ORDER BY c.discovered_at DESC LIMIT ?
            """,
            params,
        ).fetchall()
    return [dict(row) for row in rows]


def ensure_page(
    source_id: int,
    url: str,
    title: str = "",
    approved_once: bool = False,
) -> tuple[int, bool]:
    canonical = normalize_url(url)
    domain = urlsplit(canonical).hostname or ""
    now = utc_now()
    with app.connect_db() as connection:
        row = connection.execute("SELECT id FROM web_pages WHERE canonical_url = ?", (canonical,)).fetchone()
        created = row is None
        if created:
            cursor = connection.execute(
                """
                INSERT INTO web_pages(
                    canonical_url, domain, title, approved_once, first_seen_at, last_seen_at, status
                ) VALUES (?, ?, ?, ?, ?, ?, 'pending')
                """,
                (canonical, domain, title[:300], int(approved_once), now, now),
            )
            page_id = int(cursor.lastrowid)
        else:
            page_id = int(row["id"])
            connection.execute(
                """
                UPDATE web_pages SET last_seen_at = ?, title = CASE WHEN ? <> '' THEN ? ELSE title END,
                       approved_once = MAX(approved_once, ?)
                WHERE id = ?
                """,
                (now, title, title[:300], int(approved_once), page_id),
            )
        connection.execute(
            "INSERT OR IGNORE INTO source_pages(source_id, page_id, discovered_at) VALUES (?, ?, ?)",
            (source_id, page_id, now),
        )
    return page_id, created


def upsert_candidate(source_id: int, url: str, title: str, snippet: str) -> None:
    canonical = normalize_url(url)
    domain = urlsplit(canonical).hostname or ""
    with app.connect_db() as connection:
        connection.execute(
            """
            INSERT INTO crawl_candidates(
                source_id, canonical_url, domain, title, snippet, status, discovered_at
            ) VALUES (?, ?, ?, ?, ?, 'pending', ?)
            ON CONFLICT(source_id, canonical_url) DO UPDATE SET
                title = excluded.title, snippet = excluded.snippet,
                discovered_at = excluded.discovered_at,
                status = CASE WHEN crawl_candidates.status = 'rejected' THEN 'rejected' ELSE crawl_candidates.status END
            """,
            (source_id, canonical, domain, title[:300], snippet[:1200], utc_now()),
        )


def decide_candidate(candidate_id: int, action: str) -> dict[str, Any]:
    init_crawler_db()
    if action not in {"approve_url", "trust_domain", "reject"}:
        raise ValueError("审核操作无效")
    with app.connect_db() as connection:
        row = connection.execute("SELECT * FROM crawl_candidates WHERE id = ?", (candidate_id,)).fetchone()
    if not row:
        raise ValueError("候选页面不存在")
    if action == "reject":
        with app.connect_db() as connection:
            connection.execute(
                "UPDATE crawl_candidates SET status = 'rejected', decided_at = ? WHERE id = ?",
                (utc_now(), candidate_id),
            )
        return {"ok": True, "action": action}
    if action == "trust_domain":
        with app.connect_db() as connection:
            source = connection.execute("SELECT * FROM crawl_sources WHERE id = ?", (row["source_id"],)).fetchone()
        domains = json_list(source["allowed_domains_json"])
        if row["domain"] not in domains:
            domains.append(row["domain"])
            update_source(int(row["source_id"]), {"allowed_domains": domains})
    page_id, _ = ensure_page(
        int(row["source_id"]), row["canonical_url"], row["title"], approved_once=action == "approve_url"
    )
    with app.connect_db() as connection:
        connection.execute(
            "UPDATE crawl_candidates SET status = 'approved', decided_at = ? WHERE id = ?",
            (utc_now(), candidate_id),
        )
    return {"ok": True, "action": action, "page_id": page_id}


def get_page_versions(page_id: int) -> dict[str, Any]:
    init_crawler_db()
    with app.connect_db() as connection:
        page = connection.execute("SELECT * FROM web_pages WHERE id = ?", (page_id,)).fetchone()
        if not page:
            raise ValueError("网页不存在")
        versions = connection.execute(
            """
            SELECT id, content_hash, title, content_type, fetched_at,
                   LENGTH(content) AS characters, SUBSTR(content, 1, 500) AS excerpt
            FROM web_versions WHERE page_id = ? ORDER BY fetched_at DESC
            """,
            (page_id,),
        ).fetchall()
    return {"page": dict(page), "versions": [dict(row) for row in versions]}


def parse_feed_urls(content: bytes, base_url: str) -> list[str]:
    try:
        root = ElementTree.fromstring(content)
    except ElementTree.ParseError:
        return []
    urls: list[str] = []
    for element in root.iter():
        local_name = element.tag.rsplit("}", 1)[-1].lower()
        candidate = ""
        if local_name in {"loc", "link"} and element.text:
            candidate = element.text.strip()
        if local_name == "link" and element.attrib.get("href"):
            candidate = element.attrib["href"].strip()
        if candidate:
            try:
                normalized = normalize_url(urljoin(base_url, candidate))
            except ValueError:
                continue
            if normalized not in urls:
                urls.append(normalized)
            if len(urls) >= 500:
                break
    return urls


def source_from_row(row: Any) -> dict[str, Any]:
    data = serialize_source(row)
    data["id"] = int(data["id"])
    return data


def discover_source(
    source: dict[str, Any],
    search_client: Any,
    fetcher: Any,
    search_budget: list[int],
) -> tuple[list[int], list[str]]:
    new_page_ids: list[int] = []
    warnings: list[str] = []
    domains = source["allowed_domains"]
    for keyword in source["keywords"]:
        if search_budget[0] >= MAX_SEARCH_CALLS:
            warnings.append("已达到单次 50 次搜索请求上限")
            break
        search_budget[0] += 1
        try:
            results = search_client.search(keyword, source["country"], source["search_lang"])
        except Exception as exc:
            warnings.append(str(exc))
            break
        for result in results:
            try:
                canonical = normalize_url(result["url"])
            except ValueError:
                continue
            host = urlsplit(canonical).hostname or ""
            if domain_allowed(host, domains):
                page_id, created = ensure_page(source["id"], canonical, result.get("title", ""))
                if created:
                    new_page_ids.append(page_id)
            else:
                upsert_candidate(source["id"], canonical, result.get("title", ""), result.get("snippet", ""))

    feed_urls = list(source["feed_urls"])
    for domain in domains:
        default_sitemap = f"https://{domain}/sitemap.xml"
        if default_sitemap not in feed_urls:
            feed_urls.append(default_sitemap)
    for feed_url in feed_urls[:20]:
        try:
            result = fetcher.fetch(feed_url, domains, max_bytes=HTML_MAX_BYTES)
            for discovered_url in parse_feed_urls(result.content, result.url):
                host = urlsplit(discovered_url).hostname or ""
                if not domain_allowed(host, domains):
                    continue
                if discovered_url.lower().endswith((".xml", ".xml.gz")):
                    continue
                page_id, created = ensure_page(source["id"], discovered_url)
                if created:
                    new_page_ids.append(page_id)
        except Exception as exc:
            if feed_url in source["feed_urls"]:
                warnings.append(f"订阅读取失败 {feed_url}: {exc}")
    return list(dict.fromkeys(new_page_ids)), warnings


def page_task(page_id: int) -> dict[str, Any] | None:
    with app.connect_db() as connection:
        page = connection.execute("SELECT * FROM web_pages WHERE id = ?", (page_id,)).fetchone()
        if not page:
            return None
        source_rows = connection.execute(
            """
            SELECT s.allowed_domains_json FROM crawl_sources s
            JOIN source_pages sp ON sp.source_id = s.id WHERE sp.page_id = ?
            """,
            (page_id,),
        ).fetchall()
    domains: list[str] = []
    for source_row in source_rows:
        for domain in json_list(source_row["allowed_domains_json"]):
            if domain not in domains:
                domains.append(domain)
    data = dict(page)
    data["allowed_domains"] = domains
    return data


def fetch_page(fetcher: Any, task: dict[str, Any]) -> dict[str, Any]:
    conditional = {}
    if task.get("document_id") and task.get("etag"):
        conditional["If-None-Match"] = task["etag"]
    if task.get("document_id") and task.get("last_modified"):
        conditional["If-Modified-Since"] = task["last_modified"]
    approved = task["canonical_url"] if task.get("approved_once") else None
    result = fetcher.fetch(
        task["canonical_url"], task["allowed_domains"], approved_url=approved, conditional_headers=conditional
    )
    if result.not_modified:
        return {"kind": "unchanged", "result": result}
    if result.content_type == "application/pdf" or result.url.lower().endswith(".pdf"):
        text = normalize_text(app.extract_pdf(result.content))
        title = task.get("title") or page_title_from_url(result.url)
        content_type = "application/pdf"
    elif result.content_type in {"text/plain", "text/markdown"}:
        text = normalize_text(decode_body(result.content, result.headers.get("content-type", result.content_type)))
        title = task.get("title") or page_title_from_url(result.url)
        content_type = result.content_type
    else:
        text, extracted_title = extract_html(
            result.content, result.url, result.headers.get("content-type", result.content_type)
        )
        title = extracted_title or task.get("title") or page_title_from_url(result.url)
        content_type = "text/html"
    if len(text) < MIN_TEXT_LENGTH:
        raise FetchError(f"正文不足 {MIN_TEXT_LENGTH} 字符，可能是动态、登录或低信息页面")
    content_hash = hashlib.sha256(text.encode("utf-8")).hexdigest()
    if content_hash == task.get("content_hash") and task.get("document_id"):
        return {"kind": "unchanged", "result": result}
    return {
        "kind": "updated" if task.get("document_id") else "added",
        "result": result,
        "text": text,
        "title": normalize_text(title)[:300],
        "content_type": content_type,
        "content_hash": content_hash,
    }


def store_fetch_result(page_id: int, task: dict[str, Any], outcome: dict[str, Any]) -> tuple[str, str | None]:
    result: FetchResult = outcome["result"]
    now = utc_now()
    if outcome["kind"] == "unchanged":
        with app.connect_db() as connection:
            connection.execute(
                """
                UPDATE web_pages SET last_seen_at = ?, last_fetched_at = ?,
                       status = 'unchanged', error = NULL, http_status = ?,
                       etag = COALESCE(?, etag), last_modified = COALESCE(?, last_modified)
                WHERE id = ?
                """,
                (
                    now, now, result.status_code,
                    result.headers.get("etag"), result.headers.get("last-modified"), page_id,
                ),
            )
        return "unchanged", None

    text = outcome["text"]
    chunks = app.chunk_text(text)
    if not chunks or len(chunks) > 5000:
        raise FetchError("正文切分数量不在允许范围内")
    vectors, provider, warning = app.embed_for_ingestion(chunks)
    identity_hash = hashlib.sha256(("web:" + task["canonical_url"]).encode("utf-8")).hexdigest()
    with app.connect_db() as connection:
        page = connection.execute("SELECT document_id FROM web_pages WHERE id = ?", (page_id,)).fetchone()
        document_id = int(page["document_id"]) if page and page["document_id"] else None
        if document_id is None:
            cursor = connection.execute(
                """
                INSERT INTO documents(
                    name, sha256, size, created_at, source_type, source_url, content_hash, updated_at
                ) VALUES (?, ?, ?, ?, 'web', ?, ?, ?)
                """,
                (
                    outcome["title"], identity_hash, len(text.encode("utf-8")), now,
                    result.url, outcome["content_hash"], now,
                ),
            )
            document_id = int(cursor.lastrowid)
        else:
            connection.execute(
                """
                UPDATE documents SET name = ?, size = ?, source_type = 'web', source_url = ?,
                       content_hash = ?, updated_at = ? WHERE id = ?
                """,
                (
                    outcome["title"], len(text.encode("utf-8")), result.url,
                    outcome["content_hash"], now, document_id,
                ),
            )
            connection.execute("DELETE FROM chunks WHERE document_id = ?", (document_id,))
        connection.executemany(
            """
            INSERT INTO chunks(document_id, chunk_index, content, embedding, embedding_provider)
            VALUES (?, ?, ?, ?, ?)
            """,
            [
                (document_id, index + 1, content, json.dumps(vector), provider)
                for index, (content, vector) in enumerate(zip(chunks, vectors, strict=True))
            ],
        )
        connection.execute(
            """
            INSERT OR IGNORE INTO web_versions(
                page_id, content_hash, title, content, content_type, fetched_at
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                page_id, outcome["content_hash"], outcome["title"], text,
                outcome["content_type"], now,
            ),
        )
        connection.execute(
            """
            UPDATE web_pages SET document_id = ?, title = ?,
                   content_type = ?, etag = ?, last_modified = ?, content_hash = ?,
                   last_seen_at = ?, last_fetched_at = ?, status = ?, error = NULL, http_status = ?
            WHERE id = ?
            """,
            (
                document_id, outcome["title"],
                outcome["content_type"], result.headers.get("etag"), result.headers.get("last-modified"),
                outcome["content_hash"], now, now, outcome["kind"], result.status_code, page_id,
            ),
        )
    return outcome["kind"], warning


def mark_page_failed(page_id: int, message: str) -> None:
    with app.connect_db() as connection:
        connection.execute(
            "UPDATE web_pages SET status = 'failed', error = ?, last_fetched_at = ? WHERE id = ?",
            (message[:1000], utc_now(), page_id),
        )


def prune_versions() -> int:
    init_crawler_db()
    cutoff = datetime.now(timezone.utc) - timedelta(days=VERSION_RETENTION_DAYS)
    deleted = 0
    with app.connect_db() as connection:
        pages = connection.execute("SELECT id, content_hash FROM web_pages").fetchall()
        for page in pages:
            versions = connection.execute(
                "SELECT id, content_hash, fetched_at FROM web_versions WHERE page_id = ? ORDER BY fetched_at DESC, id DESC",
                (page["id"],),
            ).fetchall()
            for index, version in enumerate(versions):
                fetched_at = parse_utc(version["fetched_at"])
                expired = fetched_at is not None and fetched_at < cutoff
                over_count = index >= VERSION_RETENTION_COUNT
                if (expired or over_count) and version["content_hash"] != page["content_hash"]:
                    connection.execute("DELETE FROM web_versions WHERE id = ?", (version["id"],))
                    deleted += 1
    return deleted


def acquire_job(trigger_type: str, source_id: int | None = None) -> tuple[int, str]:
    init_crawler_db()
    owner = uuid.uuid4().hex
    now = datetime.now(timezone.utc)
    with app.connect_db() as connection:
        connection.execute("BEGIN IMMEDIATE")
        lock = connection.execute("SELECT owner, acquired_at FROM crawl_lock WHERE id = 1").fetchone()
        acquired = parse_utc(lock["acquired_at"]) if lock else None
        if lock and acquired and now - acquired < timedelta(hours=LOCK_TTL_HOURS):
            raise BusyError("已有采集任务正在运行")
        connection.execute(
            "INSERT OR REPLACE INTO crawl_lock(id, owner, acquired_at) VALUES (1, ?, ?)",
            (owner, now.isoformat(timespec="seconds")),
        )
        cursor = connection.execute(
            "INSERT INTO crawl_jobs(trigger_type, source_id, status, started_at) VALUES (?, ?, 'running', ?)",
            (trigger_type, source_id, now.isoformat(timespec="seconds")),
        )
        job_id = int(cursor.lastrowid)
    return job_id, owner


def release_job(job_id: int, owner: str, counters: dict[str, int], status: str, message: str) -> None:
    with app.connect_db() as connection:
        connection.execute(
            """
            UPDATE crawl_jobs SET status = ?, finished_at = ?, discovered = ?, fetched = ?,
                added = ?, updated = ?, unchanged = ?, skipped = ?, failed = ?,
                pending_review = ?, message = ? WHERE id = ?
            """,
            (
                status, utc_now(), counters["discovered"], counters["fetched"], counters["added"],
                counters["updated"], counters["unchanged"], counters["skipped"], counters["failed"],
                counters["pending_review"], message[:4000], job_id,
            ),
        )
        connection.execute("DELETE FROM crawl_lock WHERE id = 1 AND owner = ?", (owner,))


def execute_job(
    job_id: int,
    owner: str,
    source_id: int | None = None,
    search_client: Any | None = None,
    fetcher: Any | None = None,
) -> dict[str, Any]:
    counters = {key: 0 for key in ("discovered", "fetched", "added", "updated", "unchanged", "skipped", "failed", "pending_review")}
    warnings: list[str] = []
    owns_search = search_client is None
    owns_fetcher = fetcher is None
    try:
        search_client = search_client or BraveSearchClient()
        fetcher = fetcher or SafeFetcher()
        with app.connect_db() as connection:
            if source_id is None:
                source_rows = connection.execute("SELECT * FROM crawl_sources WHERE enabled = 1 ORDER BY id").fetchall()
            else:
                source_rows = connection.execute("SELECT * FROM crawl_sources WHERE id = ?", (source_id,)).fetchall()
        if not source_rows:
            raise CrawlerError("没有可运行的采集主题")

        search_budget = [0]
        selected_page_ids: list[int] = []
        global_seen: set[int] = set()
        remaining = MAX_PAGES
        for source_row in source_rows:
            source = source_from_row(source_row)
            before_pending = len(list_candidates("pending", 500))
            new_ids, source_warnings = discover_source(source, search_client, fetcher, search_budget)
            warnings.extend(source_warnings)
            after_pending = len(list_candidates("pending", 500))
            counters["pending_review"] += max(0, after_pending - before_pending)
            counters["discovered"] += len(new_ids)
            with app.connect_db() as connection:
                tracked_rows = connection.execute(
                    """
                    SELECT p.id FROM web_pages p JOIN source_pages sp ON sp.page_id = p.id
                    WHERE sp.source_id = ? AND p.last_fetched_at IS NOT NULL
                    ORDER BY p.last_fetched_at ASC
                    """,
                    (source["id"],),
                ).fetchall()
                pending_rows = connection.execute(
                    """
                    SELECT p.id FROM web_pages p JOIN source_pages sp ON sp.page_id = p.id
                    WHERE sp.source_id = ? AND p.last_fetched_at IS NULL
                    ORDER BY p.first_seen_at ASC
                    """,
                    (source["id"],),
                ).fetchall()
            new_queue = list(dict.fromkeys(new_ids + [int(row["id"]) for row in pending_rows]))
            tracked_queue = [int(row["id"]) for row in tracked_rows]
            source_budget = min(source["max_pages"], remaining)
            new_quota = int(source_budget * 0.6)
            tracked_quota = source_budget - new_quota
            chosen = new_queue[:new_quota] + tracked_queue[:tracked_quota]
            leftovers = new_queue[new_quota:] + tracked_queue[tracked_quota:]
            chosen.extend(leftovers[: max(0, source_budget - len(chosen))])
            for page_id in chosen:
                if page_id not in global_seen and remaining > 0:
                    selected_page_ids.append(page_id)
                    global_seen.add(page_id)
                    remaining -= 1
            if remaining <= 0:
                break

        tasks = [task for page_id in selected_page_ids if (task := page_task(page_id))]
        outcomes: dict[int, tuple[dict[str, Any] | None, Exception | None]] = {}
        with ThreadPoolExecutor(max_workers=4, thread_name_prefix="zhiyu-fetch") as pool:
            futures = {pool.submit(fetch_page, fetcher, task): task for task in tasks}
            for future in as_completed(futures):
                task = futures[future]
                try:
                    outcomes[int(task["id"])] = (future.result(), None)
                except Exception as exc:
                    outcomes[int(task["id"])] = (None, exc)
        for task in tasks:
            page_id = int(task["id"])
            outcome, error = outcomes.get(page_id, (None, FetchError("任务没有返回结果")))
            if error:
                counters["failed"] += 1
                mark_page_failed(page_id, str(error))
                continue
            counters["fetched"] += 1
            try:
                kind, embedding_warning = store_fetch_result(page_id, task, outcome or {})
                counters[kind] += 1
                if embedding_warning:
                    warnings.append(embedding_warning)
            except Exception as exc:
                counters["failed"] += 1
                mark_page_failed(page_id, str(exc))
        prune_versions()
        status = "partial" if counters["failed"] or warnings else "completed"
        message = "；".join(dict.fromkeys(warnings)) or "采集完成"
    except Exception as exc:
        status = "failed"
        message = str(exc)
    finally:
        if owns_search and search_client is not None:
            search_client.close()
        if owns_fetcher and fetcher is not None:
            fetcher.close()
        release_job(job_id, owner, counters, status, message)
    return get_job(job_id)


def get_job(job_id: int) -> dict[str, Any]:
    with app.connect_db() as connection:
        row = connection.execute("SELECT * FROM crawl_jobs WHERE id = ?", (job_id,)).fetchone()
    if not row:
        raise ValueError("采集任务不存在")
    return dict(row)


def run_sync(
    source_id: int | None = None,
    trigger_type: str = "cli",
    search_client: Any | None = None,
    fetcher: Any | None = None,
) -> dict[str, Any]:
    job_id, owner = acquire_job(trigger_type, source_id)
    return execute_job(job_id, owner, source_id, search_client, fetcher)


_BACKGROUND_THREADS: set[threading.Thread] = set()
_BACKGROUND_LOCK = threading.Lock()


def start_background_job(source_id: int | None = None) -> int:
    job_id, owner = acquire_job("manual", source_id)

    def target() -> None:
        try:
            execute_job(job_id, owner, source_id)
        finally:
            with _BACKGROUND_LOCK:
                _BACKGROUND_THREADS.discard(threading.current_thread())

    thread = threading.Thread(target=target, name=f"crawl-job-{job_id}", daemon=True)
    with _BACKGROUND_LOCK:
        _BACKGROUND_THREADS.add(thread)
    thread.start()
    return job_id


def install_windows_task(run_time: str) -> None:
    if os.name != "nt":
        raise CrawlerError("install-task 目前只支持 Windows")
    if not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", run_time):
        raise ValueError("时间必须采用 HH:MM 格式")
    python_path = str(Path(sys.executable).resolve()).replace("'", "''")
    crawler_path = str(Path(__file__).resolve()).replace("'", "''")
    workdir = str(Path(__file__).resolve().parent).replace("'", "''")
    command = (
        f"$action = New-ScheduledTaskAction -Execute '{python_path}' "
        f"-Argument '\"{crawler_path}\" run' -WorkingDirectory '{workdir}'; "
        f"$trigger = New-ScheduledTaskTrigger -Daily -At '{run_time}'; "
        "$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable "
        "-MultipleInstances IgnoreNew -ExecutionTimeLimit (New-TimeSpan -Hours 4); "
        "Register-ScheduledTask -TaskName 'ZhiyuRAGDailyCrawler' -Action $action "
        "-Trigger $trigger -Settings $settings -Description '知屿 RAG 每日互联网资料采集' -Force"
    )
    completed = subprocess.run(
        ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", command],
        text=True,
        capture_output=True,
        check=False,
    )
    if completed.returncode:
        raise CrawlerError((completed.stderr or completed.stdout or "计划任务创建失败").strip())
    print("已注册 Windows 计划任务 ZhiyuRAGDailyCrawler")


def main() -> None:
    parser = argparse.ArgumentParser(description="知屿 RAG 互联网自动采集器")
    subparsers = parser.add_subparsers(dest="command", required=True)
    run_parser = subparsers.add_parser("run", help="立即运行一次采集")
    run_parser.add_argument("--source-id", type=int)
    subparsers.add_parser("prune", help="清理过期网页版本")
    install_parser = subparsers.add_parser("install-task", help="注册 Windows 每日计划任务")
    install_parser.add_argument("--time", default="18:00")
    args = parser.parse_args()
    if args.command == "run":
        result = run_sync(args.source_id)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        if result["status"] == "failed":
            raise SystemExit(1)
    elif args.command == "prune":
        print(f"已清理 {prune_versions()} 个过期版本")
    elif args.command == "install-task":
        install_windows_task(args.time)


if __name__ == "__main__":
    main()
