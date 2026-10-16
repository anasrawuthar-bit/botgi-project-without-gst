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

    def test_sales_invoice_print_view_and_layout(self):
        from job_tickets.models import InventoryBill, InventoryCreditPayment, CompanyProfile
        from job_tickets.views.helpers import num_to_indian_words

        # Test Indian Number to Words converter
        self.assertEqual(num_to_indian_words(Decimal('17600.00')), 'Seventeen Thousand Six Hundred Rupees Only')
        self.assertEqual(num_to_indian_words(Decimal('0.00')), 'Zero Rupees Only')
        self.assertEqual(num_to_indian_words(Decimal('10050.50')), 'Ten Thousand Fifty Rupees and Fifty Paise Only')

        # Set workspace company profile
        CompanyProfile.objects.update_or_create(
            workspace=self.workspace,
            defaults={
                'company_name': 'Botgi Tech Hub',
                'address': '123 Electronics Street',
                'phone1': '+919876543210',
                'upi_id': 'botgi@upi',
            }
        )

        party = InventoryParty.objects.create(
            workspace=self.workspace,
            name="Alice Customer",
            phone="9876543211",
            party_type='customer',
        )

        bill = InventoryBill.objects.create(
            workspace=self.workspace,
            bill_number='SB-202610-001',
            invoice_number='INV-202610-001',
            entry_type='sale',
            entry_date=timezone.localdate(),
            party=party,
            created_by=self.staff_user,
        )

        entry1 = InventoryEntry.objects.create(
            workspace=self.workspace,
            bill=bill,
            entry_number='SE-001',
            entry_type='sale',
            entry_date=timezone.localdate(),
            party=party,
            product=self.product,
            quantity=1,
            unit_price=Decimal('4500.00'),
            total_amount=Decimal('4500.00'),
            serial_numbers_text='SN-IP13-DISP-001',
            created_by=self.staff_user,
        )

        # Link serial
        self.item_serial.status = ProductItemSerial.STATUS_SOLD
        self.item_serial.sale_entry = entry1
        self.item_serial.customer_warranty_until = timezone.localdate() + relativedelta(months=6)
        self.item_serial.save()

        # Product 2
        product2 = Product.objects.create(
            workspace=self.workspace,
            name='27-inch LED Monitor',
            sku='MON-27-001',
            unit_price=Decimal('13100.00'),
            cost_price=Decimal('10000.00'),
            stock_quantity=2,
            has_serial_tracking=True,
            customer_warranty_months=12,
            is_active=True,
        )
        entry2 = InventoryEntry.objects.create(
            workspace=self.workspace,
            bill=bill,
            entry_number='SE-002',
            entry_type='sale',
            entry_date=timezone.localdate(),
            party=party,
            product=product2,
            quantity=1,
            unit_price=Decimal('13100.00'),
            total_amount=Decimal('13100.00'),
            serial_numbers_text='SN-MON-9988',
            created_by=self.staff_user,
        )
        ProductItemSerial.objects.create(
            workspace=self.workspace,
            product=product2,
            serial_number='SN-MON-9988',
            status=ProductItemSerial.STATUS_SOLD,
            sale_entry=entry2,
            customer_warranty_until=timezone.localdate() + relativedelta(months=12),
        )

        # Record payment for the bill
        InventoryCreditPayment.objects.create(
            workspace=self.workspace,
            party=party,
            bill=bill,
            direction=InventoryCreditPayment.DIRECTION_RECEIVABLE,
            payment_date=timezone.localdate(),
            payment_method='upi',
            amount=Decimal('17600.00'),
            balance_before=Decimal('17600.00'),
            balance_after=Decimal('0.00'),
            created_by=self.staff_user,
        )

        url = reverse('inventory_sales_print_bill_view', args=[bill.id])
        res = self.client.get(url)
        self.assertEqual(res.status_code, 200)

        # Verify context variables
        ctx = res.context
        self.assertEqual(ctx['total_quantity'], 2)
        self.assertEqual(ctx['grand_total'], Decimal('17600.00'))
        self.assertEqual(ctx['amount_paid'], Decimal('17600.00'))
        self.assertEqual(ctx['balance_due'], Decimal('0.00'))
        self.assertEqual(ctx['payment_status'], 'PAID')
        self.assertEqual(ctx['amount_in_words'], 'Seventeen Thousand Six Hundred Rupees Only')
        self.assertEqual(ctx['company'].company_name, 'Botgi Tech Hub')

        html = res.content.decode('utf-8')

        # 1. Verify "Invoice Summary" is NOT in HTML as user requested
        self.assertNotIn('Invoice Summary', html)

        # 2. Verify total quantity is in the table footer under Qty column
        self.assertIn('Total Qty:', html)
        self.assertIn('tfoot-qty-val', html)
        self.assertIn('>2</td>', html)
        self.assertIn('Grand Total:', html)
        self.assertIn('17600.00', html)

        # 3. Verify Serial Number and warranty badge
        self.assertIn('SN-IP13-DISP-001', html)
        self.assertIn('SN-MON-9988', html)
        self.assertIn('Warranty until', html)

        # 4. Verify Amount in Words
        self.assertIn('Seventeen Thousand Six Hundred Rupees Only', html)

        # 5. Verify Customer Details and Payment Status
        self.assertIn('Alice Customer', html)
        self.assertIn('PAID', html)

        # 6. Verify Terms & Conditions and Customer Signature are omitted
        self.assertNotIn('Terms &amp; Conditions', html)
        self.assertNotIn('Customer Signature', html)
        self.assertIn('Authorized Signatory', html)
        self.assertIn('For Botgi Tech Hub', html)

    def test_technician_product_and_serial_selection(self):
        # Create technician user
        tech_group, _ = Group.objects.get_or_create(name='Technicians')
        tech_user = User.objects.create_user(
            username='tech_specialist',
            email='tech@specialist.test',
            password='password123',
        )
        tech_user.groups.add(tech_group)
        CompanyUserMembership.objects.create(
            workspace=self.workspace,
            user=tech_user,
            role=CompanyUserMembership.ROLE_TECHNICIAN,
            is_active=True,
        )
        self.technician.user = tech_user
        self.technician.save()
        self.job.assigned_to = self.technician
        self.job.status = 'Repairing'
        self.job.save()

        # Login as technician
        self.client.login(username='tech_specialist', password='password123')
        session = self.client.session
        session['active_company_workspace_id'] = self.workspace.id
        session.save()

        url = reverse('job_detail_technician', args=[self.job.job_code])

        # 1. GET request: context must have products_catalog_json and available_serials_by_number_json
        res_get = self.client.get(url)
        self.assertEqual(res_get.status_code, 200)
        self.assertIn('products_catalog_json', res_get.context)
        self.assertIn('available_serials_by_number_json', res_get.context)

        # 2. POST add_service_log with serialized product
        initial_stock = self.product.stock_quantity
        post_data = {
            'action': 'add_service_log',
            'product_id': str(self.product.id),
            'product_serial': 'SN-IP13-DISP-001',
            'quantity': '1',
            'description': 'Replaced display assembly',
            'part_cost': '4500.00',
            'service_charge': '500.00',
        }
        res_post = self.client.post(url, post_data, HTTP_X_REQUESTED_WITH='XMLHttpRequest')
        self.assertEqual(res_post.status_code, 200)
        data = res_post.json()
        self.assertTrue(data['ok'])

        # Verify stock decremented
        self.product.refresh_from_db()
        self.assertEqual(self.product.stock_quantity, initial_stock - 1)

        # Verify serial consumed
        self.item_serial.refresh_from_db()
        self.assertEqual(self.item_serial.status, ProductItemSerial.STATUS_CONSUMED_IN_JOB)
        self.assertEqual(self.item_serial.job_ticket_id, self.job.id)
        self.assertEqual(self.item_serial.sale_price, Decimal('4500.00'))
        self.assertIsNotNone(self.item_serial.customer_warranty_until)

        # Verify ProductSale record created
        sale = ProductSale.objects.filter(job_ticket=self.job, product=self.product).first()
        self.assertIsNotNone(sale)
        self.assertEqual(sale.quantity, 1)
        self.assertEqual(sale.unit_price, Decimal('4500.00'))
        created_log = sale.service_log

        # 3. Technician deletes the service log
        del_url = reverse('technician_delete_service_log', args=[created_log.id])
        res_del = self.client.post(del_url, HTTP_X_REQUESTED_WITH='XMLHttpRequest')
        self.assertEqual(res_del.status_code, 200)
        del_data = res_del.json()
        self.assertTrue(del_data['ok'])

        # Verify stock restored
        self.product.refresh_from_db()
        self.assertEqual(self.product.stock_quantity, initial_stock)

        # Verify serial released back to in_stock
        self.item_serial.refresh_from_db()
        self.assertEqual(self.item_serial.status, ProductItemSerial.STATUS_IN_STOCK)
        self.assertIsNone(self.item_serial.job_ticket)


