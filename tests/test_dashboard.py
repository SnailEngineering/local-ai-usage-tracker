from __future__ import annotations

import tempfile
import threading
import unittest
from unittest import mock
from pathlib import Path
from datetime import datetime, timezone

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


class ProjectLabelTests(unittest.TestCase):
    def test_a_unique_basename_stays_compact(self) -> None:
        self.assertEqual(
            dashboard.project_labels([("/Users/x/dev/web", "web"), ("/Users/x/dev/api", "api")]),
            {"/Users/x/dev/web": "web", "/Users/x/dev/api": "api"})

    def test_colliding_basenames_grow_only_as_far_as_they_must(self) -> None:
        labels = dashboard.project_labels([
            ("/w/acme/backend", "backend"), ("/w/globex/backend", "backend"),
            ("/w/solo/web", "web")])
        self.assertEqual(labels["/w/acme/backend"], "acme/backend")
        self.assertEqual(labels["/w/globex/backend"], "globex/backend")
        self.assertEqual(labels["/w/solo/web"], "web")

    def test_deeper_collisions_keep_widening(self) -> None:
        labels = dashboard.project_labels([
            ("/a/x/backend", "backend"), ("/b/x/backend", "backend")])
        self.assertEqual(labels["/a/x/backend"], "a/x/backend")
        self.assertEqual(labels["/b/x/backend"], "b/x/backend")

    def test_a_row_with_no_path_is_flagged_only_when_it_could_be_confused(self) -> None:
        # Pruned-archive rows kept the bare name and nothing else.
        self.assertEqual(dashboard.project_labels([(None, "old")]), {"old": "old"})
        labels = dashboard.project_labels([(None, "backend"), ("/w/acme/backend", "backend")])
        self.assertEqual(labels["/w/acme/backend"], "backend")
        self.assertEqual(labels["backend"], "backend (path unknown)")

    def test_a_bare_name_containing_a_slash_is_not_mistaken_for_a_path(self) -> None:
        self.assertEqual(dashboard.project_labels([(None, "a/b")]), {"a/b": "a/b"})


