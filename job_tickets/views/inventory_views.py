import json
from .helpers import *  # noqa: F401,F403
from .helpers import (
    _attach_inventory_credit_to_bill_summaries,
    _build_cash_flow_chart_data,
    _build_inventory_bill_summaries,
    _build_inventory_dashboard_metrics,
    _build_inventory_party_directory,
    _generate_inventory_invoice_number,
    _inventory_entry_dashboard,
    _inventory_form_errors,
    _inventory_post_response,
    _money_or_zero,
    _money_text,
    _net_amount_after_discount,
    _normalize_tax_mode_price,
    _prepare_inventory_sale_bill_print_context,
    _process_inventory_grouped_bill_edit,
    _record_inventory_credit_payment,
    _staff_access_required,
)
from ..models import BulkCampaign


@login_required
def inventory_dashboard(request):
    denied = _staff_access_required(request, "inventory")
    if denied:
        return denied
    current_workspace = getattr(request, 'current_workspace', None)
    context = _build_inventory_dashboard_metrics(current_workspace)
    context['financial_accounts'] = scope_to_workspace(
        FinancialAccount.objects.filter(is_active=True),
        current_workspace,
    ).order_by('-is_default_cash', '-is_default_bank', 'name')
    initial_cash_flow = _build_cash_flow_chart_data(current_workspace, 'this_month')
    context['initial_cash_flow_json'] = json.dumps(initial_cash_flow)
    return render(request, 'job_tickets/inventory_dashboard.html', context)


@login_required
def inventory_cash_flow_chart_api(request):
    denied = _staff_access_required(request, "inventory")
    if denied:
        return JsonResponse({'error': 'Unauthorized'}, status=403)
    current_workspace = getattr(request, 'current_workspace', None)
    range_filter = (request.GET.get('range') or 'this_month').strip().lower()
    start_date = request.GET.get('start_date')
    end_date = request.GET.get('end_date')
    data = _build_cash_flow_chart_data(current_workspace, range_filter, start_date, end_date)
    return JsonResponse(data)

@login_required
@require_POST
def inventory_record_credit_payment(request):
    denied = _staff_access_required(request, "inventory")
    if denied:
        return denied

    next_url = (request.POST.get('next') or '').strip()
    if not next_url or not url_has_allowed_host_and_scheme(
        next_url,
        allowed_hosts={request.get_host()},
        require_https=request.is_secure(),
    ):
        next_url = reverse('inventory_dashboard')

    try:
        result = _record_inventory_credit_payment(request)
        messages.success(request, result['message'])
    except ValueError as exc:
        messages.error(request, str(exc))
    return redirect(next_url)

