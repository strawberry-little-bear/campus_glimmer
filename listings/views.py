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
from .forms import ItemForm, ItemImageFormSet, OrderForm, RatingForm, ReportForm
from .models import BrowsingHistory, CampusLocation, Category, Favorite, Item, Notification, Order, OrderEvent, Rating, Report, SearchQuery
from .recommendations import get_recommendations
from .notifications import create_notification


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
    order = get_object_or_404(Order.objects.select_related('item', 'buyer', 'seller', 'meeting_location'), id=order_id)
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
            'meeting': {'completed', 'cancelled'},
            'completed': set(),
            'cancelled': set(),
        }
        seller_can_update = request.user == order.seller and target_status in transitions.get(order.status, set())
        buyer_can_update = request.user == order.buyer and target_status in {'completed', 'cancelled'}
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
    return render(
        request,
        'listings/operations_dashboard.html',
        build_operations_dashboard(period_days),
    )

def search_items(request):
    query = request.GET.get('q', '').strip()
    return redirect(f'/listings/?{urlencode({"q": query})}') if query else redirect('item_list')


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
