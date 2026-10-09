from django.contrib import admin

from .models import AcademicPhase, AcademicTerm, AcademicTermSnapshot, BrowsingHistory, CampusCampaign, CampusLocation, Category, CommunityContribution, DemandOpportunityTask, DemandPost, DemandResponse, DeliveryConfirmation, Favorite, FavoriteCollection, GiftApplication, Item, ItemAvailabilityWatch, ItemImage, LostFoundLead, LostFoundPost, MeetingAppointment, MeetingIncident, MutualAidFeedback, Notification, NotificationPreference, OpportunityDismissal, Order, OrderDispute, OrderDisputeEvidence, OrderEvent, Rating, RecommendationFeedback, Report, SavedSearch, SavedSearchMatch, SearchClick, SearchImpression, SearchQuery, SearchSynonym


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


@admin.register(CampusCampaign)
class CampusCampaignAdmin(admin.ModelAdmin):
    list_display = ('title', 'slug', 'starts_at', 'ends_at', 'is_active', 'item_count')
    list_filter = ('is_active', 'starts_at', 'ends_at')
    search_fields = ('title', 'slug', 'description')
    prepopulated_fields = {'slug': ('title',)}

    @admin.display(description='商品数')
    def item_count(self, obj):
        return obj.items.filter(status='available').count()


@admin.register(Item)
class ItemAdmin(admin.ModelAdmin):
    list_display = ('title', 'trade_mode', 'price', 'category', 'location', 'campaign', 'seller', 'status', 'expires_at', 'report_count', 'last_refreshed_at', 'refresh_count', 'created_at')
    list_filter = ('trade_mode', 'status', 'category', 'location', 'campaign', 'created_at', 'expires_at', 'last_refreshed_at')
    search_fields = ('title', 'description', 'seller__username', 'location__name')
    date_hierarchy = 'created_at'
    inlines = [ItemImageInline]

    @admin.display(description='举报数')
    def report_count(self, obj):
        return obj.reports.count()


@admin.register(FavoriteCollection)
class FavoriteCollectionAdmin(admin.ModelAdmin):
    list_display = ('name', 'user', 'favorite_count', 'created_at', 'updated_at')
    search_fields = ('name', 'user__username')
    list_filter = ('created_at', 'updated_at')

    @admin.display(description='收藏数')
    def favorite_count(self, obj):
        return obj.favorites.count()


@admin.register(Favorite)
class FavoriteAdmin(admin.ModelAdmin):
    list_display = ('user', 'item', 'collection', 'created_at')
    list_filter = ('collection', 'created_at')
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


@admin.register(MutualAidFeedback)
class MutualAidFeedbackAdmin(admin.ModelAdmin):
    list_display = ('source_label', 'helper', 'submitted_by', 'outcome', 'created_at')
    list_filter = ('outcome', 'created_at')
    search_fields = (
        'submitted_by__username', 'demand_response__responder__username',
        'lost_found_lead__respondent__username', 'note',
    )
    autocomplete_fields = ('demand_response', 'lost_found_lead', 'submitted_by')
    readonly_fields = ('created_at', 'updated_at')

    @admin.display(description='互助来源')
    def source_label(self, obj):
        source = obj.source
        return source.demand.title if obj.demand_response_id else source.post.title

    @admin.display(description='帮助者')
    def helper(self, obj):
        return obj.helper.username


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
    list_display = ('name', 'user', 'notify_frequency', 'max_matches_per_notice', 'quiet_until', 'is_active', 'created_at')
    list_filter = ('is_active', 'notify_frequency', 'category', 'location', 'created_at')
    search_fields = ('name', 'query', 'condition', 'user__username')
    autocomplete_fields = ('user', 'category', 'location')
    readonly_fields = ('created_at', 'updated_at')


@admin.register(SavedSearchMatch)
class SavedSearchMatchAdmin(admin.ModelAdmin):
    list_display = ('saved_search', 'item', 'matched_at', 'notified_at')
    list_filter = ('notified_at', 'matched_at')
    search_fields = ('saved_search__name', 'item__title')
    autocomplete_fields = ('saved_search', 'item')
    readonly_fields = ('matched_at', 'notified_at')


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


