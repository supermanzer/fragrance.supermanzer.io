import io
from unittest.mock import patch

from django.contrib.auth.models import User
from django.db import DatabaseError
from django.db.models.query import QuerySet
from django.test import TestCase
from fragrance.models import Fragrance
from fragrance.services import import_collection_from_csv

_CSV = "Fragrance,House,Status,Notes\nOne,A,own,x\nTwo,B,like,y\nThree,C,dislike,z\n"


class ImportCollectionFromCsvTests(TestCase):
    def setUp(self) -> None:
        self.user = User.objects.create_user(username="hal", password="pw12345!")

    def test_a_clean_file_creates_every_row(self) -> None:
        result = import_collection_from_csv(user=self.user, file_obj=io.StringIO(_CSV))

        self.assertEqual(result, {"created": 3, "updated": 0, "skipped": 0})
        self.assertEqual(Fragrance.objects.filter(user=self.user).count(), 3)

    def test_a_mid_file_database_failure_rolls_back_every_row_of_the_import(self) -> None:
        real_update_or_create = QuerySet.update_or_create
        calls: list[str] = []

        def fail_on_third_row(self_qs: QuerySet, **kwargs: object) -> tuple[Fragrance, bool]:
            calls.append(str(kwargs["name"]))
            if len(calls) == 3:
                raise DatabaseError("write failed")
            return real_update_or_create(self_qs, **kwargs)

        with patch.object(QuerySet, "update_or_create", autospec=True, side_effect=fail_on_third_row):
            with self.assertRaises(DatabaseError):
                import_collection_from_csv(user=self.user, file_obj=io.StringIO(_CSV))

        self.assertEqual(calls, ["One", "Two", "Three"])
        self.assertEqual(Fragrance.objects.filter(user=self.user).count(), 0)
