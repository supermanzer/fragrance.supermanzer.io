import logging

from django import forms
from django.contrib import admin, messages
from django.contrib.admin.models import LogEntry
from django.contrib.auth.models import User
from django.contrib.contenttypes.models import ContentType
from django.db import transaction
from django.db.models import QuerySet
from django.forms import ModelForm
from django.http import HttpRequest, HttpResponse, HttpResponseRedirect
from django.urls import reverse
from django.utils import timezone

from . import billing
from .ai_providers import registry
from .ai_providers.verification import persist_verification_results, run_verification_checks
from .models import (
    AIModelConfig,
    BetaSignup,
    Fragrance,
    FragranceConfig,
    Plan,
    PreferenceProfile,
    PriceChangeNotice,
    ProcessedPayment,
    Recommendation,
    RecommendationRun,
    StripeEvent,
    Subscription,
)
from .services import (
    MAX_RUN_DAY_OF_MONTH,
    PriceChangeNoticeError,
    default_schedule_for,
    derive_unique_username,
    get_or_create_fragrance_config,
    price_change_notice_problems,
    price_change_recipients,
    send_price_change_notice,
)

# Register your models here.


logger = logging.getLogger(__name__)


class PlanAdminForm(forms.ModelForm):
    """
    On the ADD form only, verifies the entered stripe_price_id against Stripe
    (billing.validate_plan_price) and refuses the save with an error on the
    price-id field if any check fails, including "Stripe could not be
    reached" -- a Plan must never be created on an unverified Price.
    stripe_price_id is read-only once a Plan exists, so the change form
    never re-checks.
    """

    class Meta:
        model = Plan
        fields = "__all__"

    def clean(self) -> dict:
        cleaned_data = super().clean()
        if not self.instance._state.adding:
            return cleaned_data
        required = ("stripe_price_id", "amount_cents", "currency", "billing_interval")
        if any(name not in cleaned_data for name in required):
            return cleaned_data
        problems = billing.validate_plan_price(
            price_id=cleaned_data["stripe_price_id"],
            amount_cents=cleaned_data["amount_cents"],
            currency=cleaned_data["currency"],
            billing_interval=cleaned_data["billing_interval"],
        )
        if problems:
            self.add_error("stripe_price_id", problems)
        return cleaned_data


class PriceChangeNoticeAdminForm(forms.ModelForm):
    """Runs the same refusal rules as the send itself, so a bad date or price is a field error, not a failed send."""

    class Meta:
        model = PriceChangeNotice
        fields = ("plan", "new_amount_cents", "effective_date")

    def clean(self) -> dict:
        cleaned_data = super().clean()
        plan = cleaned_data.get("plan")
        new_amount_cents = cleaned_data.get("new_amount_cents")
        effective_date = cleaned_data.get("effective_date")
        if plan is None or new_amount_cents is None or effective_date is None:
            return cleaned_data
        for problem in price_change_notice_problems(
            plan=plan, new_amount_cents=new_amount_cents, effective_date=effective_date
        ):
            self.add_error(None, problem)
        return cleaned_data


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

                    # No plan yet: the first payment sets it
                    # (billing._grant_period_for_invoice).
                    Subscription.objects.create(user=user, status="none")

                    # Created here, not lazily by the first GET /config/: a
                    # subscriber who never opens Settings would otherwise have
                    # no schedule (the config's post_save creates the beat
                    # PeriodicTask) and no delivery address.
                    get_or_create_fragrance_config(user=user)

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


class FragranceConfigAdminForm(forms.ModelForm):
    """Same day cap as the API serializer; the crontab builder would clamp a larger value silently."""

    def clean_run_day_of_month(self) -> int:
        day = self.cleaned_data["run_day_of_month"]
        if not 1 <= day <= MAX_RUN_DAY_OF_MONTH:
            raise forms.ValidationError(f"Day of month must be between 1 and {MAX_RUN_DAY_OF_MONTH}.")
        return day


@admin.register(FragranceConfig)
class FragranceConfigAdmin(admin.ModelAdmin):
    form = FragranceConfigAdminForm

    def save_model(self, request, obj, form, change) -> None:
        # Jitter only on create, and only for schedule fields the admin left
        # on the form's default -- a value they actually typed is a choice.
        if not change:
            day, hour = default_schedule_for(user_id=obj.user_id)
            if "run_day_of_month" not in form.changed_data:
                obj.run_day_of_month = day
            if "run_hour" not in form.changed_data:
                obj.run_hour = hour
        super().save_model(request, obj, form, change)


