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


def match(home, away, home_goals=None, away_goals=None, status="FINISHED", minute=None, utc_date=None, winner=None):
    """Build a football-data.org-shaped match dict for tests."""
    return {
        "homeTeam": {"name": home},
        "awayTeam": {"name": away},
        "score": {"fullTime": {"home": home_goals, "away": away_goals}, "winner": winner},
        "status": status,
        "minute": minute,
        "utcDate": utc_date,
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

    def test_in_play_includes_minute_when_present(self):
        scores, _ = fs.build_scores([match("Mexico", "South Africa", 1, 0, status="IN_PLAY", minute=63)])
        self.assertEqual(scores["1"], {"h": 1, "a": 0, "s": "IN_PLAY", "min": 63})

    def test_in_play_minute_as_string_is_parsed_to_int(self):
        scores, _ = fs.build_scores([match("Mexico", "South Africa", 1, 0, status="IN_PLAY", minute="63")])
        self.assertEqual(scores["1"]["min"], 63)

    def test_in_play_without_minute_has_no_min_key(self):
        # feed omitted the minute (typical on the free tier) -> graceful, no key
        scores, _ = fs.build_scores([match("Mexico", "South Africa", 1, 0, status="IN_PLAY", minute=None)])
        self.assertNotIn("min", scores["1"])

    def test_minute_only_attached_to_in_play(self):
        # half-time and final results must not carry a (meaningless) minute
        paused, _ = fs.build_scores([match("Mexico", "South Africa", 0, 0, status="PAUSED", minute=46)])
        self.assertNotIn("min", paused["1"])
        finished, _ = fs.build_scores([match("Mexico", "South Africa", 2, 0, status="FINISHED", minute=90)])
        self.assertNotIn("min", finished["1"])

    def test_garbage_minute_is_ignored(self):
        scores, _ = fs.build_scores([match("Mexico", "South Africa", 1, 0, status="IN_PLAY", minute="HT")])
        self.assertNotIn("min", scores["1"])


class TestKnockoutMapping(unittest.TestCase):
    def test_knockout_maps_by_kickoff_time_and_carries_teams(self):
        # Match 73 kicks off 2026-06-28T19:00:00Z. The teams (any pairing) come from the feed.
        scores, _ = fs.build_scores([match("South Africa", "Canada", 0, 1, utc_date="2026-06-28T19:00:00Z")])
        self.assertEqual(scores["73"], {"h": 0, "a": 1, "s": "FINISHED", "home": "South Africa", "away": "Canada"})

    def test_knockout_uses_aliased_team_names(self):
        # M75 is Netherlands vs Morocco in reality; feed spellings resolve to canonical names.
        scores, _ = fs.build_scores([match("Netherlands", "Morocco", 1, 1, utc_date="2026-06-30T01:00:00Z")])
        self.assertEqual(scores["75"]["home"], "Netherlands")
        self.assertEqual(scores["75"]["away"], "Morocco")

    def test_knockout_rematch_of_group_pair_maps_to_knockout_not_group(self):
        # A KO kickoff time wins even if the pairing coincides with a group fixture (Mexico/South Africa = M1).
        scores, _ = fs.build_scores([match("Mexico", "South Africa", 2, 1, utc_date="2026-06-28T19:00:00Z")])
        self.assertIn("73", scores)
        self.assertNotIn("1", scores)

    def test_knockout_tolerates_minor_time_drift(self):
        # 30 min later than the scheduled 19:00 slot still maps to M73 (within 2h tolerance).
        scores, _ = fs.build_scores([match("South Africa", "Canada", 0, 1, utc_date="2026-06-28T19:30:00Z")])
        self.assertIn("73", scores)

    def test_pre_draw_knockout_with_tbd_names_is_skipped(self):
        # Before the draw the feed has empty/TBD team names -> nothing to record.
        scores, _ = fs.build_scores([match("", "", None, None, status="TIMED", utc_date="2026-07-14T19:00:00Z")])
        self.assertEqual(scores, {})

    def test_group_match_with_utcdate_still_maps_by_pair(self):
        # A group fixture carrying its real kickoff time is far from any KO slot -> group path unchanged.
        scores, _ = fs.build_scores([match("Mexico", "South Africa", 2, 0, utc_date="2026-06-11T19:00:00Z")])
        self.assertEqual(scores["1"], {"h": 2, "a": 0, "s": "FINISHED"})
        self.assertNotIn("home", scores["1"])

    def test_penalty_shootout_winner_is_captured(self):
        # Level regulation score; football-data.org's winner field says who advanced.
        scores, _ = fs.build_scores([match("Germany", "Paraguay", 1, 1, utc_date="2026-06-29T20:30:00Z", winner="AWAY_TEAM")])
        self.assertEqual(scores["74"], {"h": 1, "a": 1, "s": "FINISHED", "home": "Germany", "away": "Paraguay", "w": "a"})

    def test_home_shootout_winner(self):
        scores, _ = fs.build_scores([match("Netherlands", "Morocco", 2, 2, utc_date="2026-06-30T01:00:00Z", winner="HOME_TEAM")])
        self.assertEqual(scores["75"]["w"], "h")

    def test_decisive_knockout_has_no_winner_field(self):
        # A clear result doesn't need the winner hint (the score alone decides it).
        scores, _ = fs.build_scores([match("Brazil", "Japan", 2, 1, utc_date="2026-06-29T17:00:00Z", winner="HOME_TEAM")])
        self.assertNotIn("w", scores["76"])  # decisive scores carry no redundant w



if __name__ == "__main__":
    unittest.main(verbosity=2)
