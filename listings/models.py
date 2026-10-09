from datetime import time, timedelta
from pathlib import Path

from django.core.exceptions import ValidationError
from django.db import models
from django.db.models import Q
from django.contrib.auth.models import User
from django.utils import timezone


class Category(models.Model):
    name = models.CharField(max_length=100, unique=True)
    description = models.TextField(blank=True)

    def __str__(self):
        return self.name

    class Meta:
        verbose_name = '商品分类'
        verbose_name_plural = '商品分类'
        ordering = ['name']


class CampusLocation(models.Model):
    name = models.CharField('地点名称', max_length=100, unique=True)
    building = models.CharField('楼栋 / 校区', max_length=100, blank=True)
    address = models.CharField('详细位置', max_length=200, blank=True)
    description = models.CharField('地点说明', max_length=200, blank=True)
    is_public = models.BooleanField(
        '公共交付区域',
        default=True,
        help_text='用于交付推荐与安全提示。建议将图书馆大厅、门卫室等人流较多区域标记为公共区域。',
    )
    safety_note = models.CharField(
        '安全提示',
        max_length=200,
        blank=True,
        help_text='可填写该地点的开放时间、照明或门禁等注意事项。',
    )
    is_active = models.BooleanField('启用', default=True)
    sort_order = models.PositiveIntegerField('排序', default=0)

    def __str__(self):
        return self.name

    class Meta:
        verbose_name = '校园地点'
        verbose_name_plural = '校园地点'
        ordering = ['sort_order', 'name']


class CampusCampaign(models.Model):
    """A time-bounded public collection for seasonal campus activity."""

    title = models.CharField('专题名称', max_length=100)
    slug = models.SlugField('专题标识', max_length=120, unique=True)
    description = models.TextField('专题说明', blank=True)
    starts_at = models.DateTimeField('开始时间', default=timezone.now)
    ends_at = models.DateTimeField('结束时间', null=True, blank=True)
    is_active = models.BooleanField('启用', default=True)
    created_at = models.DateTimeField('创建时间', auto_now_add=True)

    class Meta:
        verbose_name = '校园专题'
        verbose_name_plural = '校园专题'
        ordering = ['-starts_at', 'title']
        indexes = [
            models.Index(fields=['is_active', 'starts_at', 'ends_at']),
        ]

    def __str__(self):
        return self.title

    def is_live(self, now=None):
        now = now or timezone.now()
        return self.is_active and self.starts_at <= now and (
            self.ends_at is None or self.ends_at > now
        )

    def clean(self):
        super().clean()
        if self.ends_at and self.ends_at <= self.starts_at:
            raise ValidationError({'ends_at': '结束时间必须晚于开始时间。'})


class ItemQuerySet(models.QuerySet):
    def available(self, now=None):
        """Return listings that are still visible and can accept a reservation."""
        now = now or timezone.now()
        return self.filter(status='available').filter(
            Q(expires_at__isnull=True) | Q(expires_at__gt=now),
        )


class Item(models.Model):
    STATUS_CHOICES = (
        ('available', '在售'),
        ('reserved', '已预订'),
        ('sold', '已售出'),
        ('expired', '已过期'),
    )
    TRADE_MODE_CHOICES = (
        ('sale', '出售'),
        ('free', '免费赠送'),
        ('borrow', '限期借用'),
    )

    objects = ItemQuerySet.as_manager()

    title = models.CharField('商品标题', max_length=200)
    description = models.TextField('商品描述')
    trade_mode = models.CharField('交易方式', max_length=20, choices=TRADE_MODE_CHOICES, default='sale')
    price = models.DecimalField('价格', max_digits=10, decimal_places=2, default=0)
    deposit_amount = models.DecimalField(
        '借用押金', max_digits=10, decimal_places=2, default=0,
        help_text='仅限期借用使用，归还确认后由双方线下处理押金。',
    )
    borrow_days = models.PositiveSmallIntegerField(
        '默认借用天数', default=7,
        help_text='仅限期借用使用，范围为 1 至 90 天。',
    )
    category = models.ForeignKey(Category, on_delete=models.CASCADE, related_name='items', verbose_name='分类')
    location = models.ForeignKey(
        CampusLocation,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='items',
        verbose_name='交易地点',
    )
    campaign = models.ForeignKey(
        'CampusCampaign',
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='items',
        verbose_name='校园专题',
    )
    condition = models.CharField('成色', max_length=100)
    seller = models.ForeignKey(User, on_delete=models.CASCADE, related_name='listed_items', verbose_name='卖家')
    status = models.CharField('状态', max_length=20, choices=STATUS_CHOICES, default='available')
    expires_at = models.DateTimeField(
        '展示截止时间', null=True, blank=True,
        help_text='可选。到期后商品会自动下架；不填写表示长期展示。',
    )
    created_at = models.DateTimeField('发布时间', auto_now_add=True)
    updated_at = models.DateTimeField('更新时间', auto_now=True)

    @property
    def is_expired(self):
        return bool(self.expires_at and self.expires_at <= timezone.now())

    @property
    def is_available_now(self):
        return self.status == 'available' and not self.is_expired

    def __str__(self):
        return self.title

    class Meta:
        ordering = ['-created_at']
        indexes = [
            models.Index(fields=['status', '-created_at']),
            models.Index(fields=['category', 'status']),
            models.Index(fields=['location', 'status']),
            models.Index(fields=['status', 'expires_at']),
        ]


class ItemImage(models.Model):
    item = models.ForeignKey(Item, on_delete=models.CASCADE, related_name='images')
    image = models.ImageField('图片', upload_to='items/')

    def __str__(self):
        return f'{self.item.title}的图片'


class FavoriteCollection(models.Model):
    """A user-owned group for organizing saved campus items."""

    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name='favorite_collections', verbose_name='用户')
    name = models.CharField('分组名称', max_length=40)
    created_at = models.DateTimeField('创建时间', auto_now_add=True)
    updated_at = models.DateTimeField('更新时间', auto_now=True)

    class Meta:
        verbose_name = '心愿单分组'
        verbose_name_plural = '心愿单分组'
        ordering = ['name', '-created_at']
        constraints = [
            models.UniqueConstraint(fields=['user', 'name'], name='unique_user_favorite_collection_name'),
        ]
        indexes = [
            models.Index(fields=['user', 'name']),
        ]

    def __str__(self):
        return f'{self.user.username} · {self.name}'


class Favorite(models.Model):
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name='favorites')
    item = models.ForeignKey(Item, on_delete=models.CASCADE, related_name='favorites')
    collection = models.ForeignKey(
        FavoriteCollection, on_delete=models.SET_NULL, null=True, blank=True,
        related_name='favorites', verbose_name='所属分组',
    )
    created_at = models.DateTimeField('收藏时间', auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=['user', 'item'], name='unique_user_item_favorite'),
        ]
        ordering = ['-created_at']

    def __str__(self):
        return f'{self.user.username}收藏了{self.item.title}'


class ItemAvailabilityWatch(models.Model):
    """One-shot reminders for a user waiting for an unavailable item."""

    user = models.ForeignKey(
        User, on_delete=models.CASCADE, related_name='item_availability_watches', verbose_name='用户',
    )
    item = models.ForeignKey(
        Item, on_delete=models.CASCADE, related_name='availability_watches', verbose_name='商品',
    )
    created_at = models.DateTimeField('关注时间', auto_now_add=True)

    class Meta:
        verbose_name = '商品有货提醒'
        verbose_name_plural = '商品有货提醒'
        ordering = ['-created_at']
        constraints = [
            models.UniqueConstraint(fields=['user', 'item'], name='unique_user_item_availability_watch'),
        ]
        indexes = [
            models.Index(fields=['item', '-created_at']),
            models.Index(fields=['user', '-created_at']),
        ]

    def __str__(self):
        return f'{self.user.username}等待{self.item.title}有货'


