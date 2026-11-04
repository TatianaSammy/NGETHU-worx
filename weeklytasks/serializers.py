# weekly_duty_allocations/serializers.py

from rest_framework import serializers
from .models import (
    StandardWeeklyTask,
    MonthlyDutySchedule,
    DutyWeek,
    WeeklyDutyTask,
    DailyTaskAssignment,
    DutyConfirmation,
)
from sc.models import AnnualLeave, Shift
from t.models import TreatmentPlant
from django.contrib.auth import get_user_model

User = get_user_model()


# ========================
# Base Related Serializers
# ========================

class UserSerializer(serializers.ModelSerializer):
    section_name = serializers.SerializerMethodField()

    class Meta:
        model = User
        fields = ['id', 'username', 'first_name', 'last_name', 'email', 'section_name']

    def get_section_name(self, obj):
        return obj.section.name if getattr(obj, 'section', None) else None


class ShiftSerializer(serializers.ModelSerializer):
    class Meta:
        model = Shift
        fields = ['id', 'name', 'start_time', 'end_time', 'is_night_shift']


class TreatmentPlantSerializer(serializers.ModelSerializer):
    class Meta:
        model = TreatmentPlant
        fields = ['id', 'name', 'location']


# ========================
# Core Serializers
# ========================

class StandardWeeklyTaskSerializer(serializers.ModelSerializer):
    class Meta:
        model = StandardWeeklyTask
        fields = ['id', 'code', 'name', 'description', 'is_active']


class MonthlyDutyScheduleSerializer(serializers.ModelSerializer):
    treatment_plant = TreatmentPlantSerializer(read_only=True)
    created_by = UserSerializer(read_only=True)
    scheduled_sections = serializers.SerializerMethodField()

    class Meta:
        model = MonthlyDutySchedule
        fields = [
            'id', 'month', 'year', 'treatment_plant', 'created_by',
            'notes', 'scheduled_sections', 'created_at', 'updated_at'
        ]

    def get_scheduled_sections(self, obj):
        return [s for s in obj.get_scheduled_sections_names()]


class DutyWeekSerializer(serializers.ModelSerializer):
    schedule = MonthlyDutyScheduleSerializer(read_only=True)

    class Meta:
        model = DutyWeek
        fields = ['id', 'schedule', 'start_date', 'end_date']


class WeeklyDutyTaskSerializer(serializers.ModelSerializer):
    task_template = StandardWeeklyTaskSerializer(read_only=True)
    week = DutyWeekSerializer(read_only=True)
    treatment_plant = TreatmentPlantSerializer(read_only=True)
    assigned_sections = serializers.SerializerMethodField()
    assigned_users = serializers.SerializerMethodField()

    class Meta:
        model = WeeklyDutyTask
        fields = ['id', 'week', 'task_template', 'treatment_plant', 'assigned_sections', 'assigned_users']

    def get_assigned_sections(self, obj):
        return obj.get_assigned_sections_names()

    def get_assigned_users(self, obj):
        return UserSerializer(obj.get_assigned_users(), many=True).data


# ========================
# Daily Task Assignment
# ========================

class DailyTaskAssignmentSerializer(serializers.ModelSerializer):
    user = UserSerializer(read_only=True)
    shift = ShiftSerializer(read_only=True)
    task = WeeklyDutyTaskSerializer(read_only=True)

    class Meta:
        model = DailyTaskAssignment
        fields = ['id', 'task', 'user', 'date', 'shift', 'remarks', 'created_at', 'updated_at']

    def validate(self, data):
        request = self.context.get('request')
        user = request.user if request else data.get('user')
        shift = data.get('shift')
        date = data.get('date')

        if user and date and AnnualLeave.is_unavailable(user.pk, date):
            raise serializers.ValidationError({
                'date': 'You are marked on annual leave or off duty on this date and cannot be assigned a task.'
            })

        if shift and getattr(shift, 'is_night_shift', False):
            raise serializers.ValidationError("Only day shifts are allowed for task assignments.")

        if shift and user:
            exists = ShiftAssignment.objects.filter(
                staff=user,
                shift=shift,
                date=date,
                is_deleted=False
            ).exists()
            if not exists:
                raise serializers.ValidationError(f"{user.get_full_name()} is not scheduled for this shift on {date}.")

        return data


# ========================
# Duty Confirmation
# ========================

class DutyConfirmationSerializer(serializers.ModelSerializer):
    assignment = DailyTaskAssignmentSerializer(read_only=True)
    confirmed_by = UserSerializer(read_only=True)

    class Meta:
        model = DutyConfirmation
        fields = ['id', 'assignment', 'confirmed_by', 'confirmed_at', 'notes', 'created_at', 'updated_at']
