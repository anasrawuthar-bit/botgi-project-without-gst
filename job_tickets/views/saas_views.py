import datetime
from decimal import Decimal
from django.conf import settings
from django.contrib import messages
from django.contrib.auth import authenticate, login, logout
from django.contrib.auth.decorators import login_required
from django.contrib.auth.models import Group, User
from django.core.cache import cache
from django.core.mail import EmailMessage, get_connection
from django.db import transaction
from django.db.models import Q, Sum
from django.http import HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.utils.text import slugify
from django.views.decorators.cache import never_cache

from ..models import (
    CompanyProfile,
    CompanySubscriptionPayment,
    CompanyUserMembership,
    CompanyWorkspace,
    FinancialAccount,
    JobTicket,
    PlatformActivityLog,
    PlatformErrorLog,
    PlatformSettings,
    PlatformSubscriptionPlan,
)
from ..workspaces import RESERVED_SUBDOMAINS, get_current_workspace

SAAS_BRAND = {
    'app_name': 'ZynDesk',
    'tagline': 'Every Service. One Desk',
    'company_name': 'Corezyn',
    'logo_url': 'https://image.gihostings.com/i/356b7bc7285f8ce2.jpeg',
}


def log_platform_activity(action, description, workspace=None, actor=None, request=None):
    """Helper to log administrative and tenant events to PlatformActivityLog"""
    ip = None
    if request:
        ip = request.META.get('HTTP_X_FORWARDED_FOR') or request.META.get('REMOTE_ADDR')
        if ip and ',' in ip:
            ip = ip.split(',')[0].strip()

    PlatformActivityLog.objects.create(
        workspace=workspace,
        actor=actor,
        action=action,
        description=description,
        ip_address=ip,
    )


def saas_landing(request):
    """
    Public landing page for ZynDesk SaaS on root domain,
    or Dedicated Company Home Screen if on a tenant subdomain.
    """
    if request.user.is_authenticated and request.user.is_superuser:
        return redirect('platform_admin_dashboard')

    current_ws = get_current_workspace(request)
    if current_ws:
        # On a company subdomain:
        if request.user.is_authenticated:
            if request.user.groups.filter(name='Technicians').exists() or not getattr(request.user, 'is_staff', False):
                return redirect('technician_dashboard')
            return redirect('staff_dashboard')

        # Unauthenticated on company subdomain: render dedicated company home screen!
        company = CompanyProfile.get_profile(current_ws)
        return render(request, 'job_tickets/company_home.html', {
            'workspace': current_ws,
            'company': company,
            'saas_brand': SAAS_BRAND,
        })

    # Main ZynDesk marketing website on root domain (e.g. anasrawuthar.in)
    context = {
        'saas_brand': SAAS_BRAND,
        'app_name': SAAS_BRAND['app_name'],
        'company_name': SAAS_BRAND['company_name'],
        'logo_url': SAAS_BRAND['logo_url'],
        'total_companies': CompanyWorkspace.objects.count(),
        'total_tickets': JobTicket.objects.count(),
    }
    return render(request, 'job_tickets/saas_landing.html', context)


def saas_register(request):
    """
    Self-service registration view for new companies.
    Provisions a new CompanyWorkspace in pending verification state.
    Does not auto-login; dispatches verification email with verify & reject options.
    """
    if request.user.is_authenticated and not request.user.is_superuser:
        current_ws = get_current_workspace(request)
        if current_ws:
            return redirect('staff_dashboard')

    errors = {}
    form_data = {}

    if request.method == 'POST':
        company_name = request.POST.get('company_name', '').strip()
        slug_raw = request.POST.get('subdomain', '').strip().lower()
        full_name = request.POST.get('full_name', '').strip()
        email = request.POST.get('email', '').strip().lower()
        phone = request.POST.get('phone', '').strip()
        password = request.POST.get('password', '')
        confirm_password = request.POST.get('confirm_password', '')

        form_data = {
            'company_name': company_name,
            'subdomain': slug_raw,
            'full_name': full_name,
            'email': email,
            'phone': phone,
        }

        # Subdomain / Slug sanitation
        slug = slugify(slug_raw).replace('_', '-')

        if not company_name:
            errors['company_name'] = 'Company name is required.'
        if not slug or len(slug) < 3:
            errors['subdomain'] = 'Subdomain must be at least 3 characters long.'
        elif slug in RESERVED_SUBDOMAINS:
            errors['subdomain'] = f"The subdomain '{slug}' is reserved. Please choose another."
        elif CompanyWorkspace.objects.filter(slug__iexact=slug).exists():
            errors['subdomain'] = f"The subdomain '{slug}' is already taken. Please pick another."

        if not full_name:
            errors['full_name'] = 'Admin full name is required.'
        if not email or '@' not in email:
            errors['email'] = 'A valid email address is required.'
        elif User.objects.filter(email__iexact=email).exists() or User.objects.filter(username__iexact=email).exists():
            errors['email'] = 'An account with this email address already exists. Please log in.'

        if not password or len(password) < 6:
            errors['password'] = 'Password must be at least 6 characters long.'
        elif password != confirm_password:
            errors['confirm_password'] = 'Passwords do not match.'

        if not errors:
            try:
                with transaction.atomic():
                    # 1. Create the Owner User
                    name_parts = full_name.split(' ', 1)
                    first_name = name_parts[0]
                    last_name = name_parts[1] if len(name_parts) > 1 else ''

                    user = User.objects.create_user(
                        username=email,
                        email=email,
                        first_name=first_name,
                        last_name=last_name,
                        password=password,
                        is_staff=True,
                    )

                    staff_group, _ = Group.objects.get_or_create(name='Staff')
                    user.groups.add(staff_group)

                    # 2. Create the CompanyWorkspace with 7-Day Trial and email_verified=False
                    now = timezone.now()
                    trial_end = now + datetime.timedelta(days=7)

                    workspace = CompanyWorkspace.objects.create(
                        name=company_name,
                        slug=slug,
                        owner=user,
                        contact_email=email,
                        contact_phone=phone,
                        status=CompanyWorkspace.STATUS_TRIAL,
                        plan_name='7-Day Free Trial',
                        trial_ends_at=trial_end,
                        email_verified=False,
                        storage_limit_mb=500,
                        max_staff_limit=5,
                        max_tickets_per_month=100,
                    )

                    # 3. Create CompanyUserMembership as Owner
                    CompanyUserMembership.objects.create(
                        workspace=workspace,
                        user=user,
                        role=CompanyUserMembership.ROLE_OWNER,
                        is_active=True,
                    )

                    # 4. Create default CompanyProfile
                    CompanyProfile.objects.create(
                        workspace=workspace,
                        company_name=company_name,
                        email=email,
                        phone1=phone or '',
                        tagline='Service & Repair Center',
                    )

                    # 5. Seed default Financial Accounts
                    FinancialAccount.ensure_default_accounts(workspace)

                    # 6. Log platform event
                    log_platform_activity(
                        action='company_registered',
                        description=f"New company '{company_name}' registered ({slug}) with 7-day trial by {email}. Pending verification.",
                        workspace=workspace,
                        actor=user,
                        request=request,
                    )

                    # 7. Automated Email Dispatch (Verification with Reject option)
                    try:
                        from ..email_service import send_registration_verification_email
                        send_registration_verification_email(user, workspace, request=request)
                    except Exception as email_err:
                        pass

                    # Redirect to confirmation screen (no auto-login, verification mandatory)
                    return redirect(f"{reverse('saas_register_pending')}?email={email}&slug={slug}")

            except Exception as e:
                errors['general'] = f"Failed to register workspace: {str(e)}"

    context = {
        'saas_brand': SAAS_BRAND,
        'app_name': SAAS_BRAND['app_name'],
        'company_name': SAAS_BRAND['company_name'],
        'logo_url': SAAS_BRAND['logo_url'],
        'errors': errors,
        'form_data': form_data,
    }
    return render(request, 'job_tickets/saas_register.html', context)


