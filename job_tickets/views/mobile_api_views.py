import uuid
from .helpers import *  # noqa: F401,F403
from .helpers import (
    _build_checklist_schema_for_job,
    _build_inventory_dashboard_metrics,
    _build_inventory_party_directory,
    _build_inventory_product_catalog,
    _build_inventory_register_snapshot,
    _checklist_requires_completion,
    _format_checklist_required_error,
    _get_job_checklist_answers,
    _inventory_form_errors,
    _inventory_party_form_payload_from_api,
    _merge_checklist_answers,
    _missing_required_checklist_labels,
    _mobile_parse_bool,
    _mobile_parse_decimal,
    _money_text,
    _normalize_checkbox_answer,
    _normalize_checklist_answer,
    _normalize_tax_mode_price,
    _serialize_inventory_party_for_api,
    _serialize_inventory_product_for_api,
    _serialize_inventory_register_bill_for_api,
)


def _mobile_request_workspace(request, user):
    workspace = getattr(request, 'current_workspace', None)
    if workspace:
        return workspace
    membership = (
        CompanyUserMembership.objects
        .filter(user=user, is_active=True)
        .select_related('workspace')
        .order_by('workspace_id')
        .first()
    )
    return membership.workspace if membership else None


@csrf_exempt
@require_POST
def mobile_api_login(request):
    """JWT login endpoint for the Flutter mobile app."""
    payload = {}
    if 'application/json' in (request.content_type or ''):
        try:
            payload = json.loads((request.body or b'{}').decode('utf-8'))
        except (ValueError, json.JSONDecodeError, UnicodeDecodeError):
            return JsonResponse({'error': 'invalid_payload', 'message': 'Invalid JSON body.'}, status=400)
    else:
        payload = request.POST

    username = (payload.get('username') or '').strip()
    password = (payload.get('password') or '').strip()
    if not username or not password:
        return JsonResponse(
            {'error': 'missing_credentials', 'message': 'Username and password are required.'},
            status=400,
        )

    user = authenticate(request, username=username, password=password)
    if not user or not user.is_active:
        return JsonResponse({'error': 'invalid_credentials', 'message': 'Invalid username or password.'}, status=401)

    if not user.groups.filter(name='Technicians').exists():
        return JsonResponse(
            {'error': 'technician_only', 'message': 'The mobile app is for technicians only.'},
            status=403,
        )

    session_id = uuid.uuid4().hex
    ip = get_client_ip(request)
    ua = request.META.get('HTTP_USER_AGENT', 'Botgi Mobile App')
    device_name = (payload.get('device_name') or payload.get('device_model') or '').strip()
    if device_name:
        ua = f"{device_name} ({ua})"

    UserSessionActivity.objects.create(
        user=user,
        session_key=session_id,
        channel=UserSessionActivity.CHANNEL_API,
        status=UserSessionActivity.STATUS_ACTIVE,
        ip_address=ip,
        user_agent=ua,
        login_at=timezone.now(),
        last_activity_at=timezone.now(),
        expires_at=timezone.now() + timezone.timedelta(seconds=MOBILE_JWT_EXP_SECONDS),
    )

    access_token = issue_mobile_jwt(user, session_id=session_id)
    role = 'staff' if user.is_staff else 'technician'
    tech_id = ''
    if hasattr(user, 'technician_profile'):
        tech_id = user.technician_profile.unique_id or ''

    return JsonResponse(
        {
            'access_token': access_token,
            'token_type': 'Bearer',
            'expires_in': MOBILE_JWT_EXP_SECONDS,
            'user': {
                'id': user.id,
                'username': user.username,
                'full_name': user.get_full_name() or '',
                'email': user.email or '',
                'is_staff': user.is_staff,
                'role': role,
                'technician_id': tech_id,
            },
        }
    )


@csrf_exempt
@require_POST
def mobile_api_qr_login(request):
    """Logs in a mobile device using a verified QR login token."""
    payload = {}
    if 'application/json' in (request.content_type or ''):
        try:
            payload = json.loads((request.body or b'{}').decode('utf-8'))
        except (ValueError, json.JSONDecodeError, UnicodeDecodeError):
            return JsonResponse({'error': 'invalid_payload', 'message': 'Invalid JSON body.'}, status=400)
    else:
        payload = request.POST

    token_str = (payload.get('token') or payload.get('qr_token') or '').strip()
    if token_str.startswith('{') and token_str.endswith('}'):
        try:
            parsed = json.loads(token_str)
            token_str = (parsed.get('token') or token_str).strip()
        except Exception:
            pass
    if not token_str:
        return JsonResponse({'error': 'missing_token', 'message': 'QR token is required.'}, status=400)

    qr_token = MobileQrLoginToken.objects.select_related('user').filter(token=token_str).first()
    if not qr_token:
        return JsonResponse({'error': 'invalid_token', 'message': 'Invalid QR code.'}, status=401)

    if not qr_token.is_valid:
        msg = 'QR code has already been used.' if qr_token.is_used else 'QR code has expired. Please generate a new one.'
        return JsonResponse({'error': 'expired_token', 'message': msg}, status=401)

    user = qr_token.user
    if not user.is_active:
        return JsonResponse({'error': 'inactive_user', 'message': 'This account is inactive.'}, status=403)

    if not user.groups.filter(name='Technicians').exists():
        return JsonResponse(
            {'error': 'technician_only', 'message': 'The mobile app is for technicians only.'},
            status=403,
        )

    device_name = (payload.get('device_name') or payload.get('device_model') or '').strip()
    ip = get_client_ip(request)
    ua = request.META.get('HTTP_USER_AGENT', 'Botgi Mobile App (QR Login)')
    if device_name:
        ua = f"{device_name} ({ua})"

    qr_token.is_used = True
    qr_token.used_at = timezone.now()
    qr_token.device_info = device_name or ua[:250]
    qr_token.ip_address = ip
    qr_token.save(update_fields=['is_used', 'used_at', 'device_info', 'ip_address'])

    session_id = uuid.uuid4().hex
    UserSessionActivity.objects.create(
        user=user,
        session_key=session_id,
        channel=UserSessionActivity.CHANNEL_API,
        status=UserSessionActivity.STATUS_ACTIVE,
        ip_address=ip,
        user_agent=ua,
        login_at=timezone.now(),
        last_activity_at=timezone.now(),
        expires_at=timezone.now() + timezone.timedelta(seconds=MOBILE_JWT_EXP_SECONDS),
    )

    access_token = issue_mobile_jwt(user, session_id=session_id)
    role = 'staff' if user.is_staff else 'technician'
    tech_id = ''
    if hasattr(user, 'technician_profile'):
        tech_id = user.technician_profile.unique_id or ''

    return JsonResponse(
        {
            'access_token': access_token,
            'token_type': 'Bearer',
            'expires_in': MOBILE_JWT_EXP_SECONDS,
            'user': {
                'id': user.id,
                'username': user.username,
                'full_name': user.get_full_name() or '',
                'email': user.email or '',
                'is_staff': user.is_staff,
                'role': role,
                'technician_id': tech_id,
            },
        }
    )

def mobile_api_me(request):
    if request.method != 'GET':
        return JsonResponse({'error': 'method_not_allowed'}, status=405)

    user, error_response = authenticate_mobile_request(request)
    if error_response:
        return error_response
    role = 'staff' if user.is_staff else 'technician'
    tech_id = ''
    if hasattr(user, 'technician_profile'):
        tech_id = user.technician_profile.unique_id or ''

    return JsonResponse(
        {
            'user': {
                'id': user.id,
                'username': user.username,
                'full_name': user.get_full_name() or '',
                'email': user.email or '',
                'is_staff': user.is_staff,
                'role': role,
                'technician_id': tech_id,
            }
        }
    )

def mobile_api_jobs(request):
    if request.method != 'GET':
        return JsonResponse({'error': 'method_not_allowed'}, status=405)

    user, error_response = authenticate_mobile_request(request)
    if error_response:
        return error_response
    workspace = _mobile_request_workspace(request, user)

    if user.is_staff:
        if not user_has_staff_access(user, "staff_dashboard"):
            return JsonResponse({'error': 'forbidden', 'message': 'Staff dashboard access required.'}, status=403)
        jobs_qs = JobTicket.objects.all()
        if workspace:
            jobs_qs = jobs_qs.filter(workspace=workspace)
    elif hasattr(user, 'technician_profile'):
        jobs_qs = JobTicket.objects.filter(
            assigned_to=user.technician_profile,
            workspace_id=user.technician_profile.workspace_id,
        )
    else:
        jobs_qs = JobTicket.objects.none()

    # Scope filter: active, history, or all
    scope = (request.GET.get('scope') or '').strip().lower()
    finished_statuses = ['Completed', 'Closed', 'Ready for Pickup', 'Returned']
    if scope == 'active':
        jobs_qs = jobs_qs.exclude(status__in=finished_statuses)
    elif scope == 'history':
        jobs_qs = jobs_qs.filter(status__in=['Completed', 'Closed', 'Ready for Pickup'])

    # Status filter if passed directly
    status_param = (request.GET.get('status') or '').strip()
    if status_param and status_param.upper() != 'ALL':
        jobs_qs = jobs_qs.filter(status__iexact=status_param)

    # Date / Month filter
    report_month_param = (request.GET.get('report_month') or '').strip()
    preset = (request.GET.get('preset') or '').strip().lower()
    today = timezone.localdate()

    if preset == 'this_month':
        start_date = today.replace(day=1)
        if today.month == 12:
            end_date = datetime(today.year + 1, 1, 1).date()
        else:
            end_date = datetime(today.year, today.month + 1, 1).date()
        start_of_period = timezone.make_aware(datetime.combine(start_date, datetime.min.time()))
        end_of_period = timezone.make_aware(datetime.combine(end_date, datetime.min.time()))
        jobs_qs = jobs_qs.filter(updated_at__gte=start_of_period, updated_at__lt=end_of_period)
    elif preset == 'last_month':
        first_day_current = today.replace(day=1)
        last_month_end = first_day_current - timedelta(days=1)
        last_month_start = last_month_end.replace(day=1)
        start_of_period = timezone.make_aware(datetime.combine(last_month_start, datetime.min.time()))
        end_of_period = timezone.make_aware(datetime.combine(first_day_current, datetime.min.time()))
        jobs_qs = jobs_qs.filter(updated_at__gte=start_of_period, updated_at__lt=end_of_period)
    elif report_month_param:
        try:
            year_str, month_str = report_month_param.split('-')
            year = int(year_str)
            month = int(month_str)
            start_date = datetime(year, month, 1).date()
            if month == 12:
                end_date = datetime(year + 1, 1, 1).date()
            else:
                end_date = datetime(year, month + 1, 1).date()
            start_of_period = timezone.make_aware(datetime.combine(start_date, datetime.min.time()))
            end_of_period = timezone.make_aware(datetime.combine(end_date, datetime.min.time()))
            jobs_qs = jobs_qs.filter(updated_at__gte=start_of_period, updated_at__lt=end_of_period)
        except (ValueError, TypeError):
            pass

    try:
        limit = min(max(int(request.GET.get('limit', 300)), 1), 500)
    except (TypeError, ValueError):
        limit = 300

    jobs = list(
        jobs_qs.select_related('assigned_to__user', 'specialized_service')
        .prefetch_related('service_logs')
        .order_by('-updated_at')[:limit]
    )
    calculate_job_totals(jobs, exclude_vendor_charges=True)

    history_parts_total = sum(j.part_total or Decimal('0.00') for j in jobs)
    history_service_total = sum(j.service_total or Decimal('0.00') for j in jobs)
    history_grand_total = sum(j.total or Decimal('0.00') for j in jobs)

    jobs_data = []
    for job in jobs:
        jobs_data.append(
            {
                'job_code': job.job_code,
                'customer_name': job.customer_name,
                'customer_phone': job.customer_phone,
                'device': f"{job.device_type} {job.device_brand or ''} {job.device_model or ''}".strip(),
                'status': job.status,
                'created_at': timezone.localtime(job.created_at).strftime('%Y-%m-%d %H:%M'),
                'updated_at': timezone.localtime(job.updated_at).strftime('%Y-%m-%d %H:%M'),
                'total': str(job.total or Decimal('0.00')),
                'part_total': str(job.part_total or Decimal('0.00')),
                'service_total': str(job.service_total or Decimal('0.00')),
                'discount_amount': str(job.discount_amount or Decimal('0.00')),
                'is_new_assignment': bool(job.is_new_assignment),
                'returned_from_vendor': bool(
                    getattr(getattr(job, 'specialized_service', None), 'status', '') == 'Returned from Vendor'
                ),
                'assigned_to': (
                    job.assigned_to.user.username
                    if job.assigned_to and getattr(job.assigned_to, 'user', None)
                    else ''
                ),
                'priority': 'medium',
                'due_date': '',
            }
        )

    return JsonResponse({
        'count': len(jobs_data),
        'summary': {
            'count': len(jobs_data),
            'parts_total': str(history_parts_total),
            'service_total': str(history_service_total),
            'grand_total': str(history_grand_total),
        },
        'jobs': jobs_data,
    })

