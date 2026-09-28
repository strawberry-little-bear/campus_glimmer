# accounts/admin.py
from django.contrib import admin
from .models import CampusDomain, CampusVerification, Profile

@admin.register(Profile)
class ProfileAdmin(admin.ModelAdmin):
    list_display = ('user', 'student_id', 'wechat', 'phone')
    search_fields = ('user__username', 'student_id', 'wechat', 'phone')

@admin.register(CampusDomain)
class CampusDomainAdmin(admin.ModelAdmin):
    list_display = ('name', 'domain', 'is_active', 'created_at')
    list_filter = ('is_active',)
    search_fields = ('name', 'domain')
    readonly_fields = ('created_at',)


@admin.register(CampusVerification)
class CampusVerificationAdmin(admin.ModelAdmin):
    list_display = ('user', 'campus_email', 'domain_name', 'status', 'verified_at', 'updated_at')
    list_filter = ('status', 'domain_name', 'updated_at')
    search_fields = ('user__username', 'campus_email', 'domain_name')
    autocomplete_fields = ('user',)
    readonly_fields = ('token_hash', 'token_issued_at', 'verified_at', 'created_at', 'updated_at')
