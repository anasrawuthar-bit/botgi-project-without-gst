import asyncio
import hashlib
import hmac
import json
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal
from datetime import datetime, timedelta
from pathlib import Path
from unittest import skipUnless
from unittest.mock import Mock, patch

from django.conf import settings
from django.contrib.admin.sites import AdminSite
from django.contrib.auth.models import Group, User
from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import close_old_connections, connection
from django.http import HttpResponse
from django.test import Client as DjangoTestClient, RequestFactory, TestCase, TransactionTestCase, override_settings
from django.urls import reverse
from django.utils import timezone
from asgiref.sync import async_to_sync
from channels.layers import get_channel_layer
from channels.routing import URLRouter
from channels.testing import WebsocketCommunicator

from .admin import InventoryEntryAdmin, JobTicketAdmin
from .access_control import apply_staff_access
from .admin_roles import ROLE_SUPER_ADMIN
from .middleware import SessionSecurityMiddleware
from .forms import (
    AssignJobForm,
    ClientForm,
    CompanyProfileForm,
    InventoryEntryForm,
    InventoryPartyForm,
    ProductForm,
    WhatsAppIntegrationSettingsForm,
)
from .models import (
    Client,
    CompanyProfile,
    CompanyUserMembership,
    CompanyWorkspace,
    InventoryBill,
    InventoryCreditPayment,
    InventoryEntry,
    InventoryParty,
    JobReminder,
    JobTicket,
    JobTicketLog,
    JobTicketPhoto,
    MessageQueue,
    Product,
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
from .phone_utils import normalize_indian_phone
from .whatsapp_service import create_receipt_access_token, send_job_whatsapp_notification
from .views import client_login, get_phone_service_snapshot, issue_mobile_jwt, staff_job_detail, technician_report_print
from .views.helpers import get_monthly_summary_context
from .views.helpers import (
    _generate_inventory_bill_number,
    _generate_inventory_entry_number,
    _get_or_create_inventory_customer_party_for_job,
    _process_inventory_grouped_bill_edit,
)
from .consumers import staff_group_name
from .routing import websocket_urlpatterns


class WorkspaceIsolationSecurityTests(TransactionTestCase):


    def setUp(self):
        self.user = User.objects.create_user(
            username='workspace-staff',
            password='StrongPass123!',
            is_staff=True,
        )
        apply_staff_access(self.user, {'staff_dashboard', 'inventory'})
        self.workspace_a = CompanyWorkspace.objects.create(name='Workspace A', owner=self.user)
        self.workspace_b = CompanyWorkspace.objects.create(name='Workspace B', owner=self.user)
        CompanyUserMembership.objects.create(
            workspace=self.workspace_a,
            user=self.user,
            role=CompanyUserMembership.ROLE_ADMIN,
        )
        self.job_a = JobTicket.objects.create(
            workspace=self.workspace_a,
            job_code='GI-260501-001',
            customer_name='Workspace A Customer',
            customer_phone='9876543210',
            device_type='Laptop',
            reported_issue='A issue',
        )
        self.job_b = JobTicket.objects.create(
            workspace=self.workspace_b,
            job_code='GI-260501-002',
            customer_name='Workspace B Customer',
            customer_phone='9876543211',
            device_type='Phone',
            reported_issue='B issue',
        )
        self.token = issue_mobile_jwt(self.user)

    def test_mobile_job_list_is_limited_to_current_workspace(self):
        response = self.client.get(
            reverse('mobile_api_jobs'),
            HTTP_AUTHORIZATION=f'Bearer {self.token}',
        )

        self.assertEqual(response.status_code, 200)
        job_codes = {job['job_code'] for job in response.json()['jobs']}
        self.assertIn(self.job_a.job_code, job_codes)
        self.assertNotIn(self.job_b.job_code, job_codes)

    def test_mobile_job_detail_rejects_other_workspace(self):
        response = self.client.get(
            reverse('mobile_api_job_detail', kwargs={'job_code': self.job_b.job_code}),
            HTTP_AUTHORIZATION=f'Bearer {self.token}',
        )

        self.assertEqual(response.status_code, 403)

    def test_financial_dashboard_is_limited_to_current_workspace(self):
        context = get_monthly_summary_context(
            timezone.make_aware(datetime(2020, 1, 1)),
            timezone.make_aware(datetime(2030, 1, 1)),
            '2020-01-01',
            '2029-12-31',
            workspace=self.workspace_a,
        )

        self.assertEqual(context['jobs_created_count'], 1)
        self.assertEqual(context['pending_completed_count'], 0)

    def test_authenticated_non_staff_cannot_open_job_creation_success(self):
        technician = User.objects.create_user(
            username='workspace-technician',
            password='StrongPass123!',
        )
        self.client.force_login(technician)

        response = self.client.get(
            reverse('job_creation_success', kwargs={'job_code': self.job_a.job_code}),
        )

        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.url, reverse('unauthorized'))

    def test_vendor_unlock_does_not_accept_default_password(self):
        self.client.force_login(self.user)

        response = self.client.post(
            reverse('unlock_vendor_details', kwargs={'job_code': self.job_a.job_code}),
            {'vendor_password': 'vendor123'},
        )

        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.url, reverse('staff_job_detail', args=[self.job_a.job_code]))
        self.assertFalse(self.client.session.get('vendor_details_unlocked', False))

    def test_staff_websocket_does_not_receive_other_workspace_events(self):
        async def exercise():
            application = URLRouter(websocket_urlpatterns)
            communicator = WebsocketCommunicator(
                application,
                f'/ws/staff_updates/?workspace={self.workspace_a.id}',
            )
            communicator.scope['user'] = self.user
            connected, _ = await communicator.connect()
            self.assertTrue(connected)
            channel_layer = get_channel_layer()

            await channel_layer.group_send(
                staff_group_name(self.workspace_b.id),
                {
                    'type': 'job_status_update',
                    'job_code': self.job_b.job_code,
                    'status': 'Closed',
                },
            )
            with self.assertRaises(asyncio.TimeoutError):
                await asyncio.wait_for(communicator.receive_json_from(), timeout=0.2)

            await channel_layer.group_send(
                staff_group_name(self.workspace_a.id),
                {
                    'type': 'job_status_update',
                    'job_code': self.job_a.job_code,
                    'status': 'Ready for Pickup',
                },
            )
            event = await asyncio.wait_for(communicator.receive_json_from(), timeout=1)
            self.assertEqual(event['job_code'], self.job_a.job_code)
            await communicator.disconnect()

        async_to_sync(exercise)()
        connection.close()
        close_old_connections()


class BusinessIntegrityRegressionTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(
            username='integrity-staff',
            password='StrongPass123!',
            is_staff=True,
        )
        apply_staff_access(self.user, {'staff_dashboard', 'reports_vendor', 'inventory'})
        self.workspace = CompanyWorkspace.objects.create(name='Integrity Workspace', owner=self.user)
        CompanyUserMembership.objects.create(
            workspace=self.workspace,
            user=self.user,
            role=CompanyUserMembership.ROLE_ADMIN,
        )

    def test_customer_party_name_does_not_override_different_phone(self):
        existing = InventoryParty.objects.create(
            workspace=self.workspace,
            name='Same Customer',
            phone='9876543200',
            party_type='both',
        )
        job = JobTicket.objects.create(
            workspace=self.workspace,
            job_code='GI-260913-001',
            customer_name='Same Customer',
            customer_phone='9876543201',
            device_type='Laptop',
            reported_issue='Different phone',
        )

        party = _get_or_create_inventory_customer_party_for_job(job)

        self.assertNotEqual(party.id, existing.id)
        self.assertEqual(party.phone, '9876543201')
        self.assertEqual(
            InventoryParty.objects.filter(
                workspace=self.workspace,
                name='Same Customer',
            ).count(),
            2,
        )

    def test_repeated_vendor_payment_reference_is_rejected(self):
        vendor = Vendor.objects.create(
            workspace=self.workspace,
            company_name='Integrity Vendor',
            name='Vendor Contact',
        )
        job = JobTicket.objects.create(
            workspace=self.workspace,
            job_code='GI-260913-002',
            customer_name='Vendor Customer',
            customer_phone='9876543202',
            device_type='Laptop',
            reported_issue='Vendor service',
        )
        service = SpecializedService.objects.create(
            job_ticket=job,
            vendor=vendor,
            status='Returned from Vendor',
            vendor_cost=Decimal('1000.00'),
            vendor_balance_amount=Decimal('1000.00'),
        )
        self.client.force_login(self.user)
        payload = {
            'payment_scope': 'single',
            'specialized_service_id': str(service.id),
            'payment_amount': '400.00',
            'payment_method': VendorPayment.METHOD_TRANSFER,
            'payment_date': timezone.localdate().isoformat(),
            'reference_no': 'RETRY-001',
        }

        first = self.client.post(reverse('record_vendor_payment', args=[vendor.id]), payload)
        second = self.client.post(reverse('record_vendor_payment', args=[vendor.id]), payload)

        self.assertEqual(first.status_code, 302)
        self.assertEqual(second.status_code, 302)
        service.refresh_from_db()
        self.assertEqual(service.vendor_paid_amount, Decimal('400.00'))
        self.assertEqual(service.vendor_balance_amount, Decimal('600.00'))
        self.assertEqual(
            VendorPayment.objects.filter(
                specialized_service=service,
                reference_no='RETRY-001',
            ).count(),
            1,
        )

    def test_inventory_numbers_are_unique_across_workspaces(self):
        entry_date = timezone.localdate()
        bill_a = _generate_inventory_bill_number('purchase', entry_date, self.workspace)
        bill_b = _generate_inventory_bill_number('purchase', entry_date, None)
        entry_a = _generate_inventory_entry_number('purchase', entry_date, self.workspace)
        entry_b = _generate_inventory_entry_number('purchase', entry_date, None)

        self.assertNotEqual(bill_a, bill_b)
        self.assertNotEqual(entry_a, entry_b)

    def test_paid_bill_edit_is_rejected_before_stock_reconciliation(self):
        party = InventoryParty.objects.create(
            workspace=self.workspace,
            name='Paid Party',
            party_type='supplier',
        )
        product = Product.objects.create(
            workspace=self.workspace,
            name='Paid Product',
            stock_quantity=5,
        )
        bill = InventoryBill.objects.create(
            workspace=self.workspace,
            bill_number='PB-TEST-001',
            entry_type='purchase',
            entry_date=timezone.localdate(),
            invoice_number='SUP-TEST-001',
            party=party,
        )
        entry = InventoryEntry.objects.create(
            workspace=self.workspace,
            entry_number='PUR-TEST-001',
            entry_type='purchase',
            entry_date=timezone.localdate(),
            bill=bill,
            invoice_number=bill.invoice_number,
            party=party,
            product=product,
            quantity=5,
            unit_price=Decimal('100.00'),
            taxable_amount=Decimal('500.00'),
            total_amount=Decimal('500.00'),
            stock_before=0,
            stock_after=5,
        )
        InventoryCreditPayment.objects.create(
            workspace=self.workspace,
            party=party,
            bill=bill,
            direction=InventoryCreditPayment.DIRECTION_PAYABLE,
            amount=Decimal('500.00'),
            balance_before=Decimal('500.00'),
            balance_after=Decimal('0.00'),
        )
        request = RequestFactory().post(
            '/inventory/',
            {
                'bill_id': str(bill.id),
                'entry_id': str(entry.id),
                'edit_password': 'StrongPass123!',
            },
        )
        request.user = self.user
        request.current_workspace = self.workspace

        with self.assertRaisesMessage(ValueError, 'Paid or partially paid bills cannot be edited'):
            _process_inventory_grouped_bill_edit(request, entry_type='purchase')

        self.assertTrue(InventoryEntry.objects.filter(pk=entry.id).exists())
        product.refresh_from_db()
        self.assertEqual(product.stock_quantity, 5)


@skipUnless(connection.vendor == 'postgresql', 'PostgreSQL concurrency test only')
class PostgreSQLInventoryNumberingTests(TransactionTestCase):
    def test_concurrent_bill_numbers_are_unique(self):
        entry_date = timezone.localdate()

        def issue_number(_index):
            close_old_connections()
            try:
                return _generate_inventory_bill_number('sale', entry_date)
            finally:
                connection.close()

        with ThreadPoolExecutor(max_workers=4) as executor:
            numbers = list(executor.map(issue_number, range(4)))

        connection.close()
        close_old_connections()
        self.assertEqual(len(numbers), len(set(numbers)))


class IndianPhoneUtilsTests(TestCase):
    def test_normalize_accepts_formatted_number_and_removes_prefix(self):
        phone, error = normalize_indian_phone('+91 98765 43210')
        self.assertIsNone(error)
        self.assertEqual(phone, '9876543210')

    def test_normalize_rejects_alphabets(self):
        phone, error = normalize_indian_phone('98AB5 43210')
        self.assertEqual(phone, '')
        self.assertEqual(error, 'Phone number can contain numbers only. Alphabets are not allowed.')

    def test_normalize_rejects_more_than_ten_digits(self):
        phone, error = normalize_indian_phone('987654321012')
        self.assertEqual(phone, '')
        self.assertEqual(error, 'Phone number must be exactly 10 digits.')


class ClientFormPhoneValidationTests(TestCase):
    def test_client_form_saves_clean_phone_digits(self):
        form = ClientForm(data={'name': 'Arun', 'phone': '98765 43210', 'address': '', 'notes': ''})
        self.assertTrue(form.is_valid(), form.errors)
        self.assertEqual(form.cleaned_data['phone'], '9876543210')

    def test_client_form_rejects_empty_phone(self):
        form = ClientForm(data={'name': 'Arun', 'phone': '', 'address': '', 'notes': ''})
        self.assertFalse(form.is_valid())
        self.assertIn('phone', form.errors)


class ClientLoginPhoneNormalizationTests(TestCase):
    def setUp(self):
        self.factory = RequestFactory()
        self.job = JobTicket.objects.create(
            job_code='GI-260324-001',
            customer_name='Arun',
            customer_phone='9876543210',
            device_type='Laptop',
            device_brand='Dell',
            device_model='Inspiron',
            reported_issue='No display',
        )

    def test_client_login_accepts_spaced_phone_format(self):
        response = self.client.post(
            reverse('client_login'),
            {'job_code': self.job.job_code, 'phone_number': '98765 43210'},
        )
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.url, reverse('client_status', args=[self.job.job_code]))

    def test_client_login_accepts_plus_91_phone_format(self):
        response = self.client.post(
            reverse('client_login'),
            {'job_code': self.job.job_code, 'phone_number': '+91 98765 43210'},
        )
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.url, reverse('client_status', args=[self.job.job_code]))

    def test_client_login_shows_clear_phone_error_for_short_number(self):
        request = self.factory.post(
            reverse('client_login'),
            {'job_code': self.job.job_code, 'phone_number': '98765'},
        )
        response = client_login(request)
        self.assertEqual(response.status_code, 200)
        self.assertIn(b'Phone Number must be exactly 10 digits.', response.content)

    def test_customer_status_requires_session_or_ticket_bound_token(self):
        response = self.client.get(reverse('client_status', args=[self.job.job_code]))
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.url, reverse('client_login'))

        token = create_receipt_access_token(self.job)
        authorized = self.client.get(
            reverse('client_status', args=[self.job.job_code]),
            {'token': token},
        )
        self.assertEqual(authorized.status_code, 200)

    def test_receipt_token_cannot_be_reused_for_another_ticket(self):
        other_job = JobTicket.objects.create(
            job_code='GI-260324-003',
            customer_name='Other Customer',
            customer_phone='9876543211',
            device_type='Laptop',
            reported_issue='No boot',
        )
        token = create_receipt_access_token(self.job)

        response = self.client.get(
            reverse('client_status', args=[other_job.job_code]),
            {'token': token},
        )

        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.url, reverse('client_login'))

    def test_customer_bill_requires_the_same_ticket_authorization(self):
        response = self.client.get(reverse('client_bill_view', args=[self.job.job_code]))
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.url, reverse('client_login'))

    def test_modified_receipt_token_is_rejected(self):
        token = create_receipt_access_token(self.job)
        payload, signature = token.split('.', 1)
        modified_signature = f'{signature[:-1]}{("A" if signature[-1] != "A" else "B")}'
        modified_token = f'{payload}.{modified_signature}'

        response = self.client.get(
            reverse('client_status', args=[self.job.job_code]),
            {'token': modified_token},
        )

        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.url, reverse('client_login'))

    def test_expired_receipt_token_is_rejected(self):
        token = create_receipt_access_token(
            self.job,
            issued_at=timezone.now() - timedelta(days=2),
        )

        response = self.client.get(
            reverse('client_status', args=[self.job.job_code]),
            {'token': token},
        )

        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.url, reverse('client_login'))

    def test_customer_bill_view_renders_when_authorized_and_ready(self):
        from .views.helpers import build_job_billing_context
        from .models import ServiceLog, CompanyProfile

        self.job.status = 'Ready for Pickup'
        self.job.save(update_fields=['status'])

        ServiceLog.objects.create(
            job_ticket=self.job,
            description='Screen replacement',
            part_cost=Decimal('1500.00'),
            service_charge=Decimal('300.00'),
        )

        token = create_receipt_access_token(self.job)
        response = self.client.get(
            reverse('client_bill_view', args=[self.job.job_code]),
            {'token': token},
        )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, self.job.job_code)
        self.assertContains(response, 'Screen replacement')
        self.assertIn('company', response.context)
        self.assertEqual(response.context['grand_total'], Decimal('1800.00'))

    def test_build_job_billing_context_with_gst_and_product_sale(self):
        from .views.helpers import build_job_billing_context
        from .models import ServiceLog, CompanyProfile, Product, ProductSale

        company = CompanyProfile.get_profile(self.job.workspace)
        company.enable_gst = True
        company.gstin = '32AAACG1234A1Z5'
        company.gst_rate = Decimal('18.00')
        company.upi_id = 'business@upi'
        company.save()

        # Add regular service log
        ServiceLog.objects.create(
            job_ticket=self.job,
            description='Motherboard Repair',
            part_cost=Decimal('500.00'),
            service_charge=Decimal('500.00'),
        )

        # Add product sale log
        product = Product.objects.create(
            workspace=self.job.workspace,
            name='SSD 512GB NVMe',
            category='Storage',
            unit_price=Decimal('2000.00'),
            cost_price=Decimal('1500.00'),
            stock_quantity=10,
        )
        sale_log = ServiceLog.objects.create(
            job_ticket=self.job,
            description='Product Sale - SSD 512GB NVMe (Qty: 1)',
            part_cost=Decimal('2000.00'),
            service_charge=Decimal('0.00'),
        )
        ProductSale.objects.create(
            workspace=self.job.workspace,
            job_ticket=self.job,
            product=product,
            service_log=sale_log,
            quantity=1,
            unit_price=Decimal('2000.00'),
            cost_price=Decimal('1500.00'),
            line_total=Decimal('2000.00'),
            line_cost=Decimal('1500.00'),
            line_profit=Decimal('500.00'),
        )

        self.job.amount_paid = Decimal('1000.00')
        self.job.save(update_fields=['amount_paid'])

        context = build_job_billing_context(self.job)

        self.assertEqual(context['subtotal'], Decimal('3000.00'))
        self.assertEqual(context['grand_total'], Decimal('3000.00'))
        self.assertEqual(context['amount_paid'], Decimal('1000.00'))
        self.assertEqual(context['balance_due'], Decimal('2000.00'))
        self.assertTrue(context['enable_gst'])
        self.assertEqual(context['taxable_amount'] + context['total_gst'], Decimal('3000.00'))
        self.assertEqual(context['cgst_amount'] + context['sgst_amount'], context['total_gst'])
        self.assertIn('business@upi', context['upi_payment_url'])
        self.assertEqual(len(context['service_logs']), 2)
        # Check product sale enrichment
        prod_log = [l for l in context['service_logs'] if l['is_product_sale']][0]
        self.assertEqual(prod_log['product_name'], 'SSD 512GB NVMe')
        self.assertEqual(prod_log['quantity'], 1)



class PhoneLookupSnapshotTests(TestCase):
    def test_snapshot_matches_existing_plus_91_client_record(self):
        Client.objects.create(name='Existing Client', phone='+91 98765 43210')
        JobTicket.objects.create(
            job_code='GI-260324-002',
            customer_name='Existing Client',
            customer_phone='9876543210',
            device_type='Mobile',
            device_brand='Samsung',
            device_model='M31',
            reported_issue='Charging issue',
        )

        snapshot = get_phone_service_snapshot('9876543210')

        self.assertTrue(snapshot['exists'])
        self.assertEqual(snapshot['client_name'], 'Existing Client')
        self.assertEqual(snapshot['total_jobs'], 1)


@override_settings(WEB_RELEASE_VERSION='2026.04.20.1', WEB_RELEASE_POLL_INTERVAL_SECONDS=180)
class AppReleaseMetaTests(TestCase):
    def test_release_meta_endpoint_returns_current_web_version(self):
        response = self.client.get(reverse('app_release_meta'))

        self.assertEqual(response.status_code, 200)
        payload = response.json()

        self.assertTrue(payload['ok'])
        self.assertEqual(payload['web_version'], '2026.04.20.1')
        self.assertEqual(payload['poll_interval_seconds'], 180)
        self.assertIn('generated_at', payload)

    def test_base_template_includes_release_refresh_banner(self):
        template_path = Path(settings.BASE_DIR) / 'job_tickets' / 'templates' / 'job_tickets' / 'base.html'
        template_text = template_path.read_text(encoding='utf-8')

        self.assertIn('data-botgi-web-version="{{ release_info.web_version }}"', template_text)
        self.assertIn('data-botgi-release-meta-url="{% url \'app_release_meta\' %}"', template_text)
        self.assertIn('id="app-release-banner"', template_text)
        self.assertIn("job_tickets/js/app-release-monitor.js", template_text)


class SessionSecurityTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username='session-user', password='StrongPass123!')

    def test_cookie_settings_keep_local_http_login_working(self):
        self.assertTrue(settings.SESSION_COOKIE_HTTPONLY)
        self.assertTrue(settings.CSRF_COOKIE_HTTPONLY)
        self.assertEqual(settings.SESSION_COOKIE_SAMESITE, 'Lax')
        self.assertEqual(settings.CSRF_COOKIE_SAMESITE, 'Lax')
        self.assertEqual(settings.SESSION_COOKIE_AGE, settings.SESSION_IDLE_TIMEOUT_SECONDS)
        self.assertFalse(settings.SECURE_SSL_REDIRECT)
        self.assertEqual(settings.PUBLIC_BASE_URL, '')
        self.assertFalse(settings.SESSION_COOKIE_SECURE)
        self.assertFalse(settings.CSRF_COOKIE_SECURE)

    def test_login_page_sets_non_secure_csrf_cookie_for_local_http(self):
        csrf_client = DjangoTestClient(enforce_csrf_checks=True)

        response = csrf_client.get(reverse('login'))

        self.assertEqual(response.status_code, 200)
        self.assertIn(settings.CSRF_COOKIE_NAME, response.cookies)
        self.assertFalse(response.cookies[settings.CSRF_COOKIE_NAME]['secure'])

    def test_successful_login_creates_session_activity(self):
        response = self.client.post(
            reverse('login'),
            {'username': 'session-user', 'password': 'StrongPass123!'},
            REMOTE_ADDR='192.168.1.55',
            HTTP_USER_AGENT='Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/122.0 Safari/537.36',
        )

        self.assertEqual(response.status_code, 302)
        activity = UserSessionActivity.objects.get(user=self.user)
        self.assertEqual(activity.status, UserSessionActivity.STATUS_ACTIVE)
        self.assertEqual(activity.channel, UserSessionActivity.CHANNEL_WEB)
        self.assertTrue(activity.session_key)
        self.assertIsNotNone(activity.last_activity_at)
        self.assertEqual(activity.ip_address, '192.168.1.55')
        self.assertEqual(activity.device_label, 'Chrome on Windows')
        self.assertIn(settings.SESSION_COOKIE_NAME, response.cookies)
        self.assertFalse(response.cookies[settings.SESSION_COOKIE_NAME]['secure'])

    def test_logout_marks_session_as_logged_out(self):
        self.client.login(username='session-user', password='StrongPass123!')
        activity = UserSessionActivity.objects.get(user=self.user)

        response = self.client.get(reverse('logout'))

        self.assertEqual(response.status_code, 302)
        activity.refresh_from_db()
        self.assertEqual(activity.status, UserSessionActivity.STATUS_LOGGED_OUT)
        self.assertIsNotNone(activity.logout_at)

    def test_inactive_session_expires_and_redirects_to_login(self):
        self.client.login(username='session-user', password='StrongPass123!')
        session = self.client.session
        session['_last_activity_ts'] = int((timezone.now() - timedelta(seconds=settings.SESSION_IDLE_TIMEOUT_SECONDS + 5)).timestamp())
        session.save()

        response = self.client.get(reverse('home'))

        self.assertEqual(response.status_code, 302)
        self.assertTrue(response.url.startswith(f"{reverse('login')}?next="))

        activity = UserSessionActivity.objects.get(user=self.user)
        self.assertEqual(activity.status, UserSessionActivity.STATUS_EXPIRED)
        self.assertEqual(activity.logout_reason, UserSessionActivity.STATUS_EXPIRED)

    def test_staff_dashboard_redirects_to_custom_login_url_when_logged_out(self):
        response = self.client.get(reverse('staff_dashboard'))

        self.assertEqual(response.status_code, 302)
        self.assertTrue(response.url.startswith(f"{reverse('login')}?next="))
        self.assertNotIn('/accounts/login/', response.url)

    def test_session_middleware_treats_cancelled_request_as_client_abort(self):
        def cancelled_response(_request):
            raise asyncio.CancelledError()

        request = RequestFactory().get('/staff/vendors/')
        request.user = Mock(is_authenticated=False)
        middleware = SessionSecurityMiddleware(cancelled_response)

        response = middleware(request)

        self.assertEqual(response.status_code, 499)

    def test_login_ignores_unresolved_next_url(self):
        response = self.client.post(
            reverse('login'),
            {
                'username': 'session-user',
                'password': 'StrongPass123!',
                'next': '/missing-page/',
            },
            REMOTE_ADDR='192.168.1.55',
        )

        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.url, reverse('home'))

    def test_login_preserves_valid_next_url(self):
        valid_next = reverse('client_login')
        response = self.client.post(
            reverse('login'),
            {
                'username': 'session-user',
                'password': 'StrongPass123!',
                'next': valid_next,
            },
            REMOTE_ADDR='192.168.1.56',
        )

        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.url, valid_next)

    def test_login_falls_back_to_allowed_page_when_next_url_requires_missing_access(self):
        limited_user = User.objects.create_user(username='limited-staff', password='StrongPass123!', is_staff=True)
        apply_staff_access(limited_user, {'inventory'})

        response = self.client.post(
            reverse('login'),
            {
                'username': 'limited-staff',
                'password': 'StrongPass123!',
                'next': reverse('staff_dashboard'),
            },
            REMOTE_ADDR='192.168.1.55',
        )

        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.url, reverse('inventory_dashboard'))

    def test_second_login_invalidates_previous_active_session(self):
        first_client = DjangoTestClient()
        second_client = DjangoTestClient()

        first_response = first_client.post(
            reverse('login'),
            {'username': 'session-user', 'password': 'StrongPass123!'},
            REMOTE_ADDR='192.168.1.55',
        )
        self.assertEqual(first_response.status_code, 302)

        first_session_key = first_client.session.session_key
        self.assertTrue(
            UserSessionActivity.objects.filter(
                session_key=first_session_key,
                status=UserSessionActivity.STATUS_ACTIVE,
            ).exists()
        )

        second_response = second_client.post(
            reverse('login'),
            {'username': 'session-user', 'password': 'StrongPass123!'},
            REMOTE_ADDR='192.168.1.56',
        )
        self.assertEqual(second_response.status_code, 302)

        first_activity = UserSessionActivity.objects.get(session_key=first_session_key)
        self.assertEqual(first_activity.status, UserSessionActivity.STATUS_LOGGED_OUT)
        self.assertEqual(first_activity.logout_reason, UserSessionActivity.LOGOUT_REASON_NEW_LOGIN)
        self.assertEqual(
            UserSessionActivity.objects.filter(
                user=self.user,
                status=UserSessionActivity.STATUS_ACTIVE,
            ).count(),
            1,
        )

        kicked_response = first_client.get(reverse('home'))
        self.assertEqual(kicked_response.status_code, 302)
        self.assertTrue(kicked_response.url.startswith(f"{reverse('login')}?next="))
        self.assertIn('reason=session_replaced', kicked_response.url)

        login_notice_response = first_client.get(kicked_response.url)
        self.assertEqual(login_notice_response.status_code, 200)
        self.assertContains(login_notice_response, 'Signed Out From Another Device')
        self.assertContains(login_notice_response, 'this account signed in on another device')


class AdminDeletePermissionTests(TestCase):
    def setUp(self):
        self.factory = RequestFactory()
        self.site = AdminSite()
        self.inventory_admin = InventoryEntryAdmin(InventoryEntry, self.site)
        self.job_admin = JobTicketAdmin(JobTicket, self.site)

        self.super_admin = User.objects.create_user(
            username='super-admin',
            password='StrongPass123!',
            is_staff=True,
        )
        super_admin_group, _ = Group.objects.get_or_create(name=ROLE_SUPER_ADMIN)
        self.super_admin.groups.add(super_admin_group)

        self.regular_staff = User.objects.create_user(
            username='regular-staff',
            password='StrongPass123!',
            is_staff=True,
        )

    def test_super_admin_can_delete_inventory_entry_and_stock_is_restored(self):
        supplier = InventoryParty.objects.create(name='Main Supplier', party_type='supplier')
        product = Product.objects.create(
            name='SSD 512GB',
            stock_quantity=5,
            unit_price=Decimal('4500.00'),
            cost_price=Decimal('3200.00'),
        )
        bill = InventoryBill.objects.create(
            bill_number='PB-260401-001',
            entry_type='purchase',
            entry_date=timezone.localdate(),
            party=supplier,
        )
        entry = InventoryEntry.objects.create(
            bill=bill,
            entry_number='PE-260401-001',
            entry_type='purchase',
            entry_date=timezone.localdate(),
            party=supplier,
            product=product,
            quantity=5,
            unit_price=Decimal('3200.00'),
            taxable_amount=Decimal('16000.00'),
            total_amount=Decimal('16000.00'),
            stock_before=0,
            stock_after=5,
        )

        request = self.factory.post('/admin/job_tickets/inventoryentry/')
        request.user = self.super_admin

        self.assertTrue(self.inventory_admin.has_delete_permission(request, entry))
        self.inventory_admin.delete_model(request, entry)

        product.refresh_from_db()
        self.assertEqual(product.stock_quantity, 0)
        self.assertFalse(InventoryEntry.objects.filter(pk=entry.pk).exists())
        self.assertFalse(InventoryBill.objects.filter(pk=bill.pk).exists())

        request.user = self.regular_staff
        self.assertFalse(self.inventory_admin.has_delete_permission(request, None))

    def test_super_admin_can_delete_protected_job_and_linked_inventory_is_cleaned(self):
        customer = InventoryParty.objects.create(name='Walk-in Customer', party_type='customer')
        product = Product.objects.create(
            name='Laptop Battery',
            stock_quantity=3,
            unit_price=Decimal('2500.00'),
            cost_price=Decimal('1500.00'),
        )
        job = JobTicket.objects.create(
            job_code='GI-260401-900',
            customer_name='Anas',
            customer_phone='9876543210',
            device_type='Laptop',
            reported_issue='Battery not charging',
            status='Completed',
            vyapar_invoice_number='INV-900',
        )
        ServiceLog.objects.create(
            job_ticket=job,
            description='Battery replacement',
            part_cost=Decimal('2500.00'),
            service_charge=Decimal('300.00'),
        )
        bill = InventoryBill.objects.create(
            bill_number='SB-260401-001',
            entry_type='sale',
            entry_date=timezone.localdate(),
            invoice_number='INV-900',
            job_ticket=job,
            party=customer,
        )
        entry = InventoryEntry.objects.create(
            bill=bill,
            entry_number='SE-260401-001',
            entry_type='sale',
            entry_date=timezone.localdate(),
            invoice_number='INV-900',
            job_ticket=job,
            party=customer,
            product=product,
            quantity=2,
            unit_price=Decimal('2500.00'),
            taxable_amount=Decimal('5000.00'),
            total_amount=Decimal('5000.00'),
            stock_before=5,
            stock_after=3,
        )

        request = self.factory.post('/admin/job_tickets/jobticket/')
        request.user = self.super_admin

        self.assertTrue(self.job_admin.has_delete_permission(request, job))
        self.job_admin.delete_model(request, job)

        product.refresh_from_db()
        self.assertEqual(product.stock_quantity, 5)
        self.assertFalse(JobTicket.objects.filter(pk=job.pk).exists())
        self.assertFalse(InventoryEntry.objects.filter(pk=entry.pk).exists())
        self.assertFalse(InventoryBill.objects.filter(pk=bill.pk).exists())

        request.user = self.regular_staff
        self.assertFalse(self.job_admin.has_delete_permission(request, None))


