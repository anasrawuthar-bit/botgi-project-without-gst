import json
import re

from .helpers import *  # noqa: F401,F403
from .helpers import (
    _build_checklist_schema_for_job,
    _checklist_requires_completion,
    _extract_checklist_answers_from_post,
    _format_checklist_required_error,
    _get_job_checklist_answers,
    _merge_checklist_answers,
    _missing_required_checklist_labels,
    _normalize_checklist_answer,
)


@login_required
def technician_dashboard(request):

    if not request.user.groups.filter(name='Technicians').exists():
        return redirect('unauthorized')

    technician = TechnicianProfile.objects.filter(user=request.user).first()
    if not technician:
        # ... (Handle missing profile) ...
        return render(request, 'job_tickets/technician_dashboard.html', {
            'active_jobs': [], 'history_jobs': [], 'username': request.user.username, 
            'warning': 'No technician profile found. Contact admin.'
        })
    
    # Handle search query
    query = request.GET.get('q', '').strip()
    search_results = []
    if query:
        search_results = list(JobTicket.objects.filter(
            Q(assigned_to=technician) &
            (Q(job_code__icontains=query) |
             Q(customer_name__icontains=query) |
             Q(customer_phone__icontains=query) |
             Q(device_type__icontains=query))
        ).prefetch_related('service_logs').order_by('-created_at'))
        calculate_job_totals(search_results, exclude_vendor_charges=True)

    # --- 1. GET DATE FILTERS (Year and Month) ---
    report_month_param = request.GET.get('report_month')
    preset = request.GET.get('preset') # NEW: Get preset parameter
    
    history_filter = Q(assigned_to=technician)  # Start with assigned technician filter
    current_year = None
    current_month = None

    # Get the current local date once
    today = timezone.localdate()

    # NEW: Handle presets first
    if preset == 'this_month':
        current_year = today.year
        current_month = today.month
        report_month_param = None # Clear report_month_param if preset is used
    elif preset == 'last_month':
        first_day_of_current_month = today.replace(day=1)
        last_month_date = first_day_of_current_month - timedelta(days=1)
        current_year = last_month_date.year
        current_month = last_month_date.month
        report_month_param = None # Clear report_month_param if preset is used
    
    if report_month_param:
        try:
            year_str, month_str = report_month_param.split('-')
            year = int(year_str)
            month = int(month_str)
            
            start_of_period = timezone.make_aware(datetime(year, month, 1, 0, 0, 0))
            
            if month == 12:
                end_of_period = start_of_period.replace(year=year + 1, month=1)
            else:
                end_of_period = start_of_period.replace(month=month + 1)
            
            # Apply time filter to history jobs
            history_filter &= Q(updated_at__gte=start_of_period, updated_at__lt=end_of_period)
            
            current_year = year
            current_month = month
            
        except (ValueError, TypeError):
            messages.error(request, "Invalid month or year provided for filtering history.")
            # If invalid, default to current month/year
            current_year = today.year
            current_month = today.month
            start_of_period = timezone.make_aware(datetime(current_year, current_month, 1, 0, 0, 0))
            if current_month == 12:
                end_of_period = start_of_period.replace(year=current_year + 1, month=1)
            else:
                end_of_period = start_of_period.replace(month=current_month + 1)
            history_filter &= Q(updated_at__gte=start_of_period, updated_at__lt=end_of_period)
    else:
        # Default to the current month and year if no filter is applied or if a preset was used
        if current_year is None or current_month is None: # Only if not set by preset
            current_year = today.year
            current_month = today.month
        
        start_of_period = timezone.make_aware(datetime(current_year, current_month, 1, 0, 0, 0))
        if current_month == 12:
            end_of_period = start_of_period.replace(year=current_year + 1, month=1)
        else:
            end_of_period = start_of_period.replace(month=current_month + 1)
            
        history_filter &= Q(updated_at__gte=start_of_period, updated_at__lt=end_of_period)


    # --- 2. DEFINE JOB LISTS ---
    # Treat these statuses as finished for the technician's "active" view so they don't appear
    # in the technician active jobs list. 'Ready for Pickup' is a customer-facing state and
    # should not be shown in the technician's active work queue.
    # Also exclude 'Returned' jobs as they are no longer with technician
    finished_statuses = ['Completed', 'Closed', 'Ready for Pickup', 'Returned']
    
    # Active Jobs (Unfiltered by date, exclude returned jobs)
    active_jobs_query = JobTicket.objects.filter(assigned_to=technician).prefetch_related('service_logs', 'specialized_service').exclude(status__in=finished_statuses).order_by('created_at')
    
    active_jobs = list(active_jobs_query)
    
    # Add vendor return indicator to each job
    for job in active_jobs:
        job.returned_from_vendor = False
        if hasattr(job, 'specialized_service') and job.specialized_service:
            job.returned_from_vendor = job.specialized_service.status == 'Returned from Vendor'
    
    # History Jobs (Filtered by date, status, and assigned technician)
    # Remove 'Returned' from history as these jobs are not completed by technician
    history_finished_statuses = ['Completed', 'Closed', 'Ready for Pickup']
    history_jobs = list(
        JobTicket.objects.filter(history_filter)
        .filter(status__in=history_finished_statuses)
        .prefetch_related('service_logs')
        .order_by('-updated_at')
    )

    # 3. Calculate totals excluding vendor charges for technician view
    calculate_job_totals(active_jobs, exclude_vendor_charges=True)
    calculate_job_totals(history_jobs, exclude_vendor_charges=True)

    # 4. Calculate history summary totals (Footer totals)
    history_parts_total = sum(j.part_total for j in history_jobs)
    history_service_total = sum(j.service_total for j in history_jobs)
    history_grand_total = history_parts_total + history_service_total
    
    # 5. Render context
    context = {
        'technician': technician,
        'active_jobs': active_jobs,
        'history_jobs': history_jobs,
        'username': request.user.username,
        'history_parts_total': history_parts_total,
        'history_service_total': history_service_total,
        'history_grand_total': history_grand_total,
        
        # Search functionality
        'query': query,
        'search_results': search_results,
        'search_count': len(search_results) if query else 0,
        
        # Dates for the filter inputs
        'current_year': current_year,
        'current_month': current_month,
        'current_month_filter': f"{current_year:04d}-{current_month:02d}", # YYYY-MM format for input value
        'technician_join_month': technician.user.date_joined.strftime('%Y-%m'), # YYYY-MM
        'current_month_year': today.strftime('%Y-%m'), # YYYY-MM
        'month_options': [(i, date(2000, i, 1).strftime('%B')) for i in range(1, 13)],
    }

    if request.headers.get('x-requested-with') == 'XMLHttpRequest':
        # Return both history table and active jobs table for AJAX updates
        history_html = render_to_string('job_tickets/_history_table.html', context, request=request)
        
        # Also render active jobs table rows
        active_jobs_html = render_to_string('job_tickets/_active_jobs_table.html', {
            'active_jobs': active_jobs,
            'request': request,
        }, request=request)
        
        return JsonResponse({
            'html': history_html,
            'active_jobs_html': active_jobs_html
        })

    return render(request, 'job_tickets/technician_dashboard.html', context)

