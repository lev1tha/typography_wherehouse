from rest_framework import generics, permissions
from rest_framework_simplejwt.views import TokenObtainPairView

from .serializers import CloudeTokenObtainPairSerializer, UserSerializer


class CloudeTokenObtainPairView(TokenObtainPairView):
    """POST /api/token/ — login, returns JWT pair + user role."""

    serializer_class = CloudeTokenObtainPairSerializer


class MeView(generics.RetrieveAPIView):
    """GET /api/me/ — the currently authenticated staff user."""

    serializer_class = UserSerializer
    permission_classes = [permissions.IsAuthenticated]

    def get_object(self):
        return self.request.user
