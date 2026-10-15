from .helpers import *  # noqa: F401,F403
from .helpers import (
    _money_or_zero,
    _net_amount_after_discount,
    _staff_access_required,
)
from ..models import FinancialAccount, AccountTransaction


def _parse_vendor_money(raw_value, label):
    try:
        amount = Decimal((raw_value or '0').strip() or '0').quantize(Decimal('0.01'))
    except (InvalidOperation, ValueError, TypeError):
        raise ValueError(f"Invalid {label}.")
    if amount < 0:
        raise ValueError(f"{label} cannot be negative.")
    return amount


def _parse_vendor_payment_date(raw_value):
    raw_value = (raw_value or '').strip()
    if not raw_value:
        return timezone.localdate()
    try:
        return datetime.strptime(raw_value, '%Y-%m-%d').date()
    except ValueError:
        raise ValueError("Invalid payment date.")


def _clean_vendor_payment_method(raw_value):
    method = (raw_value or VendorPayment.METHOD_CASH).strip()
    valid_methods = {value for value, _label in VendorPayment.METHOD_CHOICES}
    if method not in valid_methods:
        raise ValueError("Invalid payment method.")
    return method


def _record_vendor_payment_for_locked_service(service, amount, payment_method, payment_date, reference_no, notes, user, financial_account=None):
    if not service.vendor_id:
        raise ValueError("Vendor is required before recording payment.")
    if service.status != 'Returned from Vendor':
        raise ValueError("Payment can be recorded only after the job returns from vendor.")

    balance_before = service.vendor_balance_amount
    if balance_before is None:
        balance_before = service.vendor_net_payable - (service.vendor_paid_amount or Decimal('0.00'))
    balance_before = max(balance_before or Decimal('0.00'), Decimal('0.00')).quantize(Decimal('0.01'))

    if amount <= Decimal('0.00'):
        raise ValueError("Payment amount must be greater than zero.")
    if amount > balance_before:
        raise ValueError("Payment amount cannot be greater than vendor balance.")

    normalized_reference = (reference_no or '').strip()
    if normalized_reference and VendorPayment.objects.filter(
        vendor_id=service.vendor_id,
        specialized_service_id=service.id,
        payment_date=payment_date,
        reference_no=normalized_reference,
        amount=amount,
    ).exists():
        raise ValueError("A payment with this reference has already been recorded.")

    balance_after = (balance_before - amount).quantize(Decimal('0.01'))
    payment = VendorPayment.objects.create(
        vendor=service.vendor,
        specialized_service=service,
        payment_date=payment_date,
        payment_method=payment_method,
        financial_account=financial_account,
        amount=amount,
        balance_before=balance_before,
        balance_after=balance_after,
        reference_no=normalized_reference,
        notes=(notes or '').strip(),
        created_by=user if getattr(user, 'is_authenticated', False) else None,
    )
    service.vendor_paid_amount = ((service.vendor_paid_amount or Decimal('0.00')) + amount).quantize(Decimal('0.01'))
    service.vendor_balance_amount = balance_after
    service.save(update_fields=['vendor_paid_amount', 'vendor_balance_amount'])

    # Deduct from financial account ledger
    if financial_account and amount > Decimal('0.00'):
        desc = f"Vendor Payment: {service.vendor.company_name} ({service.job_ticket.job_code})"
        if notes:
            desc = f"{desc} - {notes}"
        financial_account.withdraw(
            amount=amount,
            description=desc[:255],
            reference_no=normalized_reference,
            transaction_date=payment_date,
            vendor_payment=payment,
            user=user if getattr(user, 'is_authenticated', False) else None,
        )

    return payment


def _record_vendor_payment_for_service(service_id, amount, payment_method, payment_date, reference_no, notes, user, financial_account=None):
    service = (
        SpecializedService.objects
        .select_for_update()
        .get(id=service_id)
    )
    return _record_vendor_payment_for_locked_service(
        service,
        amount,
        payment_method,
        payment_date,
        reference_no,
        notes,
        user,
        financial_account=financial_account,
    )


