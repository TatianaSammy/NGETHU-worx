# accounts/models.py

import logging
from typing import Optional, Any

import pyotp

from django.conf import settings
from django.contrib.auth.models import (
    AbstractBaseUser,
    PermissionsMixin,
    Group,
    Permission,
)
from django.core.cache import cache
from django.core.exceptions import ValidationError
from django.core.validators import EmailValidator
from django.db import (
    models,
    transaction,
    IntegrityError,
)
from django.db.models import F, Q
from django.db.models.signals import post_save
from django.dispatch import receiver
from django.templatetags.static import static
from django.utils import timezone

from core.managers import Luleti

logger = logging.getLogger(__name__)


# ==========================================================
# CONFIG CACHE
# ==========================================================
def get_config():
    """
    Fail-safe configuration loader.
    Uses Django cache backend instead of process memory.
    """

    try:
        config = cache.get("system_configuration")

        if config:
            return config

        from core.models import SystemConfiguration

        config = (
            SystemConfiguration.objects
            .only(
                "email_domain",
                "password_expire_days",
            )
            .filter(pk=1)
            .first()
        )

        cache.set(
            "system_configuration",
            config,
            timeout=300,
        )

        return config

    except Exception as exc:
        logger.exception(
            "Unable to load system configuration: %s",
            exc,
        )
        return None


# ==========================================================
# VALIDATORS
# ==========================================================
def validate_pf_number_imported(value):
    from core.models import validate_pf_number
    return validate_pf_number(value)


def validate_mobile_number_imported(value):
    from core.models import validate_mobile_number
    return validate_mobile_number(value)


# ==========================================================
# AUDIT
# ==========================================================
def audit_user_event(user, action: str):
    try:
        logger.info(
            "AUDIT | %s | %s",
            action,
            getattr(user, "username", "UNKNOWN"),
        )
    except Exception:
        pass


# ==========================================================
# MANAGER
# ==========================================================
class CustomUserManager(Luleti):

    def create_user(
        self,
        username: str,
        email: str,
        password: Optional[str] = None,
        **extra_fields: Any,
    ):
        username, email = self.normalize_credentials(
            username,
            email,
        )

        extra_fields = self.sanitize_input(
            extra_fields
        )

        errors = self.validate_credentials(
            username,
            password,
        )

        if errors:
            raise ValidationError(errors)

        user = self.model(
            username=username.upper().strip(),
            email=email.lower().strip(),
            **extra_fields,
        )

        if password:
            user.set_password(password)

        try:
            with transaction.atomic():
                user.full_clean()
                user.save(using=self._db)

        except IntegrityError:
            raise ValidationError(
                "User with these credentials already exists."
            )

        audit_user_event(
            user,
            "CREATE_USER",
        )

        return user

    def create_superuser(
        self,
        username,
        email,
        password=None,
        **extra_fields,
    ):
        extra_fields.setdefault(
            "is_staff",
            True,
        )
        extra_fields.setdefault(
            "is_superuser",
            True,
        )
        extra_fields.setdefault(
            "email_verified",
            True,
        )
        extra_fields.setdefault(
            "is_active",
            True,
        )

        if extra_fields.get(
            "is_staff"
        ) is not True:
            raise ValueError(
                "Superuser must have is_staff=True."
            )

        if extra_fields.get(
            "is_superuser"
        ) is not True:
            raise ValueError(
                "Superuser must have is_superuser=True."
            )

        return self.create_user(
            username,
            email,
            password,
            **extra_fields,
        )


