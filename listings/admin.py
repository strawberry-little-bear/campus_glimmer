from django.contrib import admin

from .models import BrowsingHistory, CampusLocation, Category, DemandPost, DemandResponse, DeliveryConfirmation, Favorite, GiftApplication, Item, ItemAvailabilityWatch, ItemImage, MeetingAppointment, MeetingIncident, Notification, NotificationPreference, Order, OrderDispute, OrderDisputeEvidence, OrderEvent, Rating, RecommendationFeedback, Report, SavedSearch, SearchClick, SearchImpression, SearchQuery, SearchSynonym


class ItemImageInline(admin.TabularInline):
    model = ItemImage
    extra = 3


@admin.register(Category)
class CategoryAdmin(admin.ModelAdmin):
    list_display = ('name', 'description')
    search_fields = ('name',)


@admin.register(CampusLocation)
class CampusLocationAdmin(admin.ModelAdmin):
    list_display = ('name', 'building', 'address', 'is_public', 'is_active', 'sort_order')
    list_filter = ('is_active', 'is_public', 'building')
    search_fields = ('name', 'building', 'address')
    list_editable = ('is_public', 'is_active', 'sort_order')


@admin.register(Item)
class ItemAdmin(admin.ModelAdmin):
    list_display = ('title', 'trade_mode', 'price', 'category', 'location', 'seller', 'status', 'expires_at', 'report_count', 'created_at')
    list_filter = ('trade_mode', 'status', 'category', 'location', 'created_at', 'expires_at')
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
    list_display = ('item', 'reporter', 'reason', 'status', 'reviewer', 'created_at', 'reviewed_at')
    list_filter = ('status', 'reason', 'created_at')
    search_fields = ('item__title', 'reporter__username', 'detail', 'review_note')
    autocomplete_fields = ('item', 'reporter', 'reviewer')
    readonly_fields = ('created_at', 'updated_at', 'reviewed_at')
    fieldsets = (
        ('举报信息', {'fields': ('item', 'reporter', 'reason', 'detail', 'created_at')}),
        ('审核结果', {'fields': ('status', 'reviewer', 'review_note', 'reviewed_at', 'updated_at')}),
    )


@admin.register(GiftApplication)
class GiftApplicationAdmin(admin.ModelAdmin):
    list_display = ('item', 'applicant', 'status', 'meeting_location', 'created_at', 'decided_at')
    list_filter = ('status', 'meeting_location', 'created_at', 'decided_at')
    search_fields = ('item__title', 'applicant__username', 'applicant_note')
    autocomplete_fields = ('item', 'applicant', 'meeting_location', 'order')
    readonly_fields = ('created_at', 'updated_at', 'decided_at')


@admin.register(Order)
class OrderAdmin(admin.ModelAdmin):
    list_display = ('item', 'buyer', 'seller', 'agreed_price', 'status', 'meeting_location', 'confirmation_deadline', 'created_at')
    list_filter = ('status', 'meeting_location', 'confirmation_deadline', 'created_at')
    search_fields = ('item__title', 'buyer__username', 'seller__username')
    autocomplete_fields = ('item', 'buyer', 'seller', 'meeting_location')
    readonly_fields = ('agreed_price', 'created_at', 'updated_at', 'confirmation_reminder_sent_at')


@admin.register(MeetingAppointment)
class MeetingAppointmentAdmin(admin.ModelAdmin):
    list_display = ('order', 'start_at', 'end_at', 'location', 'status', 'proposed_by', 'responded_by')
    list_filter = ('status', 'location', 'start_at')
    search_fields = ('order__item__title', 'proposed_by__username', 'responded_by__username')
    autocomplete_fields = ('order', 'proposed_by', 'responded_by', 'location')
    readonly_fields = ('created_at', 'updated_at', 'responded_at', 'buyer_arrived_at', 'seller_arrived_at')


@admin.register(MeetingIncident)
class MeetingIncidentAdmin(admin.ModelAdmin):
    list_display = ('appointment', 'reported_by', 'accused', 'reason', 'status', 'reviewer', 'created_at')
    list_filter = ('reason', 'status', 'created_at')
    search_fields = ('appointment__order__item__title', 'reported_by__username', 'accused__username', 'detail')
    autocomplete_fields = ('appointment', 'reported_by', 'accused', 'reviewer')
    readonly_fields = ('created_at', 'updated_at', 'reviewed_at')


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


@admin.register(NotificationPreference)
class NotificationPreferenceAdmin(admin.ModelAdmin):
    list_display = ('user', 'order_created', 'order_status', 'meeting_incident', 'message_received', 'saved_search_match', 'item_available', 'updated_at')
    list_filter = ('order_created', 'order_status', 'meeting_incident', 'message_received', 'saved_search_match', 'item_available', 'updated_at')
    search_fields = ('user__username',)
    readonly_fields = ('updated_at',)


@admin.register(Notification)
class NotificationAdmin(admin.ModelAdmin):
    list_display = ('recipient', 'kind', 'title', 'is_read', 'actor', 'item', 'demand', 'created_at')
    list_filter = ('kind', 'is_read', 'created_at')
    search_fields = ('recipient__username', 'actor__username', 'title', 'message')
    autocomplete_fields = ('recipient', 'actor', 'order', 'item', 'demand')
    readonly_fields = ('created_at',)


@admin.register(SearchQuery)
class SearchQueryAdmin(admin.ModelAdmin):
    list_display = ('query', 'condition', 'user', 'category', 'location', 'min_price', 'max_price', 'result_count', 'created_at')
    list_filter = ('category', 'location', 'created_at')
    search_fields = ('query', 'condition', 'user__username')
    autocomplete_fields = ('user', 'category', 'location')
    readonly_fields = ('created_at',)