def mobile_api_job_detail(request, job_code):
    if request.method != 'GET':
        return JsonResponse({'error': 'method_not_allowed'}, status=405)

    user, error_response = authenticate_mobile_request(request)
    if error_response:
        return error_response

    job = get_object_or_404(
        JobTicket.objects.select_related('assigned_to__user', 'created_by', 'rack').prefetch_related(
            'service_logs__product_sale__product',
            'logs__user',
            'photos',
            'tasks__assigned_to__user',
            'tasks__created_by',
        ),
        job_code=job_code,
    )

    permissions = get_mobile_job_permissions(user, job)
    if not permissions['can_access']:
        return JsonResponse(
            {'error': 'forbidden', 'message': 'You are not allowed to access this job.'},
            status=403,
        )

    calculate_job_totals([job], exclude_vendor_charges=True)
    grand_total = (job.total or Decimal('0.00')) - (job.discount_amount or Decimal('0.00'))

    service_logs_data = []
    for service_log in job.service_logs.all().order_by('created_at'):
        product_sale = getattr(service_log, 'product_sale', None)
        service_logs_data.append(
            {
                'id': service_log.id,
                'description': service_log.description,
                'part_cost': str(service_log.part_cost or Decimal('0.00')),
                'service_charge': str(service_log.service_charge or Decimal('0.00')),
                'sales_invoice_number': service_log.sales_invoice_number or '',
                'created_at': timezone.localtime(service_log.created_at).strftime('%Y-%m-%d %H:%M'),
                'is_product_sale': bool(product_sale),
                'product_name': product_sale.product.name if product_sale and product_sale.product else '',
                'product_quantity': int(product_sale.quantity) if product_sale else 0,
                'product_unit_price': str(product_sale.unit_price or Decimal('0.00')) if product_sale else '0.00',
                'product_line_total': str(product_sale.line_total or Decimal('0.00')) if product_sale else '0.00',
            }
        )

    action_labels = {
        'CREATED': 'Job Created',
        'ASSIGNED': 'Technician Assigned',
        'STATUS': 'Status Updated',
        'NOTE': 'Note Updated',
        'SERVICE': 'Service Updated',
        'BILLING': 'Billing Updated',
        'CLOSED': 'Job Closed',
    }
    timeline_queryset = job.logs.select_related('user').order_by('-timestamp')[:60]
    timeline = []
    for entry in reversed(list(timeline_queryset)):
        timeline.append(
            {
                'action': entry.action,
                'label': action_labels.get(entry.action, entry.action.title()),
                'details': entry.details,
                'timestamp': timezone.localtime(entry.timestamp).strftime('%Y-%m-%d %H:%M'),
                'user': entry.user.username if entry.user else 'System',
            }
        )

    if not timeline:
        timeline.append(
            {
                'action': 'CREATED',
                'label': 'Job Created',
                'details': f"Job {job.job_code} created.",
                'timestamp': timezone.localtime(job.created_at).strftime('%Y-%m-%d %H:%M'),
                'user': job.created_by.username if job.created_by else 'System',
            }
        )

    checklist_schema, checklist_title, checklist_notes = _build_checklist_schema_for_job(job)
    can_change_status = _mobile_technician_can_change_status(job)
    specialized_service = getattr(job, 'specialized_service', None)

    linked_tasks = []
    for t in job.tasks.all().order_by('-created_at')[:20]:
        linked_tasks.append(
            {
                'id': t.id,
                'title': t.title,
                'description': t.description or '',
                'priority': t.priority,
                'priority_display': t.get_priority_display(),
                'status': t.status,
                'status_display': t.get_status_display(),
                'due_date': timezone.localtime(t.due_date).strftime('%Y-%m-%d %H:%M') if t.due_date else '',
                'assigned_to': (
                    t.assigned_to.user.get_full_name() or t.assigned_to.user.username
                    if t.assigned_to and getattr(t.assigned_to, 'user', None)
                    else ''
                ),
                'created_by': t.created_by.username if t.created_by else '',
                'created_at': timezone.localtime(t.created_at).strftime('%Y-%m-%d %H:%M'),
            }
        )

    active_racks_qs = DeviceRack.objects.filter(is_active=True)
    if job.workspace_id:
        active_racks_qs = active_racks_qs.filter(workspace=job.workspace)
    racks_list = list(active_racks_qs.order_by('group', 'name'))
    rack_occupancy_data = get_rack_occupancy_data(racks_list, exclude_job=job)
    available_racks = [
        {
            'id': r.id,
            'name': r.name,
            'group': r.group,
            'total_columns': max(1, r.total_columns or 10),
            'occupied': rack_occupancy_data.get(str(r.id), {}).get('occupied', {}),
            'occupied_count': rack_occupancy_data.get(str(r.id), {}).get('occupied_count', 0),
            'free_count': rack_occupancy_data.get(str(r.id), {}).get('free_count', max(1, r.total_columns or 10)),
            'free_columns': rack_occupancy_data.get(str(r.id), {}).get('free_columns', []),
        }
        for r in racks_list
    ]

    return JsonResponse(
        {
            'job': {
                'job_code': job.job_code,
                'status': job.status,
                'status_display': job.get_status_display(),
                'customer_name': job.customer_name,
                'customer_phone': job.customer_phone,
                'device_type': job.device_type or '',
                'device_brand': job.device_brand or '',
                'device_model': job.device_model or '',
                'device_serial': job.device_serial or '',
                'device_password': job.device_password or '',
                'rack_id': job.rack_id,
                'rack_column': job.rack_column,
                'rack_location': job.rack_location_display or '',
                'rack_short': job.rack_short_display or '',
                'reported_issue': job.reported_issue or '',
                'additional_items': job.additional_items or '',
                'technician_notes': job.technician_notes or '',
                'is_under_warranty': bool(job.is_under_warranty),
                'estimated_amount': str(job.estimated_amount or Decimal('0.00')) if job.estimated_amount else '',
                'estimated_delivery': job.estimated_delivery.strftime('%Y-%m-%d') if job.estimated_delivery else '',
                'vyapar_invoice_number': job.vyapar_invoice_number or '',
                'feedback_rating': int(job.feedback_rating) if job.feedback_rating else 0,
                'feedback_comment': job.feedback_comment or '',
                'feedback_date': timezone.localtime(job.feedback_date).strftime('%Y-%m-%d %H:%M')
                if job.feedback_date
                else '',
                'created_by': job.created_by.username if job.created_by else '',
                'assigned_to': (
                    job.assigned_to.user.username
                    if job.assigned_to and getattr(job.assigned_to, 'user', None)
                    else ''
                ),
                'updated_at': timezone.localtime(job.updated_at).strftime('%Y-%m-%d %H:%M'),
                'created_at': timezone.localtime(job.created_at).strftime('%Y-%m-%d %H:%M'),
                'technician_checklist': _get_job_checklist_answers(job),
                'receipt_url': f'/client-receipt/{job.job_code}/',
                'receipt_pdf_url': f'/client-receipt/{job.job_code}/pdf/',
            },
            'tasks': linked_tasks,

            'financials': {
                'part_total': str(job.part_total or Decimal('0.00')),
                'service_total': str(job.service_total or Decimal('0.00')),
                'subtotal': str(job.total or Decimal('0.00')),
                'discount_amount': str(job.discount_amount or Decimal('0.00')),
                'grand_total': str(grand_total),
            },
            'service_logs': service_logs_data,
            'timeline': timeline,
            'available_actions': get_mobile_job_available_actions(user, job),
            'available_racks': available_racks,
            'permissions': {
                'can_edit_notes': mobile_can_edit_notes(user, job),
                'can_manage_service_logs': mobile_can_manage_service_lines(user, job),
                'can_change_status': bool(permissions['is_assigned_tech'] and can_change_status),
                'can_request_specialized_service': bool(
                    permissions['is_assigned_tech']
                    and job.status not in ['Completed', 'Ready for Pickup', 'Closed']
                ),
            },
            'status_choices': _mobile_technician_status_choices(job),
            'can_change_status': bool(can_change_status),
            'checklist_required_for_completion': bool(_checklist_requires_completion(checklist_schema)),
            'specialized_service': _mobile_specialized_service_payload(specialized_service),
            'technician_checklist_schema': checklist_schema,
            'technician_checklist_title': checklist_title,
            'technician_checklist_notes': checklist_notes,
            'photos': [_mobile_job_photo_payload(request, job, photo) for photo in job.photos.all()],
        }
    )


def _mobile_technician_status_choices(job):
    if job.status in ['Closed', 'Ready for Pickup']:
        return []
    excluded_statuses = {'Pending', 'Ready for Pickup', 'Closed', 'Specialized Service'}
    return [
        {'value': value, 'label': label}
        for value, label in job.STATUS_CHOICES
        if label not in excluded_statuses
    ]


def _mobile_technician_can_change_status(job):
    if job.status in ['Closed', 'Ready for Pickup']:
        return False
    if job.status == 'Specialized Service':
        specialized_service = getattr(job, 'specialized_service', None)
        if specialized_service and specialized_service.status != 'Returned from Vendor':
            return False
    return True


def _mobile_specialized_service_payload(service):
    if not service:
        return {
            'exists': False,
            'status': '',
            'status_display': '',
            'vendor_name': '',
            'notes': '',
        }
    return {
        'exists': True,
        'status': service.status or '',
        'status_display': service.get_status_display() if service.status else '',
        'vendor_name': service.vendor.company_name if service.vendor else '',
        'notes': service.notes or '',
    }


def _mobile_job_photo_payload(request, job, photo):
    return {
        'id': photo.id,
        'name': photo.image_name or f'{job.job_code}-photo-{photo.id}.jpg',
        'content_type': photo.image_content_type or 'application/octet-stream',
        'uploaded_at': timezone.localtime(photo.uploaded_at).strftime('%Y-%m-%d %H:%M'),
        'url': request.build_absolute_uri(
            reverse('mobile_api_job_photo_file', kwargs={'job_code': job.job_code, 'photo_id': photo.id})
        ),
    }


def _mobile_normalize_checklist_answers(raw_answers, checklist_schema):
    if not isinstance(raw_answers, dict):
        raise ValueError("Checklist answers must be an object.")

    normalized = {}
    invalid_option_labels = []
    for field in checklist_schema:
        key = field['key']
        if key not in raw_answers:
            continue

        if field.get('type') == 'checkbox':
            value = _normalize_checkbox_answer(raw_answers.get(key))
        else:
            value = _normalize_checklist_answer(raw_answers.get(key))

        options = field.get('options') or []
        if field.get('type') == 'select' and value and options and value not in options:
            invalid_option_labels.append(field['label'])
            continue

        normalized[key] = value

    if invalid_option_labels:
        preview = ', '.join(invalid_option_labels[:6])
        suffix = '...' if len(invalid_option_labels) > 6 else ''
        raise ValueError(f"Invalid checklist option for: {preview}{suffix}")

    return normalized


@csrf_exempt
@require_POST
def mobile_api_job_checklist(request, job_code):
    user, error_response = authenticate_mobile_request(request)
    if error_response:
        return error_response

    try:
        payload = json.loads((request.body or b'{}').decode('utf-8'))
    except (ValueError, json.JSONDecodeError, UnicodeDecodeError):
        return JsonResponse({'error': 'invalid_payload', 'message': 'Invalid JSON body.'}, status=400)

    with transaction.atomic():
        job = get_object_or_404(JobTicket.objects.select_for_update(), job_code=job_code)
        permissions = get_mobile_job_permissions(user, job)
        if not permissions['can_access']:
            return JsonResponse(
                {'error': 'forbidden', 'message': 'You are not allowed to update this job.'},
                status=403,
            )

        checklist_schema, checklist_title, checklist_notes = _build_checklist_schema_for_job(job)
        try:
            posted_answers = _mobile_normalize_checklist_answers(payload.get('answers') or {}, checklist_schema)
        except ValueError as exc:
            return JsonResponse({'error': 'invalid_checklist', 'message': str(exc)}, status=400)

        if not checklist_schema:
            return JsonResponse({'error': 'no_checklist', 'message': 'No checklist is configured for this job.'}, status=400)

        job.technician_checklist = _merge_checklist_answers(_get_job_checklist_answers(job), posted_answers)
        job.save(update_fields=['technician_checklist', 'updated_at'])
        JobTicketLog.objects.create(job_ticket=job, user=user, action='NOTE', details='Technician checklist updated.')

    send_job_update_message(job.job_code, job.status)
    checklist_schema, checklist_title, checklist_notes = _build_checklist_schema_for_job(job)
    return JsonResponse(
        {
            'ok': True,
            'message': 'Checklist saved successfully.',
            'technician_checklist': _get_job_checklist_answers(job),
            'technician_checklist_schema': checklist_schema,
            'technician_checklist_title': checklist_title,
            'technician_checklist_notes': checklist_notes,
        }
    )


