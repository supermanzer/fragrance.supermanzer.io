from django.contrib import admin, messages
from django.forms import ModelForm
from django.http import HttpRequest, HttpResponse, HttpResponseRedirect

from .ai_providers import registry
from .ai_providers.verification import persist_verification_results, run_verification_checks
from .models import (
    AIModelConfig,
    BetaSignup,
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
    # These three are set only by the verify_ai_model conformance harness (or,
    # equivalently, the "Run conformance check now" button on this change
    # form, which calls the exact same shared verification logic).
    # supports_enum_enforcement in particular gates whether the registry
    # trusts a model's structured-output enforcement (llm.py ~line 131-134);
    # hand-editing it here would let an unverified model claim verification.
    # is_active and model_id stay editable — that's the intended operator
    # control (choosing which verified model is active).
    readonly_fields = ("verified_at", "supports_strict_schema", "supports_enum_enforcement")
    # Injects the "Run conformance check now" button into the submit row.
    # Django resolves this candidate before falling back to the stock
    # admin/change_form.html, so it only affects this ModelAdmin's form.
    change_form_template = "admin/fragrance/aimodelconfig/change_form.html"

    VERIFY_NOW_FIELD_NAME = "_verify_now"

    def save_model(
        self, request: HttpRequest, obj: AIModelConfig, form: ModelForm, change: bool
    ) -> None:
        # family/model_id are the exact pair the trust fields attest to. A
        # plain Save (as opposed to "Run conformance check now") never runs
        # verification, so a family/model_id edit must not leave a stale
        # True/timestamp standing on a row that now points at a different,
        # never-tested model.
        if change and ("family" in form.changed_data or "model_id" in form.changed_data):
            obj.verified_at = None
            obj.supports_enum_enforcement = False
            obj.supports_strict_schema = False
        super().save_model(request, obj, form, change)

    def response_change(self, request: HttpRequest, obj: AIModelConfig) -> HttpResponse:
        if self.VERIFY_NOW_FIELD_NAME in request.POST:
            return self._run_verification_and_redirect(request=request, obj=obj)
        return super().response_change(request, obj)

    def _run_verification_and_redirect(
        self, *, request: HttpRequest, obj: AIModelConfig
    ) -> HttpResponse:
        try:
            provider = registry.build_provider(config=obj)
        except (registry.MissingCredentialError, registry.UnknownProviderFamilyError) as exc:
            self.message_user(
                request, f"Could not run conformance check: {exc}", level=messages.ERROR
            )
            return HttpResponseRedirect(request.path)

        results = run_verification_checks(provider=provider)
        pass_count = sum(1 for result in results if result.passed)
        total = len(results)
        summary = "; ".join(
            f"{result.label}={'PASS' if result.passed else 'FAIL'}" for result in results
        )

        outcome = persist_verification_results(
            family=obj.family, model_id=obj.model_id, results=results
        )

        if outcome.status == "inconclusive":
            inconclusive_labels = ", ".join(
                result.label for result in results if result.inconclusive
            )
            self.message_user(
                request,
                f"Could not complete conformance check for {obj}: a connectivity, "
                f"authentication, or rate-limit error prevented check(s) "
                f"[{inconclusive_labels}] from getting a real response. No trust "
                f"flags were changed. ({summary})",
                level=messages.WARNING,
            )
        elif pass_count == total:
            self.message_user(
                request,
                f"Conformance check {pass_count}/{total} passed for {obj}. "
                f"Marked verified and updated capability flags. ({summary})",
                level=messages.SUCCESS,
            )
        else:
            self.message_user(
                request,
                f"Conformance check {pass_count}/{total} passed for {obj}. "
                f"Capability flags reset to False; verified_at left unchanged. ({summary})",
                level=messages.WARNING,
            )
        return HttpResponseRedirect(request.path)


@admin.register(FragranceConfig)
class FragranceConfigAdmin(admin.ModelAdmin):
    pass


@admin.register(Fragrance)
class FragraneAdmin(admin.ModelAdmin):
    readonly_fields = ("name_slug", "house_slug")


@admin.register(PreferenceProfile)
class PreferenceProfileAdmin(admin.ModelAdmin):
    pass


@admin.register(Recommendation)
class RecommendationAdmin(admin.ModelAdmin):
    readonly_fields = ("name_slug", "house_slug")


@admin.register(RecommendationRun)
class RecommendationRunAdmin(admin.ModelAdmin):
    pass


@admin.register(BetaSignup)
class BetaSignupAdmin(admin.ModelAdmin):
    # Approve/reject actions land in a later slice, alongside the User/
    # Subscription creation they trigger -- this is a list/inspect view only.
    list_display = ("email", "normalized_email", "status", "created_at", "approved_at")
    list_filter = ("status",)
    search_fields = ("email", "normalized_email")
    readonly_fields = (
        "email",
        "normalized_email",
        "status",
        "created_at",
        "approved_at",
        "approved_by",
        "invited_user",
        "signup_ip_hash",
        "converted_at",
    )

    def has_add_permission(self, request: HttpRequest) -> bool:
        # Every field is readonly for this slice, so the stock add form would
        # render with nothing to fill in and "Save" would create a blank row
        # that then 500s on the normalized_email unique constraint. Rows are
        # created only by the public signup endpoint until slice 2 adds
        # approve/reject actions.
        return False
