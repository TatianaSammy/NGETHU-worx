from __future__ import annotations


MODEL_SCOPE_PATHS = {
    "sc.auditlog": {"owner": "user_id"},
    "sc.shift": {"plant": ("treatment_plant_id",), "section": ("section_requirements__section_id",)},
    "sc.shiftsectionrequirement": {"plant": ("shift__treatment_plant_id",), "section": ("section_id",)},
    "sc.shiftsupervisor": {"plant": ("treatment_plant_id", "user__assigned_treatment_plant_id"), "section": ("section_id", "user__section_id")},
    "sc.annualleave": {"plant": ("treatment_plant_id", "staff__assigned_treatment_plant_id"), "section": ("staff__section_id",)},
    "sc.monthlydutyroster": {"plant": ("treatment_plant_id",), "section": ("assignments__staff__section_id",)},
    "sc.shiftassignment": {"plant": ("shift__treatment_plant_id", "duty_roster__treatment_plant_id", "staff__assigned_treatment_plant_id"), "section": ("staff__section_id",)},
    "sc.shifthandover": {"plant": ("shift__treatment_plant_id",)},
    "sc.apparatus": {"plant": ("equipment__section__treatment_plant_id",), "section": ("equipment__section_id",)},
    "sc.apparatuscondition": {"plant": ("handover__shift__treatment_plant_id", "apparatus__equipment__section__treatment_plant_id"), "section": ("apparatus__equipment__section_id",)},
    "inventory.equipment": {"plant": ("section__treatment_plant_id",), "section": ("section_id",)},
    "weeklytasks.standardweeklytask": {"global_read": True},
    "weeklytasks.monthlydutyschedule": {"plant": ("treatment_plant_id",), "section": ("section_id",)},
    "weeklytasks.dutyweek": {"plant": ("schedule__treatment_plant_id",), "section": ("schedule__section_id",)},
    "weeklytasks.weeklydutytask": {"plant": ("treatment_plant_id", "week__schedule__treatment_plant_id"), "section": ("section_id", "week__schedule__section_id")},
    "weeklytasks.dailytaskassignment": {"plant": ("task__treatment_plant_id", "user__assigned_treatment_plant_id"), "section": ("task__section_id", "user__section_id")},
    "weeklytasks.dutyconfirmation": {"plant": ("assignment__task__treatment_plant_id", "assignment__user__assigned_treatment_plant_id"), "section": ("assignment__task__section_id", "assignment__user__section_id")},
}


def user_plant_section(user):
    if not user or not getattr(user, "is_authenticated", False):
        return None, None
    section_id = getattr(user, "section_id", None)
    plant_id = getattr(user, "assigned_treatment_plant_id", None)
    section = getattr(user, "section", None)
    lab = getattr(user, "assigned_lab", None)
    section_plant_id = getattr(section, "treatment_plant_id", None)
    lab_plant_id = getattr(lab, "treatment_plant_id", None)

    if plant_id:
        if (section_plant_id and section_plant_id != plant_id) or (lab_plant_id and lab_plant_id != plant_id):
            return plant_id, None
        return plant_id, section_id

    plant_id = lab_plant_id or section_plant_id
    if lab_plant_id and section_plant_id and section_plant_id != lab_plant_id:
        return lab_plant_id, None
    return plant_id, section_id


def model_scope_key(model):
    meta = getattr(model, "_meta", None)
    if not meta:
        return ""
    return f"{meta.app_label}.{meta.model_name}".lower()