@login_required
def inventory_party_dashboard(request):
    denied = _staff_access_required(request, "inventory")
    if denied:
        return denied

    current_workspace = getattr(request, 'current_workspace', None)
    query = (request.GET.get('q') or '').strip()
    start_date = None
    end_date = None
    start_date_raw = (request.GET.get('start_date') or '').strip()
    end_date_raw = (request.GET.get('end_date') or '').strip()
    active_tab = (request.GET.get('tab') or request.POST.get('tab') or 'all').strip().lower()
    if active_tab not in {'all', 'suppliers', 'customers'}:
        active_tab = 'all'

    if start_date_raw:
        try:
            start_date = datetime.strptime(start_date_raw, '%Y-%m-%d').date()
        except ValueError:
            start_date = None
    if end_date_raw:
        try:
            end_date = datetime.strptime(end_date_raw, '%Y-%m-%d').date()
        except ValueError:
            end_date = None
    if start_date and end_date and start_date > end_date:
        start_date, end_date = end_date, start_date

    posted_entry_type = (request.POST.get('inventory_entry_edit_submit') or '').strip()
    if request.method == 'POST' and posted_entry_type in INVENTORY_ENTRY_CONFIG:
        try:
            result = _process_inventory_grouped_bill_edit(request, entry_type=posted_entry_type)
            return _inventory_post_response(
                request,
                'inventory_party_dashboard',
                True,
                result['message'],
                extra_params={'tab': active_tab},
            )
        except ValueError as exc:
            return _inventory_post_response(
                request,
                'inventory_party_dashboard',
                False,
                str(exc),
                extra_params={'tab': active_tab},
            )

    if request.method == 'POST' and 'add_inventory_party_submit' in request.POST:
        party_name = (request.POST.get('name') or '').strip()
        existing_party = InventoryParty.objects.filter(is_active=True, name__iexact=party_name)
        if current_workspace:
            existing_party = existing_party.filter(workspace=current_workspace)
        if existing_party.exists():
            messages.error(request, f'A party with the name "{party_name}" already exists.')
            party_form = InventoryPartyForm(request.POST)
        else:
            party_form = InventoryPartyForm(request.POST)
            if party_form.is_valid():
                party = party_form.save(commit=False)
                if not party.workspace_id:
                    party.workspace = current_workspace
                party.save()
                party_form.save_m2m()
                messages.success(request, f"Party '{party.name}' added successfully.")
                return redirect(f"{reverse('inventory_party_dashboard')}?tab={active_tab}")
            messages.error(request, 'Please fix the highlighted errors and try again.')
    else:
        party_form = InventoryPartyForm(
            initial={
                'opening_balance': Decimal('0.00'),
                'is_active': True,
            }
        )

    edit_party_modal_party_id = ''
    if request.method == 'POST' and 'edit_inventory_party_submit' in request.POST:
        edit_party_id = (request.POST.get('edit_party_id') or '').strip()
        party_to_edit = scope_to_workspace(
            InventoryParty.objects.filter(pk=edit_party_id),
            current_workspace,
        ).first()
        if not party_to_edit:
            messages.error(request, 'Selected party was not found.')
            return redirect(f"{reverse('inventory_party_dashboard')}?tab={active_tab}")

        edit_party_form = InventoryPartyForm(request.POST, instance=party_to_edit, prefix='edit')
        if edit_party_form.is_valid():
            updated_party = edit_party_form.save()
            messages.success(request, f"Party '{updated_party.name}' updated successfully.")
            return redirect(f"{reverse('inventory_party_dashboard')}?tab={active_tab}")

        edit_party_modal_party_id = str(party_to_edit.id)
        messages.error(request, 'Please fix the highlighted party profile errors and try again.')
    else:
        edit_party_form = InventoryPartyForm(prefix='edit')

    directory_context = _build_inventory_party_directory(
        query=query,
        start_date=start_date,
        end_date=end_date,
        workspace=current_workspace,
    )
    parties = directory_context['parties']
    suppliers = directory_context['suppliers']
    customers = directory_context['customers']
    legacy_both_count = directory_context['legacy_both_count']

    try:
        default_gst_rate = CompanyProfile.get_profile(current_workspace).gst_rate
    except Exception:
        default_gst_rate = Decimal('18.00')

    shared_edit_options = scope_to_workspace(InventoryParty.objects.filter(
        is_active=True,
    ), current_workspace).order_by('name')
    line_products = scope_to_workspace(
        Product.objects.filter(is_active=True),
        current_workspace,
    ).order_by('name')

    def serialize_party_options(option_qs):
        return [
            {
                'id': party.id,
                'label': f"{party.name} ({party.phone})" if party.phone else party.name,
            }
            for party in option_qs
        ]

    inventory_party_bill_payload = {}
    inventory_party_profile_payload = {}
    for party in parties:
        inventory_party_profile_payload[str(party.id)] = {
            'id': party.id,
            'name': party.name or '',
            'legal_name': party.legal_name or '',
            'contact_person': party.contact_person or '',
            'gst_registration_type': party.gst_registration_type or 'unregistered',
            'phone': party.phone or '',
            'gstin': party.gstin or '',
            'state_code': party.state_code or '',
            'default_place_of_supply_state': party.default_place_of_supply_state or '',
            'pan': party.pan or '',
            'email': party.email or '',
            'address': party.address or '',
            'shipping_address': party.shipping_address or '',
            'city': party.city or '',
            'state': party.state or '',
            'country': party.country or 'India',
            'pincode': party.pincode or '',
            'opening_balance': _money_text(abs(party.opening_balance or Decimal('0.00'))),
            'opening_balance_type': 'to_pay' if (party.opening_balance or Decimal('0.00')) < Decimal('0.00') else 'to_receive',
            'is_active': bool(party.is_active),
        }
        for bill in party.combined_history:
            payload_key = str(bill['bill_id'] or bill['bill_key'])
            if payload_key in inventory_party_bill_payload:
                continue

            lines = sorted(bill['lines'], key=lambda row: row.id)
            inventory_party_bill_payload[payload_key] = {
                'bill_id': bill['bill_id'] or '',
                'bill_key': bill['bill_key'],
                'entry_type': bill['entry_type'],
                'entry_type_label': bill['entry_type_label'],
                'bill_number': bill['bill_number'],
                'invoice_number': bill['invoice_number'],
                'entry_date': bill['entry_date'].isoformat() if bill['entry_date'] else '',
                'invoice_date': bill['invoice_date'].isoformat() if bill.get('invoice_date') else '',
                'party_id': bill['party_id'],
                'party_name': bill['party'].name,
                'job_code': bill['job_code'] or '',
                'party_locked': bool(bill['job_ticket']),
                'bill_notes': bill['notes'] or '',
                'bill_discount_amount': _money_text(
                    sum((row.discount_amount or Decimal('0.00') for row in lines), Decimal('0.00'))
                ),
                'lines': [
                    {
                        'entry_id': row.id,
                        'entry_number': row.entry_number,
                        'product_id': row.product_id,
                        'quantity': int(row.quantity or 0),
                        'unit_price': _money_text(row.unit_price),
                        'gst_rate': _money_text(row.gst_rate),
                        'taxable_amount': _money_text(row.taxable_amount),
                        'gst_amount': _money_text(row.gst_amount),
                        'total_amount': _money_text(row.total_amount),
                        'description': row.notes or '',
                    }
                    for row in lines
                ],
            }

    total_payable = sum((getattr(p, 'credit_payable_amount', Decimal('0.00')) for p in parties), Decimal('0.00'))
    total_receivable = sum((getattr(p, 'credit_receivable_amount', Decimal('0.00')) for p in parties), Decimal('0.00'))
    parties_with_payables = sum(1 for p in parties if getattr(p, 'credit_payable_amount', Decimal('0.00')) > Decimal('0.00'))
    parties_with_receivables = sum(1 for p in parties if getattr(p, 'credit_receivable_amount', Decimal('0.00')) > Decimal('0.00'))

    supplier_count = 0
    customer_count = 0
    for party in parties:
        p_count = getattr(party, 'purchase_count', 0)
        s_count = getattr(party, 'sale_count', 0)
        base_type = getattr(party, 'party_type', 'both') or 'both'

        is_supplier = (base_type in ('supplier', 'both')) or (p_count > 0)
        is_customer = (base_type in ('customer', 'both')) or (s_count > 0)

        party.is_supplier = is_supplier
        party.is_customer = is_customer

        if is_supplier and is_customer:
            party.computed_type = 'both'
            party.computed_type_label = 'Supplier & Customer'
        elif is_supplier:
            party.computed_type = 'supplier'
            party.computed_type_label = 'Supplier'
        else:
            party.computed_type = 'customer'
            party.computed_type_label = 'Customer'

        if is_supplier:
            supplier_count += 1
        if is_customer:
            customer_count += 1

        digits = "".join(ch for ch in (party.phone or '') if ch.isdigit())
        if len(digits) == 10:
            party.wa_phone = f"91{digits}"
        elif len(digits) > 10:
            party.wa_phone = digits
        else:
            party.wa_phone = ""

    supplier_payable = sum(
        (getattr(p, 'credit_payable_amount', Decimal('0.00')) for p in parties if getattr(p, 'is_supplier', False)),
        Decimal('0.00'),
    )
    customer_receivable = sum(
        (getattr(p, 'credit_receivable_amount', Decimal('0.00')) for p in parties if getattr(p, 'is_customer', False)),
        Decimal('0.00'),
    )

    context = {
        'party_form': party_form,
        'edit_party_form': edit_party_form,
        'edit_party_modal_party_id': edit_party_modal_party_id,
        'parties': parties,
        'suppliers': suppliers,
        'customers': customers,
        'legacy_both_count': legacy_both_count,
        'query': query,
        'total_parties': directory_context['total_parties'],
        'supplier_count': supplier_count,
        'customer_count': customer_count,
        'supplier_payable': supplier_payable,
        'customer_receivable': customer_receivable,
        'total_payable': total_payable,
        'total_receivable': total_receivable,
        'parties_with_payables': parties_with_payables,
        'parties_with_receivables': parties_with_receivables,
        'active_tab': active_tab,
        'start_date': start_date.isoformat() if start_date else '',
        'end_date': end_date.isoformat() if end_date else '',
        'default_gst_rate': default_gst_rate,
        'line_products': line_products,
        'inventory_party_bill_payload': inventory_party_bill_payload,
        'inventory_party_profile_payload': inventory_party_profile_payload,
        'inventory_party_edit_options': {
            'purchase': serialize_party_options(shared_edit_options),
            'purchase_return': serialize_party_options(shared_edit_options),
            'sale': serialize_party_options(shared_edit_options),
            'sale_return': serialize_party_options(shared_edit_options),
        },
    }
    return render(request, 'job_tickets/inventory_party_dashboard.html', context)

def _parse_inventory_filter_date(raw_value):
    raw_value = (raw_value or '').strip()
    if not raw_value:
        return None
    try:
        return datetime.strptime(raw_value, '%Y-%m-%d').date()
    except ValueError:
        return None


def _inventory_date_filters(request):
    start_date = _parse_inventory_filter_date(request.GET.get('start_date'))
    end_date = _parse_inventory_filter_date(request.GET.get('end_date'))
    if start_date and end_date and start_date > end_date:
        start_date, end_date = end_date, start_date
    return start_date, end_date


