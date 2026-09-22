"""URL routing for the Maxio subscription-billing API.

Mounted under ``/api/`` from ``sandbox/urls.py``. Endpoints are named for the
capability they expose, and each action a caller can take is separately
invocable.
"""

from django.urls import path

from . import views

app_name = 'subscriptions'

urlpatterns = [
    path('subscription-plans', views.subscription_plans, name='subscription-plans'),
    path('subscriptions', views.create_subscription, name='subscriptions'),
    path('my-subscriptions', views.my_subscriptions, name='my-subscriptions'),
]
