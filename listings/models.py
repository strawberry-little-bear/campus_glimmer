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