def _record_vendor_bulk_payment(vendor, amount, payment_method, payment_date, reference_no, notes, user, financial_account=None):
    if amount <= Decimal('0.00'):
        raise ValueError("Payment amount must be greater than zero.")

    services_qs = (
        SpecializedService.objects
        .select_for_update()
        .filter(
            vendor=vendor,
            status='Returned from Vendor',
            vendor_balance_amount__gt=0,
        )
    )
    if vendor.workspace_id:
        services_qs = services_qs.filter(job_ticket__workspace=vendor.workspace)
    services = list(services_qs.order_by('returned_date', 'id'))
    if not services:
        raise ValueError("No pending vendor balances found.")

    total_balance = sum(
        (
            max(
                service.vendor_balance_amount or Decimal('0.00'),
                Decimal('0.00'),
            )
            for service in services
        ),
        Decimal('0.00'),
    ).quantize(Decimal('0.01'))
    if amount > total_balance:
        raise ValueError("Payment amount cannot be greater than total vendor balance.")

    remaining = amount
    payments = []
    for service in services:
        if remaining <= Decimal('0.00'):
            break
        balance = max(service.vendor_balance_amount or Decimal('0.00'), Decimal('0.00')).quantize(Decimal('0.01'))
        if balance <= Decimal('0.00'):
            continue
        allocation = min(remaining, balance).quantize(Decimal('0.01'))
        payment_note = (notes or '').strip()
        if not payment_note:
            payment_note = f"Bulk payment allocated to {service.job_ticket.job_code}"
        payment = _record_vendor_payment_for_locked_service(
            service,
            allocation,
            payment_method,
            payment_date,
            reference_no,
            payment_note,
            user,
            financial_account=financial_account,
        )
        payments.append(payment)
        remaining = (remaining - allocation).quantize(Decimal('0.01'))

    return payments


def _redirect_back_to_vendor_page(request, fallback='vendor_dashboard'):
    next_url = (request.POST.get('next') or request.META.get('HTTP_REFERER') or '').strip()
    if next_url and url_has_allowed_host_and_scheme(
        next_url,
        allowed_hosts={request.get_host()},
        require_https=request.is_secure(),
    ):
        return redirect(next_url)
    return redirect(fallback)


@login_required
@require_POST
def request_specialized_service(request, job_code):
    if not request.user.groups.filter(name='Technicians').exists():
        return redirect('unauthorized')
        
    job = get_object_or_404(JobTicket, job_code=job_code)
    
    if job.status in ['Closed', 'Completed', 'Ready for Pickup']:
        messages.error(request, f"Cannot request specialized service for a job that is already {job.get_status_display().lower()}.")
        return redirect("job_detail_technician", job_code=job.job_code)

    with transaction.atomic():
        # Check if a SpecializedService already exists
        if hasattr(job, 'specialized_service'):
            service = job.specialized_service
            
            # If status is 'Returned from Vendor', reset it to allow re-assignment
            if service.status == 'Returned from Vendor':
                old_status = job.get_status_display()
                job.status = 'Specialized Service'
                job.save()
                
                # Send WebSocket update
                send_job_update_message(job.job_code, job.status)
                
                # Reset the specialized service record to await new vendor assignment
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
                
                details = f"Status changed from '{old_status}' to 'Specialized Service'. Re-requested specialized service. Service charges will be automatically handled when returned from vendor."
                JobTicketLog.objects.create(job_ticket=job, user=request.user, action='STATUS', details=details)
                
                messages.success(request, f"Job {job.job_code} has been re-sent to staff for specialized service assignment. Service charges will be automatically added when returned.")
                return redirect('technician_dashboard')
            else:
                # Already awaiting assignment or sent to vendor - prevent duplicate
                messages.warning(request, 'This job has already been marked for specialized service.')
                return redirect('job_detail_technician', job_code=job.job_code)

        # No existing SpecializedService - create a new one
        old_status = job.get_status_display()
        job.status = 'Specialized Service'
        job.save()

        # Create the tracking record for the specialized service
        SpecializedService.objects.create(job_ticket=job)

        # Log this important action
        details = f"Status changed from '{old_status}' to 'Specialized Service'. Awaiting vendor assignment by staff. Service charges will be automatically handled when returned from vendor."
        JobTicketLog.objects.create(job_ticket=job, user=request.user, action='STATUS', details=details)
        
        # Send WebSocket update
        send_job_update_message(job.job_code, job.status)

    messages.success(request, f"Job {job.job_code} has been sent to staff for specialized service assignment. Service charges will be automatically added when the job returns from the vendor.")
    return redirect('technician_dashboard')

