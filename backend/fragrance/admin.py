from django.contrib import admin

from .models import (
    Fragrance,
    FragranceConfig,
    PreferenceProfile,
    Recommendation,
    RecommendationRun,
)

# Register your models here.


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