@login_required
def job_detail_technician(request, job_code):
    # only technicians allowed
    if not request.user.groups.filter(name='Technicians').exists():
        return redirect('unauthorized')

    # safe lookup of TechnicianProfile (avoids AttributeError if profile missing)
    technician = TechnicianProfile.objects.filter(user=request.user).first()
    if not technician:
        messages.warning(request, "No technician profile found. Contact admin.")
        return redirect('unauthorized')

    # fetch job assigned to this technician
    job = get_object_or_404(
        JobTicket.objects.select_related('rack', 'assigned_to', 'workspace', 'specialized_service', 'specialized_service__vendor'),
        job_code=job_code,
        assigned_to=technician,
    )

    is_closed = (job.status == 'Closed')
    is_ready_for_pickup = (job.status == 'Ready for Pickup')

    EXCLUDED_STATUSES = ['Ready for Pickup', 'Closed', 'Specialized Service']
    
    if is_closed or is_ready_for_pickup:
        technician_status_choices = []
        can_change_status = False
    else:
        # Create a filtered list from the model's choices
        technician_status_choices = [
            (value, label) for value, label in job.STATUS_CHOICES # Access choices from the JobTicket model
            if label not in EXCLUDED_STATUSES
        ]
        can_change_status = True
        if job.status == 'Specialized Service':
            # Check if there's a specialized service record and if it's returned from vendor
            specialized_service = getattr(job, 'specialized_service', None)
            if specialized_service and specialized_service.status != 'Returned from Vendor':
                can_change_status = False

    checklist_schema, checklist_title, checklist_notes = _build_checklist_schema_for_job(job)
    checklist_required_for_completion = _checklist_requires_completion(checklist_schema)
    # ----------------------------------------------------

    if request.method == 'POST':
        if is_closed:
            error_message = "This job is closed and billed. No further changes are permitted."
            if request.headers.get('x-requested-with') == 'XMLHttpRequest':
                return JsonResponse({'ok': False, 'message': error_message, 'is_closed': True}, status=400)
            messages.error(request, error_message)
            return redirect('job_detail_technician', job_code=job_code)

        action = request.POST.get('action')

        if action == 'update_status':
            if is_ready_for_pickup:
                error_message = "Cannot change status of a job that is Ready for Pickup."
                if request.headers.get('x-requested-with') == 'XMLHttpRequest':
                    return JsonResponse({'ok': False, 'message': error_message, 'is_closed': False}, status=400)
                messages.error(request, error_message)
                return redirect('job_detail_technician', job_code=job_code)

            new_status = (request.POST.get('status') or '').strip()
            technician_notes = request.POST.get('technician_notes', '')
            posted_answers, missing_required_labels, invalid_option_labels = _extract_checklist_answers_from_post(
                request.POST,
                checklist_schema,
            )
            existing_answers = _get_job_checklist_answers(job)
            merged_answers = _merge_checklist_answers(existing_answers, posted_answers)

            # VALIDATION: Ensure the technician didn't somehow post an excluded status
            if new_status in EXCLUDED_STATUSES:
                 messages.error(request, 'Invalid status update attempt.')
                 return redirect('job_detail_technician', job_code=job_code)

            if invalid_option_labels:
                error_message = (
                    "Invalid checklist selection for: "
                    + ', '.join(invalid_option_labels[:6])
                    + ('...' if len(invalid_option_labels) > 6 else '')
                )
                if request.headers.get('x-requested-with') == 'XMLHttpRequest':
                    return JsonResponse(
                        {
                            'ok': False,
                            'message': error_message,
                            'invalid_checklist_fields': invalid_option_labels,
                        },
                        status=400,
                    )
                messages.error(request, error_message)
                return redirect('job_detail_technician', job_code=job_code)

            if new_status == 'Completed' and missing_required_labels:
                error_message = _format_checklist_required_error(missing_required_labels)
                if request.headers.get('x-requested-with') == 'XMLHttpRequest':
                    return JsonResponse(
                        {
                            'ok': False,
                            'message': error_message,
                            'missing_checklist_fields': missing_required_labels,
                        },
                        status=400,
                    )
                messages.error(request, error_message)
                return redirect('job_detail_technician', job_code=job_code)
            
            # VALIDATION: Check if job is in specialized service and not returned from vendor
            if job.status == 'Specialized Service':
                specialized_service = getattr(job, 'specialized_service', None)
                if specialized_service and specialized_service.status != 'Returned from Vendor':
                    error_message = 'Cannot change status while job is with vendor. Wait for vendor to return the device.'
                    if request.headers.get('x-requested-with') == 'XMLHttpRequest':
                        return JsonResponse({'ok': False, 'message': error_message}, status=403)
                    messages.error(request, error_message)
                    return redirect('job_detail_technician', job_code=job_code)

            # GET OLD VALUES BEFORE SAVING
            old_status = job.get_status_display()
            old_notes = job.technician_notes
            old_answers = _get_job_checklist_answers(job)

            job.status = new_status
            job.technician_notes = technician_notes
            job.technician_checklist = merged_answers
            job.save()

            if new_status == 'Completed':
                labor_charge_raw = (request.POST.get('labor_charge') or '').strip()
                if labor_charge_raw:
                    try:
                        labor_charge = Decimal(labor_charge_raw)
                        if labor_charge > Decimal('0.00'):
                            labor_log = job.service_logs.filter(description="Technician Labor Charges").first()
                            if labor_log:
                                labor_log.service_charge = labor_charge
                                labor_log.save(update_fields=['service_charge'])
                            else:
                                ServiceLog.objects.create(
                                    job_ticket=job,
                                    description="Technician Labor Charges",
                                    service_charge=labor_charge,
                                    part_cost=Decimal('0.00'),
                                )
                            JobTicketLog.objects.create(
                                job_ticket=job,
                                user=request.user,
                                action='SERVICE',
                                details=f"Labor charge of Rs {labor_charge} recorded.",
                            )
                    except (InvalidOperation, ValueError, TypeError):
                        pass

            if old_status != new_status:
                details = f"Status changed from '{old_status}' to '{job.get_status_display()}'."
                JobTicketLog.objects.create(job_ticket=job, user=request.user, action='STATUS', details=details)
                # Send WebSocket update
                send_job_update_message(job.job_code, job.status)

            if old_notes != technician_notes:
                details = f"Technician notes updated: \"{technician_notes}\""
                JobTicketLog.objects.create(job_ticket=job, user=request.user, action='NOTE', details=details)

            if old_answers != merged_answers:
                changed_labels = []
                for field in checklist_schema:
                    key = field['key']
                    old_value = _normalize_checklist_answer(old_answers.get(key, ''))
                    new_value = _normalize_checklist_answer(merged_answers.get(key, ''))
                    if old_value != new_value:
                        changed_labels.append(field['label'])
                if changed_labels:
                    summary = ', '.join(changed_labels[:8])
                    if len(changed_labels) > 8:
                        summary += '...'
                    details = f"Technician checklist updated: {summary}"
                else:
                    details = "Technician checklist updated."
                JobTicketLog.objects.create(job_ticket=job, user=request.user, action='NOTE', details=details)

            # Return JSON response for AJAX requests (no page reload unless completed)
            if request.headers.get('x-requested-with') == 'XMLHttpRequest':
                redirect_url = reverse('technician_dashboard') if new_status == 'Completed' else None
                return JsonResponse({
                    'ok': True,
                    'message': 'Job completed successfully! Redirecting to dashboard...' if new_status == 'Completed' else 'Status and notes updated successfully.',
                    'status': job.status,
                    'status_display': job.get_status_display(),
                    'technician_notes': job.technician_notes,
                    'redirect_url': redirect_url,
                })
            
            if new_status == 'Completed':
                messages.success(request, f'Job {job.job_code} marked as Completed.')
                return redirect('technician_dashboard')

            messages.success(request, 'Status updated.')
            return redirect('job_detail_technician', job_code=job_code)

        elif action == 'add_service_log':
            # Prevent adding logs if job is finalized, billed, or completed
            if job.status in ['Ready for Pickup', 'Completed', 'Closed'] or job.vyapar_invoice_number:
                error_message = 'Cannot add service logs after completion or billing.'
                if request.headers.get('x-requested-with') == 'XMLHttpRequest':
                    return JsonResponse({'ok': False, 'message': error_message, 'is_closed': is_closed}, status=400)
                messages.error(request, error_message)
                return redirect('job_detail_technician', job_code=job_code)

            # Check if job is in specialized service - prevent manual service charge entry
            if job.status == 'Specialized Service':
                error_message = 'Cannot add service logs while job is in Specialized Service. Service charges will be automatically added when returned from vendor.'
                if request.headers.get('x-requested-with') == 'XMLHttpRequest':
                    return JsonResponse({
                        'ok': False,
                        'message': error_message,
                    }, status=400)
                messages.error(request, error_message)
                return redirect('job_detail_technician', job_code=job_code)
            
            service_form = ServiceLogForm(request.POST)
            if service_form.is_valid():
                new_service = service_form.save(commit=False)
                new_service.job_ticket = job
                new_service.save()
                details = f"Added service: '{new_service.description}' (Part: {new_service.part_cost}, Service: {new_service.service_charge})"
                JobTicketLog.objects.create(job_ticket=job, user=request.user, action='SERVICE', details=details)
                
                # Send WebSocket update
                send_job_update_message(job.job_code, job.status)
                
                # Return JSON response for AJAX requests (no page reload)
                if request.headers.get('x-requested-with') == 'XMLHttpRequest':
                    # Re-fetch service logs to include the new one
                    service_logs = job.service_logs.all()
                    calculate_job_totals([job])
                    html = render_to_string('job_tickets/_service_logs_table.html', {
                        'job': job,
                        'service_logs': service_logs,
                        'total_parts_cost': job.part_total,
                        'total_service_charges': job.service_total,
                        'grand_total': job.total,
                    }, request=request)
                    return JsonResponse({
                        'ok': True,
                        'message': 'Service log added successfully.',
                        'html': html,
                        'total_parts_cost': f"{job.part_total:.2f}",
                        'total_service_charges': f"{job.service_total:.2f}",
                        'grand_total': f"{job.total:.2f}",
                        'item_count': service_logs.count(),
                    })

                
                messages.success(request, 'Service log added.')
                return redirect('job_detail_technician', job_code=job_code)
            else:
                # Return JSON with errors for AJAX requests
                if request.headers.get('x-requested-with') == 'XMLHttpRequest':
                    errors = {field: [str(e) for e in service_form.errors[field]] for field in service_form.errors}
                    return JsonResponse({
                        'ok': False,
                        'message': 'Please correct the errors in the form.',
                        'errors': errors,
                    }, status=400)
                messages.error(request, 'Please correct the errors in the form.')

        elif action == 'update_service_logs':
            # Technician updated existing service log rows (description, part_cost, service_charge)
            # Prevent edits if job is finalized, billed, or in specialized service
            if job.status in ['Ready for Pickup', 'Completed', 'Closed', 'Specialized Service'] or job.vyapar_invoice_number:
                error_msg = 'Cannot edit logs after billing or completion.' if job.status in ['Ready for Pickup', 'Completed', 'Closed'] or job.vyapar_invoice_number else 'Cannot edit service logs while job is in Specialized Service.'
                if request.headers.get('x-requested-with') == 'XMLHttpRequest':
                    return JsonResponse({'ok': False, 'error': 'editing_not_allowed', 'message': error_msg}, status=403)
                messages.error(request, error_msg)
                return redirect('job_detail_technician', job_code=job_code)
            updated_any = False
            for log in job.service_logs.all():
                try:
                    desc_key = f'description_{log.id}'
                    part_key = f'part_cost_{log.id}'
                    service_key = f'service_charge_{log.id}'

                    new_desc = request.POST.get(desc_key, '').strip()
                    new_part_raw = request.POST.get(part_key, '')
                    new_service_raw = request.POST.get(service_key, '')

                    # Parse decimals safely
                    try:
                        new_part = Decimal(new_part_raw) if new_part_raw not in (None, '') else None
                    except (InvalidOperation, ValueError, TypeError):
                        new_part = log.part_cost

                    try:
                        new_service = Decimal(new_service_raw) if new_service_raw not in (None, '') else log.service_charge
                    except (InvalidOperation, ValueError, TypeError):
                        new_service = log.service_charge

                    changed = False
                    details_parts = []
                    if new_desc and new_desc != log.description:
                        details_parts.append(f"description: '{log.description}' -> '{new_desc}'")
                        log.description = new_desc
                        changed = True

                    # Compare decimals as Decimal for accuracy
                    if new_part is not None and (log.part_cost != new_part):
                        details_parts.append(f"part_cost: ₹{log.part_cost or 0} -> ₹{new_part}")
                        log.part_cost = new_part
                        changed = True

                    if new_service is not None and (log.service_charge != new_service):
                        details_parts.append(f"service_charge: ₹{log.service_charge or 0} -> ₹{new_service}")
                        log.service_charge = new_service
                        changed = True

                    if changed:
                        log.save()
                        updated_any = True
                        JobTicketLog.objects.create(job_ticket=job, user=request.user, action='SERVICE', details='; '.join(details_parts))

                except Exception as e:
                    # Log error but continue processing other rows
                    # (Don't expose internals to user)
                    continue

            if request.headers.get('x-requested-with') == 'XMLHttpRequest':
                # Render updated table HTML and return as snapshot
                calculate_job_totals([job])
                service_logs = job.service_logs.all()
                html = render_to_string('job_tickets/_service_logs_table.html', {
                    'job': job,
                    'service_logs': service_logs,
                    'total_parts_cost': job.part_total,
                    'total_service_charges': job.service_total,
                    'grand_total': job.total,
                }, request=request)
                return JsonResponse({
                    'ok': True,
                    'updated': updated_any,
                    'message': updated_any and 'Service logs updated.' or 'No changes detected.',
                    'html': html,
                    'total_parts_cost': f"{job.part_total:.2f}",
                    'total_service_charges': f"{job.service_total:.2f}",
                    'grand_total': f"{job.total:.2f}",
                    'item_count': service_logs.count(),
                })


            if updated_any:
                messages.success(request, 'Service logs updated.')
            else:
                messages.info(request, 'No changes detected in service logs.')
            return redirect('job_detail_technician', job_code=job_code)

        elif action == 'update_rack':
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
                    if request.headers.get('x-requested-with') == 'XMLHttpRequest':
                        return JsonResponse({'ok': False, 'message': 'Selected rack not found or is inactive.'}, status=400)
                    messages.error(request, 'Selected rack not found or is inactive.')
                    return redirect('job_detail_technician', job_code=job_code)
                if rack_col_raw:
                    try:
                        parsed_col = int(rack_col_raw)
                        if 1 <= parsed_col <= max(1, new_rack.total_columns or 100):
                            new_col = parsed_col
                    except (ValueError, TypeError):
                        new_col = None

            if old_rack != new_rack or old_col != new_col:
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
                JobTicketLog.objects.create(
                    job_ticket=job,
                    user=request.user,
                    action='STATUS',
                    details=f"Rack location changed from {old_desc} to {new_desc}."
                )

            col_display = f" - Col {new_col}" if new_col else ""
            msg = f"Device moved to {new_rack.name}{col_display} ({new_rack.group})." if new_rack else "Device unassigned from rack."
            if request.headers.get('x-requested-with') == 'XMLHttpRequest':
                return JsonResponse({
                    'ok': True,
                    'message': msg,
                    'rack_id': new_rack.id if new_rack else '',
                    'rack_name': new_rack.name if new_rack else '',
                    'rack_group': new_rack.group if new_rack else '',
                    'rack_column': new_col or '',
                    'is_assigned': bool(new_rack),
                })
            messages.success(request, msg)
            return redirect('job_detail_technician', job_code=job_code)


    # GET (or invalid POST) -> render page
    service_form = ServiceLogForm()
    service_logs = job.service_logs.all()
    calculate_job_totals([job])
    subtotal = job.total
    discount = job.discount_amount or Decimal('0.00')
    grand_total = subtotal - discount

    racks_qs = DeviceRack.objects.filter(is_active=True)
    if job.workspace_id:
        racks_qs = racks_qs.filter(workspace=job.workspace)
    available_racks = list(racks_qs.order_by('group', 'name'))
    rack_occupancy_data = get_rack_occupancy_data(available_racks, exclude_job=job)
    device_photos = list(job.photos.all())

    # WhatsApp phone number formatting
    clean_digits = re.sub(r'\D', '', job.customer_phone or '')
    if len(clean_digits) == 10:
        whatsapp_phone = f"91{clean_digits}"
    elif len(clean_digits) == 12 and clean_digits.startswith('91'):
        whatsapp_phone = clean_digits
    else:
        whatsapp_phone = clean_digits

    # Delivery deadline calculation
    delivery_status = None
    days_left = None
    if job.estimated_delivery:
        today = timezone.localdate()
        diff = (job.estimated_delivery - today).days
        days_left = diff
        if diff < 0:
            delivery_status = 'overdue'
        elif diff == 0:
            delivery_status = 'today'
        elif diff == 1:
            delivery_status = 'tomorrow'
        else:
            delivery_status = 'upcoming'

    context = {
        'job': job,
        'is_closed': is_closed,
        'service_logs': service_logs,
        'service_form': service_form,
        'total_parts_cost': job.part_total,
        'total_service_charges': job.service_total,
        'subtotal': subtotal,
        'discount': discount,
        'grand_total': grand_total,
        'status_choices': technician_status_choices,
        'can_change_status': can_change_status,
        'checklist_schema': checklist_schema,
        'checklist_title': checklist_title,
        'checklist_notes': checklist_notes,
        'checklist_required_for_completion': checklist_required_for_completion,
        'available_racks': available_racks,
        'rack_occupancy_data': rack_occupancy_data,
        'rack_occupancy_data_json': json.dumps(rack_occupancy_data),
        'device_photos': device_photos,
        'whatsapp_phone': whatsapp_phone,
        'delivery_status': delivery_status,
        'days_left': days_left,
    }
    return render(request, 'job_tickets/job_detail_technician.html', context)


