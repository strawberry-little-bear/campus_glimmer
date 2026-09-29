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

from .analytics import build_operations_dashboard, build_search_insights
from .availability import notify_item_available
from .demand_matching import _match_demand, notify_demand_matches
from .lost_found_matching import expire_lost_found_posts, find_lost_found_matches, score_lost_found_posts
from .forms import DeliveryCodeForm, DemandPostForm, DisputeEvidenceForm, DisputeForm, DisputeResolutionForm, DemandResponseForm, ItemForm, ItemImageFormSet, LostFoundLeadForm, LostFoundPostForm, MeetingAppointmentForm, MeetingIncidentForm, NotificationPreferenceForm, OrderForm, RatingForm, ReportForm, ReportReviewForm, SavedSearchForm
from .models import BrowsingHistory, CampusCampaign, CampusLocation, Category, DemandPost, DeliveryConfirmation, Favorite, GiftApplication, Item, ItemAvailabilityWatch, LostFoundLead, LostFoundPost, MeetingAppointment, MeetingIncident, Notification, NotificationPreference, DemandResponse, Order, OrderDispute, OrderDisputeEvidence, OrderEvent, Rating, RecommendationFeedback, Report, SavedSearch, SearchClick, SearchImpression, SearchQuery, SearchSynonym
from .recommendations import get_recommendations
from .reputation import build_seller_reputation
from .notifications import create_notification
from .price_insights import build_price_insight
from .meeting_scheduling import (
    find_appointment_conflicts,
    recommend_meeting_locations,
    recommend_meeting_times,
)
from .order_workflow import OrderTransitionError, transition_order
from .saved_searches import notify_saved_search_matches
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
def my_orders(request):
    orders = Order.objects.filter(Q(buyer=request.user) | Q(seller=request.user)).select_related('item', 'buyer', 'seller', 'meeting_location')
    return render(request, 'listings/my_orders.html', {'orders': orders, 'title': '我的交易'})


