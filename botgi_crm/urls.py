# botgi_crm/urls.py
from django.contrib import admin
from django.urls import path, include # Make sure 'include' is here
from django.conf import settings
from django.conf.urls.static import static
from django.views.static import serve as static_serve

urlpatterns = [
    path('node/', admin.site.urls),
    path('', include('job_tickets.urls')), # This line adds all your app's URLs
]

# Serve static and media files
if settings.DEBUG:
    urlpatterns += static(settings.STATIC_URL, document_root=settings.STATIC_ROOT)
    urlpatterns += static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)
else:
    urlpatterns += [
        path('media/<path:path>', static_serve, {'document_root': settings.MEDIA_ROOT}),
    ]

# Custom HTTP Error Handlers
handler400 = 'job_tickets.views.custom_400'
handler403 = 'job_tickets.views.custom_403'
handler404 = 'job_tickets.views.custom_404'
handler500 = 'job_tickets.views.custom_500'