@login_required
@require_POST
def assignment_respond(request, pk):
    """
    POST endpoint for technician to accept/reject an Assignment.
    Expects POST: action=accept|reject, note (optional).
    Returns JSON.
    """
    action = request.POST.get("action")
    note = request.POST.get("note", "").strip()

    if action not in ("accept", "reject"):
        return HttpResponseBadRequest("invalid action")

    try:
        with transaction.atomic():
            # Lock the assignment row to avoid race conditions
            assignment = (
                Assignment.objects.select_for_update()
                .select_related("technician__user", "job")
                .get(pk=pk)
            )

            # Only the assigned technician may respond
            if assignment.technician.user != request.user:
                return HttpResponseForbidden("You are not the assigned technician for this assignment.")

            # Prevent double response
            if assignment.status != "pending":
                return JsonResponse({"ok": False, "error": "already_responded", "status": assignment.status}, status=400)

            if action == "accept":
                assignment.accept(note=note)
            else:
                assignment.reject(note=note)
            
            # CHANNELS: Send update after assignment response changes job status (removed - Django Channels no longer used)

            return JsonResponse({"ok": True, "status": assignment.status})
    except Assignment.DoesNotExist:
        return JsonResponse({"ok": False, "error": "not_found"}, status=404)
    except ValueError as e:
        return JsonResponse({"ok": False, "error": str(e)}, status=400)

