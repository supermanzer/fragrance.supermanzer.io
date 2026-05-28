from django.contrib.auth.models import User
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
        extra_kwargs = {
            'gmail_app_password_enc': {'write_only': True},
        }


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


class RecommendationRunSerializer(serializers.ModelSerializer):
    picks = RecommendationSerializer(many=True, read_only=True)

    class Meta:
        model = RecommendationRun
        exclude = ['user']
        read_only_fields = ['triggered_at', 'status', 'celery_task_id', 'error_message', 'sent_at']