@login_required
def favorite_list(request):
    favorites = Favorite.objects.filter(user=request.user).select_related('item__category', 'item__seller', 'item__location').prefetch_related('item__images')
    context = {
        'favorites': favorites,
        'favorite_ids': set(favorites.values_list('item_id', flat=True)),
        'title': '我的心愿单',
    }
    return render(request, 'listings/favorite_list.html', context)


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
def my_items(request):
    items = Item.objects.filter(seller=request.user).select_related(
        'category', 'location', 'campaign',
    ).prefetch_related('images')
    now = timezone.now()
    campaigns = CampusCampaign.objects.filter(is_active=True).filter(
        Q(ends_at__isnull=True) | Q(ends_at__gt=now),
    ).order_by('-starts_at', 'title')
    return render(request, 'listings/my_items.html', {
        'items': items,
        'campaigns': campaigns,
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


@login_required
def operations_dashboard(request):
    if not request.user.is_staff:
        raise PermissionDenied
    dashboard = build_operations_dashboard(_operations_period_days(request))
    dashboard['pending_dispute_count'] = OrderDispute.objects.filter(
        status__in={'open', 'reviewing'},
    ).count()
    dashboard['pending_moderation_count'] = ModerationEvent.objects.filter(status='pending').count()
    dashboard['pending_high_moderation_count'] = ModerationEvent.objects.filter(
        status='pending', risk_level='high',
    ).count()
    return render(request, 'listings/operations_dashboard.html', dashboard)


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

    writer.writerow([])
    writer.writerow(['分类供给', '周期内新增', '当前在售'])
    for row in dashboard['category_stats']:
        writer.writerow([row.name, row.new_count, row.available_count])

    writer.writerow([])
    writer.writerow(['地点供给与交易', '周期内新增商品', '周期内交易预约'])
    for row in dashboard['location_stats']:
        writer.writerow([row.name, row.new_count, row.order_count])
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


@login_required
def saved_search_list(request):
    saved_searches = SavedSearch.objects.filter(user=request.user).select_related('category', 'location')
    return render(request, 'listings/saved_searches.html', {
        'saved_searches': saved_searches,
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
def delete_saved_search(request, saved_search_id):
    saved_search = get_object_or_404(SavedSearch, id=saved_search_id, user=request.user)
    if request.method == 'POST':
        name = saved_search.name
        saved_search.delete()
        messages.success(request, f'已删除关注搜索“{name}”。')
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
    if is_demand_owner:
        demand_responses = demand.responses.select_related(
            'item', 'item__category', 'item__location', 'responder',
        ).all()
    elif request.user.is_authenticated:
        responded_item_ids = set(demand.responses.filter(
            responder=request.user,
        ).values_list('item_id', flat=True))
    return render(request, 'listings/demand_detail.html', {
        'demand': demand,
        'recommended_items': _demand_match_items(demand) if demand.status == 'active' else [],
        'demand_responses': demand_responses,
        'is_demand_owner': is_demand_owner,
        'responded_item_ids': responded_item_ids,
        'demand_response_form': DemandResponseForm(),
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
    if is_owner:
        leads = post.leads.select_related('respondent', 'related_post').all()
    elif request.user.is_authenticated:
        my_lead = post.leads.filter(respondent=request.user).select_related('related_post').first()
    lead_form = LostFoundLeadForm(post=post) if request.user.is_authenticated and not is_owner and post.status == 'active' else None
    return render(request, 'listings/lost_found_detail.html', {
        'post': post,
        'is_owner': is_owner,
        'leads': leads,
        'my_lead': my_lead,
        'lead_form': lead_form,
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
def unread_summary(request):
    unread_notifications = Notification.objects.filter(
        recipient=request.user, is_read=False,
    )
    unread_messages = PrivateMessage.objects.filter(
        receiver=request.user, is_read=False,
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
    if status_filter not in {'all', 'unread', 'read'}:
        status_filter = 'all'
    kind_filter = request.GET.get('kind', '').strip()
    allowed_kinds = {value for value, _ in Notification.KIND_CHOICES}
    if kind_filter not in allowed_kinds:
        kind_filter = ''
    search_query = request.GET.get('q', '').strip()[:120]

    all_notifications = Notification.objects.filter(recipient=request.user)
    notifications = all_notifications
    if status_filter == 'unread':
        notifications = notifications.filter(is_read=False)
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
    unread_kind_counts = dict(
        all_notifications.filter(is_read=False)
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
        ('unread', '仅看未读'),
        ('read', '仅看已读'),
    )
    notification_total_all = all_notifications.count()
    notification_unread_total = all_notifications.filter(is_read=False).count()
    filter_params = request.GET.copy()
    filter_params.pop('page', None)
    return render(request, 'listings/notifications.html', {
        'notifications': page,
        'notification_page': page,
        'notification_total': paginator.count,
        'notification_total_all': notification_total_all,
        'notification_unread_total': notification_unread_total,
        'notification_filtered_unread_count': notifications.filter(is_read=False).count(),
        'notification_kind_options': notification_kind_options,
        'notification_status_options': notification_status_options,
        'notification_status_filter': status_filter,
        'notification_kind_filter': kind_filter,
        'notification_search_query': search_query,
        'notification_filter_query': filter_params.urlencode(),
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

    notification_queryset = Notification.objects.filter(recipient=request.user)
    message_queryset = PrivateMessage.objects.filter(receiver=request.user)
    if status_filter == 'unread':
        notification_queryset = notification_queryset.filter(is_read=False)
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
        'activity_unread_total': all_notification_queryset.filter(is_read=False).count() + all_message_queryset.filter(is_read=False).count(),
        'activity_filtered_unread_count': filtered_notifications.filter(is_read=False).count() + filtered_messages.filter(is_read=False).count(),
        'activity_notification_count': all_notification_queryset.count(),
        'activity_message_count': all_message_queryset.count(),
    })


@login_required
def mark_all_activity_read(request):
    if request.method == 'POST':
        with transaction.atomic():
            notification_count = Notification.objects.filter(
                recipient=request.user, is_read=False,
            ).update(is_read=True)
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
def mark_notification_read(request, notification_id):
    notification = get_object_or_404(
        Notification,
        id=notification_id,
        recipient=request.user,
    )
    if request.method == 'POST' and not notification.is_read:
        notification.is_read = True
        notification.save(update_fields=['is_read'])

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
        ).update(is_read=True)
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
        ).update(is_read=True)
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
