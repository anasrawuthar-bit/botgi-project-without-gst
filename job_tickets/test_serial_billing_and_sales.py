from decimal import Decimal
import json
from django.test import TestCase, Client
from django.urls import reverse
from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from dateutil.relativedelta import relativedelta
from django.utils import timezone

from job_tickets.models import (
    CompanyUserMembership,
    CompanyWorkspace,
    JobTicket,
    ServiceLog,
    ProductSale,
    Product,
    ProductItemSerial,
    InventoryEntry,
    InventoryParty,
    TechnicianProfile,
)

User = get_user_model()


class SerialBillingAndSalesTestCase(TestCase):
    def setUp(self):
        self.client = Client()
        self.staff_group, _ = Group.objects.get_or_create(name='Staff')

        self.staff_user = User.objects.create_user(
            username='staff_serial_tester',
            email='staff@serial.test',
            password='password123',
            is_staff=True,
        )
        self.staff_user.groups.add(self.staff_group)

        self.workspace = CompanyWorkspace.objects.create(
            name="Serial Test Hub",
            slug="serial-test-hub",
            status=CompanyWorkspace.STATUS_ACTIVE,
            owner=self.staff_user,
        )
        CompanyUserMembership.objects.create(
            workspace=self.workspace,
            user=self.staff_user,
            role=CompanyUserMembership.ROLE_STAFF,
            is_active=True,
        )

        self.technician = TechnicianProfile.objects.create(
            workspace=self.workspace,
            user=self.staff_user,
            unique_id='TS01',
        )

        self.job = JobTicket.objects.create(
            job_code='SER-202610-001',
            customer_name='Bob Smith',
            customer_phone='9876543210',
            device_type='Mobile',
            device_brand='Apple',
            device_model='iPhone 13',
            reported_issue='Screen replacement',
            workspace=self.workspace,
            status='Repairing',
            assigned_to=self.technician,
        )

        self.product = Product.objects.create(
            workspace=self.workspace,
            name='iPhone 13 OLED Display',
            sku='OLED-IPH13-001',
            unit_price=Decimal('4500.00'),
            cost_price=Decimal('3000.00'),
            stock_quantity=5,
            has_serial_tracking=True,
            customer_warranty_months=6,
            is_active=True,
        )

        self.item_serial = ProductItemSerial.objects.create(
            workspace=self.workspace,
            product=self.product,
            serial_number='SN-IP13-DISP-001',
            status=ProductItemSerial.STATUS_IN_STOCK,
        )

        self.client.login(username='staff_serial_tester', password='password123')
        session = self.client.session
        session['active_company_workspace_id'] = self.workspace.id
        session.save()

    def test_job_billing_staff_serial_context_and_consumption(self):
        url = reverse('job_billing_staff', args=[self.job.job_code])

        # 1. GET request: context should provide serial tracking mappings
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)

        serials_by_num = json.loads(response.context['available_serials_by_number_json'])
        self.assertIn('SN-IP13-DISP-001', serials_by_num)
        self.assertEqual(serials_by_num['SN-IP13-DISP-001']['product_id'], self.product.id)

        # 2. POST request: bill the job with serialized product & assigned serial
        post_data = {
            'update_amounts_submit': '1',
            'action': 'save_and_close',
            'product_id[]': [str(self.product.id)],
            'product_qty[]': ['1'],
            'product_unit_price[]': ['4500.00'],
            'product_serial[]': ['SN-IP13-DISP-001'],
            'payment_received': ['4500.00'],
            'payment_method': ['cash'],
            'notes': ['Installed replacement display with serial'],
        }
        res_post = self.client.post(url, post_data)
        self.assertIn(res_post.status_code, [200, 302])

        # Verify serial status transitioned to consumed_in_job
        self.item_serial.refresh_from_db()
        self.assertEqual(self.item_serial.status, ProductItemSerial.STATUS_CONSUMED_IN_JOB)
        self.assertEqual(self.item_serial.job_ticket_id, self.job.id)
        self.assertEqual(self.item_serial.sale_price, Decimal('4500.00'))
        self.assertIsNotNone(self.item_serial.customer_warranty_until)
        self.assertIsNotNone(self.item_serial.sale_entry)

        # Verify job closure via close_job
        close_url = reverse('close_job', args=[self.job.job_code])
        close_res = self.client.post(close_url, {
            'payment_status': 'paid',
            'payment_method': 'cash',
        })
        self.assertIn(close_res.status_code, [200, 302])
        self.job.refresh_from_db()
        self.assertEqual(self.job.status, 'Closed')

    def test_job_billing_serial_released_when_service_deleted(self):
        # Pre-consume the serial in the job
        self.item_serial.status = ProductItemSerial.STATUS_CONSUMED_IN_JOB
        self.item_serial.job_ticket = self.job
        self.item_serial.sale_price = Decimal('4500.00')
        self.item_serial.save()

        # Create corresponding ServiceLog and ProductSale
        service_log = ServiceLog.objects.create(
            job_ticket=self.job,
            description=f"Part: {self.product.name}",
            part_cost=Decimal('4500.00'),
            service_charge=Decimal('0.00'),
        )
        ProductSale.objects.create(
            job_ticket=self.job,
            product=self.product,
            quantity=1,
            unit_price=Decimal('4500.00'),
            cost_price=Decimal('3000.00'),
            line_total=Decimal('4500.00'),
            line_cost=Decimal('3000.00'),
            line_profit=Decimal('1500.00'),
            service_log=service_log,
            workspace=self.workspace,
        )

        url = reverse('job_billing_staff', args=[self.job.job_code])
        post_data = {
            'update_amounts_submit': '1',
            'action': 'save_only',
            'delete_service_ids[]': [str(service_log.id)],
            'payment_received': ['0.00'],
            'payment_method': ['cash'],
        }
        res_post = self.client.post(url, post_data)
        self.assertIn(res_post.status_code, [200, 302])

        # Verify serial was released back to in_stock
        self.item_serial.refresh_from_db()
        self.assertEqual(self.item_serial.status, ProductItemSerial.STATUS_IN_STOCK)
        self.assertIsNone(self.item_serial.job_ticket)
        self.assertIsNone(self.item_serial.sale_entry)

    def test_inventory_sale_serial_first_context_and_sale(self):
        party = InventoryParty.objects.create(
            workspace=self.workspace,
            name="Walk-in Customer",
            party_type='customer',
        )

        url = reverse('inventory_sales_dashboard')
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)

        # Context has available_serials_by_number_json
        serials_by_num = json.loads(response.context['available_serials_by_number_json'])
        self.assertIn('SN-IP13-DISP-001', serials_by_num)
        self.assertEqual(serials_by_num['SN-IP13-DISP-001']['product_id'], self.product.id)

        # Record a sale with this serial number
        post_data = {
            'inventory_entry_submit': 'sale',
            'party': str(party.id),
            'entry_date': timezone.localdate().isoformat(),
            'line_product_id[]': [str(self.product.id)],
            'line_quantity[]': ['1'],
            'line_unit_price[]': ['4500.00'],
            'line_gst_rate[]': ['18.00'],
            'line_serials[]': ['SN-IP13-DISP-001'],
            'bill_discount_amount': ['0.00'],
            'bill_payment_status': 'paid',
            'bill_payment_amount': ['4500.00'],
            'bill_payment_method': 'cash',
        }
        res_post = self.client.post(url, post_data)
        self.assertIn(res_post.status_code, [200, 302])

        # Verify serial became sold with customer warranty
        self.item_serial.refresh_from_db()
        self.assertEqual(self.item_serial.status, ProductItemSerial.STATUS_SOLD)
        self.assertEqual(self.item_serial.sale_price, Decimal('4500.00'))
        self.assertIsNotNone(self.item_serial.sale_entry)
        self.assertIsNotNone(self.item_serial.customer_warranty_until)
