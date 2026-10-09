"""The public-web client ported from the POC: SSRF guards and parsing, never the network."""

from __future__ import annotations

import socket
import time
from email.utils import formatdate
from typing import Any

import pytest
from app.modules.plugins.builtin.web import (
    PublicWebClient,
    PublicWebError,
    fetch_public,
    parse_feed,
    public_addresses,
    strip_markup,
)


def rss(*items: tuple[str, str, float | None]) -> bytes:
    body = "".join(
        f"<item><title>{title}</title><link>{link}</link>"
        + (f"<pubDate>{formatdate(stamp)}</pubDate>" if stamp is not None else "")
        + "<description>&lt;b&gt;Текст&lt;/b&gt; описания</description></item>"
        for title, link, stamp in items
    )
    return f"<rss><channel>{body}</channel></rss>".encode()


def test_news_feed_keeps_only_fresh_https_items() -> None:
    now = time.time()
    feed = rss(
        ("Свежая", "https://example.com/1", now - 3600),
        ("Старая", "https://example.com/2", now - 5 * 86400),
        ("Без даты", "https://example.com/3", None),
        ("Небезопасная", "http://example.com/4", now - 60),
    )
    items = parse_feed(feed, news=True)
    assert [item["title"] for item in items] == ["Свежая"]
    assert items[0]["snippet"] == "Текст описания"
    everything = parse_feed(feed, news=False)
    assert [item["title"] for item in everything] == ["Свежая", "Старая", "Без даты"]


@pytest.mark.parametrize(
    "body",
    [b"<!DOCTYPE x><rss/>", b'<!ENTITY a "b"><rss/>', b"not xml at all"],
)
def test_feed_rejects_entities_and_garbage(body: bytes) -> None:
    with pytest.raises(PublicWebError):
        parse_feed(body, news=False)


def test_markup_is_stripped_without_scripts() -> None:
    assert (
        strip_markup("<p>Привет</p><script>alert(1)</script><style>x{}</style> мир") == "Привет мир"
    )


def fake_dns(*addresses: str) -> Any:
    def getaddrinfo(host: str, port: int, type: int = 0) -> list[tuple[Any, ...]]:
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, port)) for ip in addresses]

    return getaddrinfo


@pytest.mark.parametrize(
    "addresses",
    [("127.0.0.1",), ("10.0.0.5",), ("169.254.169.254",), ("93.184.216.34", "192.168.1.1"), ()],
)
def test_private_or_mixed_dns_answers_are_refused(
    monkeypatch: pytest.MonkeyPatch, addresses: tuple[str, ...]
) -> None:
    monkeypatch.setattr(socket, "getaddrinfo", fake_dns(*addresses))
    with pytest.raises(PublicWebError, match="публичные"):
        public_addresses("evil.example")


def test_public_dns_answers_are_accepted(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(socket, "getaddrinfo", fake_dns("93.184.216.34", "93.184.216.34"))
    assert public_addresses("example.com") == ["93.184.216.34"]


@pytest.mark.parametrize(
    "url",
    [
        "http://example.com/",
        "https://example.com:8443/",
        "https://user:pw@example.com/",
        "ftp://example.com/",
        "https://example.com/" + "a" * 2100,
        "https:///nohost",
    ],
)
def test_fetch_refuses_non_public_shapes_before_any_connection(url: str) -> None:
    with pytest.raises(PublicWebError):
        fetch_public(url)


async def test_client_builds_search_and_news_urls_and_reads_text() -> None:
    seen: list[str] = []
    now = time.time()

    def fetch(url: str) -> tuple[bytes, str, str]:
        seen.append(url)
        if "news.google.com" in url or "bing.com" in url:
            return rss(("Тема", "https://example.com/x", now - 60)), "application/rss+xml", url
        if url.endswith("/pdf"):
            return b"%PDF", "application/pdf", url
        return "<h1>Заголовок</h1><script>x()</script>".encode(), "text/html", url

    client = PublicWebClient(fetch)
    assert (await client.search("ИИ", news=True))[0]["title"] == "Тема"
    assert (await client.search("ИИ"))[0]["url"] == "https://example.com/x"
    assert seen[0].startswith("https://news.google.com/rss/search?")
    assert "when%3A2d" in seen[0] or "when:2d" in seen[0]
    assert seen[1].startswith("https://www.bing.com/search?")
    page = await client.read("https://example.com/page")
    assert page == {"url": "https://example.com/page", "text": "Заголовок", "truncated": "False"}
    with pytest.raises(PublicWebError, match="HTML"):
        await client.read("https://example.com/pdf")