class TechnicianAssignmentAndChecklistTests(TestCase):
    def setUp(self):
        self.staff_user = User.objects.create_user(
            username='dashboard-admin',
            password='StrongPass123!',
            is_staff=True,
            is_superuser=True,
        )
        self.technician_user = User.objects.create_user(
            username='actual-tech',
            password='StrongPass123!',
        )
        self.technician = TechnicianProfile.objects.create(
            user=self.technician_user,
            unique_id='TECH100',
        )
        self.staff_with_profile = User.objects.create_user(
            username='staff-profile-user',
            password='StrongPass123!',
            is_staff=True,
        )
        self.staff_profile = TechnicianProfile.objects.create(
            user=self.staff_with_profile,
            unique_id='STF100',
        )

    def test_assign_form_excludes_staff_users_even_if_profile_exists(self):
        form = AssignJobForm()

        technician_ids = list(form.fields['technician'].queryset.values_list('id', flat=True))

        self.assertIn(self.technician.id, technician_ids)
        self.assertNotIn(self.staff_profile.id, technician_ids)

    def test_staff_job_detail_reassign_list_excludes_staff_profiles(self):
        job = JobTicket.objects.create(
            job_code='GI-260407-001',
            customer_name='Reassign Customer',
            customer_phone='9876543210',
            device_type='Laptop',
            device_brand='Dell',
            device_model='Latitude',
            reported_issue='No power',
            assigned_to=self.technician,
        )

        request = RequestFactory().get(reverse('staff_job_detail', args=[job.job_code]))
        request.user = self.staff_user
        captured = {}

        def fake_render(_request, _template_name, context):
            captured['context'] = context
            return HttpResponse('ok')

        with patch('job_tickets.views.staff_views.render', side_effect=fake_render):
            response = staff_job_detail(request, job.job_code)

        self.assertEqual(response.status_code, 200)
        technician_ids = [tech.id for tech in captured['context']['technician_list']]
        self.assertIn(self.technician.id, technician_ids)
        self.assertNotIn(self.staff_profile.id, technician_ids)

    def test_staff_can_update_job_status_from_detail(self):
        job = JobTicket.objects.create(
            job_code='GI-260407-009',
            customer_name='Status Customer',
            customer_phone='9876543219',
            device_type='Laptop',
            device_brand='Dell',
            device_model='Latitude',
            reported_issue='Status update needed',
            status='Pending',
        )
        self.client.force_login(self.staff_user)

        response = self.client.post(
            reverse('staff_job_detail', args=[job.job_code]),
            {'action': 'update_status', 'status': 'Completed'},
        )

        self.assertEqual(response.status_code, 302)
        job.refresh_from_db()
        self.assertEqual(job.status, 'Completed')
        self.assertTrue(
            JobTicketLog.objects.filter(
                job_ticket=job,
                action='STATUS',
                details__icontains="Staff changed status",
            ).exists()
        )

    def test_close_job_saves_current_close_timestamp(self):
        job = JobTicket.objects.create(
            job_code='GI-260407-016',
            customer_name='Close Success',
            customer_phone='9876543226',
            device_type='Laptop',
            device_brand='HP',
            device_model='EliteBook',
            reported_issue='Close job',
            status='Ready for Pickup',
        )
        self.client.force_login(self.staff_user)
        today = timezone.localdate()

        response = self.client.post(
            reverse('close_job', args=[job.job_code]),
            {
                'next': reverse('staff_dashboard'),
            },
        )

        self.assertEqual(response.status_code, 302)
        job.refresh_from_db()
        self.assertEqual(job.status, 'Closed')
        self.assertIsNotNone(job.closed_at)
        self.assertEqual(timezone.localtime(job.closed_at).date(), today)

    def test_staff_billing_can_update_service_description(self):
        job = JobTicket.objects.create(
            job_code='GI-260407-015',
            customer_name='Description Customer',
            customer_phone='9876543225',
            device_type='Laptop',
            device_brand='Dell',
            device_model='Vostro',
            reported_issue='Description edit',
            status='Completed',
        )
        service_log = ServiceLog.objects.create(
            job_ticket=job,
            description='Old service description',
            part_cost=Decimal('100.00'),
            service_charge=Decimal('200.00'),
        )
        self.client.force_login(self.staff_user)

        response = self.client.post(
            reverse('job_billing_staff', args=[job.job_code]),
            {
                'update_amounts_submit': '1',
                f'description_{service_log.id}': 'Updated service description',
                f'part_cost_{service_log.id}': '100.00',
                f'service_charge_{service_log.id}': '200.00',
                'discount_amount': '0.00',
            },
        )

        self.assertEqual(response.status_code, 302)
        service_log.refresh_from_db()
        self.assertEqual(service_log.description, 'Updated service description')

    def test_staff_dashboard_job_create_can_schedule_reminder(self):
        self.client.force_login(self.staff_user)
        before = timezone.now()

        response = self.client.post(
            reverse('staff_dashboard'),
            {
                'job_ticket_form_submit': '1',
                'customer_name': 'Reminder Customer',
                'customer_phone': '9876543229',
                'estimated_amount': '',
                'estimated_delivery': '',
                'reminder_hours': '1',
                'reminder_minutes': '30',
                'device_forms[0].device_type': 'Laptop',
                'device_forms[0].device_brand': 'Dell',
                'device_forms[0].device_model': 'Latitude',
                'device_forms[0].device_serial': '',
                'device_forms[0].reported_issue': 'Estimate callback',
                'device_forms[0].additional_items': '',
            },
        )

        self.assertEqual(response.status_code, 302)
        job = JobTicket.objects.get(customer_name='Reminder Customer')
        reminder = JobReminder.objects.get(job_ticket=job)
        self.assertEqual(reminder.status, JobReminder.STATUS_PENDING)
        self.assertGreaterEqual(reminder.due_at, before + timedelta(hours=1, minutes=30))
        self.assertLessEqual(reminder.due_at, timezone.now() + timedelta(hours=1, minutes=31))

    def test_reminder_offset_bounds(self):
        from .views.staff_views import _parse_reminder_offset

        # Valid boundaries: 23 hours, 59 minutes
        delta, err = _parse_reminder_offset({'reminder_hours': '23', 'reminder_minutes': '59'})
        self.assertEqual(err, '')
        self.assertEqual(delta, timedelta(hours=23, minutes=59))

        delta, err = _parse_reminder_offset({'reminder_hours': '0', 'reminder_minutes': '0'})
        self.assertEqual(err, '')
        self.assertIsNone(delta)

        # Exceeding hour (> 23)
        delta, err = _parse_reminder_offset({'reminder_hours': '24', 'reminder_minutes': '0'})
        self.assertIsNone(delta)
        self.assertEqual(err, 'Reminder hour must be between 0 and 23.')

        # Exceeding minute (> 59)
        delta, err = _parse_reminder_offset({'reminder_hours': '0', 'reminder_minutes': '60'})
        self.assertIsNone(delta)
        self.assertEqual(err, 'Reminder minute must be between 0 and 59.')

        # Negative hour
        delta, err = _parse_reminder_offset({'reminder_hours': '-1', 'reminder_minutes': '0'})
        self.assertIsNone(delta)
        self.assertEqual(err, 'Reminder hour must be between 0 and 23.')

        # Negative minute
        delta, err = _parse_reminder_offset({'reminder_hours': '0', 'reminder_minutes': '-5'})
        self.assertIsNone(delta)
        self.assertEqual(err, 'Reminder minute must be between 0 and 59.')

    def test_staff_dashboard_job_create_reminder_bounds_rejection(self):
        self.client.force_login(self.staff_user)
        # Attempt with hours = 24
        response = self.client.post(
            reverse('staff_dashboard'),
            {
                'job_ticket_form_submit': '1',
                'customer_name': 'Invalid Reminder Hours',
                'customer_phone': '9876543228',
                'reminder_hours': '24',
                'reminder_minutes': '0',
                'device_forms[0].device_type': 'Laptop',
                'device_forms[0].device_brand': 'Dell',
                'device_forms[0].device_model': 'Latitude',
                'device_forms[0].reported_issue': 'Test',
            },
            HTTP_X_REQUESTED_WITH='XMLHttpRequest',
        )
        self.assertEqual(response.status_code, 400)
        data = response.json()
        self.assertFalse(data.get('success'))
        self.assertEqual(data.get('message'), 'Reminder hour must be between 0 and 23.')

        # Attempt with minutes = 60
        response = self.client.post(
            reverse('staff_dashboard'),
            {
                'job_ticket_form_submit': '1',
                'customer_name': 'Invalid Reminder Mins',
                'customer_phone': '9876543228',
                'reminder_hours': '0',
                'reminder_minutes': '60',
                'device_forms[0].device_type': 'Laptop',
                'device_forms[0].device_brand': 'Dell',
                'device_forms[0].device_model': 'Latitude',
                'device_forms[0].reported_issue': 'Test',
            },
            HTTP_X_REQUESTED_WITH='XMLHttpRequest',
        )
        self.assertEqual(response.status_code, 400)
        data = response.json()
        self.assertFalse(data.get('success'))
        self.assertEqual(data.get('message'), 'Reminder minute must be between 0 and 59.')

    def test_staff_dashboard_job_create_with_custom_job_date(self):
        self.client.force_login(self.staff_user)
        past_date = (timezone.localdate() - timedelta(days=5)).strftime('%Y-%m-%d')
        response = self.client.post(
            reverse('staff_dashboard'),
            {
                'job_ticket_form_submit': '1',
                'customer_name': 'Custom Date Customer',
                'customer_phone': '9876543299',
                'job_date': past_date,
                'device_forms[0].device_type': 'Laptop',
                'device_forms[0].device_brand': 'Dell',
                'device_forms[0].device_model': 'Inspiron',
                'device_forms[0].reported_issue': 'Keyboard issue',
            },
        )
        self.assertEqual(response.status_code, 302)
        job = JobTicket.objects.get(customer_name='Custom Date Customer')
        self.assertEqual(job.intake_date.strftime('%Y-%m-%d'), past_date)
        self.assertEqual(job.display_intake_date.strftime('%Y-%m-%d'), past_date)
        self.assertEqual(timezone.localtime(job.created_at).date(), timezone.localdate())

    def test_staff_job_detail_edit_intake_date(self):
        job = JobTicket.objects.create(
            job_code='GI-260407-099',
            customer_name='Edit Intake Customer',
            customer_phone='9876543298',
            device_type='Laptop',
            reported_issue='Screen flicker',
            intake_date=timezone.localdate(),
        )
        self.client.force_login(self.staff_user)
        past_date = (timezone.localdate() - timedelta(days=7)).strftime('%Y-%m-%d')
        response = self.client.post(
            reverse('staff_job_detail', args=[job.job_code]),
            {
                'action': 'edit_customer_device_details',
                'customer_name': 'Edit Intake Customer',
                'customer_phone': '9876543298',
                'device_type': 'Laptop',
                'reported_issue': 'Screen flicker',
                'intake_date': past_date,
            },
        )
        self.assertEqual(response.status_code, 302)
        job.refresh_from_db()
        self.assertEqual(job.intake_date.strftime('%Y-%m-%d'), past_date)
        self.assertTrue(
            JobTicketLog.objects.filter(
                job_ticket=job,
                action='DETAILS',
                details__icontains='Intake Date:',
            ).exists()
        )

    def test_staff_job_detail_can_schedule_reminder_for_selected_datetime(self):
        job = JobTicket.objects.create(
            job_code='GI-260407-017',
            customer_name='Callback Date Customer',
            customer_phone='9876543227',
            device_type='Laptop',
            device_brand='Lenovo',
            device_model='ThinkPad',
            reported_issue='Call customer with estimate',
        )
        self.client.force_login(self.staff_user)
        reminder_date = timezone.localdate() + timedelta(days=1)

        response = self.client.post(
            reverse('staff_job_detail', args=[job.job_code]),
            {
                'action': 'schedule_reminder',
                'reminder_date': reminder_date.isoformat(),
                'reminder_time': '09:30',
            },
        )

        self.assertEqual(response.status_code, 302)
        reminder = JobReminder.objects.get(job_ticket=job)
        local_due_at = timezone.localtime(reminder.due_at)
        self.assertEqual(local_due_at.date(), reminder_date)
        self.assertEqual(local_due_at.strftime('%H:%M'), '09:30')

    def test_staff_job_detail_rejects_reminder_time_outside_working_hours(self):
        job = JobTicket.objects.create(
            job_code='GI-260407-018',
            customer_name='Callback Limit Customer',
            customer_phone='9876543228',
            device_type='Laptop',
            device_brand='HP',
            device_model='Pavilion',
            reported_issue='Call outside time',
        )
        self.client.force_login(self.staff_user)

        response = self.client.post(
            reverse('staff_job_detail', args=[job.job_code]),
            {
                'action': 'schedule_reminder',
                'reminder_date': (timezone.localdate() + timedelta(days=1)).isoformat(),
                'reminder_time': '22:30',
            },
        )

        self.assertEqual(response.status_code, 302)
        self.assertFalse(JobReminder.objects.filter(job_ticket=job).exists())

    def test_due_reminders_api_prompts_once_per_cooldown(self):
        job = JobTicket.objects.create(
            job_code='GI-260407-012',
            customer_name='Due Reminder Customer',
            customer_phone='9876543222',
            device_type='Laptop',
            device_brand='Acer',
            device_model='Swift',
            reported_issue='Call with estimate',
        )
        reminder = JobReminder.objects.create(
            job_ticket=job,
            due_at=timezone.now() - timedelta(minutes=2),
            created_by=self.staff_user,
        )
        self.client.force_login(self.staff_user)

        first_response = self.client.get(reverse('due_job_reminders_api'))

        self.assertEqual(first_response.status_code, 200)
        first_payload = first_response.json()
        self.assertTrue(first_payload['ok'])
        self.assertEqual(first_payload['count'], 1)
        self.assertEqual(first_payload['reminders'][0]['job_code'], job.job_code)
        reminder.refresh_from_db()
        self.assertIsNotNone(reminder.last_prompted_at)

        second_response = self.client.get(reverse('due_job_reminders_api'))

        self.assertEqual(second_response.status_code, 200)
        self.assertEqual(second_response.json()['count'], 0)

    def test_staff_can_save_estimation_note_from_job_detail(self):
        job = JobTicket.objects.create(
            job_code='GI-260407-013',
            customer_name='Estimate Customer',
            customer_phone='9876543223',
            device_type='Laptop',
            device_brand='HP',
            device_model='Pavilion',
            reported_issue='Estimate needed',
        )
        self.client.force_login(self.staff_user)

        response = self.client.post(
            reverse('staff_job_detail', args=[job.job_code]),
            {
                'action': 'save_estimation',
                'estimated_amount': '1850.50',
                'estimation_note': 'Motherboard cleaning and OS service.',
            },
        )

        self.assertEqual(response.status_code, 302)
        job.refresh_from_db()
        self.assertEqual(job.estimated_amount, Decimal('1850.50'))
        self.assertEqual(job.estimation_note, 'Motherboard cleaning and OS service.')
        self.assertTrue(
            JobTicketLog.objects.filter(
                job_ticket=job,
                action='NOTE',
                details__icontains='estimate note updated',
            ).exists()
        )

    @patch('job_tickets.whatsapp_service.requests.request')
    def test_send_estimation_whatsapp_marks_active_reminder_done(self, mock_request):
        mock_response = Mock()
        mock_response.ok = True
        mock_response.status_code = 200
        mock_response.json.return_value = {'messages': [{'id': 'wamid.estimate123'}]}
        mock_request.return_value = mock_response

        settings_obj = WhatsAppIntegrationSettings.get_settings()
        settings_obj.delivery_method = WhatsAppIntegrationSettings.DELIVERY_CLOUD_API
        settings_obj.phone_number_id = '123456789012345'
        settings_obj.access_token = 'token-123'
        settings_obj.estimate_template_name = 'job_estimate_update'
        settings_obj.estimate_template = 'Hello {customer_name}, estimate for {job_code} is {estimated_amount}. Note: {estimation_note}'
        settings_obj.save()

        job = JobTicket.objects.create(
            job_code='GI-260407-014',
            customer_name='WhatsApp Estimate',
            customer_phone='9876543224',
            device_type='Laptop',
            device_brand='Lenovo',
            device_model='IdeaPad',
            reported_issue='Send estimate',
        )
        reminder = JobReminder.objects.create(
            job_ticket=job,
            due_at=timezone.now() - timedelta(minutes=5),
            created_by=self.staff_user,
        )
        self.client.force_login(self.staff_user)

        with self.captureOnCommitCallbacks(execute=True):
            response = self.client.post(
                reverse('staff_job_detail', args=[job.job_code]),
                {
                    'action': 'send_estimation_whatsapp',
                    'estimated_amount': '2400',
                    'estimation_note': 'Display cable replacement.',
                },
            )

        self.assertEqual(response.status_code, 302)
        job.refresh_from_db()
        reminder.refresh_from_db()
        self.assertEqual(job.estimated_amount, Decimal('2400.00'))
        self.assertEqual(reminder.status, JobReminder.STATUS_DONE)
        queue = MessageQueue.objects.get(job_ticket=job, event_type=MessageQueue.EVENT_ESTIMATE)
        self.assertEqual(queue.status, MessageQueue.STATUS_SENT)
        self.assertIn('estimate for GI-260407-014 is Rs 2400.00', queue.message)
        self.assertEqual(queue.bridge_message_id, 'wamid.estimate123')
        payload = mock_request.call_args.kwargs['json']
        self.assertEqual(payload['template']['name'], 'job_estimate_update')
        parameters = payload['template']['components'][0]['parameters']
        self.assertEqual(parameters[0]['text'], 'WhatsApp Estimate')
        self.assertEqual(parameters[2]['text'], 'Rs 2400.00')
        self.assertEqual(parameters[3]['text'], 'Display cable replacement.')

    def test_vendor_return_records_bill_as_balance_without_initial_payment(self):
        vendor = Vendor.objects.create(company_name='Board Lab', name='Ravi')
        job = JobTicket.objects.create(
            job_code='GI-260407-010',
            customer_name='Vendor Customer',
            customer_phone='9876543220',
            device_type='Laptop',
            device_brand='HP',
            device_model='EliteBook',
            reported_issue='Board repair',
            status='Specialized Service',
        )
        service = SpecializedService.objects.create(
            job_ticket=job,
            vendor=vendor,
            status='Sent to Vendor',
            sent_date=timezone.now(),
        )
        self.client.force_login(self.staff_user)

        response = self.client.post(
            reverse('mark_service_returned', args=[service.id]),
            {
                'vendor_cost': '2500.00',
                'client_charge': '3500.00',
                'vendor_bill_number': 'INV-2024-TEST',
                'return_date': '2026-10-02',
            },
        )

        self.assertEqual(response.status_code, 302)
        service.refresh_from_db()
        job.refresh_from_db()
        self.assertEqual(service.status, 'Returned from Vendor')
        self.assertEqual(service.vendor_bill_number, 'INV-2024-TEST')
        self.assertEqual(service.vendor_cost, Decimal('2500.00'))
        self.assertEqual(service.vendor_discount_amount, Decimal('0.00'))
        self.assertEqual(service.vendor_paid_amount, Decimal('0.00'))
        self.assertEqual(service.vendor_balance_amount, Decimal('2500.00'))
        self.assertEqual(service.vendor_net_payable, Decimal('2500.00'))
        self.assertEqual(service.vendor_payment_status, 'Balance Due')
        self.assertEqual(job.status, 'Repairing')
        self.assertFalse(VendorPayment.objects.filter(specialized_service=service).exists())
        self.assertTrue(
            ServiceLog.objects.filter(
                job_ticket=job,
                description='Specialized Service - Board Lab',
                service_charge=Decimal('3500.00'),
            ).exists()
        )

    def test_vendor_partial_payment_transaction_updates_balance(self):
        vendor = Vendor.objects.create(company_name='Chip Works', name='Meera')
        job = JobTicket.objects.create(
            job_code='GI-260407-011',
            customer_name='Payment Customer',
            customer_phone='9876543221',
            device_type='Laptop',
            device_brand='Lenovo',
            device_model='ThinkPad',
            reported_issue='Chip repair',
            status='Repairing',
        )
        service = SpecializedService.objects.create(
            job_ticket=job,
            vendor=vendor,
            status='Returned from Vendor',
            vendor_cost=Decimal('3000.00'),
            vendor_discount_amount=Decimal('500.00'),
            vendor_paid_amount=Decimal('1000.00'),
            vendor_balance_amount=Decimal('1500.00'),
            client_charge=Decimal('4200.00'),
            sent_date=timezone.now(),
            returned_date=timezone.now(),
        )
        self.client.force_login(self.staff_user)

        response = self.client.post(
            reverse('record_vendor_payment', args=[vendor.id]),
            {
                'payment_scope': 'single',
                'specialized_service_id': str(service.id),
                'payment_amount': '700.00',
                'payment_method': VendorPayment.METHOD_TRANSFER,
                'payment_date': timezone.localdate().strftime('%Y-%m-%d'),
                'reference_no': 'TXN-77',
            },
        )

        self.assertEqual(response.status_code, 302)
        service.refresh_from_db()
        self.assertEqual(service.vendor_paid_amount, Decimal('1700.00'))
        self.assertEqual(service.vendor_balance_amount, Decimal('800.00'))
        payment = VendorPayment.objects.get(reference_no='TXN-77')
        self.assertEqual(payment.amount, Decimal('700.00'))
        self.assertEqual(payment.balance_before, Decimal('1500.00'))
        self.assertEqual(payment.balance_after, Decimal('800.00'))
        self.assertEqual(payment.payment_method, VendorPayment.METHOD_TRANSFER)

    def test_vendor_report_csv_exports_jobs_and_payments(self):
        vendor = Vendor.objects.create(company_name='CSV Vendor Lab', name='Rafi')
        job = JobTicket.objects.create(
            job_code='GI-260407-018',
            customer_name='CSV Customer',
            customer_phone='9876543228',
            device_type='Laptop',
            device_brand='HP',
            device_model='ProBook',
            reported_issue='Vendor CSV export',
            status='Repairing',
        )
        service = SpecializedService.objects.create(
            job_ticket=job,
            vendor=vendor,
            status='Returned from Vendor',
            vendor_cost=Decimal('2500.00'),
            vendor_discount_amount=Decimal('500.00'),
            vendor_paid_amount=Decimal('700.00'),
            vendor_balance_amount=Decimal('1300.00'),
            client_charge=Decimal('3500.00'),
            sent_date=timezone.now() - timedelta(days=1),
            returned_date=timezone.now(),
        )
        VendorPayment.objects.create(
            vendor=vendor,
            specialized_service=service,
            payment_date=timezone.localdate(),
            payment_method=VendorPayment.METHOD_TRANSFER,
            amount=Decimal('700.00'),
            balance_before=Decimal('2000.00'),
            balance_after=Decimal('1300.00'),
            reference_no='CSV-TXN-01',
            created_by=self.staff_user,
        )
        self.client.force_login(self.staff_user)

        response = self.client.get(
            reverse('vendor_report_export_csv', args=[vendor.id]),
            {
                'start_date': timezone.localdate().isoformat(),
                'end_date': timezone.localdate().isoformat(),
            },
        )

        self.assertEqual(response.status_code, 200)
        self.assertIn('text/csv', response['Content-Type'])
        body = response.content.decode()
        self.assertIn('Vendor Report', body)
        self.assertIn(job.job_code, body)
        self.assertIn('2000.00', body)
        self.assertIn('CSV-TXN-01', body)
        self.assertIn(reverse('staff_job_detail', args=[job.job_code]), body)

    def test_vendor_bulk_payment_auto_allocates_oldest_balances(self):
        vendor = Vendor.objects.create(company_name='Bulk Pay Lab', name='Faisal')
        old_job = JobTicket.objects.create(
            job_code='GI-260407-015',
            customer_name='Old Balance',
            customer_phone='9876543225',
            device_type='Laptop',
            device_brand='Dell',
            device_model='Latitude',
            reported_issue='Board repair',
            status='Repairing',
        )
        new_job = JobTicket.objects.create(
            job_code='GI-260407-016',
            customer_name='New Balance',
            customer_phone='9876543226',
            device_type='Mobile',
            device_brand='Apple',
            device_model='iPhone',
            reported_issue='Display repair',
            status='Repairing',
        )
        old_service = SpecializedService.objects.create(
            job_ticket=old_job,
            vendor=vendor,
            status='Returned from Vendor',
            vendor_cost=Decimal('100.00'),
            vendor_paid_amount=Decimal('0.00'),
            vendor_balance_amount=Decimal('100.00'),
            client_charge=Decimal('180.00'),
            returned_date=timezone.now() - timedelta(days=2),
        )
        new_service = SpecializedService.objects.create(
            job_ticket=new_job,
            vendor=vendor,
            status='Returned from Vendor',
            vendor_cost=Decimal('150.00'),
            vendor_paid_amount=Decimal('0.00'),
            vendor_balance_amount=Decimal('150.00'),
            client_charge=Decimal('250.00'),
            returned_date=timezone.now() - timedelta(days=1),
        )
        self.client.force_login(self.staff_user)

        response = self.client.post(
            reverse('record_vendor_payment', args=[vendor.id]),
            {
                'payment_scope': 'bulk',
                'payment_amount': '180.00',
                'payment_method': VendorPayment.METHOD_CASH,
                'payment_date': timezone.localdate().strftime('%Y-%m-%d'),
                'reference_no': 'BULK-01',
            },
        )

        self.assertEqual(response.status_code, 302)
        old_service.refresh_from_db()
        new_service.refresh_from_db()
        self.assertEqual(old_service.vendor_paid_amount, Decimal('100.00'))
        self.assertEqual(old_service.vendor_balance_amount, Decimal('0.00'))
        self.assertEqual(new_service.vendor_paid_amount, Decimal('80.00'))
        self.assertEqual(new_service.vendor_balance_amount, Decimal('70.00'))

        payments = list(VendorPayment.objects.filter(reference_no='BULK-01').order_by('id'))
        self.assertEqual(len(payments), 2)
        self.assertEqual(payments[0].specialized_service, old_service)
        self.assertEqual(payments[0].amount, Decimal('100.00'))
        self.assertEqual(payments[0].balance_after, Decimal('0.00'))
        self.assertEqual(payments[1].specialized_service, new_service)
        self.assertEqual(payments[1].amount, Decimal('80.00'))
        self.assertEqual(payments[1].balance_after, Decimal('70.00'))

    def test_vendor_job_billing_lines_can_be_deleted_from_staff_billing(self):
        vendor = Vendor.objects.create(company_name='No Delete Lab', name='Arun')
        job = JobTicket.objects.create(
            job_code='GI-260407-017',
            customer_name='Vendor Delete Guard',
            customer_phone='9876543227',
            device_type='Laptop',
            device_brand='Dell',
            device_model='Inspiron',
            reported_issue='Vendor line guard',
            status='Repairing',
        )
        SpecializedService.objects.create(
            job_ticket=job,
            vendor=vendor,
            status='Returned from Vendor',
            vendor_cost=Decimal('1000.00'),
            client_charge=Decimal('1500.00'),
            returned_date=timezone.now(),
        )
        service_log = ServiceLog.objects.create(
            job_ticket=job,
            description='Specialized Service - No Delete Lab',
            part_cost=Decimal('0.00'),
            service_charge=Decimal('1500.00'),
        )
        self.client.force_login(self.staff_user)

        response = self.client.post(
            reverse('job_billing_staff', args=[job.job_code]),
            {
                'update_amounts_submit': '1',
                'delete_service_ids[]': [str(service_log.id)],
            },
        )

        self.assertEqual(response.status_code, 302)
        self.assertFalse(ServiceLog.objects.filter(id=service_log.id).exists())

    def test_laptop_job_can_be_completed_without_checklist_when_toggle_is_off(self):
        job = JobTicket.objects.create(
            job_code='GI-260407-002',
            customer_name='Checklist Optional',
            customer_phone='9876543211',
            device_type='Laptop',
            device_brand='HP',
            device_model='15s',
            reported_issue='Boot issue',
            status='Under Inspection',
            assigned_to=self.technician,
            requires_laptop_inspection_checklist=False,
        )
        self.client.force_login(self.technician_user)

        response = self.client.post(reverse('job_mark_completed', args=[job.job_code]))

        self.assertEqual(response.status_code, 302)
        job.refresh_from_db()
        self.assertEqual(job.status, 'Completed')

    def test_laptop_job_still_requires_checklist_when_toggle_is_on(self):
        job = JobTicket.objects.create(
            job_code='GI-260407-003',
            customer_name='Checklist Required',
            customer_phone='9876543212',
            device_type='Laptop',
            device_brand='Lenovo',
            device_model='ThinkPad',
            reported_issue='Slow performance',
            status='Under Inspection',
            assigned_to=self.technician,
            requires_laptop_inspection_checklist=True,
        )
        self.client.force_login(self.technician_user)

        response = self.client.post(reverse('job_mark_completed', args=[job.job_code]))

        self.assertEqual(response.status_code, 302)
        job.refresh_from_db()
        self.assertEqual(job.status, 'Under Inspection')


class GstMasterFormTests(TestCase):
    def test_inventory_party_form_normalizes_gst_fields_and_derives_state_codes(self):
        form = InventoryPartyForm(
            data={
                'name': 'Acme Systems',
                'legal_name': 'Acme Systems Private Limited',
                'contact_person': 'Arun',
                'gst_registration_type': 'registered',
                'phone': '9876543210',
                'gstin': '32abcde1234f1z5',
                'state_code': '',
                'default_place_of_supply_state': '',
                'pan': 'abcde1234f',
                'email': 'billing@example.com',
                'address': 'Main billing address',
                'shipping_address': '',
                'city': 'Kochi',
                'state': 'Kerala',
                'country': '',
                'pincode': '682001',
                'opening_balance': '0.00',
                'is_active': 'on',
            }
        )

        self.assertTrue(form.is_valid(), form.errors)
        party = form.save()

        self.assertEqual(party.gstin, '32ABCDE1234F1Z5')
        self.assertEqual(party.pan, 'ABCDE1234F')
        self.assertEqual(party.state_code, '32')
        self.assertEqual(party.default_place_of_supply_state, '32')
        self.assertEqual(party.country, 'India')
        self.assertEqual(party.party_type, 'both')

    def test_inventory_party_form_rejects_state_code_mismatch(self):
        form = InventoryPartyForm(
            data={
                'name': 'Mismatch Traders',
                'legal_name': '',
                'contact_person': '',
                'gst_registration_type': 'registered',
                'phone': '',
                'gstin': '32ABCDE1234F1Z5',
                'state_code': '33',
                'default_place_of_supply_state': '',
                'pan': '',
                'email': '',
                'address': '',
                'shipping_address': '',
                'city': '',
                'state': '',
                'country': 'India',
                'pincode': '',
                'opening_balance': '0.00',
                'is_active': 'on',
            }
        )

        self.assertFalse(form.is_valid())
        self.assertIn('state_code', form.errors)

    def test_product_form_normalizes_codes(self):
        form = ProductForm(
            data={
                'name': 'Premium SSD',
                'category': 'Storage',
                'brand': 'WD',
                'item_type': 'goods',
                'hsn_sac_code': '8471',
                'uqc': 'nos',
                'tax_category': 'taxable',
                'gst_rate': '18.00',
                'cess_rate': '0.00',
                'is_tax_inclusive_default': 'on',
                'cost_price': '3200.00',
                'unit_price': '4500.00',
                'stock_quantity': '10',
                'reserved_stock': '2',
                'description': '',
                'purchase_price_tax_mode': 'without_tax',
                'sales_price_tax_mode': 'without_tax',
            }
        )

        self.assertTrue(form.is_valid(), form.errors)
        product = form.save()

        self.assertEqual(product.hsn_sac_code, '8471')
        self.assertEqual(product.uqc, 'NOS')
        self.assertEqual(product.tax_category, 'taxable')

    def test_product_form_requires_zero_tax_for_exempt_items(self):
        form = ProductForm(
            data={
                'name': 'Exempt Service',
                'category': 'Support',
                'brand': '',
                'item_type': 'service',
                'hsn_sac_code': '9987',
                'uqc': '',
                'tax_category': 'exempt',
                'gst_rate': '18.00',
                'cess_rate': '0.00',
                'cost_price': '0.00',
                'unit_price': '1500.00',
                'stock_quantity': '0',
                'reserved_stock': '0',
                'description': '',
                'purchase_price_tax_mode': 'without_tax',
                'sales_price_tax_mode': 'without_tax',
            }
        )

        self.assertFalse(form.is_valid())
        self.assertIn('gst_rate', form.errors)

    def test_company_profile_form_normalizes_tax_identity_and_defaults_pos(self):
        profile = CompanyProfile.get_profile()
        form = CompanyProfileForm(
            data={
                'company_name': 'GI Service Billing',
                'legal_name': 'GI Hostings Private Limited',
                'tagline': 'Service Billing',
                'logo': '',
                'logo_url': '',
                'address': 'Kochi',
                'city': 'Kochi',
                'state': 'Kerala',
                'pincode': '682001',
                'phone1': '9876543210',
                'phone2': '',
                'email': 'hello@example.com',
                'website': 'https://example.com',
                'gstin': '32abcde1234f1z5',
                'pan': 'abcde1234f',
                'state_code': '',
                'registration_type': 'regular',
                'filing_frequency': 'quarterly',
                'qrmp_enabled': 'on',
                'lut_bond_enabled': '',
                'annual_turnover_band': 'up_to_5cr',
                'e_invoice_applicable': '',
                'default_place_of_supply_state': '',
                'bank_name': '',
                'account_number': '',
                'ifsc_code': '',
                'branch': '',
                'upi_id': '',
                'job_code_prefix': 'GI',
                'job_ticket_print_paper_size': CompanyProfile.PRINT_PAPER_A5,
                'bill_print_paper_size': CompanyProfile.PRINT_PAPER_A4,
                'enable_gst': 'on',
                'gst_rate': '18.00',
                'terms_conditions': 'Standard terms',
                'technician_display_format': 'id',
            },
            instance=profile,
        )

        self.assertTrue(form.is_valid(), form.errors)
        saved_profile = form.save()

        self.assertEqual(saved_profile.gstin, '32ABCDE1234F1Z5')
        self.assertEqual(saved_profile.pan, 'ABCDE1234F')
        self.assertEqual(saved_profile.state_code, '32')
        self.assertEqual(saved_profile.default_place_of_supply_state, '32')

    def test_company_profile_form_rejects_qrmp_with_monthly_filing(self):
        profile = CompanyProfile.get_profile()
        form = CompanyProfileForm(
            data={
                'company_name': 'GI Service Billing',
                'legal_name': 'GI Hostings Private Limited',
                'tagline': 'Service Billing',
                'logo': '',
                'logo_url': '',
                'address': 'Kochi',
                'city': 'Kochi',
                'state': 'Kerala',
                'pincode': '682001',
                'phone1': '9876543210',
                'phone2': '',
                'email': 'hello@example.com',
                'website': 'https://example.com',
                'gstin': '32ABCDE1234F1Z5',
                'pan': 'ABCDE1234F',
                'state_code': '32',
                'registration_type': 'regular',
                'filing_frequency': 'monthly',
                'qrmp_enabled': 'on',
                'lut_bond_enabled': '',
                'annual_turnover_band': 'up_to_5cr',
                'e_invoice_applicable': '',
                'default_place_of_supply_state': '32',
                'bank_name': '',
                'account_number': '',
                'ifsc_code': '',
                'branch': '',
                'upi_id': '',
                'job_code_prefix': 'GI',
                'job_ticket_print_paper_size': CompanyProfile.PRINT_PAPER_A5,
                'bill_print_paper_size': CompanyProfile.PRINT_PAPER_A4,
                'enable_gst': 'on',
                'gst_rate': '18.00',
                'terms_conditions': 'Standard terms',
            },
            instance=profile,
        )

        self.assertFalse(form.is_valid())
        self.assertIn('filing_frequency', form.errors)


