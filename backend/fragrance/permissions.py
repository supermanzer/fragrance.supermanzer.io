"""
fragrance/permissions.py

Subscription-gated DRF permission classes. Access is decided by
Subscription.has_active_access (a timestamp check); staff/superusers bypass.

Both classes raise SubscriptionRequired (402) rather than returning False, so
a lapsed user is distinguishable from an unauthenticated one (401) and from a
plain forbidden (403). Unauthenticated requests return False and fall through
to DRF's normal 401.

Neither class ever writes: a user with no Subscription row simply has no
access. Rows are created only at the quota chokepoint and at admin approval.
"""

from rest_framework.exceptions import APIException
from rest_framework.permissions import SAFE_METHODS, BasePermission
from rest_framework.request import Request
from rest_framework.views import APIView

from .models import Subscription

# NOTE: is_staff doubles as the subscription-exemption flag. A migration
# grandfathers every user who existed when the gate shipped by setting it, and
# staff bypass both these permission classes and the run quota (not the
# one-in-flight guard). Side effect: a staff account can also sign in to
# Django admin if it can reach the allowlisted admin URL, though with no model
# permissions granted it sees nothing.
SUBSCRIPTION_REQUIRED_DETAIL = "An active subscription is required for this action."


class SubscriptionRequired(APIException):
    status_code = 402
    default_code = "subscription_required"
    default_detail = SUBSCRIPTION_REQUIRED_DETAIL

    def __init__(self, detail: str | None = None, code: str | None = None) -> None:
        # Body is exactly {"code": ..., "detail": ...}; APIException's default
        # would render a bare {"detail": ...} with no machine-readable code.
        super().__init__(detail=detail, code=code)
        self.detail = {
            "code": self.default_code,
            "detail": str(detail if detail is not None else self.default_detail),
        }


def user_has_active_access(*, user: object) -> bool:
    if getattr(user, "is_staff", False) or getattr(user, "is_superuser", False):
        return True
    subscription = Subscription.objects.filter(user=user).first()
    return subscription is not None and subscription.has_active_access


class HasActiveSubscription(BasePermission):
    def has_permission(self, request: Request, view: APIView) -> bool:
        if not (request.user and request.user.is_authenticated):
            return False
        if user_has_active_access(user=request.user):
            return True
        raise SubscriptionRequired()


class HasActiveSubscriptionOrReadOnly(BasePermission):
    def has_permission(self, request: Request, view: APIView) -> bool:
        if not (request.user and request.user.is_authenticated):
            return False
        if request.method in SAFE_METHODS:
            return True
        if user_has_active_access(user=request.user):
            return True
        raise SubscriptionRequired()
