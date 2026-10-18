# chat_messages/views.py (原messages/views.py)
from django.shortcuts import render, redirect, get_object_or_404
from django.utils import timezone
from django.utils.http import url_has_allowed_host_and_scheme
from django.urls import reverse
from django.contrib.auth.decorators import login_required
from django.views.decorators.http import require_POST
from django.core.exceptions import PermissionDenied
from django.core.paginator import Paginator
from django.contrib import messages as django_messages
from django.contrib.auth.models import User
from django.db import transaction
from django.db.models.functions import Coalesce
from django.db.models import Case, Count, IntegerField, OuterRef, Q, Subquery, Value, When
from .models import Comment, ModerationAppeal, ModerationEvent, PrivateMessage
from .moderation import moderate_submission
from .forms import CommentForm, ModerationAppealForm, PrivateMessageForm
from listings.models import Item
from listings.notifications import create_notification

@login_required
def add_comment(request, item_id):
    item = get_object_or_404(Item, id=item_id)

    if request.method == 'POST':
        form = CommentForm(request.POST)
        if form.is_valid():
            moderation_event = moderate_submission(
                form.cleaned_data['content'], author=request.user, channel='comment', item=item,
            )
            if moderation_event:
                django_messages.warning(request, '这条留言包含需要复核的内容，暂未发布。请不要在站内交换外部联系方式或进行私下转账。')
                return redirect('item_detail', item_id=item.id)
            comment = form.save(commit=False)
            comment.item = item
            comment.author = request.user
            comment.save()
            if item.seller != request.user:
                create_notification(
                    item.seller, actor=request.user, kind='comment_received',
                    title='商品收到新的留言',
                    message=f'{request.user.username}评论了你的商品“{item.title}”。',
                    item=item, target_url=reverse('item_detail', args=[item.id]),
                    dedupe_key=f'comment:{item.id}:{request.user.id}',
                )
            django_messages.success(request, '评论已发布！')
            return redirect('item_detail', item_id=item.id)
    else:
        form = CommentForm()

    return redirect('item_detail', item_id=item.id)

def _mark_messages_read(queryset):
    """把一组未读私信标为已读，并记录第一次被读的时刻。

    四条写路径（逐条、全部、选中、整个会话）必须用同一个时刻，
    否则同一次“用户动手”会被拆成几个毫秒级别的时刻，统计口径就不一致了。
    """
    read_at = timezone.now()
    target_ids = list(queryset.values_list('id', flat=True))
    if not target_ids:
        return 0
    return queryset.update(is_read=True, read_at=read_at)


def _build_conversation_data(user, conversation_users):
    """Build the inbox conversation list with a bounded number of queries."""
    conversation_messages = PrivateMessage.objects.filter(
        Q(sender=OuterRef('pk'), receiver=user)
        | Q(sender=user, receiver=OuterRef('pk'))
    ).order_by('-created_at')
    sent_messages = PrivateMessage.objects.filter(sender=OuterRef('pk'), receiver=user)
    received_messages = PrivateMessage.objects.filter(receiver=OuterRef('pk'), sender=user)
    sent_count = sent_messages.order_by().values('sender').annotate(
        total=Count('id'),
    ).values('total')[:1]
    received_count = received_messages.order_by().values('receiver').annotate(
        total=Count('id'),
    ).values('total')[:1]
    unread_count = sent_messages.filter(is_read=False).order_by().values('sender').annotate(
        total=Count('id'),
    ).values('total')[:1]
    conversation_users = conversation_users.select_related('profile').annotate(
        latest_message_id=Subquery(conversation_messages.values('id')[:1]),
        latest_message_at=Subquery(conversation_messages.values('created_at')[:1]),
        message_count=(
            Coalesce(Subquery(sent_count, output_field=IntegerField()), Value(0))
            + Coalesce(Subquery(received_count, output_field=IntegerField()), Value(0))
        ),
        unread_count=Coalesce(
            Subquery(unread_count, output_field=IntegerField()), Value(0),
        ),
    ).order_by('-latest_message_at', 'username')

    conversation_users = list(conversation_users)
    latest_message_ids = [
        row.latest_message_id for row in conversation_users if row.latest_message_id
    ]
    latest_messages = PrivateMessage.objects.select_related(
        'sender', 'receiver', 'item',
    ).in_bulk(latest_message_ids)
    return [
        {
            'user': row,
            'last_message': latest_messages.get(row.latest_message_id),
            'unread_count': row.unread_count,
            'message_count': row.message_count,
        }
        for row in conversation_users
    ]


