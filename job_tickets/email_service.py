import logging
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal
from django.conf import settings
from django.core.mail import EmailMessage
from django.core.signing import BadSignature, SignatureExpired, TimestampSigner
from django.urls import reverse
from django.utils import timezone

from .models import Client, CompanyProfile, CompanyWorkspace, PlatformSettings
from .whatsapp_service import create_receipt_access_token

logger = logging.getLogger(__name__)

_email_executor = ThreadPoolExecutor(max_workers=4, thread_name_prefix='email_dispatcher')
EMAIL_VERIFY_SALT = 'zyndesk-verify-email-v1'


def _get_base_url(request=None, workspace=None):
    """Determine the best public base URL for links in emails."""
    if request:
        try:
            return request.build_absolute_uri('/').rstrip('/')
        except Exception:
            pass

    if workspace:
        root_domain = getattr(settings, 'SAAS_ROOT_DOMAIN', '')
        if workspace.custom_domain and workspace.custom_domain_verified:
            return f"https://{workspace.custom_domain}".rstrip('/')
        elif root_domain:
            return f"https://{workspace.slug}.{root_domain}".rstrip('/')

    public_url = getattr(settings, 'PUBLIC_BASE_URL', '')
    if public_url:
        return public_url.rstrip('/')

    return 'https://dir.anasrawuthar.in'


def send_email_async(subject, to_emails, html_content, plain_content=None, from_email=None, use_verification_smtp=False):
    """
    Dispatch email asynchronously in a background thread to prevent blocking HTTP requests.
    Uses centralized PlatformSettings SMTP connection.
    """
    if isinstance(to_emails, str):
        to_emails = [to_emails]

    # Filter out empty or invalid emails
    to_emails = [e.strip() for e in to_emails if e and '@' in e]
    if not to_emails:
        logger.warning("send_email_async called with no valid recipient emails.")
        return False

    def _task():
        try:
            platform_settings = PlatformSettings.get_settings()
            if not platform_settings.smtp_host:
                logger.warning("SMTP host not configured in PlatformSettings. Cannot dispatch email.")
                return

            backend_conn = platform_settings.get_smtp_connection(fail_silently=False, timeout=15)
            sender = from_email or platform_settings.smtp_from_email or 'notifications@zyndesk.com'

            fallback_plain = plain_content or "Please open this email in an HTML-compatible email reader."
            msg = EmailMessage(
                subject=subject,
                body=fallback_plain,
                from_email=sender,
                to=to_emails,
                connection=backend_conn,
            )
            msg.content_subtype = "html"
            msg.body = html_content
            msg.send(fail_silently=False)
            logger.info("Email '%s' successfully sent to %s", subject, to_emails)
        except Exception as e:
            logger.error("Failed to send email '%s' to %s: %s", subject, to_emails, str(e), exc_info=True)

    _email_executor.submit(_task)
    return True


# ==============================================================================
# 1. SAAS REGISTRATION & EMAIL VERIFICATION
# ==============================================================================

EMAIL_VERIFY_SALT = 'zyndesk-verify-email-v1'
EMAIL_REJECT_SALT = 'zyndesk-reject-email-v1'


def _get_root_domain_base_url():
    """Return public base URL of the root domain."""
    root_domain = getattr(settings, 'SAAS_ROOT_DOMAIN', '') or 'anasrawuthar.in'
    return f"https://{root_domain}".rstrip('/')


def generate_email_verification_token(workspace, user):
    """Generate a tamper-proof timestamped signature for email verification."""
    signer = TimestampSigner(salt=EMAIL_VERIFY_SALT)
    payload = f"{workspace.id}:{user.id}:{user.email}"
    return signer.sign(payload)


def generate_email_rejection_token(workspace, user):
    """Generate a tamper-proof timestamped signature to cancel/reject an unauthorized registration."""
    signer = TimestampSigner(salt=EMAIL_REJECT_SALT)
    payload = f"{workspace.id}:{user.id}:{user.email}"
    return signer.sign(payload)


