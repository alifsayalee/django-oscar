from django.urls import path

from . import views

app_name = 'subscriptions'

urlpatterns = [
    path('session', views.session, name='session'),
    path('subscription-plans', views.subscription_plans, name='plans'),
    path('maxio-customer', views.maxio_customer, name='customer'),
    path('subscriptions', views.subscriptions, name='subscribe'),
    path('subscriptions/<int:subscription_id>', views.subscription_detail, name='detail'),
    path('my-subscriptions', views.my_subscriptions, name='mine'),
]
