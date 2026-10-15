# Shared imports, constants, and helper functions extracted from the former
# monolithic job_tickets/views.py module.
from django.shortcuts import render, redirect, get_object_or_404
from django.contrib.auth import authenticate, login, logout, update_session_auth_hash
from django.contrib.auth.decorators import login_required
from django.contrib.auth.models import Group, User
from django.contrib.auth.password_validation import validate_password
from ..forms import TechnicianCreationForm
from ..access_control import (
    ACCESS_OPTIONS,
    apply_staff_access,
    clear_staff_access,
    get_staff_access,
    parse_access_keys,
    user_has_staff_access,
)
from datetime import datetime, timedelta, date
import calendar
from django.db.models import Max, Q, Sum, Count, F, DecimalField, Value, OuterRef, Subquery, IntegerField, ExpressionWrapper, Case, When, DateTimeField
from ..models import (
    Assignment,
    Client,
    CompanyProfile,
    CompanyUserMembership,
    DailyJobCodeSequence,
    DeviceChecklistTemplate,
    DeviceChecklistField,
    DeviceRack,
    Expense,
    FinancialAccount,
    AccountTransaction,
    InventoryBill,
    InventoryBillLog,
    InventoryCreditPayment,
    InventoryEntry,
    InventoryParty,
    ItemCategory,
    InventoryNumberSequence,
    InventoryVendorCredit,
    InventoryVendorCreditApplication,
    JobFieldPreset,
    JobReminder,
    JobTicket,
    JobTicketPhoto,
    JobTicketLog,
    MessageQueue,
    Product,
    ProductItemSerial,
    ProductSale,
    ServiceLog,
    SpecializedService,
    Task,
    TaskAttachment,
    TaskMessage,
    TechnicianProfile,
    UserSessionActivity,
    Vendor,
    VendorPayment,
    WhatsAppIntegrationSettings,
    MobileAppRelease,
    MobileQrLoginToken,
)
from ..forms import JobTicketForm, AssignJobForm, ServiceLogForm, ReworkForm, DiscountForm, AssignVendorForm, ReturnVendorServiceForm, ReassignTechnicianForm, TaskCreateForm, TaskMessageForm, ExpenseForm, VendorForm, FeedbackForm, CompanyProfileForm, ClientForm, ProductForm, InventoryPartyForm, InventoryEntryForm, WhatsAppIntegrationSettingsForm, get_assignable_technician_queryset, FinancialAccountForm, AccountTransferForm
from ..gst_utils import effective_tax_rate
from ..phone_utils import normalize_indian_phone, phone_lookup_variants
from ..whatsapp_service import verify_receipt_access_token
from django.db import transaction, OperationalError, ProgrammingError
from django.http import JsonResponse, HttpResponseForbidden, HttpResponseBadRequest, HttpResponse, FileResponse, HttpResponseNotFound, Http404
import secrets
from django.template.loader import render_to_string
from django.views.decorators.http import require_GET, require_POST, require_http_methods
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.cache import never_cache
from django.contrib import messages
from django.db.models.functions import Coalesce
from decimal import Decimal, InvalidOperation
from django.utils import timezone
from django.utils.http import url_has_allowed_host_and_scheme
from uuid import uuid4 
from django.urls import Resolver404, resolve, reverse
from django.core.cache import cache
from django.core.exceptions import ValidationError
from django.conf import settings
from types import SimpleNamespace
import csv
import mimetypes
import re
import base64
import hmac
import hashlib
import json
from urllib.parse import urlencode, urlsplit, quote

PRODUCT_SALE_LOG_PATTERN = re.compile(
    r'^Product Sale - (?P<name>.+?) \(Qty: (?P<qty>\d+)\)(?: \[PROD#(?P<product_id>\d+)\])?$'
)

RATE_CLEAN_REGEX = re.compile(r'^\d+(\.\d+)?$')

def validate_rate_no_special_chars(raw_value, label="Rate"):
    text = (raw_value or '').strip()
    if text and not RATE_CLEAN_REGEX.match(text):
        raise ValueError(f"{label}: special characters are not allowed in rate.")

JOB_PRESET_FIELDS = ('device_type', 'device_brand', 'reported_issue', 'additional_items')
MOBILE_JWT_EXP_SECONDS = 60 * 60 * 24 * 7  # 7 days
MOBILE_JWT_ALGORITHM = 'HS256'
CHECKLIST_FIELD_TYPES = {'text', 'textarea', 'number', 'select', 'checkbox'}
MONEY_OUTPUT_FIELD = DecimalField(max_digits=12, decimal_places=2)
MAX_JOB_PHOTO_BYTES = 10 * 1024 * 1024


def scope_to_workspace(queryset, workspace_or_request, field='workspace'):
    workspace = getattr(workspace_or_request, 'current_workspace', workspace_or_request)
    if workspace:
        return queryset.filter(**{field: workspace})
    return queryset


def user_has_workspace_access(user, workspace):
    if not workspace or getattr(user, 'is_superuser', False):
        return True
    return CompanyUserMembership.objects.filter(
        user=user,
        workspace=workspace,
        is_active=True,
    ).exists()


def validate_job_photo_upload(uploaded_file):
    if getattr(uploaded_file, 'size', 0) > MAX_JOB_PHOTO_BYTES:
        return '', 'Each photo must be 10 MB or smaller.'

    content_type = (getattr(uploaded_file, 'content_type', '') or '').strip().lower()
    if not content_type:
        guessed_type, _ = mimetypes.guess_type(getattr(uploaded_file, 'name', ''))
        content_type = (guessed_type or '').lower()

    signatures = {
        'image/jpeg': (b'\xff\xd8\xff',),
        'image/png': (b'\x89PNG\r\n\x1a\n',),
        'image/gif': (b'GIF87a', b'GIF89a'),
        'image/webp': (b'RIFF',),
    }
    if content_type not in signatures:
        return '', 'Only JPEG, PNG, GIF, and WebP photos are allowed.'

    current_position = uploaded_file.tell()
    header = uploaded_file.read(12)
    uploaded_file.seek(current_position)
    if not any(header.startswith(signature) for signature in signatures[content_type]):
        return '', 'The uploaded photo content does not match its file type.'

    if content_type == 'image/webp' and header[8:12] != b'WEBP':
        return '', 'The uploaded photo content does not match its file type.'

    return content_type, ''


def vendor_net_cost_expression(prefix=''):
    cost_field = f'{prefix}vendor_cost'
    discount_field = f'{prefix}vendor_discount_amount'
    return ExpressionWrapper(
        Coalesce(F(cost_field), Value(Decimal('0.00')), output_field=MONEY_OUTPUT_FIELD)
        - Coalesce(F(discount_field), Value(Decimal('0.00')), output_field=MONEY_OUTPUT_FIELD),
        output_field=MONEY_OUTPUT_FIELD,
    )


def sum_vendor_net_cost(queryset, prefix=''):
    return queryset.aggregate(
        total=Coalesce(
            Sum(vendor_net_cost_expression(prefix), output_field=MONEY_OUTPUT_FIELD),
            Value(Decimal('0.00')),
            output_field=MONEY_OUTPUT_FIELD,
        )
    )['total']


DEFAULT_LAPTOP_CHECKLIST_SCHEMA = [
    {
        'key': 'ports_condition',
        'label': 'Ports Condition',
        'type': 'select',
        'required': True,
        'options': ['Good', 'Issue Found', 'Not Tested'],
        'placeholder': '',
        'help_text': 'USB/Type-C/HDMI/audio ports.',
    },
    {
        'key': 'speaker_condition',
        'label': 'Speaker Condition',
        'type': 'select',
        'required': True,
        'options': ['Good', 'Issue Found', 'Not Tested'],
        'placeholder': '',
        'help_text': '',
    },
    {
        'key': 'body_condition',
        'label': 'Body Condition',
        'type': 'select',
        'required': True,
        'options': ['Good', 'Minor Damage', 'Major Damage'],
        'placeholder': '',
        'help_text': '',
    },
    {
        'key': 'screen_condition',
        'label': 'Screen Condition',
        'type': 'select',
        'required': True,
        'options': ['Good', 'Minor Issue', 'Major Issue'],
        'placeholder': '',
        'help_text': '',
    },
    {
        'key': 'keyboard_condition',
        'label': 'Keyboard Condition',
        'type': 'select',
        'required': True,
        'options': ['Good', 'Issue Found', 'Not Tested'],
        'placeholder': '',
        'help_text': '',
    },
    {
        'key': 'touchpad_condition',
        'label': 'Touchpad Condition',
        'type': 'select',
        'required': True,
        'options': ['Good', 'Issue Found', 'Not Tested'],
        'placeholder': '',
        'help_text': '',
    },
    {
        'key': 'hinges_condition',
        'label': 'Hinges Condition',
        'type': 'select',
        'required': True,
        'options': ['Good', 'Loose', 'Broken'],
        'placeholder': '',
        'help_text': '',
    },
    {
        'key': 'battery_health',
        'label': 'Battery Health',
        'type': 'text',
        'required': True,
        'options': [],
        'placeholder': 'Example: 82% / Good / Needs replacement',
        'help_text': '',
    },
    {
        'key': 'cpu_spec',
        'label': 'CPU',
        'type': 'text',
        'required': True,
        'options': [],
        'placeholder': 'Example: Intel i5 11th Gen',
        'help_text': '',
    },
    {
        'key': 'ram_spec_gb',
        'label': 'RAM (GB)',
        'type': 'text',
        'required': True,
        'options': [],
        'placeholder': 'Example: 8 GB DDR4',
        'help_text': '',
    },
    {
        'key': 'ssd_spec_gb',
        'label': 'SSD (GB)',
        'type': 'text',
        'required': True,
        'options': [],
        'placeholder': 'Example: 512 GB',
        'help_text': '',
    },
    {
        'key': 'hdd_spec_gb',
        'label': 'HDD (GB)',
        'type': 'text',
        'required': True,
        'options': [],
        'placeholder': 'Example: 1000 GB / None',
        'help_text': '',
    },
]

def _normalize_device_type(raw_device_type):
    return re.sub(r'\s+', ' ', (raw_device_type or '').strip()).lower()

def _sanitize_checklist_key(raw_key):
    key = re.sub(r'[^a-z0-9_]+', '_', (raw_key or '').strip().lower())
    return re.sub(r'_+', '_', key).strip('_')

def _normalize_checklist_answer(raw_value):
    if raw_value is None:
        return ''
    return str(raw_value).strip()

def _normalize_checkbox_answer(raw_value):
    value = _normalize_checklist_answer(raw_value).lower()
    return '1' if value in {'1', 'true', 'yes', 'on', 'checked'} else ''

def _get_job_checklist_answers(job):
    raw_data = getattr(job, 'technician_checklist', None)
    return raw_data if isinstance(raw_data, dict) else {}

def _apply_job_checklist_rules(job, normalized_type, checklist_schema, checklist_notes):
    if not checklist_schema:
        return checklist_schema, checklist_notes

    requires_checklist = bool(getattr(job, 'requires_laptop_inspection_checklist', False))
    if requires_checklist:
        required_note = 'This checklist must be completed before the technician can mark the job as completed.'
        if checklist_notes:
            checklist_notes = f"{checklist_notes} {required_note}".strip()
        else:
            checklist_notes = required_note
        return checklist_schema, checklist_notes

    optional_note = 'Checklist is optional for this job. Technician can complete the ticket even without filling these fields.'
    checklist_schema = [{**field, 'required': False} for field in checklist_schema]
    if checklist_notes:
        checklist_notes = f"{checklist_notes} {optional_note}".strip()
    else:
        checklist_notes = optional_note
    return checklist_schema, checklist_notes

def _checklist_requires_completion(checklist_schema):
    return any(field.get('required') for field in (checklist_schema or []))

def _build_checklist_schema_for_job(job):
    device_type = (getattr(job, 'device_type', '') or '').strip()
    normalized_type = _normalize_device_type(device_type)
    answers = _get_job_checklist_answers(job)

    schema = []
    checklist_title = ''
    checklist_notes = ''

    template = None
    if device_type:
        template = (
            DeviceChecklistTemplate.objects.filter(is_active=True, device_type__iexact=device_type)
            .prefetch_related('fields')
            .first()
        )

    if template:
        checklist_title = (template.name or template.device_type).strip()
        checklist_notes = (template.notes or '').strip()
        active_fields = [field for field in template.fields.all() if field.is_active]
        active_fields.sort(key=lambda field: (field.sort_order, (field.label or '').lower()))

        seen_keys = set()
        for field in active_fields:
            field_key = _sanitize_checklist_key(field.field_key)
            if not field_key or field_key in seen_keys:
                continue
            seen_keys.add(field_key)

            field_type = field.field_type if field.field_type in CHECKLIST_FIELD_TYPES else 'text'
            options = field.get_option_list() if field_type == 'select' else []
            if field_type == 'checkbox':
                value = _normalize_checkbox_answer(answers.get(field_key, ''))
            else:
                value = _normalize_checklist_answer(answers.get(field_key, ''))
            if field_type == 'select' and value and value not in options:
                options.append(value)

            schema.append(
                {
                    'key': field_key,
                    'label': (field.label or field_key.replace('_', ' ').title()).strip(),
                    'type': field_type,
                    'required': bool(field.is_required),
                    'placeholder': (field.placeholder or '').strip(),
                    'help_text': (field.help_text or '').strip(),
                    'options': options,
                    'value': value,
                }
            )

    elif 'laptop' in normalized_type:
        checklist_title = 'Laptop Full Check'
        checklist_notes = 'Default checklist is active. Staff/admin can override from admin using Device Checklist Templates.'
        for item in DEFAULT_LAPTOP_CHECKLIST_SCHEMA:
            field_key = _sanitize_checklist_key(item.get('key', ''))
            if not field_key:
                continue
            schema.append(
                {
                    'key': field_key,
                    'label': item.get('label', field_key.replace('_', ' ').title()),
                    'type': item.get('type', 'text'),
                    'required': bool(item.get('required', False)),
                    'placeholder': item.get('placeholder', ''),
                    'help_text': item.get('help_text', ''),
                    'options': list(item.get('options') or []),
                    'value': _normalize_checklist_answer(answers.get(field_key, '')),
                }
            )

    schema, checklist_notes = _apply_job_checklist_rules(
        job,
        normalized_type,
        schema,
        checklist_notes,
    )
    return schema, checklist_title, checklist_notes

def _extract_checklist_answers_from_post(post_data, checklist_schema):
    cleaned = {}
    missing_required_labels = []
    invalid_option_labels = []

    for field in checklist_schema:
        key = field['key']
        field_type = field.get('type')
        if field_type == 'checkbox':
            value = _normalize_checkbox_answer(post_data.get(f'checklist_{key}', ''))
        else:
            value = _normalize_checklist_answer(post_data.get(f'checklist_{key}', ''))
        options = field.get('options') or []

        if field_type == 'select' and value and options and value not in options:
            invalid_option_labels.append(field['label'])
            value = ''

        cleaned[key] = value

        if field.get('required') and not value:
            missing_required_labels.append(field['label'])

    return cleaned, missing_required_labels, invalid_option_labels

def _merge_checklist_answers(existing_answers, posted_answers):
    merged = dict(existing_answers or {})
    merged.update(posted_answers or {})
    return merged

def _missing_required_checklist_labels(job, checklist_schema):
    answers = _get_job_checklist_answers(job)
    missing = []
    for field in checklist_schema:
        if not field.get('required'):
            continue
        if field.get('type') == 'checkbox':
            value = _normalize_checkbox_answer(answers.get(field['key'], ''))
        else:
            value = _normalize_checklist_answer(answers.get(field['key'], ''))
        if not value:
            missing.append(field['label'])
    return missing

def _format_checklist_required_error(missing_labels):
    if not missing_labels:
        return 'Checklist is incomplete.'
    preview = ', '.join(missing_labels[:6])
    suffix = '...' if len(missing_labels) > 6 else ''
    return f"Complete required checklist fields before marking completed: {preview}{suffix}"

def _b64url_encode(raw_bytes):
    return base64.urlsafe_b64encode(raw_bytes).decode('utf-8').rstrip('=')

def _b64url_decode(encoded_text):
    padding = '=' * (-len(encoded_text) % 4)
    return base64.urlsafe_b64decode(f"{encoded_text}{padding}")

def get_client_ip(request):
    """Extract client IP address from X-Forwarded-For or REMOTE_ADDR."""
    forwarded_for = (request.META.get('HTTP_X_FORWARDED_FOR') or '').split(',')[0].strip()
    return forwarded_for or request.META.get('REMOTE_ADDR') or ''

def generate_qr_base64(data_text):
    """Generates a high-resolution base64 PNG data URI for a QR code."""
    try:
        import io
        import base64
        import qrcode
        qr = qrcode.QRCode(
            version=None,
            error_correction=qrcode.constants.ERROR_CORRECT_M,
            box_size=8,
            border=2,
        )
        qr.add_data(data_text)
        qr.make(fit=True)
        img = qr.make_image(fill_color="black", back_color="white")
        buffer = io.BytesIO()
        img.save(buffer, format="PNG")
        return "data:image/png;base64," + base64.b64encode(buffer.getvalue()).decode('utf-8')
    except Exception as exc:
        logger.error(f"Failed to generate QR code: {exc}")
        return ""


def issue_mobile_jwt(user, session_id=None):
    now_ts = int(timezone.now().timestamp())
    payload = {
        'sub': str(user.id),
        'username': user.username,
        'is_staff': bool(user.is_staff),
        'iat': now_ts,
        'exp': now_ts + MOBILE_JWT_EXP_SECONDS,
    }
    if session_id:
        payload['jti'] = str(session_id)
    header = {'alg': MOBILE_JWT_ALGORITHM, 'typ': 'JWT'}
    header_b64 = _b64url_encode(json.dumps(header, separators=(',', ':')).encode('utf-8'))
    payload_b64 = _b64url_encode(json.dumps(payload, separators=(',', ':')).encode('utf-8'))
    signing_input = f"{header_b64}.{payload_b64}".encode('utf-8')
    signature = hmac.new(settings.SECRET_KEY.encode('utf-8'), signing_input, hashlib.sha256).digest()
    signature_b64 = _b64url_encode(signature)
    return f"{header_b64}.{payload_b64}.{signature_b64}"


def decode_mobile_jwt(token):
    token = (token or '').strip()
    token_parts = token.split('.')
    if len(token_parts) != 3:
        return None, "Token format is invalid."

    header_b64, payload_b64, signature_b64 = token_parts
    signing_input = f"{header_b64}.{payload_b64}".encode('utf-8')
    expected_signature = hmac.new(settings.SECRET_KEY.encode('utf-8'), signing_input, hashlib.sha256).digest()
    expected_signature_b64 = _b64url_encode(expected_signature)
    if not hmac.compare_digest(expected_signature_b64, signature_b64):
        return None, "Token signature mismatch."

    try:
        header = json.loads(_b64url_decode(header_b64).decode('utf-8'))
        payload = json.loads(_b64url_decode(payload_b64).decode('utf-8'))
    except (ValueError, json.JSONDecodeError, UnicodeDecodeError):
        return None, "Token payload is invalid."

    if header.get('alg') != MOBILE_JWT_ALGORITHM:
        return None, "Unsupported token algorithm."

    try:
        exp_ts = int(payload.get('exp', 0))
    except (TypeError, ValueError):
        return None, "Token expiration value is invalid."

    if exp_ts <= int(timezone.now().timestamp()):
        return None, "Token has expired."

    return payload, None


def authenticate_mobile_request(request):
    auth_header = (request.headers.get('Authorization') or '').strip()
    if not auth_header.startswith('Bearer '):
        return None, JsonResponse(
            {'error': 'missing_token', 'message': 'Authorization header missing Bearer token.'},
            status=401,
        )

    token = auth_header.split(' ', 1)[1].strip()
    payload, error_message = decode_mobile_jwt(token)
    if error_message:
        return None, JsonResponse({'error': 'invalid_token', 'message': error_message}, status=401)

    user_id = payload.get('sub')
    try:
        user = User.objects.get(id=int(user_id), is_active=True)
    except (TypeError, ValueError, User.DoesNotExist):
        return None, JsonResponse({'error': 'invalid_user', 'message': 'Token user no longer exists.'}, status=401)

    # Check if device session was remotely logged out / revoked
    jti = payload.get('jti')
    if jti:
        session = UserSessionActivity.objects.filter(session_key=jti).first()
        if session:
            if session.status == UserSessionActivity.STATUS_LOGGED_OUT:
                return None, JsonResponse(
                    {'error': 'session_revoked', 'message': 'This device has been logged out from the panel.'},
                    status=401,
                )
            # Update last activity
            session.last_activity_at = timezone.now()
            session.save(update_fields=['last_activity_at'])

    return user, None

def get_mobile_job_permissions(user, job):
    workspace_id = getattr(job, 'workspace_id', None)
    membership_is_active = (
        not workspace_id
        or CompanyUserMembership.objects.filter(
            user=user,
            workspace_id=workspace_id,
            is_active=True,
        ).exists()
    )
    technician_profile = getattr(user, 'technician_profile', None)
    is_assigned_tech = (
        technician_profile is not None
        and job.assigned_to_id == technician_profile.id
        and (not workspace_id or technician_profile.workspace_id == workspace_id)
        and membership_is_active
    )
    has_linked_task = (
        technician_profile is not None
        and job.tasks.filter(assigned_to=technician_profile).exists()
        and membership_is_active
    )
    staff_can_access = bool(
        user.is_staff
        and user_has_staff_access(user, "staff_dashboard")
        and membership_is_active
    )
    can_access = staff_can_access or is_assigned_tech or has_linked_task
    return {
        'is_staff': bool(staff_can_access),
        'is_assigned_tech': bool(is_assigned_tech),
        'can_access': bool(can_access),
    }

def user_can_view_financial_reports(user):
    if not user or not getattr(user, "is_authenticated", False):
        return False
    if getattr(user, "is_superuser", False):
        return True
    if user.has_perm("job_tickets.view_financial_reports"):
        return True
    access = get_staff_access(user)
    return bool(
        access.get("reports_dashboard")
        or access.get("reports_financial")
        or access.get("reports_technician")
        or access.get("reports_vendor")
    )

def _staff_access_required(request, key, json=False):
    if not request.user.is_staff or not user_has_staff_access(request.user, key):
        if json:
            return JsonResponse({'error': 'Unauthorized'}, status=403)
        return redirect('unauthorized')
    return None

def get_mobile_job_available_actions(user, job):
    permissions = get_mobile_job_permissions(user, job)
    if not permissions['can_access']:
        return []

    actions = []
    if permissions['is_assigned_tech'] and job.is_new_assignment:
        actions.append({'key': 'acknowledge', 'label': 'Acknowledge'})
        actions.append({'key': 'return_to_staff', 'label': 'Return to Staff'})

    if job.status in ['Pending', 'Under Inspection']:
        actions.append({'key': 'start', 'label': 'Mark Started'})

    if job.status in ['Under Inspection', 'Repairing', 'Specialized Service']:
        actions.append({'key': 'complete', 'label': 'Mark Completed'})

    if permissions['is_staff'] and job.status == 'Completed' and user_has_staff_access(user, 'job_status_change'):
        actions.append({'key': 'ready_for_pickup', 'label': 'Ready for Pickup'})

    if permissions['is_staff'] and job.status in ['Completed', 'Ready for Pickup']:
        actions.append({'key': 'close', 'label': 'Close Job'})

    return actions

def mobile_can_edit_notes(user, job):
    permissions = get_mobile_job_permissions(user, job)
    return bool(permissions['can_access'])

def mobile_can_manage_service_lines(user, job):
    permissions = get_mobile_job_permissions(user, job)
    if not permissions['can_access']:
        return False

    # Do not allow manual line changes while job is with vendor.
    if job.status == 'Specialized Service':
        return False

    # Do not allow editing billing lines for closed jobs.
    if job.status == 'Closed':
        return False

    # Staff can manage billing lines across statuses when needed.
    if permissions['is_staff']:
        return True

    # Technicians should not mutate billing after finalization or invoicing.
    if job.vyapar_invoice_number:
        return False
    if job.status in ['Completed', 'Ready for Pickup', 'Closed']:
        return False

    return bool(permissions['is_assigned_tech'])

def _mobile_parse_decimal(raw_value, field_label):
    text = str(raw_value if raw_value is not None else '').strip()
    if text == '':
        return Decimal('0.00'), None

    try:
        return Decimal(text), None
    except (InvalidOperation, TypeError, ValueError):
        return None, f"Invalid amount for '{field_label}'."

def _mobile_parse_bool(raw_value):
    if isinstance(raw_value, bool):
        return raw_value
    text = str(raw_value if raw_value is not None else '').strip().lower()
    return text in {'1', 'true', 'yes', 'on'}

def get_job_field_presets():
    cache_key = 'job_field_presets_v1'
    cached = cache.get(cache_key)
    if cached is not None:
        return cached

    grouped = {field: [] for field in JOB_PRESET_FIELDS}
    seen_values = {field: set() for field in JOB_PRESET_FIELDS}

    for field_name, raw_value in JobFieldPreset.objects.filter(is_active=True).values_list('field_name', 'value'):
        if field_name not in grouped:
            continue
        value = (raw_value or '').strip()
        if not value:
            continue
        dedupe_key = value.lower()
        if dedupe_key in seen_values[field_name]:
            continue
        seen_values[field_name].add(dedupe_key)
        grouped[field_name].append(value)

    cache.set(cache_key, grouped, 300)
    return grouped

def get_active_checklist_templates(workspace=None):
    """Return dictionary of active checklist templates keyed by lowercase device_type."""
    qs = DeviceChecklistTemplate.objects.filter(is_active=True)
    if workspace:
        qs = qs.filter(Q(workspace=workspace) | Q(workspace__isnull=True))
    result = {}
    for t in qs:
        key = t.device_type.strip().lower()
        result[key] = {
            'id': t.id,
            'device_type': t.device_type,
            'name': t.name or f"{t.device_type} Inspection Checklist",
            'notes': t.notes or '',
        }
    return result

def sync_job_field_presets(device_submissions):
    if not device_submissions:
        return

    values_by_field = {field: set() for field in JOB_PRESET_FIELDS}
    for device_data in device_submissions:
        for field in JOB_PRESET_FIELDS:
            value = (device_data.get(field) or '').strip()
            if value:
                values_by_field[field].add(value[:255])

    for field_name, values in values_by_field.items():
        if not values:
            continue

        existing_values = set(
            JobFieldPreset.objects.filter(field_name=field_name, value__in=values).values_list('value', flat=True)
        )
        new_rows = [
            JobFieldPreset(field_name=field_name, value=value, sort_order=100, is_active=True)
            for value in values
            if value not in existing_values
        ]
        if new_rows:
            JobFieldPreset.objects.bulk_create(new_rows, ignore_conflicts=True)

def parse_product_sale_log(description):
    match = PRODUCT_SALE_LOG_PATTERN.match((description or '').strip())
    if not match:
        return None
    product_id_raw = match.group('product_id')
    return {
        'product_id': int(product_id_raw) if product_id_raw else None,
        'quantity': int(match.group('qty')),
        'name': match.group('name').strip(),
    }

def summarize_stock_sales(finished_jobs_qs, service_logs=None, direct_entries=None, direct_returns=None):
    """Build stock sale metrics from ProductSale ledger (with legacy fallback) and direct inventory sales."""
    sales = list(
        ProductSale.objects.filter(job_ticket__in=finished_jobs_qs).select_related('product', 'service_log')
    )

    total_revenue = Decimal('0.00')
    total_cogs = Decimal('0.00')
    total_profit = Decimal('0.00')
    total_units = 0
    sale_lines_count = 0
    product_rows = {}
    product_sale_log_ids = set()

    for sale in sales:
        revenue = sale.line_total or Decimal('0.00')
        cogs = sale.line_cost or Decimal('0.00')
        profit = sale.line_profit if sale.line_profit is not None else (revenue - cogs)
        quantity = sale.quantity or 0

        total_revenue += revenue
        total_cogs += cogs
        total_profit += profit
        total_units += quantity
        sale_lines_count += 1
        if sale.service_log_id:
            product_sale_log_ids.add(sale.service_log_id)

        product_obj = sale.product
        row = product_rows.setdefault(
            product_obj.id,
            {
                'product_id': product_obj.id,
                'name': product_obj.name,
                'sku': product_obj.sku or '-',
                'category': product_obj.category or '-',
                'units_sold': 0,
                'sale_lines': 0,
                'revenue': Decimal('0.00'),
                'cogs': Decimal('0.00'),
                'profit': Decimal('0.00'),
                'average_unit_price': Decimal('0.00'),
            },
        )
        row['units_sold'] += quantity
        row['sale_lines'] += 1
        row['revenue'] += revenue
        row['cogs'] += cogs
        row['profit'] += profit

    if direct_entries:
        for entry in direct_entries:
            revenue = entry.total_amount or Decimal('0.00')
            product_obj = entry.product
            unit_cost = (product_obj.cost_price if product_obj else Decimal('0.00')) or Decimal('0.00')
            quantity = entry.quantity or 0
            cogs = unit_cost * Decimal(quantity)
            profit = revenue - cogs

            total_revenue += revenue
            total_cogs += cogs
            total_profit += profit
            total_units += quantity
            sale_lines_count += 1

            if product_obj:
                row = product_rows.setdefault(
                    product_obj.id,
                    {
                        'product_id': product_obj.id,
                        'name': product_obj.name,
                        'sku': product_obj.sku or '-',
                        'category': product_obj.category or '-',
                        'units_sold': 0,
                        'sale_lines': 0,
                        'revenue': Decimal('0.00'),
                        'cogs': Decimal('0.00'),
                        'profit': Decimal('0.00'),
                        'average_unit_price': Decimal('0.00'),
                    },
                )
                row['units_sold'] += quantity
                row['sale_lines'] += 1
                row['revenue'] += revenue
                row['cogs'] += cogs
                row['profit'] += profit

    if direct_returns:
        for entry in direct_returns:
            revenue = entry.total_amount or Decimal('0.00')
            product_obj = entry.product
            unit_cost = (product_obj.cost_price if product_obj else Decimal('0.00')) or Decimal('0.00')
            quantity = entry.quantity or 0
            cogs = unit_cost * Decimal(quantity)
            profit = revenue - cogs

            total_revenue -= revenue
            total_cogs -= cogs
            total_profit -= profit
            total_units -= quantity
            sale_lines_count += 1

            if product_obj and product_obj.id in product_rows:
                row = product_rows[product_obj.id]
                row['units_sold'] -= quantity
                row['revenue'] -= revenue
                row['cogs'] -= cogs
                row['profit'] -= profit

    # Legacy fallback: include product sale logs that predate ProductSale ledger rows.
    if service_logs is not None:
        fallback_logs = []
        fallback_product_ids = set()
        for log in service_logs:
            if log.id in product_sale_log_ids:
                continue
            parsed = parse_product_sale_log(log.description)
            if not parsed:
                continue
            fallback_logs.append((log, parsed))
            if parsed['product_id']:
                fallback_product_ids.add(parsed['product_id'])

        fallback_products = Product.objects.in_bulk(fallback_product_ids) if fallback_product_ids else {}

        for log, parsed in fallback_logs:
            quantity = parsed['quantity']
            if quantity <= 0:
                continue

            product_obj = fallback_products.get(parsed['product_id'])
            revenue = log.part_cost or Decimal('0.00')
            unit_cost = product_obj.cost_price if product_obj else Decimal('0.00')
            cogs = unit_cost * Decimal(quantity)
            profit = revenue - cogs

            total_revenue += revenue
            total_cogs += cogs
            total_profit += profit
            total_units += quantity
            sale_lines_count += 1
            product_sale_log_ids.add(log.id)

            row_key = parsed['product_id'] or f"legacy:{parsed['name'].strip().lower()}"
            row = product_rows.setdefault(
                row_key,
                {
                    'product_id': parsed['product_id'],
                    'name': product_obj.name if product_obj else parsed['name'],
                    'sku': (product_obj.sku if product_obj else '') or '-',
                    'category': (product_obj.category if product_obj else '') or '-',
                    'units_sold': 0,
                    'sale_lines': 0,
                    'revenue': Decimal('0.00'),
                    'cogs': Decimal('0.00'),
                    'profit': Decimal('0.00'),
                    'average_unit_price': Decimal('0.00'),
                },
            )
            row['units_sold'] += quantity
            row['sale_lines'] += 1
            row['revenue'] += revenue
            row['cogs'] += cogs
            row['profit'] += profit

    products = list(product_rows.values())
    for row in products:
        if row['units_sold'] > 0:
            row['average_unit_price'] = row['revenue'] / Decimal(row['units_sold'])
    products.sort(key=lambda item: item['revenue'], reverse=True)

    avg_sale_value = (total_revenue / Decimal(sale_lines_count)) if sale_lines_count else Decimal('0.00')

    return {
        'total_revenue': total_revenue,
        'total_cogs': total_cogs,
        'total_profit': total_profit,
        'total_units': total_units,
        'sale_lines_count': sale_lines_count,
        'products': products,
        'products_count': len(products),
        'avg_sale_value': avg_sale_value,
        'service_log_ids': product_sale_log_ids,
    }

def _money_or_zero(value):
    return value if value is not None else Decimal('0.00')

def _sum_job_discounts(job_queryset):
    return job_queryset.aggregate(
        total=Coalesce(Sum('discount_amount', output_field=DecimalField()), Decimal('0.00'))
    )['total']