class WhatsAppCloudApiTests(TestCase):
    def setUp(self):
        self.settings_obj = WhatsAppIntegrationSettings.get_settings()

    def test_enabled_settings_require_cloud_api_credentials_and_template_names(self):
        form = WhatsAppIntegrationSettingsForm(
            data={
                'is_enabled': 'on',
                'delivery_method': WhatsAppIntegrationSettings.DELIVERY_CLOUD_API,
                'bridge_base_url': 'http://127.0.0.1:3001',
                'api_version': '',
                'phone_number_id': '',
                'access_token': '',
                'webhook_verify_token': '',
                'app_secret': '',
                'public_site_url': 'https://example.com',
                'default_country_code': '91',
                'template_language_code': '',
                'test_template_name': 'hello_world',
                'notify_on_created': 'on',
                'notify_on_completed': 'on',
                'notify_on_delivered': 'on',
                'created_template_name': '',
                'created_template': 'Hello {customer_name}',
                'completed_template_name': 'job_completed_update',
                'completed_template': 'Hello {customer_name}',
                'delivered_template_name': '',
                'delivered_template': 'Hello {customer_name}',
                'estimate_template_name': '',
                'estimate_template': 'Estimate {estimated_amount}',
            },
            instance=self.settings_obj,
        )

        self.assertFalse(form.is_valid())
        self.assertIn('api_version', form.errors)
        self.assertIn('phone_number_id', form.errors)
        self.assertIn('access_token', form.errors)
        self.assertIn('template_language_code', form.errors)
        self.assertNotIn('created_template_name', form.errors)
        self.assertIn('delivered_template_name', form.errors)
        self.assertIn('estimate_template_name', form.errors)

    def test_bridge_settings_do_not_require_cloud_credentials_or_template_names(self):
        form = WhatsAppIntegrationSettingsForm(
            data={
                'is_enabled': 'on',
                'delivery_method': WhatsAppIntegrationSettings.DELIVERY_BRIDGE,
                'bridge_base_url': 'http://127.0.0.1:3001',
                'api_version': '',
                'phone_number_id': '',
                'access_token': '',
                'webhook_verify_token': '',
                'app_secret': '',
                'public_site_url': 'https://example.com',
                'default_country_code': '91',
                'template_language_code': '',
                'test_template_name': '',
                'notify_on_created': 'on',
                'notify_on_completed': 'on',
                'notify_on_delivered': 'on',
                'created_template_name': '',
                'created_template': 'Hello {customer_name}, ticket {job_code}',
                'completed_template_name': '',
                'completed_template': 'Completed {job_code}',
                'delivered_template_name': '',
                'delivered_template': 'Closed {job_code}',
                'estimate_template_name': '',
                'estimate_template': 'Estimate {estimated_amount}',
            },
            instance=self.settings_obj,
        )

        self.assertTrue(form.is_valid(), form.errors)

    @patch('job_tickets.whatsapp_service.transaction.on_commit', side_effect=lambda callback: callback())
    @patch('job_tickets.whatsapp_service.requests.request')
    def test_job_notification_uses_cloud_document_delivery_for_created_ticket(self, mock_request, _mock_on_commit):
        job = JobTicket.objects.create(
            job_code='GI-260420-301',
            customer_name='Anand',
            customer_phone='9876543210',
            device_type='Laptop',
            device_brand='Dell',
            device_model='Latitude',
            reported_issue='Battery issue',
        )

        self.settings_obj.is_enabled = True
        self.settings_obj.api_version = 'v23.0'
        self.settings_obj.phone_number_id = '123456789012345'
        self.settings_obj.access_token = 'token-123'
        self.settings_obj.public_site_url = 'https://botgi.example.com'
        self.settings_obj.template_language_code = 'en_US'
        self.settings_obj.notify_on_created = True
        self.settings_obj.created_template_name = 'job_created_update'
        self.settings_obj.created_template = 'Hello {customer_name}, ticket {job_code}. Receipt: {receipt_link}'
        self.settings_obj.save()

        mock_response = Mock()
        mock_response.ok = True
        mock_response.status_code = 200
        mock_response.json.return_value = {'messages': [{'id': 'wamid.HBgM123'}]}
        mock_request.return_value = mock_response

        result = send_job_whatsapp_notification(job, MessageQueue.EVENT_CREATED)

        self.assertTrue(result['ok'])
        queue = MessageQueue.objects.get(job_ticket=job, event_type=MessageQueue.EVENT_CREATED)
        self.assertEqual(queue.status, MessageQueue.STATUS_SENT)
        self.assertEqual(queue.transport, 'whatsapp-cloud-api-document')
        self.assertEqual(queue.bridge_message_id, 'wamid.HBgM123')
        self.assertIn('/client-receipt/', queue.pdf_url)
        self.assertIn('/pdf/', queue.pdf_url)
        self.assertEqual(queue.filename, f'{job.job_code}.pdf')
        self.assertIn('/client-receipt/', queue.caption)
        self.assertIn('/client-status/', queue.caption)

        payload = mock_request.call_args.kwargs['json']
        self.assertEqual(payload['type'], 'document')
        self.assertEqual(payload['to'], '919876543210')
        self.assertEqual(payload['document']['link'], queue.pdf_url)
        self.assertEqual(payload['document']['filename'], f'{job.job_code}.pdf')
        self.assertIn('/client-receipt/', payload['document']['caption'])

        pdf_path = queue.pdf_url.replace('https://botgi.example.com', '')
        pdf_response = self.client.get(pdf_path)
        self.assertEqual(pdf_response.status_code, 200)
        self.assertEqual(pdf_response['Content-Type'], 'application/pdf')
        self.assertTrue(pdf_response.content.startswith(b'%PDF-'))

    @patch('job_tickets.whatsapp_service.transaction.on_commit', side_effect=lambda callback: callback())
    @patch('job_tickets.whatsapp_service.requests.request')
    def test_job_notification_uses_bridge_document_delivery_for_created_ticket(self, mock_request, _mock_on_commit):
        job = JobTicket.objects.create(
            job_code='GI-260420-304',
            customer_name='Nikhil',
            customer_phone='9876543210',
            device_type='Laptop',
            device_brand='Acer',
            device_model='Aspire',
            reported_issue='Keyboard issue',
        )

        self.settings_obj.is_enabled = True
        self.settings_obj.delivery_method = WhatsAppIntegrationSettings.DELIVERY_BRIDGE
        self.settings_obj.bridge_base_url = 'http://127.0.0.1:3001'
        self.settings_obj.public_site_url = 'https://botgi.example.com'
        self.settings_obj.default_country_code = '91'
        self.settings_obj.notify_on_created = True
        self.settings_obj.created_template_name = ''
        self.settings_obj.created_template = 'Hello {customer_name}, ticket {job_code}. Receipt: {receipt_link}'
        self.settings_obj.save()

        mock_response = Mock()
        mock_response.ok = True
        mock_response.status_code = 200
        mock_response.json.return_value = {
            'ok': True,
            'messageId': 'bridge-msg-123',
            'delivery': {'messageId': 'bridge-msg-123'},
        }
        mock_request.return_value = mock_response

        result = send_job_whatsapp_notification(job, MessageQueue.EVENT_CREATED)

        self.assertTrue(result['ok'])
        queue = MessageQueue.objects.get(job_ticket=job, event_type=MessageQueue.EVENT_CREATED)
        self.assertEqual(queue.status, MessageQueue.STATUS_SENT)
        self.assertEqual(queue.transport, 'whatsapp-bridge-document')
        self.assertEqual(queue.bridge_message_id, 'bridge-msg-123')

        self.assertEqual(mock_request.call_args.args[0], 'POST')
        self.assertEqual(mock_request.call_args.args[1], 'http://127.0.0.1:3001/api/messages/send-pdf')
        payload = mock_request.call_args.kwargs['json']
        self.assertEqual(payload['to'], '919876543210')
        self.assertIn('/client-receipt/', payload['pdf_url'])
        self.assertIn('/client-receipt/', payload['caption'])
        self.assertEqual(payload['filename'], f'{job.job_code}.pdf')

    @patch('job_tickets.whatsapp_service.transaction.on_commit', side_effect=lambda callback: callback())
    @patch('job_tickets.whatsapp_service.requests.request')
    def test_job_notification_retries_base_language_when_translation_missing(self, mock_request, _mock_on_commit):
        job = JobTicket.objects.create(
            job_code='GI-260420-302',
            customer_name='Arun',
            customer_phone='9876543210',
            device_type='Laptop',
            device_brand='HP',
            device_model='Pavilion',
            reported_issue='No display',
        )

        self.settings_obj.is_enabled = True
        self.settings_obj.api_version = 'v23.0'
        self.settings_obj.phone_number_id = '123456789012345'
        self.settings_obj.access_token = 'token-123'
        self.settings_obj.public_site_url = 'https://botgi.example.com'
        self.settings_obj.template_language_code = 'en_US'
        self.settings_obj.notify_on_completed = True
        self.settings_obj.completed_template_name = 'job_completed_update'
        self.settings_obj.completed_template = 'Hello {customer_name}, ticket {job_code}. Track: {status_link}'
        self.settings_obj.save()

        translation_missing_response = Mock()
        translation_missing_response.ok = False
        translation_missing_response.status_code = 404
        translation_missing_response.json.return_value = {
            'error': {
                'message': '(#132001) Template name does not exist in the translation',
                'type': 'OAuthException',
                'code': 132001,
                'error_data': {
                    'details': 'template name (job_completed_update) does not exist in en_US',
                },
            }
        }

        success_response = Mock()
        success_response.ok = True
        success_response.status_code = 200
        success_response.json.return_value = {'messages': [{'id': 'wamid.HBgM456'}]}
        mock_request.side_effect = [translation_missing_response, success_response]

        result = send_job_whatsapp_notification(job, MessageQueue.EVENT_COMPLETED)

        self.assertTrue(result['ok'])
        self.assertEqual(mock_request.call_count, 2)
        first_payload = mock_request.call_args_list[0].kwargs['json']
        second_payload = mock_request.call_args_list[1].kwargs['json']
        self.assertEqual(first_payload['template']['language']['code'], 'en_US')
        self.assertEqual(second_payload['template']['language']['code'], 'en')

        queue = MessageQueue.objects.get(job_ticket=job, event_type=MessageQueue.EVENT_COMPLETED)
        self.assertEqual(queue.status, MessageQueue.STATUS_SENT)
        self.assertEqual(queue.bridge_message_id, 'wamid.HBgM456')

    @patch('job_tickets.whatsapp_service.transaction.on_commit', side_effect=lambda callback: callback())
    @patch('job_tickets.whatsapp_service.requests.request')
    def test_job_notification_retries_without_button_when_template_has_no_url_button(self, mock_request, _mock_on_commit):
        job = JobTicket.objects.create(
            job_code='GI-260420-303',
            customer_name='Rahul',
            customer_phone='9876543210',
            device_type='Laptop',
            device_brand='Lenovo',
            device_model='IdeaPad',
            reported_issue='Charging issue',
        )

        self.settings_obj.is_enabled = True
        self.settings_obj.api_version = 'v23.0'
        self.settings_obj.phone_number_id = '123456789012345'
        self.settings_obj.access_token = 'token-123'
        self.settings_obj.public_site_url = 'https://botgi.example.com'
        self.settings_obj.template_language_code = 'en'
        self.settings_obj.notify_on_completed = True
        self.settings_obj.completed_template_name = 'job_completed_update'
        self.settings_obj.completed_template = 'Hello {customer_name}, ticket {job_code}. Track: {status_link}'
        self.settings_obj.save()

        button_error_response = Mock()
        button_error_response.ok = False
        button_error_response.status_code = 400
        button_error_response.json.return_value = {
            'error': {
                'message': '(#132000) Number of parameters does not match the expected number of params',
                'type': 'OAuthException',
                'code': 132000,
                'error_data': {
                    'details': 'button component expects 0 localizable_params',
                },
            }
        }

        success_response = Mock()
        success_response.ok = True
        success_response.status_code = 200
        success_response.json.return_value = {'messages': [{'id': 'wamid.HBgM789'}]}
        mock_request.side_effect = [button_error_response, success_response]

        result = send_job_whatsapp_notification(job, MessageQueue.EVENT_COMPLETED)

        self.assertTrue(result['ok'])
        self.assertEqual(mock_request.call_count, 2)
        first_components = mock_request.call_args_list[0].kwargs['json']['template']['components']
        second_components = mock_request.call_args_list[1].kwargs['json']['template']['components']
        self.assertEqual(len(first_components), 2)
        self.assertEqual(len(second_components), 1)
        self.assertEqual(second_components[0]['type'], 'body')

        queue = MessageQueue.objects.get(job_ticket=job, event_type=MessageQueue.EVENT_COMPLETED)
        self.assertEqual(queue.status, MessageQueue.STATUS_SENT)
        self.assertEqual(queue.bridge_message_id, 'wamid.HBgM789')


class WhatsAppCloudWebhookTests(TestCase):
    def setUp(self):
        self.settings_obj = WhatsAppIntegrationSettings.get_settings()
        self.settings_obj.webhook_verify_token = 'verify-token'
        self.settings_obj.app_secret = 'app-secret'
        self.settings_obj.save(update_fields=['webhook_verify_token', 'app_secret', 'updated_at'])

        self.queue = MessageQueue.objects.create(
            channel=MessageQueue.CHANNEL_WHATSAPP,
            event_type=MessageQueue.EVENT_MANUAL,
            target_phone='919876543210',
            message='Queued test',
            status=MessageQueue.STATUS_PENDING,
            bridge_message_id='wamid.status.1',
        )

    def _signature(self, body: bytes) -> str:
        digest = hmac.new(b'app-secret', body, hashlib.sha256).hexdigest()
        return f'sha256={digest}'

    def test_webhook_verification_returns_challenge(self):
        response = self.client.get(
            reverse('whatsapp_cloud_webhook_api'),
            {
                'hub.mode': 'subscribe',
                'hub.verify_token': 'verify-token',
                'hub.challenge': 'challenge-123',
            },
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.content, b'challenge-123')

    def test_webhook_status_update_marks_queue_sent(self):
        payload = {
            'entry': [
                {
                    'changes': [
                        {
                            'value': {
                                'statuses': [
                                    {
                                        'id': 'wamid.status.1',
                                        'status': 'delivered',
                                    }
                                ]
                            }
                        }
                    ]
                }
            ]
        }
        raw_body = json.dumps(payload).encode('utf-8')

        response = self.client.post(
            reverse('whatsapp_cloud_webhook_api'),
            data=raw_body,
            content_type='application/json',
            HTTP_X_HUB_SIGNATURE_256=self._signature(raw_body),
        )

        self.assertEqual(response.status_code, 200)
        self.queue.refresh_from_db()
        self.assertEqual(self.queue.status, MessageQueue.STATUS_SENT)
        self.assertEqual(self.queue.transport, 'whatsapp-cloud-api-webhook')


class WhatsAppEnhancementsTests(TestCase):
    def setUp(self):
        from django.core.management import call_command
        self.call_command = call_command
        self.settings_obj = WhatsAppIntegrationSettings.get_settings()
        self.settings_obj.default_country_code = '91'
        self.settings_obj.save()

        self.staff_user = User.objects.create_user(
            username='wa-admin',
            password='StrongPass123!',
            is_staff=True,
        )
        apply_staff_access(self.staff_user, {'staff_dashboard', 'job_tickets'})
        self.client.force_login(self.staff_user)

        self.job = JobTicket.objects.create(
            job_code='GI-WA-001',
            customer_name='Ramesh Kumar',
            customer_phone='9876543210',
            device_type='Mobile',
            device_brand='Samsung',
            device_model='Galaxy S21',
            reported_issue='Screen flickering',
        )

    def test_phone_number_sanitization(self):
        from .whatsapp_service import _phone_to_international

        # Leading zero stripping (e.g. Indian trunk prefix 09876543210 -> 919876543210)
        self.assertEqual(_phone_to_international('09876543210', '91'), '919876543210')
        # Leading double zero (e.g. 00919876543210 -> 919876543210)
        self.assertEqual(_phone_to_international('00919876543210', '91'), '919876543210')
        # Formatted string with spaces, parentheses, hyphens and +
        self.assertEqual(_phone_to_international('+91 (987) 654-3210', '91'), '919876543210')
        # 10-digit number with default country code
        self.assertEqual(_phone_to_international('9876543210', '91'), '919876543210')
        # Already fully formatted international
        self.assertEqual(_phone_to_international('919876543210', '91'), '919876543210')
        # Empty or invalid
        self.assertEqual(_phone_to_international('', '91'), '')
        self.assertEqual(_phone_to_international('123', '91'), '')

    def test_delivery_state_computed_property(self):
        # Pending
        q_pending = MessageQueue.objects.create(
            target_phone='919876543210',
            status=MessageQueue.STATUS_PENDING,
            channel=MessageQueue.CHANNEL_WHATSAPP,
        )
        self.assertEqual(q_pending.delivery_state, 'pending')

        # Sent
        q_sent = MessageQueue.objects.create(
            target_phone='919876543210',
            status=MessageQueue.STATUS_SENT,
            channel=MessageQueue.CHANNEL_WHATSAPP,
        )
        self.assertEqual(q_sent.delivery_state, 'sent')

        # Delivered via webhook
        q_delivered = MessageQueue.objects.create(
            target_phone='919876543210',
            status=MessageQueue.STATUS_SENT,
            response_payload={'status': 'delivered'},
            channel=MessageQueue.CHANNEL_WHATSAPP,
        )
        self.assertEqual(q_delivered.delivery_state, 'delivered')

        # Read via webhook
        q_read = MessageQueue.objects.create(
            target_phone='919876543210',
            status=MessageQueue.STATUS_SENT,
            response_payload={'status': 'read'},
            channel=MessageQueue.CHANNEL_WHATSAPP,
        )
        self.assertEqual(q_read.delivery_state, 'read')

        # Failed
        q_failed = MessageQueue.objects.create(
            target_phone='919876543210',
            status=MessageQueue.STATUS_FAILED,
            error_message='Connection refused',
            channel=MessageQueue.CHANNEL_WHATSAPP,
        )
        self.assertEqual(q_failed.delivery_state, 'failed')

    def test_inbound_webhook_reply_creates_job_log(self):
        from .whatsapp_service import process_whatsapp_webhook_payload

        inbound_payload = {
            'entry': [
                {
                    'changes': [
                        {
                            'value': {
                                'messages': [
                                    {
                                        'from': '919876543210',
                                        'id': 'wamid.inbound.test.1',
                                        'timestamp': '1700000000',
                                        'type': 'text',
                                        'text': {
                                            'body': 'Yes please proceed with repair estimate.'
                                        },
                                    }
                                ]
                            }
                        }
                    ]
                }
            ]
        }

        result = process_whatsapp_webhook_payload(inbound_payload)
        self.assertTrue(result.get('ok'))
        self.assertEqual(result.get('inbound_captured'), 1)

        # Confirm JobTicketLog was created
        log_entry = JobTicketLog.objects.filter(job_ticket=self.job, action='NOTE').first()
        self.assertIsNotNone(log_entry)
        self.assertIn('[WhatsApp Customer Reply]', log_entry.details)
        self.assertIn('Yes please proceed with repair estimate.', log_entry.details)

    def test_process_whatsapp_queue_management_command(self):
        q_failed = MessageQueue.objects.create(
            target_phone='919876543210',
            status=MessageQueue.STATUS_FAILED,
            retry_count=0,
            max_retries=3,
            message='Test retry message',
            channel=MessageQueue.CHANNEL_WHATSAPP,
        )
        q_exhausted = MessageQueue.objects.create(
            target_phone='919876543210',
            status=MessageQueue.STATUS_FAILED,
            retry_count=3,
            max_retries=3,
            message='Exhausted message',
            channel=MessageQueue.CHANNEL_WHATSAPP,
        )

        # Dry run should not mutate retry_count
        self.call_command('process_whatsapp_queue', dry_run=True)
        q_failed.refresh_from_db()
        self.assertEqual(q_failed.retry_count, 0)

        # Real execution with mocked sender
        with patch(
            'job_tickets.management.commands.process_whatsapp_queue._deliver_message_queue',
            return_value=({'ok': True, 'message_id': 'test-1'}, 'whatsapp-cloud-api'),
        ) as mock_send:
            self.call_command('process_whatsapp_queue')
            self.assertEqual(mock_send.call_count, 1)

        q_failed.refresh_from_db()
        self.assertEqual(q_failed.retry_count, 1)

        # Exhausted item should still have retry_count == 3
        q_exhausted.refresh_from_db()
        self.assertEqual(q_exhausted.retry_count, 3)

    def test_staff_actions_retry_and_custom_message(self):
        # 1. Custom message
        url = reverse('staff_job_detail', kwargs={'job_code': self.job.job_code})
        with patch('job_tickets.views.staff_views.dispatch_message_queue_task'):
            res = self.client.post(url, {
                'action': 'send_custom_whatsapp',
                'custom_message': 'Please approve the quotation of ₹1,500.',
            })
            self.assertEqual(res.status_code, 302)

        new_queue = MessageQueue.objects.filter(job_ticket=self.job, event_type=MessageQueue.EVENT_MANUAL).first()
        self.assertIsNotNone(new_queue)
        self.assertEqual(new_queue.message, 'Please approve the quotation of ₹1,500.')

        # Verify JobTicketLog logged staff custom message
        outbound_log = JobTicketLog.objects.filter(job_ticket=self.job, action='NOTE').first()
        self.assertIsNotNone(outbound_log)
        self.assertIn('[WhatsApp Outbound Message]', outbound_log.details)

        # 2. Retry message
        new_queue.status = MessageQueue.STATUS_FAILED
        new_queue.error_message = 'Network timeout'
        new_queue.save()

        with patch('job_tickets.views.staff_views.dispatch_message_queue_task') as mock_dispatch:
            res_retry = self.client.post(url, {
                'action': 'retry_whatsapp_message',
                'queue_id': new_queue.id,
            })
            self.assertEqual(res_retry.status_code, 302)
            mock_dispatch.assert_called_once_with(new_queue.id)

        new_queue.refresh_from_db()
        self.assertEqual(new_queue.status, MessageQueue.STATUS_PENDING)
        self.assertEqual(new_queue.error_message, '')


class DiscountAwareReportsTests(TestCase):
    def setUp(self):
        self.staff_user = User.objects.create_user(
            username='reports-admin',
            password='StrongPass123!',
            is_staff=True,
            is_superuser=True,
        )
        self.technician_user = User.objects.create_user(
            username='tech-report-user',
            password='StrongPass123!',
        )
        self.technician = TechnicianProfile.objects.create(
            user=self.technician_user,
            unique_id='TECH001',
        )
        self.client.force_login(self.staff_user)
        self.report_date = timezone.localdate()
        self.report_timestamp = timezone.make_aware(
            datetime.combine(self.report_date, datetime.min.time())
        ) + timedelta(hours=11)

    def _create_finished_job(self, job_code, part_cost, service_charge, discount_amount):
        job = JobTicket.objects.create(
            job_code=job_code,
            customer_name='Report Customer',
            customer_phone='9876543210',
            device_type='Laptop',
            reported_issue='No power',
            status='Completed',
            assigned_to=self.technician,
            discount_amount=Decimal(discount_amount),
        )
        ServiceLog.objects.create(
            job_ticket=job,
            description='Board repair',
            part_cost=Decimal(part_cost),
            service_charge=Decimal(service_charge),
        )
        JobTicket.objects.filter(pk=job.pk).update(updated_at=self.report_timestamp)
        return JobTicket.objects.get(pk=job.pk)

    def test_reports_chart_data_subtracts_discount_from_income(self):
        self._create_finished_job('GI-260403-901', '120.00', '280.00', '50.00')

        response = self.client.get(
            reverse('reports_chart_data'),
            {
                'start_date': self.report_date.isoformat(),
                'end_date': self.report_date.isoformat(),
            },
        )

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload['monthly'][0]['discount_total'], 50.0)
        self.assertEqual(payload['monthly'][0]['total_income'], 350.0)
        self.assertEqual(payload['monthly'][0]['net_profit'], 350.0)
        self.assertEqual(payload['yearly'][0]['discount_total'], 50.0)
        self.assertEqual(payload['yearly'][0]['total_income'], 350.0)

    def test_technician_report_print_uses_net_totals(self):
        self._create_finished_job('GI-260403-902', '150.00', '250.00', '40.00')

        request = RequestFactory().get(
            reverse('technician_report_print', args=[self.technician.id]),
            {
                'start_date': self.report_date.isoformat(),
                'end_date': self.report_date.isoformat(),
            },
        )
        request.user = self.staff_user

        captured = {}

        def fake_render(_request, _template_name, context):
            captured['context'] = context
            return HttpResponse('ok')

        with patch('job_tickets.views.report_views.render', side_effect=fake_render):
            response = technician_report_print(request, self.technician.id)

        self.assertEqual(response.status_code, 200)
        context = captured['context']
        self.assertEqual(context['total_discounts'], Decimal('40.00'))
        self.assertEqual(context['total_income'], Decimal('360.00'))
        self.assertEqual(context['jobs'][0].discount_total, Decimal('40.00'))
        self.assertEqual(context['jobs'][0].net_total, Decimal('360.00'))

    def test_technician_report_print_links_jobs_and_csv(self):
        job = self._create_finished_job('GI-260403-903', '150.00', '250.00', '40.00')

        response = self.client.get(
            reverse('technician_report_print', args=[self.technician.id]),
            {
                'start_date': self.report_date.isoformat(),
                'end_date': self.report_date.isoformat(),
            },
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, reverse('staff_job_detail', args=[job.job_code]))
        self.assertContains(response, reverse('technician_report_export_csv', args=[self.technician.id]))

    def test_technician_report_csv_exports_net_totals_and_job_url(self):
        job = self._create_finished_job('GI-260403-904', '150.00', '250.00', '40.00')

        response = self.client.get(
            reverse('technician_report_export_csv', args=[self.technician.id]),
            {
                'start_date': self.report_date.isoformat(),
                'end_date': self.report_date.isoformat(),
            },
        )

        self.assertEqual(response.status_code, 200)
        self.assertIn('text/csv', response['Content-Type'])
        body = response.content.decode()
        self.assertIn(job.job_code, body)
        self.assertIn('360.00', body)
        self.assertIn(reverse('staff_job_detail', args=[job.job_code]), body)

    def test_monthly_summary_splits_service_parts_and_labor_blocks(self):
        job = self._create_finished_job('GI-260403-905', '150.00', '250.00', '40.00')
        JobTicket.objects.filter(pk=job.pk).update(
            status='Closed',
            closed_at=self.report_timestamp,
            updated_at=self.report_timestamp,
        )

        captured = {}

        def fake_render(_request, _template_name, context):
            captured['context'] = context
            return HttpResponse('ok')

        with patch('job_tickets.views.report_views.render', side_effect=fake_render):
            response = self.client.get(
                reverse('print_monthly_summary_report'),
                {
                    'start_date': self.report_date.isoformat(),
                    'end_date': self.report_date.isoformat(),
                },
            )

        self.assertEqual(response.status_code, 200)
        blocks = {
            block['label']: block
            for block in captured['context']['closed_financial_blocks']
        }
        self.assertEqual(blocks['Service Parts']['revenue'], Decimal('150.00'))
        self.assertEqual(blocks['Service Labor']['revenue'], Decimal('250.00'))
        self.assertEqual(captured['context']['closed_gross_revenue'], Decimal('400.00'))
        self.assertEqual(captured['context']['overall_revenue'], Decimal('360.00'))

    def test_monthly_summary_csv_includes_closed_bill_balance(self):
        job = JobTicket.objects.create(
            job_code='GI-260403-906',
            customer_name='Closed Credit Customer',
            customer_phone='9876543230',
            device_type='Laptop',
            reported_issue='Closed credit balance',
            status='Closed',
            assigned_to=self.technician,
            closed_at=self.report_timestamp,
        )
        JobTicket.objects.filter(pk=job.pk).update(updated_at=self.report_timestamp)
        product = Product.objects.create(
            name='Report Product',
            sku='RPT-001',
            unit_price=Decimal('500.00'),
            cost_price=Decimal('300.00'),
            stock_quantity=5,
        )
        party = InventoryParty.objects.create(
            name='Closed Credit Customer',
            party_type='customer',
            phone='9876543230',
        )
        bill = InventoryBill.objects.create(
            bill_number='SALE-RPT-001',
            entry_type='sale',
            entry_date=self.report_date,
            invoice_number='SALE-RPT-001',
            job_ticket=job,
            party=party,
            created_by=self.staff_user,
        )
        InventoryEntry.objects.create(
            entry_number='SE-RPT-001',
            entry_type='sale',
            entry_date=self.report_date,
            bill=bill,
            invoice_number='SALE-RPT-001',
            job_ticket=job,
            party=party,
            product=product,
            quantity=1,
            unit_price=Decimal('500.00'),
            taxable_amount=Decimal('500.00'),
            total_amount=Decimal('500.00'),
            stock_before=5,
            stock_after=4,
            created_by=self.staff_user,
        )
        InventoryCreditPayment.objects.create(
            party=party,
            bill=bill,
            direction=InventoryCreditPayment.DIRECTION_RECEIVABLE,
            payment_date=self.report_date,
            payment_method=InventoryCreditPayment.METHOD_CASH,
            amount=Decimal('200.00'),
            balance_before=Decimal('500.00'),
            balance_after=Decimal('300.00'),
            created_by=self.staff_user,
        )

        response = self.client.get(
            reverse('export_monthly_summary_csv'),
            {
                'start_date': self.report_date.isoformat(),
                'end_date': self.report_date.isoformat(),
            },
        )

        self.assertEqual(response.status_code, 200)
        body = response.content.decode()
        self.assertIn('Closed Bill Balance,300.00', body)
        self.assertIn('Closed Bill Paid,200.00', body)


