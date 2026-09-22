"""URL routing for the Maxio subscription API, included under ``/api/`` by the project urlconf."""

from django.urls import path

from . import views

app_name = 'subscriptions'

urlpatterns = [
    path('subscription-plans', views.SubscriptionPlansView.as_view(), name='plans'),
    path('subscriptions', views.SubscriptionsView.as_view(), name='subscribe'),
    path('my-subscriptions', views.MySubscriptionsView.as_view(), name='my-subscriptions'),
]