@csrf_exempt
@require_POST
def mobile_api_job_technician_update(request, job_code):
    user, error_response = authenticate_mobile_request(request)
    if error_response:
        return error_response

    try:
        payload = json.loads((request.body or b'{}').decode('utf-8'))
    except (ValueError, json.JSONDecodeError, UnicodeDecodeError):
        return JsonResponse({'error': 'invalid_payload', 'message': 'Invalid JSON body.'}, status=400)

    new_status = (payload.get('status') or '').strip()
    technician_notes = payload.get('technician_notes') or ''
    raw_answers = payload.get('answers') or {}
    excluded_statuses = {'Ready for Pickup', 'Closed', 'Specialized Service'}

    with transaction.atomic():
        job = get_object_or_404(
            JobTicket.objects.select_for_update(),
            job_code=job_code,
        )
        permissions = get_mobile_job_permissions(user, job)
        if not permissions['is_assigned_tech']:
            return JsonResponse(
                {'error': 'forbidden', 'message': 'Only the assigned technician can update this job.'},
                status=403,
            )

        if job.status in ['Closed', 'Ready for Pickup']:
            return JsonResponse(
                {'error': 'status_locked', 'message': f'Cannot change status for a {job.get_status_display().lower()} job.'},
                status=400,
            )

        if not _mobile_technician_can_change_status(job):
            return JsonResponse(
                {'error': 'status_locked', 'message': 'Cannot change status while job is with vendor.'},
                status=403,
            )

        allowed_statuses = {item['value'] for item in _mobile_technician_status_choices(job)}
        if not new_status:
            return JsonResponse({'error': 'missing_status', 'message': 'Status is required.'}, status=400)
        if new_status in excluded_statuses or new_status not in allowed_statuses:
            return JsonResponse({'error': 'invalid_status', 'message': 'Invalid status update attempt.'}, status=400)

        checklist_schema, _, _ = _build_checklist_schema_for_job(job)
        try:
            posted_answers = _mobile_normalize_checklist_answers(raw_answers, checklist_schema)
        except ValueError as exc:
            return JsonResponse({'error': 'invalid_checklist', 'message': str(exc)}, status=400)

        old_status_display = job.get_status_display()
        old_notes = job.technician_notes or ''
        old_answers = _get_job_checklist_answers(job)
        merged_answers = _merge_checklist_answers(old_answers, posted_answers)

        if new_status == 'Completed':
            missing_required = []
            for field in checklist_schema:
                if not field.get('required'):
                    continue
                value = _normalize_checklist_answer(merged_answers.get(field['key'], ''))
                if not value:
                    missing_required.append(field['label'])
            if missing_required:
                return JsonResponse(
                    {
                        'error': 'checklist_incomplete',
                        'message': _format_checklist_required_error(missing_required),
                        'missing_checklist_fields': missing_required,
                    },
                    status=400,
                )

        job.status = new_status
        job.technician_notes = technician_notes
        job.technician_checklist = merged_answers
        job.save(update_fields=['status', 'technician_notes', 'technician_checklist', 'updated_at'])

        if old_status_display != job.get_status_display():
            JobTicketLog.objects.create(
                job_ticket=job,
                user=user,
                action='STATUS',
                details=f"Status changed from '{old_status_display}' to '{job.get_status_display()}'.",
            )
            send_job_update_message(job.job_code, job.status)

        if old_notes != technician_notes:
            details = f'Technician notes updated: "{technician_notes}"' if technician_notes else 'Technician notes cleared.'
            JobTicketLog.objects.create(job_ticket=job, user=user, action='NOTE', details=details)

        if old_answers != merged_answers:
            JobTicketLog.objects.create(job_ticket=job, user=user, action='NOTE', details='Technician checklist updated.')

    return JsonResponse(
        {
            'ok': True,
            'message': 'Status, notes, and checklist updated successfully.',
            'status': job.status,
            'status_display': job.get_status_display(),
            'technician_notes': job.technician_notes or '',
            'technician_checklist': _get_job_checklist_answers(job),
        }
    )


@csrf_exempt
@require_http_methods(['GET', 'POST'])
def mobile_api_job_photos(request, job_code):
    user, error_response = authenticate_mobile_request(request)
    if error_response:
        return error_response

    job = get_object_or_404(JobTicket.objects.prefetch_related('photos'), job_code=job_code)
    permissions = get_mobile_job_permissions(user, job)
    if not permissions['can_access']:
        return JsonResponse(
            {'error': 'forbidden', 'message': 'You are not allowed to access this job.'},
            status=403,
        )

    if request.method == 'GET':
        return JsonResponse({'photos': [_mobile_job_photo_payload(request, job, photo) for photo in job.photos.all()]})

    photo_files = request.FILES.getlist('photos') or request.FILES.getlist('photo')
    if not photo_files:
        return JsonResponse({'error': 'missing_photo', 'message': 'Upload at least one photo.'}, status=400)

    created_photos = []
    with transaction.atomic():
        locked_job = JobTicket.objects.select_for_update().get(id=job.id)
        for photo_file in photo_files:
            content_type, upload_error = validate_job_photo_upload(photo_file)
            if upload_error:
                return JsonResponse({'error': 'invalid_photo', 'message': upload_error}, status=400)

            created_photos.append(
                JobTicketPhoto.objects.create(
                    job_ticket=locked_job,
                    image_name=(getattr(photo_file, 'name', '') or 'device-photo').strip()[:255],
                    image_content_type=content_type[:100],
                    image_data=photo_file.read(),
                )
            )

        JobTicketLog.objects.create(
            job_ticket=locked_job,
            user=user,
            action='NOTE',
            details=f"{len(created_photos)} technician photo(s) uploaded from mobile app.",
        )

    send_job_update_message(job.job_code, job.status)
    return JsonResponse(
        {
            'ok': True,
            'message': f"{len(created_photos)} photo(s) uploaded successfully.",
            'photos': [_mobile_job_photo_payload(request, job, photo) for photo in created_photos],
        }
    )


def mobile_api_job_photo_file(request, job_code, photo_id):
    if request.method != 'GET':
        return JsonResponse({'error': 'method_not_allowed'}, status=405)

    user, error_response = authenticate_mobile_request(request)
    if error_response:
        return error_response

    job = get_object_or_404(JobTicket, job_code=job_code)
    permissions = get_mobile_job_permissions(user, job)
    if not permissions['can_access']:
        return JsonResponse({'error': 'forbidden', 'message': 'You are not allowed to access this photo.'}, status=403)

    photo = get_object_or_404(JobTicketPhoto, id=photo_id, job_ticket=job)
    if photo.image_data:
        response = HttpResponse(
            bytes(photo.image_data),
            content_type=photo.image_content_type or 'application/octet-stream',
        )
        response['Content-Disposition'] = f'inline; filename="{photo.image_name or f"{job.job_code}-photo-{photo.id}.jpg"}"'
        return response

    if photo.image:
        with photo.image.open('rb') as image_file:
            image_bytes = image_file.read()
        response = HttpResponse(
            image_bytes,
            content_type=photo.image_content_type or mimetypes.guess_type(photo.image.name)[0] or 'application/octet-stream',
        )
        response['Content-Disposition'] = f'inline; filename="{photo.image_name or f"{job.job_code}-photo-{photo.id}.jpg"}"'
        return response

    return HttpResponse(status=404)


@csrf_exempt
@require_POST
def mobile_api_job_action(request, job_code):
    user, error_response = authenticate_mobile_request(request)
    if error_response:
        return error_response

    try:
        payload = json.loads((request.body or b'{}').decode('utf-8'))
    except (ValueError, json.JSONDecodeError, UnicodeDecodeError):
        return JsonResponse({'error': 'invalid_payload', 'message': 'Invalid JSON body.'}, status=400)

    action_key = (payload.get('action') or '').strip()
    if not action_key:
        return JsonResponse({'error': 'missing_action', 'message': 'Action is required.'}, status=400)

    with transaction.atomic():
        job = get_object_or_404(
            JobTicket.objects.select_for_update(),
            job_code=job_code,
        )

        permissions = get_mobile_job_permissions(user, job)
        if not permissions['can_access']:
            return JsonResponse(
                {'error': 'forbidden', 'message': 'You are not allowed to update this job.'},
                status=403,
            )

        old_status_display = job.get_status_display()
        if action_key == 'acknowledge':
            if not permissions['is_assigned_tech']:
                return JsonResponse({'error': 'forbidden', 'message': 'Only the assigned technician can acknowledge this job.'}, status=403)
            if job.is_new_assignment:
                job.is_new_assignment = False
                job.save(update_fields=['is_new_assignment', 'updated_at'])
                JobTicketLog.objects.create(
                    job_ticket=job,
                    user=user,
                    action='ACKNOWLEDGED',
                    details=f"Job {job.job_code} acknowledged by technician.",
                )
                send_job_update_message(job.job_code, job.status)
            return JsonResponse(
                {
                    'ok': True,
                    'message': f"Job {job.job_code} acknowledged successfully.",
                    'status': job.status,
                    'status_display': job.get_status_display(),
                    'available_actions': get_mobile_job_available_actions(user, job),
                }
            )

        if action_key == 'return_to_staff':
            if not permissions['is_assigned_tech']:
                return JsonResponse({'error': 'forbidden', 'message': 'Only the assigned technician can return this job.'}, status=403)
            if job.status in ['Closed', 'Completed', 'Ready for Pickup']:
                return JsonResponse({'error': 'invalid_transition', 'message': f'Cannot return a {job.get_status_display().lower()} job to staff.'}, status=400)
            if not job.is_new_assignment:
                return JsonResponse(
                    {'error': 'invalid_transition', 'message': 'Only newly assigned jobs can be returned to staff.'},
                    status=400,
                )
            job.assigned_to = None
            job.status = 'Pending'
            job.is_new_assignment = False
            job.save(update_fields=['assigned_to', 'status', 'is_new_assignment', 'updated_at'])
            JobTicketLog.objects.create(
                job_ticket=job,
                user=user,
                action='ASSIGNED',
                details=f"Technician returned job to staff. Previous status: {old_status_display}",
            )
            send_job_update_message(job.job_code, job.status)
            return JsonResponse(
                {
                    'ok': True,
                    'message': f"Job {job.job_code} returned to staff for reassignment.",
                    'status': job.status,
                    'status_display': job.get_status_display(),
                    'available_actions': [],
                }
            )

        if action_key == 'request_specialized_service':
            if not permissions['is_assigned_tech']:
                return JsonResponse(
                    {'error': 'forbidden', 'message': 'Only the assigned technician can request specialized service.'},
                    status=403,
                )
            if job.status in ['Completed', 'Ready for Pickup', 'Closed']:
                return JsonResponse(
                    {'error': 'invalid_transition', 'message': 'Finalized jobs cannot be sent for specialized service.'},
                    status=400,
                )

            service = getattr(job, 'specialized_service', None)
            if service:
                if service.status == 'Returned from Vendor':
                    job.status = 'Specialized Service'
                    job.save(update_fields=['status', 'updated_at'])
                    service.status = 'Awaiting Assignment'
                    service.vendor = None
                    service.vendor_cost = None
                    service.vendor_discount_amount = Decimal('0.00')
                    service.vendor_paid_amount = Decimal('0.00')
                    service.vendor_balance_amount = Decimal('0.00')
                    service.client_charge = None
                    service.sent_date = None
                    service.returned_date = None
                    service.save()
                    details = (
                        f"Status changed from '{old_status_display}' to 'Specialized Service'. "
                        'Re-requested specialized service.'
                    )
                else:
                    return JsonResponse(
                        {'error': 'already_specialized', 'message': 'This job has already been marked for specialized service.'},
                        status=400,
                    )
            else:
                job.status = 'Specialized Service'
                job.save(update_fields=['status', 'updated_at'])
                SpecializedService.objects.create(job_ticket=job)
                details = (
                    f"Status changed from '{old_status_display}' to 'Specialized Service'. "
                    'Awaiting vendor assignment by staff.'
                )

            JobTicketLog.objects.create(job_ticket=job, user=user, action='STATUS', details=details)
            send_job_update_message(job.job_code, job.status)
            return JsonResponse(
                {
                    'ok': True,
                    'message': f"Job {job.job_code} sent to staff for specialized service assignment.",
                    'status': job.status,
                    'status_display': job.get_status_display(),
                    'available_actions': get_mobile_job_available_actions(user, job),
                }
            )

        if job.status == 'Closed':
            return JsonResponse({'error': 'job_closed', 'message': 'This job is closed and billed. No actions can be performed.'}, status=400)

        target_status = None
        log_action = 'STATUS'

        if action_key == 'start':
            if job.status not in ['Pending', 'Under Inspection']:
                return JsonResponse(
                    {'error': 'invalid_transition', 'message': 'Job cannot be marked started from current status.'},
                    status=400,
                )
            target_status = 'Repairing'
        elif action_key == 'complete':
            if job.status not in ['Under Inspection', 'Repairing', 'Specialized Service']:
                return JsonResponse(
                    {'error': 'invalid_transition', 'message': 'Job cannot be completed from current status.'},
                    status=400,
                )
            checklist_schema, _, _ = _build_checklist_schema_for_job(job)
            raw_answers = payload.get('answers')
            if raw_answers is not None:
                try:
                    posted_answers = _mobile_normalize_checklist_answers(raw_answers, checklist_schema)
                    job.technician_checklist = _merge_checklist_answers(_get_job_checklist_answers(job), posted_answers)
                    job.save(update_fields=['technician_checklist', 'updated_at'])
                except ValueError as exc:
                    return JsonResponse({'error': 'invalid_checklist', 'message': str(exc)}, status=400)

            if not permissions['is_staff']:
                missing_required = _missing_required_checklist_labels(job, checklist_schema)
                if missing_required:
                    return JsonResponse(
                        {
                            'error': 'checklist_incomplete',
                            'message': _format_checklist_required_error(missing_required),
                            'missing_checklist_fields': missing_required,
                        },
                        status=400,
                    )
            target_status = 'Completed'
        elif action_key == 'ready_for_pickup':
            if not permissions['is_staff'] or not user_has_staff_access(user, 'job_status_change'):
                return JsonResponse({'error': 'forbidden', 'message': 'Only staff with status change access can perform this action.'}, status=403)
            if job.status != 'Completed':
                return JsonResponse(
                    {'error': 'invalid_transition', 'message': 'Only completed jobs can be marked ready for pickup.'},
                    status=400,
                )
            target_status = 'Ready for Pickup'
        elif action_key == 'close':
            if not permissions['is_staff']:
                return JsonResponse({'error': 'forbidden', 'message': 'Only staff can perform this action.'}, status=403)
            if job.status not in ['Completed', 'Ready for Pickup']:
                return JsonResponse(
                    {'error': 'invalid_transition', 'message': 'Job cannot be closed from current status.'},
                    status=400,
                )
            target_status = 'Closed'
            log_action = 'CLOSED'
        else:
            return JsonResponse({'error': 'invalid_action', 'message': 'Unsupported action.'}, status=400)

        job.status = target_status
        job.save(update_fields=['status', 'updated_at'])

        send_job_update_message(job.job_code, job.status)
        details = f"Status changed from '{old_status_display}' to '{job.get_status_display()}'."
        JobTicketLog.objects.create(job_ticket=job, user=user, action=log_action, details=details)

    return JsonResponse(
        {
            'ok': True,
            'message': f"Job {job.job_code} updated to {job.get_status_display()}.",
            'status': job.status,
            'status_display': job.get_status_display(),
            'available_actions': get_mobile_job_available_actions(user, job),
        }
    )


