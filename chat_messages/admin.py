# messages/admin.py
from django.contrib import admin
from .models import Comment, ModerationEvent, PrivateMessage

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
    list_display = ('channel', 'author', 'item', 'risk_level', 'risk_score', 'matched_terms', 'status', 'reviewed_by', 'created_at')
    list_filter = ('channel', 'risk_level', 'status', 'created_at', 'reviewed_at')
    search_fields = ('content', 'matched_terms', 'author__username', 'item__title')
    autocomplete_fields = ('author', 'item', 'reviewed_by')
    readonly_fields = ('created_at', 'reviewed_at')
    fieldsets = (
        ('拦截内容', {'fields': ('channel', 'author', 'item', 'risk_level', 'risk_score', 'content', 'matched_terms', 'created_at')}),
        ('复核结果', {'fields': ('status', 'reviewed_by', 'reviewed_at')}),
    )
