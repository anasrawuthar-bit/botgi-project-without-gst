from django.conf import settings
from django.contrib.auth.models import User

from .models import CompanyUserMembership, CompanyWorkspace

RESERVED_SUBDOMAINS = {
    'www', 'admin', 'api', 'app', 'mail', 'platform', 'superadmin',
    'static', 'media', 'help', 'docs', 'test', 'support', 'billing', 'zyndesk'
}


def extract_subdomain(request):
    """
    Extract subdomain from request host or custom domain.
    Supports:
      - Subdomain on root domain: acme.zyndesk.com -> 'acme'
      - Custom domain: crm.custombrand.com -> looks up workspace.custom_domain
      - Local dev testing: acme.localhost:8000 -> 'acme', or query param ?ws=acme
    """
    if not request:
        return None

    # Allow query parameter override for local development (?ws=slug)
    ws_param = request.GET.get('ws')
    if ws_param and ws_param.strip():
        return ws_param.strip().lower()

    try:
        host = request.get_host().split(':')[0].lower()
    except Exception:
        return None

    # Check for direct custom domain match
    custom_ws = CompanyWorkspace.objects.filter(
        custom_domain__iexact=host,
        custom_domain_verified=True
    ).first()
    if custom_ws:
        return custom_ws.slug

    saas_root_domain = getattr(settings, 'SAAS_ROOT_DOMAIN', '') or ''
    if not saas_root_domain:
        pub_url = getattr(settings, 'PUBLIC_BASE_URL', '') or ''
        if pub_url:
            saas_root_domain = pub_url.replace('https://', '').replace('http://', '').split('/')[0].split(':')[0].lower()

    if saas_root_domain and host.endswith('.' + saas_root_domain):
        sub = host[:-len('.' + saas_root_domain)]
        parts = sub.split('.')
        candidate = parts[-1]
        if candidate and candidate not in RESERVED_SUBDOMAINS:
            return candidate

    # Development fallback for *.localhost, *.lvh.me, *.nip.io
    parts = host.split('.')
    if len(parts) >= 2 and parts[-1] in ('localhost', 'test', 'local', 'lvh', 'nip'):
        if parts[0] not in RESERVED_SUBDOMAINS:
            return parts[0]
    elif len(parts) >= 3:
        if parts[0] not in RESERVED_SUBDOMAINS:
            return parts[0]

    return None


def get_user_workspace_queryset(user):
    if not user or not getattr(user, 'is_authenticated', False):
        return CompanyWorkspace.objects.none()

    if getattr(user, 'is_superuser', False):
        # Super Admin is not a tenant member
        return CompanyWorkspace.objects.none()

    return (
        CompanyWorkspace.objects.filter(memberships__user=user, memberships__is_active=True)
        .distinct()
        .order_by('name')
    )


def get_current_workspace(request):
    if not request:
        return None

    if getattr(request, '_cached_current_workspace', None) is not None:
        return request._cached_current_workspace

    if getattr(request, 'current_workspace', None) is not None:
        request._cached_current_workspace = request.current_workspace
        return request.current_workspace

    workspace = None
    subdomain = extract_subdomain(request)
    if subdomain:
        workspace = CompanyWorkspace.objects.filter(slug__iexact=subdomain).first()

    # Super Admin is never automatically tied to a tenant workspace on the root domain
    if getattr(request, 'user', None) and getattr(request.user, 'is_superuser', False):
        request._cached_current_workspace = workspace
        request.current_workspace = workspace
        return workspace

    # If no subdomain found (e.g. root domain or direct IP), fallback to user workspace
    if not workspace and getattr(request, 'user', None) and request.user.is_authenticated:
        session = getattr(request, 'session', None)
        session_ws_id = session.get('active_workspace_id') if session else None
        if session_ws_id:
            workspace = CompanyWorkspace.objects.filter(pk=session_ws_id).first()
        if not workspace:
            workspace = get_user_workspace_queryset(request.user).first()

    request._cached_current_workspace = workspace
    request.current_workspace = workspace
    return workspace


def ensure_user_workspace_membership(user, workspace, role=CompanyUserMembership.ROLE_STAFF):
    if not isinstance(user, User) or not workspace:
        return None
    membership, _ = CompanyUserMembership.objects.get_or_create(
        workspace=workspace,
        user=user,
        defaults={'role': role},
    )
    return membership