def _net_amount_after_discount(gross_amount, discount_amount):
    gross = _money_or_zero(gross_amount)
    discount = _money_or_zero(discount_amount)
    return max(Decimal('0.00'), gross - discount)

RETURNED_TO_CLOSED_LOG_TEXT = "from 'Returned' to 'Closed'"

def get_returned_to_closed_job_ids(job_queryset=None):
    """Jobs that were closed directly from Returned status."""
    logs = JobTicketLog.objects.filter(
        details__icontains=RETURNED_TO_CLOSED_LOG_TEXT,
    )
    if job_queryset is not None:
        logs = logs.filter(job_ticket__in=job_queryset)
    return logs.values_list('job_ticket_id', flat=True).distinct()

def get_returned_to_closed_jobs(job_queryset=None):
    """Return closed jobs whose closing transition was Returned -> Closed."""
    jobs = JobTicket.objects.filter(status='Closed')
    if job_queryset is not None:
        jobs = jobs.filter(id__in=job_queryset.values('id'))
    return jobs.filter(id__in=get_returned_to_closed_job_ids(jobs))

def _money_text(amount):
    return format((_money_or_zero(amount)).quantize(Decimal('0.01')), 'f')

def get_monthly_summary_context(
    start_of_period,
    end_of_period,
    start_date_str,
    end_date_str,
    preset='',
    show_jobs='',
    workspace=None,
):
    """Build unified financial summary context for HTML/CSV/PDF outputs."""
    valid_show_jobs = {'created', 'finished', 'current_in_closed', 'previous_in', 'pending_completed', 'returned', 'vendor'}
    if show_jobs not in valid_show_jobs:
        show_jobs = ''

    closed_jobs_filter = Q(status='Closed', report_closed_at__gte=start_of_period, report_closed_at__lt=end_of_period)
    scoped_jobs = with_report_dates(scope_to_workspace(JobTicket.objects, workspace))
    monthly_closed_jobs = scoped_jobs.filter(closed_jobs_filter)
    current_month_in_closed_jobs = monthly_closed_jobs.filter(created_at__gte=start_of_period, created_at__lt=end_of_period)
    previous_month_in_closed_jobs = monthly_closed_jobs.filter(created_at__lt=start_of_period)
    pending_completed_jobs = scoped_jobs.filter(status__in=['Completed', 'Ready for Pickup'])
    ready_for_pickup_jobs = pending_completed_jobs.filter(status='Ready for Pickup')

    jobs_created = scoped_jobs.filter(created_at__gte=start_of_period, created_at__lt=end_of_period)
    jobs_returned = get_returned_to_closed_jobs(monthly_closed_jobs)
    vendor_jobs = list(monthly_closed_jobs.filter(specialized_service__isnull=False))

    def summarize_pending_value(jobs_queryset):
        totals = ServiceLog.objects.filter(job_ticket__in=jobs_queryset).aggregate(
            parts=Coalesce(Sum('part_cost', output_field=DecimalField()), Decimal('0.00')),
            service=Coalesce(Sum('service_charge', output_field=DecimalField()), Decimal('0.00')),
        )
        parts_total = totals['parts']
        service_total = totals['service']
        discount_total = _sum_job_discounts(jobs_queryset)
        return {
            'parts': parts_total,
            'service': service_total,
            'discount': discount_total,
            'value': _net_amount_after_discount(parts_total + service_total, discount_total),
        }

    def summarize_closed_financials(jobs_queryset):
        closed_logs = list(ServiceLog.objects.filter(job_ticket__in=jobs_queryset).select_related('job_ticket'))
        closed_stock_sales = summarize_stock_sales(jobs_queryset, closed_logs)
        closed_product_sale_log_ids = closed_stock_sales['service_log_ids']
        closed_service_logs = [
            log for log in closed_logs
            if log.id not in closed_product_sale_log_ids
            and 'Specialized Service' not in (log.description or '')
        ]
        closed_service_parts = sum(
            (log.part_cost or Decimal('0.00') for log in closed_service_logs),
            Decimal('0.00'),
        )
        closed_service_labor = sum(
            (log.service_charge or Decimal('0.00') for log in closed_service_logs),
            Decimal('0.00'),
        )
        closed_service_revenue = closed_service_parts + closed_service_labor
        closed_vendor_services = SpecializedService.objects.filter(job_ticket__in=jobs_queryset)
        closed_vendor_expense = sum_vendor_net_cost(closed_vendor_services)
        closed_vendor_revenue = closed_vendor_services.aggregate(
            total=Coalesce(Sum('client_charge', output_field=DecimalField()), Decimal('0.00'))
        )['total']
        closed_discount = _sum_job_discounts(jobs_queryset)
        closed_spare_revenue = closed_service_parts + closed_stock_sales['total_revenue']
        closed_labor_revenue = closed_service_labor + closed_vendor_revenue
        closed_gross_revenue = closed_spare_revenue + closed_labor_revenue
        closed_revenue = _net_amount_after_discount(
            closed_gross_revenue,
            closed_discount,
        )
        closed_expense = closed_stock_sales['total_cogs'] + closed_vendor_expense
        closed_profit = closed_revenue - closed_expense
        return {
            'count': jobs_queryset.count(),
            'revenue': closed_revenue,
            'expense': closed_expense,
            'profit': closed_profit,
            'discount': closed_discount,
            'spare_revenue': closed_spare_revenue,
            'service_revenue': closed_labor_revenue,
            'gross_revenue': closed_gross_revenue,
        }

    def summarize_closed_bill_receivables(jobs_queryset):
        bills = list(
            InventoryBill.objects
            .filter(entry_type='sale', job_ticket__in=jobs_queryset)
            .annotate(bill_total=Coalesce(Sum('lines__total_amount', output_field=DecimalField()), Decimal('0.00')))
            .order_by('entry_date', 'id')
        )
        direction = InventoryCreditPayment.DIRECTION_RECEIVABLE
        payment_totals = _inventory_credit_payment_totals([bill.id for bill in bills])

        bill_total = Decimal('0.00')
        paid_total = Decimal('0.00')
        balance_total = Decimal('0.00')
        balance_count = 0
        for bill in bills:
            total_amount = _money_or_zero(getattr(bill, 'bill_total', Decimal('0.00')))
            paid_amount = _money_or_zero(payment_totals.get((bill.id, direction), Decimal('0.00')))
            balance_amount = total_amount - paid_amount
            if balance_amount < Decimal('0.00'):
                balance_amount = Decimal('0.00')

            bill_total += total_amount
            paid_total += paid_amount
            balance_total += balance_amount
            if balance_amount > Decimal('0.00'):
                balance_count += 1

        return {
            'bill_count': len(bills),
            'balance_count': balance_count,
            'bill_total': bill_total,
            'paid': paid_total,
            'balance': balance_total,
        }

    pending_completed_summary = summarize_pending_value(pending_completed_jobs)
    ready_for_pickup_summary = summarize_pending_value(ready_for_pickup_jobs)
    current_month_in_closed_summary = summarize_closed_financials(current_month_in_closed_jobs)
    previous_month_in_closed_summary = summarize_closed_financials(previous_month_in_closed_jobs)
    closed_receivable_summary = summarize_closed_bill_receivables(monthly_closed_jobs)

    job_list = []
    if show_jobs == 'created':
        job_list = list(jobs_created.select_related('assigned_to__user').order_by('-created_at'))
    elif show_jobs == 'finished':
        job_list = list(monthly_closed_jobs.select_related('assigned_to__user').order_by('-closed_at', '-updated_at'))
    elif show_jobs == 'current_in_closed':
        job_list = list(current_month_in_closed_jobs.select_related('assigned_to__user').order_by('-closed_at', '-updated_at'))
    elif show_jobs == 'previous_in':
        job_list = list(previous_month_in_closed_jobs.select_related('assigned_to__user').order_by('-closed_at', '-updated_at'))
    elif show_jobs == 'pending_completed':
        job_list = list(pending_completed_jobs.select_related('assigned_to__user').order_by('-updated_at'))
    elif show_jobs == 'returned':
        job_list = list(jobs_returned.select_related('assigned_to__user').order_by('-updated_at'))
    elif show_jobs == 'vendor':
        job_list = list(
            monthly_closed_jobs.filter(specialized_service__isnull=False)
            .select_related('assigned_to__user', 'specialized_service__vendor')
            .order_by('-closed_at', '-updated_at')
        )

    for job in job_list:
        if show_jobs in {'finished', 'current_in_closed', 'previous_in', 'vendor'}:
            job.summary_report_date = job.report_closed_at
        else:
            job.summary_report_date = job.created_at if show_jobs == 'created' else job.updated_at
    if job_list:
        calculate_job_totals(job_list)
        for job in job_list:
            job.discount_total = _money_or_zero(job.discount_amount)
            job.net_total = _net_amount_after_discount(job.total, job.discount_total)

    period_start_date = start_of_period.date() if hasattr(start_of_period, 'date') else start_of_period
    period_end_date = end_of_period.date() if hasattr(end_of_period, 'date') else end_of_period

    direct_sales_entries = list(
        scope_to_workspace(
            InventoryEntry.objects.filter(
                entry_type='sale',
                entry_date__gte=period_start_date,
                entry_date__lt=period_end_date,
                job_ticket__isnull=True,
            ),
            workspace,
        ).select_related('product', 'bill')
    )
    direct_return_entries = list(
        scope_to_workspace(
            InventoryEntry.objects.filter(
                entry_type='sale_return',
                entry_date__gte=period_start_date,
                entry_date__lt=period_end_date,
                job_ticket__isnull=True,
            ),
            workspace,
        ).select_related('product', 'bill')
    )

    logs_in_period = list(ServiceLog.objects.filter(job_ticket__in=monthly_closed_jobs).select_related('job_ticket'))
    stock_sales = summarize_stock_sales(
        monthly_closed_jobs,
        logs_in_period,
        direct_entries=direct_sales_entries,
        direct_returns=direct_return_entries,
    )
    product_sale_log_ids = stock_sales['service_log_ids']

    non_product_logs = [log for log in logs_in_period if log.id not in product_sale_log_ids]

    # Keep vendor-related services in vendor block, not service block.
    service_logs = [log for log in non_product_logs if 'Specialized Service' not in (log.description or '')]

    service_parts_revenue = sum((log.part_cost or Decimal('0.00') for log in service_logs), Decimal('0.00'))
    service_labor_revenue = sum((log.service_charge or Decimal('0.00') for log in service_logs), Decimal('0.00'))
    service_revenue = service_parts_revenue + service_labor_revenue
    service_expense = Decimal('0.00')
    service_profit = service_revenue - service_expense

    stock_sales_income = stock_sales['total_revenue']
    stock_sales_cogs = stock_sales['total_cogs']
    stock_sales_profit = stock_sales['total_profit']

    direct_stock_sales_revenue = sum((e.total_amount or Decimal('0.00') for e in direct_sales_entries), Decimal('0.00')) - sum((e.total_amount or Decimal('0.00') for e in direct_return_entries), Decimal('0.00'))
    direct_stock_sales_cogs = sum(((e.product.cost_price or Decimal('0.00')) * Decimal(e.quantity or 0) for e in direct_sales_entries if e.product), Decimal('0.00')) - sum(((e.product.cost_price or Decimal('0.00')) * Decimal(e.quantity or 0) for e in direct_return_entries if e.product), Decimal('0.00'))
    direct_stock_sales_profit = direct_stock_sales_revenue - direct_stock_sales_cogs
    direct_stock_sales_units = sum((e.quantity or 0 for e in direct_sales_entries), 0) - sum((e.quantity or 0 for e in direct_return_entries), 0)

    vendor_services_in_period = SpecializedService.objects.filter(job_ticket__in=monthly_closed_jobs)
    vendor_expense = sum_vendor_net_cost(vendor_services_in_period)
    vendor_revenue = vendor_services_in_period.aggregate(
        total=Coalesce(Sum('client_charge', output_field=DecimalField()), Decimal('0.00'))
    )['total']
    vendor_profit = vendor_revenue - vendor_expense

    total_discounts = _sum_job_discounts(monthly_closed_jobs)
    closed_gross_revenue = service_revenue + stock_sales_income + vendor_revenue
    overall_revenue = _net_amount_after_discount(
        closed_gross_revenue,
        total_discounts,
    )
    overall_expense = service_expense + stock_sales_cogs + vendor_expense
    overall_profit = overall_revenue - overall_expense
    overall_margin = (overall_profit / overall_revenue * 100) if overall_revenue > 0 else Decimal('0.00')

    # Period Expenses (Direct Job Costs & Shop Overhead)
    scoped_expenses = scope_to_workspace(Expense.objects.all(), workspace).filter(
        expense_date__gte=period_start_date,
        expense_date__lt=period_end_date,
    )
    period_job_expenses = scoped_expenses.filter(job_ticket__isnull=False).aggregate(
        total=Coalesce(Sum('amount', output_field=DecimalField()), Decimal('0.00'))
    )['total']
    period_overhead_expenses = scoped_expenses.filter(job_ticket__isnull=True).aggregate(
        total=Coalesce(Sum('amount', output_field=DecimalField()), Decimal('0.00'))
    )['total']
    period_total_expenses = period_job_expenses + period_overhead_expenses
    period_net_profit = overall_profit - period_overhead_expenses

    closed_financial_blocks = [
        {
            'label': 'Service Parts',
            'note': 'Service part revenue from closed jobs',
            'revenue': service_parts_revenue,
            'expense': Decimal('0.00'),
            'profit': service_parts_revenue,
            'is_discount': False,
        },
        {
            'label': 'Service Labor',
            'note': 'Service labor revenue from closed jobs',
            'revenue': service_labor_revenue,
            'expense': service_expense,
            'profit': service_labor_revenue - service_expense,
            'is_discount': False,
        },
        {
            'label': 'Stock Sales',
            'note': 'Product sale lines from closed jobs',
            'revenue': stock_sales_income,
            'expense': stock_sales_cogs,
            'profit': stock_sales_profit,
            'is_discount': False,
        },
        {
            'label': 'Vendor',
            'note': 'Vendor client charge less vendor payable',
            'revenue': vendor_revenue,
            'expense': vendor_expense,
            'profit': vendor_profit,
            'is_discount': False,
        },
        {
            'label': 'Customer Discounts',
            'note': 'Job-level discounts deducted from total revenue',
            'revenue': -total_discounts,
            'revenue_abs': total_discounts,
            'expense': Decimal('0.00'),
            'profit': -total_discounts,
            'profit_abs': total_discounts,
            'is_discount': True,
        },
    ]

    all_part_income = sum((log.part_cost or Decimal('0.00') for log in logs_in_period), Decimal('0.00'))
    all_service_income = sum((log.service_charge or Decimal('0.00') for log in logs_in_period), Decimal('0.00'))

    return {
        'current_report_date': start_of_period,
        'start_date': start_date_str,
        'end_date': end_date_str,
        'preset': preset,
        'show_jobs': show_jobs,
        'job_list': job_list,
        'jobs_created_count': jobs_created.count(),
        'jobs_finished_count': monthly_closed_jobs.count(),
        'jobs_closed_count': monthly_closed_jobs.count(),
        'current_month_in_closed_count': current_month_in_closed_jobs.count(),
        'current_month_in_closed_revenue': current_month_in_closed_summary['revenue'],
        'current_month_in_closed_expense': current_month_in_closed_summary['expense'],
        'current_month_in_closed_profit': current_month_in_closed_summary['profit'],
        'current_month_in_closed_discount': current_month_in_closed_summary['discount'],
        'current_month_in_closed_spare': current_month_in_closed_summary['spare_revenue'],
        'current_month_in_closed_service': current_month_in_closed_summary['service_revenue'],
        'current_month_in_closed_gross': current_month_in_closed_summary['gross_revenue'],
        'previous_month_in_closed_count': previous_month_in_closed_jobs.count(),
        'previous_month_in_closed_revenue': previous_month_in_closed_summary['revenue'],
        'previous_month_in_closed_expense': previous_month_in_closed_summary['expense'],
        'previous_month_in_closed_profit': previous_month_in_closed_summary['profit'],
        'previous_month_in_closed_discount': previous_month_in_closed_summary['discount'],
        'previous_month_in_closed_spare': previous_month_in_closed_summary['spare_revenue'],
        'previous_month_in_closed_service': previous_month_in_closed_summary['service_revenue'],
        'previous_month_in_closed_gross': previous_month_in_closed_summary['gross_revenue'],
        'total_closed_spare_revenue': service_parts_revenue + stock_sales_income,
        'total_closed_service_revenue': service_labor_revenue + vendor_revenue,
        'direct_stock_sales_revenue': direct_stock_sales_revenue,
        'direct_stock_sales_cogs': direct_stock_sales_cogs,
        'direct_stock_sales_profit': direct_stock_sales_profit,
        'direct_stock_sales_units': direct_stock_sales_units,
        'direct_stock_sales_count': len(direct_sales_entries),
        'jobs_created_closed_count': current_month_in_closed_jobs.count(),
        'jobs_created_open_count': jobs_created.exclude(status='Closed').count(),
        'jobs_created_closed_pct': round((current_month_in_closed_jobs.count() / jobs_created.count() * 100), 1) if jobs_created.count() > 0 else Decimal('0.0'),
        'jobs_created_open_pct': round((jobs_created.exclude(status='Closed').count() / jobs_created.count() * 100), 1) if jobs_created.count() > 0 else Decimal('0.0'),
        'pending_completed_count': pending_completed_jobs.count(),
        'pending_completed_parts': pending_completed_summary['parts'],
        'pending_completed_service': pending_completed_summary['service'],
        'pending_completed_discount': pending_completed_summary['discount'],
        'pending_completed_value': pending_completed_summary['value'],
        'ready_for_pickup_count': ready_for_pickup_jobs.count(),
        'ready_for_pickup_parts': ready_for_pickup_summary['parts'],
        'ready_for_pickup_service': ready_for_pickup_summary['service'],
        'ready_for_pickup_discount': ready_for_pickup_summary['discount'],
        'ready_for_pickup_value': ready_for_pickup_summary['value'],
        'closed_receivable_bill_count': closed_receivable_summary['bill_count'],
        'closed_receivable_balance_count': closed_receivable_summary['balance_count'],
        'closed_receivable_bill_total': closed_receivable_summary['bill_total'],
        'closed_receivable_paid': closed_receivable_summary['paid'],
        'closed_receivable_balance': closed_receivable_summary['balance'],
        'jobs_returned_count': jobs_returned.count(),
        'vendor_jobs_count': len(vendor_jobs),

        # Service block
        'service_parts_revenue': service_parts_revenue,
        'service_labor_revenue': service_labor_revenue,
        'service_revenue': service_revenue,
        'service_expense': service_expense,
        'service_profit': service_profit,

        # Stock block
        'stock_sales_income': stock_sales_income,
        'stock_sales_cogs': stock_sales_cogs,
        'stock_sales_profit': stock_sales_profit,
        'stock_sales_units': stock_sales['total_units'],
        'stock_sale_lines_count': stock_sales['sale_lines_count'],
        'stock_products_count': stock_sales['products_count'],
        'stock_avg_sale_value': stock_sales['avg_sale_value'],
        'stock_products_breakdown': stock_sales['products'],

        # Vendor block
        'vendor_revenue': vendor_revenue,
        'vendor_expense': vendor_expense,
        'vendor_profit': vendor_profit,

        # Overall
        'closed_gross_revenue': closed_gross_revenue,
        'closed_financial_blocks': closed_financial_blocks,
        'overall_revenue': overall_revenue,
        'overall_expense': overall_expense,
        'overall_profit': overall_profit,
        'overall_margin': overall_margin,
        'total_discounts': total_discounts,

        # Compatibility keys used elsewhere
        'monthly_total_income': overall_revenue,
        'monthly_income_parts': all_part_income,
        'monthly_income_service': all_service_income,
        'monthly_vendor_expense': vendor_expense,
        'monthly_net_profit': overall_profit,
        'profit_margin': overall_margin,
        'repair_parts_income': service_parts_revenue,

        # Expense Management Metrics
        'period_job_expenses': period_job_expenses,
        'period_overhead_expenses': period_overhead_expenses,
        'period_total_expenses': period_total_expenses,
        'period_net_profit': period_net_profit,

        # Liquidity & Financial Accounts Summary
        'financial_accounts': list(
            FinancialAccount.objects.filter(workspace=workspace, is_active=True).order_by('-is_default_cash', '-is_default_bank', 'name')
            if workspace else FinancialAccount.objects.filter(is_active=True).order_by('-is_default_cash', '-is_default_bank', 'name')
        ),
        'total_liquid_balance': sum(
            (a.current_balance for a in FinancialAccount.objects.filter(workspace=workspace, is_active=True) if workspace)
            if workspace else (a.current_balance for a in FinancialAccount.objects.filter(is_active=True)),
            Decimal('0.00')
        ),
        'total_cash_balance': sum(
            (a.current_balance for a in FinancialAccount.objects.filter(workspace=workspace, is_active=True, account_type=FinancialAccount.ACCOUNT_TYPE_CASH) if workspace)
            if workspace else (a.current_balance for a in FinancialAccount.objects.filter(is_active=True, account_type=FinancialAccount.ACCOUNT_TYPE_CASH)),
            Decimal('0.00')
        ),
        'total_bank_balance': sum(
            (a.current_balance for a in FinancialAccount.objects.filter(workspace=workspace, is_active=True).exclude(account_type=FinancialAccount.ACCOUNT_TYPE_CASH) if workspace)
            if workspace else (a.current_balance for a in FinancialAccount.objects.filter(is_active=True).exclude(account_type=FinancialAccount.ACCOUNT_TYPE_CASH)),
            Decimal('0.00')
        ),
    }

def validate_sales_invoice_number_uniqueness(
    invoice_number,
    exclude_job_id=None,
    exclude_inventory_entry_id=None,
    exclude_inventory_bill_id=None,
    allow_inventory_job_id=None,
):
    invoice_number = (invoice_number or '').strip()
    if not invoice_number:
        return
    duplicate_qs = JobTicket.objects.filter(vyapar_invoice_number__iexact=invoice_number)
    if exclude_job_id:
        duplicate_qs = duplicate_qs.exclude(pk=exclude_job_id)
    if duplicate_qs.exists():
        raise ValueError(f"Invoice number '{invoice_number}' is already used by another job.")

    excluded_bill_ids = set()
    if exclude_inventory_bill_id:
        excluded_bill_ids.add(exclude_inventory_bill_id)
    if exclude_inventory_entry_id:
        linked_bill_id = (
            InventoryEntry.objects.filter(pk=exclude_inventory_entry_id)
            .values_list('bill_id', flat=True)
            .first()
        )
        if linked_bill_id:
            excluded_bill_ids.add(linked_bill_id)

    inventory_qs = InventoryBill.objects.filter(
        entry_type='sale',
        invoice_number__iexact=invoice_number,
    )
    if excluded_bill_ids:
        inventory_qs = inventory_qs.exclude(pk__in=excluded_bill_ids)
    if allow_inventory_job_id:
        inventory_qs = inventory_qs.exclude(job_ticket_id=allow_inventory_job_id)
    if inventory_qs.exists():
        raise ValueError(f"Invoice number '{invoice_number}' is already used in sales register.")

def _inventory_sales_invoice_prefix():
    try:
        profile = CompanyProfile.get_profile()
        raw_prefix = (profile.sales_invoice_prefix or '').strip()
    except (OperationalError, ProgrammingError):
        raw_prefix = ''
    prefix = re.sub(r'[^A-Za-z0-9]+', '', raw_prefix).upper() or 'INV'
    return prefix[:20]

def _build_dated_inventory_invoice_number(
    entry_type,
    entry_date,
    prefix,
    exclude_bill_id=None,
    workspace=None,
    reserve=True,
):
    date_part = entry_date.strftime('%Y%m%d')
    if reserve:
        sequence_key = _inventory_sequence_key('invoice', entry_type, entry_date, workspace)
        sequence = _next_inventory_sequence(sequence_key)
        invoice_number = f'{prefix}-{date_part}-{sequence:03d}'
        while InventoryBill.objects.filter(
            entry_type=entry_type,
            invoice_number=invoice_number,
        ).exclude(pk=exclude_bill_id).exists():
            sequence = _next_inventory_sequence(sequence_key)
            invoice_number = f'{prefix}-{date_part}-{sequence:03d}'
        return invoice_number

    scope_qs = InventoryBill.objects.filter(entry_type=entry_type, entry_date=entry_date)
    if exclude_bill_id:
        scope_qs = scope_qs.exclude(pk=exclude_bill_id)
    return f'{prefix}-{date_part}-{scope_qs.count() + 1:03d}'

def get_next_job_code():
    """
    Generates the next job code using the format PREFIX-YYMMDD-XXX.
    The counter is global and never resets - it always continues from the
    all-time highest counter across all days.
    """
    from django.db import transaction

    # Cache prefix to avoid a DB hit on every job creation
    cache_key_prefix = 'company_job_code_prefix_v1'
    job_prefix = cache.get(cache_key_prefix)
    if job_prefix is None:
        company = CompanyProfile.get_profile()
        job_prefix = company.job_code_prefix or 'GI'
        cache.set(cache_key_prefix, job_prefix, 600)

    today = timezone.localdate()
    date_fragment = today.strftime('%y%m%d')
    prefix = f'{job_prefix}-{date_fragment}-'

    with transaction.atomic():
        sequence, created = DailyJobCodeSequence.objects.select_for_update().get_or_create(date=today)

        # Use cached all-time max to avoid a second query inside the lock
        cache_key_max = 'dailyjobcode_max_counter_v1'
        all_time_highest = cache.get(cache_key_max)
        if all_time_highest is None:
            all_time_highest = DailyJobCodeSequence.objects.aggregate(
                max_counter=Max('last_counter')
            )['max_counter'] or 0

        next_counter = max(sequence.last_counter, all_time_highest) + 1
        job_code = f'{prefix}{next_counter:03d}'

        sequence.last_counter = next_counter
        sequence.save(update_fields=['last_counter'])

    # Update cached max so future calls skip the aggregate query
    cache.set(cache_key_max, next_counter, 600)
    return job_code

def get_phone_service_snapshot(phone, workspace=None):
    normalized_phone, _ = normalize_indian_phone(phone, required=False)
    if not normalized_phone:
        return {
            'phone': '',
            'exists': False,
            'client_id': None,
            'client_name': '',
            'total_jobs': 0,
            'open_jobs': 0,
            'recent_jobs': [],
            'outstanding_balance': '0.00',
            'latest_device': None,
        }

    phone_variants = phone_lookup_variants(normalized_phone)
    jobs_scope = JobTicket.objects.filter(customer_phone__in=phone_variants)
    if workspace:
        jobs_scope = jobs_scope.filter(workspace=workspace)
    jobs_qs = jobs_scope.order_by('-created_at')
    total_jobs = jobs_qs.count()
    open_jobs = jobs_qs.exclude(status__in=['Closed', 'Cancelled', 'Delivered']).count()
    latest_job = jobs_qs.first()

    recent_jobs = []
    job_rows_for_balance = list(jobs_qs[:15].prefetch_related('service_logs'))
    calculate_job_totals(job_rows_for_balance)
    total_billed = Decimal('0.00')
    total_paid = Decimal('0.00')
    for job in job_rows_for_balance:
        d_total = _money_or_zero(job.discount_amount)
        n_total = _net_amount_after_discount(job.total, d_total)
        p_total = job.amount_paid or Decimal('0.00')
        total_billed += n_total
        total_paid += p_total

    for job in job_rows_for_balance[:3]:
        recent_jobs.append({
            'job_code': job.job_code,
            'device_type': job.device_type,
            'device_brand': job.device_brand,
            'device_model': job.device_model,
            'status': job.status,
            'created_at': job.created_at.strftime('%Y-%m-%d'),
        })

    outstanding_balance = max(Decimal('0.00'), total_billed - total_paid)

    client_scope = Client.objects.filter(phone__in=phone_variants)
    if workspace:
        client_scope = client_scope.filter(workspace=workspace)
    client = client_scope.order_by('id').first()
    fallback_name = latest_job.customer_name if (latest_job and not client) else ''

    latest_device = None
    if latest_job:
        latest_device = {
            'device_type': latest_job.device_type or '',
            'device_brand': latest_job.device_brand or '',
            'device_model': latest_job.device_model or '',
        }

    return {
        'phone': normalized_phone,
        'exists': client is not None,
        'client_id': client.id if client else None,
        'client_name': client.name if client else fallback_name,
        'total_jobs': total_jobs,
        'open_jobs': open_jobs,
        'recent_jobs': recent_jobs,
        'outstanding_balance': f"{outstanding_balance:.2f}",
        'latest_device': latest_device,
    }


# --- HELPER FUNCTION FOR WEBSOCKET UPDATES ---

def send_job_update_message(job_code, new_status, old_status=None):
    """Utility to send a real-time status update to all clients watching jobs."""
    try:
        from channels.layers import get_channel_layer
        from asgiref.sync import async_to_sync
        from ..consumers import staff_group_name, tech_group_name, job_group_name, tech_tasks_group_name
        
        channel_layer = get_channel_layer()
        if channel_layer:
            job = (
                JobTicket.objects.select_related('assigned_to__user')
                .only(
                    'job_code',
                    'workspace_id',
                    'customer_name',
                    'device_type',
                    'device_brand',
                    'assigned_to_id',
                    'assigned_to__user__username',
                )
                .get(job_code=job_code)
            )
            tech_user = job.assigned_to.user.username if job.assigned_to and job.assigned_to.user else None
            tech_id = job.assigned_to_id
            device_str = f"{job.device_type} {job.device_brand or ''}".strip()
            workspace_id = job.workspace_id

            message = {
                'type': 'job_status_update',
                'job_code': job_code,
                'status': new_status,
                'old_status': old_status,
                'customer_name': job.customer_name,
                'device': device_str,
                'assigned_tech_id': tech_id,
                'assigned_tech_username': tech_user,
            }
            async_to_sync(channel_layer.group_send)(staff_group_name(workspace_id), message)
            if workspace_id:
                async_to_sync(channel_layer.group_send)(staff_group_name(None), message)

            async_to_sync(channel_layer.group_send)(tech_group_name(workspace_id), message)
            if workspace_id:
                async_to_sync(channel_layer.group_send)(tech_group_name(None), message)

            # Send assignment alert directly to the technician's private feed
            if tech_id:
                personal_event = {
                    'type': 'task_feed_event',
                    'action': 'job_assigned',
                    'job_code': job_code,
                    'customer_name': job.customer_name,
                    'device': device_str,
                    'status': new_status,
                    'technician_id': tech_id,
                    'technician_username': tech_user,
                }
                async_to_sync(channel_layer.group_send)(tech_tasks_group_name(workspace_id, tech_id), personal_event)
                if workspace_id:
                    async_to_sync(channel_layer.group_send)(tech_tasks_group_name(None, tech_id), personal_event)
            
            async_to_sync(channel_layer.group_send)(job_group_name(workspace_id, job_code), message)
    except Exception as e:
        # Silently fail if channels is not configured
        print(f"WebSocket update failed: {e}")


def broadcast_task_message(task, msg):
    """Broadcast a new task message to the task room and relevant dashboard feeds."""
    try:
        from channels.layers import get_channel_layer
        from asgiref.sync import async_to_sync
        from ..consumers import task_room_group_name, staff_tasks_group_name, tech_tasks_group_name

        channel_layer = get_channel_layer()
        if not channel_layer:
            return

        workspace_id = task.workspace_id
        attachments = []
        if hasattr(msg, 'attachments'):
            for att in msg.attachments.all():
                attachments.append({
                    'id': att.id,
                    'name': att.file_name or (att.file.name.split('/')[-1] if att.file else 'file'),
                    'url': att.file.url if att.file else '',
                    'size': att.file_size or 0,
                })

        payload = {
            'type': 'task_message_event',
            'task_id': task.id,
            'id': msg.id,
            'message_id': msg.id,
            'sender': msg.sender.username if msg.sender else 'System',
            'sender_id': msg.sender_id,
            'body': msg.body,
            'sent_at': timezone.localtime(msg.sent_at).strftime('%d %b %Y, %H:%M'),
            'attachments': attachments,
        }

        async_to_sync(channel_layer.group_send)(
            task_room_group_name(workspace_id, task.id),
            payload,
        )

        feed_notice = {
            'type': 'task_feed_event',
            'action': 'new_message',
            'task_id': task.id,
            'task_title': task.title,
            'sender': msg.sender.username if msg.sender else 'System',
            'preview': msg.body[:80],
        }
        async_to_sync(channel_layer.group_send)(
            staff_tasks_group_name(workspace_id),
            feed_notice,
        )
        if task.assigned_to_id:
            async_to_sync(channel_layer.group_send)(
                tech_tasks_group_name(workspace_id, task.assigned_to_id),
                feed_notice,
            )
    except Exception as e:
        print(f"broadcast_task_message failed: {e}")


