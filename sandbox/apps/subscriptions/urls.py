"""URL routes for the Maxio subscriptions API, mounted under ``/api/``."""

from django.urls import path

from . import views

app_name = "subscriptions"

urlpatterns = [
    path("subscription-plans", views.subscription_plans, name="subscription-plans"),
    path("subscriptions", views.subscriptions, name="subscriptions"),
    path("my-subscriptions", views.my_subscriptions, name="my-subscriptions"),
]