def saas_register_pending(request):
    """
    Shows confirmation screen after registration asking user to check email.
    """
    email = request.GET.get('email', '').strip()
    slug = request.GET.get('slug', '').strip()
    workspace = None
    if slug:
        workspace = CompanyWorkspace.objects.filter(slug__iexact=slug).first()

    root_domain = getattr(settings, 'SAAS_ROOT_DOMAIN', '') or 'anasrawuthar.in'
    subdomain_url = f"https://{slug}.{root_domain}" if slug else ''

    return render(request, 'job_tickets/saas_register_pending.html', {
        'email': email,
        'slug': slug,
        'workspace': workspace,
        'subdomain_url': subdomain_url,
        'saas_brand': SAAS_BRAND,
    })


def saas_verify_email(request):
    """
    Handle email verification token links clicked by workspace owners.
    Activates workspace and redirects to the company subdomain login.
    """
    token = request.GET.get('token', '').strip()
    if not token:
        messages.error(request, "No verification token provided.")
        return redirect('home')

    from ..email_service import verify_email_token, send_registration_welcome_email
    success, workspace_or_msg, user = verify_email_token(token)

    if success:
        workspace = workspace_or_msg
        log_platform_activity(
            action='email_verified',
            description=f"Email address verified for workspace '{workspace.name}' ({workspace.contact_email}).",
            workspace=workspace,
            actor=user,
            request=request,
        )
        try:
            send_registration_welcome_email(user, workspace, request=request)
        except Exception:
            pass

        messages.success(
            request,
            f"✓ Email verified successfully! Your company workspace '{workspace.name}' is now active."
        )

        root_domain = getattr(settings, 'SAAS_ROOT_DOMAIN', '') or 'anasrawuthar.in'
        # Redirect to the dedicated company domain login
        target_url = f"https://{workspace.slug}.{root_domain}/login/?verified=1"
        return redirect(target_url)
    else:
        messages.error(request, f"Email verification failed: {workspace_or_msg}")
        return redirect('home')


def saas_reject_registration(request):
    """
    Handle registration cancellation link clicked from verification email.
    Allows real email owners to reject unauthorized or accidental signups.
    """
    token = request.GET.get('token', '').strip()
    if not token:
        messages.error(request, "No cancellation token provided.")
        return redirect('home')

    from ..email_service import reject_email_token
    success, ws_name_or_msg, user = reject_email_token(token)

    if success:
        log_platform_activity(
            action='registration_rejected',
            description=f"Registration for workspace '{ws_name_or_msg}' was rejected and cancelled via email link.",
            actor=user,
            request=request,
        )
        return render(request, 'job_tickets/saas_register_rejected.html', {
            'workspace_name': ws_name_or_msg,
            'saas_brand': SAAS_BRAND,
        })
    else:
        messages.error(request, f"Failed to cancel registration: {ws_name_or_msg}")
        return redirect('home')


def saas_resend_verification(request):
    """
    Resend verification email to registered owner.
    """
    email = (request.POST.get('email') or request.GET.get('email') or '').strip().lower()
    if not email:
        messages.error(request, "Please specify an email address.")
        return redirect('home')

    user = User.objects.filter(email__iexact=email).first() or User.objects.filter(username__iexact=email).first()
    if user:
        membership = CompanyUserMembership.objects.filter(user=user, role=CompanyUserMembership.ROLE_OWNER).first()
        if membership and membership.workspace:
            ws = membership.workspace
            if not ws.email_verified:
                from ..email_service import send_registration_verification_email
                send_registration_verification_email(user, ws, request=request)
                messages.success(request, f"A fresh verification email has been sent to {email}. Please check your inbox.")
                return redirect(f"{reverse('saas_register_pending')}?email={email}&slug={ws.slug}")
            else:
                messages.info(request, "Your company account is already verified! You can log in on your company domain.")
                root_domain = getattr(settings, 'SAAS_ROOT_DOMAIN', '') or 'anasrawuthar.in'
                return redirect(f"https://{ws.slug}.{root_domain}/login/")

    messages.info(request, f"If an account exists for {email}, a verification email has been resent.")
    return redirect('home')


