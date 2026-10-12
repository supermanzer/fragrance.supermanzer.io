"""
Tests for deterministic recommendation dedup: slug rules, the slug columns and
Fragrance identity constraint, the backfill migration, rating/CSV identity,
and pipeline enforcement. Lives in its own module (not tests.py) so it is
independent of how the rest of the suite is laid out.
"""

import importlib
from io import StringIO
from unittest.mock import patch

from django.contrib.auth.models import User
from django.core.management import call_command
from django.db import IntegrityError, transaction
from django.test import TestCase
from rest_framework.test import APIClient

from .llm import (
    MAX_DEDUP_REASKS,
    CandidateCountError,
    select_candidates,
    select_replacement,
)
from .models import (
    Fragrance,
    PreferenceProfile,
    Recommendation,
    RecommendationRun,
)
from .search import verify_candidates
from .services import (
    RatingConflict,
    apply_recommendation_rating,
    import_collection_from_csv,
)
from .slugs import (
    combined_slug,
    core_name,
    name_in_text,
    same_fragrance,
    slugify_house,
    slugify_name,
)

_backfill_migration = importlib.import_module(
    "fragrance.migrations.0009_backfill_identity_slugs"
)


class _SequenceProvider:
    """Returns responses[i] on the i-th call (last one repeats); records prompts."""

    def __init__(self, responses: list[dict]) -> None:
        self.responses = responses
        self.prompts: list[str] = []

    def generate_structured(
        self, *, prompt: str, tool_name: str, description: str, schema: dict, max_tokens: int
    ) -> dict:
        index = min(len(self.prompts), len(self.responses) - 1)
        self.prompts.append(prompt)
        return self.responses[index]


def _batch(*pairs: tuple[str, str]) -> dict:
    return {"candidates": [{"name": n, "house": h} for n, h in pairs]}


_FILLER = [(f"Filler {i}", f"House {i}") for i in range(5)]