class Report(models.Model):
    REASON_CHOICES = (
        ('scam', '疑似诈骗或虚假信息'),
        ('spam', '重复发布或广告引流'),
        ('inappropriate', '不当内容'),
        ('other', '其他问题'),
    )
    STATUS_CHOICES = (
        ('pending', '待审核'),
        ('reviewing', '审核中'),
        ('resolved', '已处理'),
        ('rejected', '已驳回'),
    )

    item = models.ForeignKey(Item, on_delete=models.CASCADE, related_name='reports', verbose_name='被举报商品')
    reporter = models.ForeignKey(User, on_delete=models.CASCADE, related_name='submitted_reports', verbose_name='举报人')
    reason = models.CharField('举报原因', max_length=30, choices=REASON_CHOICES)
    detail = models.TextField('补充说明', blank=True)
    status = models.CharField('审核状态', max_length=20, choices=STATUS_CHOICES, default='pending')
    reviewer = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True, related_name='reviewed_reports', verbose_name='审核人')
    review_note = models.TextField('审核备注', blank=True)
    reviewed_at = models.DateTimeField('审核时间', null=True, blank=True)
    created_at = models.DateTimeField('举报时间', auto_now_add=True)
    updated_at = models.DateTimeField('更新时间', auto_now=True)

    class Meta:
        verbose_name = '商品举报'
        verbose_name_plural = '商品举报'
        ordering = ['status', '-created_at']
        constraints = [
            models.UniqueConstraint(fields=['item', 'reporter'], name='unique_item_reporter'),
        ]
        indexes = [
            models.Index(fields=['status', '-created_at']),
            models.Index(fields=['item', 'status']),
        ]

    def __str__(self):
        return f'{self.item.title} · {self.get_reason_display()}'


class GiftApplication(models.Model):
    """A lightweight queue entry for a free-gift listing.

    Free items can receive multiple applications without reserving the item
    immediately. The seller selects one applicant to create the actual order.
    """

    STATUS_CHOICES = (
        ('pending', '等待选择'),
        ('selected', '已选中'),
        ('rejected', '未选中'),
        ('withdrawn', '已撤回'),
    )

    item = models.ForeignKey(
        Item, on_delete=models.CASCADE, related_name='gift_applications', verbose_name='商品',
    )
    applicant = models.ForeignKey(
        User, on_delete=models.CASCADE, related_name='gift_applications', verbose_name='申请人',
    )
    meeting_location = models.ForeignKey(
        CampusLocation, on_delete=models.SET_NULL, null=True, blank=True,
        related_name='gift_applications', verbose_name='领取地点',
    )
    applicant_note = models.TextField('申请说明', blank=True)
    status = models.CharField('申请状态', max_length=20, choices=STATUS_CHOICES, default='pending')
    order = models.OneToOneField(
        'Order', on_delete=models.SET_NULL, null=True, blank=True,
        related_name='gift_application', verbose_name='生成订单',
    )
    decided_at = models.DateTimeField('处理时间', null=True, blank=True)
    created_at = models.DateTimeField('申请时间', auto_now_add=True)
    updated_at = models.DateTimeField('更新时间', auto_now=True)

    class Meta:
        verbose_name = '免费领取申请'
        verbose_name_plural = '免费领取申请'
        ordering = ['status', 'created_at']
        constraints = [
            models.UniqueConstraint(
                fields=['item', 'applicant'],
                condition=models.Q(status='pending'),
                name='unique_pending_gift_application',
            ),
        ]
        indexes = [
            models.Index(fields=['item', 'status', 'created_at']),
            models.Index(fields=['applicant', 'status', '-created_at']),
        ]

    def __str__(self):
        return f'{self.item.title} · {self.applicant.username} · {self.get_status_display()}'


class Order(models.Model):
    ACTIVE_STATUS_VALUES = ('pending', 'confirmed', 'meeting', 'borrowed')

    STATUS_CHOICES = (
        ('pending', '待卖家确认'),
        ('confirmed', '卖家已确认'),
        ('meeting', '待当面交付'),
        ('completed', '交易完成'),
        ('cancelled', '已取消'),
        ('borrowed', '借用中'),
        ('returned', '已归还'),
    )

    item = models.ForeignKey(Item, on_delete=models.CASCADE, related_name='orders', verbose_name='商品')
    buyer = models.ForeignKey(User, on_delete=models.CASCADE, related_name='purchased_orders', verbose_name='买家')
    seller = models.ForeignKey(User, on_delete=models.CASCADE, related_name='sold_orders', verbose_name='卖家')
    meeting_location = models.ForeignKey(
        CampusLocation, on_delete=models.SET_NULL, null=True, blank=True,
        related_name='orders', verbose_name='交付地点',
    )
    agreed_price = models.DecimalField('成交价格', max_digits=10, decimal_places=2)
    deposit_amount = models.DecimalField('借用押金', max_digits=10, decimal_places=2, default=0)
    return_due_at = models.DateTimeField('预计归还时间', null=True, blank=True)
    returned_at = models.DateTimeField('实际归还时间', null=True, blank=True)
    buyer_note = models.TextField('买家备注', blank=True)
    status = models.CharField('订单状态', max_length=20, choices=STATUS_CHOICES, default='pending')
    created_at = models.DateTimeField('下单时间', auto_now_add=True)
    updated_at = models.DateTimeField('更新时间', auto_now=True)
    confirmation_deadline = models.DateTimeField('卖家确认截止时间', null=True, blank=True)
    confirmation_reminder_sent_at = models.DateTimeField('确认提醒发送时间', null=True, blank=True)
    return_reminder_sent_at = models.DateTimeField('归还提醒发送时间', null=True, blank=True)
    return_overdue_notice_sent_at = models.DateTimeField('归还逾期提醒发送时间', null=True, blank=True)

    class Meta:
        verbose_name = '交易订单'
        verbose_name_plural = '交易订单'
        ordering = ['-created_at']
        constraints = [
            models.UniqueConstraint(
                fields=['item'],
                condition=Q(status__in=['pending', 'confirmed', 'meeting', 'borrowed']),
                name='unique_active_order_per_item',
            ),
        ]
        indexes = [
            models.Index(fields=['buyer', 'status']),
            models.Index(fields=['seller', 'status']),
        ]

    def __str__(self):
        return f'{self.item.title} · {self.get_status_display()}'


class OrderEvent(models.Model):
    STATUS_CHOICES = Order.STATUS_CHOICES

    order = models.ForeignKey(Order, on_delete=models.CASCADE, related_name='events', verbose_name='订单')
    actor = models.ForeignKey(
        User, on_delete=models.SET_NULL, null=True, blank=True,
        related_name='order_events', verbose_name='操作人',
    )
    from_status = models.CharField('原状态', max_length=20, blank=True)
    to_status = models.CharField('目标状态', max_length=20, choices=STATUS_CHOICES)
    note = models.CharField('事件说明', max_length=200, blank=True)
    created_at = models.DateTimeField('发生时间', auto_now_add=True)

    class Meta:
        verbose_name = '订单状态记录'
        verbose_name_plural = '订单状态记录'
        ordering = ['created_at', 'id']
        indexes = [
            models.Index(fields=['order', 'created_at']),
        ]

    def __str__(self):
        return f'{self.order.item.title} · {self.get_to_status_display()}'