@login_required
def inbox(request):
    search_query = request.GET.get('q', '').strip()[:120]
    status_filter = request.GET.get('status', 'all').strip()
    if status_filter not in {'all', 'unread', 'read'}:
        status_filter = 'all'

    received_messages = PrivateMessage.objects.filter(
        receiver=request.user,
    ).select_related('sender', 'item').order_by('-created_at')
    sent_messages = PrivateMessage.objects.filter(
        sender=request.user,
    ).select_related('receiver', 'item').order_by('-created_at')

    if search_query:
        received_messages = received_messages.filter(
            Q(content__icontains=search_query)
            | Q(sender__username__icontains=search_query)
            | Q(item__title__icontains=search_query)
        )
        sent_messages = sent_messages.filter(
            Q(content__icontains=search_query)
            | Q(receiver__username__icontains=search_query)
            | Q(item__title__icontains=search_query)
        )
    if status_filter == 'unread':
        received_messages = received_messages.filter(is_read=False)
    elif status_filter == 'read':
        received_messages = received_messages.filter(is_read=True)

    received_page = Paginator(received_messages, 20).get_page(
        request.GET.get('received_page', 1),
    )
    sent_page = Paginator(sent_messages, 20).get_page(
        request.GET.get('sent_page', 1),
    )

    conversation_users = User.objects.filter(
        Q(sent_messages__receiver=request.user) | Q(received_messages__sender=request.user)
    ).distinct().exclude(id=request.user.id)
    if search_query:
        conversation_users = conversation_users.filter(
            Q(username__icontains=search_query)
            | Q(sent_messages__receiver=request.user, sent_messages__content__icontains=search_query)
            | Q(received_messages__sender=request.user, received_messages__content__icontains=search_query)
            | Q(sent_messages__receiver=request.user, sent_messages__item__title__icontains=search_query)
            | Q(received_messages__sender=request.user, received_messages__item__title__icontains=search_query)
        ).distinct()

    conversation_data = _build_conversation_data(request.user, conversation_users)
    context = {
        'received_messages': received_messages,
        'sent_messages': sent_messages,
        'conversation_data': conversation_data,
        'received_page': received_page,
        'sent_page': sent_page,
        'search_query': search_query,
        'message_status_filter': status_filter,
        'message_status_options': (
            ('all', '全部收到的消息'),
            ('unread', '仅看未读'),
            ('read', '仅看已读'),
        ),
        'message_filtered_unread_count': received_messages.filter(is_read=False).count(),
        'message_filtered_total_count': received_page.paginator.count,
        'message_total_count': PrivateMessage.objects.filter(receiver=request.user).count(),
        'conversation_unread_count': sum(1 for data in conversation_data if data['unread_count']),
        'message_filter_query': request.GET.copy(),
    }
    context['message_filter_query'].pop('received_page', None)
    context['message_filter_query'].pop('sent_page', None)
    return render(request, 'chat_messages/inbox.html', context)


@login_required
def mark_all_messages_read(request):
    if request.method == 'POST':
        updated_count = _mark_messages_read(PrivateMessage.objects.filter(
            receiver=request.user,
            is_read=False,
        ))
        if updated_count:
            django_messages.success(request, f'已将 {updated_count} 条私信标记为已读。')
        else:
            django_messages.info(request, '当前没有未读私信。')
    return redirect('inbox')


