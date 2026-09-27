from django.contrib import admin

from .models import CampusLocation, Category, Favorite, Item, ItemImage


class ItemImageInline(admin.TabularInline):
    model = ItemImage
    extra = 3


@admin.register(Category)
class CategoryAdmin(admin.ModelAdmin):
    list_display = ('name', 'description')
    search_fields = ('name',)


@admin.register(CampusLocation)
class CampusLocationAdmin(admin.ModelAdmin):
    list_display = ('name', 'building', 'address', 'is_active', 'sort_order')
    list_filter = ('is_active', 'building')
    search_fields = ('name', 'building', 'address')
    list_editable = ('is_active', 'sort_order')


@admin.register(Item)
class ItemAdmin(admin.ModelAdmin):
    list_display = ('title', 'price', 'category', 'location', 'seller', 'status', 'created_at')
    list_filter = ('status', 'category', 'location', 'created_at')
    search_fields = ('title', 'description', 'seller__username', 'location__name')
    date_hierarchy = 'created_at'
    inlines = [ItemImageInline]


@admin.register(Favorite)
class FavoriteAdmin(admin.ModelAdmin):
    list_display = ('user', 'item', 'created_at')
    list_filter = ('created_at',)
    search_fields = ('user__username', 'item__title')