def verify_email_token(token, max_age_hours=24):
    """
    Validate and unsign an email verification token.
    Returns (True, workspace, user) or (False, error_message, None).
    """
    signer = TimestampSigner(salt=EMAIL_VERIFY_SALT)
    try:
        raw_val = signer.unsign(token, max_age=max_age_hours * 3600)
        parts = raw_val.split(':')
        if len(parts) != 3:
            return False, "Malformed verification token.", None
        ws_id, user_id, email = parts

        workspace = CompanyWorkspace.objects.filter(pk=int(ws_id)).first()
        if not workspace:
            return False, "Workspace not found.", None

        user = workspace.owner
        if not user or user.id != int(user_id):
            return False, "Invalid account association.", None

        # Mark workspace verified
        workspace.email_verified = True
        workspace.email_verified_at = timezone.now()
        if workspace.status == CompanyWorkspace.STATUS_SUSPENDED:
            workspace.status = CompanyWorkspace.STATUS_TRIAL
        workspace.save(update_fields=['email_verified', 'email_verified_at', 'status'])

        return True, workspace, user
    except SignatureExpired:
        return False, "Verification link has expired (valid for 24 hours). Please request a new link.", None
    except BadSignature:
        return False, "Invalid or corrupted verification link.", None
    except Exception as e:
        return False, f"Verification failed: {str(e)}", None


def reject_email_token(token, max_age_hours=72):
    """
    Validate and unsign an email rejection/cancellation token.
    Returns (True, workspace_name, user) or (False, error_message, None).
    """
    signer = TimestampSigner(salt=EMAIL_REJECT_SALT)
    try:
        raw_val = signer.unsign(token, max_age=max_age_hours * 3600)
        parts = raw_val.split(':')
        if len(parts) != 3:
            return False, "Malformed cancellation token.", None
        ws_id, user_id, email = parts

        workspace = CompanyWorkspace.objects.filter(pk=int(ws_id)).first()
        if not workspace:
            return False, "Workspace already cancelled or removed.", None

        workspace_name = workspace.name
        user = workspace.owner

        # Invalidate workspace and suspend account
        workspace.status = CompanyWorkspace.STATUS_SUSPENDED
        workspace.email_verified = False
        workspace.save(update_fields=['status', 'email_verified'])

        if user and not user.is_superuser:
            user.is_active = False
            user.save(update_fields=['is_active'])

        return True, workspace_name, user
    except SignatureExpired:
        return False, "Cancellation link has expired.", None
    except BadSignature:
        return False, "Invalid or corrupted cancellation link.", None
    except Exception as e:
        return False, f"Cancellation failed: {str(e)}", None