@login_required
def mark_message_read(request, message_id):
    """Mark one received message as read and return to the originating feed."""
    private_message = get_object_or_404(
        PrivateMessage, id=message_id, receiver=request.user,
    )
    if request.method == 'POST' and not private_message.is_read:
        private_message.is_read = True
        private_message.read_at = timezone.now()
        private_message.save(update_fields=['is_read', 'read_at'])

    next_url = request.POST.get('next', '').strip()
    if not next_url or not url_has_allowed_host_and_scheme(
        next_url,
        allowed_hosts={request.get_host()},
        require_https=request.is_secure(),
    ):
        next_url = reverse('inbox')
    return redirect(next_url)


@login_required
def mark_selected_messages_read(request):
    """Mark only the messages selected in the inbox as read."""
    if request.method == 'POST':
        message_ids = []
        for raw_id in request.POST.getlist('message_ids'):
            try:
                message_ids.append(int(raw_id))
            except (TypeError, ValueError):
                continue
        updated_count = _mark_messages_read(PrivateMessage.objects.filter(
            id__in=message_ids,
            receiver=request.user,
            is_read=False,
        ))
        if updated_count:
            django_messages.success(request, f'已将 {updated_count} 条选中私信标记为已读。')
        else:
            django_messages.info(request, '请选择至少一条未读私信。')
    return redirect('inbox')


@login_required
def mark_conversation_read(request, user_id):
    """Mark one sender's unread messages as read without touching other conversations."""
    other_user = get_object_or_404(User, id=user_id)
    if request.method == 'POST':
        updated_count = _mark_messages_read(PrivateMessage.objects.filter(
            sender=other_user,
            receiver=request.user,
            is_read=False,
        ))
        if updated_count:
            django_messages.success(request, f'已读完与 {other_user.username} 的未读私信。')
        else:
            django_messages.info(request, '这个会话当前没有未读私信。')
    return redirect('conversation', user_id=other_user.id)

@login_required
def send_message(request, receiver_id, item_id=None):
    receiver = get_object_or_404(User, id=receiver_id)
    item = get_object_or_404(Item, id=item_id) if item_id else None

    if request.method == 'POST':
        form = PrivateMessageForm(request.POST)
        if form.is_valid():
            moderation_event = moderate_submission(
                form.cleaned_data['content'], author=request.user,
                channel='private_message', item=item, receiver=receiver,
            )
            if moderation_event:
                form.add_error('content', '消息包含需要复核的内容，暂未发送。请修改后再试。')
            else:
                message = form.save(commit=False)
                message.sender = request.user
                message.receiver = receiver
                message.item = item
                message.save()
                create_notification(
                    receiver, actor=request.user, kind='message_received',
                    title='收到新的私信',
                    message=f'{request.user.username}给你发来了一条新消息。',
                    item=item, target_url=reverse('conversation', args=[request.user.id]),
                    dedupe_key=f'message:{request.user.id}',
                )
                django_messages.success(request, '消息已发送！')

                if item:
                    return redirect('item_detail', item_id=item.id)
                return redirect('conversation', user_id=receiver.id)
    else:
        form = PrivateMessageForm()

    context = {
        'form': form,
        'receiver': receiver,
        'item': item
    }
    # 修改这里的模板路径
    return render(request, 'chat_messages/send_message.html', context)

