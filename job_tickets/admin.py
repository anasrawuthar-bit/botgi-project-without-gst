import re
from collections import defaultdict
from decimal import Decimal

from django import forms
from django.contrib import admin, messages
from django.contrib.auth.admin import GroupAdmin as DjangoGroupAdmin
from django.contrib.auth.admin import UserAdmin as DjangoUserAdmin
from django.contrib.auth.models import Group, User
from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import Count, DecimalField, IntegerField, OuterRef, Q, Subquery, Sum, Value
from django.db.models.functions import Coalesce

from .admin_roles import ROLE_READ_ONLY, ROLE_STAFF_ADMIN, ROLE_SUPER_ADMIN
from .forms import CompanyProfileForm, InventoryPartyForm
from .gst_utils import normalize_compact_code, normalize_text_code
from .models import (
    Assignment,
    Client,
    CompanyUserMembership,
    CompanyWorkspace,
    CompanyProfile,
    DailyJobCodeSequence,
    DeviceChecklistField,
    DeviceChecklistTemplate,
    InventoryBill,
    InventoryBillLog,
    InventoryCreditPayment,
    InventoryEntry,
    InventoryParty,
    InventoryVendorCredit,
    InventoryVendorCreditApplication,
    JobFieldPreset,
    MessageQueue,
    JobTicket,
    JobTicketPhoto,
    JobTicketLog,
    PlatformSettings,
    ProductSale,
    Product,
    ServiceLog,
    SpecializedService,
    TechnicianProfile,
    UserSessionActivity,
    Vendor,
    VendorPayment,
    WhatsAppIntegrationSettings,
    WhatsAppNotificationLog,
)

PROTECTED_JOB_DELETE_STATUSES = {'Completed', 'Ready for Pickup', 'Closed'}
LOCKED_JOB_STATUSES = {'Ready for Pickup', 'Closed'}


def _has_group(user, group_name):
    return user.groups.filter(name=group_name).exists()


def is_super_admin_user(user):
    return user.is_superuser or _has_group(user, ROLE_SUPER_ADMIN)


def is_staff_admin_user(user):
    return _has_group(user, ROLE_STAFF_ADMIN)


def is_read_only_user(user):
    return _has_group(user, ROLE_READ_ONLY)


def is_allowed_admin_user(user):
    if not user.is_authenticated or not user.is_active or not user.is_staff:
        return False
    return is_super_admin_user(user) or is_staff_admin_user(user) or is_read_only_user(user)


def is_job_locked(job):
    return job.status in LOCKED_JOB_STATUSES or bool(job.vyapar_invoice_number)


def is_job_delete_protected(job):
    return (
        bool(job.vyapar_invoice_number)
        or job.status in PROTECTED_JOB_DELETE_STATUSES
        or job.service_logs.exists()
    )


def _reverse_inventory_entries(entries):
    entry_ids = [entry.id for entry in entries if getattr(entry, 'id', None)]
    if not entry_ids:
        return []

    with transaction.atomic():
        locked_entries = list(
            InventoryEntry.objects.select_for_update()
            .filter(pk__in=entry_ids)
            .order_by('id')
        )
        if not locked_entries:
            return []

        locked_products = {
            product.id: product
            for product in Product.objects.select_for_update().filter(
                pk__in={entry.product_id for entry in locked_entries}
            )
        }

        product_effects = defaultdict(int)
        entry_numbers_by_product = defaultdict(list)
        for entry in locked_entries:
            product_effects[entry.product_id] += entry.stock_effect
            entry_numbers_by_product[entry.product_id].append(entry.entry_number)

        blocked_messages = []
        for product_id, net_effect in product_effects.items():
            product = locked_products.get(product_id)
            if not product:
                blocked_messages.append(
                    f"missing product for entries {', '.join(entry_numbers_by_product[product_id])}"
                )
                continue

            restored_stock = int(product.stock_quantity or 0) - int(net_effect or 0)
            if restored_stock < 0:
                blocked_messages.append(
                    f"{product.name} ({', '.join(entry_numbers_by_product[product_id])})"
                )
                continue

            product.stock_quantity = restored_stock

        if blocked_messages:
            raise ValidationError(
                "Cannot delete inventory entries because stock reconciliation failed for: "
                + ", ".join(blocked_messages)
            )

        for product in locked_products.values():
            product.save(update_fields=['stock_quantity', 'updated_at'])

        affected_bill_ids = {entry.bill_id for entry in locked_entries if entry.bill_id}
        InventoryEntry.objects.filter(pk__in=[entry.id for entry in locked_entries]).delete()

        if affected_bill_ids:
            InventoryBill.objects.filter(pk__in=affected_bill_ids, lines__isnull=True).delete()

        return locked_entries


def _delete_job_tickets_with_cleanup(job_ids):
    normalized_job_ids = [job_id for job_id in job_ids if job_id]
    if not normalized_job_ids:
        return

    with transaction.atomic():
        linked_entries = list(
            InventoryEntry.objects.filter(job_ticket_id__in=normalized_job_ids).only('id')
        )
        if linked_entries:
            _reverse_inventory_entries(linked_entries)

        for photo in JobTicketPhoto.objects.filter(job_ticket_id__in=normalized_job_ids):
            if photo.image:
                photo.image.delete(save=False)

        JobTicket.objects.filter(pk__in=normalized_job_ids).delete()


