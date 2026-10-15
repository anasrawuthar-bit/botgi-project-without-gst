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

# Error handlers (only work when DEBUG=False)
if not settings.DEBUG:
    from job_tickets.views import custom_404, custom_500
    handler404 = custom_404
    handler500 = custom_500