class SlugFunctionTests(TestCase):
    def test_slugify_name_table(self) -> None:
        cases = {
            "Ombré Leather": "ombre-leather",
            "Wood Sage & Sea Salt": "wood-sage-and-sea-salt",
            "L'Homme Idéal": "lhomme-ideal",
            "L’Homme Idéal": "lhomme-ideal",
            "  Bleu   de Chanel ": "bleu-de-chanel",
            "No. 5": "no-5",
            "Eau de Parfum Intense": "eau-de-parfum-intense",
            "Hanae Mori Parfum": "hanae-mori-parfum",
        }
        for raw, expected in cases.items():
            with self.subTest(raw=raw):
                self.assertEqual(slugify_name(raw), expected)

    def test_core_name_strips_only_concentration_words(self) -> None:
        cases = {
            "Ombre Leather Parfum": "ombre-leather",
            "Ombré Leather": "ombre-leather",
            "Aventus Eau de Parfum": "aventus",
            "Aventus EDT": "aventus",
            "Terre d'Hermès Eau de Toilette": "terre-dhermes",
            "Black Orchid Extrait de Parfum": "black-orchid",
            "Sauvage Parfum": "sauvage",
            "Sauvage Elixir": "sauvage-elixir",
            "Cologne Intense": "cologne-intense",
            "Noir Extreme": "noir-extreme",
            "Eau de Parfum": "eau-de-parfum",
        }
        for raw, expected in cases.items():
            with self.subTest(raw=raw):
                self.assertEqual(core_name(raw), expected)

    def test_slugify_house_unknowns_are_blank(self) -> None:
        for raw in ["", "   ", "<UNKNOWN>", "unknown", "UNKNOWN", "<unknown>"]:
            with self.subTest(raw=raw):
                self.assertEqual(slugify_house(raw), "")
        self.assertEqual(slugify_house("Maison Francis Kurkdjian"), "maison-francis-kurkdjian")
        self.assertEqual(slugify_house("Hermès"), "hermes")

    def test_combined_slug(self) -> None:
        self.assertEqual(combined_slug("Leather Oud", "Dior"), "leather-oud-dior")
        self.assertEqual(combined_slug("Leather Oud", "<UNKNOWN>"), "leather-oud")

    def test_same_fragrance_table(self) -> None:
        cases = [
            ("Ombré Leather", "Tom Ford", "Ombre Leather Parfum", "Tom Ford", True),
            ("Wood Sage & Sea Salt", "Jo Malone", "Wood Sage and Sea Salt", "Jo Malone London", True),
            ("Leather Oud", "Dior", "Leather Oud", "Christian Dior", True),
            ("Leather Oud", "Dior", "Leather Oud", "<UNKNOWN>", True),
            ("Leather Oud", "", "Leather Oud", "Dior", True),
            ("Vetiver", "Guerlain", "Vetiver", "Tom Ford", False),
            ("Vetiver", "Guerlain", "Vetiver", "unknown", True),
            ("Vetiver", "Guerlain", "Vetiver Extraordinaire", "Tom Ford", False),
            ("Sauvage", "Dior", "Sauvage Elixir", "Dior", False),
            ("Aventus", "Creed", "Aventus Cologne", "Creed", False),
        ]
        for name_a, house_a, name_b, house_b, expected in cases:
            with self.subTest(a=(name_a, house_a), b=(name_b, house_b)):
                self.assertEqual(
                    same_fragrance(name_a, house_a, name_b, house_b), expected
                )

    def test_name_in_text_normalizes_accents_and_concentration(self) -> None:
        self.assertTrue(name_in_text("Ombré Leather", "Tom Ford Ombre Leather Parfum - Fragrantica"))
        self.assertTrue(name_in_text("Ombre Leather Parfum", "Ombré Leather Tom Ford"))
        self.assertFalse(name_in_text("Ombre Leather", "Tom Ford Noir Extreme"))
        self.assertFalse(name_in_text("!!!", "anything"))

    def test_name_in_text_short_core_names_do_not_overmatch(self) -> None:
        self.assertFalse(name_in_text("Y Eau de Parfum", "Tom Ford Noir Extreme"))
        self.assertFalse(name_in_text("Le Parfum", "Tom Ford Ombre Leather"))
        self.assertTrue(name_in_text("Y Eau de Parfum", "Yves Saint Laurent Y Eau de Parfum review"))
        self.assertTrue(name_in_text("Y Eau de Parfum", "YSL Y - Fragrantica"))


class IdentitySlugPersistenceTests(TestCase):
    def setUp(self) -> None:
        self.user = User.objects.create_user(username="dave", password="x")

    def test_save_sets_slugs_and_partial_save_refreshes_them(self) -> None:
        frag = Fragrance.objects.create(
            user=self.user, name="Ombré Leather", house="Tom Ford", status="own"
        )
        self.assertEqual((frag.name_slug, frag.house_slug), ("ombre-leather", "tom-ford"))

        frag.house = "<UNKNOWN>"
        frag.save(update_fields=["house"])
        frag.refresh_from_db()
        self.assertEqual(frag.house_slug, "")

    def test_recommendation_save_sets_slugs(self) -> None:
        run = RecommendationRun.objects.create(user=self.user, status="done")
        rec = Recommendation.objects.create(
            user=self.user, run=run, name="Leather Oud", house="Dior", rationale=""
        )
        self.assertEqual((rec.name_slug, rec.house_slug), ("leather-oud", "dior"))