class RoleBasedAdminMixin:
    """Role control for Django admin models."""

    staff_can_add = True
    staff_can_change = True
    staff_can_delete = False

    class Media:
        css = {'all': ('admin/css/custom_admin.css',)}

    def has_module_permission(self, request):
        return is_allowed_admin_user(request.user)

    def has_view_permission(self, request, obj=None):
        return is_allowed_admin_user(request.user)

    def has_add_permission(self, request):
        user = request.user
        if is_super_admin_user(user):
            return True
        if is_staff_admin_user(user):
            return self.staff_can_add
        return False

    def has_change_permission(self, request, obj=None):
        user = request.user
        if is_super_admin_user(user):
            return True
        if is_staff_admin_user(user):
            return self.staff_can_change
        return False

    def has_delete_permission(self, request, obj=None):
        user = request.user
        if is_super_admin_user(user):
            return True
        if is_staff_admin_user(user):
            return self.staff_can_delete
        return False


class SuperAdminOnlyMixin(RoleBasedAdminMixin):
    """Only super admins can access this admin model."""

    def has_module_permission(self, request):
        return request.user.is_active and request.user.is_staff and is_super_admin_user(request.user)

    def has_view_permission(self, request, obj=None):
        return request.user.is_active and request.user.is_staff and is_super_admin_user(request.user)

    def has_add_permission(self, request):
        return request.user.is_active and request.user.is_staff and is_super_admin_user(request.user)

    def has_change_permission(self, request, obj=None):
        return request.user.is_active and request.user.is_staff and is_super_admin_user(request.user)

    def has_delete_permission(self, request, obj=None):
        return request.user.is_active and request.user.is_staff and is_super_admin_user(request.user)


class ImmutableAuditAdminMixin(RoleBasedAdminMixin):
    """Read-only admin for audit models."""

    staff_can_add = False
    staff_can_change = False
    staff_can_delete = False
    actions = None

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


class ClientAdminForm(forms.ModelForm):
    class Meta:
        model = Client
        fields = '__all__'

    def clean_phone(self):
        raw_phone = (self.cleaned_data.get('phone') or '').strip()
        normalized_phone = re.sub(r'\s+', '', raw_phone)

        if not re.fullmatch(r'^\+?\d{8,15}$', normalized_phone):
            raise ValidationError("Phone must contain 8 to 15 digits (optional '+' prefix).")

        duplicate_qs = Client.objects.filter(phone=normalized_phone)
        if self.instance.pk:
            duplicate_qs = duplicate_qs.exclude(pk=self.instance.pk)
        if duplicate_qs.exists():
            raise ValidationError("A client with this phone already exists.")

        return normalized_phone


class ProductAdminForm(forms.ModelForm):
    class Meta:
        model = Product
        fields = '__all__'

    def clean_hsn_sac_code(self):
        return normalize_compact_code(self.cleaned_data.get('hsn_sac_code'))

    def clean_uqc(self):
        return normalize_text_code(self.cleaned_data.get('uqc'))

    def clean_unit_price(self):
        unit_price = self.cleaned_data.get('unit_price')
        if unit_price is not None and unit_price < 0:
            raise ValidationError("Unit price cannot be negative.")
        return unit_price

    def clean_cost_price(self):
        cost_price = self.cleaned_data.get('cost_price')
        if cost_price is not None and cost_price < 0:
            raise ValidationError("Cost price cannot be negative.")
        return cost_price

    def clean_gst_rate(self):
        gst_rate = self.cleaned_data.get('gst_rate')
        if gst_rate is not None and gst_rate < 0:
            raise ValidationError("GST rate cannot be negative.")
        return gst_rate

    def clean_cess_rate(self):
        cess_rate = self.cleaned_data.get('cess_rate')
        if cess_rate is not None and cess_rate < 0:
            raise ValidationError("Cess rate cannot be negative.")
        return cess_rate

    def clean(self):
        cleaned_data = super().clean()
        tax_category = cleaned_data.get('tax_category') or 'taxable'
        gst_rate = cleaned_data.get('gst_rate') or Decimal('0.00')
        cess_rate = cleaned_data.get('cess_rate') or Decimal('0.00')

        if tax_category != 'taxable':
            if gst_rate > 0:
                self.add_error('gst_rate', "GST rate must be 0 for exempt, nil-rated, or non-GST items.")
            if cess_rate > 0:
                self.add_error('cess_rate', "Cess rate must be 0 for exempt, nil-rated, or non-GST items.")

        return cleaned_data


class InventoryPartyAdminForm(InventoryPartyForm):
    class Meta(InventoryPartyForm.Meta):
        model = InventoryParty
        fields = '__all__'


class CompanyProfileAdminForm(CompanyProfileForm):
    class Meta(CompanyProfileForm.Meta):
        model = CompanyProfile
        fields = '__all__'


class JobTicketAdminForm(forms.ModelForm):
    class Meta:
        model = JobTicket
        fields = '__all__'

    def clean_discount_amount(self):
        discount = self.cleaned_data.get('discount_amount')
        if discount is not None and discount < 0:
            raise ValidationError("Discount cannot be negative.")
        return discount

    def clean_estimated_amount(self):
        amount = self.cleaned_data.get('estimated_amount')
        if amount is not None and amount < 0:
            raise ValidationError("Estimated amount cannot be negative.")
        return amount