class DeliveryConfirmation(models.Model):
    order = models.OneToOneField(
        Order, on_delete=models.CASCADE, related_name='delivery_confirmation', verbose_name='订单',
    )
    buyer_confirmed_at = models.DateTimeField('买家确认时间', null=True, blank=True)
    seller_confirmed_at = models.DateTimeField('卖家确认时间', null=True, blank=True)
    buyer_returned_at = models.DateTimeField('买家归还登记时间', null=True, blank=True)
    seller_returned_at = models.DateTimeField('卖家归还确认时间', null=True, blank=True)
    handoff_code_hash = models.CharField('交付确认码哈希', max_length=128, blank=True)
    handoff_code_hint = models.CharField('交付确认码提示', max_length=8, blank=True)
    handoff_code_issued_at = models.DateTimeField('交付确认码生成时间', null=True, blank=True)
    handoff_code_used_at = models.DateTimeField('交付确认码使用时间', null=True, blank=True)
    handoff_code_attempts = models.PositiveSmallIntegerField('交付确认码错误次数', default=0)
    created_at = models.DateTimeField('创建时间', auto_now_add=True)
    updated_at = models.DateTimeField('更新时间', auto_now=True)

    class Meta:
        verbose_name = '交付确认'
        verbose_name_plural = '交付确认'

    @property
    def is_complete(self):
        return bool(self.buyer_confirmed_at and self.seller_confirmed_at)

    @property
    def return_is_complete(self):
        return bool(self.buyer_returned_at and self.seller_returned_at)

    def __str__(self):
        return f'{self.order.item.title} · 交付确认'


class MeetingAppointment(models.Model):
    STATUS_CHOICES = (
        ('pending', '待对方确认'),
        ('confirmed', '已确认'),
        ('declined', '对方未接受'),
        ('cancelled', '已取消'),
    )

    order = models.OneToOneField(
        Order, on_delete=models.CASCADE, related_name='appointment', verbose_name='订单',
    )
    proposed_by = models.ForeignKey(
        User, on_delete=models.SET_NULL, null=True, related_name='proposed_appointments', verbose_name='提议人',
    )
    location = models.ForeignKey(
        CampusLocation, on_delete=models.SET_NULL, null=True, blank=True,
        related_name='meeting_appointments', verbose_name='交付地点',
    )
    start_at = models.DateTimeField('开始时间')
    end_at = models.DateTimeField('结束时间')
    status = models.CharField('安排状态', max_length=20, choices=STATUS_CHOICES, default='pending')
    responded_by = models.ForeignKey(
        User, on_delete=models.SET_NULL, null=True, blank=True,
        related_name='responded_appointments', verbose_name='回应人',
    )
    responded_at = models.DateTimeField('回应时间', null=True, blank=True)
    decline_reason = models.CharField('未接受原因', max_length=200, blank=True)
    buyer_arrived_at = models.DateTimeField('买家到场时间', null=True, blank=True)
    seller_arrived_at = models.DateTimeField('卖家到场时间', null=True, blank=True)
    created_at = models.DateTimeField('创建时间', auto_now_add=True)
    updated_at = models.DateTimeField('更新时间', auto_now=True)

    class Meta:
        verbose_name = '交付时间安排'
        verbose_name_plural = '交付时间安排'
        ordering = ['start_at']
        indexes = [
            models.Index(fields=['status', 'start_at']),
            models.Index(fields=['proposed_by', '-created_at']),
        ]
        constraints = [
            models.CheckConstraint(
                condition=models.Q(end_at__gt=models.F('start_at')),
                name='meeting_appointment_end_after_start',
            ),
        ]

    @property
    def is_pending(self):
        return self.status == 'pending'

    @property
    def is_confirmed(self):
        return self.status == 'confirmed'

    @property
    def check_in_open(self):
        if self.status != 'confirmed':
            return False
        now = timezone.now()
        return self.start_at - timedelta(minutes=30) <= now <= self.end_at + timedelta(minutes=30)

    @property
    def can_report_incident(self):
        return self.status == 'confirmed' and timezone.now() > self.end_at

    def __str__(self):
        return f'{self.order.item.title} · {self.start_at:%Y-%m-%d %H:%M}'


class MeetingIncident(models.Model):
    REASON_CHOICES = (
        ('no_show', '对方未到场'),
        ('late', '对方严重迟到'),
        ('safety', '现场安全问题'),
        ('other', '其他预约异常'),
    )
    STATUS_CHOICES = (
        ('open', '待处理'),
        ('reviewing', '处理中'),
        ('resolved', '已确认异常'),
        ('dismissed', '已驳回'),
    )

    appointment = models.OneToOneField(
        MeetingAppointment, on_delete=models.CASCADE, related_name='incident', verbose_name='交付安排',
    )
    reported_by = models.ForeignKey(
        User, on_delete=models.CASCADE, related_name='reported_meeting_incidents', verbose_name='提交人',
    )
    accused = models.ForeignKey(
        User, on_delete=models.CASCADE, related_name='accused_meeting_incidents', verbose_name='相关对方',
    )
    reason = models.CharField('异常类型', max_length=20, choices=REASON_CHOICES)
    detail = models.TextField('情况说明')
    status = models.CharField('处理状态', max_length=20, choices=STATUS_CHOICES, default='open')
    reviewer = models.ForeignKey(
        User, on_delete=models.SET_NULL, null=True, blank=True,
        related_name='reviewed_meeting_incidents', verbose_name='处理人',
    )
    resolution_note = models.TextField('处理意见', blank=True)
    reviewed_at = models.DateTimeField('处理时间', null=True, blank=True)
    created_at = models.DateTimeField('提交时间', auto_now_add=True)
    updated_at = models.DateTimeField('更新时间', auto_now=True)

    class Meta:
        verbose_name = '交付预约异常'
        verbose_name_plural = '交付预约异常'
        ordering = ['status', '-created_at']
        indexes = [
            models.Index(fields=['status', '-created_at']),
            models.Index(fields=['reported_by', '-created_at']),
        ]
        constraints = [
            models.CheckConstraint(
                condition=~models.Q(reported_by=models.F('accused')),
                name='meeting_incident_reporter_differs_accused',
            ),
        ]

    def __str__(self):
        return f'{self.appointment.order.item.title} · {self.get_reason_display()}'


class OrderDispute(models.Model):
    REASON_CHOICES = (
        ('not_received', '未收到商品'),
        ('mismatch', '商品与描述不符'),
        ('payment', '价格或付款问题'),
        ('safety', '交易安全问题'),
        ('other', '其他争议'),
    )
    STATUS_CHOICES = (
        ('open', '待处理'),
        ('reviewing', '处理中'),
        ('resolved', '已解决'),
        ('rejected', '已驳回'),
    )

    order = models.OneToOneField(
        Order, on_delete=models.CASCADE, related_name='dispute', verbose_name='相关订单',
    )
    opened_by = models.ForeignKey(
        User, on_delete=models.CASCADE, related_name='opened_order_disputes', verbose_name='发起人',
    )
    reason = models.CharField('争议类型', max_length=30, choices=REASON_CHOICES)
    detail = models.TextField('争议说明')
    status = models.CharField('处理状态', max_length=20, choices=STATUS_CHOICES, default='open')
    reviewer = models.ForeignKey(
        User, on_delete=models.SET_NULL, null=True, blank=True,
        related_name='reviewed_order_disputes', verbose_name='处理人',
    )
    resolution_note = models.TextField('处理意见', blank=True)
    resolved_at = models.DateTimeField('处理时间', null=True, blank=True)
    created_at = models.DateTimeField('发起时间', auto_now_add=True)
    updated_at = models.DateTimeField('更新时间', auto_now=True)

    class Meta:
        verbose_name = '交易争议'
        verbose_name_plural = '交易争议'
        ordering = ['status', '-created_at']
        indexes = [
            models.Index(fields=['status', '-created_at']),
            models.Index(fields=['opened_by', '-created_at']),
        ]

    def __str__(self):
        return f'{self.order.item.title} · {self.get_reason_display()}'


def validate_dispute_evidence(upload):
    """Keep dispute attachments small and limited to reviewable document types."""
    max_size = 5 * 1024 * 1024
    allowed_extensions = {'.jpg', '.jpeg', '.png', '.webp', '.pdf'}
    suffix = Path(upload.name).suffix.lower()
    if upload.size > max_size:
        raise ValidationError('证据文件不能超过 5 MB。')
    if suffix not in allowed_extensions:
        raise ValidationError('证据文件仅支持 JPG、PNG、WebP 或 PDF。')