def broadcast_task_status(task, old_status, new_status, user=None):
    """Broadcast a task status change to the task room and dashboard feeds."""
    try:
        from channels.layers import get_channel_layer
        from asgiref.sync import async_to_sync
        from ..consumers import task_room_group_name, staff_tasks_group_name, tech_tasks_group_name

        channel_layer = get_channel_layer()
        if not channel_layer:
            return

        workspace_id = task.workspace_id
        payload = {
            'type': 'task_status_event',
            'task_id': task.id,
            'old_status': old_status,
            'new_status': new_status,
            'status_display': task.get_status_display(),
            'updated_by': user.username if user else 'System',
            'completed_at': timezone.localtime(task.completed_at).strftime('%Y-%m-%d %H:%M') if task.completed_at else '',
        }

        async_to_sync(channel_layer.group_send)(
            task_room_group_name(workspace_id, task.id),
            payload,
        )

        feed_payload = {
            'type': 'task_feed_event',
            'action': 'status_change',
            'task_id': task.id,
            'task_title': task.title,
            'priority': task.priority,
            'old_status': old_status,
            'new_status': new_status,
            'status_display': task.get_status_display(),
            'assigned_to_id': task.assigned_to_id,
        }
        async_to_sync(channel_layer.group_send)(
            staff_tasks_group_name(workspace_id),
            feed_payload,
        )
        if task.assigned_to_id:
            async_to_sync(channel_layer.group_send)(
                tech_tasks_group_name(workspace_id, task.assigned_to_id),
                feed_payload,
            )
    except Exception as e:
        print(f"broadcast_task_status failed: {e}")


def broadcast_task_created(task):
    """Broadcast newly created task to staff dashboard and assigned technician feed (or all techs if open pool)."""
    try:
        from channels.layers import get_channel_layer
        from asgiref.sync import async_to_sync
        from ..consumers import staff_tasks_group_name, tech_tasks_group_name, tech_group_name

        channel_layer = get_channel_layer()
        if not channel_layer:
            return

        workspace_id = task.workspace_id
        feed_payload = {
            'type': 'task_feed_event',
            'action': 'task_created',
            'task_id': task.id,
            'title': task.title,
            'description': task.description[:120] if task.description else '',
            'priority': task.priority,
            'priority_display': task.get_priority_display(),
            'status': task.status,
            'status_display': task.get_status_display(),
            'due_date': timezone.localtime(task.due_date).strftime('%d %b, %H:%M') if task.due_date else '',
            'assigned_to_id': task.assigned_to_id,
            'assigned_to_name': task.assigned_to.user.username if task.assigned_to and getattr(task.assigned_to, 'user', None) else '',
            'is_open_to_all': task.is_open_to_all,
            'has_alarm': task.has_alarm,
            'alarm_time': timezone.localtime(task.alarm_time).strftime('%d %b, %H:%M') if task.alarm_time else '',
            'created_at': timezone.localtime(task.created_at).strftime('%d %b, %H:%M'),
        }

        async_to_sync(channel_layer.group_send)(
            staff_tasks_group_name(workspace_id),
            feed_payload,
        )
        if workspace_id:
            async_to_sync(channel_layer.group_send)(
                staff_tasks_group_name(None),
                feed_payload,
            )

        if task.assigned_to_id:
            async_to_sync(channel_layer.group_send)(
                tech_tasks_group_name(workspace_id, task.assigned_to_id),
                feed_payload,
            )
            if workspace_id:
                async_to_sync(channel_layer.group_send)(
                    tech_tasks_group_name(None, task.assigned_to_id),
                    feed_payload,
                )
        elif task.is_open_to_all:
            # Broadcast to ALL technicians' global channel
            async_to_sync(channel_layer.group_send)(
                tech_group_name(workspace_id),
                feed_payload,
            )
            if workspace_id:
                async_to_sync(channel_layer.group_send)(
                    tech_group_name(None),
                    feed_payload,
                )
    except Exception as e:
        print(f"broadcast_task_created failed: {e}")


def broadcast_task_accepted(task, technician):
    """Broadcast task acceptance to staff and technician feeds in real time."""
    try:
        from channels.layers import get_channel_layer
        from asgiref.sync import async_to_sync
        from ..consumers import staff_tasks_group_name, tech_group_name

        channel_layer = get_channel_layer()
        if not channel_layer:
            return

        workspace_id = task.workspace_id
        tech_name = technician.user.get_full_name() or technician.user.username
        feed_payload = {
            'type': 'task_feed_event',
            'action': 'task_accepted',
            'task_id': task.id,
            'title': task.title,
            'assigned_to_id': technician.id,
            'assigned_to_name': tech_name,
            'status': task.status,
            'status_display': task.get_status_display(),
        }

        async_to_sync(channel_layer.group_send)(staff_tasks_group_name(workspace_id), feed_payload)
        if workspace_id:
            async_to_sync(channel_layer.group_send)(staff_tasks_group_name(None), feed_payload)
        async_to_sync(channel_layer.group_send)(tech_group_name(workspace_id), feed_payload)
        if workspace_id:
            async_to_sync(channel_layer.group_send)(tech_group_name(None), feed_payload)
    except Exception as e:
        print(f"broadcast_task_accepted failed: {e}")


def broadcast_task_deleted(workspace_id, task_id, title):
    """Broadcast task deletion notice to staff dashboard feed."""
    try:
        from channels.layers import get_channel_layer
        from asgiref.sync import async_to_sync
        from ..consumers import staff_tasks_group_name

        channel_layer = get_channel_layer()
        if not channel_layer:
            return

        async_to_sync(channel_layer.group_send)(
            staff_tasks_group_name(workspace_id),
            {
                'type': 'task_feed_event',
                'action': 'task_deleted',
                'task_id': task_id,
                'title': title,
            },
        )
    except Exception as e:
        print(f"broadcast_task_deleted failed: {e}")

def report_date_bounds(start_date, end_date):
    """Inclusive calendar dates represented by an exclusive next-midnight boundary."""
    if end_date < start_date:
        start_date, end_date = end_date, start_date
    return (
        timezone.make_aware(datetime.combine(start_date, datetime.min.time())),
        timezone.make_aware(datetime.combine(end_date + timedelta(days=1), datetime.min.time())),
    )


def get_daily_report_jobs(report_date, filter_type, workspace=None):
    jobs = scope_to_workspace(JobTicket.objects, workspace)
    start, end = report_date_bounds(report_date, report_date)
    if filter_type == 'in':
        return jobs.filter(created_at__gte=start, created_at__lt=end)
    target_status = {'out': 'Closed', 'completed': 'Completed'}[filter_type]
    transitions = JobTicketLog.objects.filter(
        action__in=['STATUS', 'CLOSED'],
        details__icontains=f"to '{target_status}'",
        timestamp__gte=start,
        timestamp__lt=end,
    ).values('job_ticket_id')
    # This is an event count: moving on to pickup/closure must not erase completion.
    return jobs.filter(pk__in=transitions)


def with_report_dates(jobs):
    """Prefer audited events; retain the last-edit fallback for legacy unaudited jobs."""
    transitions = JobTicketLog.objects.filter(
        job_ticket_id=OuterRef('pk'), action__in=['STATUS', 'CLOSED'],
    ).order_by('-timestamp', '-pk')
    return jobs.annotate(
        report_closed_at=Coalesce(
            Subquery(transitions.filter(details__icontains="to 'Closed'").values('timestamp')[:1]),
            F('closed_at'), F('updated_at'), output_field=DateTimeField(),
        ),
        report_completed_at=Coalesce(
            Subquery(transitions.filter(details__icontains="to 'Completed'").values('timestamp')[:1]),
            F('updated_at'), output_field=DateTimeField(),
        ),
    ).annotate(
        report_date=Case(
            When(specialized_service__returned_date__isnull=False, then=F('specialized_service__returned_date')),
            When(status='Closed', then=F('report_closed_at')),
            When(status__in=['Completed', 'Ready for Pickup'], then=F('report_completed_at')),
            default=F('updated_at'), output_field=DateTimeField(),
        ),
    )


def get_jobs_for_report_period(start_of_period, end_of_period, status_filter=None, workspace=None):
    """Get jobs that should be reported in the given period using vendor concept.

    Vendor jobs: included when returned_date falls in period.
    Regular jobs: included using audited completion/closure dates where available.
    Uses DB-level filtering to avoid loading all jobs into Python.
    """
    from django.db.models import Q

    finished_statuses = ['Completed', 'Closed']

    base_filter = Q(status__in=finished_statuses)
    if status_filter == 'ALL_FINISHED':
        pass  # already filtered above
    elif status_filter == 'ACTIVE_WORKLOAD':
        base_filter = Q(status__in=['Under Inspection', 'Repairing', 'Specialized Service'])
    elif status_filter and status_filter != 'ALL':
        base_filter = Q(status=status_filter)

    # Regular jobs: audited report date, no vendor service or vendor not yet sent.
    scoped_jobs = with_report_dates(scope_to_workspace(JobTicket.objects, workspace))
    regular_jobs = scoped_jobs.filter(
        base_filter,
        report_date__gte=start_of_period,
        report_date__lt=end_of_period,
    ).filter(
        Q(specialized_service__isnull=True) |
        Q(specialized_service__status='Awaiting Assignment')
    )

    # Vendor jobs: returned_date in period
    vendor_jobs = scoped_jobs.filter(
        base_filter,
        specialized_service__returned_date__gte=start_of_period,
        specialized_service__returned_date__lt=end_of_period,
    )

    combined_ids = set(regular_jobs.values_list('id', flat=True)) | set(vendor_jobs.values_list('id', flat=True))
    return list(scoped_jobs.filter(id__in=combined_ids).select_related('specialized_service'))

def get_technician_jobs_for_period(start_of_period, end_of_period, workspace=None):
    """Get jobs completed in the given period for technician performance reporting.

    - For in-house regular jobs: counts jobs that reached 'Completed', 'Ready for Pickup',
      or 'Closed' status whose completion timestamp (report_completed_at) falls within [start_of_period, end_of_period).
    - For specialized service (vendor) jobs: counts jobs whose returned_date falls within [start_of_period, end_of_period).
    """
    from django.db.models import Q
    scoped_jobs = with_report_dates(scope_to_workspace(JobTicket.objects, workspace))
    finished_filter = Q(status__in=['Completed', 'Ready for Pickup', 'Closed'])

    regular_jobs = scoped_jobs.filter(
        finished_filter,
        report_completed_at__gte=start_of_period,
        report_completed_at__lt=end_of_period,
    ).filter(
        Q(specialized_service__isnull=True) |
        Q(specialized_service__status='Awaiting Assignment')
    )

    vendor_jobs = scoped_jobs.filter(
        finished_filter,
        specialized_service__returned_date__gte=start_of_period,
        specialized_service__returned_date__lt=end_of_period,
    )

    combined_ids = set(regular_jobs.values_list('id', flat=True)) | set(vendor_jobs.values_list('id', flat=True))
    return scoped_jobs.filter(id__in=combined_ids)

def calculate_job_totals(jobs, exclude_vendor_charges=False):
    """Calculates part_total, service_total, and total for a list of JobTicket objects."""
    from decimal import Decimal, InvalidOperation, ROUND_HALF_UP

    def safe_decimal(value):
        """Safely convert a value to Decimal, returning Decimal('0') if invalid."""
        if value is None:
            return Decimal('0')
        try:
            return Decimal(str(value)).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
        except (InvalidOperation, ValueError, TypeError):
            return Decimal('0')

    for job in jobs:
        try:
            # Access the logs directly from the job object (already prefetched or lazy loaded)
            job_logs = list(job.service_logs.all())
            
            # Filter out vendor service charges if requested
            if exclude_vendor_charges:
                job_logs = [log for log in job_logs if 'Specialized Service' not in log.description]
            
            # Calculate totals with safe conversion
            job.part_total = sum(safe_decimal(log.part_cost) for log in job_logs)
            job.service_total = sum(safe_decimal(log.service_charge) for log in job_logs)
            job.total = job.part_total + job.service_total
            job.grand_total = max(Decimal('0.00'), job.total - (job.discount_amount or Decimal('0.00')))

        except Exception:
            # Fallback to zero for any calculation error
            job.part_total = Decimal('0')
            job.service_total = Decimal('0')
            job.total = Decimal('0')
            job.grand_total = Decimal('0')


def build_job_billing_context(job, request=None):
    """
    Construct a unified, enriched context dict for invoice / billing print views.
    Includes workspace-aware CompanyProfile, itemized service and spare parts lines,
    consumed hardware serial numbers, and reverse tax-inclusive GST breakdown.
    """
    from decimal import Decimal, ROUND_HALF_UP
    from urllib.parse import quote

    job_tickets = [job]
    calculate_job_totals(job_tickets)

    subtotal = job.total
    discount = job.discount_amount or Decimal('0.00')
    grand_total = max(Decimal('0.00'), subtotal - discount)
    amount_paid = job.amount_paid or Decimal('0.00')
    balance_due = max(Decimal('0.00'), grand_total - amount_paid)

    # Workspace-aware company profile
    workspace = job.workspace
    if not workspace and request:
        workspace = getattr(request, 'current_workspace', None)
    company = CompanyProfile.get_profile(workspace)

    technician_id = ''
    technician_name = ''
    if job.assigned_to:
        technician_id = job.assigned_to.unique_id or str(job.assigned_to.id)
        if job.assigned_to.user:
            technician_name = (
                job.assigned_to.user.get_full_name()
                or job.assigned_to.user.username
            )

    if getattr(company, 'technician_display_format', 'id') == 'name':
        serviced_by = technician_name or technician_id or 'Workshop Staff'
    else:
        serviced_by = technician_id or technician_name or 'Workshop Staff'

    created_by_id = job.created_by.id if job.created_by else 'N/A'
    created_by_name = ''
    if job.created_by:
        created_by_name = (
            job.created_by.get_full_name()
            or job.created_by.username
        )

    # Clean and enrich service logs
    service_logs = job.service_logs.all().select_related('product_sale__product')
    cleaned_service_logs = []
    total_parts = Decimal('0.00')
    total_service = Decimal('0.00')

    for idx, log in enumerate(service_logs, start=1):
        if 'Specialized Service' in log.description:
            description = 'Specialized Service'
        else:
            description = log.description

        part_cost = log.part_cost or Decimal('0.00')
        service_charge = log.service_charge or Decimal('0.00')
        line_total = part_cost + service_charge
        total_parts += part_cost
        total_service += service_charge

        product_sale = getattr(log, 'product_sale', None)
        if product_sale:
            is_product_sale = True
            product_name = product_sale.product.name if product_sale.product else description
            quantity = product_sale.quantity or 1
            unit_price = product_sale.unit_price or (line_total / Decimal(quantity))
        else:
            is_product_sale = False
            product_name = ''
            quantity = 1
            unit_price = line_total

        cleaned_service_logs.append({
            'item_number': idx,
            'description': description,
            'part_cost': part_cost,
            'service_charge': service_charge,
            'is_product_sale': is_product_sale,
            'product_name': product_name,
            'quantity': quantity,
            'unit_price': unit_price,
            'line_total': line_total,
        })

    # Serialized parts consumed for this ticket
    consumed_serials = list(job.consumed_serials.select_related('product').all())

    # GST Breakdown (Tax-inclusive reverse computation)
    enable_gst = bool(company.enable_gst and (company.gstin or company.gst_rate))
    gst_rate = company.gst_rate or Decimal('18.00')
    if enable_gst and gst_rate > Decimal('0.00') and grand_total > Decimal('0.00'):
        tax_divisor = Decimal('1.00') + (gst_rate / Decimal('100.00'))
        taxable_amount = (grand_total / tax_divisor).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
        total_gst = (grand_total - taxable_amount).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
        cgst_rate = (gst_rate / Decimal('2')).quantize(Decimal('0.01'))
        sgst_rate = cgst_rate
        cgst_amount = (total_gst / Decimal('2')).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
        sgst_amount = total_gst - cgst_amount
    else:
        enable_gst = False
        taxable_amount = grand_total
        total_gst = Decimal('0.00')
        cgst_rate = Decimal('0.00')
        sgst_rate = Decimal('0.00')
        cgst_amount = Decimal('0.00')
        sgst_amount = Decimal('0.00')

    # Resolve default bank / financial account for payment settlement
    default_bank = None
    if workspace:
        default_bank = FinancialAccount.get_default_bank_account(workspace)
    if not default_bank:
        default_bank = FinancialAccount.objects.filter(is_active=True, is_default_bank=True).first()

    bank_name = ''
    account_number = ''
    ifsc_code = ''
    branch = ''
    upi_id = ''

    if default_bank:
        bank_name = (default_bank.bank_name or default_bank.name or '').strip()
        account_number = (default_bank.account_number or '').strip()
        ifsc_code = (default_bank.ifsc_code or '').strip()
        branch = (default_bank.branch or '').strip()
        upi_id = (default_bank.upi_id or '').strip()

    # Fallback to company profile if empty
    bank_name = bank_name or (company.bank_name or '').strip()
    account_number = account_number or (company.account_number or '').strip()
    ifsc_code = ifsc_code or (company.ifsc_code or '').strip()
    branch = branch or (company.branch or '').strip()
    upi_id = upi_id or (company.upi_id or '').strip()

    # UPI Dynamic Payment URL
    upi_payment_url = ""
    effective_upi_id = upi_id or (company.upi_id or '').strip()
    if effective_upi_id and balance_due > Decimal('0.00'):
        pa = quote(effective_upi_id, safe='@')
        pn = quote(company.company_name.strip())
        am = f"{balance_due:.2f}"
        tn = quote(f"Job-{job.job_code}")
        upi_payment_url = f"upi://pay?pa={pa}&pn={pn}&am={am}&cu=INR&tn={tn}"

    autoprint = _parse_autoprint_flag(request, default=True) if request else True

    return {
        'job': job,
        'service_logs': cleaned_service_logs,
        'consumed_serials': consumed_serials,
        'total_parts': total_parts,
        'total_service': total_service,
        'subtotal': subtotal,
        'discount': discount,
        'grand_total': grand_total,
        'amount_paid': amount_paid,
        'balance_due': balance_due,
        'taxable_amount': taxable_amount,
        'enable_gst': enable_gst,
        'gst_rate': gst_rate,
        'cgst_rate': cgst_rate,
        'sgst_rate': sgst_rate,
        'cgst_amount': cgst_amount,
        'sgst_amount': sgst_amount,
        'total_gst': total_gst,
        'technician_id': technician_id,
        'technician_name': technician_name,
        'serviced_by': serviced_by,
        'created_by_id': created_by_id,
        'created_by_name': created_by_name,
        'company': company,
        'default_bank_account': default_bank,
        'bank_name': bank_name,
        'account_number': account_number,
        'ifsc_code': ifsc_code,
        'branch': branch,
        'upi_id': upi_id,
        'upi_payment_url': upi_payment_url,
        'autoprint': autoprint,
    }


def _get_post_login_redirect(user):
    if user.is_staff:
        access = get_staff_access(user)
        if access.get("staff_dashboard"):
            return redirect('staff_dashboard')
        if access.get("reports_dashboard"):
            return redirect('reports_dashboard')
        if access.get("inventory"):
            return redirect('inventory_dashboard')
        if access.get("team_management"):
            return redirect('staff_technicians')
        if access.get("feedback_analytics"):
            return redirect('feedback_analytics')
        if access.get("company_settings"):
            return redirect('company_profile_settings')
        return redirect('unauthorized')
    if user.groups.filter(name='Technicians').exists():
        return redirect('technician_dashboard')
    return redirect('home')

def _get_safe_next_url(request):
    next_url = (request.POST.get('next') or request.GET.get('next') or '').strip()
    if not next_url:
        return ''

    if url_has_allowed_host_and_scheme(
        next_url,
        allowed_hosts={request.get_host()},
        require_https=request.is_secure(),
    ):
        candidate_path = urlsplit(next_url).path or '/'
        try:
            resolve(candidate_path)
        except Resolver404:
            return ''
        return next_url
    return ''


def _get_permitted_next_url(user, next_url):
    if not next_url or not user or not getattr(user, "is_authenticated", False):
        return ''

    candidate_path = urlsplit(next_url).path or '/'
    access = get_staff_access(user)
    is_technician = bool(getattr(user, "is_superuser", False) or user.groups.filter(name='Technicians').exists())

    guarded_prefixes = (
        ('/staff-dashboard/', bool(getattr(user, 'is_staff', False) and access.get('staff_dashboard'))),
        ('/staff/clients/', bool(getattr(user, 'is_staff', False) and access.get('staff_dashboard'))),
        ('/staff/vendors/', bool(getattr(user, 'is_staff', False) and access.get('staff_dashboard'))),
        ('/job-created/', bool(getattr(user, 'is_staff', False) and access.get('staff_dashboard'))),
        ('/staff/billing/', bool(getattr(user, 'is_staff', False) and access.get('staff_dashboard'))),
        ('/staff/job/', bool(getattr(user, 'is_staff', False) and access.get('staff_dashboard'))),
        ('/staff/job-archive/', bool(getattr(user, 'is_staff', False) and access.get('staff_dashboard'))),
        ('/api/job-status/', bool(getattr(user, 'is_staff', False) and access.get('staff_dashboard'))),
        ('/api/client-phone-lookup/', bool(getattr(user, 'is_staff', False) and access.get('staff_dashboard'))),
        ('/staff/inventory/', bool(getattr(user, 'is_staff', False) and access.get('inventory'))),
        ('/staff/products/', bool(getattr(user, 'is_staff', False) and access.get('inventory'))),
        ('/staff/technicians/', bool(getattr(user, 'is_staff', False) and access.get('team_management'))),
        ('/staff/reports/', bool(getattr(user, 'is_staff', False) and access.get('reports_dashboard'))),
        ('/staff/feedback-analytics/', bool(getattr(user, 'is_staff', False) and access.get('feedback_analytics'))),
        ('/staff/company-profile/', bool(getattr(user, 'is_staff', False) and access.get('company_settings'))),
        ('/technician-dashboard/', is_technician),
        ('/technician/', is_technician),
    )

    for prefix, allowed in guarded_prefixes:
        if candidate_path.startswith(prefix):
            return next_url if allowed else ''

    return next_url

def _summarize_user_agent(user_agent):
    user_agent = (user_agent or '').lower()
    if not user_agent:
        return 'Unknown Device'

    if 'android' in user_agent:
        os_name = 'Android'
    elif 'iphone' in user_agent or 'ipad' in user_agent or 'ios' in user_agent:
        os_name = 'iOS'
    elif 'windows' in user_agent:
        os_name = 'Windows'
    elif 'mac os' in user_agent or 'macintosh' in user_agent:
        os_name = 'macOS'
    elif 'linux' in user_agent:
        os_name = 'Linux'
    else:
        os_name = 'Unknown OS'

    if 'edg/' in user_agent:
        browser = 'Edge'
    elif 'chrome/' in user_agent and 'edg/' not in user_agent:
        browser = 'Chrome'
    elif 'firefox/' in user_agent:
        browser = 'Firefox'
    elif 'safari/' in user_agent and 'chrome/' not in user_agent:
        browser = 'Safari'
    elif 'opr/' in user_agent or 'opera' in user_agent:
        browser = 'Opera'
    else:
        browser = 'Unknown Browser'

    return f'{browser} on {os_name}'

def get_report_period(request):
    """Parses date parameters and returns timezone-aware start_date and end_date."""
    
    start_date_str = request.GET.get('start_date')
    end_date_str = request.GET.get('end_date')
    status_filter = request.GET.get('status_filter') # NEW
    preset = request.GET.get('preset')
    
    today = timezone.localdate()
    
    # 1. Custom Date Range
    if start_date_str and end_date_str:
        try:
            start_date = datetime.strptime(start_date_str, '%Y-%m-%d').date()
            end_date = datetime.strptime(end_date_str, '%Y-%m-%d').date()
            
            start_of_period, end_of_period = report_date_bounds(start_date, end_date)
            start_date_str = timezone.localdate(start_of_period).isoformat()
            end_date_str = (timezone.localdate(end_of_period) - timedelta(days=1)).isoformat()
            
            period_name = f"Custom: {start_date_str} to {end_date_str}"
            
            return {
                'start': start_of_period,
                'end': end_of_period,
                'name': period_name,
                'start_date_str': start_date_str, # Use string from GET for input field
                'end_date_str': end_date_str,     # Use string from GET for input field
                'preset': '',
            }
            
        except ValueError:
            # Fallback if dates are invalid
            return get_report_period_default()
            
    # 2. Preset: This Month
    elif preset == 'this_month':
        start_date = today.replace(day=1)
        # Calculate the first day of the next month (exclusive end boundary)
        if today.month == 12:
            end_date_boundary = today.replace(year=today.year + 1, month=1, day=1)
        else:
            end_date_boundary = today.replace(month=today.month + 1, day=1)
            
        end_date_for_input = end_date_boundary - timedelta(days=1)
        
        start_of_period = timezone.make_aware(datetime(start_date.year, start_date.month, start_date.day))
        end_of_period = timezone.make_aware(datetime(end_date_boundary.year, end_date_boundary.month, end_date_boundary.day))
        period_name = f"Monthly: {start_date.strftime('%B %Y')}"
        
        return {
            'start': start_of_period,
            'end': end_of_period,
            'name': period_name,
            'start_date_str': start_date.strftime('%Y-%m-%d'),
            'end_date_str': end_date_for_input.strftime('%Y-%m-%d'),
            'preset': 'this_month',
        }

    # 3. Preset: Previous / Last Month
    elif preset in ('last_month', 'previous_month'):
        first_day_current_month = today.replace(day=1)
        last_day_prev_month = first_day_current_month - timedelta(days=1)
        start_date = last_day_prev_month.replace(day=1)
        end_date_boundary = first_day_current_month
        end_date_for_input = last_day_prev_month

        start_of_period = timezone.make_aware(datetime(start_date.year, start_date.month, start_date.day))
        end_of_period = timezone.make_aware(datetime(end_date_boundary.year, end_date_boundary.month, end_date_boundary.day))
        period_name = f"Monthly: {start_date.strftime('%B %Y')}"

        return {
            'start': start_of_period,
            'end': end_of_period,
            'name': period_name,
            'start_date_str': start_date.strftime('%Y-%m-%d'),
            'end_date_str': end_date_for_input.strftime('%Y-%m-%d'),
            'preset': preset,
        }
        
    # 4. Preset: Last 7 Days (Weekly Meetup)
    elif preset == 'last_7_days':
        start_date = today - timedelta(days=6)
        end_date = today # Today is the end
        
        start_of_period, end_of_period = report_date_bounds(start_date, end_date)
        period_name = "Last 7 Days (Weekly)"

        return {
            'start': start_of_period,
            'end': end_of_period,
            'name': period_name,
            'start_date_str': start_date.strftime('%Y-%m-%d'),
            'end_date_str': end_date.strftime('%Y-%m-%d'),
            'preset': 'last_7_days',
        }

    # 5. Fallback/Default: If no parameters are passed, use the default helper.
    return get_report_period_default()

def get_report_period_default():
    today = timezone.localdate()
    start_date = today.replace(day=1)
    
    # Calculate the first day of the NEXT month. This is the boundary.
    if start_date.month == 12:
        # If December, next month is January of next year
        end_date_boundary = start_date.replace(year=start_date.year + 1, month=1)
    else:
        # Otherwise, just increment the month
        end_date_boundary = start_date.replace(month=start_date.month + 1)
        
    # The end date for the input field is the last day of the current month
    end_date_for_input = end_date_boundary - timedelta(days=1)
    
    # 1. Start of Period (00:00:00)
    start_of_period = timezone.make_aware(datetime(start_date.year, start_date.month, start_date.day))
    
    # 2. End of Period (The start of the next month, 00:00:00)
    # NOTE: We use end_date_boundary here, which is the correct non-inclusive end for queries
    end_of_period = timezone.make_aware(datetime(end_date_boundary.year, end_date_boundary.month, end_date_boundary.day))
    
    return {
        'start': start_of_period,
        'end': end_of_period,
        'name': f"Monthly: {start_date.strftime('%B %Y')}",
        'start_date_str': start_date.strftime('%Y-%m-%d'),
        'end_date_str': end_date_for_input.strftime('%Y-%m-%d'), # for input fields
        'preset': '',
    }

def _parse_autoprint_flag(request, default=True):
    raw_value = (request.GET.get('autoprint') or '').strip().lower()
    if not raw_value:
        return default
    return raw_value not in {'0', 'false', 'no', 'off'}

def resolve_monthly_summary_period(request):
    """Resolve monthly summary period and return normalized date metadata."""
    start_date_str = (request.GET.get('start_date') or '').strip()
    end_date_str = (request.GET.get('end_date') or '').strip()
    preset = (request.GET.get('preset') or '').strip()
    show_jobs = (request.GET.get('show_jobs') or '').strip()

    start_date = None
    end_date = None
    today = timezone.localdate()

    if start_date_str and end_date_str:
        try:
            start_date = datetime.strptime(start_date_str, '%Y-%m-%d').date()
            end_date = datetime.strptime(end_date_str, '%Y-%m-%d').date()
            if end_date < start_date:
                raise ValueError("End date cannot be before start date")
        except ValueError:
            start_date = None
            end_date = None
            start_date_str = ''
            end_date_str = ''

    if start_date is None or end_date is None:
        if preset in ('last_month', 'previous_month'):
            first_day_this_month = today.replace(day=1)
            end_date = first_day_this_month - timedelta(days=1)
            start_date = end_date.replace(day=1)
        elif preset == 'this_month':
            start_date = today.replace(day=1)
            end_date = today
        else:
            start_date = today.replace(day=1)
            if start_date.month == 12:
                next_month_start = start_date.replace(year=start_date.year + 1, month=1, day=1)
            else:
                next_month_start = start_date.replace(month=start_date.month + 1, day=1)
            end_date = next_month_start - timedelta(days=1)
            preset = ''

        start_date_str = start_date.strftime('%Y-%m-%d')
        end_date_str = end_date.strftime('%Y-%m-%d')

    start_of_period = timezone.make_aware(datetime.combine(start_date, datetime.min.time()))
    end_of_period = timezone.make_aware(datetime.combine(end_date + timedelta(days=1), datetime.min.time()))

    return {
        'start_of_period': start_of_period,
        'end_of_period': end_of_period,
        'start_date_str': start_date_str,
        'end_date_str': end_date_str,
        'preset': preset,
        'show_jobs': show_jobs,
    }
INVENTORY_ENTRY_CONFIG = {
    'purchase': {
        'title': 'Purchase',
        'icon': 'fa-cart-arrow-down',
        'description': 'Add purchase entries to increase stock.',
        'url_name': 'inventory_purchase_dashboard',
        'submit_label': 'Record Purchase',
        'success_label': 'Purchase',
        'party_label': 'Parties',
        'party_label_singular': 'Party',
    },
    'purchase_return': {
        'title': 'Purchase Return',
        'icon': 'fa-rotate-left',
        'description': 'Review purchase return bills created from the purchase register.',
        'url_name': 'inventory_purchase_return_dashboard',
        'submit_label': 'Record Purchase Return',
        'success_label': 'Purchase return',
        'party_label': 'Parties',
        'party_label_singular': 'Party',
    },
    'sale': {
        'title': 'Sales',
        'icon': 'fa-cart-shopping',
        'description': 'Create sales entries that reduce stock.',
        'url_name': 'inventory_sales_dashboard',
        'submit_label': 'Record Sale',
        'success_label': 'Sale',
        'party_label': 'Parties',
        'party_label_singular': 'Party',
    },
    'sale_return': {
        'title': 'Sales Return',
        'icon': 'fa-rotate-right',
        'description': 'Review sales return bills created from the sales register.',
        'url_name': 'inventory_sales_return_dashboard',
        'submit_label': 'Record Sales Return',
        'success_label': 'Sales return',
        'party_label': 'Parties',
        'party_label_singular': 'Party',
    },
}


def _next_inventory_sequence(sequence_key):
    with transaction.atomic():
        sequence, _created = InventoryNumberSequence.objects.get_or_create(
            sequence_key=sequence_key,
            defaults={'last_counter': 0},
        )
        sequence = InventoryNumberSequence.objects.select_for_update().get(pk=sequence.pk)
        sequence.last_counter += 1
        sequence.save(update_fields=['last_counter', 'updated_at'])
        return sequence.last_counter


def _inventory_sequence_key(kind, entry_type, entry_date, workspace=None):
    # Bill, entry, and generated invoice fields are globally unique in the
    # existing schema, so their counters must also be global.
    return f'{kind}:{entry_type}:{entry_date.isoformat()}'


def _generate_inventory_entry_number(entry_type, entry_date, workspace=None):
    prefix_map = {
        'purchase': 'PUR',
        'purchase_return': 'PRN',
        'sale': 'SAL',
        'sale_return': 'SRN',
    }
    prefix = prefix_map.get(entry_type, 'INV')
    date_part = entry_date.strftime('%Y%m%d')
    sequence_key = _inventory_sequence_key('entry', entry_type, entry_date, workspace)
    sequence = _next_inventory_sequence(sequence_key)
    entry_number = f'{prefix}-{date_part}-{sequence:03d}'
    while InventoryEntry.objects.filter(entry_number=entry_number).exists():
        sequence = _next_inventory_sequence(sequence_key)
        entry_number = f'{prefix}-{date_part}-{sequence:03d}'
    return entry_number

def _generate_inventory_bill_number(entry_type, entry_date, workspace=None):
    prefix_map = {
        'purchase': 'PB',
        'purchase_return': 'PRB',
        'sale': 'SB',
        'sale_return': 'SRB',
    }
    prefix = prefix_map.get(entry_type, 'IB')
    date_part = entry_date.strftime('%Y%m%d')
    sequence_key = _inventory_sequence_key('bill', entry_type, entry_date, workspace)
    sequence = _next_inventory_sequence(sequence_key)
    bill_number = f'{prefix}-{date_part}-{sequence:03d}'
    while InventoryBill.objects.filter(bill_number=bill_number).exists():
        sequence = _next_inventory_sequence(sequence_key)
        bill_number = f'{prefix}-{date_part}-{sequence:03d}'
    return bill_number

