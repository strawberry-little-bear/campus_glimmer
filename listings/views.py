import csv
from decimal import Decimal, InvalidOperation
from datetime import timedelta
from urllib.parse import urlencode

from django.contrib.auth.decorators import login_required
from django.contrib import messages
from django.core.paginator import Paginator
from django.core.exceptions import PermissionDenied
from django.http import HttpResponse, JsonResponse
from django.db import IntegrityError, transaction
from django.db.models import Avg, Case, Count, F, IntegerField, Q, Value, When
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.utils.http import url_has_allowed_host_and_scheme

from .analytics import build_operations_dashboard, build_search_insights
from .availability import notify_item_available
from .forms import DisputeForm, DisputeResolutionForm, ItemForm, ItemImageFormSet, NotificationPreferenceForm, OrderForm, RatingForm, ReportForm, ReportReviewForm, SavedSearchForm
from .models import BrowsingHistory, CampusLocation, Category, DeliveryConfirmation, Favorite, Item, ItemAvailabilityWatch, Notification, NotificationPreference, Order, OrderDispute, OrderEvent, Rating, RecommendationFeedback, Report, SavedSearch, SearchQuery
from .recommendations import get_recommendations
from .notifications import create_notification
from .order_workflow import OrderTransitionError, transition_order
from .saved_searches import notify_saved_search_matches
from chat_messages.models import ModerationEvent, PrivateMessage


def _favorite_ids(request):
    if not request.user.is_authenticated:
        return set()
    return set(Favorite.objects.filter(user=request.user).values_list('item_id', flat=True))


