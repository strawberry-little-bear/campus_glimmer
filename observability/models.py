from django.db import models


class AlertTransition(models.Model):
    """一次真实告警状态翻转的持久化排障记录。"""

    FIRING = 'firing'
    RESOLVED = 'resolved'
    EVENT_CHOICES = (
        (FIRING, '触发'),
        (RESOLVED, '恢复'),
    )

    event = models.CharField(max_length=20, choices=EVENT_CHOICES, verbose_name='翻转事件')
    rule_name = models.CharField(max_length=120, verbose_name='告警规则')
    severity = models.CharField(max_length=20, verbose_name='严重程度')
    summary = models.CharField(max_length=255, verbose_name='摘要')
    state = models.CharField(max_length=20, verbose_name='翻转后状态')
    value = models.FloatField(blank=True, null=True, verbose_name='观测值')
    detail = models.TextField(blank=True, verbose_name='判断依据')
    since = models.FloatField(blank=True, null=True, verbose_name='状态起点')
    occurred_at = models.FloatField(verbose_name='翻转时间戳')
    active_seconds = models.FloatField(default=0.0, verbose_name='持续秒数')
    instance = models.CharField(max_length=255, verbose_name='产生记录的实例')
    recorded_at = models.DateTimeField(auto_now_add=True, verbose_name='写入时间')

    class Meta:
        ordering = ['-occurred_at', '-id']
        indexes = [
            models.Index(fields=['-occurred_at', '-id'], name='obs_alert_occurred_idx'),
            models.Index(fields=['event', '-occurred_at'], name='obs_alert_event_idx'),
        ]
        verbose_name = '告警翻转记录'
        verbose_name_plural = '告警翻转记录'

    def __str__(self):
        return f'{self.rule_name} {self.event} @ {self.occurred_at}'

    def as_event(self):
        return {
            'event': self.event,
            'name': self.rule_name,
            'severity': self.severity,
            'summary': self.summary,
            'state': self.state,
            'value': self.value,
            'detail': self.detail,
            'since': self.since,
            'occurred_at': self.occurred_at,
            'active_seconds': self.active_seconds,
            'instance': self.instance,
        }


class AlertNotificationDelivery(models.Model):
    """一条等待 Webhook 投递的告警翻转事件。

    队列只负责把真实状态翻转交到外部通知渠道，不改变告警判断。去重键由
    ``rule + event + 时间窗口`` 生成，多副本同时观察同一次翻转时只会留下
    一行；行本身也是重试台账，失败次数、下次尝试时间与最终耗尽原因都可查。
    """

    PENDING = 'pending'
    DELIVERING = 'delivering'
    DELIVERED = 'delivered'
    SKIPPED = 'skipped'
    EXHAUSTED = 'exhausted'
    STATUS_CHOICES = (
        (PENDING, '等待投递'),
        (DELIVERING, '投递中'),
        (DELIVERED, '已投递'),
        (SKIPPED, '已跳过'),
        (EXHAUSTED, '重试耗尽'),
    )

    dedupe_key = models.CharField(max_length=128, unique=True, verbose_name='去重键')
    event = models.CharField(max_length=20, verbose_name='翻转事件')
    rule_name = models.CharField(max_length=120, verbose_name='告警规则')
    state = models.CharField(max_length=20, verbose_name='翻转后状态')
    payload = models.JSONField(verbose_name='投递载荷')
    status = models.CharField(
        max_length=20, choices=STATUS_CHOICES, default=PENDING,
        db_index=True, verbose_name='投递状态',
    )
    attempts = models.PositiveIntegerField(default=0, verbose_name='已尝试次数')
    next_attempt_at = models.DateTimeField(db_index=True, verbose_name='下次尝试时间')
    last_attempt_at = models.DateTimeField(blank=True, null=True, verbose_name='最近尝试时间')
    delivered_at = models.DateTimeField(blank=True, null=True, verbose_name='送达时间')
    last_error = models.CharField(max_length=255, blank=True, verbose_name='最近错误')
    created_at = models.DateTimeField(auto_now_add=True, verbose_name='入队时间')
    updated_at = models.DateTimeField(auto_now=True, verbose_name='更新时间')

    class Meta:
        ordering = ['next_attempt_at', 'id']
        indexes = [
            models.Index(
                fields=['status', 'next_attempt_at'],
                name='obs_delivery_due_idx',
            ),
            models.Index(fields=['-created_at', '-id'], name='obs_delivery_created_idx'),
        ]
        verbose_name = '告警通知投递'
        verbose_name_plural = '告警通知投递'

    def __str__(self):
        return f'{self.rule_name} {self.event} ({self.status})'