def _generate_inventory_invoice_number(entry_type, entry_date, workspace=None):
    if entry_type == 'sale':
        return _build_dated_inventory_invoice_number(
            entry_type='sale',
            entry_date=entry_date,
            prefix=_inventory_sales_invoice_prefix(),
            workspace=workspace,
        )
    
    prefix_map = {
        'purchase': 'PIB',
        'purchase_return': 'PRB',
        'sale_return': 'SRB',
    }
    prefix = prefix_map.get(entry_type, 'IB')
    date_part = entry_date.strftime('%Y%m%d')
    sequence_key = _inventory_sequence_key('invoice', entry_type, entry_date, workspace)
    sequence = _next_inventory_sequence(sequence_key)
    invoice_number = f'{prefix}-{date_part}-{sequence:04d}'
    while InventoryBill.objects.filter(
        entry_type=entry_type,
        invoice_number=invoice_number,
    ).exists():
        sequence = _next_inventory_sequence(sequence_key)
        invoice_number = f'{prefix}-{date_part}-{sequence:04d}'
    return invoice_number

def _peek_sales_invoice_number():
    return _build_dated_inventory_invoice_number(
        entry_type='sale',
        entry_date=timezone.localdate(),
        prefix=_inventory_sales_invoice_prefix(),
        reserve=False,
    )

def _parse_inventory_decimal(raw_value, label, default='0.00'):
    text = (raw_value or '').strip()
    if text == '':
        return Decimal(default)
    try:
        return Decimal(text)
    except (InvalidOperation, TypeError, ValueError):
        raise ValueError(label)

def _collect_inventory_bill_lines(product_ids, quantities, unit_prices, gst_rates, serials_list=None, descriptions_list=None, workspace=None):
    serials_list = serials_list or []
    descriptions_list = descriptions_list or []
    max_lines = max(
        len(product_ids),
        len(quantities),
        len(unit_prices),
        len(gst_rates),
        len(serials_list),
        len(descriptions_list),
    )

    def get_row_value(values, idx):
        if idx < len(values):
            return (values[idx] or '').strip()
        return ''

    line_items = []
    seen_product_ids = set()
    for idx in range(max_lines):
        product_id = get_row_value(product_ids, idx)
        qty_raw = get_row_value(quantities, idx)
        unit_price_raw = get_row_value(unit_prices, idx)
        gst_raw = get_row_value(gst_rates, idx)
        serials_raw = get_row_value(serials_list, idx)
        desc_raw = get_row_value(descriptions_list, idx)

        if not any([product_id, qty_raw, unit_price_raw, gst_raw, serials_raw, desc_raw]):
            continue

        line_no = idx + 1
        if not product_id:
            raise ValueError(f"Line {line_no}: select a product.")

        try:
            quantity = int(qty_raw or '0')
        except (TypeError, ValueError):
            raise ValueError(f"Line {line_no}: quantity must be a whole number.")
        if quantity <= 0:
            raise ValueError(f"Line {line_no}: quantity must be greater than zero.")

        if unit_price_raw:
            validate_rate_no_special_chars(unit_price_raw, f"Line {line_no}")
        unit_price = _parse_inventory_decimal(unit_price_raw, f"Line {line_no}: invalid unit price.")
        if unit_price < 0:
            raise ValueError(f"Line {line_no}: unit price cannot be negative.")

        gst_rate = _parse_inventory_decimal(gst_raw, f"Line {line_no}: invalid GST rate.")
        if gst_rate < 0:
            raise ValueError(f"Line {line_no}: GST rate cannot be negative.")

        product = scope_to_workspace(
            Product.objects.filter(pk=product_id, is_active=True),
            workspace,
        ).first()
        if not product:
            raise ValueError(f"Line {line_no}: selected product was not found.")
        if product.id in seen_product_ids:
            raise ValueError(
                f"Line {line_no}: duplicate product '{product.name}' is not allowed in the same bill."
            )
        seen_product_ids.add(product.id)

        parsed_serials = []
        if serials_raw:
            raw_tokens = re.split(r'[\r\n,]+', serials_raw)
            for token in raw_tokens:
                s = token.strip()
                if s and s not in parsed_serials:
                    parsed_serials.append(s)

        if product.has_serial_tracking and parsed_serials:
            if len(parsed_serials) > quantity:
                raise ValueError(
                    f"Line {line_no} ({product.name}): entered {len(parsed_serials)} serial number(s) but quantity is only {quantity}."
                )

        line_items.append(
            {
                'product': product,
                'quantity': quantity,
                'unit_price': unit_price,
                'line_amount': Decimal(quantity) * unit_price,
                'gst_rate': gst_rate,
                'serials': parsed_serials,
                'description': desc_raw,
            }
        )

    if not line_items:
        raise ValueError("Add at least one product line.")
    return line_items

def _apply_inventory_bill_discount(line_items, bill_discount_amount, bill_notes):
    subtotal_amount = sum((line['line_amount'] for line in line_items), Decimal('0.00'))
    if bill_discount_amount > subtotal_amount:
        raise ValueError("Bill discount cannot exceed subtotal amount.")

    line_discounts = [Decimal('0.00') for _ in line_items]
    if bill_discount_amount > 0 and subtotal_amount > 0:
        for idx, line in enumerate(line_items):
            provisional = (
                (bill_discount_amount * line['line_amount']) / subtotal_amount
            ).quantize(Decimal('0.01'))
            if provisional > line['line_amount']:
                provisional = line['line_amount']
            line_discounts[idx] = provisional

        allocated_discount = sum(line_discounts, Decimal('0.00'))
        remaining_discount = (bill_discount_amount - allocated_discount).quantize(Decimal('0.01'))
        step = Decimal('0.01')
        reorder_indexes = sorted(
            range(len(line_items)),
            key=lambda row_idx: line_items[row_idx]['line_amount'],
            reverse=True,
        )
        while remaining_discount != Decimal('0.00'):
            changed = False
            for row_idx in reorder_indexes:
                if remaining_discount > 0:
                    capacity = line_items[row_idx]['line_amount'] - line_discounts[row_idx]
                    if capacity >= step:
                        line_discounts[row_idx] += step
                        remaining_discount -= step
                        changed = True
                else:
                    if line_discounts[row_idx] >= step:
                        line_discounts[row_idx] -= step
                        remaining_discount += step
                        changed = True
                remaining_discount = remaining_discount.quantize(Decimal('0.01'))
                if remaining_discount == Decimal('0.00'):
                    break
            if not changed:
                break

    return [
        {
            'product': line['product'],
            'quantity': line['quantity'],
            'unit_price': line['unit_price'],
            'discount_amount': line_discounts[idx],
            'gst_rate': line['gst_rate'],
            'notes': line.get('description') or bill_notes,
            'description': line.get('description', ''),
            'serials': line.get('serials', []),
        }
        for idx, line in enumerate(line_items)
    ]

def _require_inventory_edit_password(request):
    password = (request.POST.get('edit_password') or '').strip()
    if not password:
        raise ValueError('Enter your password to edit the bill.')
    if not request.user.check_password(password):
        raise ValueError('Incorrect password.')

def _replace_inventory_bill_entries(
    request,
    *,
    entry_type,
    bill_id,
    entry_date,
    invoice_date=None,
    party,
    invoice_number,
    line_items,
    job_ticket=None,
):
    with transaction.atomic():
        locked_bill = (
            InventoryBill.objects.select_for_update()
            .filter(pk=bill_id, entry_type=entry_type)
            .first()
        )
        if not locked_bill:
            raise ValueError('Selected bill was not found.')

        locked_entries = list(
            InventoryEntry.objects.select_for_update()
            .filter(bill_id=locked_bill.id, entry_type=entry_type)
            .select_related('product')
            .order_by('id')
        )
        if not locked_entries:
            raise ValueError('Selected bill was not found.')

        locked_products = {
            product.id: product
            for product in Product.objects.select_for_update().filter(
                pk__in={row.product_id for row in locked_entries}
            )
        }
        scope_label = (invoice_number or locked_bill.invoice_number or locked_bill.bill_number).strip()
        for old_entry in locked_entries:
            product = locked_products.get(old_entry.product_id)
            if not product:
                raise ValueError(f"Product for invoice '{scope_label or old_entry.entry_number}' was not found.")
            restored_stock = product.stock_quantity - old_entry.stock_effect
            if restored_stock < 0:
                raise ValueError(
                    f"Cannot edit invoice '{scope_label or old_entry.entry_number}'. "
                    f"Stock reconciliation failed for '{product.name}'."
                )
            product.stock_quantity = restored_stock

        for product in locked_products.values():
            product.save(update_fields=['stock_quantity'])

        InventoryEntry.objects.filter(pk__in=[row.id for row in locked_entries]).delete()
        return _record_inventory_entries(
            request=request,
            entry_type=entry_type,
            entry_date=entry_date,
            invoice_date=invoice_date,
            party=party,
            invoice_number=invoice_number,
            line_items=line_items,
            job_ticket=job_ticket,
            existing_bill=locked_bill,
        )

def _build_inventory_redirect_url(request, url_name, extra_params=None):
    params = request.GET.copy()
    for key, value in (extra_params or {}).items():
        if value in (None, ''):
            params.pop(key, None)
        else:
            params[key] = value
    base_url = reverse(url_name)
    query_string = params.urlencode()
    return f"{base_url}?{query_string}" if query_string else base_url

def _inventory_post_response(request, url_name, ok, message, extra_params=None):
    redirect_url = _build_inventory_redirect_url(request, url_name, extra_params=extra_params)
    is_async = (
        request.headers.get('X-Botgi-Async') == '1'
        or request.headers.get('X-Requested-With') == 'XMLHttpRequest'
    )
    if is_async:
        payload = {
            'ok': ok,
            'message': message,
        }
        if ok:
            payload['reload_url'] = redirect_url
        return JsonResponse(payload)

    if ok:
        messages.success(request, message)
    else:
        messages.error(request, message)
    return redirect(redirect_url)

def _process_inventory_grouped_bill_edit(request, *, entry_type):
    config = INVENTORY_ENTRY_CONFIG[entry_type]
    current_workspace = getattr(request, 'current_workspace', None)
    bill_id_raw = (request.POST.get('bill_id') or '').strip()
    entry_id = (request.POST.get('entry_id') or '').strip()
    if not entry_id or not bill_id_raw.isdigit():
        raise ValueError('Selected bill was not found.')

    grouped_entry = scope_to_workspace(InventoryEntry.objects.filter(
        pk=entry_id,
        bill_id=int(bill_id_raw),
        entry_type=entry_type,
    ), current_workspace).select_related('job_ticket', 'party').first()
    if not grouped_entry:
        raise ValueError('Selected bill was not found.')

    if InventoryCreditPayment.objects.filter(bill_id=int(bill_id_raw)).exists():
        raise ValueError(
            'Paid or partially paid bills cannot be edited. '
            'Record a reversal and replacement instead.'
        )

    _require_inventory_edit_password(request)

    invoice_date = None
    if entry_type == 'purchase':
        edit_invoice_date_raw = (request.POST.get('edit_invoice_date') or '').strip()
        if not edit_invoice_date_raw:
            raise ValueError('Invoice date is required.')
        try:
            invoice_date = datetime.strptime(edit_invoice_date_raw, '%Y-%m-%d').date()
        except ValueError as exc:
            raise ValueError('Invoice date is invalid.') from exc
        new_entry_date = invoice_date
    else:
        edit_entry_date_raw = (request.POST.get('edit_entry_date') or '').strip()
        if not edit_entry_date_raw:
            raise ValueError('Date is required.')
        try:
            new_entry_date = datetime.strptime(edit_entry_date_raw, '%Y-%m-%d').date()
        except ValueError as exc:
            raise ValueError('Date is invalid.') from exc

    new_invoice_number = (request.POST.get('edit_invoice_number') or '').strip()
    if not new_invoice_number:
        raise ValueError('Invoice number is required.')

    bill_notes = (
        request.POST.get('edit_bill_notes')
        or request.POST.get('edit_notes')
        or ''
    ).strip()
    bill_discount_amount = _parse_inventory_decimal(
        (request.POST.get('edit_bill_discount_amount') or '').strip() or '0.00',
        "Invalid bill discount amount.",
    )
    if bill_discount_amount < 0:
        raise ValueError("Bill discount cannot be negative.")

    scoped_entries = list(
        scope_to_workspace(InventoryEntry.objects.filter(
            bill_id=int(bill_id_raw),
            entry_type=entry_type,
        ), current_workspace).select_related('bill', 'party', 'job_ticket').order_by('id')
    )
    if not scoped_entries:
        raise ValueError('Selected bill was not found.')

    linked_job_ids = {row.job_ticket_id for row in scoped_entries if row.job_ticket_id}
    if len(linked_job_ids) > 1:
        raise ValueError('Cannot edit a bill linked to multiple jobs.')
    if entry_type == 'sale' and linked_job_ids:
        raise ValueError('Service-linked sales bills must be edited from the job billing screen.')

    valid_party_qs = scope_to_workspace(InventoryParty.objects.filter(is_active=True), current_workspace)

    if linked_job_ids:
        new_party = scoped_entries[0].party
    else:
        edit_party_id = (request.POST.get('edit_party_id') or '').strip()
        new_party = valid_party_qs.filter(pk=edit_party_id).first()
    if not new_party:
        raise ValueError(f"Please select a valid {config['party_label_singular'].lower()}.")

    if entry_type == 'sale':
        validate_sales_invoice_number_uniqueness(
            new_invoice_number,
            exclude_inventory_bill_id=int(bill_id_raw),
        )

    line_items = _collect_inventory_bill_lines(
        request.POST.getlist('edit_line_product_id[]'),
        request.POST.getlist('edit_line_quantity[]'),
        request.POST.getlist('edit_line_unit_price[]'),
        request.POST.getlist('edit_line_gst_rate[]'),
        descriptions_list=request.POST.getlist('edit_line_description[]'),
        workspace=current_workspace,
    )
    final_line_items = _apply_inventory_bill_discount(
        line_items,
        bill_discount_amount,
        bill_notes,
    )
    created_entries = _replace_inventory_bill_entries(
        request,
        entry_type=entry_type,
        bill_id=int(bill_id_raw),
        entry_date=new_entry_date,
        invoice_date=invoice_date,
        party=new_party,
        invoice_number=new_invoice_number,
        line_items=final_line_items,
        job_ticket=scoped_entries[0].job_ticket if linked_job_ids else None,
    )

    if entry_type in {'purchase', 'purchase_return'}:
        for created_entry in created_entries:
            product = created_entry.product
            if product.cost_price != created_entry.unit_price:
                product.cost_price = created_entry.unit_price
                product.save(update_fields=['cost_price'])

    saved_invoice = created_entries[0].invoice_number if created_entries else new_invoice_number
    return {
        'invoice_number': saved_invoice,
        'message': f"{config['success_label']} invoice {saved_invoice} updated successfully.",
    }

def _get_or_create_inventory_customer_party_for_job(job):
    customer_name = (job.customer_name or '').strip() or f"Customer {job.job_code}"
    customer_phone = (job.customer_phone or '').strip()
    workspace = getattr(job, 'workspace', None)

    party_qs = InventoryParty.objects.all()
    if workspace:
        party_qs = party_qs.filter(workspace=workspace)
    party = None
    if customer_phone:
        party = party_qs.filter(phone=customer_phone).first()
    # A phone mismatch is evidence that this is a different accounting
    # party; only use the name fallback when no phone was supplied.
    if not party and not customer_phone and customer_name:
        party = party_qs.filter(name__iexact=customer_name).first()

    if party:
        update_fields = []
        if not party.is_active:
            party.is_active = True
            update_fields.append('is_active')
        if customer_phone and party.phone != customer_phone:
            party.phone = customer_phone[:20]
            update_fields.append('phone')
        if customer_name and party.name != customer_name:
            party.name = customer_name[:200]
            update_fields.append('name')
        if workspace and party.workspace_id != workspace.id:
            party.workspace = workspace
            update_fields.append('workspace')
        if update_fields:
            party.save(update_fields=update_fields)
        return party

    return InventoryParty.objects.create(
        workspace=workspace,
        name=customer_name[:200],
        party_type='both',
        phone=customer_phone[:20],
        is_active=True,
    )


def _check_duplicate_bill_submission(entry_type, invoice_number, party_id, workspace):
    """
    Guard against duplicate bill creation for non-sale entry types where
    invoice_number is supplied by the user.

    For sale bills: invoice_number is auto-generated, so this guard does not apply
    (the invoice_number uniqueness constraint handles that path).

    For purchase/purchase_return/sale_return: the combination of
    (entry_type, invoice_number, party) within a workspace must be unique.

    Returns the existing InventoryBill if a duplicate is detected, else None.
    """
    if entry_type == 'sale':
        return None  # Auto-numbered; handled elsewhere.
    if not invoice_number or not party_id:
        return None
    normalized = (invoice_number or '').strip()
    if not normalized:
        return None

    existing = scope_to_workspace(
        InventoryBill.objects.filter(
            entry_type=entry_type,
            invoice_number=normalized,
            party_id=party_id,
        ),
        workspace,
    ).first()
    return existing


def _build_inventory_initial_payment(request, entry_type, entry_date):
    if entry_type not in {'purchase', 'sale'}:
        return None

    default_status = 'paid' if entry_type == 'sale' else 'unpaid'
    payment_status = (request.POST.get('bill_payment_status') or default_status).strip().lower()
    if payment_status not in {'paid', 'unpaid', 'part_paid'}:
        raise ValueError('Select whether this bill is paid, partially paid, or unpaid.')
    if payment_status == 'unpaid':
        return {'status': 'unpaid'}

    workspace = getattr(request, 'current_workspace', None)
    financial_account_id = (request.POST.get('financial_account') or '').strip()
    target_account = None
    if financial_account_id.isdigit():
        target_account = scope_to_workspace(
            FinancialAccount.objects.filter(is_active=True),
            workspace,
        ).filter(pk=int(financial_account_id)).first()

    raw_method = (request.POST.get('bill_payment_method') or '').strip()
    if target_account:
        if target_account.account_type == FinancialAccount.ACCOUNT_TYPE_CASH:
            payment_method = InventoryCreditPayment.METHOD_CASH
        elif target_account.account_type == FinancialAccount.ACCOUNT_TYPE_UPI:
            payment_method = InventoryCreditPayment.METHOD_UPI
        elif target_account.account_type == FinancialAccount.ACCOUNT_TYPE_BANK:
            payment_method = InventoryCreditPayment.METHOD_TRANSFER
        else:
            payment_method = InventoryCreditPayment.METHOD_TRANSFER
    elif raw_method and raw_method in {choice[0] for choice in InventoryCreditPayment.METHOD_CHOICES}:
        payment_method = raw_method
        target_account = FinancialAccount.get_default_for_payment_method(workspace, payment_method)
    else:
        target_account = FinancialAccount.get_default_for_payment_method(workspace, 'cash')
        payment_method = InventoryCreditPayment.METHOD_CASH

    payment_date = entry_date
    payment_date_raw = (request.POST.get('bill_payment_date') or '').strip()
    if payment_date_raw:
        try:
            payment_date = datetime.strptime(payment_date_raw, '%Y-%m-%d').date()
        except ValueError:
            raise ValueError('Payment date is invalid.')

    amount_paid = None
    if payment_status == 'part_paid':
        amount_paid_raw = (request.POST.get('bill_amount_paid') or '').strip()
        try:
            amount_paid = Decimal(amount_paid_raw)
            if amount_paid <= Decimal('0.00'):
                raise ValueError('Partial payment amount must be greater than zero.')
        except (InvalidOperation, TypeError, ValueError):
            raise ValueError('Enter a valid partial payment amount.')

    return {
        'status': payment_status,
        'financial_account': target_account,
        'payment_method': payment_method,
        'payment_date': payment_date,
        'amount_paid': amount_paid,
        'reference_no': (request.POST.get('bill_payment_reference') or '').strip(),
    }



def _record_inventory_entries(
    request,
    entry_type,
    entry_date,
    party,
    invoice_number,
    line_items,
    invoice_date=None,
    job_ticket=None,
    existing_bill=None,
    initial_payment=None,
    bill_notes=None,
):
    if not line_items:
        raise ValueError("Add at least one product line.")

    normalized_invoice = (invoice_number or '').strip()
    created_entries = []
    workspace = (
        getattr(job_ticket, 'workspace', None)
        or getattr(party, 'workspace', None)
        or getattr(request, 'current_workspace', None)
    )

    with transaction.atomic():
        shared_invoice = normalized_invoice or _generate_inventory_invoice_number(
            entry_type,
            entry_date,
            workspace,
        )
        normalized_invoice_date = invoice_date if entry_type == 'purchase' else None
        if bill_notes is not None and str(bill_notes).strip():
            shared_notes = str(bill_notes).strip()
        else:
            shared_notes = next(((line.get('notes') or '').strip() for line in line_items if (line.get('notes') or '').strip()), '')

        if existing_bill:
            bill = InventoryBill.objects.select_for_update().get(pk=existing_bill.pk)
            update_fields = []
            if bill.entry_type != entry_type:
                bill.entry_type = entry_type
                update_fields.append('entry_type')
            if bill.entry_date != entry_date:
                bill.entry_date = entry_date
                update_fields.append('entry_date')
            if (bill.invoice_number or '') != shared_invoice:
                bill.invoice_number = shared_invoice
                update_fields.append('invoice_number')
            if bill.invoice_date != normalized_invoice_date:
                bill.invoice_date = normalized_invoice_date
                update_fields.append('invoice_date')
            if bill.party_id != party.id:
                bill.party = party
                update_fields.append('party')
            if bill.job_ticket_id != (job_ticket.id if job_ticket else None):
                bill.job_ticket = job_ticket
                update_fields.append('job_ticket')
            if workspace and bill.workspace_id != workspace.id:
                bill.workspace = workspace
                update_fields.append('workspace')
            if (bill.notes or '') != shared_notes:
                bill.notes = shared_notes
                update_fields.append('notes')
            if bill.created_by_id is None and request.user.is_authenticated:
                bill.created_by = request.user
                update_fields.append('created_by')
            if update_fields:
                bill.save(update_fields=update_fields + ['updated_at'])
        else:
            if entry_type != 'sale' and shared_invoice and party:
                InventoryParty.objects.select_for_update().filter(pk=party.pk).first()
                duplicate_bill = _check_duplicate_bill_submission(
                    entry_type=entry_type,
                    invoice_number=shared_invoice,
                    party_id=party.pk,
                    workspace=workspace,
                )
                if duplicate_bill:
                    raise ValueError(
                        f"Duplicate bill detected: Reference '{shared_invoice}' already exists for this party (Bill {duplicate_bill.bill_number})."
                    )

            bill = InventoryBill.objects.create(
                workspace=workspace,
                bill_number=_generate_inventory_bill_number(entry_type, entry_date, workspace),
                entry_type=entry_type,
                entry_date=entry_date,
                invoice_number=shared_invoice,
                invoice_date=normalized_invoice_date,
                job_ticket=job_ticket,
                party=party,
                notes=shared_notes,
                created_by=request.user if request.user.is_authenticated else None,
            )

        for line in line_items:
            product = Product.objects.select_for_update().get(pk=line['product'].pk)
            quantity = int(line['quantity'])
            unit_price = line['unit_price']
            discount_amount = line['discount_amount']
            gst_rate = line['gst_rate']
            notes = line['notes']

            stock_before = product.stock_quantity
            stock_delta = quantity if entry_type in {'purchase', 'sale_return'} else -quantity
            stock_after = stock_before + stock_delta

            if stock_after < 0 and entry_type == 'purchase_return':
                raise ValueError(
                    f"Only {stock_before} unit(s) available in stock for '{product.name}'."
                )

            taxable_amount = (Decimal(quantity) * unit_price) - discount_amount
            gst_amount = (taxable_amount * gst_rate) / Decimal('100')
            total_amount = taxable_amount + gst_amount

            serials = line.get('serials') or []
            serial_text = ', '.join(serials)

            entry = InventoryEntry.objects.create(
                workspace=workspace,
                bill=bill,
                entry_number=_generate_inventory_entry_number(entry_type, entry_date, workspace),
                entry_type=entry_type,
                entry_date=entry_date,
                invoice_number=shared_invoice,
                job_ticket=job_ticket,
                party=party,
                product=product,
                quantity=quantity,
                unit_price=unit_price,
                discount_amount=discount_amount,
                gst_rate=gst_rate,
                taxable_amount=taxable_amount.quantize(Decimal('0.01')),
                gst_amount=gst_amount.quantize(Decimal('0.01')),
                total_amount=total_amount.quantize(Decimal('0.01')),
                stock_before=stock_before,
                stock_after=stock_after,
                serial_numbers_text=serial_text,
                notes=notes,
                created_by=request.user,
            )
            created_entries.append(entry)

            if serials:
                from dateutil.relativedelta import relativedelta
                if entry_type == 'purchase':
                    for sn in serials:
                        vendor_warranty_until = None
                        if product.vendor_warranty_months > 0:
                            vendor_warranty_until = entry_date + relativedelta(months=product.vendor_warranty_months)
                        ProductItemSerial.objects.update_or_create(
                            workspace=workspace,
                            product=product,
                            serial_number=sn,
                            defaults={
                                'status': ProductItemSerial.STATUS_IN_STOCK,
                                'purchase_entry': entry,
                                'purchase_date': entry_date,
                                'purchase_cost': unit_price,
                                'vendor_warranty_until': vendor_warranty_until,
                                'notes': notes,
                            },
                        )
                elif entry_type == 'sale':
                    for sn in serials:
                        customer_warranty_until = None
                        if product.customer_warranty_months > 0:
                            customer_warranty_until = entry_date + relativedelta(months=product.customer_warranty_months)
                        item_serial = ProductItemSerial.objects.filter(
                            workspace=workspace,
                            product=product,
                            serial_number=sn,
                        ).first()
                        if item_serial:
                            item_serial.status = ProductItemSerial.STATUS_SOLD
                            item_serial.sale_entry = entry
                            item_serial.sale_date = entry_date
                            item_serial.sale_price = unit_price
                            item_serial.customer_warranty_until = customer_warranty_until
                            item_serial.save(update_fields=[
                                'status', 'sale_entry', 'sale_date', 'sale_price',
                                'customer_warranty_until', 'updated_at'
                            ])
                        else:
                            ProductItemSerial.objects.create(
                                workspace=workspace,
                                product=product,
                                serial_number=sn,
                                status=ProductItemSerial.STATUS_SOLD,
                                sale_entry=entry,
                                sale_date=entry_date,
                                sale_price=unit_price,
                                customer_warranty_until=customer_warranty_until,
                            )
                elif entry_type == 'sale_return':
                    for sn in serials:
                        item_serial = ProductItemSerial.objects.filter(
                            workspace=workspace,
                            product=product,
                            serial_number=sn,
                        ).first()
                        if item_serial:
                            item_serial.status = ProductItemSerial.STATUS_IN_STOCK
                            item_serial.sale_entry = None
                            item_serial.sale_date = None
                            item_serial.sale_price = Decimal('0.00')
                            item_serial.customer_warranty_until = None
                            item_serial.save(update_fields=[
                                'status', 'sale_entry', 'sale_date', 'sale_price',
                                'customer_warranty_until', 'updated_at'
                            ])

            # Keep master product pricing fresh from purchases.
            if entry_type in {'purchase', 'purchase_return'}:
                update_fields = []
                if product.cost_price != unit_price:
                    product.cost_price = unit_price
                    update_fields.append('cost_price')
                if (product.unit_price or Decimal('0.00')) <= 0 and unit_price > 0:
                    product.unit_price = unit_price
                    update_fields.append('unit_price')
                product.stock_quantity = stock_after
                update_fields.append('stock_quantity')
                product.save(update_fields=update_fields)
            else:
                product.stock_quantity = stock_after
                product.save(update_fields=['stock_quantity'])

        if initial_payment and initial_payment.get('status') in {'paid', 'part_paid'} and created_entries:
            direction = _inventory_credit_direction_for_entry_type(entry_type)
            bill_total = sum((entry.total_amount or Decimal('0.00') for entry in created_entries), Decimal('0.00'))
            if direction and bill_total > Decimal('0.00'):
                bill_total = bill_total.quantize(Decimal('0.01'))
                if initial_payment['status'] == 'paid':
                    pay_amount = bill_total
                    bal_after = Decimal('0.00')
                    pay_notes = 'Initial paid bill settlement'
                else:
                    pay_amount = (initial_payment.get('amount_paid') or Decimal('0.00')).quantize(Decimal('0.01'))
                    if pay_amount >= bill_total:
                        pay_amount = bill_total
                        bal_after = Decimal('0.00')
                        initial_payment['status'] = 'paid'
                        pay_notes = 'Initial paid bill settlement'
                    elif pay_amount <= Decimal('0.00'):
                        raise ValueError('Partial payment amount must be greater than zero.')
                    else:
                        bal_after = (bill_total - pay_amount).quantize(Decimal('0.01'))
                        pay_notes = 'Initial partial payment settlement'

                target_account = initial_payment.get('financial_account')
                credit_payment = InventoryCreditPayment.objects.create(
                    workspace=workspace,
                    party=party,
                    bill=bill,
                    direction=direction,
                    payment_date=initial_payment['payment_date'],
                    payment_method=initial_payment['payment_method'],
                    financial_account=target_account,
                    amount=pay_amount,
                    balance_before=bill_total,
                    balance_after=bal_after,
                    reference_no=initial_payment.get('reference_no', ''),
                    notes=pay_notes,
                    created_by=request.user if request.user.is_authenticated else None,
                )

                if target_account and pay_amount > Decimal('0.00'):
                    party_name = party.name if party else 'Direct'
                    ref_text = f" [Ref: {initial_payment.get('reference_no')}]" if initial_payment.get('reference_no') else ""
                    if direction in {'in', InventoryCreditPayment.DIRECTION_RECEIVABLE}:
                        target_account.deposit(
                            amount=pay_amount,
                            description=f"Sales Bill {bill.invoice_number or bill.bill_number}: {party_name}{ref_text}",
                            reference_no=initial_payment.get('reference_no', '') or (bill.invoice_number or bill.bill_number),
                            transaction_date=initial_payment['payment_date'],
                            inventory_credit_payment=credit_payment,
                            job_ticket=job_ticket,
                            user=request.user if request.user.is_authenticated else None,
                        )
                    elif direction in {'out', InventoryCreditPayment.DIRECTION_PAYABLE}:
                        target_account.withdraw(
                            amount=pay_amount,
                            description=f"Purchase Bill {bill.invoice_number or bill.bill_number}: {party_name}{ref_text}",
                            reference_no=initial_payment.get('reference_no', '') or (bill.invoice_number or bill.bill_number),
                            transaction_date=initial_payment['payment_date'],
                            inventory_credit_payment=credit_payment,
                            job_ticket=job_ticket,
                            user=request.user if request.user.is_authenticated else None,
                        )

    return created_entries

def _prepare_inventory_sale_bill_print_context(sale_bill, sale_entries, invoice_label=''):
    subtotal_amount = Decimal('0.00')
    total_discount = Decimal('0.00')
    taxable_total = Decimal('0.00')
    tax_total = Decimal('0.00')
    grand_total = Decimal('0.00')
    total_quantity = 0

    for line in sale_entries:
        line_subtotal = (Decimal(line.quantity or 0) * (line.unit_price or Decimal('0.00'))).quantize(Decimal('0.01'))
        line.line_subtotal = line_subtotal
        total_quantity += int(line.quantity or 0)
        subtotal_amount += line_subtotal
        total_discount += line.discount_amount or Decimal('0.00')
        taxable_total += line.taxable_amount or Decimal('0.00')
        tax_total += line.gst_amount or Decimal('0.00')
        grand_total += line.total_amount or Decimal('0.00')

    linked_job_codes = sorted(
        {line.job_ticket.job_code for line in sale_entries if line.job_ticket_id}
    )

    return {
        'bill_number': sale_bill.bill_number,
        'invoice_number': (sale_bill.invoice_number or invoice_label or sale_bill.bill_number).strip(),
        'sale_entries': sale_entries,
        'party': sale_bill.party,
        'invoice_date': sale_bill.entry_date,
        'notes': sale_bill.notes or next((line.notes for line in sale_entries if (line.notes or '').strip()), ''),
        'linked_job_codes': linked_job_codes,
        'subtotal_amount': subtotal_amount,
        'total_discount': total_discount,
        'total_quantity': total_quantity,
        'taxable_total': taxable_total,
        'tax_total': tax_total,
        'grand_total': grand_total,
        'company': CompanyProfile.get_profile(),
    }

def _inventory_bill_group_key(entry):
    if getattr(entry, 'bill_id', None):
        return f"bill:{entry.bill_id}"
    invoice_number = (entry.invoice_number or '').strip()
    if invoice_number:
        return f"{entry.entry_type}:{entry.party_id}:{invoice_number}"
    return f"{entry.entry_type}:{entry.party_id}:entry-{entry.id}"

