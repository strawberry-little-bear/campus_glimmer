# accounts/models.py
from django.db import models
from django.contrib.auth.models import User
from django.db.models.signals import post_save
from django.dispatch import receiver

class Profile(models.Model):
    user = models.OneToOneField(User, on_delete=models.CASCADE)
    avatar = models.ImageField(upload_to='avatars/', default='avatars/default.png')
    bio = models.TextField(max_length=500, blank=True)
    student_id = models.CharField(max_length=20, blank=True)
    wechat = models.CharField(max_length=50, blank=True)
    phone = models.CharField(max_length=15, blank=True)

    def __str__(self):
        return f'{self.user.username}的个人资料'

@receiver(post_save, sender=User)
def create_user_profile(sender, instance, created, **kwargs):
    if created:
        Profile.objects.create(user=instance)

@receiver(post_save, sender=User)
def save_user_profile(sender, instance, **kwargs):
    instance.profile.save()

class CampusDomain(models.Model):
    domain = models.CharField('校园邮箱域名', max_length=120, unique=True)
    name = models.CharField('学校或组织名称', max_length=120)
    is_active = models.BooleanField('启用', default=True)
    created_at = models.DateTimeField('创建时间', auto_now_add=True)

    class Meta:
        verbose_name = '校园邮箱域名'
        verbose_name_plural = '校园邮箱域名'
        ordering = ['name', 'domain']

    def save(self, *args, **kwargs):
        self.domain = self.domain.strip().lower().lstrip('@')
        super().save(*args, **kwargs)

    def __str__(self):
        return f'{self.name} · @{self.domain}'


class CampusVerification(models.Model):
    STATUS_CHOICES = (
        ('unverified', '未认证'),
        ('pending', '待验证邮箱'),
        ('verified', '已认证'),
        ('rejected', '已失效'),
    )

    user = models.OneToOneField(
        User, on_delete=models.CASCADE, related_name='campus_verification', verbose_name='用户',
    )
    campus_email = models.EmailField('校园邮箱', unique=True)
    domain_name = models.CharField('学校或组织名称', max_length=120, blank=True)
    status = models.CharField('认证状态', max_length=20, choices=STATUS_CHOICES, default='unverified')
    token_hash = models.CharField('验证令牌哈希', max_length=128, blank=True)
    token_issued_at = models.DateTimeField('令牌生成时间', null=True, blank=True)
    verified_at = models.DateTimeField('认证时间', null=True, blank=True)
    created_at = models.DateTimeField('创建时间', auto_now_add=True)
    updated_at = models.DateTimeField('更新时间', auto_now=True)

    class Meta:
        verbose_name = '校园身份认证'
        verbose_name_plural = '校园身份认证'
        ordering = ['-updated_at']
        indexes = [
            models.Index(fields=['status', '-updated_at']),
            models.Index(fields=['campus_email']),
        ]

    @property
    def is_verified(self):
        return self.status == 'verified' and bool(self.verified_at)

    def __str__(self):
        return f'{self.user.username} · {self.get_status_display()}'
