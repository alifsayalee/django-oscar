from django.urls import path

from . import views

app_name = 'subscriptions'

urlpatterns = [
    path('subscription-plans', views.subscription_plans, name='subscription-plans'),
    path('billing-customer', views.billing_customer, name='billing-customer'),
    path('subscriptions', views.subscriptions, name='subscriptions'),
    path('my-subscriptions', views.my_subscriptions, name='my-subscriptions'),
]
