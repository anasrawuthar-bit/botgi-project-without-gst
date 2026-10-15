import datetime
from decimal import Decimal

from django.db import models, transaction
from django.contrib.auth.models import User
from django.utils import timezone
from django.utils.text import slugify
from django.urls import reverse

from .gst_utils import (
    validate_gstin,
    validate_hsn_sac_code,
    validate_pan,
    validate_state_code,
)

# A user profile to link to the technician
class CompanyWorkspace(models.Model):
    STATUS_TRIAL = 'trial'
    STATUS_ACTIVE = 'active'
    STATUS_SUSPENDED = 'suspended'
    STATUS_CHOICES = [
        (STATUS_TRIAL, 'Trial'),
        (STATUS_ACTIVE, 'Active'),
        (STATUS_SUSPENDED, 'Suspended'),
    ]

    name = models.CharField(max_length=200)
    slug = models.SlugField(max_length=220, unique=True, blank=True)
    owner = models.ForeignKey(User, on_delete=models.PROTECT, related_name='owned_workspaces')
    contact_email = models.EmailField(blank=True)
    contact_phone = models.CharField(max_length=20, blank=True)
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default=STATUS_TRIAL, db_index=True)
    plan_name = models.CharField(max_length=80, default='Starter')
    timezone_name = models.CharField(max_length=80, default='Asia/Kolkata')
    notes = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['name']

    def __str__(self):
        return self.name

    def save(self, *args, **kwargs):
        if not self.slug:
            base_slug = slugify(self.name) or 'workspace'
            slug = base_slug
            counter = 2
            while CompanyWorkspace.objects.filter(slug=slug).exclude(pk=self.pk).exists():
                slug = f'{base_slug}-{counter}'
                counter += 1
            self.slug = slug
        super().save(*args, **kwargs)


class CompanyUserMembership(models.Model):
    ROLE_OWNER = 'owner'
    ROLE_ADMIN = 'admin'
    ROLE_STAFF = 'staff'
    ROLE_TECHNICIAN = 'technician'
    ROLE_INVENTORY = 'inventory'
    ROLE_VIEWER = 'viewer'
    ROLE_CHOICES = [
        (ROLE_OWNER, 'Owner'),
        (ROLE_ADMIN, 'Admin'),
        (ROLE_STAFF, 'Staff'),
        (ROLE_TECHNICIAN, 'Technician'),
        (ROLE_INVENTORY, 'Inventory'),
        (ROLE_VIEWER, 'Viewer'),
    ]

    workspace = models.ForeignKey(CompanyWorkspace, on_delete=models.CASCADE, related_name='memberships')
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name='workspace_memberships')
    role = models.CharField(max_length=20, choices=ROLE_CHOICES, default=ROLE_STAFF)
    is_active = models.BooleanField(default=True, db_index=True)
    joined_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        unique_together = ('workspace', 'user')
        ordering = ['workspace__name', 'user__username']

    def __str__(self):
        return f'{self.user.username} - {self.workspace.name} ({self.get_role_display()})'


class TechnicianProfile(models.Model):
    workspace = models.ForeignKey(
        CompanyWorkspace,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='technicians',
        db_index=True,
    )
    user = models.OneToOneField(User, on_delete=models.CASCADE, related_name='technician_profile')
    unique_id = models.CharField(max_length=10, unique=True)

    def __str__(self):
        return self.user.username


class Client(models.Model):
    workspace = models.ForeignKey(
        CompanyWorkspace,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='clients',
        db_index=True,
    )
    name = models.CharField(max_length=200)
    phone = models.CharField(max_length=20)
    email = models.EmailField(blank=True)
    company_name = models.CharField(max_length=200, blank=True)
    address = models.TextField(blank=True)
    notes = models.TextField(blank=True)
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['name']
        constraints = [
            models.UniqueConstraint(fields=['workspace', 'phone'], name='unique_client_phone_per_workspace'),
        ]

    def __str__(self):
        return f"{self.name} ({self.phone})"


