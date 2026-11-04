from rest_framework import serializers
from accounts.models import CustomUser
from t.models import TreatmentPlant
from sc.models import Shift, ShiftAssignment
from parameters.models import WWQParameter_qad
from analysis_methods.models import WWAnalysisMethod
from .models import (
    WasteWaterSamplingSite, WasteWaterSample, WasteWaterAnalysis,
    WasteWaterParameterResult, WasteWaterStabilizationPond,
    WasteWaterPondSamplingSite, WasteWaterPondSample,
    WasteWaterPondAnalysis, WasteWaterPondParameterResult
)

# ------------------ Related Serializers ------------------ #

class TreatmentPlantSerializer(serializers.ModelSerializer):
    class Meta:
        model = TreatmentPlant
        fields = ['id', 'name']

class ShiftSerializer(serializers.ModelSerializer):
    class Meta:
        model = Shift
        fields = ['id', 'name', 'start_time', 'end_time']

class ShiftAssignmentSerializer(serializers.ModelSerializer):
    employee_name = serializers.CharField(source='employee.get_full_name', read_only=True)

    class Meta:
        model = ShiftAssignment
        fields = ['id', 'employee_name', 'shift', 'date']

class CustomUserSerializer(serializers.ModelSerializer):
    class Meta:
        model = CustomUser
        fields = ['id', 'username', 'first_name', 'last_name']

class WWQParameterSerializer(serializers.ModelSerializer):
    nema_min_std = serializers.DecimalField(source='min_standard', max_digits=8, decimal_places=3, read_only=True)
    nema_max_std = serializers.DecimalField(source='max_standard', max_digits=8, decimal_places=3, read_only=True)

    class Meta:
        model = WWQParameter_qad
        fields = ['id', 'name', 'nema_min_std', 'nema_max_std']

class WWAnalysisMethodSerializer(serializers.ModelSerializer):
    class Meta:
        model = WWAnalysisMethod
        fields = ['id', 'name', 'reference']

# ------------------ Waste Water Sampling Sites ------------------ #

class WasteWaterSamplingSiteSerializer(serializers.ModelSerializer):
    treatment_plant = serializers.PrimaryKeyRelatedField(queryset=TreatmentPlant.objects.all())

    treatment_plant_detail = TreatmentPlantSerializer(source='treatment_plant', read_only=True)

    class Meta:
        model = WasteWaterSamplingSite
        fields = ['id', 'treatment_plant', 'treatment_plant_detail', 'name', 'location_description']

# ------------------ Waste Water Samples ------------------ #

class WasteWaterSampleSerializer(serializers.ModelSerializer):
    sampling_site = serializers.PrimaryKeyRelatedField(queryset=WasteWaterSamplingSite.objects.all())
    shift = serializers.PrimaryKeyRelatedField(queryset=Shift.objects.all())
    assignment = serializers.PrimaryKeyRelatedField(queryset=ShiftAssignment.objects.all())
    treatment_plant = serializers.PrimaryKeyRelatedField(queryset=TreatmentPlant.objects.all())

    sampling_site_detail = WasteWaterSamplingSiteSerializer(source='sampling_site', read_only=True)
    shift_detail = ShiftSerializer(source='shift', read_only=True)
    assignment_detail = ShiftAssignmentSerializer(source='assignment', read_only=True)
    treatment_plant_detail = TreatmentPlantSerializer(source='treatment_plant', read_only=True)

    class Meta:
        model = WasteWaterSample
        fields = ['id', 'reference_id', 'treatment_plant', 'treatment_plant_detail',
                  'sampling_site', 'sampling_site_detail', 'shift', 'shift_detail',
                  'assignment', 'assignment_detail', 'sampling_date', 'sampling_time']

# ------------------ Waste Water Parameter Results ------------------ #

class WasteWaterParameterResultSerializer(serializers.ModelSerializer):
    parameter = serializers.PrimaryKeyRelatedField(queryset=WWQParameter_qad.objects.all())
    analysis_method = serializers.PrimaryKeyRelatedField(queryset=WWAnalysisMethod.objects.all())

    parameter_detail = WWQParameterSerializer(source='parameter', read_only=True)
    analysis_method_detail = WWAnalysisMethodSerializer(source='analysis_method', read_only=True)

    class Meta:
        model = WasteWaterParameterResult
        fields = ['id', 'parameter', 'parameter_detail', 'analysis_method', 'analysis_method_detail',
                  'result', 'effluent_standard', 'kebs_reference', 'quality_compliance']

    def create(self, validated_data):
        result = validated_data.get('result')
        parameter = validated_data.get('parameter')
        analysis_method = validated_data.get('analysis_method')

        validated_data['effluent_standard'] = f"{parameter.min_standard} - {parameter.max_standard} {parameter.unit}"
        validated_data['kebs_reference'] = analysis_method.reference

        if parameter.min_standard <= result <= parameter.max_standard:
            validated_data['quality_compliance'] = "Complying"
        else:
            validated_data['quality_compliance'] = "Not Complying"

        return super().create(validated_data)

# ------------------ Waste Water Analysis ------------------ #