class FragranceIdentityConstraintTests(TestCase):
    def setUp(self) -> None:
        self.user = User.objects.create_user(username="dave", password="x")
        self.other = User.objects.create_user(username="frank", password="x")

    def _make(self, name: str, house: str, user: User | None = None) -> Fragrance:
        return Fragrance.objects.create(
            user=user or self.user, name=name, house=house, status="own"
        )

    def test_same_name_different_house_allowed(self) -> None:
        self._make("Vetiver", "Guerlain")
        self._make("Vetiver", "Tom Ford")
        self.assertEqual(Fragrance.objects.filter(user=self.user).count(), 2)

    def test_same_name_same_house_rejected(self) -> None:
        self._make("Vetiver", "Guerlain")
        with self.assertRaises(IntegrityError), transaction.atomic():
            self._make("Vetiver", "Guerlain")

    def test_case_and_accent_variants_rejected(self) -> None:
        self._make("Ombré Leather", "Tom Ford")
        with self.assertRaises(IntegrityError), transaction.atomic():
            self._make("OMBRE LEATHER", "tom ford")

    def test_edt_and_edp_of_one_scent_coexist(self) -> None:
        self._make("Aventus Eau de Parfum", "Creed")
        self._make("Aventus Eau de Toilette", "Creed")
        self.assertEqual(Fragrance.objects.filter(user=self.user).count(), 2)

    def test_other_user_may_own_same_fragrance(self) -> None:
        self._make("Vetiver", "Guerlain")
        self._make("Vetiver", "Guerlain", user=self.other)
        self.assertEqual(Fragrance.objects.filter(name="Vetiver").count(), 2)

    def test_api_same_house_duplicate_post_still_surfaces_integrity_error(self) -> None:
        client = APIClient()
        client.force_authenticate(user=self.user)
        body = {"name": "Vetiver", "house": "Guerlain", "status": "own", "notes": ""}
        self.assertEqual(client.post("/api/v1/collection/", body, format="json").status_code, 201)
        other_house = {**body, "house": "Tom Ford"}
        self.assertEqual(client.post("/api/v1/collection/", other_house, format="json").status_code, 201)
        # Unchanged from the old (user, name) constraint: DRF does not see the
        # model constraint (user is not a serializer field), so a true
        # duplicate is an unhandled IntegrityError, not a 400.
        with self.assertRaises(IntegrityError), transaction.atomic():
            client.post("/api/v1/collection/", body, format="json")
        self.assertEqual(Fragrance.objects.filter(user=self.user).count(), 2)


class BackfillMigrationTests(TestCase):
    def setUp(self) -> None:
        self.user = User.objects.create_user(username="dave", password="x")
        self.other = User.objects.create_user(username="frank", password="x")

    def _legacy(self, name: str, house: str, user: User | None = None) -> Fragrance:
        frag = Fragrance.objects.create(
            user=user or self.user, name=f"tmp-{name}-{house}-{id(self)}", house=house, status="own"
        )
        Fragrance.objects.filter(pk=frag.pk).update(
            name=name, name_slug=f"legacy-{frag.pk}", house_slug=""
        )
        return frag

    def test_backfills_both_models_and_is_idempotent(self) -> None:
        frag = self._legacy("Ombré Leather", "Tom Ford")
        run = RecommendationRun.objects.create(user=self.user, status="done")
        rec = Recommendation.objects.create(
            user=self.user, run=run, name="Leather Oud", house="<UNKNOWN>", rationale=""
        )
        Recommendation.objects.filter(pk=rec.pk).update(name_slug="legacy", house_slug="legacy")

        from django.apps import apps

        _backfill_migration.backfill_identity_slugs(apps, None)
        _backfill_migration.backfill_identity_slugs(apps, None)

        frag.refresh_from_db()
        rec.refresh_from_db()
        self.assertEqual((frag.name_slug, frag.house_slug), ("ombre-leather", "tom-ford"))
        self.assertEqual((rec.name_slug, rec.house_slug), ("leather-oud", ""))

    def test_colliding_rows_are_disambiguated_deterministically(self) -> None:
        first = self._legacy("Ombre Leather", "Tom Ford")
        second = self._legacy("Ombré Leather", "tom ford")
        third = self._legacy("OMBRE LEATHER", "Tom Ford")
        elsewhere = self._legacy("Ombre Leather", "Tom Ford", user=self.other)

        from django.apps import apps

        with self.assertLogs(_backfill_migration.logger, level="WARNING") as logs:
            _backfill_migration.backfill_identity_slugs(apps, None)
        for row in (first, second, third, elsewhere):
            row.refresh_from_db()

        self.assertEqual(first.name_slug, "ombre-leather")
        self.assertEqual(second.name_slug, f"ombre-leather-dup{second.id}")
        self.assertEqual(third.name_slug, f"ombre-leather-dup{third.id}")
        self.assertEqual(elsewhere.name_slug, "ombre-leather")
        self.assertTrue(any(f"id={second.id}" in line for line in logs.output))
        keys = Fragrance.objects.values_list("user_id", "name_slug", "house_slug")
        self.assertEqual(len(set(keys)), 4)


    def test_disambiguated_row_stays_editable_through_the_api(self) -> None:
        from django.apps import apps

        first = self._legacy("Ombre Leather", "Tom Ford")
        second = self._legacy("Ombré Leather", "Tom Ford")
        _backfill_migration.backfill_identity_slugs(apps, None)
        second.refresh_from_db()
        self.assertEqual(second.name_slug, f"ombre-leather-dup{second.id}")

        client = APIClient()
        client.force_authenticate(user=self.user)
        response = client.patch(
            f"/api/v1/collection/{second.id}/", {"notes": "still mine"}, format="json"
        )
        self.assertEqual(response.status_code, 200)
        second.refresh_from_db()
        self.assertEqual(second.notes, "still mine")
        self.assertEqual(second.name_slug, f"ombre-leather-dup{second.id}")
        first.refresh_from_db()
        self.assertEqual(first.name_slug, "ombre-leather")


