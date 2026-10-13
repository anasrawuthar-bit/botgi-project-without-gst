import json
from decimal import Decimal
from django.contrib.auth.models import User
from django.test import TestCase, Client
from django.urls import reverse

from job_tickets.access_control import apply_staff_access
from job_tickets.models import CompanyWorkspace, CompanyUserMembership, JobTicket, JobTicketLog


class OldJobIntakeTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(
            username='service_agent',
            password='Password123!',
            is_staff=True,
        )
        apply_staff_access(self.user, {'staff_dashboard'})
        self.workspace = CompanyWorkspace.objects.create(name='Main Service Center', owner=self.user)
        CompanyUserMembership.objects.create(
            workspace=self.workspace,
            user=self.user,
            role=CompanyUserMembership.ROLE_ADMIN,
        )
        self.client = Client()
        self.client.login(username='service_agent', password='Password123!')

        # Create an existing past job ticket
        self.old_job1 = JobTicket.objects.create(
            workspace=self.workspace,
            job_code='JT-1001',
            customer_name='Anas Rawuthar',
            customer_phone='9876543210',
            device_type='Laptop',
            device_brand='Dell',
            device_model='Latitude 5420',
            device_serial='SN-DELL-5420',
            device_password='1234',
            reported_issue='Keyboard not responding',
            additional_items='Charger',
            status='Completed',
            created_by=self.user,
        )

    def test_job_ticket_lookup_by_job_code(self):
        """Verify looking up old tickets by code with prefix, numeric, or hash format."""
        lookup_url = reverse('job_ticket_lookup')

        # 1. Exact code lookup
        res = self.client.get(lookup_url, {'q': 'JT-1001'})
        self.assertEqual(res.status_code, 200)
        data = res.json()
        self.assertTrue(data.get('ok'))
        self.assertTrue(data.get('found'))
        self.assertEqual(data.get('matched_job_code'), 'JT-1001')
        self.assertEqual(data.get('customer_name'), 'Anas Rawuthar')
        self.assertEqual(data.get('customer_phone'), '9876543210')
        self.assertEqual(data['primary_device']['device_brand'], 'Dell')
        self.assertEqual(data['primary_device']['device_model'], 'Latitude 5420')
        self.assertFalse(data.get('is_batch'))

        # 2. Number only lookup (e.g., 1001)
        res_num = self.client.get(lookup_url, {'q': '1001'})
        self.assertEqual(res_num.status_code, 200)
        data_num = res_num.json()
        self.assertTrue(data_num.get('found'))
        self.assertEqual(data_num.get('matched_job_code'), 'JT-1001')

        # 3. Hash prefix lookup (e.g., #1001)
        res_hash = self.client.get(lookup_url, {'q': '#1001'})
        self.assertEqual(res_hash.status_code, 200)
        data_hash = res_hash.json()
        self.assertTrue(data_hash.get('found'))
        self.assertEqual(data_hash.get('matched_job_code'), 'JT-1001')

    def test_job_ticket_lookup_by_serial(self):
        """Verify looking up an old ticket by device serial number."""
        lookup_url = reverse('job_ticket_lookup')
        res = self.client.get(lookup_url, {'q': 'SN-DELL-5420'})
        self.assertEqual(res.status_code, 200)
        data = res.json()
        self.assertTrue(data.get('found'))
        self.assertEqual(data.get('matched_job_code'), 'JT-1001')
        self.assertEqual(data['primary_device']['device_serial'], 'SN-DELL-5420')

    def test_job_ticket_lookup_batch_devices(self):
        """Verify that when an old ticket was part of a batch, all devices are returned."""
        batch_id = 'BATCH-GROUP-999'
        self.old_job1.customer_group_id = batch_id
        self.old_job1.save()

        # Create 2nd device in the same batch
        JobTicket.objects.create(
            workspace=self.workspace,
            job_code='JT-1002',
            customer_name='Anas Rawuthar',
            customer_phone='9876543210',
            device_type='Printer',
            device_brand='HP',
            device_model='LaserJet 1020',
            device_serial='SN-HP-1020',
            reported_issue='Paper jam',
            customer_group_id=batch_id,
            status='Completed',
            created_by=self.user,
        )

        lookup_url = reverse('job_ticket_lookup')
        res = self.client.get(lookup_url, {'q': '1001'})
        self.assertEqual(res.status_code, 200)
        data = res.json()
        self.assertTrue(data.get('found'))
        self.assertTrue(data.get('is_batch'))
        self.assertEqual(len(data.get('batch_devices', [])), 2)
        device_codes = [d['job_code'] for d in data['batch_devices']]
        self.assertIn('JT-1001', device_codes)
        self.assertIn('JT-1002', device_codes)

    def test_job_ticket_lookup_not_found(self):
        """Verify response when non-existent job code or serial is queried."""
        lookup_url = reverse('job_ticket_lookup')
        res = self.client.get(lookup_url, {'q': 'NONEXISTENT999'})
        self.assertEqual(res.status_code, 200)
        data = res.json()
        self.assertTrue(data.get('ok'))
        self.assertFalse(data.get('found'))

    def test_create_new_job_linked_to_old_job(self):
        """Verify creating a new job ticket properly sets original_job_ticket and logs it."""
        url = reverse('staff_dashboard')
        data = {
            'job_ticket_form_submit': 'true',
            'customer_name': 'Anas Rawuthar',
            'customer_phone': '9876543210',
            'device_forms[0].device_type': 'Laptop',
            'device_forms[0].device_brand': 'Dell',
            'device_forms[0].device_model': 'Latitude 5420',
            'device_forms[0].device_serial': 'SN-DELL-5420',
            'device_forms[0].reported_issue': 'Battery draining fast (Repeat visit)',
            'device_forms[0].original_job_code': 'JT-1001',
            'estimated_amount': '2200.00',
        }
        res = self.client.post(url, data, HTTP_X_REQUESTED_WITH='XMLHttpRequest')
        self.assertEqual(res.status_code, 200)
        json_data = res.json()
        self.assertTrue(json_data.get('success'))

        new_job_code = json_data['jobs'][0]['job_code']
        new_job = JobTicket.objects.get(job_code=new_job_code)

        # Verify foreign key link
        self.assertIsNotNone(new_job.original_job_ticket)
        self.assertEqual(new_job.original_job_ticket.job_code, 'JT-1001')

        # Verify log entry
        log_entry = JobTicketLog.objects.filter(job_ticket=new_job, action='CREATED').first()
        self.assertIsNotNone(log_entry)
        self.assertIn('Linked to previous ticket #JT-1001', log_entry.details)

        # Verify detail page displays old job reference
        detail_res = self.client.get(reverse('staff_job_detail', kwargs={'job_code': new_job.job_code}))
        self.assertEqual(detail_res.status_code, 200)
        self.assertContains(detail_res, 'Prev: #JT-1001')
        self.assertContains(detail_res, 'Repeat Service from Ticket #JT-1001')

        # Verify print receipt displays old job reference
        receipt_res = self.client.get(reverse('job_creation_receipt_print', kwargs={'job_code': new_job.job_code}))
        self.assertEqual(receipt_res.status_code, 200)
        self.assertContains(receipt_res, 'Prev Job #:')
        self.assertContains(receipt_res, 'JT-1001')

        # Verify billing print displays old job reference
        billing_res = self.client.get(reverse('job_billing_print_view', kwargs={'job_code': new_job.job_code}))
        self.assertEqual(billing_res.status_code, 200)
        self.assertContains(billing_res, 'Prev Ticket:')
        self.assertContains(billing_res, 'JT-1001')

    def test_multi_device_intake_independent_old_job_links(self):
        """Verify multi-device intake where Device 1 is linked to an old job and Device 2 is brand new."""
        url = reverse('staff_dashboard')
        data = {
            'job_ticket_form_submit': 'true',
            'customer_name': 'Anas Rawuthar',
            'customer_phone': '9876543210',
            # Device 0: linked to JT-1001
            'device_forms[0].device_type': 'Laptop',
            'device_forms[0].device_brand': 'Dell',
            'device_forms[0].device_model': 'Latitude 5420',
            'device_forms[0].reported_issue': 'Trackpad broken',
            'device_forms[0].original_job_code': 'JT-1001',
            # Device 1: brand new device, unlinked
            'device_forms[1].device_type': 'Tablet',
            'device_forms[1].device_brand': 'Apple',
            'device_forms[1].device_model': 'iPad Pro 11',
            'device_forms[1].reported_issue': 'Cracked glass',
            'device_forms[1].original_job_code': '',
        }
        res = self.client.post(url, data, HTTP_X_REQUESTED_WITH='XMLHttpRequest')
        self.assertEqual(res.status_code, 200)
        json_data = res.json()
        self.assertTrue(json_data.get('success'))
        self.assertEqual(len(json_data['jobs']), 2)

        job0 = JobTicket.objects.get(job_code=json_data['jobs'][0]['job_code'])
        job1 = JobTicket.objects.get(job_code=json_data['jobs'][1]['job_code'])

        self.assertIsNotNone(job0.original_job_ticket)
        self.assertEqual(job0.original_job_ticket.job_code, 'JT-1001')
        self.assertIsNone(job1.original_job_ticket)

        # Batch receipt print should show both and list prev job for job0
        receipt_res = self.client.get(reverse('job_creation_receipt_print', kwargs={'job_code': job0.job_code}))
        self.assertEqual(receipt_res.status_code, 200)
        self.assertContains(receipt_res, 'Prev Job')
        self.assertContains(receipt_res, 'JT-1001')