@admin.register(Fragrance)
class FragranceAdmin(admin.ModelAdmin):
    pass


@admin.register(Plan)
class PlanAdmin(admin.ModelAdmin):
    form = PlanAdminForm
    list_display = ("slug", "name", "amount_cents", "billing_interval", "is_active")
    list_filter = ("is_active", "billing_interval")
    actions = ["send_price_change_notice_action"]

    def get_readonly_fields(self, request: HttpRequest, obj: Plan | None = None) -> tuple[str, ...]:
        # An edited price id silently stops every current subscriber's
        # renewal from matching a Plan (billing._match_plan_line), so it is
        # set once, on add. Changing a price means creating a NEW Plan.
        readonly = tuple(super().get_readonly_fields(request, obj))
        if obj is not None:
            return (*readonly, "stripe_price_id")
        return readonly

    @admin.action(description="Send a 30-day price-change notice to this plan's subscribers...")
    def send_price_change_notice_action(
        self, request: HttpRequest, queryset: QuerySet[Plan]
    ) -> HttpResponse | None:
        """
        Opens the notice form for exactly one plan (prefilled), where the new
        price and effective date are entered; nothing is sent from here.
        """
        if not request.user.has_perm("fragrance.add_pricechangenotice"):
            self.message_user(request, "You may not send price-change notices.", level=messages.ERROR)
            return None
        if queryset.count() != 1:
            self.message_user(request, "Select exactly one plan.", level=messages.ERROR)
            return None
        plan = queryset.get()
        self.message_user(
            request,
            f"{price_change_recipients(plan=plan).count()} active subscriber(s) on {plan} would receive the notice.",
            level=messages.INFO,
        )
        return HttpResponseRedirect(f"{reverse('admin:fragrance_pricechangenotice_add')}?plan={plan.pk}")


@admin.register(PriceChangeNotice)
class PriceChangeNoticeAdmin(admin.ModelAdmin):
    """
    Add-only: saving the add form sends the notice (one email per active
    subscriber of the plan) and records it. Sent notices are a permanent
    record: view only, never edited or deleted. Changes nothing at Stripe.
    """

    form = PriceChangeNoticeAdminForm
    list_display = (
        "plan", "current_amount_cents", "new_amount_cents", "effective_date", "sent_at", "recipient_count",
    )
    list_filter = ("plan",)
    readonly_fields = (
        "plan", "current_amount_cents", "new_amount_cents", "effective_date", "sent_at",
        "recipient_count", "created_by",
    )

    def get_readonly_fields(
        self, request: HttpRequest, obj: PriceChangeNotice | None = None
    ) -> tuple[str, ...]:
        return self.readonly_fields if obj is not None else ()

    def has_change_permission(self, request: HttpRequest, obj: PriceChangeNotice | None = None) -> bool:
        return False

    def has_delete_permission(self, request: HttpRequest, obj: PriceChangeNotice | None = None) -> bool:
        return False

    def add_view(
        self, request: HttpRequest, form_url: str = "", extra_context: dict | None = None
    ) -> HttpResponse:
        """
        The emails are queued in a commit hook, which runs when the admin's
        transaction exits, after save_model and response_add. A queue failure
        (the service has already deleted the record) therefore surfaces here.
        """
        try:
            response = super().add_view(request, form_url=form_url, extra_context=extra_context)
        except PriceChangeNoticeError as exc:
            # Drop the "added successfully" message queued before the commit hook failed.
            list(messages.get_messages(request))
            failed_notice = getattr(request, "_price_change_notice", None)
            if failed_notice is not None:
                # The notice row was deleted by the service; its "Added" entry would dangle.
                LogEntry.objects.filter(
                    content_type=ContentType.objects.get_for_model(PriceChangeNotice),
                    object_id=str(failed_notice.pk),
                ).delete()
            self.message_user(
                request, f"Notice NOT sent: {'; '.join(exc.problems)}", level=messages.ERROR
            )
            return HttpResponseRedirect(reverse("admin:fragrance_pricechangenotice_changelist"))
        notice = getattr(request, "_price_change_notice", None)
        if notice is not None:
            self.message_user(
                request,
                f"Price-change notice queued for {notice.recipient_count} subscriber(s) of {notice.plan}.",
                level=messages.SUCCESS,
            )
        return response

    def log_addition(self, request: HttpRequest, obj: PriceChangeNotice, message: object) -> object:
        if obj.pk is None:
            return None
        return super().log_addition(request, obj, message)

    def response_add(
        self, request: HttpRequest, obj: PriceChangeNotice, post_url_continue: str | None = None
    ) -> HttpResponse:
        if obj.pk is None:
            return HttpResponseRedirect(reverse("admin:fragrance_pricechangenotice_changelist"))
        return super().response_add(request, obj, post_url_continue)

    def save_model(
        self, request: HttpRequest, obj: PriceChangeNotice, form: ModelForm, change: bool
    ) -> None:
        try:
            send_price_change_notice(notice=obj, created_by=request.user)
        except PriceChangeNoticeError as exc:
            self.message_user(
                request, f"Notice NOT sent: {'; '.join(exc.problems)}", level=messages.ERROR
            )
            return
        # The emails are queued after the admin's own transaction commits, so a
        # queue failure surfaces in add_view, not here; the result is reported there.
        request._price_change_notice = obj  # ty: ignore[unresolved-attribute]


