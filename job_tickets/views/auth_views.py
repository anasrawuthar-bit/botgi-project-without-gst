from .helpers import *  # noqa: F401,F403
from .helpers import (
    _get_permitted_next_url,
    _get_post_login_redirect,
    _get_safe_next_url,
)


def home(request):
    context = {
        'total_jobs': JobTicket.objects.count(),
        'total_clients': Client.objects.count(),
        'total_products': Product.objects.count(),
    }
    return render(request, 'job_tickets/home.html', context)


def unauthorized(request):
    return render(request, 'job_tickets/unauthorized.html')


@never_cache
def login_view(request):
    next_url = _get_safe_next_url(request)
    logout_reason = (request.GET.get('reason') or '').strip().lower()
    logout_notice = ''
    logout_notice_level = ''
    if logout_reason == 'session_replaced':
        logout_notice = 'You were logged out because this account signed in on another device.'
        logout_notice_level = 'warning'
    elif logout_reason == 'session_expired':
        logout_notice = 'Your session expired due to inactivity. Please log in again.'
        logout_notice_level = 'secondary'

    if request.user.is_authenticated:
        permitted_next_url = _get_permitted_next_url(request.user, next_url)
        if permitted_next_url:
            return redirect(permitted_next_url)
        return _get_post_login_redirect(request.user)

    if request.method == 'POST':
        username = request.POST.get('username', '').strip()
        password = request.POST.get('password', '')

        client_ip = request.META.get('REMOTE_ADDR')
        cache_key = f'login_attempts_{client_ip}'
        attempts = cache.get(cache_key, 0)

        if attempts >= 5:
            return render(request, 'job_tickets/login.html', {
                'error': 'Too many failed attempts. Please try again in 15 minutes.',
                'locked': True,
                'logout_notice': logout_notice,
                'logout_notice_level': logout_notice_level,
            })

        user = authenticate(request, username=username, password=password)
        if user is None:
            # Check if user entered an email address instead of username
            candidate = User.objects.filter(email__iexact=username).first()
            if candidate and candidate.check_password(password):
                user = candidate

        if user is not None:
            if user.is_superuser:
                # Enforce role separation: Super Admins cannot sign in through the staff/technician portal
                # Do not disclose superadmin existence to outside visitors
                cache.set(cache_key, attempts + 1, 900)
                return render(request, 'job_tickets/login.html', {
                    'error': 'Invalid username or password.',
                    'attempts_left': max(0, 5 - (attempts + 1)),
                    'logout_notice': logout_notice,
                    'logout_notice_level': logout_notice_level,
                })

            # Check tenant workspace & verification
            from ..workspaces import get_current_workspace
            current_ws = get_current_workspace(request)

            # If login occurs on a company subdomain, verify the user belongs to this specific company
            if current_ws:
                membership = CompanyUserMembership.objects.filter(workspace=current_ws, user=user, is_active=True).first()
                if not membership:
                    cache.set(cache_key, attempts + 1, 900)
                    return render(request, 'job_tickets/login.html', {
                        'error': f'Invalid credentials for {current_ws.name}. Please ensure you are logging into your assigned company portal.',
                        'attempts_left': max(0, 5 - (attempts + 1)),
                        'logout_notice': logout_notice,
                        'logout_notice_level': logout_notice_level,
                    })
                target_ws = current_ws
            else:
                membership = CompanyUserMembership.objects.filter(user=user, is_active=True).first()
                target_ws = membership.workspace if membership else None

            # Enforce Email Verification: Block login if workspace is pending verification
            if target_ws and not target_ws.email_verified:
                return render(request, 'job_tickets/login.html', {
                    'error': f'Your company account ({target_ws.name}) is pending email verification. Please verify your email before logging in.',
                    'verification_pending': True,
                    'pending_email': target_ws.contact_email or user.email,
                    'logout_notice': logout_notice,
                    'logout_notice_level': logout_notice_level,
                })

            cache.delete(cache_key)
            login(request, user)
            if target_ws:
                request.session['active_workspace_id'] = target_ws.id
            request.session.set_expiry(settings.SESSION_IDLE_TIMEOUT_SECONDS)
            permitted_next_url = _get_permitted_next_url(user, next_url)
            if permitted_next_url:
                return redirect(permitted_next_url)
            return _get_post_login_redirect(user)

        cache.set(cache_key, attempts + 1, 900)
        return render(request, 'job_tickets/login.html', {
            'error': 'Invalid username or password',
            'username': username,
            'next': next_url,
            'attempts_left': 4 - attempts,
            'logout_notice': logout_notice,
            'logout_notice_level': logout_notice_level,
        })

    return render(
        request,
        'job_tickets/login.html',
        {
            'next': next_url,
            'logout_notice': logout_notice,
            'logout_notice_level': logout_notice_level,
            'logout_reason': logout_reason,
        },
    )


@never_cache
def logout_view(request):
    request._audit_session_key = request.session.session_key
    request._session_logout_reason = UserSessionActivity.STATUS_LOGGED_OUT
    request._session_was_terminated = True
    logout(request)
    return redirect('home')
