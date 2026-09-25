from django.urls import path

from . import views

app_name = 'subscriptions-api'

urlpatterns = [
    path('session', views.session_view, name='session'),
    path('subscription-plans', views.plans_view, name='plans'),
    path('subscriptions', views.subscriptions_view, name='subscriptions'),
    path('my-subscriptions', views.my_subscriptions_view, name='my-subscriptions'),
]
