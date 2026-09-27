# messages/urls.py
from django.urls import path
from . import views

urlpatterns = [
    path('comment/<int:item_id>/', views.add_comment, name='add_comment'),
    path('inbox/', views.inbox, name='inbox'),
    path('inbox/read-all/', views.mark_all_messages_read, name='mark_all_messages_read'),
    path('send/<int:receiver_id>/', views.send_message, name='send_message'),
    path('send/<int:receiver_id>/<int:item_id>/', views.send_message, name='send_message_item'),
    path('conversation/<int:user_id>/', views.conversation, name='conversation'),
]