@login_required
def inventory_product_ledger(request, product_id):
    denied = _staff_access_required(request, "inventory")
    if denied:
        return denied

    current_workspace = getattr(request, 'current_workspace', None)
    product = get_object_or_404(scope_to_workspace(Product.objects.filter(pk=product_id), current_workspace))
    start_date, end_date = _inventory_date_filters(request)

    all_entries = list(
        InventoryEntry.objects
        .filter(product=product)
        .select_related('bill', 'party', 'job_ticket', 'created_by')
        .order_by('entry_date', 'id')
    )
    opening_qty = int(product.stock_quantity or 0) - sum(int(entry.stock_effect or 0) for entry in all_entries)

    opening_date = timezone.localtime(product.created_at).date() if product.created_at else None
    if not opening_date and all_entries:
        opening_date = all_entries[0].entry_date
    if not opening_date:
        opening_date = timezone.localdate()

    opening_unit_price = product.cost_price or product.unit_price or Decimal('0.00')

    running_qty = opening_qty
    ledger_rows = []

    # Include initial opening stock row if positive and matches date filter
    if opening_qty > 0:
        include_opening = True
        if start_date and opening_date < start_date:
            include_opening = False
        if end_date and opening_date > end_date:
            include_opening = False

        if include_opening:
            ledger_rows.append(
                SimpleNamespace(
                    entry=None,
                    entry_type='opening_stock',
                    entry_type_label='Opening Stock',
                    entry_date=opening_date,
                    invoice_number='Opening Stock',
                    party=None,
                    job_ticket=None,
                    qty_in=int(opening_qty),
                    qty_out=0,
                    stock_balance=int(opening_qty),
                    unit_price=opening_unit_price,
                    total_amount=(Decimal(opening_qty) * opening_unit_price).quantize(Decimal('0.01')),
                    created_by=None,
                )
            )

    for entry in all_entries:
        running_qty += int(entry.stock_effect or 0)
        if start_date and entry.entry_date < start_date:
            continue
        if end_date and entry.entry_date > end_date:
            continue

        qty_in = entry.quantity if entry.entry_type in {'purchase', 'sale_return'} else 0
        qty_out = entry.quantity if entry.entry_type in {'sale', 'purchase_return'} else 0
        ledger_rows.append(
            SimpleNamespace(
                entry=entry,
                entry_type=entry.entry_type,
                entry_type_label=entry.get_entry_type_display(),
                entry_date=entry.entry_date,
                invoice_number=entry.invoice_number or (entry.bill.bill_number if entry.bill_id else entry.entry_number),
                party=entry.party,
                job_ticket=entry.job_ticket,
                qty_in=int(qty_in or 0),
                qty_out=int(qty_out or 0),
                stock_balance=running_qty,
                unit_price=entry.unit_price or Decimal('0.00'),
                total_amount=entry.total_amount or Decimal('0.00'),
                created_by=entry.created_by,
            )
        )

    # If start_date was provided and opening_date was before start_date, show period opening balance
    if start_date and opening_date < start_date:
        prior_balance = opening_qty + sum(
            int(entry.stock_effect or 0) for entry in all_entries if entry.entry_date < start_date
        )
        if prior_balance > 0:
            ledger_rows.append(
                SimpleNamespace(
                    entry=None,
                    entry_type='opening_stock',
                    entry_type_label='Opening Stock',
                    entry_date=start_date,
                    invoice_number='Opening Stock',
                    party=None,
                    job_ticket=None,
                    qty_in=int(prior_balance),
                    qty_out=0,
                    stock_balance=int(prior_balance),
                    unit_price=opening_unit_price,
                    total_amount=(Decimal(prior_balance) * opening_unit_price).quantize(Decimal('0.01')),
                    created_by=None,
                )
            )

    def _ledger_sort_key(row):
        entry_date = getattr(row, 'entry_date', None) or date.min
        entry_id = row.entry.id if (getattr(row, 'entry', None) and getattr(row.entry, 'id', None)) else -1
        return (entry_date, entry_id)

    ledger_rows.sort(key=_ledger_sort_key, reverse=True)
    total_in_qty = sum(row.qty_in for row in ledger_rows)
    total_out_qty = sum(row.qty_out for row in ledger_rows)
    purchase_amount = sum(
        (row.total_amount for row in ledger_rows if row.entry_type == 'purchase'),
        Decimal('0.00'),
    )
    sales_amount = sum(
        (row.total_amount for row in ledger_rows if row.entry_type == 'sale'),
        Decimal('0.00'),
    )
    total_movement_amount = sum(
        (row.total_amount for row in ledger_rows),
        Decimal('0.00'),
    )
    current_stock_value = (Decimal(product.stock_quantity or 0) * (product.cost_price or Decimal('0.00'))).quantize(Decimal('0.01'))

    context = {
        'product': product,
        'ledger_rows': ledger_rows,
        'start_date': start_date.isoformat() if start_date else '',
        'end_date': end_date.isoformat() if end_date else '',
        'opening_qty': opening_qty,
        'total_in_qty': total_in_qty,
        'total_out_qty': total_out_qty,
        'purchase_amount': purchase_amount,
        'sales_amount': sales_amount,
        'total_movement_amount': total_movement_amount,
        'current_stock_value': current_stock_value,
    }
    return render(request, 'job_tickets/inventory_product_ledger.html', context)


@login_required
def inventory_party_ledger(request, party_id):
    denied = _staff_access_required(request, "inventory")
    if denied:
        return denied

    current_workspace = getattr(request, 'current_workspace', None)
    party = get_object_or_404(scope_to_workspace(InventoryParty.objects.filter(pk=party_id), current_workspace))
    start_date, end_date = _inventory_date_filters(request)

    entries = (
        scope_to_workspace(InventoryEntry.objects.filter(party=party), current_workspace)
        .select_related('bill', 'party', 'product', 'created_by', 'job_ticket')
    )
    payments = scope_to_workspace(
        InventoryCreditPayment.objects.filter(party=party),
        current_workspace,
    ).select_related('bill', 'created_by')

    if start_date:
        entries = entries.filter(entry_date__gte=start_date)
        payments = payments.filter(payment_date__gte=start_date)
    if end_date:
        entries = entries.filter(entry_date__lte=end_date)
        payments = payments.filter(payment_date__lte=end_date)

    bill_rows = _build_inventory_bill_summaries(entries.order_by('-entry_date', '-id'))
    _attach_inventory_credit_to_bill_summaries(bill_rows)
    payments = list(payments.order_by('-payment_date', '-created_at'))

    all_party_bills = _build_inventory_bill_summaries(
        scope_to_workspace(InventoryEntry.objects.filter(party=party), current_workspace)
        .select_related('bill', 'party', 'product', 'created_by', 'job_ticket')
        .order_by('-entry_date', '-id')
    )
    _attach_inventory_credit_to_bill_summaries(all_party_bills)

    payable_balance = sum(
        (bill['credit_balance_amount'] for bill in all_party_bills if bill['entry_type'] == 'purchase'),
        Decimal('0.00'),
    )
    receivable_balance = sum(
        (bill['credit_balance_amount'] for bill in all_party_bills if bill['entry_type'] == 'sale'),
        Decimal('0.00'),
    )
    if party.opening_balance:
        if party.opening_balance < Decimal('0.00'):
            payable_balance += abs(party.opening_balance)
        elif party.opening_balance > Decimal('0.00'):
            receivable_balance += abs(party.opening_balance)
    purchase_total = sum((bill['total_amount'] for bill in bill_rows if bill['entry_type'] == 'purchase'), Decimal('0.00'))
    sales_total = sum((bill['total_amount'] for bill in bill_rows if bill['entry_type'] == 'sale'), Decimal('0.00'))
    paid_total = sum(
        (payment.amount for payment in payments if payment.direction == InventoryCreditPayment.DIRECTION_PAYABLE),
        Decimal('0.00'),
    )
    received_total = sum(
        (payment.amount for payment in payments if payment.direction == InventoryCreditPayment.DIRECTION_RECEIVABLE),
        Decimal('0.00'),
    )

    context = {
        'party': party,
        'bill_rows': bill_rows,
        'payments': payments,
        'start_date': start_date.isoformat() if start_date else '',
        'end_date': end_date.isoformat() if end_date else '',
        'payable_balance': payable_balance,
        'receivable_balance': receivable_balance,
        'purchase_total': purchase_total,
        'sales_total': sales_total,
        'paid_total': paid_total,
        'received_total': received_total,
    }
    return render(request, 'job_tickets/inventory_party_ledger.html', context)