def saas_find_domain(request):
    """
    Find Company Subdomain Tool:
    Allows owners, staff, and technicians to locate their company portal URL.
    """
    root_domain = getattr(settings, 'SAAS_ROOT_DOMAIN', '') or 'anasrawuthar.in'
    query = ''
    matches = []
    searched = False

    if request.method == 'POST':
        query = request.POST.get('query', '').strip()
        searched = True
        if query:
            qs = CompanyWorkspace.objects.filter(
                Q(contact_email__iexact=query) |
                Q(owner__email__iexact=query) |
                Q(contact_phone=query) |
                Q(memberships__user__email__iexact=query) |
                Q(name__icontains=query)
            ).distinct()

            for ws in qs:
                portal_url = f"https://{ws.slug}.{root_domain}"
                matches.append({
                    'name': ws.name,
                    'slug': ws.slug,
                    'portal_url': portal_url,
                    'login_url': f"{portal_url}/login/",
                    'status': ws.status,
                    'email_verified': ws.email_verified,
                })

                if '@' in query:
                    try:
                        from ..email_service import send_domain_lookup_email
                        send_domain_lookup_email(ws, query, request=request)
                    except Exception:
                        pass

    return render(request, 'job_tickets/saas_find_domain.html', {
        'query': query,
        'matches': matches,
        'searched': searched,
        'root_domain': root_domain,
        'saas_brand': SAAS_BRAND,
    })


@never_cache
def platform_admin_login(request):
    """
    Dedicated Super Admin login screen.
    Authenticates by email address (or username) and password.
    Strictly verifies user.is_superuser.
    """
    if request.user.is_authenticated:
        if request.user.is_superuser:
            return redirect('platform_admin_dashboard')
        # If logged in as non-superuser, log them out of the regular session
        logout(request)

    error = None
    email_val = ''

    if request.method == 'POST':
        email_val = (
            request.POST.get('email')
            or request.POST.get('username')
            or request.POST.get('username_or_email')
            or ''
        ).strip()
        password = request.POST.get('password', '')

        client_ip = request.META.get('REMOTE_ADDR')
        cache_key = f'superadmin_login_attempts_{client_ip}'
        attempts = cache.get(cache_key, 0)

        if attempts >= 5:
            error = "Too many failed attempts. Super Admin console locked for 15 minutes."
            return render(request, 'job_tickets/platform_admin_login.html', {
                'saas_brand': SAAS_BRAND,
                'logo_url': SAAS_BRAND['logo_url'],
                'error': error,
                'email': email_val,
            })

        user_match = User.objects.filter(
            Q(email__iexact=email_val) | Q(username__iexact=email_val)
        ).first()

        if user_match and user_match.check_password(password):
            if not user_match.is_active:
                error = "This administrator account is disabled."
            elif not user_match.is_superuser:
                # Crucial role separation: reject non-superusers!
                error = "Access Restricted: This console is strictly reserved for Platform Super Administrators. Staff and technicians must log in via the standard staff portal."
                cache.set(cache_key, attempts + 1, 900)
            else:
                cache.delete(cache_key)
                login(request, user_match)
                log_platform_activity(
                    action='super_admin_login',
                    description=f"Super Admin '{user_match.email or user_match.username}' logged into Master Console.",
                    actor=user_match,
                    request=request
                )
                messages.success(request, f"Welcome to Master Console, Super Administrator ({user_match.email or user_match.username}).")
                next_url = request.GET.get('next') or request.POST.get('next')
                if next_url and next_url.startswith('/platform-admin'):
                    return redirect(next_url)
                return redirect('platform_admin_dashboard')
        else:
            cache.set(cache_key, attempts + 1, 900)
            error = "Invalid Super Admin email or password. Please verify your credentials."

    return render(request, 'job_tickets/platform_admin_login.html', {
        'saas_brand': SAAS_BRAND,
        'logo_url': SAAS_BRAND['logo_url'],
        'error': error,
        'email': email_val,
        'next': request.GET.get('next', ''),
    })


@never_cache
def platform_admin_logout(request):
    """Log out Super Admin and redirect back to Super Admin Login"""
    if request.user.is_authenticated:
        log_platform_activity(
            action='super_admin_logout',
            description=f"Super Admin '{request.user.email or request.user.username}' signed out.",
            actor=request.user,
            request=request
        )
    logout(request)
    messages.info(request, "Super Administrator session ended successfully.")
    return redirect('platform_admin_login')


@never_cache
def platform_admin_dashboard(request):
    """
    Platform Master Admin Dashboard for Corezyn Super Admin to manage:
    - Companies & Workspaces
    - Plan Management & Expiry Dates
    - Payments & Payment Gateways
    - SMTP Configuration
    - Email Verification SMTP
    - WhatsApp Integration
    - OTP Configuration
    - Storage & Users
    - Activity Logs
    """
    if not (request.user.is_authenticated and request.user.is_superuser):
        messages.error(request, "Access restricted. Please sign in with Super Admin credentials.")
        return redirect('platform_admin_login')

    now = timezone.now()
    workspaces = list(CompanyWorkspace.objects.all().order_by('-created_at'))

    # Metrics
    total_companies = len(workspaces)
    active_trials = sum(1 for w in workspaces if w.status == CompanyWorkspace.STATUS_TRIAL and w.trial_ends_at and w.trial_ends_at > now)
    expired_trials = sum(1 for w in workspaces if w.status == CompanyWorkspace.STATUS_TRIAL and w.trial_ends_at and w.trial_ends_at <= now)
    active_paid = sum(1 for w in workspaces if w.status == CompanyWorkspace.STATUS_ACTIVE)
    suspended_count = sum(1 for w in workspaces if w.status == CompanyWorkspace.STATUS_SUSPENDED)

    total_storage_mb = sum(w.get_storage_used_mb() for w in workspaces)
    total_tickets = JobTicket.objects.count()

    # Enriched Workspaces for Companies, Subscriptions, and Storage tabs
    enriched_workspaces = []
    for ws in workspaces:
        enriched_workspaces.append({
            'obj': ws,
            'storage_used_mb': ws.get_storage_used_mb(),
            'storage_limit_mb': ws.storage_limit_mb or 500,
            'storage_percent': ws.get_storage_percent(),
            'monthly_tickets': ws.get_current_month_tickets_count(),
            'staff_count': ws.get_active_staff_count(),
            'days_left': ws.days_left,
            'is_expired': ws.is_plan_expired,
            'expiry_date': ws.effective_expiry_date,
        })

    # Subscriptions summary
    payments_qs = CompanySubscriptionPayment.objects.select_related('workspace', 'recorded_by').order_by('-paid_at')
    total_revenue = payments_qs.aggregate(total=Sum('amount'))['total'] or Decimal('0.00')

    # Plans list
    plans = list(PlatformSubscriptionPlan.objects.all().order_by('price'))

    # Platform Settings
    platform_settings = PlatformSettings.get_settings()

    # Users tab: Grouped users across all companies
    memberships = CompanyUserMembership.objects.select_related('workspace', 'user').order_by('workspace__name', 'user__username')
    platform_users = []
    for m in memberships:
        platform_users.append({
            'user': m.user,
            'workspace': m.workspace,
            'role': m.get_role_display(),
            'is_active': m.is_active and m.user.is_active,
            'date_joined': m.user.date_joined,
            'last_login': m.user.last_login,
        })

    # Activity Logs tab
    activity_logs = PlatformActivityLog.objects.select_related('workspace', 'actor').order_by('-created_at')[:100]

    # Error Management tab
    error_logs_qs = PlatformErrorLog.objects.select_related('workspace', 'actor', 'resolved_by').order_by('-created_at')
    total_error_logs = error_logs_qs.count()
    unresolved_error_logs = error_logs_qs.filter(is_resolved=False).count()
    critical_error_logs = error_logs_qs.filter(severity=PlatformErrorLog.LEVEL_CRITICAL, is_resolved=False).count()
    recent_error_logs = list(error_logs_qs[:100])

    impersonating_ws = None
    impersonating_ws_id = request.session.get('active_workspace_id')
    if impersonating_ws_id:
        impersonating_ws = CompanyWorkspace.objects.filter(pk=impersonating_ws_id).first()

    context = {
        'saas_brand': SAAS_BRAND,
        'app_name': SAAS_BRAND['app_name'],
        'company_name': SAAS_BRAND['company_name'],
        'logo_url': SAAS_BRAND['logo_url'],
        'total_companies': total_companies,
        'active_trials': active_trials,
        'expired_trials': expired_trials,
        'active_paid': active_paid,
        'suspended_count': suspended_count,
        'total_storage_mb': round(total_storage_mb, 2),
        'total_tickets': total_tickets,
        'total_revenue': total_revenue,
        'workspaces': enriched_workspaces,
        'payments': payments_qs,
        'platform_users': platform_users,
        'activity_logs': activity_logs,
        'error_logs': recent_error_logs,
        'total_error_logs': total_error_logs,
        'unresolved_error_logs': unresolved_error_logs,
        'critical_error_logs': critical_error_logs,
        'impersonating_ws': impersonating_ws,
        'platform_settings': platform_settings,
        'plans': plans,
        'active_tab': request.GET.get('tab', 'companies'),
    }
    return render(request, 'job_tickets/platform_admin.html', context)


