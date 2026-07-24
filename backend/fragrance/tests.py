from types import SimpleNamespace
from unittest.mock import patch

from django.contrib.auth.models import User
from django.test import TestCase

from .llm import build_email_content_tool_schema, generate_email_content
from .models import PreferenceProfile, Recommendation, RecommendationRun


def _fake_response(input_data: dict) -> SimpleNamespace:
    """Builds a minimal stand-in for an anthropic.types.Message with one tool_use block."""
    tool_use_block = SimpleNamespace(type="tool_use", input=input_data)
    return SimpleNamespace(content=[tool_use_block], stop_reason="tool_use")


class BuildEmailContentToolSchemaTests(TestCase):
    def test_enum_contains_exact_confirmed_names(self) -> None:
        schema = build_email_content_tool_schema(
            confirmed_names=["Aventus", "Bleu de Chanel"]
        )
        name_schema = schema["input_schema"]["properties"]["rationales"]["items"][
            "properties"
        ]["name"]
        self.assertEqual(name_schema["enum"], ["Aventus", "Bleu de Chanel"])
        self.assertTrue(schema["strict"])

    def test_no_enum_key_when_no_confirmed_names(self) -> None:
        schema = build_email_content_tool_schema(confirmed_names=[])
        name_schema = schema["input_schema"]["properties"]["rationales"]["items"][
            "properties"
        ]["name"]
        self.assertNotIn("enum", name_schema)


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

        with patch("fragrance.llm.client") as mock_client:
            mock_client.messages.create.return_value = _fake_response(fake_result)
            generate_email_content(run_id=self.run.id, verified_picks=verified_picks)

        rec1.refresh_from_db()
        rec2.refresh_from_db()
        self.assertEqual(rec1.rationale, "Smoky and bold.")
        self.assertEqual(rec2.rationale, "Clean and versatile.")

        self.run.refresh_from_db()
        self.assertEqual(self.run.intro, "Welcome back.")

        # The enum sent to the API must be the exact confirmed names, in creation order.
        call_kwargs = mock_client.messages.create.call_args.kwargs
        sent_schema = call_kwargs["tools"][0]
        sent_enum = sent_schema["input_schema"]["properties"]["rationales"]["items"][
            "properties"
        ]["name"]["enum"]
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

        with patch("fragrance.llm.client") as mock_client:
            mock_client.messages.create.return_value = _fake_response(fake_result)
            with self.assertLogs("fragrance.llm", level="WARNING") as logs:
                generate_email_content(
                    run_id=self.run.id, verified_picks=verified_picks
                )

        rec1.refresh_from_db()
        self.assertEqual(rec1.rationale, "")
        self.assertTrue(
            any("Aventus" in message for message in logs.output)
        )