@login_required
@require_POST
def inventory_quick_add_party(request):
    if not request.user.is_staff or not user_has_staff_access(request.user, "inventory"):
        return JsonResponse({'ok': False, 'error': 'Unauthorized'}, status=403)

    payload = request.POST.copy()
    party_name = (payload.get('name') or '').strip()
    if not party_name:
        return JsonResponse({'ok': False, 'errors': ['Party name is required.']}, status=400)

    workspace = getattr(request, 'current_workspace', None)
    party_qs = InventoryParty.objects.filter(is_active=True, name__iexact=party_name)
    if workspace:
        party_qs = party_qs.filter(workspace=workspace)
    if party_qs.exists():
        return JsonResponse({'ok': False, 'errors': [f'A party with the name "{party_name}" already exists.']}, status=400)

    phone = (payload.get('phone') or '').strip()
    if phone:
        normalized_phone, phone_error = normalize_indian_phone(phone, required=False, field_label='Phone number')
        if phone_error:
            return JsonResponse({'ok': False, 'errors': [phone_error]}, status=400)
        phone = normalized_phone
        payload['phone'] = normalized_phone
        phone_qs = InventoryParty.objects.filter(is_active=True, phone=phone)
        if workspace:
            phone_qs = phone_qs.filter(workspace=workspace)
        if phone_qs.exists():
            existing = phone_qs.first()
            return JsonResponse({'ok': False, 'errors': [f'A party with phone number "{phone}" already exists ({existing.name}).']}, status=400)

    payload['is_active'] = 'on'
    payload.setdefault('gst_registration_type', 'registered' if (payload.get('gstin') or '').strip() else 'unregistered')
    payload.setdefault('country', 'India')
    payload.setdefault('opening_balance', '0')

    party_form = InventoryPartyForm(payload)
    if not party_form.is_valid():
        return JsonResponse({'ok': False, 'errors': _inventory_form_errors(party_form)}, status=400)

    party = party_form.save(commit=False)
    if not party.workspace_id:
        party.workspace = getattr(request, 'current_workspace', None)
    party.party_type = 'both'
    party.is_active = True
    party.save()

    label = f"{party.name} ({party.phone})" if (party.phone or '').strip() else party.name
    return JsonResponse(
        {
            'ok': True,
            'party': {
                'id': party.id,
                'name': party.name,
                'phone': party.phone or '',
                'label': label,
                'opening_balance': float(party.opening_balance or 0),
            },
        }
    )

@login_required
@require_http_methods(["GET"])
def inventory_invoice_preview(request):
    if not request.user.is_staff or not user_has_staff_access(request.user, "inventory"):
        return JsonResponse({'ok': False, 'error': 'Unauthorized'}, status=403)

    entry_type = (request.GET.get('entry_type') or 'sale').strip().lower()
    if entry_type != 'sale':
        return JsonResponse({'ok': False, 'error': 'Unsupported entry type'}, status=400)

    raw_entry_date = (request.GET.get('entry_date') or '').strip()
    entry_date = timezone.localdate()
    if raw_entry_date:
        try:
            entry_date = datetime.strptime(raw_entry_date, '%Y-%m-%d').date()
        except ValueError:
            return JsonResponse({'ok': False, 'error': 'Invalid entry date'}, status=400)

    return JsonResponse(
        {
            'ok': True,
            'invoice_number': _generate_inventory_invoice_number(entry_type, entry_date),
        }
    )

@login_required
@require_POST
def inventory_quick_add_product(request):
    if not request.user.is_staff or not (user_has_staff_access(request.user, "inventory") or user_has_staff_access(request.user, "staff_dashboard")):
        return JsonResponse({'ok': False, 'error': 'Unauthorized'}, status=403)

    payload = request.POST.copy()
    product_name = (payload.get('name') or '').strip()
    if not product_name:
        return JsonResponse({'ok': False, 'errors': ['Product name is required.']}, status=400)

    workspace = getattr(request, 'current_workspace', None)
    prod_qs = Product.objects.filter(name__iexact=product_name)
    if workspace:
        prod_qs = prod_qs.filter(workspace=workspace)
    if prod_qs.exists():
        return JsonResponse({'ok': False, 'errors': [f'A product with the name "{product_name}" already exists.']}, status=400)

    sku = (payload.get('sku') or '').strip()
    if sku:
        sku_qs = Product.objects.filter(sku__iexact=sku)
        if workspace:
            sku_qs = sku_qs.filter(workspace=workspace)
        if sku_qs.exists():
            return JsonResponse({'ok': False, 'errors': [f'A product with SKU "{sku}" already exists.']}, status=400)

    payload.setdefault('stock_quantity', '0')
    payload.setdefault('reserved_stock', '0')
    payload.setdefault('description', '')
    payload.setdefault('item_type', 'goods')
    payload.setdefault('tax_category', 'taxable')
    payload.setdefault('uqc', 'NOS')
    payload.setdefault('cess_rate', '0.00')
    payload.setdefault('purchase_price_tax_mode', 'without_tax')
    payload.setdefault('sales_price_tax_mode', 'without_tax')
    payload.setdefault('is_tax_inclusive_default', '')
    payload.setdefault('vendor_warranty_months', '0')
    payload.setdefault('customer_warranty_months', '0')

    try:
        payload.setdefault('gst_rate', str(CompanyProfile.get_profile(getattr(request, 'current_workspace', None)).gst_rate or Decimal('18.00')))
    except Exception:
        payload.setdefault('gst_rate', '18.00')

    product_form = ProductForm(payload)
    if not product_form.is_valid():
        return JsonResponse({'ok': False, 'errors': _inventory_form_errors(product_form)}, status=400)

    product = product_form.save(commit=False)
    if not product.workspace_id:
        product.workspace = getattr(request, 'current_workspace', None)
    product.cost_price = _normalize_tax_mode_price(
        product_form.cleaned_data.get('cost_price'),
        product_form.cleaned_data.get('purchase_price_tax_mode'),
        effective_tax_rate(
            product_form.cleaned_data.get('gst_rate'),
            product_form.cleaned_data.get('tax_category'),
        ),
    )
    product.unit_price = _normalize_tax_mode_price(
        product_form.cleaned_data.get('unit_price'),
        product_form.cleaned_data.get('sales_price_tax_mode'),
        effective_tax_rate(
            product_form.cleaned_data.get('gst_rate'),
            product_form.cleaned_data.get('tax_category'),
        ),
    )
    product.stock_quantity = 0
    product.save()
    if product.category:
        ItemCategory.get_or_create_category(product.category, workspace=product.workspace)

    return JsonResponse(
        {
            'ok': True,
            'product': {
                'id': product.id,
                'name': product.name,
                'sku': product.sku or '',
                'category': product.category or '',
                'brand': product.brand or '',
                'stock_quantity': int(product.stock_quantity or 0),
                'label': f"{product.name} (Stock: {int(product.stock_quantity or 0)})",
                'cost_price': format(product.cost_price, '.2f'),
                'unit_price': format(product.unit_price, '.2f'),
                'gst_rate': format(product.gst_rate, '.2f'),
                'tax_category': product.tax_category,
                'bin_location': product.bin_location or '',
                'description': product.description or '',
                'has_serial_tracking': bool(product.has_serial_tracking),
                'vendor_warranty_months': product.vendor_warranty_months or 0,
                'customer_warranty_months': product.customer_warranty_months or 0,
            },
        }
    )

@login_required
def inventory_purchase_dashboard(request):
    return _inventory_entry_dashboard(request, 'purchase')

@login_required
def inventory_purchase_return_dashboard(request):
    return _inventory_entry_dashboard(request, 'purchase_return')

@login_required
def inventory_sales_dashboard(request):
    return _inventory_entry_dashboard(request, 'sale')

@login_required
def inventory_sales_return_dashboard(request):
    return _inventory_entry_dashboard(request, 'sale_return')

