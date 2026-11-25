from django.contrib import admin
from .models import Standardization
from .forms import StandardizationForm


@admin.register(Standardization)
class StandardizationAdmin(admin.ModelAdmin):
    form = StandardizationForm
    list_display = (
        "preparation_date",
        "titrant",
        "standard",
        "standard_normality",
        "aliquot_volume_ml",
        "average_titre",
        "actual_normality",
        "prepared_by",
    )
    list_filter = ("preparation_date", "titrant", "standard")
    search_fields = ("titrant__rname", "standard__rname", "remarks")
    readonly_fields = ("average_titre", "actual_normality", "created_at")
    date_hierarchy = "preparation_date"

    def save_model(self, request, obj, form, change):
        if not change and not obj.prepared_by:
            obj.prepared_by = request.user
        super().save_model(request, obj, form, change)
