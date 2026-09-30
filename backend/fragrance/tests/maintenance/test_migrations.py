import importlib

from django.apps import apps as real_apps
from django.contrib.auth.models import User
from django.db import IntegrityError, connection, models, transaction
from django.test import TestCase
from psycopg2 import errorcodes

from fragrance.models import FragranceConfig
from fragrance.services import get_or_create_fragrance_config

migration = importlib.import_module(
    "fragrance.migrations.0010_drop_orphaned_gmail_columns"
)


def _column_names_and_nullability() -> dict[str, bool]:
    with connection.cursor() as cursor:
        columns = connection.introspection.get_table_description(
            cursor=cursor, table_name=FragranceConfig._meta.db_table
        )
    return {column.name: column.null_ok for column in columns}


class DropOrphanedGmailColumnsMigrationTests(TestCase):
    """
    Exercises migration 0010's RunPython function directly against the real
    schema editor, the same style as 0009's GrantStaffMigrationTests and
    0006's SeedDefaultAnthropicConfigMigrationTests -- calling the function
    with the live app registry rather than replaying real migration history
    via MigrationExecutor. Field instances built here stand in for the
    columns as they existed pre-11332c6 (EmailField / TextField, in place of
    the since-removed EncryptedTextField) and are never attached to
    FragranceConfig via contribute_to_class -- doing so would permanently
    leak the field onto the live model class for the rest of the test run,
    since TestCase's transaction rollback undoes row data, not Python-level
    class mutation.
    """

    def _add_orphaned_columns(self) -> None:
        gmail_user = models.EmailField(max_length=254)
        gmail_user.set_attributes_from_name(name="gmail_user")
        gmail_password = models.TextField()
        gmail_password.set_attributes_from_name(name="gmail_app_password_enc")
        with connection.schema_editor() as editor:
            editor.add_field(model=FragranceConfig, field=gmail_user)
            editor.add_field(model=FragranceConfig, field=gmail_password)

    def test_columns_present_cause_integrity_error_before_the_migration_runs_and_recovers_after(
        self,
    ) -> None:
        """
        Proves the actual bug, not just the post-fix state: with the orphaned
        NOT NULL columns present and drop_orphaned_gmail_columns not yet run,
        FragranceConfig creation (via the ORM's get_or_create -- Django's
        migration state has never heard of these columns, so the generated
        INSERT omits them) raises IntegrityError, specifically a Postgres
        NOT NULL violation naming one of the two orphaned columns -- not
        merely "some IntegrityError", which an unrelated collision could also
        produce. The identical creation then succeeds once the migration
        function has run.
        """
        self._add_orphaned_columns()
        user = User.objects.create_user(username="drift-repair-proof", password="pw12345!")

        # The nested atomic() is the savepoint the expected IntegrityError
        # rolls back to -- without it, Postgres would mark the outer
        # TestCase-wrapped transaction as aborted and every later statement
        # in this test (including the migration's own DDL) would raise
        # "current transaction is aborted" instead of running.
        with transaction.atomic():
            with self.assertRaises(IntegrityError) as caught:
                get_or_create_fragrance_config(user=user)

        cause = caught.exception.__cause__
        self.assertIsNotNone(cause)
        self.assertEqual(getattr(cause, "pgcode", None), errorcodes.NOT_NULL_VIOLATION)
        self.assertIn(cause.diag.column_name, {"gmail_user", "gmail_app_password_enc"})
        self.assertFalse(FragranceConfig.objects.filter(user=user).exists())

        with connection.schema_editor() as editor:
            migration.drop_orphaned_gmail_columns(apps=real_apps, schema_editor=editor)

        config = get_or_create_fragrance_config(user=user)
        self.assertEqual(config.user_id, user.id)

    def test_columns_present_are_dropped_and_config_creation_recovers(self) -> None:
        self._add_orphaned_columns()
        before = _column_names_and_nullability()
        self.assertIn("gmail_user", before)
        self.assertIn("gmail_app_password_enc", before)
        self.assertFalse(before["gmail_user"])
        self.assertFalse(before["gmail_app_password_enc"])

        with connection.schema_editor() as editor:
            migration.drop_orphaned_gmail_columns(apps=real_apps, schema_editor=editor)

        after = _column_names_and_nullability()
        self.assertNotIn("gmail_user", after)
        self.assertNotIn("gmail_app_password_enc", after)

        user = User.objects.create_user(username="drift-repair", password="pw12345!")
        config = get_or_create_fragrance_config(user=user)
        self.assertEqual(config.user_id, user.id)

    def test_columns_absent_is_a_genuine_no_op(self) -> None:
        before = _column_names_and_nullability()
        self.assertNotIn("gmail_user", before)
        self.assertNotIn("gmail_app_password_enc", before)

        with connection.schema_editor(collect_sql=True) as editor:
            migration.drop_orphaned_gmail_columns(apps=real_apps, schema_editor=editor)
            collected_sql = list(editor.collected_sql)

        self.assertEqual(collected_sql, [])
        after = _column_names_and_nullability()
        self.assertEqual(before, after)

        user = User.objects.create_user(username="no-drift", password="pw12345!")
        config = get_or_create_fragrance_config(user=user)
        self.assertEqual(config.user_id, user.id)


class BackwardCompatibleColumnDefaultsTests(TestCase):
    """
    The migrate-first deploy runs the previous release's code against the new
    schema for a while. That code never names these NOT NULL columns, so the
    database itself must default them (db_default), or every insert fails.
    """

    EXPECTED = {
        "fragrance_recommendationrun": {"attempt": "1", "retry_state": "''"},
        "fragrance_subscription": {"payment_recovery_state": "''"},
    }

    def test_new_not_null_columns_on_existing_tables_have_a_database_default(self) -> None:
        with connection.cursor() as cursor:
            for table, columns in self.EXPECTED.items():
                described = {
                    column.name: column
                    for column in connection.introspection.get_table_description(cursor=cursor, table_name=table)
                }
                for name, expected_default in columns.items():
                    self.assertFalse(described[name].null_ok, msg=f"{table}.{name}")
                    self.assertIn(expected_default, str(described[name].default), msg=f"{table}.{name}")

    def test_the_model_fields_declare_the_same_db_defaults(self) -> None:
        from fragrance.models import RecommendationRun, Subscription

        self.assertEqual(RecommendationRun._meta.get_field("attempt").db_default, 1)
        self.assertEqual(RecommendationRun._meta.get_field("retry_state").db_default, "")
        self.assertEqual(Subscription._meta.get_field("payment_recovery_state").db_default, "")