@login_required
def inventory_sales_print_bill_view(request, bill_id):
    denied = _staff_access_required(request, "inventory")
    if denied:
        return denied

    sale_bill = (
        scope_to_workspace(
            InventoryBill.objects.filter(pk=bill_id, entry_type='sale'),
            getattr(request, 'current_workspace', None),
        )
        .select_related('party', 'job_ticket')
        .first()
    )
    if not sale_bill:
        messages.error(request, 'Selected sales bill was not found.')
        return redirect('inventory_sales_dashboard')

    sale_entries = list(
        sale_bill.lines.select_related('bill', 'party', 'product', 'created_by', 'job_ticket')
        .order_by('id')
    )

    if not sale_entries:
        messages.error(request, f"Sales bill '{sale_bill.bill_number}' has no line items.")
        return redirect('inventory_sales_dashboard')

    context = _prepare_inventory_sale_bill_print_context(sale_bill, sale_entries)
    return render(request, 'job_tickets/inventory_sales_print.html', context)

@login_required
def inventory_sales_print_view(request, invoice_number):
    denied = _staff_access_required(request, "inventory")
    if denied:
        return denied

    normalized_invoice = (invoice_number or '').strip()
    sale_bill = (
        scope_to_workspace(InventoryBill.objects.filter(
            entry_type='sale',
            invoice_number__iexact=normalized_invoice,
        ), getattr(request, 'current_workspace', None))
        .select_related('party', 'job_ticket')
        .first()
    )
    if sale_bill:
        sale_entries = list(
            sale_bill.lines.select_related('bill', 'party', 'product', 'created_by', 'job_ticket')
            .order_by('id')
        )
        if sale_entries:
            context = _prepare_inventory_sale_bill_print_context(
                sale_bill,
                sale_entries,
                invoice_label=normalized_invoice,
            )
            return render(request, 'job_tickets/inventory_sales_print.html', context)

    messages.error(request, f"Sales invoice '{normalized_invoice}' was not found.")
    return redirect('inventory_sales_dashboard')

@login_required
def client_dashboard(request):
    denied = _staff_access_required(request, "staff_dashboard")
    if denied:
        return denied

    query = (request.GET.get('q') or '').strip()
    active_tab = (request.GET.get('tab') or 'all').strip().lower()
    if active_tab not in {'all', 'credit_due', 'repeat'}:
        active_tab = 'all'

    current_workspace = getattr(request, 'current_workspace', None)
    clients = scope_to_workspace(Client.objects.all(), current_workspace)
    jobs_scope = scope_to_workspace(JobTicket.objects.all(), current_workspace)

    if query:
        clients = clients.filter(
            Q(name__icontains=query) |
            Q(phone__icontains=query) |
            Q(company_name__icontains=query)
        )

    if request.method == 'POST' and 'add_client_submit' in request.POST:
        client_form = ClientForm(request.POST)
        if client_form.is_valid():
            client = client_form.save(commit=False)
            if not client.workspace_id:
                client.workspace = current_workspace
            client.save()
            messages.success(request, f"Client '{client.name}' added successfully.")
            return redirect('client_dashboard')
        messages.error(request, "Please fix the errors and try again.")
    else:
        client_form = ClientForm()

    from collections import Counter

    client_rows = list(clients.order_by('-created_at')[:250])
    phone_to_client_id = {}
    all_lookup_phones = set()
    for c in client_rows:
        if c.phone:
            variants = phone_lookup_variants(c.phone)
            if c.phone not in variants:
                variants.append(c.phone)
            for v in variants:
                phone_to_client_id[v] = c.id
                all_lookup_phones.add(v)

    jobs_by_client_id = {c.id: [] for c in client_rows}
    if all_lookup_phones:
        job_rows = list(
            jobs_scope
            .filter(customer_phone__in=all_lookup_phones)
            .prefetch_related('service_logs')
            .defer('technician_notes', 'technician_checklist', 'feedback_followup_note')
            .order_by('-created_at')
        )
        calculate_job_totals(job_rows)
        for job in job_rows:
            job.discount_total = _money_or_zero(job.discount_amount)
            job.net_total = _net_amount_after_discount(job.total, job.discount_total)
            job.current_paid = job.amount_paid or Decimal('0.00')
            cid = phone_to_client_id.get(job.customer_phone)
            if cid and cid in jobs_by_client_id:
                jobs_by_client_id[cid].append(job)

    total_receivable_amount = Decimal('0.00')
    credit_due_clients_count = 0
    repeat_clients_count = 0

    for client in client_rows:
        client.jobs = jobs_by_client_id.get(client.id, [])
        client.total_jobs = len(client.jobs)

        dev_counts = Counter((j.device_type or '').strip() or 'Unknown Device' for j in client.jobs)
        client.device_breakdown = [{'device_type': dt, 'count': cnt} for dt, cnt in dev_counts.most_common()]

        # Financial summaries per client
        c_billed = sum(getattr(j, 'net_total', Decimal('0.00')) for j in client.jobs)
        c_paid = sum(getattr(j, 'current_paid', Decimal('0.00')) for j in client.jobs)
        c_balance = max(Decimal('0.00'), c_billed - c_paid)
        client.total_billed = c_billed
        client.amount_paid = c_paid
        client.balance_due = c_balance

        clean_digits = ''.join(ch for ch in client.phone if ch.isdigit())
        clean_10 = clean_digits[-10:] if len(clean_digits) >= 10 else clean_digits
        client.clean_10_digit = clean_10
        client.whatsapp_url = f"https://wa.me/91{clean_10}" if len(clean_10) == 10 else None

        total_receivable_amount += c_balance
        if c_balance > Decimal('0.00'):
            credit_due_clients_count += 1
        if client.total_jobs >= 2:
            repeat_clients_count += 1

    # Filter according to active tab
    if active_tab == 'credit_due':
        displayed_clients = [c for c in client_rows if c.balance_due > Decimal('0.00')]
    elif active_tab == 'repeat':
        displayed_clients = [c for c in client_rows if c.total_jobs >= 2]
    else:
        displayed_clients = client_rows

    active_jobs_count = jobs_scope.exclude(status__in=['Closed', 'Delivered', 'Cancelled']).count()
    recent_campaigns = scope_to_workspace(BulkCampaign.objects.all(), current_workspace).order_by('-created_at')[:5]

    saved_selection = request.session.get('selected_client_ids', [])
    if not isinstance(saved_selection, list):
        saved_selection = []
    saved_selection_ids = [int(x) for x in saved_selection if str(x).isdigit()]

    context = {
        'clients': displayed_clients,
        'client_form': client_form,
        'query': query,
        'active_tab': active_tab,
        'total_clients': clients.count(),
        'total_receivable_amount': total_receivable_amount,
        'credit_due_clients_count': credit_due_clients_count,
        'repeat_clients_count': repeat_clients_count,
        'active_jobs_count': active_jobs_count,
        'recent_campaigns': recent_campaigns,
        'selected_client_ids': saved_selection_ids,
        'selected_client_count': len(saved_selection_ids),
    }
    return render(request, 'job_tickets/client_dashboard.html', context)


