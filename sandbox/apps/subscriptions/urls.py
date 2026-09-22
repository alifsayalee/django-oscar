from django.urls import path

from . import views

app_name = 'subscriptions'

urlpatterns = [
    path('subscription-plans', views.subscription_plans, name='plans'),
    path('subscriptions', views.create_subscription, name='create'),
    path('my-subscriptions', views.my_subscriptions, name='mine'),
]
