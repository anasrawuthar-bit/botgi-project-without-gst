# job_tickets/forms.py
from decimal import Decimal

from django import forms
from django.db.models import Q
from django.utils import timezone

from .models import (
    Client,
    CompanyProfile,
    Expense,
    FinancialAccount,
    InventoryEntry,
    InventoryParty,
    JobTicket,
    Product,
    ServiceLog,
    Assignment,
    Task,
    TaskAttachment,
    TaskMessage,
    TechnicianProfile,
    Vendor,
    WhatsAppIntegrationSettings,
)
from django.contrib.auth.models import User, Group
from django.contrib.auth.forms import UserCreationForm
from .gst_utils import (
    normalize_compact_code,
    normalize_text_code,
)
from .phone_utils import normalize_indian_phone


class TechnicianCreationForm(UserCreationForm):
    ROLE_CHOICES = [
        ('technician', 'Technician'),
        ('staff', 'Staff Member')
    ]
    
    full_name = forms.CharField(max_length=150, required=False, label='Full Name', help_text='Full name of the user')
    unique_id = forms.CharField(max_length=10, required=True, help_text='Unique identifier for the user')
    role = forms.ChoiceField(choices=ROLE_CHOICES, required=True, help_text='Select whether this user is a technician or staff member')
    
    class Meta(UserCreationForm.Meta):
        model = User
        fields = ['username', 'email']

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.order_fields(['username', 'full_name', 'email', 'unique_id', 'role'])

    def save(self, commit=False):
        user = super().save(commit=False)
        full_name = (self.cleaned_data.get('full_name') or '').strip()
        if full_name:
            parts = full_name.split(maxsplit=1)
            user.first_name = parts[0]
            user.last_name = parts[1] if len(parts) > 1 else ''
        else:
            user.first_name = ''
            user.last_name = ''
        if commit:
            user.save()
        return user

class JobTicketForm(forms.ModelForm):
    class Meta:
        model = JobTicket
        fields = ['customer_name', 'customer_phone', 'device_type', 
                  'device_brand', 'device_model', 'device_serial', 'device_password',
                  'reported_issue', 'additional_items', 'is_under_warranty',
                  'estimated_amount', 'estimated_delivery']

        widgets = {
            'estimated_delivery': forms.DateInput(attrs={'type': 'date'}),

            'customer_name': forms.TextInput(attrs={
                'autocomplete': 'off',
                'placeholder': 'Enter customer full name with address',
            }),
            'customer_phone': forms.TextInput(attrs={
                'type': 'text',
                'autocomplete': 'off',
                'inputmode': 'numeric',
            }),

            'device_type': forms.TextInput(attrs={
            'autocomplete': 'off'
                }),

            'device_model': forms.TextInput(attrs={
            'autocomplete': 'off'
                }),

            'device_serial': forms.TextInput(attrs={
            'autocomplete': 'off'
                }),

            'device_password': forms.TextInput(attrs={
                'autocomplete': 'off',
                'placeholder': 'e.g. 1234, Pattern, None',
            }),
        }

    def clean_customer_phone(self):
        phone, error = normalize_indian_phone(
            self.cleaned_data.get('customer_phone'),
            field_label='Customer Phone',
        )
        if error:
            raise forms.ValidationError(error)
        return phone

class ServiceLogForm(forms.ModelForm):
    class Meta:
        model = ServiceLog
        fields = ['description', 'part_cost', 'service_charge']


def get_assignable_technician_queryset(workspace=None):
    queryset = TechnicianProfile.objects.filter(
        user__is_active=True,
        user__is_staff=False,
    )
    if workspace:
        queryset = queryset.filter(Q(workspace=workspace) | Q(workspace__isnull=True))
    return queryset.select_related('user').order_by('user__username')


class AssignJobForm(forms.Form):
    technician = forms.ModelChoiceField(
        queryset=TechnicianProfile.objects.none(),
        label="Assign to Technician"
    )
    job_code = forms.CharField(widget=forms.HiddenInput())

    def __init__(self, *args, **kwargs):
        workspace = kwargs.pop('workspace', None)
        super().__init__(*args, **kwargs)
        self.fields['technician'].queryset = get_assignable_technician_queryset(workspace)

class ReworkForm(forms.Form):
    rework_reason = forms.CharField(label="Reason for Rework", widget=forms.Textarea(attrs={'class': 'form-control'}))

class DiscountForm(forms.Form):
    discount_amount = forms.DecimalField(
        label="Discount Amount",
        max_digits=10,
        decimal_places=2,
        required=False,
        widget=forms.NumberInput(attrs={'class': 'form-control'})
    )


class AssignVendorForm(forms.Form):
    vendor = forms.ModelChoiceField(
        queryset=Vendor.objects.filter(is_active=True),
        label="Assign to Vendor",
        empty_label="-- Select a Vendor --"
    )
    # This hidden field will identify which SpecializedService record we are updating
    specialized_service_id = forms.IntegerField(widget=forms.HiddenInput())

    def __init__(self, *args, **kwargs):
        workspace = kwargs.pop('workspace', None)
        super().__init__(*args, **kwargs)
        queryset = Vendor.objects.filter(is_active=True)
        if workspace:
            queryset = queryset.filter(Q(workspace=workspace) | Q(workspace__isnull=True))
        self.fields['vendor'].queryset = queryset.order_by('company_name')

class ReturnVendorServiceForm(forms.Form):
    """Form for when device returns from vendor - requires cost fields"""
    vendor_cost = forms.DecimalField(
        label="Vendor Bill Amount",
        required=True,
        widget=forms.NumberInput(attrs={'placeholder': 'e.g., 2500', 'step': '0.01'})
    )
    client_charge = forms.DecimalField(
        label="Client Charge",
        required=True,
        widget=forms.NumberInput(attrs={'placeholder': 'e.g., 3500', 'step': '0.01'})
    )
    specialized_service_id = forms.IntegerField(widget=forms.HiddenInput())

class ReassignTechnicianForm(forms.Form):
    job_code = forms.CharField(widget=forms.HiddenInput())
    new_technician = forms.ModelChoiceField(
        queryset=TechnicianProfile.objects.none(),
        label="Select New Technician",
        empty_label="--- Unassign Job ---",
        required=False
    )

    def __init__(self, *args, **kwargs):
        workspace = kwargs.pop('workspace', None)
        super().__init__(*args, **kwargs)
        self.fields['new_technician'].queryset = get_assignable_technician_queryset(workspace)


