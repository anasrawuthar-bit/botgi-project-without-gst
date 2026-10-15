from .helpers import *  # noqa: F401,F403
from .helpers import (
    _build_checklist_schema_for_job,
    _extract_checklist_answers_from_post,
    _format_checklist_required_error,
    _generate_inventory_bill_number,
    _generate_inventory_entry_number,
    _get_job_checklist_answers,
    _get_or_create_inventory_customer_party_for_job,
    _merge_checklist_answers,
    _money_or_zero,
    _net_amount_after_discount,
    _normalize_checkbox_answer,
    _normalize_checklist_answer,
    _staff_access_required,
)
from django.core.paginator import Paginator, EmptyPage, PageNotAnInteger
from django.template.loader import render_to_string
from django.http import JsonResponse
from django.db.models import Count, Q
from ..whatsapp_service import (
    _phone_to_international,
    create_message_queue,
    dispatch_message_queue_task,
    queue_job_whatsapp_message,
    send_job_whatsapp_notification,
)


REMINDER_PROMPT_COOLDOWN_MINUTES = 10
REMINDER_WORK_START = datetime.strptime('09:00', '%H:%M').time()
REMINDER_WORK_END = datetime.strptime('22:00', '%H:%M').time()
REMINDER_DEFAULT_TIME = '09:00'
FEEDBACK_FOLLOWUP_DAYS = 7
FEEDBACK_AUTO_SEND_LIMIT = 25


def _parse_html_time(raw_time):
    for fmt in ('%H:%M', '%H:%M:%S'):
        try:
            return datetime.strptime(raw_time, fmt).time()
        except (TypeError, ValueError):
            continue
    return None


def _parse_reminder_due_at(post_data, *, date_field='reminder_date', time_field='reminder_time', require_value=False):
    raw_date = (post_data.get(date_field) or '').strip()
    raw_time = (post_data.get(time_field) or '').strip()

    if not raw_date and not raw_time:
        if require_value:
            return None, 'Choose reminder date.'
        return None, ''
    if not raw_date:
        return None, 'Choose reminder date.'

    try:
        reminder_date = datetime.strptime(raw_date, '%Y-%m-%d').date()
    except (TypeError, ValueError):
        return None, 'Reminder date must be valid.'

    reminder_time = _parse_html_time(raw_time or REMINDER_DEFAULT_TIME)
    if reminder_time is None:
        return None, 'Reminder time must be valid.'

    if reminder_time < REMINDER_WORK_START or reminder_time > REMINDER_WORK_END:
        return None, 'Reminder time must be between 9:00 AM and 10:00 PM.'

    due_at = timezone.make_aware(
        datetime.combine(reminder_date, reminder_time),
        timezone.get_current_timezone(),
    )
    if due_at <= timezone.now():
        return None, 'Reminder date and time must be in the future.'

    return due_at, ''


def _default_reminder_schedule(active_reminder=None):
    now = timezone.localtime()

    if active_reminder:
        local_due_at = timezone.localtime(active_reminder.due_at)
        return {
            'date': local_due_at.date().isoformat(),
            'time': local_due_at.strftime('%H:%M'),
            'min_date': now.date().isoformat(),
            'default_time': REMINDER_DEFAULT_TIME,
        }

    reminder_date = now.date()
    if now.time() >= REMINDER_WORK_START:
        reminder_date += timedelta(days=1)

    return {
        'date': reminder_date.isoformat(),
        'time': REMINDER_DEFAULT_TIME,
        'min_date': now.date().isoformat(),
        'default_time': REMINDER_DEFAULT_TIME,
    }


def _parse_reminder_offset(post_data, *, hour_field='reminder_hours', minute_field='reminder_minutes', require_value=False):
    raw_hours = (post_data.get(hour_field) or '').strip()
    raw_minutes = (post_data.get(minute_field) or '').strip()

    if not raw_hours and not raw_minutes:
        if require_value:
            return None, 'Enter reminder hour or minute.'
        return None, ''

    try:
        hours = int(raw_hours or 0)
        minutes = int(raw_minutes or 0)
    except (TypeError, ValueError):
        return None, 'Reminder hour and minute must be valid numbers.'

    if hours < 0 or hours > 23:
        return None, 'Reminder hour must be between 0 and 23.'
    if minutes < 0 or minutes > 59:
        return None, 'Reminder minute must be between 0 and 59.'
    if hours == 0 and minutes == 0:
        if require_value:
            return None, 'Enter reminder hour or minute.'
        return None, ''

    return timedelta(hours=hours, minutes=minutes), ''


def _parse_estimated_amount(raw_amount):
    raw_amount = (raw_amount or '').strip()
    if not raw_amount:
        return None, ''
    try:
        amount = Decimal(raw_amount)
    except InvalidOperation:
        return None, 'Invalid estimate amount.'
    if amount < 0:
        return None, 'Estimate amount cannot be negative.'
    return amount.quantize(Decimal('0.01')), ''


def _save_job_estimation_from_post(request, job):
    estimated_amount, amount_error = _parse_estimated_amount(request.POST.get('estimated_amount'))
    if amount_error:
        return amount_error

    estimation_note = (request.POST.get('estimation_note') or '').strip()
    old_amount = job.estimated_amount
    old_note = job.estimation_note or ''

    if old_amount == estimated_amount and old_note == estimation_note:
        return ''

    job.estimated_amount = estimated_amount
    job.estimation_note = estimation_note
    job.save(update_fields=['estimated_amount', 'estimation_note', 'updated_at'])

    changes = []
    if old_amount != estimated_amount:
        changes.append(f"estimate amount changed from Rs {old_amount or '0.00'} to Rs {estimated_amount or '0.00'}")
    if old_note != estimation_note:
        changes.append('estimate note updated')

    JobTicketLog.objects.create(
        job_ticket=job,
        user=request.user,
        action='NOTE',
        details='Staff updated ' + ', '.join(changes) + '.',
    )
    return ''


def _active_job_reminder(job):
    return job.reminders.filter(status=JobReminder.STATUS_PENDING).order_by('due_at', 'id').first()


def _feedback_due_at_from_closed_at(closed_at):
    return closed_at + timedelta(days=FEEDBACK_FOLLOWUP_DAYS)


def _schedule_feedback_followup(job, *, save=True):
    if job.status != 'Closed':
        return []

    now = timezone.now()
    changed_fields = []
    if not job.closed_at:
        job.closed_at = now
        changed_fields.append('closed_at')
    if not job.feedback_due_at:
        job.feedback_due_at = _feedback_due_at_from_closed_at(job.closed_at)
        changed_fields.append('feedback_due_at')
    if not job.feedback_followup_enabled:
        job.feedback_followup_enabled = True
        changed_fields.append('feedback_followup_enabled')
    if job.feedback_rating and job.feedback_followup_status != JobTicket.FEEDBACK_RECEIVED:
        job.feedback_followup_status = JobTicket.FEEDBACK_RECEIVED
        changed_fields.append('feedback_followup_status')

    if save and changed_fields:
        job.save(update_fields=[*changed_fields, 'updated_at'])
    return changed_fields


def _prepare_feedback_followups():
    JobTicket.objects.filter(
        feedback_rating__isnull=False,
    ).exclude(
        feedback_followup_status=JobTicket.FEEDBACK_RECEIVED,
    ).update(
        feedback_followup_status=JobTicket.FEEDBACK_RECEIVED,
    )


def _queue_feedback_followup_message(job, *, user=None, manual=False):
    result = (
        queue_job_whatsapp_message(job, MessageQueue.EVENT_FEEDBACK)
        if manual
        else send_job_whatsapp_notification(job, MessageQueue.EVENT_FEEDBACK)
    )
    if not result.get('ok'):
        return result

    now = timezone.now()
    update_fields = ['feedback_message_sent_at', 'feedback_followup_status', 'updated_at']
    job.feedback_message_sent_at = now
    if job.feedback_followup_status == JobTicket.FEEDBACK_PENDING:
        job.feedback_followup_status = JobTicket.FEEDBACK_MESSAGE_SENT
    job.save(update_fields=update_fields)

    JobTicketLog.objects.create(
        job_ticket=job,
        user=user,
        action='FEEDBACK',
        details='Feedback WhatsApp follow-up queued.',
    )
    return result


def _auto_send_due_feedback_messages():
    _prepare_feedback_followups()
    due_jobs = JobTicket.objects.filter(
        status='Closed',
        feedback_followup_enabled=True,
        feedback_rating__isnull=True,
        feedback_due_at__lte=timezone.now(),
        feedback_message_sent_at__isnull=True,
    ).exclude(
        feedback_followup_status__in=[
            JobTicket.FEEDBACK_RECEIVED,
            JobTicket.FEEDBACK_CALLED_HAPPY,
        ]
    ).order_by('feedback_due_at', 'id')[:FEEDBACK_AUTO_SEND_LIMIT]

    sent_count = 0
    for job in due_jobs:
        result = _queue_feedback_followup_message(job)
        if result.get('ok'):
            sent_count += 1
    return sent_count


def _append_feedback_note(existing_note, new_note):
    new_note = (new_note or '').strip()
    if not new_note:
        return existing_note or ''
    timestamp = timezone.localtime(timezone.now()).strftime('%d-%m-%Y %I:%M %p')
    entry = f"{timestamp}: {new_note}"
    return f"{existing_note}\n{entry}".strip() if existing_note else entry


def _safe_next_redirect(request, fallback='staff_dashboard', **fallback_kwargs):
    next_url = (request.POST.get('next') or request.GET.get('next') or request.META.get('HTTP_REFERER') or '').strip()
    if next_url and url_has_allowed_host_and_scheme(
        next_url,
        allowed_hosts={request.get_host()},
        require_https=request.is_secure(),
    ):
        return redirect(next_url)
    return redirect(fallback, **fallback_kwargs)


