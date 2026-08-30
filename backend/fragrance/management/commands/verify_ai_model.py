"""
Conformance harness for a single (family, model_id) pair: issues all four real
pipeline schema shapes (profile, candidates, replacement, email-content-with-enum)
against it and validates each response structurally, including the enum
constraint on email-content — the one call where a wrong/hallucinated name
would silently corrupt a RecommendationRun if enum weren't actually
grammar-enforced. Does not require the pair to be the currently-active
AIModelConfig row.
"""

import sys

from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from fragrance.ai_providers import registry
from fragrance.ai_providers.base import StructuredOutputError, sanitize_sdk_error_message
from fragrance.llm import (
    CANDIDATES_PROMPT,
    CANDIDATES_SCHEMA,
    CANDIDATES_TOOL_DESCRIPTION,
    CANDIDATES_TOOL_NAME,
    EMAIL_CONTENT_PROMPT,
    EMAIL_CONTENT_TOOL_DESCRIPTION,
    EMAIL_CONTENT_TOOL_NAME,
    PROFILE_PROMPT,
    PROFILE_SCHEMA,
    PROFILE_TOOL_DESCRIPTION,
    PROFILE_TOOL_NAME,
    REPLACEMENT_PROMPT,
    REPLACEMENT_SCHEMA,
    REPLACEMENT_TOOL_DESCRIPTION,
    REPLACEMENT_TOOL_NAME,
    build_email_content_tool_schema,
)
from fragrance.models import AI_PROVIDER_FAMILIES, AIModelConfig

DUMMY_COLLECTION = (
    "Aventus by Creed [own] - my signature scent\n"
    "Bleu de Chanel by Chanel [like]\n"
    "Sauvage by Dior [dislike] - too generic"
)
DUMMY_PAST_RECOMMENDATIONS = "Silver Mountain Water"
DUMMY_SEARCH_RESULTS = (
    "Search 1: niche oud fragrances:\n"
    "- Oud Wood by Tom Ford: A smoky, creamy oud blend. (https://example.com/oud-wood)\n"
    "- Black Afgano by Nasomatto: A dense, resinous oud. (https://example.com/black-afgano)"
)
DUMMY_CONFIRMED_NAMES = ["Aventus", "Bleu de Chanel"]
DUMMY_PICKS_TEXT = "- Aventus by Creed\n- Bleu de Chanel by Chanel"


def _validate_against_schema(data: object, schema: dict, path: str = "$") -> list[str]:
    """
    Minimal JSON Schema validator covering exactly the subset our four
    pipeline schemas use (object/array/string, properties/required/items/enum/
    additionalProperties/minItems/maxItems) — not a general-purpose validator.
    """
    errors: list[str] = []
    expected_type = schema.get("type")

    if expected_type == "object":
        if not isinstance(data, dict):
            return [f"{path}: expected object, got {type(data).__name__}"]
        for required_key in schema.get("required", []):
            if required_key not in data:
                errors.append(f"{path}: missing required property '{required_key}'")
        properties = schema.get("properties", {})
        for key, value in data.items():
            if key in properties:
                errors.extend(
                    _validate_against_schema(
                        data=value, schema=properties[key], path=f"{path}.{key}"
                    )
                )
            elif schema.get("additionalProperties") is False:
                errors.append(f"{path}: unexpected property '{key}'")

    elif expected_type == "array":
        if not isinstance(data, list):
            return [f"{path}: expected array, got {type(data).__name__}"]
        item_schema = schema.get("items")
        if item_schema:
            for index, item in enumerate(data):
                errors.extend(
                    _validate_against_schema(
                        data=item, schema=item_schema, path=f"{path}[{index}]"
                    )
                )
        min_items = schema.get("minItems")
        if min_items is not None and len(data) < min_items:
            errors.append(f"{path}: expected at least {min_items} items, got {len(data)}")
        max_items = schema.get("maxItems")
        if max_items is not None and len(data) > max_items:
            errors.append(f"{path}: expected at most {max_items} items, got {len(data)}")

    elif expected_type == "string":
        if not isinstance(data, str):
            errors.append(f"{path}: expected string, got {type(data).__name__}")
        elif "enum" in schema and data not in schema["enum"]:
            errors.append(f"{path}: value {data!r} not in enum {schema['enum']}")

    return errors


