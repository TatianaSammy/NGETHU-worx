from django.contrib import admin
from .models import (
    TreatmentPlant, TreatmentPlantPhase, FilterType, WaterFilter, FilterStatus, FilterStatusEvent,
    Clarifier, DisludgeValve, Status,
    TreatedWaterLimit, MicrobialLimit, HeavyMetalLimit,
    RawWaterLimit, ClarifiedWaterLimit, CoagulatedWaterLimit, FilteredWaterLimit
)


# ========================
# Inline Admins
# ========================

class TreatmentPlantPhaseInline(admin.TabularInline):
    model = TreatmentPlantPhase
    extra = 0


class WaterFilterInline(admin.TabularInline):
    model = WaterFilter
    extra = 0
    fields = ("name", "filter_type", "location", "capacity", "flow_rate", "phase")


class FilterStatusInline(admin.StackedInline):
    model = FilterStatus
    extra = 0
    can_delete = False
    fields = (
        "installation_date", "last_maintenance_date", "next_maintenance_date",
        "maintenance_interval_days", "status", "condition_percent", "status_note",
    )


class ClarifierInline(admin.TabularInline):
    model = Clarifier
    extra = 0


class DisludgeValveInline(admin.TabularInline):
    model = DisludgeValve
    extra = 0


class TreatedWaterLimitInline(admin.TabularInline):
    model = TreatedWaterLimit
    extra = 0


class MicrobialLimitInline(admin.TabularInline):
    model = MicrobialLimit
    extra = 0


class HeavyMetalLimitInline(admin.TabularInline):
    model = HeavyMetalLimit
    extra = 0


class RawWaterLimitInline(admin.TabularInline):
    model = RawWaterLimit
    extra = 0


class ClarifiedWaterLimitInline(admin.TabularInline):
    model = ClarifiedWaterLimit
    extra = 0


class CoagulatedWaterLimitInline(admin.TabularInline):
    model = CoagulatedWaterLimit
    extra = 0


class FilteredWaterLimitInline(admin.TabularInline):
    model = FilteredWaterLimit
    extra = 0


# ========================
# Admin Classes
# ========================

@admin.register(TreatmentPlant)
class TreatmentPlantAdmin(admin.ModelAdmin):
    list_display = ['name', 'location']
    search_fields = ['name', 'location']
    inlines = [
        TreatmentPlantPhaseInline,
        WaterFilterInline,
        ClarifierInline,
        TreatedWaterLimitInline,
        MicrobialLimitInline,
        HeavyMetalLimitInline,
        RawWaterLimitInline,
        ClarifiedWaterLimitInline,
        CoagulatedWaterLimitInline,
        FilteredWaterLimitInline
    ]


@admin.register(TreatmentPlantPhase)
class TreatmentPlantPhaseAdmin(admin.ModelAdmin):
    list_display = ['name', 'treatment_plant', 'design_capacity', 'operational_capacity']
    list_filter = ['treatment_plant']
    search_fields = ['name']



@admin.register(FilterType)
class FilterTypeAdmin(admin.ModelAdmin):
    list_display = ['name', 'description']
    search_fields = ['name']


@admin.register(WaterFilter)
class WaterFilterAdmin(admin.ModelAdmin):
    list_display = ['name', 'filter_type', 'capacity', 'flow_rate', 'treatment_plant', 'phase', 'operational_status']
    list_filter = ['filter_type', 'filter_status__status', 'treatment_plant', 'phase']
    search_fields = ['name', 'location', 'treatment_plant__name', 'phase__name']
    autocomplete_fields = ['filter_type', 'treatment_plant', 'phase']
    inlines = [FilterStatusInline]

    @admin.display(description="Operational status")
    def operational_status(self, obj):
        return obj.get_status_display()

    def save_formset(self, request, form, formset, change):
        instances = formset.save(commit=False)
        for instance in instances:
            if isinstance(instance, FilterStatus):
                instance._changed_by = request.user
            instance.save()
        formset.save_m2m()
        for instance in formset.deleted_objects:
            instance.delete()


@admin.register(FilterStatus)
class FilterStatusAdmin(admin.ModelAdmin):
    list_display = ["water_filter", "status", "condition_percent", "effective_next_maintenance_date", "maintenance_due", "updated_at"]
    list_filter = ["status", "water_filter__treatment_plant", "maintenance_interval_days"]
    search_fields = ["water_filter__name", "water_filter__location", "status_note"]
    autocomplete_fields = ["water_filter"]
    readonly_fields = ["created_at", "updated_at"]

    def save_model(self, request, obj, form, change):
        obj._changed_by = request.user
        super().save_model(request, obj, form, change)


@admin.register(FilterStatusEvent)
class FilterStatusEventAdmin(admin.ModelAdmin):
    list_display = ["water_filter", "event_type", "status", "condition_percent", "performed_by", "recorded_at"]
    list_filter = ["event_type", "status", "recorded_at"]
    search_fields = ["filter_status__water_filter__name", "remarks", "performed_by__username"]
    readonly_fields = ["filter_status", "event_type", "status", "condition_percent", "remarks", "performed_by", "recorded_at"]
    ordering = ["-recorded_at", "-pk"]

    def has_add_permission(self, request):
        return False

    @admin.display(description="Filter")
    def water_filter(self, obj):
        return obj.filter_status.water_filter


@admin.register(Status)
class StatusAdmin(admin.ModelAdmin):
    list_display = ['name']
    search_fields = ['name']


@admin.register(Clarifier)
class ClarifierAdmin(admin.ModelAdmin):
    list_display = ['name', 'design', 'flow_type', 'design_capacity', 'treatment_plant']
    list_filter = ['design', 'flow_type', 'treatment_plant']
    search_fields = ['name']
    inlines = [DisludgeValveInline]
    date_hierarchy = 'installation_date'


@admin.register(DisludgeValve)
class DisludgeValveAdmin(admin.ModelAdmin):
    list_display = ['name', 'clarifier', 'status']
    list_filter = ['status']
    search_fields = ['name']


# ========================
# Limit Admins (Generic)
# ========================

class BaseLimitAdmin(admin.ModelAdmin):
    list_display = ['parameter', 'treatment_plant', 'min_value', 'max_value']
    list_filter = ['treatment_plant', 'parameter']
    search_fields = ['parameter__name']


@admin.register(TreatedWaterLimit)
class TreatedWaterLimitAdmin(BaseLimitAdmin): pass

@admin.register(MicrobialLimit)
class MicrobialLimitAdmin(BaseLimitAdmin): pass

@admin.register(HeavyMetalLimit)
class HeavyMetalLimitAdmin(BaseLimitAdmin): pass

@admin.register(RawWaterLimit)
class RawWaterLimitAdmin(BaseLimitAdmin): pass

@admin.register(ClarifiedWaterLimit)
class ClarifiedWaterLimitAdmin(BaseLimitAdmin): pass

@admin.register(CoagulatedWaterLimit)
class CoagulatedWaterLimitAdmin(BaseLimitAdmin): pass

@admin.register(FilteredWaterLimit)
class FilteredWaterLimitAdmin(BaseLimitAdmin):
    list_display = ['parameter', 'water_filter', 'treatment_plant', 'min_value', 'max_value']
    list_filter = ['treatment_plant', 'water_filter', 'parameter']