@login_required
def mark_service_returned(request, service_id):
    denied = _staff_access_required(request, "staff_dashboard")
    if denied:
        return denied

    service = get_object_or_404(
        scope_to_workspace(
            SpecializedService.objects.select_related('job_ticket'),
            getattr(request, 'current_workspace', None),
            field='job_ticket__workspace',
        ),
        id=service_id,
    )
    job = service.job_ticket

    if job.status == 'Closed':
        messages.error(request, "Permission denied: Cannot return service for an already closed job.")
        return redirect('vendor_dashboard')

    # Handle POST request with cost data
    if request.method == 'POST':
        vendor_cost = request.POST.get('vendor_cost')
        client_charge = request.POST.get('client_charge')
        vendor_bill_number = (request.POST.get('vendor_bill_number') or '').strip()
        return_date_str = (request.POST.get('return_date') or '').strip()
        
        # Validate that costs and bill number are provided
        if not vendor_cost or not client_charge:
            messages.error(request, "Both Vendor Bill Amount and Client Charge are required.")
            return redirect('vendor_dashboard')

        if not vendor_bill_number:
            messages.error(request, "Vendor Bill / Invoice Number is required.")
            return redirect('vendor_dashboard')

        return_dt = timezone.now()
        if return_date_str:
            try:
                parsed_date = datetime.strptime(return_date_str, '%Y-%m-%d').date()
                now_time = timezone.localtime().time()
                return_dt = timezone.make_aware(datetime.combine(parsed_date, now_time))
            except Exception:
                pass
        
        try:
            vendor_cost = _parse_vendor_money(vendor_cost, "vendor cost")
            client_charge = _parse_vendor_money(client_charge, "client charge")
        except ValueError as exc:
            messages.error(request, str(exc))
            return redirect('vendor_dashboard')

        vendor_discount_amount = Decimal('0.00')
        vendor_paid_amount = Decimal('0.00')
        vendor_balance_amount = vendor_cost.quantize(Decimal('0.01'))
        
        with transaction.atomic():
            # Step 1: Update the SpecializedService record with costs & bill info
            service.status = 'Returned from Vendor'
            service.returned_date = return_dt
            service.vendor_bill_number = vendor_bill_number
            service.vendor_cost = vendor_cost
            service.vendor_discount_amount = vendor_discount_amount
            service.vendor_paid_amount = vendor_paid_amount
            service.vendor_balance_amount = vendor_balance_amount
            service.client_charge = client_charge
            service.save()

            # Step 2: Automatically create ServiceLog with the client charge
            # This eliminates the need for technicians to manually add service charges
            ServiceLog.objects.update_or_create(
                job_ticket=service.job_ticket,
                description=f"Specialized Service - {service.vendor.company_name}",
                defaults={
                    'part_cost': Decimal('0.00'),
                    'service_charge': client_charge
                }
            )

            # Step 3: Update the main JobTicket status to put it back in the technician's queue
            old_status = job.get_status_display()
            job.status = 'Repairing' 
            job.save()

            # Step 4: Log this important event
            details = (
                f"Device returned from vendor '{service.vendor.company_name}'. "
                f"Bill #{vendor_bill_number}, return date {return_dt.strftime('%d-%m-%Y')}, "
                f"vendor cost Rs {vendor_cost}, balance Rs {vendor_balance_amount}, "
                f"client charge Rs {client_charge}. Service charge automatically added. "
                f"Status changed from '{old_status}' to 'Repairing'."
            )
            JobTicketLog.objects.create(job_ticket=job, user=request.user, action='STATUS', details=details)
            
            # Send WebSocket update
            send_job_update_message(job.job_code, job.status)

        messages.success(request, f"Job {job.job_code} marked as returned. Vendor bill and client charge were recorded.")
        return redirect('vendor_dashboard')
    
    # GET request should not happen, redirect to vendor dashboard
    return redirect('vendor_dashboard')