@login_required
@require_POST
def edit_client(request, client_id):
    denied = _staff_access_required(request, "staff_dashboard")
    if denied:
        return denied

    current_workspace = getattr(request, 'current_workspace', None)
    client = get_object_or_404(scope_to_workspace(Client.objects.filter(id=client_id), current_workspace))

    name = (request.POST.get('name') or '').strip()
    phone_raw = (request.POST.get('phone') or '').strip()
    email = (request.POST.get('email') or '').strip()
    company_name = (request.POST.get('company_name') or '').strip()
    address = (request.POST.get('address') or '').strip()
    notes = (request.POST.get('notes') or '').strip()

    if not name:
        messages.error(request, "Client name is required.")
        return redirect(request.POST.get('next') or reverse('client_dashboard'))

    normalized_phone, phone_error = normalize_indian_phone(phone_raw, required=True, field_label="Phone number")
    if phone_error:
        messages.error(request, phone_error)
        return redirect(request.POST.get('next') or reverse('client_dashboard'))

    duplicate_qs = Client.objects.filter(phone=normalized_phone).exclude(id=client.id)
    if client.workspace:
        duplicate_qs = duplicate_qs.filter(workspace=client.workspace)
    if duplicate_qs.exists():
        messages.error(request, f"Another client already exists with phone {normalized_phone}.")
        return redirect(request.POST.get('next') or reverse('client_dashboard'))

    old_phone = client.phone
    old_name = client.name
    client._previous_phone = old_phone
    client._previous_name = old_name
    client.name = name
    client.phone = normalized_phone
    client.email = email
    client.company_name = company_name
    client.address = address
    client.notes = notes
    client.save()

    messages.success(request, f"Client '{client.name}' updated successfully.")
    next_url = request.POST.get('next')
    if next_url and url_has_allowed_host_and_scheme(next_url, allowed_hosts={request.get_host()}):
        return redirect(next_url)
    return redirect('client_dashboard')


@login_required
def client_detail(request, client_id):
    denied = _staff_access_required(request, "staff_dashboard")
    if denied:
        return denied

    current_workspace = getattr(request, 'current_workspace', None)
    client = get_object_or_404(scope_to_workspace(Client.objects.filter(id=client_id), current_workspace))

    variants = phone_lookup_variants(client.phone)
    if client.phone not in variants:
        variants.append(client.phone)

    jobs_scope = scope_to_workspace(JobTicket.objects.all(), current_workspace)
    job_rows = list(
        jobs_scope
        .filter(customer_phone__in=variants)
        .prefetch_related('service_logs')
        .defer('technician_notes', 'technician_checklist', 'feedback_followup_note')
        .order_by('-created_at')
    )
    calculate_job_totals(job_rows)
    for job in job_rows:
        job.discount_total = _money_or_zero(job.discount_amount)
        job.net_total = _net_amount_after_discount(job.total, job.discount_total)
        job.current_paid = job.amount_paid or Decimal('0.00')

    total_billed = sum(getattr(j, 'net_total', Decimal('0.00')) for j in job_rows)
    total_paid = sum(getattr(j, 'current_paid', Decimal('0.00')) for j in job_rows)
    total_balance_due = max(Decimal('0.00'), total_billed - total_paid)
    active_jobs = [j for j in job_rows if j.status not in {'Closed', 'Delivered', 'Cancelled'}]

    party_qs = InventoryParty.objects.filter(
        party_type__in=['customer', 'both'],
        phone__in=variants,
    )
    if client.workspace:
        party_qs = party_qs.filter(workspace=client.workspace)
    else:
        party_qs = party_qs.filter(workspace__isnull=True)
    party = party_qs.first()

    credit_payments = list(
        InventoryCreditPayment.objects.filter(
            Q(party=party) if party else Q(bill__job_ticket__in=job_rows)
        ).select_related('bill').order_by('-payment_date', '-created_at')
    ) if (party or job_rows) else []

    inventory_bills = list(
        InventoryBill.objects.filter(
            Q(party=party) if party else Q(job_ticket__in=job_rows)
        ).order_by('-entry_date', '-id')
    ) if (party or job_rows) else []

    clean_digits = ''.join(ch for ch in client.phone if ch.isdigit())
    clean_10 = clean_digits[-10:] if len(clean_digits) >= 10 else clean_digits
    whatsapp_url = f"https://wa.me/91{clean_10}" if len(clean_10) == 10 else None

    context = {
        'client': client,
        'jobs': job_rows,
        'active_jobs': active_jobs,
        'total_jobs_count': len(job_rows),
        'total_billed': total_billed,
        'total_paid': total_paid,
        'total_balance_due': total_balance_due,
        'party': party,
        'credit_payments': credit_payments,
        'inventory_bills': inventory_bills,
        'whatsapp_url': whatsapp_url,
        'clean_10_digit': clean_10,
    }
    return render(request, 'job_tickets/client_detail.html', context)