class TaskCreateForm(forms.Form):
    title = forms.CharField(
        max_length=200,
        widget=forms.TextInput(attrs={'class': 'form-control', 'placeholder': 'Task title...'}),
        label='Title',
    )
    description = forms.CharField(
        required=False,
        widget=forms.Textarea(attrs={'class': 'form-control', 'rows': 3, 'placeholder': 'Describe the task...'}),
        label='Description',
    )
    priority = forms.ChoiceField(
        choices=Task.PRIORITY_CHOICES,
        initial=Task.PRIORITY_MEDIUM,
        widget=forms.Select(attrs={'class': 'form-select'}),
        label='Priority',
    )
    due_date = forms.DateTimeField(
        required=False,
        input_formats=['%Y-%m-%dT%H:%M', '%Y-%m-%d %H:%M:%S', '%Y-%m-%d %H:%M', '%Y-%m-%d'],
        widget=forms.DateTimeInput(attrs={'type': 'datetime-local', 'class': 'form-control'}),
        label='Due Date',
    )
    assigned_to = forms.ModelChoiceField(
        queryset=TechnicianProfile.objects.none(),
        required=False,
        empty_label='--- Select Technician ---',
        widget=forms.Select(attrs={
            'class': 'form-select',
            'data-searchable-select': 'true',
            'data-icon': 'fa-solid fa-user-gear',
            'data-placeholder': 'Select technician...',
            'id': 'task-assigned-to-select',
        }),
        label='Assign To',
    )
    is_open_to_all = forms.BooleanField(
        required=False,
        initial=False,
        widget=forms.CheckboxInput(attrs={'class': 'form-check-input', 'id': 'task-is-open-to-all'}),
        label='Open to All Technicians (Shared Pool)',
    )
    has_alarm = forms.BooleanField(
        required=False,
        initial=False,
        widget=forms.CheckboxInput(attrs={'class': 'form-check-input', 'id': 'task-has-alarm'}),
        label='Set Alarm Alert (High Priority Audible Ring)',
    )
    alarm_time = forms.DateTimeField(
        required=False,
        input_formats=['%Y-%m-%dT%H:%M', '%Y-%m-%d %H:%M:%S', '%Y-%m-%d %H:%M', '%Y-%m-%d'],
        widget=forms.DateTimeInput(attrs={'type': 'datetime-local', 'class': 'form-control', 'id': 'task-alarm-time'}),
        label='Alarm Time (optional, defaults to due date)',
    )
    job_reference = forms.ModelChoiceField(
        queryset=JobTicket.objects.none(),
        required=False,
        empty_label='--- No linked job ---',
        widget=forms.Select(attrs={
            'class': 'form-select',
            'data-searchable-select': 'true',
            'data-icon': 'fa-solid fa-ticket-alt',
            'data-placeholder': 'Type to search job code, customer, device...',
        }),
        label='Linked Job (optional)',
    )

    def __init__(self, *args, **kwargs):
        workspace = kwargs.pop('workspace', None)
        super().__init__(*args, **kwargs)
        self.fields['assigned_to'].queryset = get_assignable_technician_queryset(workspace)

        def tech_label(obj):
            full_name = obj.user.get_full_name()
            if full_name and full_name.strip() and full_name.strip() != obj.user.username:
                return f"{full_name.strip()} ({obj.user.username})"
            return obj.user.username

        self.fields['assigned_to'].label_from_instance = tech_label

        qs = JobTicket.objects.order_by('-created_at')
        if workspace:
            qs = qs.filter(workspace=workspace)
        self.fields['job_reference'].queryset = qs.only('id', 'job_code', 'customer_name', 'device_type', 'status')

        def job_label(obj):
            device = f" • {obj.device_type}" if obj.device_type else ""
            customer = f" • {obj.customer_name}" if obj.customer_name else ""
            return f"{obj.job_code}{customer}{device}"

        self.fields['job_reference'].label_from_instance = job_label

    def clean(self):
        cleaned_data = super().clean()
        is_open_to_all = bool(cleaned_data.get('is_open_to_all'))
        assigned_to = cleaned_data.get('assigned_to')

        if is_open_to_all and assigned_to:
            raise forms.ValidationError(
                "A task cannot be assigned to both 'Open to All' and a specific technician. "
                "Please choose either Open to All or an individual assignee."
            )

        if is_open_to_all:
            cleaned_data['assigned_to'] = None

        return cleaned_data


class TaskMessageForm(forms.Form):
    body = forms.CharField(
        widget=forms.Textarea(attrs={
            'class': 'form-control',
            'rows': 2,
            'placeholder': 'Type a message...',
        }),
        label='Message',
    )


