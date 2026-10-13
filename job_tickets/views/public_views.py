from .helpers import *  # noqa: F401,F403
from .helpers import (
    _build_checklist_schema_for_job,
    _parse_autoprint_flag,
    _staff_access_required,
)


def job_creation_success(request, job_code):
    job_ticket = get_object_or_404(JobTicket, job_code=job_code)
    if request.user.is_authenticated:
        denied = _staff_access_required(request, "staff_dashboard")
        if denied:
            return denied
        if not user_has_workspace_access(request.user, job_ticket.workspace):
            return redirect('unauthorized')
    elif not verify_receipt_access_token(
        job_ticket,
        (request.GET.get('token') or '').strip(),
    ):
        return HttpResponseForbidden("Invalid or expired job ticket link.")
    context = {
        'job_ticket': job_ticket
    }
    return render(request, 'job_tickets/job_creation_success.html', context)

# job_tickets/views.py (def get_report_period(request))

def client_login(request):
    if request.method == 'POST':
        job_code = (request.POST.get('job_code') or '').strip()
        customer_phone, customer_phone_error = normalize_indian_phone(
            request.POST.get('phone_number'),
            field_label='Phone Number',
        )

        if not job_code:
            return render(request, 'job_tickets/client_login.html', {
                'error': 'Job Ticket Number is required.',
            })
        if customer_phone_error:
            return render(request, 'job_tickets/client_login.html', {
                'error': customer_phone_error,
            })
        
        # Rate limiting for client login
        client_ip = request.META.get('REMOTE_ADDR')
        cache_key = f'client_login_attempts_{client_ip}'
        attempts = cache.get(cache_key, 0)
        
        if attempts >= 10:
            return render(request, 'job_tickets/client_login.html', {
                'error': 'Too many failed attempts. Please try again in 30 minutes.',
                'locked': True
            })
        
        try:
            job_ticket = JobTicket.objects.get(
                job_code=job_code,
                customer_phone__in=phone_lookup_variants(customer_phone),
            )
            cache.delete(cache_key)  # Clear attempts on success
            request.session['customer_job_code'] = job_ticket.job_code
            request.session.set_expiry(60 * 60)
            return redirect('client_status', job_code=job_code)
        except JobTicket.DoesNotExist:
            cache.set(cache_key, attempts + 1, 1800)  # 30 minutes
            return render(request, 'job_tickets/client_login.html', {
                'error': 'Invalid Job Ticket Number or Phone Number.',
                'attempts_left': 9 - attempts
            })
    return render(request, 'job_tickets/client_login.html')

