from unittest.mock import patch

import redis
from django.contrib.auth.models import User
from django.core.cache import cache
from django.test import TestCase, override_settings
from fragrance.models import BetaSignup
from fragrance.services import normalize_email
from fragrance.views import BetaSignupThrottle


class NormalizeEmailTests(TestCase):
    def test_lowercases(self) -> None:
        self.assertEqual(normalize_email(email="Ryan@Example.com"), "ryan@example.com")

    def test_strips_plus_tag(self) -> None:
        self.assertEqual(normalize_email(email="ryan+newsletter@example.com"), "ryan@example.com")

    def test_strips_dots_on_gmail(self) -> None:
        self.assertEqual(normalize_email(email="r.y.a.n@gmail.com"), "ryan@gmail.com")

    def test_strips_dots_on_googlemail(self) -> None:
        self.assertEqual(normalize_email(email="r.yan@googlemail.com"), "ryan@googlemail.com")

    def test_does_not_strip_dots_on_non_gmail_domain(self) -> None:
        self.assertEqual(normalize_email(email="r.yan@example.com"), "r.yan@example.com")

    def test_gmail_dot_and_plus_tag_collide(self) -> None:
        self.assertEqual(
            normalize_email(email="r.yan+1@gmail.com"), normalize_email(email="ryan@gmail.com")
        )