class ExpenseForm(forms.ModelForm):
    class Meta:
        model = Expense
        fields = [
            'title',
            'amount',
            'category',
            'expense_date',
            'payment_mode',
            'financial_account',
            'job_ticket',
            'is_billable_to_customer',
            'receipt_file',
            'notes',
        ]
        widgets = {
            'title': forms.TextInput(attrs={
                'class': 'form-control',
                'placeholder': 'e.g. Courier charges, Tea & snacks, Shop rent...',
                'required': 'required',
            }),
            'amount': forms.NumberInput(attrs={
                'class': 'form-control',
                'step': '0.01',
                'min': '0.01',
                'placeholder': '0.00',
                'required': 'required',
            }),
            'category': forms.Select(attrs={'class': 'form-select'}),
            'expense_date': forms.DateInput(attrs={'type': 'date', 'class': 'form-control'}),
            'payment_mode': forms.Select(attrs={'class': 'form-select'}),
            'financial_account': forms.Select(attrs={'class': 'form-select'}),
            'job_ticket': forms.Select(attrs={
                'class': 'form-select',
                'data-searchable-select': 'true',
                'data-icon': 'fa-solid fa-ticket-alt',
                'data-placeholder': 'Type to search job code, customer, device...',
            }),
            'is_billable_to_customer': forms.CheckboxInput(attrs={'class': 'form-check-input'}),
            'receipt_file': forms.ClearableFileInput(attrs={'class': 'form-control'}),
            'notes': forms.Textarea(attrs={'class': 'form-control', 'rows': 2, 'placeholder': 'Optional internal notes...'}),
        }

    def __init__(self, *args, **kwargs):
        workspace = kwargs.pop('workspace', None)
        self.workspace = workspace
        super().__init__(*args, **kwargs)
        self.fields['job_ticket'].empty_label = '--- None (General / Non-Job Overhead) ---'
        self.fields['job_ticket'].required = False
        self.fields['payment_mode'].required = False

        qs = JobTicket.objects.order_by('-created_at')
        if workspace:
            qs = qs.filter(workspace=workspace)
        self.fields['job_ticket'].queryset = qs.only('id', 'job_code', 'customer_name', 'device_type', 'status')

        def job_label(obj):
            device = f" • {obj.device_type}" if obj.device_type else ""
            customer = f" • {obj.customer_name}" if obj.customer_name else ""
            return f"{obj.job_code}{customer}{device}"

        self.fields['job_ticket'].label_from_instance = job_label

        # Financial Account field (primary payment selector)
        self.fields['financial_account'].required = False
        acc_qs = FinancialAccount.objects.filter(is_active=True).order_by('-is_default_cash', '-is_default_bank', 'name')
        if workspace:
            acc_qs = acc_qs.filter(workspace=workspace)
        self.fields['financial_account'].queryset = acc_qs

        default_acc = acc_qs.filter(is_default_cash=True).first() or acc_qs.first()
        if default_acc and not self.initial.get('financial_account'):
            self.initial['financial_account'] = default_acc.id

        self.fields['financial_account'].empty_label = None if default_acc else '--- Select Account ---'

        def acc_label(obj):
            type_icon = "💵 " if obj.account_type == "cash" else ("🏦 " if obj.account_type == "bank" else "📱 ")
            type_label = dict(FinancialAccount.ACCOUNT_TYPE_CHOICES).get(obj.account_type, obj.account_type)
            default_tag = " ★ [Default Cash]" if obj.is_default_cash else (" ★ [Default Bank]" if obj.is_default_bank else "")
            return f"{type_icon}{obj.name} ({type_label}){default_tag} — Bal: ₹{obj.current_balance:,.2f}"

        self.fields['financial_account'].label_from_instance = acc_label

    def clean_amount(self):
        amount = self.cleaned_data.get('amount')
        if amount is not None and amount <= Decimal('0.00'):
            raise forms.ValidationError("Expense amount must be greater than zero.")
        return amount

    def clean(self):
        cleaned_data = super().clean()
        acc = cleaned_data.get('financial_account')
        posted_mode = self.data.get('payment_mode') or cleaned_data.get('payment_mode')

        if acc:
            if acc.account_type == FinancialAccount.ACCOUNT_TYPE_CASH:
                cleaned_data['payment_mode'] = Expense.PAYMENT_MODE_CASH
            elif acc.account_type == FinancialAccount.ACCOUNT_TYPE_UPI:
                cleaned_data['payment_mode'] = Expense.PAYMENT_MODE_UPI
            elif acc.account_type == FinancialAccount.ACCOUNT_TYPE_BANK:
                if posted_mode in [Expense.PAYMENT_MODE_BANK, Expense.PAYMENT_MODE_CARD]:
                    cleaned_data['payment_mode'] = posted_mode
                else:
                    cleaned_data['payment_mode'] = Expense.PAYMENT_MODE_BANK
        elif posted_mode:
            auto_acc = FinancialAccount.get_default_for_payment_method(self.workspace, posted_mode)
            if auto_acc:
                cleaned_data['financial_account'] = auto_acc
            cleaned_data['payment_mode'] = posted_mode
        else:
            default_acc = FinancialAccount.get_default_for_payment_method(self.workspace, 'cash')
            cleaned_data['financial_account'] = default_acc
            cleaned_data['payment_mode'] = Expense.PAYMENT_MODE_CASH

        return cleaned_data

    def save(self, commit=True):
        instance = super().save(commit=False)
        if instance.financial_account:
            if instance.financial_account.account_type == FinancialAccount.ACCOUNT_TYPE_CASH:
                instance.payment_mode = Expense.PAYMENT_MODE_CASH
            elif instance.financial_account.account_type == FinancialAccount.ACCOUNT_TYPE_UPI:
                instance.payment_mode = Expense.PAYMENT_MODE_UPI
            elif instance.financial_account.account_type == FinancialAccount.ACCOUNT_TYPE_BANK:
                posted_mode = self.data.get('payment_mode')
                if posted_mode in [Expense.PAYMENT_MODE_BANK, Expense.PAYMENT_MODE_CARD]:
                    instance.payment_mode = posted_mode
                else:
                    instance.payment_mode = Expense.PAYMENT_MODE_BANK
        elif self.cleaned_data.get('payment_mode'):
            instance.payment_mode = self.cleaned_data['payment_mode']
        if commit:
            instance.save()
        return instance


class FinancialAccountForm(forms.ModelForm):
    opening_balance = forms.DecimalField(
        required=False,
        max_digits=12,
        decimal_places=2,
        widget=forms.NumberInput(attrs={'class': 'form-control', 'step': '0.01', 'min': '0.00', 'value': '0.00'}),
    )

    class Meta:
        model = FinancialAccount
        fields = [
            'name',
            'account_type',
            'bank_name',
            'account_number',
            'ifsc_code',
            'branch',
            'upi_id',
            'opening_balance',
            'is_default_cash',
            'is_default_bank',
            'notes',
        ]
        widgets = {
            'name': forms.TextInput(attrs={'class': 'form-control', 'placeholder': 'e.g. Cash in Hand, Federal Bank - Pattambi', 'required': 'required'}),
            'account_type': forms.Select(attrs={'class': 'form-select'}),
            'bank_name': forms.TextInput(attrs={'class': 'form-control', 'placeholder': 'e.g. Federal Bank'}),
            'account_number': forms.TextInput(attrs={'class': 'form-control', 'placeholder': 'e.g. 99980108152444'}),
            'ifsc_code': forms.TextInput(attrs={'class': 'form-control', 'placeholder': 'e.g. FDRL0001412'}),
            'branch': forms.TextInput(attrs={'class': 'form-control', 'placeholder': 'e.g. Pattambi'}),
            'upi_id': forms.TextInput(attrs={'class': 'form-control', 'placeholder': 'e.g. anasrawuthar@okaxis'}),
            'is_default_cash': forms.CheckboxInput(attrs={'class': 'form-check-input'}),
            'is_default_bank': forms.CheckboxInput(attrs={'class': 'form-check-input'}),
            'notes': forms.Textarea(attrs={'class': 'form-control', 'rows': 2, 'placeholder': 'Optional internal notes...'}),
        }

    def clean_opening_balance(self):
        bal = self.cleaned_data.get('opening_balance')
        if bal is not None and bal < Decimal('0.00'):
            raise forms.ValidationError("Opening balance cannot be negative.")
        if bal is None and self.instance and self.instance.pk:
            return self.instance.opening_balance
        return bal or Decimal('0.00')


