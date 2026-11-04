# weekly_duty_allocations/viewsets.py

from rest_framework import viewsets, permissions
from core.permissions import ViewModelPermissions
from rest_framework.response import Response
from rest_framework.decorators import action
from django.utils import timezone
from rest_framework.exceptions import PermissionDenied
from core.plant_section_scope import scoped_queryset

from .models import (
    StandardWeeklyTask,
    MonthlyDutySchedule,
    DutyWeek,
    WeeklyDutyTask,
    DailyTaskAssignment,
    DutyConfirmation,
)
from .serializers import (
    StandardWeeklyTaskSerializer,
    MonthlyDutyScheduleSerializer,
    DutyWeekSerializer,
    WeeklyDutyTaskSerializer,
    DailyTaskAssignmentSerializer,
    DutyConfirmationSerializer,
)

from django.contrib.auth import get_user_model

User = get_user_model()


# ============
# StandardWeeklyTask
# ============
class StandardWeeklyTaskViewSet(viewsets.ModelViewSet):
    queryset = StandardWeeklyTask.objects.all()
    serializer_class = StandardWeeklyTaskSerializer
    permission_classes = [ViewModelPermissions]

    def get_queryset(self):
        return scoped_queryset(super().get_queryset(), self.request.user)

    def check_permissions(self, request):
        super().check_permissions(request)
        if request.method not in permissions.SAFE_METHODS and not request.user.is_superuser:
            raise PermissionDenied("Only a superuser can change the shared task library.")


# ============
# MonthlyDutySchedule
# ============
class MonthlyDutyScheduleViewSet(viewsets.ModelViewSet):
    queryset = MonthlyDutySchedule.objects.prefetch_related('weeks__weekly_tasks__daily_assignments').all()
    serializer_class = MonthlyDutyScheduleSerializer
    permission_classes = [ViewModelPermissions]

    def get_queryset(self):
        return scoped_queryset(super().get_queryset(), self.request.user)

    def perform_create(self, serializer):
        serializer.save(created_by=self.request.user)

    @action(detail=True, methods=['get'])
    def assigned_users(self, request, pk=None):
        """Return all users assigned in this monthly schedule."""
        schedule = self.get_object()
        users = schedule.get_assigned_users()
        return Response([{
            'id': u.id,
            'username': u.username,
            'full_name': u.get_full_name(),
            'section_name': getattr(u.section, 'name', None)
        } for u in users])


# ============
# DutyWeek
# ============
class DutyWeekViewSet(viewsets.ModelViewSet):
    queryset = DutyWeek.objects.select_related('schedule').prefetch_related('weekly_tasks__daily_assignments').all()
    serializer_class = DutyWeekSerializer
    permission_classes = [ViewModelPermissions]

    def get_queryset(self):
        return scoped_queryset(super().get_queryset(), self.request.user)


# ============
# WeeklyDutyTask
# ============
class WeeklyDutyTaskViewSet(viewsets.ModelViewSet):
    queryset = WeeklyDutyTask.objects.select_related('week', 'task_template', 'treatment_plant').prefetch_related('daily_assignments__user').all()
    serializer_class = WeeklyDutyTaskSerializer
    permission_classes = [ViewModelPermissions]

    def get_queryset(self):
        return scoped_queryset(super().get_queryset(), self.request.user)

    @action(detail=True, methods=['get'])
    def assigned_users(self, request, pk=None):
        """Return all users assigned to this weekly task."""
        task = self.get_object()
        users = task.get_assigned_users()
        return Response([{
            'id': u.id,
            'username': u.username,
            'full_name': u.get_full_name(),
            'section_name': getattr(u.section, 'name', None)
        } for u in users])

    @action(detail=True, methods=['get'])
    def assigned_sections(self, request, pk=None):
        """Return all section names assigned via users."""
        task = self.get_object()
        return Response(task.get_assigned_sections_names())


# ============
# DailyTaskAssignment
# ============
class DailyTaskAssignmentViewSet(viewsets.ModelViewSet):
    queryset = DailyTaskAssignment.objects.select_related('task', 'user', 'shift').all()
    serializer_class = DailyTaskAssignmentSerializer
    permission_classes = [ViewModelPermissions]

    def get_queryset(self):
        return scoped_queryset(super().get_queryset(), self.request.user)

    def perform_create(self, serializer):
        serializer.save(user=self.request.user)

    @action(detail=False, methods=['get'])
    def my_tasks(self, request):
        """Return today's tasks assigned to the current user."""
        today = timezone.now().date()
        qs = self.get_queryset().filter(user=request.user, date=today)
        serializer = self.get_serializer(qs, many=True)
        return Response(serializer.data)


# ============
# DutyConfirmation
# ============
class DutyConfirmationViewSet(viewsets.ModelViewSet):
    queryset = DutyConfirmation.objects.select_related('assignment', 'confirmed_by').all()
    serializer_class = DutyConfirmationSerializer
    permission_classes = [ViewModelPermissions]

    def get_queryset(self):
        return scoped_queryset(super().get_queryset(), self.request.user)

    def perform_create(self, serializer):
        serializer.save(confirmed_by=self.request.user, confirmed_at=timezone.now())