def _build_inventory_bill_summaries(entries, max_groups=None):
    bill_summaries = []
    bill_map = {}

    for entry in entries:
        bill_key = _inventory_bill_group_key(entry)
        header_bill = getattr(entry, 'bill', None) if getattr(entry, 'bill_id', None) else None
        bill = bill_map.get(bill_key)
        if bill is None:
            safe_key = re.sub(r'[^A-Za-z0-9_-]+', '-', bill_key).strip('-') or f"inventory-bill-{len(bill_summaries) + 1}"
            bill = {
                'bill_key': bill_key,
                'bill_id': header_bill.id if header_bill else None,
                'bill_number': header_bill.bill_number if header_bill else '',
                'collapse_id': f"inventory-bill-{safe_key}",
                'entry_type': header_bill.entry_type if header_bill else entry.entry_type,
                'entry_type_label': header_bill.get_entry_type_display() if header_bill else entry.get_entry_type_display(),
                'entry_date': header_bill.entry_date if header_bill else entry.entry_date,
                'invoice_date': (
                    header_bill.invoice_date
                    if header_bill and header_bill.invoice_date
                    else (header_bill.entry_date if header_bill else entry.entry_date)
                ),
                'invoice_number': ((header_bill.invoice_number if header_bill else entry.invoice_number) or '').strip(),
                'invoice_display': ((header_bill.invoice_number if header_bill else entry.invoice_number) or '').strip() or (header_bill.bill_number if header_bill else entry.entry_number),
                'job_ticket': header_bill.job_ticket if header_bill and header_bill.job_ticket_id else entry.job_ticket,
                'job_code': (
                    header_bill.job_ticket.job_code
                    if header_bill and header_bill.job_ticket_id
                    else (entry.job_ticket.job_code if entry.job_ticket_id else '')
                ),
                'party': header_bill.party if header_bill else entry.party,
                'party_id': header_bill.party_id if header_bill else entry.party_id,
                'created_by': header_bill.created_by if header_bill and header_bill.created_by_id else entry.created_by,
                'notes': (header_bill.notes if header_bill else entry.notes) or '',
                'lines': [],
                'voucher_numbers': [],
                'line_count': 0,
                'total_quantity': 0,
                'total_amount': Decimal('0.00'),
                'stock_effect_total': 0,
                '_product_names': [],
                '_product_seen': set(),
            }
            bill_map[bill_key] = bill
            bill_summaries.append(bill)

        bill['lines'].append(entry)
        bill['voucher_numbers'].append(entry.entry_number)
        bill['line_count'] += 1
        bill['total_quantity'] += int(entry.quantity or 0)
        bill['total_amount'] += entry.total_amount or Decimal('0.00')
        bill['stock_effect_total'] += entry.stock_effect

        if not bill['notes'] and entry.notes:
            bill['notes'] = entry.notes

        product_name = entry.product.name
        if product_name not in bill['_product_seen']:
            bill['_product_seen'].add(product_name)
            bill['_product_names'].append(product_name)

    if max_groups is not None:
        bill_summaries = bill_summaries[:max_groups]

    for bill in bill_summaries:
        product_names = bill.pop('_product_names')
        bill.pop('_product_seen', None)
        bill['primary_voucher_number'] = bill['voucher_numbers'][0] if bill['voucher_numbers'] else ''
        extra_voucher_count = max(len(bill['voucher_numbers']) - 1, 0)
        bill['voucher_display'] = (
            f"{bill['primary_voucher_number']} +{extra_voucher_count} more"
            if extra_voucher_count
            else bill['primary_voucher_number']
        )
        if len(product_names) > 2:
            bill['product_summary'] = f"{', '.join(product_names[:2])} +{len(product_names) - 2} more"
        else:
            bill['product_summary'] = ', '.join(product_names)
        bill['item_label'] = f"{bill['line_count']} item" if bill['line_count'] == 1 else f"{bill['line_count']} items"
        bill['bill_number_display'] = bill['bill_number'] or bill['primary_voucher_number']

    return bill_summaries


INVENTORY_CREDIT_ENTRY_DIRECTIONS = {
    'purchase': InventoryCreditPayment.DIRECTION_PAYABLE,
    'sale': InventoryCreditPayment.DIRECTION_RECEIVABLE,
}


def _inventory_credit_direction_for_entry_type(entry_type):
    return INVENTORY_CREDIT_ENTRY_DIRECTIONS.get(entry_type)


# ---------------------------------------------------------------------------
# Accounting service helpers (Phases 2–9)
# ---------------------------------------------------------------------------

def _inventory_bill_total(bill):
    """Return the sum of all InventoryEntry.total_amount for the given bill, or job ticket total if job-linked."""
    total = (
        InventoryEntry.objects
        .filter(bill=bill)
        .aggregate(total=Coalesce(Sum('total_amount', output_field=DecimalField()), Decimal('0.00')))
    )['total'] or Decimal('0.00')
    if total <= Decimal('0.00') and getattr(bill, 'job_ticket_id', None) and bill.job_ticket:
        calculate_job_totals([bill.job_ticket])
        return max(Decimal('0.00'), (bill.job_ticket.total or Decimal('0.00')) - (bill.job_ticket.discount_amount or Decimal('0.00')))
    return total


def _inventory_bill_paid_total(bill):
    """Return the sum of all InventoryCreditPayment.amount for the given bill, including job payments if job-linked."""
    direction = _inventory_credit_direction_for_entry_type(bill.entry_type)
    if not direction:
        return Decimal('0.00')
    credit_total = (
        InventoryCreditPayment.objects
        .filter(bill=bill, direction=direction)
        .aggregate(total=Coalesce(Sum('amount', output_field=DecimalField()), Decimal('0.00')))
    )['total'] or Decimal('0.00')
    if getattr(bill, 'job_ticket_id', None) and bill.job_ticket:
        job_paid = _money_or_zero(bill.job_ticket.amount_paid)
        return max(credit_total, job_paid)
    return credit_total


def _inventory_bill_vendor_credit_applied_total(bill):
    """Return the total vendor-credit amount already applied to reduce this bill's payable."""
    return (
        InventoryVendorCreditApplication.objects
        .filter(target_bill=bill)
        .aggregate(total=Coalesce(Sum('amount', output_field=DecimalField()), Decimal('0.00')))
    )['total'] or Decimal('0.00')


def _inventory_bill_effective_balance(bill):
    """
    Effective balance due on a purchase bill, accounting for both cash payments
    and vendor credit applications.
    """
    total = _inventory_bill_total(bill)
    paid = _inventory_bill_paid_total(bill)
    credit_applied = _inventory_bill_vendor_credit_applied_total(bill)
    balance = total - paid - credit_applied
    return max(balance, Decimal('0.00'))


def _write_bill_log(bill, action, user=None, details='', related_bill=None):
    """
    Append one row to InventoryBillLog.  Never raises — audit failures must not
    abort the business transaction that called this.
    """
    try:
        InventoryBillLog.objects.create(
            workspace=bill.workspace,
            bill=bill,
            action=action,
            user=user,
            details=details,
            related_bill=related_bill,
        )
    except Exception:
        pass  # Audit failure is non-fatal; the business transaction continues.


# ---------------------------------------------------------------------------
# Phase 2–4: Paid-Bill Reversal / Stock Reversal
# ---------------------------------------------------------------------------

_REVERSAL_ENTRY_TYPE = {
    'purchase': 'purchase_return',
    'purchase_return': 'purchase',
    'sale': 'sale_return',
    'sale_return': 'sale',
}


def _reverse_inventory_bill(bill_id, user, reversal_note, workspace=None):
    """
    Atomically reverse an InventoryBill.

    Algorithm:
      1. Lock bill and all its InventoryEntry rows.
      2. Reject if already reversed.
      3. Reject if any credit payment exists and bill is a sale/purchase with
         a partial or full payment — caller must handle payment reconciliation
         before reversal (Phase 3).  For unpaid bills the reversal proceeds.
      4. Determine reversal entry_type (purchase → purchase_return, etc.).
      5. Create a new InventoryBill header (the reversal bill).
      6. For each original line: create a mirror InventoryEntry on the reversal
         bill that restores stock (opposite stock_effect).
      7. Mark original bill is_reversed=True, link reversal_of on new bill.
      8. Write audit logs on both bills.
      9. Return the reversal bill.

    Raises ValueError with a user-visible message on any business-rule violation.
    """
    with transaction.atomic():
        original = (
            scope_to_workspace(
                InventoryBill.objects.select_for_update(of=('self',)).filter(pk=bill_id),
                workspace,
            )
            .select_related('party', 'workspace', 'created_by')
            .first()
        )
        if not original:
            raise ValueError('Bill not found.')
        if original.is_reversed:
            raise ValueError(
                f"Bill {original.bill_number} has already been reversed and cannot be reversed again."
            )
        if original.reversal_of_id:
            raise ValueError(
                f"Bill {original.bill_number} is itself a reversal bill and cannot be reversed."
            )

        # Phase 3: determine payment state
        bill_total = _inventory_bill_total(original)
        paid_total = _inventory_bill_paid_total(original)
        balance = (bill_total - paid_total).quantize(Decimal('0.01'))

        # Determine effective payment status
        if bill_total > Decimal('0.00') and paid_total > Decimal('0.00'):
            if balance <= Decimal('0.00'):
                payment_state = 'fully_paid'
            else:
                payment_state = 'partially_paid'
        else:
            payment_state = 'unpaid'

        reversal_type = _REVERSAL_ENTRY_TYPE.get(original.entry_type)
        if not reversal_type:
            raise ValueError(f"Bills of type '{original.entry_type}' cannot be reversed.")

        original_entries = list(
            InventoryEntry.objects.select_for_update()
            .filter(bill=original)
            .select_related('product')
            .order_by('id')
        )
        if not original_entries:
            raise ValueError(f"Bill {original.bill_number} has no line items and cannot be reversed.")

        # Lock all products before modifying stock
        product_ids = {e.product_id for e in original_entries}
        locked_products = {
            p.id: p
            for p in Product.objects.select_for_update().filter(pk__in=product_ids)
        }

        # Validate stock before committing any changes
        for entry in original_entries:
            product = locked_products.get(entry.product_id)
            if not product:
                raise ValueError(f"Product for entry {entry.entry_number} was not found.")
            # The reversal entry has the *opposite* stock effect:
            # original sale (removes stock) → reversal adds stock back → always safe
            # original purchase (adds stock) → reversal removes stock → may underflow
            reversal_effect = -entry.stock_effect  # opposite sign
            future_stock = product.stock_quantity + reversal_effect
            if future_stock < 0:
                raise ValueError(
                    f"Cannot reverse bill {original.bill_number}: reversing "
                    f"'{product.name}' would reduce stock below zero "
                    f"(current: {product.stock_quantity}, reversal effect: {reversal_effect})."
                )

        today = timezone.localdate()
        reversal_bill = InventoryBill.objects.create(
            workspace=original.workspace,
            bill_number=_generate_inventory_bill_number(reversal_type, today, original.workspace),
            entry_type=reversal_type,
            entry_date=today,
            invoice_number=f"REV-{original.invoice_number or original.bill_number}",
            invoice_date=None,
            job_ticket=original.job_ticket,
            party=original.party,
            notes=reversal_note or f"Reversal of {original.bill_number}",
            created_by=user,
            reversal_of=original,
        )

        reversal_entries = []
        for entry in original_entries:
            product = locked_products[entry.product_id]
            rev_stock_before = product.stock_quantity
            # Reversal of a purchase (stock +N) → purchase_return (stock -N)
            # Reversal of a sale (stock -N)     → sale_return     (stock +N)
            rev_stock_delta = -entry.stock_effect  # opposite of original
            rev_stock_after = rev_stock_before + rev_stock_delta
            product.stock_quantity = rev_stock_after
            product.save(update_fields=['stock_quantity'])

            rev_entry = InventoryEntry.objects.create(
                workspace=original.workspace,
                bill=reversal_bill,
                entry_number=_generate_inventory_entry_number(reversal_type, today, original.workspace),
                entry_type=reversal_type,
                entry_date=today,
                invoice_number=reversal_bill.invoice_number,
                job_ticket=original.job_ticket,
                party=original.party,
                product=product,
                quantity=entry.quantity,
                unit_price=entry.unit_price,
                discount_amount=entry.discount_amount,
                gst_rate=entry.gst_rate,
                taxable_amount=entry.taxable_amount,
                gst_amount=entry.gst_amount,
                total_amount=entry.total_amount,
                stock_before=rev_stock_before,
                stock_after=rev_stock_after,
                notes=f"Reversal of {entry.entry_number}",
                created_by=user,
            )
            reversal_entries.append(rev_entry)

        # Phase 3: credit settlement on original bill
        # For fully_paid or partially_paid bills we create an offsetting credit
        # payment on the reversal bill so the accounting clears.
        if payment_state in {'fully_paid', 'partially_paid'} and paid_total > Decimal('0.00'):
            # The reversal bill needs the same direction as the original to absorb
            # the settlement semantics.
            reversal_direction = _inventory_credit_direction_for_entry_type(original.entry_type)
            if reversal_direction:
                reversal_total = sum(
                    (e.total_amount or Decimal('0.00') for e in reversal_entries), Decimal('0.00')
                ).quantize(Decimal('0.01'))
                # Apply as much as was paid on the original against the reversal,
                # capped at reversal total.
                settlement_amount = min(paid_total, reversal_total).quantize(Decimal('0.01'))
                if settlement_amount > Decimal('0.00'):
                    InventoryCreditPayment.objects.create(
                        workspace=original.workspace,
                        party=original.party,
                        bill=reversal_bill,
                        direction=reversal_direction,
                        payment_date=today,
                        payment_method=InventoryCreditPayment.METHOD_CASH,
                        amount=settlement_amount,
                        balance_before=reversal_total,
                        balance_after=(reversal_total - settlement_amount).quantize(Decimal('0.01')),
                        reference_no='',
                        notes=(
                            f"Auto-settlement from reversal of {original.bill_number}. "
                            f"Original payment state: {payment_state}. "
                            f"Remaining balance (if any) requires manual refund or credit."
                        ),
                        created_by=user,
                    )

        # Mark original as reversed — irreversible
        original.is_reversed = True
        original.reversal_note = reversal_note or ''
        original.reversed_at = timezone.now()
        original.reversed_by = user
        original.save(update_fields=['is_reversed', 'reversal_note', 'reversed_at', 'reversed_by', 'updated_at'])

        # Audit logs
        _write_bill_log(
            original,
            InventoryBillLog.ACTION_BILL_REVERSED,
            user=user,
            details=(
                f"Reversed by {user.get_full_name() or user.username} — "
                f"reversal bill: {reversal_bill.bill_number}. "
                f"Payment state at reversal: {payment_state}. "
                f"Note: {reversal_note or '(none)'}"
            ),
            related_bill=reversal_bill,
        )
        _write_bill_log(
            reversal_bill,
            InventoryBillLog.ACTION_REVERSAL_CREATED,
            user=user,
            details=f"Reversal of {original.bill_number}. {len(reversal_entries)} line(s) mirrored.",
            related_bill=original,
        )

    return reversal_bill, payment_state


# ---------------------------------------------------------------------------
# Phase 5–6: Purchase Return → Vendor Credit Linkage
# ---------------------------------------------------------------------------

def _link_return_to_source_bill(return_bill_id, source_bill_id, user, workspace=None):
    """
    Link a purchase_return (or sale_return) bill to its originating bill and,
    for purchase returns, create/update the InventoryVendorCredit record.

    Rules:
    - return_bill must be entry_type='purchase_return' or 'sale_return'.
    - source_bill must be the matching opposite type.
    - return_bill must not already be linked.
    - Both bills must be in the same workspace.
    - A vendor credit is created only for purchase_return bills.
    - The credit amount equals the return bill total.

    Raises ValueError on any violation.
    Returns (return_bill, vendor_credit_or_None).
    """
    _RETURN_SOURCE_MAP = {
        'purchase_return': 'purchase',
        'sale_return': 'sale',
    }
    with transaction.atomic():
        return_bill = (
            scope_to_workspace(
                InventoryBill.objects.select_for_update(of=('self',)).filter(pk=return_bill_id),
                workspace,
            )
            .select_related('party', 'workspace')
            .first()
        )
        if not return_bill:
            raise ValueError('Return bill not found.')
        expected_source_type = _RETURN_SOURCE_MAP.get(return_bill.entry_type)
        if not expected_source_type:
            raise ValueError(
                f"Bill {return_bill.bill_number} is of type '{return_bill.entry_type}' "
                "and cannot be linked as a return."
            )
        if return_bill.source_bill_id:
            raise ValueError(
                f"Bill {return_bill.bill_number} is already linked to "
                f"source bill {return_bill.source_bill.bill_number}."
            )

        source_bill = (
            scope_to_workspace(
                InventoryBill.objects.select_for_update(of=('self',)).filter(
                    pk=source_bill_id,
                    entry_type=expected_source_type,
                ),
                workspace,
            )
            .select_related('party')
            .first()
        )
        if not source_bill:
            raise ValueError(
                f"Source bill not found or is not a {expected_source_type} bill."
            )
        if source_bill.party_id != return_bill.party_id:
            raise ValueError(
                "Return bill and source bill must belong to the same party."
            )

        return_bill.source_bill = source_bill
        return_bill.save(update_fields=['source_bill', 'updated_at'])

        vendor_credit = None
        if return_bill.entry_type == 'purchase_return':
            return_total = _inventory_bill_total(return_bill).quantize(Decimal('0.01'))
            if return_total > Decimal('0.00'):
                vendor_credit = InventoryVendorCredit.objects.create(
                    workspace=return_bill.workspace,
                    party=return_bill.party,
                    return_bill=return_bill,
                    source_bill=source_bill,
                    credit_amount=return_total,
                    applied_amount=Decimal('0.00'),
                    status=InventoryVendorCredit.STATUS_OPEN,
                    notes=f"Auto-created from return bill {return_bill.bill_number}.",
                    created_by=user,
                )
                _write_bill_log(
                    return_bill,
                    InventoryBillLog.ACTION_VENDOR_CREDIT_CREATED,
                    user=user,
                    details=(
                        f"Vendor credit Rs.{return_total} created for party {return_bill.party.name}. "
                        f"Source bill: {source_bill.bill_number}."
                    ),
                    related_bill=source_bill,
                )

        _write_bill_log(
            return_bill,
            InventoryBillLog.ACTION_RETURN_LINKED,
            user=user,
            details=f"Return bill linked to source bill {source_bill.bill_number}.",
            related_bill=source_bill,
        )

    return return_bill, vendor_credit


# ---------------------------------------------------------------------------
# Phase 7: Vendor Credit Application
# ---------------------------------------------------------------------------

def _apply_vendor_credit_to_bill(vendor_credit_id, target_bill_id, amount, user, workspace=None):
    """
    Apply a portion of a vendor credit against an outstanding purchase bill.

    Rules:
    - vendor_credit must be OPEN or PARTIALLY_APPLIED.
    - target_bill must be entry_type='purchase', not reversed, same party.
    - amount must be > 0, ≤ credit balance, ≤ bill effective balance.
    - Atomic: updates applied_amount and credit status in one transaction.

    Raises ValueError on any violation.
    Returns the InventoryVendorCreditApplication created.
    """
    amount = _money_or_zero(amount).quantize(Decimal('0.01'))
    if amount <= Decimal('0.00'):
        raise ValueError("Application amount must be greater than zero.")

    with transaction.atomic():
        vc = (
            scope_to_workspace(
                InventoryVendorCredit.objects.select_for_update(of=('self',)).filter(pk=vendor_credit_id),
                workspace,
            )
            .select_related('party', 'return_bill')
            .first()
        )
        if not vc:
            raise ValueError("Vendor credit not found.")
        if vc.status in {InventoryVendorCredit.STATUS_FULLY_APPLIED, InventoryVendorCredit.STATUS_VOIDED}:
            raise ValueError(
                f"Vendor credit {vc.return_bill.bill_number} is {vc.status} and cannot be applied."
            )
        credit_balance = vc.balance_amount.quantize(Decimal('0.01'))
        if amount > credit_balance:
            raise ValueError(
                f"Application amount Rs.{amount} exceeds available credit balance Rs.{credit_balance}."
            )

        target_bill = (
            scope_to_workspace(
                InventoryBill.objects.select_for_update(of=('self',)).filter(
                    pk=target_bill_id,
                    entry_type='purchase',
                ),
                workspace,
            )
            .select_related('party')
            .first()
        )
        if not target_bill:
            raise ValueError("Target purchase bill not found.")
        if target_bill.is_reversed:
            raise ValueError(
                f"Bill {target_bill.bill_number} has been reversed and cannot receive a credit application."
            )
        if target_bill.party_id != vc.party_id:
            raise ValueError(
                "Vendor credit and target bill must belong to the same party."
            )

        effective_balance = _inventory_bill_effective_balance(target_bill).quantize(Decimal('0.01'))
        if amount > effective_balance:
            raise ValueError(
                f"Application amount Rs.{amount} exceeds bill outstanding balance Rs.{effective_balance}."
            )

        application = InventoryVendorCreditApplication.objects.create(
            workspace=vc.workspace or (target_bill.workspace),
            vendor_credit=vc,
            target_bill=target_bill,
            amount=amount,
            applied_date=timezone.localdate(),
            notes=f"Credit from return {vc.return_bill.bill_number} applied to {target_bill.bill_number}.",
            created_by=user,
        )

        vc.applied_amount = (vc.applied_amount or Decimal('0.00')) + amount
        vc.save(update_fields=['applied_amount', 'updated_at'])
        vc._recompute_status()

        _write_bill_log(
            target_bill,
            InventoryBillLog.ACTION_VENDOR_CREDIT_APPLIED,
            user=user,
            details=(
                f"Vendor credit Rs.{amount} from {vc.return_bill.bill_number} "
                f"applied to {target_bill.bill_number}. "
                f"Credit remaining: Rs.{vc.balance_amount}."
            ),
            related_bill=vc.return_bill,
        )
        _write_bill_log(
            vc.return_bill,
            InventoryBillLog.ACTION_VENDOR_CREDIT_APPLIED,
            user=user,
            details=(
                f"Rs.{amount} applied to bill {target_bill.bill_number}. "
                f"Credit remaining: Rs.{vc.balance_amount}."
            ),
            related_bill=target_bill,
        )

    return application


def _require_inventory_reversal_password(request):
    """Verify user password before allowing a reversal action."""
    password = (request.POST.get('reversal_password') or '').strip()
    if not password:
        raise ValueError('Enter your password to confirm the reversal.')
    if not request.user.check_password(password):
        raise ValueError('Incorrect password.')


def _process_bill_reversal(request, entry_type):
    """
    Handle a POST request to reverse an InventoryBill.

    Expected POST fields:
      bill_id          — ID of bill to reverse
      reversal_note    — reason for reversal (required)
      reversal_password — user's current password (required)

    Returns a dict: {'message': str, 'reversal_bill': InventoryBill, 'payment_state': str}
    Raises ValueError with user-visible message on any error.
    """
    denied = _staff_access_required(request, "inventory")
    if denied:
        raise ValueError("Access denied.")

    bill_id_raw = (request.POST.get('bill_id') or '').strip()
    reversal_note = (request.POST.get('reversal_note') or '').strip()
    if not bill_id_raw.isdigit():
        raise ValueError('Select a valid bill to reverse.')
    if not reversal_note:
        raise ValueError('A reversal reason is required.')

    _require_inventory_reversal_password(request)

    workspace = getattr(request, 'current_workspace', None)
    reversal_bill, payment_state = _reverse_inventory_bill(
        bill_id=int(bill_id_raw),
        user=request.user,
        reversal_note=reversal_note,
        workspace=workspace,
    )
    config = INVENTORY_ENTRY_CONFIG[entry_type]
    msg = (
        f"{config['success_label']} bill reversed. "
        f"Reversal bill: {reversal_bill.bill_number}. "
        f"Payment state was: {payment_state.replace('_', ' ')}."
    )
    if payment_state == 'partially_paid':
        msg += " Partial payment was auto-settled on the reversal bill. Manual refund of excess may be required."
    elif payment_state == 'fully_paid':
        msg += " Full payment was auto-settled on the reversal bill."
    return {'message': msg, 'reversal_bill': reversal_bill, 'payment_state': payment_state}


def _process_link_return_to_source(request):
    """
    Handle POST request to link a return bill to its source bill and
    (for purchase returns) create the vendor credit.

    Expected POST fields:
      return_bill_id   — ID of the purchase_return or sale_return bill
      source_bill_id   — ID of the original purchase or sale bill

    Returns dict: {'message': str, 'vendor_credit': InventoryVendorCredit|None}
    Raises ValueError on errors.
    """
    return_bill_id_raw = (request.POST.get('return_bill_id') or '').strip()
    source_bill_id_raw = (request.POST.get('source_bill_id') or '').strip()
    if not return_bill_id_raw.isdigit() or not source_bill_id_raw.isdigit():
        raise ValueError('Select valid return and source bills.')

    workspace = getattr(request, 'current_workspace', None)
    return_bill, vendor_credit = _link_return_to_source_bill(
        return_bill_id=int(return_bill_id_raw),
        source_bill_id=int(source_bill_id_raw),
        user=request.user,
        workspace=workspace,
    )
    msg = f"Return bill {return_bill.bill_number} linked to source bill {return_bill.source_bill.bill_number}."
    if vendor_credit:
        msg += f" Vendor credit Rs.{vendor_credit.credit_amount} created."
    return {'message': msg, 'vendor_credit': vendor_credit}


def _process_apply_vendor_credit(request):
    """
    Handle POST request to apply a vendor credit to a purchase bill.

    Expected POST fields:
      vendor_credit_id  — ID of the InventoryVendorCredit
      target_bill_id    — ID of the purchase InventoryBill
      amount            — Decimal amount to apply

    Returns dict: {'message': str, 'application': InventoryVendorCreditApplication}
    Raises ValueError on errors.
    """
    vc_id_raw = (request.POST.get('vendor_credit_id') or '').strip()
    bill_id_raw = (request.POST.get('target_bill_id') or '').strip()
    amount_raw = (request.POST.get('amount') or '').strip()

    if not vc_id_raw.isdigit() or not bill_id_raw.isdigit():
        raise ValueError('Select valid vendor credit and bill.')
    amount = _parse_inventory_decimal(amount_raw, 'Enter a valid application amount.')
    if amount <= Decimal('0.00'):
        raise ValueError('Application amount must be greater than zero.')

    workspace = getattr(request, 'current_workspace', None)
    application = _apply_vendor_credit_to_bill(
        vendor_credit_id=int(vc_id_raw),
        target_bill_id=int(bill_id_raw),
        amount=amount,
        user=request.user,
        workspace=workspace,
    )
    return {
        'message': (
            f"Vendor credit Rs.{application.amount} applied to "
            f"bill {application.target_bill.bill_number}."
        ),
        'application': application,
    }


def _build_vendor_credit_rows(workspace=None):
    """
    Return open/partially-applied vendor credits with their balance amounts.
    Used to populate the vendor credit dashboard widget.
    """
    credits = list(
        scope_to_workspace(
            InventoryVendorCredit.objects.exclude(
                status__in=[InventoryVendorCredit.STATUS_FULLY_APPLIED, InventoryVendorCredit.STATUS_VOIDED]
            ),
            workspace,
        )
        .select_related('party', 'return_bill', 'source_bill')
        .order_by('created_at')
    )
    for vc in credits:
        vc.computed_balance = vc.balance_amount
    return credits


def _inventory_credit_action_label(direction):
    if direction == InventoryCreditPayment.DIRECTION_PAYABLE:
        return 'Pay Supplier'
    if direction == InventoryCreditPayment.DIRECTION_RECEIVABLE:
        return 'Receive Payment'
    return ''


def _inventory_credit_status(total_amount, paid_amount):
    total = _money_or_zero(total_amount)
    paid = _money_or_zero(paid_amount)
    balance = total - paid
    if total <= 0:
        return 'No Credit'
    if balance <= Decimal('0.00'):
        return 'Paid'
    if paid > Decimal('0.00'):
        return 'Part Paid'
    return 'Credit'


def _inventory_credit_payment_totals(bill_ids):
    if not bill_ids:
        return {}
    payment_rows = (
        InventoryCreditPayment.objects
        .filter(bill_id__in=bill_ids)
        .values('bill_id', 'direction')
        .annotate(total=Coalesce(Sum('amount', output_field=DecimalField()), Decimal('0.00')))
    )
    return {
        (row['bill_id'], row['direction']): row['total'] or Decimal('0.00')
        for row in payment_rows
    }


def _attach_inventory_credit_to_bill_summaries(bill_summaries):
    bill_ids = [bill['bill_id'] for bill in bill_summaries if bill.get('bill_id')]
    payment_totals = _inventory_credit_payment_totals(bill_ids)
    for bill in bill_summaries:
        direction = _inventory_credit_direction_for_entry_type(bill.get('entry_type'))
        paid_amount = payment_totals.get((bill.get('bill_id'), direction), Decimal('0.00')) if direction else Decimal('0.00')
        total_amount = _money_or_zero(bill.get('total_amount'))
        balance_amount = total_amount - paid_amount
        if balance_amount < Decimal('0.00'):
            balance_amount = Decimal('0.00')

        bill['credit_direction'] = direction or ''
        bill['credit_action_label'] = _inventory_credit_action_label(direction)
        bill['credit_paid_amount'] = paid_amount
        bill['credit_balance_amount'] = balance_amount
        bill['credit_payment_status'] = _inventory_credit_status(total_amount, paid_amount) if direction else ''
        bill['can_record_credit_payment'] = bool(direction and bill.get('bill_id') and balance_amount > Decimal('0.00'))

    return bill_summaries


def _build_inventory_credit_rows(entry_type, limit=None, workspace=None):
    direction = _inventory_credit_direction_for_entry_type(entry_type)
    if not direction:
        return []

    bills = list(
        scope_to_workspace(InventoryBill.objects.filter(entry_type=entry_type, is_reversed=False), workspace)
        .select_related('party', 'job_ticket')
        .annotate(bill_total=Coalesce(Sum('lines__total_amount', output_field=DecimalField()), Decimal('0.00')))
        .order_by('entry_date', 'id')
    )
    payment_totals = _inventory_credit_payment_totals([bill.id for bill in bills])
    rows = []
    for bill in bills:
        # If linked job is already marked paid or settled, do not list as open receivable
        if getattr(bill, 'job_ticket_id', None) and bill.job_ticket:
            if bill.job_ticket.payment_status == 'paid':
                continue

        total_amount = _money_or_zero(getattr(bill, 'bill_total', Decimal('0.00')))
        if total_amount <= Decimal('0.00') and getattr(bill, 'job_ticket_id', None) and bill.job_ticket:
            total_amount = _inventory_bill_total(bill)
        paid_amount = payment_totals.get((bill.id, direction), Decimal('0.00'))
        if getattr(bill, 'job_ticket_id', None) and bill.job_ticket:
            paid_amount = max(paid_amount, _money_or_zero(bill.job_ticket.amount_paid))
        credit_applied = (
            _inventory_bill_vendor_credit_applied_total(bill)
            if entry_type == 'purchase'
            else Decimal('0.00')
        )
        balance_amount = max(Decimal('0.00'), total_amount - paid_amount - credit_applied)
        if balance_amount <= Decimal('0.00'):
            continue
        rows.append(
            SimpleNamespace(
                bill=bill,
                bill_id=bill.id,
                bill_number=bill.bill_number,
                invoice_number=bill.invoice_number or bill.bill_number,
                entry_date=bill.entry_date,
                party=bill.party,
                party_id=bill.party_id,
                total_amount=total_amount,
                paid_amount=paid_amount,
                balance_amount=balance_amount,
                direction=direction,
                action_label=_inventory_credit_action_label(direction),
                payment_status=_inventory_credit_status(total_amount, paid_amount),
            )
        )
    if limit is not None:
        rows = rows[:limit]
    return rows


def _build_inventory_credit_summary(workspace=None):
    payable_rows = _build_inventory_credit_rows('purchase', workspace=workspace)
    receivable_rows = _build_inventory_credit_rows('sale', workspace=workspace)
    month_start = timezone.localdate().replace(day=1)
    payment_totals = (
        scope_to_workspace(
            InventoryCreditPayment.objects.filter(payment_date__gte=month_start),
            workspace,
        )
        .values('direction')
        .annotate(total=Coalesce(Sum('amount', output_field=DecimalField()), Decimal('0.00')))
    )
    month_payment_map = {row['direction']: row['total'] or Decimal('0.00') for row in payment_totals}

    return {
        'payable_total': sum((row.balance_amount for row in payable_rows), Decimal('0.00')),
        'receivable_total': sum((row.balance_amount for row in receivable_rows), Decimal('0.00')),
        'payable_count': len(payable_rows),
        'receivable_count': len(receivable_rows),
        'month_paid_total': month_payment_map.get(InventoryCreditPayment.DIRECTION_PAYABLE, Decimal('0.00')),
        'month_received_total': month_payment_map.get(InventoryCreditPayment.DIRECTION_RECEIVABLE, Decimal('0.00')),
        'payable_rows': payable_rows[:8],
        'receivable_rows': receivable_rows[:8],
        'recent_payments': list(
            scope_to_workspace(InventoryCreditPayment.objects, workspace)
            .select_related('party', 'bill', 'created_by')
            .order_by('-payment_date', '-created_at')[:10]
        ),
    }