class OrderDisputeEvidence(models.Model):
    dispute = models.ForeignKey(
        OrderDispute, on_delete=models.CASCADE, related_name='evidence', verbose_name='交易争议',
    )
    uploaded_by = models.ForeignKey(
        User, on_delete=models.CASCADE, related_name='uploaded_order_dispute_evidence', verbose_name='上传人',
    )
    attachment = models.FileField(
        '证据文件', upload_to='dispute_evidence/%Y/%m/', validators=[validate_dispute_evidence],
    )
    note = models.CharField('证据说明', max_length=300, blank=True)
    created_at = models.DateTimeField('上传时间', auto_now_add=True)

    class Meta:
        verbose_name = '交易争议证据'
        verbose_name_plural = '交易争议证据'
        ordering = ['-created_at']
        indexes = [
            models.Index(fields=['dispute', '-created_at']),
        ]

    @property
    def filename(self):
        return Path(self.attachment.name).name

    def __str__(self):
        return f'{self.dispute} · {self.uploaded_by.username}'


class BrowsingHistory(models.Model):
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name='browsing_history', verbose_name='用户')
    item = models.ForeignKey(Item, on_delete=models.CASCADE, related_name='view_history', verbose_name='商品')
    view_count = models.PositiveIntegerField('浏览次数', default=1)
    first_viewed_at = models.DateTimeField('首次浏览时间', auto_now_add=True)
    last_viewed_at = models.DateTimeField('最近浏览时间', auto_now=True)

    class Meta:
        verbose_name = '浏览记录'
        verbose_name_plural = '浏览记录'
        ordering = ['-last_viewed_at']
        constraints = [
            models.UniqueConstraint(fields=['user', 'item'], name='unique_user_browsing_item'),
        ]
        indexes = [
            models.Index(fields=['user', '-last_viewed_at']),
        ]

    def __str__(self):
        return f'{self.user.username}浏览了{self.item.title}'


class Rating(models.Model):
    SCORE_CHOICES = tuple((score, f'{score} 星') for score in range(1, 6))

    order = models.ForeignKey(Order, on_delete=models.CASCADE, related_name='ratings', verbose_name='订单')
    rater = models.ForeignKey(User, on_delete=models.CASCADE, related_name='given_ratings', verbose_name='评价人')
    ratee = models.ForeignKey(User, on_delete=models.CASCADE, related_name='received_ratings', verbose_name='被评价人')
    score = models.PositiveSmallIntegerField('评分', choices=SCORE_CHOICES)
    comment = models.TextField('评价内容', blank=True)
    created_at = models.DateTimeField('评价时间', auto_now_add=True)

    class Meta:
        verbose_name = '交易评价'
        verbose_name_plural = '交易评价'
        ordering = ['-created_at']
        constraints = [
            models.UniqueConstraint(fields=['order', 'rater'], name='unique_order_rater'),
        ]
        indexes = [
            models.Index(fields=['ratee', '-created_at']),
        ]

    def __str__(self):
        return f'{self.rater.username}评价{self.ratee.username} · {self.score}星'


class CommunityContribution(models.Model):
    """An auditable, idempotent record of a user's positive campus contribution."""

    KIND_CHOICES = (
        ('trade_completed', '完成交易'),
        ('borrow_returned', '完成借用归还'),
        ('gift_completed', '完成免费赠送'),
        ('lost_found_help', '协助失物招领'),
        ('demand_helped', '响应校园求购'),
        ('mutual_aid_completed', '完成一次校园互助'),
    )

    user = models.ForeignKey(
        User, on_delete=models.CASCADE, related_name='community_contributions', verbose_name='用户',
    )
    kind = models.CharField('贡献类型', max_length=30, choices=KIND_CHOICES)
    points = models.PositiveIntegerField('贡献积分')
    title = models.CharField('贡献标题', max_length=120)
    description = models.CharField('贡献说明', max_length=255, blank=True)
    source_key = models.CharField(
        '来源幂等键', max_length=180, unique=True,
        help_text='同一业务事件只能产生一次贡献记录，避免重复积分。',
    )
    occurred_at = models.DateTimeField('发生时间', default=timezone.now)
    created_at = models.DateTimeField('记录时间', auto_now_add=True)

    class Meta:
        verbose_name = '校园互助贡献'
        verbose_name_plural = '校园互助贡献'
        ordering = ['-occurred_at', '-id']
        indexes = [
            models.Index(fields=['user', '-occurred_at']),
            models.Index(fields=['user', 'kind', '-occurred_at']),
        ]

    def __str__(self):
        return f'{self.user.username} · {self.title} · {self.points}分'


class MutualAidFeedback(models.Model):
    """A lightweight, owner-confirmed outcome for a non-order interaction."""

    OUTCOME_CHOICES = (
        ('completed', '已完成'),
        ('unresolved', '暂未解决'),
    )
    TAG_CHOICES = (
        ('on_time', '按约完成'),
        ('smooth', '沟通顺畅'),
        ('helpful', '信息有帮助'),
        ('needs_follow_up', '还需要后续跟进'),
    )

    demand_response = models.OneToOneField(
        'DemandResponse', on_delete=models.CASCADE, null=True, blank=True,
        related_name='feedback', verbose_name='求购响应',
    )
    lost_found_lead = models.OneToOneField(
        'LostFoundLead', on_delete=models.CASCADE, null=True, blank=True,
        related_name='feedback', verbose_name='失物招领线索',
    )
    submitted_by = models.ForeignKey(
        User, on_delete=models.CASCADE, related_name='submitted_mutual_aid_feedback',
        verbose_name='确认人',
    )
    outcome = models.CharField('互助结果', max_length=20, choices=OUTCOME_CHOICES)
    tags = models.JSONField('反馈标签', default=list, blank=True)
    note = models.CharField('补充说明', max_length=500, blank=True)
    created_at = models.DateTimeField('反馈时间', auto_now_add=True)
    updated_at = models.DateTimeField('更新时间', auto_now=True)

    class Meta:
        verbose_name = '互助完成反馈'
        verbose_name_plural = '互助完成反馈'
        ordering = ['-created_at']
        constraints = [
            models.CheckConstraint(
                condition=(
                    models.Q(demand_response__isnull=False, lost_found_lead__isnull=True)
                    | models.Q(demand_response__isnull=True, lost_found_lead__isnull=False)
                ),
                name='mutual_aid_feedback_exactly_one_source',
            ),
        ]
        indexes = [
            models.Index(fields=['submitted_by', '-created_at']),
            models.Index(fields=['outcome', '-created_at']),
        ]

    @property
    def source(self):
        return self.demand_response or self.lost_found_lead

    @property
    def helper(self):
        source = self.source
        return source.responder if hasattr(source, 'responder') else source.respondent

    def clean(self):
        if bool(self.demand_response_id) == bool(self.lost_found_lead_id):
            raise ValidationError('互助反馈必须且只能关联一条求购响应或失物招领线索。')
        if not isinstance(self.tags, list):
            raise ValidationError('反馈标签格式不正确。')
        valid_tags = {key for key, _ in self.TAG_CHOICES}
        if any(tag not in valid_tags for tag in self.tags):
            raise ValidationError('反馈标签包含不支持的选项。')

        source = self.source
        if source is None:
            return
        if source.status != 'accepted':
            raise ValidationError('只有已确认的互助响应才能提交完成反馈。')
        owner_id = (
            source.demand.requester_id
            if self.demand_response_id
            else source.post.reporter_id
        )
        if self.submitted_by_id != owner_id:
            raise ValidationError('只有互助发布者可以确认反馈结果。')

    def __str__(self):
        return f'{self.get_outcome_display()} · {self.helper.username}'