@login_required
def conversation(request, user_id):
    other_user = get_object_or_404(User, id=user_id)

    # 获取与特定用户的所有对话
    messages_queryset = PrivateMessage.objects.filter(
        (Q(sender=request.user) & Q(receiver=other_user)) |
        (Q(sender=other_user) & Q(receiver=request.user))
    ).select_related('sender', 'receiver', 'item').order_by('created_at')

    # 打开会话时一次性更新未读状态，避免逐条保存造成额外查询。
    _mark_messages_read(messages_queryset.filter(receiver=request.user, is_read=False))
    messages_page = Paginator(messages_queryset, 30).get_page(request.GET.get('page', 1))

    # 发送新消息的表单
    if request.method == 'POST':
        form = PrivateMessageForm(request.POST)
        if form.is_valid():
            moderation_event = moderate_submission(
                form.cleaned_data['content'], author=request.user,
                channel='private_message', receiver=other_user,
            )
            if moderation_event:
                form.add_error('content', '消息包含需要复核的内容，暂未发送。请修改后再试。')
            else:
                message = form.save(commit=False)
                message.sender = request.user
                message.receiver = other_user
                message.save()
                create_notification(
                    other_user, actor=request.user, kind='message_received',
                    title='收到新的私信',
                    message=f'{request.user.username}给你发来了一条新消息。',
                    target_url=reverse('conversation', args=[request.user.id]),
                    dedupe_key=f'message:{request.user.id}',
                )
                django_messages.success(request, '消息已发送！')
                return redirect('conversation', user_id=other_user.id)
    else:
        form = PrivateMessageForm()

    context = {
        'other_user': other_user,
        'messages_list': messages_page.object_list,
        'conversation_page': messages_page,
        'form': form
    }
    # 修改这里的模板路径
    return render(request, 'chat_messages/conversation.html', context)