@login_required
@require_POST
def edit_returned_service_amounts(request, service_id):
    """Edit vendor cost, bill number, return date, and client charge after marking returned."""
    denied = _staff_access_required(request, "staff_dashboard")
    if denied:
        return denied

    service = get_object_or_404(
        scope_to_workspace(
            SpecializedService.objects.select_related('job_ticket', 'vendor'),
            getattr(request, 'current_workspace', None),
            field='job_ticket__workspace',
        ),
        id=service_id,
    )
    job = service.job_ticket

    # Block editing if job status is Closed
    if job.status == 'Closed':
        err_msg = "Permission denied: Cannot edit vendor bill or client charge for a closed job."
        if request.headers.get('x-requested-with') == 'XMLHttpRequest' or request.headers.get('X-Requested-With') == 'fetch':
            return JsonResponse({'ok': False, 'error': err_msg}, status=403)
        messages.error(request, err_msg)
        return redirect('staff_job_detail', job_code=job.job_code)

    # Only returned services can be edited
    if service.status != 'Returned from Vendor':
        err_msg = "Only jobs returned from a vendor can have their return amounts edited."
        if request.headers.get('x-requested-with') == 'XMLHttpRequest' or request.headers.get('X-Requested-With') == 'fetch':
            return JsonResponse({'ok': False, 'error': err_msg}, status=400)
        messages.error(request, err_msg)
        return redirect('staff_job_detail', job_code=job.job_code)

    vendor_cost_raw = request.POST.get('vendor_cost')
    client_charge_raw = request.POST.get('client_charge')
    vendor_bill_number = (request.POST.get('vendor_bill_number') or '').strip()
    return_date_str = (request.POST.get('return_date') or '').strip()

    if not vendor_cost_raw or not client_charge_raw:
        err_msg = "Both Vendor Bill Amount and Client Charge are required."
        if request.headers.get('x-requested-with') == 'XMLHttpRequest' or request.headers.get('X-Requested-With') == 'fetch':
            return JsonResponse({'ok': False, 'error': err_msg}, status=400)
        messages.error(request, err_msg)
        return redirect('staff_job_detail', job_code=job.job_code)

    if not vendor_bill_number:
        err_msg = "Vendor Bill / Invoice Number is required."
        if request.headers.get('x-requested-with') == 'XMLHttpRequest' or request.headers.get('X-Requested-With') == 'fetch':
            return JsonResponse({'ok': False, 'error': err_msg}, status=400)
        messages.error(request, err_msg)
        return redirect('staff_job_detail', job_code=job.job_code)

    return_dt = service.returned_date or timezone.now()
    if return_date_str:
        try:
            parsed_date = datetime.strptime(return_date_str, '%Y-%m-%d').date()
            now_time = (service.returned_date.time() if service.returned_date else timezone.localtime().time())
            return_dt = timezone.make_aware(datetime.combine(parsed_date, now_time))
        except Exception:
            pass

    try:
        vendor_cost = _parse_vendor_money(vendor_cost_raw, "vendor cost")
        client_charge = _parse_vendor_money(client_charge_raw, "client charge")
    except ValueError as exc:
        if request.headers.get('x-requested-with') == 'XMLHttpRequest' or request.headers.get('X-Requested-With') == 'fetch':
            return JsonResponse({'ok': False, 'error': str(exc)}, status=400)
        messages.error(request, str(exc))
        return redirect('staff_job_detail', job_code=job.job_code)

    # Validate against amount already paid
    paid = service.vendor_paid_amount or Decimal('0.00')
    discount = service.vendor_discount_amount or Decimal('0.00')
    net_payable = (vendor_cost - discount).quantize(Decimal('0.01'))
    if net_payable < paid:
        err_msg = (
            f"Vendor bill amount (₹{vendor_cost:.2f}) minus discount (₹{discount:.2f}) "
            f"cannot be less than the amount already paid (₹{paid:.2f})."
        )
        if request.headers.get('x-requested-with') == 'XMLHttpRequest' or request.headers.get('X-Requested-With') == 'fetch':
            return JsonResponse({'ok': False, 'error': err_msg}, status=400)
        messages.error(request, err_msg)
        return redirect('staff_job_detail', job_code=job.job_code)

    new_balance = (net_payable - paid).quantize(Decimal('0.01'))

    with transaction.atomic():
        changes = []
        if (service.vendor_cost or Decimal('0.00')) != vendor_cost:
            changes.append(f"Vendor bill ₹{service.vendor_cost or 0} -> ₹{vendor_cost}")
        if (service.client_charge or Decimal('0.00')) != client_charge:
            changes.append(f"Client charge ₹{service.client_charge or 0} -> ₹{client_charge}")
        if (service.vendor_bill_number or '') != vendor_bill_number:
            changes.append(f"Bill #{service.vendor_bill_number or 'N/A'} -> #{vendor_bill_number}")

        service.vendor_cost = vendor_cost
        service.vendor_balance_amount = new_balance
        service.client_charge = client_charge
        service.vendor_bill_number = vendor_bill_number
        service.returned_date = return_dt
        service.save(update_fields=[
            'vendor_cost', 'vendor_balance_amount', 'client_charge',
            'vendor_bill_number', 'returned_date'
        ])

        # Update or create corresponding ServiceLog
        vendor_name = service.vendor.company_name if service.vendor else "Specialized Service"
        service_log = ServiceLog.objects.filter(
            job_ticket=job,
            description__icontains="Specialized Service"
        ).first()

        if service_log:
            service_log.service_charge = client_charge
            service_log.save(update_fields=['service_charge'])
        else:
            ServiceLog.objects.create(
                job_ticket=job,
                description=f"Specialized Service - {vendor_name}",
                part_cost=Decimal('0.00'),
                service_charge=client_charge
            )

        details = (
            f"Vendor return details edited for '{vendor_name}': "
            + (", ".join(changes) if changes else "Details updated")
            + f". Vendor balance updated to ₹{new_balance}."
        )
        JobTicketLog.objects.create(job_ticket=job, user=request.user, action='STATUS', details=details)

        send_job_update_message(job.job_code, job.status)

    if request.headers.get('x-requested-with') == 'XMLHttpRequest' or request.headers.get('X-Requested-With') == 'fetch':
        return JsonResponse({'ok': True, 'message': 'Vendor return amounts updated successfully.'})

    messages.success(request, f"Vendor return amounts for job {job.job_code} updated successfully.")
    return redirect('staff_job_detail', job_code=job.job_code)



