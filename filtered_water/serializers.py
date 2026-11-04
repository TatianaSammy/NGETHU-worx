from rest_framework import serializers
from .models import (
    FilteredWaterAnalysis,
    FilteredWaterResults,
    FilteredWaterLimit,
    WaterFilter,

)
from sc.models import (
    Shift,
)
from django.utils import timezone


class WaterFilterSerializer(serializers.ModelSerializer):
    class Meta:
        model = WaterFilter
        fields = [
            'id', 'name', 'capacity', 'flow_rate',
            'capacity_utilization_percent', 'status',
        ]
        read_only_fields = ['capacity_utilization_percent', 'status']


class FilteredWaterLimitSerializer(serializers.ModelSerializer):
    class Meta:
        model = FilteredWaterLimit
        fields = ['id', 'parameter', 'unit', 'min_value', 'max_value']


class FilteredWaterResultsSerializer(serializers.ModelSerializer):
    parameter = FilteredWaterLimitSerializer(read_only=True)
    parameter_id = serializers.PrimaryKeyRelatedField(
        queryset=FilteredWaterLimit.objects.all(), source='parameter', write_only=True
    )
    water_filter = WaterFilterSerializer(read_only=True)
    water_filter_id = serializers.PrimaryKeyRelatedField(
        queryset=WaterFilter.objects.all(), source='water_filter', write_only=True
    )

    class Meta:
        model = FilteredWaterResults
        fields = [
            'id',
            'parameter',
            'parameter_id',
            'water_filter',
            'water_filter_id',
            'result_value'
        ]

    def validate(self, data):
        parameter = data.get('parameter')
        water_filter = data.get('water_filter')
        if parameter and water_filter:
            if parameter.treatment_plant != self.context.get('treatment_plant'):
                raise serializers.ValidationError(
                    "Parameter's treatment plant does not match the analysis treatment plant."
                )
            if parameter.water_filter != water_filter:
                raise serializers.ValidationError(
                    "Parameter's water filter does not match the selected water filter."
                )
            if water_filter.status != 'working':
                raise serializers.ValidationError(
                    f"Cannot record result for filter '{water_filter.name}' with status '{water_filter.status}'."
                )
        return data


class ShiftSerializer(serializers.ModelSerializer):
    class Meta:
        model = Shift
        fields = ['id', 'name', 'start_time', 'end_time']


class FilteredWaterAnalysisSerializer(serializers.ModelSerializer):
    results = FilteredWaterResultsSerializer(many=True)
    shift = serializers.SerializerMethodField()

    class Meta:
        model = FilteredWaterAnalysis
        fields = [
            'id',
            'uuid',
            'timestamp',
            'treatment_plant',
            'treatment_plant_phase',
            'analysed_by',
            'verified_by',
            'state',
            'remarks',
            'shift',
            'created_at',
            'updated_at',
            'results',
        ]
        read_only_fields = ['uuid', 'timestamp', 'created_at', 'updated_at', 'state', 'analysed_by', 'verified_by']

    def get_shift(self, obj):
        now = timezone.localtime().time()
        shifts = Shift.objects.filter(shiftassignment__employee=self.context['request'].user,
                                      shiftassignment__date=timezone.localdate(),
                                      shiftassignment__attendance_status='present',
                                      shiftassignment__shift__treatment_plant=obj.treatment_plant
                                      ).distinct()
        for shift in shifts:
            if shift.start_time <= now <= shift.end_time:
                return ShiftSerializer(shift).data
        return None

    def create(self, validated_data):
        results_data = validated_data.pop('results', [])
        request = self.context.get('request')
        user = request.user if request else None

        # Create analysis first
        analysis = FilteredWaterAnalysis.objects.create(**validated_data)

        # Pass treatment plant in context for validation
        for result_data in results_data:
            serializer = FilteredWaterResultsSerializer(
                data=result_data,
                context={'treatment_plant': analysis.treatment_plant}
            )
            serializer.is_valid(raise_exception=True)
            serializer.save(analysis=analysis)

        return analysis

    def update(self, instance, validated_data):
        results_data = validated_data.pop('results', [])

        for attr, value in validated_data.items():
            setattr(instance, attr, value)
        instance.save()

        # Collect existing result ids sent by client
        existing_ids = [item.get('id') for item in results_data if 'id' in item]

        # Delete results not in the update payload
        FilteredWaterResults.objects.filter(analysis=instance).exclude(id__in=existing_ids).delete()

        for result_data in results_data:
            result_id = result_data.get('id', None)
            if result_id:
                # Update existing result
                try:
                    result_obj = FilteredWaterResults.objects.get(id=result_id, analysis=instance)
                except FilteredWaterResults.DoesNotExist:
                    continue  # Or raise error if preferable

                serializer = FilteredWaterResultsSerializer(
                    result_obj,
                    data=result_data,
                    partial=True,
                    context={'treatment_plant': instance.treatment_plant}
                )
                serializer.is_valid(raise_exception=True)
                serializer.save()
            else:
                # Create new result
                serializer = FilteredWaterResultsSerializer(
                    data=result_data,
                    context={'treatment_plant': instance.treatment_plant}
                )
                serializer.is_valid(raise_exception=True)
                serializer.save(analysis=instance)

        return instance