@login_required
@require_POST
def job_mark_started(request, job_code):
    """
    Mark a job as started (e.g., set to 'Repairing' or 'Under Inspection').
    Only the assigned technician (or staff) may perform this.
    """
    job = get_object_or_404(JobTicket, job_code=job_code)

    # Permission: allow assigned technician or staff
    is_assigned_tech = job.assigned_to and getattr(job.assigned_to, "user", None) == request.user
    is_staff_actor = bool(request.user.is_staff and user_has_staff_access(request.user, "staff_dashboard"))
    if not (is_assigned_tech or is_staff_actor):
        return HttpResponseForbidden("You are not permitted to mark this job as started.")

    if job.status in ['Closed', 'Completed', 'Ready for Pickup']:
        messages.error(request, f"Permission denied: Cannot start a job that is already {job.get_status_display().lower()}.")
        return redirect("job_detail_technician", job_code=job.job_code)

    # Decide the status you want when "started"
    old_status = job.get_status_display()
    new_status = "Repairing" if job.status != "Repairing" else job.status
    job.status = new_status
    job.save(update_fields=["status", "updated_at"])
    
    # Send WebSocket update
    if old_status != job.get_status_display():
        send_job_update_message(job.job_code, job.status)
        details = f"Status changed from '{old_status}' to '{job.get_status_display()}'."
        JobTicketLog.objects.create(job_ticket=job, user=request.user, action='STATUS', details=details)
        
    messages.success(request, f"Job {job.job_code} marked as started ({new_status}).")
    return redirect("job_detail_technician", job_code=job.job_code)