@login_required
@require_POST
def record_vendor_payment(request, vendor_id):
    denied = _staff_access_required(request, "staff_dashboard")
    if denied:
        return denied

    current_workspace = getattr(request, 'current_workspace', None)
    vendor = get_object_or_404(scope_to_workspace(Vendor.objects.filter(id=vendor_id), current_workspace))
    payment_scope = (request.POST.get('payment_scope') or 'bulk').strip()
    if payment_scope not in {'bulk', 'single'}:
        payment_scope = 'bulk'

    try:
        amount = _parse_vendor_money(request.POST.get('payment_amount'), "payment amount")
        payment_date = _parse_vendor_payment_date(request.POST.get('payment_date'))
    except ValueError as exc:
        messages.error(request, str(exc))
        return _redirect_back_to_vendor_page(request)

    reference_no = request.POST.get('reference_no')
    notes = request.POST.get('notes')

    financial_account_id = request.POST.get('financial_account')
    financial_account = None
    if financial_account_id:
        financial_account = FinancialAccount.objects.filter(
            Q(workspace=current_workspace) | Q(workspace__isnull=True),
            id=financial_account_id,
            is_active=True,
        ).first()
        if not financial_account:
            messages.error(request, "Selected payment account is invalid or inactive.")
            return _redirect_back_to_vendor_page(request)
    else:
        financial_account = FinancialAccount.get_default_cash_account(current_workspace)

    # Derive payment method automatically from the selected financial account
    raw_payment_method = request.POST.get('payment_method')
    if raw_payment_method:
        try:
            payment_method = _clean_vendor_payment_method(raw_payment_method)
        except ValueError:
            payment_method = VendorPayment.METHOD_CASH
    elif financial_account:
        if financial_account.account_type == FinancialAccount.ACCOUNT_TYPE_CASH:
            payment_method = VendorPayment.METHOD_CASH
        else:
            payment_method = VendorPayment.METHOD_TRANSFER
    else:
        payment_method = VendorPayment.METHOD_CASH

    try:
        with transaction.atomic():
            if payment_scope == 'single':
                service_id = request.POST.get('specialized_service_id')
                if not service_id:
                    raise ValueError("Select a vendor job before recording payment.")
                service = SpecializedService.objects.select_related('vendor', 'job_ticket').get(
                    id=service_id,
                    vendor=vendor,
                )
                if current_workspace and service.job_ticket.workspace_id != current_workspace.id:
                    raise SpecializedService.DoesNotExist
                payments = [
                    _record_vendor_payment_for_service(
                        service.id,
                        amount,
                        payment_method,
                        payment_date,
                        reference_no,
                        notes,
                        request.user,
                        financial_account=financial_account,
                    )
                ]
            else:
                payments = _record_vendor_bulk_payment(
                    vendor,
                    amount,
                    payment_method,
                    payment_date,
                    reference_no,
                    notes,
                    request.user,
                    financial_account=financial_account,
                )

            for payment in payments:
                log_label = "Vendor bulk payment allocated" if payment_scope == 'bulk' else "Vendor payment recorded"
                acc_label = f" via {financial_account.name}" if financial_account else ""
                JobTicketLog.objects.create(
                    job_ticket=payment.specialized_service.job_ticket,
                    user=request.user,
                    action='BILLING',
                    details=(
                        f"{log_label} for '{vendor.company_name}': "
                        f"Rs {payment.amount} by {payment.get_payment_method_display()}{acc_label} on {payment.payment_date}. "
                        f"Balance Rs {payment.balance_after}."
                    ),
                )
    except SpecializedService.DoesNotExist:
        messages.error(request, "Selected vendor job was not found.")
        return _redirect_back_to_vendor_page(request)
    except ValueError as exc:
        messages.error(request, str(exc))
        return _redirect_back_to_vendor_page(request)

    if payment_scope == 'bulk':
        messages.success(
            request,
            f"Payment of Rs {amount} recorded for {vendor.company_name} across {len(payments)} job(s).",
        )
    else:
        messages.success(request, f"Payment of Rs {payments[0].amount} recorded for {vendor.company_name}.")
    return _redirect_back_to_vendor_page(request)


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