def client_status(request, job_code):
    job_ticket = get_object_or_404(JobTicket, job_code=job_code)
    access_token = (request.GET.get('token') or '').strip()
    session_job_code = request.session.get('customer_job_code')
    if not (
        session_job_code == job_ticket.job_code
        or verify_receipt_access_token(job_ticket, access_token)
    ):
        return redirect('client_login')
    job_tickets = [job_ticket]
    calculate_job_totals(job_tickets)
    checklist_schema, checklist_title, checklist_notes = _build_checklist_schema_for_job(job_ticket)
    if checklist_notes and ("optional" in checklist_notes.lower() or "technician" in checklist_notes.lower()):
        checklist_notes = "Quality assurance inspection verified by our service team."
    
    grand_total = max(Decimal('0.00'), (job_ticket.total or Decimal('0.00')) - (job_ticket.discount_amount or Decimal('0.00')))
    amount_paid = job_ticket.amount_paid or Decimal('0.00')
    balance_due = max(Decimal('0.00'), grand_total - amount_paid)
    company = CompanyProfile.get_profile(workspace=job_ticket.workspace)
    
    bill_available = job_ticket.status in ['Ready for Pickup', 'Closed']
    can_give_feedback = job_ticket.status == 'Closed' and not job_ticket.feedback_rating
    
    if request.method == 'POST' and can_give_feedback:
        feedback_form = FeedbackForm(request.POST)
        if feedback_form.is_valid():
            job_ticket.feedback_rating = int(feedback_form.cleaned_data['rating'])
            job_ticket.feedback_comment = feedback_form.cleaned_data['comment']
            job_ticket.feedback_date = timezone.now()
            job_ticket.feedback_followup_status = JobTicket.FEEDBACK_RECEIVED
            job_ticket.save(update_fields=[
                'feedback_rating',
                'feedback_comment',
                'feedback_date',
                'feedback_followup_status',
                'updated_at',
            ])
            
            JobTicketLog.objects.create(
                job_ticket=job_ticket,
                user=None,
                action='FEEDBACK',
                details=f"Customer feedback: {job_ticket.feedback_rating}/10"
            )
            
            messages.success(request, 'Thank you for your feedback!')
            return redirect('client_status', job_code=job_code)
    else:
        feedback_form = FeedbackForm()
    
    company_phone_raw = (company.phone1 or company.phone2 or '') if company else ''
    company_phone_clean = ''.join(ch for ch in company_phone_raw if ch.isdigit())
    if company_phone_clean and len(company_phone_clean) == 10:
        company_phone_clean = f"91{company_phone_clean}"

    context = {
        'job_ticket': job_ticket,
        'access_token': access_token,
        'service_logs': job_ticket.service_logs.all(),
        'total_parts_cost': job_ticket.part_total,
        'total_service_charges': job_ticket.service_total,
        'grand_total': grand_total,
        'amount_paid': amount_paid,
        'balance_due': balance_due,
        'company': company,
        'company_phone_clean': company_phone_clean,
        'bill_available': bill_available,
        'can_give_feedback': can_give_feedback,
        'feedback_form': feedback_form,
        'checklist_schema': checklist_schema,
        'checklist_title': checklist_title,
        'checklist_notes': checklist_notes,
    }
    return render(request, 'job_tickets/client_status.html', context)

@login_required
def client_phone_lookup(request):
    if not request.user.is_staff or not user_has_staff_access(request.user, "staff_dashboard"):
        return JsonResponse({'ok': False, 'message': 'Unauthorized'}, status=403)

    phone = request.GET.get('phone')
    workspace = getattr(request, 'current_workspace', None)
    snapshot = get_phone_service_snapshot(phone, workspace=workspace)
    return JsonResponse({'ok': True, **snapshot})


