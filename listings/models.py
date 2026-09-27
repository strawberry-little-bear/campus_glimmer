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
        ('order_dispute', '交易争议更新'),
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
    created_at = models.DateTimeField('创建时间', auto_now_add=True)

    class Meta:
        verbose_name = '站内通知'
        verbose_name_plural = '站内通知'
        ordering = ['-created_at']
        indexes = [
            models.Index(fields=['recipient', 'is_read', '-created_at']),
            models.Index(fields=['recipient', '-created_at']),
        ]

    def __str__(self):
        return f'{self.recipient.username} · {self.title}'


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