@login_required
def staff_dashboard(request):
    denied = _staff_access_required(request, "staff_dashboard")
    if denied:
        return denied
    
    # Ensure Decimal is imported
    from decimal import Decimal, InvalidOperation
    period = get_report_period(request)
    start_of_period = period['start']
    end_of_period = period['end']
    
    # --- START: REWRITTEN JOB CREATION POST HANDLER FOR MULTI-DEVICE ---
    if request.method == 'POST' and 'job_ticket_form_submit' in request.POST:

        is_ajax_request = request.headers.get('X-Requested-With') == 'XMLHttpRequest'

        # CRITICAL STEP: Generate one group ID for this entire customer submission batch
        submission_group_id = str(uuid4())

        # 1. Extract static customer data from the form.
        customer_name = (request.POST.get('customer_name') or '').strip()
        customer_phone, customer_phone_error = normalize_indian_phone(
            request.POST.get('customer_phone'),
            field_label='Customer Phone',
        )

        estimated_amount_raw = (request.POST.get('estimated_amount') or '').strip()
        estimated_delivery_raw = request.POST.get('estimated_delivery')
        
        # Basic Validation
        basic_errors = []
        field_errors = {}
        if not customer_name:
            field_errors['customer_name'] = "Customer Name is required."
            basic_errors.append(field_errors['customer_name'])
        if customer_phone_error:
            field_errors['customer_phone'] = customer_phone_error
            basic_errors.append(customer_phone_error)

        if basic_errors:
            if is_ajax_request:
                return JsonResponse(
                    {'success': False, 'message': " ".join(basic_errors), 'field_errors': field_errors},
                    status=400,
                )
            request.session['show_create_job_modal'] = True
            for error in basic_errors:
                messages.error(request, error)
            return redirect('staff_dashboard')

        current_workspace = getattr(request, 'current_workspace', None)

        # Auto-create or refresh the client directory.
        try:
            client_qs = Client.objects.filter(phone__in=phone_lookup_variants(customer_phone))
            if current_workspace:
                client_qs = client_qs.filter(workspace=current_workspace)
            existing_client = client_qs.order_by('id').first()
            if existing_client:
                client_updated_fields = []
                if current_workspace and existing_client.workspace_id != current_workspace.id:
                    existing_client.workspace = current_workspace
                    client_updated_fields.append('workspace')
                if existing_client.name != customer_name:
                    existing_client.name = customer_name
                    client_updated_fields.append('name')
                if existing_client.phone != customer_phone:
                    existing_client.phone = customer_phone
                    client_updated_fields.append('phone')
                if client_updated_fields:
                    existing_client.save(update_fields=client_updated_fields)
            else:
                Client.objects.create(workspace=current_workspace, phone=customer_phone, name=customer_name)
        except Exception:
            # Client directory sync should not block ticket creation.
            pass

        job_date_raw = (request.POST.get('job_date') or '').strip()
        parsed_job_date = None
        if job_date_raw:
            try:
                parsed_job_date = datetime.strptime(job_date_raw, '%Y-%m-%d').date()
            except Exception:
                pass
        intake_date_val = parsed_job_date or timezone.localdate()

        try:
            estimated_amount = Decimal(estimated_amount_raw) if estimated_amount_raw else None
        except InvalidOperation:
            error_message = "Invalid input for Estimated Amount. Please enter a valid number."
            if is_ajax_request:
                return JsonResponse({'success': False, 'message': error_message}, status=400)
            request.session['show_create_job_modal'] = True
            messages.error(request, error_message)
            return redirect('staff_dashboard')

        estimated_delivery = None
        
        if estimated_delivery_raw:
            try:
                # The browser sends date input as YYYY-MM-DD
                estimated_delivery = datetime.strptime(estimated_delivery_raw, '%Y-%m-%d').date()
            except ValueError:
                error_message = "Invalid Estimated Delivery date format."
                if is_ajax_request:
                    return JsonResponse({'success': False, 'message': error_message}, status=400)
                request.session['show_create_job_modal'] = True
                messages.error(request, error_message)
                return redirect('staff_dashboard')

        reminder_delta, reminder_error = _parse_reminder_offset(request.POST)
        if reminder_error:
            if is_ajax_request:
                return JsonResponse({'success': False, 'message': reminder_error}, status=400)
            request.session['show_create_job_modal'] = True
            messages.error(request, reminder_error)
            return redirect('staff_dashboard')

        reminder_due_at = timezone.now() + reminder_delta if reminder_delta else None
        
        # 2. Identify and collect all device submissions using JavaScript's array naming
        device_submissions = []
        device_photo_payloads = []
        validation_errors = []
        i = 0
        # This loop correctly parses the dynamic fields sent by the frontend
        while request.POST.get(f'device_forms[{i}].device_type'):
            
            device_type = (request.POST.get(f'device_forms[{i}].device_type') or '').strip()
            device_brand = (request.POST.get(f'device_forms[{i}].device_brand') or '').strip()
            device_model = (request.POST.get(f'device_forms[{i}].device_model') or '').strip()
            device_serial = (request.POST.get(f'device_forms[{i}].device_serial') or '').strip()
            device_password = (request.POST.get(f'device_forms[{i}].device_password') or '').strip()
            reported_issue = (request.POST.get(f'device_forms[{i}].reported_issue') or '').strip()
            additional_items = (request.POST.get(f'device_forms[{i}].additional_items') or '').strip()
            photo_files = request.FILES.getlist(f'device_forms[{i}].device_photos')

            required_fields = [
                ('device_type', device_type, 'Device type'),
                ('device_brand', device_brand, 'Device brand'),
                ('device_model', device_model, 'Device model'),
                ('reported_issue', reported_issue, 'Reported issue'),
            ]

            missing_fields = [label for _, value, label in required_fields if not value]
            if missing_fields:
                for field_name, value, label in required_fields:
                    if not value:
                        validation_errors.append({
                            'device_index': i,
                            'field': field_name,
                            'message': f'{label} is required.'
                        })
            else:
                # Checkbox sends 'on' if checked, otherwise it's absent
                is_under_warranty_val = request.POST.get(f'device_forms[{i}].is_under_warranty')
                is_under_warranty = True if is_under_warranty_val == 'on' else False
                requires_laptop_inspection_checklist = (
                    request.POST.get(f'device_forms[{i}].requires_laptop_inspection_checklist') == 'on'
                )

                rack_id_val = (request.POST.get(f'device_forms[{i}].rack') or '').strip()
                rack_col_raw = (request.POST.get(f'device_forms[{i}].rack_column') or '').strip()
                rack_obj = None
                rack_col_val = None
                if rack_id_val:
                    try:
                        rack_filter = DeviceRack.objects.filter(id=int(rack_id_val), is_active=True)
                        if current_workspace:
                            rack_filter = rack_filter.filter(workspace=current_workspace)
                        rack_obj = rack_filter.first()
                        if rack_obj and rack_col_raw:
                            try:
                                parsed_col = int(rack_col_raw)
                                if 1 <= parsed_col <= max(1, rack_obj.total_columns or 100):
                                    rack_col_val = parsed_col
                            except (ValueError, TypeError):
                                rack_col_val = None
                    except (ValueError, TypeError):
                        rack_obj = None
                        rack_col_val = None

                # Check if device is linked to a previous job ticket
                original_job_code = (request.POST.get(f'device_forms[{i}].original_job_code') or '').strip()
                original_job_obj = None
                if original_job_code:
                    clean_orig = original_job_code.lstrip('#').strip()
                    orig_qs = JobTicket.objects.all()
                    if current_workspace:
                        orig_qs = orig_qs.filter(Q(workspace=current_workspace) | Q(workspace__isnull=True))
                    original_job_obj = orig_qs.filter(job_code__iexact=clean_orig).first()
                    if not original_job_obj and clean_orig.isdigit():
                        original_job_obj = orig_qs.filter(
                            Q(job_code__iexact=f"JT-{clean_orig}") |
                            Q(job_code__endswith=clean_orig)
                        ).first()

                device_submissions.append({
                    'device_type': device_type,
                    'device_brand': device_brand,
                    'device_model': device_model,
                    'device_serial': device_serial,
                    'device_password': device_password,
                    'reported_issue': reported_issue,
                    'additional_items': additional_items,
                    'is_under_warranty': is_under_warranty,
                    'requires_laptop_inspection_checklist': requires_laptop_inspection_checklist,
                    'rack': rack_obj,
                    'rack_column': rack_col_val,
                    'original_job_ticket': original_job_obj,
                })
                device_photo_payloads.append(photo_files)
            i += 1

        if validation_errors:
            if is_ajax_request:
                return JsonResponse({'success': False, 'errors': validation_errors}, status=400)
            request.session['show_create_job_modal'] = True
            for error in validation_errors:
                messages.error(request, f"Device #{error['device_index'] + 1}: {error['message']}")
            return redirect('staff_dashboard')

        for device_index, photo_files in enumerate(device_photo_payloads):
            for photo_file in photo_files:
                if not photo_file:
                    continue
                _, upload_error = validate_job_photo_upload(photo_file)
                if upload_error:
                    validation_errors.append({
                        'device_index': device_index,
                        'field': 'device_photos',
                        'message': upload_error,
                    })

        if validation_errors:
            if is_ajax_request:
                return JsonResponse({'success': False, 'errors': validation_errors}, status=400)
            request.session['show_create_job_modal'] = True
            for error in validation_errors:
                messages.error(request, f"Device #{error['device_index'] + 1}: {error['message']}")
            return redirect('staff_dashboard')

        if not device_submissions:
            error_message = "Please add at least one device to create a job ticket."
            if is_ajax_request:
                return JsonResponse({'success': False, 'message': error_message}, status=400)
            request.session['show_create_job_modal'] = True
            messages.error(request, error_message)
            return redirect('staff_dashboard')

        # 3. Create Jobs in a Transaction
        with transaction.atomic():
            created_job_codes = []
            created_jobs_payload = []
            
            for idx, device_data in enumerate(device_submissions):
                photo_files = device_photo_payloads[idx] if idx < len(device_photo_payloads) else []
                # CRITICAL: Call the helper function to get a unique, simple job code
                new_job_code = get_next_job_code() 
                
                new_job = JobTicket.objects.create(
                    workspace=current_workspace,
                    job_code=new_job_code,
                    customer_name=customer_name,
                    customer_phone=customer_phone,
                    created_by=request.user,
                    estimated_amount=estimated_amount,
                    estimated_delivery=estimated_delivery,
                    customer_group_id=submission_group_id,
                    intake_date=intake_date_val,
                    **device_data 
                )

                if photo_files:
                    for photo_file in photo_files:
                        if photo_file:
                            content_type, _ = validate_job_photo_upload(photo_file)

                            JobTicketPhoto.objects.create(
                                job_ticket=new_job,
                                image_name=(getattr(photo_file, 'name', '') or 'device-photo').strip()[:255],
                                image_content_type=content_type[:100],
                                image_data=photo_file.read(),
                            )
                
                created_details = f"Job ticket created for device: {device_data['device_type']}."
                if device_data.get('original_job_ticket'):
                    created_details += f" Linked to previous ticket #{device_data['original_job_ticket'].job_code}."
                if parsed_job_date and parsed_job_date != timezone.localdate():
                    created_details += f" Intake date: {parsed_job_date.strftime('%d-%m-%Y')}."
                if device_data.get('rack'):
                    rack_loc = device_data['rack'].name
                    if device_data.get('rack_column'):
                        rack_loc += f" - Col {device_data['rack_column']}"
                    rack_loc += f" ({device_data['rack'].group})"
                    created_details += f" Assigned to rack: {rack_loc}."
                JobTicketLog.objects.create(job_ticket=new_job, user=request.user, action='CREATED', details=created_details)

                if reminder_due_at:
                    reminder = JobReminder.objects.create(
                        job_ticket=new_job,
                        due_at=reminder_due_at,
                        created_by=request.user,
                    )
                    local_due_at = timezone.localtime(reminder.due_at).strftime('%d-%m-%Y %I:%M %p')
                    JobTicketLog.objects.create(
                        job_ticket=new_job,
                        user=request.user,
                        action='NOTE',
                        details=f"Callback reminder scheduled for {local_due_at}.",
                    )
                
                created_job_codes.append(new_job.job_code)
                created_jobs_payload.append({
                    'job_code': new_job.job_code,
                    'customer_name': new_job.customer_name,
                    'customer_phone': new_job.customer_phone,
                    'device_type': new_job.device_type,
                    'net_total': '0.00',
                    'original_job_code': new_job.original_job_ticket.job_code if new_job.original_job_ticket else None,
                    'detail_url': reverse('staff_job_detail', args=[new_job.job_code]),
                    'receipt_url': reverse('job_creation_receipt_print', args=[new_job.job_code]),
                })

        success_message = (
            f"Successfully created {len(created_job_codes)} new job ticket(s) for {customer_name}."
            if len(created_job_codes) > 1
            else f"Job {created_job_codes[0]} created successfully."
        )

        # Keep preset lists useful by learning values from created tickets.
        try:
            sync_job_field_presets(device_submissions)
        except Exception:
            pass

        if is_ajax_request:
            response_data = {
                'success': True,
                'message': success_message,
                'job_codes': created_job_codes,
                'jobs': created_jobs_payload,
            }
            if len(created_job_codes) == 1:
                response_data['redirect_url'] = reverse('job_creation_success', args=[created_job_codes[0]])
            return JsonResponse(response_data)

        # 4. Success Redirection (non-AJAX)
        if len(created_job_codes) == 1:
            return redirect('job_creation_success', job_code=created_job_codes[0])
        else:
            messages.success(request, success_message)
            return redirect('staff_dashboard')
    # --- END: REWRITTEN JOB CREATION POST HANDLER ---

    # --- START: NON-CREATION POST HANDLERS & GET LOGIC (Code is kept identical to yours below) ---

    # Handle Job Assignment Form (Logic retained)
    else: # This 'else' catches the GET request for the dashboard initially
        form = JobTicketForm()
        
    if request.method == 'POST' and 'assign_job_form_submit' in request.POST:
        assign_form = AssignJobForm(request.POST, workspace=getattr(request, 'current_workspace', None))
        if assign_form.is_valid():
            job_code = assign_form.cleaned_data['job_code']
            technician = assign_form.cleaned_data['technician']
            job_to_assign = get_object_or_404(JobTicket, job_code=job_code)

            with transaction.atomic():
                old_status = job_to_assign.get_status_display()
                job_to_assign.assigned_to = technician
                job_to_assign.status = 'Under Inspection'
                job_to_assign.is_new_assignment = True
                job_to_assign.save(update_fields=['assigned_to', 'status', 'updated_at', 'is_new_assignment'])

                details = f"Assigned to technician '{technician.user.username}' and status changed from '{old_status}' to 'Under Inspection'."
                JobTicketLog.objects.create(job_ticket=job_to_assign, user=request.user, action='ASSIGNED', details=details)

            # Send WebSocket update for real-time job assignment notification
            send_job_update_message(job_to_assign.job_code, job_to_assign.status, old_status=old_status, is_assignment=True)

            messages.success(request, f"Job {job_to_assign.job_code} assigned to {technician.user.username}.")
            return redirect('staff_dashboard')
    else:
        assign_form = AssignJobForm(workspace=getattr(request, 'current_workspace', None))

    
    # START: NEW VENDOR ASSIGNMENT LOGIC (Logic retained)
    if request.method == 'POST' and 'assign_vendor_form_submit' in request.POST:
        assign_vendor_form = AssignVendorForm(request.POST, workspace=getattr(request, 'current_workspace', None))
        if assign_vendor_form.is_valid():
            data = assign_vendor_form.cleaned_data
            service = get_object_or_404(SpecializedService, id=data['specialized_service_id'])
            
            with transaction.atomic():
                service.vendor = data['vendor']
                # Costs will be entered when device returns from vendor
                service.status = 'Sent to Vendor'
                service.sent_date = timezone.now()
                service.save()
                
                details = f"Assigned to vendor '{service.vendor.company_name}' and sent for service."
                JobTicketLog.objects.create(job_ticket=service.job_ticket, user=request.user, action='STATUS', details=details)

            messages.success(request, f"Job {service.job_ticket.job_code} assigned to {service.vendor.company_name}.")
            
            # CHANNELS: Send update (removed - Django Channels no longer used)
            
            return redirect('staff_dashboard')

    # GET QUERY AND LIST FETCHING (Logic retained)
    query = request.GET.get('q')
    current_workspace = getattr(request, 'current_workspace', None)
    search_results = []
    if query:
        search_qs = JobTicket.objects.filter(
            Q(job_code__icontains=query) |
            Q(customer_name__icontains=query) |
            Q(customer_phone__icontains=query)
        )
        if current_workspace:
            search_qs = search_qs.filter(Q(workspace=current_workspace) | Q(workspace__isnull=True))
        search_results = list(search_qs.select_related(
            'assigned_to__user'
        ).prefetch_related(
            'service_logs'
        ).order_by('-created_at'))
        
        try:
            # Calculate totals for search results
            calculate_job_totals(search_results)
            for job in search_results:
                job.discount_total = _money_or_zero(job.discount_amount)
                job.net_total = _net_amount_after_discount(job.total, job.discount_total)
        except InvalidOperation:
            messages.error(request, "Error calculating job totals. Some values may be incorrect.")
        
        # Use search results for all status filters
        job_tickets = JobTicket.objects.filter(
            Q(job_code__icontains=query) |
            Q(customer_name__icontains=query) |
            Q(customer_phone__icontains=query)
        ).order_by('-created_at')
    else:
        job_tickets = JobTicket.objects.all().order_by('-created_at')
    if current_workspace:
        job_tickets = job_tickets.filter(Q(workspace=current_workspace) | Q(workspace__isnull=True))
    # Defer large text fields not needed for dashboard listing
    job_tickets = job_tickets.defer('technician_notes', 'technician_checklist', 'feedback_followup_note')
    
    def prepare_dashboard_jobs(queryset):
        jobs = list(queryset.select_related('assigned_to__user').prefetch_related('service_logs'))
        calculate_job_totals(jobs)
        for job in jobs:
            job.discount_total = _money_or_zero(job.discount_amount)
            job.net_total = _net_amount_after_discount(job.total, job.discount_total)
        return jobs

    pending_jobs = prepare_dashboard_jobs(job_tickets.filter(status='Pending'))
    returned_jobs = prepare_dashboard_jobs(job_tickets.filter(status='Returned'))
    ready_for_pickup_jobs = prepare_dashboard_jobs(job_tickets.filter(status='Ready for Pickup'))
    completed_jobs = prepare_dashboard_jobs(job_tickets.filter(status='Completed'))
    awaiting_assignment = list(
        SpecializedService.objects.filter(status='Awaiting Assignment')
        .select_related('job_ticket')
        .prefetch_related('job_ticket__service_logs')
    )

    # Fetch and group in-progress jobs by technician (Logic retained)
    in_progress_jobs_qs = job_tickets.filter(
        Q(status='Under Inspection') | Q(status='Repairing')
    )
    in_progress_jobs = prepare_dashboard_jobs(in_progress_jobs_qs)

    grouped_in_progress_jobs = {}
    for job in in_progress_jobs:
        key = job.assigned_to.user.username if job.assigned_to and job.assigned_to.user else "Unassigned"
        if key not in grouped_in_progress_jobs:
            grouped_in_progress_jobs[key] = []
        grouped_in_progress_jobs[key].append(job)

    # Create a form instance for each of these jobs (Logic retained)
    for service in awaiting_assignment:
        calculate_job_totals([service.job_ticket])
        service.job_ticket.discount_total = _money_or_zero(service.job_ticket.discount_amount)
        service.job_ticket.net_total = _net_amount_after_discount(service.job_ticket.total, service.job_ticket.discount_total)
        service.form = AssignVendorForm(
            initial={'specialized_service_id': service.id},
            workspace=getattr(request, 'current_workspace', None) or service.job_ticket.workspace,
        )

    sent_to_vendor = SpecializedService.objects.filter(status='Sent to Vendor').select_related('job_ticket', 'vendor')

    reminder_alerts = list(
        JobReminder.objects.filter(status=JobReminder.STATUS_PENDING)
        .exclude(job_ticket__status='Closed')
        .select_related('job_ticket', 'created_by')
        .order_by('due_at', 'id')[:75]
    )
    reminder_alert_count = (
        JobReminder.objects.filter(status=JobReminder.STATUS_PENDING)
        .exclude(job_ticket__status='Closed')
        .count()
    )
    due_reminder_count = (
        JobReminder.objects.filter(
            status=JobReminder.STATUS_PENDING,
            due_at__lte=timezone.now(),
        )
        .exclude(job_ticket__status='Closed')
        .count()
    )

    # Available active racks for ticket creation
    racks_qs = DeviceRack.objects.filter(is_active=True)
    if current_workspace:
        racks_qs = racks_qs.filter(workspace=current_workspace)
    active_racks = list(racks_qs.order_by('group', 'name'))

    # Rack occupancy mapping for ticket creation modal
    rack_occupancy_data = {}
    for r in active_racks:
        rack_occupancy_data[str(r.id)] = {
            'id': r.id,
            'name': r.name,
            'group': r.group,
            'total_columns': max(1, r.total_columns or 10),
            'occupied': {},
        }

    if active_racks:
        active_rack_tickets = JobTicket.objects.filter(
            rack__in=active_racks,
            rack_column__isnull=False
        ).exclude(
            status__in=['Closed', 'Returned']
        ).values('rack_id', 'rack_column', 'job_code', 'device_type', 'customer_name')

        for t in active_rack_tickets:
            rid = str(t['rack_id'])
            col_str = str(t['rack_column'])
            if rid in rack_occupancy_data:
                if col_str not in rack_occupancy_data[rid]['occupied']:
                    rack_occupancy_data[rid]['occupied'][col_str] = []
                rack_occupancy_data[rid]['occupied'][col_str].append({
                    'job_code': t['job_code'],
                    'device_type': t['device_type'] or '',
                    'customer_name': t['customer_name'] or '',
                })

    active_checklists = get_active_checklist_templates(workspace=current_workspace)

    # FINAL CONTEXT
    context = {
        'form': JobTicketForm(), # Use an empty form here for any generic field access in the template
        'assign_form': assign_form,
        'job_field_presets': get_job_field_presets(),
        'active_checklist_templates': active_checklists,
        'active_checklist_templates_json': json.dumps(active_checklists),
        'rack_occupancy_data_json': json.dumps(rack_occupancy_data),
        'show_create_job_modal': request.session.pop('show_create_job_modal', False),
        'pending_jobs': pending_jobs,
        'grouped_in_progress_jobs': grouped_in_progress_jobs,
        'in_progress_jobs_count': len(in_progress_jobs),
        'returned_jobs': returned_jobs,
        'ready_for_pickup_jobs': ready_for_pickup_jobs,
        'completed_jobs': completed_jobs,
        'username': request.user.username,
        'query': query,
        'search_results': search_results,
        'search_count': len(search_results) if query else 0,
        'awaiting_assignment_jobs': awaiting_assignment,
        'sent_to_vendor_jobs': sent_to_vendor,
        'pending_count': len(pending_jobs),
        'ready_count': len(ready_for_pickup_jobs),
        'completed_count': len(completed_jobs),
        'returned_count': len(returned_jobs),
        'reminder_alerts': reminder_alerts,
        'reminder_alert_count': reminder_alert_count,
        'due_reminder_count': due_reminder_count,
        'active_racks': active_racks,
    }
    return render(request, 'job_tickets/staff_dashboard.html', context)