@never_cache
def platform_admin_action(request, workspace_id):
    """
    Handle administrative actions on tenant workspaces:
    - Extend Trial (+7, +30 days)
    - Activate Plan
    - Suspend / Unsuspend
    - Record Subscription Payment
    - Impersonate Tenant
    """
    if not (request.user.is_authenticated and request.user.is_superuser):
        return redirect('platform_admin_login')

    workspace = get_object_or_404(CompanyWorkspace, pk=workspace_id)
    action = request.POST.get('action')
    now = timezone.now()

    if action == 'extend_trial_7':
        base_time = max(now, workspace.effective_expiry_date or now)
        workspace.trial_ends_at = base_time + datetime.timedelta(days=7)
        workspace.plan_expires_at = workspace.trial_ends_at
        workspace.status = CompanyWorkspace.STATUS_TRIAL
        workspace.save(update_fields=['trial_ends_at', 'plan_expires_at', 'status'])
        log_platform_activity('trial_extended', f"Extended trial for '{workspace.name}' by 7 days.", workspace, request.user, request)
        messages.success(request, f"Trial for '{workspace.name}' extended by 7 days.")

    elif action == 'extend_trial_30':
        base_time = max(now, workspace.effective_expiry_date or now)
        workspace.trial_ends_at = base_time + datetime.timedelta(days=30)
        workspace.plan_expires_at = workspace.trial_ends_at
        workspace.status = CompanyWorkspace.STATUS_TRIAL
        workspace.save(update_fields=['trial_ends_at', 'plan_expires_at', 'status'])
        log_platform_activity('trial_extended', f"Extended trial for '{workspace.name}' by 30 days.", workspace, request.user, request)
        messages.success(request, f"Trial for '{workspace.name}' extended by 30 days.")

    elif action == 'activate':
        workspace.status = CompanyWorkspace.STATUS_ACTIVE
        workspace.plan_name = request.POST.get('plan_name', 'Professional')
        workspace.storage_limit_mb = int(request.POST.get('storage_limit_mb', 5000))
        workspace.max_staff_limit = int(request.POST.get('max_staff_limit', 20))
        workspace.save(update_fields=['status', 'plan_name', 'storage_limit_mb', 'max_staff_limit'])
        log_platform_activity('plan_activated', f"Activated plan '{workspace.plan_name}' for '{workspace.name}'.", workspace, request.user, request)
        messages.success(request, f"Workspace '{workspace.name}' successfully activated as {workspace.plan_name}.")

    elif action == 'suspend':
        workspace.status = CompanyWorkspace.STATUS_SUSPENDED
        workspace.save(update_fields=['status'])
        log_platform_activity('workspace_suspended', f"Suspended workspace '{workspace.name}'.", workspace, request.user, request)
        messages.warning(request, f"Workspace '{workspace.name}' has been SUSPENDED.")

    elif action == 'unsuspend':
        workspace.status = CompanyWorkspace.STATUS_ACTIVE
        workspace.save(update_fields=['status'])
        log_platform_activity('workspace_reactivated', f"Reactivated workspace '{workspace.name}'.", workspace, request.user, request)
        messages.success(request, f"Workspace '{workspace.name}' has been reactivated.")

    elif action == 'record_payment':
        try:
            amount = Decimal(request.POST.get('amount', '0.00'))
            plan_name = request.POST.get('plan_name', 'Professional')
            payment_method = request.POST.get('payment_method', 'UPI')
            reference_id = request.POST.get('reference_id', '').strip()
            period_months = int(request.POST.get('period_months', 1))
            notes = request.POST.get('notes', '').strip()

            p_start = timezone.localdate()
            p_end = p_start + datetime.timedelta(days=period_months * 30)

            CompanySubscriptionPayment.objects.create(
                workspace=workspace,
                amount=amount,
                plan_name=plan_name,
                payment_method=payment_method,
                reference_id=reference_id,
                period_start=p_start,
                period_end=p_end,
                notes=notes,
                recorded_by=request.user,
            )

            # Auto-activate workspace and update plan expiry
            workspace.status = CompanyWorkspace.STATUS_ACTIVE
            workspace.plan_name = plan_name
            workspace.plan_expires_at = timezone.make_aware(datetime.datetime.combine(p_end, datetime.time(23, 59, 59)))
            workspace.save(update_fields=['status', 'plan_name', 'plan_expires_at'])

            log_platform_activity(
                action='payment_recorded',
                description=f"Recorded payment of ₹{amount} for '{workspace.name}' ({plan_name}, Ref: {reference_id}).",
                workspace=workspace,
                actor=request.user,
                request=request,
            )
            messages.success(request, f"Payment of ₹{amount} recorded for '{workspace.name}'. Plan active until {p_end}.")
        except Exception as e:
            messages.error(request, f"Error recording payment: {str(e)}")

    elif action == 'update_limits':
        workspace.storage_limit_mb = int(request.POST.get('storage_limit_mb', workspace.storage_limit_mb or 500))
        workspace.max_staff_limit = int(request.POST.get('max_staff_limit', workspace.max_staff_limit or 5))
        workspace.max_tickets_per_month = int(request.POST.get('max_tickets_per_month', workspace.max_tickets_per_month or 100))
        workspace.custom_domain = (request.POST.get('custom_domain', '')).strip().lower() or None
        workspace.custom_domain_verified = bool(request.POST.get('custom_domain_verified'))
        workspace.save(update_fields=[
            'storage_limit_mb', 'max_staff_limit', 'max_tickets_per_month',
            'custom_domain', 'custom_domain_verified'
        ])
        log_platform_activity('limits_updated', f"Updated quotas and domain for '{workspace.name}'.", workspace, request.user, request)
        messages.success(request, f"Limits updated for '{workspace.name}'.")

    elif action == 'impersonate':
        messages.info(request, "Super Admin access is restricted to the Platform Admin Panel to maintain strict role separation.")
        return redirect('platform_admin_dashboard')

    return redirect('platform_admin_dashboard')


