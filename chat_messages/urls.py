# messages/urls.py
from django.urls import path
from . import views

urlpatterns = [
    path('comment/<int:item_id>/', views.add_comment, name='add_comment'),
    path('inbox/', views.inbox, name='inbox'),
    path('inbox/read-all/', views.mark_all_messages_read, name='mark_all_messages_read'),
    path('inbox/read-selected/', views.mark_selected_messages_read, name='mark_selected_messages_read'),
    path('message/<int:message_id>/read/', views.mark_message_read, name='mark_message_read'),
    path('conversation/<int:user_id>/read/', views.mark_conversation_read, name='mark_conversation_read'),
    path('moderation/', views.moderation_queue, name='moderation_queue'),
    path('moderation/<int:event_id>/review/', views.review_moderation_event, name='review_moderation_event'),
    path('send/<int:receiver_id>/', views.send_message, name='send_message'),
    path('send/<int:receiver_id>/<int:item_id>/', views.send_message, name='send_message_item'),
    path('conversation/<int:user_id>/', views.conversation, name='conversation'),
]