@login_required
def staff_dashboard_tab_partial(request):
    """
    Returns HTML snippets and latest counts for specific dashboard tabs.
    Used for silent, real-time background refreshing without full page reloads.
    Query params:
        - tab: comma-separated list of tabs (e.g. 'pending', 'in-progress', 'ready', 'specialized', 'completed', 'returned', 'all')
    """
    denied = _staff_access_required(request, "staff_dashboard")
    if denied:
        return JsonResponse({'ok': False, 'error': 'Unauthorized'}, status=403)

    current_workspace = getattr(request, 'current_workspace', None)
    tab_param = (request.GET.get('tab') or '').strip().lower()
    requested_tabs = set(t.strip() for t in tab_param.split(',') if t.strip())

    # Base tickets queryset scoped to workspace
    job_tickets = JobTicket.objects.all().order_by('-created_at')
    if current_workspace:
        job_tickets = job_tickets.filter(Q(workspace=current_workspace) | Q(workspace__isnull=True))
    job_tickets = job_tickets.defer('technician_notes', 'technician_checklist', 'feedback_followup_note')

    def prepare_dashboard_jobs(qs):
        jobs = list(qs.select_related('assigned_to__user').prefetch_related('service_logs'))
        calculate_job_totals(jobs)
        for job in jobs:
            job.discount_total = _money_or_zero(job.discount_amount)
            job.net_total = _net_amount_after_discount(job.total, job.discount_total)
        return jobs

    # 1. Fast live counts for all tabs
    status_counts_raw = dict(
        job_tickets.values('status').annotate(total=Count('id')).values_list('status', 'total')
    )
    pending_count = status_counts_raw.get('Pending', 0)
    in_progress_count = (
        status_counts_raw.get('Under Inspection', 0) +
        status_counts_raw.get('Repairing', 0)
    )
    ready_count = status_counts_raw.get('Ready for Pickup', 0)
    completed_count = status_counts_raw.get('Completed', 0)
    returned_count = status_counts_raw.get('Returned', 0)

    specialized_qs = SpecializedService.objects.filter(status='Awaiting Assignment')
    if current_workspace:
        specialized_qs = specialized_qs.filter(
            Q(job_ticket__workspace=current_workspace) | Q(job_ticket__workspace__isnull=True)
        )
    specialized_count = specialized_qs.count()

    all_counts = {
        'pending': pending_count,
        'in-progress': in_progress_count,
        'ready': ready_count,
        'specialized': specialized_count,
        'completed': completed_count,
        'returned': returned_count,
    }

    # 2. Render HTML only for the requested tabs
    rendered_tabs = {}

    if 'all' in requested_tabs or 'pending' in requested_tabs:
        pending_jobs = prepare_dashboard_jobs(job_tickets.filter(status='Pending'))
        html = render_to_string(
            'job_tickets/staff_tabs/tab_pending.html',
            {'pending_jobs': pending_jobs, 'pending_count': len(pending_jobs)},
            request=request,
        )
        rendered_tabs['pending'] = {'html': html, 'count': len(pending_jobs)}

    if 'all' in requested_tabs or 'in-progress' in requested_tabs:
        in_progress_jobs = prepare_dashboard_jobs(job_tickets.filter(Q(status='Under Inspection') | Q(status='Repairing')))
        grouped_in_progress_jobs = {}
        for job in in_progress_jobs:
            key = job.assigned_to.user.username if job.assigned_to and job.assigned_to.user else "Unassigned"
            if key not in grouped_in_progress_jobs:
                grouped_in_progress_jobs[key] = []
            grouped_in_progress_jobs[key].append(job)
        html = render_to_string(
            'job_tickets/staff_tabs/tab_in_progress.html',
            {
                'grouped_in_progress_jobs': grouped_in_progress_jobs,
                'in_progress_jobs_count': len(in_progress_jobs),
            },
            request=request,
        )
        rendered_tabs['in-progress'] = {'html': html, 'count': len(in_progress_jobs)}

    if 'all' in requested_tabs or 'ready' in requested_tabs:
        ready_for_pickup_jobs = prepare_dashboard_jobs(job_tickets.filter(status='Ready for Pickup'))
        html = render_to_string(
            'job_tickets/staff_tabs/tab_ready.html',
            {'ready_for_pickup_jobs': ready_for_pickup_jobs, 'ready_count': len(ready_for_pickup_jobs)},
            request=request,
        )
        rendered_tabs['ready'] = {'html': html, 'count': len(ready_for_pickup_jobs)}

    if 'all' in requested_tabs or 'specialized' in requested_tabs:
        awaiting_assignment = list(
            specialized_qs.select_related('job_ticket').prefetch_related('job_ticket__service_logs')
        )
        for service in awaiting_assignment:
            calculate_job_totals([service.job_ticket])
            service.job_ticket.discount_total = _money_or_zero(service.job_ticket.discount_amount)
            service.job_ticket.net_total = _net_amount_after_discount(service.job_ticket.total, service.job_ticket.discount_total)
            service.form = AssignVendorForm(
                initial={'specialized_service_id': service.id},
                workspace=current_workspace or service.job_ticket.workspace,
            )
        html = render_to_string(
            'job_tickets/staff_tabs/tab_specialized.html',
            {'awaiting_assignment_jobs': awaiting_assignment},
            request=request,
        )
        rendered_tabs['specialized'] = {'html': html, 'count': len(awaiting_assignment)}

    if 'all' in requested_tabs or 'completed' in requested_tabs:
        completed_jobs = prepare_dashboard_jobs(job_tickets.filter(status='Completed'))
        html = render_to_string(
            'job_tickets/staff_tabs/tab_completed.html',
            {'completed_jobs': completed_jobs, 'completed_count': len(completed_jobs)},
            request=request,
        )
        rendered_tabs['completed'] = {'html': html, 'count': len(completed_jobs)}

    if 'all' in requested_tabs or 'returned' in requested_tabs:
        returned_jobs = prepare_dashboard_jobs(job_tickets.filter(status='Returned'))
        html = render_to_string(
            'job_tickets/staff_tabs/tab_returned.html',
            {'returned_jobs': returned_jobs, 'returned_count': len(returned_jobs)},
            request=request,
        )
        rendered_tabs['returned'] = {'html': html, 'count': len(returned_jobs)}

    return JsonResponse({
        'ok': True,
        'counts': all_counts,
        'tabs': rendered_tabs,
    })


# job_tickets/views.py

# job_tickets/views.py

@login_required
def job_billing_staff(request, job_code):
    denied = _staff_access_required(request, "staff_dashboard")
    if denied:
        return denied
    
    job = get_object_or_404(JobTicket, job_code=job_code)
    
    if request.method == 'POST':
        # --- Handle Billing/Invoice Submission (Includes Job and Log Updates) ---
        if 'update_amounts_submit' in request.POST:
            if job.status == 'Closed':
                err_msg = "Permission denied: Cannot edit billing amounts for a closed job."
                if request.headers.get('x-requested-with') == 'XMLHttpRequest' or request.headers.get('X-Requested-With') == 'fetch':
                    return JsonResponse({'ok': False, 'error': err_msg}, status=403)
                messages.error(request, err_msg)
                return redirect('job_billing_staff', job_code=job_code)

            try:
                with transaction.atomic():
                    raw_delete_service_ids = request.POST.getlist('delete_service_ids[]')
                    delete_service_ids = []
                    seen_delete_ids = set()
                    for raw_service_id in raw_delete_service_ids:
                        try:
                            service_id_int = int(raw_service_id)
                        except (TypeError, ValueError):
                            continue
                        if service_id_int in seen_delete_ids:
                            continue
                        seen_delete_ids.add(service_id_int)
                        delete_service_ids.append(service_id_int)
                    delete_service_ids_set = set(delete_service_ids)

                    # 1. HANDLE JOB-LEVEL INVOICE NUMBER (shared across service + inventory sales)
                    submitted_invoice = (request.POST.get('job_sales_invoice_number') or '').strip()
                    old_vyapar_invoice = job.vyapar_invoice_number or ""
                    sale_entry_date = timezone.localdate()

                    if submitted_invoice:
                        validate_sales_invoice_number_uniqueness(
                            submitted_invoice,
                            exclude_job_id=job.id,
                            allow_inventory_job_id=job.id,
                        )
                        new_vyapar_invoice = submitted_invoice
                    elif old_vyapar_invoice:
                        new_vyapar_invoice = old_vyapar_invoice
                    else:
                        # No invoice submitted and none previously saved - leave blank.
                        new_vyapar_invoice = ''

                    if old_vyapar_invoice != new_vyapar_invoice:
                        details = f"Sales Invoice No. updated from '{old_vyapar_invoice}' to '{new_vyapar_invoice}'."
                        JobTicketLog.objects.create(job_ticket=job, user=request.user, action='BILLING', details=details)
                        job.vyapar_invoice_number = new_vyapar_invoice or None

                    # Keep existing linked inventory sales entries and bill header on the same shared invoice.
                    updated_invoice_rows = (
                        InventoryEntry.objects.filter(entry_type='sale', job_ticket=job)
                        .exclude(invoice_number=new_vyapar_invoice)
                        .update(invoice_number=new_vyapar_invoice)
                    )
                    InventoryBill.objects.filter(entry_type='sale', job_ticket=job).exclude(
                        invoice_number=new_vyapar_invoice
                    ).update(invoice_number=new_vyapar_invoice)
                    if updated_invoice_rows:
                        JobTicketLog.objects.create(
                            job_ticket=job,
                            user=request.user,
                            action='BILLING',
                            details=(
                                f"Updated {updated_invoice_rows} linked inventory sale line(s) "
                                f"to invoice '{new_vyapar_invoice}'."
                            ),
                        )
                    
                    # 2. UPDATE EXISTING SERVICE LOGS (skip lines queued for deletion)
                    product_sales_by_log_id = {
                        sale.service_log_id: sale
                        for sale in ProductSale.objects.select_related('product', 'inventory_entry').filter(
                            job_ticket=job,
                            service_log__isnull=False,
                        )
                    }
                    for log in job.service_logs.all():
                        log_id = log.id
                        if log_id in delete_service_ids_set:
                            continue

                        is_updated = False
                        product_sale_entry = product_sales_by_log_id.get(log_id)
                        try:
                            new_part_cost = Decimal(request.POST.get(f'part_cost_{log_id}', 0) or 0)
                            new_service_charge = Decimal(request.POST.get(f'service_charge_{log_id}', 0) or 0)
                        except InvalidOperation:
                            raise ValueError(f"Invalid amount entered for '{log.description}'.")

                        old_part_cost = log.part_cost or Decimal('0')
                        old_service_charge = log.service_charge or Decimal('0')
                        description_field = f'description_{log_id}'
                        if description_field in request.POST:
                            old_description = log.description or ''
                            new_description = (request.POST.get(description_field) or '').strip()
                            if not new_description:
                                raise ValueError("Service description cannot be empty.")
                            if len(new_description) > 255:
                                raise ValueError("Service description cannot exceed 255 characters.")
                            if old_description != new_description:
                                log.description = new_description
                                is_updated = True
                                details = f"Updated service description from '{old_description}' to '{new_description}'."
                                JobTicketLog.objects.create(job_ticket=job, user=request.user, action='BILLING', details=details)

                        if product_sale_entry:
                            quantity = product_sale_entry.quantity or 1
                            if quantity <= 0:
                                raise ValueError(
                                    f"Product sale quantity is invalid for '{product_sale_entry.product.name}'."
                                )

                            unit_price_field = f'product_unit_price_{log_id}'
                            if unit_price_field in request.POST:
                                try:
                                    submitted_unit_price = Decimal(request.POST.get(unit_price_field, 0) or 0).quantize(Decimal('0.01'))
                                except InvalidOperation:
                                    raise ValueError(f"Invalid unit price entered for '{product_sale_entry.product.name}'.")
                                if submitted_unit_price < 0:
                                    raise ValueError(f"Product price cannot be negative for '{product_sale_entry.product.name}'.")
                                quantized_part_cost = (submitted_unit_price * Decimal(quantity)).quantize(Decimal('0.01'))
                                new_unit_price = submitted_unit_price
                            else:
                                if new_part_cost < 0:
                                    raise ValueError(
                                        f"Product amount cannot be negative for '{product_sale_entry.product.name}'."
                                    )
                                quantized_part_cost = new_part_cost.quantize(Decimal('0.01'))
                                new_unit_price = (
                                    quantized_part_cost / Decimal(quantity)
                                ).quantize(Decimal('0.01'))

                            line_cost = (
                                (product_sale_entry.cost_price or Decimal('0.00')) * Decimal(quantity)
                            ).quantize(Decimal('0.01'))
                            line_profit = (quantized_part_cost - line_cost).quantize(Decimal('0.01'))

                            if old_part_cost != quantized_part_cost or product_sale_entry.unit_price != new_unit_price:
                                log.part_cost = quantized_part_cost
                                product_sale_entry.unit_price = new_unit_price
                                product_sale_entry.line_total = quantized_part_cost
                                product_sale_entry.line_profit = line_profit

                                if product_sale_entry.inventory_entry_id:
                                    inv_entry = product_sale_entry.inventory_entry
                                    inv_entry.unit_price = new_unit_price
                                    inv_entry.taxable_amount = quantized_part_cost
                                    inv_gst_rate = inv_entry.gst_rate or Decimal('0.00')
                                    inv_gst_amount = (quantized_part_cost * inv_gst_rate / Decimal('100')).quantize(Decimal('0.01'))
                                    inv_entry.gst_amount = inv_gst_amount
                                    inv_entry.total_amount = (quantized_part_cost + inv_gst_amount).quantize(Decimal('0.01'))
                                    inv_entry.save(update_fields=['unit_price', 'taxable_amount', 'gst_amount', 'total_amount'])
                                is_updated = True

                                details = (
                                    f"Updated product sale '{product_sale_entry.product.name}' unit price "
                                    f"to Rs {new_unit_price} (Total: Rs {quantized_part_cost})."
                                )
                                JobTicketLog.objects.create(
                                    job_ticket=job,
                                    user=request.user,
                                    action='BILLING',
                                    details=details,
                                )
                            if old_service_charge != new_service_charge:
                                log.service_charge = new_service_charge
                                is_updated = True
                                JobTicketLog.objects.create(
                                    job_ticket=job,
                                    user=request.user,
                                    action='BILLING',
                                    details=(
                                        f"Updated product sale '{product_sale_entry.product.name}' service charge "
                                        f"from Rs {old_service_charge} to Rs {new_service_charge}."
                                    ),
                                )
                        else:
                            if old_part_cost != new_part_cost:
                                log.part_cost = new_part_cost
                                is_updated = True
                                details = f"Updated '{log.description}' part cost from Rs {old_part_cost} to Rs {new_part_cost}."
                                JobTicketLog.objects.create(job_ticket=job, user=request.user, action='BILLING', details=details)

                            if old_service_charge != new_service_charge:
                                log.service_charge = new_service_charge
                                is_updated = True
                                details = f"Updated '{log.description}' service charge from Rs {old_service_charge} to Rs {new_service_charge}."
                                JobTicketLog.objects.create(job_ticket=job, user=request.user, action='BILLING', details=details)
                        
                        log_invoice_field = f'sales_invoice_number_{log_id}'
                        if log_invoice_field in request.POST:
                            old_log_invoice = log.sales_invoice_number or ""
                            new_log_invoice = request.POST.get(log_invoice_field, '').strip()
                            if old_log_invoice != new_log_invoice:
                                log.sales_invoice_number = new_log_invoice
                                is_updated = True
                                details = f"Log Invoice updated for '{log.description}' from '{old_log_invoice}' to '{new_log_invoice}'."
                                JobTicketLog.objects.create(job_ticket=job, user=request.user, action='BILLING', details=details)
                            
                        if is_updated:
                            if product_sale_entry:
                                product_sale_entry.save(update_fields=['unit_price', 'line_total', 'line_profit'])
                            log.save()
                    
                    # 3. HANDLE NEW MANUAL SERVICE LINES
                    new_descriptions = request.POST.getlist('new_description[]')
                    new_part_costs = request.POST.getlist('new_part_cost[]')
                    new_service_charges = request.POST.getlist('new_service_charge[]')

                    for i in range(len(new_descriptions)):
                        description = (new_descriptions[i] or '').strip()
                        if not description:
                            continue
                        try:
                            part_cost = Decimal((new_part_costs[i] if i < len(new_part_costs) else 0) or 0)
                            service_charge = Decimal((new_service_charges[i] if i < len(new_service_charges) else 0) or 0)
                        except InvalidOperation:
                            raise ValueError(f"Invalid amount for new service line '{description}'.")

                        ServiceLog.objects.create(
                            job_ticket=job,
                            description=description,
                            part_cost=part_cost,
                            service_charge=service_charge
                        )
                        details = f"Added new service: '{description}' (Part: Rs {part_cost}, Service: Rs {service_charge})."
                        JobTicketLog.objects.create(job_ticket=job, user=request.user, action='SERVICE', details=details)

                    # 4. HANDLE DELETIONS (restock product if product-line deleted)
                    for service_id in delete_service_ids:
                        try:
                            service_log = ServiceLog.objects.select_for_update().get(id=service_id, job_ticket=job)
                        except ServiceLog.DoesNotExist:
                            continue

                        product_sale_entry = ProductSale.objects.filter(service_log=service_log).select_related('product').first()
                        parsed_product_sale = parse_product_sale_log(service_log.description) if not product_sale_entry else None

                        if product_sale_entry:
                            product = Product.objects.select_for_update().filter(id=product_sale_entry.product_id).first()
                            if product:
                                product.stock_quantity += product_sale_entry.quantity
                                product.save(update_fields=['stock_quantity', 'updated_at'])
                                JobTicketLog.objects.create(
                                    job_ticket=job,
                                    user=request.user,
                                    action='SERVICE',
                                    details=f"Restocked {product.name} by {product_sale_entry.quantity} after deleting product sale line.",
                                )
                            if product_sale_entry.inventory_entry_id:
                                product_sale_entry.inventory_entry.delete()
                        elif parsed_product_sale:
                            product = Product.objects.select_for_update().filter(id=parsed_product_sale['product_id']).first()
                            if product:
                                product.stock_quantity += parsed_product_sale['quantity']
                                product.save(update_fields=['stock_quantity', 'updated_at'])
                                JobTicketLog.objects.create(
                                    job_ticket=job,
                                    user=request.user,
                                    action='SERVICE',
                                    details=f"Restocked {product.name} by {parsed_product_sale['quantity']} after deleting legacy product sale line.",
                                )

                        details = f"Deleted service: '{service_log.description}' (Part: Rs {service_log.part_cost}, Service: Rs {service_log.service_charge})"
                        service_log.delete()
                        JobTicketLog.objects.create(job_ticket=job, user=request.user, action='SERVICE', details=details)

                    # 5. HANDLE PRODUCT SALES (service + product in one bill)
                    # Keep this after deletions so stock released by deleted lines is immediately reusable.
                    product_ids = request.POST.getlist('product_id[]')
                    product_quantities = request.POST.getlist('product_qty[]')
                    product_unit_prices = request.POST.getlist('product_unit_price[]')
                    product_service_charges = request.POST.getlist('product_service_charge[]')
                    customer_party = _get_or_create_inventory_customer_party_for_job(job)
                    inventory_sale_bill = (
                        InventoryBill.objects.select_for_update()
                        .filter(entry_type='sale', job_ticket=job)
                        .first()
                    )
                    if inventory_sale_bill:
                        inventory_bill_updates = []
                        if inventory_sale_bill.entry_date != sale_entry_date:
                            inventory_sale_bill.entry_date = sale_entry_date
                            inventory_bill_updates.append('entry_date')
                        if (inventory_sale_bill.invoice_number or '') != new_vyapar_invoice:
                            inventory_sale_bill.invoice_number = new_vyapar_invoice
                            inventory_bill_updates.append('invoice_number')
                        if inventory_sale_bill.party_id != customer_party.id:
                            inventory_sale_bill.party = customer_party
                            inventory_bill_updates.append('party')
                        expected_bill_note = f"Auto product sale entries from job {job.job_code}."
                        if inventory_sale_bill.notes != expected_bill_note:
                            inventory_sale_bill.notes = expected_bill_note
                            inventory_bill_updates.append('notes')
                        if inventory_sale_bill.created_by_id is None:
                            inventory_sale_bill.created_by = request.user
                            inventory_bill_updates.append('created_by')
                        if inventory_bill_updates:
                            inventory_sale_bill.save(update_fields=inventory_bill_updates + ['updated_at'])
                    else:
                        inventory_sale_bill = InventoryBill.objects.create(
                            workspace=job.workspace or getattr(request, 'current_workspace', None),
                            bill_number=_generate_inventory_bill_number('sale', sale_entry_date),
                            entry_type='sale',
                            entry_date=sale_entry_date,
                            invoice_number=new_vyapar_invoice,
                            job_ticket=job,
                            party=customer_party,
                            notes=f"Auto product sale entries from job {job.job_code}.",
                            created_by=request.user,
                        )

                    for index, raw_product_id in enumerate(product_ids):
                        product_id = (raw_product_id or '').strip()
                        raw_qty = (product_quantities[index] if index < len(product_quantities) else '').strip()
                        raw_unit_price = (
                            product_unit_prices[index] if index < len(product_unit_prices) else ''
                        ).strip()
                        raw_service_charge = (
                            product_service_charges[index] if index < len(product_service_charges) else ''
                        ).strip()

                        if not product_id:
                            continue

                        try:
                            quantity = int(raw_qty)
                        except (TypeError, ValueError):
                            raise ValueError("Product quantity must be a valid whole number.")

                        try:
                            product_service_charge = Decimal(raw_service_charge or 0)
                        except InvalidOperation:
                            raise ValueError("Product service charge must be a valid amount.")

                        if quantity <= 0:
                            raise ValueError("Product quantity must be greater than zero.")
                        if product_service_charge < 0:
                            raise ValueError("Product service charge cannot be negative.")

                        product_qs = Product.objects.select_for_update().filter(pk=product_id)
                        if job.workspace_id:
                            product_qs = product_qs.filter(workspace=job.workspace)
                        product = product_qs.first()
                        if not product:
                            raise ValueError("Selected product no longer exists.")
                        stock_before = product.stock_quantity
                        stock_after = stock_before - quantity

                        if raw_unit_price:
                            try:
                                unit_price = Decimal(raw_unit_price).quantize(Decimal('0.01'))
                            except InvalidOperation:
                                unit_price = product.unit_price or Decimal('0.00')
                        else:
                            unit_price = product.unit_price or Decimal('0.00')

                        line_total = (unit_price * Decimal(quantity)).quantize(Decimal('0.01'))
                        line_description = f"Product Sale - {product.name} (Qty: {quantity})"

                        created_sale_log = ServiceLog.objects.create(
                            job_ticket=job,
                            description=line_description,
                            part_cost=line_total,
                            service_charge=product_service_charge.quantize(Decimal('0.01')),
                        )

                        line_cost = (product.cost_price or Decimal('0')) * Decimal(quantity)
                        line_profit = line_total - line_cost
                        inventory_sale_entry = InventoryEntry.objects.create(
                            workspace=job.workspace or getattr(request, 'current_workspace', None),
                            bill=inventory_sale_bill,
                            entry_number=_generate_inventory_entry_number('sale', sale_entry_date),
                            entry_type='sale',
                            entry_date=sale_entry_date,
                            invoice_number=new_vyapar_invoice,
                            job_ticket=job,
                            party=customer_party,
                            product=product,
                            quantity=quantity,
                            unit_price=unit_price,
                            discount_amount=Decimal('0.00'),
                            gst_rate=Decimal('0.00'),
                            taxable_amount=line_total.quantize(Decimal('0.01')),
                            gst_amount=Decimal('0.00'),
                            total_amount=line_total.quantize(Decimal('0.01')),
                            stock_before=stock_before,
                            stock_after=stock_after,
                            notes=f"Auto product sale entry from job {job.job_code}.",
                            created_by=request.user,
                        )

                        ProductSale.objects.create(
                            workspace=job.workspace or getattr(request, 'current_workspace', None),
                            job_ticket=job,
                            product=product,
                            service_log=created_sale_log,
                            inventory_entry=inventory_sale_entry,
                            quantity=quantity,
                            unit_price=unit_price,
                            cost_price=product.cost_price or Decimal('0.00'),
                            line_total=line_total,
                            line_cost=line_cost,
                            line_profit=line_profit,
                            sold_by=request.user,
                        )

                        product.stock_quantity = stock_after
                        product.save(update_fields=['stock_quantity', 'updated_at'])

                        JobTicketLog.objects.create(
                            job_ticket=job,
                            user=request.user,
                            action='SERVICE',
                            details=(
                                f"Added product sale: {product.name} x{quantity} at Rs {product.unit_price} each "
                                f"(Revenue: Rs {line_total}, Service Charge: Rs {product_service_charge}, "
                                f"Cost: Rs {line_cost}, Profit: Rs {line_profit}, "
                                f"Sale Invoice: {new_vyapar_invoice})."
                            ),
                        )

                    InventoryBill.objects.filter(
                        entry_type='sale',
                        job_ticket=job,
                        lines__isnull=True,
                    ).delete()

                    # 6. HANDLE DISCOUNT + FINAL SAVE
                    old_discount = job.discount_amount
                    try:
                        new_discount = Decimal(request.POST.get('discount_amount', 0) or 0)
                    except InvalidOperation:
                        raise ValueError("Invalid discount amount.")

                    if old_discount != new_discount:
                        job.discount_amount = new_discount
                        details = f"Discount updated from Rs {old_discount} to Rs {new_discount}."
                        JobTicketLog.objects.create(job_ticket=job, user=request.user, action='BILLING', details=details)

                    job.save(update_fields=['vyapar_invoice_number', 'discount_amount', 'updated_at'])
                
                messages.success(request, f"Billing and Invoice details for Job {job_code} updated successfully.")
            except ValueError as exc:
                messages.error(request, str(exc))
            except InvalidOperation:
                messages.error(request, "Invalid amount format found in billing form.")
            except Exception:
                messages.error(request, "Unable to update billing details right now. Please try again.")

            return redirect('job_billing_staff', job_code=job_code)
        
        # --- Handle Rework Form Submission ---
        if 'rework_form_submit' in request.POST:
            rework_form = ReworkForm(request.POST)
            if rework_form.is_valid():
                rework_reason = rework_form.cleaned_data['rework_reason']
                new_job_code = get_next_job_code()
                
                new_job = JobTicket.objects.create(
                    workspace=getattr(request, 'current_workspace', None) or job.workspace,
                    job_code=new_job_code,
                    customer_name=job.customer_name,
                    customer_phone=job.customer_phone,
                    device_type=job.device_type,
                    device_brand=job.device_brand,
                    device_model=job.device_model,
                    device_serial=job.device_serial,
                    reported_issue=f"Rework from original ticket {job.job_code}: {rework_reason}",
                    original_job_ticket=job,
                    status='Pending',
                    created_by=request.user
                )
                
                return redirect('job_creation_success', job_code=new_job.job_code)

    # --- GET / RENDERING PATH ---
    job_tickets = [job]
    calculate_job_totals(job_tickets)
    
    subtotal = job.total
    discount = job.discount_amount
    grand_total = subtotal - discount
    
    rework_form = ReworkForm()
    discount_form = DiscountForm(initial={'discount_amount': job.discount_amount})
    technician_id = job.assigned_to.unique_id if job.assigned_to else 'N/A'
    product_sale_log_ids = list(
        ProductSale.objects.filter(job_ticket=job, service_log__isnull=False).values_list('service_log_id', flat=True)
    )
    pure_service_logs = [log for log in job.service_logs.all() if log.id not in product_sale_log_ids]
    existing_product_sales = list(
        job.product_sales.select_related('product', 'service_log', 'inventory_entry').order_by('id')
    )

    products_qs = Product.objects.all()
    if job.workspace:
        products_qs = products_qs.filter(workspace=job.workspace)
    products_for_sale = products_qs.order_by('name')

    from ..models import ItemCategory
    item_cats_qs = ItemCategory.objects.all()
    if job.workspace:
        item_cats_qs = item_cats_qs.filter(workspace=job.workspace)
    item_categories = item_cats_qs.order_by('name')

    job_expenses = job.expenses.all().select_related('recorded_by').order_by('-expense_date', '-id')
    job_expenses_total = job_expenses.aggregate(total=Coalesce(Sum('amount'), Decimal('0.00')))['total']
    
    context = {
        'job': job,
        'service_logs': job.service_logs.all(),
        'pure_service_logs': pure_service_logs,
        'existing_product_sales': existing_product_sales,
        'products_for_sale': products_for_sale,
        'item_categories': item_categories,
        'total_parts_cost': job.part_total,
        'total_service_charges': job.service_total,
        'subtotal': subtotal,
        'discount': discount,
        'grand_total': grand_total,
        'rework_form': rework_form,
        'discount_form': discount_form,
        'technician_id': technician_id,
        'product_sale_log_ids': product_sale_log_ids,
        'payment_methods': InventoryCreditPayment.METHOD_CHOICES,
        'job_expenses': job_expenses,
        'job_expenses_total': job_expenses_total,
        'financial_accounts': list(
            FinancialAccount.objects.filter(
                Q(workspace=getattr(request, 'current_workspace', None) or job.workspace) | Q(workspace__isnull=True),
                is_active=True
            ).order_by('-is_default_cash', '-is_default_bank', 'name')
        ),
    }
    return render(request, 'job_tickets/job_billing_staff.html', context)