@login_required
def job_ticket_lookup(request):
    """
    Looks up a previous job ticket by job code or device serial number.
    Returns customer info, device details, and any related batch devices for pre-filling.
    """
    if not request.user.is_staff or not user_has_staff_access(request.user, "staff_dashboard"):
        return JsonResponse({'ok': False, 'message': 'Unauthorized'}, status=403)

    raw_query = (request.GET.get('q') or request.GET.get('job_code') or '').strip()
    if not raw_query:
        return JsonResponse({'ok': True, 'found': False, 'message': 'Please enter a job ticket number or barcode.'})

    clean_q = raw_query.lstrip('#').strip()
    workspace = getattr(request, 'current_workspace', None)

    qs = JobTicket.objects.all()
    if workspace:
        qs = qs.filter(Q(workspace=workspace) | Q(workspace__isnull=True))

    # 1. Exact match on job_code
    matched_job = qs.filter(job_code__iexact=clean_q).select_related('workspace', 'assigned_to__user', 'rack', 'original_job_ticket').first()

    # 2. Try 'JT-' prefix or ending digits
    if not matched_job and clean_q.isdigit():
        matched_job = qs.filter(
            Q(job_code__iexact=f"JT-{clean_q}") |
            Q(job_code__endswith=clean_q)
        ).select_related('workspace', 'assigned_to__user', 'rack', 'original_job_ticket').first()

    # 3. Exact serial/IMEI match
    if not matched_job and len(clean_q) >= 3:
        matched_job = qs.filter(device_serial__iexact=clean_q).select_related('workspace', 'assigned_to__user', 'rack', 'original_job_ticket').first()

    # 4. Partial job_code match
    if not matched_job and len(clean_q) >= 2:
        matched_job = qs.filter(job_code__icontains=clean_q).select_related('workspace', 'assigned_to__user', 'rack', 'original_job_ticket').first()

    if not matched_job:
        return JsonResponse({
            'ok': True,
            'found': False,
            'message': f'No previous job ticket found matching "{raw_query}".',
        })

    # Retrieve all devices in the batch if grouped
    batch_jobs = []
    if matched_job.customer_group_id:
        batch_qs = qs.filter(customer_group_id=matched_job.customer_group_id).select_related('assigned_to__user', 'rack', 'original_job_ticket').order_by('created_at', 'id')
        batch_jobs = list(batch_qs)

    if not batch_jobs:
        batch_jobs = [matched_job]

    def serialize_device(job_item):
        return {
            'id': job_item.id,
            'job_code': job_item.job_code,
            'device_type': job_item.device_type or '',
            'device_brand': job_item.device_brand or '',
            'device_model': job_item.device_model or '',
            'device_serial': job_item.device_serial or '',
            'device_password': job_item.device_password or '',
            'reported_issue': job_item.reported_issue or '',
            'additional_items': job_item.additional_items or '',
            'status': job_item.status,
            'is_under_warranty': job_item.is_under_warranty,
            'created_at': job_item.created_at.strftime('%d-%m-%Y') if job_item.created_at else '',
            'intake_date': job_item.intake_date.strftime('%d-%m-%Y') if job_item.intake_date else (job_item.created_at.strftime('%d-%m-%Y') if job_item.created_at else ''),
            'closed_at': job_item.closed_at.strftime('%d-%m-%Y') if job_item.closed_at else '',
            'technician': (job_item.assigned_to.user.get_full_name() or job_item.assigned_to.user.username) if job_item.assigned_to else '',
        }

    primary_device_data = serialize_device(matched_job)
    batch_devices_data = [serialize_device(j) for j in batch_jobs]

    phone_snapshot = get_phone_service_snapshot(matched_job.customer_phone, workspace=workspace)

    return JsonResponse({
        'ok': True,
        'found': True,
        'matched_job_code': matched_job.job_code,
        'customer_name': matched_job.customer_name,
        'customer_phone': matched_job.customer_phone,
        'total_jobs': phone_snapshot.get('total_jobs', 1),
        'open_jobs': phone_snapshot.get('open_jobs', 0),
        'outstanding_balance': phone_snapshot.get('outstanding_balance', '0.00'),
        'primary_device': primary_device_data,
        'batch_devices': batch_devices_data,
        'is_batch': len(batch_devices_data) > 1,
    })


def client_bill_view(request, job_code):
    job_ticket = get_object_or_404(
        JobTicket.objects.select_related('workspace', 'assigned_to__user', 'created_by', 'rack'),
        job_code=job_code,
    )
    access_token = (request.GET.get('token') or '').strip()
    session_job_code = request.session.get('customer_job_code')
    if not (
        session_job_code == job_ticket.job_code
        or verify_receipt_access_token(job_ticket, access_token)
    ):
        return redirect('client_login')
    
    # Only allow bill access if job is ready for pickup or closed
    if job_ticket.status not in ['Ready for Pickup', 'Closed']:
        return render(request, 'job_tickets/client_login.html', {
            'error': 'Bill is not yet available. Please check back when your device is ready for pickup.'
        })
    
    context = build_job_billing_context(job_ticket, request=request)
    return render(request, 'job_tickets/job_billing_print.html', context)