@login_required
def vendor_dashboard(request):
    denied = _staff_access_required(request, "staff_dashboard")
    if denied:
        return denied
    current_workspace = getattr(request, 'current_workspace', None)
    if request.method == 'POST' and 'add_vendor_submit' in request.POST:
        vendor_form = VendorForm(request.POST)
        if vendor_form.is_valid():
            vendor = vendor_form.save(commit=False)
            if not vendor.workspace_id:
                vendor.workspace = current_workspace
            vendor.save()
            messages.success(request, f"Vendor '{vendor_form.cleaned_data['company_name']}' added successfully.")
            return redirect('vendor_dashboard')
        else:
            messages.error(request, "Error adding vendor. Please check inputs.")
            # Fall through to GET context to display the form with errors

    else:
        vendor_form = VendorForm() # For GET request or error display

    status_filter = (request.GET.get('status') or 'all').strip().lower()
    if status_filter not in {'active', 'disabled', 'all'}:
        status_filter = 'all'

    # Get all vendors and annotate them with the count of jobs currently with them
    active_jobs_filter = Q(services__status='Sent to Vendor')
    if current_workspace:
        active_jobs_filter &= Q(services__job_ticket__workspace=current_workspace)

    base_vendors_qs = scope_to_workspace(Vendor.objects, current_workspace).annotate(
        active_jobs_count=Count('services', filter=active_jobs_filter)
    ).order_by('-is_active', 'company_name')

    total_vendors_count = base_vendors_qs.count()
    active_vendors_count = base_vendors_qs.filter(is_active=True).count()
    disabled_vendors_count = base_vendors_qs.filter(is_active=False).count()

    if status_filter == 'active':
        vendors = list(base_vendors_qs.filter(is_active=True))
    elif status_filter == 'disabled':
        vendors = list(base_vendors_qs.filter(is_active=False))
    else:
        vendors = list(base_vendors_qs)

    vendor_by_id = {vendor.id: vendor for vendor in vendors}
    for vendor in vendors:
        vendor.total_payable = Decimal('0.00')
        vendor.total_paid = Decimal('0.00')
        vendor.total_balance = Decimal('0.00')
        vendor.outstanding_services = []

    if vendor_by_id:
        settlement_rows = (
            scope_to_workspace(
                SpecializedService.objects,
                current_workspace,
                field='job_ticket__workspace',
            )
            .filter(vendor_id__in=vendor_by_id, status='Returned from Vendor')
            .values('vendor_id')
            .annotate(
                total_bill=Coalesce(Sum('vendor_cost', output_field=DecimalField()), Decimal('0.00')),
                total_discount=Coalesce(Sum('vendor_discount_amount', output_field=DecimalField()), Decimal('0.00')),
                total_paid=Coalesce(Sum('vendor_paid_amount', output_field=DecimalField()), Decimal('0.00')),
                total_balance=Coalesce(Sum('vendor_balance_amount', output_field=DecimalField()), Decimal('0.00')),
            )
        )
        for row in settlement_rows:
            vendor = vendor_by_id.get(row['vendor_id'])
            if not vendor:
                continue
            vendor.total_payable = (row['total_bill'] - row['total_discount']).quantize(Decimal('0.01'))
            vendor.total_paid = row['total_paid']
            vendor.total_balance = row['total_balance']

        outstanding_services = (
            scope_to_workspace(
                SpecializedService.objects,
                current_workspace,
                field='job_ticket__workspace',
            )
            .filter(vendor_id__in=vendor_by_id, status='Returned from Vendor', vendor_balance_amount__gt=0)
            .select_related('job_ticket', 'vendor')
            .order_by('returned_date', 'job_ticket__job_code')
        )
        for service in outstanding_services:
            vendor = vendor_by_id.get(service.vendor_id)
            if vendor:
                vendor.outstanding_services.append(service)

    # Get all jobs that are currently with any vendor, ordered for easy grouping in the template
    active_services = list(
        scope_to_workspace(
            SpecializedService.objects,
            current_workspace,
            field='job_ticket__workspace',
        )
        .filter(status='Sent to Vendor')
        .select_related('job_ticket', 'vendor')
        .prefetch_related('job_ticket__service_logs')
        .order_by('vendor__company_name', 'sent_date')
    )
    active_service_jobs = [service.job_ticket for service in active_services]
    calculate_job_totals(active_service_jobs)
    for job in active_service_jobs:
        job.discount_total = _money_or_zero(job.discount_amount)
        job.net_total = _net_amount_after_discount(job.total, job.discount_total)
    active_count_by_vendor = {vendor.id: vendor.active_jobs_count for vendor in vendors}
    for service in active_services:
        service.vendor_active_jobs_count = active_count_by_vendor.get(service.vendor_id, 0)

    recent_payments = (
        VendorPayment.objects
        .filter(vendor_id__in=vendor_by_id)
        .select_related('vendor', 'specialized_service__job_ticket', 'created_by', 'financial_account')
        .order_by('-payment_date', '-created_at')
    )
    if current_workspace:
        recent_payments = recent_payments.filter(specialized_service__job_ticket__workspace=current_workspace)
    recent_payments = recent_payments[:75]

    financial_accounts = list(
        FinancialAccount.objects.filter(
            Q(workspace=current_workspace) | Q(workspace__isnull=True),
            is_active=True,
        ).order_by('-is_default_cash', '-is_default_bank', 'name')
    )

    context = {
        'vendors': vendors,
        'vendor_form': vendor_form,
        'active_services_by_vendor': active_services,
        'recent_vendor_payments': recent_payments,
        'today_date': timezone.localdate().strftime('%Y-%m-%d'),
        'vendor_payment_method_choices': VendorPayment.METHOD_CHOICES,
        'financial_accounts': financial_accounts,
        'status_filter': status_filter,
        'total_vendors_count': total_vendors_count,
        'active_vendors_count': active_vendors_count,
        'disabled_vendors_count': disabled_vendors_count,
    }
    return render(request, 'job_tickets/vendor_dashboard.html', context)

