from django.urls import path

from . import views

app_name = 'subscriptions'

urlpatterns = [
    path('subscription-plans', views.subscription_plans, name='plans'),
    path('billing-customer', views.billing_customer, name='billing-customer'),
    path('subscriptions', views.subscriptions, name='subscribe'),
    path('my-subscriptions', views.my_subscriptions, name='my-subscriptions'),
    path('my-subscriptions/<int:subscription_id>', views.my_subscription, name='my-subscription'),
]