class JobFieldPreset(models.Model):
    FIELD_CHOICES = [
        ('device_type', 'Device Type'),
        ('device_brand', 'Device Brand'),
        ('reported_issue', 'Reported Issue'),
        ('additional_items', 'Additional Items'),
    ]

    workspace = models.ForeignKey(
        CompanyWorkspace,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='job_field_presets',
        db_index=True,
    )
    field_name = models.CharField(max_length=32, choices=FIELD_CHOICES, db_index=True)
    value = models.CharField(max_length=255)
    sort_order = models.PositiveIntegerField(default=0)
    is_active = models.BooleanField(default=True, db_index=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['field_name', 'sort_order', 'value']
        constraints = [
            models.UniqueConstraint(fields=['workspace', 'field_name', 'value'], name='unique_job_preset_per_workspace'),
        ]

    def __str__(self):
        return f"{self.get_field_name_display()}: {self.value}"


class DeviceChecklistTemplate(models.Model):
    workspace = models.ForeignKey(
        CompanyWorkspace,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='device_checklist_templates',
        db_index=True,
    )
    device_type = models.CharField(
        max_length=100,
        db_index=True,
        help_text="Device type this checklist applies to (example: Laptop, Desktop, Printer).",
    )
    name = models.CharField(
        max_length=120,
        blank=True,
        help_text="Optional internal name shown in admin.",
    )
    notes = models.TextField(
        blank=True,
        help_text="Optional guidance shown to technicians.",
    )
    is_active = models.BooleanField(default=True, db_index=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['device_type']
        constraints = [
            models.UniqueConstraint(fields=['workspace', 'device_type'], name='unique_checklist_template_per_workspace'),
        ]

    def __str__(self):
        return (self.name or self.device_type).strip()


class DeviceChecklistField(models.Model):
    FIELD_TYPE_CHOICES = [
        ('text', 'Single Line Text'),
        ('textarea', 'Multi Line Text'),
        ('number', 'Number'),
        ('select', 'Dropdown Select'),
        ('checkbox', 'Checkbox'),
    ]

    template = models.ForeignKey(
        DeviceChecklistTemplate,
        on_delete=models.CASCADE,
        related_name='fields',
    )
    field_key = models.SlugField(
        max_length=64,
        help_text="Stable key used to store this answer (example: battery_health).",
    )
    label = models.CharField(max_length=120)
    field_type = models.CharField(max_length=20, choices=FIELD_TYPE_CHOICES, default='text')
    is_required = models.BooleanField(default=True)
    placeholder = models.CharField(max_length=150, blank=True)
    help_text = models.CharField(max_length=255, blank=True)
    options = models.TextField(
        blank=True,
        help_text="For dropdown fields, enter one option per line.",
    )
    sort_order = models.PositiveIntegerField(default=0)
    is_active = models.BooleanField(default=True, db_index=True)

    class Meta:
        ordering = ['sort_order', 'label']
        unique_together = ('template', 'field_key')

    def __str__(self):
        return f"{self.template.device_type}: {self.label}"

    def get_option_list(self):
        return [line.strip() for line in (self.options or '').splitlines() if line.strip()]


PRODUCT_ITEM_TYPE_CHOICES = [
    ('goods', 'Goods'),
    ('service', 'Service'),
]


PRODUCT_TAX_CATEGORY_CHOICES = [
    ('taxable', 'Taxable'),
    ('exempt', 'Exempt'),
    ('nil_rated', 'Nil Rated'),
    ('non_gst', 'Non-GST'),
]


PARTY_GST_REGISTRATION_TYPE_CHOICES = [
    ('registered', 'Registered'),
    ('unregistered', 'Unregistered'),
    ('composition', 'Composition'),
    ('consumer', 'Consumer'),
    ('sez', 'SEZ'),
    ('overseas', 'Overseas'),
]


COMPANY_REGISTRATION_TYPE_CHOICES = [
    ('regular', 'Regular'),
    ('composition', 'Composition'),
    ('unregistered', 'Unregistered'),
]


FILING_FREQUENCY_CHOICES = [
    ('monthly', 'Monthly'),
    ('quarterly', 'Quarterly'),
]


ANNUAL_TURNOVER_BAND_CHOICES = [
    ('up_to_5cr', 'Up to Rs. 5 Cr'),
    ('5_to_10cr', 'Rs. 5 Cr to 10 Cr'),
    ('above_10cr', 'Above Rs. 10 Cr'),
]


class Product(models.Model):
    workspace = models.ForeignKey(
        CompanyWorkspace,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='products',
        db_index=True,
    )
    name = models.CharField(max_length=200)
    sku = models.CharField(max_length=50, null=True, blank=True)
    category = models.CharField(max_length=120, blank=True)
    brand = models.CharField(max_length=120, blank=True)
    item_type = models.CharField(max_length=20, choices=PRODUCT_ITEM_TYPE_CHOICES, default='goods')
    hsn_sac_code = models.CharField(
        max_length=8,
        blank=True,
        validators=[validate_hsn_sac_code],
        help_text="HSN for goods or SAC for services.",
    )
    uqc = models.CharField(
        max_length=20,
        blank=True,
        help_text="Unit Quantity Code used in GST summaries, for example NOS or KGS.",
    )
    unit_price = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    cost_price = models.DecimalField(
        max_digits=10,
        decimal_places=2,
        default=0,
        help_text="Internal purchase/stock cost per unit for profit calculations.",
    )
    gst_rate = models.DecimalField(
        max_digits=5,
        decimal_places=2,
        default=18,
        help_text="Default GST rate for this product or service.",
    )
    cess_rate = models.DecimalField(
        max_digits=5,
        decimal_places=2,
        default=0,
        help_text="Default cess rate, if applicable.",
    )
    tax_category = models.CharField(
        max_length=20,
        choices=PRODUCT_TAX_CATEGORY_CHOICES,
        default='taxable',
    )
    is_tax_inclusive_default = models.BooleanField(
        default=False,
        help_text="Treat entered prices as tax-inclusive by default for this item.",
    )
    stock_quantity = models.IntegerField(default=0)
    reserved_stock = models.PositiveIntegerField(
        default=0,
        help_text="Minimum quantity to keep in stock for essential/reserved items.",
    )
    bin_location = models.CharField(
        max_length=60,
        blank=True,
        default='',
        help_text="Rack / Shelf / Bin storage location in warehouse or shop.",
    )
    has_serial_tracking = models.BooleanField(
        default=False,
        help_text="Enable unique Serial / IMEI tracking for this product.",
    )
    vendor_warranty_months = models.PositiveIntegerField(
        default=0,
        help_text="Warranty period from supplier in months.",
    )
    customer_warranty_months = models.PositiveIntegerField(
        default=0,
        help_text="Warranty period provided to customer in months.",
    )
    description = models.TextField(blank=True)
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['name']
        constraints = [
            models.UniqueConstraint(fields=['workspace', 'sku'], name='unique_product_sku_per_workspace'),
        ]

    def __str__(self):
        return self.name

    @property
    def effective_gst_rate(self):
        if self.tax_category != 'taxable':
            return Decimal('0.00')
        return self.gst_rate or Decimal('0.00')


class ItemCategory(models.Model):
    workspace = models.ForeignKey(
        CompanyWorkspace,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='item_categories',
        db_index=True,
    )
    name = models.CharField(max_length=120)
    description = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['name']
        constraints = [
            models.UniqueConstraint(fields=['workspace', 'name'], name='unique_item_category_per_workspace'),
        ]

    def __str__(self):
        return self.name

    @classmethod
    def get_or_create_category(cls, name, workspace=None):
        clean_name = (name or '').strip()
        if not clean_name:
            return None
        cat = cls.objects.filter(workspace=workspace, name__iexact=clean_name).first()
        if not cat:
            cat = cls.objects.create(workspace=workspace, name=clean_name)
        return cat

    @classmethod
    def sync_from_products(cls, workspace=None):
        existing_cats = Product.objects.filter(workspace=workspace).exclude(category__isnull=True).exclude(category__exact='').values_list('category', flat=True).distinct()
        for c in existing_cats:
            clean_c = c.strip()
            if clean_c:
                cls.get_or_create_category(clean_c, workspace=workspace)

    @classmethod
    def get_categories_with_counts(cls, workspace=None):
        cls.sync_from_products(workspace=workspace)
        categories = list(cls.objects.filter(workspace=workspace).order_by('name'))
        counts = {}
        for p in Product.objects.filter(workspace=workspace).exclude(category__isnull=True).exclude(category__exact='').values('category'):
            c_name = (p.get('category') or '').strip().lower()
            if c_name:
                counts[c_name] = counts.get(c_name, 0) + 1
        for cat in categories:
            cat.product_count = counts.get(cat.name.strip().lower(), 0)
        return categories


class InventoryParty(models.Model):
    PARTY_TYPE_CHOICES = [
        ('supplier', 'Supplier'),
        ('customer', 'Customer'),
        ('both', 'Both'),
    ]

    workspace = models.ForeignKey(
        CompanyWorkspace,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='inventory_parties',
        db_index=True,
    )
    name = models.CharField(max_length=200)
    legal_name = models.CharField(max_length=200, blank=True)
    contact_person = models.CharField(max_length=120, blank=True)
    party_type = models.CharField(max_length=20, choices=PARTY_TYPE_CHOICES, default='both')
    gst_registration_type = models.CharField(
        max_length=20,
        choices=PARTY_GST_REGISTRATION_TYPE_CHOICES,
        default='unregistered',
    )
    phone = models.CharField(max_length=20, blank=True)
    email = models.EmailField(blank=True)
    gstin = models.CharField(
        max_length=15,
        blank=True,
        validators=[validate_gstin],
        help_text="GSTIN for B2B transactions.",
    )
    pan = models.CharField(max_length=10, blank=True, validators=[validate_pan])
    state_code = models.CharField(
        max_length=2,
        blank=True,
        validators=[validate_state_code],
        help_text="2-digit GST state code.",
    )
    default_place_of_supply_state = models.CharField(
        max_length=2,
        blank=True,
        validators=[validate_state_code],
        help_text="Optional default place of supply state code for billing.",
    )
    country = models.CharField(max_length=100, blank=True, default='India')
    address = models.TextField(blank=True)
    shipping_address = models.TextField(blank=True)
    city = models.CharField(max_length=100, blank=True)
    state = models.CharField(max_length=100, blank=True)
    pincode = models.CharField(max_length=10, blank=True)
    opening_balance = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['name']

    def __str__(self):
        return self.name


INVENTORY_ENTRY_TYPE_CHOICES = [
    ('purchase', 'Purchase'),
    ('purchase_return', 'Purchase Return'),
    ('sale', 'Sales'),
    ('sale_return', 'Sales Return'),
]


class InventoryBill(models.Model):
    workspace = models.ForeignKey(
        CompanyWorkspace,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='inventory_bills',
        db_index=True,
    )
    bill_number = models.CharField(max_length=40, unique=True)
    entry_type = models.CharField(max_length=20, choices=INVENTORY_ENTRY_TYPE_CHOICES)
    entry_date = models.DateField(default=timezone.localdate)
    invoice_number = models.CharField(
        max_length=50,
        blank=True,
        null=True,
        help_text="Supplier/customer invoice number for reference.",
    )
    invoice_date = models.DateField(
        null=True,
        blank=True,
        help_text="Original supplier/customer invoice date when different from entry date.",
    )
    job_ticket = models.ForeignKey(
        'JobTicket',
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='inventory_bills',
        help_text="Linked job ticket when this bill is generated from service billing.",
    )
    party = models.ForeignKey(InventoryParty, on_delete=models.PROTECT, related_name='inventory_bills')
    notes = models.TextField(blank=True)
    created_by = models.ForeignKey(
        User,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='inventory_bills',
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    # --- Reversal fields ---
    is_reversed = models.BooleanField(
        default=False,
        db_index=True,
        help_text="True when this bill has been formally reversed. A reversed bill cannot be reversed again.",
    )
    reversal_of = models.ForeignKey(
        'self',
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='reversal_bills',
        help_text="Points to the original bill this bill was created to reverse.",
    )
    reversal_note = models.TextField(
        blank=True,
        help_text="Reason recorded at the time of reversal.",
    )
    reversed_at = models.DateTimeField(
        null=True,
        blank=True,
        help_text="Timestamp when the original bill was marked as reversed.",
    )
    reversed_by = models.ForeignKey(
        User,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='reversed_inventory_bills',
        help_text="User who performed the reversal.",
    )

    # --- Return linkage ---
    source_bill = models.ForeignKey(
        'self',
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='return_bills',
        help_text=(
            "For purchase_return and sale_return bills: the original purchase/sale bill "
            "this return is against. Used to compute vendor credits and customer refunds."
        ),
    )

    class Meta:
        ordering = ['-entry_date', '-id']

    def __str__(self):
        return f"{self.get_entry_type_display()} {self.bill_number}"


class InventoryEntry(models.Model):
    ENTRY_TYPE_CHOICES = INVENTORY_ENTRY_TYPE_CHOICES

    workspace = models.ForeignKey(
        CompanyWorkspace,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='inventory_entries',
        db_index=True,
    )
    entry_number = models.CharField(max_length=40, unique=True)
    entry_type = models.CharField(max_length=20, choices=ENTRY_TYPE_CHOICES)
    entry_date = models.DateField(default=timezone.localdate)
    bill = models.ForeignKey('InventoryBill', on_delete=models.CASCADE, related_name='lines')
    invoice_number = models.CharField(
        max_length=50,
        blank=True,
        null=True,
        help_text="Supplier/customer invoice number for reference.",
    )
    job_ticket = models.ForeignKey(
        'JobTicket',
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='inventory_entries',
        help_text="Linked job ticket when this sale entry is generated from job billing.",
    )

    party = models.ForeignKey(InventoryParty, on_delete=models.PROTECT, related_name='inventory_entries')
    product = models.ForeignKey(Product, on_delete=models.PROTECT, related_name='inventory_entries')

    quantity = models.PositiveIntegerField(default=1)
    unit_price = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    discount_amount = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    gst_rate = models.DecimalField(max_digits=5, decimal_places=2, default=18)
    taxable_amount = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    gst_amount = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    total_amount = models.DecimalField(max_digits=12, decimal_places=2, default=0)

    stock_before = models.IntegerField(default=0)
    stock_after = models.IntegerField(default=0)

    serial_numbers_text = models.TextField(
        blank=True,
        default='',
        help_text="Serial / IMEI numbers for this entry line.",
    )
    notes = models.TextField(blank=True)
    created_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True, related_name='inventory_entries')
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-entry_date', '-id']

    def __str__(self):
        return f"{self.get_entry_type_display()} {self.entry_number}"

    @property
    def stock_effect(self):
        if self.entry_type in {'purchase', 'sale_return'}:
            return self.quantity
        return -self.quantity


class ProductItemSerial(models.Model):
    STATUS_IN_STOCK = 'in_stock'
    STATUS_SOLD = 'sold'
    STATUS_CONSUMED_IN_JOB = 'consumed_in_job'
    STATUS_DEFECTIVE_RMA = 'defective_rma'
    STATUS_WRITTEN_OFF = 'written_off'
    STATUS_CHOICES = [
        (STATUS_IN_STOCK, 'In Stock'),
        (STATUS_SOLD, 'Sold'),
        (STATUS_CONSUMED_IN_JOB, 'Consumed in Job'),
        (STATUS_DEFECTIVE_RMA, 'Defective / RMA Return'),
        (STATUS_WRITTEN_OFF, 'Written Off'),
    ]

    workspace = models.ForeignKey(
        CompanyWorkspace,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='product_serials',
        db_index=True,
    )
    product = models.ForeignKey(Product, on_delete=models.CASCADE, related_name='serial_items')
    serial_number = models.CharField(max_length=100, db_index=True)
    status = models.CharField(max_length=25, choices=STATUS_CHOICES, default=STATUS_IN_STOCK, db_index=True)

    purchase_entry = models.ForeignKey(
        InventoryEntry,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='inward_serials',
    )
    purchase_date = models.DateField(null=True, blank=True)
    purchase_cost = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    vendor_warranty_until = models.DateField(null=True, blank=True)

    sale_entry = models.ForeignKey(
        InventoryEntry,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='outward_serials',
    )
    sale_date = models.DateField(null=True, blank=True)
    sale_price = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    customer_warranty_until = models.DateField(null=True, blank=True)

    job_ticket = models.ForeignKey(
        'JobTicket',
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='consumed_serials',
    )
    notes = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-created_at']
        constraints = [
            models.UniqueConstraint(fields=['workspace', 'product', 'serial_number'], name='unique_product_serial_per_workspace'),
        ]

    def __str__(self):
        return f"{self.product.name} - {self.serial_number} ({self.get_status_display()})"

    @property
    def is_vendor_warranty_active(self):
        if not self.vendor_warranty_until:
            return False
        return self.vendor_warranty_until >= timezone.localdate()

    @property
    def is_customer_warranty_active(self):
        if not self.customer_warranty_until:
            return False
        return self.customer_warranty_until >= timezone.localdate()


class InventoryCreditPayment(models.Model):
    DIRECTION_PAYABLE = 'payable'
    DIRECTION_RECEIVABLE = 'receivable'
    DIRECTION_CHOICES = [
        (DIRECTION_PAYABLE, 'Payable'),
        (DIRECTION_RECEIVABLE, 'Receivable'),
    ]

    METHOD_CASH = 'cash'
    METHOD_UPI = 'upi'
    METHOD_CARD = 'card'
    METHOD_TRANSFER = 'transfer'
    METHOD_CHOICES = [
        (METHOD_CASH, 'Cash'),
        (METHOD_UPI, 'UPI / QR'),
        (METHOD_CARD, 'Card'),
        (METHOD_TRANSFER, 'Transfer'),
    ]

    workspace = models.ForeignKey(
        CompanyWorkspace,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='inventory_credit_payments',
        db_index=True,
    )
    party = models.ForeignKey(InventoryParty, on_delete=models.PROTECT, related_name='credit_payments')
    bill = models.ForeignKey(InventoryBill, on_delete=models.CASCADE, related_name='credit_payments')
    direction = models.CharField(max_length=20, choices=DIRECTION_CHOICES)
    payment_date = models.DateField(default=timezone.localdate)
    payment_method = models.CharField(max_length=20, choices=METHOD_CHOICES, default=METHOD_CASH)
    financial_account = models.ForeignKey(
        'FinancialAccount',
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name='inventory_credit_payments',
        db_index=True,
    )
    amount = models.DecimalField(max_digits=12, decimal_places=2)
    balance_before = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    balance_after = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    reference_no = models.CharField(max_length=100, blank=True)
    notes = models.TextField(blank=True)
    created_by = models.ForeignKey(
        User,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='inventory_credit_payments_created',
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-payment_date', '-created_at']
        indexes = [
            models.Index(fields=['direction', 'payment_date']),
            models.Index(fields=['bill', 'direction']),
        ]

    def __str__(self):
        return f"{self.get_direction_display()} {self.party.name} - {self.amount}"


class InventoryNumberSequence(models.Model):
    """Concurrency-safe counters for human-readable inventory identifiers."""
    sequence_key = models.CharField(max_length=180, unique=True)
    last_counter = models.PositiveIntegerField(default=0)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['sequence_key']

    def __str__(self):
        return f"{self.sequence_key}: {self.last_counter}"


class InventoryBillLog(models.Model):
    """
    Append-only audit log for inventory bill lifecycle events.

    Every accounting action that creates, reverses, or links a bill must write
    a row here.  Do not update or delete rows — append only.
    """
    ACTION_BILL_CREATED = 'BILL_CREATED'
    ACTION_BILL_EDITED = 'BILL_EDITED'
    ACTION_BILL_REVERSED = 'BILL_REVERSED'
    ACTION_REVERSAL_CREATED = 'REVERSAL_CREATED'
    ACTION_PAYMENT_RECORDED = 'PAYMENT_RECORDED'
    ACTION_RETURN_LINKED = 'RETURN_LINKED'
    ACTION_VENDOR_CREDIT_CREATED = 'VENDOR_CREDIT_CREATED'
    ACTION_VENDOR_CREDIT_APPLIED = 'VENDOR_CREDIT_APPLIED'

    ACTION_CHOICES = [
        (ACTION_BILL_CREATED, 'Bill Created'),
        (ACTION_BILL_EDITED, 'Bill Edited'),
        (ACTION_BILL_REVERSED, 'Bill Reversed'),
        (ACTION_REVERSAL_CREATED, 'Reversal Bill Created'),
        (ACTION_PAYMENT_RECORDED, 'Payment Recorded'),
        (ACTION_RETURN_LINKED, 'Return Linked to Source Bill'),
        (ACTION_VENDOR_CREDIT_CREATED, 'Vendor Credit Created'),
        (ACTION_VENDOR_CREDIT_APPLIED, 'Vendor Credit Applied'),
    ]

    workspace = models.ForeignKey(
        CompanyWorkspace,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='inventory_bill_logs',
        db_index=True,
    )
    bill = models.ForeignKey(
        'InventoryBill',
        on_delete=models.CASCADE,
        related_name='audit_logs',
        help_text="The bill this log entry is associated with.",
    )
    action = models.CharField(max_length=40, choices=ACTION_CHOICES, db_index=True)
    user = models.ForeignKey(
        User,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        help_text="User who performed the action.",
    )
    timestamp = models.DateTimeField(auto_now_add=True, db_index=True)
    details = models.TextField(
        blank=True,
        help_text="Human-readable description of the event. Include before/after values for edits.",
    )
    related_bill = models.ForeignKey(
        'InventoryBill',
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='related_audit_logs',
        help_text="For reversals: the reversal bill. For returns: the source bill.",
    )

    class Meta:
        ordering = ['-timestamp']

    def __str__(self):
        return f"{self.bill.bill_number} — {self.action} at {self.timestamp:%Y-%m-%d %H:%M}"


class InventoryVendorCredit(models.Model):
    """
    Represents a credit owed to the company by a vendor, arising from a purchase return.

    A vendor credit is created when a purchase_return bill is linked to an original
    purchase bill (source_bill).  The credit amount equals the return bill's total.
    It can be:
      - Applied to a future purchase bill from the same vendor (reduces amount payable).
      - Carried forward (credit balance remains open).
      - Refunded (creates a DIRECTION_RECEIVABLE InventoryCreditPayment against this record).

    The credit is considered settled when applied_amount >= credit_amount.
    """
    STATUS_OPEN = 'open'
    STATUS_PARTIALLY_APPLIED = 'partially_applied'
    STATUS_FULLY_APPLIED = 'fully_applied'
    STATUS_VOIDED = 'voided'
    STATUS_CHOICES = [
        (STATUS_OPEN, 'Open'),
        (STATUS_PARTIALLY_APPLIED, 'Partially Applied'),
        (STATUS_FULLY_APPLIED, 'Fully Applied'),
        (STATUS_VOIDED, 'Voided'),
    ]

    workspace = models.ForeignKey(
        CompanyWorkspace,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='inventory_vendor_credits',
        db_index=True,
    )
    party = models.ForeignKey(
        InventoryParty,
        on_delete=models.PROTECT,
        related_name='vendor_credits',
        help_text="The vendor (supplier) party this credit belongs to.",
    )
    return_bill = models.OneToOneField(
        'InventoryBill',
        on_delete=models.CASCADE,
        related_name='vendor_credit',
        help_text="The purchase_return bill that generated this vendor credit.",
    )
    source_bill = models.ForeignKey(
        'InventoryBill',
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='vendor_credits_from_returns',
        help_text="The original purchase bill this return is against. Null when the return is not tied to a specific bill.",
    )
    credit_amount = models.DecimalField(
        max_digits=12,
        decimal_places=2,
        help_text="Total credit value (= return bill total at time of creation). Immutable after creation.",
    )
    applied_amount = models.DecimalField(
        max_digits=12,
        decimal_places=2,
        default=0,
        help_text="Total amount of this credit that has been applied to purchase bills or refunded.",
    )
    status = models.CharField(
        max_length=20,
        choices=STATUS_CHOICES,
        default=STATUS_OPEN,
        db_index=True,
    )
    notes = models.TextField(blank=True)
    created_by = models.ForeignKey(
        User,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='vendor_credits_created',
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-created_at']

    def __str__(self):
        return f"Vendor Credit {self.return_bill.bill_number} — {self.party.name} Rs.{self.credit_amount}"

    @property
    def balance_amount(self):
        """Remaining credit not yet applied."""
        return (self.credit_amount or Decimal('0.00')) - (self.applied_amount or Decimal('0.00'))

    def _recompute_status(self):
        """Recompute and save status based on applied_amount. Call inside a transaction."""
        balance = self.balance_amount
        if self.status == self.STATUS_VOIDED:
            return
        if balance <= Decimal('0.00'):
            new_status = self.STATUS_FULLY_APPLIED
        elif (self.applied_amount or Decimal('0.00')) > Decimal('0.00'):
            new_status = self.STATUS_PARTIALLY_APPLIED
        else:
            new_status = self.STATUS_OPEN
        if new_status != self.status:
            self.status = new_status
            self.save(update_fields=['status', 'updated_at'])


class InventoryVendorCreditApplication(models.Model):
    """
    Records a single application of a vendor credit against a purchase bill.

    Each row reduces vendor_credit.applied_amount and reduces the effective
    payable on the target_bill.
    """
    workspace = models.ForeignKey(
        CompanyWorkspace,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='vendor_credit_applications',
        db_index=True,
    )
    vendor_credit = models.ForeignKey(
        InventoryVendorCredit,
        on_delete=models.CASCADE,
        related_name='applications',
    )
    target_bill = models.ForeignKey(
        'InventoryBill',
        on_delete=models.CASCADE,
        related_name='credit_applications',
        help_text="The purchase bill this credit is being applied against.",
    )
    amount = models.DecimalField(
        max_digits=12,
        decimal_places=2,
        help_text="Amount of the credit applied. Cannot exceed credit balance or bill balance.",
    )
    applied_date = models.DateField(default=timezone.localdate)
    notes = models.TextField(blank=True)
    created_by = models.ForeignKey(
        User,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='vendor_credit_applications_created',
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-applied_date', '-created_at']

    def __str__(self):
        return (
            f"Credit {self.vendor_credit.return_bill.bill_number} "
            f"→ Bill {self.target_bill.bill_number} Rs.{self.amount}"
        )


class DeviceRack(models.Model):
    GROUP_SHOP = 'Shop'
    GROUP_GODOWN = 'Godown'
    GROUP_BENCH = 'Bench'
    GROUP_DEFAULT_CHOICES = [
        (GROUP_SHOP, 'Shop'),
        (GROUP_GODOWN, 'Godown'),
        (GROUP_BENCH, 'Bench'),
    ]

    workspace = models.ForeignKey(
        CompanyWorkspace,
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name='device_racks',
        db_index=True,
    )
    name = models.CharField(
        max_length=60,
        help_text="Rack / Shelf identifier (e.g., Rack A1, Shelf 2, Tray 10).",
    )
    group = models.CharField(
        max_length=60,
        default=GROUP_SHOP,
        help_text="Location group (e.g., Shop, Godown, Bench).",
    )
    description = models.CharField(
        max_length=200,
        blank=True,
        default='',
        help_text="Optional description or position note.",
    )
    is_active = models.BooleanField(
        default=True,
        help_text="Inactive racks are hidden from intake pickers.",
    )
    total_columns = models.PositiveIntegerField(
        default=10,
        help_text="Total number of numbered columns/slots in this rack.",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['group', 'name']
        unique_together = ('workspace', 'name', 'group')

    def __str__(self):
        return f"{self.name} ({self.group})"

    @property
    def active_job_tickets(self):
        return self.job_tickets.exclude(status__in=['Closed', 'Returned'])

    @property
    def active_jobs_count(self):
        return self.active_job_tickets.count()

    def get_columns_data(self):
        """
        Returns structured column/slot occupancy data for visualization:
        {
            'columns': [{'column_num': int, 'is_occupied': bool, 'jobs': [...], 'primary_job': ...}, ...],
            'overflow_jobs': [...],
            'occupied_slots_count': int,
            'total_slots': int,
        }
        """
        total = max(1, self.total_columns or 1)
        jobs = getattr(self, 'prefetched_active_jobs', None)
        if jobs is None:
            jobs = list(self.active_job_tickets.select_related('assigned_to__user'))

        col_map = {c: [] for c in range(1, total + 1)}
        overflow = []

        for job in jobs:
            c = job.rack_column
            if c and 1 <= c <= total:
                col_map[c].append(job)
            else:
                overflow.append(job)

        cols = []
        for c in range(1, total + 1):
            slot_jobs = col_map[c]
            cols.append({
                'column_num': c,
                'is_occupied': len(slot_jobs) > 0,
                'jobs': slot_jobs,
                'primary_job': slot_jobs[0] if slot_jobs else None,
                'extra_count': max(0, len(slot_jobs) - 1),
            })
        return {
            'columns': cols,
            'overflow_jobs': overflow,
            'occupied_slots_count': sum(1 for c in cols if c['is_occupied']),
            'total_slots': total,
        }


class JobTicket(models.Model):
    FEEDBACK_PENDING = 'pending'
    FEEDBACK_MESSAGE_SENT = 'message_sent'
    FEEDBACK_RECEIVED = 'received'
    FEEDBACK_CALLED_HAPPY = 'called_happy'
    FEEDBACK_CALLED_ISSUE = 'called_issue'
    FEEDBACK_NO_ANSWER = 'no_answer'
    FEEDBACK_CALL_LATER = 'call_later'
    FEEDBACK_UNREACHABLE = 'unreachable'
    FEEDBACK_FOLLOWUP_CHOICES = [
        (FEEDBACK_PENDING, 'Pending'),
        (FEEDBACK_MESSAGE_SENT, 'Message Sent'),
        (FEEDBACK_RECEIVED, 'Feedback Received'),
        (FEEDBACK_CALLED_HAPPY, 'Called - Happy'),
        (FEEDBACK_CALLED_ISSUE, 'Called - Issue'),
        (FEEDBACK_NO_ANSWER, 'No Answer'),
        (FEEDBACK_CALL_LATER, 'Call Later'),
        (FEEDBACK_UNREACHABLE, 'Unreachable (3 Attempts)'),
    ]

    STATUS_CHOICES = [
        ('Pending', 'Pending'),
        ('Under Inspection', 'Under Inspection'),
        ('Repairing', 'Repairing'),
        ('Specialized Service', 'Specialized Service'), 
        ('Returned', 'Returned'),
        ('Completed', 'Completed'),
        ('Ready for Pickup', 'Ready for Pickup'),
        ('Closed', 'Closed'),

    ]

    PAYMENT_STATUS_UNPAID = 'unpaid'
    PAYMENT_STATUS_PART_PAID = 'part_paid'
    PAYMENT_STATUS_PAID = 'paid'
    PAYMENT_STATUS_CHOICES = [
        (PAYMENT_STATUS_UNPAID, 'Unpaid'),
        (PAYMENT_STATUS_PART_PAID, 'Partially Paid'),
        (PAYMENT_STATUS_PAID, 'Paid'),
    ]

    workspace = models.ForeignKey(
        CompanyWorkspace,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='job_tickets',
        db_index=True,
    )
    job_code = models.CharField(max_length=50, unique=True, editable=False)  # Will be generated automatically

    # Customer Details
    customer_name = models.CharField(max_length=200)
    customer_phone = models.CharField(max_length=50)

    # Device Details
    device_type = models.CharField(max_length=100)  # e.g., Laptop, Desktop, Printer
    device_brand = models.CharField(max_length=100, blank=True)
    device_model = models.CharField(max_length=100, blank=True)
    device_serial = models.CharField(max_length=100, blank=True)
    device_password = models.CharField(max_length=100, blank=True, help_text="Device PIN, password, or pattern for technician access.")
    reported_issue = models.TextField()
    additional_items = models.TextField(blank=True, help_text="e.g., Laptop bag, Charger, Mouse")
    rack = models.ForeignKey(
        DeviceRack,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='job_tickets',
        help_text="Assigned physical storage rack/shelf location for this device.",
    )
    rack_column = models.PositiveIntegerField(
        null=True,
        blank=True,
        help_text="Specific column/slot number in the storage rack.",
    )

    # Service & Status
    status = models.CharField(max_length=50, choices=STATUS_CHOICES, default='Pending')
    assigned_to = models.ForeignKey(TechnicianProfile, on_delete=models.SET_NULL, null=True, blank=True,
                                    help_text="Current accepted technician (set when assignment accepted)")
    
    # Indicates the job was newly assigned to a technician and awaiting acknowledgement
    is_new_assignment = models.BooleanField(default=False, help_text="True when job is newly assigned and awaiting technician acknowledgement.")

    intake_date = models.DateField(
        null=True,
        blank=True,
        db_index=True,
        help_text="Date when device was physically received at the shop.",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    closed_at = models.DateTimeField(null=True, blank=True)

    # Re-entry option
    original_job_ticket = models.ForeignKey('self', on_delete=models.SET_NULL, null=True, blank=True,
                                            help_text="Link to the original job ticket for re-entry/rework")

    # Add this new field for the technician's private notes
    technician_notes = models.TextField(blank=True, help_text="Internal notes for technicians/staff.")
    technician_checklist = models.JSONField(
        default=dict,
        blank=True,
        help_text="Technician checklist answers keyed by checklist field key.",
    )
    requires_laptop_inspection_checklist = models.BooleanField(
        default=False,
        help_text="When enabled for laptop jobs, the technician must complete the inspection checklist before marking the ticket completed.",
    )

    discount_amount = models.DecimalField(max_digits=10, decimal_places=2, default=0)

    # Add a field for the user who created the ticket
    created_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True)
    is_under_warranty = models.BooleanField(default=False, help_text="Check if this job is under company warranty.")

    estimated_amount = models.DecimalField(max_digits=10, decimal_places=2, null=True, blank=True)
    estimation_note = models.TextField(blank=True, help_text="Customer-facing estimation/callback note.")
    estimated_delivery = models.DateField(null=True, blank=True)
    
    vyapar_invoice_number = models.CharField(max_length=50, blank=True, null=True)

    customer_group_id = models.CharField(
        max_length=50, 
        blank=True, 
        help_text="ID used to group multiple jobs from one customer submission."
    )
    
    # Payment Settlement
    payment_status = models.CharField(
        max_length=20,
        choices=PAYMENT_STATUS_CHOICES,
        default=PAYMENT_STATUS_UNPAID,
        db_index=True,
        help_text="Settlement status of this repair ticket.",
    )
    payment_method = models.CharField(
        max_length=20,
        choices=InventoryCreditPayment.METHOD_CHOICES,
        blank=True,
        null=True,
        help_text="Payment method used for settlement.",
    )
    amount_paid = models.DecimalField(
        max_digits=12,
        decimal_places=2,
        default=Decimal('0.00'),
        help_text="Total amount collected from the customer for this job.",
    )
    payment_reference = models.CharField(
        max_length=100,
        blank=True,
        help_text="Transaction ID, UPI reference, or check number.",
    )
    financial_account = models.ForeignKey(
        'FinancialAccount',
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='settled_jobs',
        help_text="Financial account (Cash drawer, Bank, UPI) where settlement payment was deposited.",
    )
    payment_date = models.DateTimeField(
        null=True,
        blank=True,
        help_text="When the payment or settlement occurred.",
    )

    # Feedback fields
    feedback_rating = models.IntegerField(
        null=True, 
        blank=True, 
        choices=[(i, i) for i in range(1, 11)],
        help_text="Customer rating (1-10)"
    )
    feedback_comment = models.TextField(blank=True, help_text="Customer feedback comment")
    feedback_date = models.DateTimeField(null=True, blank=True, help_text="When feedback was submitted")
    feedback_due_at = models.DateTimeField(
        null=True,
        blank=True,
        db_index=True,
        help_text="When the post-service feedback follow-up becomes due.",
    )
    feedback_followup_enabled = models.BooleanField(
        default=False,
        db_index=True,
        help_text="Enabled when this job should enter the 7-day feedback follow-up queue.",
    )
    feedback_message_sent_at = models.DateTimeField(
        null=True,
        blank=True,
        help_text="When the 7-day feedback WhatsApp was queued/sent.",
    )
    feedback_followup_status = models.CharField(
        max_length=30,
        choices=FEEDBACK_FOLLOWUP_CHOICES,
        default=FEEDBACK_PENDING,
        db_index=True,
    )
    feedback_followup_note = models.TextField(blank=True)
    feedback_followup_called_at = models.DateTimeField(null=True, blank=True)
    feedback_followup_marked_by = models.ForeignKey(
        User,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='marked_feedback_followups',
    )
    feedback_call_attempts = models.PositiveIntegerField(
        default=0,
        help_text="Number of unsuccessful call attempts (e.g. no answer)."
    )

    def __str__(self):
        return f"Job Code: {self.job_code} - {self.customer_name}"

    @property
    def rack_location_display(self):
        if not self.rack:
            return ""
        if self.rack_column:
            return f"{self.rack.name} - Col {self.rack_column} ({self.rack.group})"
        return f"{self.rack.name} ({self.rack.group})"

    @property
    def rack_short_display(self):
        if not self.rack:
            return ""
        if self.rack_column:
            return f"{self.rack.name} - Col {self.rack_column}"
        return self.rack.name

    def get_report_date(self):
        """Returns the date this job should be reported based on vendor concept.
        For vendor jobs: return date when returned from vendor
        For regular jobs: return completion/closure date
        """
        # Check if this is a vendor job
        if hasattr(self, 'specialized_service') and self.specialized_service:
            vendor_service = self.specialized_service
            # If returned from vendor, use the returned date
            if vendor_service.returned_date:
                return vendor_service.returned_date
            # If still with vendor, don't include in reports yet
            elif vendor_service.status == 'Sent to Vendor':
                return None
        
        if self.status == 'Closed' and self.closed_at:
            return self.closed_at

        # For regular jobs, use the updated_at (completion/closure date)
        return self.updated_at
    
    def is_vendor_job(self):
        """Check if this job was sent to a vendor"""
        return hasattr(self, 'specialized_service') and self.specialized_service is not None

    @property
    def has_feedback_followup_due(self):
        return bool(
            self.status == 'Closed'
            and self.feedback_followup_enabled
            and self.feedback_due_at
            and self.feedback_due_at <= timezone.now()
            and not self.feedback_rating
            and self.feedback_followup_status not in {
                self.FEEDBACK_RECEIVED,
                self.FEEDBACK_CALLED_HAPPY,
                self.FEEDBACK_UNREACHABLE,
            }
        )

    @property
    def total_cost(self):
        """Calculates total cost from service logs minus discount."""
        service_logs = self.service_logs.all()
        parts = sum(((log.part_cost or Decimal('0.00')) for log in service_logs), Decimal('0.00'))
        services = sum(((log.service_charge or Decimal('0.00')) for log in service_logs), Decimal('0.00'))
        discount = self.discount_amount or Decimal('0.00')
        return max(Decimal('0.00'), (parts + services) - discount)

    @property
    def balance_due(self):
        paid = self.amount_paid or Decimal('0.00')
        return max(Decimal('0.00'), self.total_cost - paid)

    @property
    def display_intake_date(self):
        if self.intake_date:
            return self.intake_date
        if self.created_at:
            return timezone.localtime(self.created_at).date()
        return timezone.localdate()

    def save(self, *args, **kwargs):
        if not self.intake_date:
            if self.created_at:
                self.intake_date = timezone.localtime(self.created_at).date()
            else:
                self.intake_date = timezone.localdate()
        super().save(*args, **kwargs)




class JobReminder(models.Model):
    STATUS_PENDING = 'pending'
    STATUS_DONE = 'done'
    STATUS_CANCELLED = 'cancelled'
    STATUS_CHOICES = [
        (STATUS_PENDING, 'Pending'),
        (STATUS_DONE, 'Done'),
        (STATUS_CANCELLED, 'Cancelled'),
    ]

    job_ticket = models.ForeignKey(JobTicket, on_delete=models.CASCADE, related_name='reminders')
    due_at = models.DateTimeField(db_index=True)
    purpose = models.CharField(max_length=120, default='Customer callback')
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default=STATUS_PENDING, db_index=True)
    last_prompted_at = models.DateTimeField(null=True, blank=True)
    completed_at = models.DateTimeField(null=True, blank=True)
    created_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True, related_name='job_reminders')
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['due_at', 'id']
        indexes = [
            models.Index(fields=['status', 'due_at']),
        ]

    def __str__(self):
        return f"{self.job_ticket.job_code} reminder at {self.due_at:%Y-%m-%d %H:%M}"

    @property
    def is_due(self):
        return self.status == self.STATUS_PENDING and self.due_at <= timezone.now()


class JobTicketPhoto(models.Model):
    job_ticket = models.ForeignKey(JobTicket, on_delete=models.CASCADE, related_name='photos')
    image = models.ImageField(upload_to='job_ticket_photos/', blank=True, null=True)
    image_name = models.CharField(max_length=255, blank=True)
    image_content_type = models.CharField(max_length=100, blank=True)
    image_data = models.BinaryField(blank=True, null=True)
    uploaded_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-uploaded_at']

    def __str__(self):
        return f"{self.job_ticket.job_code} photo {self.id}"

    @property
    def image_url(self):
        if self.image_data:
            return reverse('staff_job_photo_file', args=[self.job_ticket.job_code, self.id])
        if self.image:
            return self.image.url
        return ''


class Assignment(models.Model):
    """Tracks technician acknowledgement of a job (accept/reject). Status-only; does not drive job state."""
    ASSIGNMENT_STATUS = [
        ('pending', 'Pending'),
        ('accepted', 'Accepted'),
        ('rejected', 'Rejected'),
    ]

    job = models.ForeignKey(JobTicket, on_delete=models.CASCADE, related_name='assignments')
    technician = models.ForeignKey(TechnicianProfile, on_delete=models.CASCADE, related_name='assignments')
    status = models.CharField(max_length=16, choices=ASSIGNMENT_STATUS, default='pending')
    created_at = models.DateTimeField(auto_now_add=True)
    responded_at = models.DateTimeField(null=True, blank=True)
    response_note = models.TextField(blank=True)

    class Meta:
        ordering = ['-created_at']
        unique_together = [('job', 'technician')]

    def __str__(self):
        return f"{self.job.job_code} → {self.technician.user.username} ({self.status})"

    def accept(self, note=""):
        """Mark accepted — no side-effects on job status."""
        self.status = 'accepted'
        self.responded_at = timezone.now()
        self.response_note = note or ""
        self.save(update_fields=['status', 'responded_at', 'response_note'])

    def reject(self, note=""):
        """Mark rejected — no side-effects on job status."""
        self.status = 'rejected'
        self.responded_at = timezone.now()
        self.response_note = note or ""
        self.save(update_fields=['status', 'responded_at', 'response_note'])


# ---------------------------------------------------------------------------
# Standalone Task Management System
# ---------------------------------------------------------------------------

class Task(models.Model):
    """A standalone work item that can be assigned to a technician, independent of job status."""

    PRIORITY_LOW = 'low'
    PRIORITY_MEDIUM = 'medium'
    PRIORITY_HIGH = 'high'
    PRIORITY_URGENT = 'urgent'
    PRIORITY_CHOICES = [
        (PRIORITY_LOW, 'Low'),
        (PRIORITY_MEDIUM, 'Medium'),
        (PRIORITY_HIGH, 'High'),
        (PRIORITY_URGENT, 'Urgent / Rush'),
    ]

    STATUS_OPEN = 'open'
    STATUS_IN_PROGRESS = 'in_progress'
    STATUS_DONE = 'done'
    STATUS_CANCELLED = 'cancelled'
    STATUS_CHOICES = [
        (STATUS_OPEN, 'Open'),
        (STATUS_IN_PROGRESS, 'In Progress'),
        (STATUS_DONE, 'Done'),
        (STATUS_CANCELLED, 'Cancelled'),
    ]

    workspace = models.ForeignKey(
        CompanyWorkspace,
        on_delete=models.CASCADE,
        related_name='tasks',
        null=True,
        blank=True,
        db_index=True,
    )
    title = models.CharField(max_length=200)
    description = models.TextField(blank=True)
    priority = models.CharField(
        max_length=16,
        choices=PRIORITY_CHOICES,
        default=PRIORITY_MEDIUM,
        db_index=True,
    )
    due_date = models.DateTimeField(null=True, blank=True)
    status = models.CharField(
        max_length=16,
        choices=STATUS_CHOICES,
        default=STATUS_OPEN,
        db_index=True,
    )
    assigned_to = models.ForeignKey(
        TechnicianProfile,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='tasks',
    )
    created_by = models.ForeignKey(
        User,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='created_tasks',
    )
    job_reference = models.ForeignKey(
        JobTicket,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='tasks',
        help_text='Optional: link this task to a job ticket for traceability.',
    )
    is_open_to_all = models.BooleanField(
        default=False,
        db_index=True,
        help_text='If True, any technician can claim/accept this task from the shared pool.',
    )
    has_alarm = models.BooleanField(
        default=False,
        db_index=True,
        help_text='Sound an audible alarm alert when task is due or assigned.',
    )
    alarm_time = models.DateTimeField(
        null=True,
        blank=True,
        help_text='Specific time to trigger the alarm (defaults to due_date or immediate if blank).',
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    completed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = [
            models.Case(
                models.When(priority='urgent', then=models.Value(0)),
                models.When(priority='high', then=models.Value(1)),
                models.When(priority='medium', then=models.Value(2)),
                models.When(priority='low', then=models.Value(3)),
                default=models.Value(4),
                output_field=models.IntegerField(),
            ),
            models.F('due_date').asc(nulls_last=True),
            '-created_at',
        ]

    def __str__(self):
        tech = self.assigned_to.user.username if self.assigned_to else ('Open Pool' if self.is_open_to_all else 'Unassigned')
        return f"[{self.get_priority_display()}] {self.title} → {tech} ({self.status})"

    @property
    def is_claimable(self):
        """Returns True if the task is in the shared pool or unassigned and still active."""
        return (self.assigned_to_id is None or self.is_open_to_all) and self.status in [self.STATUS_OPEN, self.STATUS_IN_PROGRESS]

    def accept_by_technician(self, technician):
        """Claim this task for a technician."""
        self.assigned_to = technician
        self.is_open_to_all = False
        self.save(update_fields=['assigned_to', 'is_open_to_all', 'updated_at'])

    def mark_in_progress(self):
        self.status = self.STATUS_IN_PROGRESS
        self.save(update_fields=['status', 'updated_at'])

    def mark_done(self):
        self.status = self.STATUS_DONE
        self.completed_at = timezone.now()
        self.save(update_fields=['status', 'completed_at', 'updated_at'])

    def mark_cancelled(self):
        self.status = self.STATUS_CANCELLED
        self.save(update_fields=['status', 'updated_at'])


class TaskAttachment(models.Model):
    """File attachment for a standalone Task."""
    task = models.ForeignKey(Task, on_delete=models.CASCADE, related_name='attachments')
    message = models.ForeignKey('TaskMessage', on_delete=models.SET_NULL, null=True, blank=True, related_name='attachments')
    file = models.FileField(upload_to='task_attachments/%Y/%m/', blank=True, null=True)
    file_name = models.CharField(max_length=255, blank=True)
    file_size = models.PositiveIntegerField(default=0)
    uploaded_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True)
    uploaded_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['uploaded_at']

    def __str__(self):
        return f"Attachment {self.file_name or self.id} for Task {self.task_id}"


class TaskMessage(models.Model):
    """Chat message on a Task thread between staff and technician."""
    task = models.ForeignKey(Task, on_delete=models.CASCADE, related_name='messages')
    sender = models.ForeignKey(User, on_delete=models.CASCADE, related_name='task_messages')
    body = models.TextField()
    sent_at = models.DateTimeField(auto_now_add=True)
    is_read = models.BooleanField(default=False)

    class Meta:
        ordering = ['sent_at']

    def __str__(self):
        return f"[Task {self.task_id}] {self.sender.username}: {self.body[:40]}"


class ServiceLog(models.Model):
    job_ticket = models.ForeignKey(JobTicket, on_delete=models.CASCADE, related_name='service_logs')
    description = models.CharField(max_length=255)
    part_cost = models.DecimalField(max_digits=10, decimal_places=2, null=True, blank=True)
    service_charge = models.DecimalField(max_digits=10, decimal_places=2)
    
    # CORRECT PLACEMENT OF THE NEW FIELD:
    sales_invoice_number = models.CharField(
        max_length=50, 
        blank=True, 
        null=True, 
        help_text="Reference number from the external billing software."
    )
    
    created_at = models.DateTimeField(auto_now_add=True)

    @property
    def line_total(self):
        parts = self.part_cost or Decimal('0.00')
        service = self.service_charge or Decimal('0.00')
        return parts + service

    def __str__(self):
        return f"{self.job_ticket.job_code} - {self.description}"



class ProductSale(models.Model):
    """Ledger of inventory sales captured from billing."""
    workspace = models.ForeignKey(
        CompanyWorkspace,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='product_sales',
        db_index=True,
    )
    job_ticket = models.ForeignKey(JobTicket, on_delete=models.CASCADE, related_name='product_sales')
    product = models.ForeignKey(Product, on_delete=models.PROTECT, related_name='sales')
    service_log = models.OneToOneField(
        ServiceLog,
        on_delete=models.CASCADE,
        related_name='product_sale',
        null=True,
        blank=True,
    )
    inventory_entry = models.OneToOneField(
        InventoryEntry,
        on_delete=models.SET_NULL,
        related_name='product_sale_entry',
        null=True,
        blank=True,
        help_text="Linked inventory sales register entry for this job product sale.",
    )

    quantity = models.PositiveIntegerField(default=1)
    unit_price = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    cost_price = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    line_total = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    line_cost = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    line_profit = models.DecimalField(max_digits=12, decimal_places=2, default=0)

    sold_at = models.DateTimeField(auto_now_add=True)
    sold_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True, related_name='product_sales')

    class Meta:
        ordering = ['-sold_at']

    def __str__(self):
        return f"{self.job_ticket.job_code} - {self.product.name} x{self.quantity}"


class JobTicketLog(models.Model):
    """Stores a log of all changes and events for a JobTicket."""
    ACTION_CHOICES = [
        ('CREATED', 'Job Created'),
        ('ASSIGNED', 'Technician Assigned'),
        ('STATUS', 'Status Changed'),
        ('NOTE', 'Note Updated'),
        ('SERVICE', 'Service Log Added/Updated'),
        ('BILLING', 'Billing Info Updated'),
        ('CLOSED', 'Job Closed'),
        ('DETAILS', 'Customer & Device Details Updated'),
    ]

    job_ticket = models.ForeignKey(JobTicket, on_delete=models.CASCADE, related_name='logs')
    user = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True, help_text="User who performed the action")
    timestamp = models.DateTimeField(auto_now_add=True)
    action = models.CharField(max_length=50, choices=ACTION_CHOICES)
    details = models.TextField(help_text="Description of the change, e.g., 'Status changed from Pending to Repairing'")

    class Meta:
        ordering = ['-timestamp'] # Show newest logs first

    def __str__(self):
        return f"{self.job_ticket.job_code} - {self.action} at {self.timestamp.strftime('%Y-%m-%d %H:%M')}"
    

# Add this new model at the end of job_tickets/models.py

class Vendor(models.Model):
    """Represents a third-party service provider or company."""
    workspace = models.ForeignKey(
        CompanyWorkspace,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='vendors',
        db_index=True,
    )
    name = models.CharField(max_length=200, help_text="The individual's name or contact person.")
    company_name = models.CharField(max_length=255, help_text="The official name of the vendor company.")
    phone = models.CharField(max_length=20, blank=True)
    email = models.EmailField(blank=True)
    address = models.TextField(blank=True)
    specialties = models.TextField(blank=True, help_text="Notes on what this vendor specializes in, e.g., 'Motherboard chip-level repair', 'Data recovery'.")
    is_active = models.BooleanField(default=True, db_index=True, help_text="Whether this vendor is active and available for new jobs.")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['company_name']
        constraints = [
            models.UniqueConstraint(fields=['workspace', 'company_name'], name='unique_vendor_company_per_workspace'),
        ]

    def __str__(self):
        return self.company_name
    
# Add this new model at the end of job_tickets/models.py

class SpecializedService(models.Model):
    """Tracks a job that has been sent to an external vendor."""
    STATUS_CHOICES = [
        ('Awaiting Assignment', 'Awaiting Vendor Assignment'),
        ('Sent to Vendor', 'Sent to Vendor'),
        ('Returned from Vendor', 'Returned from Vendor'),
    ]
    
    job_ticket = models.OneToOneField(JobTicket, on_delete=models.CASCADE, related_name='specialized_service')
    vendor = models.ForeignKey(Vendor, on_delete=models.SET_NULL, null=True, blank=True, related_name='services')
    
    status = models.CharField(max_length=50, choices=STATUS_CHOICES, default='Awaiting Assignment')
    
    # Financials
    vendor_cost = models.DecimalField(max_digits=10, decimal_places=2, null=True, blank=True, help_text="The amount we pay the vendor.")
    vendor_discount_amount = models.DecimalField(
        max_digits=10,
        decimal_places=2,
        default=0,
        help_text="Discount or adjustment received from the vendor.",
    )
    vendor_paid_amount = models.DecimalField(
        max_digits=10,
        decimal_places=2,
        default=0,
        help_text="Amount already paid to the vendor.",
    )
    vendor_balance_amount = models.DecimalField(
        max_digits=10,
        decimal_places=2,
        default=0,
        help_text="Remaining amount payable to the vendor after discount and paid amount.",
    )
    client_charge = models.DecimalField(max_digits=10, decimal_places=2, null=True, blank=True, help_text="The amount we charge the client for this service.")
    
    # Tracking
    sent_date = models.DateTimeField(null=True, blank=True)
    returned_date = models.DateTimeField(null=True, blank=True)
    vendor_bill_number = models.CharField(
        max_length=100,
        blank=True,
        default='',
        help_text="Vendor invoice or bill number.",
    )
    notes = models.TextField(blank=True, help_text="Internal notes about this specialized service.")

    @property
    def vendor_net_payable(self):
        vendor_cost = self.vendor_cost or Decimal('0.00')
        discount = self.vendor_discount_amount or Decimal('0.00')
        payable = vendor_cost - discount
        return payable if payable > Decimal('0.00') else Decimal('0.00')

    @property
    def vendor_payment_status(self):
        if not self.vendor_cost:
            return 'Not Entered'
        if (self.vendor_balance_amount or Decimal('0.00')) <= Decimal('0.00'):
            return 'Paid'
        if (self.vendor_paid_amount or Decimal('0.00')) > Decimal('0.00'):
            return 'Part Paid'
        return 'Balance Due'

    def __str__(self):
        return f"{self.job_ticket.job_code} -> {self.vendor.company_name if self.vendor else 'Unassigned'}"


class VendorPayment(models.Model):
    METHOD_CASH = 'cash'
    METHOD_TRANSFER = 'transfer'
    METHOD_CHOICES = [
        (METHOD_CASH, 'Cash'),
        (METHOD_TRANSFER, 'Transfer'),
    ]

    vendor = models.ForeignKey(Vendor, on_delete=models.CASCADE, related_name='payments')
    specialized_service = models.ForeignKey(
        SpecializedService,
        on_delete=models.CASCADE,
        related_name='payment_transactions',
    )
    payment_date = models.DateField(default=timezone.localdate)
    payment_method = models.CharField(max_length=20, choices=METHOD_CHOICES, default=METHOD_CASH)
    financial_account = models.ForeignKey(
        'FinancialAccount',
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name='vendor_payments',
    )
    amount = models.DecimalField(max_digits=10, decimal_places=2)
    balance_before = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    balance_after = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    reference_no = models.CharField(max_length=100, blank=True)
    notes = models.TextField(blank=True)
    created_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True, related_name='vendor_payments_created')
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-payment_date', '-created_at']

    def __str__(self):
        return f"{self.vendor.company_name} payment {self.amount} on {self.payment_date}"


class DailyJobCodeSequence(models.Model):
    """Tracks the last issued counter for a given job-code date prefix."""
    date = models.DateField(unique=True)
    last_counter = models.PositiveIntegerField(default=0)

    class Meta:
        ordering = ['-date']

    def __str__(self):
        return f"{self.date}: {self.last_counter}"


class CompanyProfile(models.Model):
    """Company profile settings - single instance"""
    workspace = models.ForeignKey(
        CompanyWorkspace,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='company_profiles',
        db_index=True,
    )
    # Basic Info
    company_name = models.CharField(max_length=200, default="GI Hostings")
    legal_name = models.CharField(
        max_length=200,
        blank=True,
        help_text="Registered legal name used on GST documents.",
    )
    tagline = models.CharField(max_length=200, default="Service Billing", blank=True)
    logo = models.ImageField(upload_to='company_logos/', blank=True, null=True)
    logo_url = models.URLField(max_length=500, blank=True, help_text="Or use image URL")
    
    # Contact Details
    address = models.TextField(default="Your Business Address")
    city = models.CharField(max_length=100, default="", blank=True)
    state = models.CharField(max_length=100, default="", blank=True)
    pincode = models.CharField(max_length=10, default="", blank=True)
    phone1 = models.CharField(max_length=20, default="+919567227005")
    phone2 = models.CharField(max_length=20, blank=True)
    email = models.EmailField(blank=True)
    website = models.URLField(blank=True, default="https://gihostings.com")
    
    # Legal & Tax Details
    gstin = models.CharField(
        max_length=15,
        blank=True,
        validators=[validate_gstin],
        help_text="GST Identification Number",
    )
    pan = models.CharField(
        max_length=10,
        blank=True,
        validators=[validate_pan],
        help_text="PAN Number",
    )
    state_code = models.CharField(
        max_length=2,
        blank=True,
        validators=[validate_state_code],
        help_text="2-digit GST state code of the business registration.",
    )
    registration_type = models.CharField(
        max_length=20,
        choices=COMPANY_REGISTRATION_TYPE_CHOICES,
        default='regular',
    )
    filing_frequency = models.CharField(
        max_length=20,
        choices=FILING_FREQUENCY_CHOICES,
        default='monthly',
    )
    qrmp_enabled = models.BooleanField(default=False, help_text="Enable quarterly return filing under QRMP.")
    lut_bond_enabled = models.BooleanField(default=False, help_text="Use LUT/Bond for eligible zero-rated supplies.")
    annual_turnover_band = models.CharField(
        max_length=20,
        choices=ANNUAL_TURNOVER_BAND_CHOICES,
        default='up_to_5cr',
    )
    e_invoice_applicable = models.BooleanField(default=False, help_text="Whether e-invoice compliance applies to this business.")
    e_way_bill_enabled = models.BooleanField(default=True, help_text="Enable e-Way Bill related workflow fields.")
    default_place_of_supply_state = models.CharField(
        max_length=2,
        blank=True,
        validators=[validate_state_code],
        help_text="Default place of supply state code used for billing.",
    )
    
    # Bank Details
    bank_name = models.CharField(max_length=200, blank=True)
    account_number = models.CharField(max_length=50, blank=True)
    ifsc_code = models.CharField(max_length=11, blank=True)
    branch = models.CharField(max_length=200, blank=True)
    upi_id = models.CharField(max_length=100, blank=True, help_text="UPI ID for payments")
    
    PRINT_PAPER_A4 = 'a4'
    PRINT_PAPER_A5 = 'a5'
    PRINT_PAPER_THERMAL_80 = 'thermal_80'
    PRINT_PAPER_CHOICES = [
        (PRINT_PAPER_A4, 'A4'),
        (PRINT_PAPER_A5, 'A5'),
        (PRINT_PAPER_THERMAL_80, 'Thermal 80mm'),
    ]
    PRINT_PAGE_SIZE_CSS = {
        PRINT_PAPER_A4: 'A4 portrait',
        PRINT_PAPER_A5: 'A5 portrait',
        PRINT_PAPER_THERMAL_80: '80mm 297mm',
    }
    PRINT_PAGE_WIDTH_CSS = {
        PRINT_PAPER_A4: '210mm',
        PRINT_PAPER_A5: '148mm',
        PRINT_PAPER_THERMAL_80: '80mm',
    }

    # Job Ticket Settings
    job_code_prefix = models.CharField(max_length=10, default="GI", help_text="Prefix for job codes (e.g., GI, SERV)")
    job_ticket_print_paper_size = models.CharField(
        max_length=20,
        choices=PRINT_PAPER_CHOICES,
        default=PRINT_PAPER_A5,
        help_text="Default paper size for job ticket / receipt prints.",
    )
    bill_print_paper_size = models.CharField(
        max_length=20,
        choices=PRINT_PAPER_CHOICES,
        default=PRINT_PAPER_A4,
        help_text="Default paper size for service and inventory bill prints.",
    )
    sales_invoice_prefix = models.CharField(
        max_length=20,
        default="INV",
        blank=True,
        help_text="Prefix for auto-generated sales invoice numbers.",
    )
    sales_invoice_next_number = models.PositiveIntegerField(
        default=1,
        help_text="Next sequence number to use for auto-generated sales invoices.",
    )
    include_workshop_device_tag = models.BooleanField(
        default=True,
        help_text="Include tear-off workshop device tag on job ticket intake receipts.",
    )
    include_ticket_signatures = models.BooleanField(
        default=True,
        help_text="Include signature section on job intake receipts.",
    )
    include_bill_signatures = models.BooleanField(
        default=True,
        help_text="Include signature section on payment bills and invoices.",
    )
    include_bill_payment_details = models.BooleanField(
        default=True,
        help_text="Include payment settlement and bank details section on bills.",
    )
    TECHNICIAN_DISPLAY_CHOICES = [
        ('id', 'Technician ID Number'),
        ('name', 'Technician Name'),
    ]
    technician_display_format = models.CharField(
        max_length=10,
        choices=TECHNICIAN_DISPLAY_CHOICES,
        default='id',
        help_text="Display technician by ID number or name under 'Serviced By' on printed bills.",
    )
    
    # GST Settings
    enable_gst = models.BooleanField(default=False, help_text="Enable GST on invoices")
    gst_rate = models.DecimalField(max_digits=5, decimal_places=2, default=18.00, help_text="GST rate in percentage")
    
    # Terms & Policies
    terms_conditions = models.TextField(blank=True, default="1. All repairs carry 30 days warranty.\n2. No warranty on physical/liquid damage.\n3. Payment due on delivery.")
    warranty_policy = models.TextField(blank=True, default="30 days warranty on all repairs. Does not cover physical or liquid damage.")
    
    class Meta:
        verbose_name = "Company Profile"
        verbose_name_plural = "Company Profile"
        permissions = [
            ("view_financial_reports", "Can view financial reports"),
        ]
    
    def __str__(self):
        return self.company_name

    @staticmethod
    def _print_page_size_css(paper_size):
        return CompanyProfile.PRINT_PAGE_SIZE_CSS.get(paper_size, 'A4 portrait')

    @staticmethod
    def _print_page_width_css(paper_size):
        return CompanyProfile.PRINT_PAGE_WIDTH_CSS.get(paper_size, '210mm')

    @staticmethod
    def _print_margin_css(paper_size, default_margin='12mm'):
        if paper_size == CompanyProfile.PRINT_PAPER_THERMAL_80:
            return '3mm'
        return default_margin

    @property
    def job_ticket_print_page_size_css(self):
        return self._print_page_size_css(self.job_ticket_print_paper_size)

    @property
    def job_ticket_print_page_width_css(self):
        return self._print_page_width_css(self.job_ticket_print_paper_size)

    @property
    def job_ticket_print_margin_css(self):
        return self._print_margin_css(self.job_ticket_print_paper_size, '6mm')

    @property
    def bill_print_page_size_css(self):
        return self._print_page_size_css(self.bill_print_paper_size)

    @property
    def bill_print_page_width_css(self):
        return self._print_page_width_css(self.bill_print_paper_size)

    @property
    def bill_print_margin_css(self):
        return self._print_margin_css(self.bill_print_paper_size, '12mm')

    @property
    def display_logo_url(self):
        """Return the workspace logo, or fall back to the root company profile logo."""
        if self.logo:
            return self.logo.url
        if self.logo_url:
            return self.logo_url
        if self.workspace:
            root_profile = CompanyProfile.objects.filter(workspace__isnull=True).first()
            if root_profile:
                if root_profile.logo:
                    return root_profile.logo.url
                if root_profile.logo_url:
                    return root_profile.logo_url
        return ""
    
    @classmethod
    def get_profile(cls, workspace=None):
        """Get or create the company profile for the active workspace."""
        if workspace:
            profile = cls.objects.filter(workspace=workspace).first()
            if not profile:
                max_id = cls.objects.aggregate(m=models.Max('id'))['m'] or 0
                profile = cls.objects.create(id=max_id + 1, workspace=workspace, company_name=workspace.name)
            return profile
        profile = cls.objects.filter(workspace__isnull=True).first() or cls.objects.first()
        if not profile:
            max_id = cls.objects.aggregate(m=models.Max('id'))['m'] or 0
            profile = cls.objects.create(id=max_id + 1)
        return profile


class PlatformSettings(models.Model):
    """Platform-level branding and footer support settings."""
    billing_author_name = models.CharField(
        max_length=200,
        default="GI HOSTINGS",
        help_text="Footer author name shown as 'Service Billing by ...'",
    )
    support_phone = models.CharField(
        max_length=20,
        default="+91 9567227005",
        help_text="Customer support phone number displayed in footer.",
    )
    whatsapp_number = models.CharField(
        max_length=20,
        default="+91 9567227005",
        help_text="WhatsApp support number displayed in footer.",
    )
    website = models.URLField(blank=True, default="https://gihostings.com")

    class Meta:
        verbose_name = "Platform Settings"
        verbose_name_plural = "Platform Settings"

    def __str__(self):
        return self.billing_author_name

    @property
    def support_phone_href(self):
        return ''.join(ch for ch in self.support_phone if ch.isdigit() or ch == '+')

    @property
    def whatsapp_link(self):
        digits = ''.join(ch for ch in self.whatsapp_number if ch.isdigit())
        if not digits:
            return ''
        return f"https://wa.me/{digits}"

    @classmethod
    def get_settings(cls):
        settings_obj, created = cls.objects.get_or_create(id=1)
        return settings_obj


class WhatsAppIntegrationSettings(models.Model):
    """Configuration for WhatsApp delivery through Cloud API or local QR bridge."""

    DELIVERY_CLOUD_API = 'cloud_api'
    DELIVERY_BRIDGE = 'bridge'
    DELIVERY_METHOD_CHOICES = [
        (DELIVERY_CLOUD_API, 'WhatsApp Cloud API'),
        (DELIVERY_BRIDGE, 'WhatsApp Bridge (QR Login)'),
    ]

    workspace = models.ForeignKey(
        CompanyWorkspace,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='whatsapp_settings',
        db_index=True,
    )
    delivery_method = models.CharField(
        max_length=20,
        choices=DELIVERY_METHOD_CHOICES,
        default=DELIVERY_CLOUD_API,
        help_text="Choose how automatic WhatsApp notifications are sent.",
    )
    bridge_base_url = models.URLField(
        default='http://127.0.0.1:3001',
        help_text="Base URL of the local WhatsApp bridge service used for QR login delivery.",
    )

    api_version = models.CharField(
        max_length=16,
        default='v23.0',
        help_text="Graph API version used for WhatsApp Cloud API requests.",
    )
    phone_number_id = models.CharField(
        max_length=64,
        blank=True,
        help_text="Phone Number ID from Meta WhatsApp Manager / API Setup.",
    )
    access_token = models.TextField(
        blank=True,
        help_text="Permanent access token used to call the WhatsApp Cloud API.",
    )
    webhook_verify_token = models.CharField(
        max_length=255,
        blank=True,
        help_text="Custom verify token used while configuring the Meta webhook.",
    )
    app_secret = models.CharField(
        max_length=255,
        blank=True,
        help_text="Optional Meta app secret used to validate webhook signatures.",
    )
    public_site_url = models.URLField(
        # default='http://127.0.0.1:8000', 
        default='http://192.168.1.4:8000',
        help_text="Public base URL used for client status/receipt links sent on WhatsApp.",
    )
    is_enabled = models.BooleanField(
        default=False,
        help_text="Enable automatic WhatsApp notifications for ticket events.",
    )
    default_country_code = models.CharField(
        max_length=5,
        default='91',
        help_text="Used when a customer phone number has no country code.",
    )
    template_language_code = models.CharField(
        max_length=20,
        default='en_US',
        help_text="Language code used for approved template messages (example: en_US).",
    )
    test_template_name = models.CharField(
        max_length=100,
        default='hello_world',
        blank=True,
        help_text="Approved template name used for manual Cloud API test sends.",
    )
    notify_on_created = models.BooleanField(default=True)
    notify_on_completed = models.BooleanField(default=True)
    notify_on_delivered = models.BooleanField(default=True)
    notify_on_feedback = models.BooleanField(default=True)
    notify_daily_report = models.BooleanField(
        default=False,
        help_text="Automatically send daily business summary report via WhatsApp.",
    )
    daily_report_phone = models.CharField(
        max_length=120,
        blank=True,
        help_text="Recipient phone number(s) for daily report (comma-separated).",
    )
    daily_report_time = models.TimeField(
        default=datetime.time(21, 0),
        help_text="Scheduled time to dispatch automatic daily report (e.g. 21:00 for 9:00 PM).",
    )
    daily_report_last_sent_date = models.DateField(
        null=True,
        blank=True,
        help_text="Last date for which the automatic daily report was dispatched.",
    )
    created_template_name = models.CharField(
        max_length=100,
        blank=True,
        help_text="Approved template name used for the ticket created notification.",
    )
    created_template = models.TextField(
        default=(
            "Hello {customer_name}, your ticket {job_code} has been created.\n"
            "Device: {device_type} - {device_brand} {device_model}\n"
            "Issue: {reported_issue}\n"
            "Status: {status}\n"
            "Receipt: {receipt_link}"
        )
    )
    completed_template_name = models.CharField(
        max_length=100,
        blank=True,
        help_text="Approved template name used for the ticket completed notification.",
    )
    completed_template = models.TextField(
        default=(
            "Hello {customer_name}, your ticket {job_code} is completed.\n"
            "Device: {device_brand} {device_model} ({device_type})\n"
            "Status: {status}\n"
            "Track status: {status_link}"
        )
    )
    delivered_template_name = models.CharField(
        max_length=100,
        blank=True,
        help_text="Approved template name used for the ticket delivered/closed notification.",
    )
    delivered_template = models.TextField(
        default=(
            "Hello {customer_name}, your ticket {job_code} is now closed.\n"
            "We hope you're happy with the service.\n"
            "Please share your feedback here: {status_link}"
        )
    )
    estimate_template_name = models.CharField(
        max_length=100,
        blank=True,
        help_text="Approved template name used for manual estimate messages.",
    )
    estimate_template = models.TextField(
        default=(
            "Hello {customer_name}, estimate for ticket {job_code} is {estimated_amount}.\n"
            "Device: {device_brand} {device_model} ({device_type})\n"
            "Note: {estimation_note}\n"
            "Status: {status}\n"
            "Track status: {status_link}"
        )
    )
    feedback_template_name = models.CharField(
        max_length=100,
        blank=True,
        help_text="Approved template name used for 7-day feedback follow-up messages.",
    )
    feedback_template = models.TextField(
        default=(
            "Hello {customer_name}, your ticket {job_code} was closed 7 days ago.\n"
            "Please share your service feedback here: {status_link}"
        ),
        blank=True,
    )
    daily_report_template = models.TextField(
        default=(
            "📊 *DAILY BUSINESS SUMMARY - {company_name}*\n"
            "📅 Date: {date} | Generated: {generated_time}\n\n"
            "🔧 *Operational Workload:*\n"
            "• 📥 New Jobs Received: {tickets_created_count}\n"
            "• ⚙️ Jobs Completed: {tickets_completed_count}\n"
            "• 🚚 Jobs Delivered / Closed: {tickets_delivered_count}\n"
            "• ⏳ Active Jobs Backlog: {active_tickets_count}\n\n"
            "💰 *Financials & Collections:*\n"
            "• 💵 Cash Receipts: ₹{cash_inflows}\n"
            "• 📱 Bank & UPI Receipts: ₹{bank_inflows}\n"
            "• 💳 Total Revenue: ₹{total_inflows}\n"
            "• 📤 Day Expenses Paid: ₹{total_expenses}\n\n"
            "🏦 *Cash Drawer Reconciliation:*\n"
            "• 🪙 Cash in Drawer: ₹{cash_drawer_balance}\n"
            "• 🌐 Total Liquid Position: ₹{total_liquid_balance}"
        ),
        blank=True,
        help_text="Template used for the daily business summary WhatsApp message.",
    )
    created_pdf_caption_template = models.TextField(
        default="Job Ticket {job_code}",
        blank=True,
        help_text="Caption used when sending the job ticket PDF on creation.",
    )
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "WhatsApp Integration Settings"
        verbose_name_plural = "WhatsApp Integration Settings"

    def __str__(self):
        return "WhatsApp Integration Settings"

    @classmethod
    def get_settings(cls, workspace=None):
        if workspace:
            settings_obj, _ = cls.objects.get_or_create(workspace=workspace)
            return settings_obj
        settings_obj, _ = cls.objects.get_or_create(id=1)
        return settings_obj


class WhatsAppNotificationLog(models.Model):
    EVENT_CHOICES = [
        ('created', 'Created'),
        ('completed', 'Completed'),
        ('delivered', 'Delivered'),
        ('estimate', 'Estimate'),
        ('feedback', 'Feedback'),
        ('daily_report', 'Daily Report'),
        ('manual', 'Manual'),
    ]

    message_queue = models.OneToOneField(
        'MessageQueue',
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='notification_log',
    )

    job_ticket = models.ForeignKey(
        JobTicket,
        on_delete=models.CASCADE,
        related_name='whatsapp_notifications',
        null=True,
        blank=True,
    )
    event_type = models.CharField(max_length=20, choices=EVENT_CHOICES)
    target_phone = models.CharField(max_length=20)
    message = models.TextField()
    was_successful = models.BooleanField(default=False)
    response_text = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-created_at']

    def __str__(self):
        prefix = self.job_ticket.job_code if self.job_ticket else 'Daily Report'
        return f"{prefix} - {self.event_type} - {'ok' if self.was_successful else 'failed'}"


class BulkCampaign(models.Model):
    STATUS_PENDING = 'pending'
    STATUS_IN_PROGRESS = 'in_progress'
    STATUS_PAUSED = 'paused'
    STATUS_COMPLETED = 'completed'
    STATUS_CANCELLED = 'cancelled'
    STATUS_CHOICES = [
        (STATUS_PENDING, 'Pending'),
        (STATUS_IN_PROGRESS, 'In Progress'),
        (STATUS_PAUSED, 'Paused'),
        (STATUS_COMPLETED, 'Completed'),
        (STATUS_CANCELLED, 'Cancelled'),
    ]

    workspace = models.ForeignKey(
        CompanyWorkspace,
        on_delete=models.CASCADE,
        related_name='bulk_campaigns',
        null=True,
        blank=True,
        db_index=True,
    )
    title = models.CharField(max_length=200)
    target_filter = models.CharField(max_length=50, default='all')
    message_template = models.TextField()
    selected_client_ids = models.JSONField(default=list, blank=True)
    total_recipients = models.PositiveIntegerField(default=0)
    sent_count = models.PositiveIntegerField(default=0)
    failed_count = models.PositiveIntegerField(default=0)
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default=STATUS_PENDING, db_index=True)
    created_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-created_at']

    def __str__(self):
        return f"{self.title} ({self.get_status_display()})"


