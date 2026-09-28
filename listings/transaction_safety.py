from dataclasses import dataclass
from decimal import Decimal

from django.db.models import Avg, Count, Q
from django.utils import timezone

from accounts.models import CampusVerification

from .models import Item, MeetingIncident, Order


@dataclass(frozen=True)
class SafetySignal:
    level: str
    title: str
    detail: str
    action: str = ''


@dataclass(frozen=True)
class TransactionSafetySummary:
    signals: tuple[SafetySignal, ...]
    warning_count: int
    positive_count: int

    @property
    def tone(self):
        if self.warning_count:
            return 'warning'
        if self.positive_count >= 2:
            return 'safe'
        return 'neutral'

    @property
    def label(self):
        if self.warning_count >= 2:
            return '交易前建议先核对安全信息'
        if self.warning_count:
            return '交易前还有一项需要注意'
        if self.positive_count >= 2:
            return '当前交易保障较完整'
        return '建议完成交付前核对'


def _participant_stats(user_id):
    stats = Order.objects.filter(Q(seller_id=user_id) | Q(buyer_id=user_id)).aggregate(
        completed=Count('id', filter=Q(status__in={'completed', 'returned'})),
        cancelled=Count('id', filter=Q(status='cancelled')),
    )
    completed = stats['completed'] or 0
    cancelled = stats['cancelled'] or 0
    closed = completed + cancelled
    rate = (completed * 100 / closed) if closed else None
    return completed, cancelled, closed, rate


def _append_signal(signals, level, title, detail, action=''):
    signals.append(SafetySignal(level=level, title=title, detail=detail, action=action))


