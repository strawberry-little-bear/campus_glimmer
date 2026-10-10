import csv
import mimetypes
import secrets
from decimal import Decimal, InvalidOperation
from pathlib import Path
from datetime import timedelta
from urllib.parse import urlencode

from django.contrib.auth.decorators import login_required
from django.contrib.auth.hashers import check_password, make_password
from django.contrib.auth.models import User
from django.contrib import messages
from django.core.paginator import Paginator
from django.core.exceptions import PermissionDenied
from django.http import FileResponse, HttpResponse, JsonResponse
from django.db import IntegrityError, transaction
from django.db.models import Avg, Case, Count, ExpressionWrapper, F, FloatField, IntegerField, Q, Value, When
from django.db.models.functions import Cast
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.utils.http import url_has_allowed_host_and_scheme

from .analytics import PERIOD_CHOICES, build_operations_dashboard, build_search_insights
from .search_combo_shift import build_search_combo_shift
from .search_threshold_feedback import build_search_threshold_feedback
from .search_trend import build_search_trend
from .governance_sla import build_governance_sla
from .demand_radar import build_demand_radar
from .search_rewrites import build_search_rewrite_candidates
from .search_synonym_effect import build_search_synonym_effects
from .demand_radar_outcome import build_demand_radar_outcomes
from .circular_impact import build_circular_impact_report
from .contributions import (
    build_contribution_summary, record_demand_response_contribution,
    record_lost_found_lead_contribution, record_mutual_aid_feedback_contribution,
    record_order_contribution,
)
from .availability import notify_item_available
from .borrow_escalation import close_escalation
from .demand_matching import _match_demand, notify_demand_matches
from .lost_found_matching import expire_lost_found_posts, find_lost_found_matches, score_lost_found_posts
from .forms import DeliveryCodeForm, DemandPostForm, DisputeEvidenceForm, DisputeForm, DisputeResolutionForm, MeetingIncidentReviewForm, DemandResponseForm, FavoriteCollectionForm, ItemForm, ItemImageFormSet, LostFoundLeadForm, LostFoundPostForm, MeetingAppointmentForm, MeetingIncidentForm, MutualAidFeedbackForm, NotificationPreferenceForm, OrderForm, RatingForm, ReportForm, ReportReviewForm, SavedSearchForm
from .models import BrowsingHistory, CampusCampaign, CampusLocation, Category, DemandOpportunityTask, DemandPost, DeliveryConfirmation, Favorite, FavoriteCollection, GiftApplication, Item, ItemAvailabilityWatch, LostFoundLead, LostFoundPost, MeetingAppointment, MeetingIncident, Notification, NotificationPreference, OpportunityDismissal, DemandResponse, MutualAidFeedback, Order, OrderDispute, OrderDisputeEvidence, OrderEvent, Rating, RecommendationFeedback, Report, SavedSearch, SavedSearchMatch, SearchClick, SearchImpression, SearchQuery, SearchSynonym
from .recommendations import get_recommendations
from .reputation import build_seller_reputation
from .notifications import active_unread_notifications, actionable_unread_q, create_notification, quiet_hours_active
from .opportunity_feed import build_opportunity_feed
from .price_insights import build_price_insight
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
from .transaction_safety import build_transaction_safety
from chat_messages.models import ModerationEvent, PrivateMessage
from accounts.models import CampusVerification


def _favorite_ids(request):
    if not request.user.is_authenticated:
        return set()
    return set(Favorite.objects.filter(user=request.user).values_list('item_id', flat=True))