@admin.register(JobTicket)
class JobTicketAdmin(RoleBasedAdminMixin, admin.ModelAdmin):
    form = JobTicketAdminForm
    list_display = (
        'job_code',
        'customer_name',
        'customer_phone',
        'device_type',
        'status',
        'assigned_technician',
        'invoice_number',
        'parts_total',
        'service_total',
        'grand_total',
        'updated_at',
    )
    list_filter = (
        'status',
        'is_under_warranty',
        'assigned_to',
        'created_at',
        'updated_at',
    )
    search_fields = (
        'job_code',
        'customer_name',
        'customer_phone',
        'device_type',
        'device_brand',
        'device_model',
        'device_serial',
        'vyapar_invoice_number',
    )
    date_hierarchy = 'created_at'
    list_per_page = 50
    raw_id_fields = ('assigned_to', 'created_by', 'original_job_ticket')
    readonly_fields = (
        'job_code',
        'parts_total',
        'service_total',
        'grand_total',
        'billing_lock_status',
        'created_at',
        'updated_at',
    )
    fieldsets = (
        ("Ticket Identity", {'fields': ('job_code', 'status', 'billing_lock_status')}),
        ("Client", {'fields': ('customer_name', 'customer_phone')}),
        (
            "Device",
            {'fields': (
                'device_type',
                'device_brand',
                'device_model',
                'device_serial',
                'reported_issue',
                'additional_items',
            )},
        ),
        (
            "Operations",
            {'fields': (
                'assigned_to',
                'is_under_warranty',
                'estimated_amount',
                'estimated_delivery',
                'discount_amount',
                'vyapar_invoice_number',
            )},
        ),
        (
            "Reference",
            {'fields': ('original_job_ticket', 'created_by', 'customer_group_id', 'technician_notes')},
        ),
        ("Audit", {'fields': ('parts_total', 'service_total', 'grand_total', 'created_at', 'updated_at')}),
    )
    actions = ('mark_selected_ready_for_pickup', 'mark_selected_closed', 'reopen_selected_jobs')

    def get_queryset(self, request):
        money_field = DecimalField(max_digits=12, decimal_places=2)
        return (
            super()
            .get_queryset(request)
            .select_related('assigned_to__user', 'created_by')
            .annotate(
                parts_total_sum=Coalesce(
                    Sum('service_logs__part_cost', output_field=money_field),
                    Value(Decimal('0.00'), output_field=money_field),
                    output_field=money_field,
                ),
                service_total_sum=Coalesce(
                    Sum('service_logs__service_charge', output_field=money_field),
                    Value(Decimal('0.00'), output_field=money_field),
                    output_field=money_field,
                ),
            )
        )

    def get_readonly_fields(self, request, obj=None):
        readonly = list(super().get_readonly_fields(request, obj))
        if obj is None:
            readonly = [field for field in readonly if field not in {'parts_total', 'service_total', 'grand_total', 'billing_lock_status'}]
        if obj and is_job_locked(obj):
            readonly.extend(
                [
                    'customer_name',
                    'customer_phone',
                    'device_type',
                    'device_brand',
                    'device_model',
                    'device_serial',
                    'reported_issue',
                    'additional_items',
                    'assigned_to',
                    'estimated_amount',
                    'estimated_delivery',
                    'discount_amount',
                    'is_under_warranty',
                    'original_job_ticket',
                    'technician_notes',
                ]
            )
        return tuple(dict.fromkeys(readonly))

    @admin.display(description="Technician", ordering='assigned_to__user__username')
    def assigned_technician(self, obj):
        if obj.assigned_to and obj.assigned_to.user:
            return obj.assigned_to.user.username
        return "-"

    @admin.display(description="Invoice No")
    def invoice_number(self, obj):
        return obj.vyapar_invoice_number or "-"

    @admin.display(description="Parts Total (Rs)", ordering='parts_total_sum')
    def parts_total(self, obj):
        value = getattr(obj, 'parts_total_sum', Decimal('0.00')) or Decimal('0.00')
        return f"{value:.2f}"

    @admin.display(description="Service Total (Rs)", ordering='service_total_sum')
    def service_total(self, obj):
        value = getattr(obj, 'service_total_sum', Decimal('0.00')) or Decimal('0.00')
        return f"{value:.2f}"

    @admin.display(description="Grand Total (Rs)")
    def grand_total(self, obj):
        if obj is None:
            return "0.00"
        parts = getattr(obj, 'parts_total_sum', Decimal('0.00')) or Decimal('0.00')
        service = getattr(obj, 'service_total_sum', Decimal('0.00')) or Decimal('0.00')
        return f"{(parts + service - (obj.discount_amount or Decimal('0.00'))):.2f}"

    @admin.display(description="Billing Lock")
    def billing_lock_status(self, obj):
        if obj is None:
            return "Open"
        if not obj.pk:
            return "Open"
        if obj.vyapar_invoice_number:
            return f"Locked - Invoice: {obj.vyapar_invoice_number}"
        if obj.status in LOCKED_JOB_STATUSES:
            return f"Locked - Status: {obj.status}"
        return "Open"

    def save_model(self, request, obj, form, change):
        if not change and not obj.created_by_id:
            obj.created_by = request.user
        super().save_model(request, obj, form, change)

    def has_delete_permission(self, request, obj=None):
        return super().has_delete_permission(request, obj)

    def get_actions(self, request):
        actions = super().get_actions(request)
        if not is_super_admin_user(request.user):
            actions.pop('delete_selected', None)
        return actions

    def delete_model(self, request, obj):
        _delete_job_tickets_with_cleanup([obj.pk])

    def delete_queryset(self, request, queryset):
        _delete_job_tickets_with_cleanup(queryset.values_list('id', flat=True))

    @admin.action(description="Mark selected jobs as Ready for Pickup")
    def mark_selected_ready_for_pickup(self, request, queryset):
        updated = 0
        skipped = 0
        for job in queryset:
            if job.status == 'Completed':
                job.status = 'Ready for Pickup'
                job.save(update_fields=['status', 'updated_at'])
                updated += 1
            else:
                skipped += 1
        if updated:
            self.message_user(request, f"{updated} job(s) moved to Ready for Pickup.", level=messages.SUCCESS)
        if skipped:
            self.message_user(request, f"{skipped} job(s) skipped due to status mismatch.", level=messages.WARNING)

    @admin.action(description="Mark selected jobs as Closed")
    def mark_selected_closed(self, request, queryset):
        updated = 0
        skipped = 0
        for job in queryset:
            if job.status in {'Ready for Pickup', 'Returned'}:
                job.status = 'Closed'
                job.save(update_fields=['status', 'updated_at'])
                updated += 1
            else:
                skipped += 1
        if updated:
            self.message_user(request, f"{updated} job(s) marked as Closed.", level=messages.SUCCESS)
        if skipped:
            self.message_user(request, f"{skipped} job(s) skipped due to status mismatch.", level=messages.WARNING)

    @admin.action(description="Reopen selected jobs to Pending")
    def reopen_selected_jobs(self, request, queryset):
        updated = 0
        skipped = 0
        for job in queryset:
            if job.status in {'Pending', 'Under Inspection', 'Repairing', 'Specialized Service'}:
                skipped += 1
                continue
            if job.vyapar_invoice_number:
                skipped += 1
                continue
            job.status = 'Pending'
            job.save(update_fields=['status', 'updated_at'])
            updated += 1
        if updated:
            self.message_user(request, f"{updated} job(s) reopened to Pending.", level=messages.SUCCESS)
        if skipped:
            self.message_user(request, f"{skipped} job(s) skipped (already active or billed).", level=messages.WARNING)


