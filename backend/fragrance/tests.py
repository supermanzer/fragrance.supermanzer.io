from types import SimpleNamespace
from unittest.mock import patch

from django.contrib.auth.models import User
from django.core import mail
from django.test import TestCase, override_settings
from rest_framework import status
from rest_framework.test import APIClient

from .llm import build_email_content_tool_schema, generate_email_content
from .models import Fragrance, FragranceConfig, PreferenceProfile, Recommendation, RecommendationRun
from .services import generate_recommendation_token
from .tasks import render_and_send_email


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


def _make_run_with_pick(
    user: User, name: str = "Aventus", house: str = "Creed", pick_status: str = "confirmed"
) -> Recommendation:
    run = RecommendationRun.objects.create(user=user, status="done")
    return Recommendation.objects.create(
        user=user, run=run, name=name, house=house, status=pick_status, rationale="Smoky and bold."
    )


class RecommendationRateViewTests(TestCase):
    def setUp(self) -> None:
        self.user = User.objects.create_user(username="dave", password="hal-9000")
        self.other_user = User.objects.create_user(username="frank", password="hal-9000")
        self.client = APIClient()
        self.client.force_authenticate(user=self.user)

    def _rate_url(self, pick_id: int) -> str:
        return f"/api/v1/recommendations/{pick_id}/rate/"

    def test_unauthenticated_request_rejected(self) -> None:
        rec = _make_run_with_pick(self.user)
        anon_client = APIClient()
        response = anon_client.post(self._rate_url(rec.id), {"action": "own"}, format="json")
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)

    def test_other_users_pick_returns_404(self) -> None:
        rec = _make_run_with_pick(self.other_user)
        response = self.client.post(self._rate_url(rec.id), {"action": "own"}, format="json")
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

    def test_first_rating_creates_fragrance(self) -> None:
        rec = _make_run_with_pick(self.user)
        response = self.client.post(self._rate_url(rec.id), {"action": "own"}, format="json")
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertEqual(response.data["action"], "own")
        self.assertEqual(response.data["name"], "Aventus")

        fragrance = Fragrance.objects.get(user=self.user, name="Aventus")
        self.assertEqual(fragrance.status, "own")
        self.assertEqual(fragrance.source_recommendation_id, rec.id)

    def test_resubmitting_same_action_is_idempotent(self) -> None:
        rec = _make_run_with_pick(self.user)
        self.client.post(self._rate_url(rec.id), {"action": "like"}, format="json")

        response = self.client.post(self._rate_url(rec.id), {"action": "like"}, format="json")
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertTrue(response.data["already_rated"])
        self.assertEqual(Fragrance.objects.filter(user=self.user, name="Aventus").count(), 1)

    def test_resubmitting_different_action_returns_conflict(self) -> None:
        rec = _make_run_with_pick(self.user)
        self.client.post(self._rate_url(rec.id), {"action": "like"}, format="json")

        response = self.client.post(self._rate_url(rec.id), {"action": "dislike"}, format="json")
        self.assertEqual(response.status_code, status.HTTP_409_CONFLICT)
        self.assertEqual(response.data["existing_action"], "like")

        fragrance = Fragrance.objects.get(user=self.user, name="Aventus")
        self.assertEqual(fragrance.status, "like")

    def test_replaced_pick_cannot_be_rated(self) -> None:
        rec = _make_run_with_pick(self.user, pick_status="replaced")
        response = self.client.post(self._rate_url(rec.id), {"action": "own"}, format="json")
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertFalse(Fragrance.objects.filter(user=self.user).exists())

    def test_invalid_action_value_rejected(self) -> None:
        rec = _make_run_with_pick(self.user)
        response = self.client.post(self._rate_url(rec.id), {"action": "meh"}, format="json")
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_name_collision_with_existing_collection_entry_succeeds(self) -> None:
        Fragrance.objects.create(user=self.user, name="Aventus", house="Old House", status="dislike")
        rec = _make_run_with_pick(self.user)

        response = self.client.post(self._rate_url(rec.id), {"action": "own"}, format="json")
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertEqual(Fragrance.objects.filter(user=self.user, name="Aventus").count(), 1)

        fragrance = Fragrance.objects.get(user=self.user, name="Aventus")
        self.assertEqual(fragrance.status, "own")
        self.assertEqual(fragrance.house, "Creed")
        self.assertEqual(fragrance.source_recommendation_id, rec.id)

    def test_second_recommendation_with_same_name_does_not_hijack_first(self) -> None:
        # Two distinct recommendations (different runs) sharing the same name — a
        # realistic LLM re-emission across monthly runs, not a contrived collision.
        first_rec = _make_run_with_pick(self.user)
        second_rec = _make_run_with_pick(self.user)
        self.assertNotEqual(first_rec.id, second_rec.id)

        first_response = self.client.post(self._rate_url(first_rec.id), {"action": "own"}, format="json")
        self.assertEqual(first_response.status_code, status.HTTP_201_CREATED)

        second_response = self.client.post(
            self._rate_url(second_rec.id), {"action": "dislike"}, format="json"
        )
        self.assertEqual(second_response.status_code, status.HTTP_409_CONFLICT)
        self.assertEqual(second_response.data["existing_action"], "own")

        # The first recommendation's link and rating must survive untouched.
        fragrance = Fragrance.objects.get(user=self.user, name="Aventus")
        self.assertEqual(fragrance.status, "own")
        self.assertEqual(fragrance.source_recommendation_id, first_rec.id)
        first_rec.refresh_from_db()
        self.assertIsNotNone(getattr(first_rec, "promoted_fragrance", None))

    def test_rerating_after_fragrance_rename_is_still_idempotent(self) -> None:
        # Regression: apply_recommendation_rating must resolve "already rated" via
        # the FK link, not a (user, name) lookup that a rename can defeat.
        rec = _make_run_with_pick(self.user)
        response = self.client.post(self._rate_url(rec.id), {"action": "like"}, format="json")
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)

        fragrance = Fragrance.objects.get(user=self.user, source_recommendation_id=rec.id)
        fragrance.name = "Aventus Renamed"
        fragrance.save(update_fields=["name"])

        response = self.client.post(self._rate_url(rec.id), {"action": "like"}, format="json")
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertTrue(response.data["already_rated"])
        self.assertEqual(Fragrance.objects.filter(user=self.user).count(), 1)

    def test_rerating_after_fragrance_rename_with_different_action_returns_conflict(self) -> None:
        rec = _make_run_with_pick(self.user)
        response = self.client.post(self._rate_url(rec.id), {"action": "like"}, format="json")
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)

        fragrance = Fragrance.objects.get(user=self.user, source_recommendation_id=rec.id)
        fragrance.name = "Aventus Renamed"
        fragrance.save(update_fields=["name"])

        response = self.client.post(self._rate_url(rec.id), {"action": "dislike"}, format="json")
        self.assertEqual(response.status_code, status.HTTP_409_CONFLICT)
        self.assertEqual(response.data["existing_action"], "like")

        fragrance.refresh_from_db()
        self.assertEqual(fragrance.status, "like")
        self.assertEqual(fragrance.name, "Aventus Renamed")


