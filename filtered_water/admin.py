from django.contrib import admin, messages
from django.utils.html import format_html
from django.core.exceptions import ValidationError
from django.utils.translation import gettext_lazy as _
from .models import (
    FilteredWaterAnalysis,
    FilteredWaterResults,
    FilteredWaterLimit,
    WaterFilter,
    ShiftAssignment,
    FilteredWaterAnalysisTransition,
)
from t.models import TreatmentPlant, TreatmentPlantPhase, FilterStatus as PlantFilterStatus


# --- Inline for FilteredWaterResults in FilteredWaterAnalysis ---

class FilteredWaterResultsInline(admin.TabularInline):
    model = FilteredWaterResults
    extra = 1
    autocomplete_fields = ('parameter', 'water_filter')
    readonly_fields = ('id',)
    fields = ('parameter', 'water_filter', 'result_value')


# --- Admin for FilteredWaterAnalysisTransitions (read-only) ---

@admin.register(FilteredWaterAnalysisTransition)
class FilteredWaterAnalysisTransitionAdmin(admin.ModelAdmin):
    list_display = ('analysis', 'from_state', 'to_state', 'performed_by', 'transition_time')
    readonly_fields = ('analysis', 'from_state', 'to_state', 'performed_by', 'transition_time')
    ordering = ('-transition_time',)
    search_fields = ('analysis__uuid', 'performed_by__username')


# --- WaterFilter Admin with colored status and actions ---

class WaterFilterStatusFilter(admin.SimpleListFilter):
    title = _('status')
    parameter_name = 'status'

    def lookups(self, request, model_admin):
        return [(status.value, status.name.capitalize()) for status in WaterFilterStatus]

    def queryset(self, request, queryset):
        if self.value():
            return queryset.filter(filter_status__status=self.value())
        return queryset


@admin.action(description="Mark selected filters as Working")
def mark_working(modeladmin, request, queryset):
    updated = 0
    for water_filter in queryset.iterator():
        status, _created = PlantFilterStatus.objects.get_or_create(water_filter=water_filter)
        status.status = "working"
        status._changed_by = request.user
        status.save()
        updated += 1
    modeladmin.message_user(request, f"{updated} filters marked as Working.", messages.SUCCESS)


@admin.action(description="Mark selected filters as Isolated")
def mark_isolated(modeladmin, request, queryset):
    updated = 0
    for water_filter in queryset.iterator():
        status, _created = PlantFilterStatus.objects.get_or_create(water_filter=water_filter)
        status.status = "isolated"
        status._changed_by = request.user
        status.save()
        updated += 1
    modeladmin.message_user(request, f"{updated} filters marked as Isolated.", messages.SUCCESS)


@admin.register(FilteredWaterResults)
class FilteredWaterResultsAdmin(admin.ModelAdmin):
    list_display = ('analysis', 'parameter_name', 'water_filter', 'result_value')
    search_fields = ('parameter__parameter__name', 'water_filter__name', 'analysis__uuid')
    list_filter = ('water_filter__filter_status__status',)
    autocomplete_fields = ('parameter', 'water_filter')

    def parameter_name(self, obj):
        return obj.parameter.parameter.name
    parameter_name.short_description = "Parameter"


# --- FilteredWaterAnalysis Admin with inlines and actions ---

@admin.action(description="Set selected analyses to Verified")
def set_verified(modeladmin, request, queryset):
    updated = 0
    for analysis in queryset:
        try:
            analysis.transition_state('verified', request.user)
            updated += 1
        except (ValidationError, PermissionError) as e:
            modeladmin.message_user(request, f"Failed to verify analysis {analysis}: {str(e)}", messages.ERROR)
    modeladmin.message_user(request, f"{updated} analyses set to Verified.", messages.SUCCESS)


@admin.register(FilteredWaterAnalysis)
class FilteredWaterAnalysisAdmin(admin.ModelAdmin):
    list_display = ('uuid_short', 'timestamp', 'treatment_plant', 'treatment_plant_phase', 'state_colored', 'analysed_by', 'verified_by')
    list_filter = ('state', 'treatment_plant', 'treatment_plant_phase')
    search_fields = ('uuid', 'remarks', 'analysed_by__username', 'verified_by__username')
    readonly_fields = ('uuid', 'timestamp', 'created_at', 'updated_at', 'state', 'analysed_by', 'verified_by')
    ordering = ('-timestamp',)
    inlines = [FilteredWaterResultsInline]
    actions = [set_verified]

    def uuid_short(self, obj):
        return str(obj.uuid)[:8]
    uuid_short.short_description = "UUID"

    def state_colored(self, obj):
        color_map = {
            'new': 'gray',
            'analysed': 'blue',
            'verified': 'green',
        }
        color = color_map.get(obj.state, 'black')
        return format_html('<strong style="color:{};">{}</strong>', color, obj.get_state_display())
    state_colored.short_description = 'State'


# --- ShiftAssignment Admin for reference if needed ---

