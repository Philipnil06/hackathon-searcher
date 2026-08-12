import json
import tempfile
import unittest
from pathlib import Path

from hackathon_searcher.profile import ApplicantProfile
from hackathon_searcher.scoring import _should_apply_for_applicant
from hackathon_searcher.travel import assess_accommodation, assess_location, assess_travel_support, travel_preferences


def profile_with(preferences: dict | None = None, **extra) -> ApplicantProfile:
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / "case.json"
        profile = {
            "applicant_id": "case", "name": "Case Tester", "email": "case@example.test", "age": 25,
            "location": {"city": "Stockholm", "country": "Sweden"}, "travel_preferences": preferences or {},
        }
        profile.update(extra)
        path.write_text(json.dumps(profile), encoding="utf-8")
        return ApplicantProfile(str(path), str(Path(directory) / "answers.json"))


def event(city="Stockholm", country="Sweden", mode="physical", status="UNKNOWN", details=None) -> dict:
    return {
        "city": city, "country": country, "physical_or_online": mode,
        "travel_support": status, "travel_support_details": details or {},
    }


class TravelPreferenceTests(unittest.TestCase):
    def test_city_only_accepts_same_city_and_rejects_foreign_event(self):
        profile = profile_with({"scope": "city", "travel_support": "not_important"})
        self.assertTrue(assess_location(event(), profile)["allowed"])
        self.assertFalse(assess_location(event("Oslo", "Norway"), profile)["allowed"])

    def test_country_scope_accepts_home_country_only(self):
        profile = profile_with({"scope": "country", "travel_support": "not_important"})
        self.assertTrue(assess_location(event("Gothenburg", "SE"), profile)["allowed"])
        self.assertFalse(assess_location(event("Oslo", "Norway"), profile)["allowed"])

    def test_europe_scope_accepts_europe_and_rejects_non_europe(self):
        profile = profile_with({"scope": "region", "region": "europe", "travel_support": "not_important"})
        self.assertTrue(assess_location(event("Paris", "France"), profile)["allowed"])
        self.assertFalse(assess_location(event("Tokyo", "Japan"), profile)["allowed"])

    def test_remote_is_explicitly_configurable(self):
        denied = profile_with({"scope": "city", "travel_support": "not_important", "include_remote": False})
        allowed = profile_with({"scope": "city", "travel_support": "not_important", "include_remote": True})
        self.assertFalse(assess_location(event(mode="online"), denied)["allowed"])
        self.assertTrue(assess_location(event(mode="online"), allowed)["allowed"])

    def test_support_not_important_and_preferred_never_block(self):
        unsupported = event(status="NO_TRAVEL_SUPPORT")
        neutral = profile_with({"scope": "anywhere", "travel_support": "not_important"})
        preferred = profile_with({"scope": "anywhere", "travel_support": "preferred", "accepted_support": ["flight_credits"]})
        self.assertTrue(assess_travel_support(unsupported, neutral)["meets_requirement"])
        self.assertTrue(assess_travel_support(unsupported, preferred)["meets_requirement"])

    def test_required_flights_do_not_accept_hotel_only(self):
        profile = profile_with({"scope": "anywhere", "travel_support": "required", "accepted_support": ["flight_credits"]})
        hotel_only = event(status="CONFIRMED_ACCOMMODATION_ONLY", details={"accommodation_provided": True})
        result = assess_travel_support(hotel_only, profile)
        self.assertFalse(result["meets_requirement"])
        self.assertEqual(result["status"], "UNSUPPORTED_REQUIRED")

    def test_required_reimbursement_honours_minimum(self):
        profile = profile_with({"scope": "anywhere", "travel_support": "required", "accepted_support": ["travel_reimbursement"], "minimum_reimbursement_eur": 150})
        enough = event(status="CONFIRMED_TRAVEL_REIMBURSEMENT", details={"amount": 250, "currency": "EUR"})
        too_low = event(status="CONFIRMED_TRAVEL_REIMBURSEMENT", details={"amount": 100, "currency": "EUR"})
        self.assertTrue(assess_travel_support(enough, profile)["meets_requirement"])
        self.assertEqual(assess_travel_support(too_low, profile)["status"], "BELOW_MINIMUM")

    def test_required_missing_support_is_blocked_and_accommodation_is_separate(self):
        profile = profile_with({"scope": "anywhere", "travel_support": "required", "accepted_support": ["any_travel_support"], "accommodation": "required"})
        self.assertEqual(assess_travel_support(event(), profile)["status"], "UNKNOWN_REQUIRED")
        self.assertFalse(assess_accommodation(event(), profile)["meets_requirement"])

    def test_legacy_profiles_are_interpreted_safely(self):
        profile = profile_with(None, travel_regions=["Europe"], travel_support_wanted=True)
        prefs = travel_preferences(profile)
        self.assertEqual(prefs["scope"], "region")
        self.assertEqual(prefs["travel_support"], "preferred")
        self.assertEqual(prefs["accepted_support"], ["any_travel_support"])

    def test_application_gate_rejects_required_unknown_support_even_with_high_scores(self):
        profile = profile_with({"scope": "anywhere", "travel_support": "required", "accepted_support": ["flight_credits"]})
        location = assess_location(event(), profile)
        support = assess_travel_support(event(), profile)
        accommodation = assess_accommodation(event(), profile)
        allowed, reason = _should_apply_for_applicant(
            95, 95, {"eligible": True}, location, support, accommodation, event(),
        )
        self.assertFalse(allowed)
        self.assertIn("Travel requirement", reason)


if __name__ == "__main__":
    unittest.main()
