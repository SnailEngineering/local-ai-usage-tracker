from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from aiusage import dashboard, db


def _event(**over) -> dict:
    row = {
        "id": "e1", "source": "claude_code_local", "provider": "anthropic",
        "ts": "2026-08-01T10:00:00Z", "day": "2026-08-01", "model": "claude-opus-5",
        "input_tokens": 100, "output_tokens": 50, "cache_write_5m_tokens": 0,
        "cache_write_1h_tokens": 0, "cache_read_tokens": 0, "reasoning_tokens": 0,
        "requests": 1, "project": "proj", "git_branch": None, "session_id": "s",
        "service_tier": None, "ingested_at": "2026-08-01T10:00:00Z",
    }
    row.update(over)
    return row


class DashboardTests(unittest.TestCase):
    def _conn(self, tmp: str, rows: list[dict]):
        conn = db.connect(Path(tmp) / "t.db")
        db.upsert_usage(conn, rows)
        conn.commit()
        return conn

    def test_unpriced_model_is_flagged_on_a_project_not_costed_at_zero(self) -> None:
        """A model with no rate must never look like a free project."""
        with tempfile.TemporaryDirectory() as tmp:
            conn = self._conn(tmp, [
                _event(id="a", model="claude-opus-5", project="priced"),
                _event(id="b", model="totally-unknown-model", project="mystery"),
            ])
            by_name = {p["project"]: p for p in dashboard.build_payload(conn)["projects"]}

            self.assertFalse(by_name["priced"]["unpriced"])
            self.assertGreater(by_name["priced"]["cost_usd"], 0)

            self.assertTrue(by_name["mystery"]["unpriced"])
            self.assertEqual(by_name["mystery"]["cost_usd"], 0.0)
            # The tokens still have to be counted even though the dollars aren't.
            self.assertEqual(by_name["mystery"]["total_tokens"], 150)

    def test_project_name_cannot_break_out_of_the_script_block(self) -> None:
        """The payload is inlined into <script>, where the HTML parser wins."""
        with tempfile.TemporaryDirectory() as tmp:
            conn = self._conn(tmp, [_event(project="</script><img src=x onerror=alert(1)>")])
            out = Path(tmp) / "dashboard.html"
            dashboard.build(conn, out)
            html = out.read_text(encoding="utf-8")

            # Exactly one closer: the template's own, after the payload.
            self.assertEqual(html.count("</script>"), 1)
            self.assertNotIn("<img src=x", html)
            self.assertIn("\\u003c/script", html)

    def test_project_cost_is_priced_per_day_across_a_rate_boundary(self) -> None:
        """`rates_for()` is a function of the day, so project rows have to be
        priced per day and summed. Pricing a whole month's tokens at one
        arbitrary day's rate silently mis-costs every project whose usage
        straddles a DATED_OVERRIDES boundary -- and makes this table stop
        reconciling with the month table beside it."""
        # gpt-5.6-terra: $2.50/MTok input through 2026-08-01, $2.00 from 08-02.
        with tempfile.TemporaryDirectory() as tmp:
            conn = self._conn(tmp, [
                _event(id="a", day="2026-08-01", ts="2026-08-01T10:00:00Z",
                       provider="openai", source="codex_local", model="gpt-5.6-terra",
                       project="straddler", input_tokens=1_000_000, output_tokens=0),
                _event(id="b", day="2026-08-02", ts="2026-08-02T10:00:00Z",
                       provider="openai", source="codex_local", model="gpt-5.6-terra",
                       project="straddler", input_tokens=1_000_000, output_tokens=0),
            ])
            payload = dashboard.build_payload(conn)
            project = {p["project"]: p for p in payload["projects"]}["straddler"]

            self.assertAlmostEqual(project["cost_usd"], 4.50, places=6)
            # The whole point: it agrees with the headline and the month table.
            self.assertAlmostEqual(project["cost_usd"], payload["totals"]["cost_usd"],
                                   places=6)
            self.assertAlmostEqual(project["cost_usd"], payload["months"][0]["cost_usd"],
                                   places=6)

    def test_the_other_bucket_never_reuses_a_model_slot(self) -> None:
        """Models past MAX_MODEL_SERIES fold into "Other". If that bucket took a
        real slot index, it would be drawn in the same colour as the last ranked
        model -- in the chart and the legend both."""
        with tempfile.TemporaryDirectory() as tmp:
            rows = [
                _event(id=f"e{i}", model=f"model-{i:02d}",
                       input_tokens=(100 - i) * 1000)
                for i in range(dashboard.MAX_MODEL_SERIES + 3)
            ]
            payload = dashboard.build_payload(self._conn(tmp, rows))

            self.assertIn("Other", payload["models"])
            self.assertNotIn("Other", payload["model_slot"])
            self.assertEqual(len(payload["model_slot"]), dashboard.MAX_MODEL_SERIES)
            # Every ranked model holds a distinct slot, so the neutral fallback
            # for "Other" cannot collide with any of them.
            self.assertEqual(sorted(payload["model_slot"].values()),
                             list(range(dashboard.MAX_MODEL_SERIES)))


if __name__ == "__main__":
    unittest.main()
