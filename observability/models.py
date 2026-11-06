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