@login_required
@require_POST
def job_mark_completed(request, job_code):
    """
    Mark a job as completed. Only the assigned technician or staff can do this.
    """
    job = get_object_or_404(JobTicket, job_code=job_code)

    is_assigned_tech = job.assigned_to and getattr(job.assigned_to, "user", None) == request.user
    is_staff_actor = bool(request.user.is_staff and user_has_staff_access(request.user, "staff_dashboard"))
    if not (is_assigned_tech or is_staff_actor):
        return HttpResponseForbidden("You are not permitted to mark this job as completed.")

    if job.status == 'Closed':
        messages.error(request, "Permission denied: Cannot modify or complete an already closed job.")
        return redirect("job_detail_technician", job_code=job.job_code)

    if not is_staff_actor:
        checklist_schema, _, _ = _build_checklist_schema_for_job(job)
        missing_required = _missing_required_checklist_labels(job, checklist_schema)
        if missing_required:
            messages.error(request, _format_checklist_required_error(missing_required))
            return redirect("job_detail_technician", job_code=job.job_code)

    old_status = job.get_status_display()
    job.status = "Completed"

    completion_note = (request.POST.get('completion_note') or '').strip()
    if completion_note:
        ts = timezone.localtime().strftime('%d-%b-%Y %I:%M %p')
        note_entry = f"[{ts}] Completion Note: {completion_note}"
        if job.technician_notes:
            job.technician_notes = f"{job.technician_notes}\n{note_entry}"
        else:
            job.technician_notes = note_entry
        JobTicketLog.objects.create(job_ticket=job, user=request.user, action='NOTE', details=f"Completion note: {completion_note}")

    job.save(update_fields=["status", "technician_notes", "updated_at"])

    labor_charge_raw = (request.POST.get('labor_charge') or '').strip()
    if labor_charge_raw:
        try:
            labor_charge = Decimal(labor_charge_raw)
            if labor_charge > Decimal('0.00'):
                labor_log = job.service_logs.filter(description="Technician Labor Charges").first()
                if labor_log:
                    labor_log.service_charge = labor_charge
                    labor_log.save(update_fields=['service_charge'])
                else:
                    ServiceLog.objects.create(
                        job_ticket=job,
                        description="Technician Labor Charges",
                        service_charge=labor_charge,
                        part_cost=Decimal('0.00'),
                    )
                JobTicketLog.objects.create(
                    job_ticket=job,
                    user=request.user,
                    action='SERVICE',
                    details=f"Labor charge of Rs {labor_charge} recorded upon completion.",
                )
        except (InvalidOperation, ValueError, TypeError):
            pass

    # Send WebSocket update
    if old_status != job.get_status_display():
        send_job_update_message(job.job_code, job.status)
        details = f"Status changed from '{old_status}' to '{job.get_status_display()}'."
        JobTicketLog.objects.create(job_ticket=job, user=request.user, action='STATUS', details=details)
        
    messages.success(request, f"Job {job.job_code} marked as Completed.")
    return redirect("job_detail_technician", job_code=job.job_code)

