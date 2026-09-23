from django.urls import path

from . import views

app_name = "subscriptions"

urlpatterns = [
    path("subscription-plans", views.PlansView.as_view(), name="plans"),
    path("subscriptions", views.SubscribeView.as_view(), name="subscribe"),
    path("my-subscriptions", views.MySubscriptionsView.as_view(), name="my-subscriptions"),
]
