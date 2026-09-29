"""Public, privacy-safe seller reputation summaries."""

from django.db.models import Avg, Count, Q

from .models import Item, MeetingIncident, MutualAidFeedback, Order, Rating


def build_seller_reputation(seller):
    """Build a small explainable reputation card without exposing private data."""
    order_stats = Order.objects.filter(seller=seller).aggregate(
        completed=Count('id', filter=Q(status__in={'completed', 'returned'})),
        cancelled=Count('id', filter=Q(status='cancelled')),
    )
    completed = order_stats['completed'] or 0
    cancelled = order_stats['cancelled'] or 0
    closed_orders = completed + cancelled
    completion_rate = round(completed * 100 / closed_orders, 1) if closed_orders else None

    rating_stats = Rating.objects.filter(ratee=seller).aggregate(
        average=Avg('score'), count=Count('id'),
    )
    rating_count = rating_stats['count'] or 0
    average = round(float(rating_stats['average']), 1) if rating_stats['average'] is not None else None
    active_listings = Item.objects.available().filter(seller=seller).count()

    badges = []
    if completed:
        badges.append('有完成交易记录')
    if closed_orders >= 3 and completion_rate >= 90:
        badges.append('交易履约稳定')
    if rating_count >= 3 and average >= 4.5:
        badges.append('评价表现良好')

    if badges:
        label = '交易记录良好'
    elif closed_orders:
        label = '有交易经验'
    else:
        label = '新卖家'

    return {
        'label': label,
        'badges': badges,
        'completed_orders': completed,
        'closed_orders': closed_orders,
        'completion_rate': completion_rate,
        'rating_average': average,
        'rating_count': rating_count,
        'active_listings': active_listings,
    }


def build_user_reputation(user):
    """Build a public, explainable reputation card for either side of a trade."""
    seller_stats = Order.objects.filter(seller=user).aggregate(
        completed=Count('id', filter=Q(status__in={'completed', 'returned'})),
        cancelled=Count('id', filter=Q(status='cancelled')),
    )
    buyer_stats = Order.objects.filter(buyer=user).aggregate(
        completed=Count('id', filter=Q(status__in={'completed', 'returned'})),
        cancelled=Count('id', filter=Q(status='cancelled')),
    )
    seller_completed = seller_stats['completed'] or 0
    buyer_completed = buyer_stats['completed'] or 0
    seller_cancelled = seller_stats['cancelled'] or 0
    buyer_cancelled = buyer_stats['cancelled'] or 0
    completed_orders = seller_completed + buyer_completed
    closed_orders = completed_orders + seller_cancelled + buyer_cancelled
    completion_rate = round(completed_orders * 100 / closed_orders, 1) if closed_orders else None

    rating_stats = Rating.objects.filter(ratee=user).aggregate(
        average=Avg('score'), count=Count('id'),
    )
    rating_count = rating_stats['count'] or 0
    rating_average = (
        round(float(rating_stats['average']), 1)
        if rating_stats['average'] is not None else None
    )
    confirmed_incidents = MeetingIncident.objects.filter(
        accused=user, status='resolved',
    ).count()
    active_listings = Item.objects.available().filter(seller=user).count()

    mutual_aid_feedbacks = MutualAidFeedback.objects.filter(
        Q(demand_response__responder=user) | Q(lost_found_lead__respondent=user),
    ).values('outcome', 'tags')
    mutual_aid_feedback_count = 0
    mutual_aid_completed_count = 0
    positive_tag_count = 0
    for feedback in mutual_aid_feedbacks:
        mutual_aid_feedback_count += 1
        if feedback['outcome'] == 'completed':
            mutual_aid_completed_count += 1
        positive_tag_count += len(set(feedback['tags'] or []) & {'on_time', 'smooth', 'helpful'})
    mutual_aid_completion_rate = (
        round(mutual_aid_completed_count * 100 / mutual_aid_feedback_count, 1)
        if mutual_aid_feedback_count else None
    )

    badges = []
    if completed_orders:
        badges.append('有完成交易记录')
    if closed_orders >= 3 and completion_rate >= 90:
        badges.append('交易履约稳定')
    if rating_count >= 3 and rating_average >= 4.5:
        badges.append('评价表现良好')
    if closed_orders >= 3 and confirmed_incidents == 0:
        badges.append('暂无已确认交付异常')
    if mutual_aid_completed_count:
        badges.append('有校园互助记录')
    if mutual_aid_feedback_count >= 3 and mutual_aid_completion_rate >= 80:
        badges.append('互助反馈稳定')

    if completed_orders >= 3 and completion_rate is not None and completion_rate >= 90:
        label = '可信交易伙伴'
    elif completed_orders:
        label = '有交易经验'
    else:
        label = '新加入校园社区'

    return {
        'label': label,
        'badges': badges,
        'completed_orders': completed_orders,
        'seller_completed_orders': seller_completed,
        'buyer_completed_orders': buyer_completed,
        'closed_orders': closed_orders,
        'completion_rate': completion_rate,
        'rating_average': rating_average,
        'rating_count': rating_count,
        'confirmed_incidents': confirmed_incidents,
        'active_listings': active_listings,
        'mutual_aid_feedback_count': mutual_aid_feedback_count,
        'mutual_aid_completed_count': mutual_aid_completed_count,
        'mutual_aid_completion_rate': mutual_aid_completion_rate,
        'mutual_aid_positive_tag_count': positive_tag_count,
    }
