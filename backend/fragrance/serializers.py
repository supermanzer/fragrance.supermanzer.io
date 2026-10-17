from django.contrib.auth.models import User
from django.contrib.auth.password_validation import validate_password
from django.contrib.auth.tokens import default_token_generator
from django.core.exceptions import ValidationError as DjangoValidationError
from django.utils.encoding import force_str
from django.utils.http import urlsafe_base64_decode
from rest_framework import serializers

from .models import FRAGRANCE_STATUSES, FragranceConfig, Fragrance, Plan, PreferenceProfile, Recommendation, RecommendationRun
from .recipient import delivery_fields
from .services import MAX_RUN_DAY_OF_MONTH, MAX_RUN_RETRIES, http_url_or_empty, resolve_beta_invite_signup


class RecommendationSerializer(serializers.ModelSerializer):
    rating = serializers.SerializerMethodField()

    class Meta:
        model = Recommendation
        fields = '__all__'
        read_only_fields = ['user', 'run']

    def to_representation(self, instance: Recommendation) -> dict:
        # Rows stored before the scheme check may hold a non-http(s) URL.
        data = super().to_representation(instance)
        data['search_source_url'] = http_url_or_empty(url=data.get('search_source_url'))
        return data

    def get_rating(self, obj: Recommendation) -> str | None:
        # promoted_fragrance is a reverse OneToOneField accessor; it raises
        # RelatedObjectDoesNotExist (an AttributeError subclass) on no match,
        # so getattr(..., None) is the correct idiom here, not try/except.
        fragrance = getattr(obj, 'promoted_fragrance', None)
        return fragrance.status if fragrance else None


class BetaAcceptSerializer(serializers.Serializer):
    """
    Validates a beta-invite token and the new password, but does not itself
    mutate anything -- BetaAcceptView performs the actual status transition
    under a row lock, since single-use enforcement needs to happen at the
    point of mutation, not at validation time, to be race-safe against a
    replayed link.

    Token resolution is delegated to services.resolve_beta_invite_signup,
    which raises BetaInviteTokenInvalidError / BetaInviteTokenAlreadyUsedError
    rather than serializers.ValidationError -- those propagate out of
    is_valid() uncaught, and BetaAcceptView translates them into the 400
    "invalid_or_expired" / 409 "already_used" contract. Only the password
    field uses DRF's normal ValidationError shape.
    """

    # allow_blank=True: an empty string must reach resolve_beta_invite_signup
    # and come back as the standard {"code": "invalid_or_expired"} shape,
    # the same as GET's request.query_params.get("token", "") -- without it,
    # DRF's field-level blank check would reject first with a third response
    # shape ({"token": ["This field may not be blank."]}) not in the
    # documented contract. A wholly *missing* "token" key still 400s with
    # that DRF field-required shape; the real frontend form always sends a
    # string (possibly empty), so that case is unreachable in practice.
    token = serializers.CharField(allow_blank=True)
    password = serializers.CharField(write_only=True)

    def validate(self, attrs: dict) -> dict:
        signup = resolve_beta_invite_signup(token=attrs['token'])

        try:
            validate_password(password=attrs['password'], user=signup.invited_user)
        except DjangoValidationError as exc:
            raise serializers.ValidationError({'password': list(exc.messages)})

        attrs['signup_id'] = signup.id
        return attrs


class BetaSignupSerializer(serializers.Serializer):
    email = serializers.EmailField()
    # Honeypot: real users never see or fill this field (hidden in the
    # frontend form); a bot that fills every input trips it. Non-empty means
    # silently drop the submission, never store it.
    website = serializers.CharField(required=False, allow_blank=True, default='')


class ChangePasswordSerializer(serializers.Serializer):
    current_password = serializers.CharField(write_only=True)
    new_password = serializers.CharField(write_only=True)
    new_password_confirm = serializers.CharField(write_only=True)
    refresh = serializers.CharField(write_only=True, required=False, allow_blank=True)

    def validate(self, attrs: dict) -> dict:
        user = self.context['request'].user
        if not user.check_password(attrs['current_password']):
            raise serializers.ValidationError({'current_password': 'Current password is incorrect.'})
        if attrs['new_password'] != attrs['new_password_confirm']:
            raise serializers.ValidationError({'new_password_confirm': 'Passwords do not match.'})
        try:
            validate_password(attrs['new_password'], user)
        except DjangoValidationError as exc:
            raise serializers.ValidationError({'new_password': list(exc.messages)})
        return attrs


class ChangePlanSerializer(serializers.Serializer):
    """Only a plan slug, resolved server-side to a Plan; never a price id."""

    plan = serializers.SlugField()


class CheckoutCreateSerializer(serializers.Serializer):
    """
    Only a plan slug -- an opaque key into the server-owned Plan table.
    A client-supplied price_id is never a recognized field here, so DRF
    silently ignores it rather than accepting it; BillingCheckoutView always
    resolves the Stripe Price id itself via Plan.objects.get(slug=...).
    """

    slug = serializers.SlugField()


