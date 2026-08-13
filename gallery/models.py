from django.contrib.auth.models import User
from django.db import models
import numpy as np

class SubscriptionPlan(models.Model):
    name = models.CharField(max_length=255)
    storage_limit_mb = models.IntegerField(help_text="Storage limit in Megabytes (MB)")

    def __block__(self):
        return self.name

    def __str__(self):
        return f"{self.name} ({self.storage_limit_mb} MB)"

class UserProfile(models.Model):
    ROLE_CHOICES = (
        ('studio_man', 'Studio Man'),
        ('camera_man', 'Camera Man'),
        ('event_organizer', 'Event Organizer'),
    )
    user = models.OneToOneField(User, on_delete=models.CASCADE, related_name='profile')
    role = models.CharField(max_length=50, choices=ROLE_CHOICES)
    subscription_plan = models.ForeignKey(SubscriptionPlan, on_delete=models.SET_NULL, null=True, blank=True)
    used_storage_mb = models.FloatField(default=0.0)

    @property
    def storage_percentage(self):
        if not self.subscription_plan or self.subscription_plan.storage_limit_mb == 0:
            return 0
        return min(100, int((self.used_storage_mb / self.subscription_plan.storage_limit_mb) * 100))

    def __str__(self):
        return f"{self.user.username} ({self.get_role_display()})"

import uuid
from django.utils.text import slugify

class Event(models.Model):
    name = models.CharField(max_length=255)
    description = models.TextField(blank=True, null=True)
    owner = models.ForeignKey(User, on_delete=models.CASCADE, related_name='events', null=True, blank=True)
    slug = models.SlugField(max_length=255, unique=True, null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    def save(self, *args, **kwargs):
        if not self.slug:
            random_code = uuid.uuid4().hex[:4]
            self.slug = f"{slugify(self.name)}-{random_code}"
        super().save(*args, **kwargs)

    def __str__(self):
        return self.name

class GalleryImage(models.Model):
    file = models.ImageField(upload_to='events/')
    thumbnail = models.ImageField(upload_to='thumbnails/', null=True, blank=True)
    filename = models.CharField(max_length=255)
    event = models.ForeignKey(Event, on_delete=models.CASCADE, related_name='images', null=True, blank=True)
    total_faces = models.IntegerField(default=0)
    uploaded_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return self.filename

class FaceEmbedding(models.Model):
    image = models.ForeignKey(GalleryImage, on_delete=models.CASCADE, related_name='embeddings')
    embedding_data = models.JSONField()  # List of floats representing the 512-d face embedding
    person_label = models.CharField(max_length=50, blank=True, null=True)

    def __str__(self):
        return f"Embedding for {self.image.filename}"

    def get_numpy_embedding(self):
        return np.array(self.embedding_data, dtype=np.float32)


class EventShareLink(models.Model):
    """A public shareable link for an event, optionally password-protected."""
    event       = models.ForeignKey(Event, on_delete=models.CASCADE, related_name='share_links')
    token       = models.UUIDField(default=uuid.uuid4, unique=True, editable=False)
    label       = models.CharField(max_length=255, default='Share Link', help_text='Organizer-visible label')
    person_label = models.CharField(max_length=100, blank=True, null=True, help_text='If set, limits access to this face group label only')
    password    = models.CharField(max_length=255, blank=True, null=True, help_text='Hashed password (blank = no protection)')
    is_active   = models.BooleanField(default=True)
    created_at  = models.DateTimeField(auto_now_add=True)
    expires_at  = models.DateTimeField(null=True, blank=True, help_text='Optional expiry. Leave blank for no expiry.')

    def __str__(self):
        return f"Share:{self.label} [{self.event.name}]"

    @property
    def is_expired(self):
        if not self.expires_at:
            return False
        from django.utils import timezone
        return timezone.now() > self.expires_at

    @property
    def is_accessible(self):
        return self.is_active and not self.is_expired

