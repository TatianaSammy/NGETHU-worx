from rest_framework import serializers
from accounts.models import CustomUser
from t.models import TreatmentPlant
from sc.models import Shift, ShiftAssignment
from parameters.models import WWQParameter_qad
from analysis_methods.models import WWAnalysisMethod
from .models import (
    WasteWaterSamplingSite, WasteWaterSample, WasteWaterAnalysis, WasteWaterParameterResult,
    IndustrialSample, IndustrialSampleAnalysis, IndustrialSampleParameterResult,
    TreatmentPlantSample, TreatmentPlantSampleAnalysis, TreatmentPlantSampleParameterResult,
    ExhausterSample, ExhausterSampleAnalysis, ExhausterSampleParameterResult
)

DATETIME_KWARGS = {
    "format": "%Y-%m-%dT%H:%M:%S",
    "input_formats": ["%Y-%m-%dT%H:%M:%S", "iso-8601"]
}

DATE_KWARGS = {
    "format": "%Y-%m-%d",
    "input_formats": ["%Y-%m-%d", "iso-8601"]
}

class CustomUserSerializer(serializers.ModelSerializer):
    class Meta:
        model = CustomUser
        fields = ['id', 'username', 'email', 'first_name', 'last_name']

class TreatmentPlantSerializer(serializers.ModelSerializer):
    class Meta:
        model = TreatmentPlant
        fields = ['id', 'name', 'location']

class ShiftSerializer(serializers.ModelSerializer):
    start_time = serializers.DateTimeField(**DATETIME_KWARGS)
    end_time = serializers.DateTimeField(**DATETIME_KWARGS)

    class Meta:
        model = Shift
        fields = ['id', 'name', 'start_time', 'end_time']

class ShiftAssignmentSerializer(serializers.ModelSerializer):
    user = CustomUserSerializer(read_only=True)

    class Meta:
        model = ShiftAssignment
        fields = ['id', 'user', 'shift']

class WWQParameterSerializer(serializers.ModelSerializer):
    nema_min_std = serializers.DecimalField(source='min_standard', max_digits=8, decimal_places=3, read_only=True)
    nema_max_std = serializers.DecimalField(source='max_standard', max_digits=8, decimal_places=3, read_only=True)

    class Meta:
        model = WWQParameter_qad
        fields = ['id', 'name', 'unit', 'nema_min_std', 'nema_max_std']

class WWAnalysisMethodSerializer(serializers.ModelSerializer):
    class Meta:
        model = WWAnalysisMethod
        fields = ['id', 'name', 'reference']

# --- WasteWaterSamplingSite ---
class WasteWaterSamplingSiteSerializer(serializers.ModelSerializer):
    treatment_plant = TreatmentPlantSerializer(read_only=True)
    treatment_plant_id = serializers.PrimaryKeyRelatedField(queryset=TreatmentPlant.objects.all(), source='treatment_plant', write_only=True)

    class Meta:
        model = WasteWaterSamplingSite
        fields = '__all__'

# --- WasteWaterSample ---
class WasteWaterSampleSerializer(serializers.ModelSerializer):
    treatment_plant = TreatmentPlantSerializer(read_only=True)
    sampling_site = WasteWaterSamplingSiteSerializer(read_only=True)
    shift = ShiftSerializer(read_only=True)
    assignment = ShiftAssignmentSerializer(read_only=True)

    treatment_plant_id = serializers.PrimaryKeyRelatedField(queryset=TreatmentPlant.objects.all(), source='treatment_plant', write_only=True)
    sampling_site_id = serializers.PrimaryKeyRelatedField(queryset=WasteWaterSamplingSite.objects.all(), source='sampling_site', write_only=True)
    shift_id = serializers.PrimaryKeyRelatedField(queryset=Shift.objects.all(), source='shift', write_only=True)
    assignment_id = serializers.PrimaryKeyRelatedField(queryset=ShiftAssignment.objects.all(), source='assignment', write_only=True)

    sampling_date = serializers.DateField(**DATE_KWARGS)
    sampling_time = serializers.TimeField(format="%H:%M:%S", input_formats=["%H:%M:%S"])

    class Meta:
        model = WasteWaterSample
        fields = '__all__'