@login_required
@require_POST
def edit_vendor(request, vendor_id):
    """Edit an existing vendor."""
    denied = _staff_access_required(request, "staff_dashboard")
    if denied:
        return denied
    
    vendor = get_object_or_404(scope_to_workspace(Vendor.objects.filter(id=vendor_id), getattr(request, 'current_workspace', None)))
    
    # Update vendor fields
    vendor.company_name = request.POST.get('company_name', vendor.company_name)
    vendor.name = request.POST.get('name', vendor.name)
    vendor.phone = request.POST.get('phone', vendor.phone)
    vendor.email = request.POST.get('email', vendor.email)
    vendor.address = request.POST.get('address', vendor.address)
    vendor.specialties = request.POST.get('specialties', vendor.specialties)
    if 'is_active' in request.POST:
        vendor.is_active = request.POST.get('is_active') in ['true', 'True', '1', 'on']
    
    try:
        vendor.save()
        messages.success(request, f"Vendor '{vendor.company_name}' updated successfully.")
    except Exception as e:
        messages.error(request, f"Error updating vendor: {str(e)}")
    
    return redirect('vendor_dashboard')

@login_required
@require_POST
def toggle_vendor_status(request, vendor_id):
    """Enable or disable a vendor."""
    denied = _staff_access_required(request, "staff_dashboard")
    if denied:
        return denied

    current_workspace = getattr(request, 'current_workspace', None)
    vendor = get_object_or_404(scope_to_workspace(Vendor.objects.filter(id=vendor_id), current_workspace))

    if vendor.is_active:
        active_services = scope_to_workspace(
            SpecializedService.objects.filter(vendor=vendor, status='Sent to Vendor'),
            current_workspace,
            field='job_ticket__workspace',
        ).count()
        if active_services > 0:
            messages.error(
                request,
                f"Cannot disable vendor '{vendor.company_name}' because they have {active_services} active job(s) with them. Please mark those jobs as returned first."
            )
            return redirect('vendor_dashboard')
        vendor.is_active = False
        vendor.save(update_fields=['is_active'])
        messages.success(request, f"Vendor '{vendor.company_name}' has been disabled.")
    else:
        vendor.is_active = True
        vendor.save(update_fields=['is_active'])
        messages.success(request, f"Vendor '{vendor.company_name}' has been enabled.")

    return redirect('vendor_dashboard')

@login_required
@require_POST
def delete_vendor(request, vendor_id):
    """Safely disable the vendor instead of deleting historical financial records."""
    return toggle_vendor_status(request, vendor_id)

def _build_vendor_report_context(request, vendor_id):
    vendor = get_object_or_404(
        scope_to_workspace(
            Vendor.objects.filter(id=vendor_id),
            getattr(request, 'current_workspace', None),
        )
    )

    # Get date filters from URL parameters
    start_date_str = request.GET.get('start_date')
    end_date_str = request.GET.get('end_date')

    # Start with all services for this vendor
    services = scope_to_workspace(
        SpecializedService.objects.filter(vendor=vendor),
        getattr(request, 'current_workspace', None), field='job_ticket__workspace',
    ).select_related('job_ticket')
    payments = VendorPayment.objects.filter(vendor=vendor).select_related('specialized_service__job_ticket', 'created_by', 'financial_account')
    period_query = ''

    # Apply date filtering using vendor concept
    if start_date_str and end_date_str:
        try:
            start_date = datetime.strptime(start_date_str, '%Y-%m-%d').date()
            end_date = datetime.strptime(end_date_str, '%Y-%m-%d').date()
            if end_date < start_date:
                start_date, end_date = end_date, start_date
                start_date_str = start_date.isoformat()
                end_date_str = end_date.isoformat()

            start_of_period, end_of_period = report_date_bounds(start_date, end_date)

            # Filter using vendor concept: only jobs returned in the period
            services = services.filter(
                returned_date__gte=start_of_period,
                returned_date__lt=end_of_period
            )
            payments = payments.filter(
                payment_date__gte=start_date,
                payment_date__lte=end_date,
            )
            period_query = urlencode({'start_date': start_date_str, 'end_date': end_date_str})
        except ValueError:
            # If date parsing fails, show all services
            start_date_str = None
            end_date_str = None
            period_query = ''

    services = services.order_by('-sent_date')
    payments = payments.order_by('-payment_date', '-created_at')

    # Calculate financial totals
    totals = services.aggregate(
        total_bill=Coalesce(Sum('vendor_cost', output_field=DecimalField()), Decimal('0')),
        total_discount=Coalesce(Sum('vendor_discount_amount', output_field=DecimalField()), Decimal('0')),
        total_paid=Coalesce(Sum('vendor_paid_amount', output_field=DecimalField()), Decimal('0')),
        total_balance=Coalesce(Sum('vendor_balance_amount', output_field=DecimalField()), Decimal('0')),
        total_charge=Coalesce(Sum('client_charge', output_field=DecimalField()), Decimal('0')),
    )

    total_net_payable = sum_vendor_net_cost(services)
    profit = totals['total_charge'] - total_net_payable

    return {
        'vendor': vendor,
        'services': services,
        'total_jobs': services.count(),
        'total_vendor_bill': totals['total_bill'],
        'total_vendor_discount': totals['total_discount'],
        'total_vendor_cost': total_net_payable,
        'total_vendor_paid': totals['total_paid'],
        'total_vendor_balance': totals['total_balance'],
        'total_client_charge': totals['total_charge'],
        'total_profit': profit,
        'vendor_payments': payments,
        'start_date': start_date_str,
        'end_date': end_date_str,
        'period_query': period_query,
        'generated_at': timezone.now(),
    }