class RecommendationConfirmViewTests(TestCase):
    def setUp(self) -> None:
        self.user = User.objects.create_user(username="dave", password="hal-9000")
        self.other_user = User.objects.create_user(username="frank", password="hal-9000")
        self.client = APIClient()  # deliberately unauthenticated throughout

    def _token(self, rec: Recommendation, action: str = "own") -> str:
        return generate_recommendation_token(recommendation=rec, action=action)

    def _get(self, token: str):
        return self.client.get(f"/api/v1/recommendations/confirm/?t={token}")

    def _post(self, token: str):
        return self.client.post(f"/api/v1/recommendations/confirm/?t={token}")

    def test_preview_works_without_authorization_header(self) -> None:
        rec = _make_run_with_pick(self.user)
        response = self._get(self._token(rec))
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertFalse(response.data["already_rated"])
        self.assertIsNone(response.data["rated_as"])
        self.assertEqual(response.data["name"], "Aventus")

    def test_preview_reports_existing_rating(self) -> None:
        rec = _make_run_with_pick(self.user)
        Fragrance.objects.create(
            user=self.user, name=rec.name, house=rec.house, status="like", source_recommendation=rec
        )
        response = self._get(self._token(rec, action="own"))
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertTrue(response.data["already_rated"])
        self.assertEqual(response.data["rated_as"], "like")
        self.assertEqual(response.data["action"], "own")

    def test_token_attribution_is_only_from_signed_payload(self) -> None:
        rec = _make_run_with_pick(self.user)
        # A token whose uid claims a different owner than the recommendation's real
        # user_id must not resolve — attribution comes only from the signed payload.
        from django.core import signing

        from .services import RECOMMENDATION_FEEDBACK_SALT

        forged_token = signing.dumps(
            {"rid": rec.id, "action": "own", "uid": self.other_user.id},
            salt=RECOMMENDATION_FEEDBACK_SALT,
        )
        response = self._get(forged_token)
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)
        self.assertEqual(response.data, {"detail": "Not found."})

    def test_tampered_token_returns_generic_404(self) -> None:
        rec = _make_run_with_pick(self.user)
        token = self._token(rec)
        response = self._get(token[:-1] + ("x" if token[-1] != "x" else "y"))
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)
        self.assertEqual(response.data, {"detail": "Not found."})

    def test_expired_token_returns_generic_404(self) -> None:
        rec = _make_run_with_pick(self.user)
        token = self._token(rec)
        with patch("fragrance.services.RECOMMENDATION_FEEDBACK_MAX_AGE", -1):
            response = self._get(token)
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)
        self.assertEqual(response.data, {"detail": "Not found."})

    def test_replaced_pick_token_returns_404(self) -> None:
        rec = _make_run_with_pick(self.user, pick_status="replaced")
        response = self._get(self._token(rec))
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

    def test_confirm_post_success_then_conflict_on_second_use(self) -> None:
        rec = _make_run_with_pick(self.user)
        token = self._token(rec, action="own")

        first = self._post(token)
        self.assertEqual(first.status_code, status.HTTP_200_OK)
        self.assertEqual(first.data["action"], "own")

        second = self._post(token)
        self.assertEqual(second.status_code, status.HTTP_409_CONFLICT)
        self.assertEqual(second.data["rated_as"], "own")

        self.assertEqual(Fragrance.objects.filter(user=self.user, name="Aventus").count(), 1)

    def test_confirm_post_conflict_on_different_action(self) -> None:
        rec = _make_run_with_pick(self.user)
        self._post(self._token(rec, action="like"))

        response = self._post(self._token(rec, action="dislike"))
        self.assertEqual(response.status_code, status.HTTP_409_CONFLICT)
        self.assertEqual(response.data["rated_as"], "like")