class RatingIdentityTests(TestCase):
    def setUp(self) -> None:
        self.user = User.objects.create_user(username="dave", password="x")
        self.run = RecommendationRun.objects.create(user=self.user, status="done")

    def _rec(self, name: str, house: str) -> Recommendation:
        return Recommendation.objects.create(
            user=self.user, run=self.run, name=name, house=house, rationale=""
        )

    def test_same_name_different_house_gets_its_own_fragrance_row(self) -> None:
        Fragrance.objects.create(user=self.user, name="Vetiver", house="Guerlain", status="own")
        rec = self._rec("Vetiver", "Tom Ford")

        fragrance, is_new = apply_recommendation_rating(recommendation=rec, action="like")

        self.assertTrue(is_new)
        self.assertEqual(fragrance.house, "Tom Ford")
        self.assertEqual(fragrance.status, "like")
        self.assertEqual(Fragrance.objects.filter(user=self.user, name="Vetiver").count(), 2)
        self.assertEqual(
            Fragrance.objects.get(user=self.user, house="Guerlain").status, "own"
        )

    def test_same_identity_unlinked_row_is_claimed_not_duplicated(self) -> None:
        existing = Fragrance.objects.create(
            user=self.user, name="Ombré Leather", house="Tom Ford", status="own"
        )
        rec = self._rec("Ombre Leather", "Tom Ford")

        fragrance, is_new = apply_recommendation_rating(recommendation=rec, action="like")

        self.assertTrue(is_new)
        self.assertEqual(fragrance.pk, existing.pk)
        self.assertEqual(fragrance.source_recommendation_id, rec.id)

    def test_row_claimed_by_other_recommendation_still_conflicts(self) -> None:
        first = self._rec("Vetiver", "Guerlain")
        second = self._rec("Vetiver", "Guerlain")
        apply_recommendation_rating(recommendation=first, action="own")
        with self.assertRaises(RatingConflict):
            apply_recommendation_rating(recommendation=second, action="own")

    def test_two_same_named_picks_from_different_houses_rate_independently(self) -> None:
        guerlain = self._rec("Vetiver", "Guerlain")
        tom_ford = self._rec("Vetiver", "Tom Ford")
        apply_recommendation_rating(recommendation=guerlain, action="own")
        apply_recommendation_rating(recommendation=tom_ford, action="dislike")
        self.assertEqual(Fragrance.objects.filter(user=self.user).count(), 2)