@login_required
def mark_ready_for_pickup(request, job_code):
    denied = _staff_access_required(request, "staff_dashboard")
    if denied:
        return denied
    if not user_has_staff_access(request.user, "job_status_change"):
        messages.error(request, "You do not have permission to change job status.")
        return redirect("staff_dashboard")
    
    job = get_object_or_404(JobTicket, job_code=job_code)
    old_status = job.get_status_display()
    job.status = 'Ready for Pickup'
    job.save()
    
    # Send WebSocket update
    send_job_update_message(job.job_code, job.status)
    details = f"Status changed from '{old_status}' to '{job.get_status_display()}'."
    JobTicketLog.objects.create(job_ticket=job, user=request.user, action='STATUS', details=details)
    
    return redirect('staff_dashboard')

@login_required
def close_job(request, job_code):
    denied = _staff_access_required(request, "staff_dashboard")
    if denied:
        return denied

    job = get_object_or_404(JobTicket, job_code=job_code)
    if request.method != 'POST':
        messages.error(request, 'Use the close confirmation window to close a job.')
        return redirect('job_billing_staff', job_code=job.job_code)

    calculate_job_totals([job])
    grand_total = max(Decimal('0.00'), (job.total or Decimal('0.00')) - (job.discount_amount or Decimal('0.00')))

    payment_status = (request.POST.get('payment_status') or 'paid').strip().lower()
    if payment_status not in {'paid', 'part_paid', 'unpaid'}:
        payment_status = 'paid'

    account_id = (request.POST.get('financial_account') or '').strip()
    target_account = None
    if account_id:
        target_account = FinancialAccount.objects.filter(
            Q(workspace=job.workspace) | Q(workspace__isnull=True),
            id=account_id,
            is_active=True,
        ).first()

    posted_payment_method = (request.POST.get('payment_method') or '').strip()
    if target_account:
        if target_account.account_type == FinancialAccount.ACCOUNT_TYPE_CASH:
            payment_method = InventoryCreditPayment.METHOD_CASH
        elif target_account.account_type == FinancialAccount.ACCOUNT_TYPE_UPI:
            payment_method = InventoryCreditPayment.METHOD_UPI
        elif target_account.account_type == FinancialAccount.ACCOUNT_TYPE_BANK:
            if posted_payment_method in [InventoryCreditPayment.METHOD_BANK, InventoryCreditPayment.METHOD_CARD]:
                payment_method = posted_payment_method
            else:
                payment_method = InventoryCreditPayment.METHOD_BANK
        else:
            payment_method = posted_payment_method or InventoryCreditPayment.METHOD_CASH
    elif posted_payment_method:
        payment_method = posted_payment_method
        target_account = FinancialAccount.get_default_for_payment_method(job.workspace, payment_method)
    else:
        target_account = FinancialAccount.get_default_for_payment_method(job.workspace, 'cash')
        payment_method = InventoryCreditPayment.METHOD_CASH

    payment_reference = (request.POST.get('payment_reference') or '').strip()

    if payment_status == 'paid':
        amount_paid = grand_total
        payment_date = timezone.now()
    elif payment_status == 'part_paid':
        try:
            amount_paid = Decimal((request.POST.get('amount_paid') or '0').strip()).quantize(Decimal('0.01'))
        except (InvalidOperation, ValueError, TypeError):
            messages.error(request, 'Invalid partial payment amount entered.')
            return redirect('job_billing_staff', job_code=job.job_code)
        if amount_paid <= Decimal('0.00'):
            messages.error(request, 'Partial payment amount must be greater than zero.')
            return redirect('job_billing_staff', job_code=job.job_code)
        if amount_paid >= grand_total:
            amount_paid = grand_total
            payment_status = 'paid'
        payment_date = timezone.now()
    else:  # unpaid (credit)
        amount_paid = Decimal('0.00')
        payment_method = ''
        target_account = None
        payment_date = None

    old_status = job.get_status_display()
    job.status = 'Closed'
    job.closed_at = timezone.now()
    job.feedback_due_at = _feedback_due_at_from_closed_at(job.closed_at)
    job.feedback_followup_enabled = True
    job.feedback_followup_status = (
        JobTicket.FEEDBACK_RECEIVED if job.feedback_rating else JobTicket.FEEDBACK_PENDING
    )
    job.payment_status = payment_status
    job.payment_method = payment_method or None
    job.financial_account = target_account
    job.amount_paid = amount_paid
    job.payment_reference = payment_reference
    job.payment_date = payment_date
    job.save(update_fields=[
        'status',
        'closed_at',
        'feedback_due_at',
        'feedback_followup_enabled',
        'feedback_followup_status',
        'payment_status',
        'payment_method',
        'financial_account',
        'amount_paid',
        'payment_reference',
        'payment_date',
        'updated_at',
    ])

    # Reverse previous inflow account transactions for this job if adjusting settlement
    for old_txn in job.account_transactions.filter(transaction_type=AccountTransaction.TYPE_INFLOW):
        old_acc = old_txn.account
        old_acc.current_balance -= old_txn.amount
        old_acc.save(update_fields=['current_balance', 'updated_at'])
        old_txn.delete()

    # Record inventory credit/payment ledger if applicable
    party = _get_or_create_inventory_customer_party_for_job(job)
    bill = InventoryBill.objects.filter(entry_type='sale', job_ticket=job).first()
    if not bill and (grand_total > Decimal('0.00') or amount_paid > Decimal('0.00')):
        bill = InventoryBill.objects.create(
            workspace=job.workspace,
            bill_number=_generate_inventory_bill_number('sale', timezone.localdate(), job.workspace),
            entry_type='sale',
            entry_date=timezone.localdate(),
            invoice_number=job.vyapar_invoice_number or '',
            job_ticket=job,
            party=party,
            notes=f"Repair Job {job.job_code} settlement",
            created_by=request.user,
        )

    inv_credit_payment = None
    if bill and amount_paid > Decimal('0.00'):
        balance_after = max(Decimal('0.00'), grand_total - amount_paid)
        inv_credit_payment = InventoryCreditPayment.objects.create(
            workspace=job.workspace,
            party=party,
            bill=bill,
            direction=InventoryCreditPayment.DIRECTION_RECEIVABLE,
            payment_date=timezone.localdate(),
            payment_method=payment_method or InventoryCreditPayment.METHOD_CASH,
            financial_account=target_account,
            amount=amount_paid,
            balance_before=grand_total,
            balance_after=balance_after,
            reference_no=payment_reference,
            notes=f"Settlement for Job {job.job_code} ({payment_status})",
            created_by=request.user,
        )

    # Deposit into financial account if payment collected
    if target_account and amount_paid > Decimal('0.00'):
        target_account.deposit(
            amount=amount_paid,
            description=f"Job Settlement: {job.job_code} ({job.customer_name})",
            reference_no=payment_reference,
            transaction_date=timezone.localdate(),
            inventory_credit_payment=inv_credit_payment,
            job_ticket=job,
            user=request.user,
        )

    method_display = dict(InventoryCreditPayment.METHOD_CHOICES).get(payment_method, payment_method) or 'N/A'
    balance_due = max(Decimal('0.00'), grand_total - amount_paid)
    details = (
        f"Status changed from '{old_status}' to 'Closed'. "
        f"Payment: {payment_status.replace('_', ' ').title()} "
        f"(Paid: Rs {amount_paid}, Balance Due: Rs {balance_due} via {method_display})."
    )
    JobTicketLog.objects.create(job_ticket=job, user=request.user, action='CLOSED', details=details)

    # Auto-mark any pending reminders as done when a job is closed
    JobReminder.objects.filter(
        job_ticket=job,
        status=JobReminder.STATUS_PENDING,
    ).update(status=JobReminder.STATUS_DONE, completed_at=timezone.now())

    send_job_update_message(job.job_code, job.status)
    messages.success(request, f"Job {job.job_code} closed successfully. Payment recorded: {payment_status.replace('_', ' ').title()}.")
    return _safe_next_redirect(request)


