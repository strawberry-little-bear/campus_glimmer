# chat_messages/views.py (原messages/views.py)
from django.shortcuts import render, redirect, get_object_or_404
from django.urls import reverse
from django.contrib.auth.decorators import login_required
from django.contrib import messages as django_messages
from django.contrib.auth.models import User
from django.db.models import Q
from django.utils import timezone
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

@login_required
def inbox(request):
    search_query = request.GET.get('q', '').strip()
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

    conversation_data = []
    for user in conversation_users:
        conversation_messages = PrivateMessage.objects.filter(
            Q(sender=request.user, receiver=user)
            | Q(sender=user, receiver=request.user)
        ).select_related('sender', 'receiver', 'item')
        last_message = conversation_messages.order_by('-created_at').first()
        unread_count = conversation_messages.filter(
            receiver=request.user,
            is_read=False,
        ).count()
        conversation_data.append({
            'user': user,
            'last_message': last_message,
            'unread_count': unread_count,
            'message_count': conversation_messages.count(),
        })

    conversation_data.sort(
        key=lambda data: data['last_message'].created_at if data['last_message'] else timezone.now(),
        reverse=True,
    )
    context = {
        'received_messages': received_messages,
        'sent_messages': sent_messages,
        'conversation_data': conversation_data,
        'search_query': search_query,
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