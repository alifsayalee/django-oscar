"""URL routing for the PayPal payments + saved-cards API.

Each capability is its own route; there is no do-everything endpoint.
"""

from django.urls import path

from . import views

app_name = 'api'

urlpatterns = [
    # Flow 1 — pay for an order
    path('orders', views.create_order, name='create-order'),
    path('orders/<int:order_id>/pay', views.pay_order, name='pay-order'),
    path('orders/<int:order_id>/fulfil', views.fulfil_order, name='fulfil-order'),
    path('orders/<int:order_id>/cancel', views.cancel_order, name='cancel-order'),
    path('orders/<int:order_id>/refunds', views.refund_order, name='refund-order'),
    path('my-orders', views.my_orders, name='my-orders'),
    path('reconciliation', views.reconciliation, name='reconciliation'),

    # Flow 2 — saved cards
    path('payment-methods', views.payment_methods, name='payment-methods'),
    path('payment-methods/<int:method_id>', views.delete_payment_method,
         name='delete-payment-method'),
]