@never_cache
@require_GET
def app_release_meta(request):
    active_app = MobileAppRelease.objects.filter(platform=MobileAppRelease.PLATFORM_ANDROID, is_active=True).first()
    mobile_data = None
    if active_app:
        mobile_data = {
            'platform': active_app.platform,
            'version_name': active_app.version_name,
            'version_code': active_app.version_code,
            'min_version_code': active_app.min_version_code,
            'release_notes': active_app.release_notes,
            'file_size': active_app.file_size_display,
            'download_url': active_app.get_download_url(request) or request.build_absolute_uri(reverse('download_technician_app')),
            'updated_at': active_app.updated_at.isoformat(),
        }

    return JsonResponse(
        {
            'ok': True,
            'web_version': getattr(settings, 'WEB_RELEASE_VERSION', 'dev'),
            'poll_interval_seconds': max(getattr(settings, 'WEB_RELEASE_POLL_INTERVAL_SECONDS', 300), 60),
            'generated_at': timezone.now().isoformat(),
            'technician_app': mobile_data,
        }
    )


@never_cache
def download_technician_app(request):
    """Directly downloads the active Android APK file for technicians."""
    active_release = MobileAppRelease.objects.filter(platform=MobileAppRelease.PLATFORM_ANDROID, is_active=True).first()
    if not active_release:
        return HttpResponseNotFound("No active technician mobile app release found.")

    if active_release.apk_file:
        try:
            response = FileResponse(
                active_release.apk_file.open('rb'),
                content_type='application/vnd.android.package-archive',
            )
            response['Content-Disposition'] = f'attachment; filename="botgi-technician-v{active_release.version_name}.apk"'
            return response
        except Exception:
            pass

    if active_release.download_url:
        return redirect(active_release.download_url)

    return HttpResponseNotFound("No APK file or download URL configured for this release.")


def technician_app_landing(request):
    """Public landing page with QR code and download button for the technician app."""
    active_release = MobileAppRelease.objects.filter(platform=MobileAppRelease.PLATFORM_ANDROID, is_active=True).first()
    download_url = request.build_absolute_uri(reverse('download_technician_app'))
    qr_image = generate_qr_base64(download_url) if download_url else ''

    return render(request, 'job_tickets/technician_app_download.html', {
        'active_release': active_release,
        'download_url': download_url,
        'qr_image': qr_image,
    })


def get_job_status_data(request):
    if not request.user.is_staff or not user_has_staff_access(request.user, "staff_dashboard"):
        return JsonResponse({'error': 'Unauthorized'}, status=403)

    pending_jobs = list(JobTicket.objects.filter(status='Pending').values('job_code', 'customer_name', 'device_type'))
    in_progress_jobs = list(JobTicket.objects.filter(Q(status='Under Inspection') | Q(status='Repairing')).values('job_code', 'customer_name', 'device_type', 'status', 'assigned_to__user__username'))
    ready_for_pickup_jobs = list(JobTicket.objects.filter(status='Ready for Pickup').values('job_code', 'customer_name', 'device_type', 'status'))
    completed_jobs = list(JobTicket.objects.filter(status='Completed').values('job_code', 'customer_name', 'device_type', 'status'))

    data = {
        'pending_jobs': pending_jobs,
        'in_progress_jobs': in_progress_jobs,
        'ready_for_pickup_jobs': ready_for_pickup_jobs,
        'completed_jobs': completed_jobs,
    }
    return JsonResponse(data)

