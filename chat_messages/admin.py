# messages/admin.py
from django.contrib import admin
from .models import Comment, ModerationAppeal, ModerationEvent, PrivateMessage

@admin.register(Comment)
class CommentAdmin(admin.ModelAdmin):
    list_display = ('author', 'item', 'content', 'created_at')
    list_filter = ('created_at',)
    search_fields = ('content', 'author__username', 'item__title')
    date_hierarchy = 'created_at'

@admin.register(PrivateMessage)
class PrivateMessageAdmin(admin.ModelAdmin):
    list_display = ('sender', 'receiver', 'content', 'created_at', 'is_read')
    list_filter = ('created_at', 'is_read')
    search_fields = ('content', 'sender__username', 'receiver__username')
    date_hierarchy = 'created_at'

@admin.register(ModerationEvent)
class ModerationEventAdmin(admin.ModelAdmin):
    list_display = ('channel', 'author', 'item', 'receiver', 'risk_tier', 'risk_level', 'risk_score', 'status', 'reviewed_by', 'released_at', 'created_at')
    list_filter = ('channel', 'risk_tier', 'risk_level', 'status', 'released_at', 'created_at', 'reviewed_at')
    search_fields = ('content', 'matched_terms', 'author__username', 'item__title', 'receiver__username')
    autocomplete_fields = ('author', 'item', 'receiver', 'reviewed_by')
    readonly_fields = ('created_at', 'reviewed_at', 'released_at', 'risk_tier')
    fieldsets = (
        ('拦截内容', {'fields': ('channel', 'author', 'item', 'receiver', 'risk_tier', 'risk_level', 'risk_score', 'content', 'matched_terms', 'created_at')}),
        ('复核结果', {'fields': ('status', 'reviewed_by', 'reviewed_at', 'released_at')}),
    )


@admin.register(ModerationAppeal)
class ModerationAppealAdmin(admin.ModelAdmin):
    list_display = (
        'event', 'appellant', 'reason', 'status', 'original_reviewed_by',
        'original_reviewed_at', 'resolved_by', 'resolved_at', 'submitted_at',
    )
    list_filter = ('status', 'reason', 'submitted_at', 'resolved_at')
    search_fields = ('statement', 'resolution_note', 'appellant__username')
    autocomplete_fields = ('appellant', 'original_reviewed_by', 'resolved_by')
    actions = None
    readonly_fields = (
        'event', 'appellant', 'reason', 'statement', 'submitted_at', 'status',
        'original_reviewed_by', 'original_reviewed_at', 'resolved_by',
        'resolved_at', 'resolution_note',
    )
    fieldsets = (
        ('申诉内容', {'fields': ('event', 'appellant', 'reason', 'statement', 'submitted_at')}),
        ('原复核快照', {'fields': ('original_reviewed_by', 'original_reviewed_at')}),
        ('处理结果', {'fields': ('status', 'resolved_by', 'resolved_at', 'resolution_note')}),
    )

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False