@login_required
def moderation_queue(request):
    if not request.user.is_staff:
        raise PermissionDenied

    status_filter = request.GET.get('status', 'pending').strip()
    if status_filter not in {'all', 'pending', 'confirmed', 'dismissed'}:
        status_filter = 'pending'
    channel_filter = request.GET.get('channel', 'all').strip()
    if channel_filter not in {'all', 'comment', 'private_message'}:
        channel_filter = 'all'
    risk_filter = request.GET.get('risk', 'all').strip()
    if risk_filter not in {'all', 'low', 'medium', 'high'}:
        risk_filter = 'all'
    tier_filter = request.GET.get('tier', 'all').strip()
    if tier_filter not in {'all', 'critical', 'elevated', 'caution'}:
        tier_filter = 'all'
    appeal_filter = request.GET.get('appeal', 'all').strip()
    if appeal_filter not in {
        'all', 'pending', 'upheld', 'accepted', 'accepted_without_restore',
    }:
        appeal_filter = 'all'
    release_filter = request.GET.get('release', 'all').strip()
    if release_filter not in {'all', 'closed', 'unreleased', 'missing_material'}:
        release_filter = 'all'
    if 'status' not in request.GET and (
        release_filter in {'closed', 'unreleased'} or appeal_filter != 'all'
    ):
        status_filter = 'all'
    search_query = request.GET.get('q', '').strip()[:120]

    base_events = ModerationEvent.objects.all()
    events = base_events.select_related(
        'author', 'item__seller', 'receiver', 'reviewed_by',
        'appeal__appellant', 'appeal__original_reviewed_by', 'appeal__resolved_by',
    )
    if status_filter != 'all':
        events = events.filter(status=status_filter)
    if channel_filter != 'all':
        events = events.filter(channel=channel_filter)
    if risk_filter != 'all':
        events = events.filter(risk_level=risk_filter)
    if tier_filter != 'all':
        events = events.filter(risk_tier=tier_filter)
    if appeal_filter != 'all':
        events = events.filter(appeal__status=appeal_filter)
    if release_filter == 'closed':
        events = events.filter(status='dismissed', released_at__isnull=False)
    elif release_filter == 'unreleased':
        events = events.filter(status='dismissed', released_at__isnull=True)
    elif release_filter == 'missing_material':
        events = events.filter(status='pending').filter(
            Q(author__isnull=True)
            | Q(channel='comment', item__isnull=True)
            | Q(channel='private_message', receiver__isnull=True)
        )
    if search_query:
        events = events.filter(
            Q(content__icontains=search_query)
            | Q(matched_terms__icontains=search_query)
            | Q(author__username__icontains=search_query)
            | Q(item__title__icontains=search_query)
        )

    events = events.order_by(
        Case(
            When(appeal__status='pending', then=Value(0)), default=Value(1),
            output_field=IntegerField(),
        ),
        Case(
            When(status='pending', then=Value(0)), default=Value(1),
            output_field=IntegerField(),
        ),
        Case(
            When(risk_tier='critical', then=Value(0)),
            When(risk_tier='elevated', then=Value(1)),
            When(risk_tier='caution', then=Value(2)),
            default=Value(3),
            output_field=IntegerField(),
        ),
        '-risk_score', '-created_at',
    )
    now = timezone.now()
    tier_counts = {
        tier: base_events.filter(status='pending', risk_tier=tier).count()
        for tier in ('critical', 'elevated', 'caution')
    }
    overdue_counts = {
        tier: base_events.filter(
            status='pending', risk_tier=tier,
            created_at__lt=now - ModerationEvent.RISK_TIER_REVIEW_TARGETS[tier],
        ).count()
        for tier in ('critical', 'elevated', 'caution')
    }

    return render(request, 'chat_messages/moderation_queue.html', {
        'moderation_events': events,
        'moderation_pending_count': base_events.filter(status='pending').count(),
        'moderation_total_count': base_events.count(),
        'moderation_status_filter': status_filter,
        'moderation_channel_filter': channel_filter,
        'moderation_risk_filter': risk_filter,
        'moderation_tier_filter': tier_filter,
        'moderation_appeal_filter': appeal_filter,
        'moderation_search_query': search_query,
        'moderation_appeal_pending_count': base_events.filter(
            appeal__status='pending',
        ).count(),
        'moderation_appeal_resolved_count': base_events.exclude(
            appeal__isnull=True,
        ).exclude(appeal__status='pending').count(),
        'moderation_pending_high_count': base_events.filter(status='pending', risk_level='high').count(),
        'moderation_released_count': base_events.filter(
            status='dismissed', released_at__isnull=False,
        ).count(),
        'moderation_unreleased_count': base_events.filter(
            status='dismissed', released_at__isnull=True,
        ).count(),
        'moderation_missing_material_count': base_events.filter(status='pending').filter(
            Q(author__isnull=True)
            | Q(channel='comment', item__isnull=True)
            | Q(channel='private_message', receiver__isnull=True)
        ).count(),
        'moderation_release_filter': release_filter,
        'moderation_release_options': (
            ('all', '全部放行状态'),
            ('closed', '误判已恢复'),
            ('unreleased', '误判未恢复'),
            ('missing_material', '待复核缺材料'),
        ),
        'moderation_status_options': (
            ('pending', '待复核'), ('confirmed', '确认违规'),
            ('dismissed', '误判放行'), ('all', '全部记录'),
        ),
        'moderation_channel_options': (
            ('all', '全部渠道'), ('comment', '商品留言'), ('private_message', '私信'),
        ),
        'moderation_risk_options': (
            ('all', '全部风险等级'), ('high', '高风险'),
            ('medium', '中风险'), ('low', '低风险'),
        ),
        'moderation_tier_options': (
            ('all', '全部复核分级'), ('critical', '严重风险'),
            ('elevated', '脱离平台联系'), ('caution', '低权重线索'),
        ),
        'moderation_appeal_options': (
            ('all', '全部申诉状态'), ('pending', '待处理申诉'),
            ('upheld', '维持原判'), ('accepted', '申诉成立并恢复'),
            ('accepted_without_restore', '成立但未恢复'),
        ),
        **{f'moderation_pending_{tier}_count': count for tier, count in tier_counts.items()},
        **{f'moderation_overdue_{tier}_count': count for tier, count in overdue_counts.items()},
    })