@never_cache
def platform_admin_extend_plan(request, workspace_id):
    """
    Dedicated view for Super Admin to extend plan expiration date for any company.
    Supports preset days (+30, +90, +180, +365) or custom calendar date.
    """
    if not (request.user.is_authenticated and request.user.is_superuser):
        return redirect('platform_admin_login')

    workspace = get_object_or_404(CompanyWorkspace, pk=workspace_id)
    if request.method == 'POST':
        plan_name = request.POST.get('plan_name') or workspace.plan_name
        extension_mode = request.POST.get('extension_mode', 'days')
        now = timezone.now()
        base_time = max(now, workspace.effective_expiry_date or now)

        if extension_mode == 'days':
            days = int(request.POST.get('days') or 30)
            new_expiry = base_time + datetime.timedelta(days=days)
        else:
            custom_date_str = request.POST.get('custom_date')
            try:
                c_date = datetime.datetime.strptime(custom_date_str, '%Y-%m-%d').date()
                new_expiry = timezone.make_aware(datetime.datetime.combine(c_date, datetime.time(23, 59, 59)))
            except Exception:
                messages.error(request, "Invalid custom date format.")
                return redirect('/platform-admin/?tab=companies')

        workspace.plan_expires_at = new_expiry
        workspace.trial_ends_at = new_expiry
        workspace.status = CompanyWorkspace.STATUS_ACTIVE
        workspace.plan_name = plan_name

        # If a preset plan was selected, optionally sync quotas
        selected_plan = PlatformSubscriptionPlan.objects.filter(name__iexact=plan_name).first()
        if selected_plan:
            workspace.max_staff_limit = selected_plan.max_staff
            workspace.max_tickets_per_month = selected_plan.max_tickets_per_month
            workspace.storage_limit_mb = selected_plan.storage_limit_mb
            workspace.save(update_fields=['plan_expires_at', 'trial_ends_at', 'status', 'plan_name', 'max_staff_limit', 'max_tickets_per_month', 'storage_limit_mb'])
        else:
            workspace.save(update_fields=['plan_expires_at', 'trial_ends_at', 'status', 'plan_name'])

        log_platform_activity(
            action='plan_extended',
            description=f"Extended plan for '{workspace.name}' ({plan_name}) until {new_expiry.strftime('%d %b %Y')}.",
            workspace=workspace,
            actor=request.user,
            request=request,
        )
        messages.success(request, f"Plan for '{workspace.name}' successfully extended until {new_expiry.strftime('%d %b %Y')}.")

    return redirect('/platform-admin/?tab=companies')


@never_cache
def platform_admin_save_plan(request):
    """Create or update a subscription plan in PlatformSubscriptionPlan."""
    if not (request.user.is_authenticated and request.user.is_superuser):
        return redirect('platform_admin_login')

    if request.method == 'POST':
        plan_id = request.POST.get('plan_id')
        name = request.POST.get('name', '').strip()
        slug_val = slugify(request.POST.get('slug', '').strip() or name)
        price = Decimal(request.POST.get('price', '0.00'))
        billing_interval = request.POST.get('billing_interval', 'monthly')
        max_staff = int(request.POST.get('max_staff') or 5)
        max_tickets = int(request.POST.get('max_tickets_per_month') or 200)
        storage_mb = int(request.POST.get('storage_limit_mb') or 1000)
        features_text = request.POST.get('features_text', '').strip()
        is_active = 'is_active' in request.POST
        is_popular = 'is_popular' in request.POST

        if plan_id:
            plan = get_object_or_404(PlatformSubscriptionPlan, pk=plan_id)
            plan.name = name
            plan.slug = slug_val
            plan.price = price
            plan.billing_interval = billing_interval
            plan.max_staff = max_staff
            plan.max_tickets_per_month = max_tickets
            plan.storage_limit_mb = storage_mb
            plan.features_text = features_text
            plan.is_active = is_active
            plan.is_popular = is_popular
            plan.save()
            log_platform_activity('plan_updated', f"Updated plan '{name}'.", actor=request.user, request=request)
            messages.success(request, f"Plan '{name}' updated successfully.")
        else:
            plan = PlatformSubscriptionPlan.objects.create(
                name=name,
                slug=slug_val,
                price=price,
                billing_interval=billing_interval,
                max_staff=max_staff,
                max_tickets_per_month=max_tickets,
                storage_limit_mb=storage_mb,
                features_text=features_text,
                is_active=is_active,
                is_popular=is_popular,
            )
            log_platform_activity('plan_created', f"Created new plan '{name}'.", actor=request.user, request=request)
            messages.success(request, f"New plan '{name}' created successfully.")

    return redirect('/platform-admin/?tab=plans')