class MessageQueue(models.Model):
    STATUS_PENDING = 'pending'
    STATUS_SENT = 'sent'
    STATUS_DELIVERED = 'delivered'
    STATUS_READ = 'read'
    STATUS_FAILED = 'failed'
    STATUS_CHOICES = [
        (STATUS_PENDING, 'Pending'),
        (STATUS_SENT, 'Sent'),
        (STATUS_DELIVERED, 'Delivered'),
        (STATUS_READ, 'Read'),
        (STATUS_FAILED, 'Failed'),
    ]

    CHANNEL_WHATSAPP = 'whatsapp'
    CHANNEL_CHOICES = [
        (CHANNEL_WHATSAPP, 'WhatsApp'),
    ]

    EVENT_CREATED = 'created'
    EVENT_COMPLETED = 'completed'
    EVENT_DELIVERED = 'delivered'
    EVENT_ESTIMATE = 'estimate'
    EVENT_FEEDBACK = 'feedback'
    EVENT_DAILY_REPORT = 'daily_report'
    EVENT_MANUAL = 'manual'
    EVENT_BULK = 'bulk'
    EVENT_CHOICES = [
        (EVENT_CREATED, 'Created'),
        (EVENT_COMPLETED, 'Completed'),
        (EVENT_DELIVERED, 'Delivered'),
        (EVENT_ESTIMATE, 'Estimate'),
        (EVENT_FEEDBACK, 'Feedback'),
        (EVENT_DAILY_REPORT, 'Daily Report'),
        (EVENT_MANUAL, 'Manual'),
        (EVENT_BULK, 'Bulk Broadcast'),
    ]

    job_ticket = models.ForeignKey(
        JobTicket,
        on_delete=models.CASCADE,
        related_name='message_queues',
        null=True,
        blank=True,
    )
    bulk_campaign = models.ForeignKey(
        BulkCampaign,
        on_delete=models.SET_NULL,
        related_name='queued_messages',
        null=True,
        blank=True,
    )
    channel = models.CharField(max_length=20, choices=CHANNEL_CHOICES, default=CHANNEL_WHATSAPP, db_index=True)
    event_type = models.CharField(max_length=20, choices=EVENT_CHOICES, default=EVENT_MANUAL, db_index=True)
    target_phone = models.CharField(max_length=20)
    message = models.TextField(blank=True)
    pdf_url = models.URLField(blank=True)
    caption = models.TextField(blank=True)
    filename = models.CharField(max_length=255, blank=True, default='job-ticket.pdf')
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default=STATUS_PENDING, db_index=True)
    transport = models.CharField(max_length=50, blank=True, default='whatsapp-cloud-api')
    bridge_message_id = models.CharField(max_length=255, blank=True)
    bridge_status_code = models.PositiveIntegerField(null=True, blank=True)
    response_payload = models.JSONField(default=dict, blank=True)
    error_message = models.TextField(blank=True)
    retry_count = models.PositiveIntegerField(default=0)
    max_retries = models.PositiveIntegerField(default=3)
    next_retry_at = models.DateTimeField(null=True, blank=True)
    sent_at = models.DateTimeField(null=True, blank=True)
    failed_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-created_at']

    def __str__(self):
        label = self.job_ticket.job_code if self.job_ticket_id else self.target_phone
        return f"{label} - {self.event_type} - {self.status}"

    @property
    def is_document(self):
        return bool((self.pdf_url or '').strip())

    @property
    def delivery_state(self):
        if self.status == self.STATUS_FAILED:
            return 'failed'
        if self.status == self.STATUS_PENDING:
            return 'pending'
        payload_status = ''
        if isinstance(self.response_payload, dict):
            payload_status = str(self.response_payload.get('status') or '').strip().lower()
        if payload_status in ('read', 'delivered'):
            return payload_status
        return self.status


