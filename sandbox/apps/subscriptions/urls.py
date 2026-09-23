"""URL routes for the Maxio subscription API, mounted under ``/api/`` by the sandbox root urls."""

from django.urls import path

from . import views

app_name = "subscriptions"

urlpatterns = [
    path("subscription-plans", views.subscription_plans, name="subscription-plans"),
    path("subscriptions", views.create_subscription, name="create-subscription"),
    path("my-subscriptions", views.my_subscriptions, name="my-subscriptions"),
]