@login_required
@require_POST
def update_feedback_followup(request, job_code):
    if not request.user.is_staff or not (
        user_has_staff_access(request.user, "staff_dashboard")
        or user_has_staff_access(request.user, "feedback_analytics")
    ):
        return redirect('unauthorized')

    job = get_object_or_404(JobTicket, job_code=job_code)
    if job.status != 'Closed':
        messages.error(request, 'Feedback follow-up is available only after the job is closed.')
        return _safe_next_redirect(request, 'staff_job_detail', job_code=job.job_code)
    if not job.feedback_followup_enabled or not job.feedback_due_at:
        messages.error(request, 'Feedback follow-up is enabled only for newly closed jobs.')
        return _safe_next_redirect(request, 'staff_job_detail', job_code=job.job_code)

    action = (request.POST.get('feedback_action') or '').strip()
    note = (request.POST.get('feedback_note') or '').strip()
    rating_raw = (request.POST.get('feedback_rating') or '').strip()
    status_map = {
        'called_happy': JobTicket.FEEDBACK_CALLED_HAPPY,
        'called_issue': JobTicket.FEEDBACK_CALLED_ISSUE,
        'no_answer': JobTicket.FEEDBACK_NO_ANSWER,
        'call_later': JobTicket.FEEDBACK_CALL_LATER,
    }

    if action == 'send_now':
        result = _queue_feedback_followup_message(job, user=request.user, manual=True)
        if result.get('ok'):
            messages.success(request, f'Feedback WhatsApp queued for {job.customer_name}.')
        else:
            messages.error(request, result.get('reason') or result.get('error') or 'Could not queue feedback WhatsApp.')
        return _safe_next_redirect(request, 'staff_job_detail', job_code=job.job_code)

    update_fields = ['feedback_followup_status', 'feedback_followup_note', 'feedback_followup_marked_by', 'updated_at']
    old_status = job.get_feedback_followup_status_display()
    job.feedback_followup_marked_by = request.user
    if note:
        job.feedback_followup_note = _append_feedback_note(job.feedback_followup_note, note)

    if action == 'mark_received':
        if not rating_raw:
            messages.error(request, 'Rating (1 to 10) is required to record customer feedback.')
            return _safe_next_redirect(request, 'staff_job_detail', job_code=job.job_code)

        try:
            rating = int(rating_raw)
        except (TypeError, ValueError):
            messages.error(request, 'Feedback rating must be between 1 and 10.')
            return _safe_next_redirect(request, 'staff_job_detail', job_code=job.job_code)
        if rating < 1 or rating > 10:
            messages.error(request, 'Feedback rating must be between 1 and 10.')
            return _safe_next_redirect(request, 'staff_job_detail', job_code=job.job_code)

        if not note:
            note = f"Customer rated {rating}/10"

        job.feedback_followup_status = JobTicket.FEEDBACK_RECEIVED
        job.feedback_rating = rating
        job.feedback_comment = note
        job.feedback_date = timezone.now()
        update_fields.extend(['feedback_rating', 'feedback_comment', 'feedback_date'])
        success_message = 'Feedback marked as received successfully.'
    elif action == 'no_answer':
        job.feedback_call_attempts = (getattr(job, 'feedback_call_attempts', 0) or 0) + 1
        job.feedback_followup_called_at = timezone.now()
        update_fields.extend(['feedback_call_attempts', 'feedback_followup_called_at', 'feedback_due_at'])

        if job.feedback_call_attempts >= 3:
            job.feedback_followup_status = JobTicket.FEEDBACK_UNREACHABLE
            job.feedback_followup_note = _append_feedback_note(
                job.feedback_followup_note,
                f"Attempt {job.feedback_call_attempts}/3: No answer. Max attempts reached; marked unreachable."
            )
            success_message = f"Call attempt {job.feedback_call_attempts}/3 recorded. Customer marked as unreachable."
        else:
            job.feedback_followup_status = JobTicket.FEEDBACK_NO_ANSWER
            job.feedback_due_at = timezone.now() + timedelta(days=1)
            job.feedback_followup_note = _append_feedback_note(
                job.feedback_followup_note,
                f"Attempt {job.feedback_call_attempts}/3: No answer. Rescheduled for tomorrow."
            )
            success_message = f"No answer recorded (Attempt {job.feedback_call_attempts}/3). Re-queued for tomorrow."
    elif action == 'call_later':
        job.feedback_followup_status = JobTicket.FEEDBACK_CALL_LATER
        job.feedback_followup_called_at = timezone.now()
        update_fields.extend(['feedback_followup_called_at', 'feedback_due_at'])

        callback_date_str = (request.POST.get('callback_date') or '').strip()
        callback_preset = (request.POST.get('callback_preset') or '').strip()

        target_due = None
        if callback_date_str:
            try:
                target_date = datetime.strptime(callback_date_str, '%Y-%m-%d').date()
                target_due = timezone.make_aware(datetime.combine(target_date, datetime.min.time()))
            except ValueError:
                pass

        if not target_due:
            if callback_preset == '1_day':
                target_due = timezone.now() + timedelta(days=1)
            else:
                target_due = timezone.now() + timedelta(days=2)

        job.feedback_due_at = target_due
        formatted_date = target_due.strftime('%d %b %Y')
        job.feedback_followup_note = _append_feedback_note(
            job.feedback_followup_note,
            f"Callback requested on {formatted_date}. {note}".strip()
        )
        success_message = f"Callback scheduled for {formatted_date}."
    elif action in status_map:
        job.feedback_followup_status = status_map[action]
        job.feedback_followup_called_at = timezone.now()
        update_fields.append('feedback_followup_called_at')
        if rating_raw:
            try:
                rating = int(rating_raw)
                if 1 <= rating <= 10:
                    job.feedback_rating = rating
                    job.feedback_comment = note or job.feedback_comment
                    job.feedback_date = timezone.now()
                    update_fields.extend(['feedback_rating', 'feedback_comment', 'feedback_date'])
            except (TypeError, ValueError):
                pass
        success_message = f'Feedback follow-up marked as {dict(JobTicket.FEEDBACK_FOLLOWUP_CHOICES)[job.feedback_followup_status]}.'
    else:
        messages.error(request, 'Invalid feedback follow-up action.')
        return _safe_next_redirect(request, 'staff_job_detail', job_code=job.job_code)

    job.save(update_fields=list(dict.fromkeys(update_fields)))
    JobTicketLog.objects.create(
        job_ticket=job,
        user=request.user,
        action='FEEDBACK',
        details=(
            f"Feedback follow-up changed from '{old_status}' to "
            f"'{job.get_feedback_followup_status_display()}'."
        ),
    )
    messages.success(request, success_message)
    return _safe_next_redirect(request, 'staff_job_detail', job_code=job.job_code)


@login_required
def job_billing_print_view(request, job_code):
    denied = _staff_access_required(request, "staff_dashboard")
    if denied:
        return denied
    
    job = get_object_or_404(
        JobTicket.objects.select_related('workspace', 'assigned_to__user', 'created_by', 'rack'),
        job_code=job_code,
    )
    if not user_has_workspace_access(request.user, job.workspace):
        return redirect('unauthorized')

    context = build_job_billing_context(job, request=request)
    return render(request, 'job_tickets/job_billing_print.html', context)