class UserSessionActivity(models.Model):
    STATUS_ACTIVE = 'active'
    STATUS_LOGGED_OUT = 'logged_out'
    STATUS_EXPIRED = 'expired'
    LOGOUT_REASON_NEW_LOGIN = 'new_login_elsewhere'
    STATUS_CHOICES = [
        (STATUS_ACTIVE, 'Active'),
        (STATUS_LOGGED_OUT, 'Logged Out'),
        (STATUS_EXPIRED, 'Expired'),
    ]

    CHANNEL_WEB = 'web'
    CHANNEL_ADMIN = 'admin'
    CHANNEL_API = 'api'
    CHANNEL_CHOICES = [
        (CHANNEL_WEB, 'Web'),
        (CHANNEL_ADMIN, 'Admin'),
        (CHANNEL_API, 'API'),
    ]

    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name='session_activities')
    session_key = models.CharField(max_length=64, unique=True, db_index=True)
    channel = models.CharField(max_length=20, choices=CHANNEL_CHOICES, default=CHANNEL_WEB)
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default=STATUS_ACTIVE, db_index=True)
    ip_address = models.GenericIPAddressField(null=True, blank=True)
    user_agent = models.TextField(blank=True)
    login_at = models.DateTimeField(default=timezone.now, db_index=True)
    last_activity_at = models.DateTimeField(default=timezone.now, db_index=True)
    last_activity_path = models.CharField(max_length=255, blank=True)
    expires_at = models.DateTimeField(null=True, blank=True, db_index=True)
    logout_at = models.DateTimeField(null=True, blank=True)
    logout_reason = models.CharField(max_length=40, blank=True)

    class Meta:
        ordering = ['-login_at']
        indexes = [
            models.Index(fields=['user', 'status']),
            models.Index(fields=['user', '-login_at']),
        ]
        verbose_name = 'User Session Activity'
        verbose_name_plural = 'User Session Activities'

    def __str__(self):
        return f"{self.user.username} - {self.get_status_display()} - {self.login_at:%Y-%m-%d %H:%M}"

    @property
    def device_label(self):
        user_agent = (self.user_agent or '').lower()
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

        if 'botgi mobile' in user_agent or 'botgi' in user_agent or self.channel == self.CHANNEL_API:
            if '(' in (self.user_agent or ''):
                prefix = self.user_agent.split('(')[0].strip()
                if prefix and prefix.lower() not in ['botgi mobile app', 'botgi mobile app (qr login)', 'botgi mobile']:
                    return f"{prefix} (Mobile App)"
            return f"Botgi Mobile App ({os_name})"

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

        return f"{browser} on {os_name}"


