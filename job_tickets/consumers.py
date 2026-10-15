import json
from channels.generic.websocket import WebsocketConsumer
from asgiref.sync import async_to_sync
from .models import CompanyUserMembership, JobTicket, TechnicianProfile
from .whatsapp_service import verify_receipt_access_token

STAFF_GROUP = 'staff_dashboard_updates'
TECH_GROUP = 'tech_dashboard_updates'


def staff_group_name(workspace_id):
    return f'{STAFF_GROUP}_{workspace_id or "legacy"}'


def tech_group_name(workspace_id):
    return f'{TECH_GROUP}_{workspace_id or "legacy"}'


def job_group_name(workspace_id, job_code):
    return f'job_{workspace_id or "legacy"}_{job_code}'


TASK_ROOM_PREFIX = 'task_room'
STAFF_TASKS_PREFIX = 'staff_tasks'
TECH_TASKS_PREFIX = 'tech_tasks'


def task_room_group_name(workspace_id, task_id):
    return f'{TASK_ROOM_PREFIX}_{workspace_id or "legacy"}_{task_id}'


def staff_tasks_group_name(workspace_id):
    return f'{STAFF_TASKS_PREFIX}_{workspace_id or "legacy"}'


def tech_tasks_group_name(workspace_id, tech_id):
    return f'{TECH_TASKS_PREFIX}_{workspace_id or "legacy"}_{tech_id}'


def _authenticate_websocket_user(scope):
    """Resolve authenticated User from session or JWT token in query string."""
    user = scope.get('user')
    if user and getattr(user, 'is_authenticated', False):
        return user

    token = _query_token(scope)
    if token:
        from .views.helpers import decode_mobile_jwt
        from django.contrib.auth.models import User
        payload, error_msg = decode_mobile_jwt(token)
        if payload and not error_msg:
            user_id = payload.get('sub')
            try:
                user = User.objects.get(id=int(user_id), is_active=True)
                return user
            except (TypeError, ValueError, User.DoesNotExist):
                pass
    return None


def _active_membership(user, workspace_id):
    """Check if user has an active membership in the workspace.

    If workspace_id is None (legacy/single-tenant setup) or if the workspace
    does not have any CompanyUserMembership records configured yet, returns True
    for backward compatibility.
    """
    if not workspace_id:
        return True
    if not CompanyUserMembership.objects.filter(workspace_id=workspace_id).exists():
        return True
    return CompanyUserMembership.objects.filter(
        user=user,
        workspace_id=workspace_id,
        is_active=True,
    ).exists()



def _query_token(scope):
    query_string = (scope.get('query_string') or b'').decode('utf-8')
    for item in query_string.split('&'):
        key, _, value = item.partition('=')
        if key == 'token':
            return value
    return ''


def _query_value(scope, wanted_key):
    query_string = (scope.get('query_string') or b'').decode('utf-8')
    for item in query_string.split('&'):
        key, _, value = item.partition('=')
        if key == wanted_key:
            return value
    return ''

class JobStatusConsumer(WebsocketConsumer):
    # This consumer handles connections to specific jobs (e.g., client status page)
    
    def connect(self):
        self.job_code = self.scope['url_route']['kwargs']['job_code']
        try:
            self.job = JobTicket.objects.only('job_code', 'workspace_id', 'assigned_to_id').get(
                job_code=self.job_code
            )
        except JobTicket.DoesNotExist:
            self.close()
            return

        user = self.scope.get('user')
        is_staff = bool(user and user.is_authenticated and getattr(user, 'is_staff', False))
        technician = TechnicianProfile.objects.filter(user=user).first() if user and user.is_authenticated else None
        is_assigned_technician = bool(
            technician
            and technician.id == self.job.assigned_to_id
            and (
                not self.job.workspace_id
                or not technician.workspace_id
                or technician.workspace_id == self.job.workspace_id
                or _active_membership(user, self.job.workspace_id)
            )
        )
        has_customer_token = verify_receipt_access_token(self.job, _query_token(self.scope))
        if not (
            (is_staff and _active_membership(user, self.job.workspace_id))
            or is_assigned_technician
            or has_customer_token
            or (user and user.is_authenticated and user.is_superuser)
        ):
            self.close()
            return

        self.job_group_name = job_group_name(self.job.workspace_id, self.job_code)

        # Join the job-specific group
        async_to_sync(self.channel_layer.group_add)(
            self.job_group_name,
            self.channel_name
        )
        self.accept()

    def disconnect(self, close_code):
        # Leave the job-specific group
        async_to_sync(self.channel_layer.group_discard)(
            self.job_group_name,
            self.channel_name
        )

    def receive(self, text_data):
        pass # Not used for detail page listening

    # Receive message from job-specific group (sent from views.py)
    def job_status_update(self, event):
        # Send the entire event dictionary to the WebSocket
        self.send(text_data=json.dumps(event))