@csrf_exempt
@require_POST
def mobile_api_job_rack(request, job_code):
    user, error_response = authenticate_mobile_request(request)
    if error_response:
        return error_response

    try:
        payload = json.loads((request.body or b'{}').decode('utf-8'))
    except (ValueError, json.JSONDecodeError, UnicodeDecodeError):
        return JsonResponse({'error': 'invalid_payload', 'message': 'Invalid JSON body.'}, status=400)

    rack_id_raw = payload.get('rack_id')
    rack_col_raw = payload.get('rack_column')

    with transaction.atomic():
        job = get_object_or_404(
            JobTicket.objects.select_for_update(),
            job_code=job_code,
        )
        permissions = get_mobile_job_permissions(user, job)
        if not (permissions['can_access'] and (
            permissions['is_assigned_tech']
            or permissions['is_staff']
            or user.is_staff
            or user.is_superuser
            or user.groups.filter(name__icontains='technician').exists()
        )):
            return JsonResponse(
                {'error': 'forbidden', 'message': 'You do not have permission to update rack for this job.'},
                status=403,
            )

        old_rack = job.rack
        old_col = job.rack_column
        new_rack = None
        new_col = None

        if rack_id_raw is not None and str(rack_id_raw).strip() != '':
            try:
                rack_id = int(rack_id_raw)
                rack_qs = DeviceRack.objects.filter(id=rack_id, is_active=True)
                if job.workspace_id:
                    rack_qs = rack_qs.filter(workspace=job.workspace)
                new_rack = rack_qs.first()
                if not new_rack:
                    return JsonResponse(
                        {'error': 'invalid_rack', 'message': 'Selected rack is not available or inactive.'},
                        status=400,
                    )
            except (ValueError, TypeError):
                return JsonResponse({'error': 'invalid_rack', 'message': 'Invalid rack ID.'}, status=400)

            if rack_col_raw is not None and str(rack_col_raw).strip() != '':
                try:
                    parsed_col = int(rack_col_raw)
                    if 1 <= parsed_col <= max(1, new_rack.total_columns or 100):
                        new_col = parsed_col
                    else:
                        return JsonResponse(
                            {
                                'error': 'invalid_column',
                                'message': f'Column must be between 1 and {new_rack.total_columns}.',
                            },
                            status=400,
                        )
                except (ValueError, TypeError):
                    return JsonResponse({'error': 'invalid_column', 'message': 'Invalid column number.'}, status=400)

        if old_rack != new_rack or old_col != new_col:
            job.rack = new_rack
            job.rack_column = new_col
            job.save(update_fields=['rack', 'rack_column', 'updated_at'])

            def _fmt_loc(r, c):
                if not r:
                    return 'None'
                if c:
                    return f"'{r.name}' - Col {c} ({r.group})"
                return f"'{r.name}' ({r.group})"

            old_desc = _fmt_loc(old_rack, old_col)
            new_desc = _fmt_loc(new_rack, new_col)
            JobTicketLog.objects.create(
                job_ticket=job,
                user=user,
                action='STATUS',
                details=f'Rack location changed from {old_desc} to {new_desc}.',
            )
            send_job_update_message(job.job_code, job.status)

    col_display = f' - Col {new_col}' if new_col else ''
    msg = (
        f'Device moved to {new_rack.name}{col_display} ({new_rack.group}).'
        if new_rack
        else 'Device unassigned from rack.'
    )

    return JsonResponse(
        {
            'ok': True,
            'message': msg,
            'rack_id': new_rack.id if new_rack else None,
            'rack_column': new_col,
            'rack_location': job.rack_location_display or '',
            'rack_short': job.rack_short_display or '',
        }
    )


@csrf_exempt
@require_POST
def mobile_api_job_notes(request, job_code):
    user, error_response = authenticate_mobile_request(request)
    if error_response:
        return error_response

    try:
        payload = json.loads((request.body or b'{}').decode('utf-8'))
    except (ValueError, json.JSONDecodeError, UnicodeDecodeError):
        return JsonResponse({'error': 'invalid_payload', 'message': 'Invalid JSON body.'}, status=400)

    new_notes = (payload.get('technician_notes') or '').strip()

    with transaction.atomic():
        job = get_object_or_404(JobTicket.objects.select_for_update(), job_code=job_code)

        permissions = get_mobile_job_permissions(user, job)
        if not permissions['can_access']:
            return JsonResponse(
                {'error': 'forbidden', 'message': 'You are not allowed to update this job.'},
                status=403,
            )

        old_notes = (job.technician_notes or '').strip()
        if old_notes == new_notes:
            return JsonResponse(
                {
                    'ok': True,
                    'message': 'No changes detected.',
                    'technician_notes': job.technician_notes or '',
                }
            )

        job.technician_notes = new_notes
        job.save(update_fields=['technician_notes', 'updated_at'])

        if new_notes:
            details = f'Technician notes updated: "{new_notes}"'
        else:
            details = 'Technician notes cleared.'
        JobTicketLog.objects.create(job_ticket=job, user=user, action='NOTE', details=details)

    send_job_update_message(job.job_code, job.status)
    return JsonResponse(
        {
            'ok': True,
            'message': 'Notes saved successfully.',
            'technician_notes': job.technician_notes or '',
        }
    )

@csrf_exempt
@require_POST
def mobile_api_service_line_create(request, job_code):
    user, error_response = authenticate_mobile_request(request)
    if error_response:
        return error_response

    try:
        payload = json.loads((request.body or b'{}').decode('utf-8'))
    except (ValueError, json.JSONDecodeError, UnicodeDecodeError):
        return JsonResponse({'error': 'invalid_payload', 'message': 'Invalid JSON body.'}, status=400)

    description = (payload.get('description') or '').strip()
    if not description:
        return JsonResponse({'error': 'missing_description', 'message': 'Description is required.'}, status=400)

    part_cost, part_error = _mobile_parse_decimal(payload.get('part_cost'), 'part cost')
    if part_error:
        return JsonResponse({'error': 'invalid_part_cost', 'message': part_error}, status=400)

    service_charge, service_error = _mobile_parse_decimal(payload.get('service_charge'), 'service charge')
    if service_error:
        return JsonResponse({'error': 'invalid_service_charge', 'message': service_error}, status=400)

    sales_invoice_number = (payload.get('sales_invoice_number') or '').strip()

    with transaction.atomic():
        job = get_object_or_404(JobTicket.objects.select_for_update(), job_code=job_code)

        if not mobile_can_manage_service_lines(user, job):
            return JsonResponse(
                {'error': 'forbidden', 'message': 'You are not allowed to manage service lines for this job.'},
                status=403,
            )

        created_line = ServiceLog.objects.create(
            job_ticket=job,
            description=description,
            part_cost=part_cost,
            service_charge=service_charge,
            sales_invoice_number=sales_invoice_number or None,
        )
        details = f"Added service: '{created_line.description}' (Part: Rs {part_cost}, Service: Rs {service_charge})"
        JobTicketLog.objects.create(job_ticket=job, user=user, action='SERVICE', details=details)

    send_job_update_message(job.job_code, job.status)
    return JsonResponse(
        {
            'ok': True,
            'message': 'Service line added successfully.',
            'service_line_id': created_line.id,
        }
    )

@csrf_exempt
@require_POST
def mobile_api_service_line_update(request, job_code, line_id):
    user, error_response = authenticate_mobile_request(request)
    if error_response:
        return error_response

    try:
        payload = json.loads((request.body or b'{}').decode('utf-8'))
    except (ValueError, json.JSONDecodeError, UnicodeDecodeError):
        return JsonResponse({'error': 'invalid_payload', 'message': 'Invalid JSON body.'}, status=400)

    with transaction.atomic():
        job = get_object_or_404(JobTicket.objects.select_for_update(), job_code=job_code)
        if not mobile_can_manage_service_lines(user, job):
            return JsonResponse(
                {'error': 'forbidden', 'message': 'You are not allowed to manage service lines for this job.'},
                status=403,
            )

        service_log = get_object_or_404(ServiceLog.objects.select_for_update(), id=line_id, job_ticket=job)

        if hasattr(service_log, 'product_sale'):
            return JsonResponse(
                {
                    'error': 'product_sale_locked',
                    'message': 'Product sale lines cannot be edited directly. Delete and re-add with the correct quantity.',
                },
                status=400,
            )

        new_description = (payload.get('description') or '').strip()
        if not new_description:
            return JsonResponse({'error': 'missing_description', 'message': 'Description is required.'}, status=400)

        new_part_cost, part_error = _mobile_parse_decimal(payload.get('part_cost'), 'part cost')
        if part_error:
            return JsonResponse({'error': 'invalid_part_cost', 'message': part_error}, status=400)

        new_service_charge, service_error = _mobile_parse_decimal(payload.get('service_charge'), 'service charge')
        if service_error:
            return JsonResponse({'error': 'invalid_service_charge', 'message': service_error}, status=400)

        new_invoice_number = (payload.get('sales_invoice_number') or '').strip()

        change_parts = []
        if service_log.description != new_description:
            change_parts.append(f"description: '{service_log.description}' -> '{new_description}'")
            service_log.description = new_description
        old_part_cost = service_log.part_cost or Decimal('0.00')
        if old_part_cost != new_part_cost:
            change_parts.append(f"part_cost: Rs {old_part_cost} -> Rs {new_part_cost}")
            service_log.part_cost = new_part_cost
        old_service_charge = service_log.service_charge or Decimal('0.00')
        if old_service_charge != new_service_charge:
            change_parts.append(f"service_charge: Rs {old_service_charge} -> Rs {new_service_charge}")
            service_log.service_charge = new_service_charge
        old_invoice_number = service_log.sales_invoice_number or ''
        if old_invoice_number != new_invoice_number:
            change_parts.append(f"sales_invoice_number: '{old_invoice_number}' -> '{new_invoice_number}'")
            service_log.sales_invoice_number = new_invoice_number or None

        if not change_parts:
            return JsonResponse({'ok': True, 'message': 'No changes detected.'})

        service_log.save(update_fields=['description', 'part_cost', 'service_charge', 'sales_invoice_number'])
        JobTicketLog.objects.create(job_ticket=job, user=user, action='SERVICE', details='; '.join(change_parts))

    send_job_update_message(job.job_code, job.status)
    return JsonResponse({'ok': True, 'message': 'Service line updated successfully.'})

@csrf_exempt
@require_http_methods(['POST', 'DELETE'])
def mobile_api_service_line_delete(request, job_code, line_id):
    user, error_response = authenticate_mobile_request(request)
    if error_response:
        return error_response

    with transaction.atomic():
        job = get_object_or_404(JobTicket.objects.select_for_update(), job_code=job_code)
        if not mobile_can_manage_service_lines(user, job):
            return JsonResponse(
                {'error': 'forbidden', 'message': 'You are not allowed to manage service lines for this job.'},
                status=403,
            )

        service_log = get_object_or_404(ServiceLog.objects.select_for_update(), id=line_id, job_ticket=job)
        line_description = service_log.description
        line_part_cost = service_log.part_cost or Decimal('0.00')
        line_service_charge = service_log.service_charge or Decimal('0.00')

        product_sale_entry = ProductSale.objects.filter(service_log=service_log).select_related('product').first()
        parsed_product_sale = parse_product_sale_log(service_log.description) if not product_sale_entry else None

        if product_sale_entry:
            product = Product.objects.select_for_update().filter(id=product_sale_entry.product_id).first()
            if product:
                product.stock_quantity += product_sale_entry.quantity
                product.save(update_fields=['stock_quantity', 'updated_at'])
                JobTicketLog.objects.create(
                    job_ticket=job,
                    user=user,
                    action='SERVICE',
                    details=(
                        f"Restocked {product.name} by {product_sale_entry.quantity} "
                        'after deleting product sale line.'
                    ),
                )
        elif parsed_product_sale:
            product = Product.objects.select_for_update().filter(id=parsed_product_sale['product_id']).first()
            if product:
                product.stock_quantity += parsed_product_sale['quantity']
                product.save(update_fields=['stock_quantity', 'updated_at'])
                JobTicketLog.objects.create(
                    job_ticket=job,
                    user=user,
                    action='SERVICE',
                    details=(
                        f"Restocked {product.name} by {parsed_product_sale['quantity']} "
                        'after deleting legacy product sale line.'
                    ),
                )

        service_log.delete()
        JobTicketLog.objects.create(
            job_ticket=job,
            user=user,
            action='SERVICE',
            details=(
                f"Deleted service: '{line_description}' "
                f"(Part: Rs {line_part_cost}, Service: Rs {line_service_charge})"
            ),
        )

    send_job_update_message(job.job_code, job.status)
    return JsonResponse({'ok': True, 'message': 'Service line deleted successfully.'})