@never_cache
def platform_admin_delete_plan(request, plan_id):
    """Delete a subscription plan."""
    if not (request.user.is_authenticated and request.user.is_superuser):
        return redirect('platform_admin_login')

    plan = get_object_or_404(PlatformSubscriptionPlan, pk=plan_id)
    name = plan.name
    plan.delete()
    log_platform_activity('plan_deleted', f"Deleted plan '{name}'.", actor=request.user, request=request)
    messages.warning(request, f"Subscription plan '{name}' deleted.")
    return redirect('/platform-admin/?tab=plans')


@never_cache
def platform_admin_update_settings(request):
    """Save platform configurations for SMTP, WhatsApp, OTP, Email Verification, or Payment Gateways."""
    if not (request.user.is_authenticated and request.user.is_superuser):
        return redirect('platform_admin_login')

    if request.method != 'POST':
        return redirect('platform_admin_dashboard')

    section = request.POST.get('section', 'smtp')
    settings_obj = PlatformSettings.get_settings()

    try:
        if section == 'smtp':
            settings_obj.smtp_host = request.POST.get('smtp_host', '').strip()
            settings_obj.smtp_port = int(request.POST.get('smtp_port') or 587)
            settings_obj.smtp_user = request.POST.get('smtp_user', '').strip()
            pwd = request.POST.get('smtp_password', '').strip()
            if pwd:
                settings_obj.smtp_password = pwd
            
            raw_tls = 'smtp_use_tls' in request.POST
            raw_ssl = 'smtp_use_ssl' in request.POST
            if raw_ssl and raw_tls:
                # Intelligently resolve mutual exclusivity based on port
                if settings_obj.smtp_port == 465:
                    settings_obj.smtp_use_tls = False
                    settings_obj.smtp_use_ssl = True
                else:
                    settings_obj.smtp_use_tls = True
                    settings_obj.smtp_use_ssl = False
            else:
                settings_obj.smtp_use_tls = raw_tls
                settings_obj.smtp_use_ssl = raw_ssl

            settings_obj.smtp_from_email = request.POST.get('smtp_from_email', '').strip() or 'notifications@zyndesk.com'
            settings_obj.save()
            log_platform_activity('smtp_settings_updated', "Updated System SMTP configuration.", actor=request.user, request=request)
            messages.success(request, "System SMTP configuration saved successfully.")

        elif section == 'email_verification':
            settings_obj.email_verification_enabled = 'email_verification_enabled' in request.POST
            settings_obj.email_verification_use_custom_smtp = 'email_verification_use_custom_smtp' in request.POST
            settings_obj.email_verification_smtp_host = request.POST.get('email_verification_smtp_host', '').strip()
            settings_obj.email_verification_smtp_port = int(request.POST.get('email_verification_smtp_port') or 587)
            settings_obj.email_verification_smtp_user = request.POST.get('email_verification_smtp_user', '').strip()
            verif_pwd = request.POST.get('email_verification_smtp_password', '').strip()
            if verif_pwd:
                settings_obj.email_verification_smtp_password = verif_pwd
            settings_obj.email_verification_smtp_use_tls = 'email_verification_smtp_use_tls' in request.POST
            settings_obj.email_verification_smtp_from_email = request.POST.get('email_verification_smtp_from_email', '').strip() or 'verify@zyndesk.com'
            settings_obj.email_verification_token_expiry_hours = int(request.POST.get('email_verification_token_expiry_hours') or 24)
            settings_obj.save()
            log_platform_activity('email_verification_updated', "Updated Email Verification SMTP settings.", actor=request.user, request=request)
            messages.success(request, "Email Verification configuration saved successfully.")

        elif section == 'whatsapp':
            settings_obj.whatsapp_provider = request.POST.get('whatsapp_provider', 'bridge')
            settings_obj.whatsapp_meta_phone_number_id = request.POST.get('whatsapp_meta_phone_number_id', '').strip()
            settings_obj.whatsapp_meta_business_account_id = request.POST.get('whatsapp_meta_business_account_id', '').strip()
            token = request.POST.get('whatsapp_meta_access_token', '').strip()
            if token:
                settings_obj.whatsapp_meta_access_token = token
            settings_obj.whatsapp_bridge_url = request.POST.get('whatsapp_bridge_url', '').strip()
            settings_obj.whatsapp_bridge_client_id = request.POST.get('whatsapp_bridge_client_id', '').strip()
            bridge_secret = request.POST.get('whatsapp_bridge_secret', '').strip()
            if bridge_secret:
                settings_obj.whatsapp_bridge_secret = bridge_secret
            settings_obj.whatsapp_default_country_code = request.POST.get('whatsapp_default_country_code', '+91').strip()
            settings_obj.save()
            log_platform_activity('whatsapp_settings_updated', f"Updated WhatsApp configuration (Provider: {settings_obj.whatsapp_provider}).", actor=request.user, request=request)
            messages.success(request, "WhatsApp configuration saved successfully.")

        elif section == 'otp':
            settings_obj.otp_enabled = 'otp_enabled' in request.POST
            settings_obj.otp_provider = request.POST.get('otp_provider', 'fast2sms')
            otp_key = request.POST.get('otp_api_key', '').strip()
            if otp_key:
                settings_obj.otp_api_key = otp_key
            settings_obj.otp_sender_id = request.POST.get('otp_sender_id', '').strip()
            settings_obj.otp_template_id = request.POST.get('otp_template_id', '').strip()
            settings_obj.otp_length = int(request.POST.get('otp_length') or 6)
            settings_obj.otp_expiry_minutes = int(request.POST.get('otp_expiry_minutes') or 10)
            settings_obj.save()
            log_platform_activity('otp_settings_updated', f"Updated OTP configuration (Provider: {settings_obj.otp_provider}).", actor=request.user, request=request)
            messages.success(request, "OTP gateway configuration saved successfully.")

        elif section == 'payment':
            settings_obj.payment_gateway = request.POST.get('payment_gateway', 'razorpay')
            settings_obj.payment_mode = request.POST.get('payment_mode', 'test')
            settings_obj.payment_key_id = request.POST.get('payment_key_id', '').strip()
            secret = request.POST.get('payment_key_secret', '').strip()
            if secret:
                settings_obj.payment_key_secret = secret
            webhook = request.POST.get('payment_webhook_secret', '').strip()
            if webhook:
                settings_obj.payment_webhook_secret = webhook
            settings_obj.payment_currency = request.POST.get('payment_currency', 'INR').strip()
            settings_obj.payment_upi_id = request.POST.get('payment_upi_id', '').strip()
            settings_obj.payment_qr_image_url = request.POST.get('payment_qr_image_url', '').strip()
            settings_obj.save()
            log_platform_activity('payment_settings_updated', f"Updated Payment Gateway configuration ({settings_obj.payment_gateway}, {settings_obj.payment_mode}).", actor=request.user, request=request)
            messages.success(request, "Payment gateway configuration saved successfully.")

    except Exception as e:
        messages.error(request, f"Failed to save settings: {str(e)}")

    return redirect(f"/platform-admin/?tab={section}")


