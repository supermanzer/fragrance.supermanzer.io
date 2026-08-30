"""
Serializer/view overrides that close a TOCTOU race in SimpleJWT's stock
TokenRefreshSerializer.

In the installed version (5.5.1), TokenRefreshSerializer.validate() does not
call check_blacklist() as a separate step — it happens implicitly inside
`self.token_class(attrs["refresh"])`, since decoding a BlacklistMixin token
with verify=True (the default) runs `verify()` -> `check_blacklist()` as part
of construction. The unconditional `refresh.blacklist()` + rotation that
follows is a second, later step with no lock between the two. Two refresh
requests presenting the same still-valid refresh token can therefore both
decode-and-pass the blacklist check before either commits its rotation, each
minting an independently-valid new refresh chain instead of exactly one
succeeding and the other being rejected.

The fix: lock the presented token's OutstandingToken row (by jti) for the
duration of the check-then-rotate sequence. A concurrent refresh call on the
same token blocks on that lock until the first request's transaction
commits, then correctly observes the token as already blacklisted.
"""
from django.db import transaction
from rest_framework_simplejwt.serializers import TokenRefreshSerializer
from rest_framework_simplejwt.settings import api_settings
from rest_framework_simplejwt.token_blacklist.models import OutstandingToken
from rest_framework_simplejwt.tokens import RefreshToken
from rest_framework_simplejwt.views import TokenRefreshView


class AtomicTokenRefreshSerializer(TokenRefreshSerializer):
    def validate(self, attrs: dict) -> dict:
        # verify=False here skips signature/expiry/blacklist checks — this
        # decode is only used to read the jti so we know which row to lock.
        # The real verification (including check_blacklist) happens inside
        # super().validate() below, after the lock is held, using a fresh
        # verify=True decode of the same token string.
        unverified = RefreshToken(attrs["refresh"], verify=False)
        jti = unverified.payload.get(api_settings.JTI_CLAIM)

        with transaction.atomic():
            if jti:
                # select_for_update() blocks a concurrent call on the same
                # jti until this transaction commits or rolls back. If no
                # matching row exists (e.g. a forged token never minted via
                # for_user()), fall through unlocked — there's nothing to
                # serialize against and super().validate() will still reject
                # it on its own terms.
                OutstandingToken.objects.select_for_update().filter(jti=jti).first()
            return super().validate(attrs)


class AtomicTokenRefreshView(TokenRefreshView):
    serializer_class = AtomicTokenRefreshSerializer
