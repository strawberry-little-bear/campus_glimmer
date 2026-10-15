# messages/models.py
from datetime import timedelta

from django.db import models
from django.contrib.auth.models import User
from django.utils import timezone
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
    RISK_TIER_CHOICES = (
        ('critical', '严重风险'),
        ('elevated', '脱离平台联系'),
        ('caution', '低权重线索'),
    )
    RISK_TIER_REVIEW_TARGETS = {
        'critical': timedelta(hours=4),
        'elevated': timedelta(hours=12),
        'caution': timedelta(hours=48),
    }

    channel = models.CharField('内容渠道', max_length=30, choices=CHANNEL_CHOICES)
    author = models.ForeignKey(
        User, on_delete=models.SET_NULL, null=True, blank=True,
        related_name='moderation_events', verbose_name='内容作者',
    )
    item = models.ForeignKey(
        Item, on_delete=models.SET_NULL, null=True, blank=True,
        related_name='moderation_events', verbose_name='关联商品',
    )
    receiver = models.ForeignKey(
        User, on_delete=models.SET_NULL, null=True, blank=True,
        related_name='received_moderation_events', verbose_name='私信接收人',
        help_text='误判放行时要恢复发送的接收人；历史记录没有这一列的值，不能自动补发。',
    )
    content = models.TextField('被拦截内容')
    matched_terms = models.CharField('命中规则', max_length=255)
    risk_score = models.PositiveSmallIntegerField('风险分数', default=0)
    risk_level = models.CharField(
        '风险等级', max_length=10, choices=RISK_LEVEL_CHOICES, default='low',
    )
    risk_tier = models.CharField(
        '复核分级', max_length=20, choices=RISK_TIER_CHOICES, default='caution',
        help_text='拦截时由命中的最高规则档位写入；只影响人工复核节奏，不是违规结论。',
    )
    status = models.CharField('审核状态', max_length=20, choices=STATUS_CHOICES, default='pending')
    reviewed_by = models.ForeignKey(
        User, on_delete=models.SET_NULL, null=True, blank=True,
        related_name='reviewed_moderation_events', verbose_name='复核人',
    )
    reviewed_at = models.DateTimeField('复核时间', null=True, blank=True)
    released_at = models.DateTimeField(
        '误判放行时间', null=True, blank=True,
        help_text='内容真的恢复到原渠道的时刻；历史记录缺少恢复材料时保持为空。',
    )
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

    @property
    def review_target_at(self):
        """按分级给出的内部复核目标时间，不是对外承诺的 SLA。"""
        target = self.RISK_TIER_REVIEW_TARGETS.get(self.risk_tier)
        return self.created_at + target if self.created_at and target else None

    @property
    def review_target_hours(self):
        target = self.RISK_TIER_REVIEW_TARGETS.get(self.risk_tier)
        return target.days * 24 + target.seconds // 3600 if target else None

    @property
    def review_timing_state(self):
        """待复核记录相对目标时间的节奏：正常、即将超时或已超时。"""
        target = self.review_target_at
        if self.status != 'pending' or target is None:
            return None
        now = timezone.now()
        if now >= target:
            return 'overdue'
        if now >= target - (target - self.created_at) / 4:
            return 'due_soon'
        return 'on_time'

    @property
    def has_release_material(self):
        """误判放行时是否具备按原渠道恢复的最低材料。"""
        if not self.author_id:
            return False
        if self.channel == 'comment':
            return bool(self.item_id)
        if self.channel == 'private_message':
            return bool(self.receiver_id)
        return False

    def __str__(self):
        return f'{self.get_channel_display()} · {self.get_status_display()} · {self.created_at:%Y-%m-%d %H:%M}'

class ModerationAppeal(models.Model):
    """确认违规后的单次人工申诉与处理留痕。"""

    REASON_CHOICES = (
        ('not_violation', '内容不违规'),
        ('missing_context', '缺少语境'),
        ('other', '其他需要补充的情况'),
    )
    STATUS_CHOICES = (
        ('pending', '待处理'),
        ('upheld', '维持原判'),
        ('accepted', '申诉成立并恢复内容'),
        ('accepted_without_restore', '申诉成立但缺少恢复材料'),
    )

    event = models.OneToOneField(
        ModerationEvent, on_delete=models.CASCADE, related_name='appeal',
        verbose_name='审核事件',
    )
    appellant = models.ForeignKey(
        User, on_delete=models.CASCADE, related_name='moderation_appeals',
        verbose_name='申诉人',
    )
    reason = models.CharField('申诉原因', max_length=30, choices=REASON_CHOICES)
    statement = models.TextField('申诉说明', max_length=1000)
    submitted_at = models.DateTimeField('提交时间', auto_now_add=True)
    status = models.CharField('处理状态', max_length=30, choices=STATUS_CHOICES, default='pending')
    original_reviewed_by = models.ForeignKey(
        User, on_delete=models.SET_NULL, null=True, blank=True,
        related_name='original_moderation_appeals', verbose_name='原复核人快照',
    )
    original_reviewed_at = models.DateTimeField('原复核时间快照', null=True, blank=True)
    resolved_by = models.ForeignKey(
        User, on_delete=models.SET_NULL, null=True, blank=True,
        related_name='resolved_moderation_appeals', verbose_name='申诉处理人',
    )
    resolved_at = models.DateTimeField('申诉处理时间', null=True, blank=True)
    resolution_note = models.TextField('处理说明', blank=True)

    class Meta:
        verbose_name = '内容申诉'
        verbose_name_plural = '内容申诉'
        ordering = ['status', '-submitted_at', '-id']
        indexes = [models.Index(fields=['status', '-submitted_at'])]

    def __str__(self):
        return f'{self.appellant_id} 对审核事件 {self.event_id} 的申诉'


class PrivateMessage(models.Model):
    sender = models.ForeignKey(User, on_delete=models.CASCADE, related_name='sent_messages')
    receiver = models.ForeignKey(User, on_delete=models.CASCADE, related_name='received_messages')
    item = models.ForeignKey(Item, on_delete=models.CASCADE, null=True, blank=True)
    content = models.TextField()
    created_at = models.DateTimeField(auto_now_add=True)
    is_read = models.BooleanField(default=False)
    read_at = models.DateTimeField(
        null=True, blank=True,
        help_text='第一次被标记为已读的时间，用于统计私信的实际响应时效。',
    )

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
            models.Index(
                fields=['receiver', 'is_read', 'read_at'],
                name='pm_receiver_read_read_at_idx',
            ),
        ]