def _release_moderated_content(event, *, released_at):
    """把人工判定误判的内容恢复到它原本要进入的渠道。

    这里的创建动作发生在 review_moderation_event 的同一个事务里：审核记录能提交，
    内容就必须回来；缺历史材料则保留 dismissed 并返回 False，不伪装成已放行。
    """
    if event.channel == 'comment' and event.author_id and event.item_id:
        Comment.objects.create(
            item=event.item,
            author=event.author,
            content=event.content,
        )
        if event.item.seller_id and event.item.seller_id != event.author_id:
            create_notification(
                event.item.seller, actor=event.author, kind='comment_received',
                title='商品收到新的留言',
                message=f'{event.author.username}评论了你的商品“{event.item.title}”。',
                item=event.item,
                target_url=reverse('item_detail', args=[event.item.id]),
                dedupe_key=f'moderation-release:comment:{event.id}',
            )
    elif event.channel == 'private_message' and event.author_id and event.receiver_id:
        PrivateMessage.objects.create(
            sender=event.author,
            receiver=event.receiver,
            item=event.item,
            content=event.content,
        )
        create_notification(
            event.receiver, actor=event.author, kind='message_received',
            title='收到新的私信',
            message=f'{event.author.username}给你发来了一条新消息。',
            item=event.item,
            target_url=reverse('conversation', args=[event.author.id]),
            dedupe_key=f'moderation-release:message:{event.id}',
        )
    else:
        return False
    event.released_at = released_at
    return True


@login_required
def review_moderation_event(request, event_id):
    if not request.user.is_staff:
        raise PermissionDenied
    if request.method != 'POST':
        return redirect('moderation_queue')

    next_url = request.POST.get('next', '').strip()
    if not url_has_allowed_host_and_scheme(
        next_url, allowed_hosts={request.get_host()}, require_https=request.is_secure(),
    ):
        next_url = ''
    with transaction.atomic():
        event = get_object_or_404(
            ModerationEvent.objects.select_for_update(of=('self',)).select_related(
            'author', 'item__seller', 'receiver',
        ),
            id=event_id,
        )
        decision = request.POST.get('decision', '').strip()
        if event.status != 'pending':
            django_messages.info(request, '这条审核记录已经处理过了。')
        elif decision not in {'confirmed', 'dismissed'}:
            django_messages.error(request, '审核结果无效，请重新选择。')
        else:
            now = timezone.now()
            event.status = decision
            event.reviewed_by = request.user
            event.reviewed_at = now
            released = False
            if decision == 'dismissed':
                released = _release_moderated_content(event, released_at=now)
            event.save(update_fields=[
                'status', 'reviewed_by', 'reviewed_at', 'released_at',
            ])
            if event.author:
                title = '内容审核已确认违规' if decision == 'confirmed' else '内容审核完成'
                if decision == 'confirmed':
                    message = '你提交的一条留言或私信因命中社区安全规则，已确认违规。'
                elif released:
                    message = '你提交的一条留言或私信经复核后未发现违规，内容已按原渠道恢复发布。'
                else:
                    message = (
                        '你提交的内容经复核后未发现违规；这条历史记录缺少恢复材料，'
                        '系统未能自动放行，请联系管理员处理。'
                    )
                create_notification(
                    event.author, actor=request.user, kind='moderation_update',
                    title=title, message=message,
                    item=event.item, target_url=reverse('notification_list'),
                )
            if decision == 'dismissed' and not released:
                django_messages.warning(
                    request,
                    '审核结论已保存，但这条历史记录缺少作者、商品或私信接收人，内容未能自动放行。',
                )
            else:
                django_messages.success(request, '审核结果已保存，并已通知内容作者。')

    return redirect(next_url or 'moderation_queue')


@login_required
def moderation_appeals(request):
    """只给当前用户看自己被确认违规的内容和单次申诉状态。"""
    events = (
        ModerationEvent.objects.filter(author=request.user, status='confirmed')
        .select_related(
            'item__seller', 'receiver', 'reviewed_by',
            'appeal__original_reviewed_by', 'appeal__resolved_by',
        )
        .order_by('-reviewed_at', '-created_at', '-id')
    )
    paginator = Paginator(events, 10)
    page_obj = paginator.get_page(request.GET.get('page'))
    return render(request, 'chat_messages/moderation_appeals.html', {
        'events': page_obj.object_list,
        'page_obj': page_obj,
        'appeal_form': ModerationAppealForm(),
        'appeal_reasons': dict(ModerationAppeal.REASON_CHOICES),
    })