def home(request):
    active_item_filter = Q(items__status='available') & (
        Q(items__expires_at__isnull=True) | Q(items__expires_at__gt=timezone.now())
    )
    categories = Category.objects.annotate(available_count=Count('items', filter=active_item_filter))
    locations = CampusLocation.objects.filter(is_active=True).annotate(available_count=Count('items', filter=active_item_filter))
    recent_items = Item.objects.available().select_related('category', 'seller', 'location').prefetch_related('images')[:8]
    context = {
        'categories': categories,
        'locations': locations,
        'recent_items': recent_items,
        'recommendations': get_recommendations(request.user, limit=8),
        'favorite_ids': _favorite_ids(request),
        'stats': {
            'items': Item.objects.available().count(),
            'categories': categories.count(),
            'members': Item.objects.values('seller').distinct().count(),
        },
    }
    return render(request, 'listings/home.html', context)


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
        feedback_click_filter = Q(
            search_clicks__created_at__gte=feedback_window,
            search_clicks__search_query__query__iexact=query,
        )
        feedback_impression_filter = Q(
            search_impressions__created_at__gte=feedback_window,
            search_impressions__search_query__query__iexact=query,
        )
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
            feedback_click_count=Count(
                'search_clicks', filter=feedback_click_filter, distinct=True,
            ),
            feedback_impression_count=Count(
                'search_impressions', filter=feedback_impression_filter, distinct=True,
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


def campaign_list(request):
    now = timezone.now()
    campaigns = _live_campaigns(now).annotate(
        available_item_count=Count(
            'items',
            filter=(
                Q(items__status='available')
                & (Q(items__expires_at__isnull=True) | Q(items__expires_at__gt=now))
            ),
            distinct=True,
        ),
    ).order_by('-starts_at', 'title')
    return render(request, 'listings/campaign_list.html', {
        'campaigns': campaigns,
        'title': '校园专题',
    })


def campaign_detail(request, slug):
    campaign = get_object_or_404(_live_campaigns(), slug=slug)
    items = Item.objects.available().filter(campaign=campaign).select_related(
        'category', 'seller', 'location',
    ).prefetch_related('images')
    return render(request, 'listings/campaign_detail.html', {
        'campaign': campaign,
        'items': items,
        'favorite_ids': _favorite_ids(request),
        'title': campaign.title,
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
        Item.objects.select_related('category', 'seller', 'location').prefetch_related('images', 'comments__author__profile'),
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
    context = {
        'item': item,
        'active_order': Order.objects.filter(
            item_id=item.id, status__in=Order.ACTIVE_STATUS_VALUES,
        ).order_by('-created_at').first(),
        'related_items': related_items,
        'rating_summary': rating_summary,
        'seller_reputation': build_seller_reputation(item.seller),
        'price_insight': build_price_insight(item),
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
    item = get_object_or_404(Item, id=item_id)
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
                        locked_item = Item.objects.select_for_update().select_related('seller', 'location').get(id=item.id)
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
                    locked_item = Item.objects.select_for_update().select_related('seller').get(id=item.id)
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
    ).select_related('item__seller', 'item__location', 'meeting_location', 'order').order_by(
        'status', '-created_at',
    )
    return render(request, 'listings/gift_applications.html', {
        'applications': applications,
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
    applications = GiftApplication.objects.filter(item=item).select_related(
        'applicant', 'meeting_location', 'order',
    ).order_by('status', 'created_at')
    return render(request, 'listings/manage_gift_applications.html', {
        'item': item,
        'applications': applications,
        'pending_count': applications.filter(status='pending').count(),
        'title': '管理领取申请',
    })


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
        return redirect('manage_gift_applications', item_id=application.item_id)

    try:
        with transaction.atomic():
            locked_application = GiftApplication.objects.select_for_update().select_related(
                'item', 'applicant', 'item__seller', 'meeting_location',
            ).get(pk=application.id)
            locked_item = Item.objects.select_for_update().get(pk=locked_application.item_id)
            if locked_application.status != 'pending':
                messages.info(request, '这个申请已经处理过了。')
                return redirect('manage_gift_applications', item_id=locked_application.item_id)
            if locked_item.trade_mode != 'free' or not locked_item.is_available_now:
                messages.info(request, '商品已经无法继续选择领取人。')
                return redirect('manage_gift_applications', item_id=locked_item.id)
            if Order.objects.filter(
                item_id=locked_item.id, status__in=Order.ACTIVE_STATUS_VALUES,
            ).exists():
                messages.info(request, '这个商品已经有一笔进行中的交易。')
                return redirect('manage_gift_applications', item_id=locked_item.id)

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
                GiftApplication.objects.select_for_update().select_related('applicant').filter(
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
        return redirect('manage_gift_applications', item_id=application.item_id)

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
    transaction_safety = build_transaction_safety(order)
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
        meeting_location_recommendations = recommend_meeting_locations(order=order)
        meeting_time_recommendations = recommend_meeting_times(order=order)
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
        'transaction_safety': transaction_safety,
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
        locked_order = Order.objects.select_for_update().select_related(
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
        appointment, created = MeetingAppointment.objects.select_for_update().get_or_create(
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
        locked_order = Order.objects.select_for_update().select_related(
            'item', 'buyer', 'seller',
        ).get(pk=order.id)
        appointment = MeetingAppointment.objects.select_for_update().select_related(
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
        locked_order = Order.objects.select_for_update().select_related(
            'item', 'buyer', 'seller',
        ).get(pk=order.id)
        appointment = MeetingAppointment.objects.select_for_update().get(order=locked_order)
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
        locked_appointment = MeetingAppointment.objects.select_for_update().select_related(
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
        locked_order = Order.objects.select_for_update().select_related('item', 'buyer', 'seller').get(pk=order.id)
        if locked_order.status != 'meeting':
            messages.error(request, '订单状态已经发生变化，请刷新后再试。')
            return redirect('order_detail', order_id=locked_order.id)
        confirmation, _ = DeliveryConfirmation.objects.select_for_update().get_or_create(order=locked_order)
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
        locked_order = Order.objects.select_for_update().select_related('item', 'buyer', 'seller').get(pk=order.id)
        confirmation, _ = DeliveryConfirmation.objects.select_for_update().get_or_create(order=locked_order)
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
        locked_order = Order.objects.select_for_update().select_related(
            'item', 'buyer', 'seller',
        ).get(pk=order.id)
        if locked_order.item.trade_mode != 'borrow' or locked_order.status != 'borrowed':
            messages.info(request, '这笔借用订单已经发生变化，请刷新后再试。')
            return redirect('order_detail', order_id=locked_order.id)

        confirmation, _ = DeliveryConfirmation.objects.select_for_update().get_or_create(
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
def report_list(request):
    if not request.user.is_staff:
        raise PermissionDenied
    reports = Report.objects.select_related(
        'item__seller', 'item__category', 'item__location', 'reporter', 'reviewer',
    )
    status_filter = request.GET.get('status', 'all')
    if status_filter in dict(Report.STATUS_CHOICES):
        reports = reports.filter(status=status_filter)
    else:
        status_filter = 'all'
    return render(request, 'listings/reports.html', {
        'reports': reports,
        'status_filter': status_filter,
        'report_statuses': Report.STATUS_CHOICES,
        'pending_report_count': Report.objects.filter(status__in={'pending', 'reviewing'}).count(),
        'title': '商品举报审核',
    })


@login_required
def review_report(request, report_id):
    if not request.user.is_staff:
        raise PermissionDenied
    report = get_object_or_404(
        Report.objects.select_related('item__seller', 'item__category', 'item__location', 'reporter', 'reviewer'),
        id=report_id,
    )
    if request.method == 'POST':
        form = ReportReviewForm(request.POST, instance=report)
        if form.is_valid():
            with transaction.atomic():
                report = Report.objects.select_for_update().select_related(
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
                    target_url=reverse('item_detail', args=[report.item_id]),
                )
            messages.success(request, '举报审核结果已保存，举报人会收到通知。')
            return redirect('report_list')
    else:
        form = ReportReviewForm(instance=report, initial={'status': 'reviewing'})
    return render(request, 'listings/report_review.html', {
        'form': form,
        'report': report,
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
                incident = MeetingIncident.objects.select_for_update().select_related(
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
    disputes = OrderDispute.objects.select_related(
        'order__item', 'order__buyer', 'order__seller', 'opened_by', 'reviewer',
    )
    return render(request, 'listings/disputes.html', {
        'disputes': disputes,
        'pending_dispute_count': disputes.filter(status__in={'open', 'reviewing'}).count(),
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
                dispute = OrderDispute.objects.select_for_update().select_related(
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


@login_required
def opportunity_feed(request):
    return render(request, 'listings/opportunity_feed.html', {
        'feed': build_opportunity_feed(request.user),
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
    orders = Order.objects.filter(Q(buyer=request.user) | Q(seller=request.user)).select_related('item', 'buyer', 'seller', 'meeting_location')
    return render(request, 'listings/my_orders.html', {'orders': orders, 'title': '我的交易'})


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
    favorites = all_favorites.select_related(
        'item__category', 'item__seller', 'item__location', 'collection',
    ).prefetch_related('item__images')
    if selected_collection:
        favorites = favorites.filter(collection=selected_collection)
    context = {
        'favorites': favorites,
        'favorite_ids': set(all_favorites.values_list('item_id', flat=True)),
        'collections': collections,
        'selected_collection': selected_collection,
        'collection_form': FavoriteCollectionForm(),
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
            messages.success(request, '商品已成功发布，快去分享给同学吧！')
            return redirect('item_detail', item_id=item.id)
    else:
        form = ItemForm()
        formset = ItemImageFormSet()
    return render(request, 'listings/item_form.html', {'form': form, 'formset': formset, 'title': '发布新商品'})


@login_required
def edit_item(request, item_id):
    item = get_object_or_404(Item, id=item_id)
    if item.seller != request.user:
        messages.error(request, '您没有权限编辑此商品。')
        return redirect('item_detail', item_id=item.id)
    if request.method == 'POST':
        form = ItemForm(request.POST, instance=item)
        formset = ItemImageFormSet(request.POST, request.FILES, instance=item)
        if form.is_valid() and formset.is_valid():
            saved_item = form.save()
            if saved_item.status == 'expired' and saved_item.expires_at and saved_item.expires_at > timezone.now():
                saved_item.status = 'available'
                saved_item.save(update_fields=['status', 'updated_at'])
                messages.info(request, '商品已重新上架，并按新的展示截止时间继续展示。')
            formset.save()
            messages.success(request, '商品信息已更新。')
            return redirect('item_detail', item_id=item.id)
    else:
        form = ItemForm(instance=item)
        formset = ItemImageFormSet(instance=item)
    return render(request, 'listings/item_form.html', {'form': form, 'formset': formset, 'title': '编辑商品', 'item': item})


@login_required
def delete_item(request, item_id):
    item = get_object_or_404(Item, id=item_id)
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
    item = get_object_or_404(Item, id=item_id)
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
    history = BrowsingHistory.objects.filter(user=request.user).select_related('item__category', 'item__seller', 'item__location').prefetch_related('item__images')[:30]
    return render(request, 'listings/browsing_history.html', {'history': history, 'title': '最近浏览'})


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
        return redirect('my_items')

    now = timezone.now()
    with transaction.atomic():
        locked = Item.objects.select_for_update().get(pk=item.pk)
        can_refresh, reason = _refresh_state(locked, now=now)
        if not can_refresh:
            messages.warning(request, reason)
            return redirect('my_items')
        locked.last_refreshed_at = now
        locked.refresh_count = locked.refresh_count + 1
        locked.save(update_fields=['last_refreshed_at', 'refresh_count', 'updated_at'])

    messages.success(request, f'已刷新《{item.title}》，商品会重新回到列表前排。')
    return redirect('my_items')


@login_required
def my_items(request):
    items = Item.objects.filter(seller=request.user).select_related(
        'category', 'location', 'campaign',
    ).prefetch_related('images')
    now = timezone.now()
    campaigns = CampusCampaign.objects.filter(is_active=True).filter(
        Q(ends_at__isnull=True) | Q(ends_at__gt=now),
    ).order_by('-starts_at', 'title')
    lifecycle = build_seller_lifecycle(request.user)
    diagnoses_by_id = {row.item_id: row for row in lifecycle['diagnoses']}
    # 把诊断结果挂到商品对象上，模板里可以直接用 item.lifecycle_diagnosis，
    # 不需要额外注册模板过滤器。
    for item in items:
        item.lifecycle_diagnosis = diagnoses_by_id.get(item.pk)
    return render(request, 'listings/my_items.html', {
        'items': items,
        'campaigns': campaigns,
        'lifecycle': lifecycle,
        'title': '我的商品',
    })


@login_required
def bulk_assign_campaign(request):
    if request.method != 'POST':
        return redirect('my_items')

    item_ids = [value for value in request.POST.getlist('item_ids') if value.isdigit()]
    if not item_ids:
        messages.warning(request, '请先选择至少一件商品。')
        return redirect('my_items')

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
        updated = Item.objects.select_for_update().filter(
            seller=request.user, id__in=item_ids,
        ).update(campaign=campaign, updated_at=timezone.now())

    action = f'加入“{campaign.title}”' if campaign else '移出当前专题'
    messages.success(request, f'已将 {updated} 件商品{action}。')
    return redirect('my_items')


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


@login_required
def operations_dashboard(request):
    if not request.user.is_staff:
        raise PermissionDenied
    days = _operations_period_days(request)
    dashboard = build_operations_dashboard(days)
    dashboard['pending_dispute_count'] = OrderDispute.objects.filter(
        status__in={'open', 'reviewing'},
    ).count()
    dashboard['pending_moderation_count'] = ModerationEvent.objects.filter(status='pending').count()
    dashboard['pending_high_moderation_count'] = ModerationEvent.objects.filter(
        status='pending', risk_level='high',
    ).count()
    active_tasks = DemandOpportunityTask.objects.filter(
        status__in=['todo', 'in_progress'],
    ).select_related('assigned_to')
    tasks_by_key = {task.radar_key: task for task in active_tasks}
    for row in dashboard['demand_radar']['rows']:
        row['active_task'] = tasks_by_key.get(row['radar_key'])
    dashboard['demand_radar_tasks'] = active_tasks[:20]
    dashboard['demand_radar_outcomes'] = build_demand_radar_outcomes(
        days=days, statuses=_demand_outcome_status_filter(request),
    )
    dashboard['demand_outcome_status_filter'] = _demand_outcome_status_filter(request)
    return render(request, 'listings/operations_dashboard.html', dashboard)


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
        if status in dict(DemandOpportunityTask.STATUS_CHOICES):
            task.status = status
            if not task.assigned_to_id and status == 'in_progress':
                task.assigned_to = request.user
            task.save(update_fields=['status', 'assigned_to', 'updated_at'])
            messages.success(request, f'任务“{task.title}”已更新为{task.get_status_display()}。')
    try:
        days = int(request.POST.get('days', 30))
    except (TypeError, ValueError):
        days = 30
    return redirect(f'{reverse("operations_dashboard")}?days={days}')


@login_required
def operations_dashboard_export(request):
    if not request.user.is_staff:
        raise PermissionDenied

    dashboard = build_operations_dashboard(_operations_period_days(request))
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
    writer.writerow([])
    writer.writerow(['求购匹配转化漏斗', '数量', '相对上一步转化率（%）', '口径说明'])
    for stage in dashboard['demand_match_insights']['funnel']:
        writer.writerow([stage['label'], stage['count'], stage['rate'], stage['note']])

    writer.writerow([])
    writer.writerow(['求购匹配分类表现', '响应数', '求购数', '确认数', '响应确认率（%）'])
    for row in dashboard['demand_match_insights']['category_rows']:
        writer.writerow([row['name'], row['response_count'], row['demand_count'], row['accepted_count'], row['acceptance_rate']])

    writer.writerow([])
    writer.writerow(['求购匹配地点表现', '响应数', '求购数', '确认数', '响应确认率（%）'])
    for row in dashboard['demand_match_insights']['location_rows']:
        writer.writerow([row['name'], row['response_count'], row['demand_count'], row['accepted_count'], row['acceptance_rate']])

    writer.writerow([])
    writer.writerow(['通知类型', '触达事件', '通知记录', '未读记录', '压缩率（%）'])
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
    writer.writerow(['排名', '用户名', '贡献事件', '贡献积分'])
    for rank, row in enumerate(contribution_insights['top_users'], start=1):
        writer.writerow([rank, row['username'], row['event_count'], row['points']])

    writer.writerow([])
    writer.writerow(['每日互助趋势', '贡献积分', '贡献事件', '参与用户'])
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
    writer.writerow(['互助反馈来源', '反馈数', '完成数', '暂未解决数', '完成率（%）'])
    for row in feedback_insights['source_rows']:
        writer.writerow([row['label'], row['feedback_count'], row['completed_count'], row['unresolved_count'], row['completion_rate']])

    writer.writerow([])
    writer.writerow(['反馈标签', '次数', '占反馈（%）'])
    for row in feedback_insights['tag_rows']:
        writer.writerow([row['label'], row['count'], row['share']])

    writer.writerow([])
    writer.writerow(['互助闭环漏斗', '数量', '相对上一步转化率（%）', '口径说明'])
    for stage in feedback_insights['funnel']:
        writer.writerow([stage['label'], stage['count'], stage['rate'], stage['note']])

    writer.writerow([])
    writer.writerow([
        '待处理交易争议',
        OrderDispute.objects.filter(status__in={'open', 'reviewing'}).count(),
    ])

    writer.writerow([])
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

    writer.writerow([])
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
    writer.writerow(['用户回访', '人数', '比例（%）', '口径说明'])
    writer.writerow([
        '上一周期新用户', dashboard['user_retention']['cohort_size'], '', '上一周期内注册的新用户',
    ])
    writer.writerow([
        '回访用户', dashboard['user_retention']['retained_users'],
        dashboard['user_retention']['rate'], dashboard['user_retention']['note'],
    ])

    writer.writerow([])
    writer.writerow(['转化漏斗', '数量', '相对上一步转化率（%）', '口径说明'])
    for stage in dashboard['conversion_funnel']:
        writer.writerow([stage['label'], stage['count'], stage['rate'], stage['note']])

    writer.writerow([])
    writer.writerow(['每日活动趋势', '发布商品', '搜索次数', '交易预约', '活动总量'])
    for point in dashboard['activity_trend']:
        writer.writerow([
            point['date'], point['items'], point['searches'], point['orders'], point['total'],
        ])

    writer.writerow([])
    writer.writerow(['订单状态', '数量'])
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
    writer.writerow(['订单阶段耗时', '数值'])
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
    writer.writerow(['分类供给', '周期内新增', '当前在售'])
    for row in dashboard['category_stats']:
        writer.writerow([row.name, row.new_count, row.available_count])

    writer.writerow([])
    writer.writerow(['地点供给与交易', '周期内新增商品', '周期内交易预约'])
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

    rhythm = dashboard['borrow_rhythm']
    writer.writerow([])
    writer.writerow(['借用节奏与学期阶段交叉'])
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
    return response

def search_items(request):
    query = request.GET.get('q', '').strip()
    return redirect(f'/listings/?{urlencode({"q": query})}') if query else redirect('item_list')


def search_insights(request):
    if not request.user.is_staff:
        raise PermissionDenied
    try:
        period_days = int(request.GET.get('days', 30))
    except (TypeError, ValueError):
        period_days = 30
    dashboard = build_search_insights(period_days, request.GET.get('q', ''))
    dashboard['rewrite_candidates'] = build_search_rewrite_candidates(days=period_days)
    dashboard['synonym_effects'] = build_search_synonym_effects(days=period_days)
    dashboard['search_trend'] = build_search_trend(
        days=period_days, query=request.GET.get('q', ''),
    )
    dashboard['combo_shift'] = build_search_combo_shift(
        days=period_days, query=request.GET.get('q', ''),
    )
    dashboard['threshold_feedback'] = build_search_threshold_feedback(days=period_days)
    return render(request, 'listings/search_insights.html', dashboard)


@login_required
def search_insights_export(request):
    if not request.user.is_staff:
        raise PermissionDenied
    try:
        period_days = int(request.GET.get('days', 30))
    except (TypeError, ValueError):
        period_days = 30
    dashboard = build_search_insights(period_days, request.GET.get('q', ''))
    dashboard['rewrite_candidates'] = build_search_rewrite_candidates(days=period_days)
    dashboard['synonym_effects'] = build_search_synonym_effects(days=period_days)
    dashboard['search_trend'] = build_search_trend(
        days=period_days, query=request.GET.get('q', ''),
    )
    dashboard['combo_shift'] = build_search_combo_shift(
        days=period_days, query=request.GET.get('q', ''),
    )
    dashboard['threshold_feedback'] = build_search_threshold_feedback(days=period_days)

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
    writer.writerow(['搜索词分析', '搜索次数', '点击次数', '点击率（%）', '无结果次数', '无结果占比（%）', '平均结果数', '独立用户数', '最近搜索时间'])
    for row in dashboard['term_rows']:
        writer.writerow([
            row['query'], row['search_count'], row['click_count'], row['click_rate'],
            row['zero_result_count'], row['zero_result_rate'], row['average_results'],
            row['unique_users'], row['last_searched'],
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
    writer.writerow(['同义词候选', '出现次数', '涉及用户数', '置信提示', '最近出现', '处理方式'])
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

    writer.writerow([])
    writer.writerow([
        '搜索趋势与筛选偏好对比',
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
    writer.writerow(['高需求缺口', '搜索次数', '无结果次数', '无结果占比（%）', '运营建议'])
    for row in dashboard['insights']:
        writer.writerow([row['query'], row['search_count'], row['zero_result_count'], row['zero_result_rate'], row['message']])

    writer.writerow([])
    writer.writerow(['分类供给缺口', '分类', '搜索次数', '无结果次数', '无结果占比（%）', '当前在售', '缺口优先级'])
    for row in dashboard['facet_supply_gaps']['categories']:
        writer.writerow([
            '分类', row['category__name'], row['search_count'], row['zero_result_count'],
            row['zero_result_rate'], row['available_count'], row['priority_score'],
        ])
    writer.writerow([])
    writer.writerow(['地点供给缺口', '地点', '搜索次数', '无结果次数', '无结果占比（%）', '当前在售', '缺口优先级'])
    for row in dashboard['facet_supply_gaps']['locations']:
        writer.writerow([
            '地点', row['location__name'], row['search_count'], row['zero_result_count'],
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
    writer.writerow(['周期对比', '当前周期', '上一周期', '变化'])
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
            messages.success(request, f'已保存“{saved_search.name}”，有新商品匹配时会通知你。')
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
    return saved_searches


@login_required
def saved_search_list(request):
    saved_searches = SavedSearch.objects.filter(user=request.user).select_related('category', 'location')
    _attach_saved_search_stats(saved_searches, user=request.user)
    return render(request, 'listings/saved_searches.html', {
        'saved_searches': saved_searches,
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
    return redirect('saved_search_list')


@login_required
def update_saved_search_cadence(request, saved_search_id):
    """Change the reminder cadence of a saved search without recreating it."""
    saved_search = get_object_or_404(SavedSearch, id=saved_search_id, user=request.user)
    if request.method != 'POST':
        return redirect('saved_search_list')

    frequency = request.POST.get('notify_frequency', '')
    valid = dict(SavedSearch.FREQUENCY_CHOICES)
    if frequency not in valid:
        messages.error(request, '提醒频率无效，请重新选择。')
        return redirect('saved_search_list')

    saved_search.notify_frequency = frequency
    raw_limit = (request.POST.get('max_matches_per_notice') or '').strip()
    if raw_limit:
        try:
            limit = int(raw_limit)
        except ValueError:
            messages.error(request, '单次最多提醒条数需要是 1 到 9 之间的整数。')
            return redirect('saved_search_list')
        if not 1 <= limit <= 9:
            messages.error(request, '单次最多提醒条数需要是 1 到 9 之间的整数。')
            return redirect('saved_search_list')
        saved_search.max_matches_per_notice = limit

    raw_quiet = (request.POST.get('quiet_until') or '').strip()
    if raw_quiet:
        parsed = parse_datetime(raw_quiet)
        if parsed is None:
            messages.error(request, '临时静默时间格式不正确。')
            return redirect('saved_search_list')
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
            return redirect('saved_search_list')

    messages.success(request, f'“{saved_search.name}”的提醒节奏已更新为{valid[frequency]}。')
    return redirect('saved_search_list')


@login_required
def delete_saved_search(request, saved_search_id):
    saved_search = get_object_or_404(SavedSearch, id=saved_search_id, user=request.user)
    if request.method == 'POST':
        name = saved_search.name
        saved_search.delete()
        messages.success(request, f'“已删除关注搜索”{name}”。')
    return redirect('saved_search_list')




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
    return render(request, 'listings/demand_list.html', {
        'demands': page,
        'page_obj': page,
        'categories': Category.objects.all(),
        'locations': CampusLocation.objects.filter(is_active=True),
        'demand_search_query': search_query,
        'demand_category_filter': category_id,
        'demand_location_filter': location_id,
        'demand_sort': sort,
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
            locked_demand = DemandPost.objects.select_for_update().select_related(
                'requester', 'category', 'location',
            ).get(pk=demand.id)
            locked_item = Item.objects.select_for_update().select_related(
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

            response = DemandResponse.objects.select_for_update().filter(
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
        locked_response = DemandResponse.objects.select_for_update().select_related(
            'demand', 'item', 'responder',
        ).get(pk=response.id)
        locked_demand = DemandPost.objects.select_for_update().get(pk=locked_response.demand_id)
        locked_item = Item.objects.select_for_update().get(pk=locked_response.item_id)
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
            other_responses = DemandResponse.objects.select_for_update().filter(
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
        locked_response = DemandResponse.objects.select_for_update().select_related(
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

    query = request.GET.get('q', '').strip()
    post_type = request.GET.get('type', '').strip()
    category_id = request.GET.get('category', '').strip()
    location_id = request.GET.get('location', '').strip()
    sort = request.GET.get('sort', 'latest').strip()
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
    if category_id.isdigit():
        posts = posts.filter(category_id=category_id)
    if location_id.isdigit():
        posts = posts.filter(location_id=location_id)
    if sort == 'ending':
        posts = posts.order_by('expires_at', '-created_at')
    elif sort == 'popular':
        posts = posts.order_by('-view_count', '-created_at')
    else:
        sort = 'latest'
        posts = posts.order_by('-created_at')

    paginator = Paginator(posts, 12)
    page_obj = paginator.get_page(request.GET.get('page'))
    filter_query = urlencode({
        key: value for key, value in {
            'q': query, 'type': post_type, 'category': category_id,
            'location': location_id, 'sort': sort,
        }.items() if value
    })
    return render(request, 'listings/lost_found_list.html', {
        'posts': page_obj.object_list,
        'page_obj': page_obj,
        'categories': Category.objects.all(),
        'locations': CampusLocation.objects.filter(is_active=True),
        'is_mine': is_mine,
        'lost_found_query': query,
        'lost_found_type': post_type,
        'lost_found_category': category_id,
        'lost_found_location': location_id,
        'lost_found_sort': sort,
        'lost_found_filter_query': filter_query,
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
        locked_lead = LostFoundLead.objects.select_for_update().select_related(
            'post', 'respondent', 'related_post',
        ).get(pk=lead.pk)
        locked_post = LostFoundPost.objects.select_for_update().get(pk=locked_lead.post_id)
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
                related = LostFoundPost.objects.select_for_update().get(pk=locked_lead.related_post_id)
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
        locked_lead = LostFoundLead.objects.select_for_update().select_related(
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
    unread_notifications = active_unread_notifications(request.user)
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
    badge_notifications = active_unread_notifications(request.user, now=now)
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
    notification_total_all = all_notifications.count()
    notification_unread_total = badge_notifications.count()
    filter_params = request.GET.copy()
    filter_params.pop('page', None)
    return render(request, 'listings/notifications.html', {
        'notifications': page,
        'notification_page': page,
        'notification_total': paginator.count,
        'notification_total_all': notification_total_all,
        'notification_unread_total': notification_unread_total,
        'notification_filtered_unread_count': (
            0 if quiet else notifications.filter(actionable_unread_q(now)).count()
        ),
        'notification_kind_options': notification_kind_options,
        'notification_status_options': notification_status_options,
        'notification_status_filter': status_filter,
        'notification_kind_filter': kind_filter,
        'notification_search_query': search_query,
        'notification_filter_query': filter_params.urlencode(),
        'notification_quiet_hours_active': quiet,
        'notification_snooze_options': (
            ('2h', '2 小时'),
            ('24h', '24 小时'),
            ('3d', '3 天'),
        ),
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
    total_count = filtered_notifications.count() + filtered_messages.count()
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
        'activity_unread_total': (
            0 if quiet else active_unread_notifications(request.user, now=now).count()
        ) + (0 if quiet else all_message_queryset.filter(is_read=False).count()),
        'activity_filtered_unread_count': (
            0 if quiet else filtered_notifications.filter(actionable_unread_q(now)).count()
        ) + (0 if quiet else filtered_messages.filter(is_read=False).count()),
        'notification_quiet_hours_active': quiet,
        'activity_notification_count': all_notification_queryset.count(),
        'activity_message_count': all_message_queryset.count(),
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
        durations = {
            '2h': (timedelta(hours=2), '2 小时'),
            '24h': (timedelta(hours=24), '24 小时'),
            '3d': (timedelta(days=3), '3 天'),
        }
        duration, label = durations.get(duration_key, durations['24h'])
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