class RecommendationFeedback(models.Model):
    ACTION_CHOICES = (
        ('interested', '想看看'),
        ('dismiss', '不感兴趣'),
    )

    user = models.ForeignKey(
        User, on_delete=models.CASCADE, related_name='recommendation_feedbacks', verbose_name='用户',
    )
    item = models.ForeignKey(
        Item, on_delete=models.CASCADE, related_name='recommendation_feedbacks', verbose_name='商品',
    )
    action = models.CharField('反馈动作', max_length=20, choices=ACTION_CHOICES)
    created_at = models.DateTimeField('首次反馈时间', auto_now_add=True)
    updated_at = models.DateTimeField('最近反馈时间', auto_now=True)

    class Meta:
        verbose_name = '推荐反馈'
        verbose_name_plural = '推荐反馈'
        ordering = ['-updated_at']
        constraints = [
            models.UniqueConstraint(fields=['user', 'item'], name='unique_recommendation_feedback'),
        ]
        indexes = [
            models.Index(fields=['user', 'action', '-updated_at']),
            models.Index(fields=['item', 'action']),
        ]

    def __str__(self):
        return f'{self.user.username} · {self.item.title} · {self.get_action_display()}'


class Notification(models.Model):
    KIND_CHOICES = (
        ('order_created', '新的交易预约'),
        ('gift_application', '新的领取申请'),
        ('gift_application_status', '领取申请状态更新'),
        ('order_status', '订单状态更新'),
        ('rating_received', '收到交易评价'),
        ('message_received', '收到新私信'),
        ('comment_received', '收到商品留言'),
        ('saved_search_match', '关注的搜索有新商品'),
        ('item_available', '商品重新有货'),
        ('item_expired', '商品展示已到期'),
        ('order_dispute', '交易争议更新'),
        ('meeting_incident', '交付预约异常'),
        ('order_expiring', '交易预约即将超时'),
        ('order_expired', '交易预约已超时'),
        ('report_update', '举报处理更新'),
        ('moderation_update', '内容审核结果'),
        ('operations_digest', '运营告警日报'),
        ('demand_match', '求购匹配提醒'),
        ('demand_response', '求购响应更新'),
        ('lost_found_match', '失物招领匹配提醒'),
        ('lost_found_lead', '失物招领线索更新'),
        ('opportunity_digest', '互助机会摘要'),
        ('saved_search_digest', '关注搜索汇总提醒'),
    )

    recipient = models.ForeignKey(User, on_delete=models.CASCADE, related_name='notifications', verbose_name='接收人')
    actor = models.ForeignKey(
        User, on_delete=models.SET_NULL, null=True, blank=True,
        related_name='triggered_notifications', verbose_name='触发人',
    )
    order = models.ForeignKey(Order, on_delete=models.CASCADE, null=True, blank=True, related_name='notifications', verbose_name='相关订单')
    item = models.ForeignKey(Item, on_delete=models.CASCADE, null=True, blank=True, related_name='notifications', verbose_name='相关商品')
    demand = models.ForeignKey(
        'DemandPost', on_delete=models.SET_NULL, null=True, blank=True,
        related_name='notifications', verbose_name='相关求购',
    )
    kind = models.CharField('通知类型', max_length=30, choices=KIND_CHOICES)
    title = models.CharField('通知标题', max_length=120)
    message = models.CharField('通知内容', max_length=255)
    target_url = models.CharField('跳转地址', max_length=255, blank=True)
    is_read = models.BooleanField('已读', default=False)
    snoozed_until = models.DateTimeField(
        '延后至', null=True, blank=True,
        help_text='在这个时间之前不计入未读提醒，适合暂时不方便处理的事项。',
    )
    dedupe_key = models.CharField('聚合键', max_length=120, blank=True, default='')
    occurrence_count = models.PositiveIntegerField('聚合次数', default=1)
    last_occurred_at = models.DateTimeField('最近发生时间', null=True, blank=True)
    created_at = models.DateTimeField('创建时间', auto_now_add=True)

    class Meta:
        verbose_name = '站内通知'
        verbose_name_plural = '站内通知'
        ordering = ['-created_at']
        indexes = [
            models.Index(fields=['recipient', 'is_read', '-created_at']),
            models.Index(fields=['recipient', '-created_at']),
            models.Index(fields=['recipient', 'kind', 'dedupe_key', 'is_read']),
            models.Index(fields=['recipient', 'is_read', 'snoozed_until', '-created_at']),
        ]

    @property
    def is_snoozed(self):
        return bool(
            not self.is_read
            and self.snoozed_until
            and self.snoozed_until > timezone.now()
        )

    def __str__(self):
        return f'{self.recipient.username} · {self.title}'


class NotificationPreference(models.Model):
    """Per-user switches for the notification channels created by the app."""

    user = models.OneToOneField(
        User, on_delete=models.CASCADE, related_name='notification_preference', verbose_name='用户',
    )
    order_created = models.BooleanField(
        '新的交易预约', default=True, help_text='有人预约你的商品时提醒。',
    )
    gift_application = models.BooleanField(
        '新的领取申请', default=True, help_text='有人申请领取你的免费商品时提醒。',
    )
    gift_application_status = models.BooleanField(
        '领取申请状态更新', default=True, help_text='免费领取申请被选中、未选中或撤回时提醒。',
    )
    order_status = models.BooleanField(
        '订单状态更新', default=True, help_text='订单状态推进、取消或完成时提醒。',
    )
    rating_received = models.BooleanField(
        '收到交易评价', default=True, help_text='交易对方完成评价时提醒。',
    )
    message_received = models.BooleanField(
        '收到新私信', default=True, help_text='收到新的私信时提醒。',
    )
    comment_received = models.BooleanField(
        '收到商品留言', default=True, help_text='有人在你的商品下留言时提醒。',
    )
    saved_search_match = models.BooleanField(
        '关注的搜索有新商品', default=True, help_text='关注的搜索匹配到新商品时提醒。',
    )
    item_available = models.BooleanField(
        '商品重新有货', default=True, help_text='你关注的商品恢复为在售时提醒。',
    )
    item_expired = models.BooleanField(
        '商品展示已到期', default=True, help_text='你发布的商品自动下架时提醒。',
    )
    order_dispute = models.BooleanField(
        '交易争议更新', default=True, help_text='交易争议状态发生变化时提醒。',
    )
    meeting_incident = models.BooleanField(
        '交付预约异常', default=True, help_text='交付预约出现到场或安全异常时提醒。',
    )
    order_expiring = models.BooleanField(
        '交易预约即将超时', default=True, help_text='交易预约接近确认截止时间时提醒。',
    )
    order_expired = models.BooleanField(
        '交易预约已超时', default=True, help_text='交易预约因超时被释放时提醒。',
    )
    report_update = models.BooleanField(
        '举报处理更新', default=True, help_text='你提交的举报有处理进展时提醒。',
    )
    moderation_update = models.BooleanField(
        '内容审核结果', default=True, help_text='你提交的留言或私信完成复核时提醒。',
    )
    operations_digest = models.BooleanField(
        '运营告警日报', default=True, help_text='管理员运营看板出现高优先级信号时发送每日摘要。',
    )
    demand_match = models.BooleanField(
        '求购匹配提醒', default=True, help_text='有商品可能符合你发布的求购信息时提醒。',
    )
    demand_response = models.BooleanField(
        '求购响应更新', default=True, help_text='有人响应你的求购，或你的响应状态发生变化时提醒。',
    )
    lost_found_match = models.BooleanField(
        '失物招领匹配提醒', default=True, help_text='有记录与我发布的失物招领信息可能匹配时提醒。',
    )
    lost_found_lead = models.BooleanField(
        '失物招领线索更新', default=True, help_text='有人提交、确认或拒绝失物招领线索时提醒。',
    )
    opportunity_digest = models.BooleanField(
        '互助机会摘要', default=True, help_text='定期汇总可能适合你响应的求购和失物招领机会。',
    )
    mutual_aid_feedback = models.BooleanField(
        '互助完成反馈', default=True, help_text='互助发布者确认响应结果后提醒参与者。',
    )
    saved_search_digest = models.BooleanField(
        '关注搜索汇总提醒', default=True,
        help_text='选择每日或每周汇总的关注搜索，会在对应周期发送一条合并提醒。',
    )
    quiet_hours_enabled = models.BooleanField(
        '启用免打扰时段', default=False,
        help_text='启用后，指定时段内不会显示顶部未读提醒，但通知仍会保留在通知中心。',
    )
    quiet_hours_start = models.TimeField(
        '免打扰开始时间', default=time(22, 0),
    )
    quiet_hours_end = models.TimeField(
        '免打扰结束时间', default=time(8, 0),
    )
    updated_at = models.DateTimeField('更新时间', auto_now=True)

    class Meta:
        verbose_name = '通知偏好'
        verbose_name_plural = '通知偏好'

    def __str__(self):
        return f'{self.user.username} · 通知偏好'