@csrf_exempt
@require_http_methods(['GET', 'POST'])
def mobile_api_inventory_summary(request):
    user, error_response = authenticate_mobile_request(request)
    if error_response:
        return error_response
    workspace = _mobile_request_workspace(request, user)
    if not user.is_staff or not user_has_staff_access(user, "inventory"):
        return JsonResponse({'error': 'forbidden', 'message': 'Inventory access required.'}, status=403)

    metrics = _build_inventory_dashboard_metrics(workspace)
    return JsonResponse(
        {
            'summary': {
                'party_count': int(metrics['party_count']),
                'product_count': int(metrics['product_count']),
                'low_stock_count': int(metrics['low_stock_count']),
                'reserved_alert_count': int(metrics['reserved_alert_count']),
                'inventory_value': _money_text(metrics['inventory_value']),
                'monthly_purchase_total': _money_text(metrics['monthly_purchase_total']),
                'monthly_purchase_return_total': _money_text(metrics['monthly_purchase_return_total']),
                'monthly_sales_total': _money_text(metrics['monthly_sales_total']),
                'monthly_sales_return_total': _money_text(metrics['monthly_sales_return_total']),
                'monthly_purchase_qty': int(metrics['monthly_purchase_qty']),
                'monthly_purchase_return_qty': int(metrics['monthly_purchase_return_qty']),
                'monthly_sales_qty': int(metrics['monthly_sales_qty']),
                'monthly_sales_return_qty': int(metrics['monthly_sales_return_qty']),
                'monthly_stock_in_qty': int(metrics['monthly_stock_in_qty']),
                'monthly_stock_out_qty': int(metrics['monthly_stock_out_qty']),
                'monthly_net_stock_qty': int(metrics['monthly_net_stock_qty']),
                'monthly_stock_in_amount': _money_text(metrics['monthly_stock_in_amount']),
                'monthly_stock_out_amount': _money_text(metrics['monthly_stock_out_amount']),
                'monthly_net_amount': _money_text(metrics['monthly_net_amount']),
            },
            'reserved_stock_products': [
                {
                    'id': product.id,
                    'name': product.name,
                    'category': product.category or '',
                    'stock_quantity': int(product.stock_quantity or 0),
                    'reserved_stock': int(product.reserved_stock or 0),
                }
                for product in metrics['reserved_stock_products']
            ],
            'recent_entries': [
                {
                    'id': entry.id,
                    'entry_number': entry.entry_number,
                    'entry_type': entry.entry_type,
                    'entry_type_label': entry.get_entry_type_display(),
                    'entry_date': entry.entry_date.isoformat() if entry.entry_date else '',
                    'invoice_number': entry.invoice_number or '',
                    'party_name': entry.party.name if entry.party_id else '',
                    'product_name': entry.product.name if entry.product_id else '',
                    'quantity': int(entry.quantity or 0),
                    'total_amount': _money_text(entry.total_amount),
                    'stock_before': int(entry.stock_before or 0),
                    'stock_after': int(entry.stock_after or 0),
                }
                for entry in metrics['recent_entries']
            ],
        }
    )

@csrf_exempt
@require_http_methods(['GET', 'POST'])
def mobile_api_inventory_parties(request):
    user, error_response = authenticate_mobile_request(request)
    if error_response:
        return error_response
    workspace = _mobile_request_workspace(request, user)
    if not user.is_staff or not user_has_staff_access(user, "inventory"):
        return JsonResponse({'error': 'forbidden', 'message': 'Inventory access required.'}, status=403)

    if request.method == 'GET':
        query = (request.GET.get('q') or '').strip()
        start_date = None
        end_date = None
        start_date_raw = (request.GET.get('start_date') or '').strip()
        end_date_raw = (request.GET.get('end_date') or '').strip()

        if start_date_raw:
            try:
                start_date = datetime.strptime(start_date_raw, '%Y-%m-%d').date()
            except ValueError:
                return JsonResponse({'error': 'invalid_start_date', 'message': 'Start date must be YYYY-MM-DD.'}, status=400)
        if end_date_raw:
            try:
                end_date = datetime.strptime(end_date_raw, '%Y-%m-%d').date()
            except ValueError:
                return JsonResponse({'error': 'invalid_end_date', 'message': 'End date must be YYYY-MM-DD.'}, status=400)
        if start_date and end_date and start_date > end_date:
            start_date, end_date = end_date, start_date

        directory = _build_inventory_party_directory(
            query=query,
            start_date=start_date,
            end_date=end_date,
            workspace=workspace,
        )
        return JsonResponse(
            {
                'summary': {
                    'total_parties': int(directory['total_parties']),
                    'supplier_count': int(directory['supplier_count']),
                    'customer_count': int(directory['customer_count']),
                    'legacy_both_count': int(directory['legacy_both_count']),
                },
                'query': query,
                'start_date': start_date.isoformat() if start_date else '',
                'end_date': end_date.isoformat() if end_date else '',
                'parties': [_serialize_inventory_party_for_api(party) for party in directory['parties']],
                'suppliers': [_serialize_inventory_party_for_api(party) for party in directory['suppliers']],
                'customers': [_serialize_inventory_party_for_api(party) for party in directory['customers']],
                'can_edit': bool(user.is_staff and user_has_staff_access(user, "inventory")),
            }
        )

    try:
        payload = json.loads((request.body or b'{}').decode('utf-8'))
    except (ValueError, json.JSONDecodeError, UnicodeDecodeError):
        return JsonResponse({'error': 'invalid_payload', 'message': 'Invalid JSON body.'}, status=400)

    form_payload = _inventory_party_form_payload_from_api(payload)

    party_form = InventoryPartyForm(form_payload)
    if not party_form.is_valid():
        return JsonResponse(
            {'error': 'invalid_payload', 'message': 'Party data is invalid.', 'errors': _inventory_form_errors(party_form)},
            status=400,
        )

    party = party_form.save(commit=False)
    party.workspace = workspace
    party.save()
    return JsonResponse(
        {
            'ok': True,
            'message': f"Party '{party.name}' created successfully.",
            'party': _serialize_inventory_party_for_api(party),
        }
    )

@csrf_exempt
@require_http_methods(['POST', 'PATCH'])
def mobile_api_inventory_party_update(request, party_id):
    user, error_response = authenticate_mobile_request(request)
    if error_response:
        return error_response
    workspace = _mobile_request_workspace(request, user)
    if not user.is_staff or not user_has_staff_access(user, "inventory"):
        return JsonResponse({'error': 'forbidden', 'message': 'Inventory access required.'}, status=403)

    party = get_object_or_404(
        scope_to_workspace(InventoryParty.objects.all(), workspace),
        id=party_id,
    )

    try:
        payload = json.loads((request.body or b'{}').decode('utf-8'))
    except (ValueError, json.JSONDecodeError, UnicodeDecodeError):
        return JsonResponse({'error': 'invalid_payload', 'message': 'Invalid JSON body.'}, status=400)

    form_payload = _inventory_party_form_payload_from_api(payload)
    party_form = InventoryPartyForm(form_payload, instance=party)
    if not party_form.is_valid():
        return JsonResponse(
            {'error': 'invalid_payload', 'message': 'Party data is invalid.', 'errors': _inventory_form_errors(party_form)},
            status=400,
        )

    updated_party = party_form.save()

    return JsonResponse(
        {
            'ok': True,
            'message': f"Party '{updated_party.name}' updated successfully.",
            'party': _serialize_inventory_party_for_api(updated_party),
        }
    )

@csrf_exempt
@require_GET
def mobile_api_inventory_register(request, entry_type):
    user, error_response = authenticate_mobile_request(request)
    if error_response:
        return error_response
    if not user.is_staff or not user_has_staff_access(user, "inventory"):
        return JsonResponse({'error': 'forbidden', 'message': 'Inventory access required.'}, status=403)
    if entry_type not in INVENTORY_ENTRY_CONFIG:
        return JsonResponse({'error': 'invalid_entry_type', 'message': 'Unsupported inventory register.'}, status=404)

    query = (request.GET.get('q') or '').strip()
    source_bill_id = None
    source_party_id = None
    source_bill_id_raw = (request.GET.get('source_bill_id') or '').strip()
    source_party_id_raw = (request.GET.get('source_party_id') or '').strip()
    if source_bill_id_raw.isdigit():
        source_bill_id = int(source_bill_id_raw)
    if source_party_id_raw.isdigit():
        source_party_id = int(source_party_id_raw)

    snapshot = _build_inventory_register_snapshot(
        entry_type,
        query=query,
        source_bill_id=source_bill_id,
        source_invoice=(request.GET.get('source_invoice') or '').strip(),
        source_party_id=source_party_id,
    )
    config = snapshot['config']

    return JsonResponse(
        {
            'config': {
                'entry_type': entry_type,
                'title': config['title'],
                'description': config['description'],
                'icon': config['icon'],
                'submit_label': config['submit_label'],
                'party_label': config['party_label'],
                'legacy_url': reverse(config['url_name']),
            },
            'summary': {
                'entry_count': int(snapshot['entry_count']),
                'total_quantity': int(snapshot['total_quantity'] or 0),
                'total_amount': _money_text(snapshot['total_amount']),
            },
            'query': query,
            'show_add_entry_button': bool(snapshot['show_add_entry_button']),
            'register_rows': [
                _serialize_inventory_register_bill_for_api(bill)
                for bill in snapshot['register_rows']
            ],
            'source_bill': snapshot['source_bill'] or None,
            'source_bill_lines': [
                {
                    'product_id': int(line['product_id']),
                    'product_name': line['product_name'],
                    'product_stock': int(line['product_stock']),
                    'max_quantity': int(line['max_quantity']),
                    'unit_price': _money_text(line['unit_price']),
                    'gst_rate': _money_text(line['gst_rate']),
                }
                for line in snapshot['source_bill_lines']
            ],
        }
    )

@csrf_exempt
@require_http_methods(['GET', 'POST'])
def mobile_api_products(request):
    user, error_response = authenticate_mobile_request(request)
    if error_response:
        return error_response
    workspace = _mobile_request_workspace(request, user)
    if user.is_staff and not user_has_staff_access(user, "inventory"):
        return JsonResponse({'error': 'forbidden', 'message': 'Inventory access required.'}, status=403)

    if request.method == 'GET':
        query = (request.GET.get('q') or '').strip()
        catalog = _build_inventory_product_catalog(query=query, workspace=workspace)
        products_data = [
            _serialize_inventory_product_for_api(product)
            for product in catalog['products'][:200]
        ]

        return JsonResponse(
            {
                'count': len(products_data),
                'filtered_count': int(catalog['filtered_count']),
                'summary': {
                    'total_products': int(catalog['total_products']),
                    'out_of_stock_count': int(catalog['out_of_stock_count']),
                    'reserved_alert_count': int(catalog['reserved_alert_count']),
                },
                'products': products_data,
                'can_edit': bool(user.is_staff and user_has_staff_access(user, "inventory")),
            }
        )

    if not user.is_staff or not user_has_staff_access(user, "inventory"):
        return JsonResponse({'error': 'forbidden', 'message': 'Only staff can create products.'}, status=403)

    try:
        payload = json.loads((request.body or b'{}').decode('utf-8'))
    except (ValueError, json.JSONDecodeError, UnicodeDecodeError):
        return JsonResponse({'error': 'invalid_payload', 'message': 'Invalid JSON body.'}, status=400)

    sku = (payload.get('sku') or '').strip()
    products_scope = scope_to_workspace(
        Product.objects.all(),
        workspace,
    )
    if sku and products_scope.filter(sku__iexact=sku).exists():
        return JsonResponse({'error': 'duplicate_sku', 'message': 'SKU already exists.'}, status=400)

    try:
        default_gst_rate = CompanyProfile.get_profile().gst_rate or Decimal('18.00')
    except Exception:
        default_gst_rate = Decimal('18.00')

    form_payload = {
        'name': (payload.get('name') or '').strip(),
        'category': (payload.get('category') or '').strip(),
        'brand': (payload.get('brand') or '').strip(),
        'item_type': (payload.get('item_type') or 'goods').strip() or 'goods',
        'hsn_sac_code': (payload.get('hsn_sac_code') or '').strip(),
        'uqc': (payload.get('uqc') or 'NOS').strip() or 'NOS',
        'tax_category': (payload.get('tax_category') or 'taxable').strip() or 'taxable',
        'gst_rate': str(payload.get('gst_rate') or default_gst_rate),
        'cess_rate': str(payload.get('cess_rate') or '0.00'),
        'is_tax_inclusive_default': 'on' if _mobile_parse_bool(payload.get('is_tax_inclusive_default')) else '',
        'cost_price': str(payload.get('cost_price') or '0.00'),
        'unit_price': str(payload.get('unit_price') or '0.00'),
        'stock_quantity': str(payload.get('stock_quantity') or '0'),
        'reserved_stock': str(payload.get('reserved_stock') or '0'),
        'description': (payload.get('description') or '').strip(),
        'purchase_price_tax_mode': (payload.get('purchase_price_tax_mode') or 'without_tax').strip() or 'without_tax',
        'sales_price_tax_mode': (payload.get('sales_price_tax_mode') or 'without_tax').strip() or 'without_tax',
    }
    product_form = ProductForm(form_payload)
    if not product_form.is_valid():
        return JsonResponse(
            {
                'error': 'invalid_product',
                'message': 'Please fix the product fields and try again.',
                'errors': _inventory_form_errors(product_form),
            },
            status=400,
        )

    product = product_form.save(commit=False)
    product.sku = sku or None
    product.is_active = _mobile_parse_bool(payload.get('is_active', True))
    product.workspace = workspace
    effective_rate = effective_tax_rate(
        product_form.cleaned_data.get('gst_rate'),
        product_form.cleaned_data.get('tax_category'),
    )
    product.cost_price = _normalize_tax_mode_price(
        product_form.cleaned_data.get('cost_price'),
        product_form.cleaned_data.get('purchase_price_tax_mode'),
        effective_rate,
    )
    product.unit_price = _normalize_tax_mode_price(
        product_form.cleaned_data.get('unit_price'),
        product_form.cleaned_data.get('sales_price_tax_mode'),
        effective_rate,
    )
    product.save()
    return JsonResponse(
        {
            'ok': True,
            'message': f"Product '{product.name}' created successfully.",
            'product_id': product.id,
            'product': _serialize_inventory_product_for_api(product),
        }
    )

