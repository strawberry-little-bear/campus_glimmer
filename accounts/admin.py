# accounts/admin.py
from django.contrib import admin
from .models import Profile

@admin.register(Profile)
class ProfileAdmin(admin.ModelAdmin):
    list_display = ('user', 'student_id', 'wechat', 'phone')
    search_fields = ('user__username', 'student_id', 'wechat', 'phone')