class SearchQuery(models.Model):
    user = models.ForeignKey(
        User, on_delete=models.SET_NULL, null=True, blank=True,
        related_name='search_queries', verbose_name='用户',
    )
    query = models.CharField('搜索词', max_length=120)
    condition = models.CharField('成色筛选', max_length=120, blank=True)
    category = models.ForeignKey(
        Category, on_delete=models.SET_NULL, null=True, blank=True,
        related_name='search_queries', verbose_name='分类',
    )
    location = models.ForeignKey(
        CampusLocation, on_delete=models.SET_NULL, null=True, blank=True,
        related_name='search_queries', verbose_name='交易地点',
    )
    min_price = models.DecimalField('最低价格', max_digits=10, decimal_places=2, null=True, blank=True)
    max_price = models.DecimalField('最高价格', max_digits=10, decimal_places=2, null=True, blank=True)
    result_count = models.PositiveIntegerField('结果数量', default=0)
    created_at = models.DateTimeField('搜索时间', auto_now_add=True)

    class Meta:
        verbose_name = '搜索记录'
        verbose_name_plural = '搜索记录'
        ordering = ['-created_at']
        indexes = [
            models.Index(fields=['query', '-created_at']),
            models.Index(fields=['user', '-created_at']),
        ]

    def __str__(self):
        return f'{self.query} · {self.result_count} 条结果'


class SearchSynonym(models.Model):
    """An operator-managed pair of equivalent terms used by marketplace search."""

    keyword = models.CharField('主关键词', max_length=120)
    synonym = models.CharField('同义词', max_length=120)
    is_active = models.BooleanField('启用', default=True)
    created_at = models.DateTimeField('创建时间', auto_now_add=True)
    updated_at = models.DateTimeField('更新时间', auto_now=True)

    class Meta:
        verbose_name = '搜索同义词'
        verbose_name_plural = '搜索同义词'
        ordering = ['keyword', 'synonym']
        constraints = [
            models.UniqueConstraint(
                fields=['keyword', 'synonym'], name='unique_search_synonym_pair',
            ),
        ]
        indexes = [
            models.Index(fields=['keyword', 'is_active']),
            models.Index(fields=['synonym', 'is_active']),
        ]

    def __str__(self):
        return f'{self.keyword} ↔ {self.synonym}'


class SearchClick(models.Model):
    """A click-through event from a recorded search to a listing detail page."""

    search_query = models.ForeignKey(
        SearchQuery, on_delete=models.SET_NULL, null=True, blank=True,
        related_name='clicks', verbose_name='搜索记录',
    )
    item = models.ForeignKey(
        Item, on_delete=models.CASCADE, related_name='search_clicks', verbose_name='商品',
    )
    user = models.ForeignKey(
        User, on_delete=models.SET_NULL, null=True, blank=True,
        related_name='search_clicks', verbose_name='用户',
    )
    position = models.PositiveIntegerField('结果位置', default=0)
    created_at = models.DateTimeField('点击时间', auto_now_add=True)

    class Meta:
        verbose_name = '搜索点击'
        verbose_name_plural = '搜索点击'
        ordering = ['-created_at']
        indexes = [
            models.Index(fields=['search_query', '-created_at']),
            models.Index(fields=['item', '-created_at']),
            models.Index(fields=['created_at']),
        ]

    def __str__(self):
        query = self.search_query.query if self.search_query else '未知搜索'
        return f'{query} · {self.item.title}'


class SearchImpression(models.Model):
    """An exposure event for one listing rendered in a search result page."""

    search_query = models.ForeignKey(
        SearchQuery, on_delete=models.SET_NULL, null=True, blank=True,
        related_name='impressions', verbose_name='搜索记录',
    )
    item = models.ForeignKey(
        Item, on_delete=models.CASCADE, related_name='search_impressions', verbose_name='商品',
    )
    user = models.ForeignKey(
        User, on_delete=models.SET_NULL, null=True, blank=True,
        related_name='search_impressions', verbose_name='用户',
    )
    position = models.PositiveIntegerField('结果位置', default=0)
    created_at = models.DateTimeField('曝光时间', auto_now_add=True)

    class Meta:
        verbose_name = '搜索曝光'
        verbose_name_plural = '搜索曝光'
        ordering = ['-created_at']
        indexes = [
            models.Index(fields=['search_query', '-created_at']),
            models.Index(fields=['item', '-created_at']),
            models.Index(fields=['created_at']),
        ]

    def __str__(self):
        query = self.search_query.query if self.search_query else '未知搜索'
        return f'{query} · {self.item.title}'


class SavedSearch(models.Model):
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name='saved_searches', verbose_name='用户')
    name = models.CharField('关注名称', max_length=80)
    query = models.CharField('搜索词', max_length=120, blank=True)
    condition = models.CharField('成色筛选', max_length=120, blank=True)
    category = models.ForeignKey(
        Category, on_delete=models.SET_NULL, null=True, blank=True,
        related_name='saved_searches', verbose_name='分类',
    )
    location = models.ForeignKey(
        CampusLocation, on_delete=models.SET_NULL, null=True, blank=True,
        related_name='saved_searches', verbose_name='交易地点',
    )
    min_price = models.DecimalField('最低价格', max_digits=10, decimal_places=2, null=True, blank=True)
    max_price = models.DecimalField('最高价格', max_digits=10, decimal_places=2, null=True, blank=True)
    is_active = models.BooleanField('启用提醒', default=True)
    FREQUENCY_CHOICES = (
        ('instant', '即时提醒'),
        ('daily', '每日汇总'),
        ('weekly', '每周汇总'),
    )
    notify_frequency = models.CharField(
        '提醒频率', max_length=10, choices=FREQUENCY_CHOICES, default='instant',
        help_text='即时提醒在商品发布时马上通知；汇总方式按日或按周合并成一条提醒。',
    )
    max_matches_per_notice = models.PositiveSmallIntegerField(
        '单次最多提醒条数', default=3,
        help_text='一条通知里最多列出几条命中商品，避免一次刷屏。',
    )
    quiet_until = models.DateTimeField(
        '临时静默至', null=True, blank=True,
        help_text='在这个时间之前不发送这条关注的提醒，适合考试周或假期。',
    )
    created_at = models.DateTimeField('创建时间', auto_now_add=True)
    updated_at = models.DateTimeField('更新时间', auto_now=True)

    class Meta:
        verbose_name = '关注的搜索'
        verbose_name_plural = '关注的搜索'
        ordering = ['-is_active', '-created_at']
        constraints = [
            models.UniqueConstraint(fields=['user', 'name'], name='unique_user_saved_search_name'),
        ]
        indexes = [
            models.Index(fields=['user', 'is_active', '-created_at']),
            models.Index(fields=['category', 'location', 'is_active']),
            models.Index(fields=['notify_frequency', 'is_active']),
        ]

    def __str__(self):
        return f'{self.user.username} · {self.name}'

    @property
    def is_quiet(self):
        """Whether this saved search is inside a user-declared silent window."""
        return bool(self.quiet_until and self.quiet_until > timezone.now())

    def deliverable_now(self, *, now=None):
        """Whether a newly matched item may trigger a notification right now."""
        now = now or timezone.now()
        if not self.is_active:
            return False
        if self.quiet_until and self.quiet_until > now:
            return False
        return self.notify_frequency == 'instant'

    @property
    def frequency_label(self):
        return dict(self.FREQUENCY_CHOICES).get(self.notify_frequency, self.notify_frequency)