@login_required
def vendor_report_detail(request, vendor_id):
    denied = _staff_access_required(request, "reports_vendor")
    if denied:
        return denied
    if not user_can_view_financial_reports(request.user):
        return redirect('unauthorized')

    context = _build_vendor_report_context(request, vendor_id)
    return render(request, 'job_tickets/vendor_report_detail.html', context)


@login_required
def vendor_report_export_csv(request, vendor_id):
    denied = _staff_access_required(request, "reports_vendor")
    if denied:
        return denied
    if not user_can_view_financial_reports(request.user):
        return redirect('unauthorized')

    context = _build_vendor_report_context(request, vendor_id)
    vendor = context['vendor']
    safe_name = re.sub(r'[^A-Za-z0-9_-]+', '-', vendor.company_name).strip('-') or 'vendor'
    if context['start_date'] and context['end_date']:
        period_text = f"{context['start_date']}_to_{context['end_date']}"
    else:
        period_text = 'all_time'
    filename = f"vendor_report_{safe_name}_{period_text}.csv"

    response = HttpResponse(content_type='text/csv')
    response['Content-Disposition'] = f'attachment; filename="{filename}"'

    def money(value):
        return f"{(_money_or_zero(value)):.2f}"

    def datetime_text(value):
        return timezone.localtime(value).strftime('%Y-%m-%d %H:%M') if value else ''

    def job_url(job_code):
        return request.build_absolute_uri(reverse('staff_job_detail', args=[job_code]))

    writer = csv.writer(response)
    writer.writerow(['Vendor Report'])
    writer.writerow(['Vendor', vendor.company_name])
    writer.writerow([
        'Period',
        f"{context['start_date']} to {context['end_date']}"
        if context['start_date'] and context['end_date']
        else 'All Time',
    ])
    writer.writerow(['Generated At', timezone.localtime(context['generated_at']).strftime('%Y-%m-%d %H:%M')])
    writer.writerow([])
    writer.writerow(['Financial Summary'])
    writer.writerow(['Jobs', context['total_jobs']])
    writer.writerow(['Vendor Bill', money(context['total_vendor_bill'])])
    writer.writerow(['Discount', money(context['total_vendor_discount'])])
    writer.writerow(['Net Payable', money(context['total_vendor_cost'])])
    writer.writerow(['Paid', money(context['total_vendor_paid'])])
    writer.writerow(['Balance', money(context['total_vendor_balance'])])
    writer.writerow(['Client Charges', money(context['total_client_charge'])])
    writer.writerow(['Profit', money(context['total_profit'])])
    writer.writerow([])

    writer.writerow(['Job History'])
    writer.writerow([
        'Job Code',
        'Customer',
        'Phone',
        'Device',
        'Status',
        'Date Sent',
        'Date Returned',
        'Vendor Bill',
        'Discount',
        'Net Payable',
        'Paid',
        'Balance',
        'Client Charge',
        'Payment Status',
        'Job URL',
    ])
    for service in context['services']:
        job = service.job_ticket
        device_label = ' '.join(part for part in [job.device_type, job.device_brand, job.device_model] if part)
        writer.writerow([
            job.job_code,
            job.customer_name,
            job.customer_phone,
            device_label,
            service.get_status_display(),
            datetime_text(service.sent_date),
            datetime_text(service.returned_date),
            money(service.vendor_cost),
            money(service.vendor_discount_amount),
            money(service.vendor_net_payable),
            money(service.vendor_paid_amount),
            money(service.vendor_balance_amount),
            money(service.client_charge),
            service.vendor_payment_status,
            job_url(job.job_code),
        ])
    writer.writerow([])

    writer.writerow(['Payment Transactions'])
    writer.writerow([
        'Date',
        'Job Code',
        'Method',
        'Account',
        'Payable Before',
        'Paid',
        'Balance',
        'Reference',
        'Notes',
        'Entered By',
        'Job URL',
    ])
    for payment in context['vendor_payments']:
        payment_job = payment.specialized_service.job_ticket
        writer.writerow([
            payment.payment_date.strftime('%Y-%m-%d') if payment.payment_date else '',
            payment_job.job_code,
            payment.get_payment_method_display(),
            payment.financial_account.name if payment.financial_account else '',
            money(payment.balance_before),
            money(payment.amount),
            money(payment.balance_after),
            payment.reference_no,
            payment.notes,
            payment.created_by.username if payment.created_by else '',
            job_url(payment_job.job_code),
        ])

    return response