def _record_inventory_credit_payment(request):
    bill_id_raw = (request.POST.get('bill_id') or '').strip()
    amount_raw = (request.POST.get('amount') or '').strip()
    payment_date_raw = (request.POST.get('payment_date') or '').strip()
    reference_no = (request.POST.get('reference_no') or '').strip()
    notes = (request.POST.get('notes') or '').strip()

    if not bill_id_raw.isdigit():
        raise ValueError('Select a valid credit bill.')

    amount = _parse_inventory_decimal(amount_raw, 'Enter a valid payment amount.')
    if amount <= Decimal('0.00'):
        raise ValueError('Payment amount must be greater than zero.')

    workspace = getattr(request, 'current_workspace', None)
    financial_account_id = (request.POST.get('financial_account') or '').strip()
    target_account = None
    if financial_account_id.isdigit():
        target_account = scope_to_workspace(
            FinancialAccount.objects.filter(is_active=True),
            workspace,
        ).filter(pk=int(financial_account_id)).first()

    raw_method = (request.POST.get('payment_method') or '').strip()
    if target_account:
        if target_account.account_type == FinancialAccount.ACCOUNT_TYPE_CASH:
            payment_method = InventoryCreditPayment.METHOD_CASH
        elif target_account.account_type == FinancialAccount.ACCOUNT_TYPE_UPI:
            payment_method = InventoryCreditPayment.METHOD_UPI
        elif target_account.account_type == FinancialAccount.ACCOUNT_TYPE_BANK:
            payment_method = InventoryCreditPayment.METHOD_TRANSFER
        else:
            payment_method = InventoryCreditPayment.METHOD_TRANSFER
    elif raw_method and raw_method in {choice[0] for choice in InventoryCreditPayment.METHOD_CHOICES}:
        payment_method = raw_method
        target_account = FinancialAccount.get_default_for_payment_method(workspace, payment_method)
    else:
        target_account = FinancialAccount.get_default_for_payment_method(workspace, 'cash')
        payment_method = InventoryCreditPayment.METHOD_CASH

    payment_date = timezone.localdate()
    if payment_date_raw:
        try:
            payment_date = datetime.strptime(payment_date_raw, '%Y-%m-%d').date()
        except ValueError:
            raise ValueError('Payment date is invalid.')

    with transaction.atomic():
        bill = (
            scope_to_workspace(InventoryBill.objects.select_for_update(), workspace)
            .select_related('party')
            .filter(pk=int(bill_id_raw))
            .first()
        )
        if not bill:
            raise ValueError('Selected bill was not found.')

        if bill.is_reversed:
            raise ValueError('Cannot record payment for a reversed bill.')

        direction = _inventory_credit_direction_for_entry_type(bill.entry_type)
        if not direction:
            raise ValueError('Credit payment is available only for purchase and sales bills.')

        if getattr(bill, 'job_ticket_id', None) and bill.job_ticket and bill.job_ticket.payment_status == 'paid':
            raise ValueError('This bill is already fully settled.')

        bill_total = _inventory_bill_total(bill)
        paid_total = _inventory_bill_paid_total(bill)
        vendor_credit_applied = (
            _inventory_bill_vendor_credit_applied_total(bill)
            if bill.entry_type == 'purchase'
            else Decimal('0.00')
        )
        balance_before = max(Decimal('0.00'), bill_total - paid_total - vendor_credit_applied)
        if balance_before <= Decimal('0.00'):
            raise ValueError('This bill is already fully settled.')
        if amount > balance_before:
            raise ValueError(f"Payment cannot exceed current balance Rs.{_money_text(balance_before)}.")

        balance_after = (balance_before - amount).quantize(Decimal('0.01'))
        payment = InventoryCreditPayment.objects.create(
            workspace=bill.workspace or workspace,
            party=bill.party,
            bill=bill,
            direction=direction,
            payment_date=payment_date,
            payment_method=payment_method,
            financial_account=target_account,
            amount=amount.quantize(Decimal('0.01')),
            balance_before=balance_before.quantize(Decimal('0.01')),
            balance_after=balance_after,
            reference_no=reference_no,
            notes=notes,
            created_by=request.user if request.user.is_authenticated else None,
        )

        # Deposit or withdraw into financial account
        if target_account and amount > Decimal('0.00'):
            party_name = bill.party.name if bill.party else 'Direct'
            ref_text = f" [Ref: {reference_no}]" if reference_no else ""
            if direction in {'in', InventoryCreditPayment.DIRECTION_RECEIVABLE}:
                target_account.deposit(
                    amount=amount.quantize(Decimal('0.01')),
                    description=f"Sales Bill {bill.invoice_number or bill.bill_number}: {party_name}{ref_text}",
                    reference_no=reference_no or (bill.invoice_number or bill.bill_number),
                    transaction_date=payment_date,
                    inventory_credit_payment=payment,
                    job_ticket=bill.job_ticket if getattr(bill, 'job_ticket_id', None) else None,
                    user=request.user if request.user.is_authenticated else None,
                )
            elif direction in {'out', InventoryCreditPayment.DIRECTION_PAYABLE}:
                target_account.withdraw(
                    amount=amount.quantize(Decimal('0.01')),
                    description=f"Purchase Bill {bill.invoice_number or bill.bill_number}: {party_name}{ref_text}",
                    reference_no=reference_no or (bill.invoice_number or bill.bill_number),
                    transaction_date=payment_date,
                    inventory_credit_payment=payment,
                    job_ticket=bill.job_ticket if getattr(bill, 'job_ticket_id', None) else None,
                    user=request.user if request.user.is_authenticated else None,
                )

        # Synchronize JobTicket if this bill is linked to a repair job
        if getattr(bill, 'job_ticket_id', None) and bill.job_ticket:
            job = bill.job_ticket
            new_amount_paid = (paid_total + amount).quantize(Decimal('0.01'))
            job.amount_paid = new_amount_paid
            job.payment_status = 'paid' if balance_after <= Decimal('0.00') else 'part_paid'
            job.payment_method = dict(InventoryCreditPayment.METHOD_CHOICES).get(payment_method, payment_method)
            if target_account:
                job.financial_account = target_account
            if reference_no:
                job.payment_reference = reference_no
            job.payment_date = timezone.now()
            job.save(update_fields=['amount_paid', 'payment_status', 'payment_method', 'financial_account', 'payment_reference', 'payment_date', 'updated_at'])

            JobTicketLog.objects.create(
                job_ticket=job,
                user=request.user if request.user.is_authenticated else None,
                action='PAYMENT_RECEIVED',
                details=(
                    f"Payment of Rs.{_money_text(amount)} recorded via Inventory Dashboard "
                    f"({job.payment_method}{' to ' + target_account.name if target_account else ''}). "
                    f"New balance: Rs.{_money_text(balance_after)} ({job.get_payment_status_display()})."
                ),
            )

    return {
        'payment': payment,
        'message': (
            f"{_inventory_credit_action_label(direction)} recorded for "
            f"{bill.party.name if bill.party else 'party'}: Rs.{_money_text(payment.amount)}."
        ),
    }


def _build_inventory_dashboard_metrics(workspace=None):
    month_start = timezone.localdate().replace(day=1)
    entries_scope = scope_to_workspace(InventoryEntry.objects.all(), workspace)
    products_scope = scope_to_workspace(Product.objects.all(), workspace)
    parties_scope = scope_to_workspace(InventoryParty.objects.all(), workspace)
    monthly_totals_qs = (
        entries_scope.filter(entry_date__gte=month_start)
        .values('entry_type')
        .annotate(
            total_amount=Coalesce(Sum('total_amount', output_field=DecimalField()), Decimal('0.00')),
            total_quantity=Coalesce(Sum('quantity'), 0),
        )
    )
    monthly_totals = {row['entry_type']: row for row in monthly_totals_qs}

    monthly_purchase_total = monthly_totals.get('purchase', {}).get('total_amount', Decimal('0.00'))
    monthly_purchase_return_total = monthly_totals.get('purchase_return', {}).get('total_amount', Decimal('0.00'))
    monthly_sales_total = monthly_totals.get('sale', {}).get('total_amount', Decimal('0.00'))
    monthly_sales_return_total = monthly_totals.get('sale_return', {}).get('total_amount', Decimal('0.00'))

    monthly_purchase_qty = monthly_totals.get('purchase', {}).get('total_quantity', 0)
    monthly_purchase_return_qty = monthly_totals.get('purchase_return', {}).get('total_quantity', 0)
    monthly_sales_qty = monthly_totals.get('sale', {}).get('total_quantity', 0)
    monthly_sales_return_qty = monthly_totals.get('sale_return', {}).get('total_quantity', 0)

    monthly_stock_in_qty = monthly_purchase_qty + monthly_sales_return_qty
    monthly_stock_out_qty = monthly_sales_qty + monthly_purchase_return_qty
    monthly_net_stock_qty = monthly_stock_in_qty - monthly_stock_out_qty

    monthly_stock_in_amount = monthly_purchase_total + monthly_sales_return_total
    monthly_stock_out_amount = monthly_sales_total + monthly_purchase_return_total
    monthly_net_amount = monthly_stock_in_amount - monthly_stock_out_amount

    # Physical total warehouse stock units across all catalog products
    total_in_stock_qty = sum((p.stock_quantity or 0) for p in products_scope)

    # Monthly retail sales COGS, Gross Profit & Margin %
    monthly_sale_entries = entries_scope.filter(entry_date__gte=month_start, entry_type='sale').select_related('product')
    monthly_sales_cogs = sum(
        ((e.product.cost_price or Decimal('0.00')) * Decimal(e.quantity or 0) for e in monthly_sale_entries if e.product),
        Decimal('0.00'),
    )
    monthly_sales_profit = max(Decimal('0.00'), monthly_sales_total - monthly_sales_cogs)
    monthly_sales_margin_pct = (
        (monthly_sales_profit / monthly_sales_total * Decimal('100.0')).quantize(Decimal('0.1'))
        if monthly_sales_total > Decimal('0.00')
        else Decimal('0.0')
    )

    inventory_value = Decimal('0.00')
    for product in products_scope.only('stock_quantity', 'cost_price'):
        inventory_value += Decimal(product.stock_quantity or 0) * (product.cost_price or Decimal('0.00'))

    low_stock_count = products_scope.filter(stock_quantity__lte=5).count()
    reserved_stock_products = list(
        products_scope.filter(
            reserved_stock__gt=0,
            stock_quantity__lte=F('reserved_stock'),
        ).order_by('stock_quantity', 'reserved_stock', 'name')[:8]
    )
    reserved_alert_count = products_scope.filter(
        reserved_stock__gt=0,
        stock_quantity__lte=F('reserved_stock'),
    ).count()

    recent_entries = list(
        entries_scope.select_related('party', 'product', 'created_by').order_by('-entry_date', '-id')[:10]
    )
    credit_summary = _build_inventory_credit_summary(workspace)

    return {
        'party_count': parties_scope.count(),
        'product_count': products_scope.count(),
        'low_stock_count': low_stock_count,
        'reserved_alert_count': reserved_alert_count,
        'total_in_stock_qty': total_in_stock_qty,
        'inventory_value': inventory_value.quantize(Decimal('0.01')) if inventory_value else Decimal('0.00'),
        'monthly_purchase_total': monthly_purchase_total,
        'monthly_purchase_return_total': monthly_purchase_return_total,
        'monthly_sales_total': monthly_sales_total,
        'monthly_sales_cogs': monthly_sales_cogs,
        'monthly_sales_profit': monthly_sales_profit,
        'monthly_sales_margin_pct': monthly_sales_margin_pct,
        'monthly_sales_return_total': monthly_sales_return_total,
        'monthly_purchase_qty': monthly_purchase_qty,
        'monthly_purchase_return_qty': monthly_purchase_return_qty,
        'monthly_sales_qty': monthly_sales_qty,
        'monthly_sales_return_qty': monthly_sales_return_qty,
        'monthly_stock_in_qty': monthly_stock_in_qty,
        'monthly_stock_out_qty': monthly_stock_out_qty,
        'monthly_net_stock_qty': monthly_net_stock_qty,
        'monthly_stock_in_amount': monthly_stock_in_amount,
        'monthly_stock_out_amount': monthly_stock_out_amount,
        'monthly_net_amount': monthly_net_amount,
        'reserved_stock_products': reserved_stock_products,
        'recent_entries': recent_entries,
        'credit_summary': credit_summary,
    }


def _build_cash_flow_chart_data(workspace=None, range_filter='this_month', start_date_str=None, end_date_str=None):
    today = timezone.localdate()
    range_filter = (range_filter or 'this_month').strip().lower()

    if range_filter == 'last_month':
        first_of_this = today.replace(day=1)
        end_date = first_of_this - timedelta(days=1)
        start_date = end_date.replace(day=1)
        agg_mode = 'day'
        range_label = end_date.strftime('%B %Y')
    elif range_filter == 'this_quarter':
        q = (today.month - 1) // 3
        q_start = q * 3 + 1
        q_end = q_start + 2
        start_date = date(today.year, q_start, 1)
        _, last_day = calendar.monthrange(today.year, q_end)
        end_date = date(today.year, q_end, last_day)
        agg_mode = 'day'
        range_label = f"Q{q + 1} {today.year}"
    elif range_filter == 'last_quarter':
        q = (today.month - 1) // 3
        if q == 0:
            lq_year = today.year - 1
            q_start, q_end = 10, 12
            lq_num = 4
        else:
            lq_year = today.year
            q_start = (q - 1) * 3 + 1
            q_end = q_start + 2
            lq_num = q
        start_date = date(lq_year, q_start, 1)
        _, last_day = calendar.monthrange(lq_year, q_end)
        end_date = date(lq_year, q_end, last_day)
        agg_mode = 'day'
        range_label = f"Q{lq_num} {lq_year}"
    elif range_filter == 'this_year':
        start_date = date(today.year, 1, 1)
        end_date = date(today.year, 12, 31)
        agg_mode = 'month'
        range_label = f"Year {today.year}"
    elif range_filter == 'last_year':
        start_date = date(today.year - 1, 1, 1)
        end_date = date(today.year - 1, 12, 31)
        agg_mode = 'month'
        range_label = f"Year {today.year - 1}"
    elif range_filter == 'custom':
        start_date = None
        end_date = None
        if start_date_str:
            try:
                start_date = datetime.strptime(start_date_str.strip(), '%Y-%m-%d').date()
            except (ValueError, TypeError):
                start_date = None
        if end_date_str:
            try:
                end_date = datetime.strptime(end_date_str.strip(), '%Y-%m-%d').date()
            except (ValueError, TypeError):
                end_date = None
        if not start_date or not end_date:
            start_date = today.replace(day=1)
            _, last_day = calendar.monthrange(today.year, today.month)
            end_date = today.replace(day=last_day)
        if start_date > end_date:
            start_date, end_date = end_date, start_date
        span_days = (end_date - start_date).days
        agg_mode = 'month' if span_days > 90 else 'day'
        range_label = f"{start_date.strftime('%b %d, %Y')} – {end_date.strftime('%b %d, %Y')}"
    else:
        start_date = today.replace(day=1)
        _, last_day = calendar.monthrange(today.year, today.month)
        end_date = today.replace(day=last_day)
        agg_mode = 'day'
        range_label = today.strftime('%B %Y')

    labels = []
    sales_list = []
    purchases_list = []
    sales_qty_list = []
    purchases_qty_list = []

    entries_scope = scope_to_workspace(InventoryEntry.objects.all(), workspace)

    if agg_mode == 'day':
        cur = start_date
        date_map = {}
        while cur <= end_date:
            date_map[cur] = {
                'sales': Decimal('0.00'),
                'purchases': Decimal('0.00'),
                'sales_qty': 0,
                'purchases_qty': 0,
            }
            cur += timedelta(days=1)

        entries_qs = (
            entries_scope.filter(
                entry_date__gte=start_date,
                entry_date__lte=end_date,
                entry_type__in=['sale', 'purchase'],
            )
            .values('entry_date', 'entry_type')
            .annotate(
                total_amt=Coalesce(Sum('total_amount', output_field=DecimalField()), Decimal('0.00')),
                total_qty=Coalesce(Sum('quantity'), 0),
            )
        )

        for row in entries_qs:
            d = row['entry_date']
            if d in date_map:
                if row['entry_type'] == 'sale':
                    date_map[d]['sales'] += row['total_amt']
                    date_map[d]['sales_qty'] += row['total_qty']
                elif row['entry_type'] == 'purchase':
                    date_map[d]['purchases'] += row['total_amt']
                    date_map[d]['purchases_qty'] += row['total_qty']

        for d, vals in date_map.items():
            labels.append(d.strftime('%b %d'))
            s = float(vals['sales'])
            p = float(vals['purchases'])
            sales_list.append(round(s, 2))
            purchases_list.append(round(p, 2))
            sales_qty_list.append(int(vals['sales_qty']))
            purchases_qty_list.append(int(vals['purchases_qty']))

    else:
        cur = start_date.replace(day=1)
        month_keys = []
        month_map = {}
        while cur <= end_date:
            k = (cur.year, cur.month)
            month_keys.append(k)
            month_map[k] = {
                'label': cur.strftime('%b %Y') if start_date.year != end_date.year else cur.strftime('%b'),
                'sales': Decimal('0.00'),
                'purchases': Decimal('0.00'),
                'sales_qty': 0,
                'purchases_qty': 0,
            }
            _, days_in_cur = calendar.monthrange(cur.year, cur.month)
            cur = cur + timedelta(days=days_in_cur)

        entries_qs = (
            entries_scope.filter(
                entry_date__gte=start_date,
                entry_date__lte=end_date,
                entry_type__in=['sale', 'purchase'],
            )
            .values('entry_date', 'entry_type')
            .annotate(
                total_amt=Coalesce(Sum('total_amount', output_field=DecimalField()), Decimal('0.00')),
                total_qty=Coalesce(Sum('quantity'), 0),
            )
        )

        for row in entries_qs:
            d = row['entry_date']
            k = (d.year, d.month)
            if k in month_map:
                if row['entry_type'] == 'sale':
                    month_map[k]['sales'] += row['total_amt']
                    month_map[k]['sales_qty'] += row['total_qty']
                elif row['entry_type'] == 'purchase':
                    month_map[k]['purchases'] += row['total_amt']
                    month_map[k]['purchases_qty'] += row['total_qty']

        for k in month_keys:
            vals = month_map[k]
            labels.append(vals['label'])
            s = float(vals['sales'])
            p = float(vals['purchases'])
            sales_list.append(round(s, 2))
            purchases_list.append(round(p, 2))
            sales_qty_list.append(int(vals['sales_qty']))
            purchases_qty_list.append(int(vals['purchases_qty']))

    total_sales = round(sum(sales_list), 2)
    total_purchases = round(sum(purchases_list), 2)
    total_sales_qty = sum(sales_qty_list)
    total_purchases_qty = sum(purchases_qty_list)

    return {
        'labels': labels,
        'sales': sales_list,
        'purchases': purchases_list,
        'sales_qty': sales_qty_list,
        'purchases_qty': purchases_qty_list,
        'total_sales': total_sales,
        'total_purchases': total_purchases,
        'total_sales_qty': total_sales_qty,
        'total_purchases_qty': total_purchases_qty,
        # Backward-compatibility aliases
        'inflow': sales_list,
        'outflow': purchases_list,
        'net': [round(s - p, 2) for s, p in zip(sales_list, purchases_list)],
        'total_inflow': total_sales,
        'total_outflow': total_purchases,
        'net_cash_flow': round(total_sales - total_purchases, 2),
        'range_label': range_label,
        'range_filter': range_filter,
        'start_date': start_date.strftime('%Y-%m-%d'),
        'end_date': end_date.strftime('%Y-%m-%d'),
        'aggregation': agg_mode,
    }


def _build_inventory_party_directory(query='', start_date=None, end_date=None, workspace=None):
    parties = scope_to_workspace(InventoryParty.objects.all(), workspace)

    if query:
        parties = parties.filter(
            Q(name__icontains=query)
            | Q(legal_name__icontains=query)
            | Q(contact_person__icontains=query)
            | Q(phone__icontains=query)
            | Q(gstin__icontains=query)
            | Q(city__icontains=query)
            | Q(state_code__icontains=query)
        )

    total_party_count = parties.count()
    parties = list(parties.order_by('name'))
    suppliers = list(parties)
    customers = list(parties)
    legacy_both_count = 0

    party_ids = [party.id for party in parties]
    stats_map = {}
    recent_map = {
        party_id: {
            'purchase': [],
            'purchase_return': [],
            'sale': [],
            'sale_return': [],
        }
        for party_id in party_ids
    }

    if party_ids:
        max_recent_per_type = 20 if start_date or end_date else 8
        party_entries_map = {party_id: [] for party_id in party_ids}
        recent_entries = scope_to_workspace(
            InventoryEntry.objects.filter(party_id__in=party_ids),
            workspace,
        )
        if start_date:
            recent_entries = recent_entries.filter(entry_date__gte=start_date)
        if end_date:
            recent_entries = recent_entries.filter(entry_date__lte=end_date)
        recent_entries = recent_entries.select_related(
            'bill',
            'party',
            'product',
            'job_ticket',
            'created_by',
        ).order_by('-entry_date', '-id')

        for entry in recent_entries:
            party_entries_map.setdefault(entry.party_id, []).append(entry)

        for party_id, entry_list in party_entries_map.items():
            grouped_bills = _build_inventory_bill_summaries(entry_list)
            _attach_inventory_credit_to_bill_summaries(grouped_bills)
            grouped_by_type = {
                'purchase': [],
                'purchase_return': [],
                'sale': [],
                'sale_return': [],
            }

            for bill in grouped_bills:
                grouped_by_type.setdefault(bill['entry_type'], []).append(bill)

            stats_map[party_id] = {
                'purchase_count': len(grouped_by_type['purchase']),
                'purchase_amount': sum((bill['total_amount'] for bill in grouped_by_type['purchase']), Decimal('0.00')),
                'purchase_return_count': len(grouped_by_type['purchase_return']),
                'purchase_return_amount': sum((bill['total_amount'] for bill in grouped_by_type['purchase_return']), Decimal('0.00')),
                'sale_count': len(grouped_by_type['sale']),
                'sale_amount': sum((bill['total_amount'] for bill in grouped_by_type['sale']), Decimal('0.00')),
                'sale_return_count': len(grouped_by_type['sale_return']),
                'sale_return_amount': sum((bill['total_amount'] for bill in grouped_by_type['sale_return']), Decimal('0.00')),
                'credit_payable_amount': sum((bill['credit_balance_amount'] for bill in grouped_by_type['purchase']), Decimal('0.00')),
                'credit_receivable_amount': sum((bill['credit_balance_amount'] for bill in grouped_by_type['sale']), Decimal('0.00')),
            }

            for entry_type_key in grouped_by_type:
                recent_map[party_id][entry_type_key] = grouped_by_type[entry_type_key][:max_recent_per_type]

    def attach_party_history(party_list):
        for party in party_list:
            stats = stats_map.get(party.id, {})
            party.purchase_count = stats.get('purchase_count', 0)
            party.purchase_amount = stats.get('purchase_amount', Decimal('0.00'))
            party.purchase_return_count = stats.get('purchase_return_count', 0)
            party.purchase_return_amount = stats.get('purchase_return_amount', Decimal('0.00'))
            party.sale_count = stats.get('sale_count', 0)
            party.sale_amount = stats.get('sale_amount', Decimal('0.00'))
            party.sale_return_count = stats.get('sale_return_count', 0)
            payable = stats.get('credit_payable_amount', Decimal('0.00'))
            receivable = stats.get('credit_receivable_amount', Decimal('0.00'))
            if party.opening_balance:
                if party.opening_balance < Decimal('0.00'):
                    payable += abs(party.opening_balance)
                elif party.opening_balance > Decimal('0.00'):
                    receivable += abs(party.opening_balance)
            party.credit_payable_amount = payable
            party.credit_receivable_amount = receivable

            party.purchase_history = recent_map.get(party.id, {}).get('purchase', [])
            party.purchase_return_history = recent_map.get(party.id, {}).get('purchase_return', [])
            party.sale_history = recent_map.get(party.id, {}).get('sale', [])
            party.sale_return_history = recent_map.get(party.id, {}).get('sale_return', [])
            party.combined_history = sorted(
                (
                    party.purchase_history
                    + party.purchase_return_history
                    + party.sale_history
                    + party.sale_return_history
                ),
                key=lambda item: (item['entry_date'], item['bill_key']),
                reverse=True,
            )[:16]

    attach_party_history(parties)

    return {
        'parties': parties,
        'suppliers': suppliers,
        'customers': customers,
        'legacy_both_count': legacy_both_count,
        'total_parties': total_party_count,
        'supplier_count': total_party_count,
        'customer_count': total_party_count,
    }

def _build_inventory_product_catalog(query='', workspace=None):
    products = scope_to_workspace(Product.objects.all(), workspace)

    if query:
        products = products.filter(
            Q(name__icontains=query)
            | Q(sku__icontains=query)
            | Q(category__icontains=query)
            | Q(brand__icontains=query)
            | Q(hsn_sac_code__icontains=query)
            | Q(uqc__icontains=query)
        )

    latest_purchase_entry = InventoryEntry.objects.filter(
        product=OuterRef('pk'),
        entry_type='purchase',
    ).order_by('-entry_date', '-id')
    products = products.annotate(
        latest_purchase_party=Subquery(latest_purchase_entry.values('party__name')[:1]),
        latest_purchase_invoice=Subquery(latest_purchase_entry.values('invoice_number')[:1]),
        latest_purchase_date=Subquery(latest_purchase_entry.values('entry_date')[:1]),
    )
    product_rows = list(products.order_by('name'))
    product_ids = [product.id for product in product_rows]
    opening_stock_map = {
        product.id: int(product.stock_quantity or 0)
        for product in product_rows
    }
    history_map = {
        product_id: {
            'purchase': [],
            'sale': [],
            'sale_return': [],
            'purchase_return': [],
            'combined': [],
        }
        for product_id in product_ids
    }
    max_history_per_type = 10
    max_combined_history = 20

    if product_ids:
        history_entries = (
            scope_to_workspace(InventoryEntry.objects.filter(product_id__in=product_ids), workspace)
            .select_related('party')
            .order_by('-entry_date', '-id')
        )
        for entry in history_entries:
            product_history = history_map.get(entry.product_id, {})
            if entry.product_id in opening_stock_map:
                opening_stock_map[entry.product_id] -= entry.stock_effect
            bucket = product_history.get(entry.entry_type)
            if bucket is None:
                continue
            if len(bucket) < max_history_per_type:
                bucket.append(entry)
            combined = product_history.get('combined')
            if combined is not None and len(combined) < max_combined_history:
                combined.append(entry)

    def build_opening_history_entry(product, opening_quantity):
        if opening_quantity <= 0:
            return None

        opening_date = timezone.localtime(product.created_at).date() if product.created_at else None
        total_amount = (Decimal(opening_quantity) * (product.cost_price or Decimal('0.00'))).quantize(Decimal('0.01'))
        return SimpleNamespace(
            entry_type='opening_stock',
            entry_date=opening_date,
            invoice_number='Opening Stock',
            party=SimpleNamespace(name='-'),
            quantity=opening_quantity,
            total_amount=total_amount,
            created_at=product.created_at,
            sort_id=-1,
        )

    def history_sort_key(item):
        entry_date = getattr(item, 'entry_date', None) or date.min
        created_at = getattr(item, 'created_at', None)
        created_at_sort = created_at.timestamp() if created_at else 0
        sort_id = getattr(item, 'id', None)
        if sort_id is None:
            sort_id = getattr(item, 'sort_id', 0)
        return (entry_date, created_at_sort, sort_id)

    for product in product_rows:
        history = history_map.get(product.id, {})
        product.purchase_history = history.get('purchase', [])
        product.sale_history = history.get('sale', [])
        product.sale_return_history = history.get('sale_return', [])
        product.purchase_return_history = history.get('purchase_return', [])
        combined_history = list(history.get('combined', []))
        opening_entry = build_opening_history_entry(product, opening_stock_map.get(product.id, 0))
        if opening_entry:
            combined_history.append(opening_entry)
        combined_history.sort(key=history_sort_key, reverse=True)
        if len(combined_history) > max_combined_history:
            if opening_entry and opening_entry in combined_history[max_combined_history:]:
                combined_history = combined_history[:max_combined_history - 1] + [opening_entry]
            else:
                combined_history = combined_history[:max_combined_history]
        product.combined_history = combined_history
        product.has_transaction_history = bool(product.combined_history)

    return {
        'products': product_rows,
        'query': query,
        'total_products': scope_to_workspace(Product.objects.all(), workspace).count(),
        'filtered_count': len(product_rows),
        'out_of_stock_count': scope_to_workspace(Product.objects.all(), workspace).filter(stock_quantity=0).count(),
        'reserved_alert_count': scope_to_workspace(Product.objects.all(), workspace).filter(
            reserved_stock__gt=0,
            stock_quantity__lte=F('reserved_stock'),
        ).count(),
    }

def _build_inventory_register_snapshot(
    entry_type,
    *,
    query='',
    source_bill_id=None,
    source_invoice='',
    source_party_id=None,
):
    config = INVENTORY_ENTRY_CONFIG[entry_type]
    entries = InventoryEntry.objects.filter(entry_type=entry_type).select_related(
        'bill',
        'party',
        'product',
        'created_by',
        'job_ticket',
    )

    if query:
        entries = entries.filter(
            Q(entry_number__icontains=query)
            | Q(bill__bill_number__icontains=query)
            | Q(invoice_number__icontains=query)
            | Q(job_ticket__job_code__icontains=query)
            | Q(party__name__icontains=query)
            | Q(product__name__icontains=query)
        )

    source_bill_payload = None
    source_bill_lines = []
    normalized_source_invoice = (source_invoice or '').strip()
    normalized_source_bill_id = int(source_bill_id) if source_bill_id else None
    normalized_source_party_id = int(source_party_id) if source_party_id else None
    if entry_type in {'purchase_return', 'sale_return'}:
        source_entry_type = 'purchase' if entry_type == 'purchase_return' else 'sale'
        source_entries = []

        if normalized_source_bill_id:
            source_bill_obj = (
                InventoryBill.objects.filter(pk=normalized_source_bill_id, entry_type=source_entry_type)
                .select_related('party')
                .first()
            )
            if source_bill_obj:
                normalized_source_party_id = source_bill_obj.party_id
                normalized_source_invoice = (source_bill_obj.invoice_number or '').strip()
                source_entries = list(
                    source_bill_obj.lines.select_related('bill', 'party', 'product').order_by('id')
                )
        elif normalized_source_invoice and normalized_source_party_id:
            source_entries = list(
                InventoryEntry.objects.filter(
                    entry_type=source_entry_type,
                    invoice_number=normalized_source_invoice,
                    party_id=normalized_source_party_id,
                )
                .select_related('bill', 'party', 'product')
                .order_by('id')
            )

        if source_entries:
            line_map = {}
            for src in source_entries:
                row = line_map.get(src.product_id)
                if row is None:
                    line_map[src.product_id] = {
                        'product_id': src.product_id,
                        'product_name': src.product.name,
                        'product_stock': int(src.product.stock_quantity or 0),
                        'max_quantity': int(src.quantity or 0),
                        'unit_price': src.unit_price or Decimal('0.00'),
                        'gst_rate': src.gst_rate or Decimal('0.00'),
                        'notes': src.notes or '',
                    }
                else:
                    row['max_quantity'] += int(src.quantity or 0)

            source_bill_lines = list(line_map.values())
            source_bill_payload = {
                'bill_id': source_entries[0].bill_id,
                'bill_number': source_entries[0].bill.bill_number if source_entries[0].bill_id else '',
                'invoice_number': normalized_source_invoice,
                'party_name': source_entries[0].party.name,
                'source_label': 'Purchase' if source_entry_type == 'purchase' else 'Sales',
            }

    ordered_entries = entries.order_by('-entry_date', '-id')
    register_rows = _build_inventory_bill_summaries(ordered_entries)

    return {
        'config': config,
        'query': query,
        'entry_count': len(register_rows),
        'total_quantity': entries.aggregate(total=Coalesce(Sum('quantity'), 0))['total'],
        'total_amount': entries.aggregate(
            total=Coalesce(Sum('total_amount', output_field=DecimalField()), Decimal('0.00'))
        )['total'],
        'register_rows': register_rows,
        'show_add_entry_button': entry_type not in {'purchase_return', 'sale_return'},
        'source_bill': source_bill_payload,
        'source_bill_lines': source_bill_lines,
    }

def _serialize_inventory_bill_summary_for_api(bill):
    return {
        'bill_id': bill['bill_id'] or '',
        'bill_key': bill['bill_key'],
        'entry_type': bill['entry_type'],
        'entry_type_label': bill['entry_type_label'],
        'entry_date': bill['entry_date'].isoformat() if bill['entry_date'] else '',
        'invoice_date': bill['invoice_date'].isoformat() if bill.get('invoice_date') else '',
        'invoice_display': bill['invoice_display'],
        'bill_number_display': bill['bill_number_display'],
        'item_label': bill['item_label'],
        'total_quantity': int(bill['total_quantity'] or 0),
        'total_amount': _money_text(bill['total_amount']),
        'product_summary': bill['product_summary'],
        'job_code': bill['job_code'] or '',
        'notes': bill['notes'] or '',
    }