@login_required
@require_POST
def technician_delete_service_log(request, log_id):
    """Allow assigned technician to delete a service log row (with checks)."""
    # Find the service log and related job
    log = get_object_or_404(ServiceLog, id=log_id)
    job = log.job_ticket

    # Verify requester is the assigned technician
    tech = TechnicianProfile.objects.filter(user=request.user).first()
    if not tech or job.assigned_to != tech:
        return JsonResponse({'ok': False, 'error': 'forbidden'}, status=403)

    # Prevent deletion if job is billed/finalized
    if job.status in ['Ready for Pickup', 'Completed', 'Closed'] or job.vyapar_invoice_number:
        return JsonResponse({'ok': False, 'error': 'editing_not_allowed', 'message': 'Cannot delete logs after billing or completion.'}, status=403)

    # Delete and log
    details = f"Service log deleted: '{log.description}' (Part: {log.part_cost}, Service: {log.service_charge})"
    log.delete()
    JobTicketLog.objects.create(job_ticket=job, user=request.user, action='SERVICE', details=details)
    calculate_job_totals([job])
    return JsonResponse({
        'ok': True,
        'message': 'Service log deleted.',
        'total_parts_cost': f"{job.part_total:.2f}",
        'total_service_charges': f"{job.service_total:.2f}",
        'grand_total': f"{job.total:.2f}",
        'item_count': job.service_logs.count(),
    })


# job_tickets/views.py (Add this function)

@login_required
def technician_acknowledge_assignment(request, job_code):
    """Technician acknowledges a newly assigned job (clears is_new_assignment)."""
    # Ensure the user is a technician
    tech = TechnicianProfile.objects.filter(user=request.user).first()
    if not tech:
        return redirect('unauthorized')

    job = get_object_or_404(JobTicket, job_code=job_code)
    
    if request.method == 'POST':
        # Ensure the user is the assigned technician
        if job.assigned_to != tech:
            messages.error(request, "You are not assigned to this job.")
            return redirect('technician_dashboard') # Or return HttpResponseForbidden

        job.is_new_assignment = False
        job.save(update_fields=['is_new_assignment', 'updated_at'])

        JobTicketLog.objects.create(job_ticket=job, user=request.user, action='ACKNOWLEDGED', details=f"Job {job.job_code} acknowledged by technician.")
        
        # Send WebSocket update
        send_job_update_message(job.job_code, job.status)

        # Re-fetch job with prefetched service_logs for rendering
        job = JobTicket.objects.filter(pk=job.pk).prefetch_related('service_logs').first()
        calculate_job_totals([job]) # Recalculate totals for the single job

        # Return JSON for AJAX requests, HTML for HTMX
        if request.headers.get('x-requested-with') == 'XMLHttpRequest':
            return JsonResponse({
                'ok': True,
                'message': 'Job acknowledged successfully.',
                'job_code': job.job_code,
            })
        
        # Render the updated job row HTML fragment for HTMX
        context = {'job': job, 'request': request} # Pass request to context for {% url %} and user checks
        updated_row_html = render_to_string('job_tickets/_job_row.html', context, request=request)
        return HttpResponse(updated_row_html)

    # For GET requests, or if not POST, redirect to dashboard
    return redirect('technician_dashboard')

@login_required
def technician_return_to_staff(request, job_code):
    """Technician returns the job to staff (unassigns and marks Pending)."""
    tech = TechnicianProfile.objects.filter(user=request.user).first()
    if not tech:
        return redirect('unauthorized')

    job = get_object_or_404(JobTicket, job_code=job_code)
    
    if request.method == 'POST': # Ensure this logic only runs for POST requests
        if job.assigned_to != tech:
            messages.error(request, "You are not assigned to this job.")
            return redirect('technician_dashboard') # Or return HttpResponseForbidden

        if job.status in ['Closed', 'Completed', 'Ready for Pickup']:
            msg = f"Cannot return a {job.get_status_display().lower()} job to staff."
            if request.headers.get('x-requested-with') == 'XMLHttpRequest':
                return JsonResponse({'ok': False, 'message': msg, 'is_closed': (job.status == 'Closed')}, status=400)
            messages.error(request, msg)
            return redirect('technician_dashboard')

        old_status = job.get_status_display()
        job.assigned_to = None
        job.status = 'Pending'
        job.is_new_assignment = False
        job.save(update_fields=['assigned_to', 'status', 'is_new_assignment', 'updated_at'])
        JobTicketLog.objects.create(job_ticket=job, user=request.user, action='ASSIGNED', details=f"Technician returned job to staff. Previous status: {old_status}")
        
        # Send WebSocket update
        send_job_update_message(job.job_code, job.status)
        
        # messages.success(request, f"Job {job.job_code} returned to staff for reassignment.") # Messages won't show with htmx swap

        # Re-fetch job with prefetched service_logs for rendering
        job = JobTicket.objects.filter(pk=job.pk).prefetch_related('service_logs').first()
        calculate_job_totals([job]) # Recalculate totals for the single job

        # Return success response for AJAX
        if request.headers.get('x-requested-with') == 'XMLHttpRequest':
            return JsonResponse({
                'ok': True,
                'message': f'Job {job.job_code} returned to staff for reassignment.',
                'redirect': True
            })
        
        messages.success(request, f'Job {job.job_code} returned to staff for reassignment.')
        return redirect('technician_dashboard')

    # For GET requests, or if not POST, redirect to dashboard
    return redirect('technician_dashboard')


