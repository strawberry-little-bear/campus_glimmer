import csv
import logging
import mimetypes
import secrets
from time import perf_counter
from decimal import Decimal, InvalidOperation
from pathlib import Path
from datetime import datetime, timedelta
from math import ceil
from urllib.parse import urlencode

from django.contrib.auth.decorators import login_required
from django.contrib.auth.hashers import check_password, make_password
from django.contrib.auth.models import User
from django.contrib import messages
from django.core.paginator import Paginator
from django.core.exceptions import PermissionDenied
from django.http import FileResponse, Http404, HttpResponse, JsonResponse
from django.db import IntegrityError, transaction
from django.db.models import Avg, Case, Count, ExpressionWrapper, F, FloatField, IntegerField, OuterRef, Q, Subquery, Value, When
from django.db.models.functions import Cast, Coalesce
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.utils.dateparse import parse_datetime
from django.utils.http import url_has_allowed_host_and_scheme
from django.views.decorators.http import require_GET, require_POST

from .analytics import PERIOD_CHOICES, build_operations_dashboard, build_search_insights
from .campaign_phase_review import build_campaign_phase_review
from .campaign_registration import (
    CampaignRegistrationClosed, join_campaign, leave_campaign,
)
from .campaign_updates import (
    CampaignUpdateSubscriptionUnavailable, queue_campaign_update_matches,
    set_campaign_update_preference,
)
from .bulk_listing_csv import BulkListingCsvParseResult, parse_bulk_listing_csv
from .bulk_listing_template import build_bulk_listing_import_template
from .bulk_listing_xlsx import parse_bulk_listing_xlsx
from .bulk_listing_import_summary import build_bulk_listing_import_error_summary
from .dashboard_build_performance import build_dashboard_build_performance
from .operations_fragments import section_needs_core, section_panel_keys, section_partial
from .operations_nav import OPERATIONS_DASHBOARD_FIRST_SCREEN, OPERATIONS_DASHBOARD_SECTIONS
# 搜索洞察这两个助手必须起别名：operations_fragments 里有一对同名函数
# （section_panel_keys / section_partial），同名导入会把看板取回端点正在用的
# 那个悄悄盖掉。症状是看板每个延后分区都变成空态，而页面其它部分完全正常——
# listings.test_operations_fragments 的等价性测试就是抓这个的。
from .search_insights_fragments import (
    SEARCH_INSIGHTS_FIRST_SCREEN,
    section_core_parts,
    section_panel_keys as search_insights_panel_keys,
)
from .search_insights_fragments import section_partial as search_insights_partial
from .lifecycle_reminder_term_trend import build_lifecycle_reminder_term_trend_dashboard
from .search_combo_shift import build_search_combo_shift
from .search_threshold_feedback import build_search_threshold_feedback
from .search_trend import build_search_trend
from .task_run_ledger_integrity import build_task_run_ledger_integrity
from .governance_sla import build_governance_sla
from .demand_radar import build_demand_radar
from .search_rejection_recurrence import build_search_rejection_recurrence
from .search_rejection_stats import build_search_rejection_stats
from .search_position_quality import build_search_position_quality
from .search_impression_pairing_quality import build_search_impression_pairing_quality
from .search_exposure_cold_start import build_search_exposure_cold_start
from .demand_match_quality import build_demand_match_quality
from .mutual_aid_feedback_notes import build_mutual_aid_feedback_notes
from .demand_opportunity_followups import build_demand_opportunity_followups
from .demand_task_aging import build_demand_task_aging
from .notification_snooze_return import build_notification_snooze_return
from .notification_kind_coverage import build_notification_kind_coverage
from .notification_preference_usage import build_notification_preference_usage
from .notification_preference_bypass_diagnosis import build_notification_preference_bypass_diagnosis
from .notification_quiet_hours_shape import build_notification_quiet_hours_shape
from .notification_aggregation_quality import build_notification_aggregation_quality
from .report_priority import (
    build_report_page, build_report_priority_for_report, build_report_queue,
)
from .report_resolution_quality import build_report_resolution_quality
from .meeting_incident_quality import build_meeting_incident_quality
from .meeting_attendance import build_meeting_attendance
from .meeting_reschedule import build_meeting_reschedule
from .meeting_service_window_quality import build_meeting_service_window_quality
from .campus_location_service_rule_quality import build_campus_location_service_rule_quality
from .refresh_quota_drift import build_refresh_quota_drift
from .item_refresh_event_ledger import build_item_refresh_event_ledger_dashboard
from .refresh_response import build_refresh_response
from .price_response import build_price_response
from .category_price_reference import build_category_price_reference
from .relist_freshness import build_relist_freshness
from .handoff_code_usage import (
    NOISE_POINTS as HANDOFF_NOISE_POINTS, build_handoff_code_usage,
)
from .return_handshake import (
    NOISE_POINTS as RETURN_HANDSHAKE_NOISE_POINTS, build_return_handshake,
)
from .delivery_handshake import (
    NOISE_POINTS as DELIVERY_HANDSHAKE_NOISE_POINTS, build_delivery_handshake,
)
from .first_viewed import (
    NOISE_POINTS as FIRST_VIEWED_NOISE_POINTS, build_first_viewed,
)
from .saved_search_latency import build_saved_search_latency
from .saved_search_limit_shape import build_saved_search_limit_shape
from .saved_search_quiet_window import build_saved_search_quiet_window
from .lost_found_matched_outcome import build_lost_found_matched_outcome
from .campus_verification_friction import build_campus_verification_friction
from .gift_application_queue import build_gift_application_queue
# 商品图片覆盖度：在架商品到底配了几张图，以前没人数过
from .item_image_coverage import build_item_image_coverage
from .location_registry_quality import build_location_registry_quality
from .location_maintenance_decision_review import build_location_maintenance_decision_review
from .location_maintenance_queue import build_location_maintenance_queue
# 订单承诺截止与催收效果：第八轮接面板时漏了这一行 import，看板整页 500。
# 测试当时只跑新模块自己的用例，没有渲染过看板，所以它一路绿着上了 main。
from .order_deadline_reminders import build_order_deadline_reminders
from .moderation_notice_preference import build_moderation_notice_preference
from .moderation_rule_quality import build_moderation_rule_quality
from .moderation_appeal_quality import build_moderation_appeal_quality
from .availability_watch_quality import build_availability_watch_quality
from .rating_quality import build_rating_quality
from .dispute_resolution_quality import build_dispute_resolution_quality
from .demand_response_conversion import (
    NOISE_POINTS as DEMAND_RESPONSE_NOISE_POINTS,
    build_demand_response_conversion,
)
from .lost_found_lead_quality import build_lost_found_lead_quality
from .search_rewrites import build_search_rewrite_candidates
from .search_synonym_effect import build_search_synonym_effects
from .demand_radar_outcome import build_demand_radar_outcomes
from .circular_impact import build_circular_impact_report
from .contributions import (
    build_contribution_summary, record_demand_response_contribution,
    record_lost_found_lead_contribution, record_mutual_aid_feedback_contribution,
    record_order_contribution,
)
# 积分等级与徽章的门槛形状。它和 contributions.py 读同一套规则表，import 排在它
# 后面，改表的人一眼能看出这里也跟着动。
from .contribution_tier_shape import build_contribution_tier_shape
from .contribution_milestone_coverage import build_contribution_milestone_coverage
from .contribution_milestone_phase_rhythm import build_contribution_milestone_phase_rhythm
from .availability import notify_item_available
from .borrow_days_shape import build_borrow_days_shape
from .dispute_evidence_shape import build_dispute_evidence_shape
from .borrow_escalation import close_escalation
from .demand_matching import _match_demand, notify_demand_matches
from .lost_found_matching import expire_lost_found_posts, find_lost_found_matches, score_lost_found_posts
from .forms import BulkListingRenewalForm, BulkListingRowFormSet, BulkListingSettingsForm, CampaignUpdateSubscriptionForm, DeliveryCodeForm, DemandPostForm, DisputeEvidenceForm, DisputeForm, DisputeResolutionForm, MeetingIncidentReviewForm, DemandResponseForm, FavoriteCollectionForm, ItemForm, ItemImageFormSet, LostFoundLeadForm, LostFoundPostForm, MeetingAppointmentForm, MeetingIncidentForm, MutualAidFeedbackForm, NotificationPreferenceForm, OrderForm, RatingForm, ReportForm, ReportReviewForm, SavedSearchForm
from .models import BrowsingHistory, CampusCampaign, CampaignRegistration, CampusLocation, Category, DemandOpportunityTask, DemandPost, DeliveryConfirmation, Favorite, FavoriteCollection, GiftApplication, Item, ItemAvailabilityWatch, ItemImage, ItemRefreshEvent, SemesterListingTemplate, LostFoundLead, LostFoundPost, MeetingAppointment, MeetingIncident, Notification, NotificationPreference, OpportunityDismissal, OpportunityImpression, DemandResponse, MutualAidFeedback, Order, OrderDispute, OrderDisputeEvidence, OrderEvent, OrderSafetyAcknowledgement, Rating, RecommendationFeedback, RecommendationImpression, Report, ReportOutcomeFeedback, SavedSearch, SavedSearchMatch, SearchClick, SearchImpression, SearchQuery, SearchSynonym, SearchSynonymRejection
from .image_processing import ItemImageLimitError, normalize_item_image
from .recommendations import get_recommendations
from .recommendation_impression_quality import build_recommendation_impression_quality
from .opportunity_impression_quality import build_opportunity_impression_quality
from .recommendation_quality import build_recommendation_quality
from .reputation import build_seller_reputation
from .notifications import (
    NOTIFICATION_SNOOZE_DURATIONS, NOTIFICATION_SNOOZE_OPTIONS,
    active_unread_notifications, actionable_unread_q,
    bulk_snooze_notifications, create_notification,
    normalize_notification_ids, quiet_hours_active,
    restore_snoozed_notifications,
)
from .opportunity_dismissal_quality import build_opportunity_dismissal_quality
from .favorite_collection_quality import build_favorite_collection_quality
from .academic_phase_supply_preparation import build_academic_phase_supply_preparation
from .academic_term_snapshot_quality import build_academic_term_snapshot_quality
from .opportunity_feed import build_opportunity_feed
from .price_insights import build_price_insight
from .price_changes import notify_item_price_drop, record_item_price_change
from .price_history import (
    build_item_price_history, build_item_price_history_maintenance,
    load_item_price_history_state,
)
from .campus_location_service_hours import evaluate_location_service_window
from .price_change_quality import build_price_change_quality_panel
from .lifecycle_diagnostics import _refresh_state, build_seller_lifecycle
from .meeting_scheduling import (
    find_appointment_conflicts,
    recommend_meeting_locations,
    recommend_meeting_times,
)
from .order_workflow import OrderTransitionError, transition_order
from .saved_searches import (
    matches_saved_search, notify_saved_search_matches,
)
from .saved_search_digest import deliver_buffered_saved_search_matches
from .transaction_safety import build_transaction_safety, transaction_safety_fingerprint
from .transaction_safety_acknowledgement_quality import (
    build_transaction_safety_acknowledgement_quality,
)
from chat_messages.models import ModerationEvent, PrivateMessage
from accounts.models import CampusVerification

logger = logging.getLogger(__name__)



def _favorite_ids(request):
    if not request.user.is_authenticated:
        return set()
    return set(Favorite.objects.filter(user=request.user).values_list('item_id', flat=True))


def _elided_page_numbers(page_obj):
    """把分页条压成「首页 … 当前页附近 … 末页」。

    商品/求购/失物招领这些列表页的页数很容易上到几十页，若把 page_range
    全量渲染成按钮，分页条会横向溢出整页。Django 的 get_elided_page_range
    默认以第 1 页为中心，而模板里调用它拿不到参数，所以在这里显式传入
    当前页码，由视图统一算好。
    """
    numbers = page_obj.paginator.get_elided_page_range(
        page_obj.number, on_each_side=1, on_ends=1,
    )
    # Django 的省略号是字符串常量（Paginator.ELLIPSIS），不是 None。模板里用
    # {% if num %} 判断，字符串会被当成真值，于是渲染出一个指向 ?page=… 的
    # 死链接。这里统一换成 None，让模板的真假判断回到预期语义。
    return [
        None if number == Paginator.ELLIPSIS else number
        for number in numbers
    ]

def home(request):
    active_item_filter = Q(items__status='available') & (
        Q(items__expires_at__isnull=True) | Q(items__expires_at__gt=timezone.now())
    )
    categories = Category.objects.annotate(available_count=Count('items', filter=active_item_filter))
    locations = CampusLocation.objects.filter(is_active=True).annotate(available_count=Count('items', filter=active_item_filter))
    recent_items = Item.objects.available().select_related('category', 'seller', 'location').prefetch_related('images')[:8]
    recommendations = get_recommendations(request.user, limit=8)
    _record_recommendation_impressions(request.user, recommendations)
    context = {
        'categories': categories,
        'locations': locations,
        'recent_items': recent_items,
        'recommendations': recommendations,
        'favorite_ids': _favorite_ids(request),
        'stats': {
            'items': Item.objects.available().count(),
            'categories': categories.count(),
            'members': Item.objects.values('seller').distinct().count(),
        },
    }
    return render(request, 'listings/home.html', context)


def _record_recommendation_impressions(user, recommendations):
    """把首页这一屏被渲染出去的推荐卡，一次性写进 `RecommendationImpression`。

    这是 `recommendation_quality` 报反馈率所缺的那个分母。反馈表只记「表了态」、
    不记「看过」：一张卡被渲染出来而用户什么也没点，库里一行都没有，所以反馈率只能
    拿 `BrowsingHistory` 反推一个下界——一件从搜索点进去的商品也占分母，却没有推荐
    卡可点。本表把「被看见」这件事真正落库，见
    `docs/recommendation-impression-design.md`。

    与 `_record_opportunity_impressions` 共用一套骨架，两处差别都是业务前提决定的：

    - **`reason_key` 与 `score` 跟着一起落库。** 机会流那边刻意不存，因为那边的分数
      在忽略时刻要重算（双向匹配，同一条机会不同时刻进池分数会变），存一份「当时的
      分数」只会得到一份永远对不齐的数据；首页这边 `Recommendation` 是 frozen
      dataclass，理由和分数在渲染那一刻就已经算出来了，跟着 `item` 一起在内存里，
      落库是零成本的。存了它，「当时挂这句话的卡，用户点了吗」第一次有了推荐当时的
      分母。
    - **匿名访客也写。** 首页「为你挑选」对没登录的人照常渲染（模板只把标题换成
      「校园里正在被关注」、把两个反馈按钮收起来，卡照常出），所以匿名用户的看见同
      样要落库，`user` 存 null。否则首页最大的那部分流量整块不在分母里，反馈率会变
      成一个只有登录用户才配被统计的数。

    四处口径，逐条都是为了让分母和分子说的是同一件事：

    - **只有成功渲染了才写。** 没有推荐可渲染时这里拿到的是空列表，一次 insert 都
      不发。
    - **每次渲染一行，不去重。** 同一件商品在不同时刻被渲染给同一个人是两次独立的
      看见，去重会让分母变小、反馈率虚高。这正是它和 `RecommendationFeedback` 上
      `unique_recommendation_feedback` 的区别：表态该折叠，看见不该。
    - **一次 `bulk_create`。** 一屏最多 8 张（`get_recommendations` 的 `limit=8`），
      一次渲染就是一次 round-trip，不随商品总数增长；也不为这个面板多跑任何一次
      select。
    - **失败不致命。** 曝光写不进去，页面照常渲染，只是这一屏的看见进不了分母。用
      `transaction.TransactionManagementError` 之外的所有异常兜住，是因为这里跑在
      请求里，一次写入失败不该把整页首页变成 500。
    """
    # 匿名访客照常写，`user` 存 null。`request.user` 在没登录时是 AnonymousUser，直接
    # 赋给外键会抛 ValueError，所以这里先落成 None。
    user_id = user.pk if user.is_authenticated else None
    rows = []
    for position, recommendation in enumerate(recommendations, start=1):
        rows.append(RecommendationImpression(
            user_id=user_id,
            item=recommendation.item,
            reason_key=recommendation.reason_key,
            score=recommendation.score,
            position=position,
        ))
    if not rows:
        return
    try:
        RecommendationImpression.objects.bulk_create(rows, batch_size=32)
    except Exception:
        # 只报告、不影响用户：写不进去的分母只是让这一周期的反馈率偏低，见 docstring。
        logger.exception('记录首页推荐展示失败：用户 %s，%d 张卡', user.pk, len(rows))


def _parse_price(value):
    try:
        amount = Decimal(value)
    except (InvalidOperation, TypeError, ValueError):
        return None
    if not amount.is_finite() or amount < 0:
        return None
    return amount


def _expand_search_terms(query):
    """Expand a query with active operator-managed synonyms without changing the original query record."""
    query = (query or '').strip()[:120]
    if not query:
        return []
    normalized_query = query.casefold()
    terms = [query]
    synonym_rows = SearchSynonym.objects.filter(
        is_active=True,
    ).filter(
        Q(keyword__icontains=query) | Q(synonym__icontains=query)
    )
    for row in synonym_rows:
        if normalized_query in row.keyword.casefold():
            terms.append(row.synonym)
        if normalized_query in row.synonym.casefold():
            terms.append(row.keyword)
    return list(dict.fromkeys(term[:120] for term in terms))[:8]


def _search_context_signature(query, condition, category, location, raw_min_price, raw_max_price, sort):
    return '|'.join((
        query[:120],
        condition[:120],
        str(category.id if category else ''),
        str(location.id if location else ''),
        raw_min_price,
        raw_max_price,
        sort,
    ))


def item_list(request, category_id=None):
    active_item_filter = Q(items__status='available') & (
        Q(items__expires_at__isnull=True) | Q(items__expires_at__gt=timezone.now())
    )
    categories = Category.objects.annotate(available_count=Count('items', filter=active_item_filter))
    locations = CampusLocation.objects.filter(is_active=True).annotate(available_count=Count('items', filter=active_item_filter))
    category = get_object_or_404(Category, id=category_id) if category_id else None
    location_id = request.GET.get('location', '').strip()
    location = get_object_or_404(CampusLocation, id=location_id, is_active=True) if location_id.isdigit() else None
    query = request.GET.get('q', '').strip()
    search_terms = _expand_search_terms(query)
    condition = request.GET.get('condition', '').strip()
    raw_min_price = request.GET.get('min_price', '').strip()
    raw_max_price = request.GET.get('max_price', '').strip()
    min_price = _parse_price(raw_min_price)
    max_price = _parse_price(raw_max_price)

    items = Item.objects.available().select_related('category', 'seller', 'location').prefetch_related('images')
    if category:
        items = items.filter(category=category)
    if location:
        items = items.filter(location=location)
    if search_terms:
        search_filter = Q()
        for term in search_terms:
            search_filter |= (
                Q(title__icontains=term)
                | Q(description__icontains=term)
                | Q(condition__icontains=term)
                | Q(category__name__icontains=term)
                | Q(location__name__icontains=term)
                | Q(location__building__icontains=term)
            )
        items = items.filter(search_filter)
    if condition:
        items = items.filter(condition__icontains=condition)
    if min_price is not None:
        items = items.filter(price__gte=min_price)
    if max_price is not None:
        items = items.filter(price__lte=max_price)

    sort = request.GET.get('sort', 'latest')
    allowed_sorts = {'latest', 'price_asc', 'price_desc', 'relevance'}
    if sort not in allowed_sorts or (sort == 'relevance' and not query):
        sort = 'latest'
    if sort == 'relevance':
        feedback_window = timezone.now() - timedelta(days=90)
        # `search_clicks` 与 `search_impressions` 是 Item 上两个独立的**反向**关系。
        # 早先把它们写在同一条 annotate 里，数据库会先把两张表 JOIN 成「该商品对这个
        # 搜索词的点击数 x 曝光数」的乘积，再做 COUNT(DISTINCT ...)。数字因为带了
        # distinct 一直是对的，但中间行数是乘积：热门搜索词下一件商品有 40 次点击、
        # 80 次曝光，就要先撑出 3200 行才知道答案其实是两个数。改成两个按商品分组的
        # 相关子查询后，每件商品只各扫一遍自己那张表。
        # 实测（200 件商品，其中 60 件各有 40 次点击、80 次曝光）：0.55s -> 0.025s，
        # 约 22 倍，输出的排序与每个计数逐格相同。
        feedback_click_counts = SearchClick.objects.filter(
            item=OuterRef('pk'),
            created_at__gte=feedback_window,
            search_query__query__iexact=query,
        ).values('item').annotate(count=Count('id')).values('count')
        feedback_impression_counts = SearchImpression.objects.filter(
            item=OuterRef('pk'),
            created_at__gte=feedback_window,
            search_query__query__iexact=query,
        ).values('item').annotate(count=Count('id')).values('count')
        items = items.annotate(
            search_rank=Case(
                *[
                    When(title__iexact=term, then=Value(3))
                    for term in search_terms
                ],
                *[
                    When(title__istartswith=term, then=Value(2))
                    for term in search_terms
                ],
                *[
                    When(title__icontains=term, then=Value(1))
                    for term in search_terms
                ],
                default=Value(0),
                output_field=IntegerField(),
            ),
            # 没有反馈时子查询返回 NULL，兜底成 0，与 Count 的语义保持一致。
            feedback_click_count=Coalesce(
                Subquery(feedback_click_counts, output_field=IntegerField()),
                Value(0),
            ),
            feedback_impression_count=Coalesce(
                Subquery(feedback_impression_counts, output_field=IntegerField()),
                Value(0),
            ),
        ).annotate(
            feedback_ctr=Case(
                When(
                    feedback_impression_count__gt=0,
                    then=ExpressionWrapper(
                        Cast(F('feedback_click_count'), FloatField())
                        / Cast(F('feedback_impression_count'), FloatField()),
                        output_field=FloatField(),
                    ),
                ),
                default=Value(0.0),
                output_field=FloatField(),
            ),
        ).order_by('-search_rank', '-feedback_ctr', '-feedback_click_count', '-created_at')
    else:
        sort_map = {'latest': '-created_at', 'price_asc': 'price', 'price_desc': '-price'}
        items = items.order_by(sort_map[sort])

    result_count = items.count()
    search_query_record = None
    if query:
        search_signature = _search_context_signature(
            query, condition, category, location, raw_min_price, raw_max_price, sort,
        )
        if not request.GET.get('page'):
            search_query_record = SearchQuery.objects.create(
                user=request.user if request.user.is_authenticated else None,
                query=query[:120],
                condition=condition[:120],
                category=category,
                location=location,
                min_price=min_price,
                max_price=max_price,
                result_count=result_count,
            )
            request.session['active_search_query_id'] = search_query_record.id
            request.session['active_search_signature'] = search_signature
        elif request.session.get('active_search_signature') == search_signature:
            search_query_record = SearchQuery.objects.filter(
                id=request.session.get('active_search_query_id'),
            ).first()

    filter_params = {}
    if query:
        filter_params['q'] = query
    if condition:
        filter_params['condition'] = condition
    if raw_min_price:
        filter_params['min_price'] = raw_min_price
    if raw_max_price:
        filter_params['max_price'] = raw_max_price
    if location:
        filter_params['location'] = location.id
    if sort != 'latest':
        filter_params['sort'] = sort

    paginator = Paginator(items, 12)
    page_obj = paginator.get_page(request.GET.get('page'))
    if search_query_record:
        impressions = []
        for position, result_item in enumerate(page_obj.object_list, start=page_obj.start_index()):
            result_item.search_click_url = (
                f"{reverse('item_detail', args=[result_item.id])}?"
                f"{urlencode({'search_id': search_query_record.id, 'position': position})}"
            )
            impressions.append(SearchImpression(
                search_query=search_query_record,
                item=result_item,
                user=request.user if request.user.is_authenticated else None,
                position=position,
            ))
        if impressions:
            SearchImpression.objects.bulk_create(impressions)
    context = {
        'categories': categories,
        'locations': locations,
        'category': category,
        'location': location,
        'items': page_obj,
        'page_obj': page_obj,
        'page_numbers': _elided_page_numbers(page_obj),
        'query': query,
        'search_terms': search_terms,
        'condition': condition,
        'min_price': raw_min_price,
        'max_price': raw_max_price,
        'sort': sort,
        'filter_query': urlencode(filter_params),
        'saved_search_form': SavedSearchForm(initial={
            'query': query,
            'condition': condition,
            'category': category.id if category else None,
            'location': location.id if location else None,
            'min_price': min_price,
            'max_price': max_price,
        }),
        'has_search_criteria': any((
            query, condition, category, location,
            min_price is not None, max_price is not None,
        )),
        'title': f'{category.name} · 商品集' if category else '发现校园好物',
        'favorite_ids': _favorite_ids(request),
        'search_query_id': search_query_record.id if search_query_record else None,
    }
    return render(request, 'listings/item_list.html', context)


def _live_campaigns(now=None):
    now = now or timezone.now()
    return CampusCampaign.objects.filter(
        is_active=True,
        starts_at__lte=now,
    ).filter(
        Q(ends_at__isnull=True) | Q(ends_at__gt=now),
    )


def _campaign_available_item_counts(campaign_ids, now):
    """按专题分组统计「当前可参加」的商品数。

    只碰 `Item` 一张表，按 `campaign_id` 分组：分组键就是被计数的行本身，
    不存在跨关系的行复制，可以安全地和下面那次报名计数分开成两条查询。
    """
    if not campaign_ids:
        return {}
    rows = (
        Item.objects
        .filter(campaign_id__in=campaign_ids, status='available')
        .filter(Q(expires_at__isnull=True) | Q(expires_at__gt=now))
        .values('campaign_id')
        .annotate(count=Count('id'))
    )
    return {row['campaign_id']: row['count'] for row in rows}


def _campaign_active_registration_counts(campaign_ids):
    """按专题分组统计「仍在生效」的报名数。只碰报名表一张表。"""
    if not campaign_ids:
        return {}
    rows = (
        CampaignRegistration.objects
        .filter(campaign_id__in=campaign_ids, is_active=True)
        .values('campaign_id')
        .annotate(count=Count('id'))
    )
    return {row['campaign_id']: row['count'] for row in rows}


def campaign_list(request):
    now = timezone.now()
    campaigns = list(_live_campaigns(now).order_by('-starts_at', 'title'))
    campaign_ids = [campaign.pk for campaign in campaigns]
    item_counts = _campaign_available_item_counts(campaign_ids, now)
    registration_counts = _campaign_active_registration_counts(campaign_ids)
    for campaign in campaigns:
        campaign.available_item_count = item_counts.get(campaign.pk, 0)
        campaign.active_registration_count = registration_counts.get(campaign.pk, 0)
    return render(request, 'listings/campaign_list.html', {
        'campaigns': campaigns,
        'title': '校园专题',
    })


def campaign_detail(request, slug):
    campaign = get_object_or_404(
        _live_campaigns().annotate(
            active_registration_count=Count(
                'registrations',
                filter=Q(registrations__is_active=True),
                distinct=True,
            ),
        ),
        slug=slug,
    )
    items = Item.objects.available().filter(campaign=campaign).select_related(
        'category', 'seller', 'location',
    ).prefetch_related('images')
    active_registration = None
    if request.user.is_authenticated:
        active_registration = CampaignRegistration.objects.filter(
            campaign=campaign,
            user=request.user,
            is_active=True,
        ).first()
    return render(request, 'listings/campaign_detail.html', {
        'campaign': campaign,
        'items': items,
        'favorite_ids': _favorite_ids(request),
        'active_registration': active_registration,
        'update_frequency_choices': (
            ('daily', '每日摘要'), ('weekly', '每周摘要'),
        ),
        'title': campaign.title,
    })


def _campaign_action_redirect(request, campaign):
    next_url = request.POST.get('next', '').strip()
    if not next_url or not url_has_allowed_host_and_scheme(
        next_url,
        allowed_hosts={request.get_host()},
        require_https=request.is_secure(),
    ):
        if campaign.is_live():
            return redirect('campaign_detail', slug=campaign.slug)
        return redirect('campaign_list')
    return redirect(next_url)


@login_required
@require_POST
def join_campaign_view(request, slug):
    campaign = get_object_or_404(_live_campaigns(), slug=slug)
    try:
        result = join_campaign(request.user, campaign)
    except CampaignRegistrationClosed:
        messages.warning(request, '这个专题当前不在可参与时间内。')
    else:
        if result.state == 'joined':
            messages.success(request, f'已加入“{campaign.title}”。加入只表示关注专题，不会预约商品或占用库存。')
        elif result.state == 'already_joined':
            messages.info(request, '你已经加入这个专题，无需重复提交。')
    return _campaign_action_redirect(request, campaign)


@login_required
@require_POST
def leave_campaign_view(request, slug):
    campaign = get_object_or_404(_live_campaigns(), slug=slug)
    try:
        result = leave_campaign(request.user, campaign)
    except CampaignRegistrationClosed:
        messages.warning(request, '这个专题当前不在可修改参与状态的时间内。')
    else:
        if result.state == 'left':
            messages.success(request, '已退出专题，之前的参与记录会保留在足迹中。')
        elif result.state == 'not_joined':
            messages.info(request, '你当前没有加入这个专题。')
    return _campaign_action_redirect(request, campaign)


@login_required
@require_POST
def update_campaign_subscription(request, slug):
    """Let an active participant opt in or out of the campaign update digest."""
    campaign = get_object_or_404(_live_campaigns(), slug=slug)
    form = CampaignUpdateSubscriptionForm(request.POST)
    if not form.is_valid():
        messages.warning(request, '专题摘要设置无效，请选择每日或每周频率后重试。')
        return _campaign_action_redirect(request, campaign)

    try:
        _, changed = set_campaign_update_preference(
            request.user,
            campaign,
            notify_updates=form.cleaned_data['notify_updates'],
            update_frequency=form.cleaned_data['update_frequency'],
        )
    except CampaignUpdateSubscriptionUnavailable as exc:
        messages.warning(request, str(exc))
    else:
        if form.cleaned_data['notify_updates']:
            frequency = dict(form.fields['update_frequency'].choices)[
                form.cleaned_data['update_frequency']
            ]
            if changed:
                messages.success(request, f'已订阅专题上新摘要，将按{frequency}合并发送。摘要不会预约商品或占用库存。')
            else:
                messages.info(request, '专题摘要设置没有变化。')
        elif changed:
            messages.success(request, '已关闭专题上新摘要，之后的新上架不会再提醒你。')
        else:
            messages.info(request, '专题上新摘要当前已经是关闭状态。')
    return _campaign_action_redirect(request, campaign)


@login_required
def my_campaigns(request):
    now = timezone.now()
    registrations = CampaignRegistration.objects.filter(
        user=request.user,
    ).select_related('campaign')
    active_registrations = list(
        registrations.filter(is_active=True).order_by('-joined_at', '-id')
    )
    history_queryset = registrations.filter(is_active=False).order_by(
        '-left_at', '-joined_at', '-id',
    )
    history_page = Paginator(history_queryset, 12).get_page(
        request.GET.get('page'),
    )
    history_registrations = list(history_page.object_list)
    for registration in active_registrations + history_registrations:
        registration.campaign_is_live = registration.campaign.is_live(now)
    history_return_url = reverse('my_campaigns')
    if history_page.number > 1:
        history_return_url += f'?page={history_page.number}#campaign-history'
    return render(request, 'listings/my_campaigns.html', {
        'active_registrations': active_registrations,
        'history_registrations': history_registrations,
        'history_total': history_page.paginator.count,
        'history_page': history_page,
        'page_obj': history_page,
        'page_numbers': _elided_page_numbers(history_page),
        'history_return_url': history_return_url,
        'title': '我的专题足迹',
    })


def search_suggestions(request):
    """Return compact, privacy-conscious suggestions for the global search box."""
    query = request.GET.get('q', '').strip()[:40]
    if len(query) < 2:
        return JsonResponse({'suggestions': []})

    suggestions = []
    seen = set()

    def add_suggestion(text, kind, label, meta=''):
        normalized = text.casefold()
        if normalized in seen:
            return
        seen.add(normalized)
        suggestions.append({
            'text': text,
            'kind': kind,
            'label': label,
            'meta': meta,
        })

    popular_queries = SearchQuery.objects.filter(
        query__icontains=query,
    ).values('query').annotate(
        search_count=Count('id'),
    ).filter(search_count__gte=2).order_by('-search_count', 'query')[:5]
    for row in popular_queries:
        add_suggestion(row['query'], 'history', '大家搜过', f"{row['search_count']} 次")

    categories = Category.objects.filter(
        name__icontains=query,
    ).annotate(
        available_count=Count('items', filter=(
            Q(items__status='available')
            & (Q(items__expires_at__isnull=True) | Q(items__expires_at__gt=timezone.now()))
        )),
    ).order_by('-available_count', 'name')[:3]
    for category in categories:
        add_suggestion(category.name, 'category', '分类', f"{category.available_count} 件在售")

    locations = CampusLocation.objects.filter(
        Q(name__icontains=query)
        | Q(building__icontains=query)
        | Q(address__icontains=query),
        is_active=True,
    ).annotate(
        available_count=Count('items', filter=(
            Q(items__status='available')
            & (Q(items__expires_at__isnull=True) | Q(items__expires_at__gt=timezone.now()))
        )),
    ).order_by('-available_count', 'sort_order', 'name')[:3]
    for location in locations:
        add_suggestion(location.name, 'location', '交易地点', f"{location.available_count} 件在售")

    return JsonResponse({'suggestions': suggestions[:8]})

def item_detail(request, item_id):
    item = get_object_or_404(
        Item.objects.select_related(
            'category', 'seller', 'seller__profile', 'location',
        ).prefetch_related('images', 'comments__author__profile'),
        id=item_id,
    )
    search_id = request.GET.get('search_id', '').strip()
    if search_id.isdigit():
        search_query_record = SearchQuery.objects.filter(id=int(search_id)).first()
        if search_query_record:
            try:
                position = max(int(request.GET.get('position', 0)), 0)
            except (TypeError, ValueError):
                position = 0
            SearchClick.objects.create(
                search_query=search_query_record,
                item=item,
                user=request.user if request.user.is_authenticated else None,
                position=position,
            )
    if request.user.is_authenticated and request.user != item.seller:
        history, created = BrowsingHistory.objects.get_or_create(user=request.user, item=item)
        if not created:
            BrowsingHistory.objects.filter(pk=history.pk).update(
                view_count=F('view_count') + 1,
                last_viewed_at=timezone.now(),
            )
    related_items = Item.objects.available().filter(category=item.category).exclude(id=item.id).select_related('seller', 'location').prefetch_related('images')[:4]
    seller_ratings = Rating.objects.filter(ratee=item.seller).select_related('rater', 'order')[:5]
    rating_summary = Rating.objects.filter(ratee=item.seller).aggregate(average=Avg('score'), count=Count('id'))
    price_history_state = load_item_price_history_state(item)
    context = {
        'item': item,
        'active_order': Order.objects.filter(
            item_id=item.id, status__in=Order.ACTIVE_STATUS_VALUES,
        ).order_by('-created_at').first(),
        'related_items': related_items,
        'rating_summary': rating_summary,
        'seller_reputation': build_seller_reputation(
            item.seller, rating_stats=rating_summary,
        ),
        'price_insight': build_price_insight(item),
        'price_history': build_item_price_history(item, state=price_history_state),
        'price_history_maintenance': build_item_price_history_maintenance(
            item, request.user, state=price_history_state,
        ),
        'seller_ratings': seller_ratings,
        'seller_verification': CampusVerification.objects.filter(
            user=item.seller, status='verified', verified_at__isnull=False,
        ).first(),
        'is_favorite': item_id in _favorite_ids(request),
        'availability_watch': (
            ItemAvailabilityWatch.objects.filter(user=request.user, item=item).first()
            if request.user.is_authenticated and request.user != item.seller else None
        ),
        'has_reported': request.user.is_authenticated and Report.objects.filter(item=item, reporter=request.user).exists(),
        'pending_gift_application': (
            GiftApplication.objects.filter(item=item, applicant=request.user, status='pending').first()
            if request.user.is_authenticated and request.user != item.seller and item.trade_mode == 'free' else None
        ),
        'pending_gift_application_count': (
            GiftApplication.objects.filter(item=item, status='pending').count()
            if request.user.is_authenticated and request.user == item.seller and item.trade_mode == 'free' else 0
        ),
    }
    return render(request, 'listings/item_detail.html', context)


@login_required
def toggle_favorite(request, item_id):
    item = get_object_or_404(Item, id=item_id)
    if request.method == 'POST':
        favorite, created = Favorite.objects.get_or_create(user=request.user, item=item)
        if created:
            messages.success(request, '已加入心愿单。')
        else:
            favorite.delete()
            messages.info(request, '已从心愿单移除。')
    next_url = request.POST.get('next') or request.META.get('HTTP_REFERER') or ''
    if not url_has_allowed_host_and_scheme(next_url, allowed_hosts={request.get_host()}):
        next_url = ''
    return redirect(next_url or 'item_detail', item_id=item.id) if not next_url else redirect(next_url)


@login_required
def toggle_availability_watch(request, item_id):
    item = get_object_or_404(Item.objects.select_related('seller'), id=item_id)
    if item.seller == request.user:
        messages.info(request, '自己的商品不需要设置有货提醒。')
    elif item.status == 'available':
        messages.info(request, '这个商品当前正在出售，可以直接预约交易。')
    elif request.method == 'POST':
        watch, created = ItemAvailabilityWatch.objects.get_or_create(
            user=request.user, item=item,
        )
        if created:
            messages.success(request, '已设置有货提醒，商品恢复在售时会通知你。')
        else:
            watch.delete()
            messages.info(request, '已取消这个商品的有货提醒。')
    next_url = request.POST.get('next', '').strip()
    if not next_url or not url_has_allowed_host_and_scheme(
        next_url,
        allowed_hosts={request.get_host()},
        require_https=request.is_secure(),
    ):
        next_url = reverse('item_detail', args=[item.id])
    return redirect(next_url)


@login_required
def recommendation_feedback(request, item_id):
    item = get_object_or_404(Item.objects.available(), id=item_id)
    if item.seller == request.user:
        messages.info(request, '自己的商品不会进入个性化推荐。')
        return redirect('home')
    if request.method != 'POST':
        return redirect('home')

    action = request.POST.get('action', '').strip()
    allowed_actions = dict(RecommendationFeedback.ACTION_CHOICES)
    if action not in allowed_actions:
        messages.error(request, '暂不支持这种推荐反馈。')
    else:
        RecommendationFeedback.objects.update_or_create(
            user=request.user,
            item=item,
            defaults={'action': action},
        )
        if action == 'interested':
            messages.success(request, '已记录“想看看”，之后会优先为你保留。')
        else:
            messages.info(request, '已减少这类推荐，之后不会优先展示它。')

    next_url = request.POST.get('next', '').strip()
    if not next_url or not url_has_allowed_host_and_scheme(
        next_url,
        allowed_hosts={request.get_host()},
        require_https=request.is_secure(),
    ):
        next_url = reverse('home')
    return redirect(next_url)


@login_required
def report_item(request, item_id):
    item = get_object_or_404(Item.objects.select_related('seller'), id=item_id)
    if item.seller == request.user:
        messages.error(request, '不能举报自己发布的商品。')
        return redirect('item_detail', item_id=item.id)
    if Report.objects.filter(item=item, reporter=request.user).exists():
        messages.info(request, '你已经举报过这个商品，我们会尽快处理。')
        return redirect('item_detail', item_id=item.id)
    if request.method == 'POST':
        form = ReportForm(request.POST)
        if form.is_valid():
            report = form.save(commit=False)
            report.item = item
            report.reporter = request.user
            try:
                report.save()
            except IntegrityError:
                messages.info(request, '你已经举报过这个商品，我们会尽快处理。')
            else:
                messages.success(request, '举报已提交，感谢你一起维护校园社区。')
            return redirect('item_detail', item_id=item.id)
    else:
        form = ReportForm()
    return render(request, 'listings/report_form.html', {'form': form, 'item': item, 'title': '举报商品'})


@login_required
def create_order(request, item_id):
    item = get_object_or_404(Item.objects.available().select_related('seller', 'location'), id=item_id)
    if item.seller == request.user:
        messages.error(request, '不能预约自己的商品。')
        return redirect('item_detail', item_id=item.id)

    # Free gifts use an application queue instead of reserving the item for
    # the first person who clicks. The seller can review the queue and select
    # the most suitable applicant.
    if item.trade_mode == 'free':
        if request.method == 'POST':
            form = OrderForm(request.POST)
            if form.is_valid():
                try:
                    with transaction.atomic():
                        locked_item = Item.objects.select_for_update(of=('self',)).select_related('seller', 'location').get(id=item.id)
                        if not locked_item.is_available_now:
                            messages.info(request, '这个免费商品刚刚结束展示或已被选中。')
                            return redirect('item_detail', item_id=item.id)
                        if GiftApplication.objects.filter(
                            item=locked_item, applicant=request.user, status='pending',
                        ).exists():
                            messages.info(request, '你已经提交过这个商品的领取申请。')
                            return redirect('my_gift_applications')
                        application = GiftApplication.objects.create(
                            item=locked_item,
                            applicant=request.user,
                            meeting_location=form.cleaned_data.get('meeting_location') or locked_item.location,
                            applicant_note=form.cleaned_data.get('buyer_note', ''),
                        )
                        create_notification(
                            locked_item.seller, actor=request.user, kind='gift_application',
                            title='收到新的领取申请',
                            message=f'{request.user.username}申请领取你的免费商品“{locked_item.title}”。',
                            item=locked_item,
                            target_url=reverse('manage_gift_applications', args=[locked_item.id]),
                        )
                except IntegrityError:
                    messages.info(request, '你已经提交过这个商品的领取申请。')
                    return redirect('my_gift_applications')
                messages.success(request, '领取申请已提交，等待发布者选择。')
                return redirect('my_gift_applications')
        else:
            form = OrderForm(initial={'meeting_location': item.location_id})
        return render(request, 'listings/order_form.html', {'form': form, 'item': item, 'title': '申请领取'})

    active_order = Order.objects.filter(
        item_id=item.id, status__in=Order.ACTIVE_STATUS_VALUES,
    ).order_by('-created_at').first()
    if active_order:
        messages.info(request, '这个商品已经有一笔进行中的交易预约。')
        return redirect('order_detail', order_id=active_order.id)
    if request.method == 'POST':
        form = OrderForm(request.POST)
        if form.is_valid():
            try:
                with transaction.atomic():
                    locked_item = Item.objects.select_for_update(of=('self',)).select_related('seller').get(id=item.id)
                    if (
                        not locked_item.is_available_now
                        or Order.objects.filter(
                            item_id=locked_item.id, status__in=Order.ACTIVE_STATUS_VALUES,
                        ).exists()
                    ):
                        messages.info(request, '这个商品刚刚被其他同学预约了。')
                        return redirect('item_detail', item_id=item.id)
                    order = form.save(commit=False)
                    order.item = locked_item
                    order.buyer = request.user
                    order.seller = locked_item.seller
                    order.agreed_price = Decimal('0.00') if locked_item.trade_mode == 'borrow' else locked_item.price
                    order.deposit_amount = locked_item.deposit_amount if locked_item.trade_mode == 'borrow' else Decimal('0.00')
                    order.return_due_at = (
                        timezone.now() + timedelta(days=locked_item.borrow_days or 7)
                        if locked_item.trade_mode == 'borrow' else None
                    )
                    order.meeting_location = order.meeting_location or locked_item.location
                    order.confirmation_deadline = timezone.now() + timedelta(hours=24)
                    order.save()
                    OrderEvent.objects.create(
                        order=order, actor=request.user, to_status=order.status,
                        note='买家发起交易预约',
                    )
                    action_label = '申请借用' if order.item.trade_mode == 'borrow' else '预约'
                    title = '收到新的借用申请' if order.item.trade_mode == 'borrow' else '收到新的交易预约'
                    create_notification(
                        order.seller, actor=request.user, kind='order_created',
                        title=title,
                        message=f'{request.user.username}{action_label}了你的商品“{order.item.title}”。',
                        order=order, item=order.item,
                        target_url=reverse('order_detail', args=[order.id]),
                    )
                    locked_item.status = 'reserved'
                    locked_item.save(update_fields=['status', 'updated_at'])
            except IntegrityError:
                messages.info(request, '这个商品刚刚被其他同学预约了。')
                return redirect('item_detail', item_id=item.id)
            messages.success(
                request,
                '借用申请已提交，等待发布者确认。' if order.item.trade_mode == 'borrow' else '预约已提交，等待卖家确认。',
            )
            return redirect('order_detail', order_id=order.id)
    else:
        form = OrderForm(initial={'meeting_location': item.location_id})
    return render(request, 'listings/order_form.html', {
        'form': form, 'item': item,
        'title': '申请借用' if item.trade_mode == 'borrow' else '预约交易',
    })


@login_required
def my_gift_applications(request):
    applications = GiftApplication.objects.filter(
        applicant=request.user,
    ).annotate(
        status_rank=Case(
            When(status='pending', then=Value(0)),
            When(status='selected', then=Value(1)),
            When(status='rejected', then=Value(2)),
            When(status='withdrawn', then=Value(3)),
            default=Value(4),
            output_field=IntegerField(),
        ),
    ).select_related(
        'item__seller', 'item__location', 'meeting_location', 'order',
    ).order_by('status_rank', '-created_at', '-pk')
    page_obj = Paginator(applications, 12).get_page(request.GET.get('page'))
    return render(request, 'listings/gift_applications.html', {
        'applications': page_obj.object_list,
        'page_obj': page_obj,
        'page_numbers': _elided_page_numbers(page_obj),
        'return_url': f"{reverse('my_gift_applications')}?page={page_obj.number}",
        'title': '我的领取申请',
    })


@login_required
def manage_gift_applications(request, item_id):
    item = get_object_or_404(Item.objects.select_related('seller', 'location'), id=item_id)
    if item.seller != request.user:
        messages.error(request, '你没有权限查看这个商品的领取申请。')
        return redirect('item_detail', item_id=item.id)
    if item.trade_mode != 'free':
        messages.info(request, '只有免费赠送商品才有领取申请队列。')
        return redirect('item_detail', item_id=item.id)
    applications = GiftApplication.objects.filter(item=item).annotate(
        status_rank=Case(
            When(status='pending', then=Value(0)),
            When(status='selected', then=Value(1)),
            When(status='rejected', then=Value(2)),
            When(status='withdrawn', then=Value(3)),
            default=Value(4),
            output_field=IntegerField(),
        ),
    ).select_related(
        'applicant', 'meeting_location', 'order',
    ).order_by('status_rank', 'created_at', '-pk')
    page_obj = Paginator(applications, 12).get_page(request.GET.get('page'))
    return render(request, 'listings/manage_gift_applications.html', {
        'item': item,
        'applications': page_obj.object_list,
        'page_obj': page_obj,
        'page_numbers': _elided_page_numbers(page_obj),
        'pending_count': applications.filter(status='pending').count(),
        'return_url': f"{reverse('manage_gift_applications', args=[item.id])}?page={page_obj.number}",
        'title': '管理领取申请',
    })


def _manage_gift_applications_redirect(request, item_id):
    next_url = request.POST.get('next', '').strip()
    if next_url and url_has_allowed_host_and_scheme(
        next_url,
        allowed_hosts={request.get_host()},
        require_https=request.is_secure(),
    ):
        return redirect(next_url)
    return redirect('manage_gift_applications', item_id=item_id)


@login_required
def accept_gift_application(request, application_id):
    application = get_object_or_404(
        GiftApplication.objects.select_related('item', 'item__seller', 'applicant'),
        id=application_id,
    )
    if application.item.seller != request.user:
        messages.error(request, '你没有权限处理这个领取申请。')
        return redirect('home')
    if request.method != 'POST':
        return _manage_gift_applications_redirect(request, application.item_id)

    try:
        with transaction.atomic():
            locked_application = GiftApplication.objects.select_for_update(of=('self',)).select_related(
                'item', 'applicant', 'item__seller', 'meeting_location',
            ).get(pk=application.id)
            locked_item = Item.objects.select_for_update(of=('self',)).get(pk=locked_application.item_id)
            if locked_application.status != 'pending':
                messages.info(request, '这个申请已经处理过了。')
                return _manage_gift_applications_redirect(request, locked_application.item_id)
            if locked_item.trade_mode != 'free' or not locked_item.is_available_now:
                messages.info(request, '商品已经无法继续选择领取人。')
                return _manage_gift_applications_redirect(request, locked_item.id)
            if Order.objects.filter(
                item_id=locked_item.id, status__in=Order.ACTIVE_STATUS_VALUES,
            ).exists():
                messages.info(request, '这个商品已经有一笔进行中的交易。')
                return _manage_gift_applications_redirect(request, locked_item.id)

            order = Order.objects.create(
                item=locked_item,
                buyer=locked_application.applicant,
                seller=locked_item.seller,
                meeting_location=locked_application.meeting_location or locked_item.location,
                agreed_price=locked_item.price,
                buyer_note=locked_application.applicant_note,
                confirmation_deadline=timezone.now() + timedelta(hours=24),
            )
            OrderEvent.objects.create(
                order=order, actor=request.user, to_status=order.status,
                note='发布者从免费领取申请队列中选定了领取人',
            )
            locked_application.status = 'selected'
            locked_application.order = order
            locked_application.decided_at = timezone.now()
            locked_application.save(update_fields=['status', 'order', 'decided_at', 'updated_at'])

            pending_applications = list(
                GiftApplication.objects.select_for_update(of=('self',)).select_related('applicant').filter(
                    item=locked_item, status='pending',
                ).exclude(pk=locked_application.pk)
            )
            now = timezone.now()
            for other in pending_applications:
                other.status = 'rejected'
                other.decided_at = now
                other.save(update_fields=['status', 'decided_at', 'updated_at'])
            locked_item.status = 'reserved'
            locked_item.save(update_fields=['status', 'updated_at'])

            create_notification(
                locked_application.applicant, actor=request.user, kind='gift_application_status',
                title='你的领取申请已被选中',
                message=f'你已被选中领取“{locked_item.title}”，请在 24 小时内确认交易安排。',
                order=order, item=locked_item,
                target_url=reverse('order_detail', args=[order.id]),
            )
            for other in pending_applications:
                create_notification(
                    other.applicant, actor=request.user, kind='gift_application_status',
                    title='领取申请结果更新',
                    message=f'商品“{locked_item.title}”已选择其他申请人，感谢你的参与。',
                    item=locked_item,
                    target_url=reverse('my_gift_applications'),
                )
    except IntegrityError:
        messages.info(request, '这个商品刚刚被其他操作选中了，请刷新申请队列。')
        return _manage_gift_applications_redirect(request, application.item_id)

    messages.success(request, '已选中领取人，并生成待确认交易订单。')
    return redirect('order_detail', order_id=order.id)


@login_required
def withdraw_gift_application(request, application_id):
    application = get_object_or_404(
        GiftApplication.objects.select_related('item'), id=application_id, applicant=request.user,
    )
    if request.method == 'POST' and application.status == 'pending':
        application.status = 'withdrawn'
        application.decided_at = timezone.now()
        application.save(update_fields=['status', 'decided_at', 'updated_at'])
        messages.success(request, '领取申请已撤回。')
    next_url = request.POST.get('next', '').strip()
    if next_url and url_has_allowed_host_and_scheme(
        next_url,
        allowed_hosts={request.get_host()},
        require_https=request.is_secure(),
    ):
        return redirect(next_url)
    return redirect('my_gift_applications')


@login_required
def order_detail(request, order_id):
    order = get_object_or_404(
        Order.objects.select_related(
            'item', 'item__category', 'item__location', 'buyer', 'seller', 'meeting_location', 'delivery_confirmation', 'dispute', 'appointment__location', 'appointment__proposed_by', 'appointment__responded_by',
        ),
        id=order_id,
    )
    if request.user not in {order.buyer, order.seller}:
        messages.error(request, '你没有权限查看这笔订单。')
        return redirect('home')
    appointment = getattr(order, 'appointment', None)
    appointment_service_evaluation = (
        evaluate_location_service_window(
            appointment.location, appointment.start_at, appointment.end_at,
        ) if appointment and appointment.location else None
    )
    transaction_safety = build_transaction_safety(order)
    safety_fingerprint = transaction_safety_fingerprint(transaction_safety)
    current_safety_acknowledgements = {
        acknowledgement.role: acknowledgement
        for acknowledgement in order.safety_acknowledgements.filter(fingerprint=safety_fingerprint)
    }
    viewer_safety_role = 'buyer' if request.user == order.buyer else 'seller'
    my_safety_acknowledgement = current_safety_acknowledgements.get(viewer_safety_role)
    can_acknowledge_order_safety = (
        order.status in {'pending', 'confirmed', 'meeting'}
        and my_safety_acknowledgement is None
    )
    meeting_form = None
    meeting_location_recommendations = []
    meeting_time_recommendations = []
    delivery_code_form = None
    handoff_code_display = None
    incident_form = None
    confirmation = getattr(order, 'delivery_confirmation', None)
    incident = getattr(appointment, 'incident', None) if appointment else None
    if order.status in {'confirmed', 'meeting'} and not (appointment and appointment.status == 'pending' and appointment.proposed_by_id != request.user.id):
        form_initial = {'location': order.meeting_location_id} if not appointment else None
        meeting_form = MeetingAppointmentForm(instance=appointment, initial=form_initial)
        meeting_location_recommendations = recommend_meeting_locations(
            order=order,
            target_start_at=appointment.start_at if appointment else None,
            target_end_at=appointment.end_at if appointment else None,
        )
        meeting_time_recommendations = recommend_meeting_times(
            order=order,
            location=appointment.location if appointment else order.meeting_location,
        )
    if confirmation and request.user == order.seller and confirmation.handoff_code_hash and not confirmation.handoff_code_used_at and not confirmation.seller_confirmed_at:
        delivery_code_form = DeliveryCodeForm()
    if confirmation and request.user == order.buyer and confirmation.handoff_code_hash and not confirmation.handoff_code_used_at:
        handoff_code_display = request.session.get(f'delivery_code_{order.id}')
    if appointment and appointment.can_report_incident and not incident:
        incident_form = MeetingIncidentForm()
    rating_target = order.seller if request.user == order.buyer else order.buyer
    my_rating = Rating.objects.filter(order=order, rater=request.user).first()
    rating_form = RatingForm() if order.status in {'completed', 'returned'} and not my_rating else None
    ratings = order.ratings.select_related('rater', 'ratee').all()
    events = order.events.select_related('actor').all()
    dispute = getattr(order, 'dispute', None)
    dispute_evidence = dispute.evidence.select_related('uploaded_by').all() if dispute else []
    dispute_evidence_form = (
        DisputeEvidenceForm()
        if dispute and dispute.status in {'open', 'reviewing'}
        else None
    )
    return render(request, 'listings/order_detail.html', {
        'order': order,
        'title': '借用订单' if order.item.trade_mode == 'borrow' else '交易订单',
        'rating_target': rating_target,
        'my_rating': my_rating,
        'rating_form': rating_form,
        'ratings': ratings,
        'events': events,
        'confirmation': confirmation,
        'delivery_code_form': delivery_code_form,
        'handoff_code_display': handoff_code_display,
        'appointment': appointment,
        'appointment_service_evaluation': appointment_service_evaluation,
        'transaction_safety': transaction_safety,
        'safety_fingerprint': safety_fingerprint,
        'buyer_safety_acknowledgement': current_safety_acknowledgements.get('buyer'),
        'seller_safety_acknowledgement': current_safety_acknowledgements.get('seller'),
        'my_safety_acknowledgement': my_safety_acknowledgement,
        'can_acknowledge_order_safety': can_acknowledge_order_safety,
        'meeting_form': meeting_form,
        'meeting_location_recommendations': meeting_location_recommendations,
        'meeting_time_recommendations': meeting_time_recommendations,
        'incident': incident,
        'incident_form': incident_form,
        'dispute': dispute,
        'dispute_evidence': dispute_evidence,
        'dispute_evidence_form': dispute_evidence_form,
        'now': timezone.now(),
    })


@login_required
def acknowledge_order_safety(request, order_id):
    """Record one participant's acknowledgement of the current safety card.

    The acknowledgement is versioned by the safety-action fingerprint. It is
    deliberately not an order event and sends no notification: reading a safety
    checklist is the participant's own risk decision, not the other party's todo.
    """
    order = get_object_or_404(
        Order.objects.select_related('item', 'buyer', 'seller'), id=order_id,
    )
    if request.user not in {order.buyer, order.seller}:
        messages.error(request, '你没有权限确认这笔订单的安全卡。')
        return redirect('home')
    if request.method != 'POST':
        return redirect('order_detail', order_id=order.id)

    with transaction.atomic():
        locked_order = Order.objects.select_for_update(of=('self',)).select_related(
            'item', 'buyer', 'seller', 'meeting_location', 'item__category', 'item__location',
        ).get(pk=order.id)
        if request.user not in {locked_order.buyer, locked_order.seller}:
            messages.error(request, '你没有权限确认这笔订单的安全卡。')
            return redirect('home')
        if locked_order.status not in {'pending', 'confirmed', 'meeting'}:
            messages.info(request, '订单已进入后续阶段，无需再补确认交易安全卡。')
            return redirect('order_detail', order_id=locked_order.id)

        summary = build_transaction_safety(locked_order)
        fingerprint = transaction_safety_fingerprint(summary)
        role = 'buyer' if request.user == locked_order.buyer else 'seller'
        _, created = OrderSafetyAcknowledgement.objects.get_or_create(
            order=locked_order,
            role=role,
            fingerprint=fingerprint,
        )

    if created:
        messages.success(request, '已确认当前交易安全卡。')
    else:
        messages.info(request, '你已经确认过当前交易安全卡。')
    return redirect('order_detail', order_id=order.id)


@login_required
def propose_meeting(request, order_id):
    order = get_object_or_404(
        Order.objects.select_related('item', 'buyer', 'seller', 'meeting_location'), id=order_id,
    )
    if request.user not in {order.buyer, order.seller}:
        messages.error(request, '你没有权限安排这笔订单的交付。')
        return redirect('home')
    if request.method != 'POST':
        return redirect('order_detail', order_id=order.id)
    if order.status not in {'confirmed', 'meeting'}:
        messages.error(request, '卖家确认订单后，才能安排交付时间。')
        return redirect('order_detail', order_id=order.id)

    form = MeetingAppointmentForm(request.POST)
    if not form.is_valid():
        error_text = ' '.join(
            error for field_errors in form.errors.values() for error in field_errors
        )
        messages.error(request, f'交付安排未保存：{error_text or "请检查填写内容。"}')
        return redirect('order_detail', order_id=order.id)

    with transaction.atomic():
        locked_order = Order.objects.select_for_update(of=('self',)).select_related(
            'item', 'buyer', 'seller',
        ).get(pk=order.id)
        if locked_order.status not in {'confirmed', 'meeting'}:
            messages.error(request, '订单状态已经发生变化，请刷新后再试。')
            return redirect('order_detail', order_id=locked_order.id)
        candidate_start = form.cleaned_data['start_at']
        candidate_end = form.cleaned_data['end_at']
        conflicts = find_appointment_conflicts(
            user=locked_order.buyer,
            start_at=candidate_start,
            end_at=candidate_end,
            exclude_order_id=locked_order.id,
            lock=True,
        ).first()
        if not conflicts:
            conflicts = find_appointment_conflicts(
                user=locked_order.seller,
                start_at=candidate_start,
                end_at=candidate_end,
                exclude_order_id=locked_order.id,
                lock=True,
            ).first()
        conflict = conflicts
        if conflict:
            messages.error(
                request,
                f'这段时间与“{conflict.order.item.title}”的另一笔交付安排冲突，请换一个时间。',
            )
            return redirect('order_detail', order_id=locked_order.id)
        appointment, created = MeetingAppointment.objects.select_for_update(of=('self',)).get_or_create(
            order=locked_order,
            defaults={
                'proposed_by': request.user,
                'start_at': form.cleaned_data['start_at'],
                'end_at': form.cleaned_data['end_at'],
                'location': form.cleaned_data['location'],
            },
        )
        if not created:
            appointment.proposed_by = request.user
            appointment.start_at = form.cleaned_data['start_at']
            appointment.end_at = form.cleaned_data['end_at']
            appointment.location = form.cleaned_data['location']
            appointment.status = 'pending'
            appointment.responded_by = None
            appointment.responded_at = None
            appointment.decline_reason = ''
            appointment.save(update_fields=[
                'proposed_by', 'start_at', 'end_at', 'location', 'status',
                'responded_by', 'responded_at', 'decline_reason', 'updated_at',
            ])
        if locked_order.meeting_location_id != appointment.location_id:
            locked_order.meeting_location_id = appointment.location_id
            locked_order.save(update_fields=['meeting_location', 'updated_at'])
        OrderEvent.objects.create(
            order=locked_order,
            actor=request.user,
            from_status=locked_order.status,
            to_status=locked_order.status,
            note='提出了新的交付时间安排' if created else '更新了交付时间安排，等待对方确认',
        )
        other_party = locked_order.seller if request.user == locked_order.buyer else locked_order.buyer
        create_notification(
            other_party,
            actor=request.user,
            kind='order_status',
            title='收到新的交付时间安排',
            message=(
                f'{request.user.username}为商品“{locked_order.item.title}”安排了 '
                f'{appointment.start_at:%m月%d日 %H:%M} 的交付时间，请确认。'
            ),
            order=locked_order,
            item=locked_order.item,
            target_url=reverse('order_detail', args=[locked_order.id]),
        )
    messages.success(request, '交付时间已提交，等待对方确认。')
    if form.service_hours_notice:
        messages.warning(request, form.service_hours_notice)
    return redirect('order_detail', order_id=order.id)


@login_required
def respond_meeting(request, order_id, decision):
    if decision not in {'accept', 'decline'}:
        messages.error(request, '无效的交付安排操作。')
        return redirect('order_detail', order_id=order_id)
    order = get_object_or_404(
        Order.objects.select_related('item', 'buyer', 'seller'), id=order_id,
    )
    if request.user not in {order.buyer, order.seller}:
        messages.error(request, '你没有权限回应这笔订单的交付安排。')
        return redirect('home')
    if request.method != 'POST':
        return redirect('order_detail', order_id=order.id)

    with transaction.atomic():
        locked_order = Order.objects.select_for_update(of=('self',)).select_related(
            'item', 'buyer', 'seller',
        ).get(pk=order.id)
        appointment = MeetingAppointment.objects.select_for_update(of=('self',)).select_related(
            'proposed_by',
        ).filter(order=locked_order).first()
        if locked_order.status not in {'confirmed', 'meeting'}:
            messages.info(request, '当前订单已经结束，不能再回应交付安排。')
            return redirect('order_detail', order_id=locked_order.id)
        if not appointment or appointment.status != 'pending':
            messages.info(request, '当前没有等待你确认的交付安排。')
            return redirect('order_detail', order_id=locked_order.id)
        if appointment.proposed_by_id == request.user.id:
            messages.info(request, '请等待对方回应你提出的交付安排。')
            return redirect('order_detail', order_id=locked_order.id)

        now = timezone.now()
        appointment.responded_by = request.user
        appointment.responded_at = now
        appointment.status = 'confirmed' if decision == 'accept' else 'declined'
        if decision == 'decline':
            appointment.decline_reason = '对方暂未接受这次时间安排'
        appointment.save(update_fields=['status', 'responded_by', 'responded_at', 'decline_reason', 'updated_at'])

        proposer = appointment.proposed_by
        if decision == 'accept':
            previous_status = locked_order.status
            if locked_order.status == 'confirmed':
                locked_order.status = 'meeting'
                locked_order.save(update_fields=['status', 'updated_at'])
            OrderEvent.objects.create(
                order=locked_order,
                actor=request.user,
                from_status=previous_status,
                to_status=locked_order.status,
                note='双方确认了交付时间安排',
            )
            title = '交付时间安排已确认'
            message = f'{request.user.username}已确认商品“{locked_order.item.title}”的交付时间。'
        else:
            OrderEvent.objects.create(
                order=locked_order,
                actor=request.user,
                from_status=locked_order.status,
                to_status=locked_order.status,
                note='暂未接受本次交付时间安排',
            )
            title = '交付时间安排未被接受'
            message = f'{request.user.username}暂未接受商品“{locked_order.item.title}”的这次交付安排，请重新协商。'
        create_notification(
            proposer,
            actor=request.user,
            kind='order_status',
            title=title,
            message=message,
            order=locked_order,
            item=locked_order.item,
            target_url=reverse('order_detail', args=[locked_order.id]),
        )
    messages.success(request, '已回应交付时间安排。' if decision == 'accept' else '已记录你的暂不接受。')
    return redirect('order_detail', order_id=order.id)


@login_required
def check_in_meeting(request, order_id):
    order = get_object_or_404(
        Order.objects.select_related('item', 'buyer', 'seller'), id=order_id,
    )
    if request.user not in {order.buyer, order.seller}:
        messages.error(request, '你没有权限操作这笔订单的到场状态。')
        return redirect('home')
    if request.method != 'POST':
        return redirect('order_detail', order_id=order.id)

    with transaction.atomic():
        locked_order = Order.objects.select_for_update(of=('self',)).select_related(
            'item', 'buyer', 'seller',
        ).get(pk=order.id)
        appointment = MeetingAppointment.objects.select_for_update(of=('self',)).get(order=locked_order)
        if locked_order.status != 'meeting' or not appointment.is_confirmed:
            messages.error(request, '只有进入待当面交付且双方已确认时间后，才能签到。')
            return redirect('order_detail', order_id=locked_order.id)
        if not appointment.check_in_open:
            messages.error(request, '当前还不在签到时间窗口内，请在约定开始前 30 分钟至结束后 30 分钟内签到。')
            return redirect('order_detail', order_id=locked_order.id)

        field_name = 'buyer_arrived_at' if request.user == locked_order.buyer else 'seller_arrived_at'
        if getattr(appointment, field_name):
            messages.info(request, '你已经登记到场，无需重复操作。')
            return redirect('order_detail', order_id=locked_order.id)
        now = timezone.now()
        setattr(appointment, field_name, now)
        appointment.save(update_fields=[field_name, 'updated_at'])
        both_arrived = bool(appointment.buyer_arrived_at and appointment.seller_arrived_at)
        OrderEvent.objects.create(
            order=locked_order,
            actor=request.user,
            from_status=locked_order.status,
            to_status=locked_order.status,
            note='双方均已登记到场' if both_arrived else f'{request.user.username}登记已到场',
        )
        other_party = locked_order.seller if request.user == locked_order.buyer else locked_order.buyer
        create_notification(
            other_party,
            actor=request.user,
            kind='order_status',
            title='对方已到达交付地点' if not both_arrived else '双方已登记到场',
            message=(
                f'{request.user.username}已登记到达商品“{locked_order.item.title}”的交付地点。'
                if not both_arrived else f'商品“{locked_order.item.title}”的买卖双方都已登记到场，可以进行交付确认。'
            ),
            order=locked_order,
            item=locked_order.item,
            target_url=reverse('order_detail', args=[locked_order.id]),
        )
    messages.success(request, '已登记到场，系统会通知交易对方。')
    return redirect('order_detail', order_id=order.id)


@login_required
def report_meeting_incident(request, order_id):
    order = get_object_or_404(
        Order.objects.select_related('item', 'buyer', 'seller', 'appointment'), id=order_id,
    )
    if request.user not in {order.buyer, order.seller}:
        messages.error(request, '你没有权限提交这笔订单的预约异常。')
        return redirect('home')
    appointment = getattr(order, 'appointment', None)
    if not appointment or order.status not in {'meeting', 'completed'} or not appointment.can_report_incident:
        messages.error(request, '只有约定结束后，才能提交预约异常记录。')
        return redirect('order_detail', order_id=order.id)
    if hasattr(appointment, 'incident'):
        messages.info(request, '这次预约已经有一条异常记录，平台会按该记录处理。')
        return redirect('order_detail', order_id=order.id)
    if request.method != 'POST':
        return redirect('order_detail', order_id=order.id)

    form = MeetingIncidentForm(request.POST)
    if not form.is_valid():
        error_text = ' '.join(error for errors in form.errors.values() for error in errors)
        messages.error(request, f'预约异常未提交：{error_text or "请检查填写内容。"}')
        return redirect('order_detail', order_id=order.id)

    with transaction.atomic():
        locked_appointment = MeetingAppointment.objects.select_for_update(of=('self',)).select_related(
            'order__item', 'order__buyer', 'order__seller',
        ).get(pk=appointment.id)
        locked_order = locked_appointment.order
        if hasattr(locked_appointment, 'incident'):
            messages.info(request, '这次预约已经有一条异常记录。')
            return redirect('order_detail', order_id=locked_order.id)
        if not locked_appointment.can_report_incident:
            messages.error(request, '约定时间尚未结束，暂时不能提交异常记录。')
            return redirect('order_detail', order_id=locked_order.id)
        accused = locked_order.seller if request.user == locked_order.buyer else locked_order.buyer
        incident = MeetingIncident.objects.create(
            appointment=locked_appointment,
            reported_by=request.user,
            accused=accused,
            reason=form.cleaned_data['reason'],
            detail=form.cleaned_data['detail'],
        )
        OrderEvent.objects.create(
            order=locked_order,
            actor=request.user,
            from_status=locked_order.status,
            to_status=locked_order.status,
            note=f'提交了交付预约异常：{incident.get_reason_display()}',
        )
        target_url = reverse('order_detail', args=[locked_order.id])
        create_notification(
            accused,
            actor=request.user,
            kind='meeting_incident',
            title='对方提交了交付预约异常',
            message=f'商品“{locked_order.item.title}”有一条新的交付预约异常记录，平台可能联系你核实。',
            order=locked_order,
            item=locked_order.item,
            target_url=target_url,
        )
        for staff_user in User.objects.filter(is_staff=True).exclude(pk=request.user.pk):
            create_notification(
                staff_user,
                actor=request.user,
                kind='meeting_incident',
                title='有新的交付预约异常待处理',
                message=f'商品“{locked_order.item.title}”的交付预约出现异常，请及时核实。',
                order=locked_order,
                item=locked_order.item,
                target_url=target_url,
            )
    messages.success(request, '预约异常已提交，平台会结合到场记录和交易时间线进行核实。')
    return redirect('order_detail', order_id=order.id)


@login_required
def generate_delivery_code(request, order_id):
    order = get_object_or_404(Order.objects.select_related('item', 'buyer', 'seller'), id=order_id)
    if request.user != order.buyer:
        messages.error(request, '只有买家可以生成交付确认码。')
        return redirect('order_detail', order_id=order.id)
    if request.method != 'POST':
        return redirect('order_detail', order_id=order.id)
    if order.status != 'meeting':
        messages.error(request, '订单进入“待当面交付”后，才能生成确认码。')
        return redirect('order_detail', order_id=order.id)

    raw_code = f'{secrets.randbelow(1_000_000):06d}'
    with transaction.atomic():
        locked_order = Order.objects.select_for_update(of=('self',)).select_related('item', 'buyer', 'seller').get(pk=order.id)
        if locked_order.status != 'meeting':
            messages.error(request, '订单状态已经发生变化，请刷新后再试。')
            return redirect('order_detail', order_id=locked_order.id)
        confirmation, _ = DeliveryConfirmation.objects.select_for_update(of=('self',)).get_or_create(order=locked_order)
        confirmation.handoff_code_hash = make_password(raw_code)
        confirmation.handoff_code_hint = f'末两位 {raw_code[-2:]}'
        confirmation.handoff_code_issued_at = timezone.now()
        confirmation.handoff_code_used_at = None
        confirmation.handoff_code_attempts = 0
        confirmation.save(update_fields=[
            'handoff_code_hash', 'handoff_code_hint', 'handoff_code_issued_at',
            'handoff_code_used_at', 'handoff_code_attempts', 'updated_at',
        ])
        request.session[f'delivery_code_{locked_order.id}'] = raw_code
        create_notification(
            locked_order.seller,
            actor=request.user,
            kind='order_status',
            title='买家已生成交付确认码',
            message=f'商品“{locked_order.item.title}”已生成新的交付确认码，请在现场向买家索取并核对。',
            order=locked_order,
            item=locked_order.item,
            target_url=reverse('order_detail', args=[locked_order.id]),
        )
    messages.success(request, f'交付确认码已生成：{raw_code}。请只在现场提供给卖家。')
    return redirect('order_detail', order_id=order.id)


@login_required
def confirm_delivery(request, order_id):
    order = get_object_or_404(Order.objects.select_related('item', 'buyer', 'seller'), id=order_id)
    if request.user not in {order.buyer, order.seller}:
        messages.error(request, '你没有权限确认这笔订单。')
        return redirect('home')
    if request.method != 'POST':
        return redirect('order_detail', order_id=order.id)
    if order.status != 'meeting':
        messages.error(request, '订单进入“待当面交付”后，才能确认交付。')
        return redirect('order_detail', order_id=order.id)

    with transaction.atomic():
        locked_order = Order.objects.select_for_update(of=('self',)).select_related('item', 'buyer', 'seller').get(pk=order.id)
        confirmation, _ = DeliveryConfirmation.objects.select_for_update(of=('self',)).get_or_create(order=locked_order)
        field_name = 'buyer_confirmed_at' if request.user == locked_order.buyer else 'seller_confirmed_at'
        if getattr(confirmation, field_name):
            messages.info(request, '你已经确认过这次交付了，请等待对方操作。')
            return redirect('order_detail', order_id=locked_order.id)
        code_used = False
        if request.user == locked_order.seller and confirmation.handoff_code_hash and not confirmation.handoff_code_used_at:
            if confirmation.handoff_code_attempts >= 5:
                messages.error(request, '交付确认码错误次数过多，请让买家重新生成一个新码。')
                return redirect('order_detail', order_id=locked_order.id)
            code_form = DeliveryCodeForm(request.POST)
            if not code_form.is_valid():
                messages.error(request, '请输入买家提供的 6 位数字交付确认码。')
                return redirect('order_detail', order_id=locked_order.id)
            if not check_password(code_form.cleaned_data['code'], confirmation.handoff_code_hash):
                confirmation.handoff_code_attempts += 1
                confirmation.save(update_fields=['handoff_code_attempts', 'updated_at'])
                remaining = max(0, 5 - confirmation.handoff_code_attempts)
                messages.error(request, f'交付确认码不正确，还可以尝试 {remaining} 次。')
                return redirect('order_detail', order_id=locked_order.id)
            confirmation.handoff_code_used_at = timezone.now()
            code_used = True
        setattr(confirmation, field_name, timezone.now())
        update_fields = [field_name, 'updated_at']
        if code_used:
            update_fields.append('handoff_code_used_at')
        confirmation.save(update_fields=update_fields)
        other_party = locked_order.seller if request.user == locked_order.buyer else locked_order.buyer

        if confirmation.is_complete:
            is_borrow = locked_order.item.trade_mode == 'borrow'
            next_status = 'borrowed' if is_borrow else 'completed'
            locked_order.status = next_status
            locked_order.save(update_fields=['status', 'updated_at'])
            if not is_borrow:
                contribution_phase = 'gift_completed' if locked_order.item.trade_mode == 'free' else 'trade_completed'
                record_order_contribution(locked_order, phase=contribution_phase)
            if not is_borrow:
                locked_order.item.status = 'sold'
                locked_order.item.save(update_fields=['status', 'updated_at'])
            OrderEvent.objects.create(
                order=locked_order,
                actor=request.user,
                from_status='meeting',
                to_status=next_status,
                note='双方确认借用已开始' if is_borrow else '双方确认交易已完成',
            )
            create_notification(
                other_party, actor=request.user, kind='order_status',
                title='借用已开始' if is_borrow else '交易已完成',
                message=(
                    f'商品“{locked_order.item.title}”已完成交付确认，借用期限至 '
                    f'{locked_order.return_due_at:%Y年%m月%d日 %H:%M}。'
                    if is_borrow else f'商品“{locked_order.item.title}”已完成双方交付确认。'
                ),
                order=locked_order, item=locked_order.item,
                target_url=reverse('order_detail', args=[locked_order.id]),
            )
            messages.success(
                request,
                '双方已完成交付确认，借用正式开始。' if is_borrow else '双方已完成交付确认，交易正式完成。',
            )
        else:
            create_notification(
                other_party, actor=request.user, kind='order_status',
                title='对方确认了交付',
                message=f'{request.user.username}已确认商品“{locked_order.item.title}”完成交付，请你确认。',
                order=locked_order, item=locked_order.item,
                target_url=reverse('order_detail', args=[locked_order.id]),
            )
            messages.success(request, '已记录你的交付确认，等待对方确认。')
    return redirect('order_detail', order_id=order.id)


@login_required
def confirm_return(request, order_id):
    """Let both parties independently confirm the return of a borrowed item."""
    order = get_object_or_404(
        Order.objects.select_related('item', 'buyer', 'seller'), id=order_id,
    )
    if request.user not in {order.buyer, order.seller}:
        messages.error(request, '你没有权限确认这笔借用订单。')
        return redirect('home')
    if request.method != 'POST':
        return redirect('order_detail', order_id=order.id)
    if order.item.trade_mode != 'borrow':
        messages.error(request, '只有限期借用订单支持归还确认。')
        return redirect('order_detail', order_id=order.id)
    if order.status != 'borrowed':
        messages.error(request, '订单进入“借用中”后才能确认归还。')
        return redirect('order_detail', order_id=order.id)

    with transaction.atomic():
        locked_order = Order.objects.select_for_update(of=('self',)).select_related(
            'item', 'buyer', 'seller',
        ).get(pk=order.id)
        if locked_order.item.trade_mode != 'borrow' or locked_order.status != 'borrowed':
            messages.info(request, '这笔借用订单已经发生变化，请刷新后再试。')
            return redirect('order_detail', order_id=locked_order.id)

        confirmation, _ = DeliveryConfirmation.objects.select_for_update(of=('self',)).get_or_create(
            order=locked_order,
        )
        field_name = 'buyer_returned_at' if request.user == locked_order.buyer else 'seller_returned_at'
        if getattr(confirmation, field_name):
            messages.info(request, '你已经登记过归还，请等待对方确认。')
            return redirect('order_detail', order_id=locked_order.id)

        setattr(confirmation, field_name, timezone.now())
        confirmation.save(update_fields=[field_name, 'updated_at'])
        other_party = locked_order.seller if request.user == locked_order.buyer else locked_order.buyer

        if confirmation.return_is_complete:
            now = timezone.now()
            locked_order.status = 'returned'
            locked_order.returned_at = now
            locked_order.save(update_fields=['status', 'returned_at', 'updated_at'])
            record_order_contribution(locked_order, phase='borrow_returned')
            # 归还确认是催收阶梯的终点：这一行只负责关闭，不删记录、不改等级，
            # 否则运营看板会继续把一笔已经还清的借用算成待办。
            close_escalation(locked_order, now=now)
            locked_order.item.status = 'available'
            locked_order.item.save(update_fields=['status', 'updated_at'])
            OrderEvent.objects.create(
                order=locked_order,
                actor=request.user,
                from_status='borrowed',
                to_status='returned',
                note='双方确认借用物品已归还，商品重新开放借用',
            )
            for recipient in (locked_order.buyer, locked_order.seller):
                create_notification(
                    recipient,
                    actor=request.user,
                    kind='order_status',
                    title='借用已归还',
                    message=f'商品“{locked_order.item.title}”已完成双方归还确认，现已重新开放借用。',
                    order=locked_order,
                    item=locked_order.item,
                    target_url=reverse('order_detail', args=[locked_order.id]),
                )
            messages.success(request, '双方已确认归还，商品重新开放借用。')
        else:
            create_notification(
                other_party,
                actor=request.user,
                kind='order_status',
                title='等待你确认借用归还',
                message=f'{request.user.username}已登记归还商品“{locked_order.item.title}”，请确认物品已收到。',
                order=locked_order,
                item=locked_order.item,
                target_url=reverse('order_detail', args=[locked_order.id]),
            )
            messages.success(request, '已记录你的归还登记，等待对方确认。')
    return redirect('order_detail', order_id=order.id)


@login_required
def open_dispute(request, order_id):
    order = get_object_or_404(Order.objects.select_related('item', 'buyer', 'seller'), id=order_id)
    if request.user not in {order.buyer, order.seller}:
        messages.error(request, '你没有权限发起这笔订单的争议。')
        return redirect('home')
    if order.status not in {'confirmed', 'meeting', 'completed', 'borrowed', 'returned'}:
        messages.error(request, '当前订单状态不支持发起交易争议。')
        return redirect('order_detail', order_id=order.id)
    if hasattr(order, 'dispute'):
        messages.info(request, '这笔订单已经有一条争议记录，请等待平台处理。')
        return redirect('order_detail', order_id=order.id)

    if request.method == 'POST':
        form = DisputeForm(request.POST)
        if form.is_valid():
            with transaction.atomic():
                dispute = form.save(commit=False)
                dispute.order = order
                dispute.opened_by = request.user
                dispute.save()
                OrderEvent.objects.create(
                    order=order,
                    actor=request.user,
                    from_status=order.status,
                    to_status=order.status,
                    note='交易争议已提交，等待平台处理',
                )
                other_party = order.seller if request.user == order.buyer else order.buyer
                create_notification(
                    other_party, actor=request.user, kind='order_dispute',
                    title='交易争议已提交',
                    message=f'{request.user.username}对商品“{order.item.title}”发起了交易争议。',
                    order=order, item=order.item,
                    target_url=reverse('order_detail', args=[order.id]),
                )
            messages.success(request, '争议已提交，平台会在后台核实处理。')
            return redirect('order_detail', order_id=order.id)
    else:
        form = DisputeForm()
    return render(request, 'listings/dispute_form.html', {
        'form': form,
        'order': order,
        'title': '发起交易争议',
    })


@login_required
def add_dispute_evidence(request, dispute_id):
    dispute = get_object_or_404(
        OrderDispute.objects.select_related('order__item', 'order__buyer', 'order__seller'),
        id=dispute_id,
    )
    order = dispute.order
    if request.user not in {order.buyer, order.seller}:
        messages.error(request, '你没有权限补充这条交易争议的证据。')
        return redirect('home')
    if dispute.status not in {'open', 'reviewing'}:
        messages.info(request, '这条争议已经结束，不能继续补充证据。')
        return redirect('order_detail', order_id=order.id)
    if request.method != 'POST':
        return redirect('order_detail', order_id=order.id)

    form = DisputeEvidenceForm(request.POST, request.FILES)
    if form.is_valid():
        with transaction.atomic():
            evidence = form.save(commit=False)
            evidence.dispute = dispute
            evidence.uploaded_by = request.user
            evidence.save()
            OrderEvent.objects.create(
                order=order,
                actor=request.user,
                from_status=order.status,
                to_status=order.status,
                note='补充了交易争议证据',
            )
            other_party = order.seller if request.user == order.buyer else order.buyer
            create_notification(
                other_party, actor=request.user, kind='order_dispute',
                title='交易争议有新的证据',
                message=f'{request.user.username}补充了商品“{order.item.title}”的争议证据。',
                order=order, item=order.item,
                target_url=reverse('order_detail', args=[order.id]),
            )
        messages.success(request, '证据已补充，相关参与人会收到提醒。')
    else:
        error_text = ' '.join(
            error for field_errors in form.errors.values() for error in field_errors
        )
        messages.error(request, f'证据未保存：{error_text or "请检查上传内容。"}')
    return redirect('order_detail', order_id=order.id)


@login_required
def download_dispute_evidence(request, evidence_id):
    evidence = get_object_or_404(
        OrderDisputeEvidence.objects.select_related('dispute__order__buyer', 'dispute__order__seller'),
        id=evidence_id,
    )
    order = evidence.dispute.order
    if request.user not in {order.buyer, order.seller} and not request.user.is_staff:
        raise PermissionDenied
    if not evidence.attachment:
        messages.error(request, '这份证据文件已不可用。')
        return redirect('order_detail', order_id=order.id)
    filename = Path(evidence.attachment.name).name
    content_type = mimetypes.guess_type(filename)[0] or 'application/octet-stream'
    response = FileResponse(
        evidence.attachment.open('rb'),
        as_attachment=True,
        filename=filename,
        content_type=content_type,
    )
    return response


@login_required
def my_reports(request):
    """Show a reporter only their own submissions and the one-shot follow-up."""
    reports = (
        Report.objects.filter(reporter=request.user)
        .select_related('item__seller', 'item__location', 'feedback')
        .order_by('-created_at', '-id')
    )
    paginator = Paginator(reports, 10)
    page_obj = paginator.get_page(request.GET.get('page'))
    return render(request, 'listings/my_reports.html', {
        'reports': page_obj.object_list,
        'page_obj': page_obj,
        'feedback_choices': ReportOutcomeFeedback.CHOICE_CHOICES,
        'title': '我的举报',
    })


@login_required
@require_POST
def submit_report_feedback(request, report_id):
    """Record one structured comprehension reply; never reopen the report."""
    report = get_object_or_404(
        Report.objects.select_related('item', 'reporter'),
        id=report_id,
        reporter=request.user,
    )
    choice = request.POST.get('choice')
    valid_choices = dict(ReportOutcomeFeedback.CHOICE_CHOICES)
    if choice not in valid_choices:
        messages.error(request, '请选择一个有效的举报结果反馈。')
    elif report.status not in {'resolved', 'rejected'}:
        messages.info(request, '这条举报还在处理中，暂时不能提交结果反馈。')
    else:
        try:
            _feedback, created = ReportOutcomeFeedback.objects.get_or_create(
                report=report,
                defaults={'choice': choice},
            )
        except IntegrityError:
            messages.info(request, '这条举报已经记录过结果反馈。')
        else:
            if created:
                messages.success(request, '感谢回访，你的反馈会帮助治理流程更好沟通。')
            else:
                messages.info(request, '这条举报已经记录过结果反馈，不能重复提交。')
    return redirect('my_reports')


@login_required
def report_list(request):
    if not request.user.is_staff:
        raise PermissionDenied
    all_reports = Report.objects.select_related(
        'item__seller', 'item__category', 'item__location', 'reporter', 'reviewer',
        'feedback',
    )
    status_filter = request.GET.get('status', 'all')
    if status_filter in dict(Report.STATUS_CHOICES):
        reports = all_reports.filter(status=status_filter)
        report_filter_query = urlencode({'status': status_filter})
    else:
        status_filter = 'all'
        reports = all_reports
        report_filter_query = ''
    report_page = build_report_page(
        reports,
        page_number=request.GET.get('page'),
    )
    page_obj = report_page['page_obj']
    return render(request, 'listings/reports.html', {
        'report_rows': report_page['rows'],
        'page_obj': page_obj,
        'page_numbers': _elided_page_numbers(page_obj),
        'report_filter_query': report_filter_query,
        'priority_counts': report_page['priority_counts'],
        'priority_labels': report_page['priority_labels'],
        'open_priority_total': report_page['open_total'],
        'status_filter': status_filter,
        'report_statuses': Report.STATUS_CHOICES,
        'pending_report_count': report_page['open_total'],
        'title': '商品举报审核',
    })


@login_required
def review_report(request, report_id):
    if not request.user.is_staff:
        raise PermissionDenied
    report = get_object_or_404(
        Report.objects.select_related(
            'item__seller', 'item__category', 'item__location', 'reporter', 'reviewer', 'feedback',
        ),
        id=report_id,
    )
    if request.method == 'POST':
        form = ReportReviewForm(request.POST, instance=report)
        if form.is_valid():
            with transaction.atomic():
                report = Report.objects.select_for_update(of=('self',)).select_related(
                    'item', 'reporter',
                ).get(pk=report.id)
                report.status = form.cleaned_data['status']
                report.review_note = form.cleaned_data['review_note']
                report.reviewer = request.user
                report.reviewed_at = timezone.now()
                report.save(update_fields=['status', 'review_note', 'reviewer', 'reviewed_at', 'updated_at'])
                create_notification(
                    report.reporter,
                    actor=request.user,
                    kind='report_update',
                    title='你提交的举报有处理进展',
                    message=f'关于商品“{report.item.title}”的举报状态已更新为“{report.get_status_display()}”。',
                    item=report.item,
                    target_url=reverse('my_reports'),
                )
            messages.success(request, '举报审核结果已保存，举报人会收到通知。')
            return redirect('report_list')
    else:
        form = ReportReviewForm(instance=report, initial={'status': 'reviewing'})
    return render(request, 'listings/report_review.html', {
        'form': form,
        'report': report,
        'priority': build_report_priority_for_report(report),
        'title': '审核商品举报',
    })


@login_required
def governance_workbench(request):
    """One workbench for the three moderation queues.

    Reports, disputes and incidents used to live on three separate pages with
    three different vocabularies, and incidents had no staff page at all. The
    workbench normalises them onto one shape and adds the dimension none of
    them had: how long each case has been waiting against its own deadline.

    The borrow queue is the only one without a staff action page, and that is
    deliberate: the platform records and notifies an overdue borrow but never
    cancels the order or touches the deposit, so the only useful thing an
    operator can do here is read the order timeline and go back to the two
    students. A button that pretended otherwise would be a lie in the UI.

    The statistics window follows the dashboard's period selector, but the open
    backlog is deliberately not filtered by it - overdue work opened before the
    period is exactly what an operator must not miss.
    """
    if not request.user.is_staff:
        raise PermissionDenied

    days = _operations_period_days(request)
    kind_filter = request.GET.get('kind', 'all')
    status_filter = request.GET.get('status', 'open')
    if kind_filter not in {'all', 'report', 'dispute', 'incident', 'borrow'}:
        kind_filter = 'all'
    if status_filter not in {'all', 'open', 'closed'}:
        status_filter = 'open'

    sla = build_governance_sla(days=days)
    rows = []
    for queue in sla['queues']:
        if kind_filter != 'all' and queue['kind'] != kind_filter:
            continue
        for case in queue['cases']:
            if status_filter == 'open' and not case['is_open']:
                continue
            if status_filter == 'closed' and case['is_open']:
                continue
            rows.append(case)
    rows.sort(key=lambda case: (0 if case['is_overdue'] else 1, case['created_at']))

    return render(request, 'listings/governance.html', {
        'governance': sla,
        'cases': rows,
        'kind_filter': kind_filter,
        'status_filter': status_filter,
        'days': days,
        'period_choices': PERIOD_CHOICES,
        'kind_choices': (
            ('all', '全部队列'),
            ('report', '商品举报'),
            ('dispute', '交易争议'),
            ('incident', '交付预约异常'),
            ('borrow', '借用逾期催收'),
        ),
        'status_choices': (
            ('open', '待处理'),
            ('closed', '已处理'),
            ('all', '全部状态'),
        ),
        'title': '治理工作台',
    })


@login_required
def review_meeting_incident(request, incident_id):
    """Handle a delivery incident from the workbench instead of the admin.

    The resolution mirrors dispute handling: the reviewer and the closing
    timestamp are written together, both sides are notified, and an OrderEvent
    records the outcome on the order timeline so the case stays auditable.
    """
    if not request.user.is_staff:
        raise PermissionDenied
    incident = get_object_or_404(
        MeetingIncident.objects.select_related(
            'appointment__order__item', 'appointment__order__buyer',
            'appointment__order__seller', 'reported_by', 'accused',
        ),
        id=incident_id,
    )
    if incident.status in {'resolved', 'dismissed'}:
        messages.info(request, '这条预约异常已经处理完成。')
        return redirect('governance_workbench')
    if request.method == 'POST':
        form = MeetingIncidentReviewForm(request.POST, instance=incident)
        if form.is_valid():
            with transaction.atomic():
                incident = MeetingIncident.objects.select_for_update(of=('self',)).select_related(
                    'appointment__order__item',
                ).get(pk=incident.id)
                incident.status = form.cleaned_data['status']
                incident.resolution_note = form.cleaned_data['resolution_note']
                incident.reviewer = request.user
                incident.reviewed_at = timezone.now()
                incident.save(update_fields=[
                    'status', 'resolution_note', 'reviewer', 'reviewed_at', 'updated_at',
                ])
                order = incident.appointment.order
                OrderEvent.objects.create(
                    order=order,
                    actor=request.user,
                    from_status=order.status,
                    to_status=order.status,
                    note=f'平台已将交付预约异常标记为“{incident.get_status_display()}”',
                )
                for recipient in {incident.reported_by, incident.accused}:
                    create_notification(
                        recipient, kind='meeting_incident',
                        title='交付预约异常处理结果已更新',
                        message=f'预约异常已{incident.get_status_display()}，可在订单时间线查看处理意见。',
                        order=order, item=order.item,
                        target_url=reverse('order_detail', args=[order.id]),
                    )
            messages.success(request, '预约异常处理结果已保存，双方会收到通知。')
            return redirect('governance_workbench')
    else:
        form = MeetingIncidentReviewForm(instance=incident, initial={'status': 'resolved'})
    return render(request, 'listings/incident_review.html', {
        'form': form,
        'incident': incident,
        'title': '处理交付预约异常',
    })


@login_required
def dispute_list(request):
    if not request.user.is_staff:
        raise PermissionDenied
    all_disputes = OrderDispute.objects.select_related(
        'order__item', 'order__buyer', 'order__seller', 'opened_by', 'reviewer',
    )
    status_filter = request.GET.get('status', 'all')
    if status_filter in dict(OrderDispute.STATUS_CHOICES):
        disputes = all_disputes.filter(status=status_filter)
        dispute_filter_query = urlencode({'status': status_filter})
    else:
        status_filter = 'all'
        disputes = all_disputes
        dispute_filter_query = ''
    disputes = disputes.annotate(
        dispute_queue_rank=Case(
            When(status='open', then=Value(0)),
            When(status='reviewing', then=Value(1)),
            default=Value(2),
            output_field=IntegerField(),
        ),
    ).order_by('dispute_queue_rank', '-created_at', '-pk')
    page_obj = Paginator(disputes, 12).get_page(request.GET.get('page'))
    return render(request, 'listings/disputes.html', {
        'disputes': page_obj.object_list,
        'page_obj': page_obj,
        'page_numbers': _elided_page_numbers(page_obj),
        'dispute_filter_query': dispute_filter_query,
        'status_filter': status_filter,
        'dispute_statuses': OrderDispute.STATUS_CHOICES,
        'pending_dispute_count': all_disputes.filter(
            status__in={'open', 'reviewing'},
        ).count(),
        'title': '交易争议处理',
    })


@login_required
def resolve_dispute(request, dispute_id):
    if not request.user.is_staff:
        raise PermissionDenied
    dispute = get_object_or_404(
        OrderDispute.objects.select_related('order__item', 'order__buyer', 'order__seller'),
        id=dispute_id,
    )
    if dispute.status in {'resolved', 'rejected'}:
        messages.info(request, '这条争议已经处理完成。')
        return redirect('dispute_list')
    if request.method == 'POST':
        form = DisputeResolutionForm(request.POST, instance=dispute)
        if form.is_valid():
            with transaction.atomic():
                dispute = OrderDispute.objects.select_for_update(of=('self',)).select_related(
                    'order__item', 'order__buyer', 'order__seller',
                ).get(pk=dispute.id)
                dispute.status = form.cleaned_data['status']
                dispute.resolution_note = form.cleaned_data['resolution_note']
                dispute.reviewer = request.user
                dispute.resolved_at = timezone.now()
                dispute.save(update_fields=['status', 'resolution_note', 'reviewer', 'resolved_at', 'updated_at'])
                OrderEvent.objects.create(
                    order=dispute.order,
                    actor=request.user,
                    from_status=dispute.order.status,
                    to_status=dispute.order.status,
                    note=f'平台已将交易争议标记为“{dispute.get_status_display()}”',
                )
                for recipient in {dispute.order.buyer, dispute.order.seller}:
                    create_notification(
                        recipient, kind='order_dispute',
                        title='交易争议处理结果已更新',
                        message=f'商品“{dispute.order.item.title}”的争议已{dispute.get_status_display()}。',
                        order=dispute.order, item=dispute.order.item,
                        target_url=reverse('order_detail', args=[dispute.order.id]),
                    )
            messages.success(request, '争议处理结果已保存，双方会收到通知。')
            return redirect('dispute_list')
    else:
        form = DisputeResolutionForm(instance=dispute, initial={'status': 'resolved'})
    return render(request, 'listings/dispute_resolve.html', {
        'form': form,
        'dispute': dispute,
        'title': '处理交易争议',
    })


@login_required
def rate_order(request, order_id):
    order = get_object_or_404(Order.objects.select_related('buyer', 'seller'), id=order_id)
    if request.user not in {order.buyer, order.seller}:
        messages.error(request, '你没有权限评价这笔订单。')
        return redirect('home')
    if order.status not in {'completed', 'returned'}:
        messages.error(request, '交易完成后才可以互相评价。')
        return redirect('order_detail', order_id=order.id)
    ratee = order.seller if request.user == order.buyer else order.buyer
    if Rating.objects.filter(order=order, rater=request.user).exists():
        messages.info(request, '你已经评价过这笔交易。')
        return redirect('order_detail', order_id=order.id)
    if request.method == 'POST':
        form = RatingForm(request.POST)
        if form.is_valid():
            rating = form.save(commit=False)
            rating.order = order
            rating.rater = request.user
            rating.ratee = ratee
            try:
                with transaction.atomic():
                    rating.save()
            except IntegrityError:
                messages.info(request, '你已经评价过这笔交易。')
            else:
                create_notification(
                    ratee, actor=request.user, kind='rating_received',
                    title='收到新的交易评价',
                    message=f'{request.user.username}给你留下了{rating.score}星评价。',
                    order=order, item=order.item,
                    target_url=reverse('item_detail', args=[order.item.id]),
                )
                messages.success(request, '评价已提交，感谢你的真实反馈。')
            return redirect('order_detail', order_id=order.id)
    else:
        form = RatingForm()
    return render(request, 'listings/rating_form.html', {
        'form': form,
        'order': order,
        'ratee': ratee,
        'title': '评价交易',
    })


@login_required
def update_order_status(request, order_id):
    order = get_object_or_404(Order.objects.select_related('item', 'buyer', 'seller'), id=order_id)
    if request.user not in {order.buyer, order.seller}:
        messages.error(request, '你没有权限操作这笔订单。')
        return redirect('home')
    if request.method == 'POST':
        try:
            order = transition_order(
                order_id=order.id,
                actor=request.user,
                target_status=request.POST.get('status'),
            )
        except OrderTransitionError as exc:
            messages.error(request, str(exc))
        else:
            messages.success(request, f'订单状态已更新为“{order.get_status_display()}”。')
    return redirect('order_detail', order_id=order.id)


@login_required
def contribution_center(request):
    return render(request, 'listings/contribution_center.html', {
        'summary': build_contribution_summary(request.user),
        'title': '我的互助贡献',
    })


@login_required
def circular_impact_report(request):
    return render(request, 'listings/circular_impact.html', {
        'report': build_circular_impact_report(request.user),
        'title': '校园循环影响力',
    })


def _record_opportunity_impressions(user, opportunities):
    """把这一屏被渲染出去的机会，一次性写进 `OpportunityImpression`。

    这是 `opportunity_dismissal_quality` 报忽略率所缺的那个分母。忽略表只记「忽略」、
    不记「看过」，一条机会被渲染出来而用户什么也没点，库里一行都没有，所以「窗口内被展示
    过的机会」一直不可得，那个模块只能报忽略自己的形状。

    三处口径，逐条都是为了让分母和分子说的是同一件事：

    - **只有成功渲染了才写。** 没有机会可渲染时这里拿到的是空列表，一次 insert 都不发。
    - **每次渲染一行，不去重。** 同一条机会在不同时刻被渲染给同一个人是两次独立的展示，
      去重会让分母变小、忽略率虚高。这正是它和 `OpportunityDismissal` 上那两个无条件
      唯一约束的区别：忽略该折叠，展示不该。
    - **一次 `bulk_create`。** 一屏最多 12 条，一次渲染就是一次 round-trip，不随用户在售
      商品数增长；也不为这个面板多跑任何一次 select。

    失败不致命：曝光写不进去，页面照常渲染，只是这一次展示进不了分母。用
    `transaction.TransactionManagementError` 之外的所有异常兜住，是因为这里跑在请求里，
    一次写入失败不该把整页机会流变成 500。
    """
    rows = []
    for opportunity in opportunities:
        rows.append(OpportunityImpression(
            user=user,
            kind=opportunity['kind'],
            demand_id=opportunity['target'].id if opportunity['kind'] == 'demand' else None,
            lost_found_post_id=(
                opportunity['target'].id if opportunity['kind'] == 'lost_found' else None
            ),
            shown_at=timezone.now(),
        ))
    if not rows:
        return
    try:
        OpportunityImpression.objects.bulk_create(rows, batch_size=32)
    except Exception:
        # 只报告、不影响用户：写不进去的分母只是让这一周期的忽略率偏低，见 docstring。
        logger.exception('记录互助机会展示失败：用户 %s，%d 条机会', user.pk, len(rows))


@login_required
def opportunity_feed(request):
    feed = build_opportunity_feed(request.user)
    _record_opportunity_impressions(request.user, feed['opportunities'])
    return render(request, 'listings/opportunity_feed.html', {
        'feed': feed,
        'title': '校园互助机会',
    })


@login_required
def dismiss_opportunity(request):
    if request.method == 'POST':
        kind = request.POST.get('kind', '').strip()
        target_id = request.POST.get('target_id', '').strip()
        if kind == 'demand':
            target = get_object_or_404(DemandPost, pk=target_id)
            filters = {'demand': target, 'lost_found_post': None}
        elif kind == 'lost_found':
            target = get_object_or_404(LostFoundPost, pk=target_id)
            filters = {'demand': None, 'lost_found_post': target}
        else:
            messages.error(request, '无法识别这条互助机会。')
            return redirect('opportunity_feed')
        OpportunityDismissal.objects.update_or_create(
            user=request.user,
            **filters,
            defaults={
                'kind': kind,
                'expires_at': timezone.now() + timedelta(days=30),
            },
        )
        messages.success(request, '这条机会将在 30 天内暂不展示，你仍可以通过原页面访问源记录。')
    return redirect('opportunity_feed')


@login_required
def my_orders(request):
    state_groups = {
        'active': set(Order.ACTIVE_STATUS_VALUES),
        'completed': {'completed', 'returned'},
        'cancelled': {'cancelled'},
    }
    base_orders = Order.objects.filter(Q(buyer=request.user) | Q(seller=request.user))
    order_counts = base_orders.aggregate(
        all=Count('id'),
        active=Count('id', filter=Q(status__in=state_groups['active'])),
        completed=Count('id', filter=Q(status__in=state_groups['completed'])),
        cancelled=Count('id', filter=Q(status__in=state_groups['cancelled'])),
        buying=Count('id', filter=Q(buyer=request.user)),
        selling=Count('id', filter=Q(seller=request.user)),
    )

    selected_state = request.GET.get('state', 'all')
    if selected_state not in {'all', *state_groups}:
        selected_state = 'all'

    orders_query = (
        base_orders
        .select_related('item', 'buyer', 'seller', 'meeting_location')
        .prefetch_related('item__images')
        .order_by('-created_at', '-pk')
    )
    if selected_state != 'all':
        orders_query = orders_query.filter(status__in=state_groups[selected_state])

    page_obj = Paginator(orders_query, 10).get_page(request.GET.get('page'))
    orders = list(page_obj.object_list)
    now = timezone.now()

    for order in orders:
        is_buyer = order.buyer_id == request.user.id
        order.viewer_role = 'buyer' if is_buyer else 'seller'
        order.viewer_role_label = '我买到' if is_buyer else '我卖出'
        order.counterparty = order.seller if is_buyer else order.buyer
        order.counterparty_role = '卖家' if is_buyer else '买家'
        if order.status in state_groups['active']:
            order.state_group = 'active'
        elif order.status in state_groups['completed']:
            order.state_group = 'completed'
        else:
            order.state_group = 'cancelled'

        if order.status == 'pending':
            order.next_step = '等待卖家确认预约' if is_buyer else '核对预约并确认或取消'
        elif order.status == 'confirmed':
            order.next_step = '与卖家约定交付时间' if is_buyer else '联系买家并安排交付'
        elif order.status == 'meeting':
            order.next_step = '按约定完成当面交付，双方确认后订单才会完成'
        elif order.status == 'borrowed':
            order.next_step = '按约定时间归还物品' if is_buyer else '关注归还时间并确认收回'
        elif order.status == 'completed':
            order.next_step = '交易已完成，可回看订单与评价记录'
        elif order.status == 'returned':
            order.next_step = '借用已归还，这笔交易已经闭环'
        else:
            order.next_step = '订单已取消，可以回到发现页继续寻找'

        deadline = None
        if order.status == 'pending':
            deadline = order.confirmation_deadline
            order.deadline_label = '卖家确认截止'
        elif order.status == 'borrowed':
            deadline = order.return_due_at
            order.deadline_label = '预计归还'
        order.deadline_at = deadline
        order.is_overdue = bool(deadline and deadline < now)

    return render(request, 'listings/my_orders.html', {
        'orders': orders,
        'order_counts': order_counts,
        'selected_state': selected_state,
        'page_obj': page_obj,
        'page_numbers': _elided_page_numbers(page_obj),
        'title': '我的交易',
    })


@login_required
def favorite_list(request):
    collections = FavoriteCollection.objects.filter(user=request.user).annotate(
        favorite_count=Count('favorites'),
    )
    selected_collection = None
    collection_id = request.GET.get('collection', '').strip()
    if collection_id:
        try:
            selected_collection = collections.get(pk=int(collection_id))
        except (TypeError, ValueError, FavoriteCollection.DoesNotExist):
            selected_collection = None

    all_favorites = Favorite.objects.filter(user=request.user)
    favorite_total_count = all_favorites.count()
    favorites = all_favorites.select_related(
        'item__category', 'item__seller', 'item__location', 'collection',
    ).prefetch_related('item__images').order_by('-created_at', '-pk')
    if selected_collection:
        favorites = favorites.filter(collection=selected_collection)
    page_obj = Paginator(favorites, 12).get_page(request.GET.get('page'))
    context = {
        'favorites': page_obj.object_list,
        'favorite_ids': set(all_favorites.values_list('item_id', flat=True)),
        'favorite_total_count': favorite_total_count,
        'collections': collections,
        'selected_collection': selected_collection,
        'collection_form': FavoriteCollectionForm(),
        'page_obj': page_obj,
        'page_numbers': _elided_page_numbers(page_obj),
        'title': '我的心愿单',
    }
    return render(request, 'listings/favorite_list.html', context)


@login_required
def create_favorite_collection(request):
    if request.method != 'POST':
        return redirect('favorite_list')
    form = FavoriteCollectionForm(request.POST)
    if form.is_valid():
        collection, created = FavoriteCollection.objects.get_or_create(
            user=request.user, name=form.cleaned_data['name'],
        )
        if created:
            messages.success(request, f'已创建“{collection.name}”分组。')
        else:
            messages.info(request, f'“{collection.name}”分组已经存在。')
    else:
        messages.error(request, '分组名称不能为空，且不能超过 40 个字。')
    return redirect('favorite_list')


@login_required
def update_favorite_collection(request, favorite_id):
    favorite = get_object_or_404(Favorite, pk=favorite_id, user=request.user)
    if request.method == 'POST':
        collection_id = request.POST.get('collection_id', '').strip()
        collection = None
        if collection_id:
            collection = get_object_or_404(
                FavoriteCollection, pk=collection_id, user=request.user,
            )
        favorite.collection = collection
        # `grouped_at` 由 Favorite.save() 在 collection_id 变化时自己写下，这里
        # 不用显式赋值。两条写入路径（这个视图和 Django admin）都靠它覆盖，见
        # 模型上该字段的注释。
        favorite.save(update_fields=['collection'])
        messages.success(request, '已更新心愿单分组。')
    next_url = request.POST.get('next') or request.META.get('HTTP_REFERER') or ''
    if not url_has_allowed_host_and_scheme(next_url, allowed_hosts={request.get_host()}):
        next_url = ''
    return redirect(next_url or 'favorite_list')


@login_required
def new_item(request):
    if request.method == 'POST':
        form = ItemForm(request.POST)
        formset = ItemImageFormSet(request.POST, request.FILES)
        if form.is_valid() and formset.is_valid():
            item = form.save(commit=False)
            item.seller = request.user
            item.save()
            for image_form in formset:
                if image_form.cleaned_data and image_form.cleaned_data.get('image'):
                    image = image_form.save(commit=False)
                    image.item = item
                    image.save()
            notify_saved_search_matches(item)
            notify_demand_matches(item)
            queue_campaign_update_matches(item)
            messages.success(request, '商品已成功发布，快去分享给同学吧！')
            return redirect('item_detail', item_id=item.id)
    else:
        form = ItemForm()
        formset = ItemImageFormSet()
    return render(request, 'listings/item_form.html', {'form': form, 'formset': formset, 'title': '发布新商品'})


@login_required
def edit_item(request, item_id):
    item = get_object_or_404(Item.objects.select_related('seller'), id=item_id)
    if item.seller != request.user:
        messages.error(request, '您没有权限编辑此商品。')
        return redirect('item_detail', item_id=item.id)
    if request.method == 'POST':
        form = ItemForm(request.POST, instance=item)
        formset = ItemImageFormSet(request.POST, request.FILES, instance=item)
        if form.is_valid() and formset.is_valid():
            price_change = None
            with transaction.atomic():
                # Lock the row only for the main listing edit. The old price is
                # read after locking so two concurrent edits cannot both use the
                # same stale “before” value as their trajectory source. Rebinding
                # the already validated POST data to the locked instance keeps
                # concurrent safety without losing the seller's submitted price.
                locked_item = Item.objects.select_for_update(of=('self',)).get(pk=item.pk)
                previous_price = locked_item.price
                previous_trade_mode = locked_item.trade_mode
                previous_campaign_id = locked_item.campaign_id
                locked_form = ItemForm(request.POST, instance=locked_item)
                if not locked_form.is_valid():
                    messages.error(request, '商品信息刚被其他修改更新，请确认内容后重试。')
                    return render(request, 'listings/item_form.html', {
                        'form': locked_form,
                        'formset': formset,
                        'title': '编辑商品',
                        'item': item,
                    })

                saved_item = locked_form.save()
                price_change = record_item_price_change(
                    saved_item,
                    old_price=previous_price,
                    changed_by=request.user,
                    old_trade_mode=previous_trade_mode,
                )
                if saved_item.status == 'expired' and saved_item.expires_at and saved_item.expires_at > timezone.now():
                    saved_item.status = 'available'
                    saved_item.save(update_fields=['status', 'updated_at'])
                    messages.info(request, '商品已重新上架，并按新的展示截止时间继续展示。')

            # The edit and its trajectory are committed first. A notification
            # problem is logged by the helper and must not roll back either one.
            if saved_item.campaign_id and saved_item.campaign_id != previous_campaign_id:
                queue_campaign_update_matches(saved_item)
            if price_change is not None:
                notify_item_price_drop(saved_item, price_change, actor=request.user)
            formset.instance = saved_item
            formset.save()
            messages.success(request, '商品信息已更新。')
            return redirect('item_detail', item_id=item.id)
    else:
        form = ItemForm(instance=item)
        formset = ItemImageFormSet(instance=item)
    return render(request, 'listings/item_form.html', {'form': form, 'formset': formset, 'title': '编辑商品', 'item': item})


@login_required
def delete_item(request, item_id):
    item = get_object_or_404(Item.objects.select_related('seller'), id=item_id)
    if item.seller != request.user:
        messages.error(request, '您没有权限删除此商品。')
        return redirect('item_detail', item_id=item.id)
    if request.method == 'POST':
        item.delete()
        messages.success(request, '商品已成功删除。')
        return redirect('my_items')
    return render(request, 'listings/item_confirm_delete.html', {'item': item})


@login_required
def mark_sold(request, item_id):
    item = get_object_or_404(Item.objects.select_related('seller'), id=item_id)
    if item.seller != request.user:
        messages.error(request, '您没有权限更改此商品状态。')
        return redirect('item_detail', item_id=item.id)
    if request.method == 'POST':
        status = request.POST.get('status')
        if status in dict(Item.STATUS_CHOICES):
            if status == 'available' and item.expires_at and item.expires_at <= timezone.now():
                messages.error(request, '展示截止时间已到，请先编辑商品并设置未来时间。')
                return redirect('item_detail', item_id=item.id)
            previous_status = item.status
            item.status = status
            item.save(update_fields=['status', 'updated_at'])
            if previous_status != 'available' and status == 'available':
                notified_count = notify_item_available(item, actor=request.user)
                if notified_count:
                    messages.info(request, f'商品状态已更新为{item.get_status_display()}，已通知 {notified_count} 位关注者。')
                    return redirect('item_detail', item_id=item.id)
            messages.success(request, f'商品状态已更新为{item.get_status_display()}。')
        return redirect('item_detail', item_id=item.id)
    return render(request, 'listings/mark_sold.html', {'item': item})


@login_required
def browsing_history(request):
    history = BrowsingHistory.objects.filter(user=request.user).select_related(
        'item__category', 'item__seller', 'item__location',
    ).prefetch_related('item__images').order_by('-last_viewed_at', '-pk')
    page_obj = Paginator(history, 24).get_page(request.GET.get('page'))
    return render(request, 'listings/browsing_history.html', {
        'history': page_obj.object_list,
        'page_obj': page_obj,
        'page_numbers': _elided_page_numbers(page_obj),
        'title': '最近浏览',
    })


@login_required
def refresh_item(request, item_id):
    """Push a listing back to the front of the browse order.

    Refreshing is deliberately rate limited: it exists so sellers can promote
    a listing they have genuinely improved, not to let anyone pin a listing
    to the top permanently.  The limits live in the lifecycle module and are
    explained back to the seller when a refresh is rejected.
    """
    item = get_object_or_404(Item, pk=item_id, seller=request.user)
    if request.method != 'POST':
        return redirect('my_items')

    can_refresh, reason = _refresh_state(item, now=timezone.now())
    if not can_refresh:
        messages.warning(request, reason)
        return _my_items_workspace_redirect(request)

    now = timezone.now()
    with transaction.atomic():
        locked = Item.objects.select_for_update(of=('self',)).get(pk=item.pk)
        can_refresh, reason = _refresh_state(locked, now=now)
        if not can_refresh:
            messages.warning(request, reason)
            return _my_items_workspace_redirect(request)
        locked.last_refreshed_at = now
        locked.refresh_count = locked.refresh_count + 1
        locked.save(update_fields=['last_refreshed_at', 'refresh_count', 'updated_at'])
        ItemRefreshEvent.objects.create(
            item=locked,
            seller_id=locked.seller_id,
            refreshed_at=now,
            refresh_count_after=locked.refresh_count,
        )

    messages.success(request, f'已刷新《{item.title}》，商品会重新回到列表前排。')
    return _my_items_workspace_redirect(request)


BULK_LISTING_IMPORT_SESSION_KEY = 'listings:bulk_listing_csv_import'
BULK_LISTING_IMPORT_MAX_ROWS = 20
BULK_LISTING_IMAGE_QUEUE_SESSION_KEY = 'listings:bulk_listing_image_queue'


def _load_bulk_listing_import(request, token=None):
    """Return the current import draft after its shape and optional token check."""
    state = request.session.get(BULK_LISTING_IMPORT_SESSION_KEY)
    if not isinstance(state, dict):
        return None
    state_token = state.get('token')
    rows = state.get('rows')
    if not isinstance(state_token, str) or not isinstance(rows, list):
        return None
    if not 1 <= len(rows) <= BULK_LISTING_IMPORT_MAX_ROWS:
        return None
    if not all(isinstance(row, dict) for row in rows):
        return None
    if token is not None and not secrets.compare_digest(state_token, str(token)):
        return None
    return state


def _store_bulk_listing_import(request, rows):
    token = secrets.token_urlsafe(18)
    request.session[BULK_LISTING_IMPORT_SESSION_KEY] = {
        'token': token,
        'rows': rows,
    }
    return token


def _clear_bulk_listing_import(request, token=None):
    state = _load_bulk_listing_import(request, token=token)
    if state is not None:
        del request.session[BULK_LISTING_IMPORT_SESSION_KEY]


def _load_bulk_listing_image_queue(request):
    """Return the current seller's short-lived image follow-up queue, if valid."""
    state = request.session.get(BULK_LISTING_IMAGE_QUEUE_SESSION_KEY)
    if not isinstance(state, dict):
        return None

    seller_id = state.get('seller_id')
    item_ids = state.get('item_ids')
    published_at = parse_datetime(state.get('published_at', ''))
    if type(seller_id) is not int or seller_id != request.user.id:
        return None
    if not isinstance(item_ids, list) or not 1 <= len(item_ids) <= BULK_LISTING_IMPORT_MAX_ROWS:
        return None
    if not all(type(item_id) is int for item_id in item_ids):
        return None
    if published_at is None:
        return None
    if timezone.is_naive(published_at):
        published_at = timezone.make_aware(published_at)

    return {
        'seller_id': seller_id,
        'item_ids': item_ids,
        'published_at': published_at,
    }


def _store_bulk_listing_image_queue(request, items):
    """Keep only the latest batch in the session; this is not a persistent task."""
    request.session[BULK_LISTING_IMAGE_QUEUE_SESSION_KEY] = {
        'seller_id': request.user.id,
        'item_ids': [item.id for item in items],
        'published_at': timezone.now().isoformat(),
    }


BULK_LISTING_TEMPLATE_LIMIT = 10


def _semester_template_initial(template):
    """Turn a stored term rhythm into editable form values for today."""
    initial = {
        'category': template.category_id,
        'location': None,
        'campaign': None,
    }
    skipped = []
    if template.location_id and template.location.is_active:
        initial['location'] = template.location_id
    elif template.location_id:
        skipped.append('交易地点')
    if template.campaign_id and template.campaign.is_active:
        initial['campaign'] = template.campaign_id
    elif template.campaign_id:
        skipped.append('校园专题')
    if template.expiry_days is not None:
        initial['expires_at'] = timezone.now() + timedelta(days=template.expiry_days)
    return initial, skipped


def _render_bulk_create_page(
    request,
    *,
    settings_form=None,
    formset=None,
    import_file_error='',
    import_row_errors=(),
    import_preview_count=0,
    import_token='',
    import_error_summary=None,
    semester_template_name='',
):
    return render(request, 'listings/bulk_item_create.html', {
        'settings_form': settings_form if settings_form is not None else BulkListingSettingsForm(),
        'formset': formset if formset is not None else BulkListingRowFormSet(),
        'max_bulk_rows': BULK_LISTING_IMPORT_MAX_ROWS,
        'title': '批量发布商品',
        'import_file_error': import_file_error,
        'import_row_errors': import_row_errors,
        'import_preview_count': import_preview_count,
        'import_token': import_token,
        'import_error_summary': import_error_summary,
        'semester_template_name': semester_template_name,
        'semester_templates': SemesterListingTemplate.objects.filter(
            seller=request.user,
        ).select_related('category', 'location', 'campaign')[:BULK_LISTING_TEMPLATE_LIMIT],
    })


@login_required
def bulk_create_items(request):
    """一次创建最多 20 件商品；全部校验通过前不写入任何一行。"""
    import_token = request.POST.get('bulk_import_token', '').strip()
    if request.method == 'POST':
        settings_form = BulkListingSettingsForm(request.POST)
        formset = BulkListingRowFormSet(request.POST)
        if settings_form.is_valid() and formset.is_valid():
            rows = [
                form for form in formset
                if form.cleaned_data.get('title') and not form.cleaned_data.get('DELETE')
            ]
            settings = settings_form.cleaned_data
            with transaction.atomic():
                items = Item.objects.bulk_create([
                    Item(
                        seller=request.user,
                        status='available',
                        category=settings['category'],
                        location=settings.get('location'),
                        campaign=settings.get('campaign'),
                        expires_at=settings.get('expires_at'),
                        title=row.cleaned_data['title'],
                        description=row.cleaned_data['description'],
                        trade_mode=row.cleaned_data['trade_mode'],
                        price=row.cleaned_data['price'],
                        deposit_amount=row.cleaned_data['deposit_amount'],
                        borrow_days=row.cleaned_data['borrow_days'],
                        condition=row.cleaned_data['condition'],
                    )
                    for row in rows
                ])

            # 匹配通知不属于商品事实事务的一部分；这里逐件复用单件发布的
            # 通知路径，避免批量发布绕过原有偏好、免打扰和去重规则。
            for item in items:
                notify_saved_search_matches(item)
                notify_demand_matches(item)
                queue_campaign_update_matches(item)

            _clear_bulk_listing_import(request, token=import_token)
            _store_bulk_listing_image_queue(request, items)
            messages.success(request, f'已批量发布 {len(items)} 件商品。')
            return redirect('bulk_listing_image_queue')

        import_state = _load_bulk_listing_import(request, token=import_token)
        preview_count = len(import_state['rows']) if import_state else 0
        return _render_bulk_create_page(
            request,
            settings_form=settings_form,
            formset=formset,
            import_preview_count=preview_count,
            import_token=import_token if preview_count else '',
        )

    settings_form = None
    raw_template_id = request.GET.get('template', '').strip()
    if raw_template_id.isdigit():
        template = SemesterListingTemplate.objects.filter(
            seller=request.user, pk=raw_template_id,
        ).select_related('category', 'location', 'campaign').first()
        if template is None:
            messages.warning(request, '这份学期模板不存在或已删除。')
        else:
            initial, skipped_fields = _semester_template_initial(template)
            settings_form = BulkListingSettingsForm(initial=initial)
            if skipped_fields:
                messages.warning(
                    request,
                    f'模板中的{"、".join(skipped_fields)}已停用，本次未回填；其他共同设置已带入。',
                )

    return _render_bulk_create_page(request, settings_form=settings_form)


def _parse_bulk_listing_import(upload) -> BulkListingCsvParseResult:
    """Dispatch one bounded upload to the CSV or XLSX parser by extension."""
    if upload is None:
        return BulkListingCsvParseResult(file_error='请选择要导入的 CSV / XLSX 文件。')

    extension = Path(upload.name).suffix.lower()
    if extension == '.csv':
        return parse_bulk_listing_csv(upload)
    if extension == '.xlsx':
        return parse_bulk_listing_xlsx(upload)
    return BulkListingCsvParseResult(file_error='仅支持 .csv 或 .xlsx 文件，请重新选择。')


@login_required
@require_POST
def bulk_listing_semester_template_save(request):
    """Save only the common settings; listing rows stay outside this write."""
    template_name = request.POST.get('semester_template_name', '').strip()
    settings_form = BulkListingSettingsForm(request.POST)

    invalid_name = not template_name or len(template_name) > 30
    if settings_form.is_valid():
        expires_at = settings_form.cleaned_data.get('expires_at')
        expiry_days = None
        if expires_at is not None:
            expiry_days = ceil((expires_at - timezone.now()).total_seconds() / 86400)
            if expiry_days > 365:
                settings_form.add_error(
                    'expires_at', '学期模板最多保存当前时间后 365 天。'
                )

    if invalid_name or not settings_form.is_valid():
        if invalid_name:
            messages.warning(request, '请填写 1 到 30 个字符的模板名称。')
        else:
            messages.warning(request, '学期模板未保存：请先修正本批次共同设置。')
        formset = BulkListingRowFormSet(request.POST) if 'form-TOTAL_FORMS' in request.POST else None
        return _render_bulk_create_page(
            request,
            settings_form=settings_form,
            formset=formset,
            semester_template_name=template_name,
        )

    settings = settings_form.cleaned_data
    with transaction.atomic():
        seller_templates = SemesterListingTemplate.objects.select_for_update(of=('self',)).filter(
            seller=request.user,
        )
        template = seller_templates.filter(name=template_name).first()
        created = template is None
        if created and seller_templates.count() >= BULK_LISTING_TEMPLATE_LIMIT:
            messages.warning(
                request,
                f'每名卖家最多保留 {BULK_LISTING_TEMPLATE_LIMIT} 个学期模板，请先删除一个或覆盖同名模板。',
            )
            formset = BulkListingRowFormSet(request.POST) if 'form-TOTAL_FORMS' in request.POST else None
            return _render_bulk_create_page(
                request,
                settings_form=settings_form,
                formset=formset,
                semester_template_name=template_name,
            )
        if created:
            template = SemesterListingTemplate.objects.create(
                seller=request.user,
                name=template_name,
                category=settings['category'],
                location=settings.get('location'),
                campaign=settings.get('campaign'),
                expiry_days=expiry_days,
            )
        else:
            template.name = template_name
            template.category = settings['category']
            template.location = settings.get('location')
            template.campaign = settings.get('campaign')
            template.expiry_days = expiry_days
            template.save()

    action = '已创建' if created else '已覆盖'
    messages.success(request, f'{action}学期模板“{template_name}”。')
    return redirect(f"{reverse('bulk_create_items')}?template={template.id}")


@login_required
def bulk_listing_semester_template_delete(request, template_id):
    if request.method != 'POST':
        return redirect('bulk_create_items')
    template = get_object_or_404(
        SemesterListingTemplate, pk=template_id, seller=request.user,
    )
    template.delete()
    messages.success(request, f'已删除学期模板“{template.name}”。')
    return redirect('bulk_create_items')


@login_required
@require_POST
def bulk_create_items_import(request):
    """Parse a CSV or XLSX draft; never create items or notifications here."""
    result = _parse_bulk_listing_import(request.FILES.get('import_file'))
    if not result.can_preview:
        formset = BulkListingRowFormSet(initial=result.rows) if result.rows else None
        return _render_bulk_create_page(
            request,
            formset=formset,
            import_file_error=result.file_error,
            import_row_errors=[error.display for error in result.row_errors],
            import_error_summary=build_bulk_listing_import_error_summary(
                result.row_errors,
            ),
        )

    token = _store_bulk_listing_import(request, result.rows)
    return redirect('bulk_create_items_import_preview', token=token)


@login_required
@require_GET
def bulk_listing_import_template(request, template_format):
    """Generate the fixed-header CSV/XLSX template without storing a file."""
    try:
        template = build_bulk_listing_import_template(template_format)
    except ValueError as exc:
        raise Http404(str(exc)) from exc

    response = HttpResponse(template.content, content_type=template.content_type)
    response['Content-Disposition'] = f'attachment; filename="{template.filename}"'
    response['Cache-Control'] = 'no-store'
    return response


@login_required
@require_GET
def bulk_create_items_import_preview(request, token):
    state = _load_bulk_listing_import(request, token=token)
    if state is None:
        messages.warning(request, '这份导入预览已失效，请重新上传文件。')
        return redirect('bulk_create_items')
    return _render_bulk_create_page(
        request,
        formset=BulkListingRowFormSet(initial=state['rows']),
        import_preview_count=len(state['rows']),
        import_token=state['token'],
    )

def _build_bulk_listing_image_queue_context(queue):
    """Build the queue's latest facts for both GET and failed POST renders."""
    now = timezone.now()
    items = Item.objects.filter(
        seller=queue['seller_id'],
        id__in=queue['item_ids'],
    ).annotate(
        image_count=Count('images'),
    ).order_by('pk')
    items_by_id = {item.id: item for item in items}

    pending_items = []
    completed_count = 0
    not_actionable_count = 0
    for item_id in queue['item_ids']:
        item = items_by_id.get(item_id)
        if item is None:
            not_actionable_count += 1
            continue
        if item.image_count:
            completed_count += 1
            continue
        still_displayable = item.status in ('available', 'reserved') and not (
            item.expires_at and item.expires_at <= now
        )
        if still_displayable:
            pending_items.append(item)
        else:
            not_actionable_count += 1

    actionable_count = completed_count + len(pending_items)
    progress_percent = (
        int(completed_count * 100 / actionable_count) if actionable_count else 100
    )
    return {
        'title': '本批次补图',
        'published_at': timezone.localtime(queue['published_at']),
        'published_count': len(queue['item_ids']),
        'pending_items': pending_items,
        'completed_count': completed_count,
        'pending_count': len(pending_items),
        'not_actionable_count': not_actionable_count,
        'actionable_count': actionable_count,
        'progress_percent': progress_percent,
    }


def _render_bulk_listing_image_queue(request, queue, *, image_errors=None):
    context = _build_bulk_listing_image_queue_context(queue)
    image_errors = image_errors or {}
    for item in context['pending_items']:
        item.bulk_image_error = image_errors.get(item.id, '')
    return render(request, 'listings/bulk_listing_images.html', context)


@login_required
def bulk_listing_image_queue(request):
    """Upload one explicitly chosen photo per item in the latest batch queue."""
    queue = _load_bulk_listing_image_queue(request)
    if queue is None:
        messages.info(request, '本批次补图队列已失效，可以在“我的商品”里继续管理商品。')
        return redirect('my_items')

    if request.method == 'POST':
        context = _build_bulk_listing_image_queue_context(queue)
        pending_items_by_id = {
            item.id: item for item in context['pending_items']
        }
        uploads = {}
        image_errors = {}
        for item_id, item in pending_items_by_id.items():
            uploaded = request.FILES.get(f'item_image_{item_id}')
            if uploaded is None:
                continue
            try:
                uploads[item_id] = normalize_item_image(uploaded)
            except ItemImageLimitError:
                image_errors[item_id] = (
                    '图片过大：单张不能超过 10 MB 或 2000 万像素。'
                )
            except Exception:
                # Pillow errors can expose paths or decoder details. Keep the
                # row actionable without leaking implementation information.
                image_errors[item_id] = '图片处理失败，请换一张清晰的图片再试。'

        if image_errors:
            messages.error(
                request,
                '本次选择的图片全部未保存：请先修正标出的商品行，再重新选择文件提交。',
            )
            return _render_bulk_listing_image_queue(
                request, queue, image_errors=image_errors,
            )
        if not uploads:
            messages.warning(request, '请先至少为一行待补图商品选择图片。')
            return _render_bulk_listing_image_queue(request, queue)

        saved_count = 0
        with transaction.atomic():
            locked_items = {
                item.id: item
                for item in Item.objects.select_for_update(of=('self',)).filter(
                    seller=request.user,
                    id__in=uploads,
                )
            }
            items_with_images = set(ItemImage.objects.filter(
                item_id__in=uploads,
            ).values_list('item_id', flat=True))
            now = timezone.now()
            for item_id, image in uploads.items():
                item = locked_items.get(item_id)
                if item is None or item_id in items_with_images:
                    continue
                still_displayable = (
                    item.status in ('available', 'reserved')
                    and not (item.expires_at and item.expires_at <= now)
                )
                if not still_displayable:
                    continue
                ItemImage.objects.create(item=item, image=image)
                saved_count += 1

        if saved_count:
            messages.success(
                request, f'已为 {saved_count} 件商品补上图片，队列进度已更新。'
            )
        else:
            messages.info(
                request, '这些商品刚被其他页面处理过，队列已按最新状态刷新。'
            )
        skipped_count = len(uploads) - saved_count
        if skipped_count:
            messages.info(
                request,
                f'有 {skipped_count} 行因商品状态或图片状态变化未写入，请以当前队列为准。',
            )
        return redirect('bulk_listing_image_queue')

    return _render_bulk_listing_image_queue(request, queue)


@login_required
def my_items(request):
    tab_configs = {
        'all': {
            'status': None,
            'label': '全部商品',
            'caption': '我的商品 · 全部',
            'empty': '您还没有发布任何商品',
        },
        'available': {
            'status': 'available',
            'label': '在售商品',
            'caption': '我的商品 · 在售',
            'empty': '没有在售状态的商品',
        },
        'reserved': {
            'status': 'reserved',
            'label': '已预订商品',
            'caption': '我的商品 · 已预订',
            'empty': '没有已预订状态的商品',
        },
        'sold': {
            'status': 'sold',
            'label': '已售出商品',
            'caption': '我的商品 · 已售出',
            'empty': '没有已售出状态的商品',
        },
        'expired': {
            'status': 'expired',
            'label': '已过期商品',
            'caption': '我的商品 · 已过期',
            'empty': '没有已过期状态的商品',
        },
    }
    selected_tab = request.GET.get('tab', 'all')
    if selected_tab not in tab_configs:
        selected_tab = 'all'
    selected_config = tab_configs[selected_tab]

    seller_items = Item.objects.filter(seller=request.user)
    item_counts = seller_items.aggregate(
        all=Count('pk'),
        available=Count('pk', filter=Q(status='available')),
        reserved=Count('pk', filter=Q(status='reserved')),
        sold=Count('pk', filter=Q(status='sold')),
        expired=Count('pk', filter=Q(status='expired')),
    )

    items_query = seller_items.select_related(
        'category', 'location', 'campaign',
    ).prefetch_related('images').order_by('-created_at', '-pk')
    if selected_config['status'] is not None:
        items_query = items_query.filter(status=selected_config['status'])
    page_obj = Paginator(items_query, 12).get_page(request.GET.get('page'))
    items = list(page_obj.object_list)

    now = timezone.now()
    campaigns = CampusCampaign.objects.filter(is_active=True).filter(
        Q(ends_at__isnull=True) | Q(ends_at__gt=now),
    ).order_by('-starts_at', 'title')
    lifecycle = build_seller_lifecycle(request.user)
    diagnoses_by_id = {row.item_id: row for row in lifecycle['diagnoses']}
    # 把诊断结果挂到当前页的商品对象上，模板里可以直接用
    # item.lifecycle_diagnosis，不需要额外注册模板过滤器。
    for item in items:
        item.lifecycle_diagnosis = diagnoses_by_id.get(item.pk)

    return render(request, 'listings/my_items.html', {
        'items': items,
        'item_counts': item_counts,
        'selected_tab': selected_tab,
        'selected_tab_label': selected_config['label'],
        'table_caption': selected_config['caption'],
        'empty_message': selected_config['empty'],
        'page_obj': page_obj,
        'page_numbers': _elided_page_numbers(page_obj),
        'return_url': f"{reverse('my_items')}?tab={selected_tab}&page={page_obj.number}",
        'campaigns': campaigns,
        'lifecycle': lifecycle,
        'has_bulk_listing_image_queue': _load_bulk_listing_image_queue(request) is not None,
        'title': '我的商品',
    })


def _my_items_workspace_redirect(request):
    """Return to the seller workspace page that submitted an action."""
    next_url = request.POST.get('next', '').strip()
    if next_url and url_has_allowed_host_and_scheme(
        next_url,
        allowed_hosts={request.get_host()},
        require_https=request.is_secure(),
    ):
        return redirect(next_url)
    return redirect('my_items')


@login_required
def bulk_assign_campaign(request):
    if request.method != 'POST':
        return _my_items_workspace_redirect(request)

    item_ids = [value for value in request.POST.getlist('item_ids') if value.isdigit()]
    if not item_ids:
        messages.warning(request, '请先选择至少一件商品。')
        return _my_items_workspace_redirect(request)

    campaign_id = request.POST.get('campaign_id', '').strip()
    campaign = None
    if campaign_id and campaign_id != 'none':
        campaign = get_object_or_404(
            CampusCampaign.objects.filter(is_active=True).filter(
                Q(ends_at__isnull=True) | Q(ends_at__gt=timezone.now()),
            ),
            pk=campaign_id,
        )

    with transaction.atomic():
        locked_items = list(
            Item.objects.select_for_update(of=('self',)).filter(
                seller=request.user, id__in=item_ids,
            ).select_related('campaign')
        )
        previous_campaigns = {item.pk: item.campaign_id for item in locked_items}
        updated = Item.objects.filter(
            pk__in=previous_campaigns,
        ).update(campaign=campaign, updated_at=timezone.now())
        if campaign is not None:
            for item in locked_items:
                if previous_campaigns[item.pk] == campaign.pk:
                    continue
                item.campaign = campaign
                queue_campaign_update_matches(item)

    action = f'加入“{campaign.title}”' if campaign else '移出当前专题'
    messages.success(request, f'已将 {updated} 件商品{action}。')
    return _my_items_workspace_redirect(request)


@login_required
def bulk_renew_items(request):
    """Renew a whole seller-selected batch with one explicit expiry.

    This is deliberately not a refresh: it only moves the public expiry (and
    revives rows already marked expired). Refresh quota, event history, and
    match notifications stay untouched.
    """
    if request.method != 'POST':
        return _my_items_workspace_redirect(request)

    raw_item_ids = [value.strip() for value in request.POST.getlist('item_ids')]
    try:
        item_ids = [int(value) for value in raw_item_ids]
    except ValueError:
        item_ids = []
    if not item_ids or len(item_ids) != len(raw_item_ids) or len(set(item_ids)) != len(item_ids):
        messages.warning(request, '请先选择至少一件商品，且不要重复提交同一件商品。')
        return _my_items_workspace_redirect(request)

    form = BulkListingRenewalForm(request.POST)
    if not form.is_valid():
        errors = []
        for field in form:
            errors.extend(error for error in field.errors)
        errors.extend(form.non_field_errors())
        messages.warning(request, f'批量续期未执行：{"；".join(errors)}')
        return _my_items_workspace_redirect(request)

    new_expires_at = form.cleaned_data['expires_at']
    with transaction.atomic():
        locked_items = list(
            Item.objects.select_for_update(of=('self',)).filter(
                seller=request.user, id__in=item_ids,
            ).order_by('id')
        )
        if len(locked_items) != len(item_ids):
            messages.warning(request, '批量续期未执行：请只选择您自己当前仍存在的商品。')
            return _my_items_workspace_redirect(request)

        blocked_items = [
            item for item in locked_items
            if not (
                item.status == 'expired'
                or (item.status == 'available' and item.expires_at is not None)
            )
        ]
        if blocked_items:
            messages.warning(
                request,
                f'批量续期未执行：{len(blocked_items)} 件商品不可续期。请只勾选已过期，或已有截止时间的在售商品。',
            )
            return _my_items_workspace_redirect(request)

        for item in locked_items:
            was_expired = item.status == 'expired'
            item.expires_at = new_expires_at
            update_fields = ['expires_at', 'updated_at']
            if was_expired:
                item.status = 'available'
                update_fields.append('status')
            item.save(update_fields=update_fields)

    messages.success(
        request,
        f'已为 {len(locked_items)} 件商品续期到 {timezone.localtime(new_expires_at):%Y-%m-%d %H:%M}。',
    )
    return _my_items_workspace_redirect(request)



# 运营看板页面与 CSV 共用的面板注册表。这里只登记「两边都导出的共享面板」；
# 页面特有的待办筛选、CSV 特有的序列化都不进这份表，避免把两个不同问题
# 混进同一份耗时里。
OPERATIONS_DASHBOARD_SHARED_BUILDERS = (
    ('demand_match_quality', '求购匹配质量', build_demand_match_quality),
    ('mutual_aid_feedback_notes', '互助反馈补充说明', build_mutual_aid_feedback_notes),
    ('demand_opportunity_followups', '需求跟进任务填写', build_demand_opportunity_followups),
    ('demand_task_aging', '需求任务等待与停留', build_demand_task_aging),
    ('notification_snooze_return', '延后通知回访', build_notification_snooze_return),
    ('notification_kind_coverage', '通知类型覆盖', build_notification_kind_coverage),
    ('notification_preference_usage', '通知偏好使用', build_notification_preference_usage),
    (
        'notification_preference_bypass_diagnosis', '通知偏好绕过诊断',
        build_notification_preference_bypass_diagnosis,
    ),
    ('notification_quiet_hours_shape', '免打扰时段形状', build_notification_quiet_hours_shape),
    ('notification_aggregation_quality', '通知聚合质量', build_notification_aggregation_quality),
    ('report_resolution_quality', '举报处理质量', build_report_resolution_quality),
    ('meeting_incident_quality', '面约异常处理', build_meeting_incident_quality),
    ('moderation_notice_preference', '治理通知偏好', build_moderation_notice_preference),
    ('meeting_attendance', '面约到场', build_meeting_attendance),
    ('meeting_reschedule', '面约改期', build_meeting_reschedule),
    ('meeting_service_window_quality', '交付开放时段风险', build_meeting_service_window_quality),
    (
        'campus_location_service_rule_quality', '校园开放规则配置审计',
        build_campus_location_service_rule_quality,
    ),
    ('refresh_quota_drift', '刷新额度漂移', build_refresh_quota_drift),
    ('item_refresh_event_ledger', '商品刷新事件台账', build_item_refresh_event_ledger_dashboard),
    ('relist_freshness', '重新上架新鲜度', build_relist_freshness),
    ('refresh_response', '商品刷新回响', build_refresh_response),
    ('price_response', '价格调整回响', build_price_response),
    ('category_price_reference', '分类调价参考', build_category_price_reference),
    ('price_change_quality', '出售价格轨迹质量', build_price_change_quality_panel),
    (
        'search_impression_pairing_quality', '搜索曝光与点击配对质量',
        build_search_impression_pairing_quality,
    ),
    ('search_exposure_cold_start', '新商品搜索曝光冷启动', build_search_exposure_cold_start),
    ('handoff_code_usage', '交付码使用', build_handoff_code_usage),
    ('return_handshake', '归还双方握手', build_return_handshake),
    ('delivery_handshake', '当面交付握手', build_delivery_handshake),
    (
        'transaction_safety_acknowledgement_quality', '交易安全卡确认质量',
        build_transaction_safety_acknowledgement_quality,
    ),
    ('first_viewed', '第一次看见之后', build_first_viewed),
    ('saved_search_latency', '关注搜索命中等待', build_saved_search_latency),
    ('saved_search_quiet_window', '关注搜索静默窗口', build_saved_search_quiet_window),
    ('saved_search_limit_shape', '关注搜索上限形状', build_saved_search_limit_shape),
    ('lost_found_matched_outcome', '失物匹配结果', build_lost_found_matched_outcome),
    ('campus_verification_friction', '校园认证等待', build_campus_verification_friction),
    ('gift_application_queue', '赠送申请队列', build_gift_application_queue),
    ('item_image_coverage', '商品图片覆盖', build_item_image_coverage),
    ('moderation_rule_quality', '治理规则命中', build_moderation_rule_quality),
    ('moderation_appeal_quality', '内容申诉处理质量', build_moderation_appeal_quality),
    ('availability_watch_quality', '到货提醒关注', build_availability_watch_quality),
    ('rating_quality', '交易评价质量', build_rating_quality),
    ('dispute_resolution_quality', '申诉处理质量', build_dispute_resolution_quality),
    ('lost_found_lead_quality', '失物线索质量', build_lost_found_lead_quality),
    ('demand_response_conversion', '求购响应转化', build_demand_response_conversion),
    ('order_deadline_reminders', '订单截止提醒', build_order_deadline_reminders),
    ('opportunity_dismissal_quality', '机会流忽略质量', build_opportunity_dismissal_quality),
    ('favorite_collection_quality', '心愿单分组形状', build_favorite_collection_quality),
    (
        'academic_phase_supply_preparation', '学期阶段供给预热',
        build_academic_phase_supply_preparation,
    ),
    (
        'academic_term_snapshot_quality', '学期阶段快照质量',
        build_academic_term_snapshot_quality,
    ),
    ('contribution_tier_shape', '积分等级与徽章形状', build_contribution_tier_shape),
    ('contribution_milestone_coverage', '贡献里程碑覆盖审计', build_contribution_milestone_coverage),
    (
        'contribution_milestone_phase_rhythm', '贡献里程碑阶段节奏',
        build_contribution_milestone_phase_rhythm,
    ),
    ('campaign_phase_review', '校园专题分阶段复盘', build_campaign_phase_review),
    ('borrow_days_shape', '默认借用天数形状', build_borrow_days_shape),
    ('dispute_evidence_shape', '申诉证据形状', build_dispute_evidence_shape),
    ('recommendation_quality', '推荐质量', build_recommendation_quality),
    ('recommendation_impression_quality', '推荐曝光日志质量', build_recommendation_impression_quality),
    ('opportunity_impression_quality', '机会流曝光日志质量', build_opportunity_impression_quality),
    ('location_registry_quality', '校园地点档案质量', build_location_registry_quality),
    ('location_maintenance_queue', '校园地点维护队列', build_location_maintenance_queue),
    (
        'location_maintenance_decision_review', '校园地点维护决策回看',
        build_location_maintenance_decision_review,
    ),
    (
        'task_run_ledger_integrity', '定时任务台账完整性审计',
        build_task_run_ledger_integrity,
    ),
    (
        'lifecycle_reminder_term_trend', '生命周期提醒的跨学期终态转化',
        build_lifecycle_reminder_term_trend_dashboard,
    ),
)


def _build_shared_operations_dashboard(
    *, days, base_builder=build_operations_dashboard,
    panel_builders=None, anchors=None,
):
    """构建页面与 CSV 共用的运营看板，并附上本次请求的耗时报告。

    测试可以注入很小的 ``base_builder`` 与 ``panel_builders``，不必为了验证
    注册表和计时契约而把几十个真实面板各跑一遍。

    ``anchors`` 是懒加载的口子：给了分区列表，就只构建这些分区渲染所需的部分，
    其余面板一个都不跑。``None``（默认）表示整座看板——CSV 导出和不带 cookie 的
    页面走这条，行为和加这个参数之前逐字节一致。分区与上下文的对应关系在
    ``operations_fragments.py``，那份表由 ``test_operations_fragments`` 反查盯
    着，不会悄悄漏键。

    耗时报告只记「本次请求真的跑了什么」：懒加载那次请求里没跑的面板，报告里
    也没有它的行，不会拿一个假的零耗时冒充「很快」。
    """
    runs = []
    dashboard = {}
    if anchors is None or section_needs_core(anchors):
        started = perf_counter()
        dashboard = base_builder(days=days)
        runs.append({
            'key': 'operations_core',
            'label': '运营看板核心统计',
            'elapsed_ms': (perf_counter() - started) * 1000,
        })

    builders = panel_builders
    if builders is None:
        builders = OPERATIONS_DASHBOARD_SHARED_BUILDERS
    if anchors is not None:
        wanted = section_panel_keys(anchors)
        # None 表示某个分区点名要整座看板（看板构建耗时分区就是），此时不筛。
        if wanted is not None:
            builders = [row for row in builders if row[0] in wanted]
    for key, label, builder in builders:
        started = perf_counter()
        dashboard[key] = builder(days=days)
        runs.append({
            'key': key,
            'label': label,
            'elapsed_ms': (perf_counter() - started) * 1000,
        })

    dashboard['dashboard_build_performance'] = build_dashboard_build_performance(runs)
    return dashboard


def _operations_period_days(request):
    try:
        return int(request.GET.get('days', 30))
    except (TypeError, ValueError):
        return 30


def _demand_outcome_status_filter(request):
    """Validate the outcome task-status filter coming from the dashboard URL."""
    status_filter = request.GET.get('task_status', 'all')
    allowed = {'all', 'todo', 'in_progress', 'completed', 'ignored'}
    return None if status_filter == 'all' or status_filter not in allowed else status_filter


# 懒加载开关。cookie 由 <head> 里一行内联脚本种下，见 base.html。
OPERATIONS_LAZY_COOKIE = 'ops_lazy'


def _wants_lazy_operations(request):
    """这次请求要不要只渲染首屏。

    只看 cookie，不猜能力：关掉脚本的浏览器永远种不下这个 cookie，于是永远走
    全量渲染——无脚本用户和 Django 测试客户端看到的一直是完整看板。反过来，
    带着 cookie 来的请求一定跑过那段内联脚本，取回分区才有人执行。
    """
    return request.COOKIES.get(OPERATIONS_LAZY_COOKIE) == '1'


def _operations_dashboard_extras(request, days, dashboard):
    """需求雷达那一段：待办归属、结果统计和筛选状态。

    页面和分区取回共用。取回单个分区也要跑这里，是因为 ``_demand.html`` 要用
    ``demand_radar_outcomes`` 和 ``demand_outcome_status_filter``——漏了它们，
    那一块会静默变成空态，而页面其它部分看起来完全正常。
    """
    active_tasks = DemandOpportunityTask.objects.filter(
        status__in=['todo', 'in_progress'],
    ).select_related('assigned_to')
    tasks_by_key = {task.radar_key: task for task in active_tasks}
    for row in dashboard.get('demand_radar', {}).get('rows', ()):
        row['active_task'] = tasks_by_key.get(row['radar_key'])
    dashboard['demand_radar_tasks'] = active_tasks[:20]
    status_filter = _demand_outcome_status_filter(request)
    dashboard['demand_radar_outcomes'] = build_demand_radar_outcomes(
        days=days, statuses=status_filter,
    )
    dashboard['demand_outcome_status_filter'] = status_filter
    return dashboard


@login_required
def operations_dashboard(request):
    if not request.user.is_staff:
        raise PermissionDenied
    days = _operations_period_days(request)
    lazy = _wants_lazy_operations(request)
    # 懒加载时只构建首屏四个分区用得到的部分；其余分区等前端来取。
    dashboard = _build_shared_operations_dashboard(
        days=days,
        anchors=OPERATIONS_DASHBOARD_FIRST_SCREEN if lazy else None,
    )
    dashboard = _operations_dashboard_extras(request, days, dashboard)
    dashboard['pending_dispute_count'] = OrderDispute.objects.filter(
        status__in={'open', 'reviewing'},
    ).count()
    dashboard['pending_moderation_count'] = ModerationEvent.objects.filter(status='pending').count()
    dashboard['pending_high_moderation_count'] = ModerationEvent.objects.filter(
        status='pending', risk_level='high',
    ).count()
    # 分区跳转条的分组数据；模板据此渲染导航，锚点在模板里与各分区的
    # <section id> 一一对应，由 test_operations_nav 囊底。
    dashboard['operations_sections'] = OPERATIONS_DASHBOARD_SECTIONS
    dashboard['ops_lazy'] = lazy
    return render(request, 'listings/operations_dashboard.html', dashboard)


@login_required
def operations_dashboard_fragment(request, anchor):
    """按需取回一个运营看板分区，返回该分区的 HTML 片段。

    只渲染 ``anchor`` 那一个分区，上下文按 ``operations_fragments`` 的表裁剪，
    所以取回「失物匹配结果」不会顺带把 64 个共享面板全跑一遍。

    权限和整页一致：登录 + staff。匿名访问 302 到登录页，非 staff 403——这里
    不做「悄悄返回空片段」，那种失败在浏览器里只是一个空分区，没人会深究。
    """
    if not request.user.is_staff:
        raise PermissionDenied
    partial = section_partial(anchor)
    if partial is None:
        raise Http404('运营看板没有这个分区：%s' % anchor)
    days = _operations_period_days(request)
    dashboard = _build_shared_operations_dashboard(days=days, anchors=(anchor,))
    dashboard = _operations_dashboard_extras(request, days, dashboard)
    return render(request, 'listings/operations/' + partial, dashboard)


# 跟进备注的长度上限。表单上是同一个数字，两处各写一份就会各自漂移，
# 所以这里集中一个常量，视图截断时用它。
MAX_DEMAND_TASK_NOTE_LENGTH = 500


def _parse_due_at(raw):
    """解析跟进截止时间，解析不了就返回 None 并给出原因。

    ``datetime-local`` 输入框交上来的是 ``2026-10-20T18:00`` 这种本地时间字符串，
    没有时区信息，直接交给 Django 会踩 ``NaiveDateTimeWarning`` 并用项目的默认时区
    解释它。这里按东八区所在的本地时区显式转成 aware 时间，解析失败就明说，而不是
    静默丢弃——一个被悄悄丢掉的期限比一个报错更糟：运营以为设了，看板以为没有。
    """
    raw = (raw or '').strip()
    if not raw:
        return None, None
    text = raw.replace('T', ' ')
    for fmt in ('%Y-%m-%d %H:%M:%S', '%Y-%m-%d %H:%M', '%Y-%m-%d %H', '%Y-%m-%d'):
        try:
            naive = datetime.strptime(text, fmt)
        except ValueError:
            continue
        return timezone.make_aware(naive, timezone.get_current_timezone()), None
    return None, '截止时间的格式看不懂，请用日期选择器重新选择。'


@login_required
def create_demand_opportunity_task(request):
    if not request.user.is_staff:
        raise PermissionDenied
    if request.method != 'POST':
        return redirect('operations_dashboard')
    try:
        days = int(request.POST.get('days', 30))
    except (TypeError, ValueError):
        days = 30
    radar_key = request.POST.get('radar_key', '').strip()
    radar = build_demand_radar(days=days)
    row = next((item for item in radar['rows'] if item['radar_key'] == radar_key), None)
    if not row:
        messages.error(request, '这个需求机会已经变化或不在当前统计周期内，请刷新运营看板后重试。')
        return redirect(f'{reverse("operations_dashboard")}?days={days}')
    task, created = DemandOpportunityTask.objects.get_or_create(
        radar_key=row['radar_key'],
        status__in=['todo', 'in_progress'],
        defaults={
            'title': row['label'],
            'category_id': row.get('category_id'),
            'location_id': row.get('location_id'),
            'level': row['level'],
            'opportunity_score': row['opportunity_score'],
            'search_count': row['search_count'],
            'demand_count': row['demand_count'],
            'available_supply': row['available_supply'],
            'evidence': row['evidence'],
            'recommendations': row['recommendations'],
            'created_by': request.user,
            # 建立任务时可以顺手把期限填上。不填也能建：一个没有期限的任务
            # 至少还被统计着，而要求必填会让人干脆不建任务。
            'due_at': _parse_due_at(request.POST.get('due_at', ''))[0],
            'note': request.POST.get('note', '').strip()[:MAX_DEMAND_TASK_NOTE_LENGTH],
        },
    )
    if created:
        messages.success(request, f'已建立“{task.title}”的需求跟进任务。')
    else:
        messages.info(request, f'“{task.title}”已经有进行中的跟进任务。')
    return redirect(f'{reverse("operations_dashboard")}?days={days}')


@login_required
def update_demand_opportunity_task(request, task_id):
    if not request.user.is_staff:
        raise PermissionDenied
    task = get_object_or_404(DemandOpportunityTask, pk=task_id)
    if request.method == 'POST':
        status = request.POST.get('status', '')
        due_at, due_error = _parse_due_at(request.POST.get('due_at', ''))
        note = request.POST.get('note', '').strip()
        # 备注长度沿用表单一侧的上限。截断而不是报错：运营正在赶时间，
        # 一段超长的备注该被收下，而不是让人删字重打。
        note = note[:MAX_DEMAND_TASK_NOTE_LENGTH]
        update_fields = []
        if status in dict(DemandOpportunityTask.STATUS_CHOICES):
            task.status = status
            if not task.assigned_to_id and status == 'in_progress':
                task.assigned_to = request.user
            update_fields.extend(['status', 'assigned_to'])
        if due_error:
            messages.error(request, due_error)
        elif due_at != task.due_at:
            task.due_at = due_at
            update_fields.append('due_at')
        if note != (task.note or '').strip():
            task.note = note
            update_fields.append('note')
        if update_fields:
            task.save(update_fields=update_fields + ['updated_at'])
            messages.success(
                request,
                f'任务“{task.title}”已更新为{task.get_status_display()}'
                + (f'，截止 {timezone.localtime(due_at):%Y-%m-%d %H:%M}。' if due_at else '。'),
            )
    try:
        days = int(request.POST.get('days', 30))
    except (TypeError, ValueError):
        days = 30
    return redirect(f'{reverse("operations_dashboard")}?days={days}')


@login_required
def operations_dashboard_export(request):
    if not request.user.is_staff:
        raise PermissionDenied

    days = _operations_period_days(request)
    dashboard = _build_shared_operations_dashboard(days=days)
    metrics = dashboard['metrics']
    response = HttpResponse(content_type='text/csv; charset=utf-8')
    response['Content-Disposition'] = (
        f"attachment; filename=campus-glimmer-operations-{dashboard['period_days']}d.csv"
    )
    response.write('\ufeff')
    writer = csv.writer(response)
    writer.writerow(['拾光校园运营数据导出'])
    writer.writerow(['统计周期', f"最近 {dashboard['period_days']} 天"])
    writer.writerow([
        '数据范围',
        f"{dashboard['period_start']:%Y-%m-%d} 至 {dashboard['period_end']:%Y-%m-%d}",
    ])
    writer.writerow([])
    writer.writerow(['核心指标', '数值'])
    metric_labels = (
        ('active_items', '当前在售商品'),
        ('new_items', '新增商品'),
        ('detail_views', '详情浏览'),
        ('favorites', '加入心愿单'),
        ('orders', '交易预约'),
        ('completed_orders', '完成交易'),
        ('completion_rate', '交易完成率（%）'),
        ('average_order_price', '平均预约金额'),
        ('searches', '搜索次数'),
        ('zero_result_searches', '无结果搜索'),
        ('zero_result_rate', '无结果占比（%）'),
        ('search_quality_score', '搜索质量评分'),
        ('search_click_through_rate', '搜索结果点击率（%）'),
        ('new_users', '新增用户'),
        ('active_users', '周期活跃用户'),
        ('retention_rate', '上一周期新用户回访率（%）'),
        ('new_reports', '新增举报'),
        ('pending_reports', '待处理举报'),
        ('notification_events', '通知触达事件数'),
        ('notification_rows', '通知记录数'),
        ('unread_notifications', '周期结束未读通知'),
        ('notification_compression_rate', '通知聚合压缩率（%）'),
        ('active_demands', '当前有效求购'),
        ('demand_matches', '求购匹配通知'),
        ('demand_match_read_rate', '求购匹配阅读率（%）'),
        ('demand_match_demands', '被匹配求购数'),
        ('demand_responses', '卖家响应数'),
        ('demand_responded_demands', '收到响应的求购数'),
        ('demand_accepted_responses', '确认匹配响应数'),
        ('demand_rejected_responses', '未采纳响应数'),
        ('demand_response_acceptance_rate', '响应确认率（%）'),
        ('demand_match_response_rate', '匹配到响应转化率（%）'),
        ('demand_response_acceptance_demand_rate', '响应到确认求购转化率（%）'),
        ('contribution_points', '周期贡献积分'),
        ('contribution_events', '周期贡献事件'),
        ('contribution_users', '周期贡献用户'),
        ('mutual_aid_accepted_interactions', '已确认互助'),
        ('mutual_aid_feedbacks', '互助结果反馈'),
        ('mutual_aid_completed', '确认互助完成'),
        ('mutual_aid_completion_rate', '互助完成率（%）'),
    )
    for key, label in metric_labels:
        value = metrics[key]
        writer.writerow([label, '' if value is None else value])

    performance = dashboard['dashboard_build_performance']
    writer.writerow([])
    writer.writerow(['看板自身构建耗时'])
    writer.writerow(['指标', '数值'])
    writer.writerow([
        '共享构建器数量', performance['metrics']['builder_total'],
        '运营看板核心统计 + 两端共用的共享面板',
    ])
    writer.writerow([
        '计入耗时（ms）', performance['metrics']['total_elapsed_ms'],
        '只含共享构建函数，不含渲染、序列化、请求筛选和下载',
    ])
    writer.writerow([
        'P50（ms）', '' if performance['metrics']['p50_ms'] is None else performance['metrics']['p50_ms'],
        '最近秩，不插值',
    ])
    writer.writerow([
        'P90（ms）', '' if performance['metrics']['p90_ms'] is None else performance['metrics']['p90_ms'],
        '最近秩，不插值',
    ])
    writer.writerow([
        '重复 key 数量', performance['metrics']['duplicate_key_total'],
        '页面与 CSV 共用注册表的自检数字',
    ])
    writer.writerow([])
    writer.writerow(['共享构建器', '耗时（ms）', '占比（%）', '累计占比（%）', '读数'])
    for row in performance['rows']:
        reading = []
        if row['is_slowest']:
            reading.append('最慢')
        if row['is_duplicate']:
            reading.append('重复 key')
        writer.writerow([
            f"构建：{row['label']}", row['elapsed_ms'],
            '' if row['share_percent'] is None else row['share_percent'],
            '' if row['cumulative_percent'] is None else row['cumulative_percent'],
            ' · '.join(reading) if reading else '—',
        ])
    writer.writerow(['口径', performance['note']])

    service_window_quality = dashboard['meeting_service_window_quality']
    service_metrics = service_window_quality['metrics']
    writer.writerow([])
    writer.writerow(['交付开放时段风险', '数值', '说明'])
    writer.writerow([
        '口径', service_window_quality['summary'],
        service_window_quality['period_note'],
    ])
    writer.writerow([
        '未来安排总数', service_metrics['total_count'],
        '只看构建后仍待确认或已确认、且尚未开始的安排',
    ])
    writer.writerow([
        '可评估安排', service_metrics['measurable_count'],
        '已填写交付地点的安排；未登记时段也算可评估，单独分类',
    ])
    writer.writerow([
        '开放时段内', service_metrics['open_count'],
        '预约区间完整落在每周规则或单日替换时段并集内',
    ])
    writer.writerow([
        '登记规则外', service_metrics['registered_outside_count'],
        '每周时段外、临时关闭与替换时段外三者互斥相加',
    ])
    writer.writerow([
        '每周时段外', service_metrics['weekly_outside_count'],
        '当天没有启用例外时，不在每周登记时段内',
    ])
    writer.writerow([
        '命中临时关闭', service_metrics['closed_exception_count'],
        '安排日期命中启用关闭例外',
    ])
    writer.writerow([
        '替换时段外', service_metrics['replacement_outside_count'],
        '安排日期命中替换例外但预约区间不在替换时段内',
    ])
    writer.writerow([
        '地点未登记每周时段', service_metrics['unregistered_count'],
        '单独统计，不视为错误，也不并入登记规则外',
    ])
    writer.writerow([
        '无交付地点', service_metrics['no_location_count'],
        '不借用订单历史地点伪装可评估',
    ])
    writer.writerow([
        '未来启用例外', service_metrics['future_count'],
        '生效日期不早于构建当天；同日没有未来安排的例外也保留在这里',
    ])
    writer.writerow([
        '例外无同日安排', service_metrics['unused_count'],
        f"覆盖 {service_metrics['unused_location_count']} 个地点；提前维护和自然无人使用都算",
    ])

    writer.writerow([])
    writer.writerow([
        '交付开放时段风险 · 地点聚合', '未来安排', '登记规则外', '例外相关',
        '未登记时段', '需人工核对',
    ])
    for row in service_window_quality['location_rows']:
        writer.writerow([
            row['location_name'], row['total_count'], row['registered_outside_count'],
            row['exception_count'], row['unregistered_count'], row['attention_count'],
        ])

    writer.writerow([])
    writer.writerow([
        '交付开放时段风险 · 完整清单', '开始时间', '结束时间', '状态', '分类', '原因', '登记规则',
    ])
    for row in service_window_quality['risk_rows']:
        writer.writerow([
            f"订单 {row['order_id']}",
            timezone.localtime(row['start_at']).strftime('%Y-%m-%d %H:%M'),
            timezone.localtime(row['end_at']).strftime('%Y-%m-%d %H:%M'),
            row['status_label'], row['kind_label'], row['reason'], row['rule_summary'],
        ])
    writer.writerow(['边界', service_window_quality['boundary_note']])

    rule_quality = dashboard['campus_location_service_rule_quality']
    rule_metrics = rule_quality['metrics']
    writer.writerow([])
    writer.writerow(['校园开放规则配置审计', '数值', '说明'])
    writer.writerow([
        '口径', rule_quality['summary'], rule_quality['period_note'],
    ])
    writer.writerow([
        '启用地点', rule_metrics['active_location_count'],
        f"停用地点 {rule_metrics['inactive_location_count']} 个；两者都只描述当前档案开关",
    ])
    writer.writerow([
        '已登记每周时段的启用地点', rule_metrics['registered_location_count'],
        '至少有一条启用每周时段；未登记地点单独列出，不当成错误',
    ])
    writer.writerow([
        '启用地点未登记每周时段', rule_metrics['unregistered_location_count'],
        '可以表示全天协商开放，也可以表示档案待补，本面板不替维护者选择',
    ])
    writer.writerow([
        '启用每周时段', rule_metrics['active_window_count'],
        f"停用时段 {rule_metrics['inactive_window_count']} 条；跨午夜 {rule_metrics['cross_midnight_window_count']} 条",
    ])
    writer.writerow([
        '每周时段重叠或相邻组合', rule_metrics['weekly_overlap_or_adjacent_group_count'],
        '统一评估器会合并这些区间；这是维护复杂度信号，不是功能故障',
    ])
    writer.writerow([
        '启用单日例外', rule_metrics['active_exception_count'],
        f"过去 {rule_metrics['past_exception_count']} · 今天 {rule_metrics['today_exception_count']} · 未来 {rule_metrics['future_exception_count']}；停用 {rule_metrics['inactive_exception_count']}",
    ])
    writer.writerow([
        '例外类型', rule_metrics['closed_exception_count'],
        f"临时关闭；替换时段 {rule_metrics['replacement_exception_count']} 条",
    ])
    writer.writerow([
        '配置信号总数', rule_metrics['signal_total'],
        '例外挂在停用地点、替换无时段、替换重叠、跨午夜截断与未登记地点可重叠计数',
    ])

    writer.writerow([])
    writer.writerow(['按地点聚合'])
    writer.writerow(['地点', '状态', '启用每周时段', '停用每周时段', '启用例外', '配置信号'])
    for row in rule_quality['location_rows']:
        writer.writerow([
            row['location_name'], row['location_active_label'],
            row['active_window_count'], row['inactive_window_count'],
            row['active_exception_count'], row['signal_count'],
        ])

    writer.writerow([])
    writer.writerow(['配置核对清单'])
    writer.writerow(['地点', '生效日期', '状态', '信号', '配置', '解释'])
    for row in rule_quality['rule_rows']:
        writer.writerow([
            row['effective_date'].strftime('%Y-%m-%d') if row['effective_date'] else '—',
            row['location_name'], row['location_active_label'],
            row['signal_label'], row['config'], row['reason'],
        ])
    writer.writerow(['边界', rule_quality['boundary_note']])

    writer.writerow([])
    writer.writerow(['求购匹配转化', '数量', '相对上一步转化率（%）', '口径说明'])
    for stage in dashboard['demand_match_insights']['funnel']:
        writer.writerow([stage['label'], stage['count'], stage['rate'], stage['note']])

    writer.writerow([])
    writer.writerow(['分类表现', '响应数', '求购数', '确认数', '响应确认率（%）'])
    for row in dashboard['demand_match_insights']['category_rows']:
        writer.writerow([row['name'], row['response_count'], row['demand_count'], row['accepted_count'], row['acceptance_rate']])

    writer.writerow([])
    writer.writerow(['地点表现', '响应数', '求购数', '确认数', '响应确认率（%）'])
    for row in dashboard['demand_match_insights']['location_rows']:
        writer.writerow([row['name'], row['response_count'], row['demand_count'], row['accepted_count'], row['acceptance_rate']])

    match_quality = dashboard['demand_match_quality']
    writer.writerow([])
    writer.writerow(['求购匹配质量', '数值', '说明'])
    writer.writerow([
        '口径与门槛', match_quality['summary'],
        f"已决断 {match_quality['metrics']['decided_responses']} 条 · "
        f"整体采纳率 {match_quality['metrics']['acceptance_rate']}% · "
        f"平均匹配分 {match_quality['metrics']['average_score'] if match_quality['metrics']['average_score'] is not None else '—'}",
    ])
    writer.writerow([
        '达到样本门槛的分段', match_quality['metrics']['measured_bands'],
        f"每段至少 {match_quality['metrics']['min_band_responses']} 条已决断响应才给结论",
    ])
    writer.writerow([
        '打分与采纳的关系', match_quality['monotonicity']['kind_label'],
        match_quality['monotonicity']['summary'],
    ])

    writer.writerow([])
    writer.writerow([
        '按匹配分的采纳率', '响应数', '采纳数', '未采纳数',
        '采纳率（%）', '平均分', '读数',
    ])
    for band in match_quality['score_bands']:
        writer.writerow([
            band['label'], band['response_count'], band['accepted_count'],
            band['rejected_count'], band['acceptance_rate'],
            band['average_score'] if band['average_score'] is not None else '',
            f"样本不足 {match_quality['metrics']['min_band_responses']} 条，只报数字"
            if band['is_small_sample'] else '可读数',
        ])

    writer.writerow([])
    writer.writerow([
        '打分里的每个维度，各自拉开了采纳率吗', '匹配依据', '带此依据响应数',
        '带此依据采纳率（%）', '不带此依据响应数', '不带此依据采纳率（%）',
        '差值（百分点）',
    ])
    for row in match_quality['dimension_effect']:
        writer.writerow([
            row['label'], row['with_count'], row['with_acceptance_rate'],
            row['without_count'], row['without_acceptance_rate'],
            row['gap_points'] if row['gap_points'] is not None else '',
        ])

    writer.writerow([])
    writer.writerow(['高分被拒', '响应数', '被拒数', '拒绝率（%）', '说明'])
    over = match_quality['over_promised']
    if over.get('has_sample'):
        writer.writerow([
            '高分被拒', over['high_count'], over['rejected_count'],
            over['rejection_rate'], over['summary'],
        ])
    else:
        writer.writerow(['高分被拒', '', '', '', over['summary']])

    writer.writerow([])
    writer.writerow(['可以做什么', '建议'])
    for index, note in enumerate(match_quality['recommendations'], start=1):
        writer.writerow([f'建议 {index}', note])

    writer.writerow([])
    writer.writerow([
        '说明', '此处只报告，不改匹配权重、不改任何门槛、'
        '也不把统计写回响应记录。',
    ])

    writer.writerow([])
    writer.writerow(['通知触达与聚合', '触达事件', '通知记录', '未读记录', '压缩率（%）'])
    for row in dashboard['notification_insights']['kind_rows']:
        writer.writerow([row['label'], row['event_count'], row['row_count'], row['unread_count'], row['compression_rate']])

    contribution_insights = dashboard['contribution_insights']
    writer.writerow([])
    writer.writerow(['校园互助影响', '数值'])
    writer.writerow(['周期贡献积分', contribution_insights['period_points']])
    writer.writerow(['周期贡献事件', contribution_insights['period_events']])
    writer.writerow(['周期参与用户', contribution_insights['period_users']])
    writer.writerow(['周期人均积分', contribution_insights['average_points_per_user']])
    writer.writerow(['累计贡献积分', contribution_insights['lifetime_points']])

    writer.writerow([])
    writer.writerow(['贡献类型', '贡献积分', '贡献事件', '参与用户', '积分占比（%）'])
    for row in contribution_insights['kind_rows']:
        writer.writerow([row['label'], row['points'], row['event_count'], row['user_count'], row['point_share']])

    writer.writerow([])
    writer.writerow(['周期贡献排行', '用户名', '贡献事件', '贡献积分'])
    for rank, row in enumerate(contribution_insights['top_users'], start=1):
        writer.writerow([rank, row['username'], row['event_count'], row['points']])

    writer.writerow([])
    writer.writerow(['每日贡献趋势', '贡献积分', '贡献事件', '参与用户'])
    for point in contribution_insights['trend']:
        if point['points'] or point['event_count']:
            writer.writerow([point['date'], point['points'], point['event_count'], point['user_count']])

    feedback_insights = dashboard['mutual_aid_feedback_insights']
    writer.writerow([])
    writer.writerow(['互助反馈闭环', '数值'])
    writer.writerow(['已确认互助', feedback_insights['accepted_interaction_count']])
    writer.writerow(['结果反馈', feedback_insights['feedback_count']])
    writer.writerow(['确认完成', feedback_insights['completed_count']])
    writer.writerow(['暂未解决', feedback_insights['unresolved_count']])
    writer.writerow(['互助完成率（%）', feedback_insights['completion_rate']])

    writer.writerow([])
    writer.writerow(['反馈来源', '反馈数', '完成数', '暂未解决数', '完成率（%）'])
    for row in feedback_insights['source_rows']:
        writer.writerow([row['label'], row['feedback_count'], row['completed_count'], row['unresolved_count'], row['completion_rate']])

    writer.writerow([])
    writer.writerow(['反馈标签', '次数', '占反馈（%）'])
    for row in feedback_insights['tag_rows']:
        writer.writerow([row['label'], row['count'], row['share']])

    writer.writerow([])
    writer.writerow(['每日反馈趋势', '反馈数', '完成数', '暂未解决数'])
    for point in feedback_insights['trend']:
        if point['feedback_count']:
            writer.writerow([point['date'], point['feedback_count'], point['completed_count'], point['unresolved_count']])

    writer.writerow([])
    writer.writerow(['互助闭环漏斗', '数量', '相对上一步转化率（%）', '口径说明'])
    for stage in feedback_insights['funnel']:
        writer.writerow([stage['label'], stage['count'], stage['rate'], stage['note']])

    notes_insights = dashboard['mutual_aid_feedback_notes']
    writer.writerow([])
    writer.writerow(['互助反馈的补充说明', '数值', '说明'])
    writer.writerow(['口径与门槛', notes_insights['summary'], f"只读 note 字段 · 短于 {notes_insights['min_note_length']} 字不计词频"])
    writer.writerow(['写过补充说明的反馈', notes_insights['fill_stats']['filled_count'], f"共 {notes_insights['fill_stats']['total']} 条反馈"])
    writer.writerow(['填写率（%）', notes_insights['fill_stats']['fill_rate'], '先报填写率再报词频：没人写的字段该被撤掉，不该被分析'])
    writer.writerow(['平均长度（字）', notes_insights['fill_stats']['average_length'], '只统计写了补充说明的那些反馈'])
    writer.writerow(['最长一条（字）', notes_insights['fill_stats']['max_length'], '上限是表单上的五百字'])

    writer.writerow([])
    writer.writerow(['长度分布', '条数'])
    for row in notes_insights['fill_stats']['length_rows']:
        writer.writerow([row['label'], row['count']])

    writer.writerow([])
    writer.writerow(['写与不写，是按结果分的吗', '结果', '反馈数', '写过说明', '填写率（%）', '平均长度（字）'])
    for row in notes_insights['outcome_contrast']['rows']:
        writer.writerow([
            row['label'], row['feedback_count'], row['filled_count'],
            row['fill_rate'], row['average_length'],
        ])
    gap = notes_insights['outcome_contrast']['gap_points']
    writer.writerow([
        '未完成减已完成（百分点）', gap,
        '相差不满 8 个百分点视为噪声，样本薄的时候和抛硬币差不多',
    ])

    writer.writerow([])
    writer.writerow(['出现得最多的词', '被多少条反馈提到', '占写了说明的反馈（%）'])
    for row in notes_insights['term_rows']:
        writer.writerow([row['term'], row['count'], row['share']])
    if not notes_insights['term_rows']:
        writer.writerow(['—', 0, 0])

    writer.writerow([])
    writer.writerow([
        '说明',
        f"高频词仅供参考，不做语义判断。一个词要出现在至少 {notes_insights['min_term_count']} 条不同的补充说明里才进榜单，"
        f"最多列 {notes_insights['top_terms']} 个。这里只报告，不公开反馈原文、不新增标签、不把任何词回写成结构化字段。",
    ])

    writer.writerow([])
    writer.writerow([
        '待处理交易争议',
        OrderDispute.objects.filter(status__in={'open', 'reviewing'}).count(),
    ])

    writer.writerow([])
    writer.writerow(['校园需求机会'])
    writer.writerow(['需求主题', '优先级', '机会分', '无结果搜索', '有效求购', '当前供给', '证据信号', '建议动作'])
    for row in dashboard['demand_radar']['rows']:
        writer.writerow([
            row['label'], row['level_label'], row['opportunity_score'],
            row['search_count'], row['demand_count'], row['available_supply'],
            '；'.join(row['evidence']), '；'.join(row['recommendations']),
        ])

    writer.writerow([])
    writer.writerow(['需求跟进闭环效果', '数值'])
    outcome_summary = dashboard['demand_radar_outcomes']['summary']
    writer.writerow(['纳入统计任务数', outcome_summary['task_count']])
    writer.writerow(['缺口收敛任务数', outcome_summary['converged_count']])
    writer.writerow(['缺口扩大任务数', outcome_summary['diverged_count']])
    writer.writerow(['基本持平任务数', outcome_summary['flat_count']])
    writer.writerow(['样本不足任务数', outcome_summary['insufficient_count']])
    writer.writerow(['闭环命中率（%）', '' if outcome_summary['hit_rate'] is None else outcome_summary['hit_rate']])
    writer.writerow(['无结果率变化中位数（百分点）', '' if outcome_summary['median_delta_points'] is None else outcome_summary['median_delta_points']])
    writer.writerow(['净增可用供给', outcome_summary['supply_added_total']])

    writer.writerow([])
    writer.writerow(['跟进主题', '任务状态', '基线无结果率（%）', '观察期无结果率（%）', '变化（百分点）', '可用供给', '建任务时供给', '判定', '说明'])
    for row in dashboard['demand_radar_outcomes']['rows']:
        writer.writerow([
            row['title'], row['status_label'],
            '' if row['baseline_zero_rate'] is None else row['baseline_zero_rate'],
            '' if row['observation_zero_rate'] is None else row['observation_zero_rate'],
            '' if row['delta_points'] is None else row['delta_points'],
            row['supply_now'], row['supply_at_creation'],
            row['outcome_label'], row['verdict'],
        ])

    followups = dashboard['demand_opportunity_followups']
    writer.writerow([])
    writer.writerow(['跟进任务的截止时间与备注', '数值', '说明'])
    writer.writerow(['口径与门槛', followups['summary'], '先报填写率再报逾期：没人填的字段该被撤掉，不该被分析'])
    writer.writerow(['周期内跟进任务', followups['fill_stats']['total'], '按任务创建时间归期'])
    writer.writerow(['设了截止时间', followups['fill_stats']['with_due_count'], f"填写率 {followups['fill_stats']['due_fill_rate']}%"])
    writer.writerow(['没有截止时间', followups['fill_stats']['without_due_count'], '任务建了但没人给期限，比逾期更能说明跟进流程有没有在跑'])
    writer.writerow(['写过备注', followups['fill_stats']['with_note_count'], f"填写率 {followups['fill_stats']['note_fill_rate']}%"])
    writer.writerow(['平均备注长度（字）', followups['fill_stats']['average_note_length'], f"最长 {followups['fill_stats']['max_note_length']} 字，上限 {MAX_DEMAND_TASK_NOTE_LENGTH} 字"])
    writer.writerow(['期限距建任务超过上限', followups['fill_stats']['distant_due_count'], f"超过 {followups['fill_stats']['max_reasonable_due_days']} 天的期限读起来像「填个日子好让字段不为空」"])

    writer.writerow([])
    writer.writerow(['有期限与无期限的完成率对比', '任务数', '已闭环', '已完成', '完成率（%）', '样本'])
    for row in followups['due_contrast']['rows']:
        writer.writerow([
            row['label'], row['task_count'], row['closed_count'],
            row['completed_count'], row['completion_rate'],
            f"样本不足 {followups['due_contrast']['min_comparison_tasks']} 条，只报数字"
            if row['is_small_sample'] else '可读数',
        ])
    gap = followups['due_contrast']['gap_points']
    writer.writerow([
        '两组完成率之差（百分点）', '' if gap is None else gap,
        f"相差不满 {followups['due_contrast']['noise_points']} 个百分点视为噪声，样本薄的时候和抛硬币差不多",
    ])

    writer.writerow([])
    writer.writerow(['逾期分档', '任务数'])
    for row in followups['overdue_stats']['bucket_rows']:
        writer.writerow([row['label'], row['count']])

    writer.writerow([])
    writer.writerow(['待处理的逾期任务', '主题', '状态', '负责人', '截止时间', '已超期（天）', '备注长度（字）', '建立时间'])
    overdue_stats = followups['overdue_stats']
    if overdue_stats['overdue_rows']:
        for row in overdue_stats['overdue_rows'][:overdue_stats['open_task_limit']]:
            writer.writerow([
                row['title'], row['status_label'],
                row['assigned_to'] or '未指派',
                timezone.localtime(row['due_at']).strftime('%Y-%m-%d %H:%M'),
                row['overdue_days'], row['note_length'],
                timezone.localtime(row['created_at']).strftime('%Y-%m-%d %H:%M'),
            ])
    else:
        writer.writerow(['—', '当前没有已过期限且未闭环的任务', '', '', '', '', ''])

    writer.writerow([])
    writer.writerow(['临期任务', '主题', '状态', '负责人', '截止时间', '还剩（天）', '备注长度（字）'])
    if overdue_stats['approaching_rows']:
        for row in overdue_stats['approaching_rows']:
            writer.writerow([
                row['title'], row['status_label'],
                row['assigned_to'] or '未指派',
                timezone.localtime(row['due_at']).strftime('%Y-%m-%d %H:%M'),
                row['days_until_due'], row['note_length'],
            ])
    else:
        writer.writerow(['—', f"当前没有 {overdue_stats['due_soon_days']} 天内到期的未闭环任务", '', '', '', ''])

    writer.writerow([])
    writer.writerow(['任务状态分布', '任务数', '设了截止时间', '写过备注'])
    for row in followups['status_rows']:
        writer.writerow([row['label'], row['task_count'], row['with_due_count'], row['with_note_count']])

    writer.writerow([])
    writer.writerow([
        '说明',
        '待处理清单不按统计周期过滤：周期开始之前就超期的那个任务，恰恰是最不该被藏起来的一件。'
        '这里只报告，不自动派单、不改任务状态、不改截止时间、不新增任何提醒；'
        '备注是跟进人写给自己的内部备忘，只数字数，不做切词、不聚类、不推断语义，也不公开任何一条原文。',
    ])

    aging = dashboard['demand_task_aging']
    writer.writerow([])
    writer.writerow(['跟进任务的接手等待与停留', '数值', '说明'])
    writer.writerow(['口径与门槛', aging['summary'], '接手时长只报上界，停留时长只报当前快照，两个口径不能相加或相减'])
    writer.writerow(['有人接手', aging['handover']['handed_count'], f"占 {aging['handover']['handed_rate']}%"])
    writer.writerow(['还没人接手', aging['handover']['waiting_count'], f"占 {aging['handover']['waiting_rate']}%"])
    writer.writerow([
        '没人接手的等待中位数（天）',
        '—' if aging['handover']['waiting_median_days'] is None else aging['handover']['waiting_median_days'],
        '没人碰过的任务 updated_at 停在 created_at，这一段可以直接读',
    ])
    writer.writerow([
        '已接手的等待中位数（天）',
        '—' if aging['handover']['handed_median_days'] is None else aging['handover']['handed_median_days'],
        '只是上界：每一次改备注、改期限都会把它往后推，真实等待只会更长',
    ])
    writer.writerow([
        '两组等待中位数之差（天）',
        '—' if aging['handover']['gap_days'] is None else aging['handover']['gap_days'],
        (
            '两组各自不足样本下限，只报数字、不给方向'
            if not aging['handover']['is_measurable']
            else f"相差不足 {aging['handover']['noise_days']} 天视为噪声，读不出方向"
            if not aging['handover']['gap_is_meaningful']
            else '已出噪声区间，方向见看板'
        ),
    ])
    writer.writerow([])
    writer.writerow(['接手等待分档', '没人接手过', '已经有人接手'])
    handed_by_label = {row['label']: row['count'] for row in aging['handover']['handed_bucket_rows']}
    for row in aging['handover']['waiting_bucket_rows']:
        writer.writerow([row['label'], row['count'], handed_by_label.get(row['label'], 0)])
    writer.writerow([])
    writer.writerow(['当前状态', '任务数', '中位停留（天）', '最久（天）', '样本是否够'])
    for row in aging['residency']['rows']:
        writer.writerow([
            row['label'], row['task_count'],
            '—' if row['median_days'] is None else row['median_days'],
            row['max_days'],
            f"不足 {aging['residency']['min_bucket_sample']} 条" if row['is_small_sample'] else '够',
        ])
    writer.writerow([])
    writer.writerow(['最久没有活动的未闭环任务', '状态', '负责人', '多久没动（天）', '有期限', '备注（字）', '建任务时间'])
    if aging['stale_rows']['rows']:
        for row in aging['stale_rows']['rows']:
            writer.writerow([
                row['title'], row['status_label'], row['assigned_to'],
                row['residency_days'], '有' if row['has_due'] else '无', row['note_length'],
                row['created_at'].astimezone(timezone.get_current_timezone()).strftime('%Y-%m-%d %H:%M'),
            ])
        writer.writerow([
            '说明', f"这里列了 {aging['stale_rows']['row_limit']} 条，一共 {aging['stale_rows']['row_total']} 条",
            '清单只按未闭环筛：已接手但停住的任务同样在列，那正是最该被点开看看的一件',
            '', '', '', '',
        ])
    else:
        writer.writerow(['—', '当前没有未闭环的跟进任务', '', '', '', '', ''])
    writer.writerow([])
    writer.writerow([
        '说明',
        '待处理清单不按统计周期过滤：周期开始之前就没人管的那个任务，恰恰是最不该被藏起来的一件。'
        '这里只报告，不自动派单、不改任务状态、不改截止时间、不新增任何提醒；'
        '负责人只作为一列出现，用来找到人，不用来比较人；空平台报「—」不报 0 天。',
    ])

    snooze = dashboard['notification_snooze_return']
    writer.writerow([])
    writer.writerow(['延后的通知，延了之后回来了吗', '数值', '说明'])
    writer.writerow([
        '口径与门槛', snooze['summary'],
        '延后期从处理时长里拆出来；只报告，不自动取消延后、不自动标记已读、不加迁移、不改写入路径',
    ])
    writer.writerow([
        '周期发送', snooze['totals']['sent_total'],
        '按 created_at 落在统计周期内筛',
    ])
    writer.writerow([
        '被延后过', snooze['totals']['snoozed_total'] or 0,
        (
            f"占 {snooze['totals']['snoozed_rate']}%"
            if snooze['totals']['snoozed_rate'] is not None else '—'
        ),
    ])
    writer.writerow([])
    writer.writerow(['结局', '条数', '占比（%）', '口径说明'])
    outcome_notes = {
        'still_snoozed': '用户说「现在不方便」，还没到回来的时刻',
        'read_while_snoozed': '不等了，提前回来处理；还没到期就被一键已读的也落进这一档',
        'read_after_return': '回来了，而且读了；到期之后被一键已读的，也会落进这一档',
        'unread_after_return': '回来了，然后继续沉默——这是本模块最硬的一个数',
        'read_without_timestamp': 'read_at 上线之前标记的老数据，如实报出来、不并进任何一档',
    }
    if not snooze['has_data']:
        writer.writerow(['当前周期还没有通知数据，延后与回来需要先有通知存在。'])
    else:
        for row in snooze['totals']['outcome_rows']:
            writer.writerow([
                row['label'], row['count'], row['share'],
                outcome_notes.get(row['key'], ''),
            ])
        writer.writerow([])
        writer.writerow(['延后档位', '条数'])
        for row in snooze['totals']['duration_rows']:
            writer.writerow([row['label'], row['count']])
    writer.writerow([])
    writer.writerow(['回来之后多久被读', '条数', '口径说明'])
    if not snooze['has_data']:
        writer.writerow(['当前周期还没有通知数据。'])
    else:
        writer.writerow([
            '到点之后才被读', snooze['return_lag']['read_after_return_count'],
            '从 snoozed_until 起算，不从 created_at 起算',
        ])
        writer.writerow([
            '中位数', snooze['return_lag']['median_label'],
            '到期未读是右删失样本，单独数、不丢不混',
        ])
        writer.writerow([
            'P90', snooze['return_lag']['p90_label'],
            '取最近秩，也就是某次真实发生过的等待，不取插值',
        ])
        writer.writerow([
            '至今没读', snooze['return_lag']['unread_after_return_count'],
            '到期之后仍未读，不计入时长分布',
        ])
        writer.writerow([])
        writer.writerow(['等了多久', '到点之后才被读'])
        for row in snooze['return_lag']['bucket_rows']:
            writer.writerow([row['label'], row['count']])
    writer.writerow([])
    writer.writerow(['把延后期从处理时长里拆出来', '中位数', '可信度'])
    if not snooze['has_data']:
        writer.writerow(['当前周期还没有通知数据，延后与回来需要先有通知存在。'])
    else:
        writer.writerow([
            '含延期的处理时长', snooze['decomposed']['with_snooze_label'],
            'notification_response 的口径：read_at - created_at',
        ])
        writer.writerow([
            '扣掉延期的处理时长', snooze['decomposed']['without_snooze_label'],
            '本模块的口径：read_at - snoozed_until，到期之后隔了多久被读',
        ])
        writer.writerow([
            '用户主动要求的等待', snooze['decomposed']['requested_label'],
            'snoozed_until - created_at，是下界：每次延后都会覆盖，前几次延掉的等待不在里面',
        ])
        writer.writerow([
            '两者之差', snooze['decomposed']['gap_label'],
            '三个数各有各的可信度，摆在一起看，不相加也不相减',
        ])
    writer.writerow([])
    writer.writerow(['延后过 vs 从没延后过', '条数', '已读', '阅读率（%）'])
    if not snooze['has_data']:
        writer.writerow(['当前周期还没有通知数据，对照需要先有通知发生。'])
    else:
        writer.writerow([
            '延后过（只收已有结论的）', snooze['contrast']['snoozed_total'],
            snooze['contrast']['snoozed_read'], snooze['contrast']['snoozed_rate'],
        ])
        writer.writerow([
            '从没延后过', snooze['contrast']['plain_total'],
            snooze['contrast']['plain_read'], snooze['contrast']['plain_rate'],
        ])
        writer.writerow([
            '差值（百分点）',
            '—' if snooze['contrast']['gap_points'] is None else snooze['contrast']['gap_points'],
            (
                '两组各自不足样本下限，只报数字、不给方向'
                if not snooze['contrast']['is_measurable']
                else f"相差不足 {snooze['contrast']['noise_points']} 个百分点视为噪声，读不出方向"
                if not snooze['contrast']['gap_is_meaningful']
                else '已出噪声区间；这是人的差别，不是通知的差别'
            ),
            '', '',
        ])
    writer.writerow([])
    writer.writerow(['通知类型', '周期发送', '被延后过', '到点之后仍未读', '还在延后中',
                     '回来之后中位等待（天）', '样本是否够'])
    if not snooze['has_data']:
        writer.writerow(['当前周期还没有通知数据。'])
    else:
        for row in snooze['kind_rows']:
            writer.writerow([
                row['label'], row['sent'], row['snoozed'],
                row['unread_after_return'], row['still_snoozed'],
                '—' if row['median_days'] is None else row['median_days'],
                f"不足 {snooze['min_bucket_sample']} 条" if row['is_small_sample'] else '够',
            ])
    writer.writerow([])
    writer.writerow(['此刻（不按统计周期过滤）', '条数', '说明'])
    writer.writerow([
        '还在延后中', snooze['current']['still_snoozed_count'],
        'snoozed_until 还没到',
    ])
    writer.writerow([
        '到期未读', snooze['current']['overdue_count'],
        '周期开始之前就到期未读的那一条，恰恰是最不该被藏起来的一件',
    ])
    writer.writerow([
        '到期未读中位等待', snooze['current']['overdue_median_label'],
        '从 snoozed_until 到现在',
    ])
    writer.writerow([
        '到期未读最久等待', snooze['current']['overdue_max_days'],
        '此刻全站最长的一条',
    ])
    for row in snooze['current']['overdue_kind_rows']:
        writer.writerow(['到期未读集中在', row['label'], row['count']])
    writer.writerow([])
    writer.writerow([
        '说明',
        '延后和未读是两种完全不同的沉默：未读是提醒没被看见，延后是提醒被看见了、被明确地推到以后。'
        '把延后并进未读，未读率会虚高；把延后当成已处理，又会漏掉一批真的被搁置的事。'
        '本模块只报告：不加迁移、不加字段、不改写入路径、不自动取消延后、不自动标记已读；'
        'snoozed_until 每次延后都被覆盖，所以「延后过几次」在库里不存在，只报「当前有没有被延后」；'
        '不列任何一条通知的原文，按通知类型聚合——通知内容是收发双方的私域内容；'
        '不按用户排名，那是一份考勤表，不是一份看板。',
    ])
    incident = dashboard['meeting_incident_quality']
    writer.writerow([])
    writer.writerow(['交付预约异常处理得公不公平', '数值', '说明'])
    writer.writerow([
        '口径与门槛', incident['summary'],
        '只读 reason / status / resolution_note 与订单双方身份，不读 detail 的自由文本，'
        '也不改写任何一条异常',
    ])
    writer.writerow([
        '周期内异常', incident['outcome']['total_count'], '按异常提交时间统计',
    ])
    writer.writerow([
        '已完结', incident['outcome']['closed_count'],
        f"确认 {incident['outcome']['resolved_count']} 条 · 驳回 "
        f"{incident['outcome']['rejected_count']} 条",
    ])
    writer.writerow([
        '还在流程里', incident['outcome']['open_count'], 'open 与 reviewing 合计',
    ])
    writer.writerow([
        '整体驳回率（%）',
        '—' if incident['outcome']['rejection_rate'] is None else incident['outcome']['rejection_rate'],
        f"分母是已完结的 {incident['outcome']['closed_count']} 条，少于 "
        f"{incident['min_closed_incidents']} 条只报数字",
    ])
    writer.writerow([])
    writer.writerow(['驳回率按异常类型拆', '异常数', '已完结', '驳回数', '驳回率（%）', '还在流程里', '样本是否够'])
    for row in incident['reason_rows']:
        writer.writerow([
            row['label'], row['total_count'], row['closed_count'], row['rejected_count'],
            '—' if row['rejection_rate'] is None else row['rejection_rate'],
            row['open_count'],
            '这一档没有样本' if row['is_empty'] else (
                f"不足 {incident['min_bucket_sample']} 条" if row['is_small_sample'] else '够'
            ),
        ])
    writer.writerow([])
    writer.writerow(['处理意见写了没写', '条数', '占比（%）', '口径说明'])
    note = incident['note']
    writer.writerow([
        '已完结的异常', note['closed_count'], '',
        '只统计已完结的：还在流程里的异常谈不到写不写意见',
    ])
    writer.writerow([
        '写了意见', note['with_note_count'],
        '—' if note['fill_rate'] is None else note['fill_rate'],
        '只数字节与空否，一个字符内容都不读',
    ])
    writer.writerow([
        '没写意见', note['without_note_count'],
        '—' if note['fill_rate'] is None else round(100 - note['fill_rate'], 1),
        '被驳回又没写意见，提交人收到的是一个没有理由的驳回',
    ])
    for row in note['by_outcome']:
        writer.writerow([
            f"其中：{row['label']}写过意见", row['with_note_count'],
            '—' if row['fill_rate'] is None else row['fill_rate'],
            f"已{row['label']} {row['closed_count']} 条"
            + ('，不足样本下限只报数字'
               if row['closed_count'] < incident['min_bucket_sample'] else ''),
        ])
    writer.writerow([
        '两档填写率之差（百分点）',
        '—' if note['outcome_gap_points'] is None else note['outcome_gap_points'],
        '',
        '两侧都过样本门槛才允许把它读成一个信号，否则只报数字' if note['outcome_comparable']
        else '至少一侧不足样本下限，这个差不作结论',
    ])
    for row in note['bucket_rows']:
        writer.writerow([
            f"意见长度：{row['label']}", row['count'],
            '—' if row['share'] is None else row['share'],
            '长度只反映写没写细，不反映写得对不对',
        ])
    writer.writerow([])
    writer.writerow(['是谁在报谁', '异常数', '已完结', '驳回数', '驳回率（%）', '样本是否够'])
    for row in incident['direction']['rows']:
        writer.writerow([
            row['label'], row['total_count'], row['closed_count'], row['rejected_count'],
            '—' if row['rejection_rate'] is None else row['rejection_rate'],
            '这一档没有样本' if row['is_empty'] else (
                f"不足 {incident['min_bucket_sample']} 条" if row['is_small_sample'] else '够'
            ),
        ])
    writer.writerow([
        '两侧驳回率之差（百分点）',
        '—' if incident['direction']['gap_points'] is None else incident['direction']['gap_points'],
        '', '',
        '两侧都过样本门槛才允许把它读成一个信号，且必须同时报两侧总量' if incident['direction']['comparable']
        else '至少一侧不足样本下限，这个差不作结论',
    ])
    writer.writerow([])
    writer.writerow(['提交人分布', '提交人数', '占比（%）', '口径说明'])
    reporters = incident['reporters']
    if reporters['is_empty']:
        writer.writerow(['当前周期没有提交人，分布形状需要先有异常发生。'])
    else:
        writer.writerow([
            '提交人数', reporters['reporter_count'], '', '按人去重后的数量',
        ])
        for row in reporters['band_rows']:
            writer.writerow([
                f"提交次数：{row['label']}", row['count'],
                '—' if row['share'] is None else row['share'],
                '形状比名次有用：绝大多数人只报过一次，说明这条通道是应急性质的',
            ])
        writer.writerow([
            '只报过一次的人占比（%）',
            '—' if reporters['once_only_rate'] is None else reporters['once_only_rate'], '',
            '有一批人反复在用，说明它是某些用户的常规手段',
        ])
        writer.writerow([
            '人均提交次数',
            '—' if reporters['avg_count'] is None else reporters['avg_count'], '',
            '受 OneToOneField(appointment) 限制，不报「同一场约会里双方各报一次」——那个数恒为 0',
        ])
    writer.writerow([
        '说明',
        '交付预约异常是站内唯一一条直指线下人身安全的申诉，reason 与 status 一直被读写，'
        '但从来没有按类型与结局拆开看过：整体驳回率一旦波动，第一反应是去查处理人，'
        '而不是去查是哪一类异常在涨。本模块只报告：'
        '不加迁移、不改 MeetingIncident 的任何字段、不改写入路径、不自动驳回或自动关单；'
        '不读 detail 与 resolution_note 的自由文本，只读长度与空否；'
        '空分母报「—」不报 0——0 会被读成「发了，占比很小」，而事实是「没有样本」；'
        '不排名、不报任何人的用户名，那不是看板，是公开处刑清单；'
        '不把驳回率和处理人挂钩——按 reviewer 拆驳回率是一份考官成绩单。',
    ])
    notice = dashboard['moderation_notice_preference']
    writer.writerow([])
    writer.writerow(['内容审核结果通知：开关、触达与沉默', '数值', '说明'])
    writer.writerow([
        '口径与门槛', notice['summary'],
        '只读 ModerationEvent 的复核结论、NotificationPreference 的开关值和 '
        'moderation_update 通知的已读标志，不读 content 与通知正文的自由文本，'
        '也不改写任何一处写入路径',
    ])
    writer.writerow([
        '周期内复核完', notice['outcome']['reviewed_count'],
        '只收 confirmed 与 dismissed，pending 是右删失样本',
    ])
    writer.writerow([
        '送到了作者眼前', notice['outcome']['delivered_count'],
        f"占 {notice['outcome']['delivered_rate']}%"
        if notice['outcome']['delivered_rate'] is not None else '空分母',
    ])
    writer.writerow([
        '作者关掉了这一类', notice['outcome']['switch_off_count'],
        'create_notification 在开关处静默返回，连通知行都不建',
    ])
    writer.writerow([
        '作者已注销', notice['outcome']['missing_author_count'],
        'author 是 SET_NULL，没有作者的复核结论谈不上送到谁眼前',
    ])
    if notice['has_data']:
        writer.writerow([])
        writer.writerow(['这条结论送到作者眼前了没有', '记录数', '送到了', '开关关着', '免打扰时段', '复核人是作者本人', '取不到偏好行'])
        for row in notice['reach_rows']:
            writer.writerow([
                row['label'], row['total_count'], row['delivered'],
                row['switch_off'], row['quiet_hours'],
                row['self_authored'], row['no_preference_row'],
            ])
        writer.writerow([
            '说明',
            '四种没送到的成因分开数，不合成一个未送达率：合成之后，这个数会随「复核人'
            '是不是内容作者」这种人极少的情况抖动，而那和用户有没有关开关毫无关系。',
        ])
        writer.writerow([])
        writer.writerow(['通知发出去之后，读没读', '条数', '已读', '未读', '已读率（%）'])
        for row in notice['read_rows']:
            writer.writerow([
                row['label'], row['sent_count'], row['read_count'],
                row['unread_count'],
                '—' if row['read_rate'] is None else row['read_rate'],
            ])
        baseline = notice['baseline_read_rate']
        writer.writerow([
            '同期全部通知', baseline['sent_count'], baseline['read_count'],
            baseline['sent_count'] - baseline['read_count'],
            '—' if baseline['read_rate'] is None else baseline['read_rate'],
        ])
        writer.writerow([
            '说明',
            '已读率按复核结论拆：确认违规和误判放行是两条情绪上完全不同的消息，'
            '它们的已读率没有理由相同。最后一行是同期全部通知的整体已读率，'
            '作对照分母——少了它，单看一类的已读率读不出「这一类是不是特别没人看」。',
        ])
    else:
        writer.writerow(['当前周期没有复核完的记录，触达与已读都无从读起。'])
    grid = notice['preference_grid']
    writer.writerow([])
    writer.writerow(['偏好开关四格', '人数', '说明'])
    writer.writerow(['有内容被复核 · 开关开着', grid['reviewed_switch_on'], ''])
    writer.writerow(['有内容被复核 · 开关关着', grid['reviewed_switch_off'], ''])
    writer.writerow(['没内容被复核 · 开关开着', grid['unreviewed_switch_on'], ''])
    writer.writerow(['没内容被复核 · 开关关着', grid['unreviewed_switch_off'], ''])
    writer.writerow([
        '说明',
        '只报四格的人数，不报用户名、不排名，也不报任何两格之间的比值。'
        '后两格的人没被复核过内容，他们关不关这个开关和「收到过审核结果」没有因果关系——'
        '直接拿第二格比第四格，比出来的是两群不同的人，不是同一群人的两种选择。',
    ])
    if notice['activity']['has_data']:
        writer.writerow([])
        writer.writerow(['关掉之后，还在不在社区里说话', '关掉的一组', '开着的一组', '方向'])
        for row in notice['activity']['rows']:
            writer.writerow([
                row['label'],
                f"{row['off_count']} / {row['off_total']}",
                f"{row['on_count']} / {row['on_total']}",
                {
                    'switch_off_higher': '关掉的一组更高',
                    'switch_on_higher': '开着的一组更高',
                    'same': '两组相同',
                }.get(row['direction'], '样本不足，只报人数'),
            ])
        writer.writerow([
            '说明',
            '只报同期人群的对照形状，不作因果：关掉通知不会让人不说话，'
            '收到一条「你违规了」也不会，两群人在发言率上的差可能只是'
            '「常发言的人更在意审核结果」。方向只给三档，不给百分比差——'
            '一个 3 个百分点和一个 30 个百分点在看板上长一样，给的指引却完全相反。'
            '两群人都过 '
            f"{notice['min_preference_users']} 人才给方向。",
        ])
    else:
        writer.writerow(['当前周期关掉与开着两组人里有一组是空的，发言对照无从读起。'])
    attendance = dashboard['meeting_attendance']

    coverage = dashboard['notification_kind_coverage']
    writer.writerow([])
    writer.writerow(['通知类型、偏好开关与用户手上的那一栏', '数值', '说明'])
    writer.writerow([
        '口径与门槛', coverage['summary'],
        '三个来源（模型元数据、KIND_CHOICES、偏好表单字段）全部从代码现读，不硬编码名单',
    ])
    writer.writerow([
        '已登记的通知类型', coverage['current']['kind_total'],
        'KIND_CHOICES 与偏好开关的并集，不是只报其中一边',
    ])
    writer.writerow([
        '没有名字的类型', coverage['current']['unnamed_count'],
        '不在 KIND_CHOICES 里，用户在自己的通知中心里按类型筛不出它们；'
        '上一轮的两种窟窿都已补上，现在是 0 且必须继续是 0',
    ])
    writer.writerow([
        '用户关不掉的开关', coverage['current']['unreachable_count'],
        '模型上有、默认开着、真的在发，但不在偏好表单里，页面上没有那一行；'
        '上一轮的两种窟窿都已补上，现在是 0 且必须继续是 0',
    ])
    writer.writerow([
        '周期内通知总数', coverage['current']['total_sent'],
        '按 created_at 落在统计周期内筛',
    ])
    writer.writerow([])
    writer.writerow(['没有名字的类型', '周期发送', '被读', '占全部通知（%）', '样本是否够'])
    if not coverage['has_data']:
        writer.writerow(['当前周期还没有通知数据。'])
    elif not coverage['unnamed_rows']:
        # 差集空和「还没看」是两件事：前者是三个来源此刻对齐，后者是这一格没内容。
        writer.writerow(['当前没有正在发送却没有名字的类型。'])
    else:
        for row in coverage['unnamed_rows']:
            writer.writerow([
                row['label'],
                row['sent_count'] if row['sent_in_period'] else '—',
                row['read_count'] if row['sent_in_period'] else '—',
                '—' if row['share'] is None else row['share'],
                f"不足 {coverage['min_bucket_sample']} 条" if row['is_small_sample'] else '够',
            ])
    writer.writerow([])
    writer.writerow(['用户关不掉的开关', '周期发送', '被读', '占全部通知（%）', '样本是否够'])
    if not coverage['has_data']:
        writer.writerow(['当前周期还没有通知数据。'])
    elif not coverage['unreachable_rows']:
        writer.writerow(['当前每个类型的开关都在偏好页面上。'])
    else:
        for row in coverage['unreachable_rows']:
            writer.writerow([
                row['label'],
                row['sent_count'] if row['sent_in_period'] else '—',
                row['read_count'] if row['sent_in_period'] else '—',
                '—' if row['share'] is None else row['share'],
                f"不足 {coverage['min_bucket_sample']} 条" if row['is_small_sample'] else '够',
            ])
    writer.writerow([])
    writer.writerow(['周期内真的发过的类型，偏好的实际状态', '接收人数', '周期发送', '关掉了', '关闭率（%）', '样本是否够'])
    if not coverage['preference_rows']:
        writer.writerow(['周期内没有发送记录，或接收人不足样本门槛，暂不判断偏好状态。'])
    else:
        for row in coverage['preference_rows']:
            writer.writerow([
                row['label'], row['recipient_count'], row['sent_count'],
                row['switched_off'], row['switched_off_rate'],
                f"不足 {coverage['min_bucket_sample']} 人" if row['is_small_sample'] else '够',
            ])
    writer.writerow([])
    writer.writerow([
        '说明',
        '三个数本应相等，而它们不相等：KIND_CHOICES、NotificationPreference 的布尔开关、'
        'NotificationPreferenceForm.Meta.fields 各是一张表，各自都自洽，差额一直被「看起来像」吃掉了。'
        '要看见它，得把三个数并排放，而不是各自数。'
        '「没有名字」问的是用户收到的东西在看板上叫什么，「关不掉」问的是用户能不能对自己的通知说不，'
        '两者问的不是同一件事，所以分两张表报，不相加也不相减。'
        '开关状态按发过通知的接收人统计，不按全站用户：一个从没收过这一类通知的用户，他的开关开着或关着'
        '都没有行为意义。这里只报告：不加迁移、不改 KIND_CHOICES、不改 NotificationPreference、'
        '不改表单字段列表；不按用户排名，那是一份考勤表，不是一份看板；'
        '不列任何一条通知的原文；空分母报「—」不报 0——0 会被读成「发了，占比很小」。',
    ])
    usage = dashboard['notification_preference_usage']
    writer.writerow([])
    writer.writerow([f"通知偏好开关：{usage['field_total']} 个开关各自的形状", '数值', '说明'])
    writer.writerow([
        '口径与门槛', usage['summary'],
        '开关名单从模型元数据现读，不硬编码；被开关挡掉的通知连一行都不建，'
        '所以「关掉之后通知少了多少」不在本模块口径里',
    ])
    writer.writerow([
        '有偏好行的用户', usage['coverage']['rows_total'],
        'NotificationPreference 逐用户一行，get_or_create 出来，默认全开',
    ])
    writer.writerow([
        '收到过通知却没有偏好行', usage['coverage']['users_without_row'],
        '单列一格，不并入「开着」——否则「多少人碰过设置页」永远问不出来',
    ])
    writer.writerow([
        '开着免打扰总闸', usage['quiet_hours']['on'],
        f"quiet_hours_enabled 不是通知类型，但和 {usage['field_total']} 个开关摆在同一张表单里",
    ])
    writer.writerow([
        '关掉免打扰总闸', usage['quiet_hours']['off'],
        '只压角标，不挡通知本身，通知中心里仍能打开',
    ])
    writer.writerow([])
    writer.writerow(['按「关掉它有没有交易后果」分组', '开着', '关掉', '合计', '关闭占比（%）', '样本是否够'])
    if not usage['by_group']:
        writer.writerow(['当前平台还没有任何偏好行。'])
    else:
        for row in usage['by_group']:
            writer.writerow([
                row['label'], row['on'], row['off'], row['on'] + row['off'],
                '—' if row['share'] is None else row['share'],
                f"不足 {usage['min_preference_rows']} 行" if not row['has_enough_rows'] else '够',
            ])
    writer.writerow([
        '说明',
        '不合成一个全站关闭率：合成之后，「用户关掉了运营告警日报」和「用户关掉了新订单通知」'
        '会读成同一件事，而前者是用户在减噪、后者可能让用户错过一笔成交。'
        '四组各自要过 '
        f"{usage['min_preference_rows']} 行才给占比。",
    ])
    writer.writerow([])
    writer.writerow([f"{len(usage['rows'])} 个开关逐个看", '分组', '开着', '关掉', '近期收到过的用户', '关着却仍收到过', '开着却没收到过', '样本是否够'])
    if not usage['has_data']:
        writer.writerow(['当前平台还没有任何偏好行。'])
    else:
        for row in usage['rows']:
            writer.writerow([
                row['field'], row['group_label'], row['on'], row['off'],
                row['notified_users'], row['off_but_seen'], row['on_but_unseen'],
                f"不足 {usage['min_preference_rows']} 行" if not row['has_enough_rows'] else '够',
            ])
    writer.writerow([
        '说明',
        '「关着却仍收到过」是这一节的观测对象：它只可能是窗口前收到的，或者汇总类通知'
        '（operations_digest / opportunity_digest / saved_search_digest）在另一个时刻生成。'
        '这一格很大，说明这个开关要么没接上、要么接的是一条用户没意识到的路径。'
        '「开着却没收到过」不是漏报——一个从没被预约过的用户开着 order_created 是天经地义。'
        '两格都只报人数，不报任何两格之间的比值。'
        '本模块只报告：不加迁移、不加字段、不改 create_notification 的静默 return None、'
        '不给任何人自动打开开关；不按用户排名、不输出用户名。',
    ])
    bypass_diagnosis = dashboard['notification_preference_bypass_diagnosis']
    writer.writerow([])
    writer.writerow(['通知偏好关了为什么还看得到', '数值', '说明'])
    writer.writerow([
        '口径与门槛', bypass_diagnosis['summary'],
        '通知时间优先读 last_occurred_at，为空回退 created_at；只按当前周期切窗口',
    ])
    writer.writerow([
        '有偏好行的用户', bypass_diagnosis['coverage']['preference_users'],
        'updated_at 是整行偏好的最后写入时间，不是逐开关关闭时间',
    ])
    writer.writerow([
        '收到过通知但没有偏好行', bypass_diagnosis['coverage']['no_preference_users'],
        '模型默认全开；收到通知不算异常，也不并入开着或关着',
    ])
    writer.writerow([
        '当前关着且本期出现', bypass_diagnosis['totals']['off_seen_users'],
        '这是上一个面板的「关着却仍收到过」，本节把它按时间顺序拆开',
    ])
    writer.writerow([
        '偏好写入后仍出现', bypass_diagnosis['totals']['suspicious_users'],
        f"{bypass_diagnosis['totals']['suspicious_rows']} 条通知行构成观察下界；不报绕过率",
    ])
    writer.writerow([])
    writer.writerow(['逐类型拆开时间顺序', '已知路径', '当前关着', '本期出现', '与后关一致', '偏好写入后仍出现', '可疑行数下界'])
    if not bypass_diagnosis['has_data']:
        writer.writerow(['当前平台还没有任何偏好行。'])
    else:
        for row in bypass_diagnosis['rows']:
            writer.writerow([
                row['field'], row['writer'], row['off_users'], row['seen_users'],
                row['consistent_users'], row['suspicious_users'], row['suspicious_rows'],
            ])
    writer.writerow([
        '说明',
        '「与后关一致」只表示时间顺序不冲突，不证明开关必然生效；'
        '「偏好写入后仍出现」也只是下界——后来保存过其他偏好会把 updated_at 推晚，'
        '从而掩盖更早的异常。表格按模型字段顺序输出，不按可疑人数排序；'
        '不读标题正文、不输出用户名、不按用户排名、不自动改偏好。',
    ])
    quiet_hours_shape = dashboard['notification_quiet_hours_shape']
    writer.writerow([])
    writer.writerow(['免打扰时段的形状', '数值', '说明'])
    writer.writerow([
        '口径与门槛', quiet_hours_shape['summary'],
        '快照读全部偏好行，days 只为与其他面板同屏而不参与过滤；免打扰只压角标，不挡通知入库',
    ])
    writer.writerow([])
    writer.writerow(['总闸与配置'])
    writer.writerow(['指标', '数值', '说明'])
    writer.writerow([
        '偏好行', quiet_hours_shape['preference_count'],
        '分母是偏好行，不是全体用户',
    ])
    writer.writerow([
        '没有偏好行', quiet_hours_shape['no_preference_count'],
        '单列一格，不并入「关掉免打扰」',
    ])
    writer.writerow([
        '开启免打扰',
        '—' if quiet_hours_shape['enabled_share'] is None else quiet_hours_shape['enabled_share'],
        f"{quiet_hours_shape['enabled_count']} 行开着总闸",
    ])
    writer.writerow([
        '照抄默认窗口',
        '—' if quiet_hours_shape['default_window_share'] is None else quiet_hours_shape['default_window_share'],
        f"{quiet_hours_shape['default_window_count']} 行为 22:00-08:00",
    ])
    writer.writerow([
        '开启且自选窗口',
        '—' if quiet_hours_shape['custom_window_share'] is None else quiet_hours_shape['custom_window_share'],
        f"{quiet_hours_shape['custom_window_count']} 行没有照抄默认窗口",
    ])
    writer.writerow([
        '总闸关着但已配自选窗口', quiet_hours_shape['disabled_custom_count'],
        '碰过窗口，却没有真正用上；不进方向、时长与逐小时叠加',
    ])
    writer.writerow([
        '开启且开始等于结束', quiet_hours_shape['invalid_equal_count'],
        '表单挡住了这条路径；非 0 说明有数据绕过表单，是待修 bug，不是一档正常分布',
    ])
    writer.writerow([])
    writer.writerow(['窗口方向与时长'])
    writer.writerow(['观察项', '数量', '占比（%）', '说明'])
    for row in quiet_hours_shape['direction_rows']:
        writer.writerow([
            row['label'], row['count'],
            '—' if row['share'] is None else row['share'],
            f"生效窗口不足 {quiet_hours_shape['min_sample']} 行，只作参考" if row['is_small_sample'] else '',
        ])
    for row in quiet_hours_shape['duration_rows']:
        writer.writerow([
            row['label'], row['count'],
            '—' if row['share'] is None else row['share'],
            '时长档左闭右开；0 样本档保留' if not row['is_empty'] else '这一档没有样本',
        ])
    writer.writerow([
        '开始等于结束', quiet_hours_shape['invalid_equal_count'], '—',
        '不进方向、时长、小时和分钟叠加；实际读取逻辑会把它当成全天生效',
    ])
    writer.writerow([])
    writer.writerow(['开始与结束小时'])
    writer.writerow(['开始小时', '数量', '占比（%）', '样本'])
    for row in quiet_hours_shape['start_hour_rows']:
        writer.writerow([
            row['label'], row['count'],
            '—' if row['share'] is None else row['share'],
            f"不足 {quiet_hours_shape['min_sample']} 行" if row['is_small_sample'] else '可读数',
        ])
    writer.writerow(['结束小时', '数量', '占比（%）', '样本'])
    for row in quiet_hours_shape['end_hour_rows']:
        writer.writerow([
            row['label'], row['count'],
            '—' if row['share'] is None else row['share'],
            f"不足 {quiet_hours_shape['min_sample']} 行" if row['is_small_sample'] else '可读数',
        ])
    writer.writerow([])
    writer.writerow(['逐小时叠加与全天峰值'])
    writer.writerow(['全天峰值人数', '数值', '说明'])
    writer.writerow([
        '同一分钟被压着角标的最多人数', quiet_hours_shape['peak']['count'],
        '按 1440 个分钟格子取最大值',
    ])
    writer.writerow([
        '峰值首次出现', quiet_hours_shape['peak']['first_minute_label'],
        '同一峰值只报第一次出现的分钟',
    ])
    writer.writerow(['小时', '平均压着人数', '小时峰值人数', '说明'])
    for row in quiet_hours_shape['hour_rows']:
        writer.writerow([
            row['label'], row['average_count'], row['peak_count'],
            '这一小时没有生效窗口覆盖' if row['peak_count'] == 0 else '',
        ])
    writer.writerow([
        '说明',
        '本模块只报告：不加迁移、不改 quiet_hours_active、不改表单校验、不自动打开总闸。'
        '免打扰只压顶部角标，通知照常入库，不报「提醒少了多少」；不读 Notification 表。'
        '不按用户排名、不输出用户名、不列任何人的具体窗口。',
    ])
    aggregation = dashboard['notification_aggregation_quality']
    writer.writerow([])
    writer.writerow(['通知聚合的质量：合并掉的是打扰，还是信息', '数值', '说明'])
    writer.writerow([
        '口径与门槛', aggregation['summary'],
        '只统计带聚合键的通知；空档报「—」不报 0——0 会被读成「发了没人读」，而事实是「没有样本」',
    ])
    writer.writerow([
        '周期内带键的通知行', aggregation['current']['keyed_row_count'],
        'dedupe_key 为空的通知走的是一条不查重的路径，不参与本模块的对照',
    ])
    writer.writerow([
        '承载的事件数', aggregation['current']['event_count'],
        '各行 occurrence_count 之和',
    ])
    writer.writerow([
        '被合并掉的事件数', aggregation['current']['compressed_event_count'],
        '事件数减行数；这一个数已经在 analytics 的压缩率里，这里只作分母说明',
    ])
    writer.writerow([
        '聚合率（%）',
        '—' if aggregation['current']['aggregation_rate'] is None else aggregation['current']['aggregation_rate'],
        '合并掉的事件占全部事件的比例',
    ])
    writer.writerow([
        '被聚合过的行', aggregation['current']['merged_row_count'],
        'occurrence_count 大于 1 的行',
    ])
    writer.writerow([])
    writer.writerow(['聚合次数分档', '条数', '已读', '阅读率（%）', '平均跨度', '口径说明'])
    if not aggregation['has_data']:
        writer.writerow(['当前周期还没有带聚合键的通知，分档需要先有通知发生。'])
    else:
        for row in aggregation['occurrence_bands']:
            writer.writerow([
                row['label'], row['row_count'], row['read_count'],
                '—' if row['read_rate'] is None else row['read_rate'],
                row['avg_span_label'],
                '这一档没有样本' if row['is_empty'] else '',
            ])
        writer.writerow([])
        writer.writerow(['聚合跨度（第一条到最后一条隔了多久）', '条数', '占比（%）', '口径说明'])
        for row in aggregation['span_rows']:
            writer.writerow([
                row['label'], row['row_count'],
                '—' if row['share'] is None else row['share'],
                '跨度和次数是两个维度：9 次摊在 10 分钟内和 9 次摊在 5 天里是两件事'
                if not row['is_empty'] else '这一档没有样本',
            ])
    writer.writerow([])
    writer.writerow(['聚合过的行 vs 从没被聚合过的行', '条数', '事件数', '已读', '未读',
                     '阅读率（%）', '平均处理时长', 'P90 处理时长'])
    if not aggregation['has_data']:
        writer.writerow(['当前周期还没有带聚合键的通知，对照需要先有通知发生。'])
    else:
        for row in aggregation['comparison']['groups']:
            writer.writerow([
                row['label'], row['row_count'], row['event_count'],
                row['read_count'], row['unread_count'],
                '—' if row['read_rate'] is None else row['read_rate'],
                row['avg_label'], row['p90_label'],
            ])
        writer.writerow([
            '差值（百分点）',
            '—' if aggregation['comparison']['read_rate_delta'] is None
            else aggregation['comparison']['read_rate_delta'],
            (
                '两侧都不足样本下限，只报数字、不给方向'
                if not aggregation['comparison']['comparable']
                else '这是两组之间的差距，不等于聚合本身有害——两组还差着类型的构成'
            ),
            '', '', '', '',
        ])
    writer.writerow([])
    writer.writerow(['通知类型', '通知行', '事件数', '被聚合的行', '聚合率（%）',
                     '平均每行次数', '最大次数', '平均跨度', '阅读率（%）', '样本是否够'])
    if not aggregation['has_data']:
        writer.writerow(['当前周期还没有带聚合键的通知。'])
    else:
        for row in aggregation['kind_rows']:
            writer.writerow([
                row['label'], row['row_count'], row['event_count'],
                row['merged_row_count'],
                '—' if row['aggregation_rate'] is None else row['aggregation_rate'],
                '—' if row['avg_occurrence'] is None else row['avg_occurrence'],
                row['max_occurrence'], row['avg_span_label'],
                '—' if row['read_rate'] is None else row['read_rate'],
                f"不足 {aggregation['min_bucket_sample']} 条" if row['is_small_sample'] else '够',
            ])
    writer.writerow([])
    writer.writerow(['合并行的结构', '数值', '说明'])
    if aggregation['structure']['is_empty']:
        writer.writerow(['当前周期没有被聚合过的通知行。'])
    else:
        writer.writerow([
            '被合并过的行', aggregation['structure']['merged_row_count'],
            'occurrence_count 大于 1 的行',
        ])
        writer.writerow([
            '其中没有跳转地址的',
            '—' if aggregation['structure']['missing_target_rate'] is None
            else f"{aggregation['structure']['missing_target_count']} 条"
                 f"（{aggregation['structure']['missing_target_rate']}%）",
            '合并会覆盖 target_url，而 title 停留在第一次——用户点进去看到的东西，和标题说的可能不是一件事',
        ])
    writer.writerow([])
    writer.writerow([
        '说明',
        '合并是通知系统里唯一一条写入时合并的路径：窗口内找到未读的同类行，occurrence_count 加一、'
        '覆盖 message 与 target_url、title 不动。读这两个字段的地方此前只有一个压缩率，而比例天生看不出'
        '样本——「合并率 60%」既可以是两分钟内的连击，也可以是拖了四天的堆积。本模块只报告：'
        '不改 create_notification、不改 dedupe_window_seconds 默认值、不加迁移、不自动拆分聚合行、'
        '不自动标记已读；未读行是右删失样本，单独数、不当 0 秒算进时长；'
        '阅读率的分母是行数不是事件数——一条压着 9 件事的未读通知，用户一次点击就能消掉它，'
        '但它压着的事一件都没被处理；两组差距不归因给聚合，两组之间还差着类型的构成；'
        '不读 message 的语义，只报 target_url 的空值比例这个结构事实；'
        '不按用户排名，那是一份考勤表，不是一份看板。',
    ])

    quality = dashboard['report_resolution_quality']
    writer.writerow([])
    writer.writerow(['商品举报处理得公不公平', '数值', '说明'])
    writer.writerow([
        '口径与门槛', quality['summary'],
        '只读 reason / status / review_note 的长度与空否、结构化结果回访，不读 detail 的自由文本，也不改写任何一条举报',
    ])
    writer.writerow([
        '周期内举报', quality['outcome']['total_count'], '按举报创建时间统计',
    ])
    writer.writerow([
        '已完结', quality['outcome']['closed_count'],
        f"处理 {quality['outcome']['resolved_count']} 条 · 驳回 {quality['outcome']['rejected_count']} 条",
    ])
    writer.writerow([
        '还在流程里', quality['outcome']['open_count'], 'pending 与 reviewing 合计',
    ])
    writer.writerow([
        '整体驳回率（%）',
        '—' if quality['outcome']['rejection_rate'] is None else quality['outcome']['rejection_rate'],
        f"分母是已完结的 {quality['outcome']['closed_count']} 条，少于 "
        f"{quality['min_closed_reports']} 条只报数字",
    ])
    feedback = quality['outcome_feedback']
    writer.writerow([
        '结果回访率（%）',
        '—' if feedback['response_rate'] is None else feedback['response_rate'],
        f"分母是已完结的 {feedback['closed_count']} 条；未回访 {feedback['waiting_count']} 条是右删失样本",
    ])
    writer.writerow([])
    writer.writerow(['结果回访说了什么', '条数', '占比（%）', '已处理结局', '已驳回结局'])
    for row in feedback['choice_rows']:
        writer.writerow([
            row['label'], row['count'],
            '—' if row['share'] is None else row['share'],
            row['resolved_count'], row['rejected_count'],
        ])
    for row in feedback['by_outcome']:
        writer.writerow([
            f"其中：{row['label']}已回访", row['replied_count'],
            '—' if row['response_rate'] is None else row['response_rate'],
            '', f"已{row['label']} {row['closed_count']} 条",
        ])
    writer.writerow(['结果回访隔了多久', '条数', '占比（%）', '口径说明'])
    for row in feedback['latency_rows']:
        writer.writerow([
            row['label'], row['count'],
            '—' if row['share'] is None else row['share'],
            '完结到回访，负时延与缺审核时间不进分母',
        ])
    writer.writerow([
        '回访负时延 / 缺审核时间',
        f"{feedback['invalid_latency_count']} / {feedback['missing_reviewed_at_count']}",
        '', '异常单独计数，不用 0 小时伪装',
    ])
    writer.writerow([])
    writer.writerow(['驳回率按举报原因拆', '举报数', '已完结', '驳回数', '驳回率（%）', '还在流程里', '样本是否够'])
    for row in quality['reason_rows']:
        writer.writerow([
            row['label'], row['total_count'], row['closed_count'], row['rejected_count'],
            '—' if row['rejection_rate'] is None else row['rejection_rate'],
            row['open_count'],
            '这一档没有样本' if row['is_empty'] else (
                f"不足 {quality['min_bucket_sample']} 条" if row['is_small_sample'] else '够'
            ),
        ])
    writer.writerow([])
    writer.writerow(['审核意见写了没写', '条数', '占比（%）', '口径说明'])
    note = quality['note']
    writer.writerow([
        '已完结的举报', note['closed_count'], '',
        '只统计已完结的：还在流程里的举报谈不到写不写意见',
    ])
    writer.writerow([
        '写了意见', note['with_note_count'],
        '—' if note['fill_rate'] is None else note['fill_rate'],
        '只数字节与空否，一个字符内容都不读',
    ])
    writer.writerow([
        '没写意见', note['without_note_count'],
        '—' if note['fill_rate'] is None else round(100 - note['fill_rate'], 1),
        '被驳回又没写意见，用户收到的是一个没有理由的拒绝',
    ])
    for row in note['by_outcome']:
        writer.writerow([
            f"其中：{row['label']}写过意见", row['with_note_count'],
            '—' if row['fill_rate'] is None else row['fill_rate'],
            f"已{row['label']} {row['closed_count']} 条"
            + ('，不足样本下限只报数字'
               if row['closed_count'] < quality['min_bucket_sample'] else ''),
        ])
    writer.writerow([
        '两档填写率之差（百分点）',
        '—' if note['outcome_gap_points'] is None else note['outcome_gap_points'],
        '',
        '两侧都过样本门槛才允许把它读成一个信号，否则只报数字' if note['outcome_comparable']
        else '至少一侧不足样本下限，这个差不作结论',
    ])
    for row in note['bucket_rows']:
        writer.writerow([
            f"意见长度：{row['label']}", row['count'],
            '—' if row['share'] is None else row['share'],
            '长度只反映写没写细，不反映写得对不对',
        ])
    writer.writerow([])
    writer.writerow(['被驳回的举报集中在哪', '数值', '口径说明'])
    conc = quality['concentration']
    if conc['is_empty']:
        writer.writerow(['当前周期没有被驳回的举报，集中度需要先有驳回发生。'])
    else:
        writer.writerow([
            '被驳回的举报', conc['rejected_count'], '只统计已完结且驳回的',
        ])
        writer.writerow([
            '涉及商品', conc['item_count'], '只报规模，不列商品名',
        ])
        writer.writerow([
            '涉及卖家', conc['seller_count'], '只报规模，不列卖家名',
        ])
        writer.writerow([
            '其中商品已不在售', conc['inactive_item_count'],
            '已预订 / 已售出 / 已过期；一件不在了的商品被举报，和一件正在卖的被举报是两件事',
        ])
        writer.writerow([
            '被多次举报且被驳回的商品', conc['repeat_item_count'], '集中度大于 1 说明有几件在被反复举报',
        ])
        writer.writerow([
            '单件商品最多被驳回几次', conc['top_item_count'], '同一件商品上的举报来自不同举报人才计',
        ])
        writer.writerow([
            '每件被驳回商品平均摊到几条举报', conc['items_per_report'],
            '接近 1 是一举报对一商品，明显大于 1 是反复举报',
        ])
    writer.writerow([])
    writer.writerow(['举报人分布', '举报人数', '占比（%）', '口径说明'])
    reporters = quality['reporters']
    if reporters['is_empty']:
        writer.writerow(['当前周期没有举报人，分布形状需要先有举报发生。'])
    else:
        writer.writerow([
            '举报人数', reporters['reporter_count'], '', '按人去重后的数量',
        ])
        for row in reporters['band_rows']:
            writer.writerow([
                f"举报次数：{row['label']}", row['count'],
                '—' if row['share'] is None else row['share'],
                '形状比名次有用：绝大多数人只举报过一次，说明这条通道是应急性质的',
            ])
        writer.writerow([
            '只举报过一次的人占比（%）',
            '—' if reporters['once_only_rate'] is None else reporters['once_only_rate'], '',
            '有一批人反复在用，说明它是某些用户的常规手段',
        ])
        writer.writerow([
            '人均举报次数',
            '—' if reporters['avg_count'] is None else reporters['avg_count'], '',
            '受 UniqueConstraint 限制，不报「同一用户对同一商品的重复举报」——那个数恒为 0',
        ])
    writer.writerow([
        '说明',
        '商品举报是站内最早的治理队列，reason 与 status 一直被读写，但从来没有按原因与结局拆开看过：'
        '整体驳回率一旦波动，第一反应是去查处理人，而不是去查是哪一类举报在涨。本模块只报告：'
        '不加迁移、不改 Report 的任何字段、不改写入路径、不自动驳回或自动关单；'
        '不读 detail 与 review_note 的自由文本，只读长度与空否；'
        '空分母报「—」不报 0——0 会被读成「发了，占比很小」，而事实是「没有样本」；'
        '不列商品名、不列卖家名、不列举报人，那不是看板，是公开处刑清单；'
        '集中度只报规模形状，不作「这几件有问题」的判定。',
    ])

    incident = dashboard['meeting_incident_quality']
    writer.writerow([])
    writer.writerow(['交付预约异常处理得公不公平', '数值', '说明'])
    writer.writerow([
        '口径与门槛', incident['summary'],
        '只读 reason / status / resolution_note 与订单双方身份，不读 detail 的自由文本，'
        '也不改写任何一条异常',
    ])
    writer.writerow([
        '周期内异常', incident['outcome']['total_count'], '按异常提交时间统计',
    ])
    writer.writerow([
        '已完结', incident['outcome']['closed_count'],
        f"确认 {incident['outcome']['resolved_count']} 条 · 驳回 "
        f"{incident['outcome']['rejected_count']} 条",
    ])
    writer.writerow([
        '还在流程里', incident['outcome']['open_count'], 'open 与 reviewing 合计',
    ])
    writer.writerow([
        '整体驳回率（%）',
        '—' if incident['outcome']['rejection_rate'] is None else incident['outcome']['rejection_rate'],
        f"分母是已完结的 {incident['outcome']['closed_count']} 条，少于 "
        f"{incident['min_closed_incidents']} 条只报数字",
    ])
    writer.writerow([])
    writer.writerow(['驳回率按异常类型拆', '异常数', '已完结', '驳回数', '驳回率（%）', '还在流程里', '样本是否够'])
    for row in incident['reason_rows']:
        writer.writerow([
            row['label'], row['total_count'], row['closed_count'], row['rejected_count'],
            '—' if row['rejection_rate'] is None else row['rejection_rate'],
            row['open_count'],
            '这一档没有样本' if row['is_empty'] else (
                f"不足 {incident['min_bucket_sample']} 条" if row['is_small_sample'] else '够'
            ),
        ])
    writer.writerow([])
    writer.writerow(['处理意见写了没写', '条数', '占比（%）', '口径说明'])
    note = incident['note']
    writer.writerow([
        '已完结的异常', note['closed_count'], '',
        '只统计已完结的：还在流程里的异常谈不到写不写意见',
    ])
    writer.writerow([
        '写了意见', note['with_note_count'],
        '—' if note['fill_rate'] is None else note['fill_rate'],
        '只数字节与空否，一个字符内容都不读',
    ])
    writer.writerow([
        '没写意见', note['without_note_count'],
        '—' if note['fill_rate'] is None else round(100 - note['fill_rate'], 1),
        '被驳回又没写意见，提交人收到的是一个没有理由的驳回',
    ])
    for row in note['by_outcome']:
        writer.writerow([
            f"其中：{row['label']}写过意见", row['with_note_count'],
            '—' if row['fill_rate'] is None else row['fill_rate'],
            f"已{row['label']} {row['closed_count']} 条"
            + ('，不足样本下限只报数字'
               if row['closed_count'] < incident['min_bucket_sample'] else ''),
        ])
    writer.writerow([
        '两档填写率之差（百分点）',
        '—' if note['outcome_gap_points'] is None else note['outcome_gap_points'],
        '',
        '两侧都过样本门槛才允许把它读成一个信号，否则只报数字' if note['outcome_comparable']
        else '至少一侧不足样本下限，这个差不作结论',
    ])
    for row in note['bucket_rows']:
        writer.writerow([
            f"意见长度：{row['label']}", row['count'],
            '—' if row['share'] is None else row['share'],
            '长度只反映写没写细，不反映写得对不对',
        ])
    writer.writerow([])
    writer.writerow(['是谁在报谁', '异常数', '已完结', '驳回数', '驳回率（%）', '样本是否够'])
    for row in incident['direction']['rows']:
        writer.writerow([
            row['label'], row['total_count'], row['closed_count'], row['rejected_count'],
            '—' if row['rejection_rate'] is None else row['rejection_rate'],
            '这一档没有样本' if row['is_empty'] else (
                f"不足 {incident['min_bucket_sample']} 条" if row['is_small_sample'] else '够'
            ),
        ])
    writer.writerow([
        '两侧驳回率之差（百分点）',
        '—' if incident['direction']['gap_points'] is None else incident['direction']['gap_points'],
        '', '',
        '两侧都过样本门槛才允许把它读成一个信号，且必须同时报两侧总量' if incident['direction']['comparable']
        else '至少一侧不足样本下限，这个差不作结论',
    ])
    writer.writerow([])
    writer.writerow(['提交人分布', '提交人数', '占比（%）', '口径说明'])
    reporters = incident['reporters']
    if reporters['is_empty']:
        writer.writerow(['当前周期没有提交人，分布形状需要先有异常发生。'])
    else:
        writer.writerow([
            '提交人数', reporters['reporter_count'], '', '按人去重后的数量',
        ])
        for row in reporters['band_rows']:
            writer.writerow([
                f"提交次数：{row['label']}", row['count'],
                '—' if row['share'] is None else row['share'],
                '形状比名次有用：绝大多数人只报过一次，说明这条通道是应急性质的',
            ])
        writer.writerow([
            '只报过一次的人占比（%）',
            '—' if reporters['once_only_rate'] is None else reporters['once_only_rate'], '',
            '有一批人反复在用，说明它是某些用户的常规手段',
        ])
        writer.writerow([
            '人均提交次数',
            '—' if reporters['avg_count'] is None else reporters['avg_count'], '',
            '受 OneToOneField(appointment) 限制，不报「同一场约会里双方各报一次」——那个数恒为 0',
        ])
    writer.writerow([
        '说明',
        '交付预约异常是站内唯一一条直指线下人身安全的申诉，reason 与 status 一直被读写，'
        '但从来没有按类型与结局拆开看过：整体驳回率一旦波动，第一反应是去查处理人，'
        '而不是去查是哪一类异常在涨。本模块只报告：'
        '不加迁移、不改 MeetingIncident 的任何字段、不改写入路径、不自动驳回或自动关单；'
        '不读 detail 与 resolution_note 的自由文本，只读长度与空否；'
        '空分母报「—」不报 0——0 会被读成「发了，占比很小」，而事实是「没有样本」；'
        '不排名、不报任何人的用户名，那不是看板，是公开处刑清单；'
        '不把驳回率和处理人挂钩——按 reviewer 拆驳回率是一份考官成绩单。',
    ])
    notice = dashboard['moderation_notice_preference']
    writer.writerow([])
    writer.writerow(['内容审核结果通知：开关、触达与沉默', '数值', '说明'])
    writer.writerow([
        '口径与门槛', notice['summary'],
        '只读 ModerationEvent 的复核结论、NotificationPreference 的开关值和 '
        'moderation_update 通知的已读标志，不读 content 与通知正文的自由文本，'
        '也不改写任何一处写入路径',
    ])
    writer.writerow([
        '周期内复核完', notice['outcome']['reviewed_count'],
        '只收 confirmed 与 dismissed，pending 是右删失样本',
    ])
    writer.writerow([
        '送到了作者眼前', notice['outcome']['delivered_count'],
        f"占 {notice['outcome']['delivered_rate']}%"
        if notice['outcome']['delivered_rate'] is not None else '空分母',
    ])
    writer.writerow([
        '作者关掉了这一类', notice['outcome']['switch_off_count'],
        'create_notification 在开关处静默返回，连通知行都不建',
    ])
    writer.writerow([
        '作者已注销', notice['outcome']['missing_author_count'],
        'author 是 SET_NULL，没有作者的复核结论谈不上送到谁眼前',
    ])
    if notice['has_data']:
        writer.writerow([])
        writer.writerow(['这条结论送到作者眼前了没有', '记录数', '送到了', '开关关着', '免打扰时段', '复核人是作者本人', '取不到偏好行'])
        for row in notice['reach_rows']:
            writer.writerow([
                row['label'], row['total_count'], row['delivered'],
                row['switch_off'], row['quiet_hours'],
                row['self_authored'], row['no_preference_row'],
            ])
        writer.writerow([
            '说明',
            '四种没送到的成因分开数，不合成一个未送达率：合成之后，这个数会随「复核人'
            '是不是内容作者」这种人极少的情况抖动，而那和用户有没有关开关毫无关系。',
        ])
        writer.writerow([])
        writer.writerow(['通知发出去之后，读没读', '条数', '已读', '未读', '已读率（%）'])
        for row in notice['read_rows']:
            writer.writerow([
                row['label'], row['sent_count'], row['read_count'],
                row['unread_count'],
                '—' if row['read_rate'] is None else row['read_rate'],
            ])
        baseline = notice['baseline_read_rate']
        writer.writerow([
            '同期全部通知', baseline['sent_count'], baseline['read_count'],
            baseline['sent_count'] - baseline['read_count'],
            '—' if baseline['read_rate'] is None else baseline['read_rate'],
        ])
        writer.writerow([
            '说明',
            '已读率按复核结论拆：确认违规和误判放行是两条情绪上完全不同的消息，'
            '它们的已读率没有理由相同。最后一行是同期全部通知的整体已读率，'
            '作对照分母——少了它，单看一类的已读率读不出「这一类是不是特别没人看」。',
        ])
    else:
        writer.writerow(['当前周期没有复核完的记录，触达与已读都无从读起。'])
    grid = notice['preference_grid']
    writer.writerow([])
    writer.writerow(['偏好开关四格', '人数', '说明'])
    writer.writerow(['有内容被复核 · 开关开着', grid['reviewed_switch_on'], ''])
    writer.writerow(['有内容被复核 · 开关关着', grid['reviewed_switch_off'], ''])
    writer.writerow(['没内容被复核 · 开关开着', grid['unreviewed_switch_on'], ''])
    writer.writerow(['没内容被复核 · 开关关着', grid['unreviewed_switch_off'], ''])
    writer.writerow([
        '说明',
        '只报四格的人数，不报用户名、不排名，也不报任何两格之间的比值。'
        '后两格的人没被复核过内容，他们关不关这个开关和「收到过审核结果」没有因果关系——'
        '直接拿第二格比第四格，比出来的是两群不同的人，不是同一群人的两种选择。',
    ])
    if notice['activity']['has_data']:
        writer.writerow([])
        writer.writerow(['关掉之后，还在不在社区里说话', '关掉的一组', '开着的一组', '方向'])
        for row in notice['activity']['rows']:
            writer.writerow([
                row['label'],
                f"{row['off_count']} / {row['off_total']}",
                f"{row['on_count']} / {row['on_total']}",
                {
                    'switch_off_higher': '关掉的一组更高',
                    'switch_on_higher': '开着的一组更高',
                    'same': '两组相同',
                }.get(row['direction'], '样本不足，只报人数'),
            ])
        writer.writerow([
            '说明',
            '只报同期人群的对照形状，不作因果：关掉通知不会让人不说话，'
            '收到一条「你违规了」也不会，两群人在发言率上的差可能只是'
            '「常发言的人更在意审核结果」。方向只给三档，不给百分比差——'
            '一个 3 个百分点和一个 30 个百分点在看板上长一样，给的指引却完全相反。'
            '两群人都过 '
            f"{notice['min_preference_users']} 人才给方向。",
        ])
    else:
        writer.writerow(['当前周期关掉与开着两组人里有一组是空的，发言对照无从读起。'])
    attendance = dashboard['meeting_attendance']
    writer.writerow([])
    writer.writerow(['约好的那一次见面', '数值', '说明'])
    writer.writerow([
        '口径与门槛', attendance['summary'],
        '只读 responded_at 与到场登记，不改写任何字段，也不自动判定爽约',
    ])
    writer.writerow([
        '已过约定结束时间的确认预约', attendance['attendance_stats']['confirmed_ended_count'],
        '还没到结束时间的约会谈不到「有没有人到」，单独数出来，不计入到场率分母',
    ])
    writer.writerow([
        '还没到核对时间', attendance['attendance_stats']['still_open_count'],
        '约定结束时间尚未到来的预约',
    ])

    writer.writerow([])
    writer.writerow(['响应与确认', '次数', '占比（%）', '口径说明'])
    response_stats = attendance['response_stats']
    writer.writerow([
        '交付安排', response_stats['total'], '',
        '周期内创建的交付时间安排',
    ])
    writer.writerow([
        '得到回应', response_stats['responded_count'], response_stats['response_rate'],
        '回应时间以 responded_at 为准，不用 updated_at——它也会在重新提议时被写',
    ])
    writer.writerow([
        '仍未回应', response_stats['pending_count'],
        response_stats['response_rate'] and round(100 - response_stats['response_rate'], 1) or 0,
        '周期结束时仍在等待对方回应的安排',
    ])
    writer.writerow([
        '回应后确认', response_stats['accepted_count'], response_stats['accept_rate'],
        '以已回应的安排为分母',
    ])
    writer.writerow([
        '回应后未接受', response_stats['declined_count'], '',
        '对方看过这个时间，但选择重新协商',
    ])

    writer.writerow([])
    writer.writerow(['回应耗时', '安排数', '占比（%）'])
    for row in response_stats['bucket_rows']:
        writer.writerow([row['label'], row['count'], row['share']])
    writer.writerow([
        '中位耗时', response_stats['median_label'], '',
    ])
    writer.writerow([
        'P90 耗时', response_stats['p90_label'], '',
    ])
    writer.writerow([
        '最快 / 最慢', f"{response_stats['fastest_label']} / {response_stats['slowest_label']}", '',
    ])

    writer.writerow([])
    writer.writerow(['到场情况', '次数', '占已结束确认预约（%）', '口径说明'])
    attendance_stats = attendance['attendance_stats']
    writer.writerow([
        '买家到场', attendance_stats['buyer_arrived_count'],
        attendance_stats['buyer_arrival_rate'],
        '以已过约定结束时间的确认预约为分母',
    ])
    writer.writerow([
        '卖家到场', attendance_stats['seller_arrived_count'],
        attendance_stats['seller_arrival_rate'],
        '同上',
    ])
    writer.writerow([
        '双方都到场', attendance_stats['both_arrived_count'],
        attendance_stats['both_arrival_rate'],
        '这一行才是「约好了也见着了」',
    ])
    writer.writerow([
        '只有一方到场', attendance_stats['one_sided_count'],
        attendance_stats['one_sided_rate'],
        '另一方在签到窗口内没有登记',
    ])
    writer.writerow([
        '双方都没到场', attendance_stats['neither_arrived_count'], '',
        '不等于爽约：爽约只能由用户提交异常，系统不自动判定',
    ])

    writer.writerow([])
    writer.writerow(['到场时间相对约定开始', '角色', '签到人数', '提前到', '迟到 15 分钟以上', '平均偏移', '最晚一次'])
    for row in attendance_stats['lateness_rows']:
        writer.writerow([
            '迟到分档', row['role'], row['arrived_count'],
            row['early_count'], row['late_count'],
            row['average_offset_label'], row['worst_offset_label'],
        ])
    for row in attendance_stats['lateness_rows']:
        for bucket in row['bucket_rows']:
            writer.writerow([
                bucket['label'], row['role'], bucket['count'], bucket['share'], '', '', '',
            ])

    writer.writerow([])
    writer.writerow(['「对方未到场」与到场登记的对照', '条数', '占该类异常（%）', '口径说明'])
    crosscheck = attendance['incident_crosscheck']
    writer.writerow([
        '该类异常合计', crosscheck['total'], '',
        '只对照「对方未到场」，安全与其他类型不参与到场对照',
    ])
    writer.writerow([
        '被指控方有到场登记', crosscheck['contradicted_count'], crosscheck['contradicted_rate'],
        '不是判定谁说谎，而是该被运营看见的矛盾',
    ])
    writer.writerow([
        '提交人到场、对方未登记', crosscheck['supported_count'], '',
        '到场记录支持提交人的说法',
    ])
    writer.writerow([
        '双方都没有到场登记', crosscheck['no_record_count'], '',
        '没有记录可依，仍按既有流程核实',
    ])
    writer.writerow([
        '其他类型异常', crosscheck['other_reason_count'], '',
        '与到没到无关，不参与到场对照',
    ])
    if crosscheck['contradicted_rows']:
        writer.writerow([
            '待核实的矛盾', '商品', '提交人', '被指控方',
            '被指控方到场登记', '提交人到场登记', '约定开始', '处理状态',
        ])
        for row in crosscheck['contradicted_rows']:
            writer.writerow([
                row['verdict_label'], row['item_title'], row['reporter'], row['accused'],
                row['accused_arrived_label'], row['reporter_arrived_label'],
                timezone.localtime(row['start_at']).strftime('%Y-%m-%d %H:%M'),
                row['status_label'],
            ])
    else:
        writer.writerow(['—', '当前没有「被指控方有到场登记」的未到场异常', '', '', '', '', '', ''])

    writer.writerow([])
    writer.writerow(['未接受原因的取值', '次数'])
    decline_stats = attendance['decline_stats']
    for row in decline_stats['reason_rows']:
        writer.writerow([row['reason'], row['count']])
    if not decline_stats['reason_rows']:
        writer.writerow(['—', '当前周期没有被对方未接受的交付安排'])

    writer.writerow([])
    writer.writerow([
        '说明',
        '未接受原因这一列只有一个取值，因为这个字段唯一的写入路径是视图里的一句硬编码，'
        '用户从来没有机会填它。如实报告它是个常量，比画一张 100% 的分布图要诚实。'
        '这里只报告，不改签到窗口、不自动判定爽约、不新增提醒，也不把统计写回任何记录。',
    ])

    writer.writerow([])
    code = dashboard['handoff_code_usage']
    writer.writerow([])
    writer.writerow(['在现场念出来的那六位数字', '数值', '说明'])
    writer.writerow([
        '口径与门槛', code['summary'],
        '只读确认码五个字段与站内通知，不改写任何字段，也不自动判定爽约',
    ])
    writer.writerow([
        '撞到几次锁定视为作废', code['lock_threshold'], '',
    ])

    writer.writerow([])
    writer.writerow(['生成与使用', '笔数', '占走完当面交付订单（%）', '口径说明'])
    adoption = code['adoption_stats']
    writer.writerow([
        '走完当面交付的订单', adoption['total'], '',
        '分母只收已经离开本阶段的订单；还挂在本阶段的是右删失样本，单独数不计入分母',
    ])
    writer.writerow([
        '生成过确认码', adoption['issued_count'], adoption['issue_rate'],
        '只看 handoff_code_hash，生成过就有哈希',
    ])
    writer.writerow([
        '在现场被核对使用', adoption['used_count'], adoption['use_rate'],
        '以生成过确认码的订单为分母',
    ])
    writer.writerow([
        '生成后没有核对记录', adoption['unused_count'], '',
        '码发下去了，现场没走完；它和上一行加起来才等于「生成过」',
    ])
    writer.writerow([
        '重新生成过（下界）', adoption['regenerated_count'], adoption['regenerated_rate'],
        '从站内通知反推；卖家关掉订单状态通知后生成不再留痕，所以这是下界不是准数',
    ])

    writer.writerow([])
    writer.writerow(['生成之后，现场发生了什么', '情况', '笔数', '占生成过（%）', '这一档说明什么'])
    attempt = code['attempt_stats']
    writer.writerow([
        '生成过确认码的订单', '', attempt['issued_total'], '',
        '没生成过码的订单不属于这一列的分母',
    ])
    for row in attempt['bucket_rows']:
        writer.writerow([
            '', row['label'], row['count'], row['share'], row['hint'],
        ])
    writer.writerow([
        '一次就对', '', attempt['first_try_count'], attempt['first_try_rate'],
        '注意：这一列描述的是最后一枚码，重新生成会把错误次数归零',
    ])
    if attempt['is_small_sample']:
        writer.writerow([
            '样本不足', '', attempt['issued_total'], '',
            f"不足 {attempt['min_measured_orders']} 笔，占比只作参考",
        ])

    writer.writerow([])
    writer.writerow(['生成到现场核对隔了多久', '间隔', '笔数', '占比（%）', '口径说明'])
    gap_stats = code['issue_gap_stats']
    writer.writerow([
        '可测量的订单', '', gap_stats['measured_count'], '',
        '只收既生成了码、又留下核对记录的订单',
    ])
    for row in gap_stats['bucket_rows']:
        writer.writerow(['', row['label'], row['count'], row['share'], ''])
    writer.writerow(['中位间隔', '', gap_stats['median_label'], '', ''])
    writer.writerow(['P90 间隔', '', gap_stats['p90_label'], '', ''])
    writer.writerow([
        '最快 / 最慢', '', f"{gap_stats['fastest_label']} / {gap_stats['slowest_label']}", '', '',
    ])

    writer.writerow([])
    writer.writerow([
        '重新生成与间隔汇总', '中位间隔', 'P90 间隔', '重新生成过（下界）',
        '可测量订单', '口径说明',
    ])
    writer.writerow([
        '生成到核对的全貌', gap_stats['median_label'], gap_stats['p90_label'],
        adoption['regenerated_count'], gap_stats['measured_count'],
        '重新生成从站内通知反推，卖家关掉订单状态通知后生成不再留痕，所以这是下界',
    ])

    writer.writerow([])
    writer.writerow([
        '用过确认码的订单更可能做成吗', '分组', '笔数', '做成数', '完成率（%）', '口径说明',
    ])
    contrast = code['outcome_contrast']
    writer.writerow([
        '用过确认码', '', contrast['used_count'], contrast['used_success_count'],
        contrast['used_success_rate'], '取消的两组都留着：取消不是失败',
    ])
    writer.writerow([
        '生成过但没用', '', contrast['unused_count'], contrast['unused_success_count'],
        contrast['unused_success_rate'], '同上',
    ])
    if contrast['gap_points'] is not None:
        writer.writerow([
            '差值（百分点）', '', contrast['gap_points'], '', '',
            '这是对照，不是结论：愿意生成码的双方可能本来就是更认真的那批人',
        ])
    if contrast['has_direction']:
        writer.writerow([
            '方向', '', '可用', '', '', '两组各自过门槛且差值超过噪声带',
        ])
    else:
        writer.writerow([
            '方向', '', '不给', '', '',
            f"两组各自不足 {contrast['min_measured_orders']} 笔，或差值落在 {HANDOFF_NOISE_POINTS} 个百分点噪声带内",
        ])

    writer.writerow([])
    writer.writerow([
        '撞到锁定的订单', '商品', '买家', '卖家', '错误次数', '重新生成过',
        '生成次数', '最终状态', '口径说明',
    ])
    if code['locked_rows']:
        for row in code['locked_rows']:
            writer.writerow([
                row['item_title'], row['buyer'], row['seller'], row['attempts'],
                '是' if row['regenerated'] else '否', row['generation_count'],
                row['status_label'], '',
            ])
    else:
        writer.writerow([
            '—', '当前周期没有撞到锁定的订单', '', '', '', '', '', '',
        ])

    writer.writerow([])
    writer.writerow([
        '说明',
        '错误次数记的是最后一枚码的错误次数，不是这笔订单累计的错误次数——重新生成会把它归零。'
        '所以「一次就对」必须和「重新生成过」合起来读，单看任何一列都会得出一个过于乐观的结论。'
        '重新生成从站内通知反推，卖家关掉订单状态通知后生成不再留痕，因此报的是下界。'
        '完成率对照只做对照、不给因果。这里只报告，不自动派单、不改确认码、不新增提醒，'
        '也不把统计写回任何记录。',
    ])

    writer.writerow([])
    handshake = dashboard['return_handshake']
    writer.writerow(['归还登记的那两次握手', '数值', '说明'])
    writer.writerow([
        '口径与门槛', handshake['summary'],
        '只读 buyer_returned_at 与 seller_returned_at，不改写任何字段，也不自动推进任何一笔订单',
    ])
    writer.writerow([
        '还挂在借用中的订单', handshake['still_borrowed_count'], '',
        '握手还没结束，是右删失样本，不计入完成率分母',
    ])
    writer.writerow([
        '从没进入借用就被取消', handshake['cancelled_before_count'], '',
        '握手压根没开始，不计入任何分母',
    ])
    writer.writerow([
        '状态是已归还却查不到进入借用中的事件', handshake['no_borrow_count'], '',
        '这不该发生；静默忽略它会让分母悄悄变小，所以单独数出来',
    ])

    writer.writerow([])
    writer.writerow(['走完借用的订单', '笔数', '占比（%）', '口径说明'])
    completion = handshake['completion_stats']
    writer.writerow([
        '周期内走完借用的订单', completion['concluded_total'], '',
        '分母只收已经离开借用中的订单；取消的借用订单留着，一次东西已经拿走、'
        '最后走取消的订单仍然发生过握手',
    ])
    writer.writerow([
        '双方都登记了归还', completion['returned_count'], completion['completion_rate'],
        '两个时间戳都写上，订单才会走到已归还',
    ])
    writer.writerow([
        '带着真实借用记录被取消', completion['cancelled_count'], '',
        '它和上一行加起来才等于走完借用的订单数',
    ])
    writer.writerow([
        '两次握手都点了的订单', completion['both_registered_count'], '',
        '它和归还数应当相等；不相等说明有订单两次握手都点了却没有走到已归还，'
        '那是数据里的一处矛盾',
    ])
    if completion['is_small_sample']:
        writer.writerow([
            '样本不足', '', '',
            f"不足 {completion['min_measured_orders']} 笔，占比只作参考",
        ])

    writer.writerow([])
    writer.writerow(['谁先登记', '笔数', '占登记过（%）', '这一档说明什么'])
    first_registered = handshake['first_registered_stats']
    writer.writerow([
        '登记过归还的订单', '', first_registered['measured_total'], '',
        '双方都没登记的订单不进这一列',
    ])
    for row in first_registered['rows']:
        writer.writerow([
            '', row['label'], row['count'], row['share'],
            '借用方先登记是正常路径；发布者先登记通常说明双方对「归还」的定义不一致',
        ])
    writer.writerow([
        '双方都还没登记', '', first_registered['unregistered_count'], '',
        '借用已经开始，但两个时间戳都还空着',
    ])

    writer.writerow([])
    writer.writerow(['双方都登记的等待时长', '笔数', '占比（%）', '口径说明'])
    wait_stats = handshake['wait_stats']
    writer.writerow([
        '可测量的订单', '', wait_stats['measured_count'], '',
        '只收双方都登记了的订单；一方登记另一方没登记的没有「等待多久」可言',
    ])
    for row in wait_stats['bucket_rows']:
        writer.writerow(['', row['label'], row['count'], row['share'], row['hint']])
    writer.writerow([
        '中位 / P90 等待', '',
        f"{wait_stats['median_label']} / {wait_stats['p90_label']}", '', '',
        '按天分档不按小时：一次归还等待的时间尺度是天',
    ])
    writer.writerow([
        '最快 / 最慢', '', f"{wait_stats['fastest_label']} / {wait_stats['slowest_label']}",
        '', '',
    ])
    if wait_stats['is_small_sample']:
        writer.writerow([
            '样本不足', '', '', '',
            f"不足 {wait_stats['min_measured_orders']} 笔，占比只作参考",
        ])

    writer.writerow([])
    writer.writerow(['仍挂在借用中的订单', '笔数', '口径说明'])
    stalled_stats = handshake['stalled_stats']
    writer.writerow([
        '逾期未归还', stalled_stats['overdue_count'], '',
        '已过预计归还时间而双方都没登记——催收阶梯该管的那一类，本模块只报数不改阶梯',
    ])
    writer.writerow([
        '已登记未确认', stalled_stats['registered_count'], '',
        '一方已登记、另一方没有——东西可能已经回来了，该做的是提醒另一方点一下',
    ])
    writer.writerow([
        '等了超过一周', stalled_stats['over_week_count'], '',
        '从先登记那一刻算到现在，按订单的一生累计，不按统计周期切',
    ])
    writer.writerow([
        '等了超过两周', stalled_stats['over_two_weeks_count'], '',
        '长尾：等了两周以上，商品和押金都还卡在这一笔上',
    ])
    if stalled_stats['is_small_sample']:
        writer.writerow([
            '样本不足', '', '',
            f"不足 {stalled_stats['min_measured_orders']} 笔，占比只作参考",
        ])

    writer.writerow([])
    writer.writerow(['已登记未确认的等待时长', '笔数', '占比（%）', '口径说明'])
    for row in stalled_stats['bucket_rows']:
        writer.writerow(['', row['label'], row['count'], row['share'], row['hint']])
    writer.writerow([
        '中位 / 最久', '',
        f"{stalled_stats['median_label']} / {stalled_stats['slowest_label']}", '', '',
        '这一列没有上界，所以报分布而不是平均值：一个等了四十天的订单会被平均值藏起来',
    ])

    writer.writerow([])
    writer.writerow([
        '已登记未确认的订单', '商品', '买家', '卖家', '先登记的一方', '在等谁',
        '已等多久', '预计归还', '登记之后还有没有动静', '口径说明',
    ])
    if handshake['stalled_rows']:
        for row in handshake['stalled_rows']:
            writer.writerow([
                row['item_title'], row['buyer'], row['seller'], row['first_label'],
                row['waiting_for'], row['waiting_label'], row['due_label'],
                row['last_event_label'],
                '报「登记之后没有新事件」而不是报「卡住」：前者是可以核实的事实，'
                '后者是一个判断' if not row['moved_after_register'] else '',
            ])
    else:
        writer.writerow([
            '—', '当前周期没有一方已登记、另一方未确认的借用订单',
            '', '', '', '', '', '', '', '',
        ])
    if handshake['stalled_rows'] and len(handshake['stalled_rows']) > handshake['stalled_row_limit']:
        writer.writerow([
            '—', f"清单最多列 {handshake['stalled_row_limit']} 条，完整的在数据库里",
            '', '', '', '', '', '', '', '',
        ])

    writer.writerow([])
    writer.writerow([
        '从没进入过借用就被取消的订单', '商品', '买家', '卖家', '当前状态', '口径说明',
    ])
    if handshake['cancelled_rows']:
        for row in handshake['cancelled_rows']:
            writer.writerow([
                row['item_title'], row['buyer'], row['seller'], row['status_label'],
                '握手压根没开始，所以不进任何分母',
            ])
    else:
        writer.writerow([
            '—', '当前周期没有从没进入过借用就被取消的订单', '', '', '', '',
        ])

    writer.writerow([])
    writer.writerow([
        '说明',
        '分母只收「周期内创建、且已经离开借用中」的借用订单。已经离开的意思是走到了已归还，'
        '或者带着一次真实的借用记录被取消；从来没进入过借用就被取消的订单，握手压根没开始，'
        '不算进来。还挂在借用中的订单是右删失样本——握手还没有结束，根本谈不上有没有完成——'
        '把它算进分母会让完成率凭空变小，这部分单独数出来，不丢也不混。'
        '等待时长一律按天分档，不按小时：一次归还等待的时间尺度是天。'
        '「已登记未确认」的等待从先登记那一刻算到现在，而「现在」不属于任何统计周期，'
        '所以它按订单的一生累计，不按周期切——按周期切会把一个跨越周期边界的等待算成两段短的，'
        '而那正是最不该被切开的样本。'
        '「逾期未归还」和「已登记未确认」必须分开报：这两类都表现为「订单还没结束」，'
        '但运营动作完全相反——前者是催收阶梯该管的事，后者是「去提醒另一方点一下」的事。'
        '「谁先登记」单独一档，因为它和「等多久」不是一回事：借用方先登记是正常路径，'
        '发布者先登记意味着他先收到了东西、而借用方还没点，这个顺序在借用的语境里是反常的。'
        '本模块只报告，不改 confirm_return 的任何逻辑、不加超时、不自动推进任何一笔订单、'
        '不改催收阶梯的触发条件、不改 close_escalation 的调用点、不新增提醒类型，'
        '也不把统计写回任何记录。',
    ])

    writer.writerow([])
    delivery_handshake = dashboard['delivery_handshake']
    writer.writerow(['当面交付的那两下确认', '数值', '说明'])
    writer.writerow([
        '口径与门槛', delivery_handshake['summary'],
        '只读 buyer_confirmed_at 与 seller_confirmed_at，不改写任何字段，'
        '也不自动推进任何一笔订单',
    ])
    writer.writerow([
        '还挂在当面交付的订单', delivery_handshake['pending_in_window'], '',
        '握手还没结束，是右删失样本，不计入分母；按订单的一生累计，不按统计周期切',
    ])
    writer.writerow([
        '全站还挂在当面交付的订单', delivery_handshake['pending_count'], '',
        '同一口径，不按周期切：一笔跨越周期边界的等待不该被算成两段短的',
    ])

    writer.writerow([])
    writer.writerow(['两下是什么顺序，中间隔了多久'])
    writer.writerow(['顺序', '笔数', '占比（%）', '中位间隔', '口径说明'])
    for row in delivery_handshake['sequence_rows']:
        writer.writerow([
            row['label'], row['count'], row['share'], row['median_gap_label'],
            '不足样本门槛，只报数字不给方向' if row['is_small_sample'] else '',
        ])
    writer.writerow([
        '合计', sum(row['count'] for row in delivery_handshake['sequence_rows']), '', '',
        '没有 DeliveryConfirmation 行的订单不进任何一档，但它仍在分母里',
    ])

    writer.writerow([])
    writer.writerow(['两下之间隔了多久'])
    writer.writerow(['间隔', '笔数', '占比（%）', '口径说明'])
    for row in delivery_handshake['gap_bucket_rows']:
        writer.writerow(['', row['label'], row['count'], row['share'], ''])

    writer.writerow([])
    writer.writerow(['只点了一半的订单'])
    writer.writerow(['情况', '笔数', '占比（%）', '口径说明'])
    hang_stats = delivery_handshake['hang_stats']
    writer.writerow([
        '只有一方点过的订单', hang_stats['total'], '', '',
    ])
    writer.writerow([
        '买家点了、卖家没点', hang_stats['buyer_only'], hang_stats['buyer_only_share'], '',
        '东西可能已经换手了，而订单还停在待当面交付',
    ])
    writer.writerow([
        '卖家点了、买家没点', hang_stats['seller_only'], hang_stats['seller_only_share'], '',
        '发布者说他给了东西，而买家还没确认——这不是安全问题，但它是一个事实',
    ])
    writer.writerow([
        '中位挂起', '', hang_stats['median_hang_label'], '',
        '从先点那一刻算到现在，不按统计周期切',
    ])

    writer.writerow([])
    writer.writerow(['挂了多久', '笔数', '占比（%）', '这一档说明什么'])
    for row in hang_stats['bucket_rows']:
        writer.writerow(['', row['label'], row['count'], row['share'], row['note']])

    writer.writerow([])
    writer.writerow(['间隔与结局的对照'])
    writer.writerow(['两下间隔', '笔数', '做成', '做成率（%）', '口径说明'])
    contrast = delivery_handshake['outcome_contrast']
    writer.writerow([
        '两下间隔在 1 小时内', contrast['fast_total'], contrast['fast_success'],
        contrast['fast_rate'], '',
    ])
    writer.writerow([
        '两下间隔超过 1 小时', contrast['slow_total'], contrast['slow_success'],
        contrast['slow_rate'], '',
    ])
    writer.writerow([
        '差值（百分点）', '', '',
        '—' if contrast['gap_points'] is None else contrast['gap_points'],
        '两组各自不足样本下限，只报数字、不给方向' if not contrast['is_measurable'] else '',
    ])

    writer.writerow([])
    writer.writerow(['只点了一半的订单清单'])
    writer.writerow(['订单号', '商品', '先点的一方', '挂了多久', '口径说明'])
    if delivery_handshake['stalled_rows']:
        for row in delivery_handshake['stalled_rows']:
            writer.writerow([
                row['order_id'], row['item_title'], row['first_side_label'],
                row['hang_label'], '',
            ])
    else:
        writer.writerow(['—', '当前周期没有只点了一半的交付订单', '', '', '', ''])
    if delivery_handshake['stalled_rows'] and len(delivery_handshake['stalled_rows']) > delivery_handshake['stalled_row_limit']:
        writer.writerow([
            '—', f"清单最多列 {delivery_handshake['stalled_row_limit']} 条，完整的在数据库里",
            '', '', '', '',
        ])

    writer.writerow([])
    writer.writerow([
        '说明',
        '分母只收「已经离开待当面交付」的订单：走到了已完成、借用中、已归还，'
        '或者带着一次真实的交付确认被取消；从没进入过当面交付就被取消的订单，'
        '两下压根没开始，不算进来。还挂在本阶段的订单是右删失样本——握手还没有结束，'
        '根本谈不上有没有点完——把它算进分母会让「只点一半」的比例凭空变浑，'
        '这部分单独数出来，不丢也不混。'
        '「双方同一秒」单独一档：它既不是买家先也不是卖家先，硬并进任何一边都会让顺序这一列失真。'
        '挂起时长从先点那一刻算到现在，不按统计周期切：一笔跨越周期边界的等待，'
        '按周期切开会被算成两段短的，而那正是最不该被切开的样本。'
        '间隔与结局只做对照、不给因果：两下隔得近的订单更可能完成，'
        '很可能只是因为双方本来就都更认真，平台没有能力把这两件事分开。'
        '本模块只报告，不改 confirm_delivery 的任何逻辑、不自动补点、不新增提醒类型、'
        '不改确认码的生成与核对，也不把统计写回任何记录。',
    ])

    writer.writerow([])
    safety_quality = dashboard['transaction_safety_acknowledgement_quality']
    writer.writerow(['交易安全卡确认到哪一步了', '数值', '说明'])
    writer.writerow([
        '口径与门槛', safety_quality['summary'],
        '主分母按订单创建时间切窗口，只收仍可确认的 pending / confirmed / meeting 订单；'
        '当前指纹在服务端重算，历史确认不冒充当前确认',
    ])
    safety_coverage = safety_quality['coverage']
    writer.writerow([
        '本期仍可确认订单', safety_coverage['total'], '',
    ])
    writer.writerow([
        '买家当前确认率（%）', safety_coverage['buyer_rate'],
        '分母是本期仍可确认订单数',
    ])
    writer.writerow([
        '卖家当前确认率（%）', safety_coverage['seller_rate'], '',
    ])
    writer.writerow([
        '双方当前确认率（%）', safety_coverage['both_rate'], '',
    ])

    writer.writerow([])
    writer.writerow(['当前确认覆盖', '笔数', '占比（%）'])
    for row in safety_coverage['rows']:
        writer.writerow([row['label'], row['count'], row['share']])

    writer.writerow([])
    writer.writerow(['安全卡确认时延', '笔数', '占比（%）', '口径说明'])
    writer.writerow([
        '双方已确认当前版本', safety_quality['latency']['completed_count'], '',
        '只有完整链路进入时延分布',
    ])
    writer.writerow([
        '仍缺至少一侧确认', safety_quality['latency']['incomplete_count'], '',
        '右删失样本，只报规模',
    ])
    for label, rows in (
        ('第一位确认', safety_quality['latency']['first_bucket_rows']),
        ('双方补齐', safety_quality['latency']['complete_bucket_rows']),
    ):
        for row in rows:
            writer.writerow([f'{label}：{row["label"]}', row['count'], row['share'], row['note']])

    writer.writerow([])
    writer.writerow(['曾被确认过的安全卡指纹数', '笔数', '占比（%）', '口径说明'])
    safety_versions = safety_quality['versions']
    for row in safety_versions['rows']:
        writer.writerow([row['label'], row['count'], row['share'], '观察下界：没被确认过的版本不留记录'])
    writer.writerow([
        '当前版本之前曾有确认', safety_versions['current_pending_after_prior_count'], '',
        '说明信号变化后当前版本仍待确认；不推断变化次数',
    ])

    writer.writerow([])
    writer.writerow(['全站待确认清单', '订单号', '商品', '状态', '缺少的角色', '订单年龄'])
    safety_pending = safety_quality['pending']
    if safety_pending['rows']:
        for row in safety_pending['rows']:
            writer.writerow([
                '', row['order_id'], row['item_title'], row['status_label'],
                row['missing_roles_label'], row['age_label'],
            ])
    else:
        writer.writerow(['当前没有仍处于可确认阶段的订单', '', '', '', '', ''])
    if safety_pending['is_truncated']:
        writer.writerow([
            f"清单最多列 {safety_pending['row_limit']} 条，完整的在数据库里",
            '', '', '', f"总计 {safety_pending['total']} 笔", '',
        ])

    writer.writerow([])
    deadline_reminders = dashboard['order_deadline_reminders']
    writer.writerow(['承诺的截止时间，和催过之后', '数值', '说明'])
    writer.writerow([
        '口径与门槛', deadline_reminders['summary'],
        f"统计周期 {deadline_reminders['period_days']} 天；"
        f"默认在截止前 {deadline_reminders['reminder_hours_default']} 小时提醒一次、"
        f"下单后 {deadline_reminders['confirmation_timeout_hours']} 小时未确认自动释放；"
        '两个数字都从写入路径读，看板不另设一套',
    ])
    writer.writerow([
        '周期内确认了卖家的订单', deadline_reminders['confirm_stats']['total'], '',
        '确认时刻只认 OrderEvent 里 to_status 为 confirmed 的最早一条，'
        '不用 updated_at——那是 auto_now，每一次字段变动都会跟着走',
    ])
    writer.writerow([
        '中位确认时长', deadline_reminders['confirm_stats']['median_label'], '',
        '从下单那一刻算到确认那一刻，按订单的一生累计，不按统计周期切',
    ])
    writer.writerow([
        '还挂在「待卖家确认」（周期内）', deadline_reminders['pending_in_window'], '',
        '确认还没发生，根本谈不上花了多久——右删失样本，不进任何分母',
    ])
    writer.writerow([
        '还挂在「待卖家确认」（全站）', deadline_reminders['pending_count'], '',
        '同一口径，不按周期切：一笔跨越周期边界的等待不该被算成两段短的',
    ])

    writer.writerow([])
    writer.writerow(['从下单到卖家确认花了多久'])
    writer.writerow(['确认时长', '笔数', '占比（%）', '口径说明'])
    for row in deadline_reminders['confirm_stats']['bucket_rows']:
        writer.writerow([
            '', row['label'], row['count'],
            '—' if row['share'] is None else row['share'], '',
        ])

    writer.writerow([])
    writer.writerow(['催过之后的四种结局', '笔数', '占比（%）', '这一档说明什么'])
    reminder_stats = deadline_reminders['reminder_stats']
    writer.writerow([
        '提醒之后确认了', reminder_stats['confirmed_after'],
        '—' if reminder_stats['confirmed_after_share'] is None else reminder_stats['confirmed_after_share'],
        '最有信息量的一列：确认距截止还剩多少',
    ])
    writer.writerow([
        '提醒之后仍被自动释放', reminder_stats['expired_after'],
        '—' if reminder_stats['expired_after_share'] is None else reminder_stats['expired_after_share'],
        '催了也没用——按事件备注里的「自动释放」认，不按状态猜',
    ])
    writer.writerow([
        '提醒之后用户主动取消', reminder_stats['cancelled_by_user'], '',
        '和承诺无关，单独数：取消是交易没继续，不是承诺没守住',
    ])
    writer.writerow([
        '确认时刻取不到', reminder_stats['unmeasurable'], '',
        '没有 confirmed 事件行，答不了催过之后多久确认——右删失样本，不进分档',
    ])
    writer.writerow([
        '合计（收到过确认提醒）', reminder_stats['total'], '',
        f"提醒时刻落在周期内才算；默认提前 {deadline_reminders['reminder_hours_default']} 小时发，发前判空、幂等",
    ])

    writer.writerow([])
    writer.writerow(['确认的时候，距截止还剩多少'])
    writer.writerow(['距截止还剩', '笔数', '占比（%）', '这一档说明什么'])
    for row in reminder_stats['margin_rows']:
        writer.writerow([
            '', row['label'], row['count'],
            '—' if row['share'] is None else row['share'], row['note'],
        ])
    writer.writerow([
        '', f"合计 {reminder_stats['confirmed_after']} 笔", '', '',
        f"不足 {deadline_reminders['min_measured_orders']} 笔只报数字、不给方向；"
        f"两档相差小于 {deadline_reminders['noise_points']} 个百分点视为噪声",
    ])

    writer.writerow([])
    writer.writerow(['借用侧：催过之后提前多久还'])
    writer.writerow(['距预计归还', '笔数', '占比（%）', '这一档说明什么'])
    borrow_stats = deadline_reminders['borrow_stats']
    writer.writerow([
        '归还提醒之后已归还', borrow_stats['returned'],
        '—' if borrow_stats['returned_share'] is None else borrow_stats['returned_share'],
        '距「预计归还」多久才真的归还',
    ])
    writer.writerow([
        '归还提醒之后被取消', borrow_stats['cancelled'], '',
        '它不是「没还」，它是「交易没有继续」',
    ])
    writer.writerow([
        '归还登记或预计归还时间缺一个', borrow_stats['unmeasurable'], '',
        '答不了「提前多久还」——右删失样本，不进分档',
    ])
    for row in borrow_stats['margin_rows']:
        writer.writerow([
            '', row['label'], row['count'],
            '—' if row['share'] is None else row['share'], row['note'],
        ])
    writer.writerow([
        '逾期通知发过仍未归还', deadline_reminders['overdue_still_borrowed'], '',
        '这是催收阶梯的领域，这里只报规模，不动阶梯',
    ])

    writer.writerow([])
    writer.writerow(['台账加成拆开', '笔数', '口径说明'])
    writer.writerow([
        '预约确认提醒', deadline_reminders['confirmation_reminded_in_window'],
        'process_order_timeouts 在截止前 6 小时发的那一批',
    ])
    writer.writerow([
        '借用归还提醒', deadline_reminders['borrow_reminded_in_window'],
        'process_borrow_due_notifications 在归还日前 6 小时发的那一批',
    ])
    writer.writerow([
        '两者之和',
        deadline_reminders['confirmation_reminded_in_window'] + deadline_reminders['borrow_reminded_in_window'],
        'TaskRun.metrics 里的 reminded 就是这个数——它把两侧加成了一起，'
        '所以调度负载涨了的时候，看板说不清是哪一侧在涨',
    ])

    writer.writerow([])
    writer.writerow([
        '催过之后仍被自动释放的订单', '订单号', '商品', '等足了多久', '催过',
    ])
    if deadline_reminders['release_rows']:
        for row in deadline_reminders['release_rows']:
            writer.writerow([
                '', row['order_id'], row['item_title'], row['waited_label'],
                '催过' if row['was_reminded'] else '没催过',
            ])
    else:
        writer.writerow(['', '—', '当前周期没有催过之后仍被自动释放的订单', '', ''])
    if len(deadline_reminders['release_rows']) > deadline_reminders['release_row_limit']:
        writer.writerow([
            '', '—', f"清单最多列 {deadline_reminders['release_row_limit']} 条，完整的在数据库里", '', '',
        ])

    writer.writerow([])
    writer.writerow(['已过截止时间，还挂在「待卖家确认」'])
    writer.writerow(['订单号', '商品', '已超时多久', '催过'])
    if deadline_reminders['pending_rows']:
        for row in deadline_reminders['pending_rows']:
            writer.writerow([
                '', row['order_id'], row['item_title'], row['overdue_label'],
                '催过' if row['was_reminded'] else '没催过',
            ])
    else:
        writer.writerow(['', '—', '当前周期没有已经超过截止时间还挂着的订单', '', ''])
    if len(deadline_reminders['pending_rows']) > deadline_reminders['pending_row_limit']:
        writer.writerow([
            '', '—', f"清单最多列 {deadline_reminders['pending_row_limit']} 条，完整的在数据库里", '', '',
        ])
    writer.writerow([
        '', '超时时长从截止时刻算到现在，不按统计周期切：「现在」不属于任何统计周期，'
        '一笔跨越边界的等待按周期切开会被算成两段短的', '', '',
    ])

    writer.writerow([])
    writer.writerow([
        '说明',
        '「催过之后」的分母是周期内真的收到过提醒、且已经离开「待卖家确认」的订单。'
        '四种结局分开报：提醒之后确认了、提醒之后仍被自动释放、提醒之后用户主动取消、'
        '确认时刻取不到。合成一个「已提醒 N 笔」会把最有效的提醒和最无效的提醒混成一个'
        '平均值——那正是运营最不该看的一个数。'
        '自动释放和用户主动取消都表现为 cancelled，靠 OrderEvent 备注里有没有「自动释放」'
        '区分：两者的运营动作完全相反，前者该查发布者是不是不上了，后者什么都不该做。'
        '确认时刻只认 OrderEvent 的 confirmed，不用 updated_at。'
        '借用侧单独取数：它有自己的状态终点（已归还）和自己的提醒字段，混在出售订单里会让'
        '两边的「催过之后」互相污染。'
        '本模块只报告，不改 process_order_timeouts 的触发条件、不调整 6 小时和 24 小时这两个'
        '默认值、不自动推进任何一笔订单、不改催收阶梯、不新增提醒类型，'
        '也不把统计写回任何记录。',
    ])
    writer.writerow([])
    reschedule = dashboard['meeting_reschedule']
    writer.writerow(['一次交付安排被改了几次才定下来', '数值', '说明'])
    writer.writerow([
        '口径与门槛', reschedule['summary'],
        '改期次数从 OrderEvent 里那句「更新了交付时间安排，等待对方确认」反推，'
        '报的是下界：MeetingAppointment 是 OneToOneField(order)，'
        '前几版被原地覆盖，没有版本表；OrderEvent 上线之前的改动没有痕迹',
    ])
    writer.writerow([
        '周期内排过交付时间的订单', reschedule['reschedule_stats']['total'], '',
        '分母只收已经有了一条安排的订单；连一次都没约上的订单谈不到改期，'
        '单独数出来，属于订单阶段流的领域',
    ])
    writer.writerow([
        '一次谈成', reschedule['reschedule_stats']['settled_once_count'],
        reschedule['reschedule_stats']['settled_once_rate'],
        '0 次改期。它是这一列的主档，不是补集——平台关心的恰恰是'
        '「有多少安排根本不用改」',
    ])
    writer.writerow([
        '改过一次以上', reschedule['reschedule_stats']['changed_count'],
        reschedule['reschedule_stats']['changed_rate'],
        '双方至少有一方改过一次主意',
    ])
    writer.writerow([
        '其中改过两次以上', reschedule['reschedule_stats']['changed_thrice_or_more_count'],
        reschedule['reschedule_stats']['changed_thrice_or_more_rate'],
        '最难谈的一批：一个时间对其中一方一直不合适，而另一方一直在迁就',
    ])
    writer.writerow([
        '最多改过几次', reschedule['reschedule_stats']['max_label'], '',
        '同一笔订单的改期痕迹条数，报下界',
    ])

    writer.writerow([])
    writer.writerow(['改了几次才定下来', '笔数', '占比（%）', '这一档说明什么'])
    for row in reschedule['reschedule_stats']['bucket_rows']:
        writer.writerow(['', row['label'], row['count'], row['share'], ''])

    writer.writerow([])
    writer.writerow(['改过的 vs 一次谈成的：对方多久回应', '中位耗时', '已回应条数', '口径说明'])
    for row in reschedule['response_contrast']['rows']:
        writer.writerow([
            '', row['label'], row['median_label'], row['total'],
            '响应时长只认 responded_at 减 created_at；updated_at 也会在改期时被写，'
            '拿它算会让「多久回应」和「多久改主意」混在一起',
        ])
    writer.writerow([
        '', '两组相差', reschedule['response_contrast']['gap_points'], '',
        f"两组各自至少 {reschedule['response_contrast']['min_measured_orders']} 条"
        f"才给方向，差值小于 {reschedule['response_contrast']['noise_points']} "
        f"个百分点视为噪声；这只是对照，不给因果——改期多的订单可能本来就是难谈的商品",
    ])

    writer.writerow([])
    writer.writerow(['改期时地点换没换', '笔数', '占比（%）', '这一档说明什么'])
    location_stats = reschedule['location_stats']
    writer.writerow([
        '', '可比对的订单', location_stats['measurable_count'], '',
        '分母是改过期、且订单和安排两边都有地点的订单',
    ])
    writer.writerow([
        '', '地点也换了的', location_stats['shifted_count'],
        location_stats['shift_rate'],
        '只能和「当前这一版」比：前几版的地点被原地覆盖了，库里没有。'
        '所以这一节报的是「最后一次改期时地点和现在这一版不一样」，'
        '不是「地点改过几次」——后者平台答不了',
    ])
    writer.writerow([
        '', '两边都没地点', location_stats['both_missing_count'], '',
        '答不了换没换，单独数，不并进「没换」',
    ])

    writer.writerow([])
    writer.writerow(['最后一版是谁提的', '笔数', '占比（%）', '这一档说明什么'])
    role_stats = reschedule['role_stats']
    writer.writerow([
        '', '买家提的', role_stats['buyer_count'], role_stats['buyer_share'],
        'proposed_by 在改期时被改写，所以能报的只是「最后一版是谁提的」，'
        '不是「谁提的次数多」——后者需要版本表，而平台没有',
    ])
    writer.writerow([
        '', '卖家提的', role_stats['seller_count'], role_stats['seller_share'],
        '只按角色分，不按用户名、不排名、不列是谁：一份「谁在反复改期」的清单'
        '是考勤表，不是运营数据',
    ])
    writer.writerow(['', '提议人已删除', role_stats['unknown_count'], '', ''])

    writer.writerow([])
    writer.writerow([
        '改过两次以上的订单', '订单号', '商品', '改过几次', '最后一版归谁',
    ])
    if reschedule['reschedule_rows']:
        for row in reschedule['reschedule_rows']:
            writer.writerow([
                '', row['order_id'], row['item_title'], row['count'],
                row['proposed_by_role'],
            ])
    else:
        writer.writerow(['', '—', '当前周期没有改过两次以上的交付安排', '', ''])
    if len(reschedule['reschedule_rows']) > reschedule['reschedule_row_limit']:
        writer.writerow([
            '', '—', f"清单最多列 {reschedule['reschedule_row_limit']} 条，完整的在数据库里", '', '',
        ])

    writer.writerow([])
    writer.writerow([
        '说明',
        '改期次数是下界，不是精确值：MeetingAppointment 是 OneToOneField(order)，'
        'propose_meeting 对同一笔订单第二次提交时把时间、地点、提议人原地覆盖，'
        'status 打回 pending，前几版不在任何表里。唯一能证明「改过」的是 OrderEvent '
        '里那句备注，所以 OrderEvent 上线之前的改动没有痕迹，会被算成一次谈成；'
        '备注是自由文本，按子串匹配，那句话一旦被改，这里会静默少算。'
        '匹配必须写全「更新了」三个字——第一次提交写的是「提出了新的…」，'
        '只多一个字，少写一个字就会把第一次提议也算成一次改期。'
        '响应时长和 meeting_attendance 用同一组分档上界：两边读的是同一类字段，'
        '尺度不一致会让运营以为其中一个看板是错的。'
        '本模块只报告，不改 propose_meeting 的 update 分支、不引入版本表、不加字段、'
        '不自动改任何一条交付安排、不自动重发通知、不按用户做任何聚合或排名、'
        '不改 meeting_scheduling 的推荐逻辑，也不把统计写回任何记录。',
    ])
    writer.writerow([])
    first_viewed = dashboard['first_viewed']
    writer.writerow(['从第一次心动到下一步', '数值', '说明'])
    writer.writerow([
        '口径与门槛', first_viewed['summary'],
        '只读 first_viewed_at 与 last_viewed_at，不改写任何一笔浏览记录、任何一件商品、'
        '任何一笔订单，也不把统计写回任何记录',
    ])
    writer.writerow([
        '至少回访过一次的浏览记录', first_viewed['revisit_count'], '',
        '一条浏览记录就是一个（用户，商品）对，本身就是一次心动的完整轨迹；'
        '不按人聚合、也不按商品聚合，那样会把多次心动混成一个数',
    ])
    writer.writerow([
        '只看过一次的浏览记录', first_viewed['single_view_count'], '',
        '没有窗口可言——first_viewed_at 和 last_viewed_at 是同一次写入的产物；'
        '它是「看了就走」的规模，是前者的对照组',
    ])

    writer.writerow([])
    writer.writerow(['考虑窗口', '笔数', '占比（%）', '这一档说明什么'])
    window_stats = first_viewed['window_stats']
    writer.writerow([
        '可分档的回访记录', window_stats['measured_count'], '', '',
        '分母只收回访过的记录；窗口算不出来的记录不计入分档也不计入分母',
    ])
    for row in window_stats['bucket_rows']:
        writer.writerow(['', row['label'], row['count'], row['share'], row['hint']])
    writer.writerow([
        '中位 / P90 / 最久', '',
        f"{window_stats['median_label']} / {window_stats['p90_label']} / "
        f"{window_stats['slowest_label']}", '', '',
        '考虑时长一律按天分档，不按小时：一次考虑的时间尺度是天',
    ])
    writer.writerow([
        '单看一次占比（%）', '', window_stats['single_view_share'], '',
        '拿两堆合起来做分母：它回答「这个平台上的心动，有多大比例看一眼就走」',
    ])
    if window_stats['is_small_sample']:
        writer.writerow([
            '样本不足', '', '', '',
            f"不足 {window_stats['min_measured_histories']} 条，占比只作参考",
        ])

    writer.writerow([])
    writer.writerow(['反复回访之后的下一步', '笔数', '占回访过（%）', '这一档说明什么'])
    next_step_stats = first_viewed['next_step_stats']
    writer.writerow([
        '回访过的记录', '', next_step_stats['measured_total'], '',
        '只看过一次的记录不进这一列：它连窗口都没有，问它「之后做了什么」没有意义',
    ])
    for row in next_step_stats['rows']:
        writer.writerow([
            '', row['label'], row['count'], row['share'], row['hint'],
        ])
    writer.writerow([
        '先下单、后浏览', next_step_stats['ordered_before_count'], '', '',
        '考虑窗口是负的，不计入任何等待分布；算进「没有任何下一步」会让这批'
        '本已成交的样本看起来像失败的样本',
    ])
    if not next_step_stats['has_direction'] and next_step_stats['measured_total']:
        writer.writerow([
            '样本不足', '', '', '',
            f"不足 {next_step_stats['min_measured_histories']} 条，占比只作参考",
        ])

    writer.writerow([])
    writer.writerow(['从第一次心动到下单', '笔数', '占比（%）', '这一档说明什么'])
    decision_stats = first_viewed['decision_stats']
    writer.writerow([
        '先浏览、后下单的记录', decision_stats['measured_count'], '', '',
        '窗口从 first_viewed_at 算到 Order.created_at；分档以周为单位，'
        '因为从心动到掏钱中间还隔着一整套预约、见面、议价',
    ])
    for row in decision_stats['bucket_rows']:
        writer.writerow(['', row['label'], row['count'], row['share'], row['hint']])
    writer.writerow([
        '中位 / P90 / 最久', '',
        f"{decision_stats['median_label']} / {decision_stats['p90_label']} / "
        f"{decision_stats['slowest_label']}", '', '',
        '这一列没有上界，所以报分布而不是平均值：一个想了 90 天的买家会被平均值藏起来',
    ])
    if decision_stats['is_small_sample']:
        writer.writerow([
            '样本不足', '', '', '',
            f"不足 {decision_stats['min_measured_histories']} 条，占比只作参考",
        ])

    writer.writerow([])
    writer.writerow([
        '想了很久却没有下一步的记录', '商品', '买家', '卖家', '浏览次数',
        '考虑窗口', '首次浏览', '最近回访', '商品状态', '口径说明',
    ])
    if first_viewed['stalled_rows']:
        for row in first_viewed['stalled_rows']:
            writer.writerow([
                row['item_title'], row['buyer'], row['seller'], row['view_count'],
                row['window_label'], row['first_viewed_label'],
                row['last_viewed_label'], row['item_status_label'],
                '窗口拖过一周且至今没有任何下一步；一周是「正常节奏」的上界，'
                '比这更短的样本不进清单',
            ])
    else:
        writer.writerow([
            '—', '当前周期没有想了超过一周、至今没有任何下一步的浏览记录',
            '', '', '', '', '', '', '',
        ])
    if (
        first_viewed['stalled_rows']
        and len(first_viewed['stalled_rows']) > first_viewed['stalled_row_limit']
    ):
        writer.writerow([
            '—', f"清单最多列 {first_viewed['stalled_row_limit']} 条，完整的在数据库里",
            '', '', '', '', '', '', '',
        ])

    writer.writerow([])
    writer.writerow([
        '说明',
        '统计窗口按 first_viewed_at 取，不按 last_viewed_at：窗口要回答「这段时间里新产生的'
        '心动考虑了多久」，起点必须是第一次心动。按 last_viewed_at 取会把一条横跨多个周期的'
        '长考虑塞进最后一个周期，而那个周期的其他记录都是当天心动的，混在一起两个数都不能用。'
        '零和「当天」必须分开：零说明两个时间戳是同一次写入的产物，压根没有第二次访问；'
        '「当天」说明第二次访问真的发生了，只是发生在同一天。把前者算成后者，'
        '会让「当天就回访」的比例虚高。'
        '只认 first_viewed_at 之后的收藏和下单：一件先被收藏、后来才第一次被浏览的商品，'
        '收藏不是浏览的结果，不能算成心动的下一步。'
        '模块不判定「为什么没行动」：反复看却不行动，可能是价格，可能是描述没说清，'
        '可能是照片看不出成色，也可能只是买家自己拖——平台没有能力区分这四者，'
        '也就不该替其中任何一种下结论。'
        '不降价、不下架、不给卖家发提醒、不用 first_viewed_at 做任何推荐排序，'
        '也不把统计写回任何记录。',
    ])


    writer.writerow([])
    latency = dashboard['saved_search_latency']
    writer.writerow(['一条命中等了多久才送到', '数值', '说明'])
    writer.writerow([
        '口径与门槛', latency['summary'],
        '只读 matched_at 与 notified_at，不改写任何一条命中、任何一件商品、'
        '任何一个关注条件，也不把统计写回任何记录',
    ])
    writer.writerow([
        '周期内已送达的命中', latency['measured_total'], '',
        '分母只收已经送达的命中；即时节奏不算，它的两个时间戳差约等于零',
    ])
    writer.writerow([
        '还等在缓冲区里的命中', latency['pending_count'], '',
        '没有送达时刻，谈不上等了多久，是右删失样本，不计入任何等待分布',
    ])
    writer.writerow([
        '送达时商品已不可交易', latency['stale_count'], '',
        '按当前状态回看，送达之后才下架或卖掉的也会算进来，所以这一栏偏高',
    ])

    writer.writerow([])
    writer.writerow(['等待档', '条数', '占比（%）', '这一档说明什么', '该档里商品已不可交易', '该档不可交易占比（%）'])
    for cadence in ('daily', 'weekly'):
        stats = latency[cadence]
        for row, cross in zip(stats['rows'], stats['cross']):
            writer.writerow([
                f"{stats['label']} · {row['label']}", row['count'], row['share'], row['hint'],
                cross['stale_count'] if cross['stale_count'] is not None else '',
                cross['stale_rate'] if cross['stale_rate'] is not None else '',
            ])
        writer.writerow([
            f"{stats['label']} 中位 / P90", '', '',
            f"{stats['median_label']} / {stats['p90_label']}",
            stats['stale_count'], '',
        ])
        if stats['is_small_sample']:
            writer.writerow([
                f"{stats['label']} 样本不足", '', '', '',
                f"不足 {latency['min_measured_matches']} 条，占比只作参考", '',
            ])

    writer.writerow([])
    writer.writerow([
        '等得最久、商品又已经不可交易的命中', '关注条件', '商品', '节奏',
        '等了', '命中时间', '送达时间',
    ])
    if latency['stale_rows']:
        for row in latency['stale_rows']:
            writer.writerow([
                row['saved_search_name'], '', row['item_title'], row['cadence_label'],
                row['wait_label'],
                row['matched_at'].strftime('%Y-%m-%d %H:%M'),
                row['notified_at'].strftime('%Y-%m-%d %H:%M'),
            ])
    else:
        writer.writerow([
            '—', '当前周期没有等得久、商品又已不可交易的命中',
            '', '', '', '', '',
        ])
    if latency['stale_row_total'] > latency['stale_row_limit']:
        writer.writerow([
            '—',
            f"清单最多列 {latency['stale_row_limit']} 条，"
            f"当前周期共 {latency['stale_row_total']} 条，完整的在数据库里",
            '', '', '', '', '',
        ])

    writer.writerow([])
    writer.writerow([
        '说明',
        '统计窗口按 matched_at 取，不按 notified_at：要回答的是「这段时间里产生的命中'
        '等了多久才送到」，起点必须是命中产生。一条上个月命中、这个月才补送到的记录，'
        '等的是上个月的那件事；按 notified_at 取会把它塞进最后一个周期，而那个周期的'
        '其他记录都是当天产生的，混在一起两个数都不能用。'
        '日度和周度各用自己的一套阈值，不合成一个平均数：日度拖过一天是异常，'
        '周度拖过一周才值得看，合成一个平均数会把两者的差异抹平，而那正是要动作的地方。'
        '时延和商品状态按档交叉，不按总数交叉：只有逐档对照才答得出「等越久是不是'
        '越容易错过」——每一档的不可交易率都差不多，说明等待不是原因；'
        '后几档明显更高，说明等待本身就是原因。'
        '商品状态只能按当前状态回看，送达之后商品仍可能被下架或卖出，历史状态没有记录，'
        '所以「送达时已不可交易」系统性偏高。宁可高估——运营看到高估的数字会去核实，'
        '看到低估的数字会直接相信。'
        '模块不判定「为什么慢了」：一条命中等了三周，可能是定时任务漏跑，'
        '可能是这一周没有新商品，也可能是这个用户的条件太宽——平台没有能力区分这三者，'
        '也就不该替任何一个用户改他的通知频率。'
        '不调整任何用户的汇总节奏、不清理失效命中、不重发通知、'
        '也不用统计结果做任何推荐排序。',
    ])

    writer.writerow([])
    quiet = dashboard['saved_search_quiet_window']
    writer.writerow(['关注搜索的临时静默：被用户主动推迟的那批命中', '数值', '说明'])
    writer.writerow([
        '口径与门槛', quiet['summary'],
        '只读 quiet_until 与缓冲命中，不改写任何一条关注、任何一条命中、'
        '任何一件商品，也不把统计写回任何记录',
    ])
    writer.writerow([
        '正在静默', quiet['quiet_now_count'], '',
        'quiet_until 是一个绝对时刻，还没到，写入路径继续跳过它',
    ])
    writer.writerow([
        '静默已过期', quiet['quiet_expired_count'], '',
        '时刻已过，写入路径不再跳过它，但它还留在字段里——不清空、也不再生效',
    ])
    writer.writerow([
        '从没静默过', quiet['never_quiet_count'], '',
        '没有按下过暂停键的关注，和按下过又清空的关注不是一回事',
    ])
    writer.writerow([
        '静默中的关注压在缓冲区里的命中', quiet['buffered_now_count'], '',
        '攒命中是暂停键的本意，这一栏不是问题',
    ])
    writer.writerow([
        '早已过期的关注还压在缓冲区里的命中', quiet['buffered_expired_count'], '',
        '这一栏才是问题：静默结束之后发送路径没接上',
    ])
    writer.writerow([
        '静默时长', '报不了', '',
        'quiet_until 只存结束时刻，站内没有任何字段记下用户什么时候按下暂停键；'
        'updated_at 每次保存都被覆盖，而节奏表单每次都写这个字段，'
        '所以它连「最后一次改动」都不是可靠信号。反推时长等于编数据，如实不报',
    ])

    writer.writerow([])
    writer.writerow(['这条关注此刻停在哪一档', '状态', '关注数', '占比（%）', '缓冲区命中', '其中商品已不可交易'])
    for row in quiet['state_rows']:
        writer.writerow([
            '—', row['label'], row['search_count'],
            '—' if row['share'] is None else row['share'],
            row['buffered_count'], row['stale_buffered_count'],
        ])
    writer.writerow([
        '说明', '', '', '',
        '占比的分母是全部关注，不是这一档自己——拿一档除一档永远得到 100%',
        '商品状态按当前状态回看，所以这一栏系统性偏高',
    ])

    writer.writerow([])
    for key, label in (('buffer_now', '正在静默'), ('buffer_expired', '静默已过期')):
        stats = quiet[key]
        writer.writerow(['静默期攒下的命中有多旧', '档位', '条数', '占比（%）', '这一档说明什么'])
        writer.writerow([
            f'{label} · 平均年龄', '', stats['measured_count'],
            '—' if stats['stale_rate'] is None else stats['stale_rate'],
            f"平均 {stats['mean_label']}，最早 {stats['oldest_label']}",
        ])
        for row in stats['rows']:
            writer.writerow([
                f'{label} · {row["label"]}', '', row['count'],
                '—' if row['share'] is None else row['share'], row['hint'],
            ])
        if stats['is_small_sample']:
            writer.writerow([
                f'{label} · 样本不足', '', stats['measured_count'], '',
                f"不足 {quiet['min_buffered_matches']} 条，年龄结构只作参考",
            ])

    writer.writerow([])
    delivered = quiet['delivered']
    writer.writerow(['静默结束之后，这条关注还活着吗', '关注条件', '节奏', '静默结束于', '暂停至今', '之后新命中', '缓冲区还剩'])
    for row in delivered['rows']:
        writer.writerow([
            row['saved_search_name'], row['cadence_label'],
            row['quiet_until'].strftime('%Y-%m-%d %H:%M'),
            f"{row['expired_for_days']:.1f} 天",
            row['later_hit_count'], row['buffered_count'],
        ])
    if not delivered['rows']:
        writer.writerow(['—', '当前没有静默已过期的关注', '', '', '', '', ''])
    if delivered['row_total'] > delivered['row_limit']:
        writer.writerow([
            '—',
            f"清单最多列 {delivered['row_limit']} 条，"
            f"当前共 {delivered['row_total']} 条，完整的在数据库里",
            '', '', '', '', '',
        ])
    writer.writerow([
        '说明', '', '', '',
        f"共 {delivered['total']} 条静默已过期的关注，其中 {delivered['silent_count']} 条"
        f"至今没有新的命中，{delivered['alive_count']} 条还有",
        '—' if delivered['alive_rate'] is None else f"{delivered['alive_rate']}%",
        '「暂停至今」的起点是静默结束，不是关注创建',
    ])
    writer.writerow([
        '说明', '', '', '',
        '只报「静默结束后还有没有新的命中」这一个事实，不报取消率——'
        '取消关注可能只是用户已经找到了东西，和静默没有因果关系',
        '不按用户排名，不报任何用户名、不列任何商品名',
        '关注名称可以报，它是用户给自己条件起的名字，不是身份',
    ])

    writer.writerow(['正在静默的关注，缓冲区的年龄结构', '关注条件', '节奏', '缓冲命中', '最早一条', '静默至'])
    wrote_row = False
    for key, label in (('buffer_now', '正在静默'), ('buffer_expired', '静默已过期')):
        for row in quiet[key]['search_rows']:
            wrote_row = True
            writer.writerow([
                '—', f"{label} · {row['saved_search_name']}", row['cadence_label'],
                row['buffered_count'], row['oldest_label'],
                row['quiet_until'].strftime('%Y-%m-%d %H:%M'),
            ])
    if not wrote_row:
        writer.writerow(['—', '两档静默关注此刻都没有压在缓冲区里的命中', '', '', '', ''])
    writer.writerow([
        '说明', '', '', '',
        '只对「正在静默」和「早已过期」两档有意义；「从没静默过」的关注压在缓冲区里的'
        '命中，成因是汇总节奏，那是 saved_search_latency 的题目',
        '静默造成的等待和平台造成的等待必须分开，混进同一个分布，'
        '运营会把「用户主动暂停」读成「我们的任务又慢了」',
        '两套阈值不合成一个平均数',
    ])

    writer.writerow([])
    limit_shape = dashboard['saved_search_limit_shape']
    writer.writerow(['用户自己配的「一条通知里列几件商品」', '数值', '说明'])
    writer.writerow([
        '口径与门槛', limit_shape['summary'],
        '副标题：单次最多提醒条数，用户自己配的截断',
        '只读 max_matches_per_notice 与缓冲区命中，不改写任何一条关注、任何一条命中、'
        '任何一件商品，也不把统计写回任何记录',
        f"一档关注至少 {limit_shape['min_limit_searches']} 条才给方向，"
        f"缓冲命中至少 {limit_shape['min_buffered_matches']} 条才给方向，"
        f"交叉格子至少 {limit_shape['min_cadence_cell']} 条才给方向",
    ])
    for row in limit_shape['limit_rows']:
        writer.writerow([
            '—',
            f"{row.label}{'（默认值）' if row.is_default else ''}",
            row.count,
            '—' if row.share is None else row.share,
            '占比的分母是全部关注，不是这一档自己',
        ])
    out_of_range = limit_shape['out_of_range']
    writer.writerow([
        '库里存的不是 1 到 9', out_of_range['stored_count'], '',
        '—' if out_of_range['share'] is None else out_of_range['share'],
        f"实际按 {out_of_range['effective_limit']} 条生效；"
        f"配置值和实际生效值不是一回事，两档分开数不合并",
    ])
    truncation = limit_shape['truncation']
    writer.writerow([
        '缓冲区里的命中', truncation['buffered_count'], '',
        '—', '只取发送侧扫过的那 20 条，第 21 条发送时压根不会被看到',
    ])
    writer.writerow([
        '正被自己配的条数挡住', truncation['hidden_count'], '',
        '—' if truncation['hidden_share'] is None else truncation['hidden_share'],
        '占比的分母是全部缓冲命中，不是被挡掉的那一批',
    ])
    writer.writerow([
        '正在被截断的关注', truncation['truncated_search_count'], '',
        '—' if truncation['truncated_share'] is None else truncation['truncated_share'],
        '占比的分母是全部关注；平均被挡掉 '
        f"{truncation['mean_hidden_label']}，最多 {truncation['max_hidden_count']} 条",
    ])

    writer.writerow([
        '说明', '', '', '',
        '只报此刻缓冲区里正在被截断的规模，不报历史规模：发送完成后 notified_at 一写，'
        '那批命中是展示的还是截断的就再也分不出来了，没有任何字段留下痕迹。'
        '把当前快照当长期趋势读，是本模块最容易被误用的地方',
        '不报条数改过几次、不报修改时刻：updated_at 被同一个表单里的 notify_frequency '
        '和 quiet_until 共用，改频率、填静默、清空静默都会刷新它，反推等于编数据',
        '空字符串提交是「不改」而不是「改成默认值」，所以默认值 3 是「主动配的」和'
        '「从没配过的」两种关注的会合点，不是「从没配过」的精确计数',
        '条数与节奏只做形状描述，不给因果、不建议该配多少条：选周度汇总又配 1 条，'
        '可能是只想要一个「有动静了就告诉我一声」的信号，平台没有能力区分这两种动机',
        '不自动调任何人的条数、不报取消率、不把「配得小」读成「用户不满」',
        '不按用户排名，不报任何用户名、不列任何商品名；关注名称可以报，'
        '它是用户给自己条件起的名字，不是身份',
    ])

    writer.writerow([])
    writer.writerow(['条数 × 节奏交叉', '节奏', '关注数', '中位条数', '平均条数', '正被截断的关注', '占比（%）'])
    for row in limit_shape['cadence_rows']:
        writer.writerow([
            '—', row['label'], row['search_count'],
            '—' if row['median_limit'] is None else row['median_limit'],
            row['mean_limit_label'],
            row['truncated_count'],
            '—' if row['truncated_share'] is None else row['truncated_share'],
        ])
    writer.writerow([
        '说明', '', '', '', '',
        '中位条数按实际生效的值算，不按库里存的配置值——存 0 的关注生效的是 3',
        '这一节只做形状描述，不给因果',
    ])

    writer.writerow([])
    writer.writerow(['正在被截断的关注', '关注条件', '节奏', '配了几条', '缓冲命中', '被挡掉'])
    for row in limit_shape['truncated_rows']:
        writer.writerow([
            '—', row.saved_search_name, row.cadence_label,
            row.configured, row.buffered_count, row.hidden_count,
        ])
    if limit_shape['truncated_row_total'] > limit_shape['truncated_row_limit']:
        writer.writerow([
            '—',
            f"清单最多列 {limit_shape['truncated_row_limit']} 条，"
            f"当前共 {limit_shape['truncated_row_total']} 条，完整的在数据库里",
            '', '', '', '',
        ])

    writer.writerow([])
    matched = dashboard['lost_found_matched_outcome']
    writer.writerow([u'失物招领确认匹配之后，记录停在哪一种终局里', u'数值', u'说明'])
    writer.writerow([
        u'口径与门槛', matched['summary'],
        u'只读 status、matched_post、已确认线索与反馈，不改写任何一条记录、'
        u'任何一条线索、任何一份反馈，也不把统计写回任何记录',
    ])
    writer.writerow([
        u'统计过的匹配记录', matched['matched_total'], '',
        u'现在 status == matched 的，加上历史上被匹配过的；不按统计周期过滤',
    ])
    for row in matched['outcome_rows']:
        writer.writerow([
            u'—', row['label'], row['post_count'],
            u'—' if row['share'] is None else row['share'],
            u'占比的分母是全部匹配记录，不是这一档自己',
        ])
    writer.writerow([
        u'说明', '', '', '',
        u'「已被发布者关闭」「已过期」「回到寻找中」三档在当前代码里都到不了：'
        u'matched 是单向门，关闭、过期、编辑三处的前置条件都要求 status == active',
    ])
    writer.writerow([
        u'仍标记已匹配', '', matched['outcome_rows'][0]['post_count'], '',
        u'确认之后什么都不做是最自然的结局，这一栏不读成问题',
    ])
    writer.writerow([
        u'其中截止时间已过', '', matched['overdue_stats']['measured_count'], '',
        u'分母是「仍标记已匹配」这一档，不是全部匹配记录',
    ])
    writer.writerow([
        u'没有截止时间的', '', matched['overdue_stats']['no_deadline_count'], '',
        u'没有截止时间的记录谈不上过期，是右删失样本，不计入任何分布',
    ])

    writer.writerow([])
    writer.writerow([
        u'挂了一个月的「已匹配」：截止时间已过，状态还没动',
        u'档位', u'条数', u'占比（%）', u'这一档说明什么',
    ])
    for row in matched['overdue_stats']['rows']:
        writer.writerow([
            u'—', row['label'], row['count'],
            u'—' if row['share'] is None else row['share'],
            u'过期越久，越可能是发布者忘了回来收尾',
        ])
    writer.writerow([
        u'说明', '', '', '',
        u'三个档位互斥且完整，加起来等于分母；不报取消率——忘了和觉得没必要，'
        u'观测数据分不开',
        u'按天分档：失物招领的时间尺度是天',
    ])
    if matched['overdue_stats']['is_small_sample']:
        writer.writerow([
            u'样本不足', '', matched['overdue_stats']['measured_count'], '',
            u"不足 {matched['min_bucket_sample']} 条，年龄结构只作参考",
        ])

    writer.writerow([])
    counterpart = matched['counterpart_stats']
    writer.writerow([u'匹配是双向的，另一条记录跟上了吗', u'数值', u'说明'])
    writer.writerow([
        u'填了关联记录的', counterpart['measured_count'], '',
        u'accept 会把两边都置为 matched 并互填 matched_post',
    ])
    writer.writerow([
        u'互为匹配', counterpart['mutual_count'], '',
        u'—' if counterpart['mutual_rate'] is None else f"{counterpart['mutual_rate']}%",
    ])
    writer.writerow([
        u'只有一边认这笔账', counterpart['one_sided_count'], '',
        u'单边匹配不是 bug——用户可能删掉了自己那条记录，但它是直接证据',
    ])
    writer.writerow([
        u'没有关联记录的', counterpart['no_counterpart_count'], '',
        u'单独数，不算进双向性的分母',
    ])
    writer.writerow([
        u'说明', '', '', '',
        u'只报「两边是不是同一件事」，不报另一条记录是谁',
        u'标题是自由文本，可能写着证件号，所以清单和这里都不出现标题',
    ])

    writer.writerow([])
    writer.writerow([
        u'哪种终局的记录，还有人回来填反馈',
        u'终局', u'已确认线索', u'提交反馈', u'提交率（%）',
    ])
    for row in matched['feedback']['rows']:
        writer.writerow([
            u'—', row['label'], row['accepted_count'], row['feedback_count'],
            u'—' if row['feedback_rate'] is None else row['feedback_rate'],
        ])
    if not matched['feedback']['rows']:
        writer.writerow([u'—', u'当前没有带已确认线索的匹配记录', '', '', ''])
    writer.writerow([
        u'说明', '', '', '',
        u'分母是这一档里已有已确认线索的记录；没有已确认线索的不进分母',
        u'只报终局维度，不重复 lost_found_lead_quality 的反馈维度结论',
    ])

    writer.writerow([])
    listing = matched['list']
    writer.writerow([
        u'截止时间已过最久的那些记录', u'记录编号', u'记录类型', u'过期多久',
        u'有另一条关联记录', u'提交过反馈',
    ])
    for row in listing['rows']:
        writer.writerow([
            u'—', row['post_id'], row['post_type_label'], row['overdue_label'],
            u'有' if row['has_counterpart'] else '—',
            u'有' if row['has_feedback'] else '—',
        ])
    if not listing['rows']:
        writer.writerow([u'—', u'当前没有截止时间已过的匹配记录', '', '', '', ''])
    if listing['row_total'] > listing['row_limit']:
        writer.writerow([
            u'—',
            u"清单最多列 {listing['row_limit']} 条，当前共 {listing['row_total']} 条，"
            u'完整的在数据库里',
            '', '', '', '',
        ])
    writer.writerow([
        u'说明', '', '', '', '',
        u'清单是给「现在去核实」用的，不是给归档用的',
        u'记录编号可以报，它是数字不是身份；标题和发布者都不报',
    ])

    writer.writerow([])
    friction = dashboard['campus_verification_friction']
    writer.writerow(['一封验证邮件，用户隔了多久才点开', '数值', '说明'])
    writer.writerow([
        '口径与门槛', friction['summary'],
        '只读 token_issued_at 与 verified_at 的差值，不改写任何一条认证记录、'
        '任何一个用户，也不把统计写回任何记录',
    ])
    writer.writerow([
        '周期内认证成功', friction['stats']['measured_count'], '',
        '分母只收已经认证成功的记录；未完成认证没有完成时刻，谈不上等了多久',
    ])
    writer.writerow([
        '还挂在「待验证邮箱」的认证', friction['pending_count'], '',
        '按 token_issued_at 取窗口数出来，是右删失样本，不计入任何耗时分布',
    ])
    writer.writerow([
        '隔超过一天才点开', friction['slow_row_total'], '',
        '多半是被当成垃圾邮件，或者用户已经忘了这回事，正是要核实的一批',
    ])

    writer.writerow([])
    writer.writerow(['等待档', '条数', '占比（%）', '这一档说明什么'])
    for row in friction['stats']['rows']:
        writer.writerow([row['label'], row['count'], row['share'], row['hint']])
    writer.writerow([
        '中位 / P90 / 平均', '', '',
        f"{friction['stats']['median_label']} / {friction['stats']['p90_label']}"
        f" / {friction['stats']['mean_label']}",
    ])
    if friction['stats']['is_small_sample']:
        writer.writerow([
            '样本不足', '', '', '',
            f"不足 {friction['min_measured_verifications']} 条，占比只作参考", '',
        ])

    writer.writerow([])
    writer.writerow([
        '按域名看，是哪一类邮箱在慢', '学校 / 域名', '认证成功', '中位耗时',
        '隔超过一天', '占比（%）', '是否值得核实',
    ])
    for row in friction['domain_rows']:
        writer.writerow([
            '', row['domain_name'], row['count'], row['median_label'],
            row['slow_count'], row['slow_rate'],
            '是' if row['slow_rate'] >= friction['slow_domain_points'] else '',
        ])

    writer.writerow([])
    writer.writerow([
        '隔了超过一天才点开链接的记录', '用户', '校园邮箱', '学校 / 域名',
        '等了', '邮件发出', '认证成功',
    ])
    if friction['slow_rows']:
        for row in friction['slow_rows']:
            writer.writerow([
                '', row['username'], row['campus_email'], row['domain_name'],
                row['elapsed_label'],
                row['token_issued_at'].strftime('%Y-%m-%d %H:%M'),
                row['verified_at'].strftime('%Y-%m-%d %H:%M'),
            ])

    writer.writerow([])
    gift = dashboard['gift_application_queue']
    writer.writerow(['免费领取的申请队列，第一次被运营读到', '数值', '说明'])
    writer.writerow([
        '口径与门槛', gift['summary'],
        '只读 applicant_note 的填写情况与 decided_at 减 created_at 的差值，不改写任何'
        '一条申请、不给卖家排序、不替卖家推荐申请人，也不把统计写回任何记录',
    ])
    writer.writerow([
        '周期内处理完的申请', gift['stats']['measured_count'], '',
        '分母只收 selected / rejected / withdrawn 三种已经处理完的状态',
    ])
    writer.writerow([
        '还挂在「等待选择」的申请', gift['pending_count'], '',
        '按 created_at 取窗口数出来，是右删失样本，不计入任何等待分布',
    ])
    writer.writerow([
        '中位 / P90 / 平均处理时长', '', '',
        f"{gift['stats']['median_label']} / {gift['stats']['p90_label']}"
        f" / {gift['stats']['mean_label']}",
    ])

    writer.writerow([])
    writer.writerow(['队列长度', '商品数', '占比（%）', '这一档说明什么'])
    for row in gift['queue_rows']:
        writer.writerow([row['label'], row['count'], row['share'], row['hint']])

    writer.writerow([])
    writer.writerow(['处理时长', '条数', '占比（%）', '这一档说明什么'])
    for row in gift['stats']['rows']:
        writer.writerow([row['label'], row['count'], row['share'], row['hint']])

    writer.writerow([])
    writer.writerow(['申请说明', '申请数', '占比（%）', '被选中', '选中率（%）'])
    for row in gift['note']['rows']:
        writer.writerow([
            row['label'], row['count'], row['share'], row['selected_count'],
            '样本不足' if row['is_small_sample'] else row['selected_rate'],
        ])
    if gift['note']['is_comparable']:
        writer.writerow([
            '两组选中率之差', '', '', gift['note']['difference'],
            gift['note']['direction'],
        ])

    writer.writerow([])
    writer.writerow(['分类', '申请数', '被选中', '选中率（%）'])
    for row in gift['category_rows']:
        writer.writerow([
            row['category_name'], row['applications'],
            row['selected_count'], row['selected_rate'],
        ])

    writer.writerow([])
    writer.writerow([
        '队最长的免费商品', '分类', '队列长度', '被选中', '已撤回', '队列档位',
    ])
    for row in gift['item_rows']:
        writer.writerow([
            row['title'], row['category_name'], row['queue_length'],
            row['selected_count'], row['withdrawn_count'], row['queue_label'],
        ])

    writer.writerow([])
    writer.writerow([
        '被申请人自己撤回的申请', '申请人', '申请说明字数', '等了',
        '提交申请', '撤回',
    ])
    for row in gift['withdrawn_rows']:
        writer.writerow([
            row['item_title'], row['applicant_username'],
            row['note_length'] if row['note_length'] else '未填写',
            row['decided_label'],
            row['created_at'].strftime('%Y-%m-%d %H:%M'),
            row['decided_at'].strftime('%Y-%m-%d %H:%M'),
        ])
    if gift['stats']['is_small_sample']:
        writer.writerow([
            '样本不足', '', '', '', '',
            f"不足 {gift['min_measured_applications']} 份，占比只作参考",
        ])

    writer.writerow([])
    images = dashboard['item_image_coverage']
    writer.writerow([
        '商品图片配了多少张，第一次被运营读到', '值', '说明',
    ])
    writer.writerow([
        '概述', images['summary'],
        '只看当前在架商品（available），已售出 / 已预订 / 已过期不进分母；'
        '上传后又删图不留历史，这里只能读到剩下几张',
    ])
    writer.writerow([
        '在架商品总数', images['coverage']['total'], '',
        '分母只含在架商品',
    ])
    writer.writerow([
        '一张图都没配', images['coverage']['no_image_count'], '',
        '这些商品在详情页上直接露出空图位',
    ])
    writer.writerow([
        '无图占比（%）', images['coverage']['no_image_rate'], '',
        '无图商品数 / 在架商品总数',
    ])
    writer.writerow([
        '无图商品被举报过', images['coverage']['no_image_reported_count'], '',
        '只统计更新前就存在的举报，不录举报理由；'
        '图片覆盖度不能推断举报原因',
    ])
    writer.writerow([
        '最小视测商品数', images['coverage']['min_measured_items'], '',
        '低于这个数量时占比只能参考',
    ])
    if images['coverage']['is_small_sample']:
        writer.writerow([
            '小样本提醒', '', '',
            f"当前在架商品只有 {images['coverage']['total']} 件，"
            f"占比仅供参考",
        ])

    writer.writerow([])
    writer.writerow(['有图 / 无图对照'])
    writer.writerow([
        '分组', '商品数', '订单数', '成交数',
        '成交率（%）',
    ])
    for key, label in (
        ('with_image', '有图商品'),
        ('no_image', '无图商品'),
    ):
        group = images['contrast'][key]
        writer.writerow([
            label, group['item_count'], group['order_count'],
            group['dealt_count'], group['dealt_rate'],
        ])
    writer.writerow([
        '差距（百分点）', '', '', '',
        images['contrast']['gap_points'],
        '只做对照不给因果；选择效应下不要把它当回应效应',
    ])
    if not images['contrast']['is_measurable']:
        writer.writerow([
            '不可比较', '', '', '',
            f"两组商品数均小于 {images['min_measured_items']}，"
            f"不展示差距结论",
        ])
    elif not images['contrast']['gap_is_meaningful']:
        writer.writerow([
            '差距未超器声线', '', '', '',
            f"差距低于 {images['noise_points']} 个百分点，不视为真实差异",
        ])

    writer.writerow([])
    writer.writerow(['图片数分档'])
    writer.writerow([
        '图片数', '商品数', '占比（%）', '订单数',
        '成交数', '成交率（%）',
    ])
    for row in images['image_counts']['rows']:
        writer.writerow([
            row['label'], row['item_count'], row['item_share'],
            row['order_count'], row['dealt_count'], row['dealt_rate'],
            '商品数低于门槛，成交率仅供参考' if row['is_small_sample'] else '',
        ])

    writer.writerow([])
    writer.writerow(['按交易方式看无图情况'])
    writer.writerow([
        '交易方式', '商品数', '无图数', '无图占比（%）',
        '订单数', '成交数', '成交率（%）',
    ])
    for row in images['by_trade_mode']['rows']:
        writer.writerow([
            row['label'], row['item_count'], row['no_image_count'],
            row['no_image_rate'], row['order_count'], row['dealt_count'],
            row['dealt_rate'],
        ])

    writer.writerow([])
    writer.writerow(['按分类看无图情况'])
    writer.writerow([
        '分类', '商品数', '无图数', '无图占比（%）',
        '订单数', '成交数', '成交率（%）',
    ])
    for row in images['by_category']['rows']:
        writer.writerow([
            row['label'], row['item_count'], row['no_image_count'],
            row['no_image_rate'], row['order_count'], row['dealt_count'],
            row['dealt_rate'],
            '商品数低于门槛，仅供参考' if row['is_small_sample'] else '',
        ])

    writer.writerow([])
    writer.writerow(['一张图都没配的商品'])
    writer.writerow([
        '商品', '分类', '交易方式',
        '举报数', '上架时间',
    ])
    for row in images['no_image_rows']['rows']:
        writer.writerow([
            row.item.title, row.category_label, row.trade_mode_label,
            row.report_count,
            row.item.created_at.strftime('%Y-%m-%d %H:%M'),
        ])
    writer.writerow([
        '共计', '', '', '',
        f"无图商品共 {images['no_image_rows']['row_total']} 件，"
        f"以上只列 {images['no_image_rows']['row_limit']} 件，不按商品进行排名",
    ])

    writer.writerow([])
    rule = dashboard['moderation_rule_quality']
    writer.writerow([
        '内容审核规则的误判与漏判，第一次被运营读到', '数值', '说明',
    ])
    writer.writerow([
        '口径与门槛', rule['summary'],
        '只读 matched_terms 拆出的规则名、risk_score 与 reviewed_at 减 created_at '
        '的差值，不改写任何一条审核记录、不自动确认或放行、不改 _RULES 与权重、'
        '不读 content 正文',
    ])
    writer.writerow([
        '周期内复核完的记录', rule['stats']['measured_count'], '',
        '分母只收 confirmed / dismissed 两种已经复核完的状态',
    ])
    writer.writerow([
        '还挂在「待复核」的记录', rule['pending_count'], '',
        '按 created_at 取窗口数出来，是右删失样本，不计入任何分布',
    ])
    writer.writerow([
        '中位 / P90 / 平均复核时长', '', '',
        f"{rule['stats']['median_label']} / {rule['stats']['p90_label']}"
        f" / {rule['stats']['mean_label']}",
    ])

    writer.writerow([])
    writer.writerow(['四条规则，各自判得准不准'])
    writer.writerow([
        '规则', '权重', '命中', '确认违规', '误判放行', '误判率（%）',
        '漏判条数', '漏判率（%）',
    ])
    for row in rule['rule_rows']:
        writer.writerow([
            row['rule_name'], row['weight'], row['hit_count'],
            row['confirmed_count'], row['dismissed_count'],
            '样本不足' if row['is_small_sample'] else row['false_positive_rate'],
            row.get('missed_count', 0),
            '未命中' if row['never_hit'] else (
                '样本不足' if row['is_small_sample'] else row['false_negative_rate']
            ),
        ])

    writer.writerow([])
    writer.writerow(['从拦截到复核，隔了多久'])
    writer.writerow(['复核时长', '条数', '占比（%）', '这一档说明什么'])
    for row in rule['stats']['rows']:
        writer.writerow([row['label'], row['count'], row['share'], row['hint']])

    writer.writerow([])
    writer.writerow(['分数段：高分的档是不是真的更容易确认违规'])
    writer.writerow([
        '分数段', '条数', '确认违规', '误判放行', '确认率（%）', '这一档是什么',
    ])
    for row in rule['score_rows']:
        writer.writerow([
            row['label'], row['count'], row['confirmed'], row['dismissed'],
            '样本不足' if row['is_small_sample'] else row['confirmed_rate'],
            row['hint'],
        ])

    writer.writerow([])
    writer.writerow(['按渠道分开报：留言和私信'])
    writer.writerow([
        '渠道', '条数', '确认违规', '误判放行', '误判率（%）',
    ])
    for row in rule['channel_rows']:
        writer.writerow([
            row['channel_label'], row['count'], row['confirmed_count'],
            row['dismissed_count'],
            '样本不足' if row['is_small_sample'] else row['false_positive_rate'],
        ])

    writer.writerow([])
    writer.writerow(['疑似漏判：确认违规，却只命中了轻规则'])
    writer.writerow([
        '疑似漏判的记录', '渠道', '风险等级', '分数', '命中的规则', '复核耗时',
        '拦截于', '复核于',
    ])
    for row in rule['missed_rows']:
        writer.writerow([
            f"#{row['event_id']}", row['channel_label'],
            row['risk_level_label'], row['risk_score'], row['rules'],
            row['review_label'],
            row['created_at'].strftime('%Y-%m-%d %H:%M'),
            row['reviewed_at'].strftime('%Y-%m-%d %H:%M'),
        ])
    if rule['missed_row_total'] > rule['missed_row_limit']:
        writer.writerow([
            '清单最多列出', rule['missed_row_limit'], '条',
            f"共 {rule['missed_row_total']} 条",
        ])
    if rule['stats']['is_small_sample']:
        writer.writerow([
            '样本不足', '', '', '', '',
            f"不足 {rule['min_measured_events']} 条，占比只作参考",
        ])

    writer.writerow([])
    writer.writerow([])
    appeal_quality = dashboard['moderation_appeal_quality']
    writer.writerow([
        '内容申诉处理质量，第一次被运营读到', '数值', '说明',
    ])
    writer.writerow([
        '口径与门槛', appeal_quality['summary'],
        '提交按 submitted_at、处理按 resolved_at、待处理按当前时刻分三条线；'
        '不读申诉说明与处理说明内容，不展示用户名和被拦截正文',
    ])
    writer.writerow([
        '当前待处理申诉', appeal_quality['pending']['measured_count'], '',
        f"最久等待 {appeal_quality['pending']['oldest_label']}，"
        f"超过一天 {appeal_quality['pending']['over_one_day_count']} 条",
    ])
    writer.writerow([
        '周期内已处理申诉', appeal_quality['outcome']['measured_count'], '',
        f"成立 {appeal_quality['outcome']['accepted_total']} 条；"
        f"样本不足 {appeal_quality['min_direction_sample']} 条时比例只作参考",
    ])

    writer.writerow([])
    writer.writerow(['确认违规之后，有多少人提出申诉'])
    writer.writerow(['口径', '数值', '说明'])
    submission = appeal_quality['submission']
    writer.writerow(['周期内提交', submission['submitted_count'], '按 submitted_at 归期'])
    writer.writerow(['申诉人数', submission['appellant_count'], '只报计数，不列用户'])
    writer.writerow([
        '同期确认违规', submission['original_decision_count'],
        '包含后来被申诉改判的记录；靠原复核时间快照找回原判事实',
    ])
    writer.writerow([
        '可见申诉率（%）', submission['appeal_rate'],
        f"同期已申诉 {submission['cohort_appeal_count']} 条；无申诉时效，只是当前下界",
    ])
    writer.writerow([
        '历史结论进入本周期', submission['historical_decision_count'],
        '提交在周期内、原判在周期外，不进入同期申诉率分母',
    ])
    writer.writerow([
        '原判到提交（中位 / P90 / 平均）', '',
        f"{submission['delay']['median_label']} / "
        f"{submission['delay']['p90_label']} / {submission['delay']['mean_label']}",
    ])
    writer.writerow([
        '原复核时间快照缺失', submission['missing_snapshot_count'],
        '缺失行不进入提交间隔分布',
    ])
    writer.writerow([
        '申诉说明字数（中位 / P90 / 平均 / 最大）', '',
        f"{submission['statement']['median']} / {submission['statement']['p90']} / "
        f"{submission['statement']['mean']} / {submission['statement']['max']}",
    ])

    writer.writerow([])
    writer.writerow(['申诉人为什么提交'])
    writer.writerow(['原因', '申诉', '申诉人', '提交占比（%）', '样本'])
    for row in appeal_quality['reason_rows']:
        writer.writerow([
            row['label'], row['count'], row['appellant_count'], row['share'],
            '样本不足' if row['is_small_sample'] else '可看方向',
        ])

    writer.writerow([])
    writer.writerow(['处理结论与恢复缺口'])
    writer.writerow(['结论', '数量', '已处理占比（%）', '口径'])
    for row in appeal_quality['outcome']['rows']:
        writer.writerow([
            row['label'], row['count'], row['share'],
            '计入申诉成立，另报恢复缺口'
            if row['status'] == 'accepted_without_restore' else '按 resolved_at 归期',
        ])
    writer.writerow([
        '成立但未恢复 / 两种成立',
        appeal_quality['outcome']['counts']['accepted_without_restore'],
        appeal_quality['outcome']['accepted_without_restore_rate'],
        '缺历史恢复材料，不伪装成已恢复',
    ])

    writer.writerow([])
    writer.writerow(['从提交到处理完成，隔了多久'])
    writer.writerow(['处理时长', '条数', '占比（%）', '说明'])
    for row in appeal_quality['resolution']['duration']['rows']:
        writer.writerow([row['label'], row['count'], row['share'], row['hint']])
    writer.writerow([
        '中位 / P90 / 平均', appeal_quality['resolution']['duration']['median_label'],
        appeal_quality['resolution']['duration']['p90_label'],
        appeal_quality['resolution']['duration']['mean_label'],
    ])

    writer.writerow([])
    writer.writerow(['换人复核了吗'])
    writer.writerow(['复核人关系', '条数', '占比（%）', '口径'])
    reviewer = appeal_quality['resolution']['reviewer']
    writer.writerow(['同一人', reviewer['same_count'], reviewer['same_rate'], '不评价对错，只报事实'])
    writer.writerow(['换过人', reviewer['different_count'], reviewer['different_rate'], '不评价对错，只报事实'])
    writer.writerow(['留痕缺失', reviewer['unknown_count'], '', '缺任一人时不猜同人或换人'])
    writer.writerow([
        '处理说明填写率（%）', appeal_quality['resolution']['note']['fill_rate'],
        appeal_quality['resolution']['note']['filled_count'],
        f"平均 {appeal_quality['resolution']['note']['mean']} 字，只数长度不读内容",
    ])

    writer.writerow([])
    writer.writerow(['当前待处理申诉与等待年龄'])
    writer.writerow(['申诉', '渠道', '原因', '提交于', '等待', '说明字数', '原复核留痕'])
    for row in appeal_quality['pending']['rows']:
        writer.writerow([
            f"#{row['appeal_id']}", row['channel_label'], row['reason_label'],
            row['submitted_at'].strftime('%Y-%m-%d %H:%M'), row['wait_label'],
            row['statement_length'], '有' if row['has_original_reviewer'] else '缺',
        ])
    writer.writerow([
        '清单说明', appeal_quality['pending']['row_total'], '',
        f"页面最多列 {appeal_quality['pending']['row_limit']} 条，CSV 输出完整清单",
    ])
    watch = dashboard['availability_watch_quality']
    writer.writerow([
        '等一个有货的人等了多久，第一次被运营读到', '数值', '说明',
    ])
    writer.writerow([
        '口径与门槛', watch['summary'],
        '只读 ItemAvailabilityWatch 的 created_at、item 的 status / trade_mode / '
        'category 和 Notification 的 is_read，不改写任何一条关注、不自动清理、'
        '不补发通知、不读任何自由文本。消费即删除，所以「等到的人等了多久」'
        '数不出来，本模块只报当前仍挂着的关注',
    ])
    writer.writerow([
        '现在还挂着的关注', watch['stats']['measured_count'], '',
        '按 created_at 取窗口，只收当前仍在等的；被取消和被消费的是右删失样本',
    ])
    writer.writerow([
        '中位 / P90 / 平均等待', '', '',
        f"{watch['stats']['median_label']} / {watch['stats']['p90_label']}"
        f" / {watch['stats']['mean_label']}",
    ])
    writer.writerow([
        '商品已不可交易的关注', watch['stats']['unavailable_count'], '',
        f"占 {watch['stats']['unavailable_rate']}%，按当前状态回看，偏高",
    ])
    writer.writerow([
        '死信', watch['stats']['dead_letter_count'], '',
        '商品已恢复在售、关注却还挂着：这些人点了提醒、商品回来了、'
        '却永远不会被通知',
    ])
    if watch['stats']['is_small_sample']:
        writer.writerow([
            '样本不足', '', '', '',
            f"不足 {watch['min_measured_watches']} 条，占比只作参考",
        ])

    writer.writerow([])
    writer.writerow(['等得越久，是不是越可能是白等'])
    writer.writerow([
        '等了多久', '关注数', '占比（%）', '商品已不可交易', '占比（%）',
    ])
    shares = {row['label']: row['share'] for row in watch['stats']['rows']}
    for row in watch['stats']['status_rows']:
        writer.writerow([
            row['label'], row['count'],
            shares.get(row['label'], '—') if row['count'] else '—',
            row['unavailable_count'] if row['count'] else '—',
            row['unavailable_rate'] if row['unavailable_rate'] is not None else '—',
        ])

    writer.writerow([])
    writer.writerow(['一件商品上有多少人在等'])
    writer.writerow(['关注数', '商品数', '占比（%）', '说明'])
    for row in watch['item_watch_rows']:
        writer.writerow([
            row['label'], row['count'], row['share'], row['hint'],
        ])

    writer.writerow([])
    writer.writerow(['死信：商品已经恢复在售，人却永远不会被通知'])
    writer.writerow(['商品', '等了多久', '点下关注于'])
    for row in watch['dead_letter_rows']:
        writer.writerow([
            row['item_title'], row['wait_label'],
            row['created_at'].strftime('%Y-%m-%d %H:%M'),
        ])
    if watch['dead_letter_row_total'] > watch['dead_letter_row_limit']:
        writer.writerow([
            '清单最多列出', watch['dead_letter_row_limit'], '条',
            f"共 {watch['dead_letter_row_total']} 条",
        ])

    writer.writerow([])
    writer.writerow(['白等：商品已经不可交易，人还在等'])
    writer.writerow(['商品', '分类', '交易方式', '等了多久', '点下关注于'])
    for row in watch['long_wait_rows']:
        writer.writerow([
            row['item_title'], row['category_name'], row['trade_mode_label'],
            row['wait_label'], row['created_at'].strftime('%Y-%m-%d %H:%M'),
        ])
    if watch['long_wait_row_total'] > watch['long_wait_row_limit']:
        writer.writerow([
            '清单最多列出', watch['long_wait_row_limit'], '条',
            f"共 {watch['long_wait_row_total']} 条",
        ])

    writer.writerow([])
    writer.writerow(['有货提醒的触达质量'])
    writer.writerow(['指标', '数值', '说明'])
    writer.writerow([
        '周期内送达', watch['delivery']['measured_count'], '',
        'kind 为 item_available 的站内通知，不重复 notification_response 的时延统计',
    ])
    writer.writerow([
        '已读', watch['delivery']['read_count'], '',
        f"占 {watch['delivery']['read_rate']}%",
    ])
    writer.writerow([
        '送达时商品已不可交易', watch['delivery']['stale_count'], '',
        f"占 {watch['delivery']['stale_rate']}%，按当前状态回看，偏高",
    ])

    writer.writerow([])
    rating = dashboard['rating_quality']
    writer.writerow([
        '交易评价打了多少分，第一次被运营读到', '数值', '说明',
    ])
    writer.writerow([
        '口径与门槛', rating['summary'],
        '只读 Rating 的 score、comment 长度和 created_at，Order 的状态与创建时间，'
        'OrderEvent 里 to_status 为 completed / returned 的完成时刻，'
        '以及 MeetingIncident 的 status，不改写任何一条评价、不催评、'
        '不把评价写回任何人的信誉分、不读评价正文',
    ])
    writer.writerow([
        '周期内走完的交易', rating['coverage']['total'], '',
        '只收 completed / returned；还挂着未完结状态的订单是右删失样本，单独数出不混入',
    ])
    writer.writerow([
        '收到评价的交易', rating['coverage']['rated_count'], '',
        f"占 {rating['coverage']['rating_rate']}%",
    ])
    writer.writerow([
        '双方都评价', rating['coverage']['both_rated_count'], '',
        f"占 {rating['coverage']['both_rated_rate']}%，唯一健康的形状",
    ])
    writer.writerow([
        '只有一方评价', rating['coverage']['one_sided_count'], '',
        f"占 {rating['coverage']['one_sided_rate']}%，另一个人再也不会回来评这笔交易",
    ])
    writer.writerow([
        '双方都没评价', rating['coverage']['neither_rated_count'], '',
        f"占 {rating['coverage']['neither_rated_rate']}%，这笔交易在评价体系里是隐形的",
    ])
    if rating['coverage']['is_small_sample']:
        writer.writerow([
            '样本不足', '', '', '',
            f"不足 {rating['min_measured_orders']} 笔，占比只作参考",
        ])

    writer.writerow([])
    writer.writerow(['这笔交易有几个人被评价'])
    writer.writerow(['评价人数', '交易数', '占比（%）', '说明'])
    for row in rating['coverage']['rating_count_rows']:
        writer.writerow([row['label'], row['count'], row['share'], row['note']])

    writer.writerow([])
    writer.writerow(['这一方评没评，不能只看总数'])
    writer.writerow(['方向', '交易数', '占比（%）', '口径说明'])
    for key, label, count, share in (
        ('buyer', '买家评了卖家', rating['coverage']['buyer_rated_count'],
         rating['coverage']['buyer_rated_rate']),
        ('seller', '卖家评了买家', rating['coverage']['seller_rated_count'],
         rating['coverage']['seller_rated_rate']),
        ('both', '双方都评', rating['coverage']['both_rated_count'],
         rating['coverage']['both_rated_rate']),
        ('one_sided', '只有一方评', rating['coverage']['one_sided_count'],
         rating['coverage']['one_sided_rate']),
        ('neither', '双方都没评', rating['coverage']['neither_rated_count'],
         rating['coverage']['neither_rated_rate']),
    ):
        writer.writerow([
            label, count,
            share if rating['coverage']['total'] else '—',
            '只要有一个人评了就算「有评价」，另一个人可能永远不评'
            if key == 'one_sided' else
            ('这笔交易在评价体系里是隐形的' if key == 'neither' else ''),
        ])

    writer.writerow([])
    writer.writerow(['分打出来之后多久才提交'])
    writer.writerow(['提交间隔', '评价数', '占比（%）', '说明'])
    for row in rating['latency']['bucket_rows']:
        writer.writerow([row['label'], row['count'], row['share'], row['note']])
    writer.writerow([
        '中位 / P90 / 平均', rating['latency']['median_label'],
        rating['latency']['p90_label'], rating['latency']['average_label'],
        '订单完成时刻取 OrderEvent 里 to_status 为 completed / returned 的那一条',
    ])
    writer.writerow([
        '交易完成一个月以后才补的评价', rating['latency']['late_count'], '',
        f"占 {rating['latency']['late_rate']}%，此时记忆已经很不可靠",
    ])
    if rating['latency']['missing_event_count']:
        writer.writerow([
            '找不到完成事件的评价', rating['latency']['missing_event_count'], '',
            'OrderEvent 缺了这一笔，属于数据本身不完整，不计入时延分布',
        ])

    writer.writerow([])
    writer.writerow(['补得最晚的几笔评价'])
    writer.writerow(['订单', '商品', '评价人', '分数', '提交间隔', '提交于'])
    for row in rating['latency']['slowest_rows']:
        writer.writerow([
            row['order'].id, row['order'].item.title,
            row['rating'].rater.username, f"{row['rating'].score} 星",
            row['latency_label'],
            row['rating'].created_at.strftime('%Y-%m-%d %H:%M'),
        ])

    writer.writerow([])
    writer.writerow(['还没有人评价的交易'])
    writer.writerow(['订单', '商品', '交易方式', '下单于'])
    for row in rating['latency']['unrated_rows']:
        writer.writerow([
            row['order'].id, row['order'].item.title,
            row['order'].item.get_trade_mode_display(),
            row['order'].created_at.strftime('%Y-%m-%d %H:%M'),
        ])
    if rating['latency']['unrated_row_total'] > rating['latency']['unrated_row_limit']:
        writer.writerow([
            '清单最多列出', rating['latency']['unrated_row_limit'], '条', '',
            f"共 {rating['latency']['unrated_row_total']} 笔",
        ])

    writer.writerow([])
    writer.writerow(['五星分布'])
    writer.writerow(['分数', '评价数', '占比（%）', '口径说明'])
    for row in rating['score']['bucket_rows']:
        writer.writerow([row['label'], row['count'], row['share'], ''])
    writer.writerow([
        '均分', rating['score']['average'], '',
        'Avg 已在卖家信誉摘要里报过，这里只作参照，不合成任何好评率',
    ])
    writer.writerow([
        '两星及以下', rating['score']['low_count'], '',
        f"占 {rating['score']['low_rate']}%，一星和二星是这套评价里唯一真正重要的信号",
    ])

    writer.writerow([])
    writer.writerow(['有交付异常的交易，打分是不是不一样'])
    writer.writerow(['分组', '交易数', '评价数', '均分', '两星及以下占比（%）', '五星占比（%）'])
    for row in rating['incident_contrast']['rows']:
        writer.writerow([
            row['label'], row['order_count'], row['rating_count'],
            row['average'] if row['average'] is not None else '—',
            row['low_rate'] if row['rating_count'] else '—',
            row['top_rate'] if row['rating_count'] else '—',
        ])
    if rating['incident_contrast']['low_rate_gap'] is not None:
        writer.writerow([
            '两星及以下占比相差', rating['incident_contrast']['low_rate_gap'],
            '个百分点',
            '噪声门槛内不给方向' if rating['incident_contrast']['is_noise']
            else '超出噪声门槛，但只是对照，不构成因果',
        ])

    writer.writerow([])
    writer.writerow(['评价里写了文字的有多少'])
    writer.writerow(['文字长度', '评价数', '占比（%）', '口径说明'])
    for row in rating['comment']['length_rows']:
        writer.writerow([row['label'], row['count'], row['share'], ''])
    writer.writerow([
        '填写率', rating['comment']['filled_count'], '',
        f"占 {rating['comment']['fill_rate']}%，平均 {rating['comment']['average_length']} 字",
    ])
    if rating['comment']['average_gap'] is not None:
        writer.writerow([
            '写了文字的平均分减没写的', rating['comment']['average_gap'], '分',
            '噪声门槛内不给方向' if rating['comment']['is_noise'] else '超出噪声门槛',
        ])

    writer.writerow([])
    writer.writerow(['按交易方式拆开'])
    writer.writerow(['交易方式', '交易数', '评价数', '收到评价的交易', '占比（%）', '均分'])
    for row in rating['trade_mode']['rows']:
        writer.writerow([
            row['label'], row['order_count'], row['rating_count'],
            row['rated_count'], row['rating_rate'],
            row['average'] if row['average'] is not None else '—',
        ])

    writer.writerow([])
    dispute = dashboard['dispute_resolution_quality']
    writer.writerow([
        '交易争议处理得公不公平，第一次被运营读到', '数值', '说明',
    ])
    writer.writerow([
        '口径与门槛', dispute['summary'],
        '只读 OrderDispute 的 reason、status、created_at、resolved_at、resolution_note '
        '长度，OrderDisputeEvidence 的 created_at，以及 Order 的 buyer / seller 判断发起方向，'
        '不改写任何一条争议、任何一条证据、任何一份处理意见，'
        '不读 detail、note 和处理意见的任何一个字符',
    ])
    writer.writerow([
        '周期内已结案的争议', dispute['outcome']['closed_count'], '',
        '只收 resolved / rejected；还在 open / reviewing 的是右删失样本，单独数出不混入',
    ])
    writer.writerow([
        '驳回', dispute['outcome']['rejected_count'], '',
        f"占 {dispute['outcome']['rejected_rate']}%",
    ])
    if dispute['outcome']['censored_count']:
        writer.writerow([
            '还在处理中', dispute['outcome']['censored_count'], '',
            '右删失样本，不计入驳回率分母',
        ])
    if dispute['outcome']['is_small_sample']:
        writer.writerow([
            '样本不足', '', '',
            f"不足 {dispute['min_closed_disputes']} 条，占比只作参考",
        ])

    writer.writerow([])
    writer.writerow(['哪一类争议更容易被驳回'])
    writer.writerow(['争议类型', '已结案', '解决', '驳回', '驳回率（%）', '其中没带附件'])
    for row in dispute['outcome']['rows']:
        writer.writerow([
            row['label'], row['closed_count'], row['resolved_count'],
            row['rejected_count'], row['rejected_rate'], row['no_evidence_count'],
        ])
    writer.writerow([
        '不报一个总的驳回率', dispute['outcome']['rejected_rate'], '个百分点',
        '五种争议的举证难度完全不是一回事，平均成一个数之后，'
        '运营第一反应是去查处理人，而不是去查是哪一类争议在涨',
    ])

    writer.writerow([])
    writer.writerow(['证据什么时候才到手上'])
    writer.writerow(['提交间隔', '争议数', '占比（%）', '说明'])
    for row in dispute['evidence']['bucket_rows']:
        writer.writerow([row['label'], row['count'], row['share'], row['note']])
    writer.writerow([
        '中位 / P90 / 平均', dispute['evidence']['median_label'],
        dispute['evidence']['p90_label'], dispute['evidence']['average_label'],
        '按 OrderDisputeEvidence.created_at 减所属争议的 created_at 算，'
        '只报最早一条附件的到达时刻',
    ])
    writer.writerow([
        '带了附件的争议', dispute['evidence']['with_evidence_count'], '',
        f"占 {dispute['evidence']['evidence_rate']}%，附件本来就是可选的，"
        f"没带不是缺失数据",
    ])
    writer.writerow([
        '附件总数', dispute['evidence']['attachment_count'], '',
        '只报规模，不判断「证据够不够」——平台没有能力判断一段聊天记录'
        '能不能证明商品与描述不符',
    ])
    if dispute['evidence']['after_close_count']:
        writer.writerow([
            '结案之后才补的附件', dispute['evidence']['after_close_count'], '',
            f"占 {dispute['evidence']['after_close_rate']}%，没有参与这次裁定",
        ])

    writer.writerow([])
    writer.writerow(['带了附件的争议，是不是更不容易被驳回'])
    writer.writerow(['分组', '已结案', '驳回', '驳回率（%）'])
    for row in dispute['evidence_contrast']['rows']:
        writer.writerow([
            row['label'], row['closed_count'], row['rejected_count'], row['rejected_rate'],
        ])
    if dispute['evidence_contrast']['gap'] is not None:
        writer.writerow([
            '不带附件的驳回率减带附件的', dispute['evidence_contrast']['gap'], '个百分点',
            '噪声门槛内不给方向' if dispute['evidence_contrast']['is_noise']
            else '超出噪声门槛，但只是对照，不构成因果',
        ])
    if not dispute['evidence_contrast']['has_both_sides']:
        writer.writerow([
            '样本不足', '', '',
            f"两组各至少 {dispute['evidence_contrast']['min_closed']} 条才给方向",
        ])

    writer.writerow([])
    writer.writerow(['争议是谁发起的'])
    writer.writerow(['发起方', '发起数', '占比（%）', '已结案', '驳回', '驳回率（%）'])
    for row in dispute['opener']['rows']:
        writer.writerow([
            row['label'], row['opened_count'], row['opened_rate'],
            row['closed_count'], row['rejected_count'], row['rejected_rate'],
        ])
    writer.writerow([
        '只报方向，不按用户聚合', '', '',
        '不排名、不报任何人的用户名：方向分布报的是流程本身是否对双方可用，'
        '不是谁难相处',
    ])

    writer.writerow([])
    writer.writerow(['处理意见写了没有'])
    writer.writerow(['处理意见', '结案数', '占比（%）', '说明'])
    for row in dispute['note']['rows']:
        writer.writerow([row['label'], row['count'], row['share'], row.get('note', '')])
    writer.writerow([
        '填写率', dispute['note']['with_note_count'], '',
        f"占 {dispute['note']['fill_rate']}%，平均 {dispute['note']['average_length']} 字",
    ])
    writer.writerow([
        '驳回却一个字都没写', dispute['note']['rejected_without_note_count'], '',
        f"占没写处理意见的 {dispute['note']['rejected_without_note_rate']}%，"
        f"双方只收到状态，不知道为什么",
    ])

    writer.writerow([])
    writer.writerow([])
    writer.writerow(['处理意见的长度分档'])
    writer.writerow(['文字长度', '结案数', '占比（%）'])
    for row in dispute['note']['bucket_rows']:
        writer.writerow([row['label'], row['count'], row['share']])

    writer.writerow([])
    writer.writerow(['从发起到结案隔了多久'])
    writer.writerow(['结案耗时', '争议数', '占比（%）'])
    for row in dispute['resolution_latency']['bucket_rows']:
        writer.writerow([row['label'], row['count'], row['share']])
    writer.writerow([
        '中位 / P90 / 平均', dispute['resolution_latency']['median_label'],
        dispute['resolution_latency']['p90_label'], dispute['resolution_latency']['average_label'],
    ])
    if dispute['resolution_latency']['missing_resolved_at_count']:
        writer.writerow([
            '已结案但找不到处理时间', dispute['resolution_latency']['missing_resolved_at_count'], '',
            '属于数据本身不完整，不计入耗时分布',
        ])

    writer.writerow([])
    writer.writerow(['结案最慢的几条争议'])
    writer.writerow(['订单', '争议类型', '发起方', '结案耗时', '提交于'])
    for row in dispute['slowest_rows']['rows']:
        writer.writerow([
            row['dispute'].order.id,
            row['dispute'].get_reason_display(),
            '买家' if row['dispute'].opened_by_id == row['dispute'].order.buyer_id else '卖家',
            row['duration_label'],
            row['dispute'].created_at.strftime('%Y-%m-%d %H:%M'),
        ])
    if dispute['slowest_rows']['row_total'] > dispute['slowest_rows']['row_limit']:
        writer.writerow([
            '清单最多列出', dispute['slowest_rows']['row_limit'], '条',
            f"共 {dispute['slowest_rows']['row_total']} 条",
        ])

    writer.writerow([])
    lost_found = dashboard['lost_found_lead_quality']
    writer.writerow([
        '失物招领的线索，有多少条走到了「已确认」', '数值', '说明',
    ])
    writer.writerow([
        '口径与门槛', lost_found['summary'],
        '只读 LostFoundPost 的 post_type、status、created_at、identifying_features '
        '长度，LostFoundLead 的 status、created_at、message 长度，以及 '
        'MutualAidFeedback 的 outcome（只认 lost_found_lead 来源），'
        '不改写任何一条线索、任何一条记录、任何一份反馈，'
        '不读 message 和 identifying_features 的任何一个字符',
    ])
    writer.writerow([
        '周期内的失物招领记录', lost_found['lead_count']['total'], '',
        '按 LostFoundPost.created_at 切窗口',
    ])
    writer.writerow([
        '收到了线索的记录', lost_found['lead_count']['posts_with_lead_count'], '',
        f"占 {lost_found['lead_count']['posts_with_lead_rate']}%，"
        f"共 {lost_found['lead_count']['lead_count']} 条线索，"
        f"平均 {lost_found['lead_count']['leads_per_post']} 条/记录",
    ])
    if lost_found['lead_count']['is_small_sample']:
        writer.writerow([
            '样本不足', '', '', '',
            f"不足 {lost_found['min_measured_posts']} 条，占比只作参考",
        ])

    writer.writerow([])
    writer.writerow(['一条记录收到了几条线索'])
    writer.writerow(['线索数', '记录数', '占比（%）', '说明'])
    for row in lost_found['lead_count']['lead_count_rows']:
        writer.writerow([row['label'], row['count'], row['share'], row['note']])

    writer.writerow([])
    writer.writerow(['确认率按记录类型拆开'])
    writer.writerow(['记录类型', '记录数', '线索数', '确认了线索的记录', '确认率（%）', '已标记匹配'])
    for row in lost_found['confirm']['rows']:
        writer.writerow([
            row['label'], row['post_count'], row['lead_count'],
            row['accepted_count'], row['confirm_rate'], row['matched_post_count'],
        ])

    writer.writerow([])
    writer.writerow(['被驳回的线索，是发布者拒绝的还是顺带清掉的'])
    writer.writerow(['线索结局', '线索数', '占全部线索（%）', '说明'])
    for row in lost_found['rejection']['rows']:
        writer.writerow([row['label'], row['count'], row['share'], row['note']])
    writer.writerow([
        '被驳回的线索合计', lost_found['rejection']['rejected_count'], '',
        f"其中 {lost_found['rejection']['bulk_cleared_count']} 条"
        f"（{lost_found['rejection']['bulk_cleared_share_of_rejected']}%）是另一条被确认时"
        f"顺带清掉的，不是发布者拒绝的",
    ])
    writer.writerow([
        '发布者真的拒绝过线索的记录', lost_found['rejection']['posts_with_rejection_count'], '',
        f"占发起过线索的记录的 "
        f"{lost_found['rejection']['posts_with_rejection_rate']}%，"
        f"分母是 {lost_found['rejection']['posts_with_leads_count']} 条记录",
    ])

    writer.writerow([])
    writer.writerow(['发布者多久收到第一条线索'])
    writer.writerow(['提交间隔', '记录数', '占比（%）', '说明'])
    for row in lost_found['latency']['bucket_rows']:
        writer.writerow([row['label'], row['count'], row['share'], ''])
    writer.writerow([
        '中位 / P90 / 平均', lost_found['latency']['median_label'],
        lost_found['latency']['p90_label'], lost_found['latency']['average_label'],
        '按 LostFoundLead.created_at 减所属记录的 created_at 算，只报最早那一条',
    ])
    writer.writerow([
        '24 小时内收到第一条线索', lost_found['latency']['same_day_count'], '',
        f"占 {lost_found['latency']['same_day_rate']}%，"
        f"失物招领的时效窗口就在一天里",
    ])

    writer.writerow([])
    writer.writerow(['确认之后，东西找回来了没有'])
    writer.writerow(['互助结果', '记录数', '占已提交反馈（%）', '说明'])
    for row in lost_found['feedback']['rows']:
        writer.writerow([row['label'], row['count'], row['share'], ''])
    writer.writerow([
        '确认了线索的记录', lost_found['feedback']['accepted_count'], '',
        f"其中 {lost_found['feedback']['feedback_count']} 条提交了反馈"
        f"（{lost_found['feedback']['feedback_rate']}%）",
    ])
    if lost_found['feedback']['without_feedback_count']:
        writer.writerow([
            '已确认但没回来填反馈', lost_found['feedback']['without_feedback_count'], '',
            lost_found['feedback']['without_feedback_note'],
        ])

    writer.writerow([])
    writer.writerow(['线索说明的长度分档'])
    writer.writerow(['文字长度', '线索数', '占比（%）', '口径说明'])
    for row in lost_found['message']['length_rows']:
        writer.writerow([row['label'], row['count'], row['share'], ''])
    writer.writerow([
        '填写率', lost_found['message']['filled_count'], '',
        f"占 {lost_found['message']['fill_rate']}%，平均 "
        f"{lost_found['message']['average_length']} 字，一个字符的内容都不读",
    ])
    writer.writerow([
        '填了辨识特征的记录', lost_found['message']['feature_filled_count'], '',
        f"占 {lost_found['message']['feature_fill_rate']}%，颜色品牌贴纸挂件是给双方核对的",
    ])

    writer.writerow([])
    writer.writerow(['还挂着没人处理的线索'])
    writer.writerow(['记录', '线索提供者', '提交于', '记录发布于'])
    for row in lost_found['pending_rows']['rows']:
        writer.writerow([
            row['post'].title, row['lead'].respondent.username,
            row['lead'].created_at.strftime('%Y-%m-%d %H:%M'),
            row['post'].created_at.strftime('%Y-%m-%d %H:%M'),
        ])
    if lost_found['pending_rows']['row_total'] > lost_found['pending_rows']['row_limit']:
        writer.writerow([
            '清单最多列出', lost_found['pending_rows']['row_limit'], '条', '',
            f"共 {lost_found['pending_rows']['row_total']} 条",
        ])

    conversion = dashboard['demand_response_conversion']
    writer.writerow([])
    writer.writerow([
        '确认了求购响应之后，交易真的发生了吗', '数值', '说明',
    ])
    writer.writerow(['口径与门槛'])
    writer.writerow([
        conversion['summary'],
        '只读 DemandResponse 的 status、updated_at、message 长度，'
        'DemandPost 的 status，Item 的 is_available_now、trade_mode，'
        '以及 Order 的 item_id、buyer_id、created_at、status，'
        '不改写任何一条响应、任何一条求购、任何一笔订单，'
        '不自动建订单、不把 fulfilled 的写入时机推到订单完成之后，'
        '不按用户做任何聚合或排名',
    ])
    writer.writerow([
        '周期内确认的求购响应', conversion['conversion']['total'], '',
        '按 DemandResponse.updated_at 切窗口，即 accept 那一刻，'
        'withdrawn 不进分母',
    ])
    writer.writerow([
        '之后真的变成了订单', conversion['conversion']['converted_count'], '',
        f"转化率 {conversion['conversion']['conversion_rate']}%，"
        f"分母是已确认响应数",
    ])
    if conversion['conversion']['is_small_sample']:
        writer.writerow([
            '样本不足', '', '', '',
            f"不足 {conversion['conversion']['min_measured_responses']} 条，"
            f"比例只作参考",
        ])
    writer.writerow([
        '订单最后成交了', conversion['dealt']['dealt_count'], '',
        f"占转化订单 {conversion['dealt']['dealt_rate']}%，"
        f"completed 和 returned 都算成交",
    ])
    writer.writerow([
        '订单被取消', conversion['dealt']['cancelled_count'], '',
        f"占转化订单 {conversion['dealt']['cancelled_rate']}%，"
        f"取消是确认之后的一次真失败，单独报",
    ])
    writer.writerow([
        '订单还在进行中', conversion['dealt']['still_open_count'], '',
        f"占转化订单 {conversion['dealt']['still_open_rate']}%",
    ])
    writer.writerow([
        '涉及多少条求购', conversion['conversion']['demand_count'], '',
        f"其中 {conversion['conversion']['demand_with_order_count']} 条求购"
        f"名下出现了订单，两者不相等说明有并发确认",
    ])

    writer.writerow([])
    writer.writerow(['确认之后，订单最后怎么样了'])
    writer.writerow(['订单结局', '笔数', '占转化订单（%）', '说明'])
    writer.writerow([
        '已成交', conversion['dealt']['dealt_count'],
        conversion['dealt']['dealt_rate'],
        'completed 和 returned 都算成交，借用走到 returned 是一次完整闭环',
    ])
    writer.writerow([
        '被取消', conversion['dealt']['cancelled_count'],
        conversion['dealt']['cancelled_rate'],
        '确认之后的一次真失败，独立一类，只报规模不猜原因',
    ])
    writer.writerow([
        '还在进行中', conversion['dealt']['still_open_count'],
        conversion['dealt']['still_open_rate'], '周期结束时还没走完所属流程',
    ])

    writer.writerow([])
    writer.writerow(['从确认到下单，隔了多久'])
    writer.writerow(['间隔', '订单数', '占比（%）', '说明'])
    for row in conversion['latency']['bucket_rows']:
        writer.writerow([row['label'], row['count'], row['share'], row['note']])
    writer.writerow([
        '中位 / P90 / 平均', conversion['latency']['median_label'],
        conversion['latency']['p90_label'], conversion['latency']['average_label'],
        '按 Order.created_at 减确认时刻算，只报有订单的那些',
    ])
    writer.writerow([
        '当天内走完', conversion['latency']['same_day_count'], '',
        f"占 {conversion['latency']['same_day_rate']}%",
    ])

    writer.writerow([])
    writer.writerow(['转化率按交易方式拆开'])
    writer.writerow(['交易方式', '已确认响应', '变成了订单', '转化率（%）', '样本是否足够'])
    for row in conversion['trade_modes']['rows']:
        writer.writerow([
            row['label'], row['total'], row['converted_count'],
            row['conversion_rate'],
            '' if not row['is_small_sample'] else
            f"不足 {conversion['conversion']['min_measured_responses']} 条",
        ])

    writer.writerow([])
    writer.writerow(['确认之后没有订单的，卡在哪'])
    writer.writerow(['卡点', '响应数', '占未转化（%）', '说明'])
    for row in conversion['blocked']['rows']:
        writer.writerow([row['label'], row['count'], row['share'], ''])
    writer.writerow([
        '卡点合计', conversion['blocked']['total'], '',
        f"其中 {conversion['blocked']['still_walking_count']} 条"
        f"（{conversion['blocked']['still_walking_rate']}%）是「还在路上」："
        f"商品在、求购在、两边都没动",
    ])

    writer.writerow([])
    writer.writerow(['响应说明写了没写'])
    writer.writerow(['文字长度', '响应数', '占填写了的（%）', '说明'])
    for row in conversion['message']['length_rows']:
        writer.writerow([row['label'], row['count'], row['share'], ''])
    writer.writerow([
        '填写率', conversion['message']['filled_count'], '',
        f"占 {conversion['message']['fill_rate']}%，平均 "
        f"{conversion['message']['average_length']} 字，一个字符的内容都不读",
    ])
    writer.writerow([
        '写了说明 vs 没写', conversion['message']['with_message_rate'], '',
        f"没写的 {conversion['message']['without_message_rate']}%，"
        f"相差 {conversion['message']['gap_points']} 个百分点",
    ])
    if not conversion['message']['is_measurable']:
        writer.writerow([
            '样本不足', '', '', '',
            '两组各需至少 12 条才给方向，差值不满 8 个百分点视为噪声',
        ])
    elif not conversion['message']['gap_is_meaningful']:
        writer.writerow([
            '差值视为噪声', '', '', '',
            f"相差 {conversion['message']['gap_points']} 个百分点，"
            f"不足 {DEMAND_RESPONSE_NOISE_POINTS} 个百分点的模块阈值，不给方向",
        ])

    writer.writerow([])
    writer.writerow(['确认之后悬着没下单的清单'])
    writer.writerow(['响应商品', '求购', '确认于', '卡点', '响应说明字数'])
    for row in conversion['stuck_rows']['rows']:
        writer.writerow([
            row.response.item.title, row.response.demand.title,
            row.response.updated_at.strftime('%Y-%m-%d %H:%M'),
            row.blocked_label,
            len((row.response.message or '').strip()),
        ])
    if conversion['stuck_rows']['row_total'] > conversion['stuck_rows']['row_limit']:
        writer.writerow([
            '清单最多列出', conversion['stuck_rows']['row_limit'], '条', '',
            f"共 {conversion['stuck_rows']['row_total']} 条",
        ])

    writer.writerow(['运营提醒', '级别', '指标', '说明', '建议动作'])
    if dashboard['operational_alerts']:
        for alert in dashboard['operational_alerts']:
            writer.writerow([
                alert['title'], alert['severity_label'],
                f"{alert['metric_label']}：{alert['metric']}",
                alert['message'], alert['action_label'],
            ])
    else:
        writer.writerow(['暂无高优先级提醒', '', '', '当前周期未触发运营提醒规则', ''])

    writer.writerow([])
    writer.writerow(['周期对比', '当前周期', '上一周期', '变化'])
    for row in dashboard['period_comparisons']:
        writer.writerow([row['label'], row['current'], row['previous'], row['change_display']])

    writer.writerow([])
    writer.writerow(['活跃用户分层', '人数', '占活跃用户（%）', '识别口径'])
    for row in dashboard['activity_segments']:
        writer.writerow([row['label'], row['count'], row['share'], row['note']])

    writer.writerow([])
    writer.writerow(['用户回访率', '人数', '比例（%）', '口径说明'])
    writer.writerow([
        '上一周期新用户', dashboard['user_retention']['cohort_size'], '', '上一周期内注册的新用户',
    ])
    writer.writerow([
        '回访用户', dashboard['user_retention']['retained_users'],
        dashboard['user_retention']['rate'], dashboard['user_retention']['note'],
    ])

    writer.writerow([])
    writer.writerow(['用户行为转化漏斗', '数量', '相对上一步转化率（%）', '口径说明'])
    for stage in dashboard['conversion_funnel']:
        writer.writerow([stage['label'], stage['count'], stage['rate'], stage['note']])

    writer.writerow([])
    writer.writerow(['每日活动趋势', '发布商品', '搜索次数', '交易预约', '活动总量'])
    for point in dashboard['activity_trend']:
        writer.writerow([
            point['date'], point['items'], point['searches'], point['orders'], point['total'],
        ])

    writer.writerow([])
    writer.writerow(['交易状态分布', '数量'])
    for row in dashboard['order_statuses']:
        writer.writerow([row['label'], row['count']])

    health = dashboard['order_health']
    writer.writerow([])
    writer.writerow(['交易健康度', '数值'])
    writer.writerow(['取消率（%）', health['cancellation_rate']])
    writer.writerow(['超时待确认', health['overdue_pending_orders']])
    writer.writerow(['完成率（%）', health['completion_rate']])
    writer.writerow(['平均确认耗时（小时）', '' if health['average_confirmation_hours'] is None else health['average_confirmation_hours']])
    writer.writerow(['风险等级', health['risk_label']])
    writer.writerow(['风险说明', health['risk_message']])

    stage_flow = dashboard['order_stage_flow']
    writer.writerow([])
    writer.writerow(['订单全链路阶段时长', '数值'])
    writer.writerow(['周期订单数', stage_flow['summary']['total_orders']])
    writer.writerow(['可测量订单数', stage_flow['summary']['measured_orders']])
    writer.writerow(['可测量占比（%）', stage_flow['summary']['measured_share']])
    writer.writerow(['仍在流程中', stage_flow['summary']['censored_orders']])
    writer.writerow(['未确认订单', stage_flow['summary']['unconfirmed_orders']])
    writer.writerow(['异常剔除样本', stage_flow['summary']['dropped_samples']])
    writer.writerow(['端到端中位耗时', stage_flow['summary']['end_to_end_median_label']])
    writer.writerow(['端到端 P90', stage_flow['summary']['end_to_end_p90_label']])
    if stage_flow['bottleneck']:
        writer.writerow(['瓶颈阶段', stage_flow['bottleneck']['label']])
        writer.writerow(['瓶颈阶段中位耗时', stage_flow['bottleneck']['median_label']])
        writer.writerow(['瓶颈阶段样本', stage_flow['bottleneck']['sample_size']])
        writer.writerow(['瓶颈占可测量总耗时（%）', stage_flow['bottleneck']['share_of_measured_total']])

    writer.writerow([])
    writer.writerow(['阶段', '中位耗时', 'P90', '平均耗时', '最快', '最慢', '样本数', '进行中', '是否为瓶颈'])
    for row in stage_flow['stage_rows']:
        writer.writerow([
            row['label'], row['median_label'], row['p90_label'], row['average_label'],
            row['fastest_label'], row['slowest_label'], row['sample_size'],
            row['still_in_stage'], '是' if row['is_bottleneck'] else '',
        ])

    for facet in stage_flow['facet_rows']:
        writer.writerow([])
        writer.writerow([f"按{facet['label']}拆分阶段耗时", '订单数', '可测量订单', '最慢阶段', '该阶段中位耗时', '端到端中位耗时', '异常剔除样本'])
        for row in facet['rows']:
            writer.writerow([
                row['name'], row['order_count'], row['measured_orders'], row['worst_stage_label'],
                row['worst_stage_median_label'], row['end_to_end_median_label'], row['dropped_samples'],
            ])

    if stage_flow['recommendations']:
        writer.writerow([])
        writer.writerow(['运营建议'])
        for recommendation in stage_flow['recommendations']:
            writer.writerow([recommendation])
    writer.writerow([])
    writer.writerow(['热门搜索', '次数', '无结果', '平均结果', '无结果占比（%）'])
    for search in dashboard['top_searches']:
        zero_share = (
            search['zero_result_count'] / search['search_count'] * 100
            if search['search_count'] else None
        )
        writer.writerow([
            search['query'], search['search_count'], search['zero_result_count'],
            None if search['average_results'] is None else round(search['average_results'], 1),
            '' if zero_share is None else round(zero_share, 1),
        ])
    if not dashboard['top_searches']:
        writer.writerow(['当前周期还没有搜索记录。', '', '', '', ''])

    escalation = dashboard['borrow_escalation']
    writer.writerow([])
    writer.writerow(['借用逾期催收阶梯'])
    writer.writerow([
        '回看天数', escalation['days'],
        '待催收', escalation['summary']['open_count'],
        '其中已上报治理队列', escalation['summary']['level_three_count'],
        '周期内升级', escalation['summary']['period_count'],
        '周期内闭环', escalation['summary']['resolved_count'],
        '最长逾期（天）', '' if escalation['summary']['max_overdue_days'] is None else escalation['summary']['max_overdue_days'],
        '累计升级次数', escalation['summary']['escalation_total'],
    ])
    writer.writerow([])
    writer.writerow(['商品与订单', '借用双方', '逾期（天）', '当前等级', '升级次数', '最近升级', '归还闭环', '状态'])
    for row in escalation['rows']:
        order = row['order']
        writer.writerow([
            f"{order.item.title} · 订单 #{order.id} · {order.item.get_trade_mode_display()}",
            f"借用 {order.buyer.username} / 出借 {order.seller.username}",
            '' if row['overdue_days'] is None else row['overdue_days'],
            row['level_label'],
            row['escalation_count'],
            '' if row['last_escalated_at'] is None else f"{timezone.localtime(row['last_escalated_at']):%Y-%m-%d %H:%M}",
            '' if row['resolved_at'] is None else f"{timezone.localtime(row['resolved_at']):%Y-%m-%d}",
            '跟进中' if row['is_open'] else '已闭环',
        ])
    if not escalation['rows']:
        writer.writerow(['当前没有需要催收的借用逾期。', '', '', '', '', '', '', ''])
    writer.writerow([
        '三级阶梯各有不同受众：一级只告诉借用方，二级同时告知双方平台已开始记录，三级上报治理队列并离开双方私域；'
        '跨级追上来时会补发二级那一声，但不会补播被跳过的一级。平台只记录和通知，不取消订单、不代扣押金。',
    ])

    writer.writerow([])
    writer.writerow(['分类与地点'])
    writer.writerow(['分类', '周期内新增', '当前在售'])
    for row in dashboard['category_stats']:
        writer.writerow([row.name, row.new_count, row.available_count])

    writer.writerow([])
    writer.writerow(['地点', '周期内新增商品', '周期内交易预约'])
    for row in dashboard['location_stats']:
        writer.writerow([row.name, row.new_count, row.order_count])

    supply = dashboard['supply_lifecycle']
    writer.writerow([])
    writer.writerow(['供给侧生命周期健康', '数值'])
    writer.writerow(['当前在售商品', supply['summary']['available_items']])
    writer.writerow(['需要卖家决策', supply['summary']['attention_items']])
    writer.writerow(['待处理占比（%）', supply['summary']['attention_share']])
    writer.writerow(['发布超 45 天无互动', supply['summary']['stale_unengaged']])
    writer.writerow(['周期内刷新次数', supply['summary']['period_refreshes']])
    writer.writerow(['周期内刷新卖家数', supply['summary']['period_refreshed_sellers']])
    writer.writerow(['有在售商品的卖家', supply['summary']['sellers_with_listings']])
    writer.writerow(['无待处理商品的卖家', supply['summary']['sellers_keeping_up']])
    writer.writerow(['卖家维护覆盖率（%）', supply['summary']['seller_coverage']])
    writer.writerow(['刷新额度已用尽卖家', supply['summary']['exhausted_sellers']])
    writer.writerow(['名下商品全部待处理卖家', supply['summary']['neglected_sellers']])

    writer.writerow([])
    writer.writerow(['生命周期等级', '商品数', '占在售（%）'])
    for row in supply['level_rows']:
        writer.writerow([row['label'], row['count'], row['share']])

    writer.writerow([])
    writer.writerow(['在管规模', '卖家数', '在售商品', '待处理商品', '无互动商品', '额度用尽卖家'])
    for row in supply['seller_load_rows']:
        writer.writerow([row['label'], row['seller_count'], row['active_count'],
                         row['attention_count'], row['unengaged_count'], row['exhausted_seller_count']])

    if supply['recommendations']:
        writer.writerow([])
        writer.writerow(['供给侧运营建议'])
        for recommendation in supply['recommendations']:
            writer.writerow([recommendation])

    refresh_ledger = dashboard['item_refresh_event_ledger']
    writer.writerow([])
    writer.writerow(['商品刷新事件台账', '数值', '说明'])
    writer.writerow(['口径', refresh_ledger['summary'], refresh_ledger['source_note']])
    writer.writerow([
        '回看天数', refresh_ledger['days'],
        f"从 {timezone.localtime(refresh_ledger['window_start']):%Y-%m-%d %H:%M} 到 "
        f"{timezone.localtime(refresh_ledger['window_end']):%Y-%m-%d %H:%M}",
    ])
    writer.writerow(['周期内事件', refresh_ledger['event_count'], 'GET 与被限流拒绝的请求不写入'])
    writer.writerow(['事件口径商品数', refresh_ledger['item_count'], '按商品去重'])
    writer.writerow(['事件口径卖家数', refresh_ledger['seller_count'], '按卖家去重，不输出身份'])
    writer.writerow([
        '首 / 末事件',
        '' if refresh_ledger['first_event_at'] is None else f"{timezone.localtime(refresh_ledger['first_event_at']):%Y-%m-%d %H:%M}",
        '' if refresh_ledger['last_event_at'] is None else f"{timezone.localtime(refresh_ledger['last_event_at']):%Y-%m-%d %H:%M}",
    ])
    writer.writerow([
        '累计刷新商品', refresh_ledger['coverage']['counted_item_count'],
        '覆盖口径分母；从未刷新过的商品不进入',
    ])
    writer.writerow([
        '完整事件覆盖', refresh_ledger['coverage']['full_count'],
        '' if refresh_ledger['coverage']['full_coverage_rate'] is None else f"覆盖率 {refresh_ledger['coverage']['full_coverage_rate']}%",
    ])
    writer.writerow([
        '只有历史字段', refresh_ledger['coverage']['historical_only_count'],
        '台账上线前刷过、之后未再刷，不回填',
    ])
    writer.writerow([
        '有历史缺口', refresh_ledger['coverage']['legacy_gap_count'],
        f"缺口 {refresh_ledger['coverage']['legacy_missing_total']} 次；这是留存边界，不是错误",
    ])
    writer.writerow([
        '序列不一致', refresh_ledger['coverage']['inconsistent_count'],
        f"最新计数不一致 {refresh_ledger['coverage']['latest_mismatch_count']} 件；序号断档 {refresh_ledger['coverage']['sequence_gap_count']} 件",
    ])
    writer.writerow([
        '未来时间事件', refresh_ledger['future_event_count'],
        f"容忍 {refresh_ledger['future_tolerance_minutes']} 分钟时钟偏差",
    ])
    writer.writerow(['说明', refresh_ledger['history_note'], '只读，不修复、不清零、不排名'])

    writer.writerow([])
    writer.writerow(['日期', '事件数', '商品数', '卖家数'])
    for row in refresh_ledger['daily_rows']:
        writer.writerow([row['day'], row['event_count'], row['item_count'], row['seller_count']])
    if refresh_ledger['daily_rows_truncated']:
        writer.writerow([
            '截断说明', refresh_ledger['daily_row_count'], refresh_ledger['daily_row_limit'],
            '页面与 CSV 都保留最近这些天数',
        ])

    refresh = dashboard['refresh_quota_drift']
    writer.writerow([])
    writer.writerow(['刷新额度为什么是一次性的', '数值', '说明'])
    writer.writerow([
        '口径与门槛', refresh['summary'],
        '只读 Item.refresh_count 与 last_refreshed_at，复用 _refresh_state 与 _level_for '
        '的判据，不清零任何字段、不改任何一条限流、不动那条「本月」文案',
    ])
    writer.writerow([
        '上限实际按什么算', '累计，不是按月',
        f"refresh_count 从不清零，所以上限实际是「发布至今最多 "
        f"{refresh['max_refresh_per_month']} 次」，而文案写的是「本月」",
    ])
    writer.writerow([
        '在售商品', refresh['quota']['live_count'], '',
        '分母。已下架、已成交的商品单独数，不混进来',
    ])
    writer.writerow([
        '额度已用尽', refresh['quota']['exhausted_live_count'], '',
        f"占在售 {refresh['quota']['exhausted_share']}%",
    ])
    writer.writerow([
        '其中被累计口径吃掉', refresh['quota']['drifted_count'], '',
        f"占已用尽 {refresh['quota']['drifted_share']}%，"
        f"判据是最后一次刷新已超过 {refresh['quota_month_days']} 天",
    ])
    writer.writerow([
        '非在售但额度用尽', refresh['quota']['exhausted_ended_count'], '',
        '生命周期已结束，「还能不能刷」对它们没有意义，单独数',
    ])

    writer.writerow([])
    writer.writerow(['在售商品里，额度卡在哪一档'])
    writer.writerow(['分组', '商品数', '占在售（%）'])
    writer.writerow(['在售商品（分母）', refresh['quota']['live_count'], ''])
    writer.writerow(['额度已用尽', refresh['quota']['exhausted_live_count'], refresh['quota']['exhausted_share']])
    writer.writerow(['其中：最后刷新在 ' + str(refresh['quota_month_days']) + ' 天内', refresh['quota']['exhausted_fresh_count'], ''])
    writer.writerow(['其中：被累计口径吃掉', refresh['quota']['drifted_count'], refresh['quota']['drifted_share']])
    writer.writerow(['非在售但额度也用尽', refresh['quota']['exhausted_ended_count'], ''])

    writer.writerow([])
    writer.writerow(['本周期撞上额度墙的商品'])
    writer.writerow(['指标', '数值', '口径说明'])
    writer.writerow([
        '本周期刷新过的商品', refresh['wall']['refreshed_in_period'], '',
        '分母。本周期一次都没刷过的商品不可能本周期撞墙',
    ])
    writer.writerow([
        '其中撞上额度墙', refresh['wall']['hit_wall_count'], '',
        f"占 {refresh['wall']['hit_wall_share']}%",
    ])
    writer.writerow([
        '这个数为什么精确', '', '',
        '限流判据是 refresh_count >= 4，而 refresh_count 只会被加一，'
        '所以它最多到 4、永远不可能到 5；于是 count 等于 4 时 '
        'last_refreshed_at 就是撞墙那一刻，不需要历史表',
    ])

    writer.writerow([])
    writer.writerow(['额度用尽的商品，距上次刷新多久'])
    writer.writerow(['距上次刷新', '商品数', '占额度用尽（%）'])
    for row in refresh['since_refresh']['bucket_rows']:
        writer.writerow([row['label'], row['count'], row['share']])
    writer.writerow([
        '超过一个月的合计', refresh['since_refresh']['over_month_count'],
        refresh['since_refresh']['over_month_share'],
        '这三档就是「本月 4 次」这句文案说谎的范围',
    ])

    writer.writerow([])
    writer.writerow(['卡在 24 小时间隔上的商品'])
    writer.writerow(['指标', '数值', '口径说明'])
    writer.writerow([
        '在售商品', refresh['cooldown']['live_count'], '',
        '分母',
    ])
    writer.writerow([
        '卡在间隔上', refresh['cooldown']['cooldown_count'], '',
        f"占 {refresh['cooldown']['cooldown_share']}%",
    ])
    writer.writerow([
        '为什么要和额度分开报', '', '',
        '两条限流给用户的提示长得一样，但这一条按相对时间算、不吃累计口径，'
        '它不会犯「本月」这个错。运营要改的是另一条',
    ])

    writer.writerow([])
    writer.writerow(['被累计口径吃掉的商品，现在什么状态'])
    writer.writerow(['生命周期等级', '商品数', '占被吃掉（%）'])
    for row in refresh['drifted_state']['rows']:
        writer.writerow([row['label'], row['count'], row['share']])

    writer.writerow([])
    writer.writerow(['被累计口径吃掉的商品清单'])
    writer.writerow(['商品', '刷新次数', '距上次刷新（天）', '沉底天数', '生命周期等级'])
    for row in refresh['drift_list']['rows']:
        writer.writerow([
            row['title'], row['refresh_count'], row['since_refresh_days'],
            row['age_days'], row['level'],
        ])
    if not refresh['drift_list']['rows']:
        writer.writerow(['—', '当前没有被累计口径吃掉的商品', '', '', ''])
    if refresh['drift_list']['truncated']:
        writer.writerow([
            '—', f"清单最多列 {len(refresh['drift_list']['rows'])} 条，完整的在数据库里",
            '', '', '',
        ])

    writer.writerow([])
    writer.writerow([
        '说明',
        '本模块只报告，不改 _refresh_state 的任何一条判据、不清零 refresh_count、'
        '不加刷新历史表、不动那条「本月刷新次数已达上限」的文案。'
        '文案和代码不一致是真的，但先有数再决定改哪一边：如果只有个位数，'
        '改一句话就够；如果是成百上千件沉底商品，那它值得一张刷新历史表。',
    ])

    search_pairing = dashboard['search_impression_pairing_quality']
    writer.writerow([])
    writer.writerow(['搜索曝光与点击配对质量'])
    writer.writerow([])
    writer.writerow(['口径说明'])
    writer.writerow(['指标', '数值', '说明'])
    for label, value, note in (
        ('回看天数', search_pairing['period_days'], '本地自然日边界，主统计只读到当前时刻'),
        ('允许配对的范围', '曝光表上线以来', '周期内点击可以配对周期外更早的同搜索同商品曝光'),
        ('用户比较', '不比较', '链接可以转发或登录状态变化，配对只看搜索、商品与时间'),
        ('位置比较', '任一更早曝光位置', '没有请求 ID，不伪装能定位某一次渲染'),
        ('每页结果数', search_pairing['metrics']['search_page_size'], '只作为当前搜索页契约说明，不用它倒推缺几张'),
    ):
        writer.writerow([label, value, note])

    writer.writerow([])
    writer.writerow(['字段与结构'])
    writer.writerow(['检查对象', '行数', '状态', '怎么读'])
    for row in search_pairing['field_rows']:
        writer.writerow([row['label'], row['count'], row['status'], row['note']])

    writer.writerow([])
    writer.writerow(['配对与延迟'])
    writer.writerow(['延迟档', '点击数', '占比（%）'])
    for row in search_pairing['latency_rows']:
        writer.writerow([
            row['label'], row['count'],
            row['share'] if row['share'] is not None else '—',
        ])

    writer.writerow([])
    writer.writerow(['覆盖'])
    writer.writerow(['指标', '数值', '说明'])
    summary = search_pairing['summary']
    for label, value, note in (
        ('周期内曝光', summary['total_impressions'], '主统计不含未来时间'),
        ('周期内点击', summary['total_clicks'], '主统计不含未来时间'),
        ('可配对点击', summary['matched_click_count'], '同一搜索、同一商品，且曝光不晚于点击'),
        ('可配对点击占比（%）', summary['matched_click_share'] if summary['matched_click_share'] is not None else '—', '分母是周期内全部点击'),
        ('位置一致点击', summary['position_match_count'], '点击位置出现在任一更早同组曝光里'),
        ('位置不一致点击', summary['position_mismatch_count'], '零位置点击不进入这张位置一致性分母'),
        ('至少被一次点击配上的曝光', summary['paired_impression_count'], '同一曝光可以被多次点击引用，这里去重'),
        ('覆盖自然日', summary['calendar_days'], '从周期开始到当前时刻'),
        ('曝光有写入自然日', summary['impression_active_days'], '至少一行曝光'),
        ('点击有写入自然日', summary['click_active_days'], '至少一行点击'),
        ('任一事件有写入自然日', summary['either_active_days'], '两类事件取并集'),
        ('无写入自然日', summary['zero_days'], '假期、低流量或无搜索都可能出现，不判写入失败'),
    ):
        writer.writerow([label, value, note])

    writer.writerow([])
    writer.writerow(['说明'])
    writer.writerow([search_pairing['note']])

    cold_start = dashboard['search_exposure_cold_start']
    writer.writerow([])
    writer.writerow(['新商品搜索曝光冷启动'])
    writer.writerow([])
    writer.writerow(['口径说明'])
    writer.writerow(['指标', '数值', '说明'])
    for label, value, note in (
        ('回看天数', cold_start['period_days'], '本地自然日边界；队列只收周期内发布的商品'),
        (
            '观察缓冲', f"{cold_start['observation_hours']} 小时",
            '发布未满该时长的商品只登记为未成熟，不进入冷启动分母',
        ),
        (
            '队列截止', timezone.localtime(cold_start['observation_cutoff']).strftime('%Y-%m-%d %H:%M'),
            '避免刚发布商品因为还没有时间被搜索看见而误判为未曝光',
        ),
        ('商品状态', '全部状态', '商品即使很快售出，发布后是否被搜索看见仍是有效事实'),
        ('首次曝光', '发布后最早一条 SearchImpression', '曝光早于发布只报形状，不生成负延迟'),
        ('首次点击', '首次曝光后的最早 SearchClick', '点击早于首曝、有点击但无曝光都只报形状'),
        ('搜索归因', '不要求', '本面板问是否出现在结果页，SearchImpression.search_query 为空仍算曝光'),
    ):
        writer.writerow([label, value, note])

    writer.writerow([])
    writer.writerow(['首曝延迟分桶'])
    writer.writerow(['首曝延迟分桶', '商品数', '占比（%）'])
    for row in cold_start['latency_rows']:
        writer.writerow([
            row['label'], row['count'],
            row['share'] if row['share'] is not None else '—',
        ])

    writer.writerow([])
    writer.writerow(['分类冷启动'])
    writer.writerow([
        '分类冷启动', '队列商品', '已首曝', '未首曝', '首曝后点击',
        '中位等待（小时）', 'P90 等待（小时）', '未首曝占比（%）',
    ])
    for row in cold_start['category_rows']:
        writer.writerow([
            row['category_name'], row['cohort_count'], row['exposed_count'],
            row['unexposed_count'], row['clicked_after_exposure_count'],
            row['median_first_exposure_hours'] if row['median_first_exposure_hours'] is not None else '—',
            row['p90_first_exposure_hours'] if row['p90_first_exposure_hours'] is not None else '—',
            row['unexposed_share'] if row['unexposed_share'] is not None else '—',
        ])

    writer.writerow([])
    writer.writerow(['新商品清单'])
    writer.writerow([
        '新商品清单', '分类', '交易方式', '状态', '发布时间', '首曝时间',
        '首曝延迟（小时）', '点击', '信号',
    ])
    for row in cold_start['item_rows']:
        if row['has_click_after_exposure']:
            click_state = '首曝后有点击'
        elif not row['has_exposure'] and row['has_click']:
            click_state = '有点击但无曝光'
        elif row['early_click_count']:
            click_state = '有早于首曝的点击'
        else:
            click_state = '无'
        writer.writerow([
            row['item_id'], row['category_name'], row['trade_mode_label'],
            row['status_label'],
            timezone.localtime(row['created_at']).strftime('%Y-%m-%d %H:%M'),
            (
                timezone.localtime(row['first_exposure_at']).strftime('%Y-%m-%d %H:%M')
                if row['first_exposure_at'] else '—'
            ),
            f"{row['first_exposure_hours']:g}" if row['first_exposure_hours'] is not None else '—',
            click_state, row['shape'],
        ])
    if not cold_start['item_rows']:
        writer.writerow(['—', '暂无可观察商品', '', '', '', '', '', '', ''])

    writer.writerow([])
    writer.writerow(['边界说明'])
    writer.writerow(['项目', '约束'])
    writer.writerow(['只读', '不调搜索排序、不自动置顶、不给商品打分，也不触发卖家提醒'])
    writer.writerow(['隐私', '不输出商品标题、描述、价格、卖家、搜索词或用户'])
    writer.writerow(['解释', '未曝光可能来自低搜索量、分类冷门或学期节奏；先看形状，不做因果判断'])

    refresh_response = dashboard['refresh_response']
    writer.writerow([])
    writer.writerow(['商品刷新后的搜索回响'])
    writer.writerow([])
    writer.writerow(['口径说明'])
    writer.writerow(['字段', '数值', '说明'])
    writer.writerow([
        '回看天数', refresh_response['period_days'],
        f"看板周期从 {timezone.localtime(refresh_response['period_start']):%Y-%m-%d %H:%M} 起",
    ])
    writer.writerow([
        '观察窗口', refresh_response['window_hours'], '刷新前后各固定小时数，共享端点只计入后窗',
    ])
    writer.writerow([
        '分析单位', '商品 × 最近一次刷新',
        '系统没有刷新历史表；当前只能观察最新一次刷新，不回填历史',
    ])
    writer.writerow([
        '固定次序', '第 1—4 次',
        '四档全列，包含 0 样本档；累计刷新次数在最新一次刷新上就是次序',
    ])
    writer.writerow([
        '样本门槛', refresh_response['min_group_items'],
        '每个次序达到该成熟样本数才标记样本可用',
    ])
    writer.writerow([
        '只读事件', '搜索曝光、搜索点击、收藏、预约',
        '点击率使用同一窗口的搜索曝光作分母',
    ])
    writer.writerow([
        '刻意不读', 'BrowsingHistory.view_count',
        '浏览次数是累计聚合，不是逐次浏览事件，不能按刷新切开核对',
    ])

    writer.writerow([])
    writer.writerow(['刷新次序对照（前 72 小时 / 后 72 小时）'])
    writer.writerow([
        '刷新次序', '成熟样本', '前窗曝光', '后窗曝光', '后窗中位曝光',
        '前窗点击率（%）', '后窗点击率（%）', '前窗收藏', '后窗收藏',
        '前窗预约', '后窗预约', '样本状态',
    ])
    for row in [*refresh_response['rows'], refresh_response['overall']]:
        sample_state = (
            '样本可用' if row['has_sample'] else
            ('少于 %d 件' % refresh_response['min_group_items'] if row['refreshes'] else '无样本')
        )
        writer.writerow([
            row['label'], row['refreshes'], row['before_impressions'],
            row['after_impressions'],
            row['after_median_impressions'] if row['after_median_impressions'] is not None else '—',
            row['before_click_rate'] if row['before_click_rate'] is not None else '—',
            row['after_click_rate'] if row['after_click_rate'] is not None else '—',
            row['before_favorites'], row['after_favorites'],
            row['before_orders'], row['after_orders'], sample_state,
        ])

    writer.writerow([])
    writer.writerow(['哪些刷新没有进入对照'])
    writer.writerow(['状态', '数量', '处理方式'])
    writer.writerow([
        '周期内观察到的刷新', refresh_response['summary']['observed_refreshes'],
        '先全部登记，再看观察条件',
    ])
    writer.writerow([
        '成熟且可对照', refresh_response['summary']['eligible_refreshes'],
        '进入次序表；每组达到样本门槛才标记可用',
    ])
    writer.writerow([
        '未满观察窗口', refresh_response['summary']['immature_refreshes'],
        '单列，不用被截断的后窗做对照',
    ])
    writer.writerow([
        '无完整前窗', refresh_response['summary']['no_full_pre_window_refreshes'],
        '单列，不把新发布商品混进对照组',
    ])
    writer.writerow([
        '时间异常', refresh_response['summary']['invalid_timestamp_refreshes'],
        '单列，不猜测正确时间',
    ])
    writer.writerow([
        '次序不在 1—4', refresh_response['summary']['unexpected_ordinal_refreshes'],
        '单列，不并入任何固定档',
    ])

    writer.writerow([])
    writer.writerow(['只读与隐私边界'])
    writer.writerow(['项目', '约束'])
    writer.writerow(['因果解释', '只描述共同变化；后窗高于前窗不代表刷新带来因果提升'])
    writer.writerow(['身份与自由文本', '不输出用户名、商品标题、描述、搜索词或订单号'])
    writer.writerow(['运营动作', '不排名、不建议自动刷新、不改排序、不写回推荐或风控'])

    price_response = dashboard['price_response']
    writer.writerow([])
    writer.writerow(['价格调整后的搜索回响'])
    writer.writerow([])
    writer.writerow(['口径与门槛'])
    writer.writerow(['字段', '数值', '说明'])
    writer.writerow([
        '回看天数', price_response['period_days'],
        f"看板周期从 {timezone.localtime(price_response['period_start']):%Y-%m-%d %H:%M} 起",
    ])
    writer.writerow([
        '观察窗口', price_response['window_hours'],
        '调价前后各固定小时数，共享端点只计入后窗',
    ])
    writer.writerow([
        '分析单位', '一条 ItemPriceChange',
        '同一商品的相邻调价重叠时，两条都不进入对照',
    ])
    writer.writerow([
        '样本门槛', price_response['min_group_changes'],
        '每个分组达到该成熟轨迹数才标记样本可用',
    ])
    writer.writerow([
        '只读事件', '搜索曝光、搜索点击、收藏、预约',
        '点击率使用同一窗口的搜索曝光作分母',
    ])
    writer.writerow([
        '刻意不读', 'BrowsingHistory.view_count',
        '浏览次数是累计聚合，不是逐次浏览事件，不能按调价切开核对',
    ])

    writer.writerow([])
    writer.writerow(['方向对照（前 72 小时 / 后 72 小时）'])
    writer.writerow([
        '方向', '成熟样本', '前窗曝光', '后窗曝光', '后窗中位曝光',
        '前窗点击率（%）', '后窗点击率（%）', '前窗收藏', '后窗收藏',
        '前窗预约', '后窗预约', '样本状态',
    ])
    for row in [*price_response['direction_rows'], price_response['overall']]:
        sample_state = (
            '样本可用' if row['has_sample'] else
            ('少于 %d 条' % price_response['min_group_changes'] if row['changes'] else '无样本')
        )
        writer.writerow([
            row['label'], row['changes'], row['before_impressions'],
            row['after_impressions'],
            row['after_median_impressions'] if row['after_median_impressions'] is not None else '—',
            row['before_click_rate'] if row['before_click_rate'] is not None else '—',
            row['after_click_rate'] if row['after_click_rate'] is not None else '—',
            row['before_favorites'], row['after_favorites'],
            row['before_orders'], row['after_orders'], sample_state,
        ])

    writer.writerow([])
    writer.writerow(['幅度档对照（前 72 小时 / 后 72 小时）'])
    writer.writerow([
        '方向', '幅度档', '成熟样本', '前窗曝光', '后窗曝光', '后窗中位曝光',
        '前窗点击率（%）', '后窗点击率（%）', '前窗收藏', '后窗收藏',
        '前窗预约', '后窗预约', '样本状态',
    ])
    for row in price_response['magnitude_rows']:
        sample_state = (
            '样本可用' if row['has_sample'] else
            ('少于 %d 条' % price_response['min_group_changes'] if row['changes'] else '无样本')
        )
        writer.writerow([
            row['direction'], row['bucket'], row['changes'], row['before_impressions'],
            row['after_impressions'],
            row['after_median_impressions'] if row['after_median_impressions'] is not None else '—',
            row['before_click_rate'] if row['before_click_rate'] is not None else '—',
            row['after_click_rate'] if row['after_click_rate'] is not None else '—',
            row['before_favorites'], row['after_favorites'],
            row['before_orders'], row['after_orders'], sample_state,
        ])

    writer.writerow([])
    writer.writerow(['哪些调价没有进入对照'])
    writer.writerow(['状态', '数量', '处理方式'])
    for label, count, handling in price_response['exclusion_rows']:
        writer.writerow([label, count, handling])

    writer.writerow([])
    writer.writerow(['只读与隐私边界'])
    writer.writerow(['项目', '约束'])
    writer.writerow(['因果解释', '只描述共同变化；后窗高于前窗不代表调价带来因果提升'])
    writer.writerow(['身份与自由文本', '不输出用户名、商品标题、描述、搜索词、订单号或通知原文'])
    writer.writerow(['运营动作', '不排名、不建议自动改价、不改排序、不写回推荐或风控'])

    category_reference = dashboard['category_price_reference']
    writer.writerow([])
    writer.writerow(['分类调价参考'])
    writer.writerow([])
    writer.writerow(['口径'])
    writer.writerow(['字段', '数值', '说明'])
    writer.writerow([
        '快照时间', f"{timezone.localtime(category_reference['snapshot_at']):%Y-%m-%d %H:%M}",
        '当前价格快照；看板周期天数不筛选本面板',
    ])
    writer.writerow([
        '样本范围', f"优先近 {category_reference['recent_sample_days']} 天",
        '近期没有样本时回退历史样本，每个分类最多取 500 件',
    ])
    writer.writerow([
        '样本状态', f"少于 {category_reference['min_share_sample_count']} 件只报数量",
        '低可信分类不输出区间外占比',
    ])
    writer.writerow([
        '当前在售', 'available 且未过期',
        'reserved / sold 只参与价格参考，不进入区间外行动计数',
    ])
    writer.writerow([
        '区间宽度', f"超过 {category_reference['wide_range_ratio'] * 100}% 标为过宽",
        '只说明分类内价格差异大，不判卖家定价错误',
    ])

    writer.writerow([])
    writer.writerow(['分类价格参考'])
    writer.writerow([
        '分类', '样本范围', '样本数', '中位数', '常见区间下限', '常见区间上限',
        '当前在售', '低于区间', '区间内', '高于区间', '区间外',
        '区间外占比（%）', '区间宽度（%）', '状态', '读法',
    ])
    for row in category_reference['rows']:
        writer.writerow([
            row['name'], row['sample_scope'], row['sample_count'],
            f"{row['median_price']:.2f}", f"{row['lower_price']:.2f}",
            f"{row['upper_price']:.2f}", row['active_count'], row['below_count'],
            row['within_count'], row['above_count'], row['out_of_range_count'],
            row['out_of_range_share'] if row['out_of_range_share'] is not None else '—',
            row['width_percent'], row['status_label'], row['hint'],
        ])

    writer.writerow([])
    writer.writerow(['只读与隐私边界'])
    writer.writerow(['项目', '约束'])
    writer.writerow(['身份与自由文本', '不输出用户名、商品标题、描述或卖家名单'])
    writer.writerow(['运营动作', '不自动改价、不发提醒、不把区间外读成定价错误'])

    price_quality = dashboard['price_change_quality']
    writer.writerow([])
    writer.writerow(['出售价格轨迹质量', '数值', '说明'])
    writer.writerow([
        '统计周期',
        f"{timezone.localtime(price_quality['period_start']):%Y-%m-%d %H:%M} 至 "
        f"{timezone.localtime(price_quality['period_end']):%Y-%m-%d %H:%M}",
        '左闭右开；不回填轨迹功能上线前的历史编辑',
    ])
    writer.writerow([
        '样本门槛', price_quality['min_sample'],
        '少于该条数只报数字，不给方向结论',
    ])
    for label, key, note in (
        ('轨迹条数', 'change_count', '窗口内全部 ItemPriceChange，不要求商品当前仍在售'),
        ('涉及商品数', 'item_count', '按商品去重；同一商品多次调价只计 1 件'),
        ('涉及卖家数', 'seller_count', '按商品卖家去重，不输出卖家名单'),
        ('降价条数', 'drop_count', '调整后价格低于调整前价格'),
        ('上涨条数', 'rise_count', '调整后价格高于调整前价格'),
        ('零元基线', 'zero_base_count', '调整前价格为 0，能计数但没有可解释幅度'),
        ('降价占比（%）', 'drop_share', '分母是窗口内全部轨迹；样本不足时留空'),
    ):
        value = price_quality['totals'][key]
        writer.writerow([label, '—' if value is None else value, note])

    writer.writerow([])
    writer.writerow(['方向与幅度', '条数', '占全部轨迹（%）', '中位幅度（%）', 'P90 幅度（%）'])
    for direction in price_quality['directions'].values():
        writer.writerow([
            direction['label'], direction['count'],
            '—' if direction['share'] is None else direction['share'],
            '—' if direction['median_ratio'] is None else direction['median_ratio'],
            '—' if direction['p90_ratio'] is None else direction['p90_ratio'],
        ])

    writer.writerow([])
    writer.writerow(['幅度分档', '条数', '占同方向（%）'])
    for row in price_quality['magnitude_rows']:
        writer.writerow([
            f"{row['direction']} {row['bucket']}", row['count'],
            '—' if row['share'] is None else row['share'],
        ])

    writer.writerow([])
    writer.writerow(['重复调价', '商品数'])
    for row in price_quality['repeat_rows']:
        writer.writerow([row['label'], row['item_count']])

    writer.writerow([])
    writer.writerow(['出售价格轨迹 · 当前分类视图', '轨迹数', '降价', '上涨', '零元基线', '降价占比（%）', '小样本'])
    for row in price_quality['category_rows']:
        writer.writerow([
            row['name'], row['change_count'], row['drop_count'], row['rise_count'],
            row['zero_base_count'],
            '—' if row['drop_share'] is None else row['drop_share'],
            '是' if row['is_small_sample'] else '否',
        ])

    writer.writerow([])
    writer.writerow([
        '说明',
        '分类是当前分类视图；重复调价是统计窗口内的下界；零元基线不进入涨跌幅分档；'
        '本面板只报告聚合，不读取商品标题、卖家用户名、通知原文或收藏人名单，'
        '也不把降价率解释成成交率或自动干预价格。',
    ])

    relist = dashboard['relist_freshness']
    writer.writerow([])
    writer.writerow(['重新上架的那件商品，为什么一回来就沉底', '数值', '说明'])
    writer.writerow([
        '口径与门槛', relist['summary'],
        '只读 Item.status、expires_at、created_at、refresh_count 与 last_refreshed_at，'
        '复用 _level_for 与 _refresh_state 的判据，不在重新上架时重置任何字段',
    ])
    writer.writerow([
        '沉底天数门槛', relist['stale_days'], '',
        '复用 lifecycle_diagnostics 的 STALE_DAYS，判据只允许有一份',
    ])
    writer.writerow([
        '在售商品', relist['denominator']['live_count'], '',
        '其中带着未来截止时间的才进本模块分母',
    ])
    writer.writerow([
        '带着未来截止时间在售', relist['denominator']['deadline_live_count'], '',
        '分母。没有截止时间的商品永远走不到 expire_items，也就没有重新上架这个动作',
    ])
    writer.writerow([
        '其中带着新鲜度欠账', relist['denominator']['drifted_count'], '',
        f"占 {relist['drifted_share']}%",
    ])

    writer.writerow([])
    writer.writerow(['欠账商品欠了多少天'])
    writer.writerow(['距发布', '商品数', '占欠账（%）'])
    for row in relist['arrears']['bucket_rows']:
        writer.writerow([row['label'], row['count'], row['share']])

    writer.writerow([])
    writer.writerow(['欠账商品现在的生命周期等级'])
    writer.writerow(['生命周期等级', '商品数', '占欠账（%）'])
    for row in relist['levels']['rows']:
        writer.writerow([row['label'], row['count'], row['share']])

    writer.writerow([])
    writer.writerow(['刷新额度对这批商品是不是空的'])
    writer.writerow(['已刷新次数', '商品数', '占欠账（%）'])
    for row in relist['quota']['bucket_rows']:
        writer.writerow([row['label'], row['count'], row['share']])
    writer.writerow([
        '现在就能刷新', relist['quota']['ready_count'], relist['quota']['ready_share'],
        '额度没用尽、24 小时间隔也没占着，一刷就灵',
    ])

    writer.writerow([])
    writer.writerow(['欠账商品里还有没有互动'])
    writer.writerow(['指标', '数值', '口径说明'])
    writer.writerow([
        '仍有浏览收藏或成交', relist['engagement']['engaged_count'],
        relist['engagement']['engaged_share'], '三者有其一即算有互动',
    ])
    writer.writerow([
        '三者皆无', relist['engagement']['silent_count'],
        relist['engagement']['silent_share'],
        '这部分连刷新都救不回来，刷回去也只是再沉一次，需要的是重新拍照或降价',
    ])

    writer.writerow([])
    writer.writerow(['带着新鲜度欠账的商品清单'])
    writer.writerow(['商品', '分类', '距发布（天）', '刷新次数', '生命周期等级'])
    for row in relist['drift_list']['rows']:
        writer.writerow([
            row.item.title, row.category_label, row.arrears_days,
            row.refresh_count, row.level_label,
        ])
    if not relist['drift_list']['rows']:
        writer.writerow(['—', '当前没有带着新鲜度欠账的在售商品', '', '', ''])
    if relist['drift_list']['truncated']:
        writer.writerow([
            '—', f"清单最多列 {relist['drift_list']['limit']} 条，完整的在数据库里",
            '', '', '',
        ])

    writer.writerow([])
    writer.writerow([
        '说明',
        '本模块只报告，不在重新上架时重置 last_refreshed_at、不给重新上架记时间戳、'
        '不加字段、不改 _level_for 的等级判据、不自动帮卖家刷新。'
        '重新上架这个动作在系统里没有留下任何时间戳，而 expires_at 在未来这个信号'
        '新发布的商品也会有，所以本模块报的是新鲜度欠账这个可观测量，'
        '不是重新上架这个不可观测量。',
    ])

    pulse = dashboard['campus_pulse']
    writer.writerow([])
    writer.writerow(['校园供需热力'])
    writer.writerow(['口径与门槛'])
    writer.writerow([
        '活跃地点', pulse['summary']['location_count'],
        '当前在售', pulse['summary']['available_supply'],
        '有效求购', pulse['summary']['active_demands'],
        '需求紧张地点', pulse['summary']['high_pressure_count'],
        '周期内预约', pulse['summary']['period_orders'],
        '周期天数', pulse['period_days'],
    ])
    writer.writerow([])
    writer.writerow(['校园地点', '楼宇', '公开地点', '当前在售', '周期新增供给',
                     '有效求购', '周期内预约', '周期内完成', '活跃度',
                     '活跃度占比（%）', '需求占比（%）', '供需状态'])
    if not pulse['rows']:
        writer.writerow(['当前周期还没有地点供需数据。'])
    else:
        for row in pulse['rows']:
            writer.writerow([
                row['name'],
                row['building'],
                '是' if row['is_public'] else '',
                row['available_supply'],
                row['new_supply'],
                row['active_demands'],
                row['period_orders'],
                row['completed_orders'],
                row['activity_score'],
                row['heat_percent'],
                row['pressure_ratio'],
                row['pressure_label'],
            ])
    writer.writerow([])
    writer.writerow(['说明'])
    writer.writerow([
        '热力按配置地点聚合，不读用户的实时定位：需求压力由公开的商品、求购和'
        '预约记录推出来，一条记录都不会指到某个人的当前位置上。',
    ])
    writer.writerow([
        '活跃度由周期新增、求购和预约加权合成，占比是同一批地点里的相对热度，'
        '不是跨周期的增长；只看一行看不出它在变热还是变冷。',
    ])
    writer.writerow([
        '供需状态只看当下这一刻的存量，与统计周期无关：期初上架、周期内卖掉的'
        '那件商品不计入在售，所以状态回答的是"现在缺不缺"，不是"这周热闹吗"。',
    ])
    writer.writerow([
        '此处只报告，不调整地点排序、不据此给任何地点增加曝光或减少曝光。',
    ])

    campaigns = dashboard['campaign_analytics']
    writer.writerow([])
    writer.writerow(['校园专题表现'])
    writer.writerow(['口径与门槛'])
    writer.writerow([
        '统计专题', campaigns['summary']['campaign_count'],
        '专题新增商品', campaigns['summary']['new_items'],
        '专题当前在售', campaigns['summary']['current_items'],
        '专题浏览', campaigns['summary']['views'],
        '专题收藏', campaigns['summary']['favorites'],
        '专题当前活跃报名', campaigns['summary']['active_registration_count'],
        '专题周期新增报名', campaigns['summary']['new_registration_count'],
        '专题当前摘要订阅', campaigns['summary']['current_subscription_count'],
        '专题待发送摘要命中', campaigns['summary']['pending_update_match_count'],
        '周期专题摘要', campaigns['summary']['period_sent_digest_count'],
        '周期专题摘要已读', campaigns['summary']['period_read_digest_count'],
        '专题摘要阅读率（%）', campaigns['summary']['digest_read_rate'],
        '启用专题目标', campaigns['summary']['goal_count'],
        '已达成目标', campaigns['summary']['achieved_goal_count'],
        '需推进目标', campaigns['summary']['goal_attention_count'],
        '漏斗浏览用户', campaigns['summary']['funnel_viewers'],
        '漏斗预约用户（浏览后）', campaigns['summary']['funnel_order_users'],
        '漏斗完成交易用户（预约后）', campaigns['summary']['funnel_completed_users'],
        '专题预约', campaigns['summary']['orders'],
        '专题完成交易', campaigns['summary']['completed_orders'],
        '浏览转预约（%）', campaigns['summary']['view_to_order_rate'],
        '需要关注', campaigns['summary']['attention_count'],
        '周期天数', campaigns['period_days'],
    ])
    writer.writerow([])
    writer.writerow(['专题目标与节奏'])
    writer.writerow(['专题', '指标', '目标值', '当前值', '完成率（%）', '剩余量',
                     '开始时间', '结束时间', '时间进度（%）', '节奏偏差（百分点）',
                     '状态', '说明'])
    if not campaigns['all_goal_rows']:
        writer.writerow(['当前统计范围内还没有启用的专题目标。'])
    else:
        for goal in campaigns['all_goal_rows']:
            writer.writerow([
                goal['campaign_title'],
                goal['metric_label'],
                goal['target_value'],
                goal['current_value'],
                goal['progress_percent'],
                goal['remaining_value'],
                goal['starts_at'].strftime('%Y-%m-%d %H:%M:%S'),
                goal['ends_at'].strftime('%Y-%m-%d %H:%M:%S')
                if goal['ends_at'] else '',
                '' if goal['elapsed_percent'] is None else goal['elapsed_percent'],
                '' if goal['pace_delta'] is None else goal['pace_delta'],
                goal['status_label'],
                goal['note'],
            ])
    writer.writerow([])
    writer.writerow(['专题', '周期新增商品', '当前在售', '参与卖家',
                     '当前活跃报名', '周期新增报名',
                     '当前摘要订阅', '待发送摘要命中', '周期摘要', '周期摘要已读',
                     '浏览', '收藏', '浏览转收藏（%）',
                     '预约', '完成交易', '完成率（%）',
                     '收藏转预约（%）', '浏览转预约（%）', '判断'])
    if not campaigns['rows']:
        writer.writerow(['当前统计周期还没有专题行为数据。'])
    else:
        for row in campaigns['rows']:
            writer.writerow([
                row['title'],
                row['new_items'],
                row['current_items'],
                row['seller_count'],
                row['active_registration_count'],
                row['new_registration_count'],
                row['current_subscription_count'],
                row['pending_update_match_count'],
                row['period_sent_digest_count'],
                row['period_read_digest_count'],
                row['views'],
                row['favorites'],
                row['view_to_favorite_rate'],
                row['orders'],
                row['completed_orders'],
                row['completion_rate'],
                row['favorite_to_order_rate'],
                row['view_to_order_rate'],
                row['performance_label'],
            ])
    writer.writerow([])
    writer.writerow(['各专题用户漏斗'])
    writer.writerow([
        '专题', '浏览用户', '预约用户（浏览后）', '浏览转预约用户（%）',
        '完成交易用户（预约后）', '预约转完成用户（%）',
        '收藏用户意向分支', '报名用户意向分支', '意向分支率（%）',
        '意向用户数',
    ])
    if not campaigns['rows']:
        writer.writerow(['当前统计周期还没有专题行为数据。'])
    else:
        for row in campaigns['rows']:
            writer.writerow([
                row['title'],
                row['funnel_viewers'],
                row['funnel_order_users'],
                row['viewer_to_order_user_rate'],
                row['funnel_completed_users'],
                row['order_to_complete_user_rate'],
                row['viewer_favorite_users'],
                row['viewer_registration_users'],
                row['intent_rate'],
                row['intent_user_count'],
            ])
    writer.writerow([])
    writer.writerow(['需要关注的专题', '判断', '说明'])
    if campaigns['attention_rows']:
        for row in campaigns['attention_rows']:
            writer.writerow([row['title'], row['performance_label'], row['performance_note']])
    else:
        writer.writerow(['暂无需要关注的专题。'])
    writer.writerow([])
    writer.writerow(['说明'])
    writer.writerow([
        '指标按专题窗口与统计周期重叠的那一段计算，不是按整个专题的历史：'
        '一个开学初就上线的专题，在"最近 30 天"里只有这 30 天的行为被计入。',
    ])
    writer.writerow([
        '浏览用户是去重后的用户数，只作为辅助信号；浏览、收藏和预约是事件数，'
        '同一个用户多次操作会被计多次。',
    ])
    writer.writerow([
        '用户漏斗按严格路径计算：预约用户必须同时出现在报告窗口的浏览用户中，'
        '完成交易用户还必须对应订单当前状态为 completed；收藏和报名是意向分支，'
        '不进入连续漏斗的分母。顶部漏斗汇总会把各专题用户数相加，同一用户参与'
        '多个专题会被重复计数，不是全站去重用户数。',
    ])
    writer.writerow([
        '专题目标按目标自己的起止时间实时重算，不随看板 7 天或 30 天窗口改变；'
        '节奏偏差等于完成率减去已流逝时间比例，低于 -10 个百分点标记为需要推进。',
    ])
    writer.writerow([
        '事件转化率用于横向比较专题，不代表任何单个用户的完整行为路径，'
        '也不代表专题本身的质量：运营人员仍需结合商品质量和活动内容判断。',
    ])
    writer.writerow([
        '报名表示用户关注专题，不会预约商品、占用库存或创建订单；'
        '当前活跃报名按尚未退出的参与区间计数，周期新增报名按加入时间归期。',
    ])
    writer.writerow([
        '摘要订阅只表示用户额外同意接收上新提醒，与报名分开计算，不进入预约率或成交率；'
        '待发送命中只统计报名仍有效、订阅仍开启、专题仍在进行且商品仍可投递的记录。',
    ])
    writer.writerow([
        '周期摘要按通知去重统计；同一条合并摘要覆盖多个专题时，各专题行会各计一次，'
        '顶部汇总只计通知一次。摘要已读只代表触达被看到，不代表购买意向。',
    ])
    writer.writerow([
        '此处只报告，不调整专题的投放位置、不自动结束或续期任何专题。',
    ])

    phase_review = dashboard['campaign_phase_review']
    phase_summary = phase_review['summary']
    writer.writerow([])
    writer.writerow(['校园专题分阶段复盘'])
    writer.writerow(['导出口径'])
    writer.writerow([
        '已结束专题', phase_summary['closed_campaign_count'],
        '本轮复盘', phase_summary['reviewed_campaign_count'],
        '仍未结束', phase_summary['open_campaign_count'],
        '阶段行', phase_summary['phase_count'],
        '可判节奏专题', phase_summary['order_sample_campaign_count'],
        '新增商品', phase_summary['new_items'],
        '浏览', phase_summary['views'],
        '收藏', phase_summary['favorites'],
        '预约', phase_summary['orders'],
        '完成交易', phase_summary['completed_orders'],
    ])
    writer.writerow([])
    writer.writerow([
        '专题', '阶段', '开始时间', '结束时间', '阶段天数', '新增商品',
        '浏览', '收藏', '预约', '完成交易', '浏览转预约（%）',
        '预约完成率（%）', '预约/百天', '浏览/百天', '新增/百天',
    ])
    if not phase_review['rows']:
        writer.writerow(['当前还没有已经结束、且时间范围有效的校园专题。'])
    else:
        for row in phase_review['rows']:
            for phase in row['phases']:
                writer.writerow([
                    row['title'],
                    phase['label'],
                    timezone.localtime(phase['starts_at']).strftime('%Y-%m-%d %H:%M'),
                    timezone.localtime(phase['ends_at']).strftime('%Y-%m-%d %H:%M'),
                    phase['days'],
                    phase['new_items'],
                    phase['views'],
                    phase['favorites'],
                    phase['orders'],
                    phase['completed_orders'],
                    '' if phase['view_to_order_rate'] is None else phase['view_to_order_rate'],
                    '' if phase['completion_rate'] is None else phase['completion_rate'],
                    '' if phase['orders_per_100_days'] is None else phase['orders_per_100_days'],
                    '' if phase['views_per_100_days'] is None else phase['views_per_100_days'],
                    '' if phase['new_items_per_100_days'] is None else phase['new_items_per_100_days'],
                ])
    writer.writerow([])
    writer.writerow(['跨专题总结'])
    writer.writerow([phase_review['cross_campaign']['conclusion']])
    writer.writerow([])
    writer.writerow(['说明'])
    writer.writerow([
        '只读复盘：只统计 ends_at 已到期的专题，并取最近 8 个；每个专题按自己的 '
        'starts_at 到 ends_at 均分前、中、后三段，边界采用半开区间。',
    ])
    writer.writerow([
        '浏览按 BrowsingHistory.last_viewed_at，商品按 Item.created_at，收藏按 '
        'Favorite.created_at，预约按 Order.created_at；完成交易只数当前状态为 completed 的订单。',
    ])
    writer.writerow([
        '只有三段都至少有 3 次预约才判断预约高峰；样本不足或并列时只报数字。'
        '这里不输出用户名、商品标题或订单号，也不调整专题、商品、曝光或通知。',
    ])

    calendar = dashboard['academic_calendar']
    writer.writerow([])
    writer.writerow(['学期节律与交易高峰'])
    if calendar['has_terms']:
        context = calendar['context']
        writer.writerow([
            '当前学期', context['term_name'] or '未配置',
            '当前阶段', context['phase_label'] or '未划分阶段',
            '阶段进度（%）', context['progress_percent'],
            '阶段剩余天数', '' if context['days_left'] is None else context['days_left'],
            '阶段说明', context['phase_note'],
        ])
    else:
        writer.writerow(['还没有配置学期日历，后台录入起止日期后这里会开始统计。'])
    source = calendar['source']
    writer.writerow([])
    writer.writerow([
        '数据来源汇总', '读快照', '实时算', '合计', '快照占比（%）',
        '实时：阶段未结束', '实时：已结束兜底', '已结束阶段', '已结束快照占比（%）',
    ])
    writer.writerow([
        '阶段行',
        source['snapshot_row_total'],
        source['live_row_total'],
        source['row_total'],
        '' if source['snapshot_share'] is None else source['snapshot_share'],
        source['running_live_row_total'],
        source['fallback_live_row_total'],
        source['closed_row_total'],
        '' if source['closed_snapshot_share'] is None else source['closed_snapshot_share'],
    ])
    if source['has_snapshot_miss']:
        writer.writerow([
            '快照缺行告警',
            f"{source['fallback_live_row_total']} 个已结束阶段没有读到匹配快照，当前实时兜底",
            f"缺行 {source['missing_snapshot_total']} 个；区间不匹配 {source['range_mismatch_total']} 个",
            '请核对 sync_academic_calendar 与阶段边界；本面板只报告，不自动补刷',
        ])
    writer.writerow([])
    writer.writerow(['学期', '学期类型', '阶段', '开始', '结束', '天数', '进行中',
                     '新增商品', '新增求购', '完成交易', '新增预约',
                     '新增搜索', '无结果搜索', '无结果率（%）',
                     '较上一阶段新增商品', '较上一阶段新增求购', '供给趋势', '求购趋势',
                     '数据来源', '来源说明'])
    if not calendar['has_terms'] or not calendar['terms']:
        writer.writerow(['当前没有可统计的学期阶段。'])
    else:
        for term in calendar['terms']:
            for row in term['phases']:
                writer.writerow([
                    row['term_name'],
                    term['kind_label'],
                    row['phase_label'],
                    row['start_date'],
                    row['end_date'],
                    row['day_count'],
                    '是' if row['is_current'] else '',
                    row['new_items'],
                    row['new_demands'],
                    row['completed_orders'],
                    row['new_orders'],
                    row['new_searches'],
                    row['zero_result_searches'],
                    row['zero_result_rate'],
                    '' if not row['previous_phase_label'] else row['delta_new_items'],
                    '' if not row['previous_phase_label'] else row['delta_new_demands'],
                    row['supply_trend_label'],
                    row['demand_trend_label'],
                    row['source_label'],
                    row['source_note'],
                ])
    writer.writerow([])
    writer.writerow(['说明'])
    writer.writerow([
        '阶段按学期开始后的偏移天数划分，偏移量可在后台调整；一个阶段覆盖哪些天'
        '完全由配置决定，不随统计周期变化。',
    ])
    writer.writerow([
        '阶段之间的比较说的是供给节律，不是因果：开学两周的商品变多，'
        '既可以解释成"学生更愿意卖东西"，也可以解释成"上一届离校清了一批库存"。',
    ])
    writer.writerow([
        '学期首阶段没有可比的上一阶段，差值留空；把它当成 0 会读成"没有增长"，'
        '而真实情况是根本没有参照。',
    ])
    writer.writerow([
        '数据来源一列说明这一行的七个计数是读快照还是实时算。已结束阶段的计数已经'
        '封闭（Item / DemandPost / Order / SearchQuery 的 created_at 全是 auto_now_add，'
        '没有写入路径回头改它们），读每周刷新的快照与实时算在数值上一致；'
        '进行中的阶段永远实时算，因为快照最多是上周的，而当前阶段是看板上唯一有人盯的一行。',
    ])
    writer.writerow([
        '实时行再分成“阶段未结束”和“已结束兜底”。前者是既定策略，后者意味着快照'
        '缺行或区间不匹配；缺行告警不推断原因，也不自动补刷。',
    ])
    writer.writerow([
        '快照占比的分母是实际渲染出来的阶段行数。占比为空的含义是"一行快照都没读到"，'
        '与 0% 不同：0% 会被读成"刷过了但一行都没命中"，而空值只说快照表还没有可用数据。',
    ])
    writer.writerow([
        '此处只报告，不修改学期日历、不按阶段调整任何商品的曝光或提醒策略。',
    ])
    rhythm = dashboard['borrow_rhythm']
    writer.writerow([])
    writer.writerow(['借用节律与学期阶段交叉'])
    writer.writerow([
        '周期内借用', rhythm['borrow_count'],
        '拖到第三级', rhythm['level_three_count'],
        '第三级率（%）', '' if rhythm['level_three_rate'] is None else rhythm['level_three_rate'],
        '日历覆盖（%）', '' if rhythm['covered_share'] is None else rhythm['covered_share'],
        '覆盖范围外借用', rhythm['unplaced_count'],
        '最小样本门槛', rhythm['min_sample_size'],
    ])
    writer.writerow([])
    writer.writerow(['阶段', '学期', '借用', '升级', '第三级',
                     '第三级率（%）', '活跃天数', '日均借用',
                     '日均第三级', '平均闭环（天）',
                     '最长逾期（天）', '跟进中', '样本不足'])
    for row in rhythm['rows']:
        writer.writerow([
            row['phase_label'], row['term_name'], row['borrow_count'], row['escalated_count'],
            row['level_three_count'],
            '' if row['level_three_rate'] is None else row['level_three_rate'],
            row['active_days'],
            '' if row['borrow_per_day'] is None else row['borrow_per_day'],
            '' if row['level_three_per_day'] is None else row['level_three_per_day'],
            '' if row['average_close_days'] is None else row['average_close_days'],
            '' if row['max_overdue_days'] is None else row['max_overdue_days'],
            row['open_count'], '是' if row['is_small_sample'] else '',
        ])

    for row in rhythm['rows']:
        if not row['category_rows']:
            continue
        writer.writerow([])
        writer.writerow([f"阶段内分类明细：{row['phase_label']}"])
        writer.writerow(['分类', '借用', '第三级',
                         '阶段内占比（%）', '第三级率（%）', '样本不足'])
        for entry in row['category_rows']:
            writer.writerow([
                entry['category_label'], entry['borrow_count'], entry['level_three_count'],
                '' if entry['share_in_phase'] is None else entry['share_in_phase'],
                '' if entry['level_three_rate'] is None else entry['level_three_rate'],
                '是' if entry['is_small_sample'] else '',
            ])

    writer.writerow([])
    writer.writerow(['说明'])
    writer.writerow([rhythm['summary']])
    writer.writerow([
        '日均分母为阶段真正产生借用的天数，因为报告窗口可能只覆盖阶段的一部分；'
        '占比的分母是所在阶段的借用总数，不是全站借用数。',
    ])
    writer.writerow([
        '无学期日历、阶段空隙两个合成桶描述的是日历覆盖率而不是节奏，'
        '单独计数不参与结论；此处只做统计聚合，不会提前催收。',
    ])


    trend = dashboard['borrow_escalation_trend']
    writer.writerow([])
    writer.writerow(['借用催收的跨周期时效对比'])
    writer.writerow([
        '当前周期起始', trend['current_period_start'].strftime('%Y-%m-%d'),
        '上一周期起始', trend['previous_period_start'].strftime('%Y-%m-%d'),
        '周期天数', trend['days'],
        '最小样本门槛', trend['min_sample_size'],
        '方向阈值（百分点）', trend['trend_delta_points'],
    ])
    writer.writerow([])
    writer.writerow([
        '指标', '本周期', '上一周期', '差值', '方向',
    ])
    for row in trend['metric_rows']:
        writer.writerow([
            row['label'],
            '' if row['current'] is None else row['current'],
            '' if row['previous'] is None else row['previous'],
            row['delta_display'],
            row['direction_label'],
        ])

    writer.writerow([])
    writer.writerow([
        '本周期借用', trend['current']['borrow_count'],
        '本周期发生过升级', trend['current']['escalated_count'],
        '本周期闭环', trend['current']['resolved_count'],
        '上一周期借用', trend['previous']['borrow_count'],
        '上一周期发生过升级', trend['previous']['escalated_count'],
        '上一周期闭环', trend['previous']['resolved_count'],
    ])
    writer.writerow([])
    writer.writerow(['说明'])
    writer.writerow([trend['summary']])
    writer.writerow([
        '周期按本地午夜切分，与搜索趋势面板同一个函数；分子分母都按同一笔借用归期，否则调度器跑过来就会抬高升级率。',
    ])
    writer.writerow([
        '升级率的分母含从未升级过的借用；催收命令只走 120 天回看窗口，超出窗口的订单会永远留在分母里，表现为升级率偏低。',
    ])
    writer.writerow([
        '闭环时长从真正发生的那一级催收起算，不从到期日起算；未闭环订单的逾期天数属于快照，不参与对比——它只会随时间变老。',
    ])
    writer.writerow([
        '此处只报告，不提前催收、不改动催收阶梯、不打标记借用人。',
    ])

    risk = dashboard['borrow_risk']
    writer.writerow([])
    writer.writerow(['借用逾期风险分层'])
    writer.writerow([
        '回看天数', risk['days'],
        '周期内借用', risk['borrow_count'],
        '发生过升级', risk['escalated_count'],
        '升级率（%）', '' if risk['escalation_rate'] is None else risk['escalation_rate'],
        '拖到第三级', risk['level_three_count'],
        '第三级率（%）', '' if risk['level_three_rate'] is None else risk['level_three_rate'],
        '最小样本门槛', risk['min_sample_size'],
    ])
    writer.writerow([])
    writer.writerow(['分层方式', '分组', '借用', '升级', '第三级', '已闭环', '跟进中',
                     '升级率（%）', '第三级率（%）', '平均闭环（天）',
                     '最长逾期（天）', '样本不足'])
    if not risk['has_data']:
        writer.writerow(['当前回看期内还没有借用订单，风险分层需要先有借用发生。'])
    else:
        for stratify_label, rows in (
            ('按分类', risk['category_rows']),
            ('按押金档位', risk['deposit_rows']),
        ):
            for row in rows:
                writer.writerow([
                    stratify_label,
                    row['key'],
                    row['borrow_count'],
                    row['escalated_count'],
                    row['level_three_count'],
                    row['resolved_count'],
                    row['open_count'],
                    '' if row['escalation_rate'] is None else row['escalation_rate'],
                    '' if row['level_three_rate'] is None else row['level_three_rate'],
                    '' if row['average_close_days'] is None else row['average_close_days'],
                    '' if row['max_overdue_days'] is None else row['max_overdue_days'],
                    '是' if row['is_small_sample'] else '',
                ])
    writer.writerow([])
    writer.writerow(['说明'])
    writer.writerow([risk['summary']])
    writer.writerow([
        '分母包含从未升级过的借用：只看升级过的订单会让每一组的升级率都变成 '
        '100%，分层就失去了意义。',
    ])
    writer.writerow([
        '两种分层读的是同一批借用，分组互斥且穷尽，所以两张表的合计应当一致；'
        '若对不上，说明改动时只动了一侧的取数。',
    ])
    writer.writerow([
        '平均闭环从真正发生的那一级催收起算，不从到期日起算：在 L1 躺了一周的订单'
        '不是治理侧花了一周处理的那一单。',
    ])
    writer.writerow([
        '最长逾期只对仍在跟进中的订单有意义，已闭环的订单不会再变老，'
        '把它和未闭环的放在一起比，会读成"闭环反而更危险"。',
    ])
    writer.writerow([
        '样本少于 ', risk['min_sample_size'], ' 笔的组只报数字、不给结论：'
        '两笔里有一笔逾期是 50%，这个数字说明不了任何事。',
    ])
    writer.writerow([
        '此处只报告，不因为某个分类看起来更容易拖就提前催收它的借用方，'
        '也不据此调整押金额度或给任何人打标记。',
    ])

    shape = dashboard['borrow_days_shape']
    writer.writerow([])
    writer.writerow(['默认借用天数的形状'])
    writer.writerow(['口径与门槛', shape['summary'],
                     '副标题：borrow_days 第一次被数字读到',
                     '只读 Item.borrow_days、Order 与 '
                     'BorrowReturnEscalation，不加迁移、不改默认值、不改 1 到 90 的校验、'
                     '不追改历史订单、不动催收阶梯'])
    writer.writerow([
        '借用商品', shape['item_count'],
        '其中在架', shape['in_stock_count'],
        '周期内借用', shape['borrow_count'],
        '发生过升级', shape['escalated_count'],
        '升级率（%）', '' if shape['escalation_rate'] is None else shape['escalation_rate'],
        '拖到第三级', shape['level_three_count'],
        '默认值', shape['default_borrow_days'],
        '默认值占比（%）',
        '' if shape['default_share'] is None else shape['default_share'],
        '配置档最小样本', shape['min_band_sample'],
        '占比方向最小样本', shape['min_item_sample'],
    ])
    writer.writerow([])
    writer.writerow(['期限档', '商品数', '其中在架', '占比（%）', '样本'])
    for row in shape['item_rows']:
        writer.writerow([
            row['label'], row['item_count'], row['in_stock_count'],
            '' if row['share'] is None else row['share'],
            f"不足 {shape['min_item_sample']} 件，只作参考" if row['is_small_sample'] else '可读数',
        ])
    writer.writerow([
        '说明', '', '', '',
        '分母是全部借用商品，包括从没被借出去的：借用商品的配置分布和「被用过的期限分布」'
        '是两个问题，只读被借过的商品会让前者看起来像后者',
        '档位取表单上的原值 1/3/7/14/30/90，不取分位数：运营看表时要能认出自己在表单上选的'
        '那一档，一个每月都会移动的分位边界什么也说明不了',
        '0 样本档全列：「90 天上没有人」本身是结论，不是表格的缺口',
    ])
    writer.writerow([])
    writer.writerow(['期限档', '配置的借用', '配置占比（%）', '实际归还的借用',
                     '实际占比（%）', '平均实际天数', '最长实际天数', '样本'])
    for row in shape['configured_rows']:
        writer.writerow([
            row['label'], row['configured_count'],
            '' if row['configured_share'] is None else row['configured_share'],
            row['actual_count'],
            '' if row['actual_share'] is None else row['actual_share'],
            '' if row['average_actual_days'] is None else row['average_actual_days'],
            '' if row['max_actual_days'] is None else row['max_actual_days'],
            '是' if row['is_small_sample'] else '',
        ])
    writer.writerow([
        '说明', '', '', '', '',
        '配置列的分母是周期内全部借用，实际列的分母是其中已归还的借用：两个分母不是同一个'
        '数，分开报。不知道这一点的读者会把完成率读成时长分布',
        '配 7 天、3 天后归还，两列各记一格：配置侧记 4-7 天档，实际侧记 1-3 天档。报一个差值'
        '列会看不出是哪一侧在动',
        '还挂在 borrowed 的订单是右删失样本，实际时长还不存在，只进配置列，'
        f"单独数出来（{shape['censored_count']} 笔），不进实际列的任何分母",
        '配置侧读的是商品的现值。下单后改过借用天数的商品，会用新值和旧订单的时长对不上——'
        '这个不对称如实报出来，不抹平：库里没有记录字段变更历史的表',
        '改商品的借用天数不追溯旧订单：一笔已经借出去的订单，它的归还截止时刻在下单那一刻'
        '就定了',
    ])
    writer.writerow([])
    writer.writerow(['期限档', '借用', '升级', '第三级', '已闭环', '跟进中',
                     '升级率（%）', '第三级率（%）', '逾期笔数', '逾期率（%）',
                     '平均超期天数', '平均超期占配置（%）', '平均闭环（天）', '样本不足'])
    for row in shape['band_rows']:
        writer.writerow([
            row['label'], row['borrow_count'], row['escalated_count'],
            row['level_three_count'], row['resolved_count'], row['open_count'],
            '' if row['escalation_rate'] is None else row['escalation_rate'],
            '' if row['level_three_rate'] is None else row['level_three_rate'],
            row['overdue_count'],
            '' if row['overdue_rate'] is None else row['overdue_rate'],
            '' if row['average_overdue_days'] is None else row['average_overdue_days'],
            '' if row['average_overdue_ratio'] is None else row['average_overdue_ratio'],
            '' if row['average_close_days'] is None else row['average_close_days'],
            '是' if row['is_small_sample'] else '',
        ])
    writer.writerow([
        '说明', '', '', '', '', '',
        '分母包含从未升级过的借用，和 borrow_risk 同一批订单、同一个口径，两表可以并排读。'
        '只看升级过的订单会让每一档的升级率都变成 100%',
        '逾期按已归还的笔算，不按未闭环的算：未闭环的逾期天数还在随时间增长，报进平均值会让'
        '这个数随读取时刻漂移',
        '主口径是「超期天数 / 配置天数」而不是绝对天数：逾期 3 天对一笔 7 天的借用和对一笔 '
        '90 天的借用是两件不同的事。绝对超期天数仍然单列，因为那是和借用者沟通时真的会用到的数',
        '平均闭环从真正发生的那一级催收起算，不从到期日起算',
        '不把相关性讲成因果：配 90 天的商品三天就还，可能是期限形同虚设，也可能这类东西本来'
        '就不需要借那么久，本模块没有能力区分',
        '不把长期限读成「更信任别人」：一个人可能只是因为懒或者照抄了默认值',
        f"周期内共有 {shape['unknown_item_count']} 笔借用的商品已不存在，其配置读不到，"
        '单独计数、不摊派到任何档位',
    ])
    writer.writerow([
        '此处只报告，不因为某个期限档看起来更容易拖就提前催收它的借用方，'
        '也不据此调整默认借用天数或给任何人打标记。',
    ])

    evidence_shape = dashboard['dispute_evidence_shape']
    writer.writerow([])
    writer.writerow(['证据文件的形状'])
    writer.writerow(['口径与门槛', evidence_shape['summary'],
                     '副标题：attachment.name 与 size 第一次被数字读到',
                     '只读 '
                     'OrderDisputeEvidence.attachment 的 name 与 size，不加迁移、不改 '
                     'validate_dispute_evidence 的 5 MB 上限与四种扩展名、不读文件内容、'
                     '不读 note、不列文件名、不按 uploaded_by 拆'])
    writer.writerow([
        '周期天数', evidence_shape['days'],
        '窗口起', evidence_shape['period_start'],
        '窗口止', evidence_shape['period_end'],
        '附件总数', evidence_shape['evidence_count'],
        '能读到大小的', evidence_shape['readable_count'],
        '读不到大小的', evidence_shape['unreadable_count'],
        '带附件的争议数', evidence_shape['dispute_count'],
        '允许的扩展名', '、'.join(evidence_shape['allowed_extensions']),
    ])
    near_limit = evidence_shape['near_limit']
    writer.writerow([
        '贴上限（≥ %s）' % near_limit['near_limit_label'],
        near_limit['near_limit_count'],
        '占能读到大小的（%）',
        near_limit['near_limit_rate'],
        '超过上限', near_limit['over_limit_count'],
        '超过上限占比（%）', near_limit['over_limit_rate'],
        '最大附件', near_limit['max_size_label'],
    ])
    writer.writerow([])
    writer.writerow(['格式', '份数', '占比（%）', '说明'])
    if not evidence_shape['has_data']:
        writer.writerow(['当前周期还没有证据数据。'])
    else:
        for row in evidence_shape['extension_rows']:
            writer.writerow([row['label'], row['count'], row['share'], row['note']])
    writer.writerow([])
    writer.writerow(['大小档', '份数', '占比（%）', '说明'])
    for row in evidence_shape['size_rows']:
        writer.writerow([
            row['label'], row['count'], row['share'],
            '不足 %d 份，只作参考' % evidence_shape['min_sample']
            if row['is_small_sample'] else '',
        ])
    writer.writerow([])
    writer.writerow(['每笔争议的附件份数', '争议数', '占比（%）', '说明'])
    per_dispute = evidence_shape['per_dispute']
    for row in per_dispute['count_rows']:
        writer.writerow([
            row['label'], row['count'], row['share'],
            '不足 %d 笔，只作参考' % evidence_shape['min_sample'] if row['is_small_sample'] else '',
        ])
    writer.writerow([
        '争议总数', per_dispute['dispute_count'],
        '单笔最多', per_dispute['max_count'],
        '总大小中位', per_dispute['median_total_label'],
        'P90 总大小', per_dispute['p90_total_label'],
        '平均总大小', per_dispute['average_total_label'],
        '单笔最大总大小', per_dispute['max_total_label'],
    ])
    writer.writerow([])
    writer.writerow(['争议状态', '争议数', '附件数', '平均份数', '总大小中位',
                     '单笔最大', '说明'])
    for row in evidence_shape['status_cross_rows']:
        writer.writerow([
            row['label'], row['dispute_count'], row['evidence_count'],
            row['average_count'], row['median_total_label'], row['max_total_label'],
            '不足 %d 笔，只报规模、不给方向' % row['min_sample']
            if not row['gives_direction'] else row['note'],
        ])
    writer.writerow([
        '此处只报告，不判断「证据够不够」——平台没有能力判断一段聊天记录能不能'
        '证明「商品与描述不符」，这个判断只有处理人能做；不读文件内容、不 OCR、'
        '不看图片尺寸、不判断截图是否与订单有关。',
    ])
    response_quality = dashboard['notification_response']
    writer.writerow([])
    writer.writerow(['通知处理时效与触达质量'])
    writer.writerow(['口径与门槛'])
    response_summary = response_quality['summary']
    critical_summary = response_quality['critical_summary']
    writer.writerow([
        '周期发送', response_summary['sent'],
        '已处理', response_summary['handled'],
        '处理率（%）', response_summary['handling_rate'],
        '未处理', response_summary['unread'],
        '超一周未处理', response_summary['stale_unread'],
        '中位处理时长', response_summary['median_label'],
        'P90 处理时长', response_summary['p90_label'],
        '交易关键类型', critical_summary['count'],
        '交易关键发送', critical_summary['sent'],
        '交易关键已读', critical_summary['read'],
        '交易关键已读率（%）', critical_summary['read_rate'],
        '周期天数', response_quality['period_days'],
    ])
    writer.writerow([])
    writer.writerow(['通知类型', '代码', '交易关键', '发送', '已读', '未读',
                     '已读率（%）', '平均处理时长', '最快', '最慢', '需关注'])
    if not response_quality['has_data']:
        writer.writerow(['当前周期还没有通知数据。'])
    else:
        for row in response_quality['kind_rows']:
            writer.writerow([
                row['label'], row['kind'],
                '是' if row['is_business_critical'] else '',
                row['sent'], row['read'], row['unread'],
                row['read_rate'], row['avg_label'],
                row['fastest_label'], row['slowest_label'],
                '是' if row['needs_attention'] else '',
            ])
    writer.writerow([])
    writer.writerow(['处理时长分布', '条数', '占比（%）'])
    for bucket in response_quality['latency_buckets']:
        writer.writerow([bucket['label'], bucket['count'], bucket['share']])
    writer.writerow([])
    writer.writerow(['发送时段', '发送量', '该时段处理量'])
    for point in response_quality['hour_profile']:
        writer.writerow([point['label'], point['sent'], point['read']])
    writer.writerow([])
    writer.writerow(['运营建议'])
    for recommendation in response_quality['recommendations']:
        writer.writerow([recommendation])
    writer.writerow([])
    writer.writerow(['说明'])
    writer.writerow([
        '处理时长按"标记已读时间 − 通知创建时间"算，反映的是用户查看并处理提醒的'
        '快慢，不代表交易本身是否完成。',
    ])
    writer.writerow([
        '未读超过一周的通知按已忽略计，不算待处理：一个七天后才被点开的角标'
        '对用户已经不再有意义，把它留在待处理里会让处理时长虚高。',
    ])
    writer.writerow([
        '小时按本地时钟切，不按 UTC：CI 跑 UTC 而项目时区是东八区，'
        '免打扰时段是按用户墙上的钟设的，按 UTC 分桶会把"没人处理的时段"读错八小时。',
    ])
    writer.writerow([
        '此处只报告，不改变任何通知的发送时段、不折叠未读角标、不修改免打扰配置。',
    ])

    reminder_effect = dashboard['lifecycle_reminder_effect']
    writer.writerow([])
    writer.writerow(['生命周期提醒触达与行动'])
    writer.writerow(['口径与门槛'])
    reminder_summary = reminder_effect['summary']
    writer.writerow([
        '周期内提醒商品', reminder_summary['reminded_items'],
        '覆盖卖家', reminder_summary['reminded_sellers'],
        '提醒通知数', reminder_summary['notices'],
        '通知被打开', reminder_summary['read_notices'],
        '通知打开率（%）', reminder_summary['notice_read_rate'],
        '找到对应通知的商品', reminder_summary['reached_items'],
        '通知被打开的商品', reminder_summary['read_items'],
        '商品打开率（%）', reminder_summary['read_rate'],
        '提醒后有动作', reminder_summary['acted_after'],
        '其中刷新上架', reminder_summary['refreshed_after'],
        '其中完成交易', reminder_summary['completed_after'],
        '行动率（%）', reminder_summary['action_rate'],
        '看了但没动', reminder_summary['read_but_inactive'],
        '完全没动作', reminder_summary['untouched'],
        '观察窗口（天）', reminder_summary['window_days'],
        '事件台账口径商品', reminder_summary['event_backed_items'],
        '混合口径商品', reminder_summary['mixed_source_items'],
        '旧时间戳口径商品', reminder_summary['legacy_timestamp_items'],
        '周期天数', reminder_effect['period_days'],
    ])
    writer.writerow([])
    writer.writerow(['提醒类型', '口径', '商品数', '被打开', '打开率（%）', '有动作', '行动率（%）'])
    if not reminder_effect['has_data']:
        writer.writerow(['周期内还没有发送过生命周期提醒。'])
    else:
        for row in reminder_effect['kind_rows']:
            writer.writerow([
                row['label'], row['note'],
                row['items'], row['read'], row['read_rate'],
                row['acted'], row['action_rate'],
            ])
    writer.writerow([])
    writer.writerow(['生命周期提醒效果：按商品分类'])
    writer.writerow(['分类', '商品数', '对应通知', '已读商品', '提醒后刷新', '提醒后终态', '有动作', '行动率（%）', '样本'])
    if not reminder_effect['category_rows']:
        writer.writerow(['周期内没有可按分类统计的提醒商品。'])
    else:
        for row in reminder_effect['category_rows']:
            writer.writerow([
                row['category_name'], row['items'], row['reached_items'],
                row['read_items'], row['refreshed_after'], row['completed_after'],
                row['acted_after'], row['action_rate'],
                '足够' if row['comparable'] else '不足，只报数',
            ])
    writer.writerow([])
    writer.writerow(['生命周期提醒效果：按卖家本周期提醒负担'])
    writer.writerow(['本周期被提醒件数', '卖家数', '商品数', '已读商品', '有动作', '行动率（%）', '样本'])
    if not reminder_effect['seller_load_rows']:
        writer.writerow(['周期内没有可按提醒负担统计的卖家。'])
    else:
        for row in reminder_effect['seller_load_rows']:
            writer.writerow([
                row['label'], row['sellers'], row['items'], row['read_items'],
                row['acted_after'], row['action_rate'],
                '足够' if row['comparable'] else '不足，只报数',
            ])
    writer.writerow([])
    writer.writerow(['分层说明'])
    writer.writerow([
        '分组少于', reminder_effect['segment_threshold'],
        '件商品时只报数字，不判方向；分类表按名称排列，提醒负担表按固定档位排列，均不按行动率排序。',
    ])
    writer.writerow([
        '“本周期被提醒件数”来自周期内提醒时间戳，不代表卖家当前库存或长期规模；'
        '两层分组都不输出卖家用户名。',
    ])

    writer.writerow([])
    writer.writerow(['当前仍未真正收到', '商品数', '说明'])
    for row in reminder_effect['blocked_rows']:
        writer.writerow([row['label'], row['items'], row['note']])
    writer.writerow([])
    writer.writerow(['商品', '卖家', '提醒类型', '等级', '发布/刷新天数', '距提醒天数', '是否看过'])
    for row in reminder_effect['untouched_rows']:
        writer.writerow([
            row['item'].title, row['seller'].username,
            '、'.join('价格参考' if kind == 'price' else '待处理' for kind in row['kinds']),
            row['level_label'], row['age_days'], row['days_since_reminder'],
            '看过' if row['read'] else '未读',
        ])
    writer.writerow([])
    writer.writerow(['运营建议'])
    for recommendation in reminder_effect['recommendations']:
        writer.writerow([recommendation])
    writer.writerow([])
    writer.writerow(['说明'])
    writer.writerow([
        '这里只统计"提醒之后发生"的动作，不区分是否由提醒引起：卖家完全可能在'
        '提醒之前就已经在整理商品。所有比例都按提醒的时间戳切开计算，'
        '读成相关性而不是因果。',
    ])
    writer.writerow([
        '动作只认两个观测到的写入点：主动刷新看 last_refreshed_at，成交看订单走到'
        '终态的时间。编辑商品不算动作，因为任何保存都会改写更新时间。',
    ])
    writer.writerow([
        '三个提醒类型桶互斥：同时收到两类提醒的商品单独成桶，只报告不比较，'
        '否则同一个卖家的动作无法归因给任何一类。',
    ])
    writer.writerow([
        '"当前仍未真正收到"用当前的偏好与免打扰时段重算，不是发送当天的历史快照：'
        '发送时的计数只存在于命令的返回值里，从来没有被存下来。',
    ])
    writer.writerow([
        '此处只报告，不补发提醒、不修改提醒阈值、不按行动率给分类或卖家排序。',
    ])

    trend_runs = dashboard['task_run_trend']
    action_trend = dashboard['lifecycle_reminder_action_trend']
    writer.writerow([])
    writer.writerow(['生命周期提醒的跨周期行动率'])
    writer.writerow(['口径与门槛'])
    writer.writerow([
        '当前周期起始', action_trend['current_period_start'].strftime('%Y-%m-%d'),
        '上一周期起始', action_trend['previous_period_start'].strftime('%Y-%m-%d'),
        '周期天数', action_trend['days'],
        '最小被提醒商品数', action_trend['min_period_items'],
        '方向阈值（百分点）', action_trend['trend_delta_points'],
        '样本是否足够', '是' if action_trend['has_sample'] else '否',
    ])
    writer.writerow([])
    writer.writerow(['指标', '本周期', '上一周期', '差值', '方向'])
    for row in action_trend['metric_rows']:
        writer.writerow([
            row['label'],
            row['current'] if row['current'] is not None else '—',
            row['previous'] if row['previous'] is not None else '—',
            row['delta_display'],
            row['direction_label'] if row['has_direction'] else '不判方向',
        ])
    writer.writerow([])
    writer.writerow(['提醒类型', '本周期商品', '本周期行动率（%）', '上一周期行动率（%）', '方向'])
    if not action_trend['kind_rows']:
        writer.writerow(['两个周期内都没有可归类的提醒。'])
    else:
        for row in action_trend['kind_rows']:
            writer.writerow([
                row['label'], row['current_items'],
                row['current_action_rate'] if row['current_action_rate'] is not None else '—',
                row['previous_action_rate'] if row['previous_action_rate'] is not None else '—',
                row['direction_label'] if row['has_direction'] else '不判方向',
            ])
    writer.writerow([])
    writer.writerow(['运营建议'])
    writer.writerow([action_trend['summary']])
    writer.writerow([])
    writer.writerow(['说明'])
    writer.writerow([
        '只有行动率带方向。打开通知和动手整理是两件事：打开率涨了只说明文案被看见，' +
        '不说明提醒更有用。被提醒商品数也不判方向，发得更多只代表这个周期更忙。',
    ])
    writer.writerow([
        '两类同时触达的商品单独成桶且只报数字：一个既收到待处理提醒又收到价格证据的动作，' +
        '无法归因给任何一类，给它判方向等于把噪声读成信号。',
    ])
    writer.writerow([
        '一个周期里没有任何提醒时，行动率不存在而不是零。上一周期有提醒而本期没有，' +
        '报的是「本期无提醒」，不是「行动率降到 0」。',
    ])
    writer.writerow([
        '本周期还在走，最后一天是残缺的；上一周期是完整的。两个窗口都按本地零点切分，' +
        '与站内其他跨周期面板同一套边界。',
    ])
    writer.writerow([
        '此处只报告，不改发送频率、不改阈值、不自动调频、不按行动率给卖家排序。' +
        '比例说的是「提醒之后有动作的比例变了」，不是「提醒变得更有说服力了」。',
    ])

    term_trend = dashboard['lifecycle_reminder_term_trend']
    writer.writerow([])
    writer.writerow(['生命周期提醒的跨学期终态转化'])
    writer.writerow(['口径与门槛'])
    writer.writerow([
        '回看天数', term_trend['days'],
        '观察窗口（天）', term_trend['window_days'],
        '最小事件口径商品数', term_trend['min_items'],
        '值得回看差值（百分点）', term_trend['gap_points'],
        '已封闭同学期阶段格子', term_trend['closed_cell_count'],
        '无学期日历事件数', term_trend['no_term_events'],
        '学期内未划分阶段事件数', term_trend['unassigned_events'],
        '观察窗口未满事件数', term_trend['open_cell_events'],
    ])
    writer.writerow([])
    writer.writerow([
        '阶段', '学期', '阶段区间', '事件数', '事件口径商品数', '卖家数',
        '重复提醒比（%）', '终态动作数', '终态动作率（%）', '样本',
        '上一学期', '上一学期终态动作率（%）', '差值（百分点）', '方向', '值得回看',
    ])
    if not term_trend['phase_groups']:
        writer.writerow(['回看期内还没有已封闭的同学期阶段格子。'])
    else:
        for group in term_trend['phase_groups']:
            current = group['current']
            previous = group['previous']
            writer.writerow([
                group['label'],
                current['term_name'],
                f"{current['start_date']:%Y-%m-%d} 至 {current['end_date']:%Y-%m-%d}",
                current['event_count'],
                current['item_count'],
                current['seller_count'],
                current['repeat_reminder_rate'] if current['repeat_reminder_rate'] is not None else '—',
                current['terminal_action_count'],
                current['terminal_action_rate'] if current['terminal_action_rate'] is not None else '—',
                '足样本' if current['has_sample'] else '样本不足',
                previous['term_name'] if previous else '—',
                previous['terminal_action_rate'] if previous and previous['terminal_action_rate'] is not None else '—',
                group['gap_points'] if group['gap_points'] is not None else '—',
                group['direction_label'] or ('不判方向' if previous else '无上一学期基线'),
                '是' if group['is_flagged'] else '否',
            ])
    writer.writerow([])
    writer.writerow(['运营读数'])
    writer.writerow([term_trend['summary']])
    writer.writerow([])
    writer.writerow(['说明'])
    writer.writerow([term_trend['source_note']])
    writer.writerow([term_trend['refresh_note']])
    writer.writerow([
        '终态动作只说明出现在最晚提醒后的 14 天内，不说明由提醒引起；'
        '事件数、商品数、卖家数和重复提醒比都只是规模，不带方向。',
    ])
    writer.writerow([
        '无学期日历、学期内未划分阶段和观察窗口未满的事件均单独计数；'
        '本面板不回填伪事件、不输出卖家身份、不排名、不自动调频。',
    ])

    writer.writerow([])
    writer.writerow(['定时任务健康度的跨周期趋势'])
    writer.writerow(['口径与门槛'])
    writer.writerow([
        '当前周期起始', trend_runs['current_period_start'].strftime('%Y-%m-%d'),
        '上一周期起始', trend_runs['previous_period_start'].strftime('%Y-%m-%d'),
        '周期天数', trend_runs['days'],
        '最小运行次数', trend_runs['min_period_runs'],
        '方向阈值（百分点）', trend_runs['trend_delta_points'],
        '耗时漂移阈值（%）', trend_runs['duration_drift_percent'],
        '异常慢倍数', trend_runs['slow_run_factor'],
    ])
    writer.writerow([])
    writer.writerow(['指标', '本周期', '上一周期', '差值', '方向'])
    for row in trend_runs['metric_rows']:
        writer.writerow([
            row['label'],
            '' if row['current'] is None else row['current'],
            '' if row['previous'] is None else row['previous'],
            row['delta_display'],
            row['direction_label'],
        ])

    writer.writerow([])
    writer.writerow([
        '命令', '本周期运行', '上一周期运行',
        '本周期失败', '上一周期失败',
        '本周期成功率（%）', '上一周期成功率（%）',
        '本周期耗时中位数（毫秒）', '上一周期耗时中位数（毫秒）',
        '本周期最长耗时（毫秒）',
        '本周期异常慢', '方向',
    ])
    for row in trend_runs['commands']:
        writer.writerow([
            row['name'],
            row['current']['run_count'], row['previous']['run_count'],
            row['current']['failed_count'], row['previous']['failed_count'],
            '' if row['current']['success_rate'] is None else row['current']['success_rate'],
            '' if row['previous']['success_rate'] is None else row['previous']['success_rate'],
            '' if row['current']['median_duration_ms'] is None else row['current']['median_duration_ms'],
            '' if row['previous']['median_duration_ms'] is None else row['previous']['median_duration_ms'],
            '' if row['current']['max_duration_ms'] is None else row['current']['max_duration_ms'],
            row['current']['slow_run_count'],
            row['direction_label'] if row['has_sample'] else '样本不足',
        ])

    writer.writerow([])
    writer.writerow(['说明'])
    writer.writerow([trend_runs['summary']])
    writer.writerow(['一次运行按它开始的时刻归期；按结束时刻归期会让卡在边界上的那次算进下一个周期，于是坏周期会报成“好周期之后跟着一个慢周期”。'])
    writer.writerow(['耗时看中位数、最长一次和异常慢的次数，不看平均值；一次跑了两个小时的运行会把平均值抬起来，而那个数字描述的其实是从未发生过的运行。'])
    writer.writerow(['运行次数不给方向：跑得更多只说明这个周期更忙，不代表更健康。'])
    writer.writerow(['此处只报告，不改 stale 阈值、不自动重试、不改调度配置。'])

    run_health = dashboard['task_run_health']
    writer.writerow([])
    writer.writerow(['定时任务运行台账与健康统计'])
    writer.writerow(['口径与门槛'])
    writer.writerow([
        '周期内运行', run_health['total_runs'],
        '失败', run_health['total_failed'],
        '成功率（%）', '' if run_health['success_rate_percent'] is None else run_health['success_rate_percent'],
        '静默命令', run_health['stale_count'],
        '正在连续失败', run_health['failing_count'],
        '静默判定天数', run_health['stale_after_days'],
        '周期天数', run_health['days'],
    ])
    writer.writerow([])
    writer.writerow(['命令', '周期内运行', '失败', '成功率（%）', '最近状态',
                     '最近运行', '最近成功', '静默', '连续失败',
                     '平均耗时（毫秒）', '累计处理'])
    for row in run_health['commands']:
        writer.writerow([
            row['name'],
            row['run_count'], row['failed_count'],
            '' if row['success_rate'] is None else round(row['success_rate'] * 100, 1),
            {'failed': '失败', 'succeeded': '成功'}.get(row['last_status'], '无记录'),
            '' if row['last_run_at'] is None else row['last_run_at'].strftime('%Y-%m-%d %H:%M'),
            '' if row['last_success_at'] is None else row['last_success_at'].strftime('%Y-%m-%d %H:%M'),
            row['silence_label'],
            row['consecutive_failures'],
            '' if row['avg_duration_ms'] is None else row['avg_duration_ms'],
            '；'.join(f'{key} {value}' for key, value in row['metrics_total'].items()),
        ])
    writer.writerow([])
    writer.writerow(['调度建议'])
    for recommendation in run_health['recommendations']:
        writer.writerow([recommendation])
    writer.writerow([])
    writer.writerow(['说明'])
    writer.writerow([run_health['summary']])
    writer.writerow([
        '静默天数按最近一次成功运行计算，不按任意一次运行算：一直失败的命令同样'
        '每天都在跑，按任意运行算会把它伪装成健康的。',
    ])
    writer.writerow([
        '--dry-run 只统计发送范围、不写入任何通知，默认不计入成功率：一个只做过'
        '预演的命令没有证明过任何事。',
    ])
    writer.writerow([
        '失败记录写在独立的短事务里，业务事务回滚也不会带走失败证据，'
        '异常仍会原样抛出，不会因为接了台账就被吞掉。',
    ])
    writer.writerow([
        '此处只报告，不重跑命令、不改调度配置、不把统计写回台账。',
    ])
    writer.writerow([])
    writer.writerow(['命令耗时的分位分布'])
    percentiles = dashboard['task_run_duration_percentiles']
    writer.writerow([
        '窗口起始', percentiles['window_start'].strftime('%Y-%m-%d'),
        '窗口结束', percentiles['window_end'].strftime('%Y-%m-%d %H:%M'),
        '周期天数', percentiles['days'],
    ])
    writer.writerow([])
    writer.writerow([
        '命令', '运行次数', '成功次数', '失败次数',
        'P50（毫秒）', 'P90（毫秒）', 'P99（毫秒）',
        'P50 样本', 'P90 样本', 'P99 样本', '尾部倍数',
    ])
    for row in percentiles['rows']:
        writer.writerow([
            row['name'],
            row['run_count'], row['succeeded_count'], row['failed_count'],
            '' if row['percentiles']['P50'] is None else row['percentiles']['P50'],
            '' if row['percentiles']['P90'] is None else row['percentiles']['P90'],
            '' if row['percentiles']['P99'] is None else row['percentiles']['P99'],
            '足够' if row['sample_enough']['P50'] else '不足',
            '足够' if row['sample_enough']['P90'] else '不足',
            '足够' if row['sample_enough']['P99'] else '不足',
            '' if row['tail_share'] is None else row['tail_share'],
        ])

    writer.writerow([])
    writer.writerow(['说明'])
    writer.writerow(['分位数取最近秩，也就是某次真实发生过的运行，不是插值出来的值：插值 P90 会落在第九次和第十次之间，那是一次从未发生过的运行。'])
    writer.writerow(['失败运行不进分位：失败可能毫秒级退出，也可能挂到被调度器杀掉，算进去会让同一个数字同时描述“多常坏”和“好的时候多慢”。'])
    writer.writerow(['样本不够时不声称该分位：十次运行的最近秩 P99 就是最大值，贴上 P99 的标签会让读者以为尾巴被量过，其实只看见了一次运行。'])
    writer.writerow(['尾部倍数取已声称的最高分位与 P50 的差：样本不足 P99 时退到 P90，而不是报告“没有尾巴”。'])
    writer.writerow(['运行时长的按小时分布'])
    hour_load = dashboard['task_run_hour_load']
    writer.writerow([
        '本周期起始', hour_load['window_start'].strftime('%Y-%m-%d'),
        '本周期结束', hour_load['window_end'].strftime('%Y-%m-%d %H:%M'),
        '上一周期起始', hour_load['previous_window_start'].strftime('%Y-%m-%d'),
        '周期天数', hour_load['days'],
        '拥塞门槛', u'至少 {} 次运行'.format(hour_load['min_hour_runs']),
        '变慢倍数', hour_load['slowdown_factor'],
    ])
    writer.writerow([])
    writer.writerow([
        u'本地小时', u'运行次数', u'成功次数', u'失败次数', u'命令数',
        u'本周期耗时中位数（毫秒）', u'本周期最长耗时（毫秒）',
        u'上一周期同一小时运行次数', u'上一周期同一小时耗时中位数（毫秒）',
        u'是否拥塞', u'这一小时比自身平时慢的命令',
    ])
    for row in hour_load['hours']:
        writer.writerow([
            row['label'],
            row['run_count'], row['succeeded_count'], row['failed_count'],
            row['command_count'],
            '' if row['median_duration_ms'] is None else row['median_duration_ms'],
            '' if row['max_duration_ms'] is None else row['max_duration_ms'],
            row['previous_run_count'],
            '' if row['previous_median_duration_ms'] is None else row['previous_median_duration_ms'],
            u'拥塞' if row['is_crowded'] else '',
            u'、'.join(row['slow_commands']),
        ])

    writer.writerow([])
    writer.writerow([u'说明'])
    writer.writerow([hour_load['summary']])
    writer.writerow([u'小时按本地时钟切，不按 UTC：CI 跑 UTC 而项目时区是东八区，本地凌晨两点的那次运行在 UTC 里是前一天傍晚六点，按 UTC 分桶会把拥塞报在一个没人配过的小时刻上。'])
    writer.writerow([u'拥塞要同时满足两个条件：这一小时至少有门槛那么多次运行，且其中有命令比它自己的全周期中位数慢到变慢倍数。只满足一个都不算，否则“忙”会被读成“堵”。'])
    writer.writerow([u'基线用命令自己的全周期中位数，不是小时之间互比：一条每次都慢的命令不该让它跑过的每个小时都显得拥塞，那是按速度给命令排名，不是发现队列。'])
    writer.writerow([u'一次运行按它开始的时刻归期，卡在小时边界上的那次归它开始的那个小时，不归它结束的那个小时。'])
    writer.writerow([u'只报最忙的若干个小时，样本少的小时不判拥塞。'])
    writer.writerow([u'此处只报告，不改调度配置、不自动挪任务、不把统计写回台账。'])
    writer.writerow([])
    writer.writerow(['定时任务失败原因分类'])
    writer.writerow(['口径与门槛'])
    causes = dashboard['task_run_failure_causes']
    writer.writerow([
        '周期天数', causes['days'],
        '失败总数', causes['total_failed'],
        '已归类', causes['classified_count'],
        '未归类', causes['unclassified_count'],
        '最大类别', causes['top_cause'] or '—',
    ])
    writer.writerow([])
    writer.writerow([
        '原因类别', '失败次数', '已归类失败数', '未归类失败数',
        '涉及命令数', '最近一次失败', '最近一次备注',
    ])
    if not causes['has_data']:
        writer.writerow(['周期内没有失败的运行记录。'])
    else:
        for row in causes['causes']:
            writer.writerow([
                row['label'],
                row['count'],
                causes['classified_count'],
                causes['unclassified_count'],
                row['command_count'],
                '' if row['last_failure_at'] is None else row['last_failure_at'].strftime('%Y-%m-%d %H:%M'),
                row['last_note'] or '',
            ])

    writer.writerow([])
    writer.writerow(['说明'])
    writer.writerow([causes['summary']])
    writer.writerow(['只统计失败的运行，成功运行不进任何类别。'])
    writer.writerow(['dry-run 的运行不算数：一次什么都没发的运行没法替任何事作证，它的失败也说明不了调度。'])
    writer.writerow(['备注里的异常名按冒号前的部分取；取不到可识别的异常名就归到未归类，不猜——归错类比不归类更糟，它把人送去错误的地方翻。'])
    writer.writerow(['分类只说明失败长什么样，不代表严重程度；同一个异常名可能是致命的配置错误，也可能只是“今天没有到期订单”的正常提前返回。'])
    writer.writerow(['此处只报告，不改分类映射表、不自动重试、不改调度配置。'])
    writer.writerow([])

    writer.writerow(['失败原因的跨周期变化'])
    cause_trend = dashboard['task_run_failure_cause_trend']
    writer.writerow([
        '本周期起始', cause_trend['window_start'].strftime('%Y-%m-%d'),
        '上一周期起始', cause_trend['previous_window_start'].strftime('%Y-%m-%d'),
        '周期天数', cause_trend['days'],
        '类别门槛', cause_trend['min_cause_runs'],
        '周期门槛', cause_trend['min_period_failures'],
        '份额漂移阈值（%）', cause_trend['cause_share_drift_percent'],
    ])
    writer.writerow([])
    writer.writerow([
        '原因类别', '本周期失败次数', '上一周期失败次数', '差值',
        '本周期份额（%）', '上一周期份额（%）', '方向',
        '本期新出现', '涉及命令数', '最近一次失败', '最近一次备注',
    ])
    if not cause_trend['has_data']:
        writer.writerow(['两个周期内都没有失败的运行记录。'])
    else:
        for row in cause_trend['causes']:
            writer.writerow([
                row['label'],
                row['current_display'],
                row['previous_display'],
                row['delta_display'],
                '' if row['current_share'] is None else row['current_share'],
                '' if row['previous_share'] is None else row['previous_share'],
                row['direction_label'],
                '是' if row['previous_count'] is None else '',
                row['command_count'],
                '' if row['last_failure_at'] is None else row['last_failure_at'].strftime('%Y-%m-%d %H:%M'),
                row['last_note'] or '',
            ])

    writer.writerow([])
    writer.writerow(['口径与门槛'])
    writer.writerow(['本周期失败总数', cause_trend['total_failed']])
    writer.writerow(['上一周期失败总数', cause_trend['previous_total_failed']])
    writer.writerow([])
    writer.writerow(['说明'])
    writer.writerow([cause_trend['summary']])
    writer.writerow(['一次运行按它开始的时刻归期，和上方的调度趋势面板同一个边界：两个面板对“这次失败算哪一期”必须给出同一个答案。'])
    writer.writerow(['方向按“占本周期失败总数的比例”判，不按次数差判：失败次数是原始计数，没有百分点这个尺度，只有本来就是百分比的数字才有。'])
    writer.writerow(['周期内失败总数要过门槛才判方向；类别本身也要过门槛——一次失败不是一种形态，两个周期各一次也读不出走向。'])
    writer.writerow(['份额就是占该周期失败总数的比例，所以份额的变化同时反映这一类的起落和整体的厚薄；整体失败变多时，单个类别的份额也可能下降。'])
    writer.writerow(['此处只报告，不改分类映射表、不自动重试、不改调度配置。'])
    writer.writerow([])

    writer.writerow([u'命令的自我重叠'])
    overlap = dashboard['task_run_overlap']
    writer.writerow([
        u'周期起始', overlap['window_start'].strftime('%Y-%m-%d'),
        u'周期结束', overlap['window_end'].strftime('%Y-%m-%d %H:%M'),
        u'周期天数', overlap['days'],
        u'节奏门槛', u'至少 {} 次运行'.format(overlap['min_runs_for_rhythm']),
        u'重叠对数门槛', overlap['min_overlap_pairs'],
        u'变慢倍数', overlap['overlap_slowdown'],
        u'逼近间隔比值', overlap['approaching_interval'],
    ])
    writer.writerow([])
    writer.writerow([
        u'命令', u'运行次数', u'成功次数',
        u'运行间隔（毫秒）', u'间隔样本数',
        u'最长耗时（毫秒）', u'最长耗时与间隔之比',
        u'重叠对数', u'重叠时耗时中位数（毫秒）',
        u'不重叠时耗时中位数（毫秒）',
        u'重叠代价', u'自我重叠', u'正在逼近',
        u'最后一次开始',
    ])
    if not overlap['has_data']:
        writer.writerow([u'窗口内还没有已记录的定时任务运行。'])
    else:
        for row in overlap['rows']:
            writer.writerow([
                row['name'],
                row['run_count'], row['succeeded_count'],
                '' if row['interval_ms'] is None else row['interval_ms'],
                row['interval_runs'],
                '' if row['longest_run_ms'] is None else row['longest_run_ms'],
                row['longest_run_ratio_display'],
                row['overlap_pair_count'],
                '' if row['overlapped_median_ms'] is None else row['overlapped_median_ms'],
                '' if row['clean_median_ms'] is None else row['clean_median_ms'],
                row['overlap_cost_display'],
                u'是' if row['is_self_overlapping'] else '',
                u'是' if row['is_approaching'] else '',
                '' if row['last_started_at'] is None else row['last_started_at'].strftime('%Y-%m-%d %H:%M'),
            ])

    writer.writerow([])
    writer.writerow([u'说明'])
    writer.writerow([overlap['summary']])
    writer.writerow([u'运行间隔从台账反推，不读 crontab：本项目没有这个数据源，而且即使有，crontab 记的是写下来的安排而不是真正跑出来的节奏。'])
    writer.writerow([u'间隔取相邻两次开始时刻的中位数，不取平均值：一次漏跑会让某一个间隔翻倍，平均值会报出一个从未发生过的节奏。'])
    writer.writerow([u'重叠按台账里真实发生过的成对运行数，不按比值推断：前一次的开始加它自己的耗时晚于后一次的开始，才算一对。'])
    writer.writerow([u'比值报在对数旁边，从不取代对数：比值是推测，而对数是两行都被记录过时间戳的运行。'])
    writer.writerow([u'重叠必须有代价才算问题：两条各自等一个外部的东西，那个东西变慢了，重叠越来越多而什么都没有退化，这种重叠只报数字。'])
    writer.writerow([u'正在逼近用观测到的最长一次运行当基数，不用分位：决定一次运行会不会撞上下一次的是真正跑过最长的那次，分位会把它抹掉。'])
    writer.writerow([u'只看当前窗口，不给间隔拿上一周期：跳过窗口起点的那一个间隔说的是窗口在哪里被切开，不是命令的节奏。'])
    writer.writerow([u'此处只报告，不改调度配置、不自动挪任务、不把统计写回台账。'])
    writer.writerow([])
    writer.writerow(['此处只报告，不拆分命令、不改调度配置、不把统计写回台账。'])

    writer.writerow([])
    writer.writerow(['定时任务台账完整性审计'])
    ledger_integrity = dashboard['task_run_ledger_integrity']
    writer.writerow([
        '周期起始', ledger_integrity['window_start'].strftime('%Y-%m-%d'),
        '周期结束', ledger_integrity['window_end'].strftime('%Y-%m-%d %H:%M'),
        '周期天数', ledger_integrity['days'],
        '未来时钟余量（分钟）', ledger_integrity['future_clock_tolerance_minutes'],
        '严重耗时不一致下限（毫秒）', ledger_integrity['duration_mismatch_base_ms'],
        '耗时不一致比例门槛（%）', ledger_integrity['duration_mismatch_share_percent'],
        '问题行清单上限', ledger_integrity['issue_row_limit'],
    ])
    writer.writerow([])

    writer.writerow(['一、台账形状'])
    writer.writerow(['口径与门槛'])
    summary = ledger_integrity['summary']
    writer.writerow([
        '台账行', summary['run_total'],
        '成功行', summary['succeeded_total'],
        '失败行', summary['failed_total'],
        'dry-run 行', summary['dry_run_total'],
    ])
    writer.writerow([
        '已知命令行', summary['known_command_total'],
        '未知命令行', summary['unknown_command_total'],
        '问题行', summary['issue_row_total'],
        '干净行', summary['clean_row_total'],
    ])
    writer.writerow([
        '问题行占比（%）', '' if summary['issue_row_share'] is None else summary['issue_row_share'],
        '需指标的成功行', summary['metric_required_total'],
        '指标可读行', summary['metric_readable_total'],
        '指标可读占比（%）', '' if summary['metric_readable_share'] is None else summary['metric_readable_share'],
        '未知指标键次数', summary['unknown_metric_key_total'],
    ])
    writer.writerow([])

    writer.writerow(['二、问题类型'])
    writer.writerow(['问题类型', '行数', '涉及命令数', '读法'])
    for row in ledger_integrity['issue_type_rows']:
        writer.writerow([
            row['label'], row['row_count'], row['command_count'],
            '固定问题类型，空平台也保留这一行',
        ])
    writer.writerow([])

    writer.writerow(['三、命令形状'])
    writer.writerow([
        '命令', '运行次数', '问题行数', '时间问题行数',
        '指标问题行数', '失败备注问题行数', '未知指标键次数', '命令名单',
    ])
    if not ledger_integrity['command_rows']:
        writer.writerow(['窗口内没有台账行。', '', '', '', '', '', '', ''])
    else:
        for row in ledger_integrity['command_rows']:
            writer.writerow([
                row['name'], row['run_count'], row['issue_row_count'],
                row['time_issue_row_count'], row['metric_issue_row_count'],
                row['missing_failure_note_row_count'],
                row['unknown_metric_key_count'],
                '已知命令' if row['is_known'] else '未知命令，单列不判错',
            ])
    writer.writerow([])

    writer.writerow(['四、问题行清单'])
    writer.writerow(['台账行', '命令', '开始时间', '问题数', '问题'])
    if not ledger_integrity['issue_rows']:
        writer.writerow(['暂无问题行。', '', '', '', ''])
    else:
        for row in ledger_integrity['issue_rows']:
            writer.writerow([
                row['id'], row['name'], row['started_at'].strftime('%Y-%m-%d %H:%M'),
                row['issue_count'], '、'.join(row['issue_labels']),
            ])
    if ledger_integrity['issue_row_hidden_total']:
        writer.writerow([
            '未展示问题行', ledger_integrity['issue_row_hidden_total'], '', '', '',
        ])
    writer.writerow([])

    writer.writerow(['说明'])
    writer.writerow(['duration_ms 来自单调时钟，started_at / finished_at 来自墙钟；只有差异超过 2000 毫秒或时间戳间隔的 10% 才算严重不一致。'])
    writer.writerow(['指标可读率的分母只有成功且非 dry-run 的行；失败行不要求指标，dry-run 不承诺发送。'])
    writer.writerow(['未知命令和未知指标键只单列，不计为问题行；它们提醒名单或字段清单需要扩展。'])
    writer.writerow(['本模块不输出原始失败备注，不修复台账、不回填指标、不改调度配置。'])
    writer.writerow([])

    writer.writerow([])
    writer.writerow(['需求雷达命中率的跨周期趋势'])
    hit_trend = dashboard['demand_radar_hit_trend']
    writer.writerow([
        '当前周期起始', hit_trend['current_period_start'].strftime('%Y-%m-%d'),
        '上一周期起始', hit_trend['previous_period_start'].strftime('%Y-%m-%d'),
        '周期天数', hit_trend['days'],
        '最小判定任务数', hit_trend['min_judged_tasks'],
        '方向阈值（百分点）', hit_trend['trend_delta_points'],
    ])
    writer.writerow([])
    writer.writerow(['指标, 本周期, 上一周期, 差值, 方向'])
    for row in hit_trend['metric_rows']:
        writer.writerow([
            row['label'],
            '' if row['current'] is None else row['current'],
            '' if row['previous'] is None else row['previous'],
            row['delta_display'],
            row['direction_label'],
        ])
    writer.writerow([])
    writer.writerow([
        '本周期创建任务', hit_trend['current']['task_count'],
        '本周期有证据', hit_trend['current']['judged_count'],
        '本周期命中', hit_trend['current']['converged_count'],
        '上一周期创建任务', hit_trend['previous']['task_count'],
        '上一周期有证据', hit_trend['previous']['judged_count'],
        '上一周期命中', hit_trend['previous']['converged_count'],
    ])
    writer.writerow([])
    writer.writerow(['说明'])
    writer.writerow([hit_trend['summary']])
    writer.writerow(['任务按创建时间归期：一个任务在创建那一刻的证据就固定了，把它算进结果落地的那个周期，等于让一个安静月继承忙碌月的成绩。'])
    writer.writerow(['每个任务按自己的观察期结束时刻判定，所以上一周期里最早创建的任务不会被误判成还没到判定时刻而丢出分母。'])
    writer.writerow(['命中率只统计有证据的任务；基线期或观察期搜索量不足的任务单独数出来，既不算成功也不算失败。'])
    writer.writerow(['这里只报告，不把命中率回流到机会分——雷达学会掩盖自己的误报，比误报本身更糟。'])

    writer.writerow([])
    writer.writerow(['需求雷达闭环的分类与地点分层'])
    hit_layers = dashboard['demand_radar_hit_layers']
    writer.writerow([
        '统计周期', hit_layers['days'],
        '每层最少有证据任务', hit_layers['min_judged_tasks'],
        '标记差值阈值（百分点）', hit_layers['gap_points'],
        '整体命中率',
        '' if hit_layers['overall']['hit_rate'] is None else hit_layers['overall']['hit_rate'],
    ])
    writer.writerow([])
    for group in hit_layers['facet_groups']:
        writer.writerow([f"按{group['label']}分层"])
        writer.writerow([
            '层', '任务数', '有证据', '命中率（%）',
            '与整体差值（百分点）',
            '中位收敛幅度（百分点）',
            '样本不足', '已标记',
        ])
        if not hit_layers['has_data']:
            writer.writerow([
                '这个周期内还没有创建跟进任务，'
                '分层命中率需要先有任务发生。',
            ])
        else:
            for layer in group['layers']:
                writer.writerow([
                    layer['label'],
                    layer['task_count'],
                    layer['judged_count'],
                    '' if layer['hit_rate'] is None else layer['hit_rate'],
                    '' if layer['gap_points'] is None else layer['gap_points'],
                    '' if layer['median_delta_points'] is None else layer['median_delta_points'],
                    layer['insufficient_count'],
                    '已标记' if layer['is_flagged'] else '',
                ])
        writer.writerow([])
    writer.writerow(['说明'])
    writer.writerow([hit_layers['summary']])
    writer.writerow([
        '命中率是层内收敛任务占该层有证据任务的比例，'
        '只跟层内的命中率比，不跟该层占全部任务的比例比；'
        '任务数只说明证据厚度，不归一到占比。',
    ])
    writer.writerow([
        '每层各自要过 ',
        hit_layers['min_judged_tasks'],
        ' 个有证据任务的门槛，'
        '门槛不会为了表格好看而放开；'
        '低于门槛的层只报数字，不给方向。',
    ])
    writer.writerow([
        '分层上的差值只说明走向，'
        '任务数少的分层上的差值不算结论；'
        '这里也不排序、不改机会分。',
    ])
    writer.writerow([
        '没有分类或地点的任务落在“',
        hit_layers['unassigned_label'],
        '”层，不会被丢掉——'
        '丢了分层就加不回整体。',
    ])
    writer.writerow([])
    writer.writerow(['需求雷达命中率的学期纵向对比'])
    writer.writerow(['口径与门槛'])
    phase_trend = dashboard['demand_radar_phase_trend']
    writer.writerow([
        '回看天数', phase_trend['days'],
        '每个阶段最少有证据任务', phase_trend['min_judged_tasks'],
        '标记差值阈值（百分点）', phase_trend['gap_points'],
        '涉及学期数', phase_trend['term_count'],
        '被标记阶段', phase_trend['flagged_count'],
        '等待证据的阶段格子', phase_trend['waiting_cell_count'],
    ])
    writer.writerow([])
    for group in phase_trend['phase_groups']:
        writer.writerow([
            f"阶段：{group['label']}"
            + ('（日历覆盖范围，不参与跳学期对比）'
               if group['is_synthetic'] else ''),
        ])
        writer.writerow([
            '学期', '任务数', '有证据',
            '命中率（%）',
            '中位收敛幅度（百分点）',
            '样本不足',
        ])
        if not phase_trend['has_data']:
            writer.writerow([
                '这个回看期内还没有创建跟进任务，'
                '学期纵向对比需要先有任务发生。',
            ])
        else:
            for cell in group['cells']:
                writer.writerow([
                    cell['term_name'] or '—',
                    cell['task_count'],
                    cell['judged_count'],
                    '' if cell['hit_rate'] is None else cell['hit_rate'],
                    '' if cell['median_delta_points'] is None
                    else cell['median_delta_points'],
                    '' if cell['has_sample'] else '样本不足',
                ])
        writer.writerow([])

    if phase_trend['comparisons']:
        writer.writerow([
            '同一阶段在相邻两个学期之间的对比',
        ])
        writer.writerow([
            '阶段', '本学期命中率（%）',
            '上一学期命中率（%）',
            '差值（百分点）',
            '样本不足', '已标记',
        ])
        for item in phase_trend['comparisons']:
            current = item['current']
            previous = item['previous']
            writer.writerow([
                item['phase_label'],
                '' if current['hit_rate'] is None else current['hit_rate'],
                '' if previous is None or previous['hit_rate'] is None
                else previous['hit_rate'],
                '' if item['gap_points'] is None else item['gap_points'],
                '' if item['has_sample'] else '样本不足',
                '已标记' if item['is_flagged'] else '',
            ])

    writer.writerow([])
    writer.writerow(['说明'])
    writer.writerow([phase_trend['summary']])
    writer.writerow([
        '每个阶段按任务自己的创建日归期，'
        '用的是学期历自己的区间，'
        '因此一个任务在这里所属的阶段'
        '与其余面板所说的一致。',
    ])
    writer.writerow([
        '比较只取相邻两个学期的同一个阶段：'
        '逆季节的需求本就不同，'
        '把上个月当成上个学期比，'
        '会把季节性读成雷达在漂移。',
    ])
    writer.writerow([
        '只在一个学期出现过的阶段不给方向：'
        '没有同类的早期阶段可减，'
        '而前三十天是另一个季节。',
    ])
    writer.writerow([
        '任务按自己的观察期结束时刻判定，'
        '不按 now，否则上个学期的任务会全部置为待定，'
        '而这正是跳学期对比要读的那批工作。',
    ])
    writer.writerow([
        '每个阶段格子各自要过 ',
        phase_trend['min_judged_tasks'],
        ' 个有证据任务的门槛，'
        '门槛不会为了让表格好看而放开；'
        '低于门槛的格子只报数字，不给方向。',
    ])
    writer.writerow([
        '阶段上的差值只说明走向，'
        '任务数少的阶段上的差值不算结论；'
        '这里也不排序、不改机会分。',
    ])
    writer.writerow([
        '早于第一个学期开始配置、'
        '或落在学期内未划分阶段的任务，'
        '分别落在“',
        phase_trend['no_term_label'],
        '”和“',
        phase_trend['unassigned_phase_label'],
        '”，不会被丢掉——'
        '丢了就会缩小分母，'
        '让每个阶段的命中率安静地变大。',
    ])
    writer.writerow([])
    chat_response = dashboard['chat_response']
    writer.writerow(['私信响应时效'])
    writer.writerow([
        '一条私信隔了多久被打开，第一次被运营读到', '数值', '说明',
    ])
    writer.writerow([
        '口径与门槛', chat_response['summary'],
        '只读 PrivateMessage 的 sender、receiver、is_read、read_at 和 created_at，'
        '不改写任何一条私信、不替谁回复、不催回复、'
        '不把打开时长写回任何人的信誉分、不对对话双方暴露"对方已读"',
    ])
    writer.writerow([
        '周期内发出', chat_response['summary']['sent'], '',
        '去重后的私信条数，含自己发给自己的会话',
    ])
    writer.writerow([
        '已被打开', chat_response['summary']['opened'], '',
        f"占 {chat_response['summary']['open_rate']}%",
    ])
    writer.writerow([
        '中位打开时长', chat_response['summary']['median_label'],
        f"P90 {chat_response['summary']['p90_label']}，样本 {chat_response['summary']['latency_sample_size']} 条",
    ])
    if not chat_response['summary']['latency_sample_sufficient']:
        writer.writerow([
            '样本不足', '', '', '',
            f"不足 {chat_response['summary']['min_latency_sample']} 条，时长只作参考",
        ])
    writer.writerow([
        '超期未打开', chat_response['summary']['unopened'], '',
        f"超过 {chat_response['silent_after_days']} 天仍未被打开，视为已经沉没",
    ])
    writer.writerow([
        '旧数据无法计时', chat_response['summary']['opened_untimed'], '',
        '在 read_at 字段上线前就已读的私信，计入打开数但不进入时长分布',
    ])

    writer.writerow([])
    writer.writerow(['一条私信发出后，多久被第一次打开'])
    writer.writerow(['区间', '条数', '占比（%）'])
    for bucket in chat_response['latency_buckets']:
        writer.writerow([bucket['label'], bucket['count'], bucket['share']])

    writer.writerow([])
    writer.writerow(['同一类人，发出去的话有没有人接'])
    writer.writerow(['角色', '发出', '打开', '打开率', '中位时长', '样本'])
    for row in chat_response['role_rows']:
        writer.writerow([
            row['label'], row['sent'], row['opened'], row['open_rate'],
            row['median_label'],
            row['sample_size'] if row['has_sample'] else '样本不足',
        ])

    writer.writerow([])
    writer.writerow(['按商品分类看私信响应'])
    writer.writerow([
        '分类', '私信数', '已打开', '打开率（%）', '中位打开', 'P90',
        '带时间戳样本', '样本',
    ])
    for row in chat_response['category_rows']:
        writer.writerow([
            row['label'], row['sent'], row['opened'], row['open_rate'],
            row['median_label'], row['p90_label'], row['sample_size'],
            row['sample_size'] if row['has_sample'] else '样本不足',
        ])
    writer.writerow([
        '未关联商品的私信数', chat_response['unlinked_messages'],
        '最小样本数', chat_response['min_context_sample'],
    ])

    writer.writerow([])
    writer.writerow(['按交易方式看私信响应'])
    writer.writerow([
        '交易方式', '私信数', '已打开', '打开率（%）', '中位打开', 'P90',
        '带时间戳样本', '样本',
    ])
    for row in chat_response['trade_mode_rows']:
        writer.writerow([
            row['label'], row['sent'], row['opened'], row['open_rate'],
            row['median_label'], row['p90_label'], row['sample_size'],
            row['sample_size'] if row['has_sample'] else '样本不足',
        ])
    writer.writerow([
        '口径说明', chat_response['context_note'],
        '分类与交易方式读取商品当前值，不伪装成私信发送时的快照',
    ])
    writer.writerow([
        '隐私边界',
        'CSV 不输出用户名、商品标题或私信内容，只保留聚合后的场景与数字',
    ])

    writer.writerow([])
    writer.writerow(['会话是聊起来了，还是只说了一句'])
    writer.writerow(['形状', '会话数', '占比（%）', '说明'])
    for row in chat_response['conversation_health']['rows']:
        writer.writerow([row['label'], row['count'], row['share'], row['note']])
    writer.writerow([
        '合计', chat_response['conversation_health']['total'], '', '',
    ])

    writer.writerow([])
    writer.writerow(['哪个钟点发出去的话没人接'])
    writer.writerow(['钟点', '发出', '已被打开', '说明'])
    for point in chat_response['hour_profile']:
        writer.writerow([
            point['label'], point['sent'], point['opened'],
            f"占该钟点发出量的 {round(point['opened'] / point['sent'] * 100, 1)}%"
            if point['sent'] else '本周期这个钟点没有私信',
        ])
    trend = dashboard['chat_response_trend']
    writer.writerow([])
    writer.writerow(['私信响应时效跨周期对比'])
    writer.writerow(['口径与门槛'])
    writer.writerow([
        '回看天数', trend['period_days'],
        '静默期（天）', trend['silent_after_days'],
    ])
    writer.writerow([
        '时长阈值', f"较上期 {trend['latency_drift_factor']} 倍",
        '比例阈值', f"{trend['trend_delta_points']} 个百分点",
    ])
    writer.writerow([
        '中位时长样本门槛', trend['min_latency_sample'],
        '比例分母门槛', trend['min_sent_sample'],
    ])
    writer.writerow([])
    writer.writerow(['整体指标的跨周期对比'])
    writer.writerow(['指标', '本周期', '上一周期', '变化', '方向', '说明'])
    for row in trend['metric_rows']:
        writer.writerow([
            row['label'], row['current_label'], row['previous_label'],
            row['delta_display'],
            row['direction_label'] if row['has_direction'] else '只报数不判方向',
            '时长按倍数判，比例按百分点判；任一侧样本不足就只报数字，不猜方向'
            if row['has_direction'] else
            'P90 与双向会话占比只报数：一封信躺半个月就能把 P90 抬起来，'
            '而会话变得双向不等于更健康',
        ])
    writer.writerow([])
    writer.writerow(['结论'])
    writer.writerow([trend['summary']])
    writer.writerow([
        '两条曲线都按私信发出时刻归期，两侧窗口等长、都从本地午夜切，'
        '与搜索趋势面板同一套口径；未打开按占本周期发出总量的比例判，'
        '不按次数差判。本模块只报告，不改写任何一条私信、不调整静默期阈值、'
        '不给任何人打回复速度分。',
    ])
    correlation = dashboard['chat_trade_correlation']
    writer.writerow([])
    writer.writerow(['私信响应与成交的共同变化'])
    writer.writerow(['口径与门槛'])
    writer.writerow([
        '回看天数', correlation['period_days'],
        '观察窗口（天）', correlation['outcome_window_days'],
    ])
    writer.writerow([
        '分析单位', '商品 × 买家 × 卖家',
        '每档样本门槛', correlation['min_bucket_sample'],
    ])
    writer.writerow([
        '响应计时', '每个会话先取自己带 read_at 买家询问的中位打开时长，再落入固定五档',
        '成交事件', 'completed 与 returned 都算完整闭环',
    ])
    writer.writerow([
        '只读边界', '不读私信内容，不输出用户名、商品标题、订单号或链接；只报告，不写回',
        '解释边界', '只描述共同变化，不把相关读成因果',
    ])
    writer.writerow([])
    writer.writerow(['响应档与会话'])
    writer.writerow([
        '响应档', '会话', '带时戳询问', '中位打开', '有订单', '7天内成交',
        '成交率（%）', '样本',
    ])
    for row in correlation['rows']:
        writer.writerow([
            row['label'], row['conversations'], row['timed_inquiries'],
            row['median_label'], row['ordered'], row['completed'],
            row['completion_rate'] if row['completion_rate'] is not None else '—',
            row['conversations'] if row['has_sample'] else '样本不足',
        ])
    writer.writerow(['会话状态'])
    writer.writerow(['状态', '会话数', '说明'])
    writer.writerow([
        '统计周期内触碰', correlation['summary']['observed_conversations'],
        '只用于发现候选会话',
    ])
    writer.writerow([
        '第一条询问在周期外', correlation['summary']['started_before_period'],
        '不算新会话，查询时看完整历史',
    ])
    writer.writerow([
        '未满观察窗口', correlation['summary']['immature_conversations'],
        '单独报告，不进入成交率分母',
    ])
    writer.writerow([
        '无法计时', correlation['summary']['untimed_conversations'],
        '已满窗口但窗口内没有带 read_at 的买家询问',
    ])
    writer.writerow([
        '成熟且可计时', correlation['summary']['eligible_conversations'],
        f"窗口内有订单 {correlation['summary']['ordered_conversations']} 个，"
        f"7 天内成交 {correlation['summary']['completed_conversations']} 个",
    ])
    writer.writerow([
        '整体成交率（%）',
        correlation['summary']['completion_rate']
        if correlation['summary']['completion_rate'] is not None else '—',
        '7 天内完整闭环会话数 ÷ 成熟且可计时会话数',
    ])
    writer.writerow(['说明'])
    writer.writerow([correlation['note']])
    writer.writerow([
        '订单创建于本次第一条询问之前的不算结果；订单或成交事件落在第一条询问后 7 天之外，'
        '也不计入本表。每个会话只计一次，避免同一会话多条询问重复进入分母，也避免商品级'
        '聚合把一个买家的订单摊到另一个买家头上。',
    ])
    quality = dashboard['recommendation_quality']
    writer.writerow([])
    writer.writerow(['首页推荐卡挑得准不准'])
    writer.writerow(['口径与门槛'])
    writer.writerow([
        '回看天数', quality['period_days'],
        '理由样本门槛', quality['metrics']['min_reason_feedback'],
        '噪声阈值（百分点）', quality['metrics']['reason_noise_points'],
    ])
    writer.writerow([
        '反馈后动作窗口（天）', quality['metrics']['followup_window_days'],
    ])
    writer.writerow([
        '曝光样本门槛（次被看见）', quality['metrics']['min_recommendation_impressions'],
    ])
    writer.writerow(['整体'])    # 表格标题行
    writer.writerow(['指标', '数值', '说明'])
    loop = quality['feedback_loop']
    writer.writerow([
        '反馈总数', loop['feedback_total'],
        '分母是有过反馈的推荐，不含展示数',
    ])
    writer.writerow([
        '正反馈比例', f"{loop['positive_rate']}%" if loop['positive_rate'] is not None else '本周期没有反馈',
        '「想看看」除以全部反馈；没有反馈时这个数不存在，不是 0%',
    ])
    writer.writerow([
        '改口数', loop['switched_total'],
        '先「想看」后「不感兴趣」的行数，一次改口算一条',
    ])
    writer.writerow([
        '被看见的卡', loop['viewed_recommendations'],
        '反馈率的分母：窗口内第一次被看见的（用户, 商品）对，来自 RecommendationImpression，不和反馈取交集',
    ])
    writer.writerow([
        '反馈率', f"{loop['feedback_rate']}%" if loop['feedback_rate'] is not None else '本周期没有被看见过的卡',
        '分母自本轮起真正落库（首页每次渲染写一行 RecommendationImpression），此前只能拿 BrowsingHistory '
        '反推、报的是下界；现在仍是下界、只低不高，偏低来源见「按推荐当时的理由」一节的说明',
    ])
    writer.writerow([])
    writer.writerow(['按理由拆'])    # 表格标题行
    writer.writerow(['理由', '反馈数', '想看看', '不感兴趣', '正反馈比例', '改口数', '样本是否够'])
    for row in quality['reason_rows']:
        if not row['feedback_count']:
            continue
        writer.writerow([
            row['label'], row['feedback_count'], row['interested_count'],
            row['dismissed_count'],
            f"{row['positive_rate']}%" if row['positive_rate'] is not None else '没有反馈',
            row['switched_count'],
            '够' if not row['is_small_sample'] else f"不足 {quality['metrics']['min_reason_feedback']} 条",
        ])
    writer.writerow([])
    writer.writerow(['按推荐当时的理由'])
    writer.writerow([
        '这一层读的是推荐那一刻挂在卡上的理由（渲染时随卡一起落库），问「当时挂这句话的卡，'
        '用户点了吗」；上面「按理由拆」读的是反馈时刻重算的理由，问「这句理由今天还成立吗」。'
        '两问不能混：用户点完按钮又去收藏、又去浏览，会让后一句滑走，前一句不会。',
    ])
    writer.writerow(['当时的理由', '被看见', '被点开', '反馈率', '样本是否够'])
    for row in quality['reason_impression_rows']:
        if not row['shown_count']:
            continue
        writer.writerow([
            row['label'], row['shown_count'], row['feedback_count'],
            f"{row['feedback_rate']}%" if row['feedback_rate'] is not None else '没有看见',
            '够' if not row['is_small_sample']
            else f"不足 {quality['metrics']['min_recommendation_impressions']} 次",
        ])
    writer.writerow([])
    writer.writerow(['按推荐当时的分数档'])
    writer.writerow([
        '档位边界照三个可解释的加分取（+10 反馈匹配、+8 分类匹配、+5 地点匹配），不按分数等分。'
        '只报规模不报反馈率：分数是五部分相加的结果，两个同分的商品可能一个靠新鲜度一个靠分类。',
    ])
    writer.writerow(['分数档', '被看见'])
    for row in quality['score_bands']:
        if not row['shown_count']:
            continue
        writer.writerow([row['label'], row['shown_count']])
    writer.writerow([])
    writer.writerow(['结论'])
    writer.writerow([quality['summary']])
    for note in quality['recommendations']:
        writer.writerow([note])
    writer.writerow([
        '理由按反馈时刻重算，走 recommendations.py 的 recommendation_reason_key；'
        '复算里的 feedback_match 取「改过口」，因为只表过一次态的行，'
        '点击本身就是入集合的那个动作，「你标记过想看」那一刻还没生效。'
        '复算还原的是点击那一刻的理由，不是推荐当时的理由。'
        '反馈率的分母是窗口内第一次被看见的卡，来自 RecommendationImpression，仍是下界：'
        '反馈的 updated_at 可能在窗口外、按钮的动机可能来自窗口外那次看见、'
        '曝光表自上线起才开始积累。'
        '本模块只报告，不调 +10、不调 0.8、不调 +8、不改任何一句理由文案。',
    ])
    impression_quality = dashboard['recommendation_impression_quality']
    writer.writerow([])
    writer.writerow(['推荐曝光日志质量'])
    writer.writerow(['口径与门槛'])
    writer.writerow([
        '回看天数', impression_quality['period_days'],
        '当前展示位置上限', impression_quality['metrics']['home_position_limit'],
        '理由样本门槛（次被看见）', impression_quality['metrics']['min_reason_impressions'],
    ])
    writer.writerow(['字段与结构'])
    writer.writerow(['检查对象', '行数', '状态', '怎么读'])
    for row in impression_quality['field_rows']:
        writer.writerow([row['label'], row['count'], row['status'], row['note']])
    writer.writerow([])
    writer.writerow(['理由分布'])
    writer.writerow(['理由', '曝光', '占比（%）', '样本'])
    for row in impression_quality['reason_rows']:
        writer.writerow([
            row['label'], row['count'],
            row['share'] if row['share'] is not None else '—',
            '够' if not row['is_small_sample']
            else f"不足 {impression_quality['metrics']['min_reason_impressions']} 次",
        ])
    writer.writerow([])
    writer.writerow(['重复与覆盖'])
    writer.writerow(['指标', '数值', '说明'])
    shape = impression_quality['summary']
    writer.writerow(['周期内落库曝光', shape['total_impressions'], '主统计只读到当前时刻'])
    writer.writerow(['匿名或已注销访客', shape['anonymous_impressions'], 'user 为空是合法形状，不是缺失'])
    writer.writerow(['登录用户曝光', shape['logged_in_impressions'], '匿名行不进入重复分母'])
    writer.writerow(['重复的用户商品对', shape['repeat_pairs'], '同一登录用户看见同一商品不止一次'])
    writer.writerow(['第一次之后多出的重复行', shape['repeat_rows'], '重复不是错误，不折叠分母'])
    writer.writerow([
        '登录用户重复行占比（%）',
        shape['repeat_share'] if shape['repeat_share'] is not None else '—',
        '分母是登录用户曝光',
    ])
    writer.writerow(['覆盖自然日', shape['calendar_days'], '从周期开始到当前时刻'])
    writer.writerow(['有写入自然日', shape['active_days'], '至少一行曝光落库'])
    writer.writerow(['无写入自然日', shape['zero_days'], '低流量或写入失败都会出现，当前模型不可区分'])
    writer.writerow([
        '最长观测间隙（小时）',
        shape['longest_gap_hours'] if shape['longest_gap_hours'] is not None else '—',
        '只描述已落库行的间隔，不判写入失败',
    ])
    writer.writerow(['说明'])
    writer.writerow([impression_quality['note']])
    opportunity_impressions = dashboard['opportunity_impression_quality']
    writer.writerow([])
    writer.writerow(['机会流曝光日志质量'])
    writer.writerow(['口径与门槛'])
    writer.writerow([
        '回看天数', opportunity_impressions['period_days'],
        '未来时间容忍（分钟）', opportunity_impressions['summary_metrics']['future_tolerance_minutes'],
        '逐日表保留天数', opportunity_impressions['summary_metrics']['daily_row_limit'],
    ])
    writer.writerow(['类型分布'])
    writer.writerow(['类型', '展示行', '占比（%）', '状态', '怎么读'])
    for row in opportunity_impressions['kind_rows']:
        writer.writerow([
            row['label'], row['count'],
            row['share'] if row['share'] is not None else '—',
            row['status'], row['note'],
        ])
    writer.writerow([])
    writer.writerow(['目标一致性'])
    writer.writerow(['口径', '展示行', '占比（%）', '状态', '说明'])
    for row in opportunity_impressions['target_rows']:
        writer.writerow([
            row['label'], row['count'],
            row['share'] if row['share'] is not None else '—',
            row['status'], row['note'],
        ])
    writer.writerow([])
    writer.writerow(['重复与时间覆盖'])
    writer.writerow(['指标', '数值', '说明'])
    impression_shape = opportunity_impressions['summary']
    writer.writerow(['周期内展示行', impression_shape['total_impressions'], '读取本地自然日窗口，容忍 1 分钟时钟偏差'])
    writer.writerow(['用户数', impression_shape['user_count'], '按 user 去重，只输出计数'])
    writer.writerow(['目标数', impression_shape['target_count'], '按求购或失物目标去重，不输出清单'])
    writer.writerow(['首次展示', impression_shape['first_seen_at'].isoformat() if impression_shape['first_seen_at'] else '—', '已落库行里的最早 shown_at'])
    writer.writerow(['末次展示', impression_shape['last_seen_at'].isoformat() if impression_shape['last_seen_at'] else '—', '已落库行里的最晚 shown_at'])
    writer.writerow(['窗口内第一次被看见', impression_shape['first_seen_pairs'], '忽略率分母按（用户, 机会）去重'])
    writer.writerow(['重复展示的用户目标对', impression_shape['repeat_pairs'], '同一用户在不同时刻再次看见同一机会'])
    writer.writerow(['第一次之后多出的重复行', impression_shape['repeat_rows'], '重复展示合法，本面板不折叠'])
    writer.writerow(['重复展示行占比（%）', impression_shape['repeat_share'] if impression_shape['repeat_share'] is not None else '—', '分母是周期内全部展示行'])
    writer.writerow(['同刻重复的用户目标时刻组', impression_shape['same_timestamp_pairs'], '只提示核对写入路径'])
    writer.writerow(['同刻重复多出的行', impression_shape['same_timestamp_rows'], '不自动删除，不判定数据库或业务重复'])
    writer.writerow(['未来时间事件', impression_shape['future_count'], '超过当前时间 1 分钟才单列，不进周期主统计'])
    writer.writerow(['覆盖自然日', impression_shape['calendar_days'], '从本地周期开始到容忍边界所在自然日'])
    writer.writerow(['有展示自然日', impression_shape['active_days'], '至少一行曝光落库'])
    writer.writerow(['零展示自然日', impression_shape['zero_days'], '低流量、无请求或写入失败都可能造成，本表不可区分'])
    writer.writerow([
        '最长观测间隙（小时）',
        impression_shape['longest_gap_hours'] if impression_shape['longest_gap_hours'] is not None else '—',
        '只描述已落库行的间隔，不判写入失败',
    ])
    writer.writerow([])
    truncated_note = (
        f"窗口共生成 {opportunity_impressions['daily_row_count'] + opportunity_impressions['daily_rows_truncated']} 天，"
        f"仅保留最近 {opportunity_impressions['daily_row_limit']} 天"
        if opportunity_impressions['daily_rows_truncated']
        else '窗口未超过逐日表保留上限'
    )
    writer.writerow(['逐日展示'])
    writer.writerow(['自然日', '展示行', '用户数', '目标数', '重复行', '状态'])
    for row in opportunity_impressions['daily_rows']:
        writer.writerow([
            row['date'].isoformat(), row['impressions'], row['user_count'],
            row['target_count'], row['repeat_rows'],
            '有展示' if row['has_data'] else '零展示',
        ])
    writer.writerow(['截断说明', truncated_note])
    writer.writerow(['说明'])
    writer.writerow([opportunity_impressions['note']])
    dismissal = dashboard['opportunity_dismissal_quality']
    writer.writerow([])
    writer.writerow(['机会流上被按「不再显示」的机会'])
    writer.writerow(['口径与门槛'])
    writer.writerow([
        '回看天数', dismissal['period_days'],
        '静默期（天）', dismissal['metrics']['silence_days'],
        '分数档样本门槛', dismissal['metrics']['min_band_sample'],
        '理由样本门槛', dismissal['metrics']['min_reason_sample'],
        '噪声阈值（百分点）', dismissal['metrics']['noise_points'],
    ])
    writer.writerow(['整体'])    # 表格标题行
    writer.writerow(['指标', '数值', '说明'])
    writer.writerow([
        '忽略总数', dismissal['metrics']['dismissal_total'],
        '窗口内落库的「暂不展示」，归期按第一次按下的时刻',
    ])
    writer.writerow([
        '今天仍能重算出分数', dismissal['metrics']['scored_total'],
        '用户名下还有在售商品或活跃失物记录能匹配上',
    ])
    writer.writerow([
        '已经匹配不上', dismissal['metrics']['unscored_total'],
        '忽略之后把商品下架或记录过期了，仍计入规模',
    ])
    writer.writerow([
        '重复忽略占比',
        f"{dismissal['metrics']['repeat_share']}%"
        if dismissal['metrics']['repeat_share'] is not None else '本周期没有忽略',
        '同一条机会被同一个人按过不止一次；这是本模块唯一报的比例',
    ])
    writer.writerow([
        '重复忽略行数', dismissal['metrics']['repeated_count'],
        '判据是 expires_at 与 created_at 的距离落在 (30, 35] 天，间接证据',
    ])
    writer.writerow([])
    writer.writerow(['按机会类型拆'])    # 表格标题行
    writer.writerow(['类型', '忽略数', '涉及用户数', '人均忽略', '其中重复忽略'])
    for row in dismissal['kind_rows']:
        writer.writerow([
            row['label'], row['dismissal_count'], row['user_count'],
            row['per_user'] if row['per_user'] is not None else '本周期没有忽略',
            row['repeated_count'],
        ])
    band_floor = dismissal['metrics']['min_band_sample']
    reason_floor = dismissal['metrics']['min_reason_sample']
    writer.writerow([])
    writer.writerow(['按分数档拆'])    # 表格标题行
    writer.writerow([
        '类型', '分数档', '忽略数', '占本类型可打分忽略', '档内平均分', '样本是否够',
    ])
    for row in dismissal['band_rows']:
        writer.writerow([
            row['kind_label'], row['label'], row['dismissal_count'],
            f"{row['share_of_kind']}%" if row['share_of_kind'] is not None else '没有可打分样本',
            row['average_score'] if row['average_score'] is not None else '',
            '够' if not row['is_small_sample'] else f'不足 {band_floor} 条',
        ])
    writer.writerow([])
    writer.writerow(['按理由拆'])    # 表格标题行
    writer.writerow(['类型', '理由', '忽略数', '占本类型可打分忽略', '平均分', '其中重复忽略', '样本是否够'])
    for row in dismissal['reason_rows']:
        if not row['dismissal_count']:
            continue
        writer.writerow([
            row['kind_label'], row['label'], row['dismissal_count'],
            f"{row['share_of_kind']}%",
            row['average_score'],
            row['repeated_count'],
            '够' if not row['is_small_sample'] else f'不足 {reason_floor} 条',
        ])
    writer.writerow([])
    writer.writerow(['重复忽略'])    # 表格标题行
    writer.writerow(['类型', '忽略数', '重复忽略数', '重复忽略占比'])
    for kind, row in dismissal['repeat_stats']['by_kind'].items():
        writer.writerow([
            row['label'], row['dismissal_count'], row['repeated_count'],
            f"{row['repeat_share']}%" if row['repeat_share'] is not None else '本周期没有忽略',
        ])
    writer.writerow([])
    writer.writerow(['忽略率：分母来自 OpportunityImpression'])    # 表格标题行
    writer.writerow(['指标', '数值', '说明'])
    rate = dismissal['dismissal_rate']
    writer.writerow([
        '窗口内第一次被看见的机会', rate['denominator'],
        '按（用户, 机会类型, 目标）去重，只看窗口内最早那次展示',
    ])
    writer.writerow([
        '其中被按了「不再显示」', rate['numerator'],
        '分子要求该机会在窗口内至少被展示过一次',
    ])
    writer.writerow([
        '忽略率（%）',
        f"{rate['rate']}%" if rate['rate'] is not None else '分母为空，报 None 不报 0',
        '下界，只低不高：窗口外看到、窗口内才按按钮的忽略进不了分子',
    ])
    writer.writerow([
        '分母样本是否够',
        '够' if not rate['is_small_sample'] else f"不足 {dismissal['metrics']['min_impressions']} 条",
        '不足时只报数字不给方向',
    ])
    writer.writerow([
        '窗口内展示总行数', dismissal['metrics']['impression_total'],
        '一行一次渲染，不去重：展示不该折叠',
    ])
    writer.writerow([
        '渲染过不止一次的机会',
        sum(
            1 for row in dismissal['rate_rows']
            if not row.get('is_unscored') and row['shown_count'] > 1
        ),
        '与重复忽略是一对正交读数：展示了 5 次只忽略 1 次是在忍',
    ])
    writer.writerow([])
    writer.writerow(['按分数档的忽略率'])    # 表格标题行
    writer.writerow([
        '类型', '分数档', '被看见次数', '其中被忽略', '忽略率（%）', '渲染过不止一次',
    ])
    for row in dismissal['rate_rows']:
        writer.writerow([
            row['kind_label'],
            row['label'],
            row['shown_count'],
            row['dismissal_count'],
            f"{row['dismissal_rate']}%" if row['dismissal_rate'] is not None else '',
            row['repeat_show_count'],
        ])
    writer.writerow([])
    writer.writerow(['结论'])
    writer.writerow([dismissal['summary']])
    for note in dismissal['recommendations']:
        writer.writerow([note])
    for kind in ('demand', 'lost_found'):
        shape = dismissal['score_shape'].get(kind)
        if shape and shape.get('has_comparison'):
            writer.writerow([shape['summary']])
    writer.writerow([
        '忽略率的分母从这一轮起真正落库：OpportunityImpression 在 views.opportunity_feed '
        '每次渲染时写一行，一次 bulk_create。它刻意不设（用户, 目标）唯一约束——忽略该折叠，'
        '展示不该，折叠会让分母变小、忽略率虚高。分母只数窗口内第一次被看见的（用户, 机会），'
        '分子要求该机会在窗口内至少被展示过一次；这个数仍然是下界，只低不高。空分母报 None '
        '不报 0，分母不足 24 条只报数字不给方向。历史数据不回填，曝光表自上线起才开始积累。'
        '分数在忽略时刻重算，走 _match_demand 与 score_lost_found_posts 自己，还原的是'
        '「今天再进池」的分数，不是忽略那一刻的分数。本模块只报告，不改任何一行的 '
        'expires_at、不改 30 天静默期、不动两个匹配器的任何常数、不按用户聚合。',
    ])
    collection_quality = dashboard['favorite_collection_quality']
    writer.writerow([])
    writer.writerow(['用户建的心愿单分组'])
    writer.writerow(['口径与门槛'])
    writer.writerow([
        '回看天数', collection_quality['period_days'],
        '空分组宽限期（天）', collection_quality['metrics']['empty_grace_days'],
        '大小档样本门槛', collection_quality['metrics']['min_band_sample'],
        '名字进榜的最少用户数', collection_quality['metrics']['min_name_sample'],
        '噪声阈值（百分点）', collection_quality['metrics']['noise_points'],
    ])
    writer.writerow(['整体'])    # 表格标题行
    writer.writerow(['指标', '数值', '说明'])
    writer.writerow([
        '窗口内新建分组', collection_quality['metrics']['window_collection_count'],
        '按 FavoriteCollection.created_at 归期，这一列写进去就不再动',
    ])
    writer.writerow([
        '窗口内新建收藏', collection_quality['metrics']['window_favorite_count'],
        '按 Favorite.created_at 归期，同上',
    ])
    writer.writerow([
        '当前分组总数', collection_quality['metrics']['group_total'],
        '快照不是窗口：分组可被删（删完收藏变未分组），问的是现在有多少个',
    ])
    writer.writerow([
        '当前收藏总数', collection_quality['metrics']['favorite_total'],
        '快照不是窗口：收藏可被取消',
    ])
    writer.writerow([
        '空分组数', collection_quality['metrics']['empty_group_total'],
        '一条收藏都没有的分组，含刚建的',
    ])
    writer.writerow([
        '早就空着的分组数', collection_quality['metrics']['stale_empty_total'],
        f'建于 {collection_quality['metrics']['empty_grace_days']} 天之前仍是空分组，'
        '这才是「建了不会用」的真信号',
    ])
    writer.writerow([])
    writer.writerow(['分组大小分布'])    # 表格标题行
    writer.writerow(['大小档', '分组数', '占全部分组'])
    for row in collection_quality['scale_rows']:
        writer.writerow([
            row['label'], row['group_count'],
            f"{row['share_of_groups']}%"
            if row['share_of_groups'] is not None else '没有分组',
        ])
    writer.writerow([])
    writer.writerow(['空分组'])    # 表格标题行
    writer.writerow(['指标', '数值', '说明'])
    empty = collection_quality['empty_rows']
    writer.writerow([
        '空分组占全部分组',
        f"{empty['empty_share']}%" if empty['empty_share'] is not None else '没有分组',
        '分母是全部分组，这是一个真正的比例',
    ])
    writer.writerow([
        '早就空着的占空分组',
        f"{empty['stale_share']}%" if empty['stale_share'] is not None else '没有空分组',
        '分母是空分组自己，报的是空分组自己的形状',
    ])
    writer.writerow([
        '早就空着的涉及用户数', empty['stale_user_count'],
        '绝对量，不是比例',
    ])
    writer.writerow([])
    writer.writerow(['分组渗透'])    # 表格标题行
    writer.writerow(['指标', '数值', '说明'])
    penetration = collection_quality['penetration']
    writer.writerow([
        '已归组收藏占全部收藏',
        f"{penetration['grouped_favorite_share']}%"
        if penetration['grouped_favorite_share'] is not None else '没有收藏',
        '分母是全部收藏，未分组的收藏就是一条普通收藏',
    ])
    writer.writerow([
        '建过分组的人占有收藏用户',
        f"{penetration['collector_share']}%"
        if penetration['collector_share'] is not None else '没有收藏',
        '分母是有收藏的用户，分子是**有收藏且建过分组**的人：建分组不要求有收藏，'
        '拿全部建分组的人当分子会让占比超过 100%',
    ])
    writer.writerow([
        '只收藏、从没建过分组', penetration['favoriter_only_count'],
        '有收藏的用户减去建过分组的人，绝对量',
    ])
    writer.writerow([
        '建了分组、但没收藏', penetration['collector_only_count'],
        'create_favorite_collection 只要求登录不要求有收藏，所以这批人存在；'
        '他们用了分组功能但分组是空的，不该被渗透率吞掉',
    ])
    writer.writerow([])
    writer.writerow(['分组名收敛'])    # 表格标题行
    writer.writerow(['分组名', '使用用户数', '分组数', '收藏数', '平均大小'])
    for row in collection_quality['name_rows']:
        writer.writerow([
            row['label'], row['user_count'], row['group_count'],
            row['favorite_count'],
            row['average_size'] if row['average_size'] is not None else '',
        ])
    writer.writerow([])
    writer.writerow(['结论'])
    writer.writerow([collection_quality['summary']])
    for note in collection_quality['recommendations']:
        writer.writerow([note])
    if collection_quality['size_shape'].get('has_comparison'):
        writer.writerow([collection_quality['size_shape']['summary']])
    writer.writerow([
        '本模块不报「先收藏后归类」：Favorite 没有 updated_at，'
        'views.update_favorite_collection 走 save(update_fields=[collection])，模型里'
        '也没有任何 auto_now 字段，所以归类时刻在库里不存在。窗口内新建的收藏读出来的 '
        'collection 是今天读到的值，不是它创建时的值，报出来会被误读成「收藏时就归组率」。'
        '同理不报分组改名频率（应用内没有改名路由，updated_at 只反映 admin 动过）和删分组'
        '之后收藏去哪了（删分组只走 admin，删完的收藏和没归过类的长得一样）。四类分母各自'
        '独立：当前全部收藏、当前全部分组、当前有收藏的用户、窗口内新建。「当前」是快照'
        '不是窗口，和窗口内的新建规模分开报。本模块只报告，不改任何一条 Favorite.collection、'
        '不删分组、不改分组名、不预设任何分组名。',
    ])
    preparation = dashboard['academic_phase_supply_preparation']
    writer.writerow([])
    writer.writerow(['学期阶段供给预热'])
    writer.writerow(['指标', '数值', '说明'])
    writer.writerow([
        '下一阶段',
        (
            f"{preparation['upcoming_phase_label']} "
            f"{preparation['upcoming_start_date']:%Y-%m-%d}"
            if preparation['has_upcoming_phase'] else '不在预热窗口'
        ),
        (
            f"距开始 {preparation['days_until']} 天"
            if preparation['has_upcoming_phase'] else preparation['empty_reason']
        ),
    ])
    writer.writerow([
        '历史基准阶段',
        (
            f"{preparation['benchmark_term_name']} "
            f"{preparation['benchmark_phase_label']} "
            f"{preparation['benchmark_start_date']:%Y-%m-%d} 至 "
            f"{preparation['benchmark_end_date']:%Y-%m-%d}"
            if preparation['has_benchmark'] else '没有可比阶段'
        ),
        (
            '优先同学期类型；没有可比阶段时不造基线'
            if preparation['has_benchmark'] else preparation['empty_reason']
        ),
    ])
    if preparation['has_benchmark']:
        writer.writerow([
            '当前预热窗口',
            f"{preparation['lead_start_date']:%Y-%m-%d} 至 {preparation['lead_end_date']:%Y-%m-%d}",
            f"固定 {preparation['preparation_days']} 天，不随看板周期参数变化",
        ])
        writer.writerow([
            '提示门槛',
            f"搜索 ≥ {preparation['min_guidance_searches']} 次，"
            f"无结果 ≥ {preparation['min_guidance_zero_results']} 次",
            '一次空结果不足以构成需求形状',
        ])
        writer.writerow([
            '节奏阈值',
            f"当前新增达到 7 天折算基准的 {preparation['pace_threshold_percent']}% 以上为接近基准",
            '折算值是读数比例，不是运营目标',
        ])
        writer.writerow([])
        writer.writerow([
            '分类', '基准搜索', '基准无结果', '无结果占比（%）',
            '基准求购', '基准新增商品', '7 天供给基准',
            '当前求购', '当前新增商品', '当前现货', '提示', '节奏', '节奏百分比',
        ])
        for row in preparation['rows']:
            writer.writerow([
                row['category_name'], row['search_count'], row['zero_result_count'],
                '' if row['zero_result_share'] is None else row['zero_result_share'],
                row['benchmark_demand_count'], row['benchmark_item_count'],
                '' if row['lead_baseline_items'] is None else row['lead_baseline_items'],
                row['lead_demand_count'], row['lead_item_count'],
                row['available_item_count'], row['guidance_label'], row['pace_label'],
                '' if row['pace_percent'] is None else row['pace_percent'],
            ])
        writer.writerow([
            '结论', preparation['summary'],
            '页面保留前 10 行，本 CSV 输出全部分类行；分类行按基准无结果搜索数排序',
        ])
        writer.writerow([
            '隐私与只读边界',
            '不输出搜索词、标题、用户或链接',
            '不自动配置专题、不改推荐、不发提醒、不创建任务',
        ])
    snapshot = dashboard['academic_term_snapshot_quality']
    writer.writerow([])
    writer.writerow(['学期阶段统计快照'])
    writer.writerow(['口径与门槛'])
    writer.writerow([
        '回看天数', snapshot['period_days'],
        '过期门槛（天）', snapshot['metrics']['stale_threshold_days'],
        '刷新中断门槛（天）', snapshot['metrics']['gap_threshold_days'],
        '覆盖率提示线（%）', snapshot['metrics']['coverage_warn_percent'],
    ])
    writer.writerow(['整体'])    # 表格标题行
    writer.writerow(['指标', '数值', '说明'])
    writer.writerow([
        '快照行数', snapshot['metrics']['row_total'],
        '涉及学期数', snapshot['metrics']['term_total'],
        '涉及阶段数', snapshot['metrics']['phase_key_total'],
    ])
    writer.writerow([
        '启用中学期的应有阶段', snapshot['metrics']['expected_phase_total'],
        '其中有对应快照的阶段', snapshot['metrics']['covered_phase_total'],
        '覆盖率（%）',
        '' if snapshot['metrics']['covered_share'] is None else snapshot['metrics']['covered_share'],
    ])
    writer.writerow([
        '匹配键失效的孤儿行', snapshot['metrics']['orphan_total'],
        '已停用学期留下的行', snapshot['metrics']['inactive_term_row_total'],
        '孤儿不算停用残留，两者分开报',
    ])
    writer.writerow([
        '结束过早的阶段（过期）', snapshot['metrics']['stale_total'],
        '占全部快照（%）',
        '' if snapshot['stale_rows']['stale_share'] is None else snapshot['stale_rows']['stale_share'],
    ])
    writer.writerow([
        '周期内命令运行次数', snapshot['metrics']['command_run_total'],
        '其中失败次数', snapshot['metrics']['command_failed_total'],
    ])
    writer.writerow([
        '没被刷到的行', snapshot['metrics']['gap_total'],
        '只在周期内有成功运行时才判，没有基准就没有「落后」',
    ])
    writer.writerow([])
    writer.writerow(['结论'])
    writer.writerow([snapshot['summary']])
    for note in snapshot['recommendations']:
        writer.writerow([note])
    for row in snapshot['orphan_rows']['orphan_rows']:
        writer.writerow([
            f"孤儿行：{row['term_name']} {row['phase_label']}"
            f"（{row['start_date']} 至 {row['end_date']}）",
        ])
    for row in snapshot['stale_rows']['stale_rows']:
        writer.writerow([
            f"过早结束：{row['term_name']} {row['phase_label']}"
            f"（{row['end_date']} 结束，已过 {row['days_since_end']} 天）",
        ])
    writer.writerow([
        '本模块只报「刷新陈旧度」，不报「内容陈旧度」：Django 的 update_or_create 会把 '
        'auto_now 字段显式补进 update_fields（query.py 第 1002-1014 行），所以每次刷新都会推进 '
        'refreshed_at，十个计数一个都没变也照推。拿它当内容变化时刻，会把一条早就不会再变的'
        '阶段一直显示为刚刚更新。因此过期判据看 end_date，不看 refreshed_at。'
        '孤儿行的判据是匹配键失效而不是学期停用：is_active 的 help_text 明写「历史数据保留」，'
        '停用学期留下的行是设计如此，算进孤儿会把一条被写死的行为报成异常。'
        '同理不报快照数与实时数的偏差——那是在给一个没人读的缓存做体检，要报先得给它读路径。'
        '本模块只报告，不刷新任何快照、不删任何孤儿行、不改任何 starts_on、不删任何阶段、'
        '不加迁移。',
    ])
    sla = dashboard['governance_sla']
    writer.writerow([])
    writer.writerow(['治理队列处理时效'])
    writer.writerow([
        '回看天数', sla['days'],
        '周期内事项', sla['total_cases'],
        '周期内已处理', sla['total_closed'],
        '当前待处理', sla['total_open'],
        '已超期', sla['total_overdue'],
        '时限内完成率（%）', '' if sla['within_sla_rate'] is None else sla['within_sla_rate'],
    ])
    writer.writerow([])
    writer.writerow(['队列', '周期内', '已处理', '待处理', '超期', '临近超期',
                     '超期率（%）', '平均耗时', '中位耗时', 'P90 耗时',
                     '最快', '最慢', '处理时限', '时限内完成数',
                     '时限内完成率（%）'])
    for queue in sla['queues']:
        writer.writerow([
            queue['label'],
            queue['total'], queue['closed'], queue['open'],
            queue['overdue'], queue['warning'],
            queue['overdue_rate'],
            queue['avg_label'], queue['median_label'], queue['p90_label'],
            queue['fastest_label'], queue['slowest_label'],
            queue['sla_label'], queue['within_sla_count'],
            '' if queue['within_sla_rate'] is None else queue['within_sla_rate'],
        ])
    writer.writerow([])
    writer.writerow(['队列', '处理时长分布', '条数', '占比（%）'])
    for queue in sla['queues']:
        for bucket in queue['latency_buckets']:
            writer.writerow([
                queue['label'], bucket['label'], bucket['count'], bucket['share'],
            ])
    writer.writerow([])
    writer.writerow(['队列', '事项', '类型', '对象', '被指方', '提起人',
                     '创建时间', '已等待', '处理时限', '剩余', '状态'])
    for queue in sla['queues']:
        for case in queue['open_cases']:
            writer.writerow([
                queue['label'],
                case['case_id'],
                case['reason_label'],
                case['subject'],
                case['counterparty'],
                case['reporter'],
                '' if case['created_at'] is None
                else case['created_at'].strftime('%Y-%m-%d %H:%M'),
                case['age_label'],
                case['sla_label'],
                case['remaining_label'],
                '已超期' if case['is_overdue'] else '时限内',
            ])
    writer.writerow([])
    writer.writerow(['治理建议'])
    for recommendation in sla['recommendations']:
        writer.writerow([recommendation])
    writer.writerow([])
    writer.writerow(['说明'])
    writer.writerow([sla['summary']])
    writer.writerow([
        '响应时长从处理人写进记录的那一刻起算，打开队列页不算响应：'
        '否则"响应快"会退化成"刷新得勤"，这个指标就废了。',
    ])
    writer.writerow([
        '借用催收队列的创建时刻取拖到第三级的那一次催收，不是订单的创建时刻：'
        '在 L1 躺了一周，不该记在治理侧的账上。',
    ])
    writer.writerow([
        '各队列的处理时限本来就不同：预约异常阻塞一次线下交付，按小时计；'
        '举报只是离线复核，可以等一两天。共用一条时限要么让异常队列天天报假警，'
        '要么让举报烂在队列里。',
    ])
    writer.writerow([
        '待处理清单不按统计周期过滤：周期开始前就超期的事项，'
        '恰恰是最不该被藏起来的那一件。',
    ])
    writer.writerow([
        '此处只报告，不自动派单、不改处理时限、不关闭任何事项。',
    ])

    tier = dashboard['contribution_tier_shape']
    writer.writerow([])
    writer.writerow(['积分等级与徽章的门槛形状', '数值', '说明'])
    writer.writerow([
        '口径与门槛', tier['summary'],
        '副标题：三张规则表第一次被数字读到',
        '只读 CommunityContribution 与 contributions.py 里的三张规则表，不加迁移、不改任何'
        '分值、不调任何人的等级、不发任何积分、不补发漏记的分',
        f"按全量算，不按周期切：等级是累计量，按周期切会让三个月前升到第三级的人在这个月消失。"
        f"days 参数只为和看板其余面板的口径字段保持一致而收下",
        f"分母是有过至少一条记录的用户（{tier['user_count']} 人），不是全体用户："
        f"第一级门槛是 {tier['level_zero']}，没记录的用户也落在这一级，但那是默认值",
        f"同用户同类型记录数至少 {tier['min_ranked_users']} 条才给方向，"
        f"类型样本至少 {tier['min_type_sample']} 条才给方向，"
        f"业务域至少 {tier['min_key_prefix']} 条才给方向，"
        f"差值小于 {tier['noise_points']} 分视为噪声",
    ])
    writer.writerow([
        '有记录的用户', tier['user_count'], '',
        '分母只收有过至少一条贡献记录的用户',
    ])
    writer.writerow([
        '贡献记录总数', tier['total_records'], '',
        '不按周期过滤，和上一条同一个分母',
    ])

    writer.writerow([])
    writer.writerow(['五级等级各自有多少人'])
    writer.writerow(['等级', '门槛（分）', '用户数', '占比（%）', '读数', '说明'])
    for row in tier['level_rows']:
        writer.writerow([
            '—', row.label, row.threshold, row.user_count,
            '—' if row.share is None else row.share,
            '最高一级' if row.is_top else '',
            '等级判定直接调产品侧的 _level_for_points，不在本地复制一份',
        ])
    writer.writerow([
        '说明', '', '', '',
        '五级全列，包括一级都没有人的那一级：漏掉空级，读者会以为这一级也是齐的',
        '占比的分母是有过记录的用户数。分子 0、分母存在时报 0.0，空分母才报「—」，'
        '两者不是一回事',
        '等级判定直接调产品侧的 _level_for_points，不在本地复制一份：门槛表将来改一个数，'
        '本地那份不会跟着动',
    ])

    writer.writerow([])
    writer.writerow(['每一级到下一级，这批人还差多少'])
    writer.writerow(['到下一级还差多少', '停在这一级的人数', '中位距离', '最大距离', '说明'])
    for row in tier['gap_rows']:
        writer.writerow([
            '—', row.distance_count, row.mean_distance_label,
            '—' if row.max_distance is None else row.max_distance,
            '距离 = 下一级门槛 − 当前积分，报中位数和最大值、不报平均值',
        ])
    writer.writerow([
        '说明', '', '',
        '距离 = 下一级门槛 − 当前积分。跨度对停在同一级的人都一样，距离才因人而异',
        '四级跨度全列。这一级一个人都没停时报「—」不报 0：0 会被读成「差 0 分就升级」',
        '报中位数和最大值，不报平均值：一个差 200 分的人会把平均值抬起来',
        '已停在最高一级的人没有下一级，不进这一节',
    ])

    writer.writerow([])
    writer.writerow(['七枚徽章，各自有多少人拿到'])
    writer.writerow(['徽章', '看什么', '门槛', '拿到的人数', '占比（%）', '说明'])
    for row in tier['badge_rows']:
        writer.writerow([
            '—', row.title,
            '事件总数' if row.metric == 'events' else '特定类型',
            row.threshold, row.earned_count,
            '—' if row.share is None else row.share,
            '判定直接调产品侧的 build_contribution_badges，不在本地复制一份',
        ])
    writer.writerow([
        '说明', '', '', '',
        '七枚全列，包括一个人都没拿到的那枚',
        '门槛和获取数摆在同一行：分开看读不出「这枚徽章是不是太难过」——一个 5 次门槛拿到'
        '4 个人，和一个 1 次门槛拿到 4 个人，是两件不同的事',
        '判定直接调产品侧的 build_contribution_badges，不在本地复制一份',
    ])

    writer.writerow([])
    writer.writerow(['哪一类行为在发分'])
    writer.writerow([
        '贡献类型', '事件数', '积分', '单次平均分', '积分占比（%）', '样本',
    ])
    for row in tier['type_rows']:
        writer.writerow([
            '—', row.label, row.event_count, row.points,
            row.mean_points_label,
            '—' if row.point_share is None else row.point_share,
            f"不足 {tier['min_type_sample']} 条，只作参考" if row.is_small_sample else '可读数',
        ])
    writer.writerow([
        '说明', '', '', '',
        '六种全列，包括一条记录都没有的类型：漏掉空类型，读者会以为这套体系里只有出现过的'
        '几种行为',
        '积分占比的分母是全部积分，不是该类型自己的事件数',
        '单次平均分报实际值，不报代码里的权重常数：两者在有补记或改过历史分值时可能不同',
    ])

    writer.writerow([])
    writer.writerow(['写入路径与幂等键的间接体检'])
    writer.writerow(['写入路径（幂等键前缀）', '事件数', '用户数', '占比（%）', '说明'])
    for row in tier['key_prefix_rows']:
        writer.writerow([
            '—', row.prefix, row.event_count, row.user_count,
            '—' if row.share is None else row.share,
            '前缀取 source_key 第一个冒号前的部分；用户数按用户去重',
        ])
    writer.writerow([
        '说明', '', '',
        '前缀取 source_key 第一个冒号前的部分。order:12:trade_completed:seller 的前缀是'
        ' order，trade_completed 只是中间一段',
        '这一节和贡献类型问的不是同一个问题：类型是「哪一种行为」，前缀是「哪一条写入路径」。'
        '一条写入路径可以写出多种类型，两者摆在一起，能看出某条路径是不是整条没在跑',
        '用户数按用户去重，不按记录数：按 source_key 分组时每组只有一个用户',
    ])

    writer.writerow([])
    writer.writerow(['同用户同类型的记录数'])
    writer.writerow(['同用户同类型的记录数', '组合数', '占比（%）', '读数', '说明'])
    for row in tier['repeat_rows']:
        writer.writerow([
            '—', row.label, row.pair_count,
            '—' if row.share is None else row.share,
            '正常形态' if row.times == 1 else '这个用户在这条路径上被记了多次',
            'pair_count 数的是「用户 × 类型」的组合数，不是用户数',
        ])
    writer.writerow([
        '说明', '', '',
        'pair_count 数的是「用户 × 类型」的组合数，不是用户数：一个用户可以在六种类型下各'
        '出现一次，那是六个组合、一个用户。占比的分母是有过记录的用户数',
        '不判断「异常多的记录」是重复还是真实高频：一个卖家这个月完成八笔交易，名下就有'
        '八条 order:* 记录，这是正常的。给出一个「疑似重复」的结论比不报更糟',
        '不报升级用了多久：库里只有 occurred_at 和 created_at，没有「何时跨过某一级」，'
        '两个字段的差本身就是补记噪声',
        '不把「停在低级」读成「不活跃」：积分是特定行为的加权和，一个人完全可能交易频繁但'
        '不碰免费赠送和失物招领',
        '不按用户排名、不输出用户名、不读 description（它是商品标题拼出来的自由文本）',
        '不重复 analytics 的周期口径：它报流量，本模块报结构',
    ])


    milestone_coverage = dashboard['contribution_milestone_coverage']
    milestone_audit = milestone_coverage['audit']
    writer.writerow([])
    writer.writerow(['贡献里程碑覆盖审计', '数值', '说明'])
    writer.writerow([
        '稳定重放应有', milestone_audit['expected_count'],
        '按用户、occurred_at、id 稳定重放贡献流水，复用在线写入的计算函数',
    ])
    writer.writerow([
        '台账已有', milestone_audit['actual_count'],
        '唯一键是用户 + 里程碑类型 + 里程碑代码',
    ])
    writer.writerow([
        '已覆盖', milestone_audit['covered_count'],
        f"覆盖率 {milestone_audit['coverage_rate'] if milestone_audit['coverage_rate'] is not None else '—'}%；空分母报 —，不报 0",
    ])
    writer.writerow([
        '回填 / 在线', f"{milestone_audit['backfilled_count']} / {milestone_audit['online_count']}",
        '只区分写入来源，不解读运营效果',
    ])
    writer.writerow([
        '缺失 / 多余', f"{milestone_audit['missing_count']} / {milestone_audit['extra_count']}",
        '只聚合报数量，不输出用户或贡献明细',
    ])

    writer.writerow([])
    writer.writerow(['一、按里程碑类型看覆盖'])
    writer.writerow(['类型', '应有', '已覆盖', '缺失', '多余', '覆盖率（%）', '回填', '在线'])
    for row in milestone_coverage['type_rows']:
        writer.writerow([
            row['type_label'], row['expected'], row['covered'], row['missing'],
            row['extra'],
            '—' if row['coverage_rate'] is None else row['coverage_rate'],
            row['backfilled'], row['online'],
        ])

    writer.writerow([])
    writer.writerow(['二、按里程碑代码看差异'])
    writer.writerow(['类型', '代码', '名称 / 快照', '应有', '已有', '缺失', '多余', '快照差异'])
    for row in milestone_coverage['code_rows']:
        writer.writerow([
            row['type_label'], row['milestone_code'], row['title'],
            row['expected'], row['actual'], row['missing'], row['extra'],
            row['snapshot_diff'],
        ])
    writer.writerow([
        '口径', '', '', '', '', '', '',
        '快照差异表示事件保存的门槛或名称与当前规则不同，可能是规则演进痕迹，不自动改历史事件',
    ])

    writer.writerow([])
    writer.writerow(['三、事实一致性信号'])
    writer.writerow(['信号', '行数', '读法'])
    for label, key, note in (
        ('触发贡献用户不一致', 'owner_mismatch_count', '事件用户与触发贡献用户不是同一人'),
        ('触发贡献指向不一致', 'trigger_mismatch_count', '与稳定重放得到的触发贡献不是同一条记录'),
        ('达成时间不一致', 'time_mismatch_count', '事件 reached_at 与贡献 occurred_at 必须相等'),
        ('跨越前后指标不一致', 'value_mismatch_count', '与稳定重放得到的跨越前后指标对不上'),
        ('跨越不等式失效', 'boundary_mismatch_count', '必须满足 previous < threshold <= reached'),
    ):
        writer.writerow([label, milestone_audit[key], note])
    writer.writerow([
        '事实不一致合计', milestone_audit['fact_mismatch_count'],
        '按事件行去重，同一行多个信号只计一次；本面板只报告，不自动修复',
    ])
    writer.writerow([
        '隐私边界', '',
        '不输出用户、贡献标题、描述或幂等键；缺失与多余只聚合到类型或代码',
    ])

    milestone_rhythm = dashboard['contribution_milestone_phase_rhythm']
    writer.writerow([])
    writer.writerow(['贡献里程碑阶段节奏', '数值', '说明'])
    writer.writerow([
        '固定回看天数', milestone_rhythm['lookback_days'],
        '看板 days 参数仅作注册表兼容，本面板不按周期选择器切片',
    ])
    writer.writerow([
        '覆盖审计门槛', '通过' if milestone_rhythm['coverage_gate']['is_clean'] else '阻塞',
        '缺失、多余或事实不一致时全部方向留空；规则快照差异不阻塞',
    ])
    writer.writerow([
        '最小样本', milestone_rhythm['min_events_per_cell'],
        '对照两侧都达到该事件数才输出方向',
    ])
    writer.writerow([
        '可比 / 薄样本 / 等待', (
            f"{milestone_rhythm['compared_count']} / "
            f"{milestone_rhythm['thin_sample_count']} / "
            f"{milestone_rhythm['waiting_count']}"
        ),
        '薄样本与等待同类上一学期只报数字',
    ])
    writer.writerow([
        '未进入对照的事件', (
            f"无学期 {milestone_rhythm['no_term_event_count']}，"
            f"未划分阶段 {milestone_rhythm['unassigned_phase_event_count']}，"
            f"进行中 {milestone_rhythm['ongoing_phase_event_count']}，"
            f"对照外 {milestone_rhythm['outside_comparison_event_count']}"
        ),
        '单独计数，不摊派进任何可比格子',
    ])
    writer.writerow(['摘要', milestone_rhythm['summary'], '只说明聚合走向，不输出用户明细'])

    writer.writerow([])
    writer.writerow(['一、同类学期同阶段对照'])
    writer.writerow([
        '阶段 / 学期类型', '里程碑', '最新学期', '最新事件', '最新天数',
        '最新每百天', '上一学期', '上一事件', '上一天数', '上一每百天',
        '事件差', '强度差', '方向', '状态',
    ])
    for group in milestone_rhythm['groups']:
        previous_name = (
            group['previous_term']['name'] if group['previous_term'] else '—'
        )
        for row in group['rows']:
            latest = row['latest']
            previous = row['previous']
            writer.writerow([
                f"{group['phase_label']} / {group['kind_label']}",
                f"{latest['type_label']} · {latest['milestone_title']}",
                latest['term_name'], latest['event_count'], latest['day_count'],
                latest['events_per_100_days'],
                previous_name if previous else '—',
                previous['event_count'] if previous else '—',
                previous['day_count'] if previous else '—',
                previous['events_per_100_days'] if previous else '—',
                '—' if row['event_count_delta'] is None else row['event_count_delta'],
                '—' if row['intensity_delta'] is None else row['intensity_delta'],
                row['direction_label'], row['status_label'],
            ])
    writer.writerow([
        '边界', '', '', '', '', '', '', '', '', '', '', '', '',
        '方向只按每百天事件强度判断，不比较裸事件数',
    ])
    writer.writerow([
        '隐私', '', '', '', '', '', '', '', '', '', '', '', '',
        '不输出用户、贡献标题、描述、幂等键或触发贡献明细',
    ])

    location_quality = dashboard['location_registry_quality']
    writer.writerow([])
    writer.writerow(['校园地点档案质量'])
    writer.writerow(['档案形状', '数值', '说明'])
    writer.writerow([
        '全部地点', location_quality['summary']['location_total'],
        '含启用与停用地点的当前快照',
    ])
    writer.writerow([
        '启用地点', location_quality['summary']['active_total'],
        f"其中公共 {location_quality['summary']['active_public_total']} 个、非公共 {location_quality['summary']['active_private_total']} 个",
    ])
    writer.writerow([
        '停用地点', location_quality['summary']['inactive_total'],
        f"其中公共 {location_quality['summary']['inactive_public_total']} 个；停用只表示选择器不可见",
    ])

    writer.writerow([])
    writer.writerow(['一、档案形状与字段覆盖'])
    writer.writerow(['地点字段覆盖', '范围', '已填写', '缺失', '总数', '填写率（%）'])
    for row in location_quality['field_rows']:
        writer.writerow([
            row['label'], row['scope'], row['filled'], row['missing'],
            row['total'], '' if row['share'] is None else row['share'],
        ])

    coverage = location_quality['coverage']
    writer.writerow([])
    writer.writerow(['当前业务覆盖', '数量 / 分类', '说明'])
    writer.writerow([
        '当前在售商品', f"{coverage['item_with_location']} / {coverage['item_total']}",
        f"有地点 {coverage['item_with_location_share'] if coverage['item_with_location_share'] is not None else '—'}%，没地点 {coverage['item_without_location']}",
    ])
    writer.writerow([
        '活跃求购', f"{coverage['demand_with_location']} / {coverage['demand_total']}",
        f"没地点 {coverage['demand_without_location']}；没地点不是数据错误",
    ])
    writer.writerow([
        '启用关注搜索', f"{coverage['saved_search_active_location']} / {coverage['saved_search_total']}",
        f"绑定停用地点 {coverage['saved_search_inactive_location']}，未限定地点 {coverage['saved_search_without_location']}",
    ])

    writer.writerow([])
    writer.writerow(['周期交付地点来源', '订单数', '占比（%）', '说明'])
    source_notes = {
        'order': 'Order.meeting_location 优先',
        'appointment': '订单没有地点时读交付安排地点',
        'item': '订单与安排都没有地点时读商品地点',
        'none': '订单、安排和商品都没有地点',
    }
    for row in location_quality['delivery_rows']:
        writer.writerow([
            row['label'], row['count'],
            '' if row['share'] is None else row['share'],
            source_notes[row['source']],
        ])

    writer.writerow([])
    writer.writerow(['三、启用地点档案清单'])
    writer.writerow(['启用地点档案', '安全语义', '楼栋 / 校区', '详细位置', '说明', '安全提示', '在售', '求购', '关注搜索'])
    for row in location_quality['location_rows']:
        writer.writerow([
            row['name'], '公共' if row['is_public'] else '非公共',
            row['building'], row['address'], row['description'], row['safety_note'],
            row['available_item_count'], row['active_demand_count'],
            row['active_saved_search_count'],
        ])
    if location_quality['location_row_hidden_total']:
        writer.writerow([
            '未展示启用地点', '', '', '', '', '',
            location_quality['location_row_hidden_total'], '', '',
        ])

    writer.writerow([])
    writer.writerow(['近似重名组', '地点数', '原文名称', '楼栋 / 详细位置', '当前在售'])
    for group in location_quality['duplicate_name_groups']:
        writer.writerow([
            group['normalized_name'], group['location_count'],
            ' / '.join(row['name'] for row in group['locations']),
            ' / '.join(filter(None, (row['building'] or row['address'] for row in group['locations']))),
            sum(row['available_item_count'] for row in group['locations']),
        ])
    if location_quality['duplicate_group_hidden_total']:
        writer.writerow(['未展示组', location_quality['duplicate_group_hidden_total'], '', '', ''])

    writer.writerow([])
    writer.writerow(['停用地点仍被当前记录引用', '在售', '求购', '关注搜索', '合计'])
    for row in location_quality['stale_reference_rows']:
        writer.writerow([
            row['name'], row['available_item_count'], row['active_demand_count'],
            row['active_saved_search_count'], row['reference_total'],
        ])
    if location_quality['stale_reference_row_hidden_total']:
        writer.writerow(['未展示地点', location_quality['stale_reference_row_hidden_total'], '', '', ''])
    writer.writerow([
        '口径', '', '', '', '',
        '档案与当前引用为快照；交付按订单时间回看；只报告，不自动合并、不改外键、不输出用户或商品信息',
    ])

    location_queue = dashboard['location_maintenance_queue']
    writer.writerow([])
    writer.writerow(['校园地点维护队列'])
    writer.writerow(['队列形状', '数值', '说明'])
    writer.writerow([
        '全部地点', location_queue['summary']['location_total'],
        f"启用 {location_queue['summary']['active_total']} 个",
    ])
    writer.writerow([
        '进入队列', location_queue['summary']['queued_total'],
        f"启用且无信号 {location_queue['summary']['active_clean_total']} 个，停用且无当前引用 {location_queue['summary']['inactive_clean_total']} 个",
    ])
    writer.writerow([
        '复核闭环', location_queue['summary']['reviewed_total'],
        f"待核对 {location_queue['summary']['unreviewed_total']} 个；已处理 {location_queue['summary']['outcome_totals']['handled']} 个，维持现状 {location_queue['summary']['outcome_totals']['accepted']} 个，暂缓 {location_queue['summary']['outcome_totals']['deferred']} 个",
    ])

    writer.writerow([])
    writer.writerow(['一、主优先级与命中信号'])
    writer.writerow(['主优先级', '地点数', '读法'])
    for row in location_queue['priority_rows']:
        writer.writerow([row['label'], row['location_total'], row['description']])

    writer.writerow([])
    writer.writerow(['命中信号', '地点数', '读法'])
    signal_notes = {
        'stale_reference': '停用地点仍被当前在售商品、活跃求购或启用关注搜索引用',
        'public_safety_gap': '启用且公共，但安全提示为空；缺提示只提示核对',
        'duplicate_name': 'Unicode 归一、大小写和空白折叠后有两个及以上启用地点同名',
        'profile_gap': '启用地点缺楼栋、详细位置、说明，或非公共地点缺安全提示',
    }
    for row in location_queue['reason_rows']:
        writer.writerow([row['label'], row['location_total'], signal_notes[row['key']]])

    writer.writerow([])
    writer.writerow(['二、维护清单'])
    writer.writerow([
        '维护地点', '主优先级', '主信号', '全部信号', '缺失字段', '状态',
        '当前引用', '复核', '行动提示',
    ])
    for row in location_queue['location_rows']:
        review = row['review_status_label']
        if row['review_outcome_label']:
            review += f" · {row['review_outcome_label']}"
        writer.writerow([
            row['name'], row['priority_label'], row['primary_reason_label'],
            '、'.join(row['reason_labels']),
            '、'.join(field['label'] for field in row['missing_fields']),
            ('启用' if row['is_active'] else '停用') + (' · 公共' if row['is_public'] else ' · 非公共'),
            f"在售 {row['available_item_count']} · 求购 {row['active_demand_count']} · 关注 {row['active_saved_search_count']}",
            review,
            row['action_hint'],
        ])
    if location_queue['location_row_hidden_total']:
        writer.writerow([
            '未展示地点', location_queue['location_row_hidden_total'], '', '', '', '', '', '', '',
        ])
    writer.writerow([
        '口径', '', '', '', '', '', '', '',
        '当前快照；主优先级互斥，信号可重叠；队列只读，决策由后台留痕且不隐藏当前信号',
    ])

    location_review = dashboard['location_maintenance_decision_review']
    writer.writerow([])
    writer.writerow(['校园地点维护决策回看'])
    writer.writerow(['回看形状', '数值', '说明'])
    writer.writerow([
        '当前队列', location_review['summary']['queued_total'],
        f"待核对 {location_review['summary']['unreviewed_total']} 个；已复核 {location_review['summary']['reviewed_total']} 个",
    ])
    writer.writerow([
        '复核比例（%）', location_review['summary']['reviewed_share'],
        '分母是当前队列总数；没有历史队列快照，不把它读成跨周期处理率',
    ])
    writer.writerow([
        '仍暂缓', location_review['summary']['deferred_total'],
        '匹配当前信号且结果为暂缓处理；只观察停留，不自动催办',
    ])

    writer.writerow([])
    writer.writerow(['一、首次决策沉淀'])
    writer.writerow(['首次决策时间', '决策数', '当前队列占比（%）', '已处理', '维持现状', '暂缓处理', '读法'])
    for row in location_review['first_decision_rows']:
        writer.writerow([
            row['label'], row['decision_total'],
            '' if row['queue_share'] is None else row['queue_share'],
            row['outcome_totals']['handled'], row['outcome_totals']['accepted'],
            row['outcome_totals']['deferred'], row['description'],
        ])

    writer.writerow([])
    writer.writerow(['处理结果', '本周期首次决策', '上一周期首次决策', '更早首次决策', '合计'])
    for row in location_review['outcome_rows']:
        writer.writerow([
            row['outcome_label'], row['cohort_totals']['current'],
            row['cohort_totals']['previous'], row['cohort_totals']['earlier'],
            row['decision_total'],
        ])

    writer.writerow([])
    writer.writerow(['二、暂缓停留清单'])
    writer.writerow(['暂缓地点', '主优先级', '全部信号', '首次决策', '最后更新', '最后更新间隔', '读法'])
    for row in location_review['deferred_rows']:
        writer.writerow([
            row['name'], row['priority_label'], '、'.join(row['reason_labels']),
            timezone.localtime(row['decision_created_at']).strftime('%Y-%m-%d'),
            timezone.localtime(row['decision_updated_at']).strftime('%Y-%m-%d'),
            f"{row['last_update_age_days']} 天",
            '只报告停留，不输出复核人与备注，也不自动催办',
        ])
    if location_review['deferred_row_hidden_total']:
        writer.writerow([
            '未展示暂缓地点', location_review['deferred_row_hidden_total'], '', '', '', '', '',
        ])
    writer.writerow([
        '口径', '', '', '', '', '',
        f"本周期从 {timezone.localtime(location_review['current_period_start']):%Y-%m-%d} 起，可能包含未结束的当天；首次决策按 created_at 归期，暂缓停留按 updated_at 到当前时刻的完整本地日期数计算",
    ])
    writer.writerow([
        '边界', '', '', '', '', '',
        '没有历史队列快照和决策编辑审计表，不计算处理率、不给升降方向；输出不包含复核人和自由备注',
    ])
    return response

def search_items(request):
    query = request.GET.get('q', '').strip()
    return redirect(f'/listings/?{urlencode({"q": query})}') if query else redirect('item_list')


def _ignores_query(builder):
    """把一个只认 ``days`` 的面板构建器包装成 ``(days, query)`` 签名。

    包装而不是去改签名：这八个构建器各自还有别的调用方（定时任务、管理页），
    为了让注册表整齐一个个改公开签名，会把同一次改动散到八个文件里，而且每个
    调用点都要重新读一遍。包装留在这一处，签名就这一处。
    """
    def build(days, query):
        return builder(days=days)
    return build


def _takes_query(builder):
    """把认 ``query`` 的面板构建器包装成 ``(days, query)`` 签名。"""
    def build(days, query):
        return builder(days=days, query=query)
    return build


# 搜索洞察的页面、分区取回与 CSV 共用的面板注册表。八个构建器里只有趋势和偏好
# 迁移吃 ``query``，上面两个小包装把剩下的统一过来，于是这张表和运营看板那张
# 一样是同构的三元组，调用点不必各自判断该传几个参数。
SEARCH_INSIGHTS_PANEL_BUILDERS = (
    ('rewrite_candidates', '搜索同义词候选', _ignores_query(build_search_rewrite_candidates)),
    ('synonym_effects', '同义词效果回流', _ignores_query(build_search_synonym_effects)),
    ('search_trend', '搜索趋势与筛选偏好', _takes_query(build_search_trend)),
    ('combo_shift', '搜索组合偏好迁移', _takes_query(build_search_combo_shift)),
    ('threshold_feedback', '候选门槛建议', _ignores_query(build_search_threshold_feedback)),
    ('rejection_stats', '候选否决分析', _ignores_query(build_search_rejection_stats)),
    ('rejection_recurrence', '否决的回头与被推翻', _ignores_query(build_search_rejection_recurrence)),
    ('position_quality', '搜索结果位置质量', _ignores_query(build_search_position_quality)),
)


def _search_insights_period_days(request):
    try:
        return int(request.GET.get('days', 30))
    except (TypeError, ValueError):
        return 30


# 懒加载开关，语义和 ops_lazy 一模一样：cookie 由 <head> 的内联脚本种下，
# 关掉脚本的浏览器永远种不下它，于是永远拿全量页面。
SEARCH_INSIGHTS_LAZY_COOKIE = 'si_lazy'


def _build_shared_search_insights(*, days, query='', anchors=None):
    """构建页面、分区取回与 CSV 共用的搜索洞察数据。

    ``anchors`` 是懒加载的口子：给了分区列表，就只构建这些分区渲染所需的部分，
    其余面板和统计块一个都不跑。``None``（默认）表示整份——CSV 导出和不带
    ``si_lazy`` cookie 的页面走这条，行为和加这个参数之前逐字节一致。分区与
    上下文的对应关系在 ``search_insights_fragments.py``，那份表由
    ``test_search_insights_fragments`` 反查盯着，不会悄悄漏键。

    和运营看板的区别在这页的核心统计没法整块裁掉：九个构建器里有八个各喂一个
    分区，剩下全挤在 ``build_search_insights`` 里，所以那边筛面板、这边还要再
    按统计块裁一次（``parts``）。
    """
    dashboard = build_search_insights(days, query, parts=section_core_parts(anchors))
    builders = SEARCH_INSIGHTS_PANEL_BUILDERS
    if anchors is not None:
        wanted = search_insights_panel_keys(anchors)
        if wanted is not None:
            builders = [row for row in builders if row[0] in wanted]
    for key, _label, builder in builders:
        dashboard[key] = builder(days, query)
    # 否决原因的选项表不查库，永远给全：整页和分区取回必须看到同一份理由列表，
    # 否则同一个下拉框在两种入口下选项会不一样，而那种不一致在页面上看不出来。
    dashboard['rejection_reasons'] = list(SearchSynonymRejection.REASON_CHOICES)
    return dashboard


def search_insights(request):
    if not request.user.is_staff:
        raise PermissionDenied
    period_days = _search_insights_period_days(request)
    query = request.GET.get('q', '')
    lazy = request.COOKIES.get(SEARCH_INSIGHTS_LAZY_COOKIE) == '1'
    # 懒加载时只构建首屏两个分区用得到的部分；其余十三个分区等前端来取。
    dashboard = _build_shared_search_insights(
        days=period_days,
        query=query,
        anchors=SEARCH_INSIGHTS_FIRST_SCREEN if lazy else None,
    )
    dashboard['si_lazy'] = lazy
    return render(request, 'listings/search_insights.html', dashboard)


def search_insights_fragment(request, anchor):
    """按需取回一个搜索洞察分区，返回该分区的 HTML 片段。

    只渲染 ``anchor`` 那一个分区，上下文按 ``search_insights_fragments`` 的表
    裁剪，所以取回「搜索结果位置质量」不会顺带把趋势、偏好迁移和词表聚合全跑
    一遍——那几个正是这页最贵的查询。

    权限和整页一致：staff。这里没有 ``@login_required``，和 ``search_insights``
    本身保持同一套口径（匿名访问 403 而不是 302 到登录页），因为调用方是
    fetch 而不是用户点击，跳登录页对脚本没有意义。
    """
    if not request.user.is_staff:
        raise PermissionDenied
    partial = search_insights_partial(anchor)
    if partial is None:
        raise Http404('搜索洞察没有这个分区：%s' % anchor)
    days = _search_insights_period_days(request)
    dashboard = _build_shared_search_insights(
        days=days, query=request.GET.get('q', ''), anchors=(anchor,),
    )
    return render(request, 'listings/search_insights/' + partial, dashboard)



def _insights_rejection_pair(request):
    """Read a normalized rewrite pair out of a POST body.

    The pair is normalized with the miner's own helper rather than the caller's
    raw strings, so a rejection written from the insights page and one written
    from the admin land on the same row instead of creating a duplicate that
    hides nothing. An unparseable pair is reported as None rather than guessed
    at: a half-normalized pair would block the wrong candidate.
    """
    from .search_rewrites import _normalize

    source = _normalize(request.POST.get('source', ''))
    target = _normalize(request.POST.get('target', ''))
    if not source or not target or source == target:
        return None
    return source, target


def reject_rewrite_candidate(request):
    """Record that a mined candidate is not an equivalence worth confirming.

    A candidate list that can only be acted on by confirming is a list with no
    memory. Without this endpoint an operator's only options are to confirm the
    pair or to leave it, and leaving it means the same rewrite surfaces again
    next week for somebody else to read. The row is deliberately small: a pair,
    a reason, and an optional note. It never expands a search and never touches
    the mining constant, because the whole point is to record a judgement
    without acting on it.
    """
    if not request.user.is_staff:
        raise PermissionDenied
    if request.method == 'POST':
        pair = _insights_rejection_pair(request)
        reason = request.POST.get('reason', '').strip()
        valid_reasons = {code for code, _ in SearchSynonymRejection.REASON_CHOICES}
        if pair and reason in valid_reasons:
            source, target = pair
            SearchSynonymRejection.objects.update_or_create(
                source=source,
                target=target,
                defaults={
                    'reason': reason,
                    'note': request.POST.get('note', '').strip()[:200],
                    'rejected_by': request.user,
                    'superseded_at': None,
                },
            )
            messages.success(request, f'已记录否决：{source} → {target}。这条候选不会再出现在列表里。')
        else:
            messages.error(request, '没能记录这条否决：词对或原因不完整。')
    return redirect('search_insights')


def restore_rewrite_candidate(request):
    """Take back a rejection so the pair can be reviewed again.

    Undoing a rejection is a normal outcome, not an error path: an operator who
    dismissed a pair in March may well reconsider it in October when the
    vocabulary has changed. The row is deleted rather than deactivated, because
    a rejected pair that is neither active nor visible would be a record nobody
    can interpret, and the reason it was dismissed is still in the audit trail
    if it is ever confirmed instead.
    """
    if not request.user.is_staff:
        raise PermissionDenied
    if request.method == 'POST':
        pair = _insights_rejection_pair(request)
        if pair:
            source, target = pair
            deleted, _ = SearchSynonymRejection.objects.filter(
                source=source, target=target,
            ).delete()
            if deleted:
                messages.success(request, f'已撤销否决：{source} → {target}。这条候选会重新参与挖掘。')
            else:
                messages.error(request, '没有找到对应的否决记录。')
        else:
            messages.error(request, '没能撤销这条否决：词对不完整。')
    return redirect('search_insights')


@login_required
def search_insights_export(request):
    if not request.user.is_staff:
        raise PermissionDenied
    try:
        period_days = int(request.GET.get('days', 30))
    except (TypeError, ValueError):
        period_days = 30
    # 导出永远走整份：CSV 是一次性、离线的，没有人「滚到这里再导」。
    dashboard = _build_shared_search_insights(
        days=period_days, query=request.GET.get('q', ''),
    )

    response = HttpResponse(content_type='text/csv; charset=utf-8')
    response['Content-Disposition'] = (
        f"attachment; filename=campus-glimmer-search-insights-{dashboard['period_days']}d.csv"
    )
    response.write('\ufeff')
    writer = csv.writer(response)
    writer.writerow(['拾光校园搜索需求洞察导出'])
    writer.writerow(['统计周期', f"最近 {dashboard['period_days']} 天"])
    writer.writerow(['数据范围', f"{dashboard['period_start']:%Y-%m-%d} 至 {dashboard['period_end']:%Y-%m-%d}"])
    writer.writerow(['搜索词筛选', dashboard['query_filter'] or '全部搜索词'])

    writer.writerow([])
    writer.writerow(['核心指标', '数值'])
    writer.writerow(['搜索次数', dashboard['metrics']['searches']])
    writer.writerow(['搜索词数量', dashboard['metrics']['unique_terms']])
    writer.writerow(['无结果搜索', dashboard['metrics']['zero_result_searches']])
    writer.writerow(['无结果占比（%）', dashboard['metrics']['zero_result_rate']])
    writer.writerow(['结果曝光', dashboard['metrics']['impressions']])
    writer.writerow(['产生曝光的搜索次数', dashboard['metrics']['exposed_searches']])
    writer.writerow(['结果点击', dashboard['metrics']['clicks']])
    writer.writerow(['搜索点击率（%）', dashboard['metrics']['click_through_rate']])
    writer.writerow(['无点击搜索', dashboard['metrics']['zero_click_searches']])
    writer.writerow(['无点击占比（%）', dashboard['metrics']['zero_click_rate']])

    writer.writerow([])
    writer.writerow(['搜索词表现', '搜索次数', '点击次数', '点击率（%）', '无结果次数', '无结果占比（%）', '平均结果数', '独立用户数', '最近搜索时间'])
    for row in dashboard['term_rows']:
        writer.writerow([
            row['query'], row['search_count'], row['click_count'], row['click_rate'],
            row['zero_result_count'], row['zero_result_rate'], row['average_results'],
            row['unique_users'], row['last_searched'],
        ])

    writer.writerow([])
    writer.writerow(['供给缺口', '搜索词', '搜索次数', '涉及用户数', '无结果次数'])
    for row in dashboard['gap_terms']:
        writer.writerow([
            '供给缺口', row['query'], row['search_count'], row['unique_users'],
            row['zero_result_count'],
        ])

    writer.writerow([])
    writer.writerow(['曝光最多的商品', '商品', '曝光次数', '出现在搜索次数'])
    for row in dashboard['exposed_items']:
        writer.writerow([
            '曝光最多的商品', row['item__title'], row['impression_count'], row['exposed_searches'],
        ])

    writer.writerow([])
    writer.writerow(['被点击最多的商品', '商品', '点击次数', '来自搜索次数'])
    for row in dashboard['clicked_items']:
        writer.writerow([
            '被点击最多的商品', row['item__title'], row['click_count'], row['unique_searches'],
        ])

    writer.writerow([])
    writer.writerow(['有搜索但未点击', '搜索词', '搜索次数', '平均结果数'])
    for row in dashboard['no_click_terms']:
        writer.writerow([
            '有搜索但未点击', row['query'], row['search_count'],
            round(row['average_results'], 1) if row['average_results'] is not None else '',
        ])

    writer.writerow([])
    writer.writerow(['同义词效果回流', '启用', '确认时间', '基线无结果率（%）', '观察无结果率（%）', '变化（百分点）', '判定', '结论'])
    for row in dashboard['synonym_effects']['rows']:
        writer.writerow([
            f"{row['keyword']} → {row['synonym']}",
            '是' if row['is_active'] else '否',
            f"{row['created_at']:%Y-%m-%d}",
            row['baseline_zero_rate'],
            row['observation_zero_rate'],
            row['delta_points'],
            row['outcome_label'],
            row['verdict'],
        ])

    writer.writerow([])
    writer.writerow(['搜索同义词候选', '出现次数', '涉及用户数', '置信提示', '最近出现', '处理方式'])
    for row in dashboard['rewrite_candidates']['rows']:
        writer.writerow([
            f"{row['source']} → {row['target']}", row['occurrences'], row['user_count'],
            row['confidence'], f"{row['last_seen']:%Y-%m-%d %H:%M}", '需人工确认后启用',
        ])

    writer.writerow([])
    writer.writerow(['候选门槛建议', '当前门槛', '建议门槛', '依据强度', '判定', '理由'])
    feedback = dashboard['threshold_feedback']
    writer.writerow([
        feedback['current_threshold'],
        feedback['recommendation']['suggested_threshold'] or '维持不变',
        feedback['confidence_label'],
        feedback['recommendation_label'],
        feedback['recommendation']['reason'],
    ])

    writer.writerow([])
    writer.writerow(['档位候选量', '门槛（次）', '候选总数', '可展示条数', '累计出现次数', '是否为当前档位'])
    for row in feedback['threshold_volumes']:
        writer.writerow([
            '档位候选量', row['threshold'], row['total'], row['visible'],
            row['occurrences'], '是' if row['is_current'] else '否',
        ])

    rejections = dashboard['rejection_stats']
    writer.writerow([])
    writer.writerow(['候选否决分析', '否决总数', '达到当前门槛', '低于门槛', '累计出现次数'])
    writer.writerow([
        '候选否决分析', rejections['total_count'], rejections['passing_count'],
        rejections['below_threshold_count'], rejections['occurrence_total'],
    ])
    writer.writerow([
        '否决分布', '原因', '条数', '占比（%）', '达到门槛条数', '平均出现次数',
    ])
    for row in rejections['reason_rows']:
        writer.writerow([
            '否决分布', row['reason_label'], row['count'], row['share'],
            row['passing_count'], row['average_occurrences'],
        ])
    writer.writerow([
        '否决信号', rejections['signal']['label'],
        rejections['not_equivalent_count'], rejections['too_rare_count'],
        rejections['ambiguous_count'],
    ])

    recurrence = dashboard['rejection_recurrence']
    writer.writerow([])
    writer.writerow([
        '否决的回头与被推翻', '仍然生效', '被确认覆盖', '推翻率（%）',
        '反复出现的词对', '累计回头次数', '口径',
    ])
    writer.writerow([
        '否决的回头与被推翻', recurrence['standing_count'],
        recurrence['superseded_count'],
        recurrence['reversed_share'] if recurrence['reversed_share'] is not None else '样本不足，暂不判断',
        recurrence['recurred_count'], recurrence['recurrence_total'],
        f"回头 {recurrence['min_recurrences']} 次以上算反复；推翻率需 {recurrence['min_judged_rejections']} 条以上样本；"
        f"超过 {recurrence['reversed_share_limit']}% 说明判据该回头看一眼；已撤销删除读不到，不编数",
    ])
    writer.writerow([])
    writer.writerow([
        '每一条否决的后来', '用户先搜', '随后改搜', '否决原因', '现在的状态',
        '否决后回头次数', '最近回头',
    ])
    for row in recurrence['rows']:
        writer.writerow([
            '每一条否决的后来', row['source'], row['target'], row['reason_label'],
            row['kind_label'], row['recurrences'],
            f"{row['last_seen']:%Y-%m-%d %H:%M}" if row['last_seen'] else '生效期间未再出现',
        ])
    writer.writerow([
        '说明', '此处只报告：不改任何否决记录、不撤销任何否决、不恢复任何候选，'
        '也不修改候选次数门槛。回头次数不进入门槛判断，撤销删除不落表、因此不报数。',
    ])
    writer.writerow([])
    writer.writerow(['被否决词对', '用户先搜', '随后改搜', '否决原因', '出现次数', '是否达到门槛', '最近出现'])
    for row in rejections['rows']:
        writer.writerow([
            f"{row['source']} → {row['target']}", row['source'], row['target'],
            row['reason_label'], row['occurrences'], row['threshold_display'],
            f"{row['last_seen']:%Y-%m-%d %H:%M}" if row['last_seen'] else '周期内未再出现',
        ])

    writer.writerow([])
    writer.writerow([
        '搜索趋势与筛选偏好',
        '本周期', '上一周期', '变化',
    ])
    writer.writerow([
        '搜索次数',
        dashboard['search_trend']['current_searches'],
        dashboard['search_trend']['previous_searches'],
        dashboard['search_trend']['volume_change']['change_display'],
    ])

    writer.writerow([])
    writer.writerow([
        '搜索词趋势',
        '本周期次数', '上周期次数', '变化',
        '方向', '无结果率变化（百分点）', '点击率变化（百分点）',
    ])
    for row in dashboard['search_trend']['term_trends']:
        writer.writerow([
            row['query'], row['current_searches'], row['previous_searches'], row['delta'],
            row['direction_label'], row['zero_result_delta'], row['click_delta'],
        ])

    writer.writerow([])
    writer.writerow([
        '筛选偏好迁移',
        '取值', '本周期次数', '上周期次数',
        '本周期占比（%）', '上周期占比（%）', '占比变化（百分点）', '方向',
    ])
    for facet in dashboard['search_trend']['facet_shifts']:
        for row in facet['rows']:
            writer.writerow([
                facet['label'], row['label'], row['current_count'], row['previous_count'],
                row['current_share'], row['previous_share'], row['share_delta'], row['direction_label'],
            ])

    writer.writerow([])
    writer.writerow([
        '分类与价格带组合',
        '分类内搜索次数', '上周期分类内搜索次数',
        '价格带', '本周期次数', '上周期次数',
        '分类内占比（%）', '上周期分类内占比（%）', '占比变化（百分点）', '方向',
    ])
    for category in dashboard['combo_shift']['rows']:
        for row in category['rows']:
            writer.writerow([
                category['category_label'], category['current_total'],
                category['previous_total'], row['label'],
                row['current_count'], row['previous_count'],
                row['current_share'], row['previous_share'],
                row['share_delta'], row['direction_label'],
            ])

    writer.writerow([])
    writer.writerow([
        '失败类型对比',
        '搜索次数', '无结果次数', '无结果率（%）',
        '有结果无点击次数', '点击率（%）', '搜索量方向',
    ])
    for row in dashboard['search_trend']['lead_comparison']:
        writer.writerow([
            row['query'], row['search_count'], row['zero_result_count'], row['zero_result_rate'],
            row['no_click_count'], row['click_rate'], row['direction_label'],
        ])

    writer.writerow([])
    writer.writerow(['运营建议', '搜索词', '搜索次数', '无结果次数', '无结果占比（%）', '建议内容'])
    for row in dashboard['insights']:
        writer.writerow([
            '运营建议', row['query'], row['search_count'],
            row['zero_result_count'], row['zero_result_rate'], row['message'],
        ])

    writer.writerow([])
    writer.writerow(['分类与地点缺口'])
    writer.writerow(['分类需求', '搜索次数', '无结果次数', '无结果占比（%）', '当前在售', '缺口优先级'])
    for row in dashboard['facet_supply_gaps']['categories']:
        writer.writerow([
            '分类需求', row['category__name'], row['search_count'], row['zero_result_count'],
            row['zero_result_rate'], row['available_count'], row['priority_score'],
        ])
    writer.writerow([])
    writer.writerow(['地点需求', '搜索次数', '无结果次数', '无结果占比（%）', '当前在售', '缺口优先级'])
    for row in dashboard['facet_supply_gaps']['locations']:
        writer.writerow([
            '地点需求', row['location__name'], row['search_count'], row['zero_result_count'],
            row['zero_result_rate'], row['available_count'], row['priority_score'],
        ])

    writer.writerow([])
    writer.writerow(['搜索时段趋势', '时段', '搜索次数', '无结果次数', '无结果占比（%）'])
    for row in dashboard['search_rhythm']['hourly']:
        writer.writerow([row['label'], row['hour'], row['count'], row['zero_result_count'], row['zero_result_rate']])

    writer.writerow([])
    writer.writerow(['星期分布', '星期', '搜索次数', '无结果次数'])
    for row in dashboard['search_rhythm']['weekdays']:
        writer.writerow([row['label'], row['weekday'], row['count'], row['zero_result_count']])

    comparison = dashboard['period_comparison']
    writer.writerow([])
    writer.writerow(['需求变化', '当前周期', '上一周期', '变化'])
    writer.writerow(['搜索次数', comparison['current_searches'], comparison['previous_searches'], comparison['search_change']['change_display']])
    writer.writerow(['无结果占比（%）', comparison['current_zero_result_rate'], comparison['previous_zero_result_rate'], comparison['zero_result_rate_change_display']])
    writer.writerow([])
    writer.writerow(['上升搜索词', '上一周期', '当前周期', '变化'])
    for row in comparison['rising_terms']:
        writer.writerow([row['query'], row['previous_count'], row['current_count'], f"+{row['delta']}"])
    writer.writerow([])
    writer.writerow(['下降搜索词', '上一周期', '当前周期', '变化'])
    for row in comparison['falling_terms']:
        writer.writerow([row['query'], row['previous_count'], row['current_count'], row['delta']])

    quality = dashboard['position_quality']
    writer.writerow([])
    writer.writerow(['搜索结果位置质量', '数值', '说明'])
    writer.writerow([
        '口径与门槛', quality['summary'],
        f"曝光 {quality['metrics']['impressions']} 次 · 点击 {quality['metrics']['clicks']} 次 · "
        f"总体点击率 {quality['metrics']['overall_click_through_rate']}%",
    ])
    writer.writerow([
        '达到样本门槛的位置档', quality['metrics']['measured_buckets'],
        f"每档至少 {quality['metrics']['min_bucket_sample']} 次曝光才给结论",
    ])
    writer.writerow([
        '明显衰减的位置档', quality['metrics']['decayed_buckets'],
        f"点击率低于前排 {quality['metrics']['decay_flag_points']} 个百分点以上",
    ])

    writer.writerow([])
    writer.writerow(['位置衰减曲线', '曝光数', '点击数', '点击率（%）', '平均曝光位置', '平均点击位置', '读数'])
    for row in quality['decay_curve']:
        writer.writerow([
            row['label'], row['impression_count'], row['click_count'],
            row['click_through_rate'],
            row['average_impression_position'] if row['average_impression_position'] is not None else '',
            row['average_click_position'] if row['average_click_position'] is not None else '',
            f"样本不足 {quality['metrics']['min_bucket_sample']} 次曝光，只报数字"
            if row['is_small_sample'] else ('点击率相对前排明显衰减' if row['decay_flag'] else '可读数'),
        ])

    effect = quality['ranking_effect']
    writer.writerow([])
    writer.writerow([
        '排序效果', '曝光平均位置', '点击平均位置', '位置差', '判定',
    ])
    writer.writerow([
        '排序效果', effect['exposure_average_position'], effect['click_average_position'],
        effect['lift'], effect['direction_label'],
    ])

    writer.writerow([])
    writer.writerow(['深度诊断', '判定', '结论'])
    writer.writerow([
        '深度诊断', quality['depth_diagnosis']['kind_label'], quality['depth_diagnosis']['summary'],
    ])

    writer.writerow([])
    writer.writerow(['可以做什么', '建议'])
    for index, note in enumerate(quality['recommendations'], start=1):
        writer.writerow([f'建议 {index}', note])

    writer.writerow([])
    writer.writerow([
        '说明', '此处只报告，不改 relevance 权重、不自动调整分页、'
        '也不把统计写回曝光或点击记录。',
    ])
    return response


@login_required
def save_search(request):
    if request.method != 'POST':
        return redirect('item_list')
    form = SavedSearchForm(request.POST)
    if form.is_valid():
        saved_search = form.save(commit=False)
        saved_search.user = request.user
        try:
            saved_search.save()
        except IntegrityError:
            messages.error(request, '你已经有一个同名的关注搜索，请换一个名称。')
        else:
            # Pointing at the management page matters: without it a user who
            # saves a condition has no way back to change its cadence or pause it.
            messages.success(
                request,
                f'已保存“{saved_search.name}”，有新商品匹配时会通知你。'
                f'可以到「关注的搜索」里调整提醒节奏。',
                extra_tags='saved-search-created',
            )
    else:
        messages.error(request, '保存失败：' + '；'.join(error for errors in form.errors.values() for error in errors))

    next_url = request.POST.get('next', '').strip()
    if not next_url or not url_has_allowed_host_and_scheme(
        next_url, allowed_hosts={request.get_host()}, require_https=request.is_secure(),
    ):
        next_url = reverse('item_list')
    return redirect(next_url)


def _attach_saved_search_stats(saved_searches, *, user):
    """Attach buffered and live match counts to each saved search."""
    pending_by_search = dict(
        SavedSearchMatch.objects.filter(
            saved_search__in=saved_searches, notified_at__isnull=True,
        ).values_list('saved_search_id').annotate(
            count=Count('saved_search_id'),
        ).values_list('saved_search_id', 'count')
    )
    available_items = Item.objects.available().select_related('category', 'location')
    if user.is_authenticated:
        available_items = available_items.exclude(seller=user)
    live_by_search = {}
    for saved_search in saved_searches:
        live_by_search[saved_search.pk] = sum(
            1 for item in available_items if matches_saved_search(saved_search, item)
        )
    for saved_search in saved_searches:
        saved_search.pending_match_count = pending_by_search.get(saved_search.pk, 0)
        saved_search.live_match_count = live_by_search.get(saved_search.pk, 0)
        # live_match_count is a live re-evaluation of the current catalogue, so it
        # can be handed back to the list page as a filter. pending_match_count
        # counts buffered rows that may already be sold or expired, so it stays
        # a plain number: linking it would promise items the page cannot show.
        saved_search.live_match_url = _saved_search_filter_url(saved_search)
    return saved_searches


def _saved_search_filter_url(saved_search):
    """Return the item-list URL that re-runs this saved search's criteria."""
    params = {}
    if saved_search.query:
        params['q'] = saved_search.query
    if saved_search.condition:
        params['condition'] = saved_search.condition
    if saved_search.category_id:
        params['category'] = saved_search.category_id
    if saved_search.location_id:
        params['location'] = saved_search.location_id
    if saved_search.min_price is not None:
        params['min_price'] = saved_search.min_price
    if saved_search.max_price is not None:
        params['max_price'] = saved_search.max_price
    if not params:
        return reverse('item_list')
    return f"{reverse('item_list')}?{urlencode(params)}"


def _saved_search_redirect(request):
    """Return to the current page after a saved-search action.

    Only same-host destinations are accepted so the `next` field cannot turn
    these small form actions into an open redirect.
    """
    next_url = (request.POST.get('next') or request.GET.get('next') or '').strip()
    if next_url and url_has_allowed_host_and_scheme(
        next_url,
        allowed_hosts={request.get_host()},
        require_https=request.is_secure(),
    ):
        return redirect(next_url)
    return redirect('saved_search_list')


@login_required
def saved_search_list(request):
    saved_search_queryset = (
        SavedSearch.objects.filter(user=request.user)
        .select_related('category', 'location')
        .order_by('-is_active', '-created_at', '-pk')
    )
    page_obj = Paginator(saved_search_queryset, 8).get_page(request.GET.get('page'))
    saved_searches = list(page_obj.object_list)
    _attach_saved_search_stats(saved_searches, user=request.user)
    return render(request, 'listings/saved_searches.html', {
        'saved_searches': saved_searches,
        'page_obj': page_obj,
        'page_numbers': _elided_page_numbers(page_obj),
        'frequency_choices': SavedSearch.FREQUENCY_CHOICES,
        'title': '关注的搜索',
    })


@login_required
def toggle_saved_search(request, saved_search_id):
    saved_search = get_object_or_404(SavedSearch, id=saved_search_id, user=request.user)
    if request.method == 'POST':
        saved_search.is_active = not saved_search.is_active
        saved_search.save(update_fields=['is_active', 'updated_at'])
        messages.success(request, f'“{saved_search.name}”已{"开启" if saved_search.is_active else "暂停"}提醒。')
    return _saved_search_redirect(request)


@login_required
def update_saved_search_cadence(request, saved_search_id):
    """Change the reminder cadence of a saved search without recreating it."""
    saved_search = get_object_or_404(SavedSearch, id=saved_search_id, user=request.user)
    if request.method != 'POST':
        return _saved_search_redirect(request)

    frequency = request.POST.get('notify_frequency', '')
    valid = dict(SavedSearch.FREQUENCY_CHOICES)
    if frequency not in valid:
        messages.error(request, '提醒频率无效，请重新选择。')
        return _saved_search_redirect(request)

    saved_search.notify_frequency = frequency
    raw_limit = (request.POST.get('max_matches_per_notice') or '').strip()
    if raw_limit:
        try:
            limit = int(raw_limit)
        except ValueError:
            messages.error(request, '单次最多提醒条数需要是 1 到 9 之间的整数。')
            return _saved_search_redirect(request)
        if not 1 <= limit <= 9:
            messages.error(request, '单次最多提醒条数需要是 1 到 9 之间的整数。')
            return _saved_search_redirect(request)
        saved_search.max_matches_per_notice = limit

    raw_quiet = (request.POST.get('quiet_until') or '').strip()
    if raw_quiet:
        parsed = parse_datetime(raw_quiet)
        if parsed is None:
            messages.error(request, '临时静默时间格式不正确。')
            return _saved_search_redirect(request)
        if timezone.is_naive(parsed):
            parsed = timezone.make_aware(parsed, timezone.get_current_timezone())
        saved_search.quiet_until = parsed
    else:
        saved_search.quiet_until = None

    saved_search.save(update_fields=[
        'notify_frequency', 'max_matches_per_notice', 'quiet_until', 'updated_at',
    ])

    if frequency == 'instant':
        delivered = deliver_buffered_saved_search_matches(saved_search=saved_search)
        if delivered:
            messages.success(
                request,
                f'“{saved_search.name}”已切换为即时提醒，补送了 {delivered} 条 待提醒命中。'
            )
            return _saved_search_redirect(request)

    messages.success(request, f'“{saved_search.name}”的提醒节奏已更新为{valid[frequency]}。')
    return _saved_search_redirect(request)


@login_required
def delete_saved_search(request, saved_search_id):
    saved_search = get_object_or_404(SavedSearch, id=saved_search_id, user=request.user)
    if request.method == 'POST':
        name = saved_search.name
        saved_search.delete()
        messages.success(request, f'已删除关注搜索“{name}”。')
    return _saved_search_redirect(request)




def _expire_demand_posts():
    DemandPost.objects.filter(
        status='active', expires_at__isnull=False, expires_at__lte=timezone.now(),
    ).update(status='expired')


def _demand_queryset():
    _expire_demand_posts()
    return DemandPost.objects.filter(status='active').select_related(
        'requester', 'category', 'location',
    )


def _demand_match_items(demand):
    items = Item.objects.available().select_related(
        'category', 'location', 'seller',
    ).prefetch_related('images')
    if demand.category_id:
        items = items.filter(category_id=demand.category_id)
    if demand.location_id:
        items = items.filter(location_id=demand.location_id)
    if demand.min_price is not None:
        items = items.filter(price__gte=demand.min_price)
    if demand.max_price is not None:
        items = items.filter(price__lte=demand.max_price)

    tokens = [token for token in demand.title.split() if len(token) >= 2]
    if tokens:
        keyword_query = Q()
        for token in tokens[:5]:
            keyword_query |= Q(title__icontains=token) | Q(description__icontains=token)
        keyword_matches = items.filter(keyword_query)
        if keyword_matches.exists():
            items = keyword_matches
    return items.annotate(
        demand_match_score=Case(
            When(category_id=demand.category_id, then=Value(3)),
            default=Value(1), output_field=IntegerField(),
        ),
    ).order_by('-demand_match_score', '-created_at')[:6]


def demand_list(request, mine=False):
    mine = mine or request.GET.get('mine') == '1'
    if mine and not request.user.is_authenticated:
        return redirect('login')
    demands = _demand_queryset()
    if mine:
        demands = demands.filter(requester=request.user)

    search_query = request.GET.get('q', '').strip()[:120]
    category_id = request.GET.get('category', '').strip()
    location_id = request.GET.get('location', '').strip()
    sort = request.GET.get('sort', 'latest').strip()
    if search_query:
        demands = demands.filter(Q(title__icontains=search_query) | Q(description__icontains=search_query))
    if category_id.isdigit():
        demands = demands.filter(category_id=category_id)
    else:
        category_id = ''
    if location_id.isdigit():
        demands = demands.filter(location_id=location_id)
    else:
        location_id = ''
    if sort == 'ending':
        demands = demands.order_by('expires_at', '-created_at')
    elif sort == 'popular':
        demands = demands.order_by('-view_count', '-created_at')
    else:
        sort = 'latest'
        demands = demands.order_by('-created_at')

    page = Paginator(demands, 12).get_page(request.GET.get('page'))
    filter_params = request.GET.copy()
    filter_params.pop('page', None)

    categories = list(Category.objects.all())
    locations = list(CampusLocation.objects.filter(is_active=True))
    category_labels = {str(category.id): category.name for category in categories}
    location_labels = {str(location.id): location.name for location in locations}
    active_filters = []
    for parameter, label, value in (
        ('q', '关键词', search_query),
        ('category', '分类', category_labels.get(category_id)),
        ('location', '地点', location_labels.get(location_id)),
    ):
        if not value:
            continue
        remaining_params = filter_params.copy()
        remaining_params.pop(parameter, None)
        active_filters.append({
            'label': label,
            'value': value,
            'remove_query': remaining_params.urlencode(),
        })

    sort_labels = {
        'latest': '最新发布',
        'ending': '即将截止',
        'popular': '浏览最多',
    }
    return render(request, 'listings/demand_list.html', {
        'demands': page,
        'page_obj': page,
        'page_numbers': _elided_page_numbers(page),
        'categories': categories,
        'locations': locations,
        'demand_search_query': search_query,
        'demand_category_filter': category_id,
        'demand_location_filter': location_id,
        'demand_sort': sort,
        'demand_sort_label': sort_labels[sort],
        'demand_active_filters': active_filters,
        'demand_active_filter_count': len(active_filters),
        'demand_filter_query': filter_params.urlencode(),
        'is_mine': mine,
    })


@login_required
def new_demand(request):
    if request.method == 'POST':
        form = DemandPostForm(request.POST)
        if form.is_valid():
            demand = form.save(commit=False)
            demand.requester = request.user
            demand.save()
            messages.success(request, '求购信息已发布，等待合适的同学来响应。')
            return redirect('demand_detail', demand_id=demand.id)
    else:
        form = DemandPostForm()
    return render(request, 'listings/demand_form.html', {
        'form': form,
        'title': '发布求购',
        'submit_label': '发布求购信息',
    })


@login_required
def edit_demand(request, demand_id):
    demand = get_object_or_404(DemandPost, id=demand_id, requester=request.user)
    if demand.status not in {'active', 'expired'}:
        messages.info(request, '只有寻找中的求购信息可以编辑。')
        return redirect('demand_detail', demand_id=demand.id)
    if request.method == 'POST':
        form = DemandPostForm(request.POST, instance=demand)
        if form.is_valid():
            demand = form.save(commit=False)
            demand.status = 'active'
            demand.save()
            messages.success(request, '求购信息已更新，并重新回到公开列表。')
            return redirect('demand_detail', demand_id=demand.id)
    else:
        form = DemandPostForm(instance=demand)
    return render(request, 'listings/demand_form.html', {
        'form': form,
        'demand': demand,
        'title': '编辑求购',
        'submit_label': '保存修改',
    })


@login_required
def close_demand(request, demand_id):
    demand = get_object_or_404(DemandPost, id=demand_id, requester=request.user)
    if request.method == 'POST' and demand.status == 'active':
        status = request.POST.get('status')
        demand.status = status if status in {'fulfilled', 'closed'} else 'closed'
        demand.save(update_fields=['status', 'updated_at'])
        messages.success(request, '求购信息已从公开列表中撤下。')
    return redirect('demand_detail', demand_id=demand.id)



@login_required
def respond_to_demand(request, demand_id, item_id):
    demand = get_object_or_404(
        DemandPost.objects.select_related('requester', 'category', 'location'), id=demand_id,
    )
    item = get_object_or_404(
        Item.objects.select_related('seller', 'category', 'location'), id=item_id,
    )
    if request.method != 'POST':
        return redirect('demand_detail', demand_id=demand.id)
    if demand.requester_id == request.user.id:
        messages.error(request, '不能响应自己的求购信息。')
        return redirect('demand_detail', demand_id=demand.id)
    if item.seller_id != request.user.id:
        messages.error(request, '只有商品发布者可以用该商品响应求购。')
        return redirect('demand_detail', demand_id=demand.id)
    form = DemandResponseForm(request.POST)
    if not form.is_valid():
        messages.error(request, '求购响应内容不符合要求，请检查后重试。')
        return redirect('demand_detail', demand_id=demand.id)

    try:
        with transaction.atomic():
            locked_demand = DemandPost.objects.select_for_update(of=('self',)).select_related(
                'requester', 'category', 'location',
            ).get(pk=demand.id)
            locked_item = Item.objects.select_for_update(of=('self',)).select_related(
                'seller', 'category', 'location',
            ).get(pk=item.id)
            if locked_demand.status != 'active' or (
                locked_demand.expires_at and locked_demand.expires_at <= timezone.now()
            ):
                messages.info(request, '这条求购已经结束，暂时不能继续响应。')
                return redirect('demand_detail', demand_id=locked_demand.id)
            if not locked_item.is_available_now:
                messages.info(request, '这件商品当前不可用，暂时不能响应求购。')
                return redirect('demand_detail', demand_id=locked_demand.id)
            match = _match_demand(locked_demand, locked_item)
            if not match:
                messages.error(request, '这件商品当前不满足求购的分类、地点、预算或关键词条件。')
                return redirect('demand_detail', demand_id=locked_demand.id)

            response = DemandResponse.objects.select_for_update(of=('self',)).filter(
                demand=locked_demand, item=locked_item,
            ).first()
            if response and response.status in {'pending', 'accepted'}:
                messages.info(request, '你已经响应过这条求购信息。')
                return redirect('demand_detail', demand_id=locked_demand.id)
            if response:
                response.responder = request.user
                response.message = form.cleaned_data['message']
                response.match_score, response.match_reason = match
                response.status = 'pending'
                response.save(update_fields=[
                    'responder', 'message', 'match_score', 'match_reason', 'status', 'updated_at',
                ])
            else:
                response = DemandResponse.objects.create(
                    demand=locked_demand,
                    item=locked_item,
                    responder=request.user,
                    message=form.cleaned_data['message'],
                    match_score=match[0],
                    match_reason=match[1],
                )
            create_notification(
                locked_demand.requester,
                actor=request.user,
                kind='demand_response',
                title='有人响应了你的求购',
                message=f'{request.user.username}用“{locked_item.title}”响应了“{locked_demand.title}”（{match[1]}）。',
                item=locked_item,
                demand=locked_demand,
                target_url=reverse('demand_detail', args=[locked_demand.id]),
                dedupe_key=f'demand-response:{locked_demand.id}:{locked_item.id}',
                dedupe_window_seconds=3600,
            )
    except IntegrityError:
        messages.info(request, '这件商品刚刚已经响应过该求购，请刷新后查看。')
        return redirect('demand_detail', demand_id=demand.id)

    messages.success(request, '求购响应已发送，等待发布者确认。')
    return redirect('demand_detail', demand_id=demand.id)


@login_required
def review_demand_response(request, response_id, action):
    response = get_object_or_404(
        DemandResponse.objects.select_related('demand', 'item', 'responder'), id=response_id,
    )
    if response.demand.requester_id != request.user.id:
        messages.error(request, '只有求购发布者可以处理响应。')
        return redirect('demand_detail', demand_id=response.demand_id)
    if request.method != 'POST' or action not in {'accept', 'reject'}:
        return redirect('demand_detail', demand_id=response.demand_id)

    with transaction.atomic():
        locked_response = DemandResponse.objects.select_for_update(of=('self',)).select_related(
            'demand', 'item', 'responder',
        ).get(pk=response.id)
        locked_demand = DemandPost.objects.select_for_update(of=('self',)).get(pk=locked_response.demand_id)
        locked_item = Item.objects.select_for_update(of=('self',)).get(pk=locked_response.item_id)
        if locked_response.status != 'pending':
            messages.info(request, '这条求购响应已经处理过了。')
            return redirect('demand_detail', demand_id=locked_demand.id)
        if action == 'accept':
            if locked_demand.status != 'active' or not locked_item.is_available_now:
                messages.info(request, '求购或商品状态已经发生变化，暂时不能确认匹配。')
                return redirect('demand_detail', demand_id=locked_demand.id)
            match = _match_demand(locked_demand, locked_item)
            if not match:
                messages.info(request, '商品已经不再满足这条求购的条件。')
                return redirect('demand_detail', demand_id=locked_demand.id)
            locked_response.status = 'accepted'
            locked_response.match_score, locked_response.match_reason = match
            locked_response.save(update_fields=['status', 'match_score', 'match_reason', 'updated_at'])
            record_demand_response_contribution(locked_response)
            locked_demand.status = 'fulfilled'
            locked_demand.save(update_fields=['status', 'updated_at'])
            other_responses = DemandResponse.objects.select_for_update(of=('self',)).filter(
                demand=locked_demand, status='pending',
            ).exclude(pk=locked_response.pk).select_related('responder', 'item')
            for other in other_responses:
                other.status = 'rejected'
                other.save(update_fields=['status', 'updated_at'])
                create_notification(
                    other.responder,
                    actor=request.user,
                    kind='demand_response',
                    title='求购响应未被采纳',
                    message=f'求购“{locked_demand.title}”已经确认了其他响应，感谢你的参与。',
                    item=other.item,
                    demand=locked_demand,
                    target_url=reverse('demand_detail', args=[locked_demand.id]),
                )
            create_notification(
                locked_response.responder,
                actor=request.user,
                kind='demand_response',
                title='你的求购响应已被确认',
                message=f'“{locked_demand.title}”已确认使用你的商品“{locked_item.title}”，请继续进入商品详情完成预约或领取申请。',
                item=locked_item,
                demand=locked_demand,
                target_url=reverse('item_detail', args=[locked_item.id]),
            )
            messages.success(request, '已确认这条响应，请继续进入商品详情完成交易。')
        else:
            locked_response.status = 'rejected'
            locked_response.save(update_fields=['status', 'updated_at'])
            create_notification(
                locked_response.responder,
                actor=request.user,
                kind='demand_response',
                title='求购响应未被采纳',
                message=f'你对“{locked_demand.title}”的响应暂未被采纳。',
                item=locked_item,
                demand=locked_demand,
                target_url=reverse('demand_detail', args=[locked_demand.id]),
            )
            messages.success(request, '已将这条响应标记为未采纳。')
    return redirect('demand_detail', demand_id=response.demand_id)

@login_required
def submit_demand_feedback(request, response_id):
    response = get_object_or_404(
        DemandResponse.objects.select_related('demand', 'demand__requester', 'item', 'responder'),
        pk=response_id,
    )
    if response.demand.requester_id != request.user.id:
        raise PermissionDenied
    if request.method != 'POST':
        return redirect('demand_detail', demand_id=response.demand_id)
    if response.status != 'accepted':
        messages.info(request, '只有已确认的求购响应才能提交完成反馈。')
        return redirect('demand_detail', demand_id=response.demand_id)
    if MutualAidFeedback.objects.filter(demand_response=response).exists():
        messages.info(request, '这次互助已经提交过结果反馈。')
        return redirect('demand_detail', demand_id=response.demand_id)

    form = MutualAidFeedbackForm(request.POST)
    if not form.is_valid():
        messages.error(request, '反馈内容不符合要求，请检查后重试。')
        return redirect('demand_detail', demand_id=response.demand_id)

    with transaction.atomic():
        locked_response = DemandResponse.objects.select_for_update(of=('self',)).select_related(
            'demand', 'demand__requester', 'item', 'responder',
        ).get(pk=response.pk)
        if locked_response.status != 'accepted':
            messages.info(request, '这条求购响应状态已经变化，暂时不能提交反馈。')
            return redirect('demand_detail', demand_id=locked_response.demand_id)
        if MutualAidFeedback.objects.filter(demand_response=locked_response).exists():
            messages.info(request, '这次互助已经提交过结果反馈。')
            return redirect('demand_detail', demand_id=locked_response.demand_id)
        feedback = MutualAidFeedback(
            demand_response=locked_response,
            submitted_by=request.user,
            outcome=form.cleaned_data['outcome'],
            tags=form.cleaned_data['tags'],
            note=form.cleaned_data['note'],
        )
        feedback.full_clean()
        feedback.save()
        record_mutual_aid_feedback_contribution(feedback)
        outcome_text = feedback.get_outcome_display()
        create_notification(
            locked_response.responder,
            actor=request.user,
            kind='mutual_aid_feedback',
            title='收到求购互助结果反馈',
            message=f'“{locked_response.demand.title}”已被发布者标记为{outcome_text}。',
            demand=locked_response.demand,
            target_url=reverse('demand_detail', args=[locked_response.demand_id]),
            dedupe_key=f'mutual-aid-feedback-demand-{feedback.id}',
            dedupe_forever=True,
        )
    messages.success(request, '已记录这次求购互助的结果。')
    return redirect('demand_detail', demand_id=response.demand_id)


def demand_detail(request, demand_id):
    demand = get_object_or_404(
        DemandPost.objects.select_related('requester', 'category', 'location'), id=demand_id,
    )
    if demand.status == 'active' and demand.expires_at and demand.expires_at <= timezone.now():
        demand.status = 'expired'
        demand.save(update_fields=['status', 'updated_at'])
    elif demand.status == 'active':
        DemandPost.objects.filter(id=demand.id).update(view_count=F('view_count') + 1)
        demand.view_count += 1
    demand_responses = []
    responded_item_ids = set()
    is_demand_owner = request.user.is_authenticated and demand.requester_id == request.user.id
    accepted_response = None
    my_demand_feedback = None
    if is_demand_owner:
        demand_responses = demand.responses.select_related(
            'item', 'item__category', 'item__location', 'responder', 'feedback',
        ).all()
        accepted_response = next(
            (response for response in demand_responses if response.status == 'accepted'),
            None,
        )
    elif request.user.is_authenticated:
        responded_item_ids = set(demand.responses.filter(
            responder=request.user,
        ).values_list('item_id', flat=True))
        my_response = demand.responses.filter(
            responder=request.user, status='accepted',
        ).select_related('feedback').first()
        if my_response:
            my_demand_feedback = getattr(my_response, 'feedback', None)

    demand_feedback = getattr(accepted_response, 'feedback', None) if accepted_response else None
    return render(request, 'listings/demand_detail.html', {
        'demand': demand,
        'recommended_items': _demand_match_items(demand) if demand.status == 'active' else [],
        'demand_responses': demand_responses,
        'is_demand_owner': is_demand_owner,
        'responded_item_ids': responded_item_ids,
        'demand_response_form': DemandResponseForm(),
        'accepted_demand_response': accepted_response,
        'demand_feedback': demand_feedback or my_demand_feedback,
        'demand_feedback_form': (
            MutualAidFeedbackForm()
            if is_demand_owner and accepted_response and not demand_feedback else None
        ),
    })



def _notify_lost_found_matches(post):
    """Notify owners of high-confidence opposite-type records once per pair."""
    for match in find_lost_found_matches(post, minimum_score=45):
        candidate = match['post']
        create_notification(
            candidate.reporter,
            actor=post.reporter,
            kind='lost_found_match',
            title='发现可能匹配的失物招领记录',
            message=f'“{post.title}”与“{candidate.title}”有 {match["score"]} 分的匹配度，建议核对地点、时间和物品特征。',
            target_url=reverse('lost_found_detail', args=[post.id]),
            dedupe_key=f'lost-found-match-{post.id}-{candidate.id}',
            dedupe_forever=True,
        )


def _lost_found_queryset(*, mine=False):
    expire_lost_found_posts()
    queryset = LostFoundPost.objects.select_related(
        'reporter', 'category', 'location', 'matched_post',
    )
    if not mine:
        queryset = queryset.filter(status='active')
    return queryset


def lost_found_list(request, mine=False):
    is_mine = bool(mine)
    posts = _lost_found_queryset(mine=is_mine)
    if is_mine:
        if not request.user.is_authenticated:
            return redirect('login')
        posts = posts.filter(reporter=request.user)

    query = request.GET.get('q', '').strip()[:120]
    post_type = request.GET.get('type', '').strip()
    category_id = request.GET.get('category', '').strip()
    location_id = request.GET.get('location', '').strip()
    sort = request.GET.get('sort', 'latest').strip()

    categories = list(Category.objects.all())
    locations = list(CampusLocation.objects.filter(is_active=True))
    category_labels = {str(category.id): category.name for category in categories}
    location_labels = {str(location.id): location.name for location in locations}

    if query:
        posts = posts.filter(
            Q(title__icontains=query)
            | Q(description__icontains=query)
            | Q(identifying_features__icontains=query)
            | Q(category__name__icontains=query)
            | Q(location__name__icontains=query)
        )
    if post_type in {'lost', 'found'}:
        posts = posts.filter(post_type=post_type)
    else:
        post_type = ''
    if category_id not in category_labels:
        category_id = ''
    if category_id:
        posts = posts.filter(category_id=category_id)
    if location_id not in location_labels:
        location_id = ''
    if location_id:
        posts = posts.filter(location_id=location_id)

    sort_labels = {
        'latest': '最新发布',
        'ending': '即将截止',
        'popular': '浏览最多',
    }
    if sort not in sort_labels:
        sort = 'latest'
    if sort == 'ending':
        posts = posts.order_by('expires_at', '-created_at')
    elif sort == 'popular':
        posts = posts.order_by('-view_count', '-created_at')
    else:
        posts = posts.order_by('-created_at')

    page_obj = Paginator(posts, 12).get_page(request.GET.get('page'))
    filter_values = {
        'q': query,
        'type': post_type,
        'category': category_id,
        'location': location_id,
        'sort': sort,
    }
    type_labels = {'lost': '我丢失了', 'found': '我捡到了'}
    active_filters = []
    for parameter, label, value in (
        ('q', '关键词', query),
        ('type', '类型', type_labels.get(post_type)),
        ('category', '分类', category_labels.get(category_id)),
        ('location', '地点', location_labels.get(location_id)),
    ):
        if not value:
            continue
        remaining = {key: item for key, item in filter_values.items() if key != parameter and item}
        active_filters.append({
            'label': label,
            'value': value,
            'remove_query': urlencode(remaining),
        })

    return render(request, 'listings/lost_found_list.html', {
        'posts': page_obj.object_list,
        'page_obj': page_obj,
        'page_numbers': _elided_page_numbers(page_obj),
        'categories': categories,
        'locations': locations,
        'is_mine': is_mine,
        'lost_found_query': query,
        'lost_found_type': post_type,
        'lost_found_category': category_id,
        'lost_found_location': location_id,
        'lost_found_sort': sort,
        'lost_found_sort_label': sort_labels[sort],
        'lost_found_active_filters': active_filters,
        'lost_found_active_filter_count': len(active_filters),
        'lost_found_filter_query': urlencode({key: value for key, value in filter_values.items() if value}),
    })


@login_required
def new_lost_found(request):
    if request.method == 'POST':
        form = LostFoundPostForm(request.POST)
        if form.is_valid():
            post = form.save(commit=False)
            post.reporter = request.user
            post.save()
            _notify_lost_found_matches(post)
            messages.success(request, '失物招领记录已发布，系统会根据地点、时间和描述为你寻找可能匹配。')
            return redirect('lost_found_detail', post.id)
    else:
        form = LostFoundPostForm()
    return render(request, 'listings/lost_found_form.html', {
        'form': form, 'title': '发布失物招领', 'submit_label': '发布记录',
    })


@login_required
def edit_lost_found(request, post_id):
    post = get_object_or_404(LostFoundPost, pk=post_id)
    if post.reporter_id != request.user.id:
        raise PermissionDenied
    if request.method == 'POST':
        form = LostFoundPostForm(request.POST, instance=post)
        if form.is_valid():
            post = form.save()
            if post.status == 'expired':
                post.status = 'active'
                post.save(update_fields=['status', 'updated_at'])
            _notify_lost_found_matches(post)
            messages.success(request, '失物招领记录已更新。')
            return redirect('lost_found_detail', post.id)
    else:
        form = LostFoundPostForm(instance=post)
    return render(request, 'listings/lost_found_form.html', {
        'form': form, 'title': '编辑失物招领', 'submit_label': '保存修改', 'post': post,
    })


@login_required
def close_lost_found(request, post_id):
    post = get_object_or_404(LostFoundPost, pk=post_id, reporter=request.user)
    if request.method == 'POST' and post.status == 'active':
        post.status = 'closed'
        post.save(update_fields=['status', 'updated_at'])
        messages.success(request, '这条失物招领记录已关闭。')
    return redirect('lost_found_detail', post.id)


def lost_found_detail(request, post_id):
    post = get_object_or_404(
        LostFoundPost.objects.select_related('reporter', 'category', 'location', 'matched_post'),
        pk=post_id,
    )
    if post.status == 'active' and post.expires_at and post.expires_at <= timezone.now():
        post.status = 'expired'
        post.save(update_fields=['status', 'updated_at'])
    elif post.status == 'active':
        LostFoundPost.objects.filter(pk=post.pk).update(view_count=F('view_count') + 1)
        post.view_count += 1

    is_owner = request.user.is_authenticated and post.reporter_id == request.user.id
    leads = []
    my_lead = None
    accepted_lead = None
    my_lost_found_feedback = None
    if is_owner:
        leads = post.leads.select_related('respondent', 'related_post', 'feedback').all()
        accepted_lead = next((lead for lead in leads if lead.status == 'accepted'), None)
    elif request.user.is_authenticated:
        my_lead = post.leads.filter(respondent=request.user).select_related(
            'related_post', 'feedback',
        ).first()
        if my_lead and my_lead.status == 'accepted':
            my_lost_found_feedback = getattr(my_lead, 'feedback', None)
    lost_found_feedback = getattr(accepted_lead, 'feedback', None) if accepted_lead else None
    lead_form = LostFoundLeadForm(post=post) if request.user.is_authenticated and not is_owner and post.status == 'active' else None
    return render(request, 'listings/lost_found_detail.html', {
        'post': post,
        'is_owner': is_owner,
        'leads': leads,
        'my_lead': my_lead,
        'lead_form': lead_form,
        'accepted_lost_found_lead': accepted_lead,
        'lost_found_feedback': lost_found_feedback or my_lost_found_feedback,
        'lost_found_feedback_form': (
            MutualAidFeedbackForm()
            if is_owner and accepted_lead and not lost_found_feedback else None
        ),
        'matches': find_lost_found_matches(post) if post.status == 'active' else [],
    })


@login_required
def submit_lost_found_lead(request, post_id):
    post = get_object_or_404(LostFoundPost, pk=post_id)
    if post.reporter_id == request.user.id:
        messages.info(request, '不能向自己发布的记录提交线索。')
        return redirect('lost_found_detail', post.id)
    if post.status != 'active':
        messages.info(request, '这条记录目前不再接受新的线索。')
        return redirect('lost_found_detail', post.id)

    existing = LostFoundLead.objects.filter(post=post, respondent=request.user).first()
    if existing and existing.status not in {'rejected', 'withdrawn'}:
        messages.info(request, '你已经提交过这条记录的线索，请等待发布者处理。')
        return redirect('lost_found_detail', post.id)
    form = LostFoundLeadForm(request.POST, instance=existing, post=post)
    form.instance.post = post
    form.instance.respondent = request.user
    if form.is_valid():
        lead = form.save(commit=False)
        lead.post = post
        lead.respondent = request.user
        lead.status = 'pending'
        lead.full_clean()
        lead.save()
        create_notification(
            post.reporter,
            actor=request.user,
            kind='lost_found_lead',
            title='收到新的失物招领线索',
            message=f'{request.user.username} 为“{post.title}”提交了一条待核验线索。',
            target_url=reverse('lost_found_detail', args=[post.id]),
            dedupe_key=f'lost-found-lead-{lead.id}',
            dedupe_forever=True,
        )
        messages.success(request, '线索已提交，只有记录发布者可以看到。')
        return redirect('lost_found_detail', post.id)
    return render(request, 'listings/lost_found_detail.html', {
        'post': post,
        'is_owner': False,
        'leads': [],
        'my_lead': existing,
        'lead_form': form,
        'matches': find_lost_found_matches(post),
    })


@login_required
def review_lost_found_lead(request, lead_id, action):
    lead = get_object_or_404(
        LostFoundLead.objects.select_related('post', 'post__reporter', 'respondent', 'related_post'),
        pk=lead_id,
    )
    if lead.post.reporter_id != request.user.id:
        raise PermissionDenied
    if request.method != 'POST' or lead.status != 'pending':
        return redirect('lost_found_detail', lead.post_id)
    if action not in {'accept', 'reject'}:
        messages.error(request, '无效的线索处理动作。')
        return redirect('lost_found_detail', lead.post_id)

    with transaction.atomic():
        locked_lead = LostFoundLead.objects.select_for_update(of=('self',)).select_related(
            'post', 'respondent', 'related_post',
        ).get(pk=lead.pk)
        locked_post = LostFoundPost.objects.select_for_update(of=('self',)).get(pk=locked_lead.post_id)
        if locked_lead.status != 'pending':
            messages.info(request, '这条线索已经被处理过了。')
            return redirect('lost_found_detail', locked_post.id)
        if action == 'accept':
            if locked_post.status != 'active':
                messages.info(request, '记录状态已经变化，暂时不能确认这条线索。')
                return redirect('lost_found_detail', locked_post.id)
            locked_lead.status = 'accepted'
            locked_lead.save(update_fields=['status', 'updated_at'])
            record_lost_found_lead_contribution(locked_lead)
            locked_post.status = 'matched'
            if locked_lead.related_post_id:
                related = LostFoundPost.objects.select_for_update(of=('self',)).get(pk=locked_lead.related_post_id)
                if related.status == 'active':
                    related.status = 'matched'
                    related.matched_post_id = locked_post.id
                    related.save(update_fields=['status', 'matched_post', 'updated_at'])
                locked_post.matched_post_id = related.id
            locked_post.save(update_fields=['status', 'matched_post', 'updated_at'])
            LostFoundLead.objects.filter(post=locked_post, status='pending').exclude(pk=locked_lead.pk).update(
                status='rejected', updated_at=timezone.now(),
            )
            create_notification(
                locked_lead.respondent,
                actor=request.user,
                kind='lost_found_lead',
                title='失物招领线索已确认',
                message=f'你提交的“{locked_post.title}”线索已被发布者确认，请通过站内消息继续核验。',
                target_url=reverse('lost_found_detail', args=[locked_post.id]),
                dedupe_key=f'lost-found-accepted-{locked_lead.id}',
                dedupe_forever=True,
            )
            messages.success(request, '已确认这条线索，记录已标记为已匹配。')
        else:
            locked_lead.status = 'rejected'
            locked_lead.save(update_fields=['status', 'updated_at'])
            create_notification(
                locked_lead.respondent,
                actor=request.user,
                kind='lost_found_lead',
                title='失物招领线索暂未匹配',
                message=f'你提交的“{locked_post.title}”线索暂未被确认，感谢你的帮助。',
                target_url=reverse('lost_found_detail', args=[locked_post.id]),
                dedupe_key=f'lost-found-rejected-{locked_lead.id}',
                dedupe_forever=True,
            )
            messages.success(request, '已将这条线索标记为暂不匹配。')
    return redirect('lost_found_detail', lead.post_id)


@login_required
def submit_lost_found_feedback(request, lead_id):
    lead = get_object_or_404(
        LostFoundLead.objects.select_related('post', 'post__reporter', 'respondent'),
        pk=lead_id,
    )
    if lead.post.reporter_id != request.user.id:
        raise PermissionDenied
    if request.method != 'POST':
        return redirect('lost_found_detail', post_id=lead.post_id)
    if lead.status != 'accepted':
        messages.info(request, '只有已确认的失物招领线索才能提交完成反馈。')
        return redirect('lost_found_detail', post_id=lead.post_id)
    if MutualAidFeedback.objects.filter(lost_found_lead=lead).exists():
        messages.info(request, '这次互助已经提交过结果反馈。')
        return redirect('lost_found_detail', post_id=lead.post_id)

    form = MutualAidFeedbackForm(request.POST)
    if not form.is_valid():
        messages.error(request, '反馈内容不符合要求，请检查后重试。')
        return redirect('lost_found_detail', post_id=lead.post_id)

    with transaction.atomic():
        locked_lead = LostFoundLead.objects.select_for_update(of=('self',)).select_related(
            'post', 'post__reporter', 'respondent',
        ).get(pk=lead.pk)
        if locked_lead.status != 'accepted':
            messages.info(request, '这条线索状态已经变化，暂时不能提交反馈。')
            return redirect('lost_found_detail', post_id=locked_lead.post_id)
        if MutualAidFeedback.objects.filter(lost_found_lead=locked_lead).exists():
            messages.info(request, '这次互助已经提交过结果反馈。')
            return redirect('lost_found_detail', post_id=locked_lead.post_id)
        feedback = MutualAidFeedback(
            lost_found_lead=locked_lead,
            submitted_by=request.user,
            outcome=form.cleaned_data['outcome'],
            tags=form.cleaned_data['tags'],
            note=form.cleaned_data['note'],
        )
        feedback.full_clean()
        feedback.save()
        record_mutual_aid_feedback_contribution(feedback)
        outcome_text = feedback.get_outcome_display()
        create_notification(
            locked_lead.respondent,
            actor=request.user,
            kind='mutual_aid_feedback',
            title='收到失物招领互助结果反馈',
            message=f'“{locked_lead.post.title}”已被发布者标记为{outcome_text}。',
            target_url=reverse('lost_found_detail', args=[locked_lead.post_id]),
            dedupe_key=f'mutual-aid-feedback-lost-found-{feedback.id}',
            dedupe_forever=True,
        )
    messages.success(request, '已记录这次失物招领互助的结果。')
    return redirect('lost_found_detail', post_id=lead.post_id)


@login_required
def unread_summary(request):
    quiet = quiet_hours_active(request.user)
    unread_notifications = active_unread_notifications(request.user, quiet=quiet)
    unread_messages = (
        PrivateMessage.objects.none() if quiet else
        PrivateMessage.objects.filter(receiver=request.user, is_read=False)
    )
    latest_entries = [
        {
            'type': 'notification',
            'title': notification.title,
            'message': notification.message,
            'url': notification.target_url or reverse('notification_list'),
            'created_at': notification.created_at,
        }
        for notification in unread_notifications.order_by('-created_at')[:3]
    ]
    latest_entries.extend(
        {
            'type': 'message',
            'title': f'{message.sender.username} 发来新私信',
            'message': message.content,
            'url': reverse('conversation', args=[message.sender_id]),
            'created_at': message.created_at,
        }
        for message in unread_messages.select_related('sender').order_by('-created_at')[:3]
    )
    latest_entries.sort(key=lambda entry: entry['created_at'], reverse=True)
    return JsonResponse({
        'notifications': unread_notifications.count(),
        'messages': unread_messages.count(),
        'latest': [
            {
                **entry,
                'created_at': entry['created_at'].isoformat(),
            }
            for entry in latest_entries[:5]
        ],
    })


@login_required
def notification_list(request):
    status_filter = request.GET.get('status', 'all').strip()
    if status_filter not in {'all', 'unread', 'snoozed', 'read'}:
        status_filter = 'all'
    kind_filter = request.GET.get('kind', '').strip()
    allowed_kinds = {value for value, _ in Notification.KIND_CHOICES}
    if kind_filter not in allowed_kinds:
        kind_filter = ''
    search_query = request.GET.get('q', '').strip()[:120]

    all_notifications = Notification.objects.filter(recipient=request.user)
    notifications = all_notifications
    now = timezone.now()
    quiet = quiet_hours_active(request.user, now=now)
    if status_filter == 'unread':
        notifications = notifications.filter(actionable_unread_q(now))
    elif status_filter == 'snoozed':
        notifications = notifications.filter(
            is_read=False, snoozed_until__gt=now,
        )
    elif status_filter == 'read':
        notifications = notifications.filter(is_read=True)
    if kind_filter:
        notifications = notifications.filter(kind=kind_filter)
    if search_query:
        notifications = notifications.filter(
            Q(title__icontains=search_query)
            | Q(message__icontains=search_query)
            | Q(item__title__icontains=search_query)
            | Q(order__item__title__icontains=search_query)
            | Q(demand__title__icontains=search_query)
        )

    notifications = notifications.select_related('actor', 'item', 'order__item', 'demand')
    paginator = Paginator(notifications, 20)
    page = paginator.get_page(request.GET.get('page'))

    kind_counts = dict(
        all_notifications.values('kind').annotate(count=Count('id')).values_list('kind', 'count')
    )
    badge_notifications = active_unread_notifications(
        request.user, now=now, quiet=quiet,
    )
    unread_kind_counts = dict(
        badge_notifications
        .values('kind')
        .annotate(count=Count('id'))
        .values_list('kind', 'count')
    )
    notification_kind_options = [
        {
            'value': value,
            'label': label,
            'count': kind_counts.get(value, 0),
            'unread_count': unread_kind_counts.get(value, 0),
        }
        for value, label in Notification.KIND_CHOICES
    ]
    notification_status_options = (
        ('all', '全部状态'),
        ('unread', '仅看待处理未读'),
        ('snoozed', '仅看已延后'),
        ('read', '仅看已读'),
    )
    # ``notifications`` starts from ``all_notifications``, so with no status,
    # kind or search filter the paginator's COUNT and the "全部" total are
    # aggregates over the very same rows.  The filtered-unread counter matches
    # the badge total for the same reason whenever the filters leave the
    # actionable set alone: an "未读" filter is idempotent, and "已读" or
    # "已延后" hold no actionable row at all.
    feed_is_unfiltered = (
        status_filter == 'all' and not kind_filter and not search_query
    )
    notification_total_all = (
        paginator.count if feed_is_unfiltered else all_notifications.count()
    )
    # The per-kind rollup below already visits every badge-eligible row, so
    # summing it yields the same total without a second COUNT over the
    # identical queryset.
    notification_unread_total = sum(unread_kind_counts.values())
    if quiet or status_filter not in {'all', 'unread'}:
        notification_filtered_unread_count = 0
    elif not kind_filter and not search_query:
        notification_filtered_unread_count = notification_unread_total
    else:
        notification_filtered_unread_count = notifications.filter(
            actionable_unread_q(now),
        ).count()
    filter_params = request.GET.copy()
    filter_params.pop('page', None)
    return render(request, 'listings/notifications.html', {
        'notifications': page,
        'notification_page': page,
        'notification_total': paginator.count,
        'notification_total_all': notification_total_all,
        'notification_unread_total': notification_unread_total,
        'notification_filtered_unread_count': notification_filtered_unread_count,
        'notification_kind_options': notification_kind_options,
        'notification_status_options': notification_status_options,
        'notification_status_filter': status_filter,
        'notification_kind_filter': kind_filter,
        'notification_search_query': search_query,
        'notification_filter_query': filter_params.urlencode(),
        'notification_quiet_hours_active': quiet,
        'notification_snooze_options': NOTIFICATION_SNOOZE_OPTIONS,
    })


@login_required
def activity_center(request):
    """Show notifications and incoming private messages in one chronological feed."""
    activity_type = request.GET.get('type', 'all').strip()
    if activity_type not in {'all', 'notifications', 'messages'}:
        activity_type = 'all'
    status_filter = request.GET.get('status', 'all').strip()
    if status_filter not in {'all', 'unread', 'read'}:
        status_filter = 'all'
    search_query = request.GET.get('q', '').strip()[:120]
    now = timezone.now()
    quiet = quiet_hours_active(request.user, now=now)

    notification_queryset = Notification.objects.filter(recipient=request.user)
    message_queryset = PrivateMessage.objects.filter(receiver=request.user)
    if status_filter == 'unread':
        notification_queryset = notification_queryset.filter(actionable_unread_q(now))
        message_queryset = message_queryset.filter(is_read=False)
    elif status_filter == 'read':
        notification_queryset = notification_queryset.filter(is_read=True)
        message_queryset = message_queryset.filter(is_read=True)
    if search_query:
        notification_queryset = notification_queryset.filter(
            Q(title__icontains=search_query)
            | Q(message__icontains=search_query)
            | Q(item__title__icontains=search_query)
            | Q(order__item__title__icontains=search_query)
            | Q(demand__title__icontains=search_query)
        )
        message_queryset = message_queryset.filter(
            Q(content__icontains=search_query)
            | Q(sender__username__icontains=search_query)
        )

    all_notification_queryset = Notification.objects.filter(recipient=request.user)
    all_message_queryset = PrivateMessage.objects.filter(receiver=request.user)
    filtered_notifications = notification_queryset if activity_type in {'all', 'notifications'} else Notification.objects.none()
    filtered_messages = message_queryset if activity_type in {'all', 'messages'} else PrivateMessage.objects.none()

    # The four headline counters below look like eight aggregates, but they
    # collapse onto far fewer queries: with no filter active the filtered feed
    # *is* the full feed, an "未读" filter is exactly the actionable-unread
    # set, and a "已读" filter cannot hold an unread row at all.  Each count is
    # therefore computed once and reused rather than issued twice.
    notification_feed_is_full = (
        activity_type in {'all', 'notifications'}
        and status_filter == 'all'
        and not search_query
    )
    message_feed_is_full = (
        activity_type in {'all', 'messages'}
        and status_filter == 'all'
        and not search_query
    )
    notification_feed_is_unread = (
        activity_type in {'all', 'notifications'}
        and status_filter == 'unread'
        and not search_query
    )
    message_feed_is_unread = (
        activity_type in {'all', 'messages'}
        and status_filter == 'unread'
        and not search_query
    )
    notification_unread_is_shared = (
        activity_type in {'all', 'notifications'}
        and status_filter in {'all', 'unread'}
        and not search_query
    )
    message_unread_is_shared = (
        activity_type in {'all', 'messages'}
        and status_filter in {'all', 'unread'}
        and not search_query
    )

    total_notifications = filtered_notifications.count()
    total_messages = filtered_messages.count()
    total_count = total_notifications + total_messages
    all_notification_count = (
        total_notifications if notification_feed_is_full
        else all_notification_queryset.count()
    )
    all_message_count = (
        total_messages if message_feed_is_full
        else all_message_queryset.count()
    )
    if quiet:
        unread_notifications = unread_messages = 0
        filtered_unread_notifications = filtered_unread_messages = 0
    else:
        unread_notifications = (
            total_notifications if notification_feed_is_unread
            else active_unread_notifications(
                request.user, now=now, quiet=quiet,
            ).count()
        )
        unread_messages = (
            total_messages if message_feed_is_unread
            else all_message_queryset.filter(is_read=False).count()
        )
        if status_filter == 'read':
            # A "已读" feed holds no unread row, so both filtered-unread
            # counters are zero by construction rather than by query.
            filtered_unread_notifications = filtered_unread_messages = 0
        else:
            filtered_unread_notifications = (
                unread_notifications if notification_unread_is_shared
                else filtered_notifications.filter(actionable_unread_q(now)).count()
            )
            filtered_unread_messages = (
                unread_messages if message_unread_is_shared
                else filtered_messages.filter(is_read=False).count()
            )
    page_size = 20
    try:
        page_number = max(1, int(request.GET.get('page', '1')))
    except (TypeError, ValueError):
        page_number = 1
    row_limit = page_number * page_size

    rows = []
    for notification in filtered_notifications.select_related('actor', 'item', 'order__item', 'demand').order_by('-created_at', '-id')[:row_limit]:
        rows.append({
            'source': 'notification',
            'source_label': '通知',
            'title': notification.title,
            'message': notification.message,
            'context': (
                notification.item.title if notification.item else
                (notification.demand.title if notification.demand else '')
            ),
            'created_at': notification.created_at,
            'is_read': notification.is_read,
            'is_snoozed': notification.is_snoozed,
            'url': notification.target_url or reverse('notification_list'),
            'read_url': reverse('mark_notification_read', args=[notification.id]),
            'icon': 'bi-bell',
            'icon_class': f'notification-icon-{notification.kind}',
        })
    for private_message in filtered_messages.select_related('sender', 'item').order_by('-created_at', '-id')[:row_limit]:
        rows.append({
            'source': 'message',
            'source_label': '私信',
            'title': f'{private_message.sender.username} 发来新私信',
            'message': private_message.content,
            'context': private_message.item.title if private_message.item else '',
            'created_at': private_message.created_at,
            'is_read': private_message.is_read,
            'url': reverse('conversation', args=[private_message.sender_id]),
            'read_url': reverse('mark_message_read', args=[private_message.id]),
            'icon': 'bi-chat-dots',
            'icon_class': 'notification-icon-message_received',
        })
    rows.sort(key=lambda row: (row['created_at'], row['source'], row['url']), reverse=True)
    offset = (page_number - 1) * page_size
    page_rows = rows[offset:offset + page_size]
    filter_params = request.GET.copy()
    filter_params.pop('page', None)

    return render(request, 'listings/activity_center.html', {
        'activity_rows': page_rows,
        'activity_page_number': page_number,
        'activity_page_size': page_size,
        'activity_total': total_count,
        'activity_has_previous': page_number > 1,
        'activity_has_next': len(rows) > offset + page_size or total_count > offset + page_size,
        'activity_filter_query': filter_params.urlencode(),
        'activity_type_filter': activity_type,
        'activity_status_filter': status_filter,
        'activity_search_query': search_query,
        'activity_unread_total': unread_notifications + unread_messages,
        'activity_filtered_unread_count': (
            filtered_unread_notifications + filtered_unread_messages
        ),
        'notification_quiet_hours_active': quiet,
        'activity_notification_count': all_notification_count,
        'activity_message_count': all_message_count,
    })


@login_required
def mark_all_activity_read(request):
    if request.method == 'POST':
        with transaction.atomic():
            notification_count = Notification.objects.filter(
                recipient=request.user, is_read=False,
            ).update(is_read=True, read_at=timezone.now())
            message_count = PrivateMessage.objects.filter(
                receiver=request.user, is_read=False,
            ).update(is_read=True)
        total_count = notification_count + message_count
        if total_count:
            messages.success(request, f'已将 {total_count} 条站内动态标记为已读。')
        else:
            messages.info(request, '当前没有未读站内动态。')

    next_url = request.POST.get('next', '').strip()
    if not next_url or not url_has_allowed_host_and_scheme(
        next_url,
        allowed_hosts={request.get_host()},
        require_https=request.is_secure(),
    ):
        next_url = reverse('activity_center')
    return redirect(next_url)


@login_required
def notification_preferences(request):
    preference, _ = NotificationPreference.objects.get_or_create(user=request.user)
    if request.method == 'POST':
        form = NotificationPreferenceForm(request.POST, instance=preference)
        if form.is_valid():
            form.save()
            messages.success(request, '通知偏好已保存，之后可以随时调整。')
            return redirect('notification_preferences')
    else:
        form = NotificationPreferenceForm(instance=preference)
    return render(request, 'listings/notification_preferences.html', {
        'form': form,
        'enabled_notification_count': sum(
            bool(getattr(preference, field_name))
            for field_name in form.fields
        ),
        'notification_kind_count': len(form.fields),
    })


@login_required
def snooze_notification(request, notification_id):
    """Temporarily remove an unread notification from the actionable inbox."""
    notification = get_object_or_404(
        Notification,
        id=notification_id,
        recipient=request.user,
    )
    if request.method == 'POST' and not notification.is_read:
        duration_key = request.POST.get('duration', '24h').strip()
        duration, label = NOTIFICATION_SNOOZE_DURATIONS.get(
            duration_key, NOTIFICATION_SNOOZE_DURATIONS['24h'],
        )
        notification.snoozed_until = timezone.now() + duration
        notification.save(update_fields=['snoozed_until'])
        messages.success(request, f'这条通知已延后 {label}，到期后会重新计入未读提醒。')

    next_url = request.POST.get('next', '').strip()
    if not next_url or not url_has_allowed_host_and_scheme(
        next_url,
        allowed_hosts={request.get_host()},
        require_https=request.is_secure(),
    ):
        next_url = reverse('notification_list')
    return redirect(next_url)


@login_required
def mark_notification_read(request, notification_id):
    notification = get_object_or_404(
        Notification,
        id=notification_id,
        recipient=request.user,
    )
    if request.method == 'POST' and not notification.is_read:
        notification.mark_read()

    next_url = request.POST.get('next', '').strip()
    if not next_url or not url_has_allowed_host_and_scheme(
        next_url,
        allowed_hosts={request.get_host()},
        require_https=request.is_secure(),
    ):
        next_url = reverse('notification_list')
    return redirect(next_url)


@login_required
def mark_all_notifications_read(request):
    if request.method == 'POST':
        Notification.objects.filter(
            recipient=request.user,
            is_read=False,
        ).update(is_read=True, read_at=timezone.now())
        messages.success(request, '所有通知已标记为已读。')
    return redirect('notification_list')


@login_required
def mark_selected_notifications_read(request):
    if request.method == 'POST':
        raw_ids = request.POST.getlist('notification_ids')
        notification_ids = []
        for raw_id in raw_ids:
            try:
                notification_ids.append(int(raw_id))
            except (TypeError, ValueError):
                continue
        updated_count = Notification.objects.filter(
            recipient=request.user,
            is_read=False,
            id__in=notification_ids,
        ).update(is_read=True, read_at=timezone.now())
        if updated_count:
            messages.success(request, f'已将 {updated_count} 条通知标记为已读。')
        else:
            messages.info(request, '请选择至少一条未读通知。')

    next_url = request.POST.get('next', '').strip()
    if not next_url or not url_has_allowed_host_and_scheme(
        next_url,
        allowed_hosts={request.get_host()},
        require_https=request.is_secure(),
    ):
        next_url = reverse('notification_list')
    return redirect(next_url)


def _notification_action_redirect(request):
    """Return to the current notification view without trusting external hosts."""
    next_url = request.POST.get('next', '').strip()
    if not next_url or not url_has_allowed_host_and_scheme(
        next_url,
        allowed_hosts={request.get_host()},
        require_https=request.is_secure(),
    ):
        next_url = reverse('notification_list')
    return redirect(next_url)


@login_required
@require_POST
def snooze_selected_notifications(request):
    raw_ids = request.POST.getlist('notification_ids')
    notification_ids = normalize_notification_ids(raw_ids)
    if not notification_ids:
        messages.info(request, '请选择至少一条当前可处理的未读通知。')
        return _notification_action_redirect(request)

    duration_key = request.POST.get('duration', '').strip()
    if duration_key not in NOTIFICATION_SNOOZE_DURATIONS:
        messages.error(request, '延后时长无效，请重新选择。')
        return _notification_action_redirect(request)

    updated_count, skipped_count = bulk_snooze_notifications(
        request.user,
        notification_ids,
        duration_key=duration_key,
    )
    label = NOTIFICATION_SNOOZE_DURATIONS[duration_key][1]
    if updated_count:
        message = f'已将 {updated_count} 条通知延后 {label}。'
        if skipped_count:
            message += f'另有 {skipped_count} 条因状态变化或不属于当前账号未处理。'
        messages.success(request, message)
    else:
        messages.info(request, '所选通知已不再适合延后，请刷新后重试。')
    return _notification_action_redirect(request)


@login_required
@require_POST
def restore_selected_notifications(request):
    raw_ids = request.POST.getlist('notification_ids')
    notification_ids = normalize_notification_ids(raw_ids)
    if not notification_ids:
        messages.info(request, '请选择至少一条仍在延后中的未读通知。')
        return _notification_action_redirect(request)

    updated_count, skipped_count = restore_snoozed_notifications(
        request.user,
        notification_ids,
    )
    if updated_count:
        message = f'已恢复 {updated_count} 条通知，它们重新计入未读提醒。'
        if skipped_count:
            message += f'另有 {skipped_count} 条因状态变化或不属于当前账号未处理。'
        messages.success(request, message)
    else:
        messages.info(request, '所选通知已经恢复、被读或不再属于当前账号。')
    return _notification_action_redirect(request)