@admin.register(ServiceLog)
class ServiceLogAdmin(RoleBasedAdminMixin, admin.ModelAdmin):
    staff_can_add = False
    staff_can_change = False
    staff_can_delete = False

    list_display = (
        'job_code',
        'description',
        'part_cost',
        'service_charge',
        'sales_invoice_number',
        'created_at',
    )
    search_fields = (
        'job_ticket__job_code',
        'job_ticket__customer_phone',
        'description',
        'sales_invoice_number',
    )
    list_filter = ('created_at',)
    raw_id_fields = ('job_ticket',)
    readonly_fields = ('created_at',)
    list_select_related = ('job_ticket',)
    list_per_page = 75

    @admin.display(description="Job Code", ordering='job_ticket__job_code')
    def job_code(self, obj):
        return obj.job_ticket.job_code

    def has_change_permission(self, request, obj=None):
        if obj and is_job_locked(obj.job_ticket):
            return False
        return super().has_change_permission(request, obj)

    def has_delete_permission(self, request, obj=None):
        if obj and is_job_locked(obj.job_ticket):
            return False
        return super().has_delete_permission(request, obj)

    def get_actions(self, request):
        actions = super().get_actions(request)
        actions.pop('delete_selected', None)
        return actions


@admin.register(Client)
class ClientAdmin(RoleBasedAdminMixin, admin.ModelAdmin):
    form = ClientAdminForm
    list_display = ('phone', 'name', 'jobs_count', 'latest_device', 'created_at', 'updated_at')
    search_fields = ('phone', 'name', 'address', 'notes')
    ordering = ('phone',)
    list_filter = ('created_at', 'updated_at')
    readonly_fields = ('created_at', 'updated_at', 'jobs_count', 'latest_device')
    list_per_page = 75

    def get_queryset(self, request):
        jobs_count_subquery = (
            JobTicket.objects.filter(customer_phone=OuterRef('phone'))
            .values('customer_phone')
            .annotate(total=Count('id'))
            .values('total')[:1]
        )
        latest_device_subquery = (
            JobTicket.objects.filter(customer_phone=OuterRef('phone'))
            .order_by('-created_at')
            .values('device_type')[:1]
        )
        return super().get_queryset(request).annotate(
            jobs_count_value=Coalesce(
                Subquery(jobs_count_subquery, output_field=IntegerField()),
                Value(0),
                output_field=IntegerField(),
            ),
            latest_device_value=Coalesce(
                Subquery(latest_device_subquery),
                Value('-'),
            ),
        )

    @admin.display(description="Jobs", ordering='jobs_count_value')
    def jobs_count(self, obj):
        return getattr(obj, 'jobs_count_value', 0)

    @admin.display(description="Latest Device", ordering='latest_device_value')
    def latest_device(self, obj):
        return getattr(obj, 'latest_device_value', '-') or '-'

    def has_delete_permission(self, request, obj=None):
        if obj and JobTicket.objects.filter(customer_phone=obj.phone).exists():
            return False
        return super().has_delete_permission(request, obj)

    def get_actions(self, request):
        actions = super().get_actions(request)
        actions.pop('delete_selected', None)
        return actions

    def delete_queryset(self, request, queryset):
        protected_phones = []
        deletable_ids = []
        for client in queryset:
            if JobTicket.objects.filter(customer_phone=client.phone).exists():
                protected_phones.append(client.phone)
            else:
                deletable_ids.append(client.id)

        if deletable_ids:
            super().delete_queryset(request, queryset.filter(id__in=deletable_ids))
        if protected_phones:
            self.message_user(
                request,
                f"Skipped clients with service history: {', '.join(protected_phones)}",
                level=messages.ERROR,
            )


@admin.register(JobFieldPreset)
class JobFieldPresetAdmin(RoleBasedAdminMixin, admin.ModelAdmin):
    list_display = ('field_name', 'value', 'sort_order', 'is_active', 'updated_at')
    list_filter = ('field_name', 'is_active')
    search_fields = ('value',)
    ordering = ('field_name', 'sort_order', 'value')
    list_per_page = 100


class DeviceChecklistFieldInline(admin.TabularInline):
    model = DeviceChecklistField
    extra = 1
    fields = (
        'sort_order',
        'field_key',
        'label',
        'field_type',
        'is_required',
        'placeholder',
        'help_text',
        'options',
        'is_active',
    )
    ordering = ('sort_order', 'label')