def _serialize_inventory_party_for_api(party):
    return {
        'id': party.id,
        'name': party.name,
        'legal_name': party.legal_name or '',
        'contact_person': party.contact_person or '',
        'party_type': party.party_type,
        'phone': party.phone or '',
        'gst_registration_type': party.gst_registration_type,
        'gst_registration_label': party.get_gst_registration_type_display(),
        'gstin': party.gstin or '',
        'state_code': party.state_code or '',
        'default_place_of_supply_state': party.default_place_of_supply_state or '',
        'pan': party.pan or '',
        'email': party.email or '',
        'address': party.address or '',
        'shipping_address': party.shipping_address or '',
        'city': party.city or '',
        'state': party.state or '',
        'country': party.country or '',
        'pincode': party.pincode or '',
        'opening_balance': _money_text(party.opening_balance),
        'is_active': bool(party.is_active),
        'purchase_count': int(getattr(party, 'purchase_count', 0) or 0),
        'purchase_amount': _money_text(getattr(party, 'purchase_amount', Decimal('0.00'))),
        'purchase_return_count': int(getattr(party, 'purchase_return_count', 0) or 0),
        'purchase_return_amount': _money_text(getattr(party, 'purchase_return_amount', Decimal('0.00'))),
        'sale_count': int(getattr(party, 'sale_count', 0) or 0),
        'sale_amount': _money_text(getattr(party, 'sale_amount', Decimal('0.00'))),
        'sale_return_count': int(getattr(party, 'sale_return_count', 0) or 0),
        'sale_return_amount': _money_text(getattr(party, 'sale_return_amount', Decimal('0.00'))),
        'combined_history': [
            _serialize_inventory_bill_summary_for_api(bill)
            for bill in getattr(party, 'combined_history', [])
        ],
    }

def _inventory_party_form_payload_from_api(payload):
    gstin = (payload.get('gstin') or '').strip()
    return {
        'name': (payload.get('name') or '').strip(),
        'legal_name': (payload.get('legal_name') or '').strip(),
        'contact_person': (payload.get('contact_person') or '').strip(),
        'gst_registration_type': (payload.get('gst_registration_type') or ('registered' if gstin else 'unregistered')).strip(),
        'phone': (payload.get('phone') or '').strip(),
        'gstin': gstin,
        'state_code': (payload.get('state_code') or '').strip(),
        'default_place_of_supply_state': (payload.get('default_place_of_supply_state') or '').strip(),
        'pan': (payload.get('pan') or '').strip(),
        'email': (payload.get('email') or '').strip(),
        'address': (payload.get('address') or '').strip(),
        'shipping_address': (payload.get('shipping_address') or '').strip(),
        'city': (payload.get('city') or '').strip(),
        'state': (payload.get('state') or '').strip(),
        'country': (payload.get('country') or '').strip() or 'India',
        'pincode': (payload.get('pincode') or '').strip(),
        'opening_balance': str(payload.get('opening_balance') or '0.00'),
        'is_active': 'on' if _mobile_parse_bool(payload.get('is_active', True)) else '',
    }

def _serialize_inventory_product_history_for_api(entry):
    entry_type = getattr(entry, 'entry_type', '') or ''
    if entry_type == 'opening_stock':
        entry_type_label = 'Opening Stock'
    elif hasattr(entry, 'get_entry_type_display'):
        entry_type_label = entry.get_entry_type_display()
    else:
        entry_type_label = entry_type.replace('_', ' ').title()

    party = getattr(entry, 'party', None)
    return {
        'entry_type': entry_type,
        'entry_type_label': entry_type_label,
        'entry_date': entry.entry_date.isoformat() if getattr(entry, 'entry_date', None) else '',
        'invoice_number': getattr(entry, 'invoice_number', '') or '',
        'party_name': getattr(party, 'name', '') if party else '',
        'quantity': int(getattr(entry, 'quantity', 0) or 0),
        'total_amount': _money_text(getattr(entry, 'total_amount', Decimal('0.00'))),
    }

def _serialize_inventory_product_for_api(product):
    return {
        'id': product.id,
        'name': product.name,
        'sku': product.sku or '',
        'category': product.category or '',
        'brand': product.brand or '',
        'item_type': product.item_type,
        'item_type_label': product.get_item_type_display(),
        'hsn_sac_code': product.hsn_sac_code or '',
        'uqc': product.uqc or '',
        'tax_category': product.tax_category,
        'tax_category_label': product.get_tax_category_display(),
        'gst_rate': _money_text(product.gst_rate),
        'effective_gst_rate': _money_text(product.effective_gst_rate),
        'cess_rate': _money_text(product.cess_rate),
        'is_tax_inclusive_default': bool(product.is_tax_inclusive_default),
        'unit_price': _money_text(product.unit_price),
        'cost_price': _money_text(product.cost_price),
        'stock_quantity': int(product.stock_quantity or 0),
        'reserved_stock': int(product.reserved_stock or 0),
        'description': product.description or '',
        'is_active': bool(product.is_active),
        'updated_at': timezone.localtime(product.updated_at).strftime('%Y-%m-%d %H:%M'),
        'latest_purchase_party': getattr(product, 'latest_purchase_party', '') or '',
        'latest_purchase_invoice': getattr(product, 'latest_purchase_invoice', '') or '',
        'latest_purchase_date': (
            getattr(product, 'latest_purchase_date').isoformat()
            if getattr(product, 'latest_purchase_date', None)
            else ''
        ),
        'combined_history': [
            _serialize_inventory_product_history_for_api(entry)
            for entry in getattr(product, 'combined_history', [])
        ],
    }

def _serialize_inventory_register_bill_for_api(bill):
    entry_type = bill['entry_type']
    config = INVENTORY_ENTRY_CONFIG[entry_type]
    legacy_url = reverse(config['url_name'])
    print_url = ''
    return_url = ''

    if entry_type == 'sale' and bill['bill_id']:
        print_url = reverse('inventory_sales_print_bill_view', kwargs={'bill_id': bill['bill_id']})

    if entry_type == 'purchase':
        if bill['bill_id']:
            return_url = f"{reverse('inventory_purchase_return_dashboard')}?{urlencode({'source_bill_id': bill['bill_id']})}"
        elif bill['invoice_number'] and bill['party_id']:
            return_url = f"{reverse('inventory_purchase_return_dashboard')}?{urlencode({'source_invoice': bill['invoice_number'], 'source_party_id': bill['party_id']})}"
    elif entry_type == 'sale':
        if bill['bill_id']:
            return_url = f"{reverse('inventory_sales_return_dashboard')}?{urlencode({'source_bill_id': bill['bill_id']})}"
        elif bill['invoice_number'] and bill['party_id']:
            return_url = f"{reverse('inventory_sales_return_dashboard')}?{urlencode({'source_invoice': bill['invoice_number'], 'source_party_id': bill['party_id']})}"

    return {
        **_serialize_inventory_bill_summary_for_api(bill),
        'party_name': bill['party'].name if bill.get('party') else '',
        'created_by_name': bill['created_by'].username if bill.get('created_by') else '',
        'voucher_display': bill.get('voucher_display', ''),
        'legacy_url': legacy_url,
        'return_url': return_url,
        'print_url': print_url,
        'show_return_action': bool(return_url),
        'show_print_action': bool(print_url),
    }

def _inventory_form_errors(form):
    errors = {}
    for field_name, field_errors in form.errors.items():
        errors[field_name] = [str(error) for error in field_errors]
    return errors

def _normalize_tax_mode_price(raw_price, price_mode, gst_rate):
    price = raw_price or Decimal('0.00')
    if price_mode == 'with_tax' and gst_rate > 0:
        divisor = Decimal('100.00') + gst_rate
        if divisor > 0:
            price = (price * Decimal('100.00')) / divisor
    return price.quantize(Decimal('0.01'))

