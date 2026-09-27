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
