# messages/models.py
from django.db import models
from django.contrib.auth.models import User
from listings.models import Item

class Comment(models.Model):
    item = models.ForeignKey(Item, on_delete=models.CASCADE, related_name='comments')
    author = models.ForeignKey(User, on_delete=models.CASCADE)
    content = models.TextField()
    created_at = models.DateTimeField(auto_now_add=True)
    
    def __str__(self):
        return f"{self.author.username}对{self.item.title}的评论"


class ModerationEvent(models.Model):
    CHANNEL_CHOICES = (
        ('comment', '商品留言'),
        ('private_message', '私信'),
    )
    STATUS_CHOICES = (
        ('pending', '待复核'),
        ('confirmed', '确认违规'),
        ('dismissed', '误判放行'),
    )
    RISK_LEVEL_CHOICES = (
        ('low', '低风险'),
        ('medium', '中风险'),
        ('high', '高风险'),
    )

    channel = models.CharField('内容渠道', max_length=30, choices=CHANNEL_CHOICES)
    author = models.ForeignKey(
        User, on_delete=models.SET_NULL, null=True, blank=True,
        related_name='moderation_events', verbose_name='内容作者',
    )
    item = models.ForeignKey(
        Item, on_delete=models.SET_NULL, null=True, blank=True,
        related_name='moderation_events', verbose_name='关联商品',
    )
    content = models.TextField('被拦截内容')
    matched_terms = models.CharField('命中规则', max_length=255)
    risk_score = models.PositiveSmallIntegerField('风险分数', default=0)
    risk_level = models.CharField(
        '风险等级', max_length=10, choices=RISK_LEVEL_CHOICES, default='low',
    )
    status = models.CharField('审核状态', max_length=20, choices=STATUS_CHOICES, default='pending')
    reviewed_by = models.ForeignKey(
        User, on_delete=models.SET_NULL, null=True, blank=True,
        related_name='reviewed_moderation_events', verbose_name='复核人',
    )
    reviewed_at = models.DateTimeField('复核时间', null=True, blank=True)
    created_at = models.DateTimeField('拦截时间', auto_now_add=True)

    class Meta:
        verbose_name = '内容审核记录'
        verbose_name_plural = '内容审核记录'
        ordering = ['status', '-risk_score', '-created_at']
        indexes = [
            models.Index(fields=['status', '-risk_score', '-created_at']),
            models.Index(fields=['risk_level', 'status', '-created_at']),
            models.Index(fields=['channel', '-created_at']),
            models.Index(fields=['author', '-created_at']),
        ]

    def __str__(self):
        return f'{self.get_channel_display()} · {self.get_status_display()} · {self.created_at:%Y-%m-%d %H:%M}'

class PrivateMessage(models.Model):
    sender = models.ForeignKey(User, on_delete=models.CASCADE, related_name='sent_messages')
    receiver = models.ForeignKey(User, on_delete=models.CASCADE, related_name='received_messages')
    item = models.ForeignKey(Item, on_delete=models.CASCADE, null=True, blank=True)
    content = models.TextField()
    created_at = models.DateTimeField(auto_now_add=True)
    is_read = models.BooleanField(default=False)
    
    def __str__(self):
        return f"从{self.sender.username}到{self.receiver.username}的私信"
        
    class Meta:
        ordering = ['-created_at']
        indexes = [
            models.Index(
                fields=['receiver', 'is_read', '-created_at'],
                name='pm_receiver_read_created_idx',
            ),
            models.Index(
                fields=['sender', 'receiver', '-created_at'],
                name='pm_sender_receiver_created_idx',
            ),
            models.Index(
                fields=['receiver', 'sender', '-created_at'],
                name='pm_receiver_sender_created_idx',
            ),
        ]