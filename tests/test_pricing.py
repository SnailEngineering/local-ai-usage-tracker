from __future__ import annotations

import unittest

from aiusage import pricing


class PricingTests(unittest.TestCase):
    def test_openai_rate_refresh_preserves_prior_rates_for_history(self) -> None:
        self.assertEqual(
            pricing.rates_for("gpt-5.6-terra", "2026-08-01", "openai"),
            (2.50, 15.00),
        )
        self.assertEqual(
            pricing.rates_for("gpt-5.6-terra", "2026-08-02", "openai"),
            (2.00, 12.00),
        )
        self.assertEqual(
            pricing.rates_for("gpt-5.6-luna", "2026-08-02", "openai"),
            (0.20, 1.20),
        )

    def test_openai_cost_uses_cached_input_discount(self) -> None:
        row = {
            "model": "gpt-5.6-terra",
            "provider": "openai",
            "day": "2026-08-02",
            "input_tokens": 1_000_000,
            "cache_read_tokens": 1_000_000,
            "output_tokens": 1_000_000,
            "cache_write_5m_tokens": 0,
            "cache_write_1h_tokens": 0,
        }
        self.assertEqual(pricing.cost_usd(row), 14.2)


if __name__ == "__main__":
    unittest.main()