@admin.register(DeviceChecklistTemplate)
class DeviceChecklistTemplateAdmin(RoleBasedAdminMixin, admin.ModelAdmin):
    list_display = ('device_type', 'name', 'is_active', 'updated_at')
    list_filter = ('is_active',)
    search_fields = ('device_type', 'name', 'notes')
    ordering = ('device_type',)
    inlines = [DeviceChecklistFieldInline]
    list_per_page = 100


@admin.register(Product)
class ProductAdmin(RoleBasedAdminMixin, admin.ModelAdmin):
    form = ProductAdminForm
    list_display = (
        'name',
        'item_type',
        'hsn_sac_code',
        'tax_category',
        'gst_rate',
        'sku',
        'category',
        'brand',
        'unit_price',
        'cost_price',
        'stock_quantity',
        'stock_status',
        'updated_at',
    )
    search_fields = ('name', 'sku', 'category', 'brand', 'hsn_sac_code', 'uqc', 'description')
    list_filter = ('item_type', 'tax_category', 'category', 'brand', 'updated_at')
    ordering = ('name',)
    readonly_fields = ('created_at', 'updated_at')
    list_per_page = 75

    @admin.display(description="Stock Status")
    def stock_status(self, obj):
        if obj.stock_quantity <= 0:
            return "Out of Stock"
        if obj.stock_quantity <= 5:
            return "Low Stock"
        return "In Stock"

    def _has_sales_history(self, product):
        return ProductSale.objects.filter(product=product).exists()

    def has_delete_permission(self, request, obj=None):
        if obj and (obj.stock_quantity > 0 or self._has_sales_history(obj)):
            return False
        return super().has_delete_permission(request, obj)

    def get_actions(self, request):
        actions = super().get_actions(request)
        actions.pop('delete_selected', None)
        return actions

    def delete_queryset(self, request, queryset):
        blocked = []
        deletable_ids = []
        for product in queryset:
            if product.stock_quantity > 0 or self._has_sales_history(product):
                blocked.append(product.name)
            else:
                deletable_ids.append(product.id)

        if deletable_ids:
            super().delete_queryset(request, queryset.filter(id__in=deletable_ids))
        if blocked:
            self.message_user(
                request,
                f"Skipped products with stock or sales history: {', '.join(blocked)}",
                level=messages.ERROR,
            )


@admin.register(ProductSale)
class ProductSaleAdmin(ImmutableAuditAdminMixin, admin.ModelAdmin):
    list_display = (
        'job_code',
        'product',
        'quantity',
        'unit_price',
        'cost_price',
        'line_total',
        'line_cost',
        'line_profit',
        'sold_at',
        'sold_by',
    )
    search_fields = (
        'job_ticket__job_code',
        'job_ticket__customer_phone',
        'product__name',
        'product__sku',
    )
    list_filter = ('sold_at', 'product__category')
    raw_id_fields = ('job_ticket', 'product', 'service_log', 'sold_by')
    readonly_fields = (
        'job_ticket',
        'product',
        'service_log',
        'quantity',
        'unit_price',
        'cost_price',
        'line_total',
        'line_cost',
        'line_profit',
        'sold_at',
        'sold_by',
    )
    list_select_related = ('job_ticket', 'product', 'sold_by')
    list_per_page = 100

    @admin.display(description='Job Code', ordering='job_ticket__job_code')
    def job_code(self, obj):
        return obj.job_ticket.job_code


@admin.register(Vendor)
class VendorAdmin(RoleBasedAdminMixin, admin.ModelAdmin):
    list_display = ('company_name', 'name', 'phone', 'email', 'active_jobs_count', 'created_at')
    search_fields = ('company_name', 'name', 'phone', 'email', 'specialties')
    list_filter = ('created_at',)
    ordering = ('company_name',)
    readonly_fields = ('created_at', 'active_jobs_count')
    list_per_page = 75

    def get_queryset(self, request):
        return super().get_queryset(request).annotate(
            active_jobs_count_value=Count('services', filter=Q(services__status='Sent to Vendor'))
        )

    @admin.display(description="Active Jobs", ordering='active_jobs_count_value')
    def active_jobs_count(self, obj):
        return getattr(obj, 'active_jobs_count_value', 0)

    def has_delete_permission(self, request, obj=None):
        if obj and SpecializedService.objects.filter(vendor=obj, status='Sent to Vendor').exists():
            return False
        return super().has_delete_permission(request, obj)

    def get_actions(self, request):
        actions = super().get_actions(request)
        actions.pop('delete_selected', None)
        return actions


@admin.register(SpecializedService)
class SpecializedServiceAdmin(RoleBasedAdminMixin, admin.ModelAdmin):
    staff_can_delete = False

    list_display = (
        'job_ticket',
        'vendor',
        'status',
        'vendor_cost',
        'vendor_discount_amount',
        'vendor_paid_amount',
        'vendor_balance_amount',
        'client_charge',
        'sent_date',
        'returned_date',
    )
    list_filter = ('status', 'vendor', 'sent_date', 'returned_date')
    search_fields = ('job_ticket__job_code', 'job_ticket__customer_phone', 'vendor__company_name')
    raw_id_fields = ('job_ticket', 'vendor')
    readonly_fields = ('sent_date', 'returned_date')
    list_select_related = ('job_ticket', 'vendor')
    list_per_page = 75

    def get_actions(self, request):
        actions = super().get_actions(request)
        actions.pop('delete_selected', None)
        return actions