@csrf_exempt
@require_POST
def mobile_api_product_update(request, product_id):
    user, error_response = authenticate_mobile_request(request)
    if error_response:
        return error_response
    workspace = _mobile_request_workspace(request, user)
    if not user.is_staff or not user_has_staff_access(user, "inventory"):
        return JsonResponse({'error': 'forbidden', 'message': 'Only staff can update products.'}, status=403)

    try:
        payload = json.loads((request.body or b'{}').decode('utf-8'))
    except (ValueError, json.JSONDecodeError, UnicodeDecodeError):
        return JsonResponse({'error': 'invalid_payload', 'message': 'Invalid JSON body.'}, status=400)

    products_scope = scope_to_workspace(
        Product.objects.all(),
        workspace,
    )
    product = get_object_or_404(
        products_scope,
        pk=product_id,
    )
    sku = (payload.get('sku') or product.sku or '').strip()
    if sku and products_scope.exclude(pk=product.pk).filter(sku__iexact=sku).exists():
        return JsonResponse({'error': 'duplicate_sku', 'message': 'SKU already exists.'}, status=400)

    form_payload = {
        'name': (payload.get('name') or product.name or '').strip(),
        'category': (payload.get('category') or product.category or '').strip(),
        'brand': (payload.get('brand') or product.brand or '').strip(),
        'item_type': (payload.get('item_type') or product.item_type or 'goods').strip() or 'goods',
        'hsn_sac_code': (payload.get('hsn_sac_code') or product.hsn_sac_code or '').strip(),
        'uqc': (payload.get('uqc') or product.uqc or 'NOS').strip() or 'NOS',
        'tax_category': (payload.get('tax_category') or product.tax_category or 'taxable').strip() or 'taxable',
        'gst_rate': str(payload.get('gst_rate') if 'gst_rate' in payload else product.gst_rate),
        'cess_rate': str(payload.get('cess_rate') if 'cess_rate' in payload else product.cess_rate),
        'is_tax_inclusive_default': (
            'on'
            if _mobile_parse_bool(
                payload.get('is_tax_inclusive_default', product.is_tax_inclusive_default)
            )
            else ''
        ),
        'cost_price': str(payload.get('cost_price') if 'cost_price' in payload else product.cost_price),
        'unit_price': str(payload.get('unit_price') if 'unit_price' in payload else product.unit_price),
        'stock_quantity': str(payload.get('stock_quantity') if 'stock_quantity' in payload else product.stock_quantity),
        'reserved_stock': str(payload.get('reserved_stock') if 'reserved_stock' in payload else product.reserved_stock),
        'description': (payload.get('description') or product.description or '').strip(),
        'purchase_price_tax_mode': (payload.get('purchase_price_tax_mode') or 'without_tax').strip() or 'without_tax',
        'sales_price_tax_mode': (payload.get('sales_price_tax_mode') or 'without_tax').strip() or 'without_tax',
    }
    original_values = {
        'name': product.name,
        'sku': product.sku,
        'category': product.category,
        'brand': product.brand,
        'item_type': product.item_type,
        'hsn_sac_code': product.hsn_sac_code,
        'uqc': product.uqc,
        'tax_category': product.tax_category,
        'gst_rate': product.gst_rate,
        'cess_rate': product.cess_rate,
        'is_tax_inclusive_default': bool(product.is_tax_inclusive_default),
        'cost_price': product.cost_price,
        'unit_price': product.unit_price,
        'stock_quantity': int(product.stock_quantity or 0),
        'reserved_stock': int(product.reserved_stock or 0),
        'description': product.description,
        'is_active': bool(product.is_active),
    }
    product_form = ProductForm(form_payload, instance=product)
    if not product_form.is_valid():
        return JsonResponse(
            {
                'error': 'invalid_product',
                'message': 'Please fix the product fields and try again.',
                'errors': _inventory_form_errors(product_form),
            },
            status=400,
        )

    updated_product = product_form.save(commit=False)
    effective_rate = effective_tax_rate(
        product_form.cleaned_data.get('gst_rate'),
        product_form.cleaned_data.get('tax_category'),
    )
    updated_values = {
        'name': updated_product.name,
        'sku': sku or None,
        'category': updated_product.category,
        'brand': updated_product.brand,
        'item_type': updated_product.item_type,
        'hsn_sac_code': updated_product.hsn_sac_code,
        'uqc': updated_product.uqc,
        'tax_category': updated_product.tax_category,
        'gst_rate': updated_product.gst_rate,
        'cess_rate': updated_product.cess_rate,
        'is_tax_inclusive_default': bool(updated_product.is_tax_inclusive_default),
        'cost_price': _normalize_tax_mode_price(
            product_form.cleaned_data.get('cost_price'),
            product_form.cleaned_data.get('purchase_price_tax_mode'),
            effective_rate,
        ),
        'unit_price': _normalize_tax_mode_price(
            product_form.cleaned_data.get('unit_price'),
            product_form.cleaned_data.get('sales_price_tax_mode'),
            effective_rate,
        ),
        'stock_quantity': int(updated_product.stock_quantity or 0),
        'reserved_stock': int(updated_product.reserved_stock or 0),
        'description': updated_product.description,
        'is_active': _mobile_parse_bool(payload.get('is_active', product.is_active)),
    }

    update_fields = []
    for field_name, new_value in updated_values.items():
        if original_values[field_name] != new_value:
            setattr(product, field_name, new_value)
            update_fields.append(field_name)

    if not update_fields:
        return JsonResponse({'ok': True, 'message': 'No changes detected.'})

    update_fields.append('updated_at')
    product.save(update_fields=update_fields)
    return JsonResponse(
        {
            'ok': True,
            'message': f"Product '{product.name}' updated successfully.",
            'product': _serialize_inventory_product_for_api(product),
        }
    )

@csrf_exempt
@require_http_methods(['GET', 'POST'])
def mobile_api_clients(request):
    user, error_response = authenticate_mobile_request(request)
    if error_response:
        return error_response
    workspace = _mobile_request_workspace(request, user)
    if user.is_staff and not user_has_staff_access(user, "staff_dashboard"):
        return JsonResponse({'error': 'forbidden', 'message': 'Staff dashboard access required.'}, status=403)

    if request.method == 'GET':
        query = (request.GET.get('q') or '').strip()
        clients_qs = scope_to_workspace(
            Client.objects.all(),
            workspace,
        ).order_by('name')
        if query:
            clients_qs = clients_qs.filter(
                Q(name__icontains=query)
                | Q(phone__icontains=query)
                | Q(email__icontains=query)
                | Q(company_name__icontains=query)
            )

        clients_data = []
        for client in clients_qs[:200]:
            clients_data.append(
                {
                    'id': client.id,
                    'name': client.name,
                    'phone': client.phone,
                    'email': client.email or '',
                    'company_name': client.company_name or '',
                    'address': client.address or '',
                    'notes': client.notes or '',
                    'is_active': bool(client.is_active),
                    'updated_at': timezone.localtime(client.updated_at).strftime('%Y-%m-%d %H:%M'),
                }
            )

        return JsonResponse(
            {
                'count': len(clients_data),
                'clients': clients_data,
                'can_edit': bool(user.is_staff and user_has_staff_access(user, "staff_dashboard")),
            }
        )

    if not user.is_staff or not user_has_staff_access(user, "staff_dashboard"):
        return JsonResponse({'error': 'forbidden', 'message': 'Only staff can create clients.'}, status=403)

    try:
        payload = json.loads((request.body or b'{}').decode('utf-8'))
    except (ValueError, json.JSONDecodeError, UnicodeDecodeError):
        return JsonResponse({'error': 'invalid_payload', 'message': 'Invalid JSON body.'}, status=400)

    name = (payload.get('name') or '').strip()
    phone, phone_error = normalize_indian_phone(payload.get('phone'), field_label='Phone number')
    if not name:
        return JsonResponse({'error': 'missing_name', 'message': 'Client name is required.'}, status=400)
    if phone_error:
        return JsonResponse({'error': 'invalid_phone', 'message': phone_error}, status=400)
    clients_scope = scope_to_workspace(
        Client.objects.all(),
        workspace,
    )
    if clients_scope.filter(phone__in=phone_lookup_variants(phone)).exists():
        return JsonResponse({'error': 'duplicate_phone', 'message': 'A client with this phone already exists.'}, status=400)

    client = Client.objects.create(
        workspace=workspace,
        name=name,
        phone=phone,
        email=(payload.get('email') or '').strip(),
        company_name=(payload.get('company_name') or '').strip(),
        address=(payload.get('address') or '').strip(),
        notes=(payload.get('notes') or '').strip(),
        is_active=_mobile_parse_bool(payload.get('is_active', True)),
    )
    return JsonResponse(
        {
            'ok': True,
            'message': f"Client '{client.name}' created successfully.",
            'client_id': client.id,
        }
    )

@csrf_exempt
@require_POST
def mobile_api_client_update(request, client_id):
    user, error_response = authenticate_mobile_request(request)
    if error_response:
        return error_response
    if not user.is_staff or not user_has_staff_access(user, "staff_dashboard"):
        return JsonResponse({'error': 'forbidden', 'message': 'Only staff can update clients.'}, status=403)

    workspace = _mobile_request_workspace(request, user)
    clients_scope = scope_to_workspace(Client.objects.all(), workspace)

    try:
        payload = json.loads((request.body or b'{}').decode('utf-8'))
    except (ValueError, json.JSONDecodeError, UnicodeDecodeError):
        return JsonResponse({'error': 'invalid_payload', 'message': 'Invalid JSON body.'}, status=400)

    client = get_object_or_404(
        clients_scope,
        pk=client_id,
    )
    client._previous_phone = client.phone
    client._previous_name = client.name
    update_fields = []

    if 'name' in payload:
        name = (payload.get('name') or '').strip()
        if not name:
            return JsonResponse({'error': 'missing_name', 'message': 'Client name is required.'}, status=400)
        if client.name != name:
            client.name = name
            update_fields.append('name')

    if 'phone' in payload:
        phone, phone_error = normalize_indian_phone(payload.get('phone'), field_label='Phone number')
        if phone_error:
            return JsonResponse({'error': 'invalid_phone', 'message': phone_error}, status=400)
        if clients_scope.exclude(id=client.id).filter(phone__in=phone_lookup_variants(phone)).exists():
            return JsonResponse({'error': 'duplicate_phone', 'message': 'A client with this phone already exists.'}, status=400)
        if client.phone != phone:
            client.phone = phone
            update_fields.append('phone')

    for text_field in ('email', 'company_name', 'address', 'notes'):
        if text_field in payload:
            new_value = (payload.get(text_field) or '').strip()
            if getattr(client, text_field) != new_value:
                setattr(client, text_field, new_value)
                update_fields.append(text_field)

    if 'is_active' in payload:
        is_active = _mobile_parse_bool(payload.get('is_active'))
        if client.is_active != is_active:
            client.is_active = is_active
            update_fields.append('is_active')

    if not update_fields:
        return JsonResponse({'ok': True, 'message': 'No changes detected.'})

    update_fields.append('updated_at')
    client.save(update_fields=update_fields)
    return JsonResponse({'ok': True, 'message': f"Client '{client.name}' updated successfully."})

@require_http_methods(['GET'])
def mobile_api_pending_approvals(request):
    user, error_response = authenticate_mobile_request(request)
    if error_response:
        return error_response
    if user.is_staff and not user_has_staff_access(user, "staff_dashboard"):
        return JsonResponse({'error': 'forbidden', 'message': 'Staff dashboard access required.'}, status=403)

    is_technician = hasattr(user, 'technician_profile')
    can_act = bool(is_technician and not user.is_staff)

    if user.is_staff:
        approvals_qs = Assignment.objects.filter(status='pending').select_related('job', 'technician__user').order_by('-created_at')
    elif is_technician:
        approvals_qs = Assignment.objects.filter(
            status='pending',
            technician=user.technician_profile,
        ).select_related('job', 'technician__user').order_by('-created_at')
    else:
        approvals_qs = Assignment.objects.none()

    approvals_data = []
    for assignment in approvals_qs[:200]:
        job = assignment.job
        approvals_data.append(
            {
                'id': assignment.id,
                'job_code': job.job_code,
                'job_status': job.status,
                'customer_name': job.customer_name,
                'customer_phone': job.customer_phone,
                'device': f"{job.device_type} {job.device_brand or ''} {job.device_model or ''}".strip(),
                'technician': assignment.technician.user.username if assignment.technician and assignment.technician.user else '',
                'created_at': timezone.localtime(assignment.created_at).strftime('%Y-%m-%d %H:%M'),
            }
        )

    return JsonResponse({'count': len(approvals_data), 'can_act': can_act, 'approvals': approvals_data})

