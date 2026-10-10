from django.urls import path
from rest_framework.routers import SimpleRouter

from .views import (
    CloudeTokenObtainPairView,
    CloudeTokenRefreshView,
    EmployeeViewSet,
    HealthView,
    MeView,
    StaffUserViewSet,
)

router = SimpleRouter()
router.register("staff/users", StaffUserViewSet, basename="staff-user")
router.register("staff/employees", EmployeeViewSet, basename="staff-employee")

urlpatterns = [
    path("token/", CloudeTokenObtainPairView.as_view(), name="token_obtain_pair"),
    path("token/refresh/", CloudeTokenRefreshView.as_view(), name="token_refresh"),
    path("health/", HealthView.as_view(), name="health"),
    path("me/", MeView.as_view(), name="me"),
] + router.urls