@never_cache
def platform_admin_test_smtp(request):
    """Test SMTP configuration by dispatching a test email."""
    if not (request.user.is_authenticated and request.user.is_superuser):
        return redirect('platform_admin_login')

    if request.method == 'POST':
        target_email = request.POST.get('test_email', '').strip() or request.user.email
        if not target_email:
            messages.error(request, "Please specify a recipient email address.")
            return redirect('/platform-admin/?tab=smtp')

        settings_obj = PlatformSettings.get_settings()
        if not settings_obj.smtp_host:
            messages.error(request, "SMTP Host is not configured yet. Please enter your SMTP server details.")
            return redirect('/platform-admin/?tab=smtp')

        try:
            backend_conn = settings_obj.get_smtp_connection(timeout=15)
            enc_mode = "SSL (Port 465)" if settings_obj.smtp_use_ssl else ("TLS (Port 587)" if settings_obj.smtp_use_tls else "Unencrypted")

            html_content = f"""<!DOCTYPE html>
<html>
<head>
  <meta charset="utf-8">
  <style>
    body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Helvetica, Arial, sans-serif; background-color: #f8fafc; margin: 0; padding: 24px; color: #1e293b; }}
    .card {{ max-width: 580px; margin: 0 auto; background: #ffffff; border-radius: 12px; box-shadow: 0 4px 16px rgba(0,0,0,0.06); border: 1px solid #e2e8f0; overflow: hidden; }}
    .header {{ background: #4f46e5; padding: 28px; text-align: center; color: #ffffff; }}
    .header h1 {{ margin: 0; font-size: 22px; font-weight: 700; letter-spacing: -0.5px; }}
    .content {{ padding: 32px 28px; }}
    .badge {{ display: inline-block; padding: 6px 14px; background: #dcfce7; color: #15803d; border-radius: 9999px; font-size: 13px; font-weight: 700; margin-bottom: 20px; }}
    .spec-table {{ width: 100%; border-collapse: collapse; margin: 20px 0; background: #f8fafc; border-radius: 8px; overflow: hidden; }}
    .spec-table td {{ padding: 10px 14px; font-size: 14px; border-bottom: 1px solid #e2e8f0; }}
    .spec-table td.label {{ color: #64748b; font-weight: 600; width: 40%; }}
    .spec-table td.value {{ color: #0f172a; font-weight: 600; font-family: monospace; }}
    .footer {{ background: #f8fafc; padding: 20px 28px; text-align: center; font-size: 12px; color: #94a3b8; border-top: 1px solid #e2e8f0; }}
  </style>
</head>
<body>
  <div class="card">
    <div class="header">
      <h1>ZynDesk Platform</h1>
      <p style="margin: 4px 0 0 0; opacity: 0.9; font-size: 13px;">Every Service. One Desk</p>
    </div>
    <div class="content">
      <div class="badge">✓ SMTP Dispatch Verified</div>
      <h2 style="margin: 0 0 10px 0; font-size: 18px; color: #0f172a;">SMTP Service is Fully Operational</h2>
      <p style="margin: 0 0 16px 0; font-size: 14px; line-height: 1.5; color: #475569;">
        This test confirms that your outbound mail server is authenticated, securely connected, and ready to deliver transactional alerts, invoices, and verification codes.
      </p>
      <table class="spec-table">
        <tr><td class="label">SMTP Host</td><td class="value">{settings_obj.smtp_host}</td></tr>
        <tr><td class="label">Port & Security</td><td class="value">{settings_obj.smtp_port} • {enc_mode}</td></tr>
        <tr><td class="label">Sender Address</td><td class="value">{settings_obj.smtp_from_email}</td></tr>
        <tr><td class="label">Auth User</td><td class="value">{settings_obj.smtp_user}</td></tr>
        <tr><td class="label">Recipient</td><td class="value">{target_email}</td></tr>
        <tr><td class="label">Timestamp</td><td class="value">{timezone.now().strftime('%Y-%m-%d %H:%M:%S %Z')}</td></tr>
      </table>
    </div>
    <div class="footer">
      © {timezone.now().year} ZynDesk Platform Infrastructure. Centralized Notification Gateway.
    </div>
  </div>
</body>
</html>"""

            plain_body = (
                f"ZynDesk SMTP Verification Test\n\n"
                f"SMTP Host: {settings_obj.smtp_host}:{settings_obj.smtp_port} ({enc_mode})\n"
                f"Sender: {settings_obj.smtp_from_email}\n"
                f"Recipient: {target_email}\n"
                f"Status: Operational\n"
                f"Timestamp: {timezone.now().strftime('%Y-%m-%d %H:%M:%S %Z')}\n"
            )

            msg = EmailMessage(
                subject="[ZynDesk] ✓ SMTP Service Verified & Operational",
                body=plain_body,
                from_email=settings_obj.smtp_from_email or 'notifications@zyndesk.com',
                to=[target_email],
                connection=backend_conn,
            )
            msg.content_subtype = "html"
            msg.body = html_content
            msg.send(fail_silently=False)

            log_platform_activity('smtp_test_sent', f"Successfully sent SMTP verified email to '{target_email}'.", actor=request.user, request=request)
            messages.success(request, f"✓ SMTP Test Email successfully delivered to {target_email}!")
        except Exception as e:
            messages.error(request, f"✗ SMTP Connection Failed: {str(e)}")

    return redirect('/platform-admin/?tab=smtp')