class FragranceConfigSerializer(serializers.ModelSerializer):
    """
    `recipient_email` is read-only here and always the verified address; a
    change goes through POST /config/recipient/ (see fragrance/recipient.py).
    The pending block is computed server-side so clients never compare clocks.
    """

    class Meta:
        model = FragranceConfig
        exclude = ['user']
        read_only_fields = ['recipient_email']
        # An out-of-range value would save and then yield an invalid crontab;
        # django-celery-beat swallows that error, silently losing the run.
        # Day of month stops at 28: 29-31 would skip short months.
        extra_kwargs = {
            'run_hour': {'min_value': 0, 'max_value': 23},
            'run_day_of_week': {'min_value': 0, 'max_value': 6},
            'run_day_of_month': {'min_value': 1, 'max_value': MAX_RUN_DAY_OF_MONTH},
            'run_month': {'min_value': 1, 'max_value': 12},
        }

    def update(self, instance: FragranceConfig, validated_data: dict) -> FragranceConfig:
        """
        Writes only the submitted columns. A full save() would write back the
        recipient_email loaded at the start of the request and undo a
        delivery-address confirmation committed in between.
        """
        for name, value in validated_data.items():
            setattr(instance, name, value)
        if validated_data:
            instance.save(update_fields=list(validated_data))
        instance.refresh_from_db(fields=['recipient_email'])
        return instance

    def to_representation(self, instance: FragranceConfig) -> dict:
        data = super().to_representation(instance)
        # The address deliveries actually use: a blank column falls back to the account email.
        data['recipient_email'] = str(instance.recipient_email or instance.user.email or '')
        fields = delivery_fields(user=instance.user)
        for key in ('pending_expires_at', 'resend_available_at'):
            value = fields[key]
            fields[key] = serializers.DateTimeField().to_representation(value=value) if value else None
        data.update(fields)
        return data


class FragranceSerializer(serializers.ModelSerializer):
    class Meta:
        model = Fragrance
        exclude = ['user']
        read_only_fields = ['added_at', 'source_recommendation']


class PasswordResetConfirmSerializer(serializers.Serializer):
    uid = serializers.CharField()
    token = serializers.CharField()
    new_password = serializers.CharField(write_only=True)
    new_password_confirm = serializers.CharField(write_only=True)

    def validate(self, attrs: dict) -> dict:
        if attrs['new_password'] != attrs['new_password_confirm']:
            raise serializers.ValidationError({'new_password_confirm': 'Passwords do not match.'})
        try:
            pk = force_str(urlsafe_base64_decode(attrs['uid']))
            user = User.objects.get(pk=pk)
        except (User.DoesNotExist, ValueError, TypeError, OverflowError):
            raise serializers.ValidationError('Invalid or expired reset link.')
        if not default_token_generator.check_token(user=user, token=attrs['token']):
            raise serializers.ValidationError('Invalid or expired reset link.')
        try:
            validate_password(password=attrs['new_password'], user=user)
        except DjangoValidationError as exc:
            raise serializers.ValidationError({'new_password': list(exc.messages)})
        attrs['user'] = user
        return attrs


class PasswordResetRequestSerializer(serializers.Serializer):
    email = serializers.EmailField()


class PlanSerializer(serializers.ModelSerializer):
    """
    Deliberately excludes stripe_price_id (and every other internal field):
    the whole point of the slug indirection in CheckoutCreateSerializer is
    that a Stripe Price id never leaves the server, so it must not leak back
    out through the GET that renders the paywall either.
    """

    class Meta:
        model = Plan
        fields = ["slug", "name", "amount_cents", "currency", "billing_interval"]


class PreferenceProfileSerializer(serializers.ModelSerializer):
    class Meta:
        model = PreferenceProfile
        exclude = ['user']
        read_only_fields = ['generated_at']


class RecommendationRatingSerializer(serializers.Serializer):
    action = serializers.ChoiceField(choices=FRAGRANCE_STATUSES)


class RecommendationRunSerializer(serializers.ModelSerializer):
    """
    A run is a whole retry chain (one row). Retry bookkeeping columns are
    excluded and re-exposed as `attempt`, `max_attempts` and `retry`;
    `error_message` is blank unless the requester is staff (strictly
    is_staff, like the subscription payload's is_staff), and blank when there
    is no request to prove otherwise.
    """

    picks = RecommendationSerializer(many=True, read_only=True)
    max_attempts = serializers.SerializerMethodField()
    retry = serializers.SerializerMethodField()

    class Meta:
        model = RecommendationRun
        exclude = [
            'user',
            'attempt_started_at',
            'retry_state',
            'next_retry_at',
            'last_failed_at',
            'retry_notice_sent_at',
            'exhausted_notice_sent_at',
            'operator_notified_at',
        ]
        read_only_fields = ['triggered_at', 'status', 'email_status', 'celery_task_id', 'error_message', 'sent_at', 'attempt']

    def get_max_attempts(self, obj: RecommendationRun) -> int:
        return MAX_RUN_RETRIES + 1

    def get_retry(self, obj: RecommendationRun) -> dict | None:
        if obj.status != 'failed' or not obj.retry_state:
            return None
        return {
            'state': obj.retry_state,
            'retries_remaining': (
                0 if obj.retry_state == 'exhausted' else max(0, MAX_RUN_RETRIES - (obj.attempt - 1))
            ),
            'next_retry_at': (
                serializers.DateTimeField().to_representation(value=obj.next_retry_at)
                if obj.retry_state == 'scheduled' and obj.next_retry_at
                else None
            ),
            'last_failed_at': (
                serializers.DateTimeField().to_representation(value=obj.last_failed_at)
                if obj.last_failed_at
                else None
            ),
        }

    def to_representation(self, instance: RecommendationRun) -> dict:
        data = super().to_representation(instance=instance)
        request = self.context.get('request')
        if not (request is not None and request.user.is_staff):
            data['error_message'] = ''
        return data
