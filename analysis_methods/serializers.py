from rest_framework import serializers
from django.core.exceptions import ValidationError
from .models import (
    ChemicalAnalysisMethod,
    MicrobialMethod,
    WWAnalysisMethod,
    CWAnalysisMethod,
)
import json
import math

# -------------------------
# Dynamic Fields Mixin
# -------------------------
class DynamicFieldMixin:
    """
    Mixin to allow dynamic field control via `fields` and `exclude` query.
    """
    def __init__(self, *args, **kwargs):
        fields = kwargs.pop("fields", None)
        exclude = kwargs.pop("exclude", None)
        super().__init__(*args, **kwargs)

        if fields is not None:
            allowed = set(fields)
            existing = set(self.fields)
            for field_name in existing - allowed:
                self.fields.pop(field_name)

        if exclude is not None:
            for field_name in exclude:
                self.fields.pop(field_name, None)

# -------------------------
# Chemical Analysis Method Serializer
# -------------------------
class ChemicalAnalysisMethodSerializer(DynamicFieldMixin, serializers.ModelSerializer):
    created_by = serializers.StringRelatedField(read_only=True)
    updated_by = serializers.StringRelatedField(read_only=True)
    input_field_names = serializers.SerializerMethodField()
    example_inputs = serializers.SerializerMethodField()
    input_labels = serializers.SerializerMethodField()
    description_text = serializers.SerializerMethodField()
    result_example = serializers.SerializerMethodField()

    class Meta:
        model = ChemicalAnalysisMethod
        fields = [
            'id', 'method', 'kebs_reference', 'formula', 'inputs_structure',
            'unit', 'rounding_precision', 'rounding_mode', 'description', 'category', 'tags',
            'version', 'is_active', 'formula_hash', 'created_by', 'updated_by',
            'created_at', 'updated_at',
            'input_field_names', 'example_inputs', 'input_labels', 'description_text', 'result_example'
        ]
        read_only_fields = ['formula_hash', 'created_by', 'updated_by', 'created_at', 'updated_at']

    def get_input_field_names(self, obj):
        return obj.get_input_fields()

    def get_example_inputs(self, obj):
        return obj.example_input_payload()

    def get_input_labels(self, obj):
        return {
            item["name"]: item.get("label", item["name"])
            for item in obj.get_input_definitions()
            if item.get("name")
        }

    def get_description_text(self, obj):
        return obj.describe_inputs()

    def get_result_example(self, obj):
        try:
            return obj.evaluate(obj.example_input_payload())
        except ValidationError:
            return None

    def validate_formula(self, value):
        from .models import validate_formula_expression
        if value:
            validate_formula_expression(value)
        return value

    def create(self, validated_data):
        request = self.context.get('request')
        if request and hasattr(request, 'user'):
            validated_data['created_by'] = request.user
            validated_data['updated_by'] = request.user
        return super().create(validated_data)

    def update(self, instance, validated_data):
        request = self.context.get('request')
        if request and hasattr(request, 'user'):
            validated_data['updated_by'] = request.user
        return super().update(instance, validated_data)

# -------------------------
# Microbial Method Serializer
# -------------------------
class MicrobialMethodSerializer(DynamicFieldMixin, serializers.ModelSerializer):
    created_by = serializers.StringRelatedField(read_only=True)
    updated_by = serializers.StringRelatedField(read_only=True)
    confirmatory_tests_list = serializers.SerializerMethodField()
    incubation_hours = serializers.SerializerMethodField()
    is_deprecated = serializers.BooleanField(read_only=True)

    class Meta:
        model = MicrobialMethod
        fields = [
            'id', 'name', 'kebs_reference', 'description', 'incubation_time',
            'confirmatory_tests', 'status', 'change_log', 'version', 'is_active',
            'created_by', 'updated_by', 'created_at', 'updated_at',
            'confirmatory_tests_list', 'incubation_hours', 'is_deprecated'
        ]
        read_only_fields = ['created_by', 'updated_by', 'created_at', 'updated_at', 'is_deprecated']

    def get_confirmatory_tests_list(self, obj):
        return obj.get_confirmatory_tests_list()

    def get_incubation_hours(self, obj):
        return obj.get_incubation_hours()

    def create(self, validated_data):
        request = self.context.get('request')
        if request and hasattr(request, 'user'):
            validated_data['created_by'] = request.user
            validated_data['updated_by'] = request.user
        return super().create(validated_data)

    def update(self, instance, validated_data):
        request = self.context.get('request')
        if request and hasattr(request, 'user'):
            validated_data['updated_by'] = request.user
        return super().update(instance, validated_data)

# -------------------------
# WasteWater Analysis Method Serializer
# -------------------------
class WWAnalysisMethodSerializer(DynamicFieldMixin, serializers.ModelSerializer):
    created_by = serializers.StringRelatedField(read_only=True)
    updated_by = serializers.StringRelatedField(read_only=True)

    class Meta:
        model = WWAnalysisMethod
        fields = [
            'id', 'name', 'reference', 'version', 'is_active',
            'created_by', 'updated_by', 'created_at', 'updated_at',
        ]
        read_only_fields = ['created_by', 'updated_by', 'created_at', 'updated_at', 'version']

    def create(self, validated_data):
        request = self.context.get('request')
        if request and hasattr(request, 'user'):
            validated_data['created_by'] = request.user
            validated_data['updated_by'] = request.user
        return super().create(validated_data)

    def update(self, instance, validated_data):
        request = self.context.get('request')
        if request and hasattr(request, 'user'):
            validated_data['updated_by'] = request.user
        return super().update(instance, validated_data)

# -------------------------
# CleanWater Analysis Method Serializer
# -------------------------
class CWAnalysisMethodSerializer(DynamicFieldMixin, serializers.ModelSerializer):
    created_by = serializers.StringRelatedField(read_only=True)
    updated_by = serializers.StringRelatedField(read_only=True)

    class Meta:
        model = CWAnalysisMethod
        fields = [
            'id', 'name', 'reference', 'version', 'is_active',
            'created_by', 'updated_by', 'created_at', 'updated_at',
        ]
        read_only_fields = ['created_by', 'updated_by', 'created_at', 'updated_at', 'version']

    def create(self, validated_data):
        request = self.context.get('request')
        if request and hasattr(request, 'user'):
            validated_data['created_by'] = request.user
            validated_data['updated_by'] = request.user
        return super().create(validated_data)

    def update(self, instance, validated_data):
        request = self.context.get('request')
        if request and hasattr(request, 'user'):
            validated_data['updated_by'] = request.user
        return super().update(instance, validated_data)