class SavedSearchMatch(models.Model):
    """Buffer for saved-search hits delivered as a digest instead of instantly."""
    saved_search = models.ForeignKey(
        SavedSearch, on_delete=models.CASCADE, related_name='buffered_matches',
        verbose_name='关注的搜索',
    )
    item = models.ForeignKey(
        Item, on_delete=models.CASCADE, related_name='saved_search_matches', verbose_name='命中商品',
    )
    matched_at = models.DateTimeField('命中时间', auto_now_add=True)
    notified_at = models.DateTimeField('已通知时间', null=True, blank=True)

    class Meta:
        verbose_name = '关注搜索命中缓冲'
        verbose_name_plural = '关注搜索命中缓冲'
        ordering = ['-matched_at']
        constraints = [
            models.UniqueConstraint(
                fields=['saved_search', 'item'], name='unique_saved_search_item_match',
            ),
        ]
        indexes = [
            models.Index(fields=['saved_search', 'notified_at', '-matched_at']),
            models.Index(fields=['item']),
        ]

    def __str__(self):
        return f'{self.saved_search.name} · {self.item.title}'

class DemandPost(models.Model):
    """A reverse listing where a student describes what they are looking for."""

    STATUS_CHOICES = (
        ('active', '寻找中'),
        ('fulfilled', '已找到'),
        ('closed', '已关闭'),
        ('expired', '已过期'),
    )

    requester = models.ForeignKey(
        User, on_delete=models.CASCADE, related_name='demand_posts', verbose_name='发布者',
    )
    title = models.CharField('求购标题', max_length=160)
    description = models.TextField('需求描述')
    category = models.ForeignKey(
        Category, on_delete=models.SET_NULL, null=True, blank=True,
        related_name='demand_posts', verbose_name='分类',
    )
    location = models.ForeignKey(
        CampusLocation, on_delete=models.SET_NULL, null=True, blank=True,
        related_name='demand_posts', verbose_name='期望地点',
    )
    min_price = models.DecimalField('最低预算', max_digits=10, decimal_places=2, null=True, blank=True)
    max_price = models.DecimalField('最高预算', max_digits=10, decimal_places=2, null=True, blank=True)
    expires_at = models.DateTimeField('需求截止时间', null=True, blank=True)
    status = models.CharField('状态', max_length=20, choices=STATUS_CHOICES, default='active')
    view_count = models.PositiveIntegerField('浏览次数', default=0)
    created_at = models.DateTimeField('发布时间', auto_now_add=True)
    updated_at = models.DateTimeField('更新时间', auto_now=True)

    class Meta:
        verbose_name = '求购信息'
        verbose_name_plural = '求购信息'
        ordering = ['-created_at']
        indexes = [
            models.Index(fields=['status', '-created_at']),
            models.Index(fields=['category', 'status']),
            models.Index(fields=['location', 'status']),
            models.Index(fields=['expires_at', 'status']),
        ]

    def __str__(self):
        return self.title


class DemandOpportunityTask(models.Model):
    """A durable staff follow-up task created from a demand-radar snapshot."""

    STATUS_CHOICES = (
        ('todo', '待跟进'),
        ('in_progress', '跟进中'),
        ('completed', '已完成'),
        ('ignored', '已忽略'),
    )

    radar_key = models.CharField('雷达主题标识', max_length=260)
    title = models.CharField('机会主题', max_length=160)
    category = models.ForeignKey(
        Category, on_delete=models.SET_NULL, null=True, blank=True,
        related_name='demand_opportunity_tasks', verbose_name='分类',
    )
    location = models.ForeignKey(
        CampusLocation, on_delete=models.SET_NULL, null=True, blank=True,
        related_name='demand_opportunity_tasks', verbose_name='地点',
    )
    level = models.CharField('优先级', max_length=20, default='medium')
    opportunity_score = models.PositiveIntegerField('机会分', default=0)
    search_count = models.PositiveIntegerField('无结果搜索次数', default=0)
    demand_count = models.PositiveIntegerField('有效求购数', default=0)
    available_supply = models.PositiveIntegerField('创建任务时供给数', default=0)
    evidence = models.JSONField('证据信号', default=list, blank=True)
    recommendations = models.JSONField('建议动作', default=list, blank=True)
    status = models.CharField('任务状态', max_length=20, choices=STATUS_CHOICES, default='todo')
    created_by = models.ForeignKey(
        User, on_delete=models.SET_NULL, null=True, blank=True,
        related_name='created_demand_opportunity_tasks', verbose_name='创建人',
    )
    assigned_to = models.ForeignKey(
        User, on_delete=models.SET_NULL, null=True, blank=True,
        related_name='assigned_demand_opportunity_tasks', verbose_name='负责人',
    )
    due_at = models.DateTimeField('跟进截止时间', null=True, blank=True)
    note = models.TextField('跟进备注', blank=True)
    created_at = models.DateTimeField('创建时间', auto_now_add=True)
    updated_at = models.DateTimeField('更新时间', auto_now=True)

    class Meta:
        verbose_name = '需求机会跟进任务'
        verbose_name_plural = '需求机会跟进任务'
        ordering = ['status', '-opportunity_score', '-created_at']
        constraints = [
            models.UniqueConstraint(
                fields=['radar_key'],
                condition=models.Q(status__in=['todo', 'in_progress']),
                name='unique_open_demand_opportunity_task',
            ),
        ]
        indexes = [
            models.Index(fields=['status', '-opportunity_score']),
            models.Index(fields=['radar_key', 'status']),
            models.Index(fields=['assigned_to', 'status']),
        ]

    def __str__(self):
        return f'{self.title} · {self.get_status_display()}'


