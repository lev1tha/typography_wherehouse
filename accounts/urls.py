from django.urls import path

from .views import CloudeTokenObtainPairView, CloudeTokenRefreshView, HealthView, MeView

urlpatterns = [
    path("token/", CloudeTokenObtainPairView.as_view(), name="token_obtain_pair"),
    path("token/refresh/", CloudeTokenRefreshView.as_view(), name="token_refresh"),
    path("health/", HealthView.as_view(), name="health"),
    path("me/", MeView.as_view(), name="me"),
]