class BetaSignupViewTests(TestCase):
    """
    Covers the public waitlist endpoint's core anti-abuse contract: a
    byte-identical response across every non-error outcome, the honeypot,
    and the Gmail-class dedup that normalize_email exists to enforce.
    """

    url = "/api/v1/beta/signup/"

    def setUp(self) -> None:
        # DRF's ScopedRateThrottle uses the default (process-wide) cache;
        # clear it so an earlier test's requests never count against this
        # test's 5/min budget.
        cache.clear()

    def test_new_signup_returns_202_and_creates_a_row(self) -> None:
        response = self.client.post(self.url, {"email": "newbie@example.com"})
        self.assertEqual(response.status_code, 202)
        self.assertTrue(
            BetaSignup.objects.filter(normalized_email="newbie@example.com").exists()
        )

    def test_new_duplicate_and_existing_user_return_byte_identical_bodies(self) -> None:
        User.objects.create_user(
            username="already", email="already@example.com", password="pw12345!"
        )

        new_response = self.client.post(self.url, {"email": "fresh@example.com"})
        duplicate_response = self.client.post(self.url, {"email": "fresh@example.com"})
        existing_user_response = self.client.post(self.url, {"email": "already@example.com"})

        self.assertEqual(new_response.status_code, 202)
        self.assertEqual(duplicate_response.status_code, 202)
        self.assertEqual(existing_user_response.status_code, 202)
        self.assertEqual(new_response.content, duplicate_response.content)
        self.assertEqual(new_response.content, existing_user_response.content)

        # Duplicate submissions collapse to one row (unique on
        # normalized_email); the "belongs to an existing User" case still
        # gets a waitlist row too -- deliberately not special-cased, since
        # branching on it would create a query-count/timing signal on an
        # endpoint whose whole point is not distinguishing these cases. The
        # admin-approval step (a later slice) is where that guard belongs.
        self.assertEqual(
            BetaSignup.objects.filter(normalized_email="fresh@example.com").count(), 1
        )
        self.assertEqual(
            BetaSignup.objects.filter(normalized_email="already@example.com").count(), 1
        )

    def test_already_approved_signup_returns_identical_body_and_is_not_reset(self) -> None:
        BetaSignup.objects.create(
            email="vip@example.com", normalized_email="vip@example.com", status="approved"
        )

        response = self.client.post(self.url, {"email": "vip@example.com"})
        control = self.client.post(self.url, {"email": "someone-else@example.com"})

        self.assertEqual(response.status_code, 202)
        self.assertEqual(response.content, control.content)
        signup = BetaSignup.objects.get(normalized_email="vip@example.com")
        self.assertEqual(signup.status, "approved")
        self.assertEqual(BetaSignup.objects.filter(normalized_email="vip@example.com").count(), 1)

    def test_malformed_email_returns_400_with_a_different_shape(self) -> None:
        response = self.client.post(self.url, {"email": "not-an-email"})
        self.assertEqual(response.status_code, 400)
        self.assertIn("email", response.json())

    def test_honeypot_filled_returns_202_and_creates_no_row(self) -> None:
        response = self.client.post(
            self.url, {"email": "bot@example.com", "website": "http://spam.example"}
        )
        self.assertEqual(response.status_code, 202)
        self.assertFalse(BetaSignup.objects.exists())

    def test_honeypot_trip_is_logged_without_the_raw_email(self) -> None:
        with self.assertLogs("fragrance.views", level="INFO") as captured:
            self.client.post(
                self.url, {"email": "bot@example.com", "website": "http://spam.example"}
            )
        self.assertTrue(any("honeypot" in message for message in captured.output))
        self.assertFalse(any("bot@example.com" in message for message in captured.output))

    def test_gmail_dot_and_plus_variant_collides_on_normalized_email(self) -> None:
        first = self.client.post(self.url, {"email": "ryan@gmail.com"})
        second = self.client.post(self.url, {"email": "r.yan+1@gmail.com"})

        self.assertEqual(first.status_code, 202)
        self.assertEqual(second.status_code, 202)
        self.assertEqual(
            BetaSignup.objects.filter(normalized_email="ryan@gmail.com").count(), 1
        )
        self.assertEqual(BetaSignup.objects.count(), 1)

    def test_signup_ip_hash_is_stored_and_is_not_the_raw_ip(self) -> None:
        self.client.post(
            self.url, {"email": "traceable@example.com"}, REMOTE_ADDR="203.0.113.5"
        )
        signup = BetaSignup.objects.get(normalized_email="traceable@example.com")
        self.assertNotEqual(signup.signup_ip_hash, "203.0.113.5")
        self.assertTrue(signup.signup_ip_hash)

    def test_unauthenticated_access_is_allowed(self) -> None:
        # No Authorization header on any request above, and none of them
        # 401'd -- this is a positive assertion that AllowAny is engaged.
        response = self.client.post(self.url, {"email": "anon@example.com"})
        self.assertNotEqual(response.status_code, 401)

    def test_sixth_request_in_a_minute_from_the_same_ip_is_throttled(self) -> None:
        # DEFAULT_THROTTLE_RATES["beta-signup"] = "5/min".
        for i in range(5):
            response = self.client.post(
                self.url, {"email": f"burst{i}@example.com"}, REMOTE_ADDR="198.51.100.7"
            )
            self.assertEqual(response.status_code, 202)

        blocked = self.client.post(
            self.url, {"email": "burst-over@example.com"}, REMOTE_ADDR="198.51.100.7"
        )
        self.assertEqual(blocked.status_code, 429)

    def test_throttle_key_is_not_defeated_by_a_forged_x_forwarded_for_header(self) -> None:
        # Regression test: the stock ScopedRateThrottle.get_ident trusts the
        # *entire* client-supplied X-Forwarded-For string when NUM_PROXIES is
        # unset, so a client that varies XFF on every request gets a fresh
        # bucket every time and the limit never engages. BetaSignupThrottle
        # must key on REMOTE_ADDR (via get_client_ip) instead, in dev where
        # there's no trusted proxy in front to have set XFF legitimately.
        for i in range(5):
            response = self.client.post(
                self.url,
                {"email": f"spoof{i}@example.com"},
                REMOTE_ADDR="198.51.100.9",
                HTTP_X_FORWARDED_FOR=f"10.0.0.{i}",
            )
            self.assertEqual(response.status_code, 202)

        blocked = self.client.post(
            self.url,
            {"email": "spoof-over@example.com"},
            REMOTE_ADDR="198.51.100.9",
            HTTP_X_FORWARDED_FOR="10.0.0.99",
        )
        self.assertEqual(blocked.status_code, 429)

    @override_settings(DEBUG=False)
    def test_prod_mode_throttles_on_x_real_ip_set_by_nginx(self) -> None:
        # With DEBUG=False, get_client_ip trusts X-Real-IP (what nginx's
        # realip-restored $remote_addr becomes upstream), not REMOTE_ADDR
        # (which would be nginx's own container address, since backend has
        # no published port in prod) and not X-Forwarded-For (client-visible
        # and only appended-to, so spoofable). Varying REMOTE_ADDR here
        # while holding X-Real-IP fixed simulates every request arriving
        # from the same one-hop nginx proxy.
        for i in range(5):
            response = self.client.post(
                self.url,
                {"email": f"prodburst{i}@example.com"},
                REMOTE_ADDR=f"172.18.0.{i}",
                HTTP_X_REAL_IP="203.0.113.42",
            )
            self.assertEqual(response.status_code, 202)

        blocked = self.client.post(
            self.url,
            {"email": "prodburst-over@example.com"},
            REMOTE_ADDR="172.18.0.99",
            HTTP_X_REAL_IP="203.0.113.42",
        )
        self.assertEqual(blocked.status_code, 429)

    def test_options_returns_405_and_does_not_leak_honeypot_field_name(self) -> None:
        # DEFAULT_METADATA_CLASS = None (settings.py) makes APIView.options()
        # return 405 instead of DRF's stock SimpleMetadata response, which
        # otherwise discloses the `website` honeypot field name (and this
        # view's docstring) to any anonymous OPTIONS probe -- defeating the
        # honeypot for a bot that checks OPTIONS before POST.
        response = self.client.options(self.url)
        self.assertEqual(response.status_code, 405)
        self.assertNotIn(b"website", response.content)

    def test_browser_style_get_does_not_leak_honeypot_field_name(self) -> None:
        # DEFAULT_METADATA_CLASS = None (above) only closes the OPTIONS path.
        # BrowsableAPIRenderer is a second, independent mechanism that renders
        # an HTML form -- including the `website` honeypot field name and this
        # view's docstring -- for any request whose Accept header prefers
        # text/html, which is what a browser sends by default when a URL is
        # opened directly. DEFAULT_RENDERER_CLASSES restricts rendering to
        # JSON only, so this must return no HTML form and no `website` string
        # anywhere in the body.
        response = self.client.get(
            self.url,
            HTTP_ACCEPT="text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        )
        self.assertEqual(response.status_code, 405)
        self.assertNotIn(b"<form", response.content)
        self.assertNotIn(b"website", response.content)

    def test_throttle_fails_open_when_cache_is_unavailable(self) -> None:
        # Django's built-in RedisCache (settings.CACHES) doesn't swallow
        # connection errors the way django-redis can with IGNORE_EXCEPTIONS.
        # BetaSignupThrottle.allow_request must catch that and allow the
        # request rather than 500ing this public, unauthenticated page.
        with patch.object(BetaSignupThrottle, "cache") as mock_cache:
            mock_cache.get.side_effect = redis.exceptions.ConnectionError("redis is down")
            response = self.client.post(self.url, {"email": "failopen@example.com"})

        self.assertEqual(response.status_code, 202)
        self.assertTrue(
            BetaSignup.objects.filter(normalized_email="failopen@example.com").exists()
        )