class AccountTransferForm(forms.Form):
    from_account = forms.ModelChoiceField(
        queryset=FinancialAccount.objects.none(),
        widget=forms.Select(attrs={'class': 'form-select', 'required': 'required'}),
        label="From Account (Pay Out)",
    )
    to_account = forms.ModelChoiceField(
        queryset=FinancialAccount.objects.none(),
        widget=forms.Select(attrs={'class': 'form-select', 'required': 'required'}),
        label="To Account (Deposit In)",
    )
    amount = forms.DecimalField(
        max_digits=12,
        decimal_places=2,
        min_value=Decimal('0.01'),
        widget=forms.NumberInput(attrs={'class': 'form-control', 'step': '0.01', 'min': '0.01', 'placeholder': '0.00', 'required': 'required'}),
        label="Amount (₹)",
    )
    transaction_date = forms.DateField(
        initial=timezone.localdate,
        widget=forms.DateInput(attrs={'type': 'date', 'class': 'form-control', 'required': 'required'}),
        label="Transfer Date",
    )
    reference_no = forms.CharField(
        max_length=100,
        required=False,
        widget=forms.TextInput(attrs={'class': 'form-control', 'placeholder': 'e.g. CDM Slip, Cheque No, UPI Ref'}),
        label="Reference / Transaction ID",
    )
    notes = forms.CharField(
        max_length=255,
        required=False,
        widget=forms.TextInput(attrs={'class': 'form-control', 'placeholder': 'e.g. Cash drawer deposit to bank'}),
        label="Purpose / Notes",
    )

    def __init__(self, *args, **kwargs):
        workspace = kwargs.pop('workspace', None)
        super().__init__(*args, **kwargs)
        qs = FinancialAccount.objects.filter(is_active=True).order_by('-is_default_cash', '-is_default_bank', 'name')
        if workspace:
            qs = qs.filter(workspace=workspace)
        self.fields['from_account'].queryset = qs
        self.fields['to_account'].queryset = qs

        def format_label(acc):
            type_label = dict(FinancialAccount.ACCOUNT_TYPE_CHOICES).get(acc.account_type, acc.account_type)
            return f"{acc.name} ({type_label}) — Bal: ₹{acc.current_balance:,.2f}"

        self.fields['from_account'].label_from_instance = format_label
        self.fields['to_account'].label_from_instance = format_label

    def clean(self):
        cleaned_data = super().clean()
        from_acc = cleaned_data.get('from_account')
        to_acc = cleaned_data.get('to_account')

        if from_acc and to_acc and from_acc.pk == to_acc.pk:
            raise forms.ValidationError("From Account and To Account cannot be the same.")
        return cleaned_data


class VendorForm(forms.ModelForm):
    class Meta:
        model = Vendor
        fields = ['company_name', 'name', 'phone', 'email', 'specialties']
        widgets = {
            'phone': forms.TextInput(attrs={'class': 'form-control phone-input', 'placeholder': '98765 43210', 'inputmode': 'numeric', 'maxlength': '10', 'autocomplete': 'off'}),
            'specialties': forms.Textarea(attrs={'rows': 3}),
        }

    def clean_phone(self):
        phone, error = normalize_indian_phone(
            self.cleaned_data.get('phone'),
            required=False,
            field_label='Phone number',
        )
        if error:
            raise forms.ValidationError(error)
        return phone


class ClientForm(forms.ModelForm):
    class Meta:
        model = Client
        fields = ['name', 'phone', 'address', 'notes']
        widgets = {
            'name': forms.TextInput(attrs={'class': 'form-control'}),
            'phone': forms.TextInput(attrs={'class': 'form-control', 'placeholder': '98765 43210', 'inputmode': 'numeric'}),
            'address': forms.Textarea(attrs={'class': 'form-control', 'rows': 2}),
            'notes': forms.Textarea(attrs={'class': 'form-control', 'rows': 2}),
        }

    def clean_phone(self):
        phone, error = normalize_indian_phone(
            self.cleaned_data.get('phone'),
            field_label='Phone number',
        )
        if error:
            raise forms.ValidationError(error)
        return phone


