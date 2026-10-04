import json
import unittest
from unittest.mock import patch

from scripts import okx_public
from astra_backend.okx_client import OKXClient


class _Response:
    def __init__(self, payload):
        self.payload = json.dumps(payload).encode()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def read(self):
        return self.payload


class PublicDomainBehaviorTests(unittest.TestCase):
    def test_primary_then_www_fallback_returns_downstream_payload(self):
        calls = []

        def opener(request, timeout):
            calls.append(request.full_url)
            if request.full_url.startswith("https://openapi.okx.com"):
                raise OSError("primary unavailable")
            return _Response({"code": "0", "data": [{"last": "123"}]})

        payload = okx_public.public_json_get(
            "/api/v5/market/ticker?instId=BTC-USDT-SWAP",
            opener=opener, timeout=3, user_agent="test")
        self.assertEqual(payload["data"][0]["last"], "123")
        self.assertEqual([u.split("/api", 1)[0] for u in calls],
                         ["https://openapi.okx.com", "https://www.okx.com"])

    def test_primary_business_error_falls_back_to_www(self):
        calls = []

        def opener(request, timeout):
            calls.append(request.full_url)
            if request.full_url.startswith("https://openapi.okx.com"):
                return _Response({"code": "50001", "msg": "temporary"})
            return _Response({"code": "0", "data": [{"last": "456"}]})

        payload = okx_public.public_json_get(
            "/api/v5/market/ticker?instId=BTC-USDT-SWAP",
            opener=opener, timeout=3, user_agent="test")
        self.assertEqual(payload["data"][0]["last"], "456")
        self.assertEqual(len(calls), 2)

    def test_indicator_path_is_allowed_by_public_boundary(self):
        payload = okx_public.public_json_get(
            "/api/v5/aigc/mcp/indicators",
            opener=lambda request, timeout: _Response({"code": "0", "data": []}),
            timeout=3, user_agent="test")
        self.assertEqual(payload, {"code": "0", "data": []})

    def test_private_and_absolute_paths_are_rejected_before_network(self):
        for path in ("/api/v5/account/balance", "/api/v5/trade/order",
                     "https://www.okx.com/api/v5/market/ticker",
                     "//www.okx.com/api/v5/market/ticker",
                     "/api/v5/market/../account/balance"):
            with self.subTest(path=path), patch("urllib.request.urlopen") as opener:
                with self.assertRaises(ValueError):
                    okx_public.public_json_get(path, opener=opener,
                                               timeout=1, user_agent="test")
                opener.assert_not_called()


class BackendPublicFailureTests(unittest.TestCase):
    def test_backend_client_still_raises_when_both_public_hosts_fail(self):
        client = OKXClient()
        with patch("astra_backend.okx_client.urlopen", side_effect=OSError("down")):
            with self.assertRaises(OSError):
                client.ticker("BTC-USDT-SWAP")


if __name__ == "__main__":
    unittest.main()