def send_registration_verification_email(user, workspace, request=None):
    """Send modern HTML verification email to new workspace owner with Verify & Reject links."""
    token = generate_email_verification_token(workspace, user)
    reject_token = generate_email_rejection_token(workspace, user)

    root_domain = getattr(settings, 'SAAS_ROOT_DOMAIN', '') or 'anasrawuthar.in'
    root_base_url = _get_root_domain_base_url()

    verify_url = f"{root_base_url}{reverse('saas_verify_email')}?token={token}"
    reject_url = f"{root_base_url}{reverse('saas_reject_registration')}?token={reject_token}"
    workspace_domain_url = f"https://{workspace.slug}.{root_domain}"

    full_name = user.get_full_name() or user.username
    subject = f"[ZynDesk] Action Required: Verify your email for {workspace.name}"

    html = f"""<!DOCTYPE html>
<html>
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <style>
    body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Helvetica, Arial, sans-serif; background-color: #f8fafc; margin: 0; padding: 24px; color: #1e293b; }}
    .card {{ max-width: 580px; margin: 0 auto; background: #ffffff; border-radius: 14px; box-shadow: 0 4px 20px rgba(0,0,0,0.06); border: 1px solid #e2e8f0; overflow: hidden; }}
    .header {{ background: #2563eb; padding: 32px 28px; text-align: center; color: #ffffff; }}
    .header h1 {{ margin: 0; font-size: 24px; font-weight: 800; letter-spacing: -0.5px; }}
    .header p {{ margin: 6px 0 0 0; opacity: 0.9; font-size: 13px; letter-spacing: 0.5px; }}
    .content {{ padding: 36px 32px; }}
    .greeting {{ font-size: 18px; font-weight: 700; color: #0f172a; margin-bottom: 12px; }}
    .lead {{ font-size: 15px; line-height: 1.6; color: #475569; margin-bottom: 24px; }}
    .ws-box {{ background: #f8fafc; border-radius: 10px; padding: 18px 20px; margin-bottom: 28px; border: 1px solid #e2e8f0; }}
    .ws-row {{ display: flex; justify-content: space-between; font-size: 13px; padding: 6px 0; border-bottom: 1px solid #edf2f7; }}
    .ws-row:last-child {{ border-bottom: none; }}
    .ws-label {{ color: #64748b; font-weight: 600; }}
    .ws-val {{ color: #0f172a; font-weight: 700; font-family: monospace; word-break: break-all; }}
    .btn-container {{ text-align: center; margin: 28px 0 16px 0; }}
    .btn {{ display: inline-block; background: #2563eb; color: #ffffff !important; padding: 14px 34px; border-radius: 8px; font-size: 15px; font-weight: 700; text-decoration: none; box-shadow: 0 4px 12px rgba(37, 99, 235, 0.35); }}
    .reject-box {{ background: #fff5f5; border: 1px solid #fed7d7; border-radius: 10px; padding: 16px; margin-top: 28px; text-align: center; }}
    .reject-title {{ color: #c53030; font-weight: 700; font-size: 13px; margin-bottom: 6px; }}
    .reject-text {{ color: #742a2a; font-size: 12px; margin-bottom: 10px; line-height: 1.5; }}
    .reject-link {{ color: #e53e3e; font-weight: 700; font-size: 13px; text-decoration: underline; }}
    .note {{ font-size: 12px; color: #94a3b8; text-align: center; margin-top: 24px; }}
    .fallback {{ font-size: 12px; color: #64748b; word-break: break-all; margin-top: 16px; background: #f8fafc; padding: 12px; border-radius: 6px; }}
    .footer {{ background: #f8fafc; padding: 20px; text-align: center; font-size: 12px; color: #94a3b8; border-top: 1px solid #e2e8f0; }}
  </style>
</head>
<body>
  <div class="card">
    <div class="header">
      <h1>ZynDesk</h1>
      <p>Every Service. One Desk</p>
    </div>
    <div class="content">
      <div class="greeting">Hi {full_name},</div>
      <div class="lead">
        Thank you for registering <strong>{workspace.name}</strong> with ZynDesk! Your dedicated company subdomain has been allocated.
        Please verify your email address to activate your company workspace and start using the system.
      </div>
      <div class="ws-box">
        <div class="ws-row"><span class="ws-label">Company Name:</span> <span class="ws-val">{workspace.name}</span></div>
        <div class="ws-row"><span class="ws-label">Assigned Subdomain:</span> <span class="ws-val">{workspace_domain_url}</span></div>
        <div class="ws-row"><span class="ws-label">Registered Admin Email:</span> <span class="ws-val">{user.email}</span></div>
        <div class="ws-row"><span class="ws-label">Trial Plan:</span> <span class="ws-val">7-Day Free Full Access</span></div>
      </div>
      <div class="btn-container">
        <a href="{verify_url}" class="btn" target="_blank">Verify Email & Activate Workspace</a>
      </div>
      <div class="fallback">
        If the button above does not work, copy and paste this verification link into your browser:<br>
        <a href="{verify_url}" style="color: #2563eb;">{verify_url}</a>
      </div>

      <!-- Reject / Cancel Registration Option -->
      <div class="reject-box">
        <div class="reject-title">Didn't register this company?</div>
        <div class="reject-text">
          If you did not initiate this account creation or someone used your email address without permission, you can reject and cancel this registration immediately.
        </div>
        <a href="{reject_url}" class="reject-link" target="_blank">Reject & Cancel Registration</a>
      </div>

      <div class="note">This verification link is valid for 24 hours. Your workspace login remains pending until verification is completed.</div>
    </div>
    <div class="footer">
      © {timezone.now().year} ZynDesk Platform by Corezyn. All rights reserved.
    </div>
  </div>
</body>
</html>"""

    plain = (
        f"Hi {full_name},\n\n"
        f"Thank you for registering {workspace.name} on ZynDesk!\n\n"
        f"Assigned Subdomain: {workspace_domain_url}\n"
        f"Admin Email: {user.email}\n\n"
        f"Please verify your email address to activate your workspace:\n{verify_url}\n\n"
        f"Did not register this account? Reject and cancel this registration here:\n{reject_url}\n\n"
        f"This link expires in 24 hours.\n\n"
        f"— ZynDesk Team"
    )

    return send_email_async(subject, user.email, html, plain)