@admin.register(OpportunityDismissal)
class OpportunityDismissalAdmin(admin.ModelAdmin):
    list_display = ('user', 'kind', 'demand', 'lost_found_post', 'expires_at', 'created_at')
    list_filter = ('kind', 'expires_at', 'created_at')
    search_fields = ('user__username', 'demand__title', 'lost_found_post__title')
    autocomplete_fields = ('user', 'demand', 'lost_found_post')
    readonly_fields = ('created_at',)


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


@admin.register(DemandOpportunityTask)
class DemandOpportunityTaskAdmin(admin.ModelAdmin):
    list_display = (
        'title', 'level', 'opportunity_score', 'status', 'assigned_to',
        'available_supply', 'created_at', 'updated_at',
    )
    list_filter = ('status', 'level', 'category', 'location')
    search_fields = ('title', 'radar_key', 'note')
    autocomplete_fields = ('category', 'location', 'created_by', 'assigned_to')
    readonly_fields = (
        'radar_key', 'opportunity_score', 'search_count', 'demand_count',
        'available_supply', 'evidence', 'recommendations', 'created_by',
        'created_at', 'updated_at',
    )


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


@admin.register(LostFoundPost)
class LostFoundPostAdmin(admin.ModelAdmin):
    list_display = ('title', 'post_type', 'category', 'location', 'reporter', 'status', 'occurred_at', 'view_count', 'created_at')
    list_filter = ('post_type', 'status', 'category', 'location', 'created_at', 'occurred_at')
    search_fields = ('title', 'description', 'identifying_features', 'reporter__username', 'location__name')
    autocomplete_fields = ('reporter', 'category', 'location', 'matched_post')
    readonly_fields = ('view_count', 'created_at', 'updated_at')


@admin.register(LostFoundLead)
class LostFoundLeadAdmin(admin.ModelAdmin):
    list_display = ('post', 'respondent', 'related_post', 'status', 'created_at', 'updated_at')
    list_filter = ('status', 'created_at', 'updated_at')
    search_fields = ('post__title', 'respondent__username', 'message', 'related_post__title')
    autocomplete_fields = ('post', 'respondent', 'related_post')
    readonly_fields = ('created_at', 'updated_at')


@admin.register(CommunityContribution)
class CommunityContributionAdmin(admin.ModelAdmin):
    list_display = ('user', 'kind', 'points', 'title', 'occurred_at', 'source_key')
    list_filter = ('kind', 'occurred_at')
    search_fields = ('user__username', 'title', 'description', 'source_key')
    autocomplete_fields = ('user',)
    readonly_fields = ('created_at',)


class AcademicPhaseInline(admin.TabularInline):
    model = AcademicPhase
    extra = 3
    fields = ('phase', 'start_offset', 'end_offset', 'note')


@admin.register(AcademicTerm)
class AcademicTermAdmin(admin.ModelAdmin):
    list_display = ('name', 'slug', 'kind', 'starts_on', 'ends_on', 'is_active', 'phase_count')
    list_filter = ('kind', 'is_active', 'starts_on')
    search_fields = ('name', 'slug')
    prepopulated_fields = {'slug': ('name',)}
    inlines = [AcademicPhaseInline]

    @admin.display(description='阶段数')
    def phase_count(self, obj):
        return obj.phases.count()


@admin.register(AcademicTermSnapshot)
class AcademicTermSnapshotAdmin(admin.ModelAdmin):
    list_display = (
        'term', 'phase_label', 'start_date', 'end_date', 'day_count',
        'new_items', 'new_demands', 'new_orders', 'completed_orders',
        'new_searches', 'zero_result_searches', 'refreshed_at',
    )
    list_filter = ('phase_key', 'term')
    search_fields = ('term__name', 'phase_label')
    readonly_fields = ('refreshed_at',)
    date_hierarchy = 'start_date'
