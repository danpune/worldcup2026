#!/usr/bin/env python3
"""
Unit tests for fetch_scores.py — the score-fetching logic.

Uses only the standard library (unittest), matching the project's
"no third-party packages" design. Run with:

    python3 -m unittest -v
    # or
    python3 test_fetch_scores.py
"""
import unittest

import fetch_scores as fs


def match(home, away, home_goals=None, away_goals=None, status="FINISHED"):
    """Build a football-data.org-shaped match dict for tests."""
    return {
        "homeTeam": {"name": home},
        "awayTeam": {"name": away},
        "score": {"fullTime": {"home": home_goals, "away": away_goals}},
        "status": status,
    }


class TestNorm(unittest.TestCase):
    def test_lowercases_and_strips_non_alnum(self):
        self.assertEqual(fs.norm("New Zealand"), "newzealand")
        self.assertEqual(fs.norm("DR Congo"), "drcongo")

    def test_strips_accents(self):
        self.assertEqual(fs.norm("Türkiye"), "turkiye")
        self.assertEqual(fs.norm("Curaçao"), "curacao")
        self.assertEqual(fs.norm("Côte d'Ivoire"), "cotedivoire")

    def test_handles_none_and_empty(self):
        self.assertEqual(fs.norm(None), "")
        self.assertEqual(fs.norm(""), "")


class TestResolveTeam(unittest.TestCase):
    def test_exact_canonical_name(self):
        self.assertEqual(fs.resolve_team("Mexico"), "Mexico")
        self.assertEqual(fs.resolve_team("South Africa"), "South Africa")

    def test_alias_resolution(self):
        # football-data.org uses different spellings than the page
        self.assertEqual(fs.resolve_team("Korea Republic"), "South Korea")
        self.assertEqual(fs.resolve_team("Turkey"), "Türkiye")
        self.assertEqual(fs.resolve_team("IR Iran"), "Iran")
        self.assertEqual(fs.resolve_team("Côte d'Ivoire"), "Ivory Coast")
        self.assertEqual(fs.resolve_team("Congo DR"), "DR Congo")
        self.assertEqual(fs.resolve_team("Czech Republic"), "Czechia")

    def test_accented_canonical_name(self):
        self.assertEqual(fs.resolve_team("Türkiye"), "Türkiye")
        self.assertEqual(fs.resolve_team("Curaçao"), "Curaçao")

    def test_unknown_team_returns_none(self):
        self.assertIsNone(fs.resolve_team("Atlantis"))
        self.assertIsNone(fs.resolve_team(""))


class TestBuildScores(unittest.TestCase):
    def test_finished_group_match_maps_to_match_number(self):
        # Match 1 in the schedule is Mexico vs South Africa
        scores, unresolved = fs.build_scores([match("Mexico", "South Africa", 2, 0)])
        self.assertEqual(scores, {"1": {"h": 2, "a": 0, "s": "FINISHED"}})
        self.assertEqual(unresolved, set())

    def test_team_order_does_not_matter(self):
        # frozenset lookup means home/away order is irrelevant for mapping
        scores, _ = fs.build_scores([match("South Africa", "Mexico", 1, 3)])
        self.assertEqual(scores, {"1": {"h": 1, "a": 3, "s": "FINISHED"}})

    def test_awarded_status_normalized_to_finished(self):
        scores, _ = fs.build_scores([match("Mexico", "South Africa", 3, 0, status="AWARDED")])
        self.assertEqual(scores["1"]["s"], "FINISHED")

    def test_in_play_match_is_kept(self):
        scores, _ = fs.build_scores([match("Mexico", "South Africa", 1, 1, status="IN_PLAY")])
        self.assertEqual(scores["1"], {"h": 1, "a": 1, "s": "IN_PLAY"})

    def test_scheduled_match_without_score_is_skipped(self):
        scores, _ = fs.build_scores([match("Mexico", "South Africa", None, None, status="TIMED")])
        self.assertEqual(scores, {})

    def test_finished_match_missing_goals_is_skipped(self):
        scores, _ = fs.build_scores([match("Mexico", "South Africa", None, None, status="FINISHED")])
        self.assertEqual(scores, {})

    def test_non_scheduled_pairing_is_skipped(self):
        # Mexico vs Brazil is not a group-stage fixture in the schedule
        scores, _ = fs.build_scores([match("Mexico", "Brazil", 1, 0)])
        self.assertEqual(scores, {})

    def test_unresolved_team_name_is_collected(self):
        scores, unresolved = fs.build_scores([match("Narnia", "Mexico", 1, 0)])
        self.assertEqual(scores, {})
        self.assertIn("Narnia", unresolved)

    def test_zero_zero_draw_is_recorded(self):
        # 0-0 is a real result; must not be treated as "no score"
        scores, _ = fs.build_scores([match("Mexico", "South Africa", 0, 0)])
        self.assertEqual(scores["1"], {"h": 0, "a": 0, "s": "FINISHED"})


if __name__ == "__main__":
    unittest.main(verbosity=2)
