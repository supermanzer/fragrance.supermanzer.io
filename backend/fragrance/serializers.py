from django.contrib.auth.models import User
from django.contrib.auth.password_validation import validate_password
from django.contrib.auth.tokens import default_token_generator
from django.core.exceptions import ValidationError as DjangoValidationError
from django.utils.encoding import force_str
from django.utils.http import urlsafe_base64_decode
from rest_framework import serializers

from .models import (
    FRAGRANCE_STATUSES,
    MAX_COLLECTION_SIZE,
    Fragrance,
    FragranceConfig,
    PreferenceProfile,
    Recommendation,
    RecommendationRun,
)
from .slugs import has_identity, slugify_house, slugify_name


class BetaSignupSerializer(serializers.Serializer):
    email = serializers.EmailField()
    # Honeypot: real users never see or fill this field (hidden in the
    # frontend form); a bot that fills every input trips it. Non-empty means
    # silently drop the submission, never store it.
    website = serializers.CharField(required=False, allow_blank=True, default='')


class FragranceConfigSerializer(serializers.ModelSerializer):
    class Meta:
        model = FragranceConfig
        exclude = ['user']


class FragranceSerializer(serializers.ModelSerializer):
    class Meta:
        model = Fragrance
        exclude = ['user']
        read_only_fields = ['added_at']

    def validate(self, attrs: dict) -> dict:
        """
        DRF cannot derive a validator from the (user, name_slug, house_slug)
        constraint (user is excluded, slugs are not editable), so identity
        uniqueness is checked here against the same slug rules the model and
        the constraint use. The view still maps a racing IntegrityError to 409.
        """
        user = self.context['request'].user
        instance = self.instance
        name = attrs.get('name', instance.name if instance else '')
        house = attrs.get('house', instance.house if instance else '')

        if instance is None and Fragrance.objects.filter(user=user).count() >= MAX_COLLECTION_SIZE:
            raise serializers.ValidationError(
                f'Your collection is limited to {MAX_COLLECTION_SIZE} fragrances. '
                'Remove some before adding more.'
            )
        name_changed = instance is None or ('name' in attrs and attrs['name'] != instance.name)
        if name_changed and not has_identity(name):
            raise serializers.ValidationError(
                {'name': 'The fragrance name must contain letters or numbers.'}
            )

        name_slug = slugify_name(name)
        house_slug = slugify_house(house)
        if instance is not None and (name_slug, house_slug) == (
            slugify_name(instance.name),
            slugify_house(instance.house),
        ):
            # Unchanged identity (notes-only PATCH, case-only rename, or a
            # disambiguated '-dup<id>' row): nothing new to collide with.
            return attrs
        clash = Fragrance.objects.filter(user=user, name_slug=name_slug, house_slug=house_slug)
        if instance is not None:
            clash = clash.exclude(pk=instance.pk)
        if clash.exists():
            raise serializers.ValidationError(
                {'name': 'A fragrance with this name and house is already in your collection.'}
            )
        return attrs


class PreferenceProfileSerializer(serializers.ModelSerializer):
    class Meta:
        model = PreferenceProfile
        exclude = ['user']
        read_only_fields = ['generated_at']


class RecommendationSerializer(serializers.ModelSerializer):
    rating = serializers.SerializerMethodField()

    class Meta:
        model = Recommendation
        fields = '__all__'
        read_only_fields = ['user', 'run']

    def get_rating(self, obj: Recommendation) -> str | None:
        # promoted_fragrance is a reverse OneToOneField accessor; it raises
        # RelatedObjectDoesNotExist (an AttributeError subclass) on no match,
        # so getattr(..., None) is the correct idiom here, not try/except.
        fragrance = getattr(obj, 'promoted_fragrance', None)
        return fragrance.status if fragrance else None


class RecommendationRatingSerializer(serializers.Serializer):
    action = serializers.ChoiceField(choices=FRAGRANCE_STATUSES)


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


class PasswordResetRequestSerializer(serializers.Serializer):
    email = serializers.EmailField()


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


class RecommendationRunSerializer(serializers.ModelSerializer):
    picks = RecommendationSerializer(many=True, read_only=True)

    class Meta:
        model = RecommendationRun
        exclude = ['user']
        read_only_fields = ['triggered_at', 'status', 'email_status', 'celery_task_id', 'error_message', 'sent_at']
