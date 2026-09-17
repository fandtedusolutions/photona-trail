import os
import django
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "photona_project.settings")
django.setup()

from django.core.cache import cache
cache.set("test_key", {"foo": "bar"}, timeout=100)
print("Cache get:", cache.get("test_key"))
