# job_tickets/context_processors.py
from django.conf import settings

from .access_control import get_staff_access
from .models import CompanyProfile, PlatformSettings
from .workspaces import get_current_workspace

SAAS_BRAND = {
    'app_name': 'ZynDesk',
    'tagline': 'Every Service. One Desk',
    'company_name': 'Corezyn',
    'logo_url': 'https://image.gihostings.com/i/356b7bc7285f8ce2.jpeg',
}


def company_profile(request):
    """Make company profile and SaaS branding available in all templates with fault-tolerant fallbacks"""
    current_workspace = None
    try:
        current_workspace = get_current_workspace(request)
    except Exception:
        pass

    company = None
    try:
        company = CompanyProfile.get_profile(current_workspace)
    except Exception:
        pass

    platform = None
    try:
        platform = PlatformSettings.get_settings()
    except Exception:
        pass

    staff_access = {}
    try:
        staff_access = get_staff_access(getattr(request, 'user', None))
    except Exception:
        pass

    return {
        'current_workspace': current_workspace,
        'company': company,
        'platform': platform,
        'staff_access': staff_access,
        'saas_brand': SAAS_BRAND,
        'is_trial': getattr(current_workspace, 'is_trial', False) if current_workspace else False,
        'trial_days_left': getattr(current_workspace, 'days_left_in_trial', 0) if current_workspace else 0,
        'release_info': {
            'web_version': getattr(settings, 'WEB_RELEASE_VERSION', 'dev'),
            'poll_interval_ms': max(getattr(settings, 'WEB_RELEASE_POLL_INTERVAL_SECONDS', 300), 60) * 1000,
        },
    }
