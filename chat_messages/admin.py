# messages/admin.py
from django.contrib import admin
from .models import Comment, PrivateMessage

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