def send_domain_lookup_email(workspace, recipient_email, request=None):
    """Send company portal domain link to a user who used the Find Subdomain tool."""
    root_domain = getattr(settings, 'SAAS_ROOT_DOMAIN', '') or 'anasrawuthar.in'
    workspace_domain_url = f"https://{workspace.slug}.{root_domain}"
    login_url = f"{workspace_domain_url}/login/"
    subject = f"[ZynDesk] Your Company Portal URL for {workspace.name}"

    html = f"""<!DOCTYPE html>
<html>
<head>
  <meta charset="utf-8">
  <style>
    body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Helvetica, Arial, sans-serif; background-color: #f8fafc; margin: 0; padding: 24px; color: #1e293b; }}
    .card {{ max-width: 580px; margin: 0 auto; background: #ffffff; border-radius: 14px; box-shadow: 0 4px 20px rgba(0,0,0,0.06); border: 1px solid #e2e8f0; overflow: hidden; }}
    .header {{ background: #2563eb; padding: 28px; text-align: center; color: #ffffff; }}
    .content {{ padding: 32px; }}
    .btn {{ display: inline-block; background: #2563eb; color: #ffffff !important; padding: 12px 28px; border-radius: 8px; font-size: 15px; font-weight: 700; text-decoration: none; margin-top: 16px; }}
    .footer {{ background: #f8fafc; padding: 18px; text-align: center; font-size: 12px; color: #94a3b8; }}
  </style>
</head>
<body>
  <div class="card">
    <div class="header">
      <h2 style="margin:0;">ZynDesk Domain Lookup</h2>
    </div>
    <div class="content">
      <p>Hello,</p>
      <p>We received a request to locate your assigned ZynDesk company portal.</p>
      <div style="background:#f1f5f9; padding:16px; border-radius:8px; margin:20px 0;">
        <p style="margin:4px 0;"><strong>Company:</strong> {workspace.name}</p>
        <p style="margin:4px 0;"><strong>Portal URL:</strong> <a href="{workspace_domain_url}">{workspace_domain_url}</a></p>
        <p style="margin:4px 0;"><strong>Staff & Tech Sign In:</strong> <a href="{login_url}">{login_url}</a></p>
      </div>
      <div style="text-align:center;">
        <a href="{workspace_domain_url}" class="btn">Open Company Portal</a>
      </div>
      <p style="font-size:12px; color:#64748b; margin-top:24px;">If you did not request this information, you can safely ignore this email.</p>
    </div>
    <div class="footer">© {timezone.now().year} ZynDesk Platform by Corezyn.</div>
  </div>
</body>
</html>"""

    plain = (
        f"Hello,\n\n"
        f"Here are your ZynDesk company portal details for {workspace.name}:\n\n"
        f"Portal URL: {workspace_domain_url}\n"
        f"Login URL: {login_url}\n\n"
        f"— ZynDesk Team"
    )

    return send_email_async(subject, recipient_email, html, plain)


