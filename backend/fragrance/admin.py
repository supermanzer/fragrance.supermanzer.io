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
