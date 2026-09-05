"""
Conformance harness for a single (family, model_id) pair: issues all four real
pipeline schema shapes (profile, candidates, replacement, email-content-with-enum)
against it and validates each response structurally, including the enum
constraint on email-content — the one call where a wrong/hallucinated name
would silently corrupt a RecommendationRun if enum weren't actually
grammar-enforced. Does not require the pair to be the currently-active
AIModelConfig row.

The actual check-running and result-persisting logic lives in
fragrance.ai_providers.verification — this command is a thin CLI wrapper over
it, shared with the AIModelConfig admin change-page "Run conformance check
now" button.
"""

import sys

from django.core.management.base import BaseCommand, CommandError

from fragrance.ai_providers import registry
from fragrance.ai_providers.verification import (
    persist_verification_results,
    run_verification_checks,
)
from fragrance.models import AI_PROVIDER_FAMILIES, AIModelConfig


class Command(BaseCommand):
    help = (
        "Conformance harness: issues all four pipeline schema shapes against a "
        "(family, model_id) pair and validates each response, including the "
        "enum constraint on email-content. Makes real API calls."
    )

    def add_arguments(self, parser: object) -> None:
        parser.add_argument(
            "family",
            choices=[family for family, _ in AI_PROVIDER_FAMILIES],
            help="AI provider family, e.g. anthropic, openai, qwen",
        )
        parser.add_argument("model_id", help="Provider-specific model identifier")
        parser.add_argument(
            "--yes",
            action="store_true",
            dest="assume_yes",
            help="On full pass, persist verified_at/capability flags on a matching AIModelConfig row without prompting",
        )

    def handle(self, *args: object, **options: object) -> None:
        family = options["family"]
        model_id = options["model_id"]
        assume_yes = options["assume_yes"]

        try:
            provider = registry.build_provider(
                config=AIModelConfig(family=family, model_id=model_id)
            )
        except registry.MissingCredentialError as exc:
            raise CommandError(str(exc))
        except registry.UnknownProviderFamilyError as exc:
            raise CommandError(str(exc))

        self.stdout.write(f"Verifying {family}:{model_id} ...\n")

        results = run_verification_checks(provider=provider)

        for result in results:
            status_word = self.style.SUCCESS("PASS") if result.passed else self.style.ERROR("FAIL")
            self.stdout.write(f"  [{status_word}] {result.label}: {result.detail}")

        pass_count = sum(1 for result in results if result.passed)
        total = len(results)
        self.stdout.write(f"\n{pass_count}/{total} schema checks passed.")

        if any(result.inconclusive for result in results):
            # At least one check never got a real answer (connectivity,
            # auth, or rate-limit failure) — this is a safety no-op, not a
            # partial pass: persist_verification_results leaves verified_at
            # and both capability flags exactly as they were, since a
            # transport failure carries zero information about the model's
            # actual capability.
            inconclusive_labels = ", ".join(
                result.label for result in results if result.inconclusive
            )
            persist_verification_results(
                family=family, model_id=model_id, results=results
            )
            self.stdout.write(
                self.style.WARNING(
                    "Could not complete verification: connectivity, authentication, "
                    f"or rate-limit error on check(s) [{inconclusive_labels}]. No "
                    "trust flags were changed."
                )
            )
            sys.exit(1)

        if pass_count != total:
            # Partial pass, every check reached the provider: reset any
            # stale capability flags on a matching row (never touching
            # verified_at) unconditionally — this is a safety reset, not an
            # operator decision, so it isn't gated behind --yes/the
            # confirmation prompt below.
            persist_verification_results(
                family=family, model_id=model_id, results=results
            )
            sys.exit(1)

        config = AIModelConfig.objects.filter(family=family, model_id=model_id).first()
        if config is None:
            self.stdout.write(
                "No matching AIModelConfig row exists for this (family, model_id) — "
                "create one via /admin/ if you want to activate it."
            )
            return

        if not assume_yes:
            answer = input(
                f"Mark AIModelConfig {config} as verified now? [y/N]: "
            ).strip().lower()
            if answer not in ("y", "yes"):
                self.stdout.write("Skipped — verified_at left unchanged.")
                return

        outcome = persist_verification_results(
            family=family, model_id=model_id, results=results
        )
        self.stdout.write(self.style.SUCCESS(f"Marked {outcome.config} as verified."))