class InventoryUxDefaultsTests(TestCase):
    def setUp(self):
        self.staff_user = User.objects.create_user(
            username='inventory-admin',
            password='StrongPass123!',
            is_staff=True,
            is_superuser=True,
        )

    def test_product_form_prefills_zero_values_for_new_product(self):
        form = ProductForm()

        self.assertEqual(form.fields['cost_price'].initial, Decimal('0.00'))
        self.assertEqual(form.fields['unit_price'].initial, Decimal('0.00'))
        self.assertEqual(form.fields['stock_quantity'].initial, 0)
        self.assertEqual(form.fields['reserved_stock'].initial, 0)

    def test_inventory_party_dashboard_creates_shared_party(self):
        self.client.force_login(self.staff_user)

        response = self.client.post(
            reverse('inventory_party_dashboard'),
            {
                'add_inventory_party_submit': '1',
                'name': 'Walk In Customer',
                'gst_registration_type': 'unregistered',
                'phone': '9876543210',
                'country': 'India',
                'opening_balance': '0.00',
                'is_active': 'on',
            },
        )

        self.assertEqual(response.status_code, 302)
        party = InventoryParty.objects.get(name='Walk In Customer')
        self.assertEqual(party.party_type, 'both')

    def test_inventory_party_dashboard_updates_party_profile(self):
        self.client.force_login(self.staff_user)
        party = InventoryParty.objects.create(
            name='Old Party Name',
            phone='9999999999',
            city='Old City',
        )

        response = self.client.post(
            reverse('inventory_party_dashboard'),
            {
                'edit_inventory_party_submit': '1',
                'edit_party_id': str(party.id),
                'edit-name': 'Updated Party Name',
                'edit-legal_name': 'Updated Legal Name',
                'edit-contact_person': 'Anas',
                'edit-gst_registration_type': 'unregistered',
                'edit-phone': '8888888888',
                'edit-gstin': '',
                'edit-state_code': '',
                'edit-default_place_of_supply_state': '',
                'edit-pan': '',
                'edit-email': 'party@example.com',
                'edit-address': 'Main Road',
                'edit-shipping_address': 'Warehouse Road',
                'edit-city': 'Kochi',
                'edit-state': 'Kerala',
                'edit-country': 'India',
                'edit-pincode': '682001',
                'edit-opening_balance': '1250.00',
                'edit-is_active': 'on',
            },
        )

        self.assertEqual(response.status_code, 302)
        party.refresh_from_db()
        self.assertEqual(party.name, 'Updated Party Name')
        self.assertEqual(party.legal_name, 'Updated Legal Name')
        self.assertEqual(party.contact_person, 'Anas')
        self.assertEqual(party.phone, '8888888888')
        self.assertEqual(party.email, 'party@example.com')
        self.assertEqual(party.address, 'Main Road')
        self.assertEqual(party.shipping_address, 'Warehouse Road')
        self.assertEqual(party.city, 'Kochi')
        self.assertEqual(party.state, 'Kerala')
        self.assertEqual(party.country, 'India')
        self.assertEqual(party.pincode, '682001')
        self.assertEqual(party.opening_balance, Decimal('1250.00'))
        self.assertEqual(party.party_type, 'both')

    def test_inventory_party_dashboard_shows_single_party_directory_section(self):
        self.client.force_login(self.staff_user)
        InventoryParty.objects.create(name='Hardware Supplier', party_type='supplier')
        InventoryParty.objects.create(name='Retail Customer', party_type='customer')

        captured = {}

        def fake_render(_request, template_name, context):
            captured['template_name'] = template_name
            captured['context'] = context
            return HttpResponse('ok')

        with patch('job_tickets.views.inventory_views.render', side_effect=fake_render):
            response = self.client.get(reverse('inventory_party_dashboard'))

        self.assertEqual(response.status_code, 200)
        self.assertEqual(captured['template_name'], 'job_tickets/inventory_party_dashboard.html')
        self.assertEqual(
            [party.name for party in captured['context']['parties']],
            ['Hardware Supplier', 'Retail Customer'],
        )
        template_path = Path(settings.BASE_DIR) / 'job_tickets' / 'templates' / 'job_tickets' / 'inventory_party_dashboard.html'
        template_text = template_path.read_text(encoding='utf-8')
        self.assertIn('Party Directory', template_text)
        self.assertNotIn('Combined Party Directory', template_text)
        self.assertNotIn('Supplier Side', template_text)
        self.assertNotIn('Customer Side', template_text)
        self.assertNotIn('partyMasterTabs', template_text)

    def test_inventory_entry_form_uses_shared_party_queryset_for_purchase_and_sale(self):
        shared_party = InventoryParty.objects.create(name='Shared Ledger Party')

        purchase_form = InventoryEntryForm(entry_type='purchase')
        sale_form = InventoryEntryForm(entry_type='sale')

        self.assertEqual(purchase_form.fields['party'].label, 'Party')
        self.assertEqual(sale_form.fields['party'].label, 'Party')
        self.assertIn(shared_party, purchase_form.fields['party'].queryset)
        self.assertIn(shared_party, sale_form.fields['party'].queryset)

    def test_inventory_sales_dashboard_shows_quick_add_product_controls(self):
        self.client.force_login(self.staff_user)

        captured = {}

        def fake_render(_request, template_name, context):
            captured['template_name'] = template_name
            captured['context'] = context
            return HttpResponse('ok')

        with patch('job_tickets.views.helpers.render', side_effect=fake_render):
            response = self.client.get(reverse('inventory_sales_dashboard'))

        self.assertEqual(response.status_code, 200)
        self.assertEqual(captured['template_name'], 'job_tickets/inventory_entry_dashboard.html')
        self.assertEqual(captured['context']['entry_type'], 'sale')
        template_path = Path(settings.BASE_DIR) / 'job_tickets' / 'templates' / 'job_tickets' / 'inventory_entry_dashboard.html'
        template_text = template_path.read_text(encoding='utf-8')
        self.assertIn('toggle-quick-product-btn', template_text)
        self.assertIn("entry_type == 'purchase' or entry_type == 'sale'", template_text)

    def test_inventory_entry_dashboard_wires_line_product_typeahead(self):
        template_path = Path(settings.BASE_DIR) / 'job_tickets' / 'templates' / 'job_tickets' / 'inventory_entry_dashboard.html'
        template_text = template_path.read_text(encoding='utf-8')

        self.assertIn('inventory-line-product-picker', template_text)
        self.assertIn('inventory-line-product-options', template_text)
        self.assertIn("replace(/\\s+/g, ' ').trim()", template_text)
        self.assertIn("pickerInput.setAttribute('list', listId);", template_text)
        self.assertIn('initializeLineProductPicker(row);', template_text)
        self.assertIn('refreshLineProductPickers();', template_text)

    def test_inventory_dashboard_uses_expanded_workspace_chrome(self):
        self.client.force_login(self.staff_user)

        response = self.client.get(reverse('inventory_dashboard'))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'inventory-workspace')
        self.assertContains(response, 'id="appSidebar"')
        self.assertContains(response, 'class="app-layout has-sidebar"')
        self.assertContains(response, reverse('inventory_dashboard'))


class InventorySaleStockRulesTests(TestCase):
    def setUp(self):
        self.staff_user = User.objects.create_user(
            username='inventory-sale-admin',
            password='StrongPass123!',
            is_staff=True,
            is_superuser=True,
        )
        self.customer = InventoryParty.objects.create(
            name='Counter Customer',
        )
        self.product = Product.objects.create(
            name='USB Keyboard',
            category='Peripherals',
            unit_price=Decimal('850.00'),
            cost_price=Decimal('500.00'),
            stock_quantity=0,
        )

    def test_inventory_sale_can_reduce_stock_below_zero(self):
        self.client.force_login(self.staff_user)

        response = self.client.post(
            reverse('inventory_sales_dashboard'),
            {
                'inventory_entry_submit': 'sale',
                'entry_date': timezone.localdate().isoformat(),
                'party': str(self.customer.id),
                'bill_discount_amount': '0.00',
                'bill_notes': '',
                'line_product_id[]': [str(self.product.id)],
                'line_quantity[]': ['2'],
                'line_unit_price[]': ['850.00'],
                'line_gst_rate[]': ['18.00'],
            },
        )

        self.assertEqual(response.status_code, 302)
        self.product.refresh_from_db()
        self.assertEqual(self.product.stock_quantity, -2)

        entry = InventoryEntry.objects.get(entry_type='sale', product=self.product)
        self.assertEqual(entry.stock_before, 0)
        self.assertEqual(entry.stock_after, -2)


class InventoryCreditPaymentTests(TestCase):
    def setUp(self):
        self.staff_user = User.objects.create_user(
            username='inventory-credit-admin',
            password='StrongPass123!',
            is_staff=True,
            is_superuser=True,
        )
        self.party = InventoryParty.objects.create(name='Credit Party')
        self.product = Product.objects.create(
            name='Credit Product',
            unit_price=Decimal('1000.00'),
            cost_price=Decimal('700.00'),
            stock_quantity=5,
        )

    def _create_bill(self, entry_type='purchase', total='1000.00', invoice_number=None):
        sequence = InventoryBill.objects.count() + 1
        invoice_number = invoice_number or f'{entry_type.upper()}-INV-{sequence:03d}'
        bill = InventoryBill.objects.create(
            bill_number=f'{entry_type.upper()}-BILL-{sequence:03d}',
            entry_type=entry_type,
            entry_date=timezone.localdate(),
            invoice_date=timezone.localdate() if entry_type == 'purchase' else None,
            invoice_number=invoice_number,
            party=self.party,
        )
        InventoryEntry.objects.create(
            bill=bill,
            entry_number=f'{entry_type.upper()}-ENTRY-{sequence:03d}',
            entry_type=entry_type,
            entry_date=timezone.localdate(),
            invoice_number=bill.invoice_number,
            party=self.party,
            product=self.product,
            quantity=1,
            unit_price=Decimal(total),
            taxable_amount=Decimal(total),
            total_amount=Decimal(total),
            stock_before=5,
            stock_after=6 if entry_type == 'purchase' else 4,
        )
        return bill

    def test_purchase_credit_payment_records_payable_balance(self):
        self.client.force_login(self.staff_user)
        bill = self._create_bill('purchase', '1000.00')

        response = self.client.post(
            reverse('inventory_record_credit_payment'),
            {
                'bill_id': str(bill.id),
                'amount': '300.00',
                'payment_date': timezone.localdate().isoformat(),
                'payment_method': 'cash',
                'reference_no': 'CASH-1',
                'next': reverse('inventory_purchase_dashboard'),
            },
        )

        self.assertEqual(response.status_code, 302)
        payment = InventoryCreditPayment.objects.get(bill=bill)
        self.assertEqual(payment.direction, InventoryCreditPayment.DIRECTION_PAYABLE)
        self.assertEqual(payment.balance_before, Decimal('1000.00'))
        self.assertEqual(payment.balance_after, Decimal('700.00'))

    def test_sale_credit_payment_records_receivable_balance(self):
        self.client.force_login(self.staff_user)
        bill = self._create_bill('sale', '1500.00')

        response = self.client.post(
            reverse('inventory_record_credit_payment'),
            {
                'bill_id': str(bill.id),
                'amount': '500.00',
                'payment_date': timezone.localdate().isoformat(),
                'payment_method': 'transfer',
                'reference_no': 'UPI-1',
            },
        )

        self.assertEqual(response.status_code, 302)
        payment = InventoryCreditPayment.objects.get(bill=bill)
        self.assertEqual(payment.direction, InventoryCreditPayment.DIRECTION_RECEIVABLE)
        self.assertEqual(payment.balance_before, Decimal('1500.00'))
        self.assertEqual(payment.balance_after, Decimal('1000.00'))

    def test_inventory_credit_payment_cannot_exceed_balance(self):
        self.client.force_login(self.staff_user)
        bill = self._create_bill('purchase', '1000.00')

        response = self.client.post(
            reverse('inventory_record_credit_payment'),
            {
                'bill_id': str(bill.id),
                'amount': '1200.00',
                'payment_date': timezone.localdate().isoformat(),
                'payment_method': 'cash',
            },
        )

        self.assertEqual(response.status_code, 302)
        self.assertFalse(InventoryCreditPayment.objects.filter(bill=bill).exists())

    def test_sale_bill_marked_paid_creates_full_initial_settlement(self):
        self.client.force_login(self.staff_user)

        response = self.client.post(
            reverse('inventory_sales_dashboard'),
            {
                'inventory_entry_submit': 'sale',
                'entry_date': timezone.localdate().isoformat(),
                'party': str(self.party.id),
                'bill_discount_amount': '0.00',
                'bill_notes': '',
                'bill_payment_status': 'paid',
                'bill_payment_method': 'transfer',
                'bill_payment_date': timezone.localdate().isoformat(),
                'bill_payment_reference': 'UPI-PAID',
                'line_product_id[]': [str(self.product.id)],
                'line_quantity[]': ['1'],
                'line_unit_price[]': ['1000.00'],
                'line_gst_rate[]': ['0.00'],
            },
        )

        self.assertEqual(response.status_code, 302)
        bill = InventoryBill.objects.get(entry_type='sale')
        payment = InventoryCreditPayment.objects.get(bill=bill)
        self.assertEqual(payment.direction, InventoryCreditPayment.DIRECTION_RECEIVABLE)
        self.assertEqual(payment.payment_method, InventoryCreditPayment.METHOD_TRANSFER)
        self.assertEqual(payment.amount, Decimal('1000.00'))
        self.assertEqual(payment.balance_before, Decimal('1000.00'))
        self.assertEqual(payment.balance_after, Decimal('0.00'))
        self.assertEqual(payment.reference_no, 'UPI-PAID')

    def test_purchase_bill_marked_unpaid_stays_as_credit_balance(self):
        self.client.force_login(self.staff_user)

        response = self.client.post(
            reverse('inventory_purchase_dashboard'),
            {
                'inventory_entry_submit': 'purchase',
                'entry_date': timezone.localdate().isoformat(),
                'invoice_number': 'PUR-CREDIT-001',
                'party': str(self.party.id),
                'bill_discount_amount': '0.00',
                'bill_notes': '',
                'bill_payment_status': 'unpaid',
                'line_product_id[]': [str(self.product.id)],
                'line_quantity[]': ['1'],
                'line_unit_price[]': ['700.00'],
                'line_gst_rate[]': ['0.00'],
            },
        )

        self.assertEqual(response.status_code, 302)
        bill = InventoryBill.objects.get(entry_type='purchase')
        self.assertFalse(InventoryCreditPayment.objects.filter(bill=bill).exists())

        response = self.client.get(reverse('inventory_purchase_dashboard'))
        self.assertContains(response, 'Pay Supplier')
        self.assertContains(response, '700.00')

    def test_purchase_bill_saves_supplier_invoice_date(self):
        self.client.force_login(self.staff_user)
        entry_date = timezone.localdate()
        invoice_date = entry_date - timedelta(days=3)

        response = self.client.post(
            reverse('inventory_purchase_dashboard'),
            {
                'inventory_entry_submit': 'purchase',
                'invoice_date': invoice_date.isoformat(),
                'invoice_number': 'SUP-INV-DATE-001',
                'party': str(self.party.id),
                'bill_discount_amount': '0.00',
                'bill_notes': '',
                'bill_payment_status': 'unpaid',
                'line_product_id[]': [str(self.product.id)],
                'line_quantity[]': ['1'],
                'line_unit_price[]': ['700.00'],
                'line_gst_rate[]': ['0.00'],
            },
        )

        self.assertEqual(response.status_code, 302)
        bill = InventoryBill.objects.get(invoice_number='SUP-INV-DATE-001')
        self.assertEqual(bill.entry_date, invoice_date)
        self.assertEqual(bill.invoice_date, invoice_date)

        response = self.client.get(reverse('inventory_purchase_dashboard'))
        self.assertContains(response, 'Invoice Date')
        self.assertNotContains(response, '<th>Entry Date</th>', html=False)
        self.assertContains(response, invoice_date.strftime('%Y-%m-%d'))

    def test_inventory_credit_sections_render_on_dashboards(self):
        self.client.force_login(self.staff_user)
        self._create_bill('purchase', '1000.00')
        self._create_bill('sale', '1500.00')

        pages = [
            (reverse('inventory_dashboard'), 'To Pay'),
            (reverse('inventory_purchase_dashboard'), 'Pay Supplier'),
            (reverse('inventory_sales_dashboard'), 'Receive Payment'),
            (reverse('inventory_party_dashboard'), 'To Collect'),
        ]

        for url, expected_text in pages:
            with self.subTest(url=url):
                response = self.client.get(url)

                self.assertEqual(response.status_code, 200)
                self.assertContains(response, expected_text)

    def test_purchase_register_filters_by_payment_status_and_links_party_ledger(self):
        self.client.force_login(self.staff_user)
        unpaid_bill = self._create_bill('purchase', '1000.00', invoice_number='PUR-CREDIT-STATUS')
        part_paid_bill = self._create_bill('purchase', '1200.00', invoice_number='PUR-PART-STATUS')
        paid_bill = self._create_bill('purchase', '700.00', invoice_number='PUR-PAID-STATUS')
        InventoryCreditPayment.objects.create(
            party=self.party,
            bill=part_paid_bill,
            direction=InventoryCreditPayment.DIRECTION_PAYABLE,
            payment_date=timezone.localdate(),
            payment_method=InventoryCreditPayment.METHOD_CASH,
            amount=Decimal('200.00'),
            balance_before=Decimal('1200.00'),
            balance_after=Decimal('1000.00'),
            created_by=self.staff_user,
        )
        InventoryCreditPayment.objects.create(
            party=self.party,
            bill=paid_bill,
            direction=InventoryCreditPayment.DIRECTION_PAYABLE,
            payment_date=timezone.localdate(),
            payment_method=InventoryCreditPayment.METHOD_CASH,
            amount=Decimal('700.00'),
            balance_before=Decimal('700.00'),
            balance_after=Decimal('0.00'),
            created_by=self.staff_user,
        )

        response = self.client.get(reverse('inventory_purchase_dashboard'), {'payment_status': 'credit'})
        self.assertContains(response, 'PUR-CREDIT-STATUS')
        self.assertNotContains(response, 'PUR-PART-STATUS')
        self.assertContains(response, reverse('inventory_party_ledger', args=[self.party.id]))

        response = self.client.get(reverse('inventory_purchase_dashboard'), {'payment_status': 'part_paid'})
        self.assertContains(response, 'PUR-PART-STATUS')
        self.assertNotContains(response, 'PUR-CREDIT-STATUS')

        response = self.client.get(reverse('inventory_purchase_dashboard'), {'payment_status': 'paid'})
        self.assertContains(response, 'PUR-PAID-STATUS')
        self.assertNotContains(response, 'PUR-PART-STATUS')


class InventoryLedgerPageTests(TestCase):
    def setUp(self):
        self.staff_user = User.objects.create_user(
            username='inventory-ledger-admin',
            password='StrongPass123!',
            is_staff=True,
            is_superuser=True,
        )
        self.party = InventoryParty.objects.create(
            name='Ledger Party',
            phone='9876543210',
        )
        self.product = Product.objects.create(
            name='Ledger Product',
            category='Parts',
            unit_price=Decimal('1200.00'),
            cost_price=Decimal('800.00'),
            stock_quantity=8,
        )

    def _create_bill(self, entry_type, invoice_number, total, quantity=1):
        bill = InventoryBill.objects.create(
            bill_number=f'{entry_type.upper()}-{invoice_number}',
            entry_type=entry_type,
            entry_date=timezone.localdate(),
            invoice_date=timezone.localdate() if entry_type == 'purchase' else None,
            invoice_number=invoice_number,
            party=self.party,
            created_by=self.staff_user,
        )
        stock_effect = quantity if entry_type in {'purchase', 'sale_return'} else -quantity
        InventoryEntry.objects.create(
            bill=bill,
            entry_number=f'{entry_type.upper()}-ENTRY-{invoice_number}',
            entry_type=entry_type,
            entry_date=bill.entry_date,
            invoice_number=invoice_number,
            party=self.party,
            product=self.product,
            quantity=quantity,
            unit_price=Decimal(total),
            taxable_amount=Decimal(total),
            total_amount=Decimal(total),
            stock_before=self.product.stock_quantity - stock_effect,
            stock_after=self.product.stock_quantity,
            created_by=self.staff_user,
        )
        return bill

    def test_product_ledger_renders_full_movement_and_product_master_link(self):
        self.client.force_login(self.staff_user)
        self._create_bill('purchase', 'PUR-LEDGER-1', '800.00', quantity=2)
        self._create_bill('sale', 'SALE-LEDGER-1', '1200.00', quantity=1)

        response = self.client.get(reverse('inventory_product_dashboard'))
        self.assertContains(response, reverse('inventory_product_ledger', args=[self.product.id]))
        self.assertContains(response, 'Ledger')

        response = self.client.get(reverse('inventory_product_ledger', args=[self.product.id]))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Product Ledger')
        self.assertContains(response, 'Ledger Product')
        self.assertContains(response, 'PUR-LEDGER-1')
        self.assertContains(response, 'SALE-LEDGER-1')
        self.assertContains(response, 'Stock Movement')

    def test_party_ledger_renders_bills_payments_and_party_master_link(self):
        self.client.force_login(self.staff_user)
        bill = self._create_bill('purchase', 'PUR-PARTY-1', '1000.00')
        InventoryCreditPayment.objects.create(
            party=self.party,
            bill=bill,
            direction=InventoryCreditPayment.DIRECTION_PAYABLE,
            payment_date=timezone.localdate(),
            payment_method=InventoryCreditPayment.METHOD_CASH,
            amount=Decimal('250.00'),
            balance_before=Decimal('1000.00'),
            balance_after=Decimal('750.00'),
            created_by=self.staff_user,
        )

        response = self.client.get(reverse('inventory_party_dashboard'))
        self.assertContains(response, reverse('inventory_party_ledger', args=[self.party.id]))
        self.assertContains(response, 'Ledger')

        response = self.client.get(reverse('inventory_party_ledger', args=[self.party.id]))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Party Ledger')
        self.assertContains(response, 'Ledger Party')
        self.assertContains(response, 'PUR-PARTY-1')
        self.assertContains(response, 'Payment History')
        self.assertContains(response, '750.00')


class StaffBillingZeroStockProductTests(TestCase):
    def setUp(self):
        self.staff_user = User.objects.create_user(
            username='staff-billing-admin',
            password='StrongPass123!',
            is_staff=True,
            is_superuser=True,
        )
        self.job = JobTicket.objects.create(
            job_code='GI-260413-001',
            customer_name='Walk In Customer',
            customer_phone='9876543210',
            device_type='Laptop',
            device_brand='Dell',
            device_model='Vostro',
            reported_issue='Keyboard issue',
            status='Completed',
        )
        self.product = Product.objects.create(
            name='Laptop Keyboard',
            category='Peripherals',
            unit_price=Decimal('1200.00'),
            cost_price=Decimal('800.00'),
            stock_quantity=0,
        )

    def test_staff_billing_page_lists_zero_stock_products_for_sale(self):
        self.client.force_login(self.staff_user)

        response = self.client.get(reverse('job_billing_staff', args=[self.job.job_code]))

        self.assertEqual(response.status_code, 200)
        self.assertIn(self.product, list(response.context['products_for_sale']))
        self.assertContains(response, 'Laptop Keyboard (Stock: 0)')

    def test_staff_billing_manage_products_opens_inventory_in_new_tab(self):
        self.client.force_login(self.staff_user)

        response = self.client.get(reverse('job_billing_staff', args=[self.job.job_code]))

        self.assertEqual(response.status_code, 200)
        self.assertContains(
            response,
            'href="/staff/inventory/products/" target="_blank" rel="noopener"',
        )
        self.assertContains(response, 'Manage Products')

    def test_staff_billing_can_sell_zero_stock_product_and_track_negative_stock(self):
        self.client.force_login(self.staff_user)

        response = self.client.post(
            reverse('job_billing_staff', args=[self.job.job_code]),
            {
                'update_amounts_submit': '1',
                'job_sales_invoice_number': '',
                'discount_amount': '0.00',
                'product_id[]': [str(self.product.id)],
                'product_qty[]': ['2'],
                'product_service_charge[]': ['150.00'],
            },
        )

        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.url, reverse('job_billing_staff', args=[self.job.job_code]))

        self.product.refresh_from_db()
        self.assertEqual(self.product.stock_quantity, -2)

        sale = ProductSale.objects.get(job_ticket=self.job, product=self.product)
        self.assertEqual(sale.quantity, 2)

        entry = InventoryEntry.objects.get(entry_type='sale', job_ticket=self.job, product=self.product)
        self.assertEqual(entry.stock_before, 0)
        self.assertEqual(entry.stock_after, -2)

class InventoryApiTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(
            username='inventory-api-admin',
            password='StrongPass123!',
            is_staff=True,
            is_superuser=True,
        )
        self.token = issue_mobile_jwt(self.user)

    def test_mobile_inventory_summary_returns_metrics(self):
        supplier = InventoryParty.objects.create(
            name='API Supplier',
            party_type='supplier',
        )
        product = Product.objects.create(
            name='Router Board',
            category='Networking',
            unit_price=Decimal('1500.00'),
            cost_price=Decimal('900.00'),
            stock_quantity=4,
            reserved_stock=5,
        )
        bill = InventoryBill.objects.create(
            bill_number='PB-260403-001',
            entry_type='purchase',
            entry_date=timezone.localdate(),
            party=supplier,
        )
        InventoryEntry.objects.create(
            bill=bill,
            entry_number='PE-260403-001',
            entry_type='purchase',
            entry_date=timezone.localdate(),
            party=supplier,
            product=product,
            quantity=4,
            unit_price=Decimal('900.00'),
            taxable_amount=Decimal('3600.00'),
            total_amount=Decimal('3600.00'),
            stock_before=0,
            stock_after=4,
        )

        response = self.client.get(
            reverse('mobile_api_inventory_summary'),
            HTTP_AUTHORIZATION=f'Bearer {self.token}',
        )

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload['summary']['party_count'], 1)
        self.assertEqual(payload['summary']['product_count'], 1)
        self.assertEqual(payload['summary']['reserved_alert_count'], 1)
        self.assertEqual(payload['summary']['monthly_purchase_total'], '3600.00')
        self.assertEqual(payload['reserved_stock_products'][0]['name'], 'Router Board')

    def test_mobile_inventory_parties_create_and_list(self):
        create_response = self.client.post(
            reverse('mobile_api_inventory_parties'),
            data='{"name":"API Customer","phone":"9876543210","opening_balance":"0.00"}',
            content_type='application/json',
            HTTP_AUTHORIZATION=f'Bearer {self.token}',
        )

        self.assertEqual(create_response.status_code, 200)
        created_payload = create_response.json()
        self.assertTrue(created_payload['ok'])
        self.assertEqual(created_payload['party']['party_type'], 'both')

        list_response = self.client.get(
            reverse('mobile_api_inventory_parties'),
            HTTP_AUTHORIZATION=f'Bearer {self.token}',
        )

        self.assertEqual(list_response.status_code, 200)
        payload = list_response.json()
        self.assertEqual(payload['summary']['total_parties'], 1)
        self.assertEqual(payload['parties'][0]['name'], 'API Customer')

    def test_mobile_inventory_party_update_normalizes_tax_identity_fields(self):
        party = InventoryParty.objects.create(
            name='Original Supplier',
            party_type='supplier',
            phone='9999999999',
        )

        response = self.client.post(
            reverse('mobile_api_inventory_party_update', kwargs={'party_id': party.id}),
            data='{"name":"Updated Supplier","phone":"8888888888","pan":"abcde1234f","gstin":"32abcde1234f1z5","shipping_address":"Warehouse lane","pincode":"682001","opening_balance":"1250.00"}',
            content_type='application/json',
            HTTP_AUTHORIZATION=f'Bearer {self.token}',
        )

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertTrue(payload['ok'])
        self.assertEqual(payload['party']['name'], 'Updated Supplier')
        self.assertEqual(payload['party']['pan'], 'ABCDE1234F')
        self.assertEqual(payload['party']['gstin'], '32ABCDE1234F1Z5')
        self.assertEqual(payload['party']['shipping_address'], 'Warehouse lane')
        self.assertEqual(payload['party']['pincode'], '682001')
        self.assertEqual(payload['party']['opening_balance'], '1250.00')
        self.assertEqual(payload['party']['party_type'], 'both')

    def test_mobile_products_list_includes_tax_metadata_and_history(self):
        supplier = InventoryParty.objects.create(
            name='History Supplier',
            party_type='supplier',
        )
        product = Product.objects.create(
            name='Thermal Printer Head',
            sku='TPH-01',
            category='Printer Parts',
            brand='Epson',
            item_type='goods',
            hsn_sac_code='8471',
            uqc='NOS',
            tax_category='taxable',
            gst_rate=Decimal('18.00'),
            cess_rate=Decimal('0.00'),
            cost_price=Decimal('1200.00'),
            unit_price=Decimal('1650.00'),
            stock_quantity=2,
            reserved_stock=1,
            description='Dot matrix printer replacement head',
        )
        bill = InventoryBill.objects.create(
            bill_number='PB-260403-010',
            entry_type='purchase',
            entry_date=timezone.localdate(),
            party=supplier,
            invoice_number='SUP-7788',
        )
        InventoryEntry.objects.create(
            bill=bill,
            entry_number='PUR-260403-010',
            entry_type='purchase',
            entry_date=timezone.localdate(),
            invoice_number='SUP-7788',
            party=supplier,
            product=product,
            quantity=2,
            unit_price=Decimal('1200.00'),
            gst_rate=Decimal('18.00'),
            taxable_amount=Decimal('2400.00'),
            gst_amount=Decimal('432.00'),
            total_amount=Decimal('2832.00'),
            stock_before=0,
            stock_after=2,
        )

        response = self.client.get(
            reverse('mobile_api_products'),
            HTTP_AUTHORIZATION=f'Bearer {self.token}',
        )

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload['summary']['total_products'], 1)
        self.assertEqual(payload['products'][0]['hsn_sac_code'], '8471')
        self.assertEqual(payload['products'][0]['latest_purchase_party'], 'History Supplier')
        self.assertEqual(payload['products'][0]['combined_history'][0]['entry_type'], 'purchase')

    def test_mobile_inventory_register_returns_grouped_purchase_rows(self):
        supplier = InventoryParty.objects.create(
            name='Register Supplier',
            party_type='supplier',
        )
        product = Product.objects.create(
            name='SSD 512GB',
            category='Storage',
            unit_price=Decimal('4200.00'),
            cost_price=Decimal('3200.00'),
            stock_quantity=3,
        )
        bill = InventoryBill.objects.create(
            bill_number='PB-260403-020',
            entry_type='purchase',
            entry_date=timezone.localdate(),
            party=supplier,
            invoice_number='PUR-INV-20',
        )
        InventoryEntry.objects.create(
            bill=bill,
            entry_number='PUR-260403-020',
            entry_type='purchase',
            entry_date=timezone.localdate(),
            invoice_number='PUR-INV-20',
            party=supplier,
            product=product,
            quantity=3,
            unit_price=Decimal('3200.00'),
            gst_rate=Decimal('18.00'),
            taxable_amount=Decimal('9600.00'),
            gst_amount=Decimal('1728.00'),
            total_amount=Decimal('11328.00'),
            stock_before=0,
            stock_after=3,
            created_by=self.user,
        )

        response = self.client.get(
            reverse('mobile_api_inventory_register', kwargs={'entry_type': 'purchase'}),
            HTTP_AUTHORIZATION=f'Bearer {self.token}',
        )

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload['config']['title'], 'Purchase')
        self.assertEqual(payload['summary']['entry_count'], 1)
        self.assertEqual(payload['register_rows'][0]['party_name'], 'Register Supplier')
        self.assertTrue(payload['register_rows'][0]['show_return_action'])


class MobileTechnicianApiTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(
            username='mobile-tech',
            password='StrongPass123!',
        )
        self.technician = TechnicianProfile.objects.create(
            user=self.user,
            unique_id='MT100',
        )
        self.token = issue_mobile_jwt(self.user)
        self.job = JobTicket.objects.create(
            job_code='GI-260506-501',
            customer_name='Mobile App Client',
            customer_phone='9876543299',
            device_type='Laptop',
            device_brand='Dell',
            device_model='Latitude',
            reported_issue='Needs inspection',
            assigned_to=self.technician,
            status='Repairing',
            requires_laptop_inspection_checklist=True,
        )

    def test_mobile_technician_can_save_checklist_answers(self):
        response = self.client.post(
            reverse('mobile_api_job_checklist', kwargs={'job_code': self.job.job_code}),
            data=json.dumps({'answers': {'ports_condition': 'Good', 'body_condition': 'Good'}}),
            content_type='application/json',
            HTTP_AUTHORIZATION=f'Bearer {self.token}',
        )

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertTrue(payload['ok'])
        self.job.refresh_from_db()
        self.assertEqual(self.job.technician_checklist['ports_condition'], 'Good')
        self.assertEqual(self.job.technician_checklist['body_condition'], 'Good')

    def test_mobile_technician_can_upload_job_photo(self):
        photo = SimpleUploadedFile(
            'device.jpg',
            b'\xff\xd8\xff\xe0mobile-photo-bytes',
            content_type='image/jpeg',
        )

        response = self.client.post(
            reverse('mobile_api_job_photos', kwargs={'job_code': self.job.job_code}),
            data={'photos': [photo]},
            HTTP_AUTHORIZATION=f'Bearer {self.token}',
        )

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertTrue(payload['ok'])
        self.assertEqual(len(payload['photos']), 1)
        stored_photo = JobTicketPhoto.objects.get(job_ticket=self.job)
        self.assertEqual(stored_photo.image_name, 'device.jpg')
        self.assertEqual(stored_photo.image_content_type, 'image/jpeg')
        self.assertEqual(bytes(stored_photo.image_data), b'\xff\xd8\xff\xe0mobile-photo-bytes')

    def test_mobile_technician_rejects_non_image_photo_content(self):
        photo = SimpleUploadedFile(
            'device.jpg',
            b'not-an-image',
            content_type='image/jpeg',
        )

        response = self.client.post(
            reverse('mobile_api_job_photos', kwargs={'job_code': self.job.job_code}),
            data={'photos': [photo]},
            HTTP_AUTHORIZATION=f'Bearer {self.token}',
        )

        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()['error'], 'invalid_photo')

    def test_mobile_jobs_include_dashboard_totals_and_new_assignment_flag(self):
        self.job.is_new_assignment = True
        self.job.save(update_fields=['is_new_assignment'])
        ServiceLog.objects.create(
            job_ticket=self.job,
            description='Keyboard replacement',
            part_cost=Decimal('1200.00'),
            service_charge=Decimal('300.00'),
        )

        response = self.client.get(
            reverse('mobile_api_jobs'),
            HTTP_AUTHORIZATION=f'Bearer {self.token}',
        )

        self.assertEqual(response.status_code, 200)
        job_payload = response.json()['jobs'][0]
        self.assertEqual(job_payload['job_code'], self.job.job_code)
        self.assertEqual(job_payload['part_total'], '1200.00')
        self.assertEqual(job_payload['service_total'], '300.00')
        self.assertTrue(job_payload['is_new_assignment'])
        self.assertFalse(job_payload['returned_from_vendor'])

    def test_mobile_technician_can_acknowledge_new_assignment(self):
        self.job.is_new_assignment = True
        self.job.save(update_fields=['is_new_assignment'])

        response = self.client.post(
            reverse('mobile_api_job_action', kwargs={'job_code': self.job.job_code}),
            data=json.dumps({'action': 'acknowledge'}),
            content_type='application/json',
            HTTP_AUTHORIZATION=f'Bearer {self.token}',
        )

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()['ok'])
        self.job.refresh_from_db()
        self.assertFalse(self.job.is_new_assignment)

    def test_mobile_technician_can_return_new_assignment_to_staff(self):
        self.job.is_new_assignment = True
        self.job.save(update_fields=['is_new_assignment'])

        response = self.client.post(
            reverse('mobile_api_job_action', kwargs={'job_code': self.job.job_code}),
            data=json.dumps({'action': 'return_to_staff'}),
            content_type='application/json',
            HTTP_AUTHORIZATION=f'Bearer {self.token}',
        )

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()['ok'])
        self.job.refresh_from_db()
        self.assertIsNone(self.job.assigned_to)
        self.assertEqual(self.job.status, 'Pending')
        self.assertFalse(self.job.is_new_assignment)

    def test_mobile_technician_detail_includes_webapp_permission_controls(self):
        response = self.client.get(
            reverse('mobile_api_job_detail', kwargs={'job_code': self.job.job_code}),
            HTTP_AUTHORIZATION=f'Bearer {self.token}',
        )

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertTrue(payload['permissions']['can_change_status'])
        self.assertTrue(payload['permissions']['can_request_specialized_service'])
        self.assertIn({'value': 'Repairing', 'label': 'Repairing'}, payload['status_choices'])
        self.assertNotIn({'value': 'Pending', 'label': 'Pending'}, payload['status_choices'])
        self.assertNotIn({'value': 'Specialized Service', 'label': 'Specialized Service'}, payload['status_choices'])
        self.assertTrue(payload['checklist_required_for_completion'])

    def test_mobile_technician_can_update_status_notes_and_checklist_together(self):
        response = self.client.post(
            reverse('mobile_api_job_technician_update', kwargs={'job_code': self.job.job_code}),
            data=json.dumps(
                {
                    'status': 'Under Inspection',
                    'technician_notes': 'Checking display and ports.',
                    'answers': {'ports_condition': 'Good'},
                }
            ),
            content_type='application/json',
            HTTP_AUTHORIZATION=f'Bearer {self.token}',
        )

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()['ok'])
        self.job.refresh_from_db()
        self.assertEqual(self.job.status, 'Under Inspection')
        self.assertEqual(self.job.technician_notes, 'Checking display and ports.')
        self.assertEqual(self.job.technician_checklist['ports_condition'], 'Good')

    def test_mobile_technician_cannot_set_pending_status(self):
        response = self.client.post(
            reverse('mobile_api_job_technician_update', kwargs={'job_code': self.job.job_code}),
            data=json.dumps({'status': 'Pending', 'technician_notes': '', 'answers': {}}),
            content_type='application/json',
            HTTP_AUTHORIZATION=f'Bearer {self.token}',
        )

        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()['error'], 'invalid_status')

    def test_mobile_technician_can_edit_service_line(self):
        service_log = ServiceLog.objects.create(
            job_ticket=self.job,
            description='Old repair',
            part_cost=Decimal('100.00'),
            service_charge=Decimal('50.00'),
        )

        response = self.client.post(
            reverse(
                'mobile_api_service_line_update',
                kwargs={'job_code': self.job.job_code, 'line_id': service_log.id},
            ),
            data=json.dumps(
                {
                    'description': 'Updated repair',
                    'part_cost': '125.00',
                    'service_charge': '75.00',
                }
            ),
            content_type='application/json',
            HTTP_AUTHORIZATION=f'Bearer {self.token}',
        )

        self.assertEqual(response.status_code, 200)
        service_log.refresh_from_db()
        self.assertEqual(service_log.description, 'Updated repair')
        self.assertEqual(service_log.part_cost, Decimal('125.00'))
        self.assertEqual(service_log.service_charge, Decimal('75.00'))

    def test_mobile_technician_can_request_specialized_service(self):
        response = self.client.post(
            reverse('mobile_api_job_action', kwargs={'job_code': self.job.job_code}),
            data=json.dumps({'action': 'request_specialized_service'}),
            content_type='application/json',
            HTTP_AUTHORIZATION=f'Bearer {self.token}',
        )

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()['ok'])
        self.job.refresh_from_db()
        self.assertEqual(self.job.status, 'Specialized Service')
        self.assertTrue(SpecializedService.objects.filter(job_ticket=self.job).exists())