# ---------------------------------------------------------------------------
# Standalone Task Management Views (Technician)
# ---------------------------------------------------------------------------

@login_required
def technician_task_dashboard(request):
    """Technician's view of assigned standalone tasks and claimable open pool tasks."""
    if not request.user.groups.filter(name='Technicians').exists():
        return redirect('unauthorized')

    technician = TechnicianProfile.objects.filter(user=request.user).first()
    if not technician:
        return render(request, 'job_tickets/technician_task_dashboard.html', {
            'tasks': [], 'warning': 'No technician profile found. Contact admin.'
        })

    tab = (request.GET.get('tab') or 'my').strip().lower()
    if tab == 'pool':
        qs = Task.objects.filter(
            Q(is_open_to_all=True) | Q(assigned_to__isnull=True),
            status__in=[Task.STATUS_OPEN, Task.STATUS_IN_PROGRESS],
        )
        if technician.workspace:
            qs = qs.filter(workspace=technician.workspace)
    else:
        tab = 'my'
        qs = Task.objects.filter(assigned_to=technician)

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
        status = 'active'
        qs = qs.filter(status__in=[Task.STATUS_OPEN, Task.STATUS_IN_PROGRESS])

    # Priority filter
    priority = (request.GET.get('priority') or '').strip().lower()
    if priority in dict(Task.PRIORITY_CHOICES):
        qs = qs.filter(priority=priority)

    tasks = list(qs.select_related('created_by', 'job_reference', 'assigned_to__user').prefetch_related('attachments', 'messages'))

    my_base_qs = Task.objects.filter(assigned_to=technician)
    urgent_count = my_base_qs.filter(priority=Task.PRIORITY_URGENT, status__in=[Task.STATUS_OPEN, Task.STATUS_IN_PROGRESS]).count()
    open_count = my_base_qs.filter(status=Task.STATUS_OPEN).count()
    in_progress_count = my_base_qs.filter(status=Task.STATUS_IN_PROGRESS).count()
    done_count = my_base_qs.filter(status=Task.STATUS_DONE).count()

    pool_qs = Task.objects.filter(
        Q(is_open_to_all=True) | Q(assigned_to__isnull=True),
        status__in=[Task.STATUS_OPEN, Task.STATUS_IN_PROGRESS],
    )
    if technician.workspace:
        pool_qs = pool_qs.filter(workspace=technician.workspace)
    pool_count = pool_qs.count()

    context = {
        'tasks': tasks,
        'current_tab': tab,
        'pool_count': pool_count,
        'urgent_count': urgent_count,
        'open_count': open_count,
        'in_progress_count': in_progress_count,
        'done_count': done_count,
        'status_filter': status,
        'priority_filter': priority,
        'search_query': q,
        'technician': technician,
    }
    return render(request, 'job_tickets/technician_task_dashboard.html', context)


@login_required
def technician_task_detail(request, task_id):
    """Technician view for a single task with message thread, attachments, and claim/status actions."""
    if not request.user.groups.filter(name='Technicians').exists():
        return redirect('unauthorized')

    technician = TechnicianProfile.objects.filter(user=request.user).first()
    if not technician:
        return redirect('technician_task_dashboard')

    claimable_q = Q(assigned_to=technician) | Q(is_open_to_all=True) | Q(assigned_to__isnull=True)
    task_qs = Task.objects.select_related('created_by', 'job_reference', 'assigned_to__user')
    if technician.workspace:
        task_qs = task_qs.filter(Q(workspace=technician.workspace) | Q(workspace__isnull=True))

    task = get_object_or_404(task_qs.filter(claimable_q), id=task_id)

    attachments = task.attachments.all()
    messages_list = task.messages.select_related('sender').prefetch_related('attachments').all()
    message_form = TaskMessageForm()

    context = {
        'task': task,
        'technician': technician,
        'is_assigned_to_me': bool(task.assigned_to_id == technician.id),
        'can_claim': bool(task.is_claimable and task.assigned_to_id != technician.id),
        'attachments': attachments,
        'messages_list': messages_list,
        'message_form': message_form,
    }
    return render(request, 'job_tickets/technician_task_detail.html', context)


@login_required
@require_POST
def technician_task_accept(request, task_id):
    """Claim and accept an open pool / unassigned task."""
    if not request.user.groups.filter(name='Technicians').exists():
        return redirect('unauthorized')

    technician = TechnicianProfile.objects.filter(user=request.user).first()
    if not technician:
        messages.error(request, 'No technician profile found.')
        return redirect('technician_task_dashboard')

    from django.db import transaction
    with transaction.atomic():
        try:
            task = Task.objects.select_for_update().get(id=task_id)
        except Task.DoesNotExist:
            messages.error(request, 'Task not found.')
            return redirect('technician_task_dashboard')

        if not task.is_claimable:
            messages.warning(request, f"Task '{task.title}' has already been claimed or is closed.")
            return redirect('technician_task_dashboard')

        task.accept_by_technician(technician)
        msg = TaskMessage.objects.create(
            task=task,
            sender=request.user,
            body=f"{request.user.get_full_name() or request.user.username} accepted and claimed this task from the open pool.",
        )
        broadcast_task_message(task, msg)
        broadcast_task_accepted(task, technician)

    messages.success(request, f"Task '{task.title}' claimed successfully and added to your queue.")
    return redirect('technician_task_detail', task_id=task.id)