def send_registration_welcome_email(user, workspace, request=None):
    """Send welcome onboarding email with workspace URL and quick-start instructions."""
    base_url = _get_base_url(request, workspace)
    dashboard_url = f"{base_url}{reverse('staff_dashboard')}"
    full_name = user.get_full_name() or user.username
    subject = f"Welcome to ZynDesk! Getting started with {workspace.name}"

    html = f"""<!DOCTYPE html>
<html>
<head>
  <meta charset="utf-8">
  <style>
    body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Helvetica, Arial, sans-serif; background-color: #f8fafc; margin: 0; padding: 24px; color: #1e293b; }}
    .card {{ max-width: 580px; margin: 0 auto; background: #ffffff; border-radius: 14px; box-shadow: 0 4px 20px rgba(0,0,0,0.06); border: 1px solid #e2e8f0; overflow: hidden; }}
    .header {{ background: #4f46e5; padding: 32px 28px; text-align: center; color: #ffffff; }}
    .header h1 {{ margin: 0; font-size: 24px; font-weight: 800; }}
    .header p {{ margin: 6px 0 0 0; opacity: 0.9; font-size: 13px; }}
    .content {{ padding: 36px 32px; }}
    .step-box {{ margin: 20px 0; border-left: 3px solid #4f46e5; padding-left: 16px; }}
    .step-title {{ font-weight: 700; color: #0f172a; font-size: 14px; }}
    .step-desc {{ font-size: 13px; color: #64748b; margin-top: 2px; }}
    .btn {{ display: inline-block; background: #4f46e5; color: #ffffff !important; padding: 14px 32px; border-radius: 8px; font-size: 15px; font-weight: 700; text-decoration: none; }}
    .footer {{ background: #f8fafc; padding: 20px; text-align: center; font-size: 12px; color: #94a3b8; border-top: 1px solid #e2e8f0; }}
  </style>
</head>
<body>
  <div class="card">
    <div class="header">
      <h1>ZynDesk</h1>
      <p>Every Service. One Desk</p>
    </div>
    <div class="content">
      <h2 style="margin: 0 0 12px 0; font-size: 20px; color: #0f172a;">Welcome to ZynDesk, {full_name}!</h2>
      <p style="color: #475569; font-size: 14px; line-height: 1.6;">
        Your workspace <strong>{workspace.name}</strong> is live with a <strong>7-Day Free Trial</strong>.
      </p>
      <h4 style="margin: 24px 0 12px 0; font-size: 14px; text-transform: uppercase; color: #64748b; letter-spacing: 0.5px;">Quick Setup Guide</h4>
      <div class="step-box">
        <div class="step-title">1. Configure Company Profile</div>
        <div class="step-desc">Upload your business logo, address, and GST details for tax-compliant invoices.</div>
      </div>
      <div class="step-box">
        <div class="step-title">2. Add Technicians & Staff</div>
        <div class="step-desc">Create technician logins and equip them with the ZynDesk mobile app.</div>
      </div>
      <div class="step-box">
        <div class="step-title">3. Create Your First Repair Job Ticket</div>
        <div class="step-desc">Accept devices, record customer faults, assign racks, and send automated receipts.</div>
      </div>
      <div style="text-align: center; margin: 32px 0;">
        <a href="{dashboard_url}" class="btn" target="_blank">Open Your ZynDesk Dashboard</a>
      </div>
    </div>
    <div class="footer">
      © {timezone.now().year} ZynDesk Platform. Need help? Reply directly to this email.
    </div>
  </div>
</body>
</html>"""

    plain = (
        f"Welcome to ZynDesk, {full_name}!\n\n"
        f"Your workspace {workspace.name} is ready.\n"
        f"Access your dashboard here: {dashboard_url}\n\n"
        f"— ZynDesk Team"
    )

    return send_email_async(subject, user.email, html, plain)


# ==============================================================================
# 2. JOB TICKET INTAKE RECEIPT EMAIL
# ==============================================================================

def get_job_customer_email(job):
    """Extract customer email from Client directory associated with the job's customer phone."""
    if not job.customer_phone:
        return None
    qs = Client.objects.filter(phone=job.customer_phone)
    if job.workspace:
        qs = qs.filter(workspace=job.workspace)
    client_obj = qs.first()
    return (client_obj.email or '').strip() if client_obj else None


