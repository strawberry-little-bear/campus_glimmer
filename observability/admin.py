from django.contrib import admin

from .models import AlertTransition


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