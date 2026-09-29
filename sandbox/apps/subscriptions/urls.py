from django.urls import path

from . import views

app_name = 'subscriptions-api'

urlpatterns = [
    path('subscription-plans', views.subscription_plans, name='plans'),
    path('subscriptions', views.create_subscription, name='subscribe'),
    path('my-subscriptions', views.my_subscriptions, name='my-subscriptions'),
]