def _build_checks() -> list[dict]:
    return [
        {
            "label": "profile",
            "prompt": PROFILE_PROMPT.format(
                collection=DUMMY_COLLECTION,
                past_recommendations=DUMMY_PAST_RECOMMENDATIONS,
            ),
            "tool_name": PROFILE_TOOL_NAME,
            "description": PROFILE_TOOL_DESCRIPTION,
            "schema": PROFILE_SCHEMA,
            "max_tokens": 1024,
        },
        {
            "label": "candidates",
            "prompt": CANDIDATES_PROMPT.format(
                loved_notes="oud, amber",
                liked_notes="citrus",
                disliked_notes="aquatic",
                owns_list="Aventus",
                past_recommendations=DUMMY_PAST_RECOMMENDATIONS,
                search_results=DUMMY_SEARCH_RESULTS,
            ),
            "tool_name": CANDIDATES_TOOL_NAME,
            "description": CANDIDATES_TOOL_DESCRIPTION,
            "schema": CANDIDATES_SCHEMA,
            "max_tokens": 1024,
        },
        {
            "label": "replacement",
            "prompt": REPLACEMENT_PROMPT.format(
                failed_name="Silver Mountain Water",
                loved_notes="oud, amber",
                liked_notes="citrus",
                disliked_notes="aquatic",
                past_recommendations=DUMMY_PAST_RECOMMENDATIONS,
                search_results=DUMMY_SEARCH_RESULTS,
            ),
            "tool_name": REPLACEMENT_TOOL_NAME,
            "description": REPLACEMENT_TOOL_DESCRIPTION,
            "schema": REPLACEMENT_SCHEMA,
            "max_tokens": 512,
        },
        {
            "label": "email-content-with-enum",
            "prompt": EMAIL_CONTENT_PROMPT.format(
                loved_notes="oud, amber",
                liked_notes="citrus",
                picks=DUMMY_PICKS_TEXT,
            ),
            "tool_name": EMAIL_CONTENT_TOOL_NAME,
            "description": EMAIL_CONTENT_TOOL_DESCRIPTION,
            "schema": build_email_content_tool_schema(
                confirmed_names=DUMMY_CONFIRMED_NAMES
            ),
            "max_tokens": 2048,
        },
    ]


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
            help="On full pass, set verified_at on a matching AIModelConfig row without prompting",
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

        results: list[tuple[str, bool, str]] = []
        for check in _build_checks():
            label = check["label"]
            try:
                response = provider.generate_structured(
                    prompt=check["prompt"],
                    tool_name=check["tool_name"],
                    description=check["description"],
                    schema=check["schema"],
                    max_tokens=check["max_tokens"],
                )
            except StructuredOutputError as exc:
                results.append((label, False, f"provider error: {exc}"))
                continue
            except Exception as exc:  # network/auth/rate-limit errors from the real SDK
                # Sanitized: an OpenAI/Qwen auth failure's exception text can
                # carry a vendor-masked key fragment, and command stdout is a
                # known capture surface for this project.
                results.append((label, False, f"unexpected error: {sanitize_sdk_error_message(exc)}"))
                continue

            errors = _validate_against_schema(data=response, schema=check["schema"])
            if errors:
                results.append((label, False, "; ".join(errors)))
            else:
                results.append((label, True, "ok"))

        for label, passed, detail in results:
            status_word = self.style.SUCCESS("PASS") if passed else self.style.ERROR("FAIL")
            self.stdout.write(f"  [{status_word}] {label}: {detail}")

        pass_count = sum(1 for _, passed, _ in results if passed)
        total = len(results)
        self.stdout.write(f"\n{pass_count}/{total} schema checks passed.")

        if pass_count != total:
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

        config.verified_at = timezone.now()
        config.save(update_fields=["verified_at"])
        self.stdout.write(self.style.SUCCESS(f"Marked {config} as verified."))
