"""Shared LLM provider fakes and canned structured responses for fragrance tests."""


import json
from types import SimpleNamespace


class _FakeProvider:
    """
    Minimal StructuredLLMProvider stand-in for pipeline tests: returns a fixed
    response and records every call's kwargs so tests can assert on the exact
    schema/prompt sent, without depending on any real SDK client.
    """

    def __init__(self, response: dict) -> None:
        self.response = response
        self.calls: list[dict] = []

    def generate_structured(
        self,
        *,
        prompt: str,
        tool_name: str,
        description: str,
        schema: dict,
        max_tokens: int,
    ) -> dict:
        self.calls.append(
            {
                "prompt": prompt,
                "tool_name": tool_name,
                "description": description,
                "schema": schema,
                "max_tokens": max_tokens,
            }
        )
        return self.response


def _fake_anthropic_response(input_data: dict) -> SimpleNamespace:
    tool_use_block = SimpleNamespace(type="tool_use", input=input_data)
    return SimpleNamespace(content=[tool_use_block], stop_reason="tool_use")


def _fake_openai_response(data: dict, refusal: str | None = None) -> SimpleNamespace:
    message = SimpleNamespace(
        content=None if refusal else json.dumps(data), refusal=refusal
    )
    return SimpleNamespace(choices=[SimpleNamespace(message=message)])


class _ScriptedProvider:
    """
    StructuredLLMProvider stand-in for full-pipeline tests: returns a
    per-tool-name canned response, so a single instance can stand in for all
    of a run's LLM calls (create_profile, select_candidates,
    generate_email_content, ...) without hitting a real SDK.
    """

    def __init__(self, responses_by_tool: dict[str, dict]) -> None:
        self._responses = responses_by_tool
        self.calls: list[dict] = []

    def generate_structured(
        self,
        *,
        prompt: str,
        tool_name: str,
        description: str,
        schema: dict,
        max_tokens: int,
    ) -> dict:
        self.calls.append({"tool_name": tool_name, "prompt": prompt})
        return self._responses[tool_name]


class _ScriptedProviderWithErrors:
    """
    Like _ScriptedProvider, but a response value that is an Exception
    instance is raised instead of returned — lets a single provider
    stand-in exercise run_verification_checks' three-way exception handling
    (StructuredOutputError / SDK BadRequestError / everything else) for one
    specific check while the other three succeed normally.
    """

    def __init__(self, responses_by_tool: dict[str, dict | Exception]) -> None:
        self._responses = responses_by_tool

    def generate_structured(
        self,
        *,
        prompt: str,
        tool_name: str,
        description: str,
        schema: dict,
        max_tokens: int,
    ) -> dict:
        value = self._responses[tool_name]
        if isinstance(value, Exception):
            raise value
        return value


def _echo_query_search(query: str, num_results: int = 5) -> list[dict]:
    # verify_candidates title-matches on candidate name-in-title; embedding the
    # query (which always starts with the candidate name) in the fake title
    # guarantees every candidate is 'confirmed' without exercising the
    # select_replacement LLM call, which is out of scope for this test.
    return [{"title": query, "url": "https://fragrantica.com/x", "content": "c"}]


_VALID_PROFILE_RESPONSE = {
    "loved_notes": "oud, amber",
    "liked_notes": "citrus",
    "disliked_notes": "aquatic",
    "owns_list": ["Aventus"],
    "search_angle_1": "niche oud fragrances",
    "search_angle_2": "amber fragrances 2026",
}
_VALID_REPLACEMENT_RESPONSE = {"name": "Oud Wood", "house": "Tom Ford"}


def _valid_candidates_response(count: int = 5) -> dict:
    return {
        "candidates": [
            {"name": f"Candidate {i}", "house": f"House {i}"} for i in range(count)
        ]
    }


def _valid_email_content_response() -> dict:
    return {
        "intro": "Welcome back.",
        "rationales": [
            {"name": name, "rationale": f"Suits {name}."}
            for name in ["Aventus", "Bleu de Chanel"]
        ],
    }
