# chat_messages/apps.py
from django.apps import AppConfig

class ChatMessagesConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'chat_messages'
    verbose_name = '用户消息'
