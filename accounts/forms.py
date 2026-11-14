# accounts/forms.py

import pyotp

from django import forms
from django.contrib.auth import password_validation
from django.contrib.auth.forms import ReadOnlyPasswordHashField
from django.contrib.auth.models import Group, Permission
from django.core.exceptions import ValidationError
from django.core.validators import FileExtensionValidator
from django.db import transaction
from django.utils import timezone

from accounts.models import CustomUser, get_config
from core.models import (
    Section,
    SystemConfiguration,
    IPRestriction,
)


# ==========================================================
# MIXINS
# ==========================================================

class BootstrapFormMixin:
    """
    Automatically applies Bootstrap styling.
    """

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        for field_name, field in self.fields.items():
            widget = field.widget

            if isinstance(
                widget,
                (
                    forms.CheckboxInput,
                    forms.CheckboxSelectMultiple,
                ),
            ):
                widget.attrs["class"] = "form-check-input"

            elif isinstance(
                widget,
                (
                    forms.Select,
                    forms.SelectMultiple,
                ),
            ):
                widget.attrs["class"] = "form-select"

            else:
                widget.attrs["class"] = "form-control"

            widget.attrs.setdefault(
                "autocomplete",
                "off",
            )


# ==========================================================
# PROFILE PICTURE
# ==========================================================

class ProfilePictureForm(
    BootstrapFormMixin,
    forms.ModelForm,
):
    class Meta:
        model = CustomUser
        fields = ["profile_picture"]

        widgets = {
            "profile_picture": forms.FileInput(
                attrs={
                    "accept": "image/*",
                }
            )
        }

    def clean_profile_picture(self):
        image = self.cleaned_data.get(
            "profile_picture"
        )

        if not image:
            return image

        if image.size > 5 * 1024 * 1024:
            raise ValidationError(
                "Image cannot exceed 5MB."
            )

        return image


# ==========================================================
# CREATE USER
# ==========================================================

class CustomUserCreateForm(
    BootstrapFormMixin,
    forms.ModelForm,
):
    password1 = forms.CharField(
        label="Password",
        strip=False,
        widget=forms.PasswordInput,
    )

    password2 = forms.CharField(
        label="Confirm Password",
        strip=False,
        widget=forms.PasswordInput,
    )

    class Meta:
        model = CustomUser
        fields = [
            "username",
            "email",
            "first_name",
            "last_name",
            "mobile_number",
            "assigned_treatment_plant",
            "section",
            "profile_picture",
            "is_active",
            "is_staff",
            "email_verified",
            "groups",
            "user_permissions",
        ]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        from django.db.models import Q
        plant_id = None
        if self.is_bound:
            plant_id = self.data.get("assigned_treatment_plant")
        elif self.instance and getattr(self.instance, "assigned_treatment_plant_id", None):
            plant_id = self.instance.assigned_treatment_plant_id

        if "section" in self.fields:
            if plant_id:
                self.fields["section"].queryset = Section.objects.filter(
                    Q(treatment_plant_id=plant_id) | Q(treatment_plant__isnull=True),
                    is_active=True
                ).order_by("name")
            else:
                self.fields["section"].queryset = Section.objects.filter(is_active=True).order_by("name")

        widgets = {
            "groups": forms.SelectMultiple(
                attrs={
                    "size": 10,
                }
            ),
            "user_permissions": forms.SelectMultiple(
                attrs={
                    "size": 12,
                }
            ),
        }

    def clean_email(self):
        email = (
            self.cleaned_data["email"]
            .lower()
            .strip()
        )

        if (
            CustomUser.objects
            .filter(email=email)
            .exists()
        ):
            raise ValidationError(
                "Email already exists."
            )

        return email

    def clean_password2(self):
        p1 = self.cleaned_data.get(
            "password1"
        )
        p2 = self.cleaned_data.get(
            "password2"
        )

        if p1 != p2:
            raise ValidationError(
                "Passwords do not match."
            )

        password_validation.validate_password(
            p2
        )

        return p2

    @transaction.atomic
    def save(self, commit=True):
        user = super().save(commit=False)
        user.set_password(
            self.cleaned_data["password1"]
        )

        if commit:
            user.save()
            self.save_m2m()

        return user


# ==========================================================
# UPDATE USER
# ==========================================================