def build_transaction_safety(order):
    """Build an explainable, non-blocking safety checklist for an order.

    The checklist only uses information already visible to the platform. It
    never blocks a transaction or exposes private contact information; it gives
    both parties concrete steps they can take before meeting.
    """
    signals = []
    item = order.item
    appointment = getattr(order, 'appointment', None)
    meeting_location = (
        appointment.location if appointment and appointment.location_id
        else order.meeting_location or item.location
    )

    if not item.images.exists():
        _append_signal(
            signals,
            'warning',
            '商品暂未上传图片',
            '图片有助于双方在交付前确认外观和成色，建议先通过站内沟通补充细节。',
            '补充商品图片或在交付前再次核对成色',
        )

    item_price = Decimal(str(item.price or 0))
    if item.trade_mode == 'sale' and item_price > 0:
        average_price = Item.objects.filter(
            category_id=item.category_id,
            trade_mode='sale',
            price__gt=0,
            status__in={'available', 'reserved', 'sold'},
        ).exclude(pk=item.pk).aggregate(average=Avg('price'))['average']
        average_price = Decimal(str(average_price or 0))
        if average_price > 0 and item_price < average_price * Decimal('0.45'):
            _append_signal(
                signals,
                'warning',
                '商品价格明显低于同类近期均值',
                f'当前价格约为同类均价的 {float(item_price / average_price * 100):.0f}%，低价本身不代表异常，请在交付前确认商品状态。',
                '不要提前支付平台外费用，并使用当面交付确认码',
            )

    seller_completed, seller_cancelled, seller_closed, seller_rate = _participant_stats(order.seller_id)
    if seller_closed == 0:
        _append_signal(
            signals,
            'info',
            '发布者暂无历史完成交易',
            '这是发布者在平台上的第一笔可见交易记录，建议优先选择公共区域完成交付。',
            '选择公共交付地点并保留订单沟通记录',
        )
    elif seller_rate is not None and seller_closed >= 3 and seller_rate < 75:
        _append_signal(
            signals,
            'warning',
            '发布者历史履约率偏低',
            f'历史关闭订单 {seller_closed} 笔，完成率约 {seller_rate:.0f}%。这不是平台裁定，但建议先确认交付细节。',
            '确认时间、地点和商品状态后再前往交付',
        )
    elif seller_completed:
        _append_signal(
            signals,
            'positive',
            '发布者有可参考的完成交易记录',
            f'已完成或归还 {seller_completed} 笔交易。',
        )

    verified_user_ids = set(
        CampusVerification.objects.filter(
            user_id__in={order.seller_id, order.buyer_id},
            status='verified',
            verified_at__isnull=False,
        ).values_list('user_id', flat=True)
    )
    if order.seller_id in verified_user_ids:
        _append_signal(
            signals,
            'positive',
            '发布者已完成校园身份认证',
            '认证状态只代表校园邮箱验证通过，不代表平台为交易结果担保。',
        )

    if not meeting_location:
        _append_signal(
            signals,
            'warning',
            '尚未确定交付地点',
            '订单还没有可追踪的校园交付地点，建议先完成地点和时间协商。',
            '优先选择已配置的校园公共地点',
        )
    elif not meeting_location.is_public:
        _append_signal(
            signals,
            'warning',
            '当前地点未标记为公共交付区域',
            '平台没有将该地点标记为人流较多的公共区域，建议重新选择更安全的地点。',
            '改约到图书馆大厅、门卫室等公共区域',
        )
    else:
        _append_signal(
            signals,
            'positive',
            '交付地点属于公共区域',
            meeting_location.safety_note or '建议在光线充足、人流较多的位置完成交付。',
        )

    if appointment is None:
        if order.status in {'confirmed', 'meeting'}:
            _append_signal(
                signals,
                'info',
                '尚未形成双方确认的预约',
                '订单状态已进入交付准备阶段，但时间和地点仍需要双方在订单内确认。',
                '使用订单内的预约表单提交可追踪安排',
            )
    elif appointment.status == 'confirmed':
        local_start = timezone.localtime(appointment.start_at)
        local_end = timezone.localtime(appointment.end_at)
        if local_start.hour < 8 or local_end.hour >= 22:
            _append_signal(
                signals,
                'warning',
                '预约时间处于非典型校园活动时段',
                '夜间或过早时段可能受到门禁、照明和人流影响。',
                '尽量改约到白天或工作日课后时段',
            )
        else:
            _append_signal(
                signals,
                'positive',
                '双方已确认交付时间',
                f'预约时间为 {timezone.localtime(appointment.start_at):%m月%d日 %H:%M}。',
            )
    elif appointment.status == 'pending':
        _append_signal(
            signals,
            'info',
            '交付安排仍等待对方确认',
            '在对方接受前不要把这次时间视为最终约定。',
            '等待对方确认或重新协商',
        )

    if order.status == 'meeting':
        confirmation = getattr(order, 'delivery_confirmation', None)
        if confirmation and confirmation.handoff_code_hash:
            _append_signal(
                signals,
                'positive',
                '已启用交付确认码',
                '双方可以在现场核对一次性确认码，减少误确认和冒领风险。',
            )
        else:
            _append_signal(
                signals,
                'info',
                '建议启用交付确认码',
                '交付前生成确认码，并只在现场当面核对，不要提前公开发送。',
                '进入订单详情生成确认码',
            )

    incident_count = MeetingIncident.objects.filter(
        accused_id__in={order.buyer_id, order.seller_id},
        status='resolved',
    ).count()
    if incident_count:
        _append_signal(
            signals,
            'warning',
            '参与方存在已确认的交付异常记录',
            f'平台记录到 {incident_count} 条已确认交付异常，请在交付前充分沟通并保留订单记录。',
            '选择公共地点并使用交付确认码',
        )

    warnings = tuple(signal for signal in signals if signal.level == 'warning')
    positives = tuple(signal for signal in signals if signal.level == 'positive')
    signals.sort(key=lambda signal: {'warning': 0, 'info': 1, 'positive': 2}[signal.level])
    return TransactionSafetySummary(
        signals=tuple(signals),
        warning_count=len(warnings),
        positive_count=len(positives),
    )
