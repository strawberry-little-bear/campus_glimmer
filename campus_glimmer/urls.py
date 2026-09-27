# campus_glimmer/urls.py
from django.contrib import admin
from django.urls import path, include
from django.http import JsonResponse
from django.conf import settings
from django.conf.urls.static import static
from listings.views import home


def health_check(request):
    return JsonResponse({'status': 'ok', 'service': 'campus-glimmer'})

urlpatterns = [
    path('admin/', admin.site.urls),
    path('', home, name='home'),
    path('healthz/', health_check, name='health_check'),
    path('accounts/', include('accounts.urls')),
    path('listings/', include('listings.urls')),
    path('messages/', include('chat_messages.urls')), 
]

if settings.DEBUG:
    urlpatterns += static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)