class Expense(models.Model):
    """Business expense tracking for both direct job costs and general operational overhead."""

    CATEGORY_RENT = 'rent'
    CATEGORY_UTILITIES = 'utilities'
    CATEGORY_REFRESHMENTS = 'refreshments'
    CATEGORY_TRAVEL = 'travel'
    CATEGORY_TOOLS_EQUIPMENT = 'tools_equipment'
    CATEGORY_JOB_PARTS_OUTSOURCE = 'job_parts_outsource'
    CATEGORY_SOFTWARE_INTERNET = 'software_internet'
    CATEGORY_OFFICE_SUPPLIES = 'office_supplies'
    CATEGORY_SALARY_ADVANCE = 'salary_advance'
    CATEGORY_MAINTENANCE = 'maintenance'
    CATEGORY_OTHER = 'other'

    CATEGORY_CHOICES = [
        (CATEGORY_RENT, 'Rent'),
        (CATEGORY_UTILITIES, 'Electricity & Utilities'),
        (CATEGORY_REFRESHMENTS, 'Tea, Snacks & Refreshments'),
        (CATEGORY_TRAVEL, 'Travel & Conveyance'),
        (CATEGORY_TOOLS_EQUIPMENT, 'Tools & Equipment'),
        (CATEGORY_JOB_PARTS_OUTSOURCE, 'Job Parts / External Lab / Outsourcing'),
        (CATEGORY_SOFTWARE_INTERNET, 'Internet & Software'),
        (CATEGORY_OFFICE_SUPPLIES, 'Office & Cleaning Supplies'),
        (CATEGORY_SALARY_ADVANCE, 'Staff Advance / Wages'),
        (CATEGORY_MAINTENANCE, 'Shop Maintenance & Repairs'),
        (CATEGORY_OTHER, 'Other Overhead'),
    ]

    PAYMENT_MODE_CASH = 'cash'
    PAYMENT_MODE_UPI = 'upi'
    PAYMENT_MODE_BANK = 'bank_transfer'
    PAYMENT_MODE_CARD = 'card'
    PAYMENT_MODE_PETTY_CASH = 'petty_cash'
    PAYMENT_MODE_OTHER = 'other'

    PAYMENT_MODE_CHOICES = [
        (PAYMENT_MODE_CASH, 'Cash'),
        (PAYMENT_MODE_UPI, 'UPI / GPay / PhonePe'),
        (PAYMENT_MODE_BANK, 'Bank Transfer / NEFT'),
        (PAYMENT_MODE_CARD, 'Debit / Credit Card'),
        (PAYMENT_MODE_PETTY_CASH, 'Petty Cash'),
        (PAYMENT_MODE_OTHER, 'Other'),
    ]

    workspace = models.ForeignKey(
        CompanyWorkspace,
        on_delete=models.CASCADE,
        related_name='expenses',
        null=True,
        blank=True,
        db_index=True,
    )
    expense_number = models.CharField(max_length=50, blank=True, db_index=True)
    title = models.CharField(max_length=200)
    amount = models.DecimalField(max_digits=12, decimal_places=2)
    category = models.CharField(max_length=50, choices=CATEGORY_CHOICES, default=CATEGORY_OTHER, db_index=True)
    payment_mode = models.CharField(max_length=30, choices=PAYMENT_MODE_CHOICES, default=PAYMENT_MODE_CASH)
    financial_account = models.ForeignKey(
        'FinancialAccount',
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='expenses',
        help_text="Financial account from which this expense was paid.",
    )
    expense_date = models.DateField(default=timezone.localdate, db_index=True)

    # Optional Link to Job Ticket
    job_ticket = models.ForeignKey(
        'JobTicket',
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='expenses',
        db_index=True,
    )
    is_billable_to_customer = models.BooleanField(
        default=False,
        help_text="Whether this expense is passed on or billed to the customer."
    )

    recorded_by = models.ForeignKey(
        User,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='recorded_expenses',
    )
    receipt_file = models.FileField(upload_to='expense_receipts/', null=True, blank=True)
    notes = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-expense_date', '-id']
        indexes = [
            models.Index(fields=['workspace', 'expense_date']),
            models.Index(fields=['workspace', 'category']),
            models.Index(fields=['job_ticket']),
        ]

    def __str__(self):
        return f"{self.expense_number or self.id} - {self.title} ({self.amount})"

    @property
    def is_job_related(self):
        return self.job_ticket_id is not None

    def save(self, *args, **kwargs):
        is_new = self._state.adding or not self.pk
        if self.financial_account:
            if self.financial_account.account_type == 'cash':
                self.payment_mode = self.PAYMENT_MODE_CASH
            elif self.financial_account.account_type == 'upi_wallet':
                self.payment_mode = self.PAYMENT_MODE_UPI
            elif self.financial_account.account_type == 'bank':
                if self.payment_mode not in [self.PAYMENT_MODE_BANK, self.PAYMENT_MODE_CARD]:
                    self.payment_mode = self.PAYMENT_MODE_BANK
        super().save(*args, **kwargs)
        if is_new and not self.expense_number:
            date_prefix = (self.expense_date or timezone.localdate()).strftime('%Y%m')
            self.expense_number = f"EXP-{date_prefix}-{self.id:04d}"
            Expense.objects.filter(pk=self.pk).update(expense_number=self.expense_number)


