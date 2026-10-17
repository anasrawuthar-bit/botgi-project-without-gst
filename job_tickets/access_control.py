# job_tickets/access_control.py
from django.contrib.auth.models import Group, Permission
from django.contrib.contenttypes.models import ContentType

from .models import CompanyProfile

ACCESS_CONTROL_GROUP = "Access Control"

ACCESS_GROUPS = {
    "staff_dashboard": "Access: Staff Dashboard",
    "job_status_change": "Access: Change Job Status",
    "task_management": "Access: Task Management",
    "expense_management": "Access: Expense Management",
    "account_management": "Access: Bank & Financial Accounts",
    "team_management": "Access: Team Management",
    "inventory": "Access: Inventory Module",
    "feedback_analytics": "Access: Feedback Analytics",
    "company_settings": "Access: Company Settings",
    "reports_dashboard": "Access: Reports Dashboard",
    "reports_technician": "Access: Reports Technician Performance",
    "reports_vendor": "Access: Reports Vendor Performance",
    "reports_financial": "Access: Reports Financial Summary",
}

ACCESS_OPTIONS = [
    {"key": "staff_dashboard", "label": "Staff Dashboard", "section": "general"},
    {"key": "job_status_change", "label": "Change Job Status", "section": "general"},
    {"key": "task_management", "label": "Task Management", "section": "general"},
    {"key": "expense_management", "label": "Expense Management", "section": "general"},
    {"key": "account_management", "label": "Bank & Financial Accounts", "section": "general"},
    {"key": "team_management", "label": "Staff & Technician Management", "section": "general"},
    {"key": "inventory", "label": "Inventory Module", "section": "general"},
    {"key": "feedback_analytics", "label": "Customer Feedback Analytics", "section": "general"},
    {"key": "company_settings", "label": "Company Settings", "section": "general"},
    {"key": "reports_dashboard", "label": "Reports Dashboard", "section": "reports"},
    {"key": "reports_technician", "label": "Technicians Performance", "section": "reports"},
    {"key": "reports_vendor", "label": "Vendor Performance Report", "section": "reports"},
    {"key": "reports_financial", "label": "Financial Summary", "section": "reports"},
]

REPORT_SECTION_KEYS = {
    "reports_technician",
    "reports_vendor",
    "reports_financial",
}
REPORT_KEYS = {"reports_dashboard"} | REPORT_SECTION_KEYS


def ensure_access_groups():
    for name in [ACCESS_CONTROL_GROUP] + list(ACCESS_GROUPS.values()):
        Group.objects.get_or_create(name=name)


def normalize_access_keys(access_keys):
    keys = set(access_keys or [])
    if keys & REPORT_SECTION_KEYS:
        keys.add("reports_dashboard")
    return keys


def _get_financial_reports_permission():
    try:
        content_type = ContentType.objects.get_for_model(CompanyProfile)
        return Permission.objects.get(codename="view_financial_reports", content_type=content_type)
    except Permission.DoesNotExist:
        return None


def _sync_report_permission(user, access_keys):
    permission = _get_financial_reports_permission()
    if not permission:
        return
    if access_keys & REPORT_KEYS:
        user.user_permissions.add(permission)
    else:
        user.user_permissions.remove(permission)


def apply_staff_access(user, access_keys):
    ensure_access_groups()
    access_keys = normalize_access_keys(access_keys)

    access_group_names = list(ACCESS_GROUPS.values())
    selected_group_names = [ACCESS_GROUPS[key] for key in access_keys if key in ACCESS_GROUPS]

    control_group = Group.objects.filter(name=ACCESS_CONTROL_GROUP).first()
    if control_group:
        user.groups.add(control_group)

    access_groups = Group.objects.filter(name__in=access_group_names)
    if access_groups.exists():
        user.groups.remove(*access_groups.exclude(name__in=selected_group_names))

    if selected_group_names:
        selected_groups = Group.objects.filter(name__in=selected_group_names)
        if selected_groups.exists():
            user.groups.add(*selected_groups)

    _sync_report_permission(user, access_keys)


def clear_staff_access(user):
    ensure_access_groups()
    group_names = list(ACCESS_GROUPS.values()) + [ACCESS_CONTROL_GROUP]
    groups = Group.objects.filter(name__in=group_names)
    if groups.exists():
        user.groups.remove(*groups)
    _sync_report_permission(user, set())


def parse_access_keys(post_data):
    keys = {key for key in ACCESS_GROUPS if post_data.get(f"access_{key}")}
    return normalize_access_keys(keys)


def get_staff_access(user, group_names=None):
    access = {key: False for key in ACCESS_GROUPS}
    access["controlled"] = False
    access["reports_overview"] = False

    if not user or not getattr(user, "is_authenticated", False):
        return access

    if getattr(user, "is_superuser", False):
        # Super Admin is strictly isolated from tenant staff roles
        for key in ACCESS_GROUPS:
            access[key] = False
        access["controlled"] = True
        access["reports_overview"] = False
        return access

    if not getattr(user, "is_staff", False):
        return access

    if group_names is None:
        group_names = set(user.groups.values_list("name", flat=True))
    else:
        group_names = set(group_names)

    access["controlled"] = ACCESS_CONTROL_GROUP in group_names

    if not access["controlled"]:
        for key in ACCESS_GROUPS:
            access[key] = True

        if not user.has_perm("job_tickets.view_financial_reports"):
            for key in REPORT_KEYS:
                access[key] = False
            access["reports_overview"] = False
        else:
            access["reports_overview"] = True
        return access

    for key, group_name in ACCESS_GROUPS.items():
        access[key] = group_name in group_names

    if access.get("company_settings"):
        access["account_management"] = True

    if access["reports_technician"] or access["reports_vendor"] or access["reports_financial"]:
        access["reports_dashboard"] = True

    access["reports_overview"] = access["reports_dashboard"]
    return access


def user_has_staff_access(user, key):
    return bool(get_staff_access(user).get(key))