class StaffDashboardConsumer(WebsocketConsumer):
    # This consumer handles connections for the Staff Dashboard
    def connect(self):
        user = _authenticate_websocket_user(self.scope)
        if not user or not getattr(user, 'is_staff', False):
            self.close()
            return
        workspace_id = _query_value(self.scope, 'workspace')
        try:
            workspace_id = int(workspace_id) if workspace_id else None
        except ValueError:
            self.close()
            return
        if not workspace_id:
            memberships = CompanyUserMembership.objects.filter(user=user, is_active=True)
            workspace_id = memberships.values_list('workspace_id', flat=True).first()
        if workspace_id and not _active_membership(user, workspace_id) and not user.is_superuser:
            self.close()
            return
        self.workspace_id = workspace_id
        async_to_sync(self.channel_layer.group_add)(
            staff_group_name(self.workspace_id),
            self.channel_name
        )
        async_to_sync(self.channel_layer.group_add)(
            staff_tasks_group_name(self.workspace_id),
            self.channel_name
        )
        self.accept()

    def disconnect(self, close_code):
        if hasattr(self, 'workspace_id'):
            async_to_sync(self.channel_layer.group_discard)(
                staff_group_name(self.workspace_id),
                self.channel_name
            )
            async_to_sync(self.channel_layer.group_discard)(
                staff_tasks_group_name(self.workspace_id),
                self.channel_name
            )

    def receive(self, text_data):
        pass

    # Receive message from staff update group (sent from views.py)
    def job_status_update(self, event):
        self.send(text_data=json.dumps(event))

    def job_assigned(self, event):
        self.send(text_data=json.dumps(event))

    def task_feed_event(self, event):
        self.send(text_data=json.dumps(event))


class TechnicianDashboardConsumer(WebsocketConsumer):
    # This consumer handles connections for the Technician Dashboard
    def connect(self):
        user = _authenticate_websocket_user(self.scope)
        if not user:
            self.close()
            return
        technician = TechnicianProfile.objects.filter(user=user).first()
        if not technician:
            self.close()
            return
        if technician.workspace_id and not _active_membership(user, technician.workspace_id) and not user.is_superuser:
            self.close()
            return
        self.workspace_id = technician.workspace_id
        self.tech_id = technician.id
        async_to_sync(self.channel_layer.group_add)(
            tech_group_name(self.workspace_id),
            self.channel_name
        )
        async_to_sync(self.channel_layer.group_add)(
            tech_tasks_group_name(self.workspace_id, self.tech_id),
            self.channel_name
        )
        self.accept()

    def disconnect(self, close_code):
        if hasattr(self, 'workspace_id'):
            async_to_sync(self.channel_layer.group_discard)(
                tech_group_name(self.workspace_id),
                self.channel_name
            )
        if hasattr(self, 'tech_id') and hasattr(self, 'workspace_id'):
            async_to_sync(self.channel_layer.group_discard)(
                tech_tasks_group_name(self.workspace_id, self.tech_id),
                self.channel_name
            )
        
    def receive(self, text_data):
        pass

    # Receive message from technician update group (sent from views.py)
    def job_status_update(self, event):
        self.send(text_data=json.dumps(event))

    def job_assigned(self, event):
        self.send(text_data=json.dumps(event))

    def task_feed_event(self, event):
        self.send(text_data=json.dumps(event))