@login_required
def product_dashboard(request):
    denied = _staff_access_required(request, "inventory")
    if denied:
        return denied

    current_url_name = getattr(getattr(request, 'resolver_match', None), 'url_name', '')
    redirect_target = 'inventory_product_dashboard' if current_url_name == 'inventory_product_dashboard' else 'product_dashboard'

    query = (request.GET.get('q') or '').strip()
    category_filter = (request.GET.get('category') or '').strip()
    current_workspace = getattr(request, 'current_workspace', None)
    products = scope_to_workspace(Product.objects.all(), current_workspace)

    if query:
        products = products.filter(
            Q(name__icontains=query) |
            Q(sku__icontains=query) |
            Q(category__icontains=query) |
            Q(brand__icontains=query) |
            Q(hsn_sac_code__icontains=query) |
            Q(uqc__icontains=query)
        )
    if category_filter:
        products = products.filter(category__iexact=category_filter)

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
            InventoryEntry.objects.filter(product_id__in=product_ids)
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

        # Margin & Stock Status computations
        stock_qty = product.stock_quantity or 0
        res_stock = product.reserved_stock or 0
        if stock_qty <= 0:
            product.stock_status = 'out_of_stock'
        elif res_stock > 0 and stock_qty <= res_stock:
            product.stock_status = 'reserved_alert'
        else:
            product.stock_status = 'in_stock'

        cost = product.cost_price or Decimal('0.00')
        price = product.unit_price or Decimal('0.00')
        if cost > Decimal('0.00') and price > cost:
            profit = price - cost
            margin = (profit / price) * Decimal('100.00')
            product.profit_amount = profit
            product.margin_percent = margin.quantize(Decimal('0.1'))
        else:
            product.profit_amount = None
            product.margin_percent = None

        # Stock Value computation
        product.stock_value = (Decimal(max(0, stock_qty)) * cost).quantize(Decimal('0.01'))

    if request.method == 'POST':
        if 'update_reserved_stock_submit' in request.POST:
            is_async_request = request.headers.get('X-Botgi-Async') == '1'
            reload_url = request.get_full_path()
            product = scope_to_workspace(
                Product.objects.filter(pk=request.POST.get('product_id')),
                current_workspace,
            ).first()
            if not product:
                if is_async_request:
                    return JsonResponse({'ok': False, 'message': 'Selected product was not found.'}, status=404)
                messages.error(request, 'Selected product was not found.')
                return redirect(redirect_target)

            try:
                reserved_stock = int((request.POST.get('reserved_stock') or '0').strip() or '0')
            except (TypeError, ValueError):
                if is_async_request:
                    return JsonResponse({'ok': False, 'message': 'Reserved stock must be a whole number.'}, status=400)
                messages.error(request, 'Reserved stock must be a whole number.')
                return redirect(redirect_target)

            if reserved_stock < 0:
                if is_async_request:
                    return JsonResponse({'ok': False, 'message': 'Reserved stock cannot be negative.'}, status=400)
                messages.error(request, 'Reserved stock cannot be negative.')
                return redirect(redirect_target)

            if product.reserved_stock != reserved_stock:
                product.reserved_stock = reserved_stock
                product.save(update_fields=['reserved_stock', 'updated_at'])
                if is_async_request:
                    return JsonResponse(
                        {
                            'ok': True,
                            'message': f"Reserved stock updated for '{product.name}'.",
                            'reload_url': reload_url,
                        }
                    )
                messages.success(request, f"Reserved stock updated for '{product.name}'.")
            else:
                if is_async_request:
                    return JsonResponse(
                        {
                            'ok': True,
                            'message': f"No reserved stock change for '{product.name}'.",
                            'reload_url': reload_url,
                        }
                    )
                messages.info(request, f"No reserved stock change for '{product.name}'.")
            return redirect(redirect_target)

        if 'add_product_submit' in request.POST:
            product_name = (request.POST.get('name') or '').strip()
            existing_prod = Product.objects.filter(name__iexact=product_name)
            if current_workspace:
                existing_prod = existing_prod.filter(workspace=current_workspace)
            if existing_prod.exists():
                messages.error(request, f'A product with the name "{product_name}" already exists.')
                product_form = ProductForm(request.POST)
            else:
                product_form = ProductForm(request.POST)
                if product_form.is_valid():
                    gst_rate = effective_tax_rate(
                        product_form.cleaned_data.get('gst_rate'),
                        product_form.cleaned_data.get('tax_category'),
                    )
                    product = product_form.save(commit=False)
                    product.cost_price = _normalize_tax_mode_price(
                        product_form.cleaned_data.get('cost_price'),
                        product_form.cleaned_data.get('purchase_price_tax_mode'),
                        gst_rate,
                    )
                    product.unit_price = _normalize_tax_mode_price(
                        product_form.cleaned_data.get('unit_price'),
                        product_form.cleaned_data.get('sales_price_tax_mode'),
                        gst_rate,
                    )
                    if not product.workspace_id:
                        product.workspace = getattr(request, 'current_workspace', None)
                    product.save()
                    if product.category:
                        ItemCategory.get_or_create_category(product.category, workspace=product.workspace)
                    messages.success(request, f"Product '{product.name}' added successfully.")
                    return redirect(redirect_target)
                messages.error(request, "Please fix the errors and try again.")
        elif 'edit_product_submit' in request.POST:
            product_id = request.POST.get('product_id')
            product = scope_to_workspace(
                Product.objects.filter(pk=product_id),
                current_workspace,
            ).first()
            if not product:
                messages.error(request, "Selected product was not found.")
                return redirect(redirect_target)

            product_form = ProductForm(request.POST, instance=product)
            if product_form.is_valid():
                gst_rate = effective_tax_rate(
                    product_form.cleaned_data.get('gst_rate'),
                    product_form.cleaned_data.get('tax_category'),
                )
                edited_product = product_form.save(commit=False)
                edited_product.cost_price = _normalize_tax_mode_price(
                    product_form.cleaned_data.get('cost_price'),
                    product_form.cleaned_data.get('purchase_price_tax_mode'),
                    gst_rate,
                )
                edited_product.unit_price = _normalize_tax_mode_price(
                    product_form.cleaned_data.get('unit_price'),
                    product_form.cleaned_data.get('sales_price_tax_mode'),
                    gst_rate,
                )
                edited_product.save()
                if edited_product.category:
                    ItemCategory.get_or_create_category(edited_product.category, workspace=edited_product.workspace)
                messages.success(request, f"Product '{edited_product.name}' updated successfully.")
                return redirect(redirect_target)
            else:
                err_msgs = [f"{field}: {errors[0]}" for field, errors in product_form.errors.items()]
                messages.error(request, f"Please fix the errors in the edit form: {'; '.join(err_msgs)}")
        elif 'update_product_price_submit' in request.POST:
            product_id = request.POST.get('product_id')
            product = scope_to_workspace(
                Product.objects.filter(pk=product_id),
                current_workspace,
            ).first()
            if not product:
                messages.error(request, "Selected product was not found.")
                return redirect(redirect_target)

            cost_raw = (request.POST.get('cost_price') or '').strip()
            unit_raw = (request.POST.get('unit_price') or '').strip()
            if (cost_raw and not RATE_CLEAN_REGEX.match(cost_raw)) or (unit_raw and not RATE_CLEAN_REGEX.match(unit_raw)):
                messages.error(request, "Special characters are not allowed in price/rate fields.")
                return redirect(redirect_target)

            try:
                raw_cost = Decimal(cost_raw or '0.00')
                raw_unit = Decimal(unit_raw or '0.00')
            except Exception:
                messages.error(request, "Please enter valid numeric prices.")
                return redirect(redirect_target)

            if raw_cost < Decimal('0.00') or raw_unit < Decimal('0.00'):
                messages.error(request, "Prices cannot be negative.")
                return redirect(redirect_target)

            purchase_tax_mode = request.POST.get('purchase_price_tax_mode') or 'without_tax'
            sales_tax_mode = request.POST.get('sales_price_tax_mode') or 'without_tax'
            gst_rate = effective_tax_rate(product.gst_rate, product.tax_category)

            product.cost_price = _normalize_tax_mode_price(raw_cost, purchase_tax_mode, gst_rate)
            product.unit_price = _normalize_tax_mode_price(raw_unit, sales_tax_mode, gst_rate)
            product.save(update_fields=['cost_price', 'unit_price', 'updated_at'])
            messages.success(request, f"Prices updated for '{product.name}' (Purchase: ₹{product.cost_price:.2f}, Selling: ₹{product.unit_price:.2f}).")
            return redirect(redirect_target)
        elif 'delete_product_submit' in request.POST:
            product_id = request.POST.get('product_id')
            product = scope_to_workspace(
                Product.objects.filter(pk=product_id),
                current_workspace,
            ).first()
            if not product:
                messages.error(request, "Selected product was not found.")
                return redirect(redirect_target)

            has_entries = InventoryEntry.objects.filter(product=product).exists()
            has_sales = ProductSale.objects.filter(product=product).exists()
            if has_entries or has_sales:
                product.is_active = False
                product.save(update_fields=['is_active', 'updated_at'])
                messages.info(request, f"Product '{product.name}' has transaction records and was deactivated.")
            else:
                prod_name = product.name
                product.delete()
                messages.success(request, f"Product '{prod_name}' deleted successfully.")
            return redirect(redirect_target)
        elif 'add_category_submit' in request.POST:
            is_async = request.headers.get('X-Botgi-Async') == '1' or request.headers.get('X-Requested-With') == 'XMLHttpRequest'
            cat_name = (request.POST.get('category_name') or '').strip()
            cat_desc = (request.POST.get('category_description') or '').strip()
            if not cat_name:
                if is_async:
                    return JsonResponse({'ok': False, 'message': 'Category name cannot be empty.'}, status=400)
                messages.error(request, 'Category name cannot be empty.')
                return redirect(redirect_target)

            existing = ItemCategory.objects.filter(workspace=current_workspace, name__iexact=cat_name).first()
            if existing:
                if is_async:
                    return JsonResponse({'ok': False, 'message': f"Category '{cat_name}' already exists."}, status=400)
                messages.error(request, f"Category '{cat_name}' already exists.")
                return redirect(redirect_target)

            cat = ItemCategory.objects.create(
                workspace=current_workspace,
                name=cat_name,
                description=cat_desc,
            )
            if is_async:
                return JsonResponse({
                    'ok': True,
                    'message': f"Category '{cat.name}' created successfully.",
                    'category': {'id': cat.id, 'name': cat.name, 'description': cat.description, 'product_count': 0}
                })
            messages.success(request, f"Category '{cat.name}' created successfully.")
            return redirect(redirect_target)
        elif 'edit_category_submit' in request.POST:
            is_async = request.headers.get('X-Botgi-Async') == '1' or request.headers.get('X-Requested-With') == 'XMLHttpRequest'
            cat_id = request.POST.get('category_id')
            new_name = (request.POST.get('category_name') or '').strip()
            cat_desc = (request.POST.get('category_description') or '').strip()

            cat = ItemCategory.objects.filter(pk=cat_id, workspace=current_workspace).first()
            if not cat:
                if is_async:
                    return JsonResponse({'ok': False, 'message': 'Category not found.'}, status=404)
                messages.error(request, 'Category not found.')
                return redirect(redirect_target)

            if not new_name:
                if is_async:
                    return JsonResponse({'ok': False, 'message': 'Category name cannot be empty.'}, status=400)
                messages.error(request, 'Category name cannot be empty.')
                return redirect(redirect_target)

            conflict = ItemCategory.objects.filter(workspace=current_workspace, name__iexact=new_name).exclude(pk=cat.pk).first()
            if conflict:
                if is_async:
                    return JsonResponse({'ok': False, 'message': f"Another category named '{new_name}' already exists."}, status=400)
                messages.error(request, f"Another category named '{new_name}' already exists.")
                return redirect(redirect_target)

            old_name = cat.name
            cat.name = new_name
            cat.description = cat_desc
            cat.save()

            if old_name.lower() != new_name.lower():
                Product.objects.filter(workspace=current_workspace, category__iexact=old_name).update(category=new_name)

            if is_async:
                return JsonResponse({'ok': True, 'message': f"Category updated to '{cat.name}'."})
            messages.success(request, f"Category updated to '{cat.name}'.")
            return redirect(redirect_target)
        elif 'delete_category_submit' in request.POST:
            is_async = request.headers.get('X-Botgi-Async') == '1' or request.headers.get('X-Requested-With') == 'XMLHttpRequest'
            cat_id = request.POST.get('category_id')

            cat = ItemCategory.objects.filter(pk=cat_id, workspace=current_workspace).first()
            if not cat:
                if is_async:
                    return JsonResponse({'ok': False, 'message': 'Category not found.'}, status=404)
                messages.error(request, 'Category not found.')
                return redirect(redirect_target)

            cat_name = cat.name
            Product.objects.filter(workspace=current_workspace, category__iexact=cat_name).update(category='')
            cat.delete()

            if is_async:
                return JsonResponse({'ok': True, 'message': f"Category '{cat_name}' deleted."})
            messages.success(request, f"Category '{cat_name}' deleted.")
            return redirect(redirect_target)
        else:
            product_form = ProductForm()
    else:
        product_form = ProductForm()

    products_scope = scope_to_workspace(Product.objects.filter(is_active=True), current_workspace)
    out_of_stock_count = products_scope.filter(stock_quantity__lte=0).count()
    in_stock_count = products_scope.filter(stock_quantity__gt=0).count()
    reserved_alert_count = products_scope.filter(
        reserved_stock__gt=0,
        stock_quantity__lte=F('reserved_stock'),
    ).count()
    categories = ItemCategory.get_categories_with_counts(workspace=current_workspace)
    total_stock_quantity = sum(int(p.stock_quantity or 0) for p in product_rows)
    total_stock_value = sum((getattr(p, 'stock_value', None) or Decimal('0.00')) for p in product_rows)

    context = {
        'products': product_rows,
        'product_form': product_form,
        'query': query,
        'category_filter': category_filter,
        'selected_category': category_filter,
        'categories': categories,
        'total_products': products_scope.count(),
        'in_stock_count': in_stock_count,
        'reserved_alert_count': reserved_alert_count,
        'out_of_stock_count': out_of_stock_count,
        'total_stock_quantity': total_stock_quantity,
        'total_stock_value': total_stock_value,
        'from_inventory': current_url_name == 'inventory_product_dashboard',
    }
    return render(request, 'job_tickets/product_dashboard.html', context)