@admin.register(VendorPayment)
class VendorPaymentAdmin(RoleBasedAdminMixin, admin.ModelAdmin):
    staff_can_delete = False

    list_display = (
        'vendor',
        'specialized_service',
        'payment_date',
        'payment_method',
        'financial_account',
        'amount',
        'balance_before',
        'balance_after',
        'created_by',
        'created_at',
    )
    list_filter = ('payment_method', 'financial_account', 'payment_date', 'vendor')
    search_fields = (
        'vendor__company_name',
        'specialized_service__job_ticket__job_code',
        'reference_no',
        'notes',
    )
    raw_id_fields = ('vendor', 'specialized_service', 'financial_account', 'created_by')
    readonly_fields = ('created_at',)
    list_select_related = ('vendor', 'specialized_service__job_ticket', 'financial_account', 'created_by')
    list_per_page = 100

    def get_actions(self, request):
        actions = super().get_actions(request)
        actions.pop('delete_selected', None)
        return actions


@admin.register(Assignment)
class AssignmentAdmin(RoleBasedAdminMixin, admin.ModelAdmin):
    staff_can_add = False
    staff_can_change = False
    staff_can_delete = False

    list_display = ('job', 'technician', 'status', 'created_at', 'responded_at')
    list_filter = ('status', 'created_at', 'responded_at')
    search_fields = ('job__job_code', 'technician__user__username', 'response_note')
    raw_id_fields = ('job', 'technician')
    readonly_fields = ('created_at', 'responded_at')
    list_select_related = ('job', 'technician__user')
    list_per_page = 75

    def get_actions(self, request):
        actions = super().get_actions(request)
        actions.pop('delete_selected', None)
        return actions


@admin.register(TechnicianProfile)
class TechnicianProfileAdmin(SuperAdminOnlyMixin, admin.ModelAdmin):
    list_display = ('user', 'unique_id')
    search_fields = ('user__username', 'unique_id')
    list_select_related = ('user',)
    list_per_page = 75

    def has_delete_permission(self, request, obj=None):
        if obj and (JobTicket.objects.filter(assigned_to=obj).exists() or Assignment.objects.filter(technician=obj).exists()):
            return False
        return super().has_delete_permission(request, obj)

    def get_actions(self, request):
        actions = super().get_actions(request)
        actions.pop('delete_selected', None)
        return actions


@admin.register(JobTicketLog)
class JobTicketLogAdmin(ImmutableAuditAdminMixin, admin.ModelAdmin):
    list_display = ('job_ticket', 'action', 'user', 'timestamp', 'details')
    search_fields = ('job_ticket__job_code', 'action', 'details', 'user__username')
    list_filter = ('action', 'timestamp')
    raw_id_fields = ('job_ticket', 'user')
    readonly_fields = ('job_ticket', 'action', 'user', 'timestamp', 'details')
    list_select_related = ('job_ticket', 'user')
    list_per_page = 100


@admin.register(DailyJobCodeSequence)
class DailyJobCodeSequenceAdmin(ImmutableAuditAdminMixin, admin.ModelAdmin):
    list_display = ('date', 'last_counter')
    ordering = ('-date',)
    readonly_fields = ('date', 'last_counter')
    list_per_page = 100


@admin.register(CompanyProfile)
class CompanyProfileAdmin(RoleBasedAdminMixin, admin.ModelAdmin):
    form = CompanyProfileAdminForm
    staff_can_add = False
    staff_can_delete = False

    list_display = (
        'company_name',
        'legal_name',
        'gstin',
        'registration_type',
        'filing_frequency',
        'enable_gst',
        'e_invoice_applicable',
    )
    search_fields = ('company_name', 'legal_name', 'gstin', 'phone1', 'email', 'website')
    fieldsets = (
        ("Brand", {'fields': ('company_name', 'legal_name', 'tagline', 'logo', 'logo_url')}),
        (
            "Contact",
            {'fields': ('address', 'city', 'state', 'pincode', 'phone1', 'phone2', 'email', 'website')},
        ),
        (
            "Tax Profile",
            {
                'fields': (
                    'gstin',
                    'pan',
                    'state_code',
                    'registration_type',
                    'filing_frequency',
                    'qrmp_enabled',
                    'lut_bond_enabled',
                    'annual_turnover_band',
                    'e_invoice_applicable',
                    'e_way_bill_enabled',
                    'default_place_of_supply_state',
                )
            },
        ),
        ("Banking", {'fields': ('bank_name', 'account_number', 'ifsc_code', 'branch', 'upi_id')}),
        (
            "Invoice",
            {
                'fields': (
                    'job_code_prefix',
                    'job_ticket_print_paper_size',
                    'bill_print_paper_size',
                    'include_workshop_device_tag',
                    'include_ticket_signatures',
                    'include_bill_signatures',
                    'include_bill_payment_details',
                    'technician_display_format',
                    'sales_invoice_next_number',
                    'enable_gst',
                    'gst_rate',
                )
            },
        ),
        ("Policy", {'fields': ('terms_conditions', 'warranty_policy')}),
    )

    def has_add_permission(self, request):
        if CompanyProfile.objects.exists():
            return False
        return super().has_add_permission(request)

    def has_delete_permission(self, request, obj=None):
        return False

    def get_actions(self, request):
        actions = super().get_actions(request)
        actions.pop('delete_selected', None)
        return actions


class CompanyUserMembershipInline(admin.TabularInline):
    model = CompanyUserMembership
    extra = 0
    raw_id_fields = ('user',)


@admin.register(CompanyWorkspace)
class CompanyWorkspaceAdmin(RoleBasedAdminMixin, admin.ModelAdmin):
    list_display = ('name', 'owner', 'status', 'plan_name', 'contact_phone', 'contact_email', 'created_at')
    list_filter = ('status', 'plan_name', 'created_at')
    search_fields = ('name', 'slug', 'owner__username', 'owner__email', 'contact_phone', 'contact_email')
    prepopulated_fields = {'slug': ('name',)}
    raw_id_fields = ('owner',)
    inlines = (CompanyUserMembershipInline,)