class ProductForm(forms.ModelForm):
    PRICE_TAX_MODE_CHOICES = [
        ('without_tax', 'Without Tax'),
        ('with_tax', 'With Tax'),
    ]
    purchase_price_tax_mode = forms.ChoiceField(
        choices=PRICE_TAX_MODE_CHOICES,
        initial='without_tax',
        label='Purchase Price Type',
        widget=forms.Select(attrs={'class': 'form-select'}),
    )
    sales_price_tax_mode = forms.ChoiceField(
        choices=PRICE_TAX_MODE_CHOICES,
        initial='without_tax',
        label='Sales Price Type',
        widget=forms.Select(attrs={'class': 'form-select'}),
    )

    class Meta:
        model = Product
        fields = [
            'name',
            'category',
            'brand',
            'item_type',
            'hsn_sac_code',
            'uqc',
            'tax_category',
            'gst_rate',
            'cess_rate',
            'is_tax_inclusive_default',
            'cost_price',
            'unit_price',
            'stock_quantity',
            'reserved_stock',
            'bin_location',
            'has_serial_tracking',
            'vendor_warranty_months',
            'customer_warranty_months',
            'description',
        ]
        labels = {
            'item_type': 'Item Type',
            'hsn_sac_code': 'HSN / SAC Code',
            'uqc': 'UQC',
            'tax_category': 'Tax Category',
            'gst_rate': 'GST Rate (%)',
            'cess_rate': 'Cess Rate (%)',
            'is_tax_inclusive_default': 'Prices include tax by default',
            'cost_price': 'Purchase Price',
            'unit_price': 'Sales Price',
            'stock_quantity': 'Opening Stock Quantity',
            'reserved_stock': 'Reserved Stock Alert',
            'bin_location': 'Rack / Shelf / Bin Location',
            'has_serial_tracking': 'Track Unique Serial / IMEI',
            'vendor_warranty_months': 'Vendor Warranty (Months)',
            'customer_warranty_months': 'Customer Warranty (Months)',
        }
        widgets = {
            'name': forms.TextInput(attrs={'class': 'form-control', 'autocomplete': 'off'}),
            'category': forms.TextInput(attrs={'class': 'form-control', 'autocomplete': 'off', 'list': 'product-category-datalist'}),
            'brand': forms.TextInput(attrs={'class': 'form-control', 'autocomplete': 'off'}),
            'item_type': forms.Select(attrs={'class': 'form-select'}),
            'hsn_sac_code': forms.TextInput(attrs={'class': 'form-control', 'placeholder': '8471 or 9987', 'autocomplete': 'off'}),
            'uqc': forms.TextInput(attrs={'class': 'form-control', 'placeholder': 'NOS, KGS, BOX', 'autocomplete': 'off'}),
            'tax_category': forms.Select(attrs={'class': 'form-select'}),
            'gst_rate': forms.NumberInput(attrs={'class': 'form-control', 'step': '0.01', 'min': '0', 'autocomplete': 'off'}),
            'cess_rate': forms.NumberInput(attrs={'class': 'form-control', 'step': '0.01', 'min': '0', 'autocomplete': 'off'}),
            'is_tax_inclusive_default': forms.CheckboxInput(attrs={'class': 'form-check-input'}),
            'unit_price': forms.NumberInput(attrs={'class': 'form-control', 'step': '0.01', 'autocomplete': 'off'}),
            'cost_price': forms.NumberInput(attrs={'class': 'form-control', 'step': '0.01', 'autocomplete': 'off'}),
            'stock_quantity': forms.NumberInput(attrs={'class': 'form-control', 'min': '0', 'step': '1', 'autocomplete': 'off'}),
            'reserved_stock': forms.NumberInput(attrs={'class': 'form-control', 'min': '0', 'step': '1', 'autocomplete': 'off'}),
            'bin_location': forms.TextInput(attrs={'class': 'form-control', 'placeholder': 'e.g. Rack A - Shelf 2', 'autocomplete': 'off'}),
            'has_serial_tracking': forms.CheckboxInput(attrs={'class': 'form-check-input'}),
            'vendor_warranty_months': forms.NumberInput(attrs={'class': 'form-control', 'min': '0', 'step': '1', 'autocomplete': 'off'}),
            'customer_warranty_months': forms.NumberInput(attrs={'class': 'form-control', 'min': '0', 'step': '1', 'autocomplete': 'off'}),
            'description': forms.Textarea(attrs={'class': 'form-control', 'rows': 2, 'autocomplete': 'off'}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for field in self.fields.values():
            if not isinstance(field.widget, (forms.HiddenInput, forms.CheckboxInput, forms.RadioSelect)):
                field.widget.attrs.setdefault('autocomplete', 'off')
        for f in [
            'vendor_warranty_months', 'customer_warranty_months',
            'item_type', 'hsn_sac_code', 'uqc', 'tax_category', 'gst_rate',
            'cess_rate', 'is_tax_inclusive_default', 'purchase_price_tax_mode',
            'sales_price_tax_mode', 'stock_quantity', 'reserved_stock',
            'cost_price', 'unit_price',
        ]:
            if f in self.fields:
                self.fields[f].required = False

        if not self.is_bound and not getattr(self.instance, 'pk', None):
            if 'vendor_warranty_months' in self.fields:
                self.fields['vendor_warranty_months'].initial = 0
            if 'customer_warranty_months' in self.fields:
                self.fields['customer_warranty_months'].initial = 0
            if 'gst_rate' in self.fields:
                self.fields['gst_rate'].initial = Decimal('0.00')
            if 'uqc' in self.fields:
                self.fields['uqc'].initial = 'NOS'
            if 'cess_rate' in self.fields:
                self.fields['cess_rate'].initial = Decimal('0.00')
            if 'cost_price' in self.fields:
                self.fields['cost_price'].initial = Decimal('0.00')
            if 'unit_price' in self.fields:
                self.fields['unit_price'].initial = Decimal('0.00')
            if 'stock_quantity' in self.fields:
                self.fields['stock_quantity'].initial = 0
            if 'reserved_stock' in self.fields:
                self.fields['reserved_stock'].initial = 0

    def clean_vendor_warranty_months(self):
        val = self.cleaned_data.get('vendor_warranty_months')
        return val if val is not None else 0

    def clean_customer_warranty_months(self):
        val = self.cleaned_data.get('customer_warranty_months')
        return val if val is not None else 0

    def clean_hsn_sac_code(self):
        return ''

    def clean_uqc(self):
        return normalize_text_code(self.cleaned_data.get('uqc')) or 'NOS'

    def clean_stock_quantity(self):
        qty = self.cleaned_data.get('stock_quantity')
        if qty is None:
            if self.instance and self.instance.pk:
                return self.instance.stock_quantity or 0
            return 0
        return qty

    def clean_cost_price(self):
        cost_price = self.cleaned_data.get('cost_price')
        if cost_price is None:
            if self.instance and self.instance.pk:
                return self.instance.cost_price or Decimal('0.00')
            return Decimal('0.00')
        if cost_price < 0:
            raise forms.ValidationError('Purchase price cannot be negative.')
        return cost_price

    def clean_unit_price(self):
        unit_price = self.cleaned_data.get('unit_price')
        if unit_price is None:
            if self.instance and self.instance.pk:
                return self.instance.unit_price or Decimal('0.00')
            return Decimal('0.00')
        if unit_price < 0:
            raise forms.ValidationError('Sales price cannot be negative.')
        return unit_price

    def clean_reserved_stock(self):
        reserved_stock = self.cleaned_data.get('reserved_stock')
        if reserved_stock is None:
            if self.instance and self.instance.pk:
                return self.instance.reserved_stock or 0
            return 0
        if reserved_stock < 0:
            raise forms.ValidationError('Reserved stock cannot be negative.')
        return reserved_stock

    def clean_gst_rate(self):
        return Decimal('0.00')

    def clean_cess_rate(self):
        return Decimal('0.00')

    def clean(self):
        cleaned_data = super().clean()
        cleaned_data['item_type'] = cleaned_data.get('item_type') or 'goods'
        cleaned_data['hsn_sac_code'] = ''
        cleaned_data['uqc'] = cleaned_data.get('uqc') or 'NOS'
        cleaned_data['tax_category'] = 'taxable'
        cleaned_data['gst_rate'] = Decimal('0.00')
        cleaned_data['cess_rate'] = Decimal('0.00')
        cleaned_data['purchase_price_tax_mode'] = 'without_tax'
        cleaned_data['sales_price_tax_mode'] = 'without_tax'
        cleaned_data['is_tax_inclusive_default'] = False
        return cleaned_data


class InventoryPartyForm(forms.ModelForm):
    class Meta:
        model = InventoryParty
        fields = [
            'name',
            'legal_name',
            'contact_person',
            'phone',
            'pan',
            'email',
            'address',
            'shipping_address',
            'pincode',
            'city',
            'state',
            'country',
            'opening_balance',
            'is_active',
        ]
        widgets = {
            'name': forms.TextInput(attrs={'class': 'form-control', 'autocomplete': 'off'}),
            'legal_name': forms.TextInput(attrs={'class': 'form-control', 'autocomplete': 'off'}),
            'contact_person': forms.TextInput(attrs={'class': 'form-control', 'autocomplete': 'off'}),
            'phone': forms.TextInput(attrs={'class': 'form-control phone-input', 'placeholder': '98765 43210', 'inputmode': 'numeric', 'maxlength': '10', 'autocomplete': 'off'}),
            'pan': forms.TextInput(attrs={'class': 'form-control', 'placeholder': 'AAAAA0000A', 'autocomplete': 'off'}),
            'email': forms.EmailInput(attrs={'class': 'form-control', 'autocomplete': 'off'}),
            'address': forms.Textarea(attrs={'class': 'form-control', 'rows': 2, 'autocomplete': 'off'}),
            'shipping_address': forms.Textarea(attrs={'class': 'form-control', 'rows': 2, 'autocomplete': 'off'}),
            'pincode': forms.TextInput(attrs={'class': 'form-control', 'placeholder': 'e.g. 682001', 'maxlength': '6', 'autocomplete': 'off'}),
            'city': forms.TextInput(attrs={'class': 'form-control', 'autocomplete': 'off'}),
            'state': forms.TextInput(attrs={'class': 'form-control', 'autocomplete': 'off'}),
            'country': forms.TextInput(attrs={'class': 'form-control', 'autocomplete': 'off'}),
            'opening_balance': forms.NumberInput(attrs={'class': 'form-control', 'step': '0.01', 'autocomplete': 'off'}),
            'is_active': forms.CheckboxInput(attrs={'class': 'form-check-input'}),
        }
        labels = {
            'name': 'Party Name',
            'legal_name': 'Legal Name',
            'contact_person': 'Contact Person',
            'address': 'Billing Address',
            'shipping_address': 'Shipping Address',
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for field in self.fields.values():
            if not isinstance(field.widget, (forms.HiddenInput, forms.CheckboxInput, forms.RadioSelect)):
                field.widget.attrs.setdefault('autocomplete', 'off')
        for f in ['legal_name']:
            if f in self.fields:
                self.fields[f].required = False
        if not self.is_bound and not getattr(self.instance, 'pk', None):
            self.fields['country'].initial = 'India'
            self.fields['opening_balance'].initial = Decimal('0.00')
            self.fields['is_active'].initial = True
        elif getattr(self.instance, 'pk', None) and self.instance.opening_balance is not None:
            self.fields['opening_balance'].initial = abs(self.instance.opening_balance)

    def clean_phone(self):
        phone, error = normalize_indian_phone(
            self.cleaned_data.get('phone'),
            required=False,
            field_label='Phone number',
        )
        if error:
            raise forms.ValidationError(error)
        return phone

    def clean_pan(self):
        return normalize_compact_code(self.cleaned_data.get('pan'))

    def clean_country(self):
        return (self.cleaned_data.get('country') or '').strip() or 'India'

    def clean(self):
        cleaned_data = super().clean()
        cleaned_data['gst_registration_type'] = 'unregistered'
        cleaned_data['gstin'] = ''
        cleaned_data['state_code'] = ''
        cleaned_data['default_place_of_supply_state'] = ''
        if not (cleaned_data.get('shipping_address') or '').strip():
            cleaned_data['shipping_address'] = (cleaned_data.get('address') or '').strip()

        raw_balance = abs(cleaned_data.get('opening_balance') or Decimal('0.00'))
        bal_type_key = self.add_prefix('opening_balance_type')
        bal_type = (self.data.get(bal_type_key) or 'to_receive').strip()
        if bal_type == 'to_pay':
            cleaned_data['opening_balance'] = -raw_balance
        else:
            cleaned_data['opening_balance'] = raw_balance

        return cleaned_data

    def save(self, commit=True):
        party = super().save(commit=False)
        # Keep a single shared party master for both purchase and sales flows.
        party.party_type = 'both'
        party.gst_registration_type = 'unregistered'
        party.gstin = ''
        party.state_code = ''
        party.default_place_of_supply_state = ''
        if 'opening_balance' in self.cleaned_data:
            party.opening_balance = self.cleaned_data['opening_balance']
        if not (party.shipping_address or '').strip():
            party.shipping_address = (party.address or '').strip()
        if commit:
            party.save()
            self.save_m2m()
        return party


class InventoryEntryForm(forms.ModelForm):
    invoice_date = forms.DateField(
        required=False,
        label='Invoice Date',
        widget=forms.DateInput(attrs={'class': 'form-control', 'type': 'date', 'autocomplete': 'off'}),
    )

    class Meta:
        model = InventoryEntry
        fields = ['entry_date', 'invoice_date', 'invoice_number', 'party']
        widgets = {
            'entry_date': forms.DateInput(attrs={'class': 'form-control', 'type': 'date', 'autocomplete': 'off'}),
            'invoice_number': forms.TextInput(attrs={'class': 'form-control', 'placeholder': 'Leave blank for auto invoice number', 'autocomplete': 'off'}),
            'party': forms.Select(attrs={'class': 'form-select'}),
        }

    def __init__(self, *args, entry_type='purchase', workspace=None, **kwargs):
        super().__init__(*args, **kwargs)
        for field in self.fields.values():
            if not isinstance(field.widget, (forms.HiddenInput, forms.CheckboxInput, forms.RadioSelect)):
                field.widget.attrs.setdefault('autocomplete', 'off')
        self.entry_type = entry_type

        self.fields['party'].label = "Party"
        party_qs = InventoryParty.objects.filter(is_active=True)
        if workspace:
            party_qs = party_qs.filter(workspace=workspace)
        self.fields['party'].queryset = party_qs.order_by('name')
        self.fields['party'].label_from_instance = (
            lambda party: f"{party.name} ({party.phone})" if (party.phone or '').strip() else party.name
        )
        self.fields['entry_date'].initial = timezone.localdate
        self.fields['entry_date'].label = 'Date'
        self.fields['invoice_date'].initial = timezone.localdate
        if entry_type == 'sale':
            self.fields['invoice_date'].required = False
            self.fields['invoice_number'].required = False
            self.fields['invoice_number'].widget.attrs.update({
                'placeholder': 'Auto generated invoice number',
                'readonly': 'readonly',
            })
        elif entry_type == 'purchase':
            self.fields['entry_date'].required = False
            self.fields['invoice_date'].required = False
            self.fields['invoice_number'].required = True
            self.fields['invoice_number'].widget.attrs['placeholder'] = 'Party invoice number'
        else:
            self.fields['invoice_date'].required = False
            self.fields['invoice_number'].required = True
            self.fields['invoice_number'].widget.attrs['placeholder'] = 'Invoice number'

class FeedbackForm(forms.Form):
    rating = forms.ChoiceField(
        choices=[(i, i) for i in range(1, 11)],
        widget=forms.RadioSelect,
        label="Rate your experience (1-10)"
    )
    comment = forms.CharField(
        widget=forms.Textarea(attrs={'rows': 4, 'placeholder': 'Share your experience with us...'}),
        required=False,
        label="Additional comments"
    )

class CompanyProfileForm(forms.ModelForm):
    class Meta:
        model = CompanyProfile
        fields = [
            'company_name', 'legal_name', 'tagline', 'logo', 'logo_url',
            'address', 'city', 'state', 'pincode',
            'phone1', 'phone2', 'email', 'website',
            'gstin', 'pan', 'state_code', 'registration_type', 'filing_frequency', 'qrmp_enabled',
            'lut_bond_enabled', 'annual_turnover_band', 'e_invoice_applicable',
            'default_place_of_supply_state',
            'bank_name', 'account_number', 'ifsc_code', 'branch', 'upi_id',
            'job_code_prefix', 'job_ticket_print_paper_size', 'bill_print_paper_size',
            'include_workshop_device_tag', 'include_ticket_signatures', 'include_bill_signatures',
            'include_bill_payment_details',
            'technician_display_format',
            'enable_gst', 'gst_rate',
            'terms_conditions'
        ]
        widgets = {
            'company_name': forms.TextInput(attrs={'class': 'form-control'}),
            'legal_name': forms.TextInput(attrs={'class': 'form-control'}),
            'tagline': forms.TextInput(attrs={'class': 'form-control'}),
            'logo_url': forms.URLInput(attrs={'class': 'form-control', 'placeholder': 'https://example.com/logo.png'}),
            'address': forms.Textarea(attrs={'class': 'form-control', 'rows': 2}),
            'city': forms.TextInput(attrs={'class': 'form-control'}),
            'state': forms.TextInput(attrs={'class': 'form-control'}),
            'pincode': forms.TextInput(attrs={'class': 'form-control'}),
            'phone1': forms.TextInput(attrs={'class': 'form-control'}),
            'phone2': forms.TextInput(attrs={'class': 'form-control'}),
            'email': forms.EmailInput(attrs={'class': 'form-control'}),
            'website': forms.URLInput(attrs={'class': 'form-control'}),
            'gstin': forms.TextInput(attrs={'class': 'form-control', 'placeholder': '22AAAAA0000A1Z5'}),
            'pan': forms.TextInput(attrs={'class': 'form-control', 'placeholder': 'AAAAA0000A'}),
            'state_code': forms.TextInput(attrs={'class': 'form-control', 'placeholder': '32'}),
            'registration_type': forms.Select(attrs={'class': 'form-select'}),
            'filing_frequency': forms.Select(attrs={'class': 'form-select'}),
            'qrmp_enabled': forms.CheckboxInput(attrs={'class': 'form-check-input'}),
            'lut_bond_enabled': forms.CheckboxInput(attrs={'class': 'form-check-input'}),
            'annual_turnover_band': forms.Select(attrs={'class': 'form-select'}),
            'e_invoice_applicable': forms.CheckboxInput(attrs={'class': 'form-check-input'}),
            'default_place_of_supply_state': forms.TextInput(attrs={'class': 'form-control', 'placeholder': '32'}),
            'bank_name': forms.TextInput(attrs={'class': 'form-control'}),
            'account_number': forms.TextInput(attrs={'class': 'form-control'}),
            'ifsc_code': forms.TextInput(attrs={'class': 'form-control', 'placeholder': 'SBIN0001234'}),
            'branch': forms.TextInput(attrs={'class': 'form-control'}),
            'upi_id': forms.TextInput(attrs={'class': 'form-control', 'placeholder': 'yourname@paytm'}),
            'job_code_prefix': forms.TextInput(attrs={'class': 'form-control', 'placeholder': 'GI'}),
            'job_ticket_print_paper_size': forms.Select(attrs={'class': 'form-select'}),
            'bill_print_paper_size': forms.Select(attrs={'class': 'form-select'}),
            'include_workshop_device_tag': forms.CheckboxInput(attrs={'class': 'form-check-input'}),
            'include_ticket_signatures': forms.CheckboxInput(attrs={'class': 'form-check-input'}),
            'include_bill_signatures': forms.CheckboxInput(attrs={'class': 'form-check-input'}),
            'include_bill_payment_details': forms.CheckboxInput(attrs={'class': 'form-check-input'}),
            'technician_display_format': forms.Select(attrs={'class': 'form-select'}),
            'gst_rate': forms.NumberInput(attrs={'class': 'form-control', 'step': '0.01'}),
            'terms_conditions': forms.Textarea(attrs={'class': 'form-control', 'rows': 4}),
        }
        labels = {
            'legal_name': 'Legal Name',
            'state_code': 'State Code',
            'default_place_of_supply_state': 'Default POS State',
            'qrmp_enabled': 'Enable QRMP',
            'lut_bond_enabled': 'LUT / Bond Enabled',
            'e_invoice_applicable': 'E-Invoice Applicable',
            'job_ticket_print_paper_size': 'Job Ticket Print Paper Size',
            'bill_print_paper_size': 'Bill Print Paper Size',
            'include_workshop_device_tag': 'Include Tear-Off Workshop Device Tag',
            'include_ticket_signatures': 'Include Signatures on Intake Slip',
            'include_bill_signatures': 'Include Signatures on Bill & Invoice',
            'include_bill_payment_details': 'Include Payment Settlement & Bank Details on Bills',
            'technician_display_format': 'Technician Display on Bill',
        }

    def clean_gstin(self):
        return normalize_compact_code(self.cleaned_data.get('gstin'))

    def clean_pan(self):
        return normalize_compact_code(self.cleaned_data.get('pan'))

    def clean_state_code(self):
        return normalize_compact_code(self.cleaned_data.get('state_code'))

    def clean_default_place_of_supply_state(self):
        return normalize_compact_code(self.cleaned_data.get('default_place_of_supply_state'))

    def clean_gst_rate(self):
        gst_rate = self.cleaned_data.get('gst_rate')
        if gst_rate is not None and gst_rate < 0:
            raise forms.ValidationError('GST rate cannot be negative.')
        return gst_rate

    def clean(self):
        cleaned_data = super().clean()
        gstin = cleaned_data.get('gstin') or ''
        state_code = cleaned_data.get('state_code') or ''
        default_pos = cleaned_data.get('default_place_of_supply_state') or ''
        registration_type = cleaned_data.get('registration_type') or 'regular'
        cleaned_data['enable_gst'] = False
        cleaned_data['gst_rate'] = Decimal('0.00')
        cleaned_data['qrmp_enabled'] = False
        cleaned_data['lut_bond_enabled'] = False
        cleaned_data['e_invoice_applicable'] = False

        if gstin and not state_code:
            cleaned_data['state_code'] = gstin[:2]
            state_code = cleaned_data['state_code']

        if gstin and state_code and gstin[:2] != state_code:
            self.add_error('state_code', 'State code must match the first two digits of the GSTIN.')

        if not default_pos and state_code:
            cleaned_data['default_place_of_supply_state'] = state_code

        return cleaned_data


class WhatsAppIntegrationSettingsForm(forms.ModelForm):
    class Meta:
        model = WhatsAppIntegrationSettings
        fields = [
            'is_enabled',
            'delivery_method',
            'bridge_base_url',
            'api_version',
            'phone_number_id',
            'access_token',
            'webhook_verify_token',
            'app_secret',
            'public_site_url',
            'default_country_code',
            'template_language_code',
            'test_template_name',
            'notify_on_created',
            'notify_on_completed',
            'notify_on_delivered',
            'notify_on_feedback',
            'notify_daily_report',
            'daily_report_phone',
            'daily_report_time',
            'created_template_name',
            'created_template',
            'completed_template_name',
            'completed_template',
            'delivered_template_name',
            'delivered_template',
            'estimate_template_name',
            'estimate_template',
            'feedback_template_name',
            'feedback_template',
            'daily_report_template',
        ]
        widgets = {
            'is_enabled': forms.CheckboxInput(attrs={'class': 'form-check-input'}),
            'delivery_method': forms.Select(attrs={'class': 'form-select'}),
            'bridge_base_url': forms.URLInput(attrs={'class': 'form-control', 'placeholder': 'http://127.0.0.1:3001'}),
            'api_version': forms.TextInput(attrs={'class': 'form-control', 'placeholder': 'v23.0'}),
            'phone_number_id': forms.TextInput(attrs={'class': 'form-control', 'placeholder': '123456789012345'}),
            'access_token': forms.Textarea(attrs={'class': 'form-control', 'rows': 3, 'placeholder': 'EAAG...'}),
            'webhook_verify_token': forms.TextInput(attrs={'class': 'form-control', 'placeholder': 'choose-a-random-secret'}),
            'app_secret': forms.TextInput(attrs={'class': 'form-control', 'placeholder': 'Meta app secret (optional)'}),
            'public_site_url': forms.URLInput(attrs={'class': 'form-control', 'placeholder': 'http://127.0.0.1:8000'}),
            'default_country_code': forms.TextInput(attrs={'class': 'form-control', 'placeholder': '91'}),
            'template_language_code': forms.TextInput(attrs={'class': 'form-control', 'placeholder': 'en_US'}),
            'test_template_name': forms.TextInput(attrs={'class': 'form-control', 'placeholder': 'hello_world'}),
            'notify_on_created': forms.CheckboxInput(attrs={'class': 'form-check-input'}),
            'notify_on_completed': forms.CheckboxInput(attrs={'class': 'form-check-input'}),
            'notify_on_delivered': forms.CheckboxInput(attrs={'class': 'form-check-input'}),
            'notify_on_feedback': forms.CheckboxInput(attrs={'class': 'form-check-input'}),
            'notify_daily_report': forms.CheckboxInput(attrs={'class': 'form-check-input'}),
            'daily_report_phone': forms.TextInput(attrs={'class': 'form-control', 'placeholder': 'e.g. 9876543210 (comma-separated for multiple)'}),
            'daily_report_time': forms.TimeInput(attrs={'type': 'time', 'class': 'form-control'}),
            'created_template_name': forms.TextInput(attrs={'class': 'form-control', 'placeholder': 'job_created_update'}),
            'created_template': forms.Textarea(attrs={'class': 'form-control', 'rows': 4}),
            'completed_template_name': forms.TextInput(attrs={'class': 'form-control', 'placeholder': 'job_completed_update'}),
            'completed_template': forms.Textarea(attrs={'class': 'form-control', 'rows': 4}),
            'delivered_template_name': forms.TextInput(attrs={'class': 'form-control', 'placeholder': 'job_closed_update'}),
            'delivered_template': forms.Textarea(attrs={'class': 'form-control', 'rows': 4}),
            'estimate_template_name': forms.TextInput(attrs={'class': 'form-control', 'placeholder': 'job_estimate_update'}),
            'estimate_template': forms.Textarea(attrs={'class': 'form-control', 'rows': 4}),
            'feedback_template_name': forms.TextInput(attrs={'class': 'form-control', 'placeholder': 'job_feedback_followup'}),
            'feedback_template': forms.Textarea(attrs={'class': 'form-control', 'rows': 4}),
            'daily_report_template': forms.Textarea(attrs={'class': 'form-control', 'rows': 6}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for field_name in ('api_version', 'template_language_code', 'daily_report_phone', 'daily_report_time', 'daily_report_template'):
            if field_name in self.fields:
                self.fields[field_name].required = False

    def clean(self):
        cleaned_data = super().clean()

        text_fields = [
            'delivery_method',
            'bridge_base_url',
            'api_version',
            'phone_number_id',
            'access_token',
            'webhook_verify_token',
            'app_secret',
            'public_site_url',
            'default_country_code',
            'template_language_code',
            'test_template_name',
            'created_template_name',
            'completed_template_name',
            'delivered_template_name',
            'estimate_template_name',
            'feedback_template_name',
        ]
        for field_name in text_fields:
            value = cleaned_data.get(field_name)
            if isinstance(value, str):
                cleaned_data[field_name] = value.strip()

        if not cleaned_data.get('is_enabled'):
            return cleaned_data

        delivery_method = cleaned_data.get('delivery_method') or WhatsAppIntegrationSettings.DELIVERY_CLOUD_API

        required_fields = {
            'public_site_url': 'Public Site URL is required so status and receipt links work correctly.',
        }
        if delivery_method == WhatsAppIntegrationSettings.DELIVERY_BRIDGE:
            required_fields['bridge_base_url'] = 'Bridge Base URL is required when WhatsApp Bridge delivery is selected.'
        else:
            required_fields.update(
                {
                    'api_version': 'API version is required when WhatsApp Cloud API delivery is selected.',
                    'phone_number_id': 'Phone Number ID is required when WhatsApp Cloud API delivery is selected.',
                    'access_token': 'Access token is required when WhatsApp Cloud API delivery is selected.',
                    'template_language_code': 'Template language code is required when WhatsApp Cloud API delivery is selected.',
                }
            )

        for field_name, error_message in required_fields.items():
            if not cleaned_data.get(field_name):
                self.add_error(field_name, error_message)

        if delivery_method == WhatsAppIntegrationSettings.DELIVERY_CLOUD_API:
            if cleaned_data.get('notify_on_completed') and not cleaned_data.get('completed_template_name'):
                self.add_error('completed_template_name', 'Approved template name is required for completed notifications.')
            if cleaned_data.get('notify_on_delivered') and not cleaned_data.get('delivered_template_name'):
                self.add_error('delivered_template_name', 'Approved template name is required for delivered notifications.')
            if not cleaned_data.get('estimate_template_name'):
                self.add_error('estimate_template_name', 'Approved template name is required for estimate messages.')
            if cleaned_data.get('notify_on_feedback') and not cleaned_data.get('feedback_template_name'):
                self.add_error('feedback_template_name', 'Approved template name is required for feedback follow-up messages.')

        return cleaned_data
