import json
from datetime import timedelta
from django.contrib.auth.models import User
from django.test import Client, TestCase
from django.urls import reverse
from django.utils import timezone

from job_tickets.models import (
    CompanyProfile,
    CompanyUserMembership,
    CompanyWorkspace,
    PlatformSettings,
    PlatformSubscriptionPlan,
)


class SuperAdminSeparationAndFeaturesTestCase(TestCase):
    def setUp(self):
        self.client = Client()

        # 1. Create a Super Admin account (email + username)
        self.superadmin_password = "SuperPassword123!"
        self.superadmin = User.objects.create_superuser(
            username="admin_boss",
            email="superadmin@zyndesk.com",
            password=self.superadmin_password,
        )

        # 2. Create a standard staff/technician account
        self.staff_password = "StaffPassword123!"
        self.staff_user = User.objects.create_user(
            username="tech_john",
            email="john@repairshop.com",
            password=self.staff_password,
            is_staff=True,
            is_superuser=False,
        )

        # 3. Create a test workspace & profile
        self.workspace = CompanyWorkspace.objects.create(
            name="Apex Repairs",
            slug="apex",
            owner=self.staff_user,
            status=CompanyWorkspace.STATUS_ACTIVE,
            plan_name="Starter",
            plan_expires_at=timezone.now() + timedelta(days=15),
        )
        self.profile = CompanyProfile.objects.filter(workspace=self.workspace).first()
        if not self.profile:
            self.profile = CompanyProfile.objects.filter(workspace__isnull=True).first()
            if self.profile:
                self.profile.workspace = self.workspace
                self.profile.company_name = "Apex Repairs Pvt"
                self.profile.email = "owner@apexrepairs.com"
                self.profile.save()
            else:
                from django.db import connection
                if connection.vendor == 'postgresql':
                    with connection.cursor() as cursor:
                        cursor.execute("SELECT setval(pg_get_serial_sequence('job_tickets_companyprofile', 'id'), coalesce(max(id), 1)) FROM job_tickets_companyprofile;")
                self.profile = CompanyProfile.objects.create(
                    workspace=self.workspace,
                    company_name="Apex Repairs Pvt",
                    email="owner@apexrepairs.com",
                )
        CompanyUserMembership.objects.create(
            workspace=self.workspace,
            user=self.staff_user,
            role="technician",
            is_active=True,
        )

    def test_superadmin_can_login_with_email_to_platform_admin(self):
        """Super Admin can log into /platform-admin/login/ using their email address"""
        login_url = reverse("platform_admin_login")
        response = self.client.post(
            login_url,
            {"email": "superadmin@zyndesk.com", "password": self.superadmin_password},
            follow=True,
        )
        self.assertEqual(response.status_code, 200)
        # Check that user is logged in and redirected to platform admin dashboard
        self.assertTrue(response.context["user"].is_authenticated)
        self.assertTrue(response.context["user"].is_superuser)
        self.assertTemplateUsed(response, "job_tickets/platform_admin.html")

    def test_superadmin_can_login_with_username_to_platform_admin(self):
        """Super Admin can also log into /platform-admin/login/ using their username"""
        login_url = reverse("platform_admin_login")
        response = self.client.post(
            login_url,
            {"email": "admin_boss", "password": self.superadmin_password},
            follow=True,
        )
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.context["user"].is_authenticated)
        self.assertTemplateUsed(response, "job_tickets/platform_admin.html")

    def test_staff_user_cannot_login_to_platform_admin(self):
        """Non-superadmin staff user must be rejected from /platform-admin/login/"""
        login_url = reverse("platform_admin_login")
        response = self.client.post(
            login_url,
            {"email": "john@repairshop.com", "password": self.staff_password},
            follow=True,
        )
        self.assertEqual(response.status_code, 200)
        # Should not be authenticated on platform admin
        self.assertFalse(response.context["user"].is_authenticated)
        # Should display restriction error
        error = response.context.get("error", "")
        self.assertIn("Access Restricted", error)

    def test_superadmin_cannot_login_to_staff_panel(self):
        """Super Admin credentials must NOT be allowed to log into standard /login/ and must not disclose superadmin info"""
        staff_login_url = reverse("login")

        # 1. Ensure GET request to /login/ does not leak Super Admin links or text
        get_response = self.client.get(staff_login_url)
        self.assertNotContains(get_response, "Super Admin")
        self.assertNotContains(get_response, "platform-admin")

        # 2. Ensure POST attempt fails cleanly without leaking superadmin status
        response = self.client.post(
            staff_login_url,
            {"username": "superadmin@zyndesk.com", "password": self.superadmin_password},
            follow=True,
        )
        self.assertEqual(response.status_code, 200)
        # Must not be authenticated as staff
        self.assertFalse(response.context["user"].is_authenticated)
        # Error must be generic without disclosing superadmin existence
        error = response.context.get("error", "")
        self.assertIn("Invalid username or password", error)
        self.assertNotContains(response, "Super Admin")
        self.assertNotContains(response, "platform-admin")

    def test_superadmin_extend_workspace_plan(self):
        """Super Admin can extend a company's plan expiry date"""
        self.client.login(username="admin_boss", password=self.superadmin_password)
        old_expiry = self.workspace.plan_expires_at

        extend_url = reverse("platform_admin_extend_plan", args=[self.workspace.id])
        response = self.client.post(
            extend_url,
            {"extension_mode": "days", "days": "30"},
            follow=True,
        )
        self.assertEqual(response.status_code, 200)

        self.workspace.refresh_from_db()
        self.assertGreater(self.workspace.plan_expires_at, old_expiry)

    def test_superadmin_update_platform_settings(self):
        """Super Admin can update platform SMTP, WhatsApp, OTP, and Payment settings"""
        self.client.login(username="admin_boss", password=self.superadmin_password)

        update_url = reverse("platform_admin_update_settings")
        payload = {
            "section": "smtp",
            "smtp_host": "smtp.sendgrid.net",
            "smtp_port": "587",
            "smtp_user": "apikey",
            "smtp_from_email": "notifications@zyndesk.com",
            "smtp_use_tls": "on",
        }
        response = self.client.post(update_url, payload, follow=True)
        self.assertEqual(response.status_code, 200)

        settings_obj = PlatformSettings.get_settings()
        self.assertEqual(settings_obj.smtp_host, "smtp.sendgrid.net")
        self.assertEqual(settings_obj.smtp_port, 587)
        self.assertEqual(settings_obj.smtp_user, "apikey")
        self.assertEqual(settings_obj.smtp_from_email, "notifications@zyndesk.com")
        self.assertTrue(settings_obj.smtp_use_tls)

    def test_superadmin_crud_platform_plan(self):
        """Super Admin can create/update subscription plans"""
        self.client.login(username="admin_boss", password=self.superadmin_password)

        save_plan_url = reverse("platform_admin_save_plan")
        response = self.client.post(
            save_plan_url,
            {
                "name": "Ultra VIP Plan",
                "slug": "ultra-vip",
                "price": "4999.00",
                "billing_interval": "monthly",
                "max_staff": "50",
                "max_tickets_per_month": "5000",
                "storage_limit_mb": "10000",
                "features_text": "All Features\nVIP Support",
                "is_active": "on",
            },
            follow=True,
        )
        self.assertEqual(response.status_code, 200)
        plan = PlatformSubscriptionPlan.objects.filter(slug="ultra-vip").first()
        self.assertIsNotNone(plan)
        self.assertEqual(plan.name, "Ultra VIP Plan")
        self.assertEqual(float(plan.price), 4999.0)
