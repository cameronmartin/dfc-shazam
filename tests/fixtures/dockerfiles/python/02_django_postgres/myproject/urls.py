"""URL configuration for myproject project."""
from django.http import JsonResponse
from django.urls import path


def health(request):
    return JsonResponse({"status": "healthy"})


def home(request):
    return JsonResponse({"message": "Hello from Django!"})


urlpatterns = [
    path("", home),
    path("health/", health),
]
