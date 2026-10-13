"""Ordered, unsigned OKX public reads; private transports must stay separate."""
from __future__ import annotations

import json
from urllib.parse import urlsplit
from urllib.request import Request

from astra_backend.collection import endpoint_timing
from astra_backend.http_deadline import open_with_deadline
from astra_backend.deadline import DeadlineExceeded, check_deadline

OKX_PUBLIC_HOSTS = ("https://openapi.okx.com", "https://www.okx.com")
_PUBLIC_PREFIXES = (
    "/api/v5/market/",
    "/api/v5/public/",
    "/api/v5/rubik/",
    "/api/v5/aigc/",
)


def validate_public_path(path: str) -> None:
    """Reject absolute URLs and every account/order or non-public path."""
    parsed = urlsplit(path)
    if (parsed.scheme or parsed.netloc or parsed.fragment
            or not parsed.path.startswith(_PUBLIC_PREFIXES)
            or any(part in (".", "..") for part in parsed.path.split("/"))):
        raise ValueError("Only relative OKX public market paths are allowed")


def public_json_get(path: str, *, opener, timeout: float, user_agent: str,
                    accept=None, request_factory=Request):
    """Try each host once, preserving the caller's payload and final exception.

    The caller supplies its opener at call time so both import spellings retain
    their existing patch seams. No credentials, signed headers, or writes are
    accepted. A final rejected payload is returned for the caller's original
    missing-data/error handling; a final transport/parser exception is raised.
    Each host consumes the same deadline; late responses cannot start failover.
    This helper adds no retry, sleep, cache or pooled-session policy.
    """
    validate_public_path(path)
    if accept is None:
        accept = lambda payload: payload.get("code") in (None, "0", 0)
    for index, host in enumerate(OKX_PUBLIC_HOSTS):
        check_deadline()
        try:
            with endpoint_timing("GET", path, host) as trace:
                req = request_factory(host + path, headers={"User-Agent": user_agent})
                with open_with_deadline(req, opener=opener, timeout=timeout) as response:
                    check_deadline()
                    payload = json.loads(response.read().decode("utf-8"))
                check_deadline()
                accepted = accept(payload)
                check_deadline()
                trace["result"] = "ok" if accepted else "code_error"
                if accepted or index == len(OKX_PUBLIC_HOSTS) - 1:
                    return payload
        except DeadlineExceeded:
            raise
        except Exception:
            check_deadline()
            if index == len(OKX_PUBLIC_HOSTS) - 1:
                raise
