"""Focused regression for the reviewed same-release application allowlist."""
from __future__ import annotations

import importlib.util
from pathlib import Path
import unittest


SPEC = importlib.util.spec_from_file_location(
    "verify_deployment_source", Path(__file__).with_name("verify_deployment_source.py"))
guard = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(guard)


class DeploymentSourceGuardTests(unittest.TestCase):
    def test_public_domain_delta_is_exactly_reviewed(self):
        self.assertEqual(guard.APPLICATION_PATCH, {
            "astra_backend/exchanges/okx.py",
            "astra_backend/exchanges/diagnostics.py",
            "astra_backend/okx_client.py",
            "deploy/install.sh",
            "docs/exchange_support_matrix.md",
            "env.example",
            "scripts/README.md",
            "scripts/backtest_engine.py",
            "scripts/brain/dispatch.py",
            "scripts/brain/packages.py",
            "scripts/factor_library.py",
            "scripts/factors/okx_quant_factors.py",
            "scripts/factors/smart_money.py",
            "scripts/market_data_service.py",
            "scripts/news_sentiment_harvester.py",
            "scripts/okx_public.py",
            "scripts/sync_full_ledger.py",
            "scripts/trader/factors.py",
            "scripts/trader/circuit_guard.py",
            "tests/ops/test_brain_dispatch.py",
            "tests/core/test_okx_client.py",
            "tests/venues/test_market_data_service.py",
            "tests/venues/test_market_data_service_tails.py",
            "tests/venues/test_exchange_diagnostics_tails.py",
            "tests/venues/test_okx_public_data.py",
            "tests/venues/test_okx_public_domains.py",
        })


if __name__ == "__main__":
    unittest.main()
