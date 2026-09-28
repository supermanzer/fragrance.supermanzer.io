import importlib
import threading

from django.apps import apps as django_apps
from django.test import TestCase, TransactionTestCase
from fragrance.models import AIModelConfig


# Migration module filenames start with digits, which aren't valid dotted-
# import identifiers — importlib is how Django's own migration loader
# imports them too.
_seed_default_anthropic_config_migration = importlib.import_module(
    "fragrance.migrations.0006_seed_default_anthropic_config"
)


class AIModelConfigTests(TestCase):
    def setUp(self) -> None:
        # Migration 0006 seeds a default anthropic:claude-haiku-4-5-20251001
        # row into every fresh test database — these tests need a genuinely
        # empty table to assert their own preconditions, not the migration's.
        AIModelConfig.objects.all().delete()

    def test_activating_a_new_row_deactivates_the_previously_active_one(self) -> None:
        first = AIModelConfig.objects.create(
            family="anthropic",
            model_id="claude-haiku-4-5-20251001",
            is_active=True,
            supports_enum_enforcement=True,
        )
        second = AIModelConfig.objects.create(
            family="openai",
            model_id="gpt-4.1-mini",
            is_active=True,
            supports_enum_enforcement=True,
        )

        first.refresh_from_db()
        second.refresh_from_db()
        self.assertFalse(first.is_active)
        self.assertTrue(second.is_active)
        self.assertEqual(AIModelConfig.objects.filter(is_active=True).count(), 1)

    def test_saving_inactive_row_does_not_disturb_active_row(self) -> None:
        active = AIModelConfig.objects.create(
            family="anthropic", model_id="claude-haiku-4-5-20251001", is_active=True
        )
        inactive = AIModelConfig.objects.create(
            family="qwen", model_id="qwen-max", is_active=False
        )
        inactive.notes = "updated"
        inactive.save(update_fields=["notes"])

        active.refresh_from_db()
        self.assertTrue(active.is_active)
        self.assertEqual(AIModelConfig.objects.filter(is_active=True).count(), 1)