class CsvImportIdentityTests(TestCase):
    def setUp(self) -> None:
        self.user = User.objects.create_user(username="dave", password="x")

    def _import(self, body: str) -> dict:
        return import_collection_from_csv(user=self.user, file_obj=StringIO(body))

    def test_same_name_different_house_rows_both_created(self) -> None:
        result = self._import(
            "fragrance,status,house,notes\nVetiver,own,Guerlain,\nVetiver,like,Tom Ford,\n"
        )
        self.assertEqual(result, {"created": 2, "updated": 0, "skipped": 0})

    def test_reimport_updates_rather_than_duplicates(self) -> None:
        self._import("fragrance,status,house,notes\nOmbré Leather,own,Tom Ford,\n")
        result = self._import("fragrance,status,house,notes\nOmbre Leather,like,Tom Ford,hi\n")
        self.assertEqual(result, {"created": 0, "updated": 1, "skipped": 0})
        frag = Fragrance.objects.get(user=self.user)
        self.assertEqual((frag.status, frag.notes), ("like", "hi"))

    def test_reimport_with_corrected_house_updates_unknown_house_row(self) -> None:
        self._import("fragrance,status,house,notes\nVetiver,own,,\n")
        result = self._import("fragrance,status,house,notes\nVetiver,own,Guerlain,\n")
        self.assertEqual(result["updated"], 1)
        self.assertEqual(Fragrance.objects.get(user=self.user).house, "Guerlain")


def _profile(user: User) -> PreferenceProfile:
    return PreferenceProfile.objects.create(
        user=user,
        loved_notes="oud",
        liked_notes="citrus",
        disliked_notes="aquatic",
        owns_list=["LLM says it owns nothing relevant"],
        search_angle_1="x",
        search_angle_2="y",
    )


