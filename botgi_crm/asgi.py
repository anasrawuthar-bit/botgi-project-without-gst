# botgi_crm/asgi.py

import os
from django.core.asgi import get_asgi_application

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'botgi_crm.settings')
from botgi_crm import django_compat  # noqa: F401

# Initialize Django ASGI application early to ensure the AppRegistry is populated
django_asgi_app = get_asgi_application()

# Import Channels components and routing AFTER get_asgi_application()
from channels.auth import AuthMiddlewareStack  # For session/user support in WebSockets
from channels.routing import ProtocolTypeRouter, URLRouter
from job_tickets import routing

application = ProtocolTypeRouter({
    # HTTP requests will be handled by the default Django ASGI application.
    "http": django_asgi_app,
    
    # WebSocket connections will be routed by the URLRouter.
    "websocket": AuthMiddlewareStack(
        URLRouter(
            routing.websocket_urlpatterns
        )
    ),
})