@login_required
def staff_job_detail(request, job_code):
    denied = _staff_access_required(request, "staff_dashboard")
    if denied:
        return denied

    job = get_object_or_404(
        JobTicket.objects.select_related('original_job_ticket', 'rack', 'assigned_to__user', 'created_by').prefetch_related('photos'),
        job_code=job_code
    )
    if not user_has_workspace_access(request.user, job.workspace):
        return redirect('unauthorized')
    session = getattr(request, 'session', None)
    if session is not None:
        session['vendor_details_unlocked'] = (
            session.get('vendor_details_unlocked_job_code') == job.job_code
        )
    checklist_schema, checklist_title, checklist_notes = _build_checklist_schema_for_job(job)

    if request.method == 'POST':
        action = (request.POST.get('action') or '').strip()
        if action == 'update_status':
            if not user_has_staff_access(request.user, 'job_status_change'):
                messages.error(request, 'You do not have permission to change job status.')
                return redirect('staff_job_detail', job_code=job_code)

            new_status = (request.POST.get('status') or '').strip()
            valid_statuses = {value for value, _label in JobTicket.STATUS_CHOICES}

            if new_status not in valid_statuses:
                messages.error(request, 'Invalid job status selected.')
                return redirect('staff_job_detail', job_code=job_code)

            if new_status == job.status:
                messages.info(request, 'Status is already up to date.')
                return redirect('staff_job_detail', job_code=job_code)

            old_status = job.get_status_display()
            with transaction.atomic():
                job.status = new_status
                update_fields = ['status', 'updated_at']
                if new_status == 'Closed' and not job.closed_at:
                    job.closed_at = timezone.now()
                    update_fields.append('closed_at')
                if new_status == 'Closed' and not job.feedback_due_at:
                    job.feedback_due_at = _feedback_due_at_from_closed_at(job.closed_at or timezone.now())
                    update_fields.append('feedback_due_at')
                if new_status == 'Closed':
                    job.feedback_followup_enabled = True
                    job.feedback_followup_status = (
                        JobTicket.FEEDBACK_RECEIVED if job.feedback_rating else JobTicket.FEEDBACK_PENDING
                    )
                    update_fields.append('feedback_followup_enabled')
                    update_fields.append('feedback_followup_status')
                elif new_status != 'Closed' and job.closed_at:
                    job.closed_at = None
                    job.feedback_due_at = None
                    job.feedback_followup_enabled = False
                    job.feedback_message_sent_at = None
                    job.feedback_followup_status = JobTicket.FEEDBACK_PENDING
                    job.feedback_followup_called_at = None
                    job.feedback_followup_marked_by = None
                    update_fields.append('closed_at')
                    update_fields.extend([
                        'feedback_due_at',
                        'feedback_followup_enabled',
                        'feedback_message_sent_at',
                        'feedback_followup_status',
                        'feedback_followup_called_at',
                        'feedback_followup_marked_by',
                    ])
                update_fields = list(dict.fromkeys(update_fields))
                job.save(update_fields=update_fields)
                if new_status == 'Specialized Service':
                    service, _created = SpecializedService.objects.get_or_create(job_ticket=job)
                    if service.status == 'Returned from Vendor':
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
                details = f"Staff changed status from '{old_status}' to '{job.get_status_display()}'."
                JobTicketLog.objects.create(job_ticket=job, user=request.user, action='STATUS', details=details)

            send_job_update_message(job.job_code, job.status)
            messages.success(request, f"Status updated to {job.get_status_display()}.")
            return redirect('staff_job_detail', job_code=job_code)

        if action == 'update_checklist':
            posted_answers, missing_required_labels, invalid_option_labels = _extract_checklist_answers_from_post(
                request.POST,
                checklist_schema,
            )

            if invalid_option_labels:
                messages.error(
                    request,
                    "Invalid checklist selection for: "
                    + ', '.join(invalid_option_labels[:6])
                    + ('...' if len(invalid_option_labels) > 6 else ''),
                )
                return redirect('staff_job_detail', job_code=job_code)

            if missing_required_labels:
                messages.error(request, _format_checklist_required_error(missing_required_labels))
                return redirect('staff_job_detail', job_code=job_code)

            old_answers = _get_job_checklist_answers(job)
            merged_answers = _merge_checklist_answers(old_answers, posted_answers)

            if merged_answers == old_answers:
                messages.info(request, 'No checklist changes detected.')
                return redirect('staff_job_detail', job_code=job_code)

            job.technician_checklist = merged_answers
            job.save(update_fields=['technician_checklist', 'updated_at'])

            change_details = []
            for field in checklist_schema:
                key = field['key']
                field_type = field.get('type')
                if field_type == 'checkbox':
                    old_value = _normalize_checkbox_answer(old_answers.get(key, ''))
                    new_value = _normalize_checkbox_answer(merged_answers.get(key, ''))
                    old_text = 'Verified' if old_value == '1' else 'Not Verified'
                    new_text = 'Verified' if new_value == '1' else 'Not Verified'
                else:
                    old_value = _normalize_checklist_answer(old_answers.get(key, ''))
                    new_value = _normalize_checklist_answer(merged_answers.get(key, ''))
                    old_text = old_value or 'blank'
                    new_text = new_value or 'blank'

                if old_value != new_value:
                    change_details.append(f"{field['label']}: '{old_text}' -> '{new_text}'")

            if change_details:
                details = "Staff updated inspection checklist: " + "; ".join(change_details[:10])
                if len(change_details) > 10:
                    details += "..."
            else:
                details = "Staff updated inspection checklist."

            JobTicketLog.objects.create(
                job_ticket=job,
                user=request.user,
                action='NOTE',
                details=details,
            )
            messages.success(request, 'Inspection checklist updated.')
            return redirect('staff_job_detail', job_code=job_code)

        if action in {'save_estimation', 'send_estimation_whatsapp'}:
            estimation_error = _save_job_estimation_from_post(request, job)
            if estimation_error:
                messages.error(request, estimation_error)
                return redirect('staff_job_detail', job_code=job_code)

            if action == 'save_estimation':
                messages.success(request, 'Estimate note saved.')
                return redirect('staff_job_detail', job_code=job_code)

            if job.estimated_amount is None:
                messages.error(request, 'Enter estimate amount before sending WhatsApp.')
                return redirect('staff_job_detail', job_code=job_code)

            result = queue_job_whatsapp_message(job, MessageQueue.EVENT_ESTIMATE)
            if result.get('ok'):
                queue_id = (result.get('data') or {}).get('message_queue_id')
                queue = MessageQueue.objects.filter(id=queue_id).first() if queue_id else None
                if queue and queue.status == MessageQueue.STATUS_FAILED:
                    messages.error(request, queue.error_message or 'Estimate WhatsApp failed.')
                else:
                    active_reminder = _active_job_reminder(job)
                    if active_reminder:
                        now = timezone.now()
                        active_reminder.status = JobReminder.STATUS_DONE
                        active_reminder.completed_at = now
                        active_reminder.save(update_fields=['status', 'completed_at', 'updated_at'])
                        JobTicketLog.objects.create(
                            job_ticket=job,
                            user=request.user,
                            action='NOTE',
                            details='Callback reminder completed after estimate WhatsApp was queued.',
                        )
                    messages.success(request, 'Estimate WhatsApp queued.')
            else:
                messages.error(request, result.get('error') or 'Unable to queue estimate WhatsApp.')
            return redirect('staff_job_detail', job_code=job_code)

        if action in {'schedule_reminder', 'reschedule_reminder'}:
            due_at, reminder_error = _parse_reminder_due_at(request.POST, require_value=False)
            if due_at is None and not reminder_error:
                reminder_delta, reminder_error = _parse_reminder_offset(request.POST, require_value=True)
                due_at = timezone.now() + reminder_delta if reminder_delta else None
            if reminder_error:
                messages.error(request, reminder_error)
                return redirect('staff_job_detail', job_code=job_code)

            reminder_id = (request.POST.get('reminder_id') or '').strip()
            reminder = None
            if reminder_id:
                reminder = get_object_or_404(JobReminder, id=reminder_id, job_ticket=job)
            if reminder is None:
                reminder = _active_job_reminder(job)

            local_due_at = timezone.localtime(due_at).strftime('%d-%m-%Y %I:%M %p')
            if reminder:
                old_due_at = timezone.localtime(reminder.due_at).strftime('%d-%m-%Y %I:%M %p')
                reminder.due_at = due_at
                reminder.status = JobReminder.STATUS_PENDING
                reminder.completed_at = None
                reminder.last_prompted_at = None
                reminder.save(update_fields=['due_at', 'status', 'completed_at', 'last_prompted_at', 'updated_at'])
                details = f"Callback reminder rescheduled from {old_due_at} to {local_due_at}."
            else:
                JobReminder.objects.create(
                    job_ticket=job,
                    due_at=due_at,
                    created_by=request.user,
                )
                details = f"Callback reminder scheduled for {local_due_at}."

            JobTicketLog.objects.create(job_ticket=job, user=request.user, action='NOTE', details=details)
            messages.success(request, 'Reminder scheduled.')
            return redirect('staff_job_detail', job_code=job_code)

        if action == 'complete_reminder':
            reminder_id = (request.POST.get('reminder_id') or '').strip()
            reminder = get_object_or_404(JobReminder, id=reminder_id, job_ticket=job)
            if reminder.status != JobReminder.STATUS_DONE:
                reminder.status = JobReminder.STATUS_DONE
                reminder.completed_at = timezone.now()
                reminder.save(update_fields=['status', 'completed_at', 'updated_at'])
                JobTicketLog.objects.create(
                    job_ticket=job,
                    user=request.user,
                    action='NOTE',
                    details='Callback reminder marked done.',
                )
                messages.success(request, 'Reminder marked done.')
            else:
                messages.info(request, 'Reminder is already done.')
            return redirect('staff_job_detail', job_code=job_code)

        if action == 'retry_whatsapp_message':
            queue_id = (request.POST.get('queue_id') or '').strip()
            queue_obj = MessageQueue.objects.filter(pk=queue_id, job_ticket=job).first()
            if queue_obj:
                queue_obj.status = MessageQueue.STATUS_PENDING
                queue_obj.error_message = ''
                queue_obj.save(update_fields=['status', 'error_message', 'updated_at'])
                dispatch_message_queue_task(queue_obj.id)
                messages.success(request, f"WhatsApp message ({queue_obj.get_event_type_display()}) re-queued.")
            else:
                messages.error(request, 'WhatsApp queue item not found.')
            return redirect('staff_job_detail', job_code=job_code)

        if action == 'send_custom_whatsapp':
            custom_msg = (request.POST.get('custom_message') or '').strip()
            if not custom_msg:
                messages.error(request, 'Please enter a message to send.')
                return redirect('staff_job_detail', job_code=job_code)
            result = create_message_queue(
                job.customer_phone,
                job=job,
                event_type=MessageQueue.EVENT_MANUAL,
                message=custom_msg,
            )
            if result.get('ok'):
                JobTicketLog.objects.create(
                    job_ticket=job,
                    user=request.user,
                    action='NOTE',
                    details=f"[WhatsApp Outbound Message]: {custom_msg[:500]}",
                )
                messages.success(request, 'WhatsApp message queued for delivery.')
            else:
                messages.error(request, result.get('error') or 'Failed to queue WhatsApp message.')
            return redirect('staff_job_detail', job_code=job_code)

        if action == 'edit_customer_device_details':
            new_customer_name = (request.POST.get('customer_name') or '').strip()
            new_customer_phone_raw = (request.POST.get('customer_phone') or '').strip()
            new_device_type = (request.POST.get('device_type') or '').strip()
            new_device_brand = (request.POST.get('device_brand') or '').strip()
            new_device_model = (request.POST.get('device_model') or '').strip()
            new_device_serial = (request.POST.get('device_serial') or '').strip()
            new_device_password = (request.POST.get('device_password') or '').strip()
            new_reported_issue = (request.POST.get('reported_issue') or '').strip()
            new_additional_items = (request.POST.get('additional_items') or '').strip()
            new_intake_date_raw = (request.POST.get('intake_date') or '').strip()
            edit_reason = (request.POST.get('edit_reason') or '').strip()

            parsed_new_intake_date = None
            if new_intake_date_raw:
                try:
                    parsed_new_intake_date = datetime.strptime(new_intake_date_raw, '%Y-%m-%d').date()
                except Exception:
                    pass

            if not new_customer_name:
                messages.error(request, 'Customer name is required.')
                return redirect('staff_job_detail', job_code=job_code)

            new_customer_phone, phone_error = normalize_indian_phone(new_customer_phone_raw, required=True, field_label='Customer Phone')
            if phone_error:
                messages.error(request, phone_error)
                return redirect('staff_job_detail', job_code=job_code)

            if not new_device_type:
                messages.error(request, 'Device type is required.')
                return redirect('staff_job_detail', job_code=job_code)

            if not new_reported_issue:
                messages.error(request, 'Reported issue is required.')
                return redirect('staff_job_detail', job_code=job_code)

            changes = []
            if job.customer_name != new_customer_name:
                changes.append(f"Customer Name: '{job.customer_name}' -> '{new_customer_name}'")
            if job.customer_phone != new_customer_phone:
                changes.append(f"Customer Phone: '{job.customer_phone}' -> '{new_customer_phone}'")
            if parsed_new_intake_date and job.intake_date != parsed_new_intake_date:
                old_intake_display = job.intake_date.strftime('%d-%m-%Y') if job.intake_date else 'N/A'
                changes.append(f"Intake Date: '{old_intake_display}' -> '{parsed_new_intake_date.strftime('%d-%m-%Y')}'")
            if job.device_type != new_device_type:
                changes.append(f"Device Type: '{job.device_type}' -> '{new_device_type}'")
            if (job.device_brand or '') != new_device_brand:
                changes.append(f"Device Brand: '{job.device_brand or 'N/A'}' -> '{new_device_brand or 'N/A'}'")
            if (job.device_model or '') != new_device_model:
                changes.append(f"Device Model: '{job.device_model or 'N/A'}' -> '{new_device_model or 'N/A'}'")
            if (job.device_serial or '') != new_device_serial:
                changes.append(f"Serial: '{job.device_serial or 'N/A'}' -> '{new_device_serial or 'N/A'}'")
            if (job.device_password or '') != new_device_password:
                changes.append(f"Device Password: '{job.device_password or 'None'}' -> '{new_device_password or 'None'}'")
            if (job.reported_issue or '') != new_reported_issue:
                changes.append("Reported Issue updated")
            if (job.additional_items or '') != new_additional_items:
                changes.append(f"Additional Items: '{job.additional_items or 'None'}' -> '{new_additional_items or 'None'}'")

            if not changes:
                messages.info(request, 'No changes were detected.')
                return redirect('staff_job_detail', job_code=job_code)

            target_ws = getattr(request, 'current_workspace', None) or job.workspace
            with transaction.atomic():
                old_phone = job.customer_phone
                job.customer_name = new_customer_name
                job.customer_phone = new_customer_phone
                if parsed_new_intake_date:
                    job.intake_date = parsed_new_intake_date
                job.device_type = new_device_type
                job.device_brand = new_device_brand
                job.device_model = new_device_model
                job.device_serial = new_device_serial
                job.device_password = new_device_password
                job.reported_issue = new_reported_issue
                job.additional_items = new_additional_items
                job.save(update_fields=[
                    'customer_name', 'customer_phone', 'intake_date', 'device_type',
                    'device_brand', 'device_model', 'device_serial', 'device_password',
                    'reported_issue', 'additional_items', 'updated_at'
                ])

                # Sync Client model if needed
                if target_ws:
                    client = Client.objects.filter(
                        workspace=target_ws,
                        phone__in=phone_lookup_variants(old_phone)
                    ).first()
                    if client:
                        client_updates = []
                        if client.phone != new_customer_phone:
                            existing_with_new_phone = Client.objects.filter(
                                workspace=target_ws,
                                phone=new_customer_phone
                            ).exclude(id=client.id).first()
                            if not existing_with_new_phone:
                                client.phone = new_customer_phone
                                client_updates.append('phone')
                        if client.name != new_customer_name:
                            client.name = new_customer_name
                            client_updates.append('name')
                        if client_updates:
                            client_updates.append('updated_at')
                            client.save(update_fields=client_updates)

                log_details = '; '.join(changes)
                if edit_reason:
                    log_details += f" (Reason: {edit_reason})"
                JobTicketLog.objects.create(
                    job_ticket=job,
                    user=request.user,
                    action='DETAILS',
                    details=log_details
                )

            messages.success(request, 'Customer and device details updated successfully.')
            return redirect('staff_job_detail', job_code=job_code)

    job_tickets = [job]
    calculate_job_totals(job_tickets)
    
    history_logs = job.logs.all().select_related('user')
    specialized_service = SpecializedService.objects.filter(job_ticket=job).first()
    technician_list = get_assignable_technician_queryset(getattr(request, 'current_workspace', None) or job.workspace)
    job_reminders = job.reminders.select_related('created_by').order_by('-due_at', '-id')
    active_reminder = _active_job_reminder(job)
    reminder_schedule = _default_reminder_schedule(active_reminder)
    
    if job.customer_group_id:
        related_jobs = JobTicket.objects.filter(
            customer_group_id=job.customer_group_id
        ).exclude(job_code=job.job_code).order_by('-created_at')
    else:
        related_jobs = JobTicket.objects.none()
    
    subtotal = job.total
    grand_total = subtotal - job.discount_amount
    amount_paid = job.amount_paid or Decimal('0.00')
    balance_due = max(Decimal('0.00'), grand_total - amount_paid)
    job_payments = (
        InventoryCreditPayment.objects.filter(bill__job_ticket=job)
        .select_related('created_by')
        .order_by('-payment_date', '-id')
    )
    
    # Generate QR code URL
    qr_url = request.build_absolute_uri(f'/qr/{job.job_code}/')

    whatsapp_settings = WhatsAppIntegrationSettings.get_settings(job.workspace)
    clean_target = _phone_to_international(job.customer_phone, whatsapp_settings.default_country_code)
    import urllib.parse
    wa_default_msg = urllib.parse.quote(
        f"Hello {job.customer_name}, regarding your service ticket {job.job_code} ({job.device_brand} {job.device_model}): "
    )
    whatsapp_direct_url = f"https://wa.me/{clean_target}?text={wa_default_msg}" if clean_target else ''
    whatsapp_queue_history = job.message_queues.all().order_by('-created_at')[:10]

    job_expenses = job.expenses.all().select_related('recorded_by').order_by('-expense_date', '-id')
    job_expenses_total = job_expenses.aggregate(total=Coalesce(Sum('amount'), Decimal('0.00')))['total']
    vendor_payable = (
        Decimal(str(specialized_service.vendor_net_payable or 0))
        if specialized_service and specialized_service.vendor_cost
        else Decimal('0.00')
    )
    inventory_parts_cost = (
        ProductSale.objects.filter(job_ticket=job).aggregate(
            total=Coalesce(Sum('line_cost'), Decimal('0.00'))
        )['total']
    )
    job_net_profit = grand_total - vendor_payable - job_expenses_total - inventory_parts_cost

    job_expense_form = ExpenseForm(
        initial={'job_ticket': job.id, 'category': Expense.CATEGORY_JOB_PARTS_OUTSOURCE},
        workspace=job.workspace,
    )

    # Available active racks for moving/reassigning device storage
    racks_qs = DeviceRack.objects.filter(is_active=True)
    if job.workspace_id:
        racks_qs = racks_qs.filter(workspace=job.workspace)
    available_racks = list(racks_qs.order_by('group', 'name'))

    context = {
        'job': job,
        'service_logs': job.service_logs.all(),
        'history_logs': history_logs,
        'total_parts_cost': job.part_total,
        'total_service_charges': job.service_total,
        'subtotal': subtotal,
        'discount_amount': job.discount_amount,
        'grand_total': grand_total,
        'amount_paid': amount_paid,
        'balance_due': balance_due,
        'job_payments': job_payments,
        'payment_methods': InventoryCreditPayment.METHOD_CHOICES,
        'specialized_service': specialized_service,
        'related_jobs': related_jobs,
        'technician_list': technician_list,
        'ReassignTechnicianForm': ReassignTechnicianForm(workspace=getattr(request, 'current_workspace', None) or job.workspace),
        'qr_url': qr_url,
        'checklist_schema': checklist_schema,
        'checklist_title': checklist_title,
        'checklist_notes': checklist_notes,
        'staff_status_choices': JobTicket.STATUS_CHOICES,
        'today': timezone.localdate().isoformat(),
        'job_reminders': job_reminders,
        'active_reminder': active_reminder,
        'reminder_schedule': reminder_schedule,
        'whatsapp_direct_url': whatsapp_direct_url,
        'whatsapp_clean_phone': clean_target,
        'whatsapp_queue_history': whatsapp_queue_history,
        'whatsapp_settings': whatsapp_settings,
        'job_expenses': job_expenses,
        'job_expenses_total': job_expenses_total,
        'inventory_parts_cost': inventory_parts_cost,
        'job_net_profit': job_net_profit,
        'job_expense_form': job_expense_form,
        'available_racks': available_racks,
        'financial_accounts': list(
            FinancialAccount.objects.filter(
                Q(workspace=getattr(request, 'current_workspace', None) or job.workspace) | Q(workspace__isnull=True),
                is_active=True
            ).order_by('-is_default_cash', '-is_default_bank', 'name')
        ),
    }
    return render(request, 'job_tickets/staff_job_detail.html', context)


@login_required
@require_POST
def staff_update_job_rack(request, job_code):
    """Update or move the physical storage rack location for a device."""
    denied = _staff_access_required(request, "staff_dashboard")
    if denied:
        return denied

    job = get_object_or_404(JobTicket, job_code=job_code)
    if not user_has_workspace_access(request.user, job.workspace):
        return redirect('unauthorized')

    rack_id_raw = (request.POST.get('rack_id') or '').strip()
    rack_col_raw = (request.POST.get('rack_column') or '').strip()
    old_rack = job.rack
    old_col = job.rack_column
    new_rack = None
    new_col = None

    if rack_id_raw:
        rack_qs = DeviceRack.objects.filter(id=rack_id_raw, is_active=True)
        if job.workspace_id:
            rack_qs = rack_qs.filter(workspace=job.workspace)
        new_rack = rack_qs.first()
        if not new_rack:
            messages.error(request, "Selected rack was not found or is inactive.")
            return _safe_next_redirect(request, 'staff_job_detail', job_code=job.job_code)
        if rack_col_raw:
            try:
                parsed_col = int(rack_col_raw)
                if 1 <= parsed_col <= max(1, new_rack.total_columns or 100):
                    new_col = parsed_col
            except (ValueError, TypeError):
                new_col = None

    if old_rack == new_rack and old_col == new_col:
        messages.info(request, "Rack location is already up to date.")
        return _safe_next_redirect(request, 'staff_job_detail', job_code=job.job_code)

    job.rack = new_rack
    job.rack_column = new_col
    job.save(update_fields=['rack', 'rack_column', 'updated_at'])

    def _fmt_loc(r, c):
        if not r:
            return "None"
        if c:
            return f"'{r.name}' - Col {c} ({r.group})"
        return f"'{r.name}' ({r.group})"

    old_desc = _fmt_loc(old_rack, old_col)
    new_desc = _fmt_loc(new_rack, new_col)
    log_detail = f"Rack location changed from {old_desc} to {new_desc}."
    JobTicketLog.objects.create(job_ticket=job, user=request.user, action='STATUS', details=log_detail)

    if new_rack:
        col_txt = f" - Col {new_col}" if new_col else ""
        messages.success(request, f"Device moved to {new_rack.name}{col_txt} ({new_rack.group}).")
    else:
        messages.success(request, "Device released from rack storage.")

    return _safe_next_redirect(request, 'staff_job_detail', job_code=job.job_code)


@login_required
@require_POST
def staff_job_collect_payment(request, job_code):
    denied = _staff_access_required(request, "staff_dashboard")
    if denied:
        return denied

    job = get_object_or_404(JobTicket, job_code=job_code)
    if not user_has_workspace_access(request.user, job.workspace):
        return redirect('unauthorized')

    calculate_job_totals([job])
    grand_total = max(Decimal('0.00'), (job.total or Decimal('0.00')) - (job.discount_amount or Decimal('0.00')))
    current_paid = job.amount_paid or Decimal('0.00')
    balance_due = max(Decimal('0.00'), grand_total - current_paid)

    if balance_due <= Decimal('0.00'):
        messages.info(request, f"Job {job.job_code} is already fully settled.")
        return redirect('staff_job_detail', job_code=job_code)

    amount_raw = (request.POST.get('amount') or '').strip()
    try:
        amount = Decimal(amount_raw).quantize(Decimal('0.01'))
    except (InvalidOperation, ValueError, TypeError):
        messages.error(request, "Please enter a valid payment amount.")
        return redirect('staff_job_detail', job_code=job_code)

    if amount <= Decimal('0.00'):
        messages.error(request, "Payment amount must be greater than zero.")
        return redirect('staff_job_detail', job_code=job_code)

    if amount > balance_due:
        messages.error(request, f"Payment amount (Rs.{amount}) cannot exceed current balance due of Rs.{balance_due}.")
        return redirect('staff_job_detail', job_code=job_code)

    financial_account_id = request.POST.get('financial_account')
    target_account = None
    if financial_account_id:
        target_account = FinancialAccount.objects.filter(
            Q(workspace=getattr(request, 'current_workspace', None) or job.workspace) | Q(workspace__isnull=True),
            id=financial_account_id,
            is_active=True,
        ).first()

    raw_method = (request.POST.get('payment_method') or '').strip()
    custom_method = (request.POST.get('custom_payment_method') or '').strip()
    if raw_method in {'custom', 'other'} and custom_method:
        payment_method_display = custom_method
        inv_method = InventoryCreditPayment.METHOD_CASH
    elif raw_method and raw_method in dict(InventoryCreditPayment.METHOD_CHOICES):
        inv_method = raw_method
        payment_method_display = dict(InventoryCreditPayment.METHOD_CHOICES).get(inv_method, inv_method)
    elif target_account:
        if target_account.account_type == FinancialAccount.ACCOUNT_TYPE_CASH:
            inv_method = InventoryCreditPayment.METHOD_CASH
            payment_method_display = 'Cash'
        elif target_account.account_type == FinancialAccount.ACCOUNT_TYPE_UPI:
            inv_method = InventoryCreditPayment.METHOD_UPI
            payment_method_display = 'UPI / QR'
        else:
            inv_method = InventoryCreditPayment.METHOD_TRANSFER
            payment_method_display = 'Bank Transfer'
    else:
        inv_method = InventoryCreditPayment.METHOD_CASH
        payment_method_display = 'Cash'

    if not target_account:
        target_account = FinancialAccount.get_default_for_payment_method(job.workspace, inv_method)

    payment_reference = (request.POST.get('payment_reference') or '').strip()
    notes = (request.POST.get('notes') or '').strip()

    # Dynamic Custom Payment Fields
    custom_field_names = request.POST.getlist('custom_field_name[]')
    custom_field_values = request.POST.getlist('custom_field_value[]')
    custom_pairs = []
    for name, val in zip(custom_field_names, custom_field_values):
        k = (name or '').strip()
        v = (val or '').strip()
        if k and v:
            custom_pairs.append(f"{k}: {v}")

    custom_fields_str = f" [{', '.join(custom_pairs)}]" if custom_pairs else ""

    payment_date_raw = (request.POST.get('payment_date') or '').strip()
    if payment_date_raw:
        try:
            payment_date = datetime.strptime(payment_date_raw, '%Y-%m-%d').date()
        except ValueError:
            payment_date = timezone.localdate()
    else:
        payment_date = timezone.localdate()

    with transaction.atomic():
        new_paid = (current_paid + amount).quantize(Decimal('0.01'))
        new_balance = max(Decimal('0.00'), grand_total - new_paid)
        job.amount_paid = new_paid
        job.payment_status = 'paid' if new_balance <= Decimal('0.00') else 'part_paid'
        job.payment_method = payment_method_display
        job.financial_account = target_account
        if payment_reference:
            job.payment_reference = payment_reference
        job.payment_date = timezone.now()
        job.save(update_fields=['amount_paid', 'payment_status', 'payment_method', 'payment_reference', 'financial_account', 'payment_date', 'updated_at'])

        # Sync with Inventory Party & Bill
        party = _get_or_create_inventory_customer_party_for_job(job)
        bill = InventoryBill.objects.filter(entry_type='sale', job_ticket=job).first()
        if not bill:
            bill = InventoryBill.objects.create(
                workspace=job.workspace,
                bill_number=_generate_inventory_bill_number('sale', payment_date, job.workspace),
                entry_type='sale',
                entry_date=payment_date,
                invoice_number=job.vyapar_invoice_number or '',
                job_ticket=job,
                party=party,
                notes=f"Repair Job {job.job_code} settlement",
                created_by=request.user,
            )

        pay_note = f"Collected for Job {job.job_code} via {payment_method_display}.{custom_fields_str} {notes}".strip()
        inv_payment = InventoryCreditPayment.objects.create(
            workspace=job.workspace,
            party=party,
            bill=bill,
            direction=InventoryCreditPayment.DIRECTION_RECEIVABLE,
            payment_date=payment_date,
            payment_method=inv_method,
            financial_account=target_account,
            amount=amount,
            balance_before=balance_due,
            balance_after=new_balance,
            reference_no=payment_reference,
            notes=pay_note,
            created_by=request.user,
        )

        if target_account and amount > Decimal('0.00'):
            target_account.deposit(
                amount=amount,
                description=f"Job Balance Payment: {job.job_code} ({job.customer_name})",
                reference_no=payment_reference,
                transaction_date=payment_date,
                job_ticket=job,
                inventory_credit_payment=inv_payment,
                user=request.user,
            )

        acc_text = f" into {target_account.name}" if target_account else f" via {payment_method_display}"
        log_details = f"Collected installment payment of Rs.{amount}{acc_text}."
        if payment_reference:
            log_details += f" Ref: {payment_reference}."
        if custom_pairs:
            log_details += f" Custom Fields: {', '.join(custom_pairs)}."
        log_details += f" Remaining Balance: Rs.{new_balance} ({job.get_payment_status_display()})."
        JobTicketLog.objects.create(job_ticket=job, user=request.user, action='PAYMENT', details=log_details)

    send_job_update_message(job.job_code, job.status)
    messages.success(request, f"Payment of Rs.{amount} collected successfully via {payment_method_display}. Remaining balance: Rs.{new_balance}.")
    return redirect('staff_job_detail', job_code=job_code)

