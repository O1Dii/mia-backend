"""SSRF guard for remote MCP servers (Review §5.4.3 policy engine).

Every outbound request, including each redirect hop the MCP SDK follows, goes
through :class:`GuardedTransport`:

1. ``https`` only, no userinfo, no IP literals, the host must be in the
   plugin's ``permissions.network`` and, when configured, in the global
   allow-list;
2. the host is resolved once and **every** returned address must be a public
   unicast address (private, loopback, link-local/cloud metadata, CGNAT,
   multicast, reserved and IPv4-mapped forms are refused), so a resolver that
   mixes a public and a private answer is rejected as a whole;
3. the connection is pinned to the vetted address (the URL host is replaced by
   the IP, ``Host`` and TLS SNI/certificate checks keep the original name), so a
   second resolution cannot rebind the request to an internal address.
"""

from __future__ import annotations

import asyncio
import ipaddress
import socket
from collections.abc import Awaitable, Callable, Iterable, Sequence

import httpx2

Resolver = Callable[[str, int], Awaitable[Sequence[str]]]

_BLOCKED_NETWORKS = tuple(
    ipaddress.ip_network(network)
    for network in (
        "0.0.0.0/8",
        "10.0.0.0/8",
        "100.64.0.0/10",
        "127.0.0.0/8",
        "169.254.0.0/16",
        "172.16.0.0/12",
        "192.0.0.0/24",
        "192.0.2.0/24",
        "192.88.99.0/24",
        "192.168.0.0/16",
        "198.18.0.0/15",
        "198.51.100.0/24",
        "203.0.113.0/24",
        "224.0.0.0/4",
        "240.0.0.0/4",
        "::/128",
        "::1/128",
        "::ffff:0:0/96",
        "64:ff9b::/96",
        "64:ff9b:1::/48",
        "100::/64",
        "2001::/23",
        "2001:db8::/32",
        "2002::/16",
        "fc00::/7",
        "fe80::/10",
        "fec0::/10",
        "ff00::/8",
        "fd00:ec2::254/128",
    )
)


class HostBlockedError(Exception):
    """The destination is not allowed; nothing was sent."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def is_public_address(value: str) -> bool:
    try:
        address = ipaddress.ip_address(value.split("%", 1)[0])
    except ValueError:
        return False
    if not address.is_global:
        return False
    return not any(address in network for network in _BLOCKED_NETWORKS)


async def system_resolver(host: str, port: int) -> Sequence[str]:
    loop = asyncio.get_running_loop()
    infos = await loop.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    return [str(info[4][0]) for info in infos]


def _is_ip_literal(host: str) -> bool:
    try:
        ipaddress.ip_address(host.strip("[]"))
    except ValueError:
        return False
    return True


class HostPolicy:
    """Which hosts one plugin may reach."""

    def __init__(
        self, allowed_hosts: Iterable[str], *, global_allowlist: Iterable[str] | None = None
    ) -> None:
        self.allowed_hosts = frozenset(host.lower() for host in allowed_hosts)
        self.global_allowlist = (
            frozenset(host.lower() for host in global_allowlist)
            if global_allowlist is not None
            else None
        )

    def check(self, url: httpx2.URL) -> tuple[str, int]:
        if url.scheme != "https":
            raise HostBlockedError("scheme")
        if url.userinfo:
            raise HostBlockedError("userinfo")
        host = url.host.lower().rstrip(".")
        if not host or _is_ip_literal(host):
            raise HostBlockedError("ip_literal")
        if host not in self.allowed_hosts:
            raise HostBlockedError("host_not_allowed")
        if self.global_allowlist is not None and host not in self.global_allowlist:
            raise HostBlockedError("host_not_in_global_allowlist")
        return host, url.port or 443


async def vetted_address(host: str, port: int, resolver: Resolver) -> str:
    """Resolve ``host`` and return one address if *all* answers are public."""
    try:
        addresses = list(await resolver(host, port))
    except OSError as error:
        raise HostBlockedError("dns_failure") from error
    if not addresses:
        raise HostBlockedError("dns_empty")
    if not all(is_public_address(address) for address in addresses):
        raise HostBlockedError("private_address")
    return addresses[0]


class GuardedTransport(httpx2.AsyncBaseTransport):
    """Check, resolve and pin every request before the inner transport sees it."""

    def __init__(
        self,
        policy: HostPolicy,
        *,
        resolver: Resolver = system_resolver,
        inner: httpx2.AsyncBaseTransport | None = None,
    ) -> None:
        self._policy = policy
        self._resolver = resolver
        self._inner = inner or httpx2.AsyncHTTPTransport(retries=0)

    async def handle_async_request(self, request: httpx2.Request) -> httpx2.Response:
        host, port = self._policy.check(request.url)
        address = await vetted_address(host, port, self._resolver)
        request.url = request.url.copy_with(host=address)
        request.extensions = {**request.extensions, "sni_hostname": host}
        return await self._inner.handle_async_request(request)

    async def aclose(self) -> None:
        await self._inner.aclose()