class AIModelConfigConcurrencyTests(TransactionTestCase):
    """
    Regression test for the exclusivity race traced in .claude/reports/
    2026-08-30-ai-provider-abstraction-review.md, finding 5: two transactions
    activating two different, already-existing rows near-simultaneously used
    to both end up committed with is_active=True, because a blocked
    transaction's cleanup UPDATE only re-checks rows it already matched at
    the start of its own statement — never rows that only become active
    after the other transaction commits. The partial unique index added in
    migration 0005, combined with save() now deactivating other rows before
    activating self, closes this: at most one row can ever be active, and
    the losing transaction gets a clear ActiveAIModelConfigConflictError
    instead of silently succeeding.

    Requires TransactionTestCase (real commits across threads, each with its
    own DB connection) — a TestCase's single wrapped, uncommitted transaction
    can't reproduce cross-transaction lock contention, which is the actual
    mechanism this bug (and its fix) depends on.
    """

    def test_two_existing_rows_activated_concurrently_leaves_exactly_one_active(
        self,
    ) -> None:
        row_a = AIModelConfig.objects.create(family="anthropic", model_id="model-a")
        row_b = AIModelConfig.objects.create(family="openai", model_id="model-b")
        AIModelConfig.objects.create(
            family="qwen", model_id="model-c", is_active=True
        )

        # None = save() succeeded; otherwise the exception class name.
        # Depending on how much the two transactions actually overlap (real
        # thread/DB-connection timing, not fully controllable from Python),
        # either both saves can succeed — if they end up fully serialized,
        # the second one's deactivate-step legitimately sees the first's
        # already-committed row and turns it off itself, no conflict — or
        # one can hit ActiveAIModelConfigConflictError. Both are correct
        # outcomes of the fix; what must never happen, in either case, is
        # ending up with two committed active rows or any *other* exception
        # type leaking out unsanitized.
        outcomes: list[tuple[str, str | None]] = []
        outcomes_lock = threading.Lock()
        barrier = threading.Barrier(2)

        def _activate(pk: int, label: str) -> None:
            from django.db import connection

            try:
                barrier.wait()
                row = AIModelConfig.objects.get(pk=pk)
                row.is_active = True
                try:
                    row.save(update_fields=["is_active"])
                    outcome = None
                except Exception as exc:  # noqa: BLE001 - test wants the type
                    outcome = type(exc).__name__
                with outcomes_lock:
                    outcomes.append((label, outcome))
            finally:
                # Each thread opens its own DB connection; only the main
                # thread's connection is auto-closed after a test.
                connection.close()

        threads = [
            threading.Thread(target=_activate, args=(row_a.pk, "a")),
            threading.Thread(target=_activate, args=(row_b.pk, "b")),
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        self.assertEqual(len(outcomes), 2)
        for _, outcome in outcomes:
            self.assertIn(outcome, (None, "ActiveAIModelConfigConflictError"))
        # The invariant that actually matters: never two committed active
        # rows, regardless of which interleaving occurred above.
        self.assertEqual(AIModelConfig.objects.filter(is_active=True).count(), 1)


class SeedDefaultAnthropicConfigMigrationTests(TestCase):
    """
    Exercises migration 0006's RunPython functions directly (calling them
    with the live app registry, not a full MigrationExecutor state replay)
    rather than actually migrating the test DB backward and forward. This
    project has no migration-testing package installed, and juggling real
    schema-history state via MigrationExecutor inside a TransactionTestCase
    (which flushes django_migrations along with every other table on
    teardown) is a known source of test-suite-ordering fragility for a
    migration that has no schema operations to begin with — 0006 is
    RunPython-only, so its actual behavior *is* "call this function against
    a real AIModelConfig table," which is what these tests do directly.
    """

    def setUp(self) -> None:
        # The test DB already carries this same migration's own seeded row
        # (applied once, at test-DB creation time, like every other
        # migration) — clear it so "fresh table" here means what it says,
        # not "whatever this migration already did during DB setup."
        AIModelConfig.objects.all().delete()

    def test_fresh_table_seeds_exactly_the_expected_row(self) -> None:
        self.assertEqual(AIModelConfig.objects.count(), 0)

        _seed_default_anthropic_config_migration.seed_default_anthropic_config(
            django_apps, None
        )

        rows = list(AIModelConfig.objects.all())
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(row.family, "anthropic")
        self.assertEqual(row.model_id, "claude-haiku-4-5-20251001")
        self.assertTrue(row.is_active)
        self.assertTrue(row.supports_strict_schema)
        self.assertTrue(row.supports_enum_enforcement)
        self.assertIsNone(row.verified_at)
        self.assertIn(
            _seed_default_anthropic_config_migration.SEED_NOTE_MARKER, row.notes
        )

    def test_already_populated_table_is_a_genuine_no_op(self) -> None:
        # Mirrors dev: a row already exists (here, standing in for the one
        # Task 5's manual verify_ai_model run created) before this migration
        # ever runs.
        existing = AIModelConfig.objects.create(
            family="openai", model_id="gpt-4.1-mini", is_active=True
        )

        _seed_default_anthropic_config_migration.seed_default_anthropic_config(
            django_apps, None
        )

        self.assertEqual(AIModelConfig.objects.count(), 1)
        existing.refresh_from_db()
        self.assertEqual(existing.family, "openai")
        self.assertTrue(existing.is_active)

    def test_reverse_deletes_only_the_row_it_seeded(self) -> None:
        _seed_default_anthropic_config_migration.seed_default_anthropic_config(
            django_apps, None
        )

        _seed_default_anthropic_config_migration.remove_seeded_default_anthropic_config(
            django_apps, None
        )

        self.assertEqual(AIModelConfig.objects.count(), 0)

    def test_reverse_does_not_delete_a_row_an_admin_has_since_edited(self) -> None:
        _seed_default_anthropic_config_migration.seed_default_anthropic_config(
            django_apps, None
        )
        seeded = AIModelConfig.objects.get()
        seeded.notes = "an admin edited this row after it was seeded"
        seeded.save(update_fields=["notes"])

        _seed_default_anthropic_config_migration.remove_seeded_default_anthropic_config(
            django_apps, None
        )

        self.assertEqual(AIModelConfig.objects.count(), 1)