class StaffJobCreationWhatsAppTests(TestCase):
    def setUp(self):
        self.staff_user = User.objects.create_user(
            username='whatsapp-job-admin',
            password='StrongPass123!',
            is_staff=True,
            is_superuser=True,
        )
        self.client.force_login(self.staff_user)

        self.settings_obj = WhatsAppIntegrationSettings.get_settings()
        self.settings_obj.is_enabled = True
        self.settings_obj.api_version = 'v23.0'
        self.settings_obj.phone_number_id = '123456789012345'
        self.settings_obj.access_token = 'token-123'
        self.settings_obj.public_site_url = 'https://botgi.example.com'
        self.settings_obj.template_language_code = 'en_US'
        self.settings_obj.notify_on_created = True
        self.settings_obj.created_template_name = 'job_created_update'
        self.settings_obj.created_template = 'Hello {customer_name}, ticket {job_code}. Receipt: {receipt_link}'
        self.settings_obj.save()

    @patch('job_tickets.whatsapp_service.requests.request')
    def test_staff_dashboard_job_create_sends_created_ticket_pdf_message(self, mock_request):
        mock_response = Mock()
        mock_response.ok = True
        mock_response.status_code = 200
        mock_response.json.return_value = {'messages': [{'id': 'wamid.HBgM123456'}]}
        mock_request.return_value = mock_response

        with self.captureOnCommitCallbacks(execute=True):
            response = self.client.post(
                reverse('staff_dashboard'),
                {
                    'job_ticket_form_submit': '1',
                    'customer_name': 'Anand',
                    'customer_phone': '9876543210',
                    'estimated_amount': '',
                    'estimated_delivery': '',
                    'device_forms[0].device_type': 'Laptop',
                    'device_forms[0].device_brand': 'Dell',
                    'device_forms[0].device_model': 'Latitude',
                    'device_forms[0].device_serial': '',
                    'device_forms[0].reported_issue': 'Battery issue',
                    'device_forms[0].additional_items': '',
                },
            )

        self.assertEqual(response.status_code, 302)

        job = JobTicket.objects.get(customer_name='Anand', customer_phone='9876543210')
        self.assertEqual(response.url, reverse('job_creation_success', args=[job.job_code]))

        queue = MessageQueue.objects.get(job_ticket=job, event_type=MessageQueue.EVENT_CREATED)
        self.assertEqual(queue.status, MessageQueue.STATUS_SENT)
        self.assertEqual(queue.transport, 'whatsapp-cloud-api-document')
        self.assertEqual(queue.bridge_message_id, 'wamid.HBgM123456')
        self.assertIn('/client-receipt/', queue.pdf_url)
        self.assertIn('/pdf/', queue.pdf_url)
        self.assertIn('/client-receipt/', queue.caption)
        self.assertEqual(queue.filename, f'{job.job_code}.pdf')

        payload = mock_request.call_args.kwargs['json']
        self.assertEqual(payload['type'], 'document')
        self.assertEqual(payload['to'], '919876543210')
        self.assertEqual(payload['document']['link'], queue.pdf_url)
        self.assertIn('/client-receipt/', payload['document']['caption'])


# =============================================================================
# P3 Accounting Tests — Phases 2–10
# Written against P2 baseline (117 existing tests preserved intact)
# =============================================================================

from .models import (
    InventoryBillLog,
    InventoryVendorCredit,
    InventoryVendorCreditApplication,
)
from .views.helpers import (
    _reverse_inventory_bill,
    _link_return_to_source_bill,
    _apply_vendor_credit_to_bill,
    _check_duplicate_bill_submission,
    _inventory_bill_total,
    _inventory_bill_paid_total,
    _inventory_bill_effective_balance,
    _write_bill_log,
    _record_inventory_entries,
    _generate_inventory_bill_number,
)
import datetime as _dt


# ---------------------------------------------------------------------------
# Shared fixture helpers
# ---------------------------------------------------------------------------

def _make_workspace_and_user(username='p3-user'):
    user = User.objects.create_user(
        username=username,
        password='StrongPass123!',
        is_staff=True,
    )
    from job_tickets.access_control import apply_staff_access
    apply_staff_access(user, {'staff_dashboard', 'inventory', 'task_management'})
    ws = CompanyWorkspace.objects.create(name=f'P3 Workspace {username}', owner=user)
    from job_tickets.models import CompanyUserMembership
    CompanyUserMembership.objects.create(
        workspace=ws,
        user=user,
        role=CompanyUserMembership.ROLE_ADMIN,
    )
    return ws, user


def _make_party(workspace, name='Test Party'):
    return InventoryParty.objects.create(
        workspace=workspace,
        name=name,
        party_type='both',
    )


def _make_product(workspace, name='Widget', stock=100, cost_price='10.00', unit_price='20.00'):
    return Product.objects.create(
        workspace=workspace,
        name=name,
        stock_quantity=stock,
        cost_price=Decimal(cost_price),
        unit_price=Decimal(unit_price),
        is_active=True,
    )


def _make_purchase_bill(workspace, party, product, quantity=5, unit_price='10.00', gst_rate='0.00', user=None):
    """Create a purchase InventoryBill with one line using the service layer."""
    from job_tickets.views.helpers import _generate_inventory_bill_number, _generate_inventory_entry_number
    from django.utils import timezone
    import uuid as _uuid
    entry_date = timezone.localdate()
    bill = InventoryBill.objects.create(
        workspace=workspace,
        bill_number=_generate_inventory_bill_number('purchase', entry_date, workspace),
        entry_type='purchase',
        entry_date=entry_date,
        invoice_number=f'PB-TEST-{_uuid.uuid4().hex[:8]}',
        party=party,
        created_by=user,
    )
    up = Decimal(unit_price)
    gr = Decimal(gst_rate)
    qty = int(quantity)
    taxable = (Decimal(qty) * up).quantize(Decimal('0.01'))
    gst_amt = (taxable * gr / Decimal('100')).quantize(Decimal('0.01'))
    total = (taxable + gst_amt).quantize(Decimal('0.01'))
    stock_before = product.stock_quantity
    stock_after = stock_before + qty
    InventoryEntry.objects.create(
        workspace=workspace,
        bill=bill,
        entry_number=_generate_inventory_entry_number('purchase', entry_date, workspace),
        entry_type='purchase',
        entry_date=entry_date,
        invoice_number=bill.invoice_number,
        party=party,
        product=product,
        quantity=qty,
        unit_price=up,
        discount_amount=Decimal('0.00'),
        gst_rate=gr,
        taxable_amount=taxable,
        gst_amount=gst_amt,
        total_amount=total,
        stock_before=stock_before,
        stock_after=stock_after,
        created_by=user,
    )
    product.stock_quantity = stock_after
    product.save(update_fields=['stock_quantity'])
    return bill


def _make_sale_bill(workspace, party, product, quantity=2, unit_price='20.00', gst_rate='0.00', user=None):
    """Create a sale InventoryBill with one line."""
    from job_tickets.views.helpers import _generate_inventory_bill_number, _generate_inventory_entry_number
    from django.utils import timezone
    import uuid as _uuid
    entry_date = timezone.localdate()
    bill = InventoryBill.objects.create(
        workspace=workspace,
        bill_number=_generate_inventory_bill_number('sale', entry_date, workspace),
        entry_type='sale',
        entry_date=entry_date,
        invoice_number=f'SB-TEST-{_uuid.uuid4().hex[:8]}',
        party=party,
        created_by=user,
    )
    up = Decimal(unit_price)
    gr = Decimal(gst_rate)
    qty = int(quantity)
    taxable = (Decimal(qty) * up).quantize(Decimal('0.01'))
    gst_amt = (taxable * gr / Decimal('100')).quantize(Decimal('0.01'))
    total = (taxable + gst_amt).quantize(Decimal('0.01'))
    stock_before = product.stock_quantity
    stock_after = stock_before - qty
    InventoryEntry.objects.create(
        workspace=workspace,
        bill=bill,
        entry_number=_generate_inventory_entry_number('sale', entry_date, workspace),
        entry_type='sale',
        entry_date=entry_date,
        invoice_number=bill.invoice_number,
        party=party,
        product=product,
        quantity=qty,
        unit_price=up,
        discount_amount=Decimal('0.00'),
        gst_rate=gr,
        taxable_amount=taxable,
        gst_amount=gst_amt,
        total_amount=total,
        stock_before=stock_before,
        stock_after=stock_after,
        created_by=user,
    )
    product.stock_quantity = stock_after
    product.save(update_fields=['stock_quantity'])
    return bill


def _pay_bill_fully(bill, user=None):
    """Create a single InventoryCreditPayment that fully settles the bill."""
    from job_tickets.views.helpers import (
        _inventory_bill_total,
        _inventory_credit_direction_for_entry_type,
    )
    from django.utils import timezone
    total = _inventory_bill_total(bill)
    direction = _inventory_credit_direction_for_entry_type(bill.entry_type)
    if not direction or total <= Decimal('0.00'):
        return None
    return InventoryCreditPayment.objects.create(
        workspace=bill.workspace,
        party=bill.party,
        bill=bill,
        direction=direction,
        payment_date=timezone.localdate(),
        payment_method=InventoryCreditPayment.METHOD_CASH,
        amount=total,
        balance_before=total,
        balance_after=Decimal('0.00'),
        reference_no='',
        notes='Test full payment',
        created_by=user,
    )


def _pay_bill_partial(bill, amount, user=None):
    """Create a partial InventoryCreditPayment against the bill."""
    from job_tickets.views.helpers import (
        _inventory_bill_total,
        _inventory_credit_direction_for_entry_type,
    )
    from django.utils import timezone
    total = _inventory_bill_total(bill)
    direction = _inventory_credit_direction_for_entry_type(bill.entry_type)
    if not direction:
        return None
    paid_so_far = _inventory_bill_paid_total(bill)
    balance_before = total - paid_so_far
    return InventoryCreditPayment.objects.create(
        workspace=bill.workspace,
        party=bill.party,
        bill=bill,
        direction=direction,
        payment_date=timezone.localdate(),
        payment_method=InventoryCreditPayment.METHOD_CASH,
        amount=amount,
        balance_before=balance_before,
        balance_after=(balance_before - amount).quantize(Decimal('0.01')),
        reference_no='',
        notes='Test partial payment',
        created_by=user,
    )


# ---------------------------------------------------------------------------
# Phase 2 + 4: Paid-Bill Reversal & Stock Reversal
# ---------------------------------------------------------------------------

class BillReversalUnpaidTests(TestCase):
    """An unpaid bill can be reversed; stock is restored; original is marked reversed."""

    def setUp(self):
        self.ws, self.user = _make_workspace_and_user('rev-unpaid')
        self.party = _make_party(self.ws)
        self.product = _make_product(self.ws, stock=100)
        self.bill = _make_purchase_bill(self.ws, self.party, self.product, quantity=10, user=self.user)

    def test_stock_increases_by_purchase_quantity_on_creation(self):
        self.product.refresh_from_db()
        self.assertEqual(self.product.stock_quantity, 110)

    def test_reversal_restores_stock(self):
        reversal_bill, payment_state = _reverse_inventory_bill(
            self.bill.pk, self.user, 'Test reversal', workspace=self.ws
        )
        self.product.refresh_from_db()
        # Purchase adds stock; reversal (purchase_return) removes it back
        self.assertEqual(self.product.stock_quantity, 100)

    def test_reversal_creates_opposite_entry_type(self):
        reversal_bill, _ = _reverse_inventory_bill(
            self.bill.pk, self.user, 'Test reversal', workspace=self.ws
        )
        self.assertEqual(reversal_bill.entry_type, 'purchase_return')

    def test_reversal_links_reversal_of_on_new_bill(self):
        reversal_bill, _ = _reverse_inventory_bill(
            self.bill.pk, self.user, 'Test reversal', workspace=self.ws
        )
        self.assertEqual(reversal_bill.reversal_of_id, self.bill.pk)

    def test_original_bill_is_marked_reversed(self):
        _reverse_inventory_bill(self.bill.pk, self.user, 'Test reversal', workspace=self.ws)
        self.bill.refresh_from_db()
        self.assertTrue(self.bill.is_reversed)

    def test_original_bill_stores_reversal_metadata(self):
        _reverse_inventory_bill(self.bill.pk, self.user, 'Correction note', workspace=self.ws)
        self.bill.refresh_from_db()
        self.assertEqual(self.bill.reversal_note, 'Correction note')
        self.assertIsNotNone(self.bill.reversed_at)
        self.assertEqual(self.bill.reversed_by, self.user)

    def test_payment_state_is_unpaid(self):
        _, payment_state = _reverse_inventory_bill(
            self.bill.pk, self.user, 'Test', workspace=self.ws
        )
        self.assertEqual(payment_state, 'unpaid')

    def test_audit_log_written_on_original(self):
        _reverse_inventory_bill(self.bill.pk, self.user, 'Test', workspace=self.ws)
        self.assertTrue(
            InventoryBillLog.objects.filter(
                bill=self.bill,
                action=InventoryBillLog.ACTION_BILL_REVERSED,
            ).exists()
        )

    def test_audit_log_written_on_reversal(self):
        reversal_bill, _ = _reverse_inventory_bill(self.bill.pk, self.user, 'Test', workspace=self.ws)
        self.assertTrue(
            InventoryBillLog.objects.filter(
                bill=reversal_bill,
                action=InventoryBillLog.ACTION_REVERSAL_CREATED,
            ).exists()
        )


class BillReversalSaleUnpaidTests(TestCase):
    """Reversing an unpaid sale bill restores stock (adds back)."""

    def setUp(self):
        self.ws, self.user = _make_workspace_and_user('rev-sale-unpaid')
        self.party = _make_party(self.ws)
        self.product = _make_product(self.ws, stock=50)
        self.bill = _make_sale_bill(self.ws, self.party, self.product, quantity=5, user=self.user)

    def test_sale_removes_stock(self):
        self.product.refresh_from_db()
        self.assertEqual(self.product.stock_quantity, 45)

    def test_reversal_adds_stock_back(self):
        _reverse_inventory_bill(self.bill.pk, self.user, 'Return', workspace=self.ws)
        self.product.refresh_from_db()
        self.assertEqual(self.product.stock_quantity, 50)

    def test_reversal_entry_type_is_sale_return(self):
        reversal_bill, _ = _reverse_inventory_bill(self.bill.pk, self.user, 'Return', workspace=self.ws)
        self.assertEqual(reversal_bill.entry_type, 'sale_return')


class BillReversalPartiallyPaidTests(TestCase):
    """A partially paid bill can be reversed; settlement is auto-applied on reversal bill."""

    def setUp(self):
        self.ws, self.user = _make_workspace_and_user('rev-partial')
        self.party = _make_party(self.ws)
        self.product = _make_product(self.ws, stock=100)
        self.bill = _make_purchase_bill(
            self.ws, self.party, self.product, quantity=10,
            unit_price='10.00', gst_rate='0.00', user=self.user
        )  # total = 100.00
        _pay_bill_partial(self.bill, Decimal('40.00'), user=self.user)

    def test_payment_state_is_partially_paid(self):
        _, payment_state = _reverse_inventory_bill(self.bill.pk, self.user, 'Fix', workspace=self.ws)
        self.assertEqual(payment_state, 'partially_paid')

    def test_reversal_creates_auto_settlement_on_reversal_bill(self):
        reversal_bill, _ = _reverse_inventory_bill(self.bill.pk, self.user, 'Fix', workspace=self.ws)
        # Auto-settlement credit payment should exist on reversal bill
        auto_payments = InventoryCreditPayment.objects.filter(bill=reversal_bill)
        self.assertTrue(auto_payments.exists())
        total_settled = sum(p.amount for p in auto_payments)
        # Settlement should equal amount paid on original (40.00), capped at reversal total (100.00)
        self.assertEqual(total_settled, Decimal('40.00'))

    def test_original_bill_is_marked_reversed(self):
        _reverse_inventory_bill(self.bill.pk, self.user, 'Fix', workspace=self.ws)
        self.bill.refresh_from_db()
        self.assertTrue(self.bill.is_reversed)

    def test_stock_is_restored(self):
        _reverse_inventory_bill(self.bill.pk, self.user, 'Fix', workspace=self.ws)
        self.product.refresh_from_db()
        self.assertEqual(self.product.stock_quantity, 100)


class BillReversalFullyPaidTests(TestCase):
    """A fully paid bill can be reversed; full settlement is auto-applied on reversal bill."""

    def setUp(self):
        self.ws, self.user = _make_workspace_and_user('rev-full')
        self.party = _make_party(self.ws)
        self.product = _make_product(self.ws, stock=100)
        self.bill = _make_purchase_bill(
            self.ws, self.party, self.product, quantity=10,
            unit_price='10.00', gst_rate='0.00', user=self.user
        )  # total = 100.00
        _pay_bill_fully(self.bill, user=self.user)

    def test_payment_state_is_fully_paid(self):
        _, payment_state = _reverse_inventory_bill(self.bill.pk, self.user, 'Fix', workspace=self.ws)
        self.assertEqual(payment_state, 'fully_paid')

    def test_reversal_creates_full_settlement_on_reversal_bill(self):
        reversal_bill, _ = _reverse_inventory_bill(self.bill.pk, self.user, 'Fix', workspace=self.ws)
        auto_payments = InventoryCreditPayment.objects.filter(bill=reversal_bill)
        self.assertTrue(auto_payments.exists())
        total_settled = sum(p.amount for p in auto_payments)
        self.assertEqual(total_settled, Decimal('100.00'))

    def test_original_bill_is_marked_reversed(self):
        _reverse_inventory_bill(self.bill.pk, self.user, 'Fix', workspace=self.ws)
        self.bill.refresh_from_db()
        self.assertTrue(self.bill.is_reversed)

    def test_stock_is_restored(self):
        _reverse_inventory_bill(self.bill.pk, self.user, 'Fix', workspace=self.ws)
        self.product.refresh_from_db()
        self.assertEqual(self.product.stock_quantity, 100)


class BillDoubleReversalRejectionTests(TestCase):
    """A reversed bill cannot be reversed again."""

    def setUp(self):
        self.ws, self.user = _make_workspace_and_user('rev-double')
        self.party = _make_party(self.ws)
        self.product = _make_product(self.ws, stock=100)
        self.bill = _make_purchase_bill(self.ws, self.party, self.product, quantity=5, user=self.user)
        _reverse_inventory_bill(self.bill.pk, self.user, 'First reversal', workspace=self.ws)

    def test_second_reversal_raises(self):
        with self.assertRaises(ValueError) as ctx:
            _reverse_inventory_bill(self.bill.pk, self.user, 'Attempt again', workspace=self.ws)
        self.assertIn('already been reversed', str(ctx.exception))

    def test_reversal_of_reversal_raises(self):
        reversal_bill = InventoryBill.objects.filter(reversal_of=self.bill).first()
        self.assertIsNotNone(reversal_bill)
        with self.assertRaises(ValueError) as ctx:
            _reverse_inventory_bill(reversal_bill.pk, self.user, 'Attempt', workspace=self.ws)
        self.assertIn('reversal bill', str(ctx.exception))


class BillReversalPreservesHistoricalDataTests(TestCase):
    """The original bill and its entries must not be modified by a reversal."""

    def setUp(self):
        self.ws, self.user = _make_workspace_and_user('rev-history')
        self.party = _make_party(self.ws)
        self.product = _make_product(self.ws, stock=100)
        self.bill = _make_purchase_bill(
            self.ws, self.party, self.product, quantity=8,
            unit_price='15.00', gst_rate='0.00', user=self.user
        )
        self.original_entry = InventoryEntry.objects.filter(bill=self.bill).first()
        self.original_entry_number = self.original_entry.entry_number
        self.original_quantity = self.original_entry.quantity
        self.original_total = self.original_entry.total_amount
        self.original_stock_before = self.original_entry.stock_before
        self.original_stock_after = self.original_entry.stock_after

    def test_original_entry_survives_intact(self):
        _reverse_inventory_bill(self.bill.pk, self.user, 'Test', workspace=self.ws)
        self.original_entry.refresh_from_db()
        self.assertEqual(self.original_entry.entry_number, self.original_entry_number)
        self.assertEqual(self.original_entry.quantity, self.original_quantity)
        self.assertEqual(self.original_entry.total_amount, self.original_total)
        self.assertEqual(self.original_entry.stock_before, self.original_stock_before)
        self.assertEqual(self.original_entry.stock_after, self.original_stock_after)

    def test_original_bill_financial_fields_unchanged(self):
        original_bill_number = self.bill.bill_number
        original_party = self.bill.party_id
        _reverse_inventory_bill(self.bill.pk, self.user, 'Test', workspace=self.ws)
        self.bill.refresh_from_db()
        self.assertEqual(self.bill.bill_number, original_bill_number)
        self.assertEqual(self.bill.party_id, original_party)
        # entry_type, entry_date, invoice_number all remain unchanged
        self.assertEqual(self.bill.entry_type, 'purchase')


class BillReversalStockSnapshotTests(TestCase):
    """Reversal entry must carry correct stock_before and stock_after snapshots."""

    def setUp(self):
        self.ws, self.user = _make_workspace_and_user('rev-snap')
        self.party = _make_party(self.ws)
        self.product = _make_product(self.ws, stock=100)
        self.bill = _make_purchase_bill(
            self.ws, self.party, self.product, quantity=10, user=self.user
        )  # stock: 100 → 110

    def test_reversal_entry_snapshot_is_correct(self):
        reversal_bill, _ = _reverse_inventory_bill(self.bill.pk, self.user, 'Test', workspace=self.ws)
        rev_entry = InventoryEntry.objects.filter(bill=reversal_bill).first()
        self.assertIsNotNone(rev_entry)
        # stock before reversal entry = 110 (product was at 110 after purchase)
        self.assertEqual(rev_entry.stock_before, 110)
        # stock after reversal = 110 - 10 = 100
        self.assertEqual(rev_entry.stock_after, 100)


class BillReversalAtomicRollbackTests(TestCase):
    """If reversal fails mid-way (e.g. stock underflow), nothing is committed."""

    def setUp(self):
        self.ws, self.user = _make_workspace_and_user('rev-rollback')
        self.party = _make_party(self.ws)
        self.product = _make_product(self.ws, stock=5)
        self.bill = _make_purchase_bill(
            self.ws, self.party, self.product, quantity=5, user=self.user
        )  # stock goes 5 → 10

    def test_reversal_of_purchase_that_would_understock_via_intermediate_sale(self):
        # Sell 8 units (beyond original 5 but within the 10 after purchase)
        sale_bill = _make_sale_bill(
            self.ws, self.party, self.product, quantity=8, user=self.user
        )  # stock: 10 → 2
        self.product.refresh_from_db()
        self.assertEqual(self.product.stock_quantity, 2)

        # Now try to reverse the original purchase: would return 5 units → 2 - 5 = -3 → should fail
        with self.assertRaises(ValueError) as ctx:
            _reverse_inventory_bill(self.bill.pk, self.user, 'Attempt', workspace=self.ws)
        self.assertIn('below zero', str(ctx.exception))

        # Stock must be unchanged (rollback)
        self.product.refresh_from_db()
        self.assertEqual(self.product.stock_quantity, 2)

        # Original bill must NOT be marked reversed
        self.bill.refresh_from_db()
        self.assertFalse(self.bill.is_reversed)

        # No reversal bill created
        self.assertFalse(InventoryBill.objects.filter(reversal_of=self.bill).exists())


class BillReversalWorkspaceScopeTests(TestCase):
    """A reversal cannot cross workspace boundaries."""

    def setUp(self):
        self.ws_a, self.user_a = _make_workspace_and_user('rev-ws-a')
        self.ws_b, self.user_b = _make_workspace_and_user('rev-ws-b')
        self.party_a = _make_party(self.ws_a, 'Party A')
        self.product_a = _make_product(self.ws_a, stock=100)
        self.bill_a = _make_purchase_bill(self.ws_a, self.party_a, self.product_a, user=self.user_a)

    def test_reversal_from_wrong_workspace_fails(self):
        with self.assertRaises(ValueError):
            _reverse_inventory_bill(self.bill_a.pk, self.user_b, 'Cross attempt', workspace=self.ws_b)

    def test_original_bill_not_marked_reversed_on_cross_workspace_attempt(self):
        try:
            _reverse_inventory_bill(self.bill_a.pk, self.user_b, 'Cross', workspace=self.ws_b)
        except ValueError:
            pass
        self.bill_a.refresh_from_db()
        self.assertFalse(self.bill_a.is_reversed)


# ---------------------------------------------------------------------------
# Phase 5–6: Purchase Return → Vendor Credit Linkage
# ---------------------------------------------------------------------------

class PurchaseReturnLinkageTests(TestCase):
    """A purchase_return bill can be linked to its source purchase bill."""

    def setUp(self):
        self.ws, self.user = _make_workspace_and_user('ret-link')
        self.party = _make_party(self.ws)
        self.product = _make_product(self.ws, stock=100)
        self.purchase_bill = _make_purchase_bill(
            self.ws, self.party, self.product, quantity=10,
            unit_price='10.00', gst_rate='0.00', user=self.user
        )
        # Create a purchase_return bill manually
        from job_tickets.views.helpers import _generate_inventory_bill_number, _generate_inventory_entry_number
        from django.utils import timezone
        entry_date = timezone.localdate()
        self.return_bill = InventoryBill.objects.create(
            workspace=self.ws,
            bill_number=_generate_inventory_bill_number('purchase_return', entry_date, self.ws),
            entry_type='purchase_return',
            entry_date=entry_date,
            invoice_number='PRN-TEST-001',
            party=self.party,
            created_by=self.user,
        )
        InventoryEntry.objects.create(
            workspace=self.ws,
            bill=self.return_bill,
            entry_number=_generate_inventory_entry_number('purchase_return', entry_date, self.ws),
            entry_type='purchase_return',
            entry_date=entry_date,
            invoice_number='PRN-TEST-001',
            party=self.party,
            product=self.product,
            quantity=3,
            unit_price=Decimal('10.00'),
            discount_amount=Decimal('0.00'),
            gst_rate=Decimal('0.00'),
            taxable_amount=Decimal('30.00'),
            gst_amount=Decimal('0.00'),
            total_amount=Decimal('30.00'),
            stock_before=110,
            stock_after=107,
            created_by=self.user,
        )
        self.product.stock_quantity = 107
        self.product.save(update_fields=['stock_quantity'])

    def test_link_sets_source_bill(self):
        _link_return_to_source_bill(
            self.return_bill.pk, self.purchase_bill.pk, self.user, workspace=self.ws
        )
        self.return_bill.refresh_from_db()
        self.assertEqual(self.return_bill.source_bill_id, self.purchase_bill.pk)

    def test_link_creates_vendor_credit(self):
        _, vc = _link_return_to_source_bill(
            self.return_bill.pk, self.purchase_bill.pk, self.user, workspace=self.ws
        )
        self.assertIsNotNone(vc)
        self.assertIsInstance(vc, InventoryVendorCredit)

    def test_vendor_credit_amount_equals_return_total(self):
        _, vc = _link_return_to_source_bill(
            self.return_bill.pk, self.purchase_bill.pk, self.user, workspace=self.ws
        )
        self.assertEqual(vc.credit_amount, Decimal('30.00'))

    def test_vendor_credit_status_is_open(self):
        _, vc = _link_return_to_source_bill(
            self.return_bill.pk, self.purchase_bill.pk, self.user, workspace=self.ws
        )
        self.assertEqual(vc.status, InventoryVendorCredit.STATUS_OPEN)

    def test_vendor_credit_links_source_bill(self):
        _, vc = _link_return_to_source_bill(
            self.return_bill.pk, self.purchase_bill.pk, self.user, workspace=self.ws
        )
        self.assertEqual(vc.source_bill_id, self.purchase_bill.pk)

    def test_double_link_raises(self):
        _link_return_to_source_bill(
            self.return_bill.pk, self.purchase_bill.pk, self.user, workspace=self.ws
        )
        with self.assertRaises(ValueError) as ctx:
            _link_return_to_source_bill(
                self.return_bill.pk, self.purchase_bill.pk, self.user, workspace=self.ws
            )
        self.assertIn('already linked', str(ctx.exception))

    def test_audit_log_written(self):
        _link_return_to_source_bill(
            self.return_bill.pk, self.purchase_bill.pk, self.user, workspace=self.ws
        )
        self.assertTrue(
            InventoryBillLog.objects.filter(
                bill=self.return_bill,
                action=InventoryBillLog.ACTION_RETURN_LINKED,
            ).exists()
        )

    def test_vendor_credit_audit_log_written(self):
        _link_return_to_source_bill(
            self.return_bill.pk, self.purchase_bill.pk, self.user, workspace=self.ws
        )
        self.assertTrue(
            InventoryBillLog.objects.filter(
                bill=self.return_bill,
                action=InventoryBillLog.ACTION_VENDOR_CREDIT_CREATED,
            ).exists()
        )


class PurchaseReturnWrongPartyTests(TestCase):
    """A return bill cannot be linked to a source bill with a different party."""

    def setUp(self):
        self.ws, self.user = _make_workspace_and_user('ret-party')
        self.party_a = _make_party(self.ws, 'Party A')
        self.party_b = _make_party(self.ws, 'Party B')
        self.product = _make_product(self.ws, stock=100)
        self.purchase_bill = _make_purchase_bill(self.ws, self.party_a, self.product, user=self.user)
        from job_tickets.views.helpers import _generate_inventory_bill_number, _generate_inventory_entry_number
        from django.utils import timezone
        entry_date = timezone.localdate()
        self.return_bill = InventoryBill.objects.create(
            workspace=self.ws,
            bill_number=_generate_inventory_bill_number('purchase_return', entry_date, self.ws),
            entry_type='purchase_return',
            entry_date=entry_date,
            invoice_number='PRN-DIFF-001',
            party=self.party_b,  # Different party
            created_by=self.user,
        )

    def test_cross_party_link_raises(self):
        with self.assertRaises(ValueError) as ctx:
            _link_return_to_source_bill(
                self.return_bill.pk, self.purchase_bill.pk, self.user, workspace=self.ws
            )
        self.assertIn('same party', str(ctx.exception))


