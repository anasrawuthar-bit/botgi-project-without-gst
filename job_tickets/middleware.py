import asyncio
from datetime import datetime, timedelta, timezone as dt_timezone
from urllib.parse import urlencode

from django.conf import settings
from django.contrib import messages
from django.contrib.auth import logout
from django.http import HttpResponse, JsonResponse
from django.shortcuts import redirect
from django.urls import reverse
from django.utils import timezone

import threading
from .models import UserSessionActivity
from .signals import SESSION_ACTIVITY_KEY
from .workspaces import get_current_workspace

_thread_locals = threading.local()
_last_known_base_url = None


def get_current_request():
    return getattr(_thread_locals, 'request', None)


def get_last_known_base_url() -> str | None:
    global _last_known_base_url
    req = get_current_request()
    if req:
        try:
            url = req.build_absolute_uri('/').rstrip('/')
            if url:
                _last_known_base_url = url
                return url
        except Exception:
            pass
    return _last_known_base_url


class CurrentWorkspaceMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        global _last_known_base_url
        _thread_locals.request = request
        try:
            base = request.build_absolute_uri('/').rstrip('/')
            if base:
                _last_known_base_url = base
        except Exception:
            pass

        request.current_workspace = get_current_workspace(request)

        # Bypass status checks for static files, media, auth endpoints, and platform admin
        path = request.path_info
        is_bypass = (
            path.startswith('/static/')
            or path.startswith('/media/')
            or path.startswith('/platform-admin/')
            or path.startswith('/logout/')
            or path.startswith('/admin/')
            or path.startswith('/register/')
            or path.startswith('/api/check-domain/')
        )

        # Allow superuser full access even if a workspace is expired or suspended
        is_superuser = getattr(request, 'user', None) and getattr(request.user, 'is_superuser', False)

        if request.current_workspace and not is_bypass and not is_superuser:
            ws = request.current_workspace
            from django.shortcuts import render

            brand_ctx = {
                'workspace': ws,
                'app_name': 'ZynDesk',
                'tagline': 'Every Service. One Desk',
                'company_name': 'Corezyn',
                'logo_url': 'https://image.gihostings.com/i/356b7bc7285f8ce2.jpeg',
            }

            if ws.is_suspended:
                return render(request, 'job_tickets/account_suspended.html', brand_ctx, status=403)

            if ws.is_trial_expired:
                return render(request, 'job_tickets/trial_expired.html', brand_ctx, status=402)

        return self.get_response(request)


class SessionSecurityMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        try:
            return self._handle_request(request)
        except asyncio.CancelledError:
            # Daphne/ASGI raises this when the browser cancels a request.
            return HttpResponse(status=499)

    def _handle_request(self, request):
        if not request.user.is_authenticated:
            return self.get_response(request)

        now = timezone.now()
        idle_timeout = int(getattr(settings, 'SESSION_IDLE_TIMEOUT_SECONDS', getattr(settings, 'SESSION_COOKIE_AGE', 1800)))
        session_key = request.session.session_key

        if session_key:
            UserSessionActivity.objects.filter(
                status=UserSessionActivity.STATUS_ACTIVE,
                expires_at__lt=now,
            ).update(
                status=UserSessionActivity.STATUS_EXPIRED,
                logout_reason=UserSessionActivity.STATUS_EXPIRED,
                logout_at=now,
                expires_at=now,
            )

        if self._has_session_been_replaced(request, session_key, now):
            request._audit_session_key = session_key
            request._session_logout_reason = UserSessionActivity.LOGOUT_REASON_NEW_LOGIN
            request._session_was_terminated = True
            logout(request)

            if self._expects_json(request):
                return JsonResponse(
                    {
                        'error': 'session_replaced',
                        'message': 'This account was signed in on another device. Please log in again.',
                    },
                    status=401,
                )

            messages.warning(request, 'You were logged out because this account signed in on another device.')
            return redirect(f"{reverse('login')}?{self._build_login_query(request.get_full_path(), 'session_replaced')}")

        if self._has_session_expired(request, now, idle_timeout):
            request._audit_session_key = session_key
            request._session_logout_reason = UserSessionActivity.STATUS_EXPIRED
            request._session_was_terminated = True
            logout(request)

            if self._expects_json(request):
                return JsonResponse(
                    {
                        'error': 'session_expired',
                        'message': 'Session expired due to inactivity. Please log in again.',
                    },
                    status=401,
                )

            messages.warning(request, 'Session expired due to inactivity. Please log in again.')
            return redirect(f"{reverse('login')}?{self._build_login_query(request.get_full_path(), 'session_expired')}")

        request.session[SESSION_ACTIVITY_KEY] = int(now.timestamp())
        request.session.set_expiry(idle_timeout)
        response = self.get_response(request)

        if session_key and not getattr(request, '_session_was_terminated', False):
            UserSessionActivity.objects.filter(
                session_key=session_key,
                user=request.user,
                status=UserSessionActivity.STATUS_ACTIVE,
            ).update(
                last_activity_at=now,
                last_activity_path=(request.path or '')[:255],
                expires_at=now + timedelta(seconds=idle_timeout),
            )

        return response

    def _has_session_expired(self, request, now, idle_timeout):
        raw_timestamp = request.session.get(SESSION_ACTIVITY_KEY)
        if raw_timestamp in (None, ''):
            return False

        try:
            last_activity = datetime.fromtimestamp(float(raw_timestamp), tz=dt_timezone.utc)
        except (TypeError, ValueError, OSError):
            return False

        return now - last_activity > timedelta(seconds=idle_timeout)

    def _has_session_been_replaced(self, request, session_key, now):
        if not session_key:
            return False

        session_activity = UserSessionActivity.objects.filter(
            session_key=session_key,
            user=request.user,
        ).first()
        if not session_activity:
            return False

        if session_activity.status != UserSessionActivity.STATUS_ACTIVE:
            return True

        if session_activity.expires_at and session_activity.expires_at <= now:
            return True

        return False

    def _expects_json(self, request):
        accept_header = (request.headers.get('Accept') or '').lower()
        requested_with = (request.headers.get('X-Requested-With') or '').lower()
        return (
            request.path.startswith('/api/')
            or requested_with == 'xmlhttprequest'
            or 'application/json' in accept_header
        )

    def _build_login_query(self, next_url, reason):
        return urlencode(
            {
                'next': next_url,
                'reason': reason,
            }
        )


class PlatformErrorLoggingMiddleware:
    """
    Global exception & error capturing middleware.
    Catches unhandled exceptions, logs them with full traceback,
    records them into PlatformErrorLog for the Super Admin console,
    and allows Django's custom 500 handler to render a graceful page.
    """
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        return self.get_response(request)

    def process_exception(self, request, exception):
        try:
            import logging
            import traceback
            from .models import PlatformErrorLog

            logger = logging.getLogger('zyndesk.errors')
            tb_str = traceback.format_exc()
            user = getattr(request, 'user', None)
            actor = user if (user and getattr(user, 'is_authenticated', False)) else None
            workspace = getattr(request, 'current_workspace', None)
            ip = request.META.get('HTTP_X_FORWARDED_FOR') or request.META.get('REMOTE_ADDR') or ''
            if ip and ',' in ip:
                ip = ip.split(',')[0].strip()

            err_type = type(exception).__name__
            err_msg = str(exception) or 'An unhandled exception occurred.'

            logger.error(
                f"[500 Error] {err_type} at {request.method} {request.path}: {err_msg}\n{tb_str}"
            )

            PlatformErrorLog.objects.create(
                workspace=workspace,
                actor=actor,
                error_type=err_type,
                severity=PlatformErrorLog.LEVEL_CRITICAL,
                status_code=500,
                request_path=(request.path or '')[:500],
                request_method=(request.method or 'GET')[:10],
                message=err_msg[:2000],
                traceback=tb_str,
                ip_address=ip or None,
            )
        except Exception:
            pass
        return None

