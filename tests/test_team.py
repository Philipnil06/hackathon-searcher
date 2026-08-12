import unittest

from hackathon_searcher.team import MAX_TEAM_SIZE, TeamConfig


class TeamConfigTests(unittest.TestCase):
    def test_solo_and_four_member_teams_are_valid(self):
        self.assertEqual(TeamConfig.from_dict({"members": ["alex"]}).members, ("alex",))
        members = ("alex", "sam", "jordan", "riley")
        self.assertEqual(TeamConfig.from_dict({"team_id": "demo", "members": members, "minimum_team_score": 60}).members, members)

    def test_team_rejects_more_than_four_or_duplicate_members(self):
        with self.assertRaises(ValueError):
            TeamConfig.from_dict({"members": ["a", "b", "c", "d", "e"]})
        with self.assertRaises(ValueError):
            TeamConfig.from_dict({"members": ["alex", "alex"]})

    def test_maximum_team_size_is_four(self):
        self.assertEqual(MAX_TEAM_SIZE, 4)


if __name__ == "__main__":
    unittest.main()
