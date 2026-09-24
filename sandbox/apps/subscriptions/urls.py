from django.urls import path

from . import views

app_name = 'subscriptions'

urlpatterns = [
    path('subscription-plans', views.subscription_plans, name='subscription-plans'),
    path('subscriptions', views.create_subscription, name='subscriptions'),
    path('my-subscriptions', views.my_subscriptions, name='my-subscriptions'),
]