@login_required
@require_POST
def staff_delete_job_photo(request, job_code, photo_id):
    denied = _staff_access_required(request, "staff_dashboard")
    if denied:
        return denied

    job = get_object_or_404(JobTicket, job_code=job_code)
    if not user_has_workspace_access(request.user, job.workspace):
        return redirect('unauthorized')
    photo = get_object_or_404(JobTicketPhoto, id=photo_id, job_ticket=job)

    try:
        if photo.image:
            photo.image.delete(save=False)
    except Exception:
        pass

    photo.delete()
    messages.success(request, 'Photo deleted successfully.')
    return redirect('staff_job_detail', job_code=job_code)

@login_required
def staff_job_photo_file(request, job_code, photo_id):
    job = get_object_or_404(JobTicket, job_code=job_code)
    photo = get_object_or_404(JobTicketPhoto, id=photo_id, job_ticket=job)

    # Permission check: allow superusers, assigned technicians, technicians in workspace, and authorized staff/workspace users
    can_view = False
    if request.user.is_superuser:
        can_view = True
    elif job.assigned_to and job.assigned_to.user_id == request.user.id:
        can_view = True
    elif request.user.groups.filter(name='Technicians').exists():
        tech_profile = getattr(request.user, 'technician_profile', None)
        if not tech_profile:
            tech_profile = TechnicianProfile.objects.filter(user=request.user).first()
        if tech_profile and (not job.workspace or not tech_profile.workspace or tech_profile.workspace_id == job.workspace_id):
            can_view = True
    elif request.user.is_staff and user_has_staff_access(request.user, "staff_dashboard"):
        if user_has_workspace_access(request.user, job.workspace):
            can_view = True
    elif user_has_workspace_access(request.user, job.workspace):
        can_view = True

    if not can_view:
        return redirect('unauthorized')

    if photo.image_data:
        response = HttpResponse(photo.image_data, content_type=photo.image_content_type or 'application/octet-stream')
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

@login_required
@require_POST
def unlock_vendor_details(request, job_code):
    """Unlock vendor details section with password verification."""
    if not request.user.is_staff or not user_has_staff_access(request.user, "staff_dashboard"):
        return JsonResponse({'ok': False, 'error': 'Unauthorized'}, status=403)
    
    password = request.POST.get('vendor_password', '').strip()
    
    # Check against the authenticated user's password.
    from django.contrib.auth import authenticate
    user_auth = authenticate(username=request.user.username, password=password)
    
    if user_auth is not None:
        job = get_object_or_404(JobTicket, job_code=job_code)
        if not user_has_workspace_access(request.user, job.workspace):
            return JsonResponse({'ok': False, 'error': 'Unauthorized'}, status=403)
        request.session['vendor_details_unlocked_job_code'] = job_code
        request.session['vendor_details_unlocked'] = True
        request.session.modified = True
        request.session.set_expiry(3600)

        # Get specialized service data for AJAX response
        specialized_service = SpecializedService.objects.filter(job_ticket=job).first()
        
        if request.headers.get('x-requested-with') == 'XMLHttpRequest':
            return JsonResponse({
                'ok': True, 
                'message': 'Vendor details unlocked.',
                'vendor_name': str(specialized_service.vendor) if specialized_service and specialized_service.vendor else 'N/A',
                'status': specialized_service.get_status_display() if specialized_service else 'N/A',
                'vendor_cost': float(specialized_service.vendor_cost) if specialized_service and specialized_service.vendor_cost else 0,
                'vendor_discount_amount': float(specialized_service.vendor_discount_amount) if specialized_service else 0,
                'vendor_paid_amount': float(specialized_service.vendor_paid_amount) if specialized_service else 0,
                'vendor_balance_amount': float(specialized_service.vendor_balance_amount) if specialized_service else 0,
                'vendor_net_payable': float(specialized_service.vendor_net_payable) if specialized_service else 0,
                'vendor_payment_status': specialized_service.vendor_payment_status if specialized_service else 'N/A',
                'client_charge': float(specialized_service.client_charge) if specialized_service and specialized_service.client_charge else 0,
                'sent_date': specialized_service.sent_date.strftime('%Y-%m-%d') if specialized_service and specialized_service.sent_date else None,
                'returned_date': specialized_service.returned_date.strftime('%Y-%m-%d') if specialized_service and specialized_service.returned_date else None,
                'vendor_bill_number': specialized_service.vendor_bill_number if specialized_service and specialized_service.vendor_bill_number else '',
                'service_id': specialized_service.id if specialized_service else None,
                'is_returned': (specialized_service.status == 'Returned from Vendor') if specialized_service else False,
                'is_closed': (job.status == 'Closed'),
            })
        messages.success(request, 'Vendor details unlocked.')
    else:
        if request.headers.get('x-requested-with') == 'XMLHttpRequest':
            return JsonResponse({'ok': False, 'error': 'Incorrect password.'}, status=400)
        messages.error(request, 'Incorrect password.')
    
    return redirect('staff_job_detail', job_code=job_code)

@login_required
@require_POST
def lock_vendor_details(request, job_code):
    """Lock vendor details section."""
    if not request.user.is_staff or not user_has_staff_access(request.user, "staff_dashboard"):
        return JsonResponse({'ok': False, 'error': 'Unauthorized'}, status=403)
    
    request.session.pop('vendor_details_unlocked_job_code', None)
    request.session['vendor_details_unlocked'] = False
    request.session.modified = True  # Ensure session is saved
    
    if request.headers.get('x-requested-with') == 'XMLHttpRequest':
        return JsonResponse({'ok': True, 'message': 'Vendor details locked.'})
    
    messages.info(request, 'Vendor details locked.')
    return redirect('staff_job_detail', job_code=job_code)


@login_required
@require_GET
def due_job_reminders_api(request):
    if not request.user.is_staff or not user_has_staff_access(request.user, "staff_dashboard"):
        return JsonResponse({'ok': False, 'error': 'Unauthorized'}, status=403)

    now = timezone.now()
    prompt_cutoff = now - timedelta(minutes=REMINDER_PROMPT_COOLDOWN_MINUTES)
    reminders = list(
        JobReminder.objects.filter(
            status=JobReminder.STATUS_PENDING,
            due_at__lte=now,
        )
        .exclude(job_ticket__status='Closed')
        .filter(Q(last_prompted_at__isnull=True) | Q(last_prompted_at__lte=prompt_cutoff))
        .select_related('job_ticket')
        .order_by('due_at', 'id')[:5]
    )

    reminder_ids = [reminder.id for reminder in reminders]
    if reminder_ids:
        JobReminder.objects.filter(id__in=reminder_ids).update(last_prompted_at=now)

    payload = []
    for reminder in reminders:
        job = reminder.job_ticket
        payload.append({
            'id': reminder.id,
            'job_code': job.job_code,
            'customer_name': job.customer_name,
            'customer_phone': job.customer_phone,
            'device': ' '.join(part for part in [job.device_type, job.device_brand, job.device_model] if part).strip(),
            'due_at': timezone.localtime(reminder.due_at).strftime('%d-%m-%Y %I:%M %p'),
            'url': reverse('staff_job_detail', args=[job.job_code]),
        })

    return JsonResponse({
        'ok': True,
        'count': len(payload),
        'cooldown_minutes': REMINDER_PROMPT_COOLDOWN_MINUTES,
        'reminders': payload,
    })


@login_required
def api_all_jobs(request):
    """API endpoint to fetch all jobs as JSON for real-time updates"""
    if not request.user.is_staff or not user_has_staff_access(request.user, "staff_dashboard"):
        return JsonResponse({'error': 'Unauthorized'}, status=403)
    
    jobs = JobTicket.objects.all().select_related(
        'assigned_to__user', 'created_by'
    ).prefetch_related('service_logs').order_by('-created_at')
    
    calculate_job_totals(jobs)
    
    jobs_data = []
    for job in jobs:
        jobs_data.append({
            'job_code': job.job_code,
            'customer_name': job.customer_name,
            'customer_phone': job.customer_phone,
            'device_type': job.device_type,
            'device_brand': job.device_brand or '',
            'device_model': job.device_model or '',
            'status': job.status,
            'status_display': job.get_status_display(),
            'assigned_to': job.assigned_to.user.username if job.assigned_to and job.assigned_to.user else None,
            'created_at': job.created_at.strftime('%Y-%m-%d %H:%M'),
            'updated_at': job.updated_at.strftime('%Y-%m-%d %H:%M'),
            'total': str(job.total) if job.total else '0.00',
            'part_total': str(job.part_total) if job.part_total else '0.00',
            'service_total': str(job.service_total) if job.service_total else '0.00',
            'discount_amount': str(job.discount_amount) if job.discount_amount else '0.00',
            'technician_notes': job.technician_notes or '',
            'url': f'/staff/job/{job.job_code}/',
        })
    
    return JsonResponse({'jobs': jobs_data})

@login_required
def print_active_workload_report(request):
    access = get_staff_access(request.user)
    if not request.user.is_staff or not access.get("reports_overview"):
        return redirect('unauthorized')
    
    # Define statuses that represent active work (In-Progress)
    active_statuses = ['Under Inspection', 'Repairing', 'Specialized Service', 'Returned']
    
    # 1. Fetch active jobs, excluding those that are ready for pickup or closed
    active_jobs = list(JobTicket.objects.filter(
        status__in=active_statuses
    ).select_related('assigned_to__user').prefetch_related('service_logs').order_by('assigned_to__user__username', 'job_code'))
    calculate_job_totals(active_jobs)
    for job in active_jobs:
        job.discount_total = _money_or_zero(job.discount_amount)
        job.net_total = _net_amount_after_discount(job.total, job.discount_total)

    # 2. Group the jobs by Technician for clear reporting
    grouped_jobs = {}
    
    for job in active_jobs:
        technician_name = job.assigned_to.user.username if job.assigned_to and job.assigned_to.user else "UNASSIGNED"
        
        if technician_name not in grouped_jobs:
            grouped_jobs[technician_name] = []
            
        grouped_jobs[technician_name].append(job)

    # 3. Prepare the final context
    company = CompanyProfile.get_profile()
    context = {
        'grouped_jobs': grouped_jobs,
        'report_date': datetime.now(),
        'company': company,
    }
    return render(request, 'job_tickets/print_active_workload_report.html', context)

@login_required
@require_POST
def job_reassign_staff(request, job_code):
    denied = _staff_access_required(request, "staff_dashboard")
    if denied:
        return denied
    
    job = get_object_or_404(JobTicket, job_code=job_code)
    
    form = ReassignTechnicianForm(request.POST, workspace=getattr(request, 'current_workspace', None) or job.workspace)

    if form.is_valid():
        new_technician = form.cleaned_data['new_technician']

        # Get old values for logging
        old_technician_name = job.assigned_to.user.username if job.assigned_to else "Unassigned"
        new_technician_name = new_technician.user.username if new_technician else "Unassigned"

        # Prevent assigning to the same person
        if job.assigned_to == new_technician and new_technician is not None:
            messages.warning(request, f"Job {job_code} is already assigned to {old_technician_name}.")
            return redirect('staff_job_detail', job_code=job_code)

        with transaction.atomic():
            job.assigned_to = new_technician
            job.is_new_assignment = True if new_technician else False
            job.save(update_fields=['assigned_to', 'is_new_assignment', 'updated_at'])

            details = f"Reassigned from '{old_technician_name}' to '{new_technician_name}' by staff."
            JobTicketLog.objects.create(job_ticket=job, user=request.user, action='ASSIGNED', details=details)

            send_job_update_message(job.job_code, job.status, is_assignment=bool(new_technician))

        messages.success(request, f"Job {job_code} successfully reassigned to {new_technician_name}.")
        return redirect('staff_job_detail', job_code=job_code)

    messages.error(request, "Invalid reassignment attempt.")
    return redirect('staff_job_detail', job_code=job_code)


@login_required
def staff_job_archive_view(request):
    access = get_staff_access(request.user)
    if not request.user.is_staff or not access.get("reports_overview"):
        return redirect('unauthorized')

    current_workspace = getattr(request, 'current_workspace', None)
    jobs_queryset = scope_to_workspace(
        JobTicket.objects, current_workspace
    ).select_related('assigned_to__user').order_by('-created_at')

    # 1. Search Query (Job Code, Customer Name, Phone, Device)
    q = (request.GET.get('q') or '').strip()
    if q:
        jobs_queryset = jobs_queryset.filter(
            Q(job_code__icontains=q) |
            Q(customer_name__icontains=q) |
            Q(customer_phone__icontains=q) |
            Q(device_brand__icontains=q) |
            Q(device_model__icontains=q) |
            Q(device_type__icontains=q)
        )

    # 2. Status Filter
    selected_status = (request.GET.get('status') or '').strip()
    if selected_status:
        jobs_queryset = jobs_queryset.filter(status=selected_status)

    # 3. Date Presets & Range Filter
    preset = (request.GET.get('preset') or '').strip()
    start_date_str = (request.GET.get('start_date') or '').strip()
    end_date_str = (request.GET.get('end_date') or '').strip()
    today = timezone.localdate()

    start_date = None
    end_date = None
    if preset == 'today':
        start_date = today
        end_date = today
        start_date_str = start_date.strftime('%Y-%m-%d')
        end_date_str = end_date.strftime('%Y-%m-%d')
    elif preset == 'this_week':
        start_date = today - timedelta(days=today.weekday())
        end_date = today
        start_date_str = start_date.strftime('%Y-%m-%d')
        end_date_str = end_date.strftime('%Y-%m-%d')
    elif preset == 'this_month':
        start_date = today.replace(day=1)
        end_date = today
        start_date_str = start_date.strftime('%Y-%m-%d')
        end_date_str = end_date.strftime('%Y-%m-%d')
    elif preset == 'all_time':
        start_date = None
        end_date = None
        start_date_str = ''
        end_date_str = ''
    elif start_date_str and end_date_str:
        try:
            start_date = datetime.strptime(start_date_str, '%Y-%m-%d').date()
            end_date = datetime.strptime(end_date_str, '%Y-%m-%d').date()
        except ValueError:
            start_date = None
            end_date = None
            messages.error(request, "Invalid date format provided for filtering.")

    if start_date and end_date:
        start_of_period = timezone.make_aware(datetime.combine(start_date, datetime.min.time()))
        end_of_period = timezone.make_aware(datetime.combine(end_date, datetime.max.time()))
        jobs_queryset = jobs_queryset.filter(
            created_at__gte=start_of_period,
            created_at__lte=end_of_period
        )

    total_jobs_count = jobs_queryset.count()

    # 4. Pagination (25 per page)
    paginator = Paginator(jobs_queryset, 25)
    page_number = request.GET.get('page')
    try:
        page_obj = paginator.page(page_number)
    except PageNotAnInteger:
        page_obj = paginator.page(1)
    except EmptyPage:
        page_obj = paginator.page(paginator.num_pages)

    # 5. Context Setup (No Amount calculation needed)
    context = {
        'page_obj': page_obj,
        'jobs': page_obj.object_list,
        'total_jobs_count': total_jobs_count,
        'q': q,
        'selected_status': selected_status,
        'preset': preset,
        'current_start_date': start_date_str,
        'current_end_date': end_date_str,
        'today_date_str': today.strftime('%Y-%m-%d'),
        'company_start_date': get_company_start_date().strftime('%Y-%m-%d'),
        'status_choices': JobTicket.STATUS_CHOICES,
    }
    return render(request, 'job_tickets/staff_job_archive.html', context)


