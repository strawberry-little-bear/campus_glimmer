from datetime import timedelta
import secrets

from django.conf import settings
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.contrib.auth.hashers import check_password, make_password
from django.contrib.auth.models import User
from django.core.mail import send_mail
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.utils.encoding import force_bytes, force_str
from django.utils.http import urlsafe_base64_decode, urlsafe_base64_encode

from .forms import CampusVerificationForm, ProfileUpdateForm, UserRegisterForm, UserUpdateForm
from .models import CampusVerification
from chat_messages.models import PrivateMessage

from listings.models import Item, Rating
from listings.reputation import build_user_reputation


def register(request):
    if request.method == 'POST':
        form = UserRegisterForm(request.POST)
        if form.is_valid():
            form.save()
            messages.success(request, '账户已创建，现在您可以登录了！')
            return redirect('login')
    else:
        form = UserRegisterForm()
    return render(request, 'accounts/register.html', {'form': form})


@login_required
def profile(request):
    unread_messages_count = PrivateMessage.objects.filter(
        receiver=request.user,
        is_read=False,
    ).count()
    context = {
        'unread_messages_count': unread_messages_count,
        'campus_verification': CampusVerification.objects.filter(user=request.user).first(),
    }
    return render(request, 'accounts/profile.html', context)


def public_profile(request, user_id):
    user = get_object_or_404(User.objects.select_related('profile'), id=user_id)
    reputation = build_user_reputation(user)
    active_items = list(
        Item.objects.available().filter(seller=user)
        .select_related('category', 'location')
        .prefetch_related('images')[:8]
    )
    ratings = Rating.objects.filter(ratee=user).select_related('rater').order_by('-created_at')[:8]
    verification = CampusVerification.objects.filter(
        user=user, status='verified', verified_at__isnull=False,
    ).first()
    return render(request, 'accounts/public_profile.html', {
        'profile_user': user,
        'reputation': reputation,
        'active_items': active_items,
        'ratings': ratings,
        'campus_verification': verification,
        'is_self': request.user.is_authenticated and request.user == user,
    })


@login_required
def edit_profile(request):
    if request.method == 'POST':
        u_form = UserUpdateForm(request.POST, instance=request.user)
        p_form = ProfileUpdateForm(request.POST, request.FILES, instance=request.user.profile)
        if u_form.is_valid() and p_form.is_valid():
            u_form.save()
            p_form.save()
            messages.success(request, '您的个人资料已更新！')
            return redirect('profile')
    else:
        u_form = UserUpdateForm(instance=request.user)
        p_form = ProfileUpdateForm(instance=request.user.profile)

    return render(request, 'accounts/edit_profile.html', {
        'u_form': u_form,
        'p_form': p_form,
    })


@login_required
def campus_verification(request):
    verification = CampusVerification.objects.filter(user=request.user).first()
    if verification and verification.is_verified:
        return render(request, 'accounts/campus_verification.html', {
            'verification': verification,
            'form': None,
            'title': '校园身份认证',
        })

    if request.method == 'POST':
        form = CampusVerificationForm(request.POST, instance=verification, user=request.user)
        if form.is_valid():
            verification = form.save(commit=False)
            verification.user = request.user
            verification.domain_name = form.domain_record.name
            verification.status = 'pending'
            raw_token = secrets.token_urlsafe(32)
            verification.token_hash = make_password(raw_token)
            verification.token_issued_at = timezone.now()
            verification.verified_at = None
            verification.save()
            uidb64 = urlsafe_base64_encode(force_bytes(request.user.pk))
            verification_url = request.build_absolute_uri(
                reverse('verify_campus_email', args=[uidb64, raw_token]),
            )
            send_mail(
                subject='拾光校园：请验证你的校园邮箱',
                message=(
                    f'你好，{request.user.username}。请在 24 小时内打开以下链接完成校园身份认证：\n\n'
                    f'{verification_url}\n\n如果这不是你的操作，请忽略此邮件。'
                ),
                from_email=getattr(settings, 'DEFAULT_FROM_EMAIL', 'no-reply@campus-glimmer.local'),
                recipient_list=[verification.campus_email],
                fail_silently=False,
            )
            messages.success(request, '验证邮件已发送，请前往校园邮箱完成认证。')
            return redirect('campus_verification')
    else:
        form = CampusVerificationForm(instance=verification, user=request.user)

    return render(request, 'accounts/campus_verification.html', {
        'verification': verification,
        'form': form,
        'title': '校园身份认证',
    })


def verify_campus_email(request, uidb64, token):
    try:
        user_id = force_str(urlsafe_base64_decode(uidb64))
        user = User.objects.get(pk=user_id)
    except (TypeError, ValueError, OverflowError, User.DoesNotExist):
        messages.error(request, '校园邮箱验证链接无效。')
        return redirect('login')

    verification = get_object_or_404(CampusVerification, user=user)
    expired = (
        not verification.token_issued_at
        or verification.token_issued_at < timezone.now() - timedelta(hours=24)
    )
    if verification.status != 'pending' or expired or not check_password(token, verification.token_hash):
        messages.error(request, '校园邮箱验证链接无效或已过期，请重新发送验证邮件。')
        if request.user.is_authenticated and request.user == user:
            return redirect('campus_verification')
        return redirect('login')

    verification.status = 'verified'
    verification.verified_at = timezone.now()
    verification.token_hash = ''
    verification.save(update_fields=['status', 'verified_at', 'token_hash', 'updated_at'])
    messages.success(request, '校园身份认证成功，其他同学会看到你的认证标识。')
    return redirect('profile' if request.user.is_authenticated and request.user == user else 'login')