@login_required
def job_creation_receipt_print_view(request, job_code):
    denied = _staff_access_required(request, "staff_dashboard")
    if denied:
        return denied
    job_ticket = get_object_or_404(
        JobTicket.objects.select_related('rack', 'assigned_to__user', 'created_by', 'original_job_ticket'),
        job_code=job_code,
    )

    if job_ticket.customer_group_id:
        grouped_jobs = list(JobTicket.objects.filter(
            workspace_id=job_ticket.workspace_id,
            customer_group_id=job_ticket.customer_group_id,
        ).select_related('rack', 'assigned_to__user', 'original_job_ticket').order_by('created_at'))
    else:
        grouped_jobs = [job_ticket]

    estimated_amount = job_ticket.estimated_amount if job_ticket.estimated_amount is not None else 0
    estimated_delivery = job_ticket.estimated_delivery or (job_ticket.created_at + timedelta(days=3))
    autoprint = _parse_autoprint_flag(request, default=True)

    checklist_schema, _, _ = _build_checklist_schema_for_job(job_ticket)
    checklist_summary = []
    for item in checklist_schema:
        val = item.get('value')
        if val is True or val == 'true' or val == 1:
            checklist_summary.append({'label': item.get('label', ''), 'value': 'Passed / OK'})
        elif val and str(val).strip() and val is not False:
            checklist_summary.append({'label': item.get('label', ''), 'value': str(val).strip()})

    context = {
        'job_ticket': job_ticket,
        'grouped_jobs': grouped_jobs,
        'estimated_amount': estimated_amount,
        'estimated_delivery': estimated_delivery,
        'checklist_summary': checklist_summary,
        'company': CompanyProfile.get_profile(getattr(request, 'current_workspace', None)),
        'autoprint': autoprint,
    }
    return render(request, 'job_tickets/job_creation_receipt_print.html', context)

def job_creation_receipt_public_view(request, job_code):
    job_ticket = get_object_or_404(
        JobTicket.objects.select_related('rack', 'assigned_to__user', 'created_by', 'original_job_ticket'),
        job_code=job_code,
    )
    token = (request.GET.get('token') or '').strip()
    if not verify_receipt_access_token(job_ticket, token):
        return HttpResponseForbidden("Invalid or expired receipt link.")

    if job_ticket.customer_group_id:
        grouped_jobs = list(JobTicket.objects.filter(
            workspace_id=job_ticket.workspace_id,
            customer_group_id=job_ticket.customer_group_id,
        ).select_related('rack', 'assigned_to__user', 'original_job_ticket').order_by('created_at'))
    else:
        grouped_jobs = [job_ticket]

    estimated_amount = job_ticket.estimated_amount if job_ticket.estimated_amount is not None else 0
    estimated_delivery = job_ticket.estimated_delivery or (job_ticket.created_at + timedelta(days=3))
    autoprint = _parse_autoprint_flag(request, default=False)

    checklist_schema, _, _ = _build_checklist_schema_for_job(job_ticket)
    checklist_summary = []
    for item in checklist_schema:
        val = item.get('value')
        if val is True or val == 'true' or val == 1:
            checklist_summary.append({'label': item.get('label', ''), 'value': 'Passed / OK'})
        elif val and str(val).strip() and val is not False:
            checklist_summary.append({'label': item.get('label', ''), 'value': str(val).strip()})

    context = {
        'job_ticket': job_ticket,
        'grouped_jobs': grouped_jobs,
        'estimated_amount': estimated_amount,
        'estimated_delivery': estimated_delivery,
        'checklist_summary': checklist_summary,
        'company': CompanyProfile.get_profile(getattr(request, 'current_workspace', None)),
        'autoprint': autoprint,
    }
    return render(request, 'job_tickets/job_creation_receipt_print.html', context)


def _pdf_escape(value):
    text = str(value or '')
    return text.replace('\\', '\\\\').replace('(', '\\(').replace(')', '\\)')