@csrf_exempt
@require_POST
def mobile_api_pending_approval_action(request, assignment_id):
    user, error_response = authenticate_mobile_request(request)
    if error_response:
        return error_response

    if not hasattr(user, 'technician_profile'):
        return JsonResponse({'error': 'forbidden', 'message': 'Only technicians can respond to approvals.'}, status=403)

    try:
        payload = json.loads((request.body or b'{}').decode('utf-8'))
    except (ValueError, json.JSONDecodeError, UnicodeDecodeError):
        return JsonResponse({'error': 'invalid_payload', 'message': 'Invalid JSON body.'}, status=400)

    action_key = (payload.get('action') or '').strip().lower()
    note = (payload.get('note') or '').strip()
    if action_key not in {'accept', 'reject'}:
        return JsonResponse({'error': 'invalid_action', 'message': 'Action must be accept or reject.'}, status=400)

    with transaction.atomic():
        assignment = get_object_or_404(
            Assignment.objects.select_for_update(),
            pk=assignment_id,
        )

        if assignment.technician_id != user.technician_profile.id:
            return JsonResponse({'error': 'forbidden', 'message': 'You are not assigned to this approval.'}, status=403)
        if assignment.status != 'pending':
            return JsonResponse({'error': 'already_responded', 'message': 'Approval already responded.'}, status=400)

        if action_key == 'accept':
            assignment.accept(note=note)
            details = f"Assignment accepted by technician '{user.username}'."
            message = 'Approval accepted.'
        else:
            assignment.reject(note=note)
            details = f"Assignment rejected by technician '{user.username}'."
            message = 'Approval rejected.'

        if note:
            details = f'{details} Note: {note}'

        JobTicketLog.objects.create(job_ticket=assignment.job, user=user, action='ASSIGNED', details=details)
        assignment.job.refresh_from_db(fields=['status', 'updated_at'])

    send_job_update_message(assignment.job.job_code, assignment.job.status)
    return JsonResponse(
        {
            'ok': True,
            'message': message,
            'job_code': assignment.job.job_code,
            'job_status': assignment.job.status,
            'job_status_display': assignment.job.get_status_display(),
        }
    )

@require_http_methods(['GET'])
def mobile_api_reports_summary(request):
    user, error_response = authenticate_mobile_request(request)
    if error_response:
        return error_response
    if not user.is_staff or not user_has_staff_access(user, "reports_financial"):
        return JsonResponse({'error': 'forbidden', 'message': 'Only staff can access reports.'}, status=403)

    period = resolve_monthly_summary_period(request)
    report_context = get_monthly_summary_context(
        period['start_of_period'],
        period['end_of_period'],
        period['start_date_str'],
        period['end_date_str'],
        preset=period['preset'],
        show_jobs='',
    )

    top_products = []
    for row in report_context['stock_products_breakdown'][:8]:
        top_products.append(
            {
                'product_id': row['product_id'],
                'name': row['name'],
                'sku': row['sku'],
                'units_sold': row['units_sold'],
                'sale_lines': row['sale_lines'],
                'average_unit_price': str(row['average_unit_price']),
                'revenue': str(row['revenue']),
                'cogs': str(row['cogs']),
                'profit': str(row['profit']),
            }
        )

    return JsonResponse(
        {
            'period': {
                'start_date': period['start_date_str'],
                'end_date': period['end_date_str'],
                'preset': period['preset'],
            },
            'summary': {
                'jobs_created': report_context['jobs_created_count'],
                'jobs_finished': report_context['jobs_finished_count'],
                'jobs_closed': report_context['jobs_closed_count'],
                'current_month_in_closed': report_context['current_month_in_closed_count'],
                'current_month_in_closed_revenue': str(report_context['current_month_in_closed_revenue']),
                'current_month_in_closed_expense': str(report_context['current_month_in_closed_expense']),
                'current_month_in_closed_profit': str(report_context['current_month_in_closed_profit']),
                'previous_month_in_closed': report_context['previous_month_in_closed_count'],
                'previous_month_in_closed_revenue': str(report_context['previous_month_in_closed_revenue']),
                'previous_month_in_closed_expense': str(report_context['previous_month_in_closed_expense']),
                'previous_month_in_closed_profit': str(report_context['previous_month_in_closed_profit']),
                'pending_completed_count': report_context['pending_completed_count'],
                'pending_completed_value': str(report_context['pending_completed_value']),
                'ready_for_pickup_count': report_context['ready_for_pickup_count'],
                'ready_for_pickup_value': str(report_context['ready_for_pickup_value']),
                'closed_receivable_bill_count': report_context['closed_receivable_bill_count'],
                'closed_receivable_balance_count': report_context['closed_receivable_balance_count'],
                'closed_receivable_bill_total': str(report_context['closed_receivable_bill_total']),
                'closed_receivable_paid': str(report_context['closed_receivable_paid']),
                'closed_receivable_balance': str(report_context['closed_receivable_balance']),
                'jobs_returned': report_context['jobs_returned_count'],
                'vendor_jobs': report_context['vendor_jobs_count'],
                'overall_revenue': str(report_context['overall_revenue']),
                'overall_expense': str(report_context['overall_expense']),
                'overall_profit': str(report_context['overall_profit']),
                'overall_margin': str(report_context['overall_margin']),
                'service_revenue': str(report_context['service_revenue']),
                'service_profit': str(report_context['service_profit']),
                'stock_sales_income': str(report_context['stock_sales_income']),
                'stock_sales_cogs': str(report_context['stock_sales_cogs']),
                'stock_sales_profit': str(report_context['stock_sales_profit']),
                'stock_sales_units': report_context['stock_sales_units'],
                'stock_products_count': report_context['stock_products_count'],
                'vendor_revenue': str(report_context['vendor_revenue']),
                'vendor_expense': str(report_context['vendor_expense']),
                'vendor_profit': str(report_context['vendor_profit']),
            },
            'top_products': top_products,
        }
    )


# ---------------------------------------------------------------------------
# Mobile Task API Endpoints (Phase 5)
# ---------------------------------------------------------------------------

def _serialize_task_for_mobile(task, user=None, detailed=False):
    """Serialize a Task for the Flutter mobile app."""
    is_tech = hasattr(user, 'technician_profile') if user else False
    is_mine = False
    if user:
        if is_tech and task.assigned_to_id == user.technician_profile.id:
            is_mine = True
        elif task.created_by_id == user.id:
            is_mine = True

    is_overdue = bool(
        task.due_date and
        task.due_date < timezone.now() and
        task.status in [Task.STATUS_OPEN, Task.STATUS_IN_PROGRESS]
    )

    tech_id = getattr(getattr(user, 'technician_profile', None), 'id', None)
    can_accept = bool(is_tech and task.is_claimable and (not task.assigned_to_id or task.assigned_to_id != tech_id))

    data = {
        'id': task.id,
        'title': task.title,
        'description': task.description,
        'priority': task.priority,
        'priority_display': task.get_priority_display(),
        'status': task.status,
        'status_display': task.get_status_display(),
        'due_date': timezone.localtime(task.due_date).strftime('%Y-%m-%d %H:%M') if task.due_date else '',
        'is_overdue': is_overdue,
        'is_mine': is_mine,
        'is_open_to_all': task.is_open_to_all,
        'has_alarm': task.has_alarm,
        'alarm_time': timezone.localtime(task.alarm_time).strftime('%Y-%m-%d %H:%M') if task.alarm_time else '',
        'is_claimable': task.is_claimable,
        'can_accept': can_accept,
        'created_at': timezone.localtime(task.created_at).strftime('%Y-%m-%d %H:%M'),
        'completed_at': timezone.localtime(task.completed_at).strftime('%Y-%m-%d %H:%M') if task.completed_at else '',
        'created_by': task.created_by.username if task.created_by else 'System',
        'assigned_to': task.assigned_to.user.username if task.assigned_to and task.assigned_to.user else '',
        'assigned_to_id': task.assigned_to_id,
        'assigned_to_name': (task.assigned_to.user.get_full_name() or task.assigned_to.user.username) if task.assigned_to and task.assigned_to.user else '',
        'job_reference': {
            'id': task.job_reference.id,
            'job_code': task.job_reference.job_code,
            'status': task.job_reference.status,
            'customer_name': task.job_reference.customer_name,
            'device': f"{task.job_reference.device_type} {task.job_reference.device_brand or ''}".strip(),
        } if task.job_reference else None,
        'attachments_count': task.attachments.count(),
        'messages_count': task.messages.count(),
    }
    if detailed:
        data['attachments'] = [
            {
                'id': att.id,
                'file_name': att.file_name or (att.file.name.split('/')[-1] if att.file else f"attachment_{att.id}"),
                'file_size': att.file_size,
                'file_url': att.file.url if att.file else '',
                'uploaded_at': timezone.localtime(att.uploaded_at).strftime('%Y-%m-%d %H:%M'),
                'uploaded_by': att.uploaded_by.username if att.uploaded_by else '',
            }
            for att in task.attachments.all()
        ]
        data['messages'] = [
            {
                'id': msg.id,
                'sender': msg.sender.username,
                'sender_is_self': bool(user and msg.sender_id == user.id),
                'body': msg.body,
                'sent_at': timezone.localtime(msg.sent_at).strftime('%Y-%m-%d %H:%M'),
            }
            for msg in task.messages.select_related('sender').all()
        ]
    return data


@csrf_exempt
def mobile_api_tasks(request):
    """List or create standalone tasks for technician or staff."""
    user, error_response = authenticate_mobile_request(request)
    if error_response:
        return error_response

    workspace = _mobile_request_workspace(request, user)
    is_tech = hasattr(user, 'technician_profile')

    if request.method == 'POST':
        # Create Task from mobile: staff only (technicians cannot create tasks)
        if not user.is_staff or not user_has_staff_access(user, "task_management"):
            return JsonResponse({'error': 'forbidden', 'message': 'Only staff with task management permission can create tasks.'}, status=403)

        try:
            payload = json.loads((request.body or b'{}').decode('utf-8'))
        except (ValueError, json.JSONDecodeError, UnicodeDecodeError):
            payload = request.POST

        title = (payload.get('title') or '').strip()
        if not title:
            return JsonResponse({'error': 'missing_title', 'message': 'Task title is required.'}, status=400)

        description = (payload.get('description') or '').strip()
        priority = (payload.get('priority') or Task.PRIORITY_MEDIUM).strip().lower()
        if priority not in dict(Task.PRIORITY_CHOICES):
            priority = Task.PRIORITY_MEDIUM

        due_date_str = (payload.get('due_date') or '').strip()
        due_date = None
        if due_date_str:
            try:
                from django.utils.dateparse import parse_datetime
                due_date = parse_datetime(due_date_str)
                if not due_date:
                    from datetime import datetime
                    due_date = datetime.strptime(due_date_str[:16], '%Y-%m-%d %H:%M')
                if due_date and timezone.is_naive(due_date):
                    due_date = timezone.make_aware(due_date)
            except Exception:
                due_date = None

        is_open_to_all = bool(payload.get('is_open_to_all'))
        has_alarm = bool(payload.get('has_alarm'))
        alarm_time_str = (payload.get('alarm_time') or '').strip()
        alarm_time = None
        if alarm_time_str:
            try:
                from django.utils.dateparse import parse_datetime
                alarm_time = parse_datetime(alarm_time_str)
                if alarm_time and timezone.is_naive(alarm_time):
                    alarm_time = timezone.make_aware(alarm_time)
            except Exception:
                alarm_time = None
        if has_alarm and not alarm_time:
            alarm_time = due_date or timezone.now()

        assigned_to_id = payload.get('assigned_to_id')
        assigned_to = None
        if is_open_to_all:
            assigned_to = None
        elif assigned_to_id:
            assigned_to = TechnicianProfile.objects.filter(id=assigned_to_id).first()
        elif is_tech and (payload.get('assign_to_me') or payload.get('assigned_to_me')):
            assigned_to = user.technician_profile

        job_reference_id = payload.get('job_reference_id')
        job_code = (payload.get('job_code') or '').strip()
        job_ref = None
        if job_reference_id:
            job_ref = JobTicket.objects.filter(id=job_reference_id).first()
        elif job_code:
            job_ref = JobTicket.objects.filter(job_code=job_code).first()

        task = Task.objects.create(
            workspace=workspace,
            title=title,
            description=description,
            priority=priority,
            due_date=due_date,
            assigned_to=assigned_to,
            is_open_to_all=is_open_to_all,
            has_alarm=has_alarm,
            alarm_time=alarm_time,
            job_reference=job_ref,
            created_by=user,
            status=Task.STATUS_OPEN,
        )

        initial_message = (payload.get('initial_message') or '').strip()
        if initial_message:
            init_msg = TaskMessage.objects.create(
                task=task,
                sender=user,
                body=initial_message,
            )
            broadcast_task_message(task, init_msg)

        broadcast_task_created(task)
        return JsonResponse({
            'ok': True,
            'message': 'Task created successfully.',
            'task': _serialize_task_for_mobile(task, user=user, detailed=True),
        }, status=201)

    # GET: List tasks
    if user.is_staff and not user_has_staff_access(user, "task_management"):
        return JsonResponse({'error': 'forbidden', 'message': 'Task management access required.'}, status=403)

    pool_filter = request.GET.get('pool') in ('1', 'true', 'yes') or request.GET.get('scope') == 'pool'
    mine_filter = request.GET.get('mine') in ('1', 'true', 'yes')

    if pool_filter:
        base_qs = scope_to_workspace(Task.objects.all(), workspace).filter(
            Q(is_open_to_all=True) | Q(assigned_to__isnull=True),
            status__in=[Task.STATUS_OPEN, Task.STATUS_IN_PROGRESS]
        )
    elif is_tech and not user.is_staff:
        base_qs = Task.objects.filter(assigned_to=user.technician_profile)
    elif mine_filter and is_tech:
        base_qs = scope_to_workspace(Task.objects.all(), workspace).filter(assigned_to=user.technician_profile)
    elif mine_filter and user.is_staff:
        base_qs = scope_to_workspace(Task.objects.all(), workspace).filter(
            Q(assigned_to__user=user) | Q(created_by=user)
        )
    else:
        base_qs = scope_to_workspace(Task.objects.all(), workspace)

    workspace_all = scope_to_workspace(Task.objects.all(), workspace)
    pool_count = workspace_all.filter(
        Q(is_open_to_all=True) | Q(assigned_to__isnull=True),
        status__in=[Task.STATUS_OPEN, Task.STATUS_IN_PROGRESS]
    ).count()

    # Compute KPI statistics over base_qs
    total_count = base_qs.count()
    active_count = base_qs.filter(status__in=[Task.STATUS_OPEN, Task.STATUS_IN_PROGRESS]).count()
    urgent_count = base_qs.filter(priority=Task.PRIORITY_URGENT, status__in=[Task.STATUS_OPEN, Task.STATUS_IN_PROGRESS]).count()
    in_progress_count = base_qs.filter(status=Task.STATUS_IN_PROGRESS).count()
    open_count = base_qs.filter(status=Task.STATUS_OPEN).count()
    done_count = base_qs.filter(status=Task.STATUS_DONE).count()

    qs = base_qs

    # Search
    q = (request.GET.get('q') or '').strip()
    if q:
        qs = qs.filter(
            Q(title__icontains=q) |
            Q(description__icontains=q) |
            Q(job_reference__job_code__icontains=q)
        )

    # Status filter
    status = (request.GET.get('status') or '').strip().lower()
    if status in dict(Task.STATUS_CHOICES):
        qs = qs.filter(status=status)
    elif status == 'all':
        pass
    else:
        # Default: active (open + in_progress)
        qs = qs.filter(status__in=[Task.STATUS_OPEN, Task.STATUS_IN_PROGRESS])

    # Priority filter
    priority = (request.GET.get('priority') or '').strip().lower()
    if priority in dict(Task.PRIORITY_CHOICES):
        qs = qs.filter(priority=priority)

    tasks_list = list(
        qs.select_related('assigned_to__user', 'created_by', 'job_reference')
        .prefetch_related('attachments', 'messages')[:200]
    )

    tasks_data = [_serialize_task_for_mobile(t, user=user) for t in tasks_list]
    return JsonResponse({
        'count': len(tasks_data),
        'metrics': {
            'total': total_count,
            'active': active_count,
            'urgent': urgent_count,
            'in_progress': in_progress_count,
            'open': open_count,
            'done': done_count,
            'pool': pool_count,
        },
        'tasks': tasks_data,
    })


