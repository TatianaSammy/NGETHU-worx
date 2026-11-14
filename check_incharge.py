import django, os
os.environ["DJANGO_SETTINGS_MODULE"]="qad.settings"
django.setup()
from django.apps import apps
for model in apps.get_models():
    for f in model._meta.fields:
        name = f.name.lower()
        if any(k in name for k in ['incharge', 'in_charge', 'charge', 'section_head', 'lead']):
            print(f"{model._meta.label}.{f.name} ({f.get_internal_type()})")

from accounts.models import Section, Role, UserProfile
print("Section fields:", [f.name for f in Section._meta.fields])
print("Role fields:", [f.name for f in Role._meta.fields])
print("UserProfile fields:", [f.name for f in UserProfile._meta.fields])