class SelectCandidatesDedupTests(TestCase):
    def setUp(self) -> None:
        self.user = User.objects.create_user(username="dave", password="x")
        self.other = User.objects.create_user(username="frank", password="x")
        self.profile = _profile(self.user)
        run = RecommendationRun.objects.create(user=self.user, status="done")
        Recommendation.objects.create(
            user=self.user, run=run, name="Leather Oud", house="Dior", status="confirmed", rationale=""
        )
        Recommendation.objects.create(
            user=self.user, run=run, name="Wood Sage & Sea Salt", house="Jo Malone",
            status="replaced", rationale="",
        )
        Fragrance.objects.create(user=self.user, name="Ombré Leather", house="Tom Ford", status="own")
        Fragrance.objects.create(user=self.user, name="Bad One", house="Nowhere", status="dislike")
        # Another user's history must never influence this user's exclusions.
        Fragrance.objects.create(user=self.other, name="Filler 0", house="House 0", status="own")

    def _run(self, provider: _SequenceProvider) -> list[dict]:
        return select_candidates(
            user_id=self.user.id,
            profile_id=self.profile.id,
            search_results="",
            provider=provider,
        )

    def test_clean_answer_passes_without_reask(self) -> None:
        provider = _SequenceProvider([_batch(*_FILLER)])
        self.assertEqual(len(self._run(provider)), 5)
        self.assertEqual(len(provider.prompts), 1)

    def test_prompt_lists_name_by_house_from_db_not_llm(self) -> None:
        provider = _SequenceProvider([_batch(*_FILLER)])
        self._run(provider)
        prompt = provider.prompts[0]
        self.assertIn("Leather Oud by Dior", prompt)
        self.assertIn("Wood Sage & Sea Salt by Jo Malone", prompt)
        self.assertIn("Ombré Leather by Tom Ford", prompt)
        self.assertIn("Bad One by Nowhere", prompt)
        self.assertNotIn("LLM says it owns", prompt)

    def test_repeat_is_rejected_then_reasked_then_succeeds(self) -> None:
        provider = _SequenceProvider(
            [
                _batch(("Leather Oud", "<UNKNOWN>"), *_FILLER[:4]),
                _batch(*_FILLER),
            ]
        )
        with self.assertLogs("fragrance.dedup", level="WARNING") as logs:
            result = self._run(provider)
        self.assertEqual(len(provider.prompts), 2)
        self.assertNotIn("Leather Oud", [c["name"] for c in result])
        self.assertIn("Leather Oud", provider.prompts[1])
        self.assertIn("previously_recommended", provider.prompts[1])
        joined = "\n".join(logs.output)
        self.assertIn("reason=previously_recommended", joined)
        self.assertIn("unknown_house_wildcard=True", joined)
        self.assertIn(f"user_id={self.user.id}", joined)
        self.assertNotIn("@", joined)

    def test_owned_variant_with_accent_and_concentration_is_rejected(self) -> None:
        provider = _SequenceProvider(
            [
                _batch(("Ombre Leather Parfum", "Tom Ford"), *_FILLER[:4]),
                _batch(*_FILLER),
            ]
        )
        with self.assertLogs("fragrance.dedup", level="WARNING") as logs:
            self._run(provider)
        self.assertIn("reason=owned", "\n".join(logs.output))

    def test_disliked_collection_item_is_rejected(self) -> None:
        provider = _SequenceProvider([_batch(("Bad One", "Nowhere"), *_FILLER[:4]), _batch(*_FILLER)])
        with self.assertLogs("fragrance.dedup", level="WARNING") as logs:
            self._run(provider)
        self.assertIn("reason=in_collection", "\n".join(logs.output))

    def test_replaced_pick_is_still_excluded(self) -> None:
        provider = _SequenceProvider(
            [_batch(("Wood Sage and Sea Salt", "Jo Malone London"), *_FILLER[:4]), _batch(*_FILLER)]
        )
        self._run(provider)
        self.assertEqual(len(provider.prompts), 2)

    def test_duplicate_within_batch_is_rejected(self) -> None:
        provider = _SequenceProvider(
            [
                _batch(("Vetiver", "Guerlain"), ("Vetiver", "Tom Ford"), *_FILLER[:3]),
                _batch(*_FILLER),
            ]
        )
        with self.assertLogs("fragrance.dedup", level="WARNING") as logs:
            self._run(provider)
        self.assertIn("reason=duplicate_in_batch", "\n".join(logs.output))

    def test_same_name_different_house_as_history_is_allowed(self) -> None:
        # Leather Oud by a clearly different house is a different fragrance.
        provider = _SequenceProvider([_batch(("Leather Oud", "Guerlain"), *_FILLER[:4])])
        self.assertEqual(len(self._run(provider)), 5)
        self.assertEqual(len(provider.prompts), 1)

    def test_always_repeating_raises_candidate_count_error(self) -> None:
        provider = _SequenceProvider([_batch(("Leather Oud", "Dior"), *_FILLER[:4])])
        with self.assertRaises(CandidateCountError):
            self._run(provider)
        self.assertEqual(len(provider.prompts), 1 + MAX_DEDUP_REASKS)

    def test_wrong_count_still_raises_without_reask(self) -> None:
        provider = _SequenceProvider([_batch(*_FILLER[:4])])
        with self.assertRaises(CandidateCountError):
            self._run(provider)
        self.assertEqual(len(provider.prompts), 1)