# --- WasteWaterParameterResult ---
class WasteWaterParameterResultSerializer(serializers.ModelSerializer):
    parameter = WWQParameterSerializer(read_only=True)
    analysis_method = WWAnalysisMethodSerializer(read_only=True)

    parameter_id = serializers.PrimaryKeyRelatedField(queryset=WWQParameter_qad.objects.all(), source='parameter', write_only=True)
    analysis_method_id = serializers.PrimaryKeyRelatedField(queryset=WWAnalysisMethod.objects.all(), source='analysis_method', write_only=True)

    class Meta:
        model = WasteWaterParameterResult
        fields = '__all__'

# --- WasteWaterAnalysis ---
class WasteWaterAnalysisSerializer(serializers.ModelSerializer):
    sample = WasteWaterSampleSerializer(read_only=True)
    sample_id = serializers.PrimaryKeyRelatedField(queryset=WasteWaterSample.objects.all(), source='sample', write_only=True)

    analysed_by = CustomUserSerializer(read_only=True)
    verified_by = CustomUserSerializer(read_only=True)
    confirmed_by = CustomUserSerializer(read_only=True)
    approved_by = CustomUserSerializer(read_only=True)

    analysed_by_id = serializers.PrimaryKeyRelatedField(queryset=CustomUser.objects.all(), source='analysed_by', write_only=True, required=False)
    verified_by_id = serializers.PrimaryKeyRelatedField(queryset=CustomUser.objects.all(), source='verified_by', write_only=True, required=False)
    confirmed_by_id = serializers.PrimaryKeyRelatedField(queryset=CustomUser.objects.all(), source='confirmed_by', write_only=True, required=False)
    approved_by_id = serializers.PrimaryKeyRelatedField(queryset=CustomUser.objects.all(), source='approved_by', write_only=True, required=False)

    parameter_results = WasteWaterParameterResultSerializer(many=True, read_only=True)
    analysis_datetime = serializers.DateTimeField(**DATETIME_KWARGS)

    class Meta:
        model = WasteWaterAnalysis
        fields = '__all__'

# --- IndustrialSample ---
class IndustrialSampleSerializer(serializers.ModelSerializer):
    shift = ShiftSerializer(read_only=True)
    assignment = ShiftAssignmentSerializer(read_only=True)

    shift_id = serializers.PrimaryKeyRelatedField(queryset=Shift.objects.all(), source='shift', write_only=True)
    assignment_id = serializers.PrimaryKeyRelatedField(queryset=ShiftAssignment.objects.all(), source='assignment', write_only=True)

    sampling_date = serializers.DateField(**DATE_KWARGS)
    sampling_time = serializers.TimeField(format="%H:%M:%S", input_formats=["%H:%M:%S"])

    class Meta:
        model = IndustrialSample
        fields = '__all__'

# --- TreatmentPlantSample ---
class TreatmentPlantSampleSerializer(serializers.ModelSerializer):
    treatment_plant = TreatmentPlantSerializer(read_only=True)
    shift = ShiftSerializer(read_only=True)
    assignment = ShiftAssignmentSerializer(read_only=True)

    treatment_plant_id = serializers.PrimaryKeyRelatedField(queryset=TreatmentPlant.objects.all(), source='treatment_plant', write_only=True)
    shift_id = serializers.PrimaryKeyRelatedField(queryset=Shift.objects.all(), source='shift', write_only=True)
    assignment_id = serializers.PrimaryKeyRelatedField(queryset=ShiftAssignment.objects.all(), source='assignment', write_only=True)

    sampling_date = serializers.DateField(**DATE_KWARGS)
    sampling_time = serializers.TimeField(format="%H:%M:%S", input_formats=["%H:%M:%S"])

    class Meta:
        model = TreatmentPlantSample
        fields = '__all__'

# --- ExhausterSample ---
class ExhausterSampleSerializer(serializers.ModelSerializer):
    shift = ShiftSerializer(read_only=True)
    assignment = ShiftAssignmentSerializer(read_only=True)

    shift_id = serializers.PrimaryKeyRelatedField(queryset=Shift.objects.all(), source='shift', write_only=True)
    assignment_id = serializers.PrimaryKeyRelatedField(queryset=ShiftAssignment.objects.all(), source='assignment', write_only=True)

    sampling_date = serializers.DateField(**DATE_KWARGS)
    sampling_time = serializers.TimeField(format="%H:%M:%S", input_formats=["%H:%M:%S"])

    class Meta:
        model = ExhausterSample
        fields = '__all__'

