# accounts/signals.py
import logging
from django.db import transaction
from django.db.models.signals import m2m_changed, post_save
from django.dispatch import receiver
from django.contrib.auth.models import Permission
from .models import CustomUser

logger = logging.getLogger(__name__)


def _get_group_permissions(user: CustomUser):
    """
    Return all permissions granted via the user's groups.
    """
    return set(
        Permission.objects.filter(group__in=user.groups.all()).distinct()
    )


def _sync_permissions(user: CustomUser):
    """
    Merge group permissions into user_permissions.
    - Group membership is mandatory
    - User-level permissions are preserved
    - Zero DB writes if already synced
    """
    group_perms = _get_group_permissions(user)

    if not group_perms:
        raise ValueError("CustomUser must belong to at least one group.")

    current_perms = set(user.user_permissions.all())
    missing_perms = group_perms - current_perms

    if not missing_perms:
        logger.debug(
            "Permissions already synced for user '%s'. No action taken.",
            user.username,
        )
        return

    user.user_permissions.add(*missing_perms)

    logger.info(
        "Added %d group permissions to user '%s'.",
        len(missing_perms),
        user.username,
    )


# ============================================================
# Sync when groups are ADDED (never on clear)
# ============================================================
@receiver(m2m_changed, sender=CustomUser.groups.through)
def sync_group_permissions(sender, instance, action, **kwargs):
    """
    Sync permissions only AFTER groups are added.
    Admin clears M2M first; we must ignore that phase.
    """
    if action != "post_add":
        return

    transaction.on_commit(lambda: _sync_permissions(instance))


# ============================================================
# Sync on creation (API / scripts)
# ============================================================
@receiver(post_save, sender=CustomUser)
def ensure_group_permissions_on_create(sender, instance, created, **kwargs):
    """
    Apply group permissions for newly created users.
    Deferred until transaction commit so M2M relations exist.
    """
    if not created:
        return

    transaction.on_commit(lambda: _sync_permissions(instance))