class SelectReplacementDedupTests(TestCase):
    def setUp(self) -> None:
        self.user = User.objects.create_user(username="dave", password="x")
        self.profile = _profile(self.user)
        run = RecommendationRun.objects.create(user=self.user, status="done")
        Recommendation.objects.create(
            user=self.user, run=run, name="Old Pick", house="Creed", status="confirmed", rationale=""
        )
        self.failed = {"name": "Failed", "house": "Nowhere"}
        self.batch = [self.failed, {"name": "Batch Mate", "house": "Hermès"}]

    def _run(self, provider: _SequenceProvider) -> dict:
        return select_replacement(
            user_id=self.user.id,
            failed_candidate=self.failed,
            search_results="",
            profile=self.profile,
            provider=provider,
            batch_candidates=self.batch,
        )

    def test_clean_replacement_returned(self) -> None:
        provider = _SequenceProvider([{"name": "Fresh", "house": "Amouage"}])
        self.assertEqual(self._run(provider)["name"], "Fresh")

    def test_repeat_of_history_rejected_then_reasked(self) -> None:
        provider = _SequenceProvider(
            [{"name": "Old Pick", "house": "Creed"}, {"name": "Fresh", "house": "Amouage"}]
        )
        self.assertEqual(self._run(provider)["name"], "Fresh")
        self.assertEqual(len(provider.prompts), 2)

    def test_same_name_as_batch_mate_rejected_even_with_other_house(self) -> None:
        provider = _SequenceProvider(
            [{"name": "Batch Mate", "house": "Other"}, {"name": "Fresh", "house": "Amouage"}]
        )
        self.assertEqual(self._run(provider)["name"], "Fresh")

    def test_always_repeating_raises(self) -> None:
        provider = _SequenceProvider([{"name": "Old Pick", "house": "<UNKNOWN>"}])
        with self.assertRaises(CandidateCountError):
            self._run(provider)
        self.assertEqual(len(provider.prompts), 1 + MAX_DEDUP_REASKS)


class VerifyCandidatesDedupTests(TestCase):
    def setUp(self) -> None:
        self.user = User.objects.create_user(username="dave", password="x")
        self.profile = _profile(self.user)
        self.run = RecommendationRun.objects.create(user=self.user, status="running")

    def test_same_named_candidates_from_different_houses_are_both_processed(self) -> None:
        candidates = [
            {"name": "Vetiver", "house": "Guerlain"},
            {"name": "Vetiver", "house": "Tom Ford"},
        ]
        with patch(
            "fragrance.search.searxng_search",
            side_effect=lambda query, num_results=5: [
                {"title": query, "url": "https://example.com/x", "content": "c"}
            ],
        ):
            picks = verify_candidates(
                user_id=self.user.id,
                run_id=self.run.id,
                candidates=candidates,
                profile=self.profile,
                provider=_SequenceProvider([{}]),
            )
        self.assertEqual([p["house"] for p in picks], ["Guerlain", "Tom Ford"])
        rows = Recommendation.objects.filter(run=self.run).order_by("id")
        self.assertEqual([r.house_slug for r in rows], ["guerlain", "tom-ford"])

    def test_replacement_that_repeats_history_is_rejected_and_reasked(self) -> None:
        old_run = RecommendationRun.objects.create(user=self.user, status="done")
        Recommendation.objects.create(
            user=self.user, run=old_run, name="Old Pick", house="Creed", rationale=""
        )
        provider = _SequenceProvider(
            [{"name": "Old Pick", "house": "Creed"}, {"name": "Fresh", "house": "Amouage"}]
        )
        with patch(
            "fragrance.search.searxng_search",
            return_value=[{"title": "unrelated", "url": "https://e.com", "content": "c"}],
        ):
            picks = verify_candidates(
                user_id=self.user.id,
                run_id=self.run.id,
                candidates=[{"name": "Unverifiable", "house": "Nowhere"}],
                profile=self.profile,
                provider=provider,
            )
        self.assertEqual([p["status"] for p in picks], ["replaced", "confirmed"])
        self.assertEqual(picks[1]["name"], "Fresh")

    def test_replacement_always_repeating_raises(self) -> None:
        provider = _SequenceProvider([{"name": "Unverifiable", "house": "Nowhere"}])
        with patch(
            "fragrance.search.searxng_search",
            return_value=[{"title": "unrelated", "url": "https://e.com", "content": "c"}],
        ):
            with self.assertRaises(CandidateCountError):
                verify_candidates(
                    user_id=self.user.id,
                    run_id=self.run.id,
                    candidates=[{"name": "Unverifiable", "house": "Nowhere"}],
                    profile=self.profile,
                    provider=provider,
                )

    def test_accent_and_concentration_title_match_confirms(self) -> None:
        with patch(
            "fragrance.search.searxng_search",
            return_value=[
                {"title": "Tom Ford Ombre Leather Parfum", "url": "https://e.com/ol", "content": "c"}
            ],
        ):
            picks = verify_candidates(
                user_id=self.user.id,
                run_id=self.run.id,
                candidates=[{"name": "Ombré Leather", "house": "Tom Ford"}],
                profile=self.profile,
                provider=_SequenceProvider([{}]),
            )
        self.assertEqual(picks[0]["status"], "confirmed")