@login_required
def inventory_track_serial(request):
    if not request.user.is_staff or not user_has_staff_access(request.user, "inventory"):
        return JsonResponse({'ok': False, 'error': 'Unauthorized'}, status=403)

    query = (request.GET.get('serial') or '').strip()
    if not query:
        return JsonResponse({'ok': False, 'error': 'Serial number query is required.'}, status=400)

    workspace = getattr(request, 'current_workspace', None)
    item = scope_to_workspace(
        ProductItemSerial.objects.filter(serial_number__iexact=query),
        workspace,
    ).select_related(
        'product', 'purchase_entry__party', 'purchase_entry__bill',
        'sale_entry__party', 'sale_entry__bill', 'job_ticket'
    ).first()

    if not item:
        return JsonResponse({
            'ok': True,
            'found': False,
            'message': f"No item found with Serial / IMEI '{query}'.",
        })

    purchase_party = item.purchase_entry.party.name if item.purchase_entry and item.purchase_entry.party else ''
    purchase_invoice = item.purchase_entry.invoice_number if item.purchase_entry else ''
    sale_party = item.sale_entry.party.name if item.sale_entry and item.sale_entry.party else ''
    sale_invoice = item.sale_entry.invoice_number if item.sale_entry else ''
    job_code = item.job_ticket.daily_job_code if item.job_ticket else ''

    today = timezone.localdate()
    vendor_warranty_days = (item.vendor_warranty_until - today).days if item.vendor_warranty_until else None
    customer_warranty_days = (item.customer_warranty_until - today).days if item.customer_warranty_until else None

    return JsonResponse({
        'ok': True,
        'found': True,
        'item': {
            'serial_number': item.serial_number,
            'product_id': item.product.id,
            'product_name': item.product.name,
            'sku': item.product.sku or '',
            'bin_location': item.product.bin_location or 'Not specified',
            'status': item.status,
            'status_label': item.get_status_display(),
            'purchase_date': item.purchase_date.isoformat() if item.purchase_date else '',
            'purchase_cost': format(item.purchase_cost, '.2f'),
            'supplier_name': purchase_party,
            'purchase_invoice': purchase_invoice,
            'sale_date': item.sale_date.isoformat() if item.sale_date else '',
            'sale_price': format(item.sale_price, '.2f'),
            'customer_name': sale_party,
            'sale_invoice': sale_invoice,
            'job_ticket_code': job_code,
            'vendor_warranty_until': item.vendor_warranty_until.isoformat() if item.vendor_warranty_until else '',
            'is_vendor_warranty_active': item.is_vendor_warranty_active,
            'vendor_warranty_days': vendor_warranty_days,
            'customer_warranty_until': item.customer_warranty_until.isoformat() if item.customer_warranty_until else '',
            'is_customer_warranty_active': item.is_customer_warranty_active,
            'customer_warranty_days': customer_warranty_days,
            'notes': item.notes or '',
        },
    })


@login_required
def inventory_pincode_lookup(request, pincode):
    denied = _staff_access_required(request, "inventory")
    if denied:
        return denied

    code = (pincode or '').strip()
    if not code.isdigit() or len(code) != 6:
        return JsonResponse({'success': False, 'error': 'Invalid PIN code. Must be 6 digits.'}, status=400)

    cache_key = f"pincode_lookup_{code}"
    cached = cache.get(cache_key)
    if cached:
        return JsonResponse(cached)

    import urllib.request
    import json

    url = f"https://api.postalpincode.in/pincode/{code}"
    req = urllib.request.Request(
        url,
        headers={'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64)'}
    )
    try:
        with urllib.request.urlopen(req, timeout=5) as response:
            payload = json.loads(response.read().decode('utf-8'))
            if payload and isinstance(payload, list) and payload[0].get('Status') == 'Success':
                post_offices = payload[0].get('PostOffice') or []
                if post_offices:
                    district = post_offices[0].get('District') or ''
                    state = post_offices[0].get('State') or ''
                    result = {
                        'success': True,
                        'city': district,
                        'state': state,
                        'pincode': code,
                    }
                    cache.set(cache_key, result, 86400 * 7)
                    return JsonResponse(result)
    except Exception:
        pass

    return JsonResponse({'success': False, 'error': 'Location not found for this PIN code.'}, status=404)

