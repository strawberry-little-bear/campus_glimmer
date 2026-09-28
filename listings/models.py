from django.db import models
from django.contrib.auth.models import User


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
    is_active = models.BooleanField('启用', default=True)
    sort_order = models.PositiveIntegerField('排序', default=0)

    def __str__(self):
        return self.name

    class Meta:
        verbose_name = '校园地点'
        verbose_name_plural = '校园地点'
        ordering = ['sort_order', 'name']


class Item(models.Model):
    STATUS_CHOICES = (
        ('available', '在售'),
        ('reserved', '已预订'),
        ('sold', '已售出'),
    )

    title = models.CharField('商品标题', max_length=200)
    description = models.TextField('商品描述')
    price = models.DecimalField('价格', max_digits=10, decimal_places=2)
    category = models.ForeignKey(Category, on_delete=models.CASCADE, related_name='items', verbose_name='分类')
    location = models.ForeignKey(
        CampusLocation,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='items',
        verbose_name='交易地点',
    )
    condition = models.CharField('成色', max_length=100)
    seller = models.ForeignKey(User, on_delete=models.CASCADE, related_name='listed_items', verbose_name='卖家')
    status = models.CharField('状态', max_length=20, choices=STATUS_CHOICES, default='available')
    created_at = models.DateTimeField('发布时间', auto_now_add=True)
    updated_at = models.DateTimeField('更新时间', auto_now=True)

    def __str__(self):
        return self.title

    class Meta:
        ordering = ['-created_at']
        indexes = [
            models.Index(fields=['status', '-created_at']),
            models.Index(fields=['category', 'status']),
            models.Index(fields=['location', 'status']),
        ]


class ItemImage(models.Model):
    item = models.ForeignKey(Item, on_delete=models.CASCADE, related_name='images')
    image = models.ImageField('图片', upload_to='items/')

    def __str__(self):
        return f'{self.item.title}的图片'


class Favorite(models.Model):
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name='favorites')
    item = models.ForeignKey(Item, on_delete=models.CASCADE, related_name='favorites')
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


class Order(models.Model):
    STATUS_CHOICES = (
        ('pending', '待卖家确认'),
        ('confirmed', '卖家已确认'),
        ('meeting', '待当面交付'),
        ('completed', '交易完成'),
        ('cancelled', '已取消'),
    )

    item = models.OneToOneField(Item, on_delete=models.CASCADE, related_name='order', verbose_name='商品')
    buyer = models.ForeignKey(User, on_delete=models.CASCADE, related_name='purchased_orders', verbose_name='买家')
    seller = models.ForeignKey(User, on_delete=models.CASCADE, related_name='sold_orders', verbose_name='卖家')
    meeting_location = models.ForeignKey(
        CampusLocation, on_delete=models.SET_NULL, null=True, blank=True,
        related_name='orders', verbose_name='交付地点',
    )
    agreed_price = models.DecimalField('成交价格', max_digits=10, decimal_places=2)
    buyer_note = models.TextField('买家备注', blank=True)
    status = models.CharField('订单状态', max_length=20, choices=STATUS_CHOICES, default='pending')
    created_at = models.DateTimeField('下单时间', auto_now_add=True)
    updated_at = models.DateTimeField('更新时间', auto_now=True)
    confirmation_deadline = models.DateTimeField('卖家确认截止时间', null=True, blank=True)
    confirmation_reminder_sent_at = models.DateTimeField('确认提醒发送时间', null=True, blank=True)

    class Meta:
        verbose_name = '交易订单'
        verbose_name_plural = '交易订单'
        ordering = ['-created_at']
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
    created_at = models.DateTimeField('创建时间', auto_now_add=True)
    updated_at = models.DateTimeField('更新时间', auto_now=True)

    class Meta:
        verbose_name = '交付确认'
        verbose_name_plural = '交付确认'

    @property
    def is_complete(self):
        return bool(self.buyer_confirmed_at and self.seller_confirmed_at)

    def __str__(self):
        return f'{self.order.item.title} · 交付确认'


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
        ('order_status', '订单状态更新'),
        ('rating_received', '收到交易评价'),
        ('message_received', '收到新私信'),
        ('comment_received', '收到商品留言'),
        ('saved_search_match', '关注的搜索有新商品'),
        ('item_available', '商品重新有货'),
        ('order_dispute', '交易争议更新'),
        ('order_expiring', '交易预约即将超时'),
        ('order_expired', '交易预约已超时'),
        ('report_update', '举报处理更新'),
        ('moderation_update', '内容审核结果'),
        ('operations_digest', '运营告警日报'),
    )

    recipient = models.ForeignKey(User, on_delete=models.CASCADE, related_name='notifications', verbose_name='接收人')
    actor = models.ForeignKey(
        User, on_delete=models.SET_NULL, null=True, blank=True,
        related_name='triggered_notifications', verbose_name='触发人',
    )
    order = models.ForeignKey(Order, on_delete=models.CASCADE, null=True, blank=True, related_name='notifications', verbose_name='相关订单')
    item = models.ForeignKey(Item, on_delete=models.CASCADE, null=True, blank=True, related_name='notifications', verbose_name='相关商品')
    kind = models.CharField('通知类型', max_length=30, choices=KIND_CHOICES)
    title = models.CharField('通知标题', max_length=120)
    message = models.CharField('通知内容', max_length=255)
    target_url = models.CharField('跳转地址', max_length=255, blank=True)
    is_read = models.BooleanField('已读', default=False)
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
        ]

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
    order_dispute = models.BooleanField(
        '交易争议更新', default=True, help_text='交易争议状态发生变化时提醒。',
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
        ]

    def __str__(self):
        return f'{self.user.username} · {self.name}'