class CustomUserForm(
    BootstrapFormMixin,
    forms.ModelForm,
):
    class Meta:
        model = CustomUser
        exclude = [
            "password",
            "last_login",
            "date_joined",
            "password_last_set",
            "mfa_secret",
            "failed_login_attempts",
            "deactivated_at",
        ]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        from django.db.models import Q
        plant_id = None
        if self.is_bound:
            plant_id = self.data.get("assigned_treatment_plant")
        elif self.instance and getattr(self.instance, "assigned_treatment_plant_id", None):
            plant_id = self.instance.assigned_treatment_plant_id

        if "section" in self.fields:
            if plant_id:
                self.fields["section"].queryset = Section.objects.filter(
                    Q(treatment_plant_id=plant_id) | Q(treatment_plant__isnull=True),
                    is_active=True
                ).order_by("name")
            else:
                self.fields["section"].queryset = Section.objects.filter(is_active=True).order_by("name")

    def clean_email(self):
        email = (
            self.cleaned_data["email"]
            .lower()
            .strip()
        )

        qs = (
            CustomUser.objects
            .exclude(pk=self.instance.pk)
            .filter(email=email)
        )

        if qs.exists():
            raise ValidationError(
                "Email already exists."
            )

        return email


# ==========================================================
# ADMIN USER FORM
# ==========================================================

class CustomUserAdminForm(
    BootstrapFormMixin,
    forms.ModelForm,
):
    password = ReadOnlyPasswordHashField()

    class Meta:
        model = CustomUser
        fields = "__all__"


# ==========================================================
# PROFILE
# ==========================================================

class UserProfileForm(
    BootstrapFormMixin,
    forms.ModelForm,
):
    class Meta:
        model = CustomUser
        fields = [
            "first_name",
            "last_name",
            "email",
            "mobile_number",
            "profile_picture",
        ]


# ==========================================================
# SEARCH
# ==========================================================

class UserSearchForm(
    BootstrapFormMixin,
    forms.Form,
):
    search = forms.CharField(
        required=False,
    )

    section = forms.ModelChoiceField(
        queryset=Section.objects.all(),
        required=False,
    )

    group = forms.ModelChoiceField(
        queryset=Group.objects.all(),
        required=False,
    )

    is_active = forms.NullBooleanField(
        required=False,
    )

    is_locked = forms.NullBooleanField(
        required=False,
    )


# ==========================================================
# PASSWORD CHANGE
# ==========================================================

class UserPasswordChangeForm(
    BootstrapFormMixin,
    forms.Form,
):
    old_password = forms.CharField(
        widget=forms.PasswordInput
    )

    new_password1 = forms.CharField(
        widget=forms.PasswordInput
    )

    new_password2 = forms.CharField(
        widget=forms.PasswordInput
    )

    def __init__(
        self,
        user,
        *args,
        **kwargs,
    ):
        self.user = user
        super().__init__(*args, **kwargs)

    def clean_old_password(self):
        password = self.cleaned_data[
            "old_password"
        ]

        if not self.user.check_password(
            password
        ):
            raise ValidationError(
                "Incorrect password."
            )

        return password

    def clean(self):
        cleaned = super().clean()

        p1 = cleaned.get(
            "new_password1"
        )
        p2 = cleaned.get(
            "new_password2"
        )

        if p1 != p2:
            raise ValidationError(
                "Passwords do not match."
            )

        password_validation.validate_password(
            p1,
            self.user,
        )

        return cleaned

    def save(self):
        password = self.cleaned_data[
            "new_password1"
        ]

        self.user.set_password(password)
        self.user.password_last_set = (
            timezone.now()
        )
        self.user.save()

        return self.user


# ==========================================================
# ADMIN PASSWORD RESET
# ==========================================================

class AdminPasswordResetForm(
    UserPasswordChangeForm
):
    def clean_old_password(self):
        return ""


# ==========================================================
# MFA
# ==========================================================

class MFAEnableForm(forms.Form):
    enable = forms.BooleanField(
        required=False
    )


