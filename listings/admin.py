from django.contrib import admin

from .models import BrowsingHistory, CampusLocation, Category, Favorite, Item, ItemImage, Notification, Order, OrderEvent, Rating, Report


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
    list_display = ('title', 'price', 'category', 'location', 'seller', 'status', 'report_count', 'created_at')
    list_filter = ('status', 'category', 'location', 'created_at')
    search_fields = ('title', 'description', 'seller__username', 'location__name')
    date_hierarchy = 'created_at'
    inlines = [ItemImageInline]

    @admin.display(description='举报数')
    def report_count(self, obj):
        return obj.reports.count()


@admin.register(Favorite)
class FavoriteAdmin(admin.ModelAdmin):
    list_display = ('user', 'item', 'created_at')
    list_filter = ('created_at',)
    search_fields = ('user__username', 'item__title')


@admin.register(Report)
class ReportAdmin(admin.ModelAdmin):
    list_display = ('item', 'reporter', 'reason', 'status', 'reviewer', 'created_at', 'updated_at')
    list_filter = ('status', 'reason', 'created_at')
    search_fields = ('item__title', 'reporter__username', 'detail', 'review_note')
    autocomplete_fields = ('item', 'reporter', 'reviewer')
    readonly_fields = ('created_at', 'updated_at')
    fieldsets = (
        ('举报信息', {'fields': ('item', 'reporter', 'reason', 'detail', 'created_at')}),
        ('审核结果', {'fields': ('status', 'reviewer', 'review_note', 'updated_at')}),
    )


@admin.register(Order)
class OrderAdmin(admin.ModelAdmin):
    list_display = ('item', 'buyer', 'seller', 'agreed_price', 'status', 'meeting_location', 'created_at', 'updated_at')
    list_filter = ('status', 'meeting_location', 'created_at')
    search_fields = ('item__title', 'buyer__username', 'seller__username')
    autocomplete_fields = ('item', 'buyer', 'seller', 'meeting_location')
    readonly_fields = ('agreed_price', 'created_at', 'updated_at')


@admin.register(BrowsingHistory)
class BrowsingHistoryAdmin(admin.ModelAdmin):
    list_display = ('user', 'item', 'view_count', 'first_viewed_at', 'last_viewed_at')
    list_filter = ('last_viewed_at',)
    search_fields = ('user__username', 'item__title')
    autocomplete_fields = ('user', 'item')
    readonly_fields = ('first_viewed_at', 'last_viewed_at')


@admin.register(Rating)
class RatingAdmin(admin.ModelAdmin):
    list_display = ('order', 'rater', 'ratee', 'score', 'comment_preview', 'created_at')
    list_filter = ('score', 'created_at')
    search_fields = ('order__item__title', 'rater__username', 'ratee__username', 'comment')
    autocomplete_fields = ('order', 'rater', 'ratee')
    readonly_fields = ('created_at',)

    @admin.display(description='评价内容')
    def comment_preview(self, obj):
        if not obj.comment:
            return '—'
        return obj.comment[:36] + ('…' if len(obj.comment) > 36 else '')


@admin.register(OrderEvent)
class OrderEventAdmin(admin.ModelAdmin):
    list_display = ('order', 'from_status', 'to_status', 'actor', 'note', 'created_at')
    list_filter = ('to_status', 'created_at')
    search_fields = ('order__item__title', 'actor__username', 'note')
    autocomplete_fields = ('order', 'actor')
    readonly_fields = ('created_at',)


@admin.register(Notification)
class NotificationAdmin(admin.ModelAdmin):
    list_display = ('recipient', 'kind', 'title', 'is_read', 'actor', 'created_at')
    list_filter = ('kind', 'is_read', 'created_at')
    search_fields = ('recipient__username', 'actor__username', 'title', 'message')
    autocomplete_fields = ('recipient', 'actor', 'order', 'item')
    readonly_fields = ('created_at',)