class DashboardTests(unittest.TestCase):
    def _conn(self, tmp: str, rows: list[dict]):
        conn = db.connect(Path(tmp) / "t.db")
        db.upsert_usage(conn, rows)
        conn.commit()
        return conn

    def test_provider_cards_reconcile_across_months_and_current_periods(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            conn = self._conn(tmp, [
                _event(id="a", day="2026-09-14", input_tokens=1_000_000,
                       output_tokens=0, cache_read_tokens=3_000_000),
                _event(id="b", day="2026-09-14", input_tokens=1_000_000, output_tokens=0),
                _event(id="c", day="2026-09-14", provider="openai", source="codex_local",
                       model="gpt-5.6-terra", input_tokens=1_000_000,
                       output_tokens=0, cache_read_tokens=1_000_000),
                _event(id="d", day="2026-09-20", provider="openai", source="codex_local",
                       model="unknown", input_tokens=0, output_tokens=0, cache_read_tokens=500),
                _event(id="e", day="2026-09-01", input_tokens=1_000_000, output_tokens=0),
                _event(id="f", day="2026-08-01", provider="openai", source="codex_local",
                       model="gpt-5.6-terra", input_tokens=1_000_000, output_tokens=0),
                _event(id="g", day="2026-08-02", input_tokens=1_000_000, output_tokens=0),
            ])
            self.addCleanup(conn.close)
            with mock.patch.object(dashboard, "datetime", wraps=datetime) as clock:
                clock.now.return_value = datetime(2026, 9, 20, 12, tzinfo=timezone.utc)
                payload = dashboard.build_payload(conn)
            providers = payload["provider_totals"]
            self.assertEqual(providers["anthropic"]["total_tokens"], 7_000_000)
            self.assertEqual(providers["openai"]["total_tokens"], 3_000_500)
            self.assertAlmostEqual(providers["anthropic"]["cost_usd"], 21.5)
            self.assertAlmostEqual(providers["openai"]["cost_usd"], 4.7)
            self.assertEqual(providers["anthropic"]["cache_read"], 3_000_000)
            self.assertEqual(providers["openai"]["cache_read"], 1_000_500)
            self.assertEqual(providers["anthropic"]["active_days"], 3)
            self.assertEqual(providers["openai"]["active_days"], 3)
            self.assertEqual(payload["totals"]["active_days"], 5)  # shared day counted once
            self.assertTrue(providers["openai"]["unpriced"])
            self.assertFalse(providers["anthropic"]["unpriced"])

            for metric in ("cost_usd", "total_tokens", "cache_read"):
                self.assertAlmostEqual(sum(p[metric] for p in providers.values()), payload["totals"][metric])
                for month in payload["months"]:
                    split = payload["month_providers"][month["month"]]
                    self.assertAlmostEqual(sum(p[metric] for p in split.values()), month[metric])
            self.assertEqual(payload["month_providers"]["2026-09"]["anthropic"]["active_days"], 2)
            for period in ("month", "week", "today"):
                split = payload["period_providers"][period]
                self.assertAlmostEqual(sum(p["cost_usd"] for p in split.values()),
                                       payload["totals"]["cost_" + period])
            self.assertAlmostEqual(payload["period_providers"]["week"]["anthropic"]["cost_usd"], 11.5)
            today = payload["period_providers"]["today"]
            self.assertEqual(today["anthropic"]["active_days"], 0)
            self.assertEqual(today["openai"]["priced_events"], 0)
            self.assertTrue(today["openai"]["unpriced"])

    def test_provider_cards_include_zero_usage_for_a_missing_provider(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            conn = self._conn(tmp, [_event()])
            self.addCleanup(conn.close)
            payload = dashboard.build_payload(conn)
            for split in (payload["provider_totals"], payload["month_providers"]["2026-08"]):
                self.assertEqual(split["openai"]["cost_usd"], 0)
                self.assertEqual(split["openai"]["total_tokens"], 0)
                self.assertEqual(split["openai"]["active_days"], 0)
                self.assertFalse(split["openai"]["unpriced"])

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

    def test_same_named_projects_in_different_directories_stay_separate(self) -> None:
        """Both collectors used to keep only the basename, so two unrelated
        `backend` checkouts were summed into one row."""
        with tempfile.TemporaryDirectory() as tmp:
            conn = self._conn(tmp, [
                _event(id="a", project="backend", project_path="/w/acme/backend",
                       input_tokens=1_000_000),
                _event(id="b", project="backend", project_path="/w/globex/backend",
                       input_tokens=2_000_000, ts="2026-09-01T10:00:00Z", day="2026-09-01"),
                _event(id="c", project="backend", project_path="/w/globex/backend",
                       input_tokens=1_000_000, ts="2026-09-02T10:00:00Z", day="2026-09-02"),
            ])
            payload = dashboard.build_payload(conn)
            by_name = {p["project"]: p for p in payload["projects"]}
            self.assertEqual(set(by_name), {"acme/backend", "globex/backend"})
            self.assertEqual(by_name["acme/backend"]["messages"], 1)
            self.assertEqual(by_name["globex/backend"]["messages"], 2)
            # ...and the month drill-down names them the same way.
            self.assertEqual(
                {p["project"] for p in payload["month_projects"]["2026-09"]},
                {"globex/backend"})

    def test_a_project_split_across_providers_is_still_one_row(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            conn = self._conn(tmp, [
                _event(id="a", project="web", project_path="/w/web"),
                _event(id="b", project="web", project_path="/w/web", provider="openai",
                       source="codex_local", model="gpt-5"),
            ])
            projects = dashboard.build_payload(conn)["projects"]
            self.assertEqual([(p["project"], p["messages"]) for p in projects], [("web", 2)])

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


class BuildWriteTests(unittest.TestCase):
    def _conn(self, tmp: str):
        conn = db.connect(Path(tmp) / "t.db")
        db.upsert_usage(conn, [_event()])
        conn.commit()
        return conn

    def test_the_page_is_swapped_in_atomically_leaving_no_temp_behind(self) -> None:
        """More than one process writes this path -- the launchd schedule, a
        manual ./collect.py. A plain write lets a reader see a half-written
        page, so build() renames a temp file into place instead."""
        with tempfile.TemporaryDirectory() as tmp:
            conn = self._conn(tmp)
            out = Path(tmp) / "sub" / "dashboard.html"

            dashboard.build(conn, out)
            self.assertIn("<title>AI Usage</title>", out.read_text())

            dashboard.build(conn, out)   # overwriting an existing page works
            self.assertIn("<title>AI Usage</title>", out.read_text())

            self.assertEqual([p.name for p in out.parent.iterdir()],
                             ["dashboard.html"], "a temp file was left behind")

    def test_a_write_that_dies_before_the_rename_keeps_the_old_page(self) -> None:
        """The rename is the commit point. Writing the page in place instead
        would truncate it first, so a crash mid-write leaves a reader -- a
        browser on file://, the next --serve -- with half a document."""
        with tempfile.TemporaryDirectory() as tmp:
            conn = self._conn(tmp)
            out = Path(tmp) / "dashboard.html"
            dashboard.build(conn, out)
            good = out.read_text()

            # Fails after the content is written, before it is swapped in --
            # exactly the window a plain write_text has no answer for.
            with mock.patch.object(dashboard.os, "replace",
                                   side_effect=OSError("no space left on device")):
                with self.assertRaises(OSError):
                    dashboard.build(conn, out)

            self.assertEqual(out.read_text(), good, "the previous page was lost")
            leftovers = [p.name for p in out.parent.iterdir()
                         if p.name.startswith("dashboard.html.")]
            self.assertEqual(leftovers, [], "a temp file was left behind")

    def test_concurrent_writers_do_not_share_a_temp_file(self) -> None:
        """--serve writes through from request threads, so two writers can
        share a pid. A temp name derived from the pid alone would have them
        open the same file and interleave into it, and the loser's rename
        would find nothing there."""
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "dashboard.html"
            # Big and distinct, so an interleave shows up as a mixed file
            # rather than two writes that happen to agree.
            pages = [chr(ord("a") + i) * 200_000 for i in range(8)]
            errors: list[BaseException] = []

            def writer(html: str) -> None:
                try:
                    dashboard.write_page(html, out)
                except BaseException as exc:   # noqa: BLE001 - reported below
                    errors.append(exc)

            threads = [threading.Thread(target=writer, args=(page,)) for page in pages]
            for t in threads:
                t.start()
            for t in threads:
                t.join()

            self.assertEqual(errors, [])
            # Whoever renamed last wins, but the file must be exactly one
            # page -- never two spliced together.
            self.assertIn(out.read_text(), pages)
            self.assertEqual([p.name for p in out.parent.iterdir()],
                             ["dashboard.html"], "a temp file was left behind")