# --- Generic Analysis Serializer Mixin ---
class GenericAnalysisMixin(serializers.ModelSerializer):
    analysed_by = CustomUserSerializer(read_only=True)
    verified_by = CustomUserSerializer(read_only=True)
    confirmed_by = CustomUserSerializer(read_only=True)
    approved_by = CustomUserSerializer(read_only=True)

    analysed_by_id = serializers.PrimaryKeyRelatedField(queryset=CustomUser.objects.all(), source='analysed_by', write_only=True, required=False)
    verified_by_id = serializers.PrimaryKeyRelatedField(queryset=CustomUser.objects.all(), source='verified_by', write_only=True, required=False)
    confirmed_by_id = serializers.PrimaryKeyRelatedField(queryset=CustomUser.objects.all(), source='confirmed_by', write_only=True, required=False)
    approved_by_id = serializers.PrimaryKeyRelatedField(queryset=CustomUser.objects.all(), source='approved_by', write_only=True, required=False)

    analysis_datetime = serializers.DateTimeField(**DATETIME_KWARGS)

# --- IndustrialSampleAnalysis ---
class IndustrialSampleAnalysisSerializer(GenericAnalysisMixin):
    sample = IndustrialSampleSerializer(read_only=True)
    sample_id = serializers.PrimaryKeyRelatedField(queryset=IndustrialSample.objects.all(), source='sample', write_only=True)

    parameter_results = serializers.SerializerMethodField()

    class Meta:
        model = IndustrialSampleAnalysis
        fields = '__all__'

    def get_parameter_results(self, obj):
        return IndustrialSampleParameterResultSerializer(obj.parameter_results.all(), many=True).data

# --- TreatmentPlantSampleAnalysis ---
class TreatmentPlantSampleAnalysisSerializer(GenericAnalysisMixin):
    sample = TreatmentPlantSampleSerializer(read_only=True)
    sample_id = serializers.PrimaryKeyRelatedField(queryset=TreatmentPlantSample.objects.all(), source='sample', write_only=True)

    parameter_results = serializers.SerializerMethodField()

    class Meta:
        model = TreatmentPlantSampleAnalysis
        fields = '__all__'

    def get_parameter_results(self, obj):
        return TreatmentPlantSampleParameterResultSerializer(obj.parameter_results.all(), many=True).data

# --- ExhausterSampleAnalysis ---
class ExhausterSampleAnalysisSerializer(GenericAnalysisMixin):
    sample = ExhausterSampleSerializer(read_only=True)
    sample_id = serializers.PrimaryKeyRelatedField(queryset=ExhausterSample.objects.all(), source='sample', write_only=True)

    parameter_results = serializers.SerializerMethodField()

    class Meta:
        model = ExhausterSampleAnalysis
        fields = '__all__'

    def get_parameter_results(self, obj):
        return ExhausterSampleParameterResultSerializer(obj.parameter_results.all(), many=True).data

# --- Generic ParameterResult Serializer Mixin ---
class GenericParameterResultMixin(serializers.ModelSerializer):
    parameter = WWQParameterSerializer(read_only=True)
    analysis_method = WWAnalysisMethodSerializer(read_only=True)

    parameter_id = serializers.PrimaryKeyRelatedField(queryset=WWQParameter_qad.objects.all(), source='parameter', write_only=True)
    analysis_method_id = serializers.PrimaryKeyRelatedField(queryset=WWAnalysisMethod.objects.all(), source='analysis_method', write_only=True)

# --- IndustrialSampleParameterResult ---
class IndustrialSampleParameterResultSerializer(GenericParameterResultMixin):
    class Meta:
        model = IndustrialSampleParameterResult
        fields = '__all__'

# --- TreatmentPlantSampleParameterResult ---
class TreatmentPlantSampleParameterResultSerializer(GenericParameterResultMixin):
    class Meta:
        model = TreatmentPlantSampleParameterResult
        fields = '__all__'

# --- ExhausterSampleParameterResult ---
class ExhausterSampleParameterResultSerializer(GenericParameterResultMixin):
    class Meta:
        model = ExhausterSampleParameterResult
        fields = '__all__'
