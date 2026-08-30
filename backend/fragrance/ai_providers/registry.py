"""
fragrance/ai_providers/registry.py

Resolves the active AIModelConfig row (or an arbitrary one-off family/model_id,
for the verify_ai_model conformance harness) to a concrete StructuredLLMProvider.

Callers resolve once per run, not once per call — tasks.py's
monthly_fragrance_run resolves at the top of the run and threads the same
provider instance through every pipeline step, so a run never straddles two
providers if an admin flips is_active mid-run.
"""

from ..models import AIModelConfig
from .anthropic_provider import AnthropicProvider
from .base import StructuredLLMProvider
from .openai_provider import OpenAIProvider
from .qwen_provider import QwenProvider


class NoActiveAIModelConfigError(Exception):
    """Raised when no AIModelConfig row has is_active=True."""


class UnknownProviderFamilyError(Exception):
    """Raised when an AIModelConfig.family value has no registered adapter class."""


class MissingCredentialError(Exception):
    """Raised when the resolved family's API key is not configured in settings."""


class EnumEnforcementUnsupportedError(Exception):
    """
    Raised when the caller declares enum_required=True (the email-content call)
    but the resolved AIModelConfig.supports_enum_enforcement is False. A wrong
    or hallucinated fragrance name from an unconstrained model silently
    corrupts a RecommendationRun via rationale_map.get(rec.name, "") — this
    must be a hard failure at resolution time, not an advisory warning.
    """


_CREDENTIAL_SETTINGS: dict[str, str] = {
    "anthropic": "ANTHROPIC_API_KEY",
    "openai": "OPENAI_API_KEY",
    "qwen": "DASHSCOPE_API_KEY",
}

_PROVIDER_CLASSES: dict[str, type] = {
    "anthropic": AnthropicProvider,
    "openai": OpenAIProvider,
    "qwen": QwenProvider,
}


def _get_api_key(*, family: str) -> str:
    from django.conf import settings

    setting_name = _CREDENTIAL_SETTINGS.get(family)
    if setting_name is None:
        raise UnknownProviderFamilyError(f"Unknown AI provider family: {family!r}")
    api_key = getattr(settings, setting_name, "")
    if not api_key:
        raise MissingCredentialError(
            f"{setting_name} is not configured — cannot resolve a provider for "
            f"family {family!r}. Set it in backend/.env and recreate the "
            f"container so it's picked up."
        )
    return api_key


def build_provider(*, config: AIModelConfig) -> StructuredLLMProvider:
    """
    Constructs a provider instance from an AIModelConfig-shaped object — need
    not be persisted or active. Used directly by verify_ai_model to exercise a
    (family, model_id) pair that isn't the currently active row.
    """
    provider_class = _PROVIDER_CLASSES.get(config.family)
    if provider_class is None:
        raise UnknownProviderFamilyError(
            f"Unknown AI provider family: {config.family!r}"
        )
    api_key = _get_api_key(family=config.family)
    return provider_class(model_id=config.model_id, api_key=api_key)


def get_active_config(*, enum_required: bool = False) -> AIModelConfig:
    """
    Returns the single active AIModelConfig row. Raises NoActiveAIModelConfigError
    if none is active, or EnumEnforcementUnsupportedError if enum_required=True
    and the active row's supports_enum_enforcement is False.
    """
    config = AIModelConfig.objects.filter(is_active=True).first()
    if config is None:
        raise NoActiveAIModelConfigError(
            "No active AIModelConfig row — configure one via /admin/ before "
            "running the pipeline."
        )
    if enum_required and not config.supports_enum_enforcement:
        raise EnumEnforcementUnsupportedError(
            f"Active AIModelConfig {config.family}:{config.model_id} does not "
            "support enum enforcement, but this run requires it for the "
            "email-content call (enum_required=True). Verify a model that "
            "does via `manage.py verify_ai_model` and mark it verified before "
            "activating it."
        )
    return config


def get_active_provider(*, enum_required: bool = False) -> StructuredLLMProvider:
    """Convenience wrapper: get_active_config + build_provider in one call."""
    config = get_active_config(enum_required=enum_required)
    return build_provider(config=config)