class LostFoundPost(models.Model):
    """A campus lost-and-found record with explainable cross-post matching."""

    TYPE_CHOICES = (
        ('lost', '我丢失了'),
        ('found', '我捡到了'),
    )
    STATUS_CHOICES = (
        ('active', '寻找中'),
        ('matched', '已匹配'),
        ('closed', '已关闭'),
        ('expired', '已过期'),
    )

    reporter = models.ForeignKey(
        User, on_delete=models.CASCADE, related_name='lost_found_posts', verbose_name='发布者',
    )
    post_type = models.CharField('记录类型', max_length=10, choices=TYPE_CHOICES)
    title = models.CharField('标题', max_length=160)
    description = models.TextField('详细描述')
    category = models.ForeignKey(
        Category, on_delete=models.SET_NULL, null=True, blank=True,
        related_name='lost_found_posts', verbose_name='物品分类',
    )
    location = models.ForeignKey(
        CampusLocation, on_delete=models.SET_NULL, null=True, blank=True,
        related_name='lost_found_posts', verbose_name='发生地点',
    )
    occurred_at = models.DateTimeField('发生时间')
    expires_at = models.DateTimeField('信息截止时间', null=True, blank=True)
    identifying_features = models.CharField(
        '颜色与辨识特征', max_length=240, blank=True,
        help_text='建议填写颜色、品牌、贴纸、挂件等不宜公开过度暴露的特征。',
    )
    status = models.CharField('状态', max_length=12, choices=STATUS_CHOICES, default='active')
    matched_post = models.ForeignKey(
        'self', on_delete=models.SET_NULL, null=True, blank=True,
        related_name='matched_by_posts', verbose_name='匹配记录',
    )
    view_count = models.PositiveIntegerField('浏览次数', default=0)
    created_at = models.DateTimeField('发布时间', auto_now_add=True)
    updated_at = models.DateTimeField('更新时间', auto_now=True)

    class Meta:
        verbose_name = '失物招领记录'
        verbose_name_plural = '失物招领记录'
        ordering = ['-created_at']
        indexes = [
            models.Index(fields=['status', 'post_type', '-created_at']),
            models.Index(fields=['category', 'status']),
            models.Index(fields=['location', 'status']),
            models.Index(fields=['occurred_at', 'status']),
            models.Index(fields=['expires_at', 'status']),
        ]

    def clean(self):
        if self.matched_post_id and self.matched_post_id == self.pk:
            raise ValidationError('不能将记录匹配到自身。')
        if self.matched_post_id and self.matched_post:
            if self.matched_post.post_type == self.post_type:
                raise ValidationError('只有“丢失”和“拾到”记录可以互相匹配。')

    @property
    def is_active_now(self):
        return self.status == 'active' and (
            self.expires_at is None or self.expires_at > timezone.now()
        )

    def __str__(self):
        return f'{self.get_post_type_display()} · {self.title}'


class OpportunityDismissal(models.Model):
    """Temporarily hide a recommendation without mutating its source record."""

    KIND_CHOICES = (
        ('demand', '校园求购'),
        ('lost_found', '失物招领'),
    )

    user = models.ForeignKey(
        User, on_delete=models.CASCADE, related_name='opportunity_dismissals', verbose_name='用户',
    )
    kind = models.CharField('机会类型', max_length=20, choices=KIND_CHOICES)
    demand = models.ForeignKey(
        'DemandPost', on_delete=models.CASCADE, null=True, blank=True,
        related_name='opportunity_dismissals', verbose_name='求购信息',
    )
    lost_found_post = models.ForeignKey(
        LostFoundPost, on_delete=models.CASCADE, null=True, blank=True,
        related_name='opportunity_dismissals', verbose_name='失物招领记录',
    )
    expires_at = models.DateTimeField('忽略截止时间')
    created_at = models.DateTimeField('忽略时间', auto_now_add=True)

    class Meta:
        verbose_name = '互助机会忽略记录'
        verbose_name_plural = '互助机会忽略记录'
        ordering = ['-created_at']
        constraints = [
            models.CheckConstraint(
                condition=(
                    models.Q(kind='demand', demand__isnull=False, lost_found_post__isnull=True)
                    | models.Q(kind='lost_found', demand__isnull=True, lost_found_post__isnull=False)
                ),
                name='opportunity_dismissal_target_matches_kind',
            ),
            models.UniqueConstraint(
                fields=['user', 'demand'],
                condition=models.Q(demand__isnull=False),
                name='unique_opportunity_dismissal_demand',
            ),
            models.UniqueConstraint(
                fields=['user', 'lost_found_post'],
                condition=models.Q(lost_found_post__isnull=False),
                name='unique_opportunity_dismissal_lost_found',
            ),
        ]
        indexes = [
            models.Index(fields=['user', 'expires_at']),
            models.Index(fields=['kind', 'expires_at']),
        ]

    def __str__(self):
        target = self.demand or self.lost_found_post
        return f'{self.user.username} · 暂不展示 · {target}'


class LostFoundLead(models.Model):
    """A private lead sent in response to a lost-and-found post."""

    STATUS_CHOICES = (
        ('pending', '待确认'),
        ('accepted', '已确认'),
        ('rejected', '暂不匹配'),
        ('withdrawn', '已撤回'),
    )

    post = models.ForeignKey(
        LostFoundPost, on_delete=models.CASCADE, related_name='leads', verbose_name='失物招领记录',
    )
    respondent = models.ForeignKey(
        User, on_delete=models.CASCADE, related_name='lost_found_leads', verbose_name='线索提供者',
    )
    related_post = models.ForeignKey(
        LostFoundPost, on_delete=models.SET_NULL, null=True, blank=True,
        related_name='related_leads', verbose_name='关联记录',
    )
    message = models.TextField('线索说明')
    status = models.CharField('处理状态', max_length=12, choices=STATUS_CHOICES, default='pending')
    created_at = models.DateTimeField('提交时间', auto_now_add=True)
    updated_at = models.DateTimeField('更新时间', auto_now=True)

    class Meta:
        verbose_name = '失物招领线索'
        verbose_name_plural = '失物招领线索'
        ordering = ['-created_at']
        constraints = [
            models.UniqueConstraint(
                fields=['post', 'respondent'], name='unique_lost_found_lead_by_user',
            ),
        ]
        indexes = [
            models.Index(fields=['post', 'status', '-created_at']),
            models.Index(fields=['respondent', 'status', '-created_at']),
        ]

    def clean(self):
        if self.post_id and self.respondent_id and self.post.reporter_id == self.respondent_id:
            raise ValidationError('不能向自己发布的失物招领记录提交线索。')
        if self.related_post_id and self.related_post_id == self.post_id:
            raise ValidationError('关联记录不能与目标记录相同。')
        if self.respondent_id and self.related_post_id and self.related_post:
            if self.related_post.reporter_id != self.respondent_id:
                raise ValidationError('关联记录必须是你自己发布的记录。')
        if self.post_id and self.related_post_id and self.related_post:
            if self.related_post.post_type == self.post.post_type:
                raise ValidationError('关联记录必须与目标记录类型相反。')

    def __str__(self):
        return f'{self.post.title} · {self.respondent.username} · {self.get_status_display()}'


class DemandResponse(models.Model):
    """A seller's explicit response to an active purchase demand."""

    STATUS_CHOICES = (
        ('pending', '待处理'),
        ('accepted', '已确认匹配'),
        ('rejected', '未采纳'),
        ('withdrawn', '已撤回'),
    )

    demand = models.ForeignKey(
        DemandPost, on_delete=models.CASCADE, related_name='responses', verbose_name='求购信息',
    )
    item = models.ForeignKey(
        Item, on_delete=models.CASCADE, related_name='demand_responses', verbose_name='响应商品',
    )
    responder = models.ForeignKey(
        User, on_delete=models.CASCADE, related_name='demand_responses', verbose_name='响应人',
    )
    message = models.TextField('响应说明', blank=True)
    match_score = models.PositiveSmallIntegerField('匹配分数', default=0)
    match_reason = models.CharField('匹配依据', max_length=200, blank=True)
    status = models.CharField('响应状态', max_length=20, choices=STATUS_CHOICES, default='pending')
    created_at = models.DateTimeField('响应时间', auto_now_add=True)
    updated_at = models.DateTimeField('更新时间', auto_now=True)

    class Meta:
        verbose_name = '求购响应'
        verbose_name_plural = '求购响应'
        ordering = ['-created_at']
        constraints = [
            models.UniqueConstraint(
                fields=['demand', 'item'], name='unique_demand_response_item',
            ),
        ]
        indexes = [
            models.Index(fields=['demand', 'status', '-created_at']),
            models.Index(fields=['responder', 'status', '-created_at']),
        ]

    def clean(self):
        if self.item_id and self.responder_id and self.item.seller_id != self.responder_id:
            raise ValidationError('求购响应人必须是商品发布者。')
        if self.demand_id and self.responder_id and self.demand.requester_id == self.responder_id:
            raise ValidationError('不能响应自己的求购信息。')

    def __str__(self):
        return f'{self.demand.title} · {self.item.title} · {self.get_status_display()}'