class ReportRepeatRecommendationsCommandTests(TestCase):
    def test_reports_ids_and_slugs_only_and_writes_nothing(self) -> None:
        user = User.objects.create_user(username="dave", email="dave@example.com", password="x")
        run1 = RecommendationRun.objects.create(user=user, status="done")
        run2 = RecommendationRun.objects.create(user=user, status="done")
        a = Recommendation.objects.create(user=user, run=run1, name="Leather Oud", house="Dior", rationale="")
        b = Recommendation.objects.create(user=user, run=run2, name="Leather Oud", house="<UNKNOWN>", rationale="")
        Recommendation.objects.create(user=user, run=run1, name="Vetiver", house="Guerlain", rationale="")
        Recommendation.objects.create(user=user, run=run2, name="Vetiver", house="Tom Ford", rationale="")
        before = (Recommendation.objects.count(), Fragrance.objects.count())

        out = StringIO()
        call_command("report_repeat_recommendations", stdout=out)

        text = out.getvalue()
        self.assertIn(f"user_id={user.id} name_slug=leather-oud", text)
        self.assertIn(f"recommendation_ids=[{a.id}, {b.id}]", text)
        self.assertIn("repeat_groups=1", text)
        self.assertNotIn("vetiver", text)
        self.assertNotIn("dave", text)
        self.assertEqual(before, (Recommendation.objects.count(), Fragrance.objects.count()))

    def test_reports_predicted_fragrance_identity_collisions(self) -> None:
        user = User.objects.create_user(username="dave", password="x")
        a = Fragrance.objects.create(user=user, name="Ombre Leather", house="Tom Ford", status="own")
        b = Fragrance.objects.create(user=user, name="Other", house="Tom Ford", status="own")
        Fragrance.objects.filter(pk=b.pk).update(name="Ombré Leather", name_slug="legacy")
        out = StringIO()
        call_command("report_repeat_recommendations", stdout=out)
        self.assertIn(
            f"fragrance_identity_collision user_id={user.id} name_slug=ombre-leather "
            f"house_slug=tom-ford fragrance_ids=[{a.id}, {b.id}]",
            out.getvalue(),
        )
        self.assertIn("fragrance_identity_collisions=1", out.getvalue())

    def test_promoted_fragrance_is_not_counted_as_a_second_occurrence(self) -> None:
        user = User.objects.create_user(username="dave", password="x")
        run = RecommendationRun.objects.create(user=user, status="done")
        rec = Recommendation.objects.create(user=user, run=run, name="Aventus", house="Creed", rationale="")
        Fragrance.objects.create(
            user=user, name="Aventus", house="Creed", status="own", source_recommendation=rec
        )
        out = StringIO()
        call_command("report_repeat_recommendations", stdout=out)
        self.assertIn("repeat_groups=0", out.getvalue())