@login_required
@require_POST
def submit_moderation_appeal(request, event_id):
    """让确认违规事件的作者提交一次申诉，并保存原复核快照。"""
    with transaction.atomic():
        event = get_object_or_404(
            ModerationEvent.objects.select_for_update(of=('self',)).select_related('reviewed_by'),
            id=event_id, author=request.user,
        )
        if event.status != 'confirmed':
            django_messages.info(request, '这条内容还不是确认违规状态，暂不能申诉。')
            return redirect('moderation_appeals')
        if hasattr(event, 'appeal'):
            django_messages.info(request, '这条审核记录已经提交过申诉，不能重复提交。')
            return redirect('moderation_appeals')

        form = ModerationAppealForm(request.POST)
        if form.is_valid():
            appeal = form.save(commit=False)
            appeal.event = event
            appeal.appellant = request.user
            appeal.original_reviewed_by = event.reviewed_by
            appeal.original_reviewed_at = event.reviewed_at
            appeal.save()
            django_messages.success(request, '申诉已提交，管理员会再次复核这条内容。')
        else:
            django_messages.error(request, '申诉原因和说明不能为空，说明不能超过 1000 字。')

    return redirect('moderation_appeals')


def _safe_moderation_next_url(request):
    next_url = request.POST.get('next', '').strip()
    if not url_has_allowed_host_and_scheme(
        next_url, allowed_hosts={request.get_host()}, require_https=request.is_secure(),
    ):
        return ''
    return next_url


@login_required
@require_POST
def resolve_moderation_appeal(request, appeal_id):
    """在同一个事务里处理申诉；改判必须走既有内容恢复路径。"""
    if not request.user.is_staff:
        raise PermissionDenied

    next_url = _safe_moderation_next_url(request)
    with transaction.atomic():
        appeal = get_object_or_404(
            ModerationAppeal.objects.select_for_update(of=('self',)).select_related(
                'event__author', 'event__item__seller', 'event__receiver',
            ),
            id=appeal_id,
        )
        event = appeal.event
        decision = request.POST.get('decision', '').strip()
        note = request.POST.get('resolution_note', '').strip()

        if appeal.status != 'pending':
            django_messages.info(request, '这条申诉已经处理过了。')
        elif event.status != 'confirmed':
            django_messages.error(request, '关联审核事件状态已变化，请先确认当前记录。')
        elif decision not in {'upheld', 'accepted'}:
            django_messages.error(request, '申诉处理结果无效，请重新选择。')
        elif not note or len(note) > 1000:
            django_messages.error(request, '请填写 1000 字以内的申诉处理说明。')
        else:
            now = timezone.now()
            released = False
            if decision == 'accepted':
                event.released_at = None
                released = _release_moderated_content(event, released_at=now)
                event.status = 'dismissed'
                event.reviewed_by = request.user
                event.reviewed_at = now
                event.save(update_fields=[
                    'status', 'reviewed_by', 'reviewed_at', 'released_at',
                ])
                appeal.status = 'accepted' if released else 'accepted_without_restore'
            else:
                appeal.status = 'upheld'

            appeal.resolved_by = request.user
            appeal.resolved_at = now
            appeal.resolution_note = note
            appeal.save(update_fields=[
                'status', 'resolved_by', 'resolved_at', 'resolution_note',
            ])

            if appeal.appellant_id:
                if appeal.status == 'accepted':
                    message = '申诉成立，这条内容已按原渠道恢复发布。'
                elif appeal.status == 'accepted_without_restore':
                    message = '申诉成立；这条历史记录缺少恢复材料，系统未能自动放行。'
                else:
                    message = '管理员复核后维持原确认违规结论。'
                create_notification(
                    appeal.appellant, actor=request.user, kind='moderation_update',
                    title='内容申诉已处理', message=message,
                    item=event.item, target_url=reverse('moderation_appeals'),
                )

            if appeal.status == 'accepted_without_restore':
                django_messages.warning(request, '申诉已改判，但历史记录缺少恢复材料，内容未能自动放行。')
            else:
                django_messages.success(request, '申诉处理结果已保存，并已通知申诉人。')

    return redirect(next_url or 'moderation_queue')