def staff_job_filtered_archive_view(request, status_code):
    access = get_staff_access(request.user)
    if not request.user.is_staff or not access.get("reports_overview"):
        return redirect('unauthorized')

    start_date_str = (request.GET.get('start_date') or '').strip()
    end_date_str = (request.GET.get('end_date') or '').strip()
    status_filter = (request.GET.get('status_filter') or '').strip()
    preset = (request.GET.get('preset') or '').strip()
    q = (request.GET.get('q') or '').strip()
    today = timezone.localdate()

    current_workspace = getattr(request, 'current_workspace', None)
    jobs_queryset = scope_to_workspace(
        JobTicket.objects, current_workspace,
    ).select_related('assigned_to__user').order_by('-created_at')

    # Status Group Filtering
    report_title = "Filtered Job Archive"
    q_status_filter = Q()

    if status_code == 'Pending':
        q_status_filter = Q(status='Pending')
        report_title = "Pending Jobs Archive"
    elif status_code == 'Active':
        q_status_filter = Q(status__in=['Under Inspection', 'Repairing', 'Specialized Service'])
        report_title = "Active Workload Archive"
    elif status_code == 'CompletedReady':
        q_status_filter = Q(status__in=['Completed', 'Ready for Pickup'])
        report_title = "Completed/Ready Jobs Archive"
    elif status_code == 'Returned':
        closed_returned_job_ids = get_returned_to_closed_job_ids()
        if status_filter == 'closed':
            q_status_filter = Q(id__in=closed_returned_job_ids, status='Closed')
            report_title = "Returned -> Closed Jobs"
        elif status_filter == 'returned':
            q_status_filter = Q(status='Returned')
            report_title = "Returned Jobs - Still Returned"
        else:
            q_status_filter = Q(status='Returned') | Q(id__in=closed_returned_job_ids, status='Closed')
            report_title = "Returned -> Closed Jobs"
    elif status_code == 'Closed':
        q_status_filter = Q(status='Closed')
        report_title = "Closed Jobs Archive"

    jobs_queryset = jobs_queryset.filter(q_status_filter)

    # Search filter
    if q:
        jobs_queryset = jobs_queryset.filter(
            Q(job_code__icontains=q) |
            Q(customer_name__icontains=q) |
            Q(customer_phone__icontains=q) |
            Q(device_brand__icontains=q) |
            Q(device_model__icontains=q) |
            Q(device_type__icontains=q)
        )

    # Date filter
    start_date = None
    end_date = None
    if preset == 'today':
        start_date = today
        end_date = today
        start_date_str = start_date.strftime('%Y-%m-%d')
        end_date_str = end_date.strftime('%Y-%m-%d')
    elif preset == 'this_week':
        start_date = today - timedelta(days=today.weekday())
        end_date = today
        start_date_str = start_date.strftime('%Y-%m-%d')
        end_date_str = end_date.strftime('%Y-%m-%d')
    elif preset == 'this_month':
        start_date = today.replace(day=1)
        end_date = today
        start_date_str = start_date.strftime('%Y-%m-%d')
        end_date_str = end_date.strftime('%Y-%m-%d')
    elif preset == 'all_time':
        start_date = None
        end_date = None
        start_date_str = ''
        end_date_str = ''
    elif start_date_str and end_date_str:
        try:
            start_date = datetime.strptime(start_date_str, '%Y-%m-%d').date()
            end_date = datetime.strptime(end_date_str, '%Y-%m-%d').date()
        except ValueError:
            start_date = None
            end_date = None
            messages.error(request, "Invalid date format provided for filtering.")

    if start_date and end_date:
        start_of_period = timezone.make_aware(datetime.combine(start_date, datetime.min.time()))
        end_of_period = timezone.make_aware(datetime.combine(end_date, datetime.max.time()))
        jobs_queryset = jobs_queryset.filter(
            created_at__gte=start_of_period,
            created_at__lte=end_of_period
        )

    total_jobs_count = jobs_queryset.count()

    paginator = Paginator(jobs_queryset, 25)
    page_number = request.GET.get('page')
    try:
        page_obj = paginator.page(page_number)
    except PageNotAnInteger:
        page_obj = paginator.page(1)
    except EmptyPage:
        page_obj = paginator.page(paginator.num_pages)

    returned_count = 0
    closed_returned_count = 0
    if status_code == 'Returned':
        returned_count = JobTicket.objects.filter(status='Returned').count()
        closed_returned_count = get_returned_to_closed_jobs().count()

    context = {
        'page_obj': page_obj,
        'jobs': page_obj.object_list,
        'report_title': report_title,
        'q': q,
        'preset': preset,
        'current_start_date': start_date_str,
        'current_end_date': end_date_str,
        'today_date_str': today.strftime('%Y-%m-%d'),
        'company_start_date': get_company_start_date().strftime('%Y-%m-%d'),
        'status_code': status_code,
        'status_filter': status_filter,
        'total_jobs_count': total_jobs_count,
        'returned_count': returned_count,
        'closed_returned_count': closed_returned_count,
    }
    return render(request, 'job_tickets/closed_job_archive.html', context)


# ---------------------------------------------------------------------------
# Standalone Task Management Views (Staff)
# ---------------------------------------------------------------------------

@login_required
def task_dashboard(request):
    """Priority-ordered dashboard of standalone tasks with search and filtering."""
    denied = _staff_access_required(request, "task_management")
    if denied:
        return denied

    workspace = getattr(request, 'current_workspace', None)
    qs = Task.objects.all()
    if workspace:
        qs = qs.filter(workspace=workspace)

    # Search query
    q = (request.GET.get('q') or '').strip()
    if q:
        qs = qs.filter(
            Q(title__icontains=q) |
            Q(description__icontains=q) |
            Q(assigned_to__user__username__icontains=q) |
            Q(job_reference__job_code__icontains=q)
        )

    # Priority filter
    priority = (request.GET.get('priority') or '').strip().lower()
    if priority in dict(Task.PRIORITY_CHOICES):
        qs = qs.filter(priority=priority)

    # Status filter
    status = (request.GET.get('status') or '').strip().lower()
    if status in dict(Task.STATUS_CHOICES):
        qs = qs.filter(status=status)
    elif status == 'all':
        pass
    else:
        status = 'active'
        qs = qs.filter(status__in=[Task.STATUS_OPEN, Task.STATUS_IN_PROGRESS])

    # Technician filter
    tech_id = request.GET.get('tech')
    if tech_id == 'pool':
        qs = qs.filter(Q(is_open_to_all=True) | Q(assigned_to__isnull=True))
    elif tech_id:
        qs = qs.filter(assigned_to_id=tech_id)

    # Date filter
    start_date_str = (request.GET.get('start_date') or '').strip()
    end_date_str = (request.GET.get('end_date') or '').strip()
    date_by = (request.GET.get('date_by') or 'created').strip().lower()

    if start_date_str:
        try:
            start_date = timezone.datetime.strptime(start_date_str, '%Y-%m-%d').date()
            if date_by == 'due':
                qs = qs.filter(due_date__date__gte=start_date)
            else:
                qs = qs.filter(created_at__date__gte=start_date)
        except ValueError:
            start_date_str = ''

    if end_date_str:
        try:
            end_date = timezone.datetime.strptime(end_date_str, '%Y-%m-%d').date()
            if date_by == 'due':
                qs = qs.filter(due_date__date__lte=end_date)
            else:
                qs = qs.filter(created_at__date__lte=end_date)
        except ValueError:
            end_date_str = ''

    tasks = list(qs.select_related('assigned_to__user', 'created_by', 'job_reference').prefetch_related('attachments', 'messages'))

    # Metrics on total tasks
    base_qs = Task.objects.all()
    if workspace:
        base_qs = base_qs.filter(workspace=workspace)
    urgent_count = base_qs.filter(priority=Task.PRIORITY_URGENT, status__in=[Task.STATUS_OPEN, Task.STATUS_IN_PROGRESS]).count()
    open_count = base_qs.filter(status=Task.STATUS_OPEN).count()
    in_progress_count = base_qs.filter(status=Task.STATUS_IN_PROGRESS).count()
    done_count = base_qs.filter(status=Task.STATUS_DONE).count()
    pool_count = base_qs.filter(
        Q(is_open_to_all=True) | Q(assigned_to__isnull=True),
        status__in=[Task.STATUS_OPEN, Task.STATUS_IN_PROGRESS],
    ).count()
    alarm_count = base_qs.filter(
        has_alarm=True,
        status__in=[Task.STATUS_OPEN, Task.STATUS_IN_PROGRESS],
    ).count()
    active_count = open_count + in_progress_count
    all_count = base_qs.count()

    create_form = TaskCreateForm(workspace=workspace)
    technicians = get_assignable_technician_queryset(workspace)

    context = {
        'tasks': tasks,
        'urgent_count': urgent_count,
        'open_count': open_count,
        'in_progress_count': in_progress_count,
        'done_count': done_count,
        'active_count': active_count,
        'all_count': all_count,
        'pool_count': pool_count,
        'alarm_count': alarm_count,
        'status_filter': status,
        'priority_filter': priority,
        'tech_filter': tech_id,
        'search_query': q,
        'start_date': start_date_str,
        'end_date': end_date_str,
        'date_by': date_by,
        'create_form': create_form,
        'technicians': technicians,
    }
    return render(request, 'job_tickets/task_dashboard.html', context)


@login_required
@require_POST
def task_create(request):
    """Create a new standalone task."""
    denied = _staff_access_required(request, "task_management")
    if denied:
        return denied

    workspace = getattr(request, 'current_workspace', None)
    form = TaskCreateForm(request.POST, workspace=workspace)
    if form.is_valid():
        is_open_to_all = bool(form.cleaned_data.get('is_open_to_all'))
        assigned_to = None if is_open_to_all else form.cleaned_data.get('assigned_to')
        has_alarm = bool(form.cleaned_data.get('has_alarm'))
        alarm_time = form.cleaned_data.get('alarm_time')
        if has_alarm and not alarm_time:
            alarm_time = form.cleaned_data.get('due_date') or timezone.now()

        task = Task.objects.create(
            workspace=workspace,
            title=form.cleaned_data['title'],
            description=form.cleaned_data.get('description') or '',
            priority=form.cleaned_data['priority'],
            due_date=form.cleaned_data.get('due_date'),
            assigned_to=assigned_to,
            is_open_to_all=is_open_to_all,
            has_alarm=has_alarm,
            alarm_time=alarm_time,
            job_reference=form.cleaned_data.get('job_reference'),
            created_by=request.user,
        )
        files = request.FILES.getlist('attachments')
        for f in files:
            TaskAttachment.objects.create(
                task=task,
                file=f,
                file_name=f.name,
                file_size=f.size,
                uploaded_by=request.user,
            )
        initial_message = (request.POST.get('initial_message') or '').strip()
        if initial_message:
            init_msg = TaskMessage.objects.create(
                task=task,
                sender=request.user,
                body=initial_message,
            )
            broadcast_task_message(task, init_msg)
        broadcast_task_created(task)
        messages.success(request, f"Task '{task.title}' created successfully.")
        return redirect('task_detail', task_id=task.id)

    for field, errs in form.errors.items():
        messages.error(request, f"{field}: {', '.join(errs)}")
    return redirect('task_dashboard')


@login_required
def task_detail(request, task_id):
    """View task detail with message thread, attachments, and status controls."""
    denied = _staff_access_required(request, "task_management")
    if denied:
        return denied

    workspace = getattr(request, 'current_workspace', None)
    qs = Task.objects.select_related('assigned_to__user', 'created_by', 'job_reference', 'workspace')
    if workspace:
        qs = qs.filter(workspace=workspace)
    task = get_object_or_404(qs, id=task_id)

    attachments = task.attachments.all()
    messages_list = task.messages.select_related('sender').prefetch_related('attachments').all()
    message_form = TaskMessageForm()
    technicians = get_assignable_technician_queryset(workspace)

    context = {
        'task': task,
        'attachments': attachments,
        'messages_list': messages_list,
        'message_form': message_form,
        'technicians': technicians,
    }
    return render(request, 'job_tickets/task_detail.html', context)


@login_required
@require_POST
def task_message_send(request, task_id):
    """Send a message and optional attachments on a task thread."""
    workspace = getattr(request, 'current_workspace', None)
    qs = Task.objects.all()
    if workspace:
        qs = qs.filter(workspace=workspace)
    task = get_object_or_404(qs, id=task_id)

    is_tech = hasattr(request.user, 'technician_profile') and task.assigned_to == request.user.technician_profile
    if request.user.is_staff:
        denied = _staff_access_required(request, "task_management")
        if denied:
            return denied
    elif not is_tech:
        return HttpResponseForbidden("Access denied.")

    form = TaskMessageForm(request.POST)
    if form.is_valid():
        msg = TaskMessage.objects.create(
            task=task,
            sender=request.user,
            body=form.cleaned_data['body'],
        )
        msg_attachments = []
        files = request.FILES.getlist('attachments')
        for f in files:
            att = TaskAttachment.objects.create(
                task=task,
                message=msg,
                file=f,
                file_name=f.name,
                file_size=f.size,
                uploaded_by=request.user,
            )
            msg_attachments.append({
                'id': att.id,
                'name': att.file_name or (att.file.name.split('/')[-1] if att.file else 'file'),
                'url': att.file.url if att.file else '',
                'size': att.file_size or 0,
            })
        broadcast_task_message(task, msg)
        if request.headers.get('x-requested-with') == 'XMLHttpRequest':
            return JsonResponse({
                'ok': True,
                'id': msg.id,
                'sender': msg.sender.username,
                'body': msg.body,
                'sent_at': timezone.localtime(msg.sent_at).strftime('%d %b %Y, %H:%M'),
                'attachments': msg_attachments,
            })

    if is_tech and not request.user.is_staff:
        return redirect('technician_task_detail', task_id=task.id)
    return redirect('task_detail', task_id=task.id)


@login_required
@require_POST
def task_update_status(request, task_id):
    """Update task status, priority, or assignee."""
    workspace = getattr(request, 'current_workspace', None)
    qs = Task.objects.all()
    if workspace:
        qs = qs.filter(workspace=workspace)
    task = get_object_or_404(qs, id=task_id)

    is_tech = hasattr(request.user, 'technician_profile') and task.assigned_to == request.user.technician_profile
    if request.user.is_staff:
        denied = _staff_access_required(request, "task_management")
        if denied:
            return denied
    elif not is_tech:
        return HttpResponseForbidden("Access denied.")

    old_status = task.status
    new_status = (request.POST.get('status') or '').strip().lower()
    if new_status in dict(Task.STATUS_CHOICES):
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
            broadcast_task_status(task, old_status, new_status, request.user)
        messages.success(request, f"Task status updated to {task.get_status_display()}.")

    if request.user.is_staff:
        if 'priority' in request.POST and request.POST['priority'] in dict(Task.PRIORITY_CHOICES):
            task.priority = request.POST['priority']
            task.save(update_fields=['priority', 'updated_at'])
            broadcast_task_status(task, task.status, task.status, request.user)
        if 'assigned_to' in request.POST or 'is_open_to_all' in request.POST or 'assignment_type' in request.POST:
            assignment_type = request.POST.get('assignment_type')
            is_open = assignment_type == 'open' or request.POST.get('is_open_to_all') in ['true', '1', 'on', True]
            if is_open:
                task.is_open_to_all = True
                task.assigned_to = None
                task.save(update_fields=['assigned_to', 'is_open_to_all', 'updated_at'])
                broadcast_task_status(task, task.status, task.status, request.user)
            elif 'assigned_to' in request.POST or assignment_type == 'specific':
                tech_id = request.POST.get('assigned_to')
                task.assigned_to_id = tech_id if tech_id else None
                task.is_open_to_all = False
                task.save(update_fields=['assigned_to', 'is_open_to_all', 'updated_at'])
                broadcast_task_status(task, task.status, task.status, request.user)
                if task.assigned_to_id:
                    broadcast_task_created(task)

    if request.headers.get('x-requested-with') == 'XMLHttpRequest':
        return JsonResponse({
            'ok': True,
            'status': task.status,
            'status_display': task.get_status_display(),
            'priority': task.priority,
            'priority_display': task.get_priority_display(),
        })

    if is_tech and not request.user.is_staff:
        return redirect('technician_task_detail', task_id=task.id)
    return redirect('task_detail', task_id=task.id)


@login_required
@require_POST
def task_delete(request, task_id):
    """Delete a task (staff only)."""
    denied = _staff_access_required(request, "task_management")
    if denied:
        return denied

    workspace = getattr(request, 'current_workspace', None)
    qs = Task.objects.all()
    if workspace:
        qs = qs.filter(workspace=workspace)
    task = get_object_or_404(qs, id=task_id)
    workspace_id = task.workspace_id
    title = task.title
    task.delete()
    broadcast_task_deleted(workspace_id, task_id, title)
    messages.success(request, f"Task '{title}' deleted.")
    return redirect('task_dashboard')