class FinancialAccount(models.Model):
    """
    Financial Account representing liquid cash drawers, bank accounts, or digital/UPI accounts.
    Workspace-scoped with live balance tracking, transaction ledger, and self-transfer support.
    """
    ACCOUNT_TYPE_CASH = 'cash'
    ACCOUNT_TYPE_BANK = 'bank'
    ACCOUNT_TYPE_UPI = 'upi_wallet'

    ACCOUNT_TYPE_CHOICES = [
        (ACCOUNT_TYPE_CASH, 'Cash Drawer / In-Hand'),
        (ACCOUNT_TYPE_BANK, 'Bank Account'),
        (ACCOUNT_TYPE_UPI, 'UPI QR / Digital Wallet'),
    ]

    workspace = models.ForeignKey(
        CompanyWorkspace,
        on_delete=models.CASCADE,
        related_name='financial_accounts',
        null=True,
        blank=True,
        db_index=True,
    )
    name = models.CharField(max_length=150, help_text="e.g. Cash in Hand, Federal Bank - Pattambi, Petty Cash")
    account_type = models.CharField(
        max_length=20,
        choices=ACCOUNT_TYPE_CHOICES,
        default=ACCOUNT_TYPE_CASH,
        db_index=True,
    )

    # Bank Details (used when account_type == 'bank' or 'upi_wallet')
    bank_name = models.CharField(max_length=150, blank=True)
    account_number = models.CharField(max_length=50, blank=True)
    ifsc_code = models.CharField(max_length=20, blank=True)
    branch = models.CharField(max_length=150, blank=True)
    upi_id = models.CharField(max_length=100, blank=True, help_text="UPI ID or VPA for payments")

    # Balances
    opening_balance = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal('0.00'))
    current_balance = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal('0.00'))

    # Default flags
    is_default_cash = models.BooleanField(
        default=False,
        help_text="Primary account for cash collections and cash expenses.",
    )
    is_default_bank = models.BooleanField(
        default=False,
        help_text="Primary account for UPI/bank collections, QR codes, and bank transfers.",
    )

    is_active = models.BooleanField(default=True)
    notes = models.TextField(blank=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-is_default_cash', '-is_default_bank', 'name']
        indexes = [
            models.Index(fields=['workspace', 'account_type']),
            models.Index(fields=['workspace', 'is_active']),
        ]

    def __str__(self):
        type_label = dict(self.ACCOUNT_TYPE_CHOICES).get(self.account_type, self.account_type)
        return f"{self.name} ({type_label}) - ₹{self.current_balance:,.2f}"

    def save(self, *args, **kwargs):
        is_new = self._state.adding or not self.pk
        if is_new and self.current_balance == Decimal('0.00') and self.opening_balance != Decimal('0.00'):
            self.current_balance = self.opening_balance

        super().save(*args, **kwargs)

        # Enforce single default cash and single default bank per workspace
        if self.is_default_cash and self.workspace:
            FinancialAccount.objects.filter(
                workspace=self.workspace,
                is_default_cash=True,
            ).exclude(pk=self.pk).update(is_default_cash=False)

        if self.is_default_bank and self.workspace:
            FinancialAccount.objects.filter(
                workspace=self.workspace,
                is_default_bank=True,
            ).exclude(pk=self.pk).update(is_default_bank=False)

            # Sync primary bank details to CompanyProfile for customer invoice display
            profile = CompanyProfile.objects.filter(workspace=self.workspace).first()
            if profile:
                fields_to_update = []
                if self.bank_name:
                    profile.bank_name = self.bank_name
                    fields_to_update.append('bank_name')
                if self.account_number:
                    profile.account_number = self.account_number
                    fields_to_update.append('account_number')
                if self.ifsc_code:
                    profile.ifsc_code = self.ifsc_code
                    fields_to_update.append('ifsc_code')
                if self.branch:
                    profile.branch = self.branch
                    fields_to_update.append('branch')
                if self.upi_id:
                    profile.upi_id = self.upi_id
                    fields_to_update.append('upi_id')
                if fields_to_update:
                    profile.save(update_fields=fields_to_update)

    def deposit(self, amount, description, reference_no='', transaction_date=None, job_ticket=None, inventory_credit_payment=None, user=None, txn_type='inflow'):
        """Deposit money into account and create ledger record atomically."""
        from django.db import transaction
        amount_dec = Decimal(str(amount))
        if amount_dec <= Decimal('0.00'):
            return None

        with transaction.atomic():
            acc = FinancialAccount.objects.select_for_update().get(pk=self.pk)
            acc.current_balance += amount_dec
            acc.save(update_fields=['current_balance', 'updated_at'])
            self.current_balance = acc.current_balance

            txn = AccountTransaction.objects.create(
                account=acc,
                transaction_type=txn_type,
                amount=amount_dec,
                balance_after=acc.current_balance,
                transaction_date=transaction_date or timezone.localdate(),
                description=description,
                reference_no=reference_no or '',
                job_ticket=job_ticket,
                inventory_credit_payment=inventory_credit_payment,
                created_by=user,
            )
            return txn

    def withdraw(self, amount, description, reference_no='', transaction_date=None, expense=None, vendor_payment=None, inventory_credit_payment=None, job_ticket=None, user=None, txn_type='outflow'):
        """Withdraw money from account and create ledger record atomically."""
        from django.db import transaction
        amount_dec = Decimal(str(amount))
        if amount_dec <= Decimal('0.00'):
            return None

        with transaction.atomic():
            acc = FinancialAccount.objects.select_for_update().get(pk=self.pk)
            acc.current_balance -= amount_dec
            acc.save(update_fields=['current_balance', 'updated_at'])
            self.current_balance = acc.current_balance

            txn = AccountTransaction.objects.create(
                account=acc,
                transaction_type=txn_type,
                amount=amount_dec,
                balance_after=acc.current_balance,
                transaction_date=transaction_date or timezone.localdate(),
                description=description,
                reference_no=reference_no or '',
                job_ticket=job_ticket,
                expense=expense,
                vendor_payment=vendor_payment,
                inventory_credit_payment=inventory_credit_payment,
                created_by=user,
            )
            return txn

    @classmethod
    def transfer(cls, from_account, to_account, amount, reference_no='', notes='', description='', transaction_date=None, user=None):
        """
        Perform an internal contra self-transfer between two accounts atomically.
        Creates dual counterpart AccountTransaction records.
        """
        from django.db import transaction
        amount_dec = Decimal(str(amount))
        if amount_dec <= Decimal('0.00'):
            raise ValueError("Transfer amount must be greater than zero.")
        if from_account.pk == to_account.pk:
            raise ValueError("Cannot transfer to the same account.")
        if from_account.workspace_id != to_account.workspace_id:
            raise ValueError("Accounts must belong to the same workspace.")

        txn_date = transaction_date or timezone.localdate()

        with transaction.atomic():
            first_id, second_id = sorted([from_account.pk, to_account.pk])
            locked = {acc.pk: acc for acc in cls.objects.select_for_update().filter(pk__in=[first_id, second_id])}
            from_acc = locked[from_account.pk]
            to_acc = locked[to_account.pk]

            from_acc.current_balance -= amount_dec
            from_acc.save(update_fields=['current_balance', 'updated_at'])

            to_acc.current_balance += amount_dec
            to_acc.save(update_fields=['current_balance', 'updated_at'])

            custom_desc = description or notes
            desc_out = custom_desc or f"Self-Transfer to {to_acc.name}"
            desc_in = custom_desc or f"Self-Transfer from {from_acc.name}"

            txn_out = AccountTransaction.objects.create(
                account=from_acc,
                transaction_type=AccountTransaction.TYPE_TRANSFER_OUT,
                amount=amount_dec,
                balance_after=from_acc.current_balance,
                transaction_date=txn_date,
                description=desc_out,
                reference_no=reference_no or '',
                created_by=user,
            )

            txn_in = AccountTransaction.objects.create(
                account=to_acc,
                transaction_type=AccountTransaction.TYPE_TRANSFER_IN,
                amount=amount_dec,
                balance_after=to_acc.current_balance,
                transaction_date=txn_date,
                description=desc_in,
                reference_no=reference_no or '',
                transfer_counterpart=txn_out,
                created_by=user,
            )

            txn_out.transfer_counterpart = txn_in
            txn_out.save(update_fields=['transfer_counterpart'])

            from_account.current_balance = from_acc.current_balance
            to_account.current_balance = to_acc.current_balance

            return txn_out, txn_in

    @classmethod
    def get_default_cash_account(cls, workspace):
        if not workspace:
            return None
        acc = cls.objects.filter(workspace=workspace, is_active=True, is_default_cash=True).first()
        if not acc:
            acc = cls.objects.filter(workspace=workspace, is_active=True, account_type=cls.ACCOUNT_TYPE_CASH).first()
        if not acc:
            cls.ensure_default_accounts(workspace)
            acc = cls.objects.filter(workspace=workspace, is_active=True, is_default_cash=True).first()
        return acc

    @classmethod
    def get_default_bank_account(cls, workspace):
        if not workspace:
            return None
        acc = cls.objects.filter(workspace=workspace, is_active=True, is_default_bank=True).first()
        if not acc:
            acc = cls.objects.filter(
                workspace=workspace,
                is_active=True,
                account_type__in=[cls.ACCOUNT_TYPE_BANK, cls.ACCOUNT_TYPE_UPI],
            ).first()
        if not acc:
            cls.ensure_default_accounts(workspace)
            acc = cls.objects.filter(workspace=workspace, is_active=True, is_default_bank=True).first()
        return acc

    @classmethod
    def get_default_for_payment_method(cls, workspace, payment_method):
        if not workspace:
            return None
        pm = (payment_method or '').lower().strip()
        if pm in ['cash', 'petty_cash']:
            return cls.get_default_cash_account(workspace)
        return cls.get_default_bank_account(workspace) or cls.get_default_cash_account(workspace)

    @classmethod
    def ensure_default_accounts(cls, workspace):
        """Ensure standard Cash in Hand and Bank accounts exist for a workspace."""
        if not workspace:
            return
        # Cash account
        has_cash = cls.objects.filter(workspace=workspace, account_type=cls.ACCOUNT_TYPE_CASH).exists()
        if not has_cash:
            cls.objects.create(
                workspace=workspace,
                name="Cash in Hand",
                account_type=cls.ACCOUNT_TYPE_CASH,
                opening_balance=Decimal('0.00'),
                current_balance=Decimal('0.00'),
                is_default_cash=True,
                is_active=True,
            )

        # Bank account
        has_bank = cls.objects.filter(workspace=workspace, account_type__in=[cls.ACCOUNT_TYPE_BANK, cls.ACCOUNT_TYPE_UPI]).exists()
        if not has_bank:
            profile = CompanyProfile.objects.filter(workspace=workspace).first()
            bank_name = (profile.bank_name if profile and profile.bank_name else "Federal Bank").strip()
            cls.objects.create(
                workspace=workspace,
                name=f"{bank_name} - Pattambi" if bank_name == "Federal Bank" else bank_name,
                account_type=cls.ACCOUNT_TYPE_BANK,
                bank_name=bank_name,
                account_number=profile.account_number if profile and profile.account_number else "99980108152444",
                ifsc_code=profile.ifsc_code if profile and profile.ifsc_code else "FDRL0001412",
                branch=profile.branch if profile and profile.branch else "Pattambi",
                upi_id=profile.upi_id if profile and profile.upi_id else "anasrawuthar@okaxis",
                opening_balance=Decimal('0.00'),
                current_balance=Decimal('0.00'),
                is_default_bank=True,
                is_active=True,
            )


class AccountTransaction(models.Model):
    """
    Detailed audit transaction ledger for a FinancialAccount.
    Tracks inflows, outflows, and self-transfer movements.
    """
    TYPE_INFLOW = 'inflow'
    TYPE_OUTFLOW = 'outflow'
    TYPE_TRANSFER_IN = 'transfer_in'
    TYPE_TRANSFER_OUT = 'transfer_out'

    TRANSACTION_TYPE_CHOICES = [
        (TYPE_INFLOW, 'Inflow (Receipt)'),
        (TYPE_OUTFLOW, 'Outflow (Expense/Payment)'),
        (TYPE_TRANSFER_IN, 'Self Transfer (Received)'),
        (TYPE_TRANSFER_OUT, 'Self Transfer (Sent)'),
    ]

    account = models.ForeignKey(
        FinancialAccount,
        on_delete=models.CASCADE,
        related_name='transactions',
        db_index=True,
    )
    transaction_type = models.CharField(
        max_length=20,
        choices=TRANSACTION_TYPE_CHOICES,
        db_index=True,
    )
    amount = models.DecimalField(max_digits=12, decimal_places=2)
    balance_after = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal('0.00'))
    transaction_date = models.DateField(default=timezone.localdate, db_index=True)
    description = models.CharField(max_length=255)
    reference_no = models.CharField(max_length=100, blank=True)

    job_ticket = models.ForeignKey(
        'JobTicket',
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='account_transactions',
        db_index=True,
    )
    expense = models.ForeignKey(
        'Expense',
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='account_transactions',
        db_index=True,
    )
    vendor_payment = models.ForeignKey(
        'VendorPayment',
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='account_transactions',
        db_index=True,
    )
    inventory_credit_payment = models.ForeignKey(
        'InventoryCreditPayment',
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='account_transactions',
        db_index=True,
    )
    transfer_counterpart = models.ForeignKey(
        'self',
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='counterparts',
    )
    created_by = models.ForeignKey(
        User,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='created_account_transactions',
    )

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-transaction_date', '-id']
        indexes = [
            models.Index(fields=['account', 'transaction_date']),
            models.Index(fields=['transaction_type', 'transaction_date']),
        ]

    def __str__(self):
        return f"{self.transaction_date} [{self.get_transaction_type_display()}] {self.account.name}: ₹{self.amount} (Bal: ₹{self.balance_after})"