class WasteWaterAnalysisSerializer(serializers.ModelSerializer):
    sample = serializers.PrimaryKeyRelatedField(queryset=WasteWaterSample.objects.all())
    analysed_by = CustomUserSerializer(read_only=True)
    verified_by = CustomUserSerializer(read_only=True)
    confirmed_by = CustomUserSerializer(read_only=True)
    approved_by = CustomUserSerializer(read_only=True)
    parameter_results = WasteWaterParameterResultSerializer(many=True, read_only=True)

    class Meta:
        model = WasteWaterAnalysis
        fields = ['id', 'sample', 'status', 'analysed_by', 'verified_by', 'confirmed_by',
                  'approved_by', 'analysis_datetime', 'remarks', 'parameter_results']

    def update(self, instance, validated_data):
        new_status = validated_data.get('status', instance.status)
        user = self.context['request'].user

        if new_status != instance.status:
            instance.change_status(new_status, user, user_has_permission=True)
        instance.remarks = validated_data.get('remarks', instance.remarks)
        instance.save()
        return instance

# ------------------ Waste Water Stabilization Ponds ------------------ #

class WasteWaterStabilizationPondSerializer(serializers.ModelSerializer):
    treatment_plant = serializers.PrimaryKeyRelatedField(queryset=TreatmentPlant.objects.all())
    treatment_plant_detail = TreatmentPlantSerializer(source='treatment_plant', read_only=True)

    class Meta:
        model = WasteWaterStabilizationPond
        fields = ['id', 'name', 'treatment_plant', 'treatment_plant_detail', 'description']

# ------------------ Waste Water Pond Sampling Sites ------------------ #

class WasteWaterPondSamplingSiteSerializer(serializers.ModelSerializer):
    pond = serializers.PrimaryKeyRelatedField(queryset=WasteWaterStabilizationPond.objects.all())
    pond_detail = WasteWaterStabilizationPondSerializer(source='pond', read_only=True)

    class Meta:
        model = WasteWaterPondSamplingSite
        fields = ['id', 'pond', 'pond_detail', 'site_name', 'description']

# ------------------ Waste Water Pond Samples ------------------ #

class WasteWaterPondSampleSerializer(serializers.ModelSerializer):
    site = serializers.PrimaryKeyRelatedField(queryset=WasteWaterPondSamplingSite.objects.all())
    shift = serializers.PrimaryKeyRelatedField(queryset=Shift.objects.all())
    assignment = serializers.PrimaryKeyRelatedField(queryset=ShiftAssignment.objects.all())

    site_detail = WasteWaterPondSamplingSiteSerializer(source='site', read_only=True)
    shift_detail = ShiftSerializer(source='shift', read_only=True)
    assignment_detail = ShiftAssignmentSerializer(source='assignment', read_only=True)

    class Meta:
        model = WasteWaterPondSample
        fields = ['id', 'reference_id', 'site', 'site_detail', 'sampling_date',
                  'sampling_time', 'shift', 'shift_detail', 'assignment', 'assignment_detail']

# ------------------ Waste Water Pond Parameter Results ------------------ #

class WasteWaterPondParameterResultSerializer(serializers.ModelSerializer):
    parameter = serializers.PrimaryKeyRelatedField(queryset=WWQParameter_qad.objects.all())
    analysis_method = serializers.PrimaryKeyRelatedField(queryset=WWAnalysisMethod.objects.all())

    parameter_detail = WWQParameterSerializer(source='parameter', read_only=True)
    analysis_method_detail = WWAnalysisMethodSerializer(source='analysis_method', read_only=True)

    class Meta:
        model = WasteWaterPondParameterResult
        fields = ['id', 'parameter', 'parameter_detail', 'analysis_method', 'analysis_method_detail',
                  'result', 'effluent_standard', 'kebs_reference', 'quality_compliance']

    def create(self, validated_data):
        result = validated_data.get('result')
        parameter = validated_data.get('parameter')
        analysis_method = validated_data.get('analysis_method')

        validated_data['effluent_standard'] = f"{parameter.min_standard} - {parameter.max_standard} {parameter.unit}"
        validated_data['kebs_reference'] = analysis_method.reference

        if parameter.min_standard <= result <= parameter.max_standard:
            validated_data['quality_compliance'] = "Complying"
        else:
            validated_data['quality_compliance'] = "Not Complying"

        return super().create(validated_data)

# ------------------ Waste Water Pond Analysis ------------------ #

class WasteWaterPondAnalysisSerializer(serializers.ModelSerializer):
    sample = serializers.PrimaryKeyRelatedField(queryset=WasteWaterPondSample.objects.all())
    analysed_by = CustomUserSerializer(read_only=True)
    verified_by = CustomUserSerializer(read_only=True)
    confirmed_by = CustomUserSerializer(read_only=True)
    approved_by = CustomUserSerializer(read_only=True)
    parameter_results = WasteWaterPondParameterResultSerializer(many=True, read_only=True)

    class Meta:
        model = WasteWaterPondAnalysis
        fields = ['id', 'sample', 'status', 'analysed_by', 'verified_by', 'confirmed_by',
                  'approved_by', 'analysis_datetime', 'remarks', 'parameter_results']

    def update(self, instance, validated_data):
        new_status = validated_data.get('status', instance.status)
        user = self.context['request'].user

        if new_status != instance.status:
            instance.change_status(new_status, user, user_has_permission=True)
        instance.remarks = validated_data.get('remarks', instance.remarks)
        instance.save()
        return instance
