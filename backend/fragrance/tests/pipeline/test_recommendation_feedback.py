from unittest.mock import patch

from django.contrib.auth.models import User
from django.test import TestCase
from fragrance.models import Fragrance, Recommendation
from fragrance.services import generate_recommendation_token
from fragrance.tests.support.factories import _make_run_with_pick, active_subscription_for
from rest_framework import status
from rest_framework.test import APIClient


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

        from fragrance.services import RECOMMENDATION_FEEDBACK_SALT

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


class SourceRecommendationOwnershipTests(TestCase):
    """A user can never link their own Fragrance to another tenant's pick."""

    def setUp(self) -> None:
        self.alice = User.objects.create_user(username="alice", password="hal-9000")
        self.bob = User.objects.create_user(username="bob", password="hal-9000")
        for user in (self.alice, self.bob):
            active_subscription_for(user)
        self.alice_client = APIClient(raise_request_exception=False)
        self.alice_client.force_authenticate(user=self.alice)
        self.bob_client = APIClient(raise_request_exception=False)
        self.bob_client.force_authenticate(user=self.bob)

    def test_collection_create_ignores_source_recommendation(self) -> None:
        pick = _make_run_with_pick(self.alice)

        response = self.bob_client.post(
            "/api/v1/collection/",
            {"name": "Bob frag", "house": "BobHouse", "status": "own", "source_recommendation": pick.id},
            format="json",
        )

        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertIsNone(response.data["source_recommendation"])
        self.assertIsNone(Fragrance.objects.get(user=self.bob, name="Bob frag").source_recommendation_id)

    def test_collection_update_ignores_source_recommendation(self) -> None:
        pick = _make_run_with_pick(self.alice)
        mine = Fragrance.objects.create(user=self.bob, name="Bob frag", house="BobHouse", status="own")

        response = self.bob_client.patch(
            f"/api/v1/collection/{mine.id}/", {"source_recommendation": pick.id}, format="json"
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        mine.refresh_from_db()
        self.assertIsNone(mine.source_recommendation_id)

    def test_owner_rating_is_unaffected_by_other_users_collection(self) -> None:
        pick = _make_run_with_pick(self.alice)
        self.bob_client.post(
            "/api/v1/collection/",
            {"name": "Bob frag", "house": "BobHouse", "status": "dislike", "source_recommendation": pick.id},
            format="json",
        )

        response = self.alice_client.post(
            f"/api/v1/recommendations/{pick.id}/rate/", {"action": "own"}, format="json"
        )

        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertEqual(response.data["name"], "Aventus")
        self.assertEqual(response.data["house"], "Creed")
        mine = Fragrance.objects.get(user=self.alice, source_recommendation=pick)
        self.assertEqual((mine.status, mine.name), ("own", "Aventus"))

    def test_preexisting_cross_tenant_link_never_returns_the_other_users_data(self) -> None:
        # State left behind by the old writable field: bob's row points at alice's pick.
        pick = _make_run_with_pick(self.alice)
        Fragrance.objects.create(
            user=self.bob, name="Bob frag", house="BobHouse", status="dislike", source_recommendation=pick
        )

        with self.assertLogs("django.request", level="ERROR"):
            response = self.alice_client.post(
                f"/api/v1/recommendations/{pick.id}/rate/", {"action": "own"}, format="json"
            )

        # bob's row still owns the unique link, so alice's create collides: an
        # honest 500 until the cleanup query removes the poisoned row. What
        # must hold is that bob's data is never handed to alice.
        self.assertEqual(response.status_code, status.HTTP_500_INTERNAL_SERVER_ERROR)
        self.assertNotIn(b"Bob frag", response.content)
        self.assertNotIn(b"BobHouse", response.content)
        self.assertNotIn(b"existing_action", response.content)