@login_required
def technician_profile(request):
    """Technician profile view: personal details, password reset, active mobile devices, app download."""
    if not request.user.groups.filter(name='Technicians').exists():
        return redirect('unauthorized')

    technician = TechnicianProfile.objects.filter(user=request.user).first()
    
    if request.method == 'POST':
        action = request.POST.get('action') or ('change_password' if 'change_password_submit' in request.POST else '')
        if action == 'change_password':
            current_password = (request.POST.get('current_password') or '').strip()
            new_password = (request.POST.get('new_password') or '').strip()
            confirm_password = (request.POST.get('confirm_password') or '').strip()

            if not current_password or not new_password or not confirm_password:
                messages.error(request, 'All password fields are required.')
            elif not request.user.check_password(current_password):
                messages.error(request, 'Current password is incorrect.')
            elif new_password != confirm_password:
                messages.error(request, 'New password and confirm password do not match.')
            else:
                try:
                    validate_password(new_password, user=request.user)
                    request.user.set_password(new_password)
                    request.user.save(update_fields=['password'])
                    update_session_auth_hash(request, request.user)
                    messages.success(request, 'Your password has been changed successfully.')
                    return redirect('technician_profile')
                except ValidationError as exc:
                    messages.error(request, " ".join(exc.messages))
        elif action == 'update_profile':
            full_name = (request.POST.get('full_name') or '').strip()
            email = (request.POST.get('email') or '').strip()

            parts = full_name.split(maxsplit=1)
            request.user.first_name = parts[0] if parts else ''
            request.user.last_name = parts[1] if len(parts) > 1 else ''
            request.user.email = email
            request.user.save(update_fields=['first_name', 'last_name', 'email'])
            messages.success(request, 'Profile updated successfully.')
            return redirect('technician_profile')

    active_sessions = list(
        UserSessionActivity.objects.filter(
            user=request.user,
            channel=UserSessionActivity.CHANNEL_API,
            status=UserSessionActivity.STATUS_ACTIVE,
        ).order_by('-last_activity_at')
    )

    recent_sessions = list(
        UserSessionActivity.objects.filter(
            user=request.user,
            channel=UserSessionActivity.CHANNEL_API,
        ).exclude(status=UserSessionActivity.STATUS_ACTIVE).order_by('-last_activity_at')[:5]
    )

    active_release = MobileAppRelease.objects.filter(platform=MobileAppRelease.PLATFORM_ANDROID, is_active=True).first()
    download_url = request.build_absolute_uri(reverse('download_technician_app'))
    download_qr_base64 = generate_qr_base64(download_url) if download_url else ''

    context = {
        'technician': technician,
        'user': request.user,
        'active_sessions': active_sessions,
        'recent_sessions': recent_sessions,
        'active_release': active_release,
        'download_url': download_url,
        'download_qr_base64': download_qr_base64,
    }
    return render(request, 'job_tickets/technician_profile.html', context)


@login_required
def technician_generate_qr_token(request):
    """Generates a dynamic 10-minute QR login token for the logged-in technician."""
    if not request.user.groups.filter(name='Technicians').exists():
        return JsonResponse({'ok': False, 'message': 'The mobile app is for technicians only.'}, status=403)

    token_str = secrets.token_urlsafe(32)
    expires_at = timezone.now() + timezone.timedelta(minutes=10)
    qr_token = MobileQrLoginToken.objects.create(
        token=token_str,
        user=request.user,
        expires_at=expires_at,
    )
    server_url = request.build_absolute_uri('/').rstrip('/')
    qr_payload = json.dumps({
        'action': 'botgi_mobile_qr_login',
        'token': qr_token.token,
        'username': request.user.username,
        'server_url': server_url,
        'expires_at': qr_token.expires_at.isoformat(),
    })
    qr_image = generate_qr_base64(qr_payload)
    return JsonResponse({
        'ok': True,
        'token': qr_token.token,
        'qr_image': qr_image,
        'expires_at': qr_token.expires_at.isoformat(),
        'expires_in_seconds': 600,
    })


@never_cache
@require_GET
def qr_login_token_status(request, token):
    """Checks the live scanning status of a generated QR login token."""
    qr_token = MobileQrLoginToken.objects.filter(token=token).first()
    if not qr_token:
        return JsonResponse({'ok': False, 'status': 'not_found'}, status=404)

    if qr_token.is_used:
        return JsonResponse({
            'ok': True,
            'status': 'used',
            'device_info': qr_token.device_info or 'Mobile Device',
            'used_at': qr_token.used_at.isoformat() if qr_token.used_at else '',
        })
    elif not qr_token.is_valid:
        return JsonResponse({'ok': True, 'status': 'expired'})
    else:
        return JsonResponse({'ok': True, 'status': 'pending'})


@login_required
@require_POST
def technician_logout_device(request, session_id):
    """Remotely logs out a specific mobile device belonging to the logged-in user."""
    session = get_object_or_404(UserSessionActivity, id=session_id, user=request.user)
    session.status = UserSessionActivity.STATUS_LOGGED_OUT
    session.logout_at = timezone.now()
    session.logout_reason = 'user_revoked'
    session.save(update_fields=['status', 'logout_at', 'logout_reason'])

    if request.headers.get('x-requested-with') == 'XMLHttpRequest' or 'application/json' in (request.content_type or ''):
        return JsonResponse({'ok': True, 'message': 'Device logged out successfully.'})

    messages.success(request, 'Mobile device logged out successfully.')
    return redirect('technician_profile')


@login_required
@require_POST
def technician_logout_all_devices(request):
    """Remotely logs out all active mobile devices belonging to the logged-in user."""
    count = UserSessionActivity.objects.filter(
        user=request.user,
        channel=UserSessionActivity.CHANNEL_API,
        status=UserSessionActivity.STATUS_ACTIVE,
    ).update(
        status=UserSessionActivity.STATUS_LOGGED_OUT,
        logout_at=timezone.now(),
        logout_reason='user_revoked_all',
    )

    if request.headers.get('x-requested-with') == 'XMLHttpRequest' or 'application/json' in (request.content_type or ''):
        return JsonResponse({'ok': True, 'message': f'{count} device(s) logged out successfully.'})

    messages.success(request, f'{count} mobile device(s) logged out successfully.')
    return redirect('technician_profile')


