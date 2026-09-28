from django.test import TestCase
from django.urls import resolve
from django.urls.exceptions import Resolver404


class RegisterRouteRemovedTests(TestCase):
    def test_register_endpoint_no_longer_resolves(self) -> None:
        with self.assertRaises(Resolver404):
            resolve("/api/v1/auth/register/")

    def test_register_endpoint_404s_over_http(self) -> None:
        response = self.client.post(
            "/api/v1/auth/register/",
            {"username": "sneaky", "email": "sneaky@example.com", "password": "pw12345!"},
        )
        self.assertEqual(response.status_code, 404)