def _simple_job_receipt_pdf_bytes(job_ticket, grouped_jobs, estimated_amount, estimated_delivery):
    lines = [
        'BOTGI Job Ticket Receipt',
        f'Primary Job: {job_ticket.job_code}',
        f'Customer: {job_ticket.customer_name}',
        f'Phone: {job_ticket.customer_phone}',
        f'Date: {timezone.localtime(job_ticket.created_at).strftime("%Y-%m-%d %I:%M %p")}',
        f'Estimated Amount: Rs {Decimal(estimated_amount):.2f}',
        f'Estimated Delivery: {estimated_delivery.strftime("%Y-%m-%d") if estimated_delivery else "-"}',
        '',
        'Jobs',
    ]

    for job in grouped_jobs:
        device = ' '.join(part for part in [job.device_brand, job.device_model, f'({job.device_type})'] if part).strip()
        lines.extend([
            f'- {job.job_code}',
            f'  Device: {device or job.device_type or "-"}',
            f'  Issue: {job.reported_issue or "-"}',
            f'  Status: {job.get_status_display()}',
        ])
        if job.additional_items:
            lines.append(f'  Items: {job.additional_items}')

    content_lines = ['BT /F1 16 Tf 50 790 Td (BOTGI Job Ticket Receipt) Tj ET']
    y = 760
    for line in lines[1:]:
        if y < 60:
            break
        font_size = 11 if line else 6
        content_lines.append(f'BT /F1 {font_size} Tf 50 {y} Td ({_pdf_escape(line)}) Tj ET')
        y -= 18 if line else 10

    content = '\n'.join(content_lines).encode('latin-1', errors='replace')
    objects = [
        b'1 0 obj\n<< /Type /Catalog /Pages 2 0 R >>\nendobj\n',
        b'2 0 obj\n<< /Type /Pages /Kids [3 0 R] /Count 1 >>\nendobj\n',
        b'3 0 obj\n<< /Type /Page /Parent 2 0 R /MediaBox [0 0 595 842] /Resources << /Font << /F1 4 0 R >> >> /Contents 5 0 R >>\nendobj\n',
        b'4 0 obj\n<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>\nendobj\n',
        b'5 0 obj\n<< /Length ' + str(len(content)).encode('ascii') + b' >>\nstream\n' + content + b'\nendstream\nendobj\n',
    ]

    pdf = bytearray(b'%PDF-1.4\n')
    offsets = [0]
    for obj in objects:
        offsets.append(len(pdf))
        pdf.extend(obj)
    xref_offset = len(pdf)
    pdf.extend(f'xref\n0 {len(objects) + 1}\n'.encode('ascii'))
    pdf.extend(b'0000000000 65535 f \n')
    for offset in offsets[1:]:
        pdf.extend(f'{offset:010d} 00000 n \n'.encode('ascii'))
    pdf.extend(
        f'trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\nstartxref\n{xref_offset}\n%%EOF\n'.encode('ascii')
    )
    return bytes(pdf)


def job_creation_receipt_pdf_public_view(request, job_code):
    job_ticket = get_object_or_404(JobTicket, job_code=job_code)
    token = (request.GET.get('token') or '').strip()
    if not verify_receipt_access_token(job_ticket, token):
        return HttpResponseForbidden("Invalid or expired receipt link.")

    if job_ticket.customer_group_id:
        grouped_jobs = list(
            JobTicket.objects.filter(
                workspace_id=job_ticket.workspace_id,
                customer_group_id=job_ticket.customer_group_id,
            ).order_by('created_at')
        )
    else:
        grouped_jobs = [job_ticket]

    estimated_amount = job_ticket.estimated_amount if job_ticket.estimated_amount is not None else Decimal('0.00')
    estimated_delivery = job_ticket.estimated_delivery or (job_ticket.created_at + timedelta(days=3))
    pdf_bytes = _simple_job_receipt_pdf_bytes(job_ticket, grouped_jobs, estimated_amount, estimated_delivery)

    response = HttpResponse(pdf_bytes, content_type='application/pdf')
    response['Content-Disposition'] = f'inline; filename="{job_ticket.job_code}.pdf"'
    return response


def qr_access(request, job_code):
    """Direct access to job status via QR code without login"""
    job_ticket = get_object_or_404(JobTicket, job_code=job_code)
    token = (request.GET.get('token') or '').strip()
    if not verify_receipt_access_token(job_ticket, token):
        return HttpResponseForbidden("Invalid or expired QR link.")
    return redirect(f"{reverse('client_status', args=[job_code])}?token={token}")

def custom_404(request, exception):
    """Custom 404 error page"""
    return render(request, '404.html', status=404)

def custom_500(request):
    """Custom 500 error page"""
    return render(request, '500.html', status=500)
