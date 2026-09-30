"""search_source_url is stored and served only when it is an absolute http(s) URL."""

from unittest.mock import MagicMock, patch

from django.contrib.auth.models import User
from django.core import mail
from django.test import TestCase
from fragrance.models import FragranceConfig, PreferenceProfile, Recommendation, RecommendationRun
from fragrance.search import verify_candidates
from fragrance.serializers import RecommendationSerializer
from fragrance.services import http_url_or_empty
from fragrance.tasks import render_and_send_email


class HttpUrlOrEmptyTests(TestCase):
    def test_http_and_https_urls_pass_through(self) -> None:
        for url in ("http://fragrantica.com/x", "https://www.basenotes.net/a?b=1"):
            self.assertEqual(http_url_or_empty(url=url), url)

    def test_other_schemes_and_malformed_values_become_empty(self) -> None:
        for url in (
            "javascript:alert(1)", "JaVaScRiPt:alert(1)", "data:text/html,<b>x</b>", "ftp://x.org/a",
            "//evil.example/x", "https:///nohost", "not a url", "", "http://[::1", None, 42,
        ):
            self.assertEqual(http_url_or_empty(url=url), "", msg=repr(url))


class VerifyCandidatesUrlTests(TestCase):
    def setUp(self) -> None:
        self.user = User.objects.create_user(username="dave", password="hal-9000")
        self.profile = PreferenceProfile.objects.create(
            user=self.user, loved_notes="oud", liked_notes="", disliked_notes="",
            owns_list=[], search_angle_1="a", search_angle_2="b",
        )
        self.run = RecommendationRun.objects.create(user=self.user, status="running")

    def _verify(self, *, url: str) -> list[dict]:
        candidates = [{"name": "Aventus", "house": "Creed"}]
        with patch(
            "fragrance.search.searxng_search",
            return_value=[{"title": "Aventus review", "url": url, "content": "c"}],
        ):
            return verify_candidates(
                user_id=self.user.id, run_id=self.run.id, candidates=candidates,
                profile=self.profile, provider=MagicMock(),
            )

    def test_an_https_source_is_stored_and_returned(self) -> None:
        picks = self._verify(url="https://fragrantica.com/x")

        self.assertEqual(picks[0]["search_source_url"], "https://fragrantica.com/x")
        self.assertEqual(Recommendation.objects.get().search_source_url, "https://fragrantica.com/x")

    def test_a_javascript_source_is_still_a_confirmed_pick_but_with_no_url(self) -> None:
        picks = self._verify(url="javascript:alert(document.cookie)")

        self.assertEqual((picks[0]["status"], picks[0]["search_source_url"]), ("confirmed", ""))
        pick = Recommendation.objects.get()
        self.assertEqual((pick.status, pick.search_source_url), ("confirmed", ""))


class ServedUrlTests(TestCase):
    def setUp(self) -> None:
        self.user = User.objects.create_user(username="dave", email="dave@example.com", password="hal-9000")
        self.run = RecommendationRun.objects.create(user=self.user, status="done")

    def _pick(self, *, url: str) -> Recommendation:
        return Recommendation.objects.create(
            user=self.user, run=self.run, name="Aventus", house="Creed", status="confirmed",
            rationale="r", search_source_url=url,
        )

    def test_the_serializer_blanks_a_legacy_non_http_url_and_keeps_a_good_one(self) -> None:
        bad = self._pick(url="javascript:alert(1)")
        good = self._pick(url="https://fragrantica.com/x")

        self.assertEqual(RecommendationSerializer(bad).data["search_source_url"], "")
        self.assertEqual(RecommendationSerializer(good).data["search_source_url"], "https://fragrantica.com/x")

    def test_the_email_never_links_a_legacy_non_http_url(self) -> None:
        FragranceConfig.objects.create(user=self.user, recipient_email="dave@example.com")
        self._pick(url="javascript:alert(1)")

        render_and_send_email(user_id=self.user.id, run_id=self.run.id)

        self.assertNotIn("javascript:", mail.outbox[0].body)
