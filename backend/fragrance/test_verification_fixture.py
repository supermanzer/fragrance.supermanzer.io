import re

from django.test import SimpleTestCase

from fragrance.ai_providers.verification import DUMMY_SEARCH_RESULTS
from fragrance.llm import EXPECTED_CANDIDATE_COUNT


class VerificationFixtureTests(SimpleTestCase):
    def test_dummy_search_results_offer_enough_distinct_houses(self) -> None:
        """
        The candidates conformance check asks for exactly EXPECTED_CANDIDATE_COUNT
        picks, one per house, from DUMMY_SEARCH_RESULTS. A fixture with fewer
        houses fails any model that declines to invent fragrances.
        """
        houses = re.findall(r"^- .+? by (.+?): ", DUMMY_SEARCH_RESULTS, flags=re.MULTILINE)
        self.assertGreaterEqual(len(set(houses)), EXPECTED_CANDIDATE_COUNT)
        self.assertEqual(len(houses), len(set(houses)))
