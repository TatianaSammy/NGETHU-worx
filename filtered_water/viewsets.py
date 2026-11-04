from rest_framework import viewsets, status, filters
from core.permissions import ViewModelPermissions
from rest_framework.decorators import action
from rest_framework.response import Response
from rest_framework.permissions import IsAuthenticated
from django.db import transaction
from django.utils import timezone
from django_filters.rest_framework import DjangoFilterBackend

from .models import (
    FilteredWaterAnalysis,
    FilteredWaterResults,
    FilteredWaterLimit,
    WaterFilter,

)
from sc.models import (
    Shift,
)
from .serializers import (
    FilteredWaterAnalysisSerializer,
    FilteredWaterResultsSerializer,
    FilteredWaterLimitSerializer,
    WaterFilterSerializer,
    ShiftSerializer,
)


class WaterFilterViewSet(viewsets.ModelViewSet):
    """
    CRUD for WaterFilter with filtering by status.
    """
    queryset = WaterFilter.objects.all()
    serializer_class = WaterFilterSerializer
    filterset_fields = ["filter_status__status", "treatment_plant", "phase"]
    permission_classes = [ViewModelPermissions]
    filter_backends = [DjangoFilterBackend, filters.SearchFilter, filters.OrderingFilter]
    filterset_fields = ['status']
    search_fields = ['name']
    ordering_fields = ['name', 'status']
    ordering = ['name']


class FilteredWaterLimitViewSet(viewsets.ModelViewSet):
    """
    CRUD for FilteredWaterLimit, searchable by parameter and unit.
    """
    queryset = FilteredWaterLimit.objects.all()
    serializer_class = FilteredWaterLimitSerializer
    permission_classes = [ViewModelPermissions]
    filter_backends = [filters.SearchFilter, filters.OrderingFilter]
    search_fields = ['parameter', 'unit']
    ordering_fields = ['parameter', 'min_value', 'max_value']
    ordering = ['parameter']


class FilteredWaterResultsViewSet(viewsets.ModelViewSet):
    """
    CRUD for FilteredWaterResults.
    Includes validation context for treatment_plant.
    """
    queryset = FilteredWaterResults.objects.select_related('parameter', 'water_filter').all()
    serializer_class = FilteredWaterResultsSerializer
    permission_classes = [ViewModelPermissions]
    filter_backends = [DjangoFilterBackend, filters.OrderingFilter]
    filterset_fields = ['parameter', 'water_filter']
    ordering_fields = ['result_value']
    ordering = ['id']

    def get_serializer_context(self):
        context = super().get_serializer_context()
        # Optionally pass treatment_plant from query params or default None
        treatment_plant_id = self.request.query_params.get('treatment_plant')
        if treatment_plant_id:
            from .models import TreatmentPlant  # if exists
            try:
                treatment_plant = TreatmentPlant.objects.get(id=treatment_plant_id)
                context['treatment_plant'] = treatment_plant
            except TreatmentPlant.DoesNotExist:
                context['treatment_plant'] = None
        return context


class ShiftViewSet(viewsets.ReadOnlyModelViewSet):
    """
    Readonly shifts.
    Could be extended with filtering by treatment_plant or date.
    """
    queryset = Shift.objects.all()
    serializer_class = ShiftSerializer
    permission_classes = [ViewModelPermissions]
    filter_backends = [DjangoFilterBackend, filters.OrderingFilter]
    filterset_fields = ['treatment_plant']
    ordering_fields = ['start_time', 'end_time']
    ordering = ['start_time']


class FilteredWaterAnalysisViewSet(viewsets.ModelViewSet):
    """
    Complex nested CRUD for FilteredWaterAnalysis with embedded FilteredWaterResults.
    """
    queryset = FilteredWaterAnalysis.objects.select_related(
        'treatment_plant', 'analysed_by', 'verified_by'
    ).prefetch_related('results__parameter', 'results__water_filter')
    serializer_class = FilteredWaterAnalysisSerializer
    permission_classes = [ViewModelPermissions]
    filter_backends = [DjangoFilterBackend, filters.OrderingFilter, filters.SearchFilter]
    filterset_fields = [
        'treatment_plant',
        'treatment_plant_phase',
        'state',
        'analysed_by',
        'verified_by',
        'timestamp',
    ]
    ordering_fields = ['timestamp', 'created_at', 'updated_at']
    ordering = ['-timestamp']
    search_fields = ['remarks']

    def get_serializer_context(self):
        context = super().get_serializer_context()
        # Pass request user for shift and validation context
        context['request'] = self.request
        return context

    @transaction.atomic
    def create(self, request, *args, **kwargs):
        # Use your serializer's create logic (already handles nested results)
        return super().create(request, *args, **kwargs)

    @transaction.atomic
    def update(self, request, *args, **kwargs):
        # Use your serializer's update logic (handles nested results)
        return super().update(request, *args, **kwargs)

    @action(detail=True, methods=['post'], url_path='verify')
    def verify_analysis(self, request, pk=None):
        """
        Custom action to verify an analysis.
        For example, set state to 'verified' and set verified_by user.
        """
        analysis = self.get_object()
        if analysis.state == 'verified':
            return Response({'detail': 'Already verified.'}, status=status.HTTP_400_BAD_REQUEST)

        analysis.state = 'verified'
        analysis.verified_by = request.user
        analysis.save()
        serializer = self.get_serializer(analysis)
        return Response(serializer.data, status=status.HTTP_200_OK)

    @action(detail=False, methods=['get'], url_path='current-shift')
    def current_shift(self, request):
        """
        Returns the shift object for the current user and current time if any.
        """
        now = timezone.localtime().time()
        user = request.user
        today = timezone.localdate()

        shifts = Shift.objects.filter(
            shiftassignment__employee=user,
            shiftassignment__date=today,
            shiftassignment__attendance_status='present',
            shiftassignment__shift__treatment_plant__in=request.query_params.getlist('treatment_plant')
        ).distinct()

        for shift in shifts:
            if shift.start_time <= now <= shift.end_time:
                serializer = ShiftSerializer(shift)
                return Response(serializer.data)

        return Response({'detail': 'No current shift found.'}, status=status.HTTP_404_NOT_FOUND)