@admin.register(SavedSearch)
class SavedSearchAdmin(admin.ModelAdmin):
    list_display = ('name', 'user', 'query', 'category', 'location', 'min_price', 'max_price', 'is_active', 'created_at')
    list_filter = ('is_active', 'category', 'location', 'created_at')
    search_fields = ('name', 'query', 'condition', 'user__username')
    autocomplete_fields = ('user', 'category', 'location')
    readonly_fields = ('created_at', 'updated_at')


@admin.register(DeliveryConfirmation)
class DeliveryConfirmationAdmin(admin.ModelAdmin):
    list_display = ('order', 'buyer_confirmed_at', 'seller_confirmed_at', 'handoff_code_status', 'handoff_code_attempts', 'updated_at')
    list_filter = ('updated_at',)
    search_fields = ('order__item__title', 'order__buyer__username', 'order__seller__username')
    autocomplete_fields = ('order',)
    readonly_fields = ('created_at', 'updated_at', 'handoff_code_hash', 'handoff_code_issued_at', 'handoff_code_used_at')

    @admin.display(description='确认码状态')
    def handoff_code_status(self, obj):
        if not obj.handoff_code_hash:
            return '未生成'
        if obj.handoff_code_used_at:
            return '已使用'
        if obj.handoff_code_attempts >= 5:
            return '已锁定'
        return f'有效（{obj.handoff_code_hint}）'


@admin.register(OrderDispute)
class OrderDisputeAdmin(admin.ModelAdmin):
    list_display = ('order', 'opened_by', 'reason', 'status', 'reviewer', 'created_at', 'resolved_at')
    list_filter = ('status', 'reason', 'created_at', 'resolved_at')
    search_fields = ('order__item__title', 'opened_by__username', 'detail', 'resolution_note')
    autocomplete_fields = ('order', 'opened_by', 'reviewer')
    readonly_fields = ('created_at', 'updated_at', 'resolved_at')


@admin.register(OrderDisputeEvidence)
class OrderDisputeEvidenceAdmin(admin.ModelAdmin):
    list_display = ('dispute', 'uploaded_by', 'attachment_name', 'created_at')
    list_filter = ('created_at',)
    search_fields = ('dispute__order__item__title', 'uploaded_by__username', 'note')
    autocomplete_fields = ('dispute', 'uploaded_by')
    readonly_fields = ('created_at',)

    @admin.display(description='文件')
    def attachment_name(self, obj):
        return obj.filename


@admin.register(RecommendationFeedback)
class RecommendationFeedbackAdmin(admin.ModelAdmin):
    list_display = ('user', 'item', 'action', 'updated_at')
    list_filter = ('action', 'updated_at')
    search_fields = ('user__username', 'item__title')
    autocomplete_fields = ('user', 'item')
    readonly_fields = ('created_at', 'updated_at')


@admin.register(ItemAvailabilityWatch)
class ItemAvailabilityWatchAdmin(admin.ModelAdmin):
    list_display = ('item', 'user', 'created_at')
    list_filter = ('created_at',)
    search_fields = ('item__title', 'user__username')
    autocomplete_fields = ('item', 'user')
    readonly_fields = ('created_at',)


@admin.register(SearchClick)
class SearchClickAdmin(admin.ModelAdmin):
    list_display = ('search_query', 'item', 'user', 'position', 'created_at')
    list_filter = ('created_at',)
    search_fields = ('search_query__query', 'item__title', 'user__username')
    date_hierarchy = 'created_at'


@admin.register(SearchImpression)
class SearchImpressionAdmin(admin.ModelAdmin):
    list_display = ('search_query', 'item', 'user', 'position', 'created_at')
    list_filter = ('created_at',)
    search_fields = ('search_query__query', 'item__title', 'user__username')
    date_hierarchy = 'created_at'


@admin.register(SearchSynonym)
class SearchSynonymAdmin(admin.ModelAdmin):
    list_display = ('keyword', 'synonym', 'is_active', 'updated_at')
    list_filter = ('is_active',)
    search_fields = ('keyword', 'synonym')
    list_editable = ('is_active',)


@admin.register(DemandPost)
class DemandPostAdmin(admin.ModelAdmin):
    list_display = ('title', 'requester', 'category', 'location', 'budget_display', 'status', 'view_count', 'expires_at', 'created_at')
    list_filter = ('status', 'category', 'location', 'created_at')
    search_fields = ('title', 'description', 'requester__username')
    autocomplete_fields = ('requester', 'category', 'location')
    readonly_fields = ('view_count', 'created_at', 'updated_at')
    date_hierarchy = 'created_at'

    @admin.display(description='预算')
    def budget_display(self, obj):
        if obj.min_price is not None and obj.max_price is not None:
            return f'¥{obj.min_price} - ¥{obj.max_price}'
        if obj.max_price is not None:
            return f'不超过 ¥{obj.max_price}'
        if obj.min_price is not None:
            return f'不低于 ¥{obj.min_price}'
        return '未填写'


@admin.register(DemandResponse)
class DemandResponseAdmin(admin.ModelAdmin):
    list_display = ('demand', 'item', 'responder', 'match_score', 'status', 'created_at', 'updated_at')
    list_filter = ('status', 'created_at', 'updated_at')
    search_fields = ('demand__title', 'item__title', 'responder__username', 'message')
    autocomplete_fields = ('demand', 'item', 'responder')
    readonly_fields = ('match_score', 'match_reason', 'created_at', 'updated_at')
