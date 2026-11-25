"""Password rules for password-reset flows."""

import re
import unicodedata

from django.contrib.auth import get_user_model
from django.contrib.auth.hashers import check_password
from django.contrib.auth.password_validation import validate_password
from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils import timezone

from core.models import SystemConfiguration, UserPasswordHistory, validate_password_strength


PASSWORD_HISTORY_LIMIT = 6
MIN_RESET_PASSWORD_LENGTH = 12
_NAME_FRAGMENT_LENGTH = 3
User = get_user_model()


def _normalize_for_comparison(value):
    decomposed = unicodedata.normalize("NFKD", value or "")
    without_marks = "".join(char for char in decomposed if not unicodedata.combining(char))
    return "".join(char for char in without_marks.casefold() if char.isalnum())


def _name_fragments(user):
    """Return name fragments long enough to identify personal information."""
    fragments = set()
    for value in (user.first_name, user.last_name, user.get_full_name()):
        normalized = _normalize_for_comparison(value)
        decomposed = unicodedata.normalize("NFKD", value or "")
        without_marks = "".join(char for char in decomposed if not unicodedata.combining(char))
        words = re.findall(r"[^\W_]+", without_marks.casefold(), flags=re.UNICODE)
        for word in words:
            normalized_word = _normalize_for_comparison(word)
            if len(normalized_word) < _NAME_FRAGMENT_LENGTH:
                continue
            if len(normalized_word) == _NAME_FRAGMENT_LENGTH:
                fragments.add(normalized_word)
            else:
                fragments.update(
                    normalized_word[index:index + _NAME_FRAGMENT_LENGTH]
                    for index in range(len(normalized_word) - _NAME_FRAGMENT_LENGTH + 1)
                )
        # Also cover names stored as a single display value with unusual separators.
        if len(normalized) == _NAME_FRAGMENT_LENGTH:
            fragments.add(normalized)
    return fragments


def validate_reset_password(user, raw_password):
    """Apply Django's validators, site policy, name exclusion, and six-password history."""
    validate_password(raw_password, user)
    validate_password_strength(raw_password)

    required_character_rules = (
        (any(char.isupper() for char in raw_password), "Password must include an uppercase letter."),
        (any(char.islower() for char in raw_password), "Password must include a lowercase letter."),
        (any(char.isdigit() for char in raw_password), "Password must include a number."),
        (any(char in "!@#$%^&*()_+-=" for char in raw_password), "Password must include a special character."),
    )
    for rule_met, error_message in required_character_rules:
        if not rule_met:
            raise ValidationError(error_message)

    minimum_length = max(MIN_RESET_PASSWORD_LENGTH, int(SystemConfiguration.current().password_min_length or 0))
    if len(raw_password) < minimum_length:
        raise ValidationError(f"Password must be at least {minimum_length} characters long.")

    normalized_password = _normalize_for_comparison(raw_password)
    if any(fragment in normalized_password for fragment in _name_fragments(user)):
        raise ValidationError("Password must not contain your name or recognizable parts of it.")

    if user.check_password(raw_password):
        raise ValidationError("Choose a password you have not used in the last six password changes.")

    # The current password is checked separately, so the five newest archived
    # hashes complete the six most recent passwords, including the current one.
    recent_hashes = UserPasswordHistory.objects.filter(user=user).order_by("-created_at", "-pk").values_list(
        "password_hash", flat=True
    )[:PASSWORD_HISTORY_LIMIT - 1]
    if any(check_password(raw_password, encoded) for encoded in recent_hashes):
        raise ValidationError("Choose a password you have not used in the last six password changes.")


@transaction.atomic
def apply_password_reset(user_id, raw_password, expected_password_version=None):
    """Validate and atomically apply a reset, recording the prior hash for reuse checks."""
    user = User.objects.select_for_update().get(pk=user_id)

    current_password_version = user.password_last_set.isoformat() if user.password_last_set else ""
    if expected_password_version is not None and current_password_version != expected_password_version:
        raise ValidationError("This password reset link has already been used. Request a new one.")

    validate_reset_password(user, raw_password)
    prior_hash = user.password

    user.set_password(raw_password)
    user.password_last_set = timezone.now()
    user.save(update_fields=["password", "password_last_set"])

    if prior_hash:
        UserPasswordHistory.objects.create(user=user, password_hash=prior_hash)
        excess_ids = list(
            UserPasswordHistory.objects.filter(user=user)
            .order_by("-created_at", "-pk")
            .values_list("pk", flat=True)[PASSWORD_HISTORY_LIMIT:]
        )
        if excess_ids:
            UserPasswordHistory.objects.filter(pk__in=excess_ids).delete()

    return user