@admin.register(CompanyUserMembership)
class CompanyUserMembershipAdmin(RoleBasedAdminMixin, admin.ModelAdmin):
    list_display = ('workspace', 'user', 'role', 'is_active', 'joined_at')
    list_filter = ('role', 'is_active', 'joined_at')
    search_fields = ('workspace__name', 'user__username', 'user__email')
    raw_id_fields = ('workspace', 'user')


@admin.register(PlatformSettings)
class PlatformSettingsAdmin(RoleBasedAdminMixin, admin.ModelAdmin):
    staff_can_add = False
    staff_can_delete = False

    list_display = ('billing_author_name', 'support_phone', 'whatsapp_number', 'website')
    search_fields = ('billing_author_name', 'support_phone', 'whatsapp_number', 'website')
    fieldsets = (
        (
            "Footer Branding",
            {'fields': ('billing_author_name',)},
        ),
        (
            "Support Channels",
            {'fields': ('support_phone', 'whatsapp_number', 'website')},
        ),
    )

    def has_add_permission(self, request):
        if PlatformSettings.objects.exists():
            return False
        return super().has_add_permission(request)

    def has_delete_permission(self, request, obj=None):
        return False

    def get_actions(self, request):
        actions = super().get_actions(request)
        actions.pop('delete_selected', None)
        return actions


@admin.register(WhatsAppIntegrationSettings)
class WhatsAppIntegrationSettingsAdmin(RoleBasedAdminMixin, admin.ModelAdmin):
    staff_can_add = False
    staff_can_delete = False

    list_display = (
        'is_enabled',
        'delivery_method',
        'phone_number_id',
        'template_language_code',
        'bridge_base_url',
        'public_site_url',
        'default_country_code',
        'notify_on_created',
        'notify_on_completed',
        'notify_on_delivered',
        'notify_on_feedback',
        'updated_at',
    )
    fieldsets = (
        (
            "Delivery",
            {
                'fields': (
                    'is_enabled',
                    'delivery_method',
                    'public_site_url',
                    'default_country_code',
                )
            },
        ),
        (
            "Cloud API",
            {
                'fields': (
                    'api_version',
                    'phone_number_id',
                    'access_token',
                    'webhook_verify_token',
                    'app_secret',
                    'template_language_code',
                    'test_template_name',
                )
            },
        ),
        ("WhatsApp Bridge", {'fields': ('bridge_base_url',)}),
        ("Events", {'fields': ('notify_on_created', 'notify_on_completed', 'notify_on_delivered', 'notify_on_feedback')}),
        (
            "Template Names",
            {'fields': ('created_template_name', 'completed_template_name', 'delivered_template_name', 'estimate_template_name', 'feedback_template_name')},
        ),
        (
            "Template Parameter Maps",
            {'fields': ('created_template', 'completed_template', 'delivered_template', 'estimate_template', 'feedback_template')},
        ),
    )

    def has_add_permission(self, request):
        if WhatsAppIntegrationSettings.objects.exists():
            return False
        return super().has_add_permission(request)

    def has_delete_permission(self, request, obj=None):
        return False

    def get_actions(self, request):
        actions = super().get_actions(request)
        actions.pop('delete_selected', None)
        return actions


@admin.register(WhatsAppNotificationLog)
class WhatsAppNotificationLogAdmin(ImmutableAuditAdminMixin, admin.ModelAdmin):
    list_display = ('job_ticket', 'event_type', 'target_phone', 'was_successful', 'created_at')
    list_filter = ('event_type', 'was_successful', 'created_at')
    search_fields = ('job_ticket__job_code', 'target_phone', 'message', 'response_text')
    raw_id_fields = ('job_ticket', 'message_queue')
    readonly_fields = ('message_queue', 'job_ticket', 'event_type', 'target_phone', 'message', 'was_successful', 'response_text', 'created_at')
    list_select_related = ('job_ticket',)
    list_per_page = 100


@admin.register(MessageQueue)
class MessageQueueAdmin(ImmutableAuditAdminMixin, admin.ModelAdmin):
    list_display = (
        'id',
        'job_ticket',
        'event_type',
        'target_phone',
        'status',
        'transport',
        'bridge_message_id',
        'created_at',
    )
    list_filter = ('status', 'event_type', 'channel', 'created_at')
    search_fields = ('job_ticket__job_code', 'target_phone', 'message', 'caption', 'bridge_message_id', 'error_message')
    raw_id_fields = ('job_ticket',)
    readonly_fields = (
        'job_ticket',
        'channel',
        'event_type',
        'target_phone',
        'message',
        'pdf_url',
        'caption',
        'filename',
        'status',
        'transport',
        'bridge_message_id',
        'bridge_status_code',
        'response_payload',
        'error_message',
        'sent_at',
        'failed_at',
        'created_at',
        'updated_at',
    )
    list_select_related = ('job_ticket',)
    list_per_page = 100


@admin.register(InventoryParty)
class InventoryPartyAdmin(RoleBasedAdminMixin, admin.ModelAdmin):
    form = InventoryPartyAdminForm
    staff_can_delete = False

    list_display = (
        'name',
        'party_type',
        'gst_registration_type',
        'phone',
        'gstin',
        'state_code',
        'is_active',
        'updated_at',
    )
    list_filter = ('party_type', 'gst_registration_type', 'is_active', 'state')
    search_fields = ('name', 'legal_name', 'contact_person', 'phone', 'gstin', 'pan', 'city', 'state_code')
    ordering = ('name',)
    list_per_page = 100

    def get_actions(self, request):
        actions = super().get_actions(request)
        actions.pop('delete_selected', None)
        return actions


