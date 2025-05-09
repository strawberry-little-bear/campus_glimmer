# chat_messages/views.py (原messages/views.py)
from django.shortcuts import render, redirect, get_object_or_404
from django.contrib.auth.decorators import login_required
from django.contrib import messages as django_messages
from django.contrib.auth.models import User
from django.db.models import Q
from .models import Comment, PrivateMessage
from .forms import CommentForm, PrivateMessageForm
from listings.models import Item

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
            django_messages.success(request, '评论已发布！')
            return redirect('item_detail', item_id=item.id)
    else:
        form = CommentForm()
        
    return redirect('item_detail', item_id=item.id)

@login_required
def inbox(request):
    # 获取收到和发出的消息
    received_messages = PrivateMessage.objects.filter(receiver=request.user).order_by('-created_at')
    sent_messages = PrivateMessage.objects.filter(sender=request.user).order_by('-created_at')
    
    # 获取与用户有过交流的所有用户
    conversation_users = User.objects.filter(
        Q(sent_messages__receiver=request.user) | Q(received_messages__sender=request.user)
    ).distinct().exclude(id=request.user.id)
    
    # 为每个用户准备会话信息
    conversation_data = []
    for user in conversation_users:
        # 获取来自此用户的最后一条消息
        last_message = PrivateMessage.objects.filter(
            sender=user, 
            receiver=request.user
        ).order_by('-created_at').first()
        
        # 计算来自此用户的未读消息数量
        unread_count = PrivateMessage.objects.filter(
            sender=user, 
            receiver=request.user, 
            is_read=False
        ).count()
        
        # 汇总此用户的会话数据
        user_data = {
            'user': user,
            'last_message': last_message,
            'unread_count': unread_count
        }
        
        conversation_data.append(user_data)
    
    context = {
        'received_messages': received_messages,
        'sent_messages': sent_messages,
        'conversation_data': conversation_data
    }
    return render(request, 'chat_messages/inbox.html', context)

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