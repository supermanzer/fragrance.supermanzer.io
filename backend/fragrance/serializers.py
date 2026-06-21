from django.contrib.auth.models import User
from django.contrib.auth.password_validation import validate_password
from django.contrib.auth.tokens import default_token_generator
from django.core.exceptions import ValidationError as DjangoValidationError
from django.utils.encoding import force_str
from django.utils.http import urlsafe_base64_decode
from rest_framework import serializers

from .models import FragranceConfig, Fragrance, PreferenceProfile, Recommendation, RecommendationRun


class UserRegistrationSerializer(serializers.ModelSerializer):
    password = serializers.CharField(write_only=True)

    class Meta:
        model = User
        fields = ['username', 'email', 'password']

    def create(self, validated_data):
        return User.objects.create_user(**validated_data)


class FragranceConfigSerializer(serializers.ModelSerializer):
    class Meta:
        model = FragranceConfig
        exclude = ['user']


class FragranceSerializer(serializers.ModelSerializer):
    class Meta:
        model = Fragrance
        exclude = ['user']
        read_only_fields = ['added_at']


class PreferenceProfileSerializer(serializers.ModelSerializer):
    class Meta:
        model = PreferenceProfile
        exclude = ['user']
        read_only_fields = ['generated_at']


class RecommendationSerializer(serializers.ModelSerializer):
    class Meta:
        model = Recommendation
        fields = '__all__'
        read_only_fields = ['user', 'run']


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
        read_only_fields = ['triggered_at', 'status', 'celery_task_id', 'error_message', 'sent_at']
