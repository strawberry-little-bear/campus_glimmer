# campus_glimmer/urls.py
from django.contrib import admin
from django.urls import path, include
from django.conf import settings
from django.conf.urls.static import static
from listings.views import home
from .health import health_check, ready_check
from .metrics import METRICS_PATH, metrics_view


urlpatterns = [
    path('admin/', admin.site.urls),
    path('', home, name='home'),
    path('healthz/', health_check, name='health_check'),
    path('readyz/', ready_check, name='ready_check'),
    path(METRICS_PATH.lstrip('/'), metrics_view, name='metrics'),
    path('accounts/', include('accounts.urls')),
    path('listings/', include('listings.urls')),
    path('messages/', include('chat_messages.urls')), 
]

if settings.DEBUG:
    urlpatterns += static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)
