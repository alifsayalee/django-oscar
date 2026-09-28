from django.contrib import admin

from .models import MaxioWrite


@admin.register(MaxioWrite)
class MaxioWriteAdmin(admin.ModelAdmin):  # type: ignore[type-arg]
    list_display = ('reference', 'kind', 'user', 'plan_handle', 'outcome',
                    'provider_id', 'provider_state', 'claimed_at')
    list_filter = ('kind', 'outcome')
    search_fields = ('reference', 'provider_id', 'user__email')
    readonly_fields = [f.name for f in MaxioWrite._meta.fields]
