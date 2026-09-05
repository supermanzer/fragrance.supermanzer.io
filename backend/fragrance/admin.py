from django.contrib import admin

from .models import (
    AIModelConfig,
    Fragrance,
    FragranceConfig,
    PreferenceProfile,
    Recommendation,
    RecommendationRun,
)

# Register your models here.


@admin.register(AIModelConfig)
class AIModelConfigAdmin(admin.ModelAdmin):
    list_display = (
        "family",
        "model_id",
        "is_active",
        "supports_strict_schema",
        "supports_enum_enforcement",
        "verified_at",
    )
    list_filter = ("family", "is_active")
    # These three are set only by the verify_ai_model conformance harness.
    # supports_enum_enforcement in particular gates whether the registry
    # trusts a model's structured-output enforcement (llm.py ~line 131-134);
    # hand-editing it here would let an unverified model claim verification.
    # is_active and model_id stay editable — that's the intended operator
    # control (choosing which verified model is active).
    readonly_fields = ("verified_at", "supports_strict_schema", "supports_enum_enforcement")


@admin.register(FragranceConfig)
class FragranceConfigAdmin(admin.ModelAdmin):
    pass


@admin.register(Fragrance)
class FragraneAdmin(admin.ModelAdmin):
    pass


@admin.register(PreferenceProfile)
class PreferenceProfileAdmin(admin.ModelAdmin):
    pass


@admin.register(Recommendation)
class RecommendationAdmin(admin.ModelAdmin):
    pass


@admin.register(RecommendationRun)
class RecommendationRunAdmin(admin.ModelAdmin):
    pass