def home(request):
    categories = Category.objects.annotate(available_count=Count('items', filter=Q(items__status='available')))
    locations = CampusLocation.objects.filter(is_active=True).annotate(available_count=Count('items', filter=Q(items__status='available')))
    recent_items = Item.objects.filter(status='available').select_related('category', 'seller', 'location').prefetch_related('images')[:8]
    context = {
        'categories': categories,
        'locations': locations,
        'recent_items': recent_items,
        'recommendations': get_recommendations(request.user, limit=8),
        'favorite_ids': _favorite_ids(request),
        'stats': {
            'items': Item.objects.filter(status='available').count(),
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


def item_list(request, category_id=None):
    categories = Category.objects.annotate(available_count=Count('items', filter=Q(items__status='available')))
    locations = CampusLocation.objects.filter(is_active=True).annotate(available_count=Count('items', filter=Q(items__status='available')))
    category = get_object_or_404(Category, id=category_id) if category_id else None
    location_id = request.GET.get('location', '').strip()
    location = get_object_or_404(CampusLocation, id=location_id, is_active=True) if location_id.isdigit() else None
    query = request.GET.get('q', '').strip()
    condition = request.GET.get('condition', '').strip()
    raw_min_price = request.GET.get('min_price', '').strip()
    raw_max_price = request.GET.get('max_price', '').strip()
    min_price = _parse_price(raw_min_price)
    max_price = _parse_price(raw_max_price)

    items = Item.objects.filter(status='available').select_related('category', 'seller', 'location').prefetch_related('images')
    if category:
        items = items.filter(category=category)
    if location:
        items = items.filter(location=location)
    if query:
        items = items.filter(
            Q(title__icontains=query)
            | Q(description__icontains=query)
            | Q(condition__icontains=query)
            | Q(category__name__icontains=query)
            | Q(location__name__icontains=query)
            | Q(location__building__icontains=query)
        )
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
        items = items.annotate(
            search_rank=Case(
                When(title__iexact=query, then=Value(3)),
                When(title__istartswith=query, then=Value(2)),
                When(title__icontains=query, then=Value(1)),
                default=Value(0),
                output_field=IntegerField(),
            ),
        ).order_by('-search_rank', '-created_at')
    else:
        sort_map = {'latest': '-created_at', 'price_asc': 'price', 'price_desc': '-price'}
        items = items.order_by(sort_map[sort])

    result_count = items.count()
    if query and not request.GET.get('page'):
        SearchQuery.objects.create(
            user=request.user if request.user.is_authenticated else None,
            query=query[:120],
            condition=condition[:120],
            category=category,
            location=location,
            min_price=min_price,
            max_price=max_price,
            result_count=result_count,
        )

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
    context = {
        'categories': categories,
        'locations': locations,
        'category': category,
        'location': location,
        'items': page_obj,
        'page_obj': page_obj,
        'query': query,
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
    }
    return render(request, 'listings/item_list.html', context)


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
        available_count=Count('items', filter=Q(items__status='available')),
    ).order_by('-available_count', 'name')[:3]
    for category in categories:
        add_suggestion(category.name, 'category', '分类', f"{category.available_count} 件在售")

    locations = CampusLocation.objects.filter(
        Q(name__icontains=query)
        | Q(building__icontains=query)
        | Q(address__icontains=query),
        is_active=True,
    ).annotate(
        available_count=Count('items', filter=Q(items__status='available')),
    ).order_by('-available_count', 'sort_order', 'name')[:3]
    for location in locations:
        add_suggestion(location.name, 'location', '交易地点', f"{location.available_count} 件在售")

    return JsonResponse({'suggestions': suggestions[:8]})

def item_detail(request, item_id):
    item = get_object_or_404(
        Item.objects.select_related('category', 'seller', 'location').prefetch_related('images', 'comments__author__profile'),
        id=item_id,
    )
    if request.user.is_authenticated and request.user != item.seller:
        history, created = BrowsingHistory.objects.get_or_create(user=request.user, item=item)
        if not created:
            BrowsingHistory.objects.filter(pk=history.pk).update(
                view_count=F('view_count') + 1,
                last_viewed_at=timezone.now(),
            )
    related_items = Item.objects.filter(category=item.category, status='available').exclude(id=item.id).select_related('seller', 'location').prefetch_related('images')[:4]
    seller_ratings = Rating.objects.filter(ratee=item.seller).select_related('rater', 'order')[:5]
    rating_summary = Rating.objects.filter(ratee=item.seller).aggregate(average=Avg('score'), count=Count('id'))
    context = {
        'item': item,
        'related_items': related_items,
        'rating_summary': rating_summary,
        'seller_ratings': seller_ratings,
        'is_favorite': item_id in _favorite_ids(request),
        'availability_watch': (
            ItemAvailabilityWatch.objects.filter(user=request.user, item=item).first()
            if request.user.is_authenticated and request.user != item.seller else None
        ),
        'has_reported': request.user.is_authenticated and Report.objects.filter(item=item, reporter=request.user).exists(),
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
    item = get_object_or_404(Item, id=item_id, status='available')
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
    item = get_object_or_404(Item.objects.select_related('seller', 'location'), id=item_id)
    if item.seller == request.user:
        messages.error(request, '不能预约自己发布的商品。')
        return redirect('item_detail', item_id=item.id)
    if hasattr(item, 'order'):
        messages.info(request, '这个商品已经有一笔交易预约。')
        return redirect('order_detail', order_id=item.order.id)
    if request.method == 'POST':
        form = OrderForm(request.POST)
        if form.is_valid():
            try:
                with transaction.atomic():
                    locked_item = Item.objects.select_for_update().select_related('seller').get(id=item.id)
                    if locked_item.status != 'available' or hasattr(locked_item, 'order'):
                        messages.info(request, '这个商品刚刚被其他同学预约了。')
                        return redirect('item_detail', item_id=item.id)
                    order = form.save(commit=False)
                    order.item = locked_item
                    order.buyer = request.user
                    order.seller = locked_item.seller
                    order.agreed_price = locked_item.price
                    order.meeting_location = order.meeting_location or locked_item.location
                    order.confirmation_deadline = timezone.now() + timedelta(hours=24)
                    order.save()
                    OrderEvent.objects.create(
                        order=order, actor=request.user, to_status=order.status,
                        note='买家发起交易预约',
                    )
                    create_notification(
                        order.seller, actor=request.user, kind='order_created',
                        title='收到新的交易预约',
                        message=f'{request.user.username}预约了你的商品“{order.item.title}”。',
                        order=order, item=order.item,
                        target_url=reverse('order_detail', args=[order.id]),
                    )
                    locked_item.status = 'reserved'
                    locked_item.save(update_fields=['status', 'updated_at'])
            except IntegrityError:
                messages.info(request, '这个商品刚刚被其他同学预约了。')
                return redirect('item_detail', item_id=item.id)
            messages.success(request, '预约已提交，等待卖家确认。')
            return redirect('order_detail', order_id=order.id)
    else:
        form = OrderForm(initial={'meeting_location': item.location_id})
    return render(request, 'listings/order_form.html', {'form': form, 'item': item, 'title': '预约交易'})


@login_required
def order_detail(request, order_id):
    order = get_object_or_404(
        Order.objects.select_related(
            'item', 'buyer', 'seller', 'meeting_location', 'delivery_confirmation', 'dispute',
        ),
        id=order_id,
    )
    if request.user not in {order.buyer, order.seller}:
        messages.error(request, '你没有权限查看这笔订单。')
        return redirect('home')
    rating_target = order.seller if request.user == order.buyer else order.buyer
    my_rating = Rating.objects.filter(order=order, rater=request.user).first()
    rating_form = RatingForm() if order.status == 'completed' and not my_rating else None
    ratings = order.ratings.select_related('rater', 'ratee').all()
    events = order.events.select_related('actor').all()
    return render(request, 'listings/order_detail.html', {
        'order': order,
        'title': '交易订单',
        'rating_target': rating_target,
        'my_rating': my_rating,
        'rating_form': rating_form,
        'ratings': ratings,
        'events': events,
        'confirmation': getattr(order, 'delivery_confirmation', None),
        'dispute': getattr(order, 'dispute', None),
    })


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
        setattr(confirmation, field_name, timezone.now())
        confirmation.save(update_fields=[field_name, 'updated_at'])
        other_party = locked_order.seller if request.user == locked_order.buyer else locked_order.buyer

        if confirmation.is_complete:
            locked_order.status = 'completed'
            locked_order.save(update_fields=['status', 'updated_at'])
            locked_order.item.status = 'sold'
            locked_order.item.save(update_fields=['status', 'updated_at'])
            OrderEvent.objects.create(
                order=locked_order,
                actor=request.user,
                from_status='meeting',
                to_status='completed',
                note='双方确认交易已完成',
            )
            create_notification(
                other_party, actor=request.user, kind='order_status',
                title='交易已完成',
                message=f'商品“{locked_order.item.title}”已完成双方交付确认。',
                order=locked_order, item=locked_order.item,
                target_url=reverse('order_detail', args=[locked_order.id]),
            )
            messages.success(request, '双方已完成交付确认，交易正式完成。')
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
def open_dispute(request, order_id):
    order = get_object_or_404(Order.objects.select_related('item', 'buyer', 'seller'), id=order_id)
    if request.user not in {order.buyer, order.seller}:
        messages.error(request, '你没有权限发起这笔订单的争议。')
        return redirect('home')
    if order.status not in {'confirmed', 'meeting', 'completed'}:
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
    if order.status != 'completed':
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
            form.save()
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
    items = Item.objects.filter(seller=request.user).select_related('category', 'location').prefetch_related('images')
    return render(request, 'listings/my_items.html', {'items': items, 'title': '我的商品'})


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
        ('new_users', '新增用户'),
        ('active_users', '周期活跃用户'),
        ('retention_rate', '上一周期新用户回访率（%）'),
        ('new_reports', '新增举报'),
        ('pending_reports', '待处理举报'),
        ('notification_events', '通知触达事件数'),
        ('notification_rows', '通知记录数'),
        ('unread_notifications', '周期结束未读通知'),
        ('notification_compression_rate', '通知聚合压缩率（%）'),
    )
    for key, label in metric_labels:
        value = metrics[key]
        writer.writerow([label, '' if value is None else value])
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

    writer.writerow([])
    writer.writerow(['搜索词分析', '搜索次数', '无结果次数', '无结果占比（%）', '平均结果数', '独立用户数', '最近搜索时间'])
    for row in dashboard['term_rows']:
        writer.writerow([
            row['query'], row['search_count'], row['zero_result_count'], row['zero_result_rate'],
            row['average_results'], row['unique_users'], row['last_searched'],
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


@login_required
def unread_summary(request):
    return JsonResponse({
        'notifications': Notification.objects.filter(
            recipient=request.user, is_read=False,
        ).count(),
        'messages': PrivateMessage.objects.filter(
            receiver=request.user, is_read=False,
        ).count(),
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
        )

    notifications = notifications.select_related('actor', 'item', 'order__item')
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
