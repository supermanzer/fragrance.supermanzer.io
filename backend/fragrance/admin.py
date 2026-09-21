from django.contrib import admin, messages
from django.contrib.auth.models import User
from django.db import transaction
from django.db.models import QuerySet
from django.forms import ModelForm
from django.http import HttpRequest, HttpResponse, HttpResponseRedirect
from django.utils import timezone

from .ai_providers import registry
from .ai_providers.verification import persist_verification_results, run_verification_checks
from .models import (
    AIModelConfig,
    BetaSignup,
    Fragrance,
    FragranceConfig,
    Plan,
    PreferenceProfile,
    ProcessedPayment,
    Recommendation,
    RecommendationRun,
    StripeEvent,
    Subscription,
)
from .services import derive_unique_username

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


@admin.register(Plan)
class PlanAdmin(admin.ModelAdmin):
    list_display = ("slug", "name", "amount_cents", "billing_interval", "is_active")
    list_filter = ("is_active", "billing_interval")


@admin.register(Subscription)
class SubscriptionAdmin(admin.ModelAdmin):
    list_display = ("user", "plan", "status", "access_expires_at", "last_paid_at")
    list_filter = ("status", "plan")
    search_fields = ("user__username", "user__email", "stripe_customer_id", "stripe_subscription_id")
    # Every field here is Stripe-derived state -- editing it here would let
    # the admin drift from what Stripe actually reports, which is exactly
    # the class of bug the payment-driven design (see Subscription's
    # docstring) exists to avoid. Read-only for operator visibility during
    # manual verification (see the plan's end-to-end walk), never for
    # granting access by hand.
    readonly_fields = [f.name for f in Subscription._meta.fields]

    def has_add_permission(self, request: HttpRequest) -> bool:
        return False


@admin.register(StripeEvent)
class StripeEventAdmin(admin.ModelAdmin):
    list_display = ("stripe_event_id", "event_type", "status", "received_at", "processed_at")
    list_filter = ("status", "event_type")
    search_fields = ("stripe_event_id",)
    readonly_fields = [f.name for f in StripeEvent._meta.fields]

    def has_add_permission(self, request: HttpRequest) -> bool:
        return False


@admin.register(ProcessedPayment)
class ProcessedPaymentAdmin(admin.ModelAdmin):
    list_display = ("stripe_invoice_id", "subscription", "amount_paid_cents", "applied_at", "period_end")
    search_fields = ("stripe_invoice_id", "stripe_subscription_id")
    readonly_fields = [f.name for f in ProcessedPayment._meta.fields]

    def has_add_permission(self, request: HttpRequest) -> bool:
        return False


_DEFAULT_PLAN_SLUG = "beta-monthly"


@admin.register(BetaSignup)
class BetaSignupAdmin(admin.ModelAdmin):
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
    actions = ["approve_signups", "reject_signups", "resend_invite"]

    def has_add_permission(self, request: HttpRequest) -> bool:
        # Every field is readonly, so the stock add form would render with
        # nothing to fill in and "Save" would create a blank row that then
        # 500s on the normalized_email unique constraint. Rows are created
        # only by the public signup endpoint; approve/reject are actions,
        # not the add form.
        return False

    @admin.action(description="Approve selected signups (creates User + Subscription, queues invite email)")
    def approve_signups(self, request: HttpRequest, queryset) -> None:
        from .tasks import send_beta_invite_email

        approved: list[str] = []
        skipped: list[str] = []

        for signup in queryset:
            try:
                with transaction.atomic():
                    # Re-fetched and locked under the transaction so two admins
                    # approving the same row at once serialize instead of both
                    # passing the pending-status guard.
                    locked = BetaSignup.objects.select_for_update().get(pk=signup.pk)

                    if locked.status != "pending":
                        skipped.append(f"{locked.email} (status is '{locked.status}', not 'pending')")
                        continue

                    # User.email is not unique in django.contrib.auth --
                    # PasswordResetRequestView already has a latent
                    # MultipleObjectsReturned 500 for exactly this reason.
                    # A .get() here would reproduce that bug; .exists() never can.
                    if User.objects.filter(email__iexact=locked.email).exists():
                        skipped.append(f"{locked.email} (a User with this email already exists)")
                        continue

                    username = derive_unique_username(email=locked.email)
                    user = User(username=username, email=locked.email, is_active=True)
                    user.set_unusable_password()
                    user.save()

                    default_plan = Plan.objects.filter(
                        slug=_DEFAULT_PLAN_SLUG, is_active=True
                    ).first()
                    Subscription.objects.create(user=user, plan=default_plan, status="none")

                    locked.approved_at = timezone.now()
                    locked.approved_by = request.user
                    locked.invited_user = user
                    locked.status = "approved"
                    locked.save(
                        update_fields=["approved_at", "approved_by", "invited_user", "status"]
                    )

                    # Queued only after the transaction commits -- a worker
                    # could otherwise pick this up before the User/BetaSignup
                    # rows it reads are actually visible.
                    transaction.on_commit(
                        lambda signup_id=locked.id: send_beta_invite_email.delay(
                            signup_id=signup_id
                        )
                    )
                    approved.append(locked.email)
            except Exception as exc:
                skipped.append(f"{signup.email} (error: {exc})")

        if approved:
            self.message_user(
                request, f"Approved {len(approved)} signup(s): {', '.join(approved)}",
                level=messages.SUCCESS,
            )
        if skipped:
            self.message_user(
                request, f"Skipped {len(skipped)} signup(s): {'; '.join(skipped)}",
                level=messages.WARNING,
            )

    @admin.action(description="Resend invite email (mints a fresh token)")
    def resend_invite(self, request: HttpRequest, queryset: QuerySet[BetaSignup]) -> None:
        """
        approve_signups guards on status == 'pending', so once a signup
        moves to 'approved' nothing can re-trigger its invite email --
        "it never arrived" (spam filter, Postmark bounce/suppression, a
        typo'd address) was previously a dead end short of a Django shell.
        Guards on 'approved' specifically: 'pending' has never had a User to
        invite, and 'converted'/'rejected' should never receive a fresh
        accept link.
        """
        from .tasks import send_beta_invite_email

        resent: list[str] = []
        skipped: list[str] = []

        for signup in queryset:
            with transaction.atomic():
                # Locked for the same reason approve_signups locks: two
                # admins resending the same row at once should serialize
                # rather than both pass the status guard.
                locked = BetaSignup.objects.select_for_update().get(pk=signup.pk)

                if locked.status != "approved":
                    skipped.append(f"{locked.email} (status is '{locked.status}', not 'approved')")
                    continue

                transaction.on_commit(
                    lambda signup_id=locked.id: send_beta_invite_email.delay(signup_id=signup_id)
                )
                resent.append(locked.email)

        if resent:
            self.message_user(
                request, f"Resent invite email to {len(resent)} signup(s): {', '.join(resent)}",
                level=messages.SUCCESS,
            )
        if skipped:
            self.message_user(
                request, f"Skipped {len(skipped)} signup(s): {'; '.join(skipped)}",
                level=messages.WARNING,
            )

    @admin.action(description="Reject selected signups")
    def reject_signups(self, request: HttpRequest, queryset) -> None:
        updated = queryset.filter(status="pending").update(status="rejected")
        skipped_count = queryset.count() - updated
        self.message_user(request, f"Rejected {updated} signup(s).", level=messages.SUCCESS)
        if skipped_count:
            self.message_user(
                request,
                f"Skipped {skipped_count} signup(s) not in 'pending' status.",
                level=messages.WARNING,
            )