class MFAVerificationForm(
    BootstrapFormMixin,
    forms.Form,
):
    token = forms.CharField(
        max_length=6,
        min_length=6,
    )

    def __init__(
        self,
        user,
        *args,
        **kwargs,
    ):
        self.user = user
        super().__init__(*args, **kwargs)

    def clean_token(self):
        token = self.cleaned_data[
            "token"
        ]

        if not self.user.verify_mfa(
            token
        ):
            raise ValidationError(
                "Invalid token."
            )

        return token


# ==========================================================
# GROUPS
# ==========================================================

class UserGroupForm(
    BootstrapFormMixin,
    forms.ModelForm,
):
    class Meta:
        model = CustomUser
        fields = ["groups"]


# ==========================================================
# PERMISSIONS
# ==========================================================

class UserPermissionForm(
    BootstrapFormMixin,
    forms.ModelForm,
):
    class Meta:
        model = CustomUser
        fields = ["user_permissions"]


# ==========================================================
# LOCK / ACTIVATE
# ==========================================================

class UserActivationForm(
    BootstrapFormMixin,
    forms.ModelForm,
):
    class Meta:
        model = CustomUser
        fields = ["is_active"]


class UserLockForm(
    BootstrapFormMixin,
    forms.ModelForm,
):
    class Meta:
        model = CustomUser
        fields = ["is_locked"]


# ==========================================================
# BULK ACTIONS
# ==========================================================

class BulkUserActionForm(
    BootstrapFormMixin,
    forms.Form,
):
    ACTIONS = (
        ("activate", "Activate"),
        ("deactivate", "Deactivate"),
        ("lock", "Lock"),
        ("unlock", "Unlock"),
        ("delete", "Delete"),
        ("enable_mfa", "Enable MFA"),
        ("disable_mfa", "Disable MFA"),
    )

    action = forms.ChoiceField(
        choices=ACTIONS
    )

    users = forms.ModelMultipleChoiceField(
        queryset=CustomUser.objects.all(),
        widget=forms.SelectMultiple(
            attrs={
                "size": 15,
            }
        ),
    )


# ==========================================================
# SECTION FORM
# ==========================================================

class SectionForm(
    BootstrapFormMixin,
    forms.ModelForm,
):
    class Meta:
        model = Section
        fields = "__all__"


# ==========================================================
# IP RESTRICTION FORM
# ==========================================================

class IPRestrictionForm(
    BootstrapFormMixin,
    forms.ModelForm,
):
    class Meta:
        model = IPRestriction
        fields = "__all__"


# ==========================================================
# SYSTEM CONFIGURATION
# ==========================================================

class SystemConfigurationForm(
    BootstrapFormMixin,
    forms.ModelForm,
):
    class Meta:
        model = SystemConfiguration
        fields = "__all__"


# ==========================================================
# GROUP MANAGEMENT FORM
# ==========================================================

class GroupAdminForm(
    BootstrapFormMixin,
    forms.ModelForm,
):
    permissions = forms.ModelMultipleChoiceField(
        queryset=Permission.objects.select_related("content_type").order_by("content_type__app_label", "name"),
        required=False,
        widget=forms.SelectMultiple(
            attrs={"class": "form-select", "size": 10}
        ),
        help_text="Hold Ctrl (or Cmd) to select multiple permissions.",
    )

    class Meta:
        model = Group
        fields = ["name", "permissions"]


# ==========================================================
# PERMISSION MANAGEMENT FORM
# ==========================================================

class PermissionAdminForm(
    BootstrapFormMixin,
    forms.ModelForm,
):
    class Meta:
        model = Permission
        fields = ["name", "codename", "content_type"]


# ==========================================================
# TREATMENT PLANT & LAB FORMS
# ==========================================================

class TreatmentPlantAdminForm(
    BootstrapFormMixin,
    forms.ModelForm,
):
    class Meta:
        from t.models import TreatmentPlant
        model = TreatmentPlant
        fields = ["code", "name", "location"]


class LaboratoryAdminForm(
    BootstrapFormMixin,
    forms.ModelForm,
):
    class Meta:
        from laboratory.models import Laboratory
        model = Laboratory
        fields = ["name", "treatment_plant"]


class ListImportForm(forms.Form):
    file = forms.FileField(
        required=True,
        widget=forms.FileInput(attrs={"class": "form-control", "accept": ".csv,.xlsx,.xls"}),
        help_text="Select a CSV or Excel (.xlsx) file to import.",
    )