@admin.register(PreferenceProfile)
class PreferenceProfileAdmin(admin.ModelAdmin):
    pass


@admin.register(ProcessedPayment)
class ProcessedPaymentAdmin(admin.ModelAdmin):
    list_display = ("stripe_invoice_id", "subscription", "amount_paid_cents", "applied_at", "period_end")
    search_fields = ("stripe_invoice_id", "stripe_subscription_id")
    readonly_fields = [f.name for f in ProcessedPayment._meta.fields]

    def has_add_permission(self, request: HttpRequest) -> bool:
        return False


@admin.register(Recommendation)
class RecommendationAdmin(admin.ModelAdmin):
    pass


@admin.register(RecommendationRun)
class RecommendationRunAdmin(admin.ModelAdmin):
    pass


@admin.register(StripeEvent)
class StripeEventAdmin(admin.ModelAdmin):
    list_display = ("stripe_event_id", "event_type", "status", "received_at", "processed_at")
    list_filter = ("status", "event_type")
    search_fields = ("stripe_event_id",)
    readonly_fields = [f.name for f in StripeEvent._meta.fields]

    def has_add_permission(self, request: HttpRequest) -> bool:
        return False


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
    actions = ["unlock_disputed_accounts"]

    def has_add_permission(self, request: HttpRequest) -> bool:
        return False

    # Deleting a revoked row would let get_or_create recreate it as "none",
    # bypassing the lock and the unlock action's audit line.
    def has_delete_permission(
        self, request: HttpRequest, obj: Subscription | None = None
    ) -> bool:
        return False

    @admin.action(
        description="Unlock disputed account (revoked -> none; access stays lapsed)",
        permissions=["change"],
    )
    def unlock_disputed_accounts(
        self, request: HttpRequest, queryset: QuerySet[Subscription]
    ) -> None:
        """
        The only way out of `revoked`. Sets status back to "none" and nothing
        else: access_expires_at is kept (the dispute set it to the dispute
        time), so access stays lapsed until a NEW subscription's payment
        grants normally. Do this only after the disputed payment and any
        money received on the locked account have been settled in Stripe.
        Each row is re-fetched under a row lock so a concurrent revoke or
        grant cannot interleave; non-revoked rows are skipped and reported.
        """
        for subscription in queryset:
            with transaction.atomic():
                try:
                    locked = Subscription.objects.select_for_update().get(pk=subscription.pk)
                except Subscription.DoesNotExist:
                    self.message_user(
                        request,
                        f"Skipped subscription {subscription.pk}: it no longer exists.",
                        level=messages.WARNING,
                    )
                    continue
                label = f"{locked.user} (subscription {locked.pk})"
                if locked.status != "revoked":
                    self.message_user(
                        request,
                        f"Skipped {label}: status is '{locked.status}', not 'revoked'.",
                        level=messages.WARNING,
                    )
                    continue
                locked.status = "none"
                locked.save(update_fields=["status"])
            logger.warning(
                "Disputed account UNLOCKED: subscription %s (user %s) set revoked -> none by "
                "admin user %s",
                locked.pk,
                locked.user_id,
                request.user.pk,
            )
            self.message_user(
                request,
                f"Unlocked {label}: status is now 'none'; access stays lapsed until a new "
                "subscription is paid.",
                level=messages.SUCCESS,
            )