class PurchaseReturnCrossWorkspaceLinkTests(TestCase):
    """A return bill cannot be linked to a source bill from another workspace."""

    def setUp(self):
        self.ws_a, self.user = _make_workspace_and_user('ret-cws')
        self.ws_b, _ = _make_workspace_and_user('ret-cws-b')
        self.party = _make_party(self.ws_a, 'Shared Party Name')
        self.product = _make_product(self.ws_a, stock=100)
        self.purchase_bill = _make_purchase_bill(self.ws_a, self.party, self.product, user=self.user)
        from job_tickets.views.helpers import _generate_inventory_bill_number
        from django.utils import timezone
        entry_date = timezone.localdate()
        self.return_bill = InventoryBill.objects.create(
            workspace=self.ws_a,
            bill_number=_generate_inventory_bill_number('purchase_return', entry_date, self.ws_a),
            entry_type='purchase_return',
            entry_date=entry_date,
            invoice_number='PRN-CWS-001',
            party=self.party,
            created_by=self.user,
        )

    def test_source_bill_in_wrong_workspace_fails(self):
        # Purchase bill is in ws_a; querying from ws_b scope should not find it
        with self.assertRaises(ValueError) as ctx:
            _link_return_to_source_bill(
                self.return_bill.pk, self.purchase_bill.pk, self.user, workspace=self.ws_b
            )
        # Either "Return bill not found" or "Source bill not found" is acceptable
        self.assertIn('not found', str(ctx.exception))


class ReturnBeforeVendorPaymentTests(TestCase):
    """Return before any vendor payment: full credit available, balance on purchase unchanged (credit reduces it)."""

    def setUp(self):
        self.ws, self.user = _make_workspace_and_user('ret-before-pay')
        self.party = _make_party(self.ws)
        self.product = _make_product(self.ws, stock=100)
        self.purchase_bill = _make_purchase_bill(
            self.ws, self.party, self.product,
            quantity=10, unit_price='10.00', gst_rate='0.00', user=self.user
        )  # total = 100.00

    def test_full_credit_available_before_payment(self):
        from job_tickets.views.helpers import _generate_inventory_bill_number, _generate_inventory_entry_number
        from django.utils import timezone
        entry_date = timezone.localdate()
        return_bill = InventoryBill.objects.create(
            workspace=self.ws,
            bill_number=_generate_inventory_bill_number('purchase_return', entry_date, self.ws),
            entry_type='purchase_return',
            entry_date=entry_date,
            invoice_number='PRN-BPY-001',
            party=self.party,
            created_by=self.user,
        )
        InventoryEntry.objects.create(
            workspace=self.ws, bill=return_bill,
            entry_number=_generate_inventory_entry_number('purchase_return', entry_date, self.ws),
            entry_type='purchase_return', entry_date=entry_date,
            invoice_number='PRN-BPY-001', party=self.party, product=self.product,
            quantity=5, unit_price=Decimal('10.00'), discount_amount=Decimal('0.00'),
            gst_rate=Decimal('0.00'), taxable_amount=Decimal('50.00'),
            gst_amount=Decimal('0.00'), total_amount=Decimal('50.00'),
            stock_before=110, stock_after=105, created_by=self.user,
        )
        _, vc = _link_return_to_source_bill(
            return_bill.pk, self.purchase_bill.pk, self.user, workspace=self.ws
        )
        self.assertEqual(vc.balance_amount, Decimal('50.00'))
        self.assertEqual(vc.status, InventoryVendorCredit.STATUS_OPEN)


class ReturnAfterPartialVendorPaymentTests(TestCase):
    """Return after partial payment: credit can be applied to remaining balance."""

    def setUp(self):
        self.ws, self.user = _make_workspace_and_user('ret-after-partial')
        self.party = _make_party(self.ws)
        self.product = _make_product(self.ws, stock=100)
        self.purchase_bill = _make_purchase_bill(
            self.ws, self.party, self.product,
            quantity=10, unit_price='10.00', gst_rate='0.00', user=self.user
        )  # total = 100.00
        _pay_bill_partial(self.purchase_bill, Decimal('60.00'), user=self.user)
        # balance remaining = 40.00
        from job_tickets.views.helpers import _generate_inventory_bill_number, _generate_inventory_entry_number
        from django.utils import timezone
        entry_date = timezone.localdate()
        self.return_bill = InventoryBill.objects.create(
            workspace=self.ws,
            bill_number=_generate_inventory_bill_number('purchase_return', entry_date, self.ws),
            entry_type='purchase_return',
            entry_date=entry_date,
            invoice_number='PRN-APY-001',
            party=self.party,
            created_by=self.user,
        )
        InventoryEntry.objects.create(
            workspace=self.ws, bill=self.return_bill,
            entry_number=_generate_inventory_entry_number('purchase_return', entry_date, self.ws),
            entry_type='purchase_return', entry_date=entry_date,
            invoice_number='PRN-APY-001', party=self.party, product=self.product,
            quantity=3, unit_price=Decimal('10.00'), discount_amount=Decimal('0.00'),
            gst_rate=Decimal('0.00'), taxable_amount=Decimal('30.00'),
            gst_amount=Decimal('0.00'), total_amount=Decimal('30.00'),
            stock_before=110, stock_after=107, created_by=self.user,
        )

    def test_effective_balance_after_partial_payment(self):
        balance = _inventory_bill_effective_balance(self.purchase_bill)
        self.assertEqual(balance, Decimal('40.00'))

    def test_credit_can_be_applied_to_remaining_balance(self):
        _, vc = _link_return_to_source_bill(
            self.return_bill.pk, self.purchase_bill.pk, self.user, workspace=self.ws
        )
        app = _apply_vendor_credit_to_bill(
            vc.pk, self.purchase_bill.pk, Decimal('30.00'), self.user, workspace=self.ws
        )
        self.assertEqual(app.amount, Decimal('30.00'))
        # Effective balance should now be 40.00 - 30.00 = 10.00
        eff = _inventory_bill_effective_balance(self.purchase_bill)
        self.assertEqual(eff, Decimal('10.00'))


class ReturnAfterFullVendorPaymentTests(TestCase):
    """Return after full vendor payment: credit is an open positive balance (not silently negative)."""

    def setUp(self):
        self.ws, self.user = _make_workspace_and_user('ret-after-full')
        self.party = _make_party(self.ws)
        self.product = _make_product(self.ws, stock=100)
        self.purchase_bill = _make_purchase_bill(
            self.ws, self.party, self.product,
            quantity=10, unit_price='10.00', gst_rate='0.00', user=self.user
        )  # total = 100.00
        _pay_bill_fully(self.purchase_bill, user=self.user)

    def test_effective_balance_after_full_payment_is_zero(self):
        balance = _inventory_bill_effective_balance(self.purchase_bill)
        self.assertEqual(balance, Decimal('0.00'))

    def test_vendor_credit_created_with_positive_balance(self):
        from job_tickets.views.helpers import _generate_inventory_bill_number, _generate_inventory_entry_number
        from django.utils import timezone
        entry_date = timezone.localdate()
        return_bill = InventoryBill.objects.create(
            workspace=self.ws,
            bill_number=_generate_inventory_bill_number('purchase_return', entry_date, self.ws),
            entry_type='purchase_return',
            entry_date=entry_date,
            invoice_number='PRN-FULL-001',
            party=self.party,
            created_by=self.user,
        )
        InventoryEntry.objects.create(
            workspace=self.ws, bill=return_bill,
            entry_number=_generate_inventory_entry_number('purchase_return', entry_date, self.ws),
            entry_type='purchase_return', entry_date=entry_date,
            invoice_number='PRN-FULL-001', party=self.party, product=self.product,
            quantity=5, unit_price=Decimal('10.00'), discount_amount=Decimal('0.00'),
            gst_rate=Decimal('0.00'), taxable_amount=Decimal('50.00'),
            gst_amount=Decimal('0.00'), total_amount=Decimal('50.00'),
            stock_before=110, stock_after=105, created_by=self.user,
        )
        _, vc = _link_return_to_source_bill(
            return_bill.pk, self.purchase_bill.pk, self.user, workspace=self.ws
        )
        # Credit should be open and positive — NOT made negative or silently zeroed
        self.assertEqual(vc.credit_amount, Decimal('50.00'))
        self.assertEqual(vc.balance_amount, Decimal('50.00'))
        self.assertEqual(vc.status, InventoryVendorCredit.STATUS_OPEN)

    def test_vendor_credit_cannot_be_applied_to_fully_paid_bill(self):
        """Applying credit to a bill with zero balance should raise."""
        from job_tickets.views.helpers import _generate_inventory_bill_number, _generate_inventory_entry_number
        from django.utils import timezone
        entry_date = timezone.localdate()
        return_bill = InventoryBill.objects.create(
            workspace=self.ws,
            bill_number=_generate_inventory_bill_number('purchase_return', entry_date, self.ws),
            entry_type='purchase_return',
            entry_date=entry_date,
            invoice_number='PRN-FULL-002',
            party=self.party,
            created_by=self.user,
        )
        InventoryEntry.objects.create(
            workspace=self.ws, bill=return_bill,
            entry_number=_generate_inventory_entry_number('purchase_return', entry_date, self.ws),
            entry_type='purchase_return', entry_date=entry_date,
            invoice_number='PRN-FULL-002', party=self.party, product=self.product,
            quantity=5, unit_price=Decimal('10.00'), discount_amount=Decimal('0.00'),
            gst_rate=Decimal('0.00'), taxable_amount=Decimal('50.00'),
            gst_amount=Decimal('0.00'), total_amount=Decimal('50.00'),
            stock_before=110, stock_after=105, created_by=self.user,
        )
        _, vc = _link_return_to_source_bill(
            return_bill.pk, self.purchase_bill.pk, self.user, workspace=self.ws
        )
        with self.assertRaises(ValueError) as ctx:
            _apply_vendor_credit_to_bill(
                vc.pk, self.purchase_bill.pk, Decimal('10.00'), self.user, workspace=self.ws
            )
        self.assertIn('balance', str(ctx.exception).lower())


# ---------------------------------------------------------------------------
# Phase 7: Vendor Credit Application
# ---------------------------------------------------------------------------

class VendorCreditApplicationTests(TestCase):
    """Core vendor credit application scenarios."""

    def setUp(self):
        self.ws, self.user = _make_workspace_and_user('vc-app')
        self.party = _make_party(self.ws)
        self.product = _make_product(self.ws, stock=200)
        self.purchase_bill = _make_purchase_bill(
            self.ws, self.party, self.product,
            quantity=10, unit_price='10.00', user=self.user
        )  # total=100
        # Create a second purchase bill to apply credit to
        self.purchase_bill_2 = _make_purchase_bill(
            self.ws, self.party, self.product,
            quantity=5, unit_price='10.00', user=self.user
        )  # total=50
        # Create return and vendor credit
        from job_tickets.views.helpers import _generate_inventory_bill_number, _generate_inventory_entry_number
        from django.utils import timezone
        entry_date = timezone.localdate()
        return_bill = InventoryBill.objects.create(
            workspace=self.ws,
            bill_number=_generate_inventory_bill_number('purchase_return', entry_date, self.ws),
            entry_type='purchase_return',
            entry_date=entry_date,
            invoice_number='PRN-VC-APP-001',
            party=self.party,
            created_by=self.user,
        )
        InventoryEntry.objects.create(
            workspace=self.ws, bill=return_bill,
            entry_number=_generate_inventory_entry_number('purchase_return', entry_date, self.ws),
            entry_type='purchase_return', entry_date=entry_date,
            invoice_number='PRN-VC-APP-001', party=self.party, product=self.product,
            quantity=8, unit_price=Decimal('10.00'), discount_amount=Decimal('0.00'),
            gst_rate=Decimal('0.00'), taxable_amount=Decimal('80.00'),
            gst_amount=Decimal('0.00'), total_amount=Decimal('80.00'),
            stock_before=210, stock_after=202, created_by=self.user,
        )
        _, self.vc = _link_return_to_source_bill(
            return_bill.pk, self.purchase_bill.pk, self.user, workspace=self.ws
        )  # credit=80.00

    def test_credit_balance_initial(self):
        self.assertEqual(self.vc.balance_amount, Decimal('80.00'))

    def test_partial_application_reduces_credit_balance(self):
        _apply_vendor_credit_to_bill(
            self.vc.pk, self.purchase_bill_2.pk, Decimal('30.00'), self.user, workspace=self.ws
        )
        self.vc.refresh_from_db()
        self.assertEqual(self.vc.balance_amount, Decimal('50.00'))

    def test_partial_application_sets_status_partially_applied(self):
        _apply_vendor_credit_to_bill(
            self.vc.pk, self.purchase_bill_2.pk, Decimal('30.00'), self.user, workspace=self.ws
        )
        self.vc.refresh_from_db()
        self.assertEqual(self.vc.status, InventoryVendorCredit.STATUS_PARTIALLY_APPLIED)

    def test_full_application_sets_status_fully_applied(self):
        _apply_vendor_credit_to_bill(
            self.vc.pk, self.purchase_bill_2.pk, Decimal('50.00'), self.user, workspace=self.ws
        )
        # Remaining balance: 80-50=30; apply remaining 30 to first bill
        _apply_vendor_credit_to_bill(
            self.vc.pk, self.purchase_bill.pk, Decimal('30.00'), self.user, workspace=self.ws
        )
        self.vc.refresh_from_db()
        self.assertEqual(self.vc.status, InventoryVendorCredit.STATUS_FULLY_APPLIED)

    def test_application_creates_application_record(self):
        _apply_vendor_credit_to_bill(
            self.vc.pk, self.purchase_bill_2.pk, Decimal('20.00'), self.user, workspace=self.ws
        )
        self.assertTrue(
            InventoryVendorCreditApplication.objects.filter(
                vendor_credit=self.vc,
                target_bill=self.purchase_bill_2,
            ).exists()
        )

    def test_exceeding_credit_balance_raises(self):
        with self.assertRaises(ValueError) as ctx:
            _apply_vendor_credit_to_bill(
                self.vc.pk, self.purchase_bill_2.pk, Decimal('100.00'), self.user, workspace=self.ws
            )
        self.assertIn('credit balance', str(ctx.exception))

    def test_exceeding_bill_balance_raises(self):
        # purchase_bill_2 total = 50; try to apply 60
        with self.assertRaises(ValueError) as ctx:
            _apply_vendor_credit_to_bill(
                self.vc.pk, self.purchase_bill_2.pk, Decimal('60.00'), self.user, workspace=self.ws
            )
        self.assertIn('balance', str(ctx.exception))

    def test_cross_party_application_raises(self):
        party_b = _make_party(self.ws, 'Party B')
        product_b = _make_product(self.ws, name='Widget B', stock=100)
        other_bill = _make_purchase_bill(self.ws, party_b, product_b, user=self.user)
        with self.assertRaises(ValueError) as ctx:
            _apply_vendor_credit_to_bill(
                self.vc.pk, other_bill.pk, Decimal('10.00'), self.user, workspace=self.ws
            )
        self.assertIn('same party', str(ctx.exception))

    def test_applying_to_reversed_bill_raises(self):
        reversal_bill, _ = _reverse_inventory_bill(
            self.purchase_bill_2.pk, self.user, 'Reverse for test', workspace=self.ws
        )
        self.purchase_bill_2.refresh_from_db()
        with self.assertRaises(ValueError) as ctx:
            _apply_vendor_credit_to_bill(
                self.vc.pk, self.purchase_bill_2.pk, Decimal('10.00'), self.user, workspace=self.ws
            )
        self.assertIn('reversed', str(ctx.exception))

    def test_audit_log_written_on_application(self):
        _apply_vendor_credit_to_bill(
            self.vc.pk, self.purchase_bill_2.pk, Decimal('20.00'), self.user, workspace=self.ws
        )
        self.assertTrue(
            InventoryBillLog.objects.filter(
                bill=self.purchase_bill_2,
                action=InventoryBillLog.ACTION_VENDOR_CREDIT_APPLIED,
            ).exists()
        )


class VendorCreditStatusTransitionTests(TestCase):
    """Vendor credit status must track correctly through applications."""

    def setUp(self):
        self.ws, self.user = _make_workspace_and_user('vc-status')
        self.party = _make_party(self.ws)
        self.product = _make_product(self.ws, stock=200)
        from job_tickets.views.helpers import _generate_inventory_bill_number, _generate_inventory_entry_number
        from django.utils import timezone
        entry_date = timezone.localdate()
        return_bill = InventoryBill.objects.create(
            workspace=self.ws,
            bill_number=_generate_inventory_bill_number('purchase_return', entry_date, self.ws),
            entry_type='purchase_return',
            entry_date=entry_date,
            invoice_number='PRN-STATUS-001',
            party=self.party,
            created_by=self.user,
        )
        self.target = _make_purchase_bill(self.ws, self.party, self.product, quantity=10, unit_price='10.00', user=self.user)
        InventoryEntry.objects.create(
            workspace=self.ws, bill=return_bill,
            entry_number=_generate_inventory_entry_number('purchase_return', entry_date, self.ws),
            entry_type='purchase_return', entry_date=entry_date,
            invoice_number='PRN-STATUS-001', party=self.party, product=self.product,
            quantity=5, unit_price=Decimal('10.00'), discount_amount=Decimal('0.00'),
            gst_rate=Decimal('0.00'), taxable_amount=Decimal('50.00'),
            gst_amount=Decimal('0.00'), total_amount=Decimal('50.00'),
            stock_before=210, stock_after=205, created_by=self.user,
        )
        _, self.vc = _link_return_to_source_bill(
            return_bill.pk, self.target.pk, self.user, workspace=self.ws
        )  # credit=50.00

    def test_initial_status_is_open(self):
        self.assertEqual(self.vc.status, InventoryVendorCredit.STATUS_OPEN)

    def test_partial_application_moves_to_partially_applied(self):
        _apply_vendor_credit_to_bill(self.vc.pk, self.target.pk, Decimal('20.00'), self.user, workspace=self.ws)
        self.vc.refresh_from_db()
        self.assertEqual(self.vc.status, InventoryVendorCredit.STATUS_PARTIALLY_APPLIED)

    def test_full_application_moves_to_fully_applied(self):
        _apply_vendor_credit_to_bill(self.vc.pk, self.target.pk, Decimal('50.00'), self.user, workspace=self.ws)
        self.vc.refresh_from_db()
        self.assertEqual(self.vc.status, InventoryVendorCredit.STATUS_FULLY_APPLIED)

    def test_fully_applied_credit_cannot_be_applied_again(self):
        _apply_vendor_credit_to_bill(self.vc.pk, self.target.pk, Decimal('50.00'), self.user, workspace=self.ws)
        target2 = _make_purchase_bill(self.ws, self.party, self.product, quantity=5, unit_price='10.00', user=self.user)
        with self.assertRaises(ValueError) as ctx:
            _apply_vendor_credit_to_bill(self.vc.pk, target2.pk, Decimal('10.00'), self.user, workspace=self.ws)
        self.assertIn('fully_applied', str(ctx.exception))


# ---------------------------------------------------------------------------
# Phase 8: Idempotency
# ---------------------------------------------------------------------------

class DuplicateBillSubmissionDetectionTests(TestCase):
    """_check_duplicate_bill_submission returns existing bill on same (type, invoice, party, workspace)."""

    def setUp(self):
        self.ws, self.user = _make_workspace_and_user('idem-det')
        self.party = _make_party(self.ws)
        self.product = _make_product(self.ws, stock=100)
        self.bill = _make_purchase_bill(self.ws, self.party, self.product, user=self.user)

    def test_same_invoice_same_party_same_workspace_detected(self):
        existing = _check_duplicate_bill_submission(
            entry_type='purchase',
            invoice_number=self.bill.invoice_number,
            party_id=self.party.pk,
            workspace=self.ws,
        )
        self.assertIsNotNone(existing)
        self.assertEqual(existing.pk, self.bill.pk)

    def test_different_invoice_not_detected(self):
        existing = _check_duplicate_bill_submission(
            entry_type='purchase',
            invoice_number='TOTALLY-DIFFERENT-INV',
            party_id=self.party.pk,
            workspace=self.ws,
        )
        self.assertIsNone(existing)

    def test_different_workspace_not_detected(self):
        ws2, _ = _make_workspace_and_user('idem-ws2')
        existing = _check_duplicate_bill_submission(
            entry_type='purchase',
            invoice_number=self.bill.invoice_number,
            party_id=self.party.pk,
            workspace=ws2,
        )
        self.assertIsNone(existing)

    def test_sale_type_always_returns_none(self):
        """Sale bills use auto-generated invoice numbers; the duplicate guard is skipped."""
        existing = _check_duplicate_bill_submission(
            entry_type='sale',
            invoice_number=self.bill.invoice_number,
            party_id=self.party.pk,
            workspace=self.ws,
        )
        self.assertIsNone(existing)

    def test_none_invoice_returns_none(self):
        existing = _check_duplicate_bill_submission(
            entry_type='purchase',
            invoice_number=None,
            party_id=self.party.pk,
            workspace=self.ws,
        )
        self.assertIsNone(existing)


class LegitimateRepeatSaleTests(TestCase):
    """Two different sales to the same party are not treated as duplicates."""

    def setUp(self):
        self.ws, self.user = _make_workspace_and_user('idem-sale')
        self.party = _make_party(self.ws)
        self.product = _make_product(self.ws, stock=100)

    def test_two_sales_same_party_different_invoices(self):
        bill1 = _make_sale_bill(self.ws, self.party, self.product, quantity=1, user=self.user)
        bill2 = _make_sale_bill(self.ws, self.party, self.product, quantity=2, user=self.user)
        self.assertNotEqual(bill1.pk, bill2.pk)
        self.assertNotEqual(bill1.invoice_number, bill2.invoice_number)


# ---------------------------------------------------------------------------
# Phase 9: Audit Trail
# ---------------------------------------------------------------------------

class InventoryBillLogWriteTests(TestCase):
    """_write_bill_log creates a record and does not raise on error."""

    def setUp(self):
        self.ws, self.user = _make_workspace_and_user('audit-log')
        self.party = _make_party(self.ws)
        self.product = _make_product(self.ws, stock=100)
        self.bill = _make_purchase_bill(self.ws, self.party, self.product, user=self.user)

    def test_write_creates_log_row(self):
        _write_bill_log(
            self.bill,
            InventoryBillLog.ACTION_BILL_CREATED,
            user=self.user,
            details='Created in test.',
        )
        self.assertEqual(
            InventoryBillLog.objects.filter(
                bill=self.bill,
                action=InventoryBillLog.ACTION_BILL_CREATED,
            ).count(),
            1,
        )

    def test_log_stores_user(self):
        _write_bill_log(self.bill, InventoryBillLog.ACTION_BILL_CREATED, user=self.user)
        log = InventoryBillLog.objects.get(bill=self.bill, action=InventoryBillLog.ACTION_BILL_CREATED)
        self.assertEqual(log.user, self.user)

    def test_log_stores_details(self):
        _write_bill_log(self.bill, InventoryBillLog.ACTION_PAYMENT_RECORDED, details='Rs.50 cash received.')
        log = InventoryBillLog.objects.get(bill=self.bill, action=InventoryBillLog.ACTION_PAYMENT_RECORDED)
        self.assertIn('Rs.50', log.details)

    def test_log_does_not_raise_on_invalid_input(self):
        """Audit failure must never abort a business transaction."""
        try:
            _write_bill_log(self.bill, 'INVALID_ACTION', details='Should not raise.')
        except Exception as exc:
            self.fail(f"_write_bill_log raised unexpectedly: {exc}")

    def test_reversal_produces_two_log_rows(self):
        _reverse_inventory_bill(self.bill.pk, self.user, 'Audit test', workspace=self.ws)
        reversal_bill = InventoryBill.objects.filter(reversal_of=self.bill).first()
        self.assertTrue(
            InventoryBillLog.objects.filter(
                bill=self.bill, action=InventoryBillLog.ACTION_BILL_REVERSED
            ).exists()
        )
        self.assertTrue(
            InventoryBillLog.objects.filter(
                bill=reversal_bill, action=InventoryBillLog.ACTION_REVERSAL_CREATED
            ).exists()
        )


class AuditLogLinksRelatedBillTests(TestCase):
    """related_bill FK on log rows correctly identifies counterpart bill."""

    def setUp(self):
        self.ws, self.user = _make_workspace_and_user('audit-rel')
        self.party = _make_party(self.ws)
        self.product = _make_product(self.ws, stock=100)
        self.bill = _make_purchase_bill(self.ws, self.party, self.product, user=self.user)

    def test_reversal_log_related_bill_is_reversal_bill(self):
        reversal_bill, _ = _reverse_inventory_bill(self.bill.pk, self.user, 'Rel test', workspace=self.ws)
        log = InventoryBillLog.objects.get(bill=self.bill, action=InventoryBillLog.ACTION_BILL_REVERSED)
        self.assertEqual(log.related_bill_id, reversal_bill.pk)

    def test_reversal_log_related_bill_on_reversal_is_original(self):
        reversal_bill, _ = _reverse_inventory_bill(self.bill.pk, self.user, 'Rel test', workspace=self.ws)
        log = InventoryBillLog.objects.get(bill=reversal_bill, action=InventoryBillLog.ACTION_REVERSAL_CREATED)
        self.assertEqual(log.related_bill_id, self.bill.pk)


# ---------------------------------------------------------------------------
# Phase 10: PostgreSQL concurrency (environment-gated)
# ---------------------------------------------------------------------------

@skipUnless(connection.vendor == 'postgresql', 'PostgreSQL concurrency test only')
class PostgreSQLReversalConcurrencyTests(TransactionTestCase):
    """
    Concurrent reversal attempts on the same bill must not both succeed.
    """

    def setUp(self):
        self.ws, self.user = _make_workspace_and_user('pg-rev')
        self.party = _make_party(self.ws)
        self.product = _make_product(self.ws, stock=100)
        self.bill = _make_purchase_bill(self.ws, self.party, self.product, quantity=10, user=self.user)

    def test_concurrent_reversal_only_one_succeeds(self):
        results = []
        errors = []

        def attempt_reversal():
            from django.db import close_old_connections
            close_old_connections()
            try:
                reversal_bill, _ = _reverse_inventory_bill(
                    self.bill.pk, self.user, 'Concurrent attempt', workspace=self.ws
                )
                results.append(reversal_bill.pk)
            except ValueError as exc:
                errors.append(str(exc))
            finally:
                connection.close()

        from concurrent.futures import ThreadPoolExecutor
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(attempt_reversal) for _ in range(2)]
            for f in futures:
                f.result()

        connection.close()
        close_old_connections()

        # Exactly one should succeed, one should fail
        self.assertEqual(len(results), 1, f"Expected 1 success, got {len(results)}: {results}")
        self.assertEqual(len(errors), 1, f"Expected 1 error, got {len(errors)}: {errors}")

        # Bill should be reversed exactly once
        self.bill.refresh_from_db()
        self.assertTrue(self.bill.is_reversed)
        reversal_count = InventoryBill.objects.filter(reversal_of=self.bill).count()
        self.assertEqual(reversal_count, 1)


@skipUnless(connection.vendor == 'postgresql', 'PostgreSQL concurrency test only')
class PostgreSQLVendorCreditConcurrencyTests(TransactionTestCase):
    """
    Concurrent applications of the same vendor credit must not over-apply.
    Requires PostgreSQL (SELECT FOR UPDATE row locking).
    """

    def setUp(self):
        self.ws, self.user = _make_workspace_and_user('pg-vc')
        self.party = _make_party(self.ws)
        self.product = _make_product(self.ws, stock=200)
        from job_tickets.views.helpers import _generate_inventory_bill_number, _generate_inventory_entry_number
        from django.utils import timezone
        entry_date = timezone.localdate()
        self.target1 = _make_purchase_bill(self.ws, self.party, self.product, quantity=5, unit_price='10.00', user=self.user)
        self.target2 = _make_purchase_bill(self.ws, self.party, self.product, quantity=5, unit_price='10.00', user=self.user)
        return_bill = InventoryBill.objects.create(
            workspace=self.ws,
            bill_number=_generate_inventory_bill_number('purchase_return', entry_date, self.ws),
            entry_type='purchase_return',
            entry_date=entry_date,
            invoice_number='PRN-PG-001',
            party=self.party,
            created_by=self.user,
        )
        InventoryEntry.objects.create(
            workspace=self.ws, bill=return_bill,
            entry_number=_generate_inventory_entry_number('purchase_return', entry_date, self.ws),
            entry_type='purchase_return', entry_date=entry_date,
            invoice_number='PRN-PG-001', party=self.party, product=self.product,
            quantity=5, unit_price=Decimal('10.00'), discount_amount=Decimal('0.00'),
            gst_rate=Decimal('0.00'), taxable_amount=Decimal('50.00'),
            gst_amount=Decimal('0.00'), total_amount=Decimal('50.00'),
            stock_before=210, stock_after=205, created_by=self.user,
        )
        _, self.vc = _link_return_to_source_bill(
            return_bill.pk, self.target1.pk, self.user, workspace=self.ws
        )  # credit=50.00

    def test_concurrent_applications_do_not_over_apply(self):
        successes = []
        errors = []

        def apply(target_pk, amount):
            from django.db import close_old_connections
            close_old_connections()
            try:
                app = _apply_vendor_credit_to_bill(
                    self.vc.pk, target_pk, amount, self.user, workspace=self.ws
                )
                successes.append(app.amount)
            except ValueError as exc:
                errors.append(str(exc))
            finally:
                connection.close()

        from concurrent.futures import ThreadPoolExecutor
        # Both try to apply 40.00 from a 50.00 credit — only one should fit both within credit and bill balance
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [
                pool.submit(apply, self.target1.pk, Decimal('40.00')),
                pool.submit(apply, self.target2.pk, Decimal('40.00')),
            ]
            for f in futures:
                f.result()

        connection.close()
        close_old_connections()

        self.vc.refresh_from_db()
        # Total applied must not exceed credit amount (50.00)
        total_applied = sum(successes)
        self.assertLessEqual(
            total_applied, Decimal('50.00'),
            f"Over-applied: total {total_applied} > credit {self.vc.credit_amount}",
        )


# ---------------------------------------------------------------------------
# Phase 9: Real Purchase Duplicate Integration & Concurrency Tests
# ---------------------------------------------------------------------------

class RealPurchaseDuplicateIntegrationTests(TestCase):
    """Verify duplicate bill rejection directly within _record_inventory_entries."""

    def setUp(self):
        from django.test import RequestFactory
        self.factory = RequestFactory()
        self.ws, self.user = _make_workspace_and_user('pur-idem-user')
        self.party = _make_party(self.ws, name='Unique Supplier')
        self.product = _make_product(self.ws, name='Test Part', stock=50)

    def _build_request(self, ws=None, user=None):
        request = self.factory.post('/')
        request.user = user or self.user
        request.current_workspace = ws or self.ws
        return request

    def test_duplicate_purchase_submission_rejected(self):
        entry_date = timezone.localdate()
        req = self._build_request()
        lines = [{
            'product': self.product,
            'quantity': 2,
            'unit_price': Decimal('50.00'),
            'discount_amount': Decimal('0.00'),
            'gst_rate': Decimal('0.00'),
            'notes': 'First purchase',
        }]

        entries = _record_inventory_entries(
            request=req,
            entry_type='purchase',
            entry_date=entry_date,
            party=self.party,
            invoice_number='INV-REAL-101',
            line_items=lines,
        )
        self.assertTrue(len(entries) > 0)

        # Attempting identical purchase submission
        with self.assertRaises(ValueError) as ctx:
            _record_inventory_entries(
                request=req,
                entry_type='purchase',
                entry_date=entry_date,
                party=self.party,
                invoice_number='INV-REAL-101',
                line_items=lines,
            )
        self.assertIn('Duplicate bill detected', str(ctx.exception))

    def test_different_reference_purchase_accepted(self):
        entry_date = timezone.localdate()
        req = self._build_request()
        lines = [{
            'product': self.product,
            'quantity': 1,
            'unit_price': Decimal('50.00'),
            'discount_amount': Decimal('0.00'),
            'gst_rate': Decimal('0.00'),
            'notes': 'Separate bill',
        }]

        e1 = _record_inventory_entries(
            request=req,
            entry_type='purchase',
            entry_date=entry_date,
            party=self.party,
            invoice_number='INV-SEP-001',
            line_items=lines,
        )
        e2 = _record_inventory_entries(
            request=req,
            entry_type='purchase',
            entry_date=entry_date,
            party=self.party,
            invoice_number='INV-SEP-002',
            line_items=lines,
        )
        self.assertNotEqual(e1[0].bill_id, e2[0].bill_id)

    def test_cross_workspace_duplicate_reference_accepted(self):
        ws2, user2 = _make_workspace_and_user('pur-idem-user2')
        party2 = _make_party(ws2, name='Supplier WS2')
        product2 = _make_product(ws2, name='Part WS2', stock=50)

        entry_date = timezone.localdate()
        req1 = self._build_request(self.ws, self.user)
        req2 = self._build_request(ws2, user2)

        lines1 = [{'product': self.product, 'quantity': 1, 'unit_price': Decimal('50.00'), 'discount_amount': Decimal('0.00'), 'gst_rate': Decimal('0.00'), 'notes': ''}]
        lines2 = [{'product': product2, 'quantity': 1, 'unit_price': Decimal('50.00'), 'discount_amount': Decimal('0.00'), 'gst_rate': Decimal('0.00'), 'notes': ''}]

        e1 = _record_inventory_entries(req1, 'purchase', entry_date, self.party, 'SHARED-INV-99', lines1)
        e2 = _record_inventory_entries(req2, 'purchase', entry_date, party2, 'SHARED-INV-99', lines2)
        self.assertNotEqual(e1[0].bill_id, e2[0].bill_id)
        self.assertNotEqual(e1[0].bill.workspace_id, e2[0].bill.workspace_id)


