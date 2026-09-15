"""Hardened HTTP primitives shared by the read-only forge adapters.

Forge endpoints are the untrusted side of ``boundary:remote-forge``. The
stdlib :func:`urllib.request.urlopen` follows HTTP redirects automatically
and resends request headers -- including ``Authorization`` -- to the
redirect target. A malicious, compromised, or MITM'd forge can therefore
answer a request with a ``3xx`` to an attacker-controlled host and either
exfiltrate the configured credential or pivot the authenticated request to
an internal service the WeftMark host can reach (SSRF). That redirect
happens at the HTTP layer, so it also escapes any application-level
same-host validation an adapter performs on a response-supplied pagination
URL.

The adapters read JSON REST endpoints that do not depend on cross-origin
redirects, so :func:`build_forge_opener` returns an opener that refuses any
redirect changing the scheme, host, or port, while still allowing a
same-origin redirect (for example trailing-slash normalization).
"""

from __future__ import annotations

from typing import Any
from urllib.error import URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, OpenerDirector, Request, build_opener


class CrossOriginRedirectError(URLError):
    """Raised when a forge response redirects across an origin boundary.

    Subclasses :class:`urllib.error.URLError` so the adapters' existing
    ``except URLError`` transport-unavailable path treats a refused
    redirect as an unavailable observation rather than a real response.
    """

    def __init__(self, origin: str, target: str) -> None:
        super().__init__(f"refused cross-origin redirect from {origin!r} to {target!r}")


class _SameOriginRedirectHandler(HTTPRedirectHandler):
    """Follow only redirects that stay on the original scheme/host/port."""

    def redirect_request(
        self,
        req: Request,
        fp: Any,
        code: int,
        msg: str,
        headers: Any,
        newurl: str,
    ) -> Request | None:
        current = urlsplit(req.full_url)
        target = urlsplit(newurl)
        if (target.scheme, target.hostname, target.port) != (
            current.scheme,
            current.hostname,
            current.port,
        ):
            raise CrossOriginRedirectError(req.full_url, newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def build_forge_opener() -> OpenerDirector:
    """Build an opener that refuses redirects leaving the request origin.

    ``build_opener`` replaces the default :class:`HTTPRedirectHandler` with
    the same-origin variant while keeping the default HTTP(S) handlers, so
    the returned opener is a drop-in for :func:`urllib.request.urlopen`.
    """

    return build_opener(_SameOriginRedirectHandler())


MAX_RESPONSE_BYTES = 16 * 1024 * 1024
"""Cap on a single forge response body (one REST page); pagination is
separately bounded by each adapter."""


class ResponseTooLargeError(URLError):
    """Raised when a forge response body exceeds the read cap.

    Subclasses :class:`urllib.error.URLError` so the adapters' existing
    transport-unavailable path treats an oversized body as unavailable.
    """

    def __init__(self, max_bytes: int) -> None:
        super().__init__(f"forge response body exceeded {max_bytes}-byte read cap")


def read_capped(fp: Any, *, max_bytes: int = MAX_RESPONSE_BYTES) -> bytes:
    """Read at most ``max_bytes`` from ``fp``, refusing a larger body.

    A hostile or malfunctioning forge (the untrusted side of
    ``boundary:remote-forge``) can otherwise return an unbounded body and
    exhaust memory. One byte past the cap is read to detect overflow
    without buffering the whole response.
    """

    data = fp.read(max_bytes + 1)
    if len(data) > max_bytes:
        raise ResponseTooLargeError(max_bytes)
    return data
