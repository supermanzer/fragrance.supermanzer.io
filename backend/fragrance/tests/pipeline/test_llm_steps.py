from django.contrib.auth.models import User
from django.test import TestCase
from fragrance.llm import (
    CandidateCountError,
    generate_email_content,
    select_candidates,
    select_replacement,
)
from fragrance.models import PreferenceProfile, Recommendation, RecommendationRun
from fragrance.tests.support.llm_fakes import _FakeProvider


class GenerateEmailContentTests(TestCase):
    def setUp(self) -> None:
        self.user = User.objects.create_user(
            username="dave", password="hal-9000"
        )
        self.profile = PreferenceProfile.objects.create(
            user=self.user,
            loved_notes="oud, amber",
            liked_notes="citrus",
            disliked_notes="aquatic",
            owns_list=[],
            search_angle_1="niche oud fragrances",
            search_angle_2="amber fragrances 2026",
        )
        self.run = RecommendationRun.objects.create(
            user=self.user, profile=self.profile, status="running"
        )

    def _make_confirmed(self, name: str, house: str = "House") -> Recommendation:
        return Recommendation.objects.create(
            user=self.user,
            run=self.run,
            name=name,
            house=house,
            status="confirmed",
            rationale="",
        )

    def test_exact_match_rationale_assignment(self) -> None:
        rec1 = self._make_confirmed(name="Aventus")
        rec2 = self._make_confirmed(name="Bleu de Chanel")
        verified_picks = [
            {"name": "Aventus", "house": "House", "status": "confirmed"},
            {"name": "Bleu de Chanel", "house": "House", "status": "confirmed"},
        ]
        fake_result = {
            "intro": "Welcome back.",
            "rationales": [
                {"name": "Aventus", "rationale": "Smoky and bold."},
                {"name": "Bleu de Chanel", "rationale": "Clean and versatile."},
            ],
        }

        fake_provider = _FakeProvider(response=fake_result)
        generate_email_content(
            run_id=self.run.id, verified_picks=verified_picks, provider=fake_provider
        )

        rec1.refresh_from_db()
        rec2.refresh_from_db()
        self.assertEqual(rec1.rationale, "Smoky and bold.")
        self.assertEqual(rec2.rationale, "Clean and versatile.")

        self.run.refresh_from_db()
        self.assertEqual(self.run.intro, "Welcome back.")

        # The enum sent to the provider must be the exact confirmed names, in creation order.
        self.assertEqual(len(fake_provider.calls), 1)
        sent_schema = fake_provider.calls[0]["schema"]
        sent_enum = sent_schema["properties"]["rationales"]["items"]["properties"][
            "name"
        ]["enum"]
        self.assertEqual(sent_enum, ["Aventus", "Bleu de Chanel"])

    def test_missing_rationale_logs_warning_and_does_not_raise(self) -> None:
        rec1 = self._make_confirmed(name="Aventus")
        verified_picks = [{"name": "Aventus", "house": "House", "status": "confirmed"}]
        # LLM returns a paraphrased name that doesn't match the enum-constrained rec name.
        fake_result = {
            "intro": "Welcome back.",
            "rationales": [
                {"name": "Aventus Eau de Parfum", "rationale": "Smoky and bold."}
            ],
        }

        fake_provider = _FakeProvider(response=fake_result)
        with self.assertLogs("fragrance.llm", level="WARNING") as logs:
            generate_email_content(
                run_id=self.run.id,
                verified_picks=verified_picks,
                provider=fake_provider,
            )

        rec1.refresh_from_db()
        self.assertEqual(rec1.rationale, "")
        self.assertTrue(
            any("Aventus" in message for message in logs.output)
        )


class CandidateCountValidationTests(TestCase):
    """
    App-level guard for a pre-existing gap: no provider family reliably
    enforces the schema's minItems/maxItems at generation time (confirmed
    unsupported in OpenAI's strict-mode subset for our purposes; unconfirmed
    for Qwen; never enforced for Anthropic even pre-refactor).
    """

    def setUp(self) -> None:
        self.user = User.objects.create_user(username="dave", password="hal-9000")
        self.profile = PreferenceProfile.objects.create(
            user=self.user,
            loved_notes="oud",
            liked_notes="citrus",
            disliked_notes="aquatic",
            owns_list=[],
            search_angle_1="x",
            search_angle_2="y",
        )

    def test_four_candidates_rejected(self) -> None:
        provider = _FakeProvider(
            response={"candidates": [{"name": "A", "house": "H"}] * 4}
        )
        with self.assertRaises(CandidateCountError):
            select_candidates(
                user_id=self.user.id,
                profile_id=self.profile.id,
                search_results="",
                provider=provider,
            )

    def test_six_candidates_rejected(self) -> None:
        provider = _FakeProvider(
            response={"candidates": [{"name": "A", "house": "H"}] * 6}
        )
        with self.assertRaises(CandidateCountError):
            select_candidates(
                user_id=self.user.id,
                profile_id=self.profile.id,
                search_results="",
                provider=provider,
            )

    def test_exactly_five_candidates_accepted(self) -> None:
        provider = _FakeProvider(
            response={
                "candidates": [
                    {"name": f"Candidate {i}", "house": "H"} for i in range(5)
                ]
            }
        )
        result = select_candidates(
            user_id=self.user.id,
            profile_id=self.profile.id,
            search_results="",
            provider=provider,
        )
        self.assertEqual(len(result), 5)

    def test_select_replacement_missing_field_rejected(self) -> None:
        provider = _FakeProvider(response={"name": "Only Has A Name"})
        with self.assertRaises(CandidateCountError):
            select_replacement(
                user_id=self.user.id,
                failed_candidate={"name": "Failed", "house": "Nowhere"},
                search_results="",
                profile=self.profile,
                provider=provider,
            )