def send_job_ticket_intake_email(job, recipient_email=None, request=None):
    """
    Send a branded digital repair ticket receipt email to the customer with live status tracking.
    """
    target_email = (recipient_email or get_job_customer_email(job) or '').strip()
    if not target_email or '@' not in target_email:
        return False, "Customer email address not found or invalid."

    workspace = job.workspace
    company = CompanyProfile.get_profile(workspace)
    base_url = _get_base_url(request, workspace)

    access_token = create_receipt_access_token(job)
    status_url = f"{base_url}{reverse('client_status', args=[job.job_code])}?token={access_token}"
    receipt_url = f"{base_url}{reverse('job_creation_receipt_public', args=[job.job_code])}?token={access_token}&autoprint=0"

    company_name = company.company_name or 'Service Center'
    subject = f"[{company_name}] Repair Ticket #{job.job_code} Created - {job.device_brand} {job.device_model}"

    est_cost = f"₹{job.estimated_amount:,.2f}" if job.estimated_amount else "Pending Diagnostic Inspection"
    est_date = job.estimated_delivery.strftime('%d %b %Y') if job.estimated_delivery else "Will be notified upon inspection"
    intake_str = job.created_at.strftime('%d %b %Y, %I:%M %p') if job.created_at else timezone.now().strftime('%d %b %Y')

    html = f"""<!DOCTYPE html>
<html>
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <style>
    body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Helvetica, Arial, sans-serif; background-color: #f8fafc; margin: 0; padding: 24px; color: #1e293b; }}
    .card {{ max-width: 600px; margin: 0 auto; background: #ffffff; border-radius: 14px; box-shadow: 0 4px 20px rgba(0,0,0,0.06); border: 1px solid #e2e8f0; overflow: hidden; }}
    .header {{ background: #1e293b; padding: 28px; text-align: center; color: #ffffff; }}
    .header h1 {{ margin: 0; font-size: 22px; font-weight: 700; }}
    .header p {{ margin: 4px 0 0 0; opacity: 0.8; font-size: 13px; }}
    .content {{ padding: 32px 28px; }}
    .ticket-badge {{ display: inline-block; padding: 6px 14px; background: #e0e7ff; color: #3730a3; border-radius: 9999px; font-size: 13px; font-weight: 800; font-family: monospace; margin-bottom: 16px; }}
    .spec-table {{ width: 100%; border-collapse: collapse; margin: 20px 0; background: #f8fafc; border-radius: 8px; overflow: hidden; border: 1px solid #e2e8f0; }}
    .spec-table td {{ padding: 10px 14px; font-size: 13px; border-bottom: 1px solid #e2e8f0; }}
    .spec-table td.label {{ color: #64748b; font-weight: 600; width: 36%; }}
    .spec-table td.val {{ color: #0f172a; font-weight: 600; }}
    .btn-wrap {{ display: flex; flex-direction: column; gap: 10px; margin: 28px 0; text-align: center; }}
    .btn-track {{ display: inline-block; background: #4f46e5; color: #ffffff !important; padding: 14px 24px; border-radius: 8px; font-size: 14px; font-weight: 700; text-decoration: none; }}
    .btn-receipt {{ display: inline-block; background: #f1f5f9; color: #334155 !important; padding: 10px 20px; border-radius: 8px; font-size: 13px; font-weight: 600; text-decoration: none; border: 1px solid #cbd5e1; }}
    .footer {{ background: #f8fafc; padding: 20px 28px; text-align: center; font-size: 12px; color: #94a3b8; border-top: 1px solid #e2e8f0; }}
  </style>
</head>
<body>
  <div class="card">
    <div class="header">
      <h1>{company_name}</h1>
      <p>{company.tagline or 'Service & Repair Center'}</p>
    </div>
    <div class="content">
      <div class="ticket-badge">JOB #{job.job_code}</div>
      <h2 style="margin: 0 0 10px 0; font-size: 18px; color: #0f172a;">Device Received for Service</h2>
      <p style="margin: 0 0 16px 0; font-size: 14px; color: #475569; line-height: 1.5;">
        Dear {job.customer_name}, we have logged your device intake into our service center. Our technicians have initiated inspection.
      </p>

      <table class="spec-table">
        <tr><td class="label">Job Ticket</td><td class="val" style="color: #4f46e5;">#{job.job_code}</td></tr>
        <tr><td class="label">Device</td><td class="val">{job.device_brand} {job.device_model} ({job.device_type})</td></tr>
        <tr><td class="label">Serial / IMEI</td><td class="val">{job.device_serial or '—'}</td></tr>
        <tr><td class="label">Reported Fault</td><td class="val">{job.reported_issue}</td></tr>
        <tr><td class="label">Accessories</td><td class="val">{job.additional_items or 'None'}</td></tr>
        <tr><td class="label">Intake Date</td><td class="val">{intake_str}</td></tr>
        <tr><td class="label">Estimated Delivery</td><td class="val">{est_date}</td></tr>
        <tr><td class="label">Estimated Cost</td><td class="val" style="color: #15803d;">{est_cost}</td></tr>
      </table>

      <div class="btn-wrap">
        <a href="{status_url}" class="btn-track" target="_blank">🔍 Track Repair Status Live Online</a>
        <a href="{receipt_url}" class="btn-receipt" target="_blank">📄 View & Print Digital Intake Receipt</a>
      </div>

      <div style="font-size: 12px; color: #64748b; line-height: 1.5;">
        <strong>Need support?</strong> Call {company.phone1 or 'our support team'} or visit our workshop with your ticket code <strong>#{job.job_code}</strong>.
      </div>
    </div>
    <div class="footer">
      {company.company_name} • {company.address or ''} • Powered by ZynDesk
    </div>
  </div>
</body>
</html>"""

    plain = (
        f"Repair Ticket #{job.job_code} Created\n\n"
        f"Dear {job.customer_name},\n"
        f"Your device {job.device_brand} {job.device_model} ({job.device_type}) was checked in at {company_name}.\n"
        f"Reported Issue: {job.reported_issue}\n"
        f"Estimated Cost: {est_cost}\n\n"
        f"Track your repair progress live: {status_url}\n"
        f"Digital Receipt: {receipt_url}\n\n"
        f"— {company_name}"
    )

    send_email_async(subject, target_email, html, plain)
    return True, f"Intake receipt successfully emailed to {target_email}."