def _inventory_entry_dashboard(request, entry_type):
    denied = _staff_access_required(request, "inventory")
    if denied:
        return denied

    config = INVENTORY_ENTRY_CONFIG[entry_type]
    current_workspace = getattr(request, 'current_workspace', None)
    query = (request.GET.get('q') or '').strip()
    date_filter = (request.GET.get('date_filter') or '').strip().lower()
    start_date_raw = (request.GET.get('start_date') or '').strip()
    end_date_raw = (request.GET.get('end_date') or '').strip()
    payment_status_filter = (request.GET.get('payment_status') or 'all').strip().lower()
    if payment_status_filter not in {'all', 'credit', 'part_paid', 'paid'}:
        payment_status_filter = 'all'
    category_filter = (request.GET.get('category') or '').strip()

    if not date_filter:
        if start_date_raw or end_date_raw:
            date_filter = 'custom'
        else:
            date_filter = 'this_month'

    today = timezone.localdate()
    start_date = None
    end_date = None

    if date_filter == 'this_month':
        start_date = today.replace(day=1)
        _, last_day = calendar.monthrange(today.year, today.month)
        end_date = today.replace(day=last_day)
        start_date_raw = start_date.strftime('%Y-%m-%d')
        end_date_raw = end_date.strftime('%Y-%m-%d')
    elif date_filter == 'last_month':
        first_of_this_month = today.replace(day=1)
        last_day_of_last_month = first_of_this_month - timedelta(days=1)
        start_date = last_day_of_last_month.replace(day=1)
        end_date = last_day_of_last_month
        start_date_raw = start_date.strftime('%Y-%m-%d')
        end_date_raw = end_date.strftime('%Y-%m-%d')
    elif date_filter == 'quarterly':
        q_start_month = ((today.month - 1) // 3) * 3 + 1
        q_end_month = q_start_month + 2
        start_date = date(today.year, q_start_month, 1)
        _, last_day = calendar.monthrange(today.year, q_end_month)
        end_date = date(today.year, q_end_month, last_day)
        start_date_raw = start_date.strftime('%Y-%m-%d')
        end_date_raw = end_date.strftime('%Y-%m-%d')
    elif date_filter == 'all':
        start_date = None
        end_date = None
        start_date_raw = ''
        end_date_raw = ''
    elif date_filter == 'custom':
        if start_date_raw:
            try:
                start_date = datetime.strptime(start_date_raw, '%Y-%m-%d').date()
            except ValueError:
                start_date = None
                start_date_raw = ''
        if end_date_raw:
            try:
                end_date = datetime.strptime(end_date_raw, '%Y-%m-%d').date()
            except ValueError:
                end_date = None
                end_date_raw = ''
        if start_date and end_date and start_date > end_date:
            start_date, end_date = end_date, start_date
            start_date_raw, end_date_raw = end_date_raw, start_date_raw
    else:
        date_filter = 'this_month'
        start_date = today.replace(day=1)
        _, last_day = calendar.monthrange(today.year, today.month)
        end_date = today.replace(day=last_day)
        start_date_raw = start_date.strftime('%Y-%m-%d')
        end_date_raw = end_date.strftime('%Y-%m-%d')

    entries = scope_to_workspace(
        InventoryEntry.objects.filter(entry_type=entry_type),
        current_workspace,
    ).select_related('bill', 'party', 'product', 'created_by', 'job_ticket')

    if query:
        entries = entries.filter(
            Q(entry_number__icontains=query)
            | Q(bill__bill_number__icontains=query)
            | Q(invoice_number__icontains=query)
            | Q(job_ticket__job_code__icontains=query)
            | Q(party__name__icontains=query)
            | Q(product__name__icontains=query)
        )
    if start_date:
        entries = entries.filter(entry_date__gte=start_date)
    if end_date:
        entries = entries.filter(entry_date__lte=end_date)
    if category_filter:
        entries = entries.filter(product__category__iexact=category_filter)

    source_bill = None
    source_bill_lines = []
    auto_open_add_modal = False
    source_party_id = None
    source_invoice = ''
    source_bill_id = None
    if entry_type in {'purchase_return', 'sale_return'}:
        source_bill_id_raw = (request.GET.get('source_bill_id') or '').strip()
        source_invoice = (request.GET.get('source_invoice') or '').strip()
        source_party_raw = (request.GET.get('source_party_id') or '').strip()
        source_entry_type = 'purchase' if entry_type == 'purchase_return' else 'sale'

        source_entries = []
        if source_bill_id_raw.isdigit():
            source_bill_id = int(source_bill_id_raw)
            source_bill_obj = (
                scope_to_workspace(
                    InventoryBill.objects.filter(pk=source_bill_id, entry_type=source_entry_type),
                    current_workspace,
                )
                .select_related('party')
                .first()
            )
            if source_bill_obj:
                source_party_id = source_bill_obj.party_id
                source_invoice = (source_bill_obj.invoice_number or '').strip()
                source_entries = list(
                    source_bill_obj.lines.select_related('bill', 'party', 'product').order_by('id')
                )
        elif source_invoice and source_party_raw.isdigit():
            source_party_id = int(source_party_raw)
            source_entries = list(
                scope_to_workspace(InventoryEntry.objects.filter(
                    entry_type=source_entry_type,
                    invoice_number=source_invoice,
                    party_id=source_party_id,
                ), current_workspace)
                .select_related('bill', 'party', 'product')
                .order_by('id')
            )

        if source_entries:
            line_map = {}
            for src in source_entries:
                row = line_map.get(src.product_id)
                if row is None:
                    line_map[src.product_id] = {
                        'product_id': src.product_id,
                        'product_name': src.product.name,
                        'product_stock': src.product.stock_quantity,
                        'max_quantity': int(src.quantity or 0),
                        'unit_price': src.unit_price or Decimal('0.00'),
                        'gst_rate': src.gst_rate or Decimal('0.00'),
                        'notes': src.notes or '',
                    }
                else:
                    row['max_quantity'] += int(src.quantity or 0)

            source_bill_lines = list(line_map.values())
            source_bill = {
                'bill_id': source_entries[0].bill_id,
                'bill_number': source_entries[0].bill.bill_number if source_entries[0].bill_id else '',
                'invoice_number': source_invoice,
                'party_name': source_entries[0].party.name,
                'source_label': 'Purchase' if source_entry_type == 'purchase' else 'Sales',
            }
            auto_open_add_modal = True

    line_entry_error = None
    submitted_lines_data = []
    default_gst_rate = Decimal('18.00')
    bill_discount_value = '0.00'
    bill_notes_value = ''
    bill_payment_status_value = 'paid' if entry_type == 'sale' else 'unpaid'
    bill_payment_method_value = InventoryCreditPayment.METHOD_CASH
    bill_payment_date_value = timezone.localdate().isoformat()
    bill_payment_reference_value = ''
    bill_credit_due_date_value = ''
    if request.method == 'POST':
        if request.POST.get('inventory_entry_submit') == entry_type:
            bill_discount_value = (request.POST.get('bill_discount_amount') or '').strip() or '0.00'
            bill_notes_value = (request.POST.get('bill_notes') or '').strip()
            bill_payment_status_value = (request.POST.get('bill_payment_status') or bill_payment_status_value).strip().lower()
            bill_payment_method_value = (request.POST.get('bill_payment_method') or bill_payment_method_value).strip()
            bill_payment_date_value = (request.POST.get('bill_payment_date') or bill_payment_date_value).strip()
            bill_payment_reference_value = (request.POST.get('bill_payment_reference') or '').strip()
            bill_credit_due_date_value = (request.POST.get('bill_credit_due_date') or '').strip()
            entry_form = InventoryEntryForm(request.POST, entry_type=entry_type, workspace=current_workspace)
            if entry_form.is_valid():
                try:
                    raw_entry_date = entry_form.cleaned_data.get('entry_date')
                    invoice_date = entry_form.cleaned_data.get('invoice_date') or raw_entry_date or timezone.localdate()
                    entry_date = invoice_date if entry_type == 'purchase' else raw_entry_date
                    if not entry_date:
                        raise ValueError("Date is required.")
                    party = entry_form.cleaned_data['party']
                    invoice_number = ''
                    if entry_type != 'sale':
                        invoice_number = (entry_form.cleaned_data.get('invoice_number') or '').strip()
                    bill_notes = bill_notes_value
                    if bill_credit_due_date_value and bill_payment_status_value in {'unpaid', 'part_paid'}:
                        due_tag = f"Payment Due: {bill_credit_due_date_value}"
                        if due_tag not in bill_notes:
                            bill_notes = f"{bill_notes}\n{due_tag}".strip() if bill_notes else due_tag
                    bill_discount_amount = _parse_inventory_decimal(
                        bill_discount_value,
                        "Invalid bill discount amount.",
                    )
                    if bill_discount_amount < 0:
                        raise ValueError("Bill discount cannot be negative.")
                    if entry_type != 'sale' and not invoice_number:
                        raise ValueError("Invoice number is required.")
                    if entry_type == 'sale' and invoice_number:
                        validate_sales_invoice_number_uniqueness(invoice_number)

                    line_items = _collect_inventory_bill_lines(
                        request.POST.getlist('line_product_id[]'),
                        request.POST.getlist('line_quantity[]'),
                        request.POST.getlist('line_unit_price[]'),
                        request.POST.getlist('line_gst_rate[]'),
                        serials_list=request.POST.getlist('line_serials[]'),
                        descriptions_list=request.POST.getlist('line_description[]'),
                        workspace=current_workspace,
                    )
                    final_line_items = _apply_inventory_bill_discount(
                        line_items,
                        bill_discount_amount,
                        bill_notes,
                    )
                    initial_payment = _build_inventory_initial_payment(request, entry_type, entry_date)

                    created_entries = _record_inventory_entries(
                        request=request,
                        entry_type=entry_type,
                        entry_date=entry_date,
                        invoice_date=invoice_date,
                        party=party,
                        invoice_number=invoice_number,
                        line_items=final_line_items,
                        initial_payment=initial_payment,
                        bill_notes=bill_notes,
                    )
                    shared_bill_number = created_entries[0].invoice_number if created_entries else invoice_number
                    created_bill = created_entries[0].bill if created_entries and created_entries[0].bill_id else None
                    messages.success(
                        request,
                        (
                            f"{config['success_label']} saved with {len(created_entries)} line(s), "
                            f"invoice {shared_bill_number}."
                        ),
                    )
                    redirect_target = reverse(config['url_name'])
                    if entry_type == 'sale' and created_bill:
                        redirect_target = f"{redirect_target}?just_recorded_bill={created_bill.id}"
                    return redirect(redirect_target)
                except (ValueError, ValidationError) as exc:
                    if hasattr(exc, 'message_dict'):
                        msgs = []
                        for f, errs in exc.message_dict.items():
                            msgs.append(f"{f}: {', '.join(errs)}")
                        line_entry_error = " ; ".join(msgs)
                    elif hasattr(exc, 'messages'):
                        line_entry_error = " ; ".join(exc.messages)
                    else:
                        line_entry_error = str(exc)
                    messages.error(request, line_entry_error)
                except Exception as exc:
                    line_entry_error = f"An unexpected error occurred while saving: {str(exc)}"
                    messages.error(request, line_entry_error)
            else:
                error_list = []
                for field_name, errors in entry_form.errors.items():
                    field_label = (
                        entry_form.fields[field_name].label
                        if field_name in entry_form.fields and entry_form.fields[field_name].label
                        else field_name.replace('_', ' ').title()
                    )
                    for err in errors:
                        error_list.append(f"{field_label}: {err}")
                line_entry_error = " ; ".join(error_list) if error_list else "Please review the form."
                messages.error(request, f"{config['success_label']} was not saved. {line_entry_error}")

            auto_open_add_modal = True

            posted_pids = request.POST.getlist('line_product_id[]')
            posted_descs = request.POST.getlist('line_description[]')
            posted_qtys = request.POST.getlist('line_quantity[]')
            posted_prices = request.POST.getlist('line_unit_price[]')
            posted_gsts = request.POST.getlist('line_gst_rate[]')
            posted_serials = request.POST.getlist('line_serials[]')
            submitted_lines_data = []
            for i in range(len(posted_pids)):
                pid = (posted_pids[i] or '').strip()
                desc = posted_descs[i].strip() if i < len(posted_descs) else ''
                qty = posted_qtys[i].strip() if i < len(posted_qtys) else '1'
                price = posted_prices[i].strip() if i < len(posted_prices) else '0.00'
                gst = posted_gsts[i].strip() if i < len(posted_gsts) else '0.00'
                sn = posted_serials[i].strip() if i < len(posted_serials) else ''
                if pid or desc or price != '0.00' or sn:
                    submitted_lines_data.append({
                        'product_id': pid,
                        'description': desc,
                        'quantity': qty,
                        'unit_price': price,
                        'gst_rate': gst,
                        'serials': sn,
                    })
        elif request.POST.get('inventory_entry_edit_submit') == entry_type:
            try:
                result = _process_inventory_grouped_bill_edit(request, entry_type=entry_type)
                messages.success(request, result['message'])
            except ValueError as exc:
                messages.error(request, str(exc))
            return redirect(config['url_name'])

            entry_id = request.POST.get('entry_id')
            entry = scope_to_workspace(
                InventoryEntry.objects.filter(pk=entry_id, entry_type=entry_type),
                current_workspace,
            ).first()
            if not entry:
                messages.error(request, 'Selected entry was not found.')
                return redirect(config['url_name'])

            if entry_type == 'sale':
                edit_entry_date_raw = (request.POST.get('edit_entry_date') or '').strip()
                requested_bill_id = (request.POST.get('bill_id') or '').strip()
                new_invoice_number = (request.POST.get('edit_invoice_number') or '').strip()
                edit_party_id = (request.POST.get('edit_party_id') or '').strip()
                bill_notes = (request.POST.get('edit_bill_notes') or '').strip()
                bill_discount_value = (request.POST.get('edit_bill_discount_amount') or '').strip() or '0.00'
                scope_invoice = (entry.invoice_number or '').strip()
                scope_bill_id = entry.bill_id
                if requested_bill_id.isdigit():
                    scope_bill_id = int(requested_bill_id)

                if not edit_entry_date_raw:
                    messages.error(request, 'Entry date is required.')
                    return redirect(config['url_name'])
                try:
                    new_entry_date = datetime.strptime(edit_entry_date_raw, '%Y-%m-%d').date()
                except ValueError:
                    messages.error(request, 'Entry date is invalid.')
                    return redirect(config['url_name'])

                if not new_invoice_number:
                    messages.error(request, 'Invoice number is required.')
                    return redirect(config['url_name'])

                scope_qs = scope_to_workspace(InventoryEntry.objects.filter(entry_type='sale'), current_workspace)
                if scope_bill_id:
                    scope_qs = scope_qs.filter(bill_id=scope_bill_id)
                elif scope_invoice:
                    scope_qs = scope_qs.filter(invoice_number__iexact=scope_invoice)
                else:
                    scope_qs = scope_qs.filter(pk=entry.pk)
                scoped_entries = list(scope_qs.select_related('bill', 'party', 'job_ticket').order_by('id'))
                if not scoped_entries:
                    messages.error(request, 'Selected sales bill was not found.')
                    return redirect(config['url_name'])

                linked_job_ids = {row.job_ticket_id for row in scoped_entries if row.job_ticket_id}
                if len(linked_job_ids) > 1:
                    messages.error(request, 'Cannot edit a sales bill linked to multiple jobs.')
                    return redirect(config['url_name'])
                if linked_job_ids:
                    messages.error(
                        request,
                        'Service-linked sales bills must be edited from the job billing screen.',
                    )
                    return redirect(config['url_name'])

                existing_bill = scoped_entries[0].bill if scoped_entries[0].bill_id else None
                new_party = scope_to_workspace(InventoryParty.objects.filter(
                    is_active=True,
                    pk=edit_party_id,
                ), current_workspace).first()
                if not new_party:
                    messages.error(request, "Please select a valid party.")
                    return redirect(config['url_name'])

                def parse_decimal(raw_value, label):
                    text = (raw_value or '').strip()
                    if text == '':
                        return Decimal('0.00')
                    try:
                        return Decimal(text)
                    except (InvalidOperation, TypeError, ValueError):
                        raise ValueError(label)

                try:
                    bill_discount_amount = parse_decimal(
                        bill_discount_value,
                        "Invalid bill discount amount.",
                    )
                    if bill_discount_amount < 0:
                        raise ValueError("Bill discount cannot be negative.")

                    product_ids = request.POST.getlist('edit_line_product_id[]')
                    quantities = request.POST.getlist('edit_line_quantity[]')
                    unit_prices = request.POST.getlist('edit_line_unit_price[]')
                    gst_rates = request.POST.getlist('edit_line_gst_rate[]')

                    max_lines = max(
                        len(product_ids),
                        len(quantities),
                        len(unit_prices),
                        len(gst_rates),
                    )

                    def get_row_value(values, idx):
                        if idx < len(values):
                            return (values[idx] or '').strip()
                        return ''

                    line_items = []
                    seen_product_ids = set()
                    for idx in range(max_lines):
                        product_id = get_row_value(product_ids, idx)
                        qty_raw = get_row_value(quantities, idx)
                        unit_price_raw = get_row_value(unit_prices, idx)
                        gst_raw = get_row_value(gst_rates, idx)

                        if not any([product_id, qty_raw, unit_price_raw, gst_raw]):
                            continue

                        line_no = idx + 1
                        if not product_id:
                            raise ValueError(f"Line {line_no}: select a product.")

                        try:
                            quantity = int(qty_raw or '0')
                        except (TypeError, ValueError):
                            raise ValueError(f"Line {line_no}: quantity must be a whole number.")
                        if quantity <= 0:
                            raise ValueError(f"Line {line_no}: quantity must be greater than zero.")

                        if unit_price_raw:
                            validate_rate_no_special_chars(unit_price_raw, f"Line {line_no}")
                        unit_price = parse_decimal(unit_price_raw, f"Line {line_no}: invalid unit price.")
                        if unit_price < 0:
                            raise ValueError(f"Line {line_no}: unit price cannot be negative.")

                        gst_rate = parse_decimal(gst_raw, f"Line {line_no}: invalid GST rate.")
                        if gst_rate < 0:
                            raise ValueError(f"Line {line_no}: GST rate cannot be negative.")

                        product = scope_to_workspace(
                            Product.objects.filter(pk=product_id, is_active=True),
                            current_workspace,
                        ).first()
                        if not product:
                            raise ValueError(f"Line {line_no}: selected product was not found.")
                        if product.id in seen_product_ids:
                            raise ValueError(
                                f"Line {line_no}: duplicate product '{product.name}' is not allowed in the same bill."
                            )
                        seen_product_ids.add(product.id)

                        line_amount = Decimal(quantity) * unit_price
                        line_items.append(
                            {
                                'product': product,
                                'quantity': quantity,
                                'unit_price': unit_price,
                                'line_amount': line_amount,
                                'gst_rate': gst_rate,
                            }
                        )

                    if not line_items:
                        raise ValueError("Add at least one product line.")

                    subtotal_amount = sum((line['line_amount'] for line in line_items), Decimal('0.00'))
                    if bill_discount_amount > subtotal_amount:
                        raise ValueError("Bill discount cannot exceed subtotal amount.")

                    line_discounts = [Decimal('0.00') for _ in line_items]
                    if bill_discount_amount > 0 and subtotal_amount > 0:
                        for idx, line in enumerate(line_items):
                            provisional = (
                                (bill_discount_amount * line['line_amount']) / subtotal_amount
                            ).quantize(Decimal('0.01'))
                            if provisional > line['line_amount']:
                                provisional = line['line_amount']
                            line_discounts[idx] = provisional

                        allocated_discount = sum(line_discounts, Decimal('0.00'))
                        remaining_discount = (bill_discount_amount - allocated_discount).quantize(Decimal('0.01'))
                        step = Decimal('0.01')
                        reorder_indexes = sorted(
                            range(len(line_items)),
                            key=lambda row_idx: line_items[row_idx]['line_amount'],
                            reverse=True,
                        )
                        while remaining_discount != Decimal('0.00'):
                            changed = False
                            for row_idx in reorder_indexes:
                                if remaining_discount > 0:
                                    capacity = line_items[row_idx]['line_amount'] - line_discounts[row_idx]
                                    if capacity >= step:
                                        line_discounts[row_idx] += step
                                        remaining_discount -= step
                                        changed = True
                                else:
                                    if line_discounts[row_idx] >= step:
                                        line_discounts[row_idx] -= step
                                        remaining_discount += step
                                        changed = True
                                remaining_discount = remaining_discount.quantize(Decimal('0.01'))
                                if remaining_discount == Decimal('0.00'):
                                    break
                            if not changed:
                                break

                    final_line_items = []
                    for idx, line in enumerate(line_items):
                        final_line_items.append(
                            {
                                'product': line['product'],
                                'quantity': line['quantity'],
                                'unit_price': line['unit_price'],
                                'discount_amount': line_discounts[idx],
                                'gst_rate': line['gst_rate'],
                                'notes': bill_notes,
                            }
                        )
                except ValueError as exc:
                    messages.error(request, str(exc))
                    return redirect(config['url_name'])

                try:
                    with transaction.atomic():
                        locked_scope_qs = scope_to_workspace(
                            InventoryEntry.objects.select_for_update().filter(entry_type='sale'),
                            current_workspace,
                        )
                        if scope_bill_id:
                            locked_scope_qs = locked_scope_qs.filter(bill_id=scope_bill_id)
                        elif scope_invoice:
                            locked_scope_qs = locked_scope_qs.filter(invoice_number__iexact=scope_invoice)
                        else:
                            locked_scope_qs = locked_scope_qs.filter(pk=entry.pk)
                        locked_entries = list(locked_scope_qs.select_related('bill', 'party', 'job_ticket').order_by('id'))
                        if not locked_entries:
                            raise ValueError('Selected sales bill was not found.')

                        locked_bill = locked_entries[0].bill if locked_entries[0].bill_id else existing_bill
                        validate_sales_invoice_number_uniqueness(
                            new_invoice_number,
                            exclude_inventory_bill_id=locked_bill.id if locked_bill else None,
                        )

                        locked_products = {
                            product.id: product
                            for product in scope_to_workspace(Product.objects.select_for_update().filter(
                                pk__in={row.product_id for row in locked_entries}
                            ), current_workspace)
                        }
                        for old_entry in locked_entries:
                            product = locked_products.get(old_entry.product_id)
                            if not product:
                                raise ValueError(
                                    f"Product for voucher '{old_entry.entry_number}' was not found."
                                )
                            restored_stock = product.stock_quantity - old_entry.stock_effect
                            if restored_stock < 0:
                                raise ValueError(
                                    (
                                        f"Cannot edit bill '{scope_invoice or old_entry.entry_number}'. "
                                        f"Stock reconciliation failed for '{product.name}'."
                                    )
                                )
                            product.stock_quantity = restored_stock
                        for product in locked_products.values():
                            product.save(update_fields=['stock_quantity'])

                        InventoryEntry.objects.filter(pk__in=[row.id for row in locked_entries]).delete()
                        created_entries = _record_inventory_entries(
                            request=request,
                            entry_type='sale',
                            entry_date=new_entry_date,
                            party=new_party,
                            invoice_number=new_invoice_number,
                            line_items=final_line_items,
                            existing_bill=locked_bill,
                        )

                    saved_invoice = created_entries[0].invoice_number if created_entries else new_invoice_number
                    messages.success(
                        request,
                        f"Sales bill {saved_invoice} updated with {len(created_entries)} line(s).",
                    )
                except ValueError as exc:
                    messages.error(request, str(exc))
                return redirect(config['url_name'])

            new_invoice_number = (request.POST.get('edit_invoice_number') or '').strip()
            new_notes = (request.POST.get('edit_notes') or '').strip()
            edit_party_id = request.POST.get('edit_party_id')
            is_service_linked_sale = entry_type == 'sale' and bool(entry.job_ticket_id)
            new_unit_price = None
            new_quantity = None
            new_gst_rate = None
            new_product = None

            if entry_type in {'purchase', 'purchase_return'}:
                valid_party_qs = scope_to_workspace(InventoryParty.objects.filter(is_active=True), current_workspace)
                product_id_raw = (request.POST.get('edit_product_id') or '').strip()
                quantity_raw = (request.POST.get('edit_quantity') or '').strip()
                unit_price_raw = (request.POST.get('edit_unit_price') or '').strip()
                gst_rate_raw = (request.POST.get('edit_gst_rate') or '').strip()

                if not product_id_raw:
                    messages.error(request, 'Product is required.')
                    return redirect(config['url_name'])
                new_product = scope_to_workspace(
                    Product.objects.filter(pk=product_id_raw, is_active=True),
                    current_workspace,
                ).first()
                if not new_product:
                    messages.error(request, 'Selected product was not found.')
                    return redirect(config['url_name'])

                try:
                    new_quantity = int(quantity_raw or '0')
                except (TypeError, ValueError):
                    messages.error(request, 'Quantity must be a whole number.')
                    return redirect(config['url_name'])
                if new_quantity <= 0:
                    messages.error(request, 'Quantity must be greater than zero.')
                    return redirect(config['url_name'])

                if unit_price_raw == '':
                    messages.error(request, 'Rate is required.')
                    return redirect(config['url_name'])
                if not RATE_CLEAN_REGEX.match(unit_price_raw):
                    messages.error(request, 'Special characters are not allowed in rate.')
                    return redirect(config['url_name'])
                try:
                    new_unit_price = Decimal(unit_price_raw)
                except (InvalidOperation, TypeError, ValueError):
                    messages.error(request, 'Enter a valid rate.')
                    return redirect(config['url_name'])
                if new_unit_price < 0:
                    messages.error(request, 'Rate cannot be negative.')
                    return redirect(config['url_name'])

                if gst_rate_raw == '':
                    messages.error(request, 'GST rate is required.')
                    return redirect(config['url_name'])
                try:
                    new_gst_rate = Decimal(gst_rate_raw)
                except (InvalidOperation, TypeError, ValueError):
                    messages.error(request, 'Enter a valid GST rate.')
                    return redirect(config['url_name'])
                if new_gst_rate < 0:
                    messages.error(request, 'GST rate cannot be negative.')
                    return redirect(config['url_name'])
            else:
                valid_party_qs = scope_to_workspace(InventoryParty.objects.filter(is_active=True), current_workspace)
            if is_service_linked_sale:
                new_party = entry.party
            else:
                new_party = valid_party_qs.filter(pk=edit_party_id).first()

            if not new_invoice_number:
                messages.error(request, 'Invoice number is required.')
                return redirect(config['url_name'])
            if entry_type == 'sale' and (entry.invoice_number or '') != new_invoice_number:
                try:
                    validate_sales_invoice_number_uniqueness(
                        new_invoice_number,
                        exclude_inventory_entry_id=entry.pk,
                    )
                except ValueError as exc:
                    messages.error(request, str(exc))
                    return redirect(config['url_name'])
            if is_service_linked_sale and edit_party_id and str(edit_party_id) != str(entry.party_id):
                messages.error(
                    request,
                    'Party is fixed for service-linked sales entries and cannot be changed.',
                )
                return redirect(config['url_name'])
            if not new_party:
                messages.error(request, f"Please select a valid {config['party_label_singular'].lower()}.")
                return redirect(config['url_name'])

            original_product_id = entry.product_id
            original_quantity = entry.quantity
            original_stock_effect = entry.stock_effect

            update_fields = []
            shared_bill_update = {
                'invoice_number': new_invoice_number,
                'party_id': new_party.id,
                'notes': new_notes,
            }
            if (entry.invoice_number or '') != new_invoice_number:
                entry.invoice_number = new_invoice_number
                update_fields.append('invoice_number')
            if entry.party_id != new_party.id:
                entry.party = new_party
                update_fields.append('party')
            if (entry.notes or '') != new_notes:
                entry.notes = new_notes
                update_fields.append('notes')
            if new_product is not None and entry.product_id != new_product.id:
                entry.product = new_product
                update_fields.append('product')
            if new_quantity is not None and entry.quantity != new_quantity:
                entry.quantity = new_quantity
                update_fields.append('quantity')
            if new_unit_price is not None:
                line_amount = Decimal(entry.quantity) * new_unit_price
                if (entry.discount_amount or Decimal('0.00')) > line_amount:
                    messages.error(
                        request,
                        (
                            f"Rate is too low for voucher {entry.entry_number}. "
                            "Existing discount exceeds line amount."
                        ),
                    )
                    return redirect(config['url_name'])
                taxable_amount = line_amount - (entry.discount_amount or Decimal('0.00'))
                effective_gst_rate = new_gst_rate if new_gst_rate is not None else (entry.gst_rate or Decimal('0.00'))
                gst_amount = (taxable_amount * effective_gst_rate) / Decimal('100')
                total_amount = taxable_amount + gst_amount
                taxable_amount = taxable_amount.quantize(Decimal('0.01'))
                gst_amount = gst_amount.quantize(Decimal('0.01'))
                total_amount = total_amount.quantize(Decimal('0.01'))

                if entry.unit_price != new_unit_price:
                    entry.unit_price = new_unit_price
                    update_fields.append('unit_price')
                if new_gst_rate is not None and entry.gst_rate != new_gst_rate:
                    entry.gst_rate = new_gst_rate
                    update_fields.append('gst_rate')
                if entry.taxable_amount != taxable_amount:
                    entry.taxable_amount = taxable_amount
                    update_fields.append('taxable_amount')
                if entry.gst_amount != gst_amount:
                    entry.gst_amount = gst_amount
                    update_fields.append('gst_amount')
                if entry.total_amount != total_amount:
                    entry.total_amount = total_amount
                    update_fields.append('total_amount')

            if update_fields:
                with transaction.atomic():
                    locked_bill = None
                    if entry.bill_id:
                        locked_bill = InventoryBill.objects.select_for_update().filter(pk=entry.bill_id).first()

                    if entry_type in {'purchase', 'purchase_return'} and ('product' in update_fields or 'quantity' in update_fields):
                        original_product = Product.objects.select_for_update().filter(pk=original_product_id).first()
                        if not original_product:
                            messages.error(request, 'Original product for this voucher is missing.')
                            return redirect(config['url_name'])

                        updated_stock_effect = entry.stock_effect
                        if entry.product_id == original_product_id:
                            adjusted_stock = original_product.stock_quantity + (updated_stock_effect - original_stock_effect)
                            if adjusted_stock < 0:
                                messages.error(
                                    request,
                                    f"Cannot update voucher {entry.entry_number}. Stock would become negative for '{original_product.name}'.",
                                )
                                return redirect(config['url_name'])

                            original_product.stock_quantity = adjusted_stock
                            original_product.save(update_fields=['stock_quantity'])
                            stock_before = adjusted_stock - updated_stock_effect
                            stock_after = adjusted_stock
                        else:
                            updated_product = Product.objects.select_for_update().filter(pk=entry.product_id).first()
                            if not updated_product:
                                messages.error(request, 'Selected product was not found.')
                                return redirect(config['url_name'])

                            reverted_stock = original_product.stock_quantity - original_stock_effect
                            if reverted_stock < 0:
                                messages.error(
                                    request,
                                    (
                                        f"Cannot switch product for voucher {entry.entry_number}. "
                                        f"Current stock of '{original_product.name}' is too low to reverse old quantity ({original_quantity})."
                                    ),
                                )
                                return redirect(config['url_name'])

                            applied_stock = updated_product.stock_quantity + updated_stock_effect
                            if applied_stock < 0:
                                messages.error(
                                    request,
                                    (
                                        f"Cannot update voucher {entry.entry_number}. "
                                        f"Only {updated_product.stock_quantity} unit(s) available for '{updated_product.name}'."
                                    ),
                                )
                                return redirect(config['url_name'])

                            original_product.stock_quantity = reverted_stock
                            original_product.save(update_fields=['stock_quantity'])
                            updated_product.stock_quantity = applied_stock
                            updated_product.save(update_fields=['stock_quantity'])
                            stock_before = applied_stock - updated_stock_effect
                            stock_after = applied_stock

                        entry.stock_before = stock_before
                        entry.stock_after = stock_after
                        for field_name in ['stock_before', 'stock_after']:
                            if field_name not in update_fields:
                                update_fields.append(field_name)

                    entry.save(update_fields=update_fields)
                    if locked_bill:
                        bill_update_fields = []
                        if (locked_bill.invoice_number or '') != new_invoice_number:
                            locked_bill.invoice_number = new_invoice_number
                            bill_update_fields.append('invoice_number')
                        if locked_bill.party_id != new_party.id:
                            locked_bill.party = new_party
                            bill_update_fields.append('party')
                        if (locked_bill.notes or '') != new_notes:
                            locked_bill.notes = new_notes
                            bill_update_fields.append('notes')
                        if bill_update_fields:
                            locked_bill.save(update_fields=bill_update_fields + ['updated_at'])
                        InventoryEntry.objects.filter(bill_id=locked_bill.id).exclude(pk=entry.pk).update(
                            invoice_number=shared_bill_update['invoice_number'],
                            party_id=shared_bill_update['party_id'],
                            notes=shared_bill_update['notes'],
                        )
                    if (
                        entry_type in {'purchase', 'purchase_return'}
                        and new_unit_price is not None
                        and ('unit_price' in update_fields or 'product' in update_fields)
                    ):
                        product = Product.objects.select_for_update().filter(pk=entry.product_id).first()
                        if product and product.cost_price != new_unit_price:
                            product.cost_price = new_unit_price
                            product.save(update_fields=['cost_price'])
                messages.success(request, f"{config['success_label']} voucher {entry.entry_number} updated successfully.")
            else:
                messages.info(request, f"No changes detected for voucher {entry.entry_number}.")
            return redirect(config['url_name'])

    try:
        default_gst_rate = CompanyProfile.get_profile(current_workspace).gst_rate
    except Exception:
        default_gst_rate = Decimal('18.00')

    if request.method == 'POST' and request.POST.get('inventory_entry_submit') == entry_type:
        # entry_form was already instantiated and validated above; keep it.
        pass
    else:
        initial_data = {
            'entry_date': timezone.localdate(),
        }
        if entry_type in {'purchase_return', 'sale_return'} and source_party_id:
            initial_data['party'] = source_party_id
        if entry_type in {'purchase_return', 'sale_return'} and source_bill:
            initial_data['invoice_number'] = source_bill['invoice_number']

        entry_form = InventoryEntryForm(
            entry_type=entry_type,
            initial=initial_data,
            workspace=current_workspace,
        )

    sale_invoice_preview = ''
    if entry_type == 'sale':
        preview_entry_date = timezone.localdate()
        raw_preview_date = ''

        if request.method == 'POST' and request.POST.get('inventory_entry_submit') == entry_type:
            raw_preview_date = (request.POST.get('entry_date') or '').strip()
        else:
            initial_entry_date = entry_form.initial.get('entry_date')
            if isinstance(initial_entry_date, date):
                preview_entry_date = initial_entry_date

        if raw_preview_date:
            try:
                preview_entry_date = datetime.strptime(raw_preview_date, '%Y-%m-%d').date()
            except ValueError:
                preview_entry_date = timezone.localdate()

        sale_invoice_preview = _generate_inventory_invoice_number('sale', preview_entry_date)

    edit_party_options = scope_to_workspace(
        InventoryParty.objects.filter(is_active=True),
        current_workspace,
    ).order_by('name')
    line_products = scope_to_workspace(
        Product.objects.filter(is_active=True),
        current_workspace,
    ).order_by('name')

    ordered_entries = entries.order_by('-entry_date', '-id')
    register_is_grouped = True
    register_rows = _build_inventory_bill_summaries(ordered_entries)
    _attach_inventory_credit_to_bill_summaries(register_rows)

    # Settlement distribution metrics across all bills in current query/date range
    total_bills_all_statuses = len(register_rows)
    paid_bills_count = sum(1 for bill in register_rows if (bill.get('credit_balance_amount') or Decimal('0.00')) <= Decimal('0.00'))
    part_paid_bills_count = sum(
        1 for bill in register_rows
        if (bill.get('credit_balance_amount') or Decimal('0.00')) > Decimal('0.00')
        and (bill.get('credit_paid_amount') or Decimal('0.00')) > Decimal('0.00')
    )
    credit_bills_count = sum(
        1 for bill in register_rows
        if (bill.get('credit_balance_amount') or Decimal('0.00')) > Decimal('0.00')
        and (bill.get('credit_paid_amount') or Decimal('0.00')) <= Decimal('0.00')
    )

    if entry_type in {'purchase', 'sale'} and payment_status_filter != 'all':
        def bill_matches_payment_filter(bill):
            paid_amount = bill.get('credit_paid_amount') or Decimal('0.00')
            balance_amount = bill.get('credit_balance_amount') or Decimal('0.00')
            if payment_status_filter == 'credit':
                return balance_amount > Decimal('0.00') and paid_amount <= Decimal('0.00')
            if payment_status_filter == 'part_paid':
                return balance_amount > Decimal('0.00') and paid_amount > Decimal('0.00')
            if payment_status_filter == 'paid':
                return balance_amount <= Decimal('0.00')
            return True

        register_rows = [bill for bill in register_rows if bill_matches_payment_filter(bill)]

    if entry_type == 'sale':
        register_empty_colspan = 11
    elif entry_type == 'purchase':
        register_empty_colspan = 10
    else:
        register_empty_colspan = 8
    register_total_quantity = sum((int(bill.get('total_quantity') or 0) for bill in register_rows), 0)
    register_total_amount = sum((bill.get('total_amount') or Decimal('0.00') for bill in register_rows), Decimal('0.00'))
    register_paid_total = sum((bill.get('credit_paid_amount') or Decimal('0.00') for bill in register_rows), Decimal('0.00'))
    register_balance_total = sum((bill.get('credit_balance_amount') or Decimal('0.00') for bill in register_rows), Decimal('0.00'))

    paid_pct = (
        (register_paid_total / register_total_amount * Decimal('100.0')).quantize(Decimal('0.1'))
        if register_total_amount > Decimal('0.00')
        else Decimal('0.0')
    )
    balance_pct = (
        (register_balance_total / register_total_amount * Decimal('100.0')).quantize(Decimal('0.1'))
        if register_total_amount > Decimal('0.00')
        else Decimal('0.0')
    )
    bill_amount_paid_value = (request.POST.get('bill_amount_paid') or '').strip()

    def money_text(amount):
        return format((amount or Decimal('0.00')).quantize(Decimal('0.01')), 'f')

    inventory_bill_payload = {}
    for bill in register_rows:
        payload_key = str(bill['bill_id'] or bill['bill_key'])
        lines = sorted(bill['lines'], key=lambda row: row.id)
        inventory_bill_payload[payload_key] = {
            'bill_id': bill['bill_id'] or '',
            'bill_number': bill['bill_number'],
            'invoice_number': bill['invoice_number'],
            'entry_date': bill['entry_date'].isoformat() if bill['entry_date'] else '',
            'invoice_date': bill['invoice_date'].isoformat() if bill.get('invoice_date') else '',
            'party_id': bill['party_id'],
            'party_name': bill['party'].name,
            'job_code': bill['job_code'] or '',
            'party_locked': bool(bill['job_ticket']),
            'bill_notes': bill['notes'] or '',
            'bill_discount_amount': money_text(
                sum((row.discount_amount or Decimal('0.00') for row in lines), Decimal('0.00'))
            ),
            'lines': [
                {
                    'entry_id': row.id,
                    'entry_number': row.entry_number,
                    'product_id': row.product_id,
                    'quantity': int(row.quantity or 0),
                    'unit_price': money_text(row.unit_price),
                    'gst_rate': money_text(row.gst_rate),
                    'taxable_amount': money_text(row.taxable_amount),
                    'gst_amount': money_text(row.gst_amount),
                    'total_amount': money_text(row.total_amount),
                    'description': row.notes or '',
                }
                for row in lines
            ],
        }

    walkin_customer_id = ''
    walkin_customer_name = 'Walk-in Customer'
    if entry_type == 'sale':
        walkin_customer = scope_to_workspace(
            InventoryParty.objects.filter(name__iexact='Walk-in Customer', is_active=True),
            current_workspace,
        ).first()
        if not walkin_customer:
            try:
                walkin_customer = InventoryParty.objects.create(
                    workspace=current_workspace,
                    name='Walk-in Customer',
                    party_type='customer',
                    is_active=True,
                )
            except Exception:
                pass
        if walkin_customer:
            walkin_customer_id = str(walkin_customer.id)
            walkin_customer_name = walkin_customer.name

    party_balances_map = {}
    if entry_type in {'purchase', 'sale'}:
        credit_bills = _build_inventory_credit_rows(entry_type, workspace=current_workspace)
        for cbill in credit_bills:
            p_id = str(cbill.party_id)
            party_balances_map[p_id] = round(
                float(Decimal(str(party_balances_map.get(p_id, 0.0))) + cbill.balance_amount),
                2,
            )
        # Also include active parties with opening balances if not already covered
        active_parties_qs = InventoryParty.objects.filter(is_active=True)
        if current_workspace:
            active_parties_qs = scope_to_workspace(active_parties_qs, current_workspace)
        for party in active_parties_qs:
            if party.opening_balance and party.opening_balance > Decimal('0.00'):
                p_id = str(party.id)
                party_balances_map[p_id] = round(
                    float(Decimal(str(party_balances_map.get(p_id, 0.0))) + party.opening_balance),
                    2,
                )
    party_balances_json = json.dumps(party_balances_map)

    products_catalog_map = {}
    for p in line_products:
        products_catalog_map[str(p.id)] = {
            'id': p.id,
            'name': p.name,
            'sku': p.sku or '',
            'cost_price': float(p.cost_price or Decimal('0.00')),
            'unit_price': float(p.unit_price or Decimal('0.00')),
            'stock_quantity': p.stock_quantity,
            'reserved_stock': p.reserved_stock,
            'gst_rate': float(p.effective_gst_rate or Decimal('18.00')),
            'category': p.category or '',
            'brand': p.brand or '',
            'description': p.description or '',
            'bin_location': p.bin_location or '',
            'has_serial_tracking': bool(p.has_serial_tracking),
            'vendor_warranty_months': p.vendor_warranty_months or 0,
            'customer_warranty_months': p.customer_warranty_months or 0,
            'label': f"{p.name} (Stock: {p.stock_quantity})",
        }
    products_catalog_json = json.dumps(products_catalog_map)

    in_stock_serials = ProductItemSerial.objects.filter(
        workspace=current_workspace,
        status=ProductItemSerial.STATUS_IN_STOCK,
    ).values('product_id', 'serial_number')
    available_serials_map = {}
    for s in in_stock_serials:
        pid_str = str(s['product_id'])
        if pid_str not in available_serials_map:
            available_serials_map[pid_str] = []
        available_serials_map[pid_str].append(s['serial_number'])
    available_serials_json = json.dumps(available_serials_map)

    just_recorded_bill_data = None
    just_bill_id = request.GET.get('just_recorded_bill')
    if just_bill_id and entry_type == 'sale':
        try:
            j_bill = scope_to_workspace(
                InventoryBill.objects.filter(pk=int(just_bill_id), entry_type='sale'),
                current_workspace,
            ).select_related('party', 'workspace').first()
            if j_bill:
                b_total = _inventory_bill_total(j_bill)
                b_paid = _inventory_bill_paid_total(j_bill)
                b_balance = max(b_total - b_paid, Decimal('0.00'))
                print_url = reverse('inventory_sales_print_bill_view', kwargs={'bill_id': j_bill.id})

                party_phone = (j_bill.party.phone if j_bill.party else '').strip()
                clean_digits = re.sub(r'\D', '', party_phone)
                if clean_digits.startswith('91') and len(clean_digits) == 12:
                    phone_for_wa = clean_digits
                elif len(clean_digits) == 10:
                    phone_for_wa = f"91{clean_digits}"
                else:
                    phone_for_wa = clean_digits if len(clean_digits) >= 10 else ''

                shop_name = getattr(current_workspace, 'name', 'Our Store') if current_workspace else 'Our Store'
                wa_msg = (
                    f"Hello {j_bill.party.name if j_bill.party else 'Customer'},\n"
                    f"Thank you for your purchase from {shop_name}!\n\n"
                    f"Invoice No: {j_bill.invoice_number or j_bill.bill_number}\n"
                    f"Date: {j_bill.entry_date}\n"
                    f"Total: Rs. {b_total:.2f}\n"
                    f"Paid: Rs. {b_paid:.2f}\n"
                )
                if b_balance > Decimal('0.00'):
                    wa_msg += f"Balance Due: Rs. {b_balance:.2f}\n"
                wa_msg += "\nThank you for choosing us!"

                if phone_for_wa:
                    wa_url = f"https://wa.me/{phone_for_wa}?text={quote(wa_msg)}"
                else:
                    wa_url = f"https://wa.me/?text={quote(wa_msg)}"

                just_recorded_bill_data = {
                    'id': j_bill.id,
                    'invoice_number': j_bill.invoice_number or j_bill.bill_number,
                    'party_name': j_bill.party.name if j_bill.party else 'Walk-in Customer',
                    'party_phone': party_phone,
                    'total_amount': b_total,
                    'paid_amount': b_paid,
                    'balance_amount': b_balance,
                    'print_url': print_url,
                    'whatsapp_url': wa_url,
                }
        except (ValueError, TypeError):
            pass

    context = {
        'config': config,
        'entry_type': entry_type,
        'entry_form': entry_form,
        'edit_party_options': edit_party_options,
        'line_products': line_products,
        'default_gst_rate': default_gst_rate,
        'line_entry_error': line_entry_error,
        'entries': ordered_entries,
        'register_rows': register_rows,
        'register_is_grouped': register_is_grouped,
        'register_empty_colspan': register_empty_colspan,
        'query': query,
        'category_filter': category_filter,
        'selected_category': category_filter,
        'item_categories': ItemCategory.get_categories_with_counts(workspace=current_workspace),
        'date_filter': date_filter,
        'start_date': start_date_raw,
        'end_date': end_date_raw,
        'payment_status_filter': payment_status_filter,
        'entry_count': len(register_rows),
        'total_quantity': register_total_quantity,
        'total_amount': register_total_amount,
        'register_paid_total': register_paid_total,
        'register_balance_total': register_balance_total,
        'paid_bills_count': paid_bills_count,
        'part_paid_bills_count': part_paid_bills_count,
        'credit_bills_count': credit_bills_count,
        'total_bills_all_statuses': total_bills_all_statuses,
        'paid_pct': paid_pct,
        'balance_pct': balance_pct,
        'bill_amount_paid_value': bill_amount_paid_value,
        'bill_discount_value': bill_discount_value,
        'bill_notes_value': bill_notes_value,
        'bill_payment_status_value': bill_payment_status_value,
        'bill_payment_method_value': bill_payment_method_value,
        'bill_payment_date_value': bill_payment_date_value,
        'bill_payment_reference_value': bill_payment_reference_value,
        'bill_credit_due_date_value': bill_credit_due_date_value,
        'source_bill': source_bill,
        'source_bill_lines': source_bill_lines,
        'source_bill_mode': bool(source_bill_lines),
        'auto_open_add_modal': auto_open_add_modal,
        'submitted_lines_data': submitted_lines_data,
        'sale_invoice_preview': sale_invoice_preview,
        'inventory_bill_payload': inventory_bill_payload,
        'show_add_entry_button': entry_type not in {'purchase_return', 'sale_return'},
        'walkin_customer_id': walkin_customer_id,
        'walkin_customer_name': walkin_customer_name,
        'party_balances_json': party_balances_json,
        'products_catalog_json': products_catalog_json,
        'available_serials_json': available_serials_json,
        'just_recorded_bill_data': just_recorded_bill_data,
        'financial_accounts': scope_to_workspace(
            FinancialAccount.objects.filter(is_active=True),
            current_workspace,
        ).order_by('-is_default_cash', '-is_default_bank', 'name'),
    }
    return render(request, 'job_tickets/inventory_entry_dashboard.html', context)

def get_daily_daybook_context(target_date, workspace=None):
    """
    Computes daily Daybook and Cash Drawer reconciliation for a specific date.
    Calculates:
      - Opening Cash in Drawer (at start of day)
      - Cash Inflows (Jobs paid/part-paid in cash, retail sales)
      - Cash Outflows (Expenses paid in cash)
      - Contra Transfers (Cash to Bank deposits, Bank to Cash withdrawals)
      - Physical Expected Cash in Drawer
      - Bank / UPI Inflows, Outflows, and Net Movement
      - Account-by-Account live and closing balances
      - Itemized chronological audit ledger for the date
    """
    from datetime import datetime
    from decimal import Decimal
    from django.db.models import Sum
    from django.db.models.functions import Coalesce

    if not isinstance(target_date, date):
        if isinstance(target_date, str):
            try:
                target_date = datetime.strptime(target_date.strip(), '%Y-%m-%d').date()
            except (ValueError, TypeError):
                target_date = timezone.localdate()
        elif hasattr(target_date, 'date'):
            target_date = target_date.date()
        else:
            target_date = timezone.localdate()

    today = timezone.localdate()
    is_today = (target_date == today)

    # 1. Accounts
    accounts_qs = FinancialAccount.objects.filter(is_active=True)
    if workspace:
        accounts_qs = accounts_qs.filter(workspace=workspace)
    accounts = list(accounts_qs.order_by('-is_default_cash', '-is_default_bank', 'name'))

    default_cash = next((a for a in accounts if a.is_default_cash), None)
    if not default_cash:
        default_cash = next((a for a in accounts if a.account_type == FinancialAccount.ACCOUNT_TYPE_CASH), None)

    default_bank = next((a for a in accounts if a.is_default_bank), None)
    if not default_bank:
        default_bank = next((a for a in accounts if a.account_type in [FinancialAccount.ACCOUNT_TYPE_BANK, FinancialAccount.ACCOUNT_TYPE_UPI]), None)

    # 2. Transactions for target_date
    txns_qs = AccountTransaction.objects.filter(transaction_date=target_date)
    if workspace:
        txns_qs = txns_qs.filter(account__workspace=workspace)
    txns = list(
        txns_qs.select_related(
            'account',
            'job_ticket',
            'expense',
            'created_by',
            'transfer_counterpart__account',
        ).order_by('created_at', 'id')
    )

    # 3. Aggregations by Account Type
    cash_inflows = Decimal('0.00')
    cash_outflows = Decimal('0.00')
    cash_to_bank = Decimal('0.00')
    bank_to_cash = Decimal('0.00')

    bank_inflows = Decimal('0.00')
    bank_outflows = Decimal('0.00')

    for t in txns:
        acc_type = t.account.account_type
        amt = t.amount or Decimal('0.00')
        if acc_type == FinancialAccount.ACCOUNT_TYPE_CASH:
            if t.transaction_type == AccountTransaction.TYPE_INFLOW:
                cash_inflows += amt
            elif t.transaction_type == AccountTransaction.TYPE_OUTFLOW:
                cash_outflows += amt
            elif t.transaction_type == AccountTransaction.TYPE_TRANSFER_OUT:
                cash_to_bank += amt
            elif t.transaction_type == AccountTransaction.TYPE_TRANSFER_IN:
                bank_to_cash += amt
        else:
            if t.transaction_type == AccountTransaction.TYPE_INFLOW:
                bank_inflows += amt
            elif t.transaction_type == AccountTransaction.TYPE_OUTFLOW:
                bank_outflows += amt

    net_cash_flow = cash_inflows - cash_outflows - cash_to_bank + bank_to_cash
    net_bank_flow = bank_inflows - bank_outflows + cash_to_bank - bank_to_cash
    total_receipts = cash_inflows + bank_inflows
    total_payments = cash_outflows + bank_outflows
    total_contra = cash_to_bank + bank_to_cash
    net_liquidity_change = total_receipts - total_payments

    # 4. Cash Drawer Reconciliation (Primary Cash Account)
    if default_cash:
        prior_txns = AccountTransaction.objects.filter(
            account=default_cash,
            transaction_date__lt=target_date,
        )
        prior_in = prior_txns.filter(
            transaction_type__in=[AccountTransaction.TYPE_INFLOW, AccountTransaction.TYPE_TRANSFER_IN]
        ).aggregate(total=Coalesce(Sum('amount'), Decimal('0.00')))['total']
        prior_out = prior_txns.filter(
            transaction_type__in=[AccountTransaction.TYPE_OUTFLOW, AccountTransaction.TYPE_TRANSFER_OUT]
        ).aggregate(total=Coalesce(Sum('amount'), Decimal('0.00')))['total']
        opening_cash = (default_cash.opening_balance or Decimal('0.00')) + prior_in - prior_out

        dc_txns = [t for t in txns if t.account_id == default_cash.id]
        dc_in = sum((t.amount for t in dc_txns if t.transaction_type == AccountTransaction.TYPE_INFLOW), Decimal('0.00'))
        dc_out = sum((t.amount for t in dc_txns if t.transaction_type == AccountTransaction.TYPE_OUTFLOW), Decimal('0.00'))
        dc_dep = sum((t.amount for t in dc_txns if t.transaction_type == AccountTransaction.TYPE_TRANSFER_OUT), Decimal('0.00'))
        dc_wth = sum((t.amount for t in dc_txns if t.transaction_type == AccountTransaction.TYPE_TRANSFER_IN), Decimal('0.00'))
        closing_cash = opening_cash + dc_in - dc_out - dc_dep + dc_wth
    else:
        opening_cash = Decimal('0.00')
        closing_cash = Decimal('0.00')
        dc_in = dc_out = dc_dep = dc_wth = Decimal('0.00')

    # 5. Account-by-Account Breakdown for target_date
    account_rows = []
    total_liquid_balance = Decimal('0.00')
    total_cash_balance = Decimal('0.00')
    total_bank_balance = Decimal('0.00')

    for acc in accounts:
        acc_txns = [t for t in txns if t.account_id == acc.id]
        in_amt = sum((t.amount for t in acc_txns if t.transaction_type == AccountTransaction.TYPE_INFLOW), Decimal('0.00'))
        out_amt = sum((t.amount for t in acc_txns if t.transaction_type == AccountTransaction.TYPE_OUTFLOW), Decimal('0.00'))
        tin_amt = sum((t.amount for t in acc_txns if t.transaction_type == AccountTransaction.TYPE_TRANSFER_IN), Decimal('0.00'))
        tout_amt = sum((t.amount for t in acc_txns if t.transaction_type == AccountTransaction.TYPE_TRANSFER_OUT), Decimal('0.00'))
        acc_net = (in_amt + tin_amt) - (out_amt + tout_amt)

        curr_bal = acc.current_balance or Decimal('0.00')
        total_liquid_balance += curr_bal
        if acc.account_type == FinancialAccount.ACCOUNT_TYPE_CASH:
            total_cash_balance += curr_bal
        else:
            total_bank_balance += curr_bal

        account_rows.append({
            'account': acc,
            'name': acc.name,
            'type': acc.account_type,
            'type_display': acc.get_account_type_display(),
            'is_default_cash': acc.is_default_cash,
            'is_default_bank': acc.is_default_bank,
            'inflows': in_amt,
            'outflows': out_amt,
            'transfers_in': tin_amt,
            'transfers_out': tout_amt,
            'net_movement': acc_net,
            'current_balance': curr_bal,
            'txns_count': len(acc_txns),
        })

    return {
        'target_date': target_date,
        'target_date_str': target_date.strftime('%Y-%m-%d'),
        'is_today': is_today,
        'default_cash': default_cash,
        'default_bank': default_bank,
        # Primary Drawer Reconciliation
        'opening_cash': opening_cash,
        'dc_in': dc_in,
        'dc_out': dc_out,
        'dc_dep': dc_dep,
        'dc_wth': dc_wth,
        'closing_cash': closing_cash,
        'expected_drawer_cash': closing_cash,
        # Cash Type Totals
        'cash_inflows': cash_inflows,
        'cash_outflows': cash_outflows,
        'cash_to_bank': cash_to_bank,
        'bank_to_cash': bank_to_cash,
        'net_cash_flow': net_cash_flow,
        # Bank / UPI
        'bank_inflows': bank_inflows,
        'bank_outflows': bank_outflows,
        'net_bank_flow': net_bank_flow,
        # Firm-wide
        'total_receipts': total_receipts,
        'total_payments': total_payments,
        'total_contra': total_contra,
        'net_liquidity_change': net_liquidity_change,
        'total_liquid_balance': total_liquid_balance,
        'total_cash_balance': total_cash_balance,
        'total_bank_balance': total_bank_balance,
        # Details
        'accounts': accounts,
        'account_rows': account_rows,
        'transactions': txns,
        'transactions_count': len(txns),
    }

def get_company_start_date():
    """Finds the creation date of the very first job ticket."""
    first_job = JobTicket.objects.order_by('created_at').first()
    return first_job.created_at.date() if first_job else timezone.localdate()

def get_rack_occupancy_data(racks, exclude_job=None):
    """
    Builds a dictionary of occupancy data for a list of DeviceRack objects.
    Returns:
    {
        str(rack_id): {
            'id': int,
            'name': str,
            'group': str,
            'total_columns': int,
            'occupied': {
                '1': [{'job_code': '...', 'device_type': '...', 'customer_name': '...'}],
                ...
            },
            'occupied_count': int,
            'free_count': int,
            'free_columns': [int, ...],
        }
    }
    """
    from ..models import JobTicket
    occupancy_data = {}
    racks_list = list(racks)
    for r in racks_list:
        total_cols = max(1, r.total_columns or 10)
        occupancy_data[str(r.id)] = {
            'id': r.id,
            'name': r.name,
            'group': r.group,
            'total_columns': total_cols,
            'occupied': {},
            'occupied_count': 0,
            'free_count': total_cols,
            'free_columns': list(range(1, total_cols + 1)),
        }

    if racks_list:
        tickets_qs = JobTicket.objects.filter(
            rack__in=racks_list,
            rack_column__isnull=False
        ).exclude(
            status__in=['Closed', 'Returned']
        )
        if exclude_job:
            tickets_qs = tickets_qs.exclude(id=exclude_job.id)

        active_rack_tickets = tickets_qs.values('rack_id', 'rack_column', 'job_code', 'device_type', 'customer_name')
        for t in active_rack_tickets:
            rid = str(t['rack_id'])
            col_str = str(t['rack_column'])
            if rid in occupancy_data:
                if col_str not in occupancy_data[rid]['occupied']:
                    occupancy_data[rid]['occupied'][col_str] = []
                occupancy_data[rid]['occupied'][col_str].append({
                    'job_code': t['job_code'],
                    'device_type': t['device_type'] or '',
                    'customer_name': t['customer_name'] or '',
                })

        for rid, info in occupancy_data.items():
            total_cols = info['total_columns']
            occupied_cols = [int(c) for c in info['occupied'].keys() if c.isdigit() and 1 <= int(c) <= total_cols]
            occupied_set = set(occupied_cols)
            free_cols = [c for c in range(1, total_cols + 1) if c not in occupied_set]
            info['occupied_count'] = len(occupied_cols)
            info['free_count'] = len(free_cols)
            info['free_columns'] = free_cols

    return occupancy_data

__all__ = [name for name in globals() if not name.startswith("__") and name != "__all__"]