class PostgreSQLPurchaseDuplicateConcurrencyTests(TransactionTestCase):
    """Concurrent duplicate purchase submissions are cleanly serialized and rejected."""

    def setUp(self):
        from django.test import RequestFactory
        self.factory = RequestFactory()
        self.ws, self.user = _make_workspace_and_user('concur-pur')
        self.party = _make_party(self.ws, name='Concur Supplier')
        self.product = _make_product(self.ws, name='Concur Widget', stock=200)

    def test_concurrent_duplicate_purchase_submissions(self):
        from concurrent.futures import ThreadPoolExecutor
        from django.db import connection, close_old_connections

        successes = []
        duplicates = []
        errors = []

        def submit_purchase():
            close_old_connections()
            req = self.factory.post('/')
            req.user = self.user
            req.current_workspace = self.ws
            lines = [{
                'product': self.product,
                'quantity': 2,
                'unit_price': Decimal('30.00'),
                'discount_amount': Decimal('0.00'),
                'gst_rate': Decimal('0.00'),
                'notes': 'Concurrent entry',
            }]
            try:
                entries = _record_inventory_entries(
                    request=req,
                    entry_type='purchase',
                    entry_date=timezone.localdate(),
                    party=self.party,
                    invoice_number='CONCUR-INV-DUPLICATE',
                    line_items=lines,
                )
                successes.append(entries[0].bill_id)
            except ValueError as exc:
                if 'Duplicate bill detected' in str(exc):
                    duplicates.append(str(exc))
                else:
                    errors.append(str(exc))
            except Exception as e:
                errors.append(str(e))
            finally:
                connection.close()

        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(submit_purchase), pool.submit(submit_purchase)]
            for f in futures:
                f.result()

        connection.close()
        close_old_connections()

        self.assertEqual(len(errors), 0, f"Unexpected errors: {errors}")
        self.assertEqual(len(successes), 1, "Exactly one thread should have succeeded creating the purchase bill.")
        self.assertEqual(len(duplicates), 1, "Exactly one thread should have been blocked by duplicate detection.")


class OverTheCounterPartialPaymentTests(TestCase):
    """Test partial payment on direct inventory bill creation."""

    def setUp(self):
        from django.test import RequestFactory
        self.factory = RequestFactory()
        self.ws, self.user = _make_workspace_and_user('otc-user')
        self.party = _make_party(self.ws, name='Walk-in Customer')
        self.product = _make_product(self.ws, name='Screen Protector', stock=50, unit_price='200.00')

    def test_partial_payment_creates_credit_payment_and_computes_balance(self):
        req = self.factory.post('/')
        req.user = self.user
        req.current_workspace = self.ws
        lines = [{
            'product': self.product,
            'quantity': 1,
            'unit_price': Decimal('200.00'),
            'discount_amount': Decimal('0.00'),
            'gst_rate': Decimal('0.00'),
            'notes': 'Sale with partial payment',
        }]
        initial_pay = {
            'status': 'part_paid',
            'payment_method': 'upi',
            'payment_date': timezone.localdate(),
            'amount_paid': Decimal('120.00'),
            'reference_no': 'UPI-OTC-789',
        }
        entries = _record_inventory_entries(
            request=req,
            entry_type='sale',
            entry_date=timezone.localdate(),
            party=self.party,
            invoice_number='',
            line_items=lines,
            initial_payment=initial_pay,
        )
        bill = entries[0].bill
        payments = InventoryCreditPayment.objects.filter(bill=bill)
        self.assertEqual(payments.count(), 1)
        p = payments.first()
        self.assertEqual(p.amount, Decimal('120.00'))
        self.assertEqual(p.balance_before, Decimal('200.00'))
        self.assertEqual(p.balance_after, Decimal('80.00'))
        self.assertEqual(p.payment_method, 'upi')
        self.assertEqual(p.reference_no, 'UPI-OTC-789')
        self.assertEqual(p.direction, InventoryCreditPayment.DIRECTION_RECEIVABLE)
        self.assertEqual(_inventory_bill_paid_total(bill), Decimal('120.00'))


class JobSettlementAndCreditLifecycleTests(TestCase):
    """Test full payment, partial payment, and credit handling on job closure."""

    def setUp(self):
        self.ws, self.user = _make_workspace_and_user('job-settle-user')
        self.client_obj = self.client
        self.client_obj.force_login(self.user)

        self.tech_user = User.objects.create_user(username='tech1', password='password123')
        from django.contrib.auth.models import Group
        tech_group, _ = Group.objects.get_or_create(name='Technicians')
        self.tech_user.groups.add(tech_group)
        self.tech_profile = TechnicianProfile.objects.create(
            workspace=self.ws,
            user=self.tech_user,
            unique_id='TECH001',
        )

        self.job = JobTicket.objects.create(
            workspace=self.ws,
            job_code='JOB-SETTLE-001',
            customer_name='John Customer',
            customer_phone='9876543210',
            device_type='Laptop',
            reported_issue='Won\'t boot',
            status='Ready for Pickup',
            discount_amount=Decimal('50.00'),
            created_by=self.user,
        )
        ServiceLog.objects.create(
            job_ticket=self.job,
            description='Power IC replacement',
            part_cost=Decimal('300.00'),
            service_charge=Decimal('250.00'),
        )
        # Total cost = (300 + 250) - 50 = 500.00

    def test_job_settlement_full_payment(self):
        url = reverse('close_job', args=[self.job.job_code])
        response = self.client_obj.post(url, {
            'payment_status': 'paid',
            'payment_method': 'upi',
            'payment_reference': 'UPI-REF-999',
        })
        self.assertEqual(response.status_code, 302)
        self.job.refresh_from_db()
        self.assertEqual(self.job.status, 'Closed')
        self.assertEqual(self.job.payment_status, 'paid')
        self.assertEqual(self.job.payment_method, 'upi')
        self.assertEqual(self.job.amount_paid, Decimal('500.00'))
        self.assertEqual(self.job.balance_due, Decimal('0.00'))
        self.assertEqual(self.job.payment_reference, 'UPI-REF-999')
        self.assertIsNotNone(self.job.payment_date)

        # Verify receivable credit payment
        payments = InventoryCreditPayment.objects.filter(party__phone=self.job.customer_phone)
        self.assertEqual(payments.count(), 1)
        self.assertEqual(payments.first().amount, Decimal('500.00'))
        self.assertEqual(payments.first().balance_after, Decimal('0.00'))

    def test_job_settlement_partial_payment(self):
        url = reverse('close_job', args=[self.job.job_code])
        response = self.client_obj.post(url, {
            'payment_status': 'part_paid',
            'payment_method': 'cash',
            'amount_paid': '300.00',
            'payment_reference': 'CASH-PART-1',
        })
        self.assertEqual(response.status_code, 302)
        self.job.refresh_from_db()
        self.assertEqual(self.job.status, 'Closed')
        self.assertEqual(self.job.payment_status, 'part_paid')
        self.assertEqual(self.job.amount_paid, Decimal('300.00'))
        self.assertEqual(self.job.balance_due, Decimal('200.00'))

        payments = InventoryCreditPayment.objects.filter(party__phone=self.job.customer_phone)
        self.assertEqual(payments.count(), 1)
        self.assertEqual(payments.first().amount, Decimal('300.00'))
        self.assertEqual(payments.first().balance_after, Decimal('200.00'))

    def test_job_settlement_credit_unpaid(self):
        url = reverse('close_job', args=[self.job.job_code])
        response = self.client_obj.post(url, {
            'payment_status': 'unpaid',
        })
        self.assertEqual(response.status_code, 302)
        self.job.refresh_from_db()
        self.assertEqual(self.job.status, 'Closed')
        self.assertEqual(self.job.payment_status, 'unpaid')
        self.assertEqual(self.job.amount_paid, Decimal('0.00'))
        self.assertEqual(self.job.balance_due, Decimal('500.00'))

        payments = InventoryCreditPayment.objects.filter(party__phone=self.job.customer_phone)
        self.assertEqual(payments.count(), 0)


class TechnicianAssignmentAndCompletionTests(TestCase):
    """Test technician assignment and job completion with labor charges and notes."""

    def setUp(self):
        self.ws, self.staff_user = _make_workspace_and_user('staff-assign-user')
        self.client_obj = self.client
        self.client_obj.force_login(self.staff_user)

        self.tech_user = User.objects.create_user(username='technician_joe', password='password123')
        from django.contrib.auth.models import Group
        tech_group, _ = Group.objects.get_or_create(name='Technicians')
        self.tech_user.groups.add(tech_group)
        self.tech_profile = TechnicianProfile.objects.create(
            workspace=self.ws,
            user=self.tech_user,
            unique_id='TECH002',
        )

        self.job = JobTicket.objects.create(
            workspace=self.ws,
            job_code='JOB-TECH-001',
            customer_name='Alice Smith',
            customer_phone='9123456780',
            device_type='Laptop',
            reported_issue='Blue screen of death',
            status='Pending',
            requires_laptop_inspection_checklist=False,
            created_by=self.staff_user,
        )

    def test_staff_assigns_technician(self):
        url = reverse('staff_dashboard')
        response = self.client_obj.post(url, {
            'assign_job_form_submit': '1',
            'job_code': self.job.job_code,
            'technician': self.tech_profile.pk,
        })
        self.assertEqual(response.status_code, 302)
        self.job.refresh_from_db()
        self.assertEqual(self.job.assigned_to, self.tech_profile)
        self.assertEqual(self.job.status, 'Under Inspection')
        self.assertTrue(self.job.is_new_assignment)

    def test_technician_marks_completed_with_labor_charge_and_note(self):
        self.job.assigned_to = self.tech_profile
        self.job.status = 'Repairing'
        self.job.save()

        # Login as technician
        self.client_obj.force_login(self.tech_user)
        url = reverse('job_mark_completed', args=[self.job.job_code])
        response = self.client_obj.post(url, {
            'labor_charge': '450.00',
            'completion_note': 'Replaced thermal paste and RAM module',
        })
        self.assertEqual(response.status_code, 302)
        self.job.refresh_from_db()
        self.assertEqual(self.job.status, 'Completed')
        self.assertIn('Replaced thermal paste and RAM module', self.job.technician_notes)

        # Service log for labor created
        labor_log = self.job.service_logs.filter(description='Technician Labor Charges').first()
        self.assertIsNotNone(labor_log)
        self.assertEqual(labor_log.service_charge, Decimal('450.00'))

    def test_technician_detail_status_update_completed_redirects_to_technician_dashboard(self):
        self.job.assigned_to = self.tech_profile
        self.job.status = 'Repairing'
        self.job.save()

        self.client_obj.force_login(self.tech_user)
        url = reverse('job_detail_technician', args=[self.job.job_code])
        response = self.client_obj.post(url, {
            'action': 'update_status',
            'status': 'Completed',
            'labor_charge': '300.00',
            'technician_notes': 'Repaired successfully',
        })
        self.assertRedirects(response, reverse('technician_dashboard'))
        self.job.refresh_from_db()
        self.assertEqual(self.job.status, 'Completed')

    def test_technician_detail_status_update_completed_ajax_returns_redirect_url(self):
        self.job.assigned_to = self.tech_profile
        self.job.status = 'Repairing'
        self.job.save()

        self.client_obj.force_login(self.tech_user)
        url = reverse('job_detail_technician', args=[self.job.job_code])
        response = self.client_obj.post(
            url,
            {
                'action': 'update_status',
                'status': 'Completed',
                'labor_charge': '300.00',
                'technician_notes': 'Repaired successfully',
            },
            HTTP_X_REQUESTED_WITH='XMLHttpRequest',
        )
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertTrue(data.get('ok'))
        self.assertEqual(data.get('redirect_url'), reverse('technician_dashboard'))
        self.job.refresh_from_db()
        self.assertEqual(self.job.status, 'Completed')

    def test_technician_job_detail_routes_both_paths(self):
        self.job.assigned_to = self.tech_profile
        self.job.save()
        self.client_obj.force_login(self.tech_user)

        # Primary path: /technician-dashboard/<job_code>/
        resp1 = self.client_obj.get(reverse('job_detail_technician', args=[self.job.job_code]))
        self.assertEqual(resp1.status_code, 200)

        # Alias path: /technician/job/<job_code>/
        resp2 = self.client_obj.get(reverse('technician_job_detail_alias', args=[self.job.job_code]))
        self.assertEqual(resp2.status_code, 200)




class StandaloneTaskSystemTests(TestCase):
    def setUp(self):
        self.workspace, self.staff_user = _make_workspace_and_user('task_staff')
        self.tech_user = User.objects.create_user(
            username='task_tech',
            password='password123',
            email='tech@tasks.com',
        )
        tech_group, _ = Group.objects.get_or_create(name='Technicians')
        self.tech_user.groups.add(tech_group)
        CompanyUserMembership.objects.create(
            user=self.tech_user,
            workspace=self.workspace,
            role=CompanyUserMembership.ROLE_TECHNICIAN,
        )
        self.tech_profile = TechnicianProfile.objects.create(
            user=self.tech_user,
            workspace=self.workspace,
            unique_id='TECH-001',
        )
        self.client = DjangoTestClient()
        self.mobile_token = issue_mobile_jwt(self.tech_user)

    def test_task_priority_ordering(self):
        t_low = Task.objects.create(
            workspace=self.workspace,
            title='Low Priority Task',
            priority='low',
            created_by=self.staff_user,
        )
        t_urgent = Task.objects.create(
            workspace=self.workspace,
            title='Urgent Priority Task',
            priority='urgent',
            created_by=self.staff_user,
        )
        t_med = Task.objects.create(
            workspace=self.workspace,
            title='Medium Priority Task',
            priority='medium',
            created_by=self.staff_user,
        )
        t_high = Task.objects.create(
            workspace=self.workspace,
            title='High Priority Task',
            priority='high',
            created_by=self.staff_user,
        )

        tasks = list(Task.objects.filter(workspace=self.workspace))
        self.assertEqual(tasks[0].id, t_urgent.id)
        self.assertEqual(tasks[1].id, t_high.id)
        self.assertEqual(tasks[2].id, t_med.id)
        self.assertEqual(tasks[3].id, t_low.id)

    def test_staff_task_web_flow(self):
        self.client.force_login(self.staff_user)

        # 1. Staff dashboard
        response = self.client.get(reverse('task_dashboard'))
        self.assertEqual(response.status_code, 200)

        # 2. Staff creates task
        response = self.client.post(reverse('task_create'), {
            'title': 'Server Diagnostics',
            'description': 'Inspect server cooling and disks',
            'priority': 'urgent',
            'assigned_to': self.tech_profile.id,
        })
        self.assertEqual(response.status_code, 302)
        task = Task.objects.filter(workspace=self.workspace, title='Server Diagnostics').first()
        self.assertIsNotNone(task)
        self.assertEqual(task.priority, 'urgent')
        self.assertEqual(task.assigned_to, self.tech_profile)

        # 3. Staff detail & sends message
        msg_response = self.client.post(reverse('task_message_send', args=[task.id]), {
            'body': 'Please bring replacement thermal paste.',
        })
        self.assertEqual(msg_response.status_code, 302)
        self.assertEqual(task.messages.count(), 1)
        self.assertEqual(task.messages.first().body, 'Please bring replacement thermal paste.')

        # 4. Staff updates status
        status_resp = self.client.post(reverse('task_update_status', args=[task.id]), {
            'status': 'in_progress',
        })
        self.assertEqual(status_resp.status_code, 302)
        task.refresh_from_db()
        self.assertEqual(task.status, 'in_progress')

    def test_task_assignment_mutual_exclusivity(self):
        """Option A: Task assignment is either open to all or a specific person, never both."""
        self.client.force_login(self.staff_user)

        # 1. Invalid: Both Open to All and a specific person selected
        resp_invalid = self.client.post(reverse('task_create'), {
            'title': 'Conflicting Assignment Task',
            'priority': 'medium',
            'is_open_to_all': 'on',
            'assigned_to': self.tech_profile.id,
        })
        # Should redirect with form error and not create task
        self.assertEqual(resp_invalid.status_code, 302)
        self.assertFalse(Task.objects.filter(workspace=self.workspace, title='Conflicting Assignment Task').exists())

        # 2. Valid: Open to All only
        resp_open = self.client.post(reverse('task_create'), {
            'title': 'Pool Shared Task',
            'priority': 'medium',
            'is_open_to_all': 'on',
            'assigned_to': '',
        })
        self.assertEqual(resp_open.status_code, 302)
        task_open = Task.objects.filter(workspace=self.workspace, title='Pool Shared Task').first()
        self.assertIsNotNone(task_open)
        self.assertTrue(task_open.is_open_to_all)
        self.assertIsNone(task_open.assigned_to)

        # 3. Valid: Specific technician only
        resp_specific = self.client.post(reverse('task_create'), {
            'title': 'Targeted Tech Task',
            'priority': 'high',
            'is_open_to_all': '',
            'assigned_to': self.tech_profile.id,
        })
        self.assertEqual(resp_specific.status_code, 302)
        task_specific = Task.objects.filter(workspace=self.workspace, title='Targeted Tech Task').first()
        self.assertIsNotNone(task_specific)
        self.assertFalse(task_specific.is_open_to_all)
        self.assertEqual(task_specific.assigned_to, self.tech_profile)

        # 4. Reassignment: Switch from Open to All -> Specific Person via status update
        update_to_specific = self.client.post(reverse('task_update_status', args=[task_open.id]), {
            'assignment_type': 'specific',
            'assigned_to': self.tech_profile.id,
        })
        self.assertEqual(update_to_specific.status_code, 302)
        task_open.refresh_from_db()
        self.assertFalse(task_open.is_open_to_all)
        self.assertEqual(task_open.assigned_to, self.tech_profile)

        # 5. Reassignment: Switch from Specific Person -> Open to All via status update
        update_to_open = self.client.post(reverse('task_update_status', args=[task_open.id]), {
            'assignment_type': 'open',
            'assigned_to': '',
        })
        self.assertEqual(update_to_open.status_code, 302)
        task_open.refresh_from_db()
        self.assertTrue(task_open.is_open_to_all)
        self.assertIsNone(task_open.assigned_to)

    def test_task_management_access_control(self):
        limited_staff = User.objects.create_user(
            username='limited_task_staff',
            password='Password123!',
            is_staff=True,
        )
        CompanyUserMembership.objects.create(
            workspace=self.workspace,
            user=limited_staff,
            role=CompanyUserMembership.ROLE_STAFF,
        )
        # Deny task_management permission (only give staff_dashboard)
        apply_staff_access(limited_staff, {'staff_dashboard'})

        self.client.force_login(limited_staff)
        # Access should be denied (redirects to unauthorized)
        resp = self.client.get(reverse('task_dashboard'))
        self.assertEqual(resp.status_code, 302)
        self.assertIn(reverse('unauthorized'), resp.url)

        # Post create should also be denied
        create_resp = self.client.post(reverse('task_create'), {'title': 'Blocked Task'})
        self.assertEqual(create_resp.status_code, 302)
        self.assertIn(reverse('unauthorized'), create_resp.url)

        # Now grant task_management permission
        apply_staff_access(limited_staff, {'staff_dashboard', 'task_management'})
        resp = self.client.get(reverse('task_dashboard'))
        self.assertEqual(resp.status_code, 200)

    def test_task_management_in_staff_technicians_page_and_edit_user(self):
        apply_staff_access(self.staff_user, {'staff_dashboard', 'team_management', 'task_management'})
        self.client.force_login(self.staff_user)

        # 1. Verify Task Management is listed in staff_technicians page
        resp = self.client.get(reverse('staff_technicians'))
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, 'Task Management')

        # 2. Edit another staff user to toggle Task Management on
        target_staff = User.objects.create_user(
            username='target_sub_staff',
            password='Password123!',
            is_staff=True,
        )
        CompanyUserMembership.objects.create(
            workspace=self.workspace,
            user=target_staff,
            role=CompanyUserMembership.ROLE_STAFF,
        )
        apply_staff_access(target_staff, {'staff_dashboard'})

        edit_resp = self.client.post(reverse('edit_user', args=[target_staff.id]), {
            'username': 'target_sub_staff',
            'role': 'staff',
            'access_staff_dashboard': 'on',
            'access_task_management': 'on',
        })
        self.assertEqual(edit_resp.status_code, 302)

        from job_tickets.access_control import user_has_staff_access
        self.assertTrue(user_has_staff_access(target_staff, 'task_management'))

    def test_technician_task_web_flow(self):
        task = Task.objects.create(
            workspace=self.workspace,
            title='Bench Test Device',
            priority='high',
            assigned_to=self.tech_profile,
            created_by=self.staff_user,
        )

        self.client.force_login(self.tech_user)

        # Technician task dashboard
        resp = self.client.get(reverse('technician_task_dashboard'))
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, 'Bench Test Device')

        # Technician task detail
        detail_resp = self.client.get(reverse('technician_task_detail', args=[task.id]))
        self.assertEqual(detail_resp.status_code, 200)

        # Technician marks completed
        complete_resp = self.client.post(reverse('task_update_status', args=[task.id]), {
            'status': 'done',
        })
        self.assertEqual(complete_resp.status_code, 302)
        task.refresh_from_db()
        self.assertEqual(task.status, 'done')
        self.assertIsNotNone(task.completed_at)

    def test_mobile_task_api_flow(self):
        task = Task.objects.create(
            workspace=self.workspace,
            title='Onsite Router Configuration',
            description='Setup mesh Wi-Fi and VLANs',
            priority='urgent',
            assigned_to=self.tech_profile,
            created_by=self.staff_user,
        )

        auth_headers = {'HTTP_AUTHORIZATION': f'Bearer {self.mobile_token}'}

        # 1. List tasks
        resp = self.client.get(reverse('mobile_api_tasks'), **auth_headers)
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertEqual(data['count'], 1)
        self.assertEqual(data['tasks'][0]['id'], task.id)
        self.assertEqual(data['tasks'][0]['priority'], 'urgent')

        # 2. Detail
        detail_resp = self.client.get(reverse('mobile_api_task_detail', args=[task.id]), **auth_headers)
        self.assertEqual(detail_resp.status_code, 200)
        self.assertEqual(detail_resp.json()['task']['title'], 'Onsite Router Configuration')

        # 3. Update status to in_progress
        upd_resp = self.client.post(
            reverse('mobile_api_task_update_status', args=[task.id]),
            data=json.dumps({'status': 'in_progress'}),
            content_type='application/json',
            **auth_headers,
        )
        self.assertEqual(upd_resp.status_code, 200)
        task.refresh_from_db()
        self.assertEqual(task.status, 'in_progress')

        # 4. Send message
        msg_resp = self.client.post(
            reverse('mobile_api_task_message_send', args=[task.id]),
            data=json.dumps({'message': 'Arrived at site, beginning configuration.'}),
            content_type='application/json',
            **auth_headers,
        )
        self.assertEqual(msg_resp.status_code, 200)
        self.assertEqual(task.messages.count(), 1)
        self.assertEqual(task.messages.first().body, 'Arrived at site, beginning configuration.')

    def test_mobile_task_creation_and_lookups_workflow(self):
        auth_headers = {'HTTP_AUTHORIZATION': f'Bearer {self.mobile_token}'}

        # 1. Technicians lookup
        tech_resp = self.client.get(reverse('mobile_api_technicians'), **auth_headers)
        self.assertEqual(tech_resp.status_code, 200)
        tech_data = tech_resp.json()
        self.assertIn('technicians', tech_data)

        # 2. Jobs quick-list lookup
        jobs_resp = self.client.get(reverse('mobile_api_jobs_quicklist'), **auth_headers)
        self.assertEqual(jobs_resp.status_code, 200)
        jobs_data = jobs_resp.json()
        self.assertIn('jobs', jobs_data)

        # 3. Technician cannot create task from mobile (403 Forbidden)
        create_payload = {
            'title': 'Bench Diagnostic for Acer Nitro',
            'description': 'Check GPU thermal throttling',
            'priority': 'high',
            'initial_message': 'Started logging temperatures',
        }
        tech_create_resp = self.client.post(
            reverse('mobile_api_tasks'),
            data=json.dumps(create_payload),
            content_type='application/json',
            **auth_headers,
        )
        self.assertEqual(tech_create_resp.status_code, 403)

        # 4. Staff CAN create task via mobile API (201 Created)
        staff_token = issue_mobile_jwt(self.staff_user)
        staff_auth_headers = {'HTTP_AUTHORIZATION': f'Bearer {staff_token}'}
        staff_payload = dict(create_payload, assigned_to_id=self.tech_profile.id)
        create_resp = self.client.post(
            reverse('mobile_api_tasks'),
            data=json.dumps(staff_payload),
            content_type='application/json',
            **staff_auth_headers,
        )
        self.assertEqual(create_resp.status_code, 201)
        res_data = create_resp.json()
        self.assertTrue(res_data['ok'])
        created_task_id = res_data['task']['id']
        self.assertEqual(res_data['task']['title'], 'Bench Diagnostic for Acer Nitro')
        self.assertEqual(res_data['task']['priority'], 'high')

        # 5. Technician cannot edit task details from mobile (403 Forbidden)
        upd_payload = {
            'priority': 'urgent',
            'description': 'GPU thermal paste dried out, fan replacement needed',
        }
        tech_upd_resp = self.client.post(
            reverse('mobile_api_task_detail', args=[created_task_id]),
            data=json.dumps(upd_payload),
            content_type='application/json',
            **auth_headers,
        )
        self.assertEqual(tech_upd_resp.status_code, 403)

        # 6. Staff CAN edit task details via mobile API (200 OK)
        staff_upd_resp = self.client.post(
            reverse('mobile_api_task_detail', args=[created_task_id]),
            data=json.dumps(upd_payload),
            content_type='application/json',
            **staff_auth_headers,
        )
        self.assertEqual(staff_upd_resp.status_code, 200)
        self.assertEqual(staff_upd_resp.json()['task']['priority'], 'urgent')

        # 5. Fetch tasks with metrics
        list_resp = self.client.get(reverse('mobile_api_tasks'), **auth_headers)
        self.assertEqual(list_resp.status_code, 200)
        list_data = list_resp.json()
        self.assertIn('metrics', list_data)
        self.assertGreaterEqual(list_data['metrics']['total'], 1)


class TaskRealtimeWebSocketTests(TransactionTestCase):
    def setUp(self):
        self.workspace, self.staff_user = _make_workspace_and_user('task_ws_staff')
        self.tech_user = User.objects.create_user(
            username='task_ws_tech',
            password='password123',
            email='tech_ws@tasks.com',
        )
        tech_group, _ = Group.objects.get_or_create(name='Technicians')
        self.tech_user.groups.add(tech_group)
        CompanyUserMembership.objects.create(
            user=self.tech_user,
            workspace=self.workspace,
            role=CompanyUserMembership.ROLE_TECHNICIAN,
        )
        self.tech_profile = TechnicianProfile.objects.create(
            user=self.tech_user,
            workspace=self.workspace,
            unique_id='TWS001',
        )
        self.task = Task.objects.create(
            workspace=self.workspace,
            title='Realtime Diagnostic Task',
            description='Test websocket syncing and live chat',
            priority='urgent',
            assigned_to=self.tech_profile,
            created_by=self.staff_user,
        )
        self.mobile_token = issue_mobile_jwt(self.tech_user)

    def test_task_chat_consumer_staff_broadcast(self):
        from .consumers import task_room_group_name

        async def exercise():
            application = URLRouter(websocket_urlpatterns)
            communicator = WebsocketCommunicator(
                application,
                f'/ws/tasks/{self.task.id}/',
            )
            communicator.scope['user'] = self.staff_user
            connected, _ = await communicator.connect()
            self.assertTrue(connected)

            channel_layer = get_channel_layer()
            await channel_layer.group_send(
                task_room_group_name(self.workspace.id, self.task.id),
                {
                    'type': 'task_message_event',
                    'task_id': self.task.id,
                    'id': 101,
                    'message_id': 101,
                    'sender': 'task_ws_staff',
                    'body': 'Real-time test message for technician',
                    'sent_at': '2026-09-16 18:00',
                    'attachments': [],
                },
            )

            event = await asyncio.wait_for(communicator.receive_json_from(), timeout=2)
            self.assertEqual(event.get('type'), 'task_message_event')
            self.assertEqual(event.get('body'), 'Real-time test message for technician')
            self.assertEqual(event.get('sender'), 'task_ws_staff')

            await channel_layer.group_send(
                task_room_group_name(self.workspace.id, self.task.id),
                {
                    'type': 'task_status_event',
                    'task_id': self.task.id,
                    'old_status': 'open',
                    'new_status': 'in_progress',
                    'status_display': 'In Progress',
                    'updated_by': 'task_ws_staff',
                    'completed_at': '',
                },
            )
            status_event = await asyncio.wait_for(communicator.receive_json_from(), timeout=2)
            self.assertEqual(status_event.get('type'), 'task_status_event')
            self.assertEqual(status_event.get('new_status'), 'in_progress')

            await communicator.disconnect()

        asyncio.run(exercise())

    def test_task_chat_consumer_mobile_jwt_connection(self):
        async def exercise():
            application = URLRouter(websocket_urlpatterns)
            communicator = WebsocketCommunicator(
                application,
                f'/ws/tasks/{self.task.id}/?token={self.mobile_token}',
            )
            connected, _ = await communicator.connect()
            self.assertTrue(connected)
            await communicator.disconnect()

        asyncio.run(exercise())

    def test_tech_task_dashboard_consumer_feed(self):
        from .consumers import tech_tasks_group_name

        async def exercise():
            application = URLRouter(websocket_urlpatterns)
            communicator = WebsocketCommunicator(
                application,
                f'/ws/tech_tasks/?token={self.mobile_token}',
            )
            connected, _ = await communicator.connect()
            self.assertTrue(connected)

            channel_layer = get_channel_layer()
            await channel_layer.group_send(
                tech_tasks_group_name(self.workspace.id, self.tech_profile.id),
                {
                    'type': 'task_feed_event',
                    'action': 'task_created',
                    'task_id': self.task.id,
                    'task_title': self.task.title,
                    'priority': self.task.priority,
                    'status': self.task.status,
                    'assigned_to': self.tech_user.username,
                },
            )
            feed_event = await asyncio.wait_for(communicator.receive_json_from(), timeout=2)
            self.assertEqual(feed_event.get('type'), 'task_feed_event')
            self.assertEqual(feed_event.get('action'), 'task_created')
            self.assertEqual(feed_event.get('task_id'), self.task.id)

            await communicator.disconnect()

        asyncio.run(exercise())

    def test_broadcast_task_helpers(self):
        from .views.helpers import (
            broadcast_task_message,
            broadcast_task_status,
            broadcast_task_created,
            broadcast_task_deleted,
        )

        msg = TaskMessage.objects.create(
            task=self.task,
            sender=self.staff_user,
            body='Sync broadcast helper test message',
        )
        broadcast_task_message(self.task, msg)
        broadcast_task_status(self.task, 'open', 'in_progress', self.staff_user)
        broadcast_task_created(self.task)
        broadcast_task_deleted(self.workspace.id, self.task.id, self.task.title)

    def test_task_chat_consumer_legacy_workspace_assigned_tech(self):
        legacy_task = Task.objects.create(
            workspace=None,
            title='Legacy Task Without Workspace',
            description='Should allow assigned tech even without workspace or membership',
            priority='high',
            assigned_to=self.tech_profile,
            created_by=self.staff_user,
        )

        async def exercise():
            application = URLRouter(websocket_urlpatterns)
            communicator = WebsocketCommunicator(
                application,
                f'/ws/tasks/{legacy_task.id}/',
            )
            communicator.scope['user'] = self.tech_user
            connected, _ = await communicator.connect()
            self.assertTrue(connected)
            await communicator.disconnect()

        asyncio.run(exercise())


class ClientManagementLifecycleTests(TestCase):
    """Test client dashboard, edit client, and 360 profile views."""

    def setUp(self):
        self.user = User.objects.create_user(
            username='client-mgmt-staff',
            password='StrongPass123!',
            is_staff=True,
        )
        apply_staff_access(self.user, {'staff_dashboard', 'inventory'})
        self.workspace = CompanyWorkspace.objects.create(name='Client WS', owner=self.user)
        CompanyUserMembership.objects.create(
            workspace=self.workspace,
            user=self.user,
            role=CompanyUserMembership.ROLE_ADMIN,
        )
        self.client.force_login(self.user)
        self.client_obj = Client.objects.create(
            workspace=self.workspace,
            name='Ramesh Kumar',
            phone='9876543210',
            email='ramesh@example.com',
            company_name='Ramesh Enterprises',
            address='123 Main St, Mumbai',
        )
        self.job = JobTicket.objects.create(
            workspace=self.workspace,
            job_code='JOB-CLI-001',
            customer_name='Ramesh Kumar',
            customer_phone='9876543210',
            device_type='Laptop',
            reported_issue='Overheating',
            status='Ready for Pickup',
            amount_paid=Decimal('200.00'),
            payment_status='part_paid',
        )
        ServiceLog.objects.create(
            job_ticket=self.job,
            description='Thermal paste replacement',
            part_cost=Decimal('100.00'),
            service_charge=Decimal('400.00'),
        )
        # Total = 500, Paid = 200, Balance = 300

    def test_client_dashboard_renders_financial_stats_and_tabs(self):
        url = reverse('client_dashboard')
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Ramesh Kumar')
        self.assertContains(response, 'Due:')
        self.assertContains(response, '300.00')

        # Filter by credit_due tab
        resp_credit = self.client.get(f"{url}?tab=credit_due")
        self.assertEqual(resp_credit.status_code, 200)
        self.assertContains(resp_credit, 'Ramesh Kumar')

    def test_edit_client_updates_details(self):
        party = InventoryParty.objects.create(
            workspace=self.workspace,
            name='Ramesh Kumar',
            phone='9876543210',
            party_type='customer',
        )
        url = reverse('edit_client', args=[self.client_obj.id])
        response = self.client.post(url, {
            'name': 'Ramesh K. Updated',
            'phone': '9899988888',
            'email': 'updated@example.com',
            'company_name': 'Ramesh Tech Solutions',
            'address': '456 Second St, Mumbai',
            'notes': 'VIP Customer',
        })
        self.assertEqual(response.status_code, 302)
        self.client_obj.refresh_from_db()
        self.assertEqual(self.client_obj.name, 'Ramesh K. Updated')
        self.assertEqual(self.client_obj.phone, '9899988888')
        self.assertEqual(self.client_obj.company_name, 'Ramesh Tech Solutions')
        self.assertEqual(self.client_obj.notes, 'VIP Customer')

        # Verify historical JobTickets cascaded to the new phone and name
        self.job.refresh_from_db()
        self.assertEqual(self.job.customer_phone, '9899988888')
        self.assertEqual(self.job.customer_name, 'Ramesh K. Updated')

        # Verify linked InventoryParty cascaded to the new phone and name
        party.refresh_from_db()
        self.assertEqual(party.phone, '9899988888')
        self.assertEqual(party.name, 'Ramesh K. Updated')
        self.assertEqual(party.legal_name, 'Ramesh Tech Solutions')

        # Verify client detail still renders all previous jobs after phone change
        detail_url = reverse('client_detail', args=[self.client_obj.id])
        detail_resp = self.client.get(detail_url)
        self.assertEqual(detail_resp.status_code, 200)
        self.assertContains(detail_resp, 'JOB-CLI-001')

        # Verify client dashboard still displays the client's jobs
        dash_url = reverse('client_dashboard')
        dash_resp = self.client.get(dash_url)
        self.assertEqual(dash_resp.status_code, 200)
        self.assertContains(dash_resp, 'JOB-CLI-001')

    def test_edit_client_rejects_duplicate_phone(self):
        Client.objects.create(
            workspace=self.workspace,
            name='Other Person',
            phone='9123456789',
        )
        url = reverse('edit_client', args=[self.client_obj.id])
        response = self.client.post(url, {
            'name': 'Ramesh Kumar',
            'phone': '9123456789',
        })
        self.assertEqual(response.status_code, 302)
        self.client_obj.refresh_from_db()
        self.assertEqual(self.client_obj.phone, '9876543210')

    def test_client_detail_renders_360_profile_and_ledger(self):
        url = reverse('client_detail', args=[self.client_obj.id])
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Ramesh Kumar')
        self.assertContains(response, 'JOB-CLI-001')
        self.assertContains(response, 'Lifetime Billed')
        self.assertContains(response, 'Outstanding Credit')
        self.assertContains(response, '300.00')