# ==========================================================
# USER MODEL
# ==========================================================
class CustomUser(
    AbstractBaseUser,
    PermissionsMixin,
):

    # ======================================================
    # CORE
    # ======================================================
    username = models.CharField(
        max_length=20,
        unique=True,
        db_index=True,
        validators=[
            validate_pf_number_imported
        ],
    )

    email = models.EmailField(
        unique=True,
        db_index=True,
        validators=[EmailValidator()],
    )

    first_name = models.CharField(
        max_length=50,
        blank=True,
    )

    last_name = models.CharField(
        max_length=50,
        blank=True,
    )

    # ======================================================
    # ORGANIZATION
    # ======================================================
    section = models.ForeignKey(
        "core.Section",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="users",
    )

    assigned_treatment_plant = models.ForeignKey(
        "t.TreatmentPlant",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="assigned_users",
    )

    assigned_lab = models.ForeignKey(
        "laboratory.Laboratory",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="assigned_users",
    )

    # ======================================================
    # CONTACT
    # ======================================================
    mobile_number = models.CharField(
        max_length=13,
        unique=True,
        null=True,
        blank=True,
        validators=[
            validate_mobile_number_imported
        ],
    )

    profile_picture = models.ImageField(
        upload_to="profile_pictures/",
        blank=True,
        null=True,
    )

    # ======================================================
    # SECURITY
    # ======================================================
    is_active = models.BooleanField(
        default=True,
        db_index=True,
    )

    is_staff = models.BooleanField(
        default=False,
    )

    is_locked = models.BooleanField(
        default=False,
        db_index=True,
    )

    email_verified = models.BooleanField(
        default=False,
    )

    failed_login_attempts = (
        models.PositiveIntegerField(
            default=0
        )
    )

    password_last_set = models.DateTimeField(
        default=timezone.now
    )

    deactivated_at = models.DateTimeField(
        null=True,
        blank=True,
    )

    # ======================================================
    # MFA
    # ======================================================
    mfa_enabled = models.BooleanField(
        default=False
    )

    mfa_secret = models.CharField(
        max_length=255,
        blank=True,
        null=True,
    )

    # ======================================================
    # SYSTEM
    # ======================================================
    date_joined = models.DateTimeField(
        auto_now_add=True,
        db_index=True,
    )

    # ======================================================
    # PERMISSIONS
    # ======================================================
    groups = models.ManyToManyField(
        Group,
        related_name="accounts_users",
        blank=True,
    )

    user_permissions = models.ManyToManyField(
        Permission,
        related_name="accounts_users_permissions",
        blank=True,
    )

    # ======================================================
    # MANAGER
    # ======================================================
    objects = CustomUserManager()

    USERNAME_FIELD = "username"
    REQUIRED_FIELDS = ["email"]

    def has_perm(self, perm, obj=None):
        """Give active superusers full access and reserve delete perms to them."""
        if not self.is_active:
            return False
        if self.is_superuser:
            return True
        if isinstance(perm, str):
            _, separator, codename = perm.partition(".")
            if separator and codename.startswith("delete_"):
                return False
        return super().has_perm(perm, obj=obj)

    def has_module_perms(self, app_label):
        if self.is_active and self.is_superuser:
            return True
        return super().has_module_perms(app_label)

    def get_all_permissions(self, obj=None):
        permissions = super().get_all_permissions(obj)
        if self.is_active and self.is_superuser:
            return permissions
        return {
            permission
            for permission in permissions
            if not permission.partition(".")[2].startswith("delete_")
        }

    # ======================================================
    # META
    # ======================================================
    class Meta:
        ordering = ["username"]

        indexes = [
            models.Index(
                fields=["username"]
            ),
            models.Index(
                fields=["email"]
            ),
            models.Index(
                fields=["is_active"]
            ),
            models.Index(
                fields=["is_locked"]
            ),
            models.Index(
                fields=["date_joined"]
            ),
        ]

        constraints = [
            models.CheckConstraint(
                condition=Q(
                    failed_login_attempts__gte=0
                ),
                name="accounts_failed_login_non_negative",
            )
        ]

    # ======================================================
    # VALIDATION
    # ======================================================
    def clean(self):
        super().clean()

        if self.username:
            self.username = (
                self.username.upper().strip()
            )

        if self.email:
            self.email = (
                self.email.lower().strip()
            )

        config = get_config()

        if (
            config
            and self.email
            and config.email_domain
        ):
            domain = (
                self.email.split("@")[-1]
                .lower()
                .strip()
            )

            if (
                domain
                != config.email_domain.lower()
            ):
                raise ValidationError(
                    {
                        "email":
                        "Invalid email domain."
                    }
                )

        validate_pf_number_imported(
            self.username
        )

        if self.mobile_number:
            validate_mobile_number_imported(
                self.mobile_number
            )

        if not self.assigned_treatment_plant_id:
            if self.assigned_lab_id:
                self.assigned_treatment_plant = self.assigned_lab.treatment_plant
            elif self.section_id:
                section_plant = getattr(self.section, "treatment_plant", None)
                if section_plant:
                    self.assigned_treatment_plant = section_plant

        if self.section_id and self.assigned_treatment_plant_id:
            section_plant_id = getattr(self.section, "treatment_plant_id", None)
            if section_plant_id and section_plant_id != self.assigned_treatment_plant_id:
                raise ValidationError({
                    "section": (
                        f"Section '{self.section.name}' must belong to the user's assigned treatment plant."
                    )
                })

        if self.assigned_lab_id and self.assigned_treatment_plant_id:
            lab_plant_id = self.assigned_lab.treatment_plant_id
            if lab_plant_id != self.assigned_treatment_plant_id:
                raise ValidationError({
                    "assigned_lab": "The assigned laboratory must belong to the user's assigned treatment plant."
                })

    # ======================================================
    # SAVE
    # ======================================================
    def save(self, *args, **kwargs):
        if self.is_superuser:
            self.is_staff = True

        if self.username:
            self.username = (
                self.username.upper().strip()
            )

        if self.email:
            self.email = (
                self.email.lower().strip()
            )

        super().save(
            *args,
            **kwargs,
        )

    # ======================================================
    # PASSWORD
    # ======================================================
    def set_password(
        self,
        raw_password,
    ):
        super().set_password(
            raw_password
        )
        self.password_last_set = (
            timezone.now()
        )

    def is_password_expired(self):
        config = get_config()

        days = (
            getattr(
                config,
                "password_expire_days",
                None,
            )
            or 90
        )

        return (
            timezone.now()
            >
            self.password_last_set
            + timezone.timedelta(
                days=days
            )
        )

    # ======================================================
    # DISPLAY
    # ======================================================
    @property
    def full_name(self):
        return (
            f"{self.first_name} "
            f"{self.last_name}"
        ).strip() or self.username

    def get_full_name(self):
        """Provide Django's standard user name API for workflow integrations."""
        return self.full_name

    @property
    def short_name(self):
        return (
            self.first_name
            or self.username
        )

    @property
    def profile_picture_url(self):
        try:
            if self.profile_picture:
                return self.profile_picture.url
        except Exception:
            pass

        return static(
            "img/default.png"
        )

    def __str__(self):
        return (
            self.full_name
            or self.username
            or str(self.pk)
        )

    # ======================================================
    # ACCOUNT LOCKING
    # ======================================================
    def increment_failed_login_attempts(
        self,
    ):
        self.__class__.objects.filter(
            pk=self.pk
        ).update(
            failed_login_attempts=F(
                "failed_login_attempts"
            ) + 1
        )

        self.refresh_from_db(
            fields=[
                "failed_login_attempts"
            ]
        )

        max_attempts = getattr(
            settings,
            "MAX_FAILED_LOGIN_ATTEMPTS",
            5,
        )

        if (
            self.failed_login_attempts
            >= max_attempts
        ):
            self.is_locked = True
            self.save(
                update_fields=[
                    "is_locked"
                ]
            )

            audit_user_event(
                self,
                "ACCOUNT_LOCKED",
            )

    def reset_failed_login_attempts(
        self,
    ):
        self.failed_login_attempts = 0
        self.save(
            update_fields=[
                "failed_login_attempts"
            ]
        )

    def lock_account(self):
        self.is_locked = True
        self.save(
            update_fields=[
                "is_locked"
            ]
        )

    def unlock_account(self):
        self.is_locked = False
        self.failed_login_attempts = 0

        self.save(
            update_fields=[
                "is_locked",
                "failed_login_attempts",
            ]
        )

    def deactivate_account(self):
        self.is_active = False
        self.deactivated_at = (
            timezone.now()
        )

        self.save(
            update_fields=[
                "is_active",
                "deactivated_at",
            ]
        )

    def activate_account(self):
        self.is_active = True
        self.deactivated_at = None

        self.save(
            update_fields=[
                "is_active",
                "deactivated_at",
            ]
        )

    # ======================================================
    # MFA
    # ======================================================
    def enable_mfa(
        self,
        secret=None,
    ):
        secret = (
            secret
            or pyotp.random_base32()
        )

        self.mfa_enabled = True
        self.mfa_secret = secret

        self.save(
            update_fields=[
                "mfa_enabled",
                "mfa_secret",
            ]
        )

        return secret

    def disable_mfa(self):
        self.mfa_enabled = False
        self.mfa_secret = None

        self.save(
            update_fields=[
                "mfa_enabled",
                "mfa_secret",
            ]
        )

    def verify_mfa(
        self,
        token: str,
    ) -> bool:
        if (
            not self.mfa_enabled
            or not self.mfa_secret
        ):
            return False

        try:
            totp = pyotp.TOTP(
                self.mfa_secret
            )

            return totp.verify(
                token,
                valid_window=1,
            )

        except Exception:
            return False


# ==========================================================
# SIGNALS
# ==========================================================
@receiver(
    post_save,
    sender=CustomUser,
)
def user_post_save(
    sender,
    instance,
    created,
    **kwargs,
):
    try:
        action = (
            "CREATED"
            if created
            else "UPDATED"
        )

        audit_user_event(
            instance,
            f"USER_{action}",
        )

    except Exception:
        logger.exception(
            "Post-save audit failed."
        )