# ==============================================================================
# 3. JOB INVOICE / BILL DELIVERY EMAIL
# ==============================================================================

def send_job_invoice_email(job, recipient_email=None, request=None):
    """
    Send completed repair Tax Invoice & Bill delivery email with itemized services, parts, and PDF link.
    """
    from .views.helpers import build_job_billing_context

    target_email = (recipient_email or get_job_customer_email(job) or '').strip()
    if not target_email or '@' not in target_email:
        return False, "Customer email address not found or invalid."

    context = build_job_billing_context(job, request=request)
    company = context.get('company')
    workspace = job.workspace
    base_url = _get_base_url(request, workspace)

    access_token = create_receipt_access_token(job)
    bill_url = f"{base_url}{reverse('client_bill_view', args=[job.job_code])}?token={access_token}"

    company_name = company.company_name or 'Service Center'
    subject = f"[{company_name}] Service Invoice #{job.job_code} - {job.device_brand} {job.device_model}"

    subtotal = context.get('subtotal', Decimal('0.00'))
    discount = context.get('discount', Decimal('0.00'))
    grand_total = context.get('grand_total', Decimal('0.00'))
    amount_paid = context.get('amount_paid', Decimal('0.00'))
    balance_due = context.get('balance_due', Decimal('0.00'))
    cleaned_logs = context.get('cleaned_service_logs', [])

    # Build rows
    items_html = ""
    for idx, item in enumerate(cleaned_logs, 1):
        items_html += f"""
        <tr>
          <td style="padding: 8px 12px; border-bottom: 1px solid #e2e8f0; font-size: 13px;">{idx}</td>
          <td style="padding: 8px 12px; border-bottom: 1px solid #e2e8f0; font-size: 13px; font-weight: 600;">{item.get('description', '')}</td>
          <td style="padding: 8px 12px; border-bottom: 1px solid #e2e8f0; font-size: 13px; text-align: center;">{item.get('quantity', 1)}</td>
          <td style="padding: 8px 12px; border-bottom: 1px solid #e2e8f0; font-size: 13px; text-align: right; font-weight: 600;">₹{item.get('total_price', '0.00')}</td>
        </tr>
        """

    is_paid = balance_due <= Decimal('0.00')
    status_badge = '<span style="display:inline-block; padding: 4px 12px; background: #dcfce7; color: #15803d; border-radius: 9999px; font-size: 12px; font-weight: 800;">PAID IN FULL</span>' if is_paid else f'<span style="display:inline-block; padding: 4px 12px; background: #fee2e2; color: #b91c1c; border-radius: 9999px; font-size: 12px; font-weight: 800;">BALANCE DUE: ₹{balance_due:,.2f}</span>'

    html = f"""<!DOCTYPE html>
<html>
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <style>
    body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Helvetica, Arial, sans-serif; background-color: #f8fafc; margin: 0; padding: 24px; color: #1e293b; }}
    .card {{ max-width: 620px; margin: 0 auto; background: #ffffff; border-radius: 14px; box-shadow: 0 4px 20px rgba(0,0,0,0.06); border: 1px solid #e2e8f0; overflow: hidden; }}
    .header {{ background: #1e293b; padding: 28px; text-align: center; color: #ffffff; }}
    .header h1 {{ margin: 0; font-size: 22px; font-weight: 700; }}
    .header p {{ margin: 4px 0 0 0; opacity: 0.8; font-size: 13px; }}
    .content {{ padding: 32px 28px; }}
    .table {{ width: 100%; border-collapse: collapse; margin: 18px 0; background: #f8fafc; border-radius: 8px; overflow: hidden; border: 1px solid #e2e8f0; }}
    .table th {{ background: #f1f5f9; padding: 10px 12px; font-size: 12px; text-transform: uppercase; color: #64748b; text-align: left; }}
    .totals {{ width: 100%; margin-top: 14px; }}
    .totals td {{ padding: 6px 12px; font-size: 13px; }}
    .totals td.val {{ text-align: right; font-weight: 700; font-family: monospace; }}
    .btn {{ display: inline-block; background: #4f46e5; color: #ffffff !important; padding: 14px 28px; border-radius: 8px; font-size: 14px; font-weight: 700; text-decoration: none; box-shadow: 0 4px 12px rgba(79, 70, 229, 0.35); }}
    .footer {{ background: #f8fafc; padding: 20px; text-align: center; font-size: 12px; color: #94a3b8; border-top: 1px solid #e2e8f0; }}
  </style>
</head>
<body>
  <div class="card">
    <div class="header">
      <h1>{company_name}</h1>
      <p>Service Invoice & Delivery Receipt</p>
    </div>
    <div class="content">
      <div style="display: flex; justify-content: space-between; align-items: center; margin-bottom: 20px;">
        <div>
          <span style="font-weight: 800; font-size: 16px; color: #0f172a;">Invoice #{job.job_code}</span>
          <div style="font-size: 12px; color: #64748b;">Device: {job.device_brand} {job.device_model}</div>
        </div>
        <div>
          {status_badge}
        </div>
      </div>

      <p style="font-size: 14px; color: #475569; margin: 0 0 16px 0;">
        Dear {job.customer_name}, your repair service has been finalized. Below is your detailed service summary:
      </p>

      <table class="table">
        <thead>
          <tr>
            <th style="width: 8%;">#</th>
            <th>Item / Service Description</th>
            <th style="text-align: center; width: 14%;">Qty</th>
            <th style="text-align: right; width: 22%;">Amount</th>
          </tr>
        </thead>
        <tbody>
          {items_html if items_html else '<tr><td colspan="4" style="text-align:center; padding: 14px; color: #94a3b8;">General Diagnostic & Hardware Repair Service</td></tr>'}
        </tbody>
      </table>

      <table class="totals">
        <tr><td style="color: #64748b;">Subtotal</td><td class="val">₹{subtotal:,.2f}</td></tr>
        {f'<tr><td style="color: #64748b;">Discount</td><td class="val" style="color: #15803d;">-₹{discount:,.2f}</td></tr>' if discount > 0 else ''}
        <tr style="border-top: 1px solid #e2e8f0; font-size: 15px;"><td style="font-weight: 700; color: #0f172a;">Grand Total</td><td class="val" style="font-size: 16px; color: #0f172a;">₹{grand_total:,.2f}</td></tr>
        <tr><td style="color: #64748b;">Amount Paid</td><td class="val" style="color: #15803d;">₹{amount_paid:,.2f}</td></tr>
        <tr style="border-top: 1px solid #e2e8f0;"><td style="font-weight: 700; color: #b91c1c;">Balance Due</td><td class="val" style="color: #b91c1c; font-size: 15px;">₹{balance_due:,.2f}</td></tr>
      </table>

      <div style="text-align: center; margin: 32px 0;">
        <a href="{bill_url}" class="btn" target="_blank">📄 View & Download Full Tax Invoice / PDF</a>
      </div>

      <div style="font-size: 12px; color: #64748b; line-height: 1.5; border-top: 1px solid #e2e8f0; padding-top: 16px;">
        <strong>Warranty Terms:</strong> {company.warranty_policy or 'Standard 30-day service warranty on serviced components.'}
      </div>
    </div>
    <div class="footer">
      {company.company_name} • Phone: {company.phone1 or ''} • GSTIN: {company.gstin or 'N/A'} • Powered by ZynDesk
    </div>
  </div>
</body>
</html>"""

    plain = (
        f"Invoice #{job.job_code} from {company_name}\n\n"
        f"Dear {job.customer_name},\n"
        f"Your device {job.device_brand} {job.device_model} repair is completed.\n"
        f"Grand Total: ₹{grand_total:,.2f}\n"
        f"Amount Paid: ₹{amount_paid:,.2f}\n"
        f"Balance Due: ₹{balance_due:,.2f}\n\n"
        f"View and download your official invoice: {bill_url}\n\n"
        f"— {company_name}"
    )

    send_email_async(subject, target_email, html, plain)
    return True, f"Tax invoice successfully emailed to {target_email}."
