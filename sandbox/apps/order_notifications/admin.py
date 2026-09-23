from django.contrib import admin

from .models import ContactNumber, Notification


@admin.register(Notification)
class NotificationAdmin(admin.ModelAdmin):
    list_display = ("id", "order", "kind", "outcome", "provider_sid", "created_at")
    list_filter = ("kind", "outcome")
    readonly_fields = [f.name for f in Notification._meta.fields]


@admin.register(ContactNumber)
class ContactNumberAdmin(admin.ModelAdmin):
    list_display = ("id", "user", "country_code", "created_at", "removed_at")
    exclude = ("phone_number",)
