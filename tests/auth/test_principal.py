"""Origin checks for cookie-authenticated mutations (no database needed)."""

from __future__ import annotations

from typing import cast

import pytest
from app.modules.auth.principal import (
    AuthenticationFailure,
    RequestAuthenticator,
    WebCookiePolicy,
)
from app.modules.auth.service import AuthService
from starlette.requests import Request


def request(method: str, headers: dict[str, str]) -> Request:
    return Request(
        {
            "type": "http",
            "method": method,
            "scheme": "http",
            "server": ("mia.test", 80),
            "path": "/v1/dashboard",
            "headers": [(name.lower().encode(), value.encode()) for name, value in headers.items()],
        }
    )


AUTHENTICATOR = RequestAuthenticator(
    cast(AuthService, object()),
    WebCookiePolicy(trusted_origins=frozenset({"https://app.mia.test"})),
)


@pytest.mark.parametrize(
    "headers",
    [
        {"host": "mia.test"},
        {"host": "mia.test", "origin": "https://mia.test", "sec-fetch-site": "same-origin"},
        {"host": "mia.test", "sec-fetch-site": "none"},
        {"host": "mia.test", "origin": "https://app.mia.test", "sec-fetch-site": "same-site"},
    ],
)
def test_same_origin_or_trusted_mutations_pass(headers: dict[str, str]) -> None:
    AUTHENTICATOR.enforce_same_origin(request("PUT", headers))


@pytest.mark.parametrize(
    "headers",
    [
        {"host": "mia.test", "origin": "https://evil.example"},
        {"host": "mia.test", "origin": "null"},
        {"host": "mia.test", "sec-fetch-site": "cross-site"},
        {"host": "mia.test", "sec-fetch-site": "same-site", "origin": "https://x.mia.test"},
    ],
)
def test_cross_site_mutations_fail(headers: dict[str, str]) -> None:
    with pytest.raises(AuthenticationFailure) as failure:
        AUTHENTICATOR.enforce_same_origin(request("POST", headers))
    assert failure.value.status_code == 403


def test_safe_methods_are_not_origin_checked() -> None:
    AUTHENTICATOR.enforce_same_origin(
        request("GET", {"host": "mia.test", "origin": "https://evil.example"})
    )
