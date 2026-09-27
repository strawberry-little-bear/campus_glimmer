# chat_messages/views.py (原messages/views.py)
from django.shortcuts import render, redirect, get_object_or_404
from django.urls import reverse
from django.contrib.auth.decorators import login_required
from django.contrib import messages as django_messages
from django.contrib.auth.models import User
from django.db.models.functions import Coalesce
from django.db.models import Count, IntegerField, OuterRef, Q, Subquery, Value
from .models import Comment, PrivateMessage
from .forms import CommentForm, PrivateMessageForm
from listings.models import Item
from listings.notifications import create_notification

@login_required
def add_comment(request, item_id):
    item = get_object_or_404(Item, id=item_id)
    
    if request.method == 'POST':
        form = CommentForm(request.POST)
        if form.is_valid():
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
                )
            django_messages.success(request, '评论已发布！')
            return redirect('item_detail', item_id=item.id)
    else:
        form = CommentForm()
        
    return redirect('item_detail', item_id=item.id)

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

    context = {
        'received_messages': received_messages,
        'sent_messages': sent_messages,
        'conversation_data': _build_conversation_data(request.user, conversation_users),
        'search_query': search_query,
        'message_status_filter': status_filter,
        'message_status_options': (
            ('all', '全部收到的消息'),
            ('unread', '仅看未读'),
            ('read', '仅看已读'),
        ),
        'message_filtered_unread_count': received_messages.filter(is_read=False).count(),
    }
    return render(request, 'chat_messages/inbox.html', context)


@login_required
def mark_all_messages_read(request):
    if request.method == 'POST':
        updated_count = PrivateMessage.objects.filter(
            receiver=request.user,
            is_read=False,
        ).update(is_read=True)
        if updated_count:
            django_messages.success(request, f'已将 {updated_count} 条私信标记为已读。')
        else:
            django_messages.info(request, '当前没有未读私信。')
    return redirect('inbox')

@login_required
def send_message(request, receiver_id, item_id=None):
    receiver = get_object_or_404(User, id=receiver_id)
    item = get_object_or_404(Item, id=item_id) if item_id else None
    
    if request.method == 'POST':
        form = PrivateMessageForm(request.POST)
        if form.is_valid():
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
    messages_list = PrivateMessage.objects.filter(
        (Q(sender=request.user) & Q(receiver=other_user)) |
        (Q(sender=other_user) & Q(receiver=request.user))
    ).order_by('created_at')
    
    # 标记收到的消息为已读
    unread_messages = messages_list.filter(receiver=request.user, is_read=False)
    for msg in unread_messages:
        msg.is_read = True
        msg.save()
        
    # 发送新消息的表单
    if request.method == 'POST':
        form = PrivateMessageForm(request.POST)
        if form.is_valid():
            message = form.save(commit=False)
            message.sender = request.user
            message.receiver = other_user
            message.save()
            create_notification(
                other_user, actor=request.user, kind='message_received',
                title='收到新的私信',
                message=f'{request.user.username}给你发来了一条新消息。',
                target_url=reverse('conversation', args=[request.user.id]),
            )
            django_messages.success(request, '消息已发送！')
            return redirect('conversation', user_id=other_user.id)
    else:
        form = PrivateMessageForm()
        
    context = {
        'other_user': other_user,
        'messages_list': messages_list,
        'form': form
    }
    # 修改这里的模板路径
    return render(request, 'chat_messages/conversation.html', context)