class TaskChatConsumer(WebsocketConsumer):
    """Live bidirectional task room: messaging thread, status changes, priority changes."""

    def connect(self):
        self.task_id = self.scope['url_route']['kwargs']['task_id']
        from .models import Task
        try:
            self.task = Task.objects.select_related('assigned_to__user').only(
                'id', 'workspace_id', 'assigned_to_id', 'assigned_to__user_id'
            ).get(id=self.task_id)
        except (ValueError, Task.DoesNotExist):
            self.close()
            return

        self.user = _authenticate_websocket_user(self.scope)
        if not self.user:
            self.close()
            return

        is_staff_in_workspace = self.user.is_staff and _active_membership(self.user, self.task.workspace_id)
        is_assigned_tech = bool(
            self.task.assigned_to
            and self.task.assigned_to.user_id == self.user.id
            and (
                not self.task.workspace_id
                or not self.task.assigned_to.workspace_id
                or self.task.assigned_to.workspace_id == self.task.workspace_id
                or _active_membership(self.user, self.task.workspace_id)
            )
        )

        if not (is_staff_in_workspace or is_assigned_tech or self.user.is_superuser):
            self.close()
            return

        self.room_group_name = task_room_group_name(self.task.workspace_id, self.task_id)
        async_to_sync(self.channel_layer.group_add)(
            self.room_group_name,
            self.channel_name
        )
        self.accept()

    def disconnect(self, close_code):
        if hasattr(self, 'room_group_name'):
            async_to_sync(self.channel_layer.group_discard)(
                self.room_group_name,
                self.channel_name
            )

    def receive(self, text_data):
        try:
            data = json.loads(text_data)
        except (ValueError, json.JSONDecodeError):
            return

        action = data.get('action')
        if action == 'send_message':
            body = (data.get('body') or data.get('message') or '').strip()
            if body and self.user:
                from .models import Task, TaskMessage
                from .views.helpers import broadcast_task_message
                task = Task.objects.get(id=self.task_id)
                msg = TaskMessage.objects.create(
                    task=task,
                    sender=self.user,
                    body=body,
                )
                broadcast_task_message(task, msg)
        elif action == 'typing':
            async_to_sync(self.channel_layer.group_send)(
                self.room_group_name,
                {
                    'type': 'task_typing_event',
                    'username': self.user.username if self.user else 'Someone',
                    'is_typing': bool(data.get('is_typing', True)),
                }
            )

    def task_message_event(self, event):
        self.send(text_data=json.dumps(event))

    def task_status_event(self, event):
        self.send(text_data=json.dumps(event))

    def task_priority_event(self, event):
        self.send(text_data=json.dumps(event))

    def task_typing_event(self, event):
        self.send(text_data=json.dumps(event))


class StaffTaskDashboardConsumer(WebsocketConsumer):
    """Broadcasts task creations, deletions, and status changes to staff dashboard."""

    def connect(self):
        self.user = _authenticate_websocket_user(self.scope)
        if not self.user or not self.user.is_staff:
            self.close()
            return

        workspace_id = _query_value(self.scope, 'workspace')
        try:
            workspace_id = int(workspace_id) if workspace_id else None
        except ValueError:
            self.close()
            return

        if not workspace_id:
            memberships = CompanyUserMembership.objects.filter(user=self.user, is_active=True)
            workspace_id = memberships.values_list('workspace_id', flat=True).first()

        if workspace_id and not _active_membership(self.user, workspace_id) and not self.user.is_superuser:
            self.close()
            return

        self.workspace_id = workspace_id
        self.group_name = staff_tasks_group_name(self.workspace_id)
        async_to_sync(self.channel_layer.group_add)(
            self.group_name,
            self.channel_name
        )
        self.accept()

    def disconnect(self, close_code):
        if hasattr(self, 'group_name'):
            async_to_sync(self.channel_layer.group_discard)(
                self.group_name,
                self.channel_name
            )

    def receive(self, text_data):
        pass

    def task_feed_event(self, event):
        self.send(text_data=json.dumps(event))


class TechnicianTaskDashboardConsumer(WebsocketConsumer):
    """Broadcasts task assignments and changes to technician dashboard."""

    def connect(self):
        self.user = _authenticate_websocket_user(self.scope)
        if not self.user:
            self.close()
            return

        technician = TechnicianProfile.objects.filter(user=self.user).first()
        if not technician:
            self.close()
            return

        if technician.workspace_id and not _active_membership(self.user, technician.workspace_id) and not self.user.is_superuser:
            self.close()
            return

        self.workspace_id = technician.workspace_id
        self.technician_id = technician.id
        self.group_name = tech_tasks_group_name(self.workspace_id, self.technician_id)
        async_to_sync(self.channel_layer.group_add)(
            self.group_name,
            self.channel_name
        )
        self.accept()

    def disconnect(self, close_code):
        if hasattr(self, 'group_name'):
            async_to_sync(self.channel_layer.group_discard)(
                self.group_name,
                self.channel_name
            )

    def receive(self, text_data):
        pass

    def task_feed_event(self, event):
        self.send(text_data=json.dumps(event))


class FallbackRejectConsumer(WebsocketConsumer):
    """Silently closes unrecognized WebSocket connections (such as Next.js HMR or bot probes)."""

    def connect(self):
        self.close(code=4404)