@override_settings(
    EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend",
    FRONTEND_URL="http://test.local",
)
class RenderAndSendEmailTests(TestCase):
    def setUp(self) -> None:
        self.user = User.objects.create_user(username="dave", password="hal-9000")
        self.config = FragranceConfig.objects.create(
            user=self.user, recipient_email="dave@example.com"
        )
        self.run = RecommendationRun.objects.create(user=self.user, status="done", intro="Welcome back.")
        self.confirmed_pick = Recommendation.objects.create(
            user=self.user, run=self.run, name="Aventus", house="Creed",
            status="confirmed", rationale="Smoky and bold.",
        )
        Recommendation.objects.create(
            user=self.user, run=self.run, name="Rejected Pick", house="Nowhere",
            status="replaced", rationale="Failed verification.",
        )

    def test_email_contains_three_confirm_urls_for_confirmed_pick_only(self) -> None:
        render_and_send_email(user_id=self.user.id, run_id=self.run.id)

        self.assertEqual(len(mail.outbox), 1)
        body = mail.outbox[0].body
        self.assertEqual(body.count("http://test.local/recommendations/confirm?t="), 3)
        self.assertNotIn("Rejected Pick", body)

        self.run.refresh_from_db()
        self.assertEqual(self.run.email_status, "sent")

    def test_missing_config_propagates_for_celery_retry(self) -> None:
        self.config.delete()
        with self.assertRaises(FragranceConfig.DoesNotExist):
            render_and_send_email(user_id=self.user.id, run_id=self.run.id)
