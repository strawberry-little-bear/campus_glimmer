from django.contrib import admin

from .models import AlertNotificationDelivery, AlertTransition


@admin.register(AlertTransition)
class AlertTransitionAdmin(admin.ModelAdmin):
    list_display = ('occurred_at', 'event', 'rule_name', 'severity', 'instance')
    list_filter = ('event', 'severity')
    search_fields = ('rule_name', 'summary', 'detail', 'instance')
    ordering = ('-occurred_at', '-id')
    readonly_fields = tuple(field.name for field in AlertTransition._meta.fields)

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(AlertNotificationDelivery)
class AlertNotificationDeliveryAdmin(admin.ModelAdmin):
    list_display = (
        'created_at', 'rule_name', 'event', 'status', 'attempts', 'next_attempt_at',
    )
    list_filter = ('status', 'event', 'rule_name')
    search_fields = ('rule_name', 'last_error', 'dedupe_key')
    ordering = ('-created_at', '-id')
    readonly_fields = tuple(field.name for field in AlertNotificationDelivery._meta.fields)

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False
