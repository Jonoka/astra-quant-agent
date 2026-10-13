"""Budgeted urllib opening without hidden redirect requests.

Callers supply their existing opener at call time. Mock/custom transports remain
at that seam; only the real stdlib urlopen gets a private, no-redirect opener.
No global opener is installed or changed, including in concurrent workers.
"""
from __future__ import annotations

from types import FunctionType
import urllib.request
from urllib.request import HTTPRedirectHandler, build_opener

from astra_backend.deadline import check_deadline, current_deadline


def _is_stdlib_urlopen(opener) -> bool:
    # Importing this module while urlopen is mocked must not turn that mock
    # into permission to bypass it. Verify the stdlib code source at call time,
    # including when a later import restores/reloads the real function.
    return (isinstance(opener, FunctionType)
            and opener.__module__ == "urllib.request"
            and opener.__name__ == "urlopen"
            and opener.__code__.co_filename == urllib.request.__file__)


class NoRedirectHandler(HTTPRedirectHandler):
    """A redirect is a failed read, never another implicit network request."""

    def redirect_request(self, request, response, code, message, headers, new_url):
        check_deadline()
        return None


def open_with_deadline(request, *, opener, timeout: float):
    """Clamp the socket timeout and forbid stdlib redirects under a budget."""
    check_deadline()
    transport = opener
    if current_deadline() is not None and _is_stdlib_urlopen(opener):
        transport = build_opener(NoRedirectHandler()).open
    response = transport(request, timeout=min(timeout, check_deadline()))
    try:
        check_deadline()
    except BaseException:
        # The caller has not entered its response context yet. Close a late
        # response here so cancellation cannot leak an open socket/resource.
        try:
            response.close()
        except Exception:
            pass
        raise
    return response
