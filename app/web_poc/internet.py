"""Public HTTPS/RSS connector with DNS pinning and bounded, content-only extraction."""

import asyncio
import html
import http.client
import ipaddress
import socket
import ssl
import time
import xml.etree.ElementTree as ET
from email.utils import parsedate_to_datetime
from html.parser import HTMLParser
from typing import Any
from urllib.parse import urlencode, urljoin, urlsplit


class InternetError(Exception):
    pass


def public_addresses(host: str) -> list[str]:
    addresses = list(
        dict.fromkeys(
            str(row[4][0]) for row in socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)
        )
    )
    if not addresses or any(
        not ipaddress.ip_address(ip).is_global or ipaddress.ip_address(ip).is_multicast
        for ip in addresses
    ):
        raise InternetError("Разрешены только публичные HTTPS-сайты.")
    return addresses


class PublicHTTPS(http.client.HTTPSConnection):
    def connect(self) -> None:
        # Resolve once, validate all answers, connect to the literal IP, keep original TLS SNI.
        addresses = public_addresses(self.host)
        self.sock = socket.create_connection((addresses[0], 443), timeout=8)
        self.sock = ssl.create_default_context().wrap_socket(self.sock, server_hostname=self.host)


def fetch_public(url: str) -> tuple[bytes, str, str]:
    for _ in range(4):
        try:
            parsed = urlsplit(url)
            if (
                parsed.scheme != "https"
                or not parsed.hostname
                or parsed.port not in {None, 443}
                or parsed.username
                or parsed.password
                or len(url) > 2048
            ):
                raise InternetError(
                    "Укажите публичный HTTPS-адрес без пароля и нестандартного порта."
                )
            host = parsed.hostname.encode("idna").decode("ascii")
            connection = PublicHTTPS(host, timeout=8)
            try:
                connection.request(
                    "GET",
                    (parsed.path or "/") + ("?" + parsed.query if parsed.query else ""),
                    headers={
                        "User-Agent": "MiaWebPOC/0.2 (read-only news reader)",
                        "Accept-Encoding": "identity",
                    },
                )
                response = connection.getresponse()
                if response.status in {301, 302, 303, 307, 308}:
                    url = urljoin(url, response.getheader("Location", ""))
                    continue
                if response.status != 200:
                    raise InternetError(
                        "Источник недоступен или ограничивает автоматическое чтение."
                    )
                chunks = bytearray()
                deadline = time.monotonic() + 12
                while True:
                    chunk = response.read1(65536)
                    if not chunk:
                        break
                    chunks.extend(chunk)
                    if len(chunks) > 1_000_000:
                        raise InternetError("Страница слишком большая для POC.")
                    if time.monotonic() > deadline:
                        raise InternetError("Источник слишком медленно отдаёт страницу.")
                body = bytes(chunks)
                if response.getheader("Content-Encoding", "identity") not in {"", "identity"}:
                    raise InternetError("Источник использует неподдерживаемое сжатие.")
                return body, response.getheader("Content-Type", ""), url
            finally:
                connection.close()
        except (OSError, ValueError, http.client.HTTPException):
            raise InternetError("Не удалось прочитать публичный источник.") from None
    raise InternetError("Слишком много перенаправлений.")


class TextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.hidden = 0
        self.parts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in {"script", "style", "noscript"}:
            self.hidden += 1

    def handle_endtag(self, tag: str) -> None:
        if tag in {"script", "style", "noscript"}:
            self.hidden = max(0, self.hidden - 1)

    def handle_data(self, data: str) -> None:
        if not self.hidden and data.strip():
            self.parts.append(data.strip())


def strip_markup(value: str) -> str:
    parser = TextExtractor()
    parser.feed(value)
    return " ".join(parser.parts)


def parse_feed(body: bytes, *, news: bool) -> list[dict[str, Any]]:
    if b"<!doctype" in body.lower() or b"<!entity" in body.lower():
        raise InternetError("Источник вернул неподдерживаемый XML.")
    try:
        root = ET.fromstring(body)
    except ET.ParseError:
        raise InternetError("Источник не вернул RSS. Попробуйте позже.") from None
    items: list[dict[str, Any]] = []
    for item in root.findall(".//item")[:30]:
        title, url = item.findtext("title", ""), item.findtext("link", "")
        if not title or not url.startswith("https://"):
            continue
        published = item.findtext("pubDate", "")
        if news:
            try:
                stamp = parsedate_to_datetime(published).timestamp()
            except (ValueError, TypeError, OverflowError):
                continue
            if not time.time() - 48 * 3600 <= stamp <= time.time() + 900:
                continue
        items.append(
            {
                "title": html.unescape(title)[:300],
                "url": url[:2048],
                "source": item.findtext("source", urlsplit(url).hostname or "Источник"),
                "published_at": published,
                "snippet": strip_markup(item.findtext("description", ""))[:800],
            }
        )
    return items[:8]


class Internet:
    async def search(self, query: str, *, news: bool = False) -> list[dict[str, Any]]:
        if news:
            url = "https://news.google.com/rss/search?" + urlencode(
                {
                    "q": query + " when:2d",
                    "hl": "ru",
                    "gl": "US",
                    "ceid": "US:ru",
                }
            )
        else:
            url = "https://www.bing.com/search?" + urlencode({"q": query, "format": "rss"})
        body, _, _ = await asyncio.to_thread(fetch_public, url)
        return parse_feed(body, news=news)

    async def read(self, url: str) -> dict[str, str]:
        body, content_type, final_url = await asyncio.to_thread(fetch_public, url)
        if not any(
            kind in content_type for kind in ("text/html", "text/plain", "application/xhtml")
        ):
            raise InternetError("Поддерживаются HTML и обычный текст, без файлов и авторизации.")
        text = body.decode("utf-8", errors="replace")
        if "html" in content_type:
            text = strip_markup(text)
        return {"url": final_url, "text": text[:12000], "truncated": str(len(text) > 12000)}