class MobileAppRelease(models.Model):
    PLATFORM_ANDROID = 'android'
    PLATFORM_IOS = 'ios'
    PLATFORM_CHOICES = [
        (PLATFORM_ANDROID, 'Android (APK)'),
        (PLATFORM_IOS, 'iOS'),
    ]

    workspace = models.ForeignKey(
        'CompanyWorkspace',
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='app_releases',
    )
    platform = models.CharField(max_length=20, choices=PLATFORM_CHOICES, default=PLATFORM_ANDROID)
    version_name = models.CharField(max_length=50, help_text="e.g. 1.2.0")
    version_code = models.PositiveIntegerField(help_text="Numeric build number, e.g. 12")
    apk_file = models.FileField(upload_to='app_releases/', blank=True, null=True)
    download_url = models.URLField(max_length=500, blank=True, help_text="External download URL if APK not uploaded directly.")
    release_notes = models.TextField(blank=True, help_text="What's new in this version.")
    min_version_code = models.PositiveIntegerField(default=1, help_text="Older builds below this will be forced to update.")
    is_active = models.BooleanField(default=True, db_index=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    uploaded_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True, related_name='uploaded_app_releases')

    class Meta:
        ordering = ['-version_code', '-created_at']

    def __str__(self):
        return f"{self.get_platform_display()} v{self.version_name} ({self.version_code})"

    @property
    def file_size_display(self):
        try:
            if self.apk_file and hasattr(self.apk_file, 'size') and self.apk_file.size:
                size_mb = self.apk_file.size / (1024 * 1024)
                return f"{size_mb:.1f} MB"
        except Exception:
            pass
        return "N/A"

    def get_download_url(self, request=None):
        if self.apk_file:
            try:
                url = self.apk_file.url
                if request:
                    return request.build_absolute_uri(url)
                return url
            except Exception:
                pass
        return self.download_url or ''


class MobileQrLoginToken(models.Model):
    token = models.CharField(max_length=64, unique=True, db_index=True)
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name='qr_login_tokens')
    created_at = models.DateTimeField(auto_now_add=True)
    expires_at = models.DateTimeField(db_index=True)
    is_used = models.BooleanField(default=False, db_index=True)
    used_at = models.DateTimeField(null=True, blank=True)
    device_info = models.CharField(max_length=255, blank=True)
    ip_address = models.GenericIPAddressField(null=True, blank=True)

    class Meta:
        ordering = ['-created_at']

    def __str__(self):
        return f"QR Login Token for {self.user.username} ({'Used' if self.is_used else 'Pending'})"

    @property
    def is_valid(self):
        return (not self.is_used) and (self.expires_at > timezone.now())