class InventorySerialTrackingPhase2Tests(TestCase):
    """Test Phase 2 serial/IMEI tracking, bin locations, warranty, and lifecycle audit."""

    def setUp(self):
        self.user = User.objects.create_user(
            username='inventory-p2-tester',
            password='StrongPass123!',
            is_staff=True,
        )
        apply_staff_access(self.user, {'staff_dashboard', 'inventory'})
        self.workspace = CompanyWorkspace.objects.create(name='ERP Test WS', owner=self.user)
        CompanyUserMembership.objects.create(
            workspace=self.workspace,
            user=self.user,
            role=CompanyUserMembership.ROLE_ADMIN,
        )
        self.client.force_login(self.user)
        self.supplier = InventoryParty.objects.create(
            workspace=self.workspace,
            name='Global Hardware Distributors',
            party_type='supplier',
            phone='9811122233',
        )
        self.customer = InventoryParty.objects.create(
            workspace=self.workspace,
            name='Rohit Sharma',
            party_type='customer',
            phone='9844455566',
        )
        self.product = Product.objects.create(
            workspace=self.workspace,
            name='iPhone 15 Pro OLED Display',
            sku='SCR-IP15P-OLED',
            bin_location='Rack A / Shelf 2 / Bin 04',
            has_serial_tracking=True,
            vendor_warranty_months=12,
            customer_warranty_months=6,
            cost_price=Decimal('12000.00'),
            unit_price=Decimal('16500.00'),
            gst_rate=Decimal('18.00'),
            stock_quantity=0,
        )

    def test_product_phase2_fields(self):
        self.assertEqual(self.product.bin_location, 'Rack A / Shelf 2 / Bin 04')
        self.assertTrue(self.product.has_serial_tracking)
        self.assertEqual(self.product.vendor_warranty_months, 12)
        self.assertEqual(self.product.customer_warranty_months, 6)

    def test_purchase_and_sale_serial_lifecycle(self):
        purchase_url = reverse('inventory_purchase_dashboard')
        post_data = {
            'inventory_entry_submit': 'purchase',
            'entry_date': '2026-09-25',
            'invoice_number': 'INV-SUP-8891',
            'party': str(self.supplier.id),
            'bill_discount_amount': '0.00',
            'bill_notes': 'Inward stock for displays',
            'line_product_id[]': [str(self.product.id)],
            'line_quantity[]': ['2'],
            'line_unit_price[]': ['12000.00'],
            'line_gst_rate[]': ['18.00'],
            'line_serials[]': ['SN-DISP-001, SN-DISP-002'],
        }
        resp = self.client.post(purchase_url, post_data)
        self.assertEqual(resp.status_code, 302)

        # Verify serial records created in stock
        from .models import ProductItemSerial, InventoryEntry
        serials = list(ProductItemSerial.objects.filter(product=self.product).order_by('serial_number'))
        self.assertEqual(len(serials), 2)
        self.assertEqual(serials[0].serial_number, 'SN-DISP-001')
        self.assertEqual(serials[0].status, 'in_stock')
        self.assertEqual(serials[0].purchase_cost, Decimal('12000.00'))
        self.assertIsNotNone(serials[0].vendor_warranty_until)
        self.assertTrue(serials[0].is_vendor_warranty_active)

        # Now sell one serialized item
        sale_url = reverse('inventory_sales_dashboard')
        sale_data = {
            'inventory_entry_submit': 'sale',
            'entry_date': '2026-09-25',
            'party': str(self.customer.id),
            'bill_discount_amount': '0.00',
            'bill_payment_status': 'paid',
            'bill_notes': 'Screen replacement sale',
            'line_product_id[]': [str(self.product.id)],
            'line_quantity[]': ['1'],
            'line_unit_price[]': ['16500.00'],
            'line_gst_rate[]': ['18.00'],
            'line_serials[]': ['SN-DISP-001'],
        }
        sale_resp = self.client.post(sale_url, sale_data)
        self.assertEqual(sale_resp.status_code, 302)

        # Verify serial status transition to sold
        serials[0].refresh_from_db()
        self.assertEqual(serials[0].status, 'sold')
        self.assertEqual(serials[0].sale_price, Decimal('16500.00'))
        self.assertIsNotNone(serials[0].customer_warranty_until)
        self.assertTrue(serials[0].is_customer_warranty_active)

        # Verify track serial endpoint returns complete lifecycle audit
        track_url = reverse('inventory_track_serial') + '?serial=SN-DISP-001'
        track_resp = self.client.get(track_url)
        self.assertEqual(track_resp.status_code, 200)
        data = track_resp.json()
        self.assertTrue(data['ok'])
        self.assertTrue(data['found'])
        self.assertEqual(data['item']['serial_number'], 'SN-DISP-001')
        self.assertEqual(data['item']['status'], 'sold')
        self.assertEqual(data['item']['bin_location'], 'Rack A / Shelf 2 / Bin 04')
        self.assertEqual(data['item']['supplier_name'], 'Global Hardware Distributors')
        self.assertEqual(data['item']['customer_name'], 'Rohit Sharma')
        self.assertTrue(data['item']['is_customer_warranty_active'])

        # Verify non-existent serial returns found=False
        miss_url = reverse('inventory_track_serial') + '?serial=UNKNOWN-999'
        miss_resp = self.client.get(miss_url)
        self.assertEqual(miss_resp.status_code, 200)
        miss_data = miss_resp.json()
        self.assertTrue(miss_data['ok'])
        self.assertFalse(miss_data['found'])


class TechnicianProfileAndQrMobileTests(TestCase):
    def setUp(self):
        self.tech_group, _ = Group.objects.get_or_create(name='Technicians')
        self.staff_group, _ = Group.objects.get_or_create(name='Staff')

        self.tech_user = User.objects.create_user(
            username='tech_sam',
            password='InitialPassword123!',
            first_name='Sam',
            last_name='Tech',
            email='sam@example.com',
        )
        self.tech_user.groups.add(self.tech_group)
        self.tech_profile = TechnicianProfile.objects.create(
            user=self.tech_user,
            unique_id='TECH-007',
        )

        self.staff_user = User.objects.create_user(
            username='staff_alex',
            password='StaffPassword123!',
            is_staff=True,
        )
        self.staff_user.groups.add(self.staff_group)
        apply_staff_access(self.staff_user, {'team_management', 'company_settings', 'staff_dashboard'})

    def test_technician_profile_get_and_password_change(self):
        self.client.force_login(self.tech_user)
        resp = self.client.get(reverse('technician_profile'))
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, 'TECH-007')
        self.assertContains(resp, 'Sam Tech')
        self.assertContains(resp, 'Role')
        self.assertContains(resp, 'Technician')
        self.assertNotContains(resp, 'Assigned Workspace')
        self.assertContains(resp, 'Change Password')

        # Update profile POST
        update_resp = self.client.post(reverse('technician_profile'), {
            'action': 'update_profile',
            'full_name': 'Samuel Technical',
            'email': 'samuel.tech@example.com',
        }, follow=True)
        self.assertEqual(update_resp.status_code, 200)
        self.assertContains(update_resp, 'Profile updated successfully.')
        self.tech_user.refresh_from_db()
        self.assertEqual(self.tech_user.first_name, 'Samuel')
        self.assertEqual(self.tech_user.last_name, 'Technical')
        self.assertEqual(self.tech_user.email, 'samuel.tech@example.com')

        # Change password POST with invalid current password
        resp_err = self.client.post(reverse('technician_profile'), {
            'action': 'change_password',
            'current_password': 'WrongPassword123',
            'new_password': 'NewStrongPassword456!',
            'confirm_password': 'NewStrongPassword456!',
        }, follow=True)
        self.assertEqual(resp_err.status_code, 200)
        self.assertContains(resp_err, 'Current password is incorrect.')

        # Change password POST success
        resp_ok = self.client.post(reverse('technician_profile'), {
            'action': 'change_password',
            'current_password': 'InitialPassword123!',
            'new_password': 'NewStrongPassword456!',
            'confirm_password': 'NewStrongPassword456!',
        }, follow=True)
        self.assertEqual(resp_ok.status_code, 200)
        self.assertContains(resp_ok, 'Your password has been changed successfully.')

        self.tech_user.refresh_from_db()
        self.assertTrue(self.tech_user.check_password('NewStrongPassword456!'))

    def test_technician_generate_qr_token_and_status_polling(self):
        self.client.force_login(self.tech_user)
        gen_resp = self.client.get(reverse('technician_generate_qr_token'))
        self.assertEqual(gen_resp.status_code, 200)
        gen_data = gen_resp.json()
        self.assertTrue(gen_data['ok'])
        token_str = gen_data['token']
        self.assertIn('data:image/png;base64,', gen_data['qr_image'])

        # Poll status -> should be pending
        status_resp = self.client.get(reverse('qr_login_token_status', args=[token_str]))
        self.assertEqual(status_resp.status_code, 200)
        self.assertEqual(status_resp.json()['status'], 'pending')

    def test_mobile_api_qr_login_success_and_replay_protection(self):
        # Create token
        token_str = 'test-qr-token-12345'
        qr_tok = MobileQrLoginToken.objects.create(
            token=token_str,
            user=self.tech_user,
            expires_at=timezone.now() + timezone.timedelta(minutes=10),
        )

        # Login via mobile API using token
        login_resp = self.client.post(
            reverse('mobile_api_qr_login'),
            data=json.dumps({'qr_token': token_str, 'device_name': 'Pixel 8 Pro'}),
            content_type='application/json',
        )
        self.assertEqual(login_resp.status_code, 200)
        login_data = login_resp.json()
        self.assertIn('access_token', login_data)
        self.assertEqual(login_data['user']['username'], 'tech_sam')
        self.assertEqual(login_data['user']['technician_id'], 'TECH-007')

        qr_tok.refresh_from_db()
        self.assertTrue(qr_tok.is_used)
        self.assertIsNotNone(qr_tok.used_at)

        # Check status endpoint now reports used
        status_resp = self.client.get(reverse('qr_login_token_status', args=[token_str]))
        self.assertEqual(status_resp.json()['status'], 'used')

        # Second attempt with same token fails (replay protection)
        replay_resp = self.client.post(
            reverse('mobile_api_qr_login'),
            data=json.dumps({'qr_token': token_str}),
            content_type='application/json',
        )
        self.assertEqual(replay_resp.status_code, 401)
        self.assertEqual(replay_resp.json()['error'], 'expired_token')

    def test_mobile_session_tracking_and_remote_logout(self):
        # 1. Login via mobile API
        login_resp = self.client.post(
            reverse('mobile_api_login'),
            data=json.dumps({'username': 'tech_sam', 'password': 'InitialPassword123!', 'device_name': 'Samsung Galaxy Tab'}),
            content_type='application/json',
        )
        self.assertEqual(login_resp.status_code, 200)
        access_token = login_resp.json()['access_token']

        # 2. Access authenticated mobile endpoint
        me_resp = self.client.get(
            reverse('mobile_api_me'),
            HTTP_AUTHORIZATION=f'Bearer {access_token}',
        )
        self.assertEqual(me_resp.status_code, 200)

        # 3. Verify session was recorded in UserSessionActivity
        session = UserSessionActivity.objects.filter(user=self.tech_user, channel='api', status='active').first()
        self.assertIsNotNone(session)
        self.assertIn('Samsung Galaxy Tab', session.user_agent)

        # 4. Technician remotely logs out device from panel
        self.client.force_login(self.tech_user)
        logout_resp = self.client.post(
            reverse('technician_logout_device', args=[session.id]),
            HTTP_X_REQUESTED_WITH='XMLHttpRequest',
        )
        self.assertEqual(logout_resp.status_code, 200)

        session.refresh_from_db()
        self.assertEqual(session.status, UserSessionActivity.STATUS_LOGGED_OUT)

        # 5. Mobile API request with that token now immediately rejected
        revoked_resp = self.client.get(
            reverse('mobile_api_me'),
            HTTP_AUTHORIZATION=f'Bearer {access_token}',
        )
        self.assertEqual(revoked_resp.status_code, 401)
        self.assertEqual(revoked_resp.json()['error'], 'session_revoked')

    def test_staff_generate_qr_token_and_remote_device_management(self):
        self.client.force_login(self.staff_user)

        # Staff generates QR for tech
        staff_qr_resp = self.client.get(reverse('staff_generate_qr_token', args=[self.tech_user.id]))
        self.assertEqual(staff_qr_resp.status_code, 200)
        data = staff_qr_resp.json()
        self.assertTrue(data['ok'])
        self.assertEqual(data['username'], 'tech_sam')

        # Simulate device logging in
        session = UserSessionActivity.objects.create(
            user=self.tech_user,
            session_key='session-key-staff-test',
            channel=UserSessionActivity.CHANNEL_API,
            status=UserSessionActivity.STATUS_ACTIVE,
            user_agent='Motorola Edge 40 (Botgi Mobile App)',
            login_at=timezone.now(),
            last_activity_at=timezone.now(),
        )

        # Staff fetches active devices for tech
        dev_resp = self.client.get(reverse('staff_user_active_devices', args=[self.tech_user.id]))
        self.assertEqual(dev_resp.status_code, 200)
        self.assertEqual(len(dev_resp.json()['devices']), 1)

        # Staff logs out all devices for tech
        logout_resp = self.client.post(
            reverse('staff_logout_all_devices', args=[self.tech_user.id]),
            HTTP_X_REQUESTED_WITH='XMLHttpRequest',
        )
        self.assertEqual(logout_resp.status_code, 200)

        session.refresh_from_db()
        self.assertEqual(session.status, UserSessionActivity.STATUS_LOGGED_OUT)

    def test_app_release_management_and_download(self):
        self.client.force_login(self.staff_user)

        # Upload release with external URL
        upload_resp = self.client.post(reverse('app_release_upload'), {
            'version_name': '2.1.0',
            'version_code': '21',
            'min_version_code': '10',
            'download_url': 'https://example.com/builds/app-v2.1.0.apk',
            'release_notes': 'Fixed camera scan and job status sync.',
            'is_active': 'on',
        })
        self.assertEqual(upload_resp.status_code, 302)

        release = MobileAppRelease.objects.filter(version_code=21).first()
        self.assertIsNotNone(release)
        self.assertTrue(release.is_active)
        self.assertEqual(release.version_name, '2.1.0')

        # Check public meta endpoint
        meta_resp = self.client.get(reverse('app_release_meta'))
        self.assertEqual(meta_resp.status_code, 200)
        meta_data = meta_resp.json()
        self.assertTrue(meta_data['ok'])
        self.assertIn('technician_app', meta_data)
        self.assertEqual(meta_data['technician_app']['version_name'], '2.1.0')
        self.assertEqual(meta_data['technician_app']['version_code'], 21)

        # Check download redirect
        dl_resp = self.client.get(reverse('download_technician_app'))
        self.assertEqual(dl_resp.status_code, 302)
        self.assertEqual(dl_resp.url, 'https://example.com/builds/app-v2.1.0.apk')

        # Toggle release active
        toggle_resp = self.client.post(reverse('app_release_toggle_active', args=[release.id]))
        self.assertEqual(toggle_resp.status_code, 302)
        release.refresh_from_db()
        self.assertFalse(release.is_active)

        # Delete release
        del_resp = self.client.post(reverse('app_release_delete', args=[release.id]))
        self.assertEqual(del_resp.status_code, 302)
        self.assertFalse(MobileAppRelease.objects.filter(id=release.id).exists())

    def test_staff_cannot_access_technician_profile_or_mobile_app(self):
        # 1. Staff accessing technician profile is redirected
        self.client.force_login(self.staff_user)
        resp = self.client.get(reverse('technician_profile'))
        self.assertEqual(resp.status_code, 302)
        self.assertIn(reverse('unauthorized'), resp.url)

        # 2. Staff calling technician QR generation gets 403
        gen_resp = self.client.get(reverse('technician_generate_qr_token'))
        self.assertEqual(gen_resp.status_code, 403)
        self.assertFalse(gen_resp.json()['ok'])

        # 3. Staff trying to log into mobile app via credentials gets 403 technician_only
        login_resp = self.client.post(
            reverse('mobile_api_login'),
            data=json.dumps({'username': 'staff_alex', 'password': 'StaffPassword123!'}),
            content_type='application/json',
        )
        self.assertEqual(login_resp.status_code, 403)
        self.assertEqual(login_resp.json()['error'], 'technician_only')

        # 4. Staff cannot have a QR token generated for them
        staff_qr_resp = self.client.get(reverse('staff_generate_qr_token', args=[self.staff_user.id]))
        self.assertEqual(staff_qr_resp.status_code, 400)
        self.assertFalse(staff_qr_resp.json()['ok'])


class InventorySalesPurchaseChartTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username='chart_staff', password='Password123!', is_staff=True)
        self.workspace = CompanyWorkspace.objects.create(name='Chart Test Workshop', slug='chart-test-ws', owner=self.user)
        CompanyUserMembership.objects.create(user=self.user, workspace=self.workspace, role='ADMIN')
        apply_staff_access(self.user, [
            "dashboard", "job_tickets", "clients", "inventory",
        ])
        self.party = InventoryParty.objects.create(
            name='Test Party',
            party_type='both',
            workspace=self.workspace,
        )
        self.product = Product.objects.create(
            name='Test SSD',
            sku='SSD-512',
            unit_price=Decimal('2500.00'),
            cost_price=Decimal('1800.00'),
            workspace=self.workspace,
        )
        self.bill = InventoryBill.objects.create(
            bill_number='BILL-CHART-001',
            entry_type='sale',
            party=self.party,
            workspace=self.workspace,
            entry_date=timezone.localdate(),
        )
        InventoryEntry.objects.create(
            entry_number='ENT-S-001',
            bill=self.bill,
            party=self.party,
            product=self.product,
            entry_type='sale',
            quantity=2,
            unit_price=Decimal('2500.00'),
            total_amount=Decimal('5000.00'),
            workspace=self.workspace,
            entry_date=timezone.localdate(),
        )
        self.purchase_bill = InventoryBill.objects.create(
            bill_number='BILL-PUR-001',
            entry_type='purchase',
            party=self.party,
            workspace=self.workspace,
            entry_date=timezone.localdate(),
        )
        InventoryEntry.objects.create(
            entry_number='ENT-P-001',
            bill=self.purchase_bill,
            party=self.party,
            product=self.product,
            entry_type='purchase',
            quantity=5,
            unit_price=Decimal('1800.00'),
            total_amount=Decimal('9000.00'),
            workspace=self.workspace,
            entry_date=timezone.localdate(),
        )

    def test_build_chart_data_aggregates_sales_and_purchases(self):
        from .views.helpers import _build_cash_flow_chart_data
        data = _build_cash_flow_chart_data(self.workspace, 'this_month')
        self.assertEqual(data['total_sales'], 5000.00)
        self.assertEqual(data['total_purchases'], 9000.00)
        self.assertEqual(data['total_sales_qty'], 2)
        self.assertEqual(data['total_purchases_qty'], 5)
        self.assertIn('sales', data)
        self.assertIn('purchases', data)
        self.assertEqual(len(data['sales']), len(data['labels']))
        self.assertEqual(len(data['purchases']), len(data['labels']))

    def test_chart_api_endpoint(self):
        self.client.force_login(self.user)
        response = self.client.get(reverse('inventory_cash_flow_chart_api'), {'range': 'this_month'})
        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload['total_sales'], 5000.00)
        self.assertEqual(payload['total_purchases'], 9000.00)
        self.assertEqual(payload['total_sales_qty'], 2)
        self.assertEqual(payload['total_purchases_qty'], 5)


class RateFieldNoSpecialCharactersTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username='test_rate_user', password='password123', is_staff=True)
        self.workspace = CompanyWorkspace.objects.create(name='Rate Test Workshop', slug='rate-test-ws', owner=self.user)
        CompanyUserMembership.objects.create(user=self.user, workspace=self.workspace, role='ADMIN')
        self.product = Product.objects.create(
            name='Test Mouse',
            cost_price=Decimal('100.00'),
            unit_price=Decimal('150.00'),
            workspace=self.workspace,
            stock_quantity=10,
        )

    def test_collect_inventory_bill_lines_rejects_special_characters(self):
        from .views.helpers import _collect_inventory_bill_lines
        invalid_rates = ['100@', '$100', '-50', '1e5', '₹200', '100#', '12%34', '100+']
        for bad_rate in invalid_rates:
            with self.subTest(rate=bad_rate):
                with self.assertRaises(ValueError) as ctx:
                    _collect_inventory_bill_lines(
                        product_ids=[str(self.product.id)],
                        quantities=['1'],
                        unit_prices=[bad_rate],
                        gst_rates=['0.00'],
                        workspace=self.workspace,
                    )
                self.assertIn('special characters are not allowed in rate', str(ctx.exception).lower())

    def test_collect_inventory_bill_lines_accepts_valid_rates(self):
        from .views.helpers import _collect_inventory_bill_lines
        valid_rates = ['100', '150.50', '0.75', '2500.00']
        for good_rate in valid_rates:
            with self.subTest(rate=good_rate):
                lines = _collect_inventory_bill_lines(
                    product_ids=[str(self.product.id)],
                    quantities=['1'],
                    unit_prices=[good_rate],
                    gst_rates=['0.00'],
                    workspace=self.workspace,
                )
                self.assertEqual(len(lines), 1)
                self.assertEqual(lines[0]['unit_price'], Decimal(good_rate))


class PhoneValidationTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username='phone_test_user', password='password123', is_staff=True)
        self.workspace = CompanyWorkspace.objects.create(name='Phone Test Workshop', slug='phone-test-ws', owner=self.user)
        CompanyUserMembership.objects.create(user=self.user, workspace=self.workspace, role='ADMIN')
        apply_staff_access(self.user, ["dashboard", "inventory", "staff_dashboard"])

    def test_inventory_party_form_phone_validation(self):
        from .forms import InventoryPartyForm
        # Invalid phone with letters/symbols
        form_invalid = InventoryPartyForm(data={
            'name': 'Test Supplier',
            'phone': '897465465465zsdfgzsdxf',
            'country': 'India',
            'opening_balance': '0.00',
        })
        self.assertFalse(form_invalid.is_valid())
        self.assertIn('phone', form_invalid.errors)

        # Valid 10-digit phone
        form_valid = InventoryPartyForm(data={
            'name': 'Valid Supplier',
            'phone': '9876543210',
            'country': 'India',
            'opening_balance': '0.00',
        })
        self.assertTrue(form_valid.is_valid())
        self.assertEqual(form_valid.cleaned_data['phone'], '9876543210')

    def test_quick_add_party_phone_validation(self):
        self.client.force_login(self.user)
        # Attempting to save with invalid letters/too long phone
        resp_invalid = self.client.post(reverse('inventory_quick_add_party'), {
            'name': 'Party With Bad Phone',
            'phone': '897465465465zsdfgzsdxf',
        })
        self.assertEqual(resp_invalid.status_code, 400)
        self.assertFalse(resp_invalid.json().get('ok', True))

        # Saving with clean 10-digit phone
        resp_valid = self.client.post(reverse('inventory_quick_add_party'), {
            'name': 'Party With Good Phone',
            'phone': '9876543210',
        })
        self.assertEqual(resp_valid.status_code, 200)
        self.assertTrue(resp_valid.json().get('ok'))
        self.assertEqual(resp_valid.json()['party']['phone'], '9876543210')

    def test_customer_phone_lookup_accepts_spaced_and_raw_10_digits(self):
        from .views.helpers import normalize_indian_phone
        phone, err = normalize_indian_phone('98765 43210')
        self.assertEqual(phone, '9876543210')
        self.assertIsNone(err)
        phone2, err2 = normalize_indian_phone('+91 98765 43210')
        self.assertEqual(phone2, '9876543210')
        self.assertIsNone(err2)
        self.client.force_login(self.user)
        resp = self.client.get(reverse('client_phone_lookup'), {'phone': '98765 43210'})
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(resp.json().get('ok'))



class QuickAddProductCategoryTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(
            username="catuser",
            password="testpassword123",
            is_staff=True,
        )
        self.workspace = CompanyWorkspace.objects.create(
            name="Category Test Workspace",
            slug="cat-test-ws",
            owner=self.user,
        )
        self.user.current_workspace = self.workspace
        self.user.save()
        CompanyUserMembership.objects.create(user=self.user, workspace=self.workspace, role='ADMIN')
        apply_staff_access(self.user, ["dashboard", "inventory"])
        from .models import ItemCategory
        ItemCategory.objects.create(workspace=self.workspace, name="Smartphones")
        ItemCategory.objects.create(workspace=self.workspace, name="Accessories")

    def test_inventory_entry_renders_category_datalist(self):
        self.client.force_login(self.user)
        response = self.client.get(reverse('inventory_sales_dashboard'))
        self.assertEqual(response.status_code, 200)
        content = response.content.decode('utf-8')
        self.assertIn('id="quick-product-category"', content)
        self.assertIn('list="quick-product-category-datalist"', content)
        self.assertIn('id="quick-product-category-datalist"', content)
        self.assertIn('Smartphones', content)
        self.assertIn('Accessories', content)

    def test_quick_add_product_creates_and_returns_category(self):
        from .models import ItemCategory
        self.client.force_login(self.user)
        response = self.client.post(reverse('inventory_quick_add_product'), {
            'name': 'iPhone 15 Pro',
            'sku': 'IPH15PRO-128',
            'category': 'Flagship Phones',
            'cost_price': '80000.00',
            'unit_price': '110000.00',
        })
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertTrue(data.get('ok'))
        self.assertEqual(data['product']['category'], 'Flagship Phones')
        self.assertTrue(
            ItemCategory.objects.filter(workspace=self.workspace, name='Flagship Phones').exists()
        )


class InventoryCostingAVCOTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="avcotester", password="password123")
        apply_staff_access(self.user, ['inventory'])
        self.workspace = CompanyWorkspace.objects.create(name="AVCO Test Shop", slug="avco-test-shop", owner=self.user)
        CompanyUserMembership.objects.create(
            workspace=self.workspace,
            user=self.user,
            role="admin",
            is_active=True
        )
        self.supplier = InventoryParty.objects.create(
            workspace=self.workspace,
            party_type="supplier",
            name="Mega Parts Supply"
        )
        self.product = Product.objects.create(
            workspace=self.workspace,
            name="500GB NVMe SSD",
            sku="SSD-500-NVME",
            cost_price=Decimal("0.00"),
            unit_price=Decimal("0.00"),
            stock_quantity=0
        )

    def test_first_purchase_sets_initial_cost_and_auto_markup(self):
        from .views.helpers import _record_inventory_entries
        request = RequestFactory().post('/')
        request.user = self.user

        line_items = [{
            'product': self.product,
            'quantity': 10,
            'unit_price': Decimal("1000.00"),
            'discount_amount': Decimal("0.00"),
            'gst_rate': Decimal("0.00"),
            'notes': 'Initial batch',
            'serials': []
        }]

        _record_inventory_entries(
            request=request,
            entry_type='purchase',
            entry_date=timezone.localdate(),
            party=self.supplier,
            invoice_number='INV-PUR-001',
            line_items=line_items,
        )

        self.product.refresh_from_db()
        self.assertEqual(self.product.stock_quantity, 10)
        self.assertEqual(self.product.cost_price, Decimal("1000.00"))
        self.assertEqual(self.product.last_purchase_price, Decimal("1000.00"))
        # Automatic Option B markup: 30% default when selling price was 0
        self.assertEqual(self.product.unit_price, Decimal("1300.00"))

    def test_second_purchase_weighted_average_cost(self):
        from .views.helpers import _record_inventory_entries
        # Set existing stock: 6 units @ 1000.00, selling @ 1400.00 (40% markup)
        self.product.stock_quantity = 6
        self.product.cost_price = Decimal("1000.00")
        self.product.unit_price = Decimal("1400.00")
        self.product.save()

        request = RequestFactory().post('/')
        request.user = self.user

        # Buy 4 units @ 1300.00
        line_items = [{
            'product': self.product,
            'quantity': 4,
            'unit_price': Decimal("1300.00"),
            'discount_amount': Decimal("0.00"),
            'gst_rate': Decimal("0.00"),
            'notes': 'Restock batch',
            'serials': []
        }]

        _record_inventory_entries(
            request=request,
            entry_type='purchase',
            entry_date=timezone.localdate(),
            party=self.supplier,
            invoice_number='INV-PUR-002',
            line_items=line_items,
        )

        self.product.refresh_from_db()
        self.assertEqual(self.product.stock_quantity, 10)
        # Expected AVCO: (6 * 1000 + 4 * 1300) / 10 = (6000 + 5200) / 10 = 1120.00
        self.assertEqual(self.product.cost_price, Decimal("1120.00"))
        self.assertEqual(self.product.last_purchase_price, Decimal("1300.00"))
        # Selling price was 1400. Margin against new avg cost 1120 is 20% (>= 15%), and 1400 > 1300, so remains healthy
        self.assertEqual(self.product.unit_price, Decimal("1400.00"))

    def test_price_spike_exceeding_selling_price_auto_bumps_selling_price(self):
        from .views.helpers import _record_inventory_entries
        # Existing stock: 5 units @ 1000.00, selling @ 1200.00
        self.product.stock_quantity = 5
        self.product.cost_price = Decimal("1000.00")
        self.product.unit_price = Decimal("1200.00")
        self.product.save()

        request = RequestFactory().post('/')
        request.user = self.user

        # Supplier spikes price to 1500.00 (which exceeds current selling price 1200.00!)
        line_items = [{
            'product': self.product,
            'quantity': 5,
            'unit_price': Decimal("1500.00"),
            'discount_amount': Decimal("0.00"),
            'gst_rate': Decimal("0.00"),
            'notes': 'Spike batch',
            'serials': []
        }]

        _record_inventory_entries(
            request=request,
            entry_type='purchase',
            entry_date=timezone.localdate(),
            party=self.supplier,
            invoice_number='INV-PUR-003',
            line_items=line_items,
        )

        self.product.refresh_from_db()
        # AVCO = (5 * 1000 + 5 * 1500) / 10 = 1250.00
        self.assertEqual(self.product.cost_price, Decimal("1250.00"))
        self.assertEqual(self.product.last_purchase_price, Decimal("1500.00"))
        # Option B Auto-protection: Selling price MUST be bumped above purchase rate!
        self.assertGreater(self.product.unit_price, Decimal("1500.00"))

    def test_price_drop_lowers_avco_without_reducing_selling_price(self):
        from .views.helpers import _record_inventory_entries
        # Existing stock: 10 units @ 1000.00, selling @ 1500.00
        self.product.stock_quantity = 10
        self.product.cost_price = Decimal("1000.00")
        self.product.unit_price = Decimal("1500.00")
        self.product.save()

        request = RequestFactory().post('/')
        request.user = self.user

        # Price drops to 800.00
        line_items = [{
            'product': self.product,
            'quantity': 10,
            'unit_price': Decimal("800.00"),
            'discount_amount': Decimal("0.00"),
            'gst_rate': Decimal("0.00"),
            'notes': 'Discount batch',
            'serials': []
        }]

        _record_inventory_entries(
            request=request,
            entry_type='purchase',
            entry_date=timezone.localdate(),
            party=self.supplier,
            invoice_number='INV-PUR-004',
            line_items=line_items,
        )

        self.product.refresh_from_db()
        # AVCO = (10 * 1000 + 10 * 800) / 20 = 900.00
        self.assertEqual(self.product.cost_price, Decimal("900.00"))
        self.assertEqual(self.product.last_purchase_price, Decimal("800.00"))
        # Selling price is NOT reduced, protecting higher profit margin
        self.assertEqual(self.product.unit_price, Decimal("1500.00"))






