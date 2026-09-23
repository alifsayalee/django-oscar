from django.urls import path

from . import views

app_name = 'subscriptions'

urlpatterns = [
    path('subscription-plans', views.subscription_plans, name='plans'),
    path('subscriptions', views.subscriptions, name='subscribe'),
    path('my-subscriptions', views.my_subscriptions, name='mine'),
]