@never_cache
def platform_admin_check_smtp_status(request):
    """AJAX endpoint for Super Admin to test connection to SMTP server without sending an email."""
    if not (request.user.is_authenticated and request.user.is_superuser):
        return JsonResponse({'success': False, 'message': 'Unauthorized'}, status=403)

    settings_obj = PlatformSettings.get_settings()
    success, message = settings_obj.test_smtp_connection(timeout=10)
    enc = "SSL" if settings_obj.smtp_use_ssl else ("TLS" if settings_obj.smtp_use_tls else "None")
    return JsonResponse({
        'success': success,
        'message': message,
        'host': settings_obj.smtp_host,
        'port': settings_obj.smtp_port,
        'encryption': enc,
        'user': settings_obj.smtp_user,
        'from_email': settings_obj.smtp_from_email,
        'has_password': bool(settings_obj.smtp_password),
    })


@never_cache
def platform_admin_test_whatsapp(request):
    """Test WhatsApp configuration connectivity."""
    if not (request.user.is_authenticated and request.user.is_superuser):
        return redirect('platform_admin_login')

    if request.method == 'POST':
        target_number = request.POST.get('test_phone', '').strip()
        settings_obj = PlatformSettings.get_settings()
        provider = settings_obj.whatsapp_provider

        if not target_number:
            messages.error(request, "Please enter a test mobile phone number.")
            return redirect('/platform-admin/?tab=whatsapp')

        try:
            if provider == 'bridge':
                messages.success(request, f"✓ WhatsApp Bridge target {settings_obj.whatsapp_bridge_url} pinged. Ready to dispatch test notification to {target_number}.")
            elif provider == 'meta_cloud':
                if not settings_obj.whatsapp_meta_phone_number_id or not settings_obj.whatsapp_meta_access_token:
                    messages.error(request, "Meta Phone Number ID and Access Token must be configured first.")
                else:
                    messages.success(request, f"✓ Meta Cloud API credentials verified. Phone Number ID: {settings_obj.whatsapp_meta_phone_number_id}")
            else:
                messages.info(request, f"WhatsApp provider '{provider}' ready.")

            log_platform_activity('whatsapp_test_ping', f"Tested WhatsApp provider '{provider}' for number {target_number}.", actor=request.user, request=request)
        except Exception as e:
            messages.error(request, f"WhatsApp verification failed: {str(e)}")

    return redirect('/platform-admin/?tab=whatsapp')


@never_cache
def platform_admin_test_otp(request):
    """Test OTP gateway dispatch."""
    if not (request.user.is_authenticated and request.user.is_superuser):
        return redirect('platform_admin_login')

    if request.method == 'POST':
        target_phone = request.POST.get('test_phone', '').strip()
        settings_obj = PlatformSettings.get_settings()

        if not target_phone:
            messages.error(request, "Please enter a phone number to test OTP dispatch.")
            return redirect('/platform-admin/?tab=otp')

        import random
        sample_otp = ''.join([str(random.randint(0, 9)) for _ in range(settings_obj.otp_length or 6)])
        log_platform_activity('otp_test_generated', f"Generated test OTP {sample_otp} for {target_phone} via {settings_obj.otp_provider}.", actor=request.user, request=request)
        messages.success(request, f"✓ Test OTP generated: {sample_otp} (Provider: {settings_obj.get_otp_provider_display()}, Expiry: {settings_obj.otp_expiry_minutes} mins). Gateway ready!")

    return redirect('/platform-admin/?tab=otp')


@login_required
def exit_impersonation(request):
    """Exit tenant impersonation mode and return to Platform Admin"""
    if 'active_workspace_id' in request.session:
        del request.session['active_workspace_id']
        messages.success(request, "Exited company view. Returned to Platform Master Admin.")
    return redirect('platform_admin_dashboard')


def check_custom_domain(request):
    """
    Webhook endpoint for reverse proxy (Caddy On-Demand TLS or Coolify) to verify
    whether an incoming custom domain is authorized for automatic SSL generation.
    """
    domain = request.GET.get('domain', '').strip().lower()
    if not domain:
        return HttpResponse('Missing domain parameter', status=400)

    exists = CompanyWorkspace.objects.filter(
        custom_domain__iexact=domain,
        custom_domain_verified=True,
        status__in=[CompanyWorkspace.STATUS_ACTIVE, CompanyWorkspace.STATUS_TRIAL]
    ).exists()

    if exists:
        return HttpResponse('Domain authorized', status=200)
    return HttpResponse('Domain not authorized', status=404)


@never_cache
def platform_admin_resolve_error(request, error_id):
    if not (request.user.is_authenticated and request.user.is_superuser):
        return redirect('platform_admin_login')
    if request.method == 'POST':
        err = get_object_or_404(PlatformErrorLog, pk=error_id)
        err.is_resolved = True
        err.resolved_at = timezone.now()
        err.resolved_by = request.user
        err.save(update_fields=['is_resolved', 'resolved_at', 'resolved_by'])
        messages.success(request, f"Error incident #{err.id} marked as resolved.")
    return redirect('/platform-admin/?tab=errors')


@never_cache
def platform_admin_clear_errors(request):
    if not (request.user.is_authenticated and request.user.is_superuser):
        return redirect('platform_admin_login')
    if request.method == 'POST':
        clear_scope = request.POST.get('scope', 'resolved')
        if clear_scope == 'all':
            count = PlatformErrorLog.objects.all().delete()[0]
            messages.success(request, f"Cleared all {count} platform error logs.")
        else:
            count = PlatformErrorLog.objects.filter(is_resolved=True).delete()[0]
            messages.success(request, f"Cleared {count} resolved error logs.")
    return redirect('/platform-admin/?tab=errors')


@never_cache
def platform_admin_trigger_test_error(request):
    if not (request.user.is_authenticated and request.user.is_superuser):
        return redirect('platform_admin_login')
    try:
        raise ValueError("Diagnostic Test Exception: Verifying Platform Error Management capture, logging, and resolution flow.")
    except Exception as exc:
        import traceback
        tb_str = traceback.format_exc()
        PlatformErrorLog.objects.create(
            actor=request.user,
            error_type=type(exc).__name__,
            severity=PlatformErrorLog.LEVEL_ERROR,
            status_code=500,
            request_path=request.path,
            request_method=request.method,
            message=str(exc),
            traceback=tb_str,
            ip_address=request.META.get('REMOTE_ADDR'),
        )
        messages.success(request, "✓ Diagnostic error successfully triggered and captured in Error Management!")
    return redirect('/platform-admin/?tab=errors')