@admin.register(InventoryCreditPayment)
class InventoryCreditPaymentAdmin(RoleBasedAdminMixin, admin.ModelAdmin):
    staff_can_delete = False

    list_display = (
        'party',
        'bill',
        'direction',
        'payment_date',
        'payment_method',
        'amount',
        'balance_before',
        'balance_after',
        'created_by',
        'created_at',
    )
    list_filter = ('direction', 'payment_method', 'payment_date')
    search_fields = ('party__name', 'bill__bill_number', 'bill__invoice_number', 'reference_no', 'notes')
    raw_id_fields = ('party', 'bill', 'created_by')
    readonly_fields = ('created_at',)
    list_select_related = ('party', 'bill', 'created_by')
    list_per_page = 100


@admin.register(InventoryEntry)
class InventoryEntryAdmin(RoleBasedAdminMixin, admin.ModelAdmin):
    staff_can_add = False
    staff_can_change = False
    staff_can_delete = False
    actions = ('delete_selected',)

    list_display = (
        'entry_number',
        'invoice_number',
        'entry_date',
        'entry_type',
        'party',
        'product',
        'quantity',
        'total_amount',
        'stock_before',
        'stock_after',
    )
    list_filter = ('entry_type', 'entry_date')
    search_fields = ('entry_number', 'invoice_number', 'party__name', 'product__name', 'notes')
    raw_id_fields = ('party', 'product', 'created_by')
    readonly_fields = (
        'entry_number',
        'invoice_number',
        'entry_type',
        'entry_date',
        'party',
        'product',
        'quantity',
        'unit_price',
        'discount_amount',
        'gst_rate',
        'taxable_amount',
        'gst_amount',
        'total_amount',
        'stock_before',
        'stock_after',
        'notes',
        'created_by',
        'created_at',
    )
    list_select_related = ('party', 'product', 'created_by')
    list_per_page = 100

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        if not is_super_admin_user(request.user):
            return False
        if obj is None:
            return True

        restored_stock = int(obj.product.stock_quantity or 0) - int(obj.stock_effect or 0)
        return restored_stock >= 0

    def get_actions(self, request):
        actions = super().get_actions(request)
        if not is_super_admin_user(request.user):
            actions.pop('delete_selected', None)
        return actions

    def delete_model(self, request, obj):
        _reverse_inventory_entries([obj])

    def delete_queryset(self, request, queryset):
        try:
            _reverse_inventory_entries(queryset.only('id'))
        except ValidationError as exc:
            self.message_user(request, str(exc), level=messages.ERROR)


@admin.register(UserSessionActivity)
class UserSessionActivityAdmin(ImmutableAuditAdminMixin, admin.ModelAdmin):
    list_display = ('user', 'channel', 'device_label', 'status', 'ip_address', 'login_at', 'last_activity_at', 'logout_at')
    list_filter = ('channel', 'status', 'login_at')
    search_fields = ('user__username', 'session_key', 'ip_address', 'user_agent', 'last_activity_path')
    raw_id_fields = ('user',)
    readonly_fields = (
        'user',
        'session_key',
        'channel',
        'device_label',
        'status',
        'ip_address',
        'user_agent',
        'login_at',
        'last_activity_at',
        'last_activity_path',
        'expires_at',
        'logout_at',
        'logout_reason',
    )
    list_select_related = ('user',)
    list_per_page = 100

    @admin.display(description='Device')
    def device_label(self, obj):
        return obj.device_label


class SecureUserAdmin(SuperAdminOnlyMixin, DjangoUserAdmin):
    pass


class SecureGroupAdmin(SuperAdminOnlyMixin, DjangoGroupAdmin):
    pass


try:
    admin.site.unregister(User)
except admin.sites.NotRegistered:
    pass

try:
    admin.site.unregister(Group)
except admin.sites.NotRegistered:
    pass

admin.site.register(User, SecureUserAdmin)
admin.site.register(Group, SecureGroupAdmin)

admin.site.site_header = "ZynDesk | Service Billing Administration"
admin.site.site_title = "ZynDesk Admin"
admin.site.index_title = "Operations Control Panel"
admin.site.site_url = "/"
admin.site.disable_action('delete_selected')


@admin.register(InventoryBillLog)
class InventoryBillLogAdmin(admin.ModelAdmin):
    list_display = ('bill', 'action', 'user', 'timestamp', 'related_bill')
    list_filter = ('action', 'timestamp')
    search_fields = ('bill__bill_number', 'details', 'user__username')
    readonly_fields = ('workspace', 'bill', 'action', 'user', 'timestamp', 'details', 'related_bill')
    ordering = ('-timestamp',)

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(InventoryVendorCredit)
class InventoryVendorCreditAdmin(admin.ModelAdmin):
    list_display = ('return_bill', 'party', 'credit_amount', 'applied_amount', 'status', 'created_at')
    list_filter = ('status', 'created_at')
    search_fields = ('party__name', 'return_bill__bill_number')
    readonly_fields = (
        'workspace', 'party', 'return_bill', 'source_bill',
        'credit_amount', 'applied_amount', 'status',
        'created_by', 'created_at', 'updated_at',
    )
    ordering = ('-created_at',)

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(InventoryVendorCreditApplication)
class InventoryVendorCreditApplicationAdmin(admin.ModelAdmin):
    list_display = ('vendor_credit', 'target_bill', 'amount', 'applied_date', 'created_by')
    list_filter = ('applied_date',)
    search_fields = ('target_bill__bill_number', 'vendor_credit__return_bill__bill_number')
    readonly_fields = (
        'workspace', 'vendor_credit', 'target_bill',
        'amount', 'applied_date', 'notes', 'created_by', 'created_at',
    )
    ordering = ('-applied_date',)

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False