def scoped_queryset(queryset, user):
    if getattr(user, "is_superuser", False):
        return queryset
    key = model_scope_key(queryset.model)
    paths = MODEL_SCOPE_PATHS.get(key)
    if paths is None:
        if queryset.model._meta.app_label in {"sc", "weeklytasks"}:
            return queryset.none()
        if queryset.model._meta.model_name in {"user", "customuser"}:
            plant_id, section_id = user_plant_section(user)
            if not plant_id or not section_id:
                return queryset.none()
            return queryset.filter(assigned_treatment_plant_id=plant_id, section_id=section_id)
        if queryset.model._meta.app_label == "core" and queryset.model._meta.model_name == "section":
            plant_id, section_id = user_plant_section(user)
            return queryset.filter(pk=section_id) if plant_id and section_id else queryset.none()
        if queryset.model._meta.app_label == "t" and queryset.model._meta.model_name == "treatmentplant":
            plant_id, section_id = user_plant_section(user)
            return queryset.filter(pk=plant_id) if plant_id and section_id else queryset.none()
        return queryset

    if paths.get("global_read"):
        return queryset
    if owner_path := paths.get("owner"):
        return queryset.filter(**{owner_path: getattr(user, "pk", None)})

    plant_id, section_id = user_plant_section(user)
    if not plant_id or not section_id:
        return queryset.none()

    if key == "sc.shiftassignment":
        return queryset.filter(
            shift__treatment_plant_id=plant_id,
            shift__section_requirements__section_id=section_id,
            staff__assigned_treatment_plant_id=plant_id,
            staff__section_id=section_id,
            duty_roster__treatment_plant_id=plant_id,
        ).distinct()
    if key == "sc.monthlydutyroster":
        return queryset.filter(
            treatment_plant_id=plant_id,
            assignments__is_deleted=False,
            assignments__staff__assigned_treatment_plant_id=plant_id,
            assignments__staff__section_id=section_id,
        ).distinct()
    if key == "sc.shifthandover":
        from django.db.models import Q

        outgoing_in_scope = Q(outgoing_supervisor__is_superuser=True) | Q(
            outgoing_supervisor__section_id=section_id,
            outgoing_supervisor__assigned_treatment_plant_id=plant_id,
        )
    if key == "sc.shiftassignment":
        return bool(
            obj.staff.assigned_treatment_plant_id == plant_id
            and obj.staff.section_id == section_id
            and obj.shift.treatment_plant_id == plant_id
            and obj.duty_roster.treatment_plant_id == plant_id
            and obj.shift.section_requirements.filter(section_id=section_id).exists()
        )
        incoming_in_scope = Q(incoming_supervisor__is_superuser=True) | Q(
            incoming_supervisor__section_id=section_id,
            incoming_supervisor__assigned_treatment_plant_id=plant_id,
        )
        return queryset.filter(
            shift__treatment_plant_id=plant_id,
            shift__section_requirements__section_id=section_id,
        ).filter(outgoing_in_scope, incoming_in_scope).distinct()
    if key == "sc.apparatuscondition":
        from django.db.models import Q

        outgoing_in_scope = Q(handover__outgoing_supervisor__is_superuser=True) | Q(
            handover__outgoing_supervisor__section_id=section_id,
            handover__outgoing_supervisor__assigned_treatment_plant_id=plant_id,
        )
        incoming_in_scope = Q(handover__incoming_supervisor__is_superuser=True) | Q(
            handover__incoming_supervisor__section_id=section_id,
            handover__incoming_supervisor__assigned_treatment_plant_id=plant_id,
        )
        return queryset.filter(
            handover__shift__treatment_plant_id=plant_id,
            handover__shift__section_requirements__section_id=section_id,
            apparatus__equipment__section__treatment_plant_id=plant_id,
            apparatus__equipment__section_id=section_id,
        ).filter(outgoing_in_scope, incoming_in_scope).distinct()
    if key == "weeklytasks.dailytaskassignment":
        from django.db.models import Q

        return queryset.filter(
            task__treatment_plant_id=plant_id,
            user__assigned_treatment_plant_id=plant_id,
            task__section_id=section_id,
            user__section_id=section_id,
        ).filter(
            Q(shift__isnull=True)
            | Q(
                shift__treatment_plant_id=plant_id,
                shift__section_requirements__section_id=section_id,
            )
        ).distinct()

    filters = {path: plant_id for path in paths.get("plant", ())}
    filters.update({path: section_id for path in paths.get("section", ())})
    return queryset.filter(**filters).distinct()


def object_in_scope(obj, user, *, allow_global_read=False):
    if getattr(user, "is_superuser", False):
        return True
    key = model_scope_key(type(obj))
    paths = MODEL_SCOPE_PATHS.get(key)
    if paths is None:
        if type(obj)._meta.app_label in {"sc", "weeklytasks"}:
            return False
        return True
    if paths.get("global_read"):
        return allow_global_read
    if owner_path := paths.get("owner"):
        return _resolve_attr(obj, owner_path) == getattr(user, "pk", None)

    plant_id, section_id = user_plant_section(user)
    if not plant_id or not section_id:
        return False

    if key == "sc.shift":
        return bool(
            obj.pk
            and obj.treatment_plant_id == plant_id
            and obj.section_requirements.filter(section_id=section_id).exists()
        )
    if key == "sc.monthlydutyroster":
        return bool(
            obj.pk
            and obj.treatment_plant_id == plant_id
            and obj.assignments.filter(
                is_deleted=False,
                staff__assigned_treatment_plant_id=plant_id,
                staff__section_id=section_id,
            ).exists()
        )
    if key == "sc.shifthandover":
        shift = getattr(obj, "shift", None)
        parties = (getattr(obj, "outgoing_supervisor", None), getattr(obj, "incoming_supervisor", None))
        return bool(
            shift
            and shift.treatment_plant_id == plant_id
            and shift.section_requirements.filter(section_id=section_id).exists()
            and all(
                person
                and (
                    person.is_superuser
                    or (
                        person.assigned_treatment_plant_id == plant_id
                        and person.section_id == section_id
                    )
                )
                for person in parties
            )
        )
    if key == "sc.apparatuscondition":
        handover = getattr(obj, "handover", None)
        apparatus = getattr(obj, "apparatus", None)
        return bool(
            handover
            and apparatus
            and object_in_scope(handover, user)
            and apparatus.equipment.section_id == section_id
            and apparatus.equipment.section.treatment_plant_id == plant_id
        )
    if key == "weeklytasks.dailytaskassignment":
        shift = getattr(obj, "shift", None)
        if shift and (
            shift.treatment_plant_id != plant_id
            or not shift.section_requirements.filter(section_id=section_id).exists()
        ):
            return False
    if any(_resolve_attr(obj, path) != plant_id for path in paths.get("plant", ())):
        return False
    for path in paths.get("section", ()):
        if "__" in path:
            if getattr(obj, "pk", None):
                if not scoped_queryset(type(obj).objects.filter(pk=obj.pk), user).exists():
                    return False
                break
            return False
        if _resolve_attr(obj, path) != section_id:
            return False
    return True


def _resolve_attr(obj, path):
    value = obj
    for part in path.split("__"):
        if value is None:
            return None
        value = getattr(value, part, None)
    return value