@csrf_exempt
def mobile_api_task_detail(request, task_id):
    """Get or update full task details including attachments and messages."""
    user, error_response = authenticate_mobile_request(request)
    if error_response:
        return error_response

    if user.is_staff and not user_has_staff_access(user, "task_management"):
        return JsonResponse({'error': 'forbidden', 'message': 'Task management access required.'}, status=403)

    workspace = _mobile_request_workspace(request, user)
    is_tech = hasattr(user, 'technician_profile')

    task = get_object_or_404(
        Task.objects.select_related('assigned_to__user', 'created_by', 'job_reference'),
        pk=task_id,
    )

    tech_profile = getattr(user, 'technician_profile', None)
    if is_tech and not user.is_staff and task.assigned_to != tech_profile:
        if not task.is_claimable:
            return JsonResponse({'error': 'forbidden', 'message': 'You are not assigned to this task.'}, status=403)

    if request.method in ('POST', 'PATCH', 'PUT'):
        # Edit Task details from mobile: staff only (technicians cannot edit task definitions)
        if not user.is_staff or not user_has_staff_access(user, "task_management"):
            return JsonResponse({'error': 'forbidden', 'message': 'Only staff with task management permission can edit task details.'}, status=403)
        try:
            payload = json.loads((request.body or b'{}').decode('utf-8'))
        except (ValueError, json.JSONDecodeError, UnicodeDecodeError):
            payload = request.POST

        update_fields = ['updated_at']
        if 'title' in payload and (payload['title'] or '').strip():
            task.title = payload['title'].strip()
            update_fields.append('title')
        if 'description' in payload:
            task.description = (payload['description'] or '').strip()
            update_fields.append('description')
        if 'priority' in payload and payload['priority'] in dict(Task.PRIORITY_CHOICES):
            task.priority = payload['priority']
            update_fields.append('priority')
        if 'due_date' in payload:
            due_str = (payload['due_date'] or '').strip()
            if due_str:
                try:
                    from django.utils.dateparse import parse_datetime
                    due_dt = parse_datetime(due_str)
                    if not due_dt:
                        from datetime import datetime
                        due_dt = datetime.strptime(due_str[:16], '%Y-%m-%d %H:%M')
                    if due_dt and timezone.is_naive(due_dt):
                        due_dt = timezone.make_aware(due_dt)
                    task.due_date = due_dt
                except Exception:
                    pass
            else:
                task.due_date = None
            update_fields.append('due_date')
        if 'assigned_to_id' in payload:
            aid = payload['assigned_to_id']
            if aid:
                tech = TechnicianProfile.objects.filter(id=aid).first()
                if tech:
                    task.assigned_to = tech
                    update_fields.append('assigned_to')
            else:
                task.assigned_to = None
                update_fields.append('assigned_to')

        task.save(update_fields=list(set(update_fields)))
        return JsonResponse({
            'ok': True,
            'message': 'Task updated successfully.',
            'task': _serialize_task_for_mobile(task, user=user, detailed=True),
        })

    return JsonResponse({
        'ok': True,
        'task': _serialize_task_for_mobile(task, user=user, detailed=True),
    })


def mobile_api_technicians(request):
    """List active technicians available for task/job assignment."""
    user, error_response = authenticate_mobile_request(request)
    if error_response:
        return error_response
    workspace = _mobile_request_workspace(request, user)
    techs = get_assignable_technician_queryset(workspace).select_related('user')
    tech_data = [
        {
            'id': t.id,
            'unique_id': t.unique_id,
            'name': t.user.get_full_name() or t.user.username,
            'username': t.user.username,
            'is_me': bool(hasattr(user, 'technician_profile') and user.technician_profile.id == t.id),
        }
        for t in techs
    ]
    return JsonResponse({'technicians': tech_data})


def mobile_api_jobs_quicklist(request):
    """List recent/active jobs for dropdown selection (e.g. when creating/linking a task)."""
    user, error_response = authenticate_mobile_request(request)
    if error_response:
        return error_response
    workspace = _mobile_request_workspace(request, user)
    qs = scope_to_workspace(JobTicket.objects.exclude(status='Closed'), workspace)
    q = (request.GET.get('q') or '').strip()
    if q:
        qs = qs.filter(
            Q(job_code__icontains=q) |
            Q(customer_name__icontains=q) |
            Q(device_brand__icontains=q) |
            Q(device_model__icontains=q)
        )
    jobs = qs.order_by('-created_at')[:30]
    data = [
        {
            'id': j.id,
            'job_code': j.job_code,
            'customer_name': j.customer_name,
            'device': f"{j.device_type} {j.device_brand or ''}".strip(),
            'status': j.status,
        }
        for j in jobs
    ]
    return JsonResponse({'jobs': data})


@csrf_exempt
@require_POST
def mobile_api_task_attachment_upload(request, task_id):
    """Upload an attachment or photo to a task."""
    user, error_response = authenticate_mobile_request(request)
    if error_response:
        return error_response

    task = get_object_or_404(Task, pk=task_id)
    file_obj = request.FILES.get('file') or request.FILES.get('attachment')
    if not file_obj:
        return JsonResponse({'error': 'no_file', 'message': 'No file uploaded.'}, status=400)

    att = TaskAttachment.objects.create(
        task=task,
        file=file_obj,
        file_name=file_obj.name,
        file_size=file_obj.size,
        uploaded_by=user,
    )
    return JsonResponse({
        'ok': True,
        'message': 'Attachment uploaded.',
        'attachment': {
            'id': att.id,
            'file_name': att.file_name,
            'file_size': att.file_size,
            'file_url': att.file.url if att.file else '',
            'uploaded_at': timezone.localtime(att.uploaded_at).strftime('%Y-%m-%d %H:%M'),
            'uploaded_by': user.username,
        }
    })


@csrf_exempt
@require_POST
def mobile_api_task_update_status(request, task_id):
    """Update task status from mobile (in_progress, done, open, cancelled)."""
    user, error_response = authenticate_mobile_request(request)
    if error_response:
        return error_response

    task = get_object_or_404(Task, pk=task_id)
    is_tech = hasattr(user, 'technician_profile') and task.assigned_to == user.technician_profile
    if user.is_staff:
        if not user_has_staff_access(user, "task_management"):
            return JsonResponse({'error': 'forbidden', 'message': 'Task management access required.'}, status=403)
    elif not is_tech:
        return JsonResponse({'error': 'forbidden', 'message': 'Access denied.'}, status=403)

    try:
        payload = json.loads((request.body or b'{}').decode('utf-8'))
    except (ValueError, json.JSONDecodeError, UnicodeDecodeError):
        payload = request.POST

    old_status = task.status
    new_status = (payload.get('status') or '').strip().lower()
    if new_status not in dict(Task.STATUS_CHOICES):
        return JsonResponse({'error': 'invalid_status', 'message': f"Invalid status '{new_status}'."}, status=400)

    if new_status == Task.STATUS_IN_PROGRESS:
        task.mark_in_progress()
    elif new_status == Task.STATUS_DONE:
        task.mark_done()
    elif new_status == Task.STATUS_CANCELLED:
        task.mark_cancelled()
    elif new_status == Task.STATUS_OPEN:
        task.status = Task.STATUS_OPEN
        task.completed_at = None
        task.save(update_fields=['status', 'completed_at', 'updated_at'])

    if new_status != old_status:
        broadcast_task_status(task, old_status, new_status, user)

    return JsonResponse({
        'ok': True,
        'status': task.status,
        'status_display': task.get_status_display(),
        'completed_at': timezone.localtime(task.completed_at).strftime('%Y-%m-%d %H:%M') if task.completed_at else '',
    })


@csrf_exempt
@require_POST
def mobile_api_task_accept(request, task_id):
    """Technician claims and accepts a task from the open pool."""
    user, error_response = authenticate_mobile_request(request)
    if error_response:
        return error_response

    is_tech = hasattr(user, 'technician_profile')
    if not is_tech:
        return JsonResponse({'error': 'forbidden', 'message': 'Only technicians can claim tasks.'}, status=403)

    technician = user.technician_profile
    from django.db import transaction
    with transaction.atomic():
        try:
            task = Task.objects.select_for_update().get(id=task_id)
        except Task.DoesNotExist:
            return JsonResponse({'error': 'not_found', 'message': 'Task not found.'}, status=404)

        if not task.is_claimable:
            return JsonResponse({'error': 'already_claimed', 'message': 'This task has already been claimed or is closed.'}, status=400)

        task.accept_by_technician(technician)
        msg = TaskMessage.objects.create(
            task=task,
            sender=user,
            body=f"{user.get_full_name() or user.username} accepted and claimed this task from the open pool.",
        )
        broadcast_task_message(task, msg)
        broadcast_task_accepted(task, technician)

    return JsonResponse({
        'ok': True,
        'message': f"Task '{task.title}' claimed successfully.",
        'task': _serialize_task_for_mobile(task, user=user, detailed=True),
    })


@csrf_exempt
@require_POST
def mobile_api_task_message_send(request, task_id):
    """Send a message on a task thread from mobile."""
    user, error_response = authenticate_mobile_request(request)
    if error_response:
        return error_response

    task = get_object_or_404(Task, pk=task_id)
    is_tech = hasattr(user, 'technician_profile') and task.assigned_to == user.technician_profile
    if user.is_staff:
        if not user_has_staff_access(user, "task_management"):
            return JsonResponse({'error': 'forbidden', 'message': 'Task management access required.'}, status=403)
    elif not is_tech:
        return JsonResponse({'error': 'forbidden', 'message': 'Access denied.'}, status=403)

    body = ''
    if 'application/json' in (request.content_type or ''):
        try:
            payload = json.loads((request.body or b'{}').decode('utf-8'))
            body = (payload.get('body') or payload.get('message') or '').strip()
        except (ValueError, json.JSONDecodeError, UnicodeDecodeError):
            return JsonResponse({'error': 'invalid_payload', 'message': 'Invalid JSON body.'}, status=400)
    else:
        body = (request.POST.get('body') or request.POST.get('message') or '').strip()

    if not body:
        return JsonResponse({'error': 'missing_body', 'message': 'Message body cannot be empty.'}, status=400)

    msg = TaskMessage.objects.create(
        task=task,
        sender=user,
        body=body,
    )

    # Handle optional file upload
    files = request.FILES.getlist('attachments')
    for f in files:
        TaskAttachment.objects.create(
            task=task,
            message=msg,
            file=f,
            file_name=f.name,
            file_size=f.size,
            uploaded_by=user,
        )

    broadcast_task_message(task, msg)

    return JsonResponse({
        'ok': True,
        'message': {
            'id': msg.id,
            'sender': user.username,
            'sender_is_self': True,
            'body': msg.body,
            'sent_at': timezone.localtime(msg.sent_at).strftime('%Y-%m-%d %H:%M'),
        }
    })


def mobile_api_task_messages(request, task_id):
    """Poll messages for a task, optionally filtering since_id."""
    user, error_response = authenticate_mobile_request(request)
    if error_response:
        return error_response

    task = get_object_or_404(Task, pk=task_id)
    is_tech = hasattr(user, 'technician_profile') and task.assigned_to == user.technician_profile
    if user.is_staff:
        if not user_has_staff_access(user, "task_management"):
            return JsonResponse({'error': 'forbidden', 'message': 'Task management access required.'}, status=403)
    elif not is_tech:
        return JsonResponse({'error': 'forbidden', 'message': 'Access denied.'}, status=403)

    qs = task.messages.select_related('sender').all()
    since_id = request.GET.get('since_id')
    if since_id and since_id.isdigit():
        qs = qs.filter(id__gt=int(since_id))

    messages_data = [
        {
            'id': msg.id,
            'sender': msg.sender.username,
            'sender_is_self': bool(msg.sender_id == user.id),
            'body': msg.body,
            'sent_at': timezone.localtime(msg.sent_at).strftime('%Y-%m-%d %H:%M'),
        }
        for msg in qs
    ]
    return JsonResponse({'count': len(messages_data), 'messages': messages_data})
