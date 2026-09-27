from decimal import Decimal, InvalidOperation
from urllib.parse import urlencode

from django.contrib.auth.decorators import login_required
from django.contrib import messages
from django.core.paginator import Paginator
from django.core.exceptions import PermissionDenied
from django.db import IntegrityError, transaction
from django.db.models import Avg, Case, Count, F, IntegerField, Q, Value, When
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.utils.http import url_has_allowed_host_and_scheme

from .analytics import build_operations_dashboard
from .forms import DisputeForm, DisputeResolutionForm, ItemForm, ItemImageFormSet, OrderForm, RatingForm, ReportForm, SavedSearchForm
from .models import BrowsingHistory, CampusLocation, Category, DeliveryConfirmation, Favorite, Item, Notification, Order, OrderDispute, OrderEvent, Rating, Report, SavedSearch, SearchQuery
from .recommendations import get_recommendations
from .notifications import create_notification
from .saved_searches import notify_saved_search_matches


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
        target_status = request.POST.get('status')
        transitions = {
            'pending': {'confirmed', 'cancelled'},
            'confirmed': {'meeting', 'cancelled'},
            'meeting': {'cancelled'},
            'completed': set(),
            'cancelled': set(),
        }
        seller_can_update = request.user == order.seller and target_status in transitions.get(order.status, set())
        buyer_can_update = request.user == order.buyer and target_status == 'cancelled' and order.status in {'pending', 'confirmed', 'meeting'}
        if seller_can_update or buyer_can_update:
            previous_status = order.status
            order.status = target_status
            order.save(update_fields=['status', 'updated_at'])
            event_notes = {
                'confirmed': '卖家确认了交易预约',
                'meeting': '卖家将订单推进到当面交付',
                'completed': '双方确认交易已完成',
                'cancelled': '订单被取消，商品恢复为在售',
            }
            OrderEvent.objects.create(
                order=order,
                actor=request.user,
                from_status=previous_status,
                to_status=target_status,
                note=event_notes.get(target_status, '订单状态已更新'),
            )
            other_party = order.buyer if request.user == order.seller else order.seller
            create_notification(
                other_party, actor=request.user, kind='order_status',
                title='订单状态有更新',
                message=f'商品“{order.item.title}”的订单已更新为“{order.get_status_display()}”。',
                order=order, item=order.item,
                target_url=reverse('order_detail', args=[order.id]),
            )
            if target_status == 'cancelled':
                order.item.status = 'available'
                order.item.save(update_fields=['status', 'updated_at'])
            elif target_status == 'completed':
                order.item.status = 'sold'
                order.item.save(update_fields=['status', 'updated_at'])
            messages.success(request, f'订单状态已更新为“{order.get_status_display()}”。')
        else:
            messages.error(request, '当前订单状态不允许执行这个操作。')
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
            item.status = status
            item.save(update_fields=['status', 'updated_at'])
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


@login_required
def operations_dashboard(request):
    if not request.user.is_staff:
        raise PermissionDenied
    try:
        period_days = int(request.GET.get('days', 30))
    except (TypeError, ValueError):
        period_days = 30
    dashboard = build_operations_dashboard(period_days)
    dashboard['pending_dispute_count'] = OrderDispute.objects.filter(
        status__in={'open', 'reviewing'},
    ).count()
    return render(request, 'listings/operations_dashboard.html', dashboard)

def search_items(request):
    query = request.GET.get('q', '').strip()
    return redirect(f'/listings/?{urlencode({"q": query})}') if query else redirect('item_list')


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
def notification_list(request):
    notifications = Notification.objects.filter(
        recipient=request.user,
    ).select_related('actor', 'item', 'order__item')
    paginator = Paginator(notifications, 20)
    page = paginator.get_page(request.GET.get('page'))
    return render(request, 'listings/notifications.html', {
        'notifications': page,
        'notification_page': page,
        'notification_total': paginator.count,
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
