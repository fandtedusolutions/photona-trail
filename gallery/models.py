from django.contrib.auth.models import User
from django.db import models
import numpy as np

class SubscriptionPlan(models.Model):
    name = models.CharField(max_length=255)
    storage_limit_mb = models.IntegerField(help_text="Storage limit in Megabytes (MB)")
    album_limit = models.IntegerField(default=0, help_text="Maximum number of albums/events allowed (0 for unlimited)")
    is_active = models.BooleanField(default=True, help_text="Set to False to hide this plan from users.")

    def __block__(self):
        return self.name

    def __str__(self):
        limit_str = f"{self.album_limit} albums" if self.album_limit > 0 else "unlimited albums"
        return f"{self.name} ({self.storage_limit_mb} MB, {limit_str})"

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
    custom_storage_limit_mb = models.IntegerField(null=True, blank=True, help_text="Override plan storage limit (in MB)")
    custom_album_limit = models.IntegerField(null=True, blank=True, help_text="Override plan album limit (0 for unlimited, null to use plan default)")
    raw_password = models.CharField(max_length=255, blank=True, null=True, help_text="Store plain text password for admin reference")

    # Trial & Payment Fields
    trial_start_date = models.DateTimeField(auto_now_add=True)
    trial_duration_days = models.IntegerField(default=7, help_text="Number of days the trial is valid")
    is_paid = models.BooleanField(default=False, help_text="Set to True if the user has paid, bypassing trial limits")


    @property
    def effective_storage_limit_mb(self):
        base_limit = self.subscription_plan.storage_limit_mb if self.subscription_plan else 0
        extra = self.custom_storage_limit_mb if self.custom_storage_limit_mb is not None else 0
        return base_limit + extra

    @property
    def effective_album_limit(self):
        if self.custom_album_limit is not None:
            return self.custom_album_limit
        if self.subscription_plan:
            return self.subscription_plan.album_limit
        return 0

    @property
    def total_albums_count(self):
        return self.user.events.count()

    @property
    def has_reached_album_limit(self):
        if self.user.is_superuser:
            return False
        limit = self.effective_album_limit
        if not limit or limit <= 0:
            return False
        return self.total_albums_count >= limit

    @property
    def albums_remaining(self):
        limit = self.effective_album_limit
        if not limit or limit <= 0:
            return None
        return max(0, limit - self.total_albums_count)

    @property
    def storage_percentage(self):

        limit = self.effective_storage_limit_mb
        if limit == 0:
            return 0
        return min(100, int((self.used_storage_mb / limit) * 100))

    @property
    def trial_expire_date(self):
        import datetime
        if not self.trial_start_date:
            return None
        days = self.trial_duration_days if self.trial_duration_days is not None else 7
        return self.trial_start_date + datetime.timedelta(days=days)

    @property
    def is_active_trial(self):
        from django.utils import timezone
        if self.is_paid:
            return True
        expire_dt = self.trial_expire_date
        if not expire_dt:
            return True
        return timezone.now() < expire_dt

    @property
    def trial_days_remaining(self):
        from django.utils import timezone
        if self.is_paid:
            return None
        expire_dt = self.trial_expire_date
        if not expire_dt:
            return None
        diff = expire_dt - timezone.now()
        if diff.total_seconds() <= 0:
            return 0
        return max(0, diff.days + (1 if diff.seconds > 0 else 0))

    def __str__(self):
        return f"{self.user.username} ({self.get_role_display()})"

import uuid
from django.utils.text import slugify

def event_cover_upload_path(instance, filename):
    organizer = instance.owner.username if instance.owner else 'unassigned'
    return f'covers/{organizer}/{instance.slug}/{filename}'

def event_watermark_upload_path(instance, filename):
    organizer = instance.owner.username if instance.owner else 'unassigned'
    return f'watermarks/{organizer}/{instance.slug}/{filename}'

class Event(models.Model):
    name = models.CharField(max_length=255)
    description = models.TextField(blank=True, null=True)
    owner = models.ForeignKey(User, on_delete=models.CASCADE, related_name='events', null=True, blank=True)
    slug = models.SlugField(max_length=255, unique=True, null=True, blank=True)
    cover_image = models.ImageField(upload_to=event_cover_upload_path, null=True, blank=True)
    event_date = models.DateField(null=True, blank=True)
    compression_percentage = models.IntegerField(default=100, help_text="Image compression percentage (1-100). 100 means no extra compression.")
    default_preset = models.CharField(max_length=100, default='original', blank=True, help_text="Default non-destructive preset overlay applied across event photos")
    default_preset_settings = models.JSONField(default=dict, blank=True, help_text="Fine-tune adjustments settings dictionary for default preset")
    
    # Watermark Settings
    watermark_enabled = models.BooleanField(default=False)
    watermark_type = models.CharField(max_length=20, default='text')  # 'text' | 'image'
    watermark_text = models.CharField(max_length=120, blank=True, default='')
    watermark_image = models.ImageField(upload_to=event_watermark_upload_path, null=True, blank=True)
    watermark_position = models.CharField(max_length=30, default='bottom-right')  # 9 positions: top-left, top-center, top-right, center-left, center, center-right, bottom-left, bottom-center, bottom-right
    watermark_opacity = models.IntegerField(default=75)  # 10 to 100
    watermark_size = models.IntegerField(default=25)  # 10 to 60 (font/logo scale %)
    
    expires_at = models.DateTimeField(null=True, blank=True, help_text="Optional event expiry. Leave blank for no expiry.")
    created_at = models.DateTimeField(auto_now_add=True)

    @property
    def is_expired(self):
        if not self.expires_at:
            return False
        from django.utils import timezone
        return timezone.now() > self.expires_at

    def get_watermark_config(self):
        if not self.watermark_enabled:
            return None
        return {
            'enabled': True,
            'type': self.watermark_type or 'text',
            'text': self.watermark_text or self.name,
            'image_url': self.watermark_image.url if self.watermark_image else None,
            'position': self.watermark_position or 'bottom-right',
            'opacity': self.watermark_opacity or 75,
            'size': self.watermark_size or 25,
        }

    def save(self, *args, **kwargs):
        if not self.slug:
            random_code = uuid.uuid4().hex[:4]
            self.slug = f"{slugify(self.name)}-{random_code}"
        super().save(*args, **kwargs)

    def __str__(self):
        return self.name

def event_image_upload_path(instance, filename):
    organizer = instance.event.owner.username if instance.event and instance.event.owner else 'unassigned'
    event_slug = instance.event.slug if instance.event else 'unassigned'
    return f'events/{organizer}/{event_slug}/{filename}'

def event_thumbnail_upload_path(instance, filename):
    organizer = instance.event.owner.username if instance.event and instance.event.owner else 'unassigned'
    event_slug = instance.event.slug if instance.event else 'unassigned'
    return f'thumbnails/{organizer}/{event_slug}/{filename}'

class GalleryImage(models.Model):
    file = models.ImageField(upload_to=event_image_upload_path)
    thumbnail = models.ImageField(upload_to=event_thumbnail_upload_path, null=True, blank=True)
    filename = models.CharField(max_length=255)
    event = models.ForeignKey(Event, on_delete=models.CASCADE, related_name='images', null=True, blank=True)
    total_faces = models.IntegerField(default=0)
    preset_name = models.CharField(max_length=100, default='original', blank=True, help_text="Active non-destructive preset overlay ('bw', 'warm', 'editorial', 'original', etc.)")
    preset_settings = models.JSONField(default=dict, blank=True, help_text="Fine-tune adjustments settings dictionary (exposure, contrast, saturation, grain, etc.)")
    uploaded_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return self.filename

    def get_secure_file_token(self):
        from django.core.signing import Signer
        return Signer().sign(f"{self.id}:file")

    def get_secure_thumbnail_token(self):
        from django.core.signing import Signer
        if not self.thumbnail:
            return self.get_secure_file_token()
        return Signer().sign(f"{self.id}:thumbnail")

    def get_secure_processed_token(self):
        from django.core.signing import Signer
        return Signer().sign(f"{self.id}:processed")

    def get_secure_processed_thumbnail_token(self):
        from django.core.signing import Signer
        return Signer().sign(f"{self.id}:processed_thumbnail")

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
    search_only = models.BooleanField(default=False, help_text='If true, guests can only upload a selfie to find their photos, and cannot browse the full gallery.')
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

class GuestLead(models.Model):
    """Lead generated from a guest performing a selfie search."""
    event = models.ForeignKey(Event, on_delete=models.CASCADE, related_name='leads')
    name = models.CharField(max_length=255)
    age = models.IntegerField()
    phone = models.CharField(max_length=50)
    email = models.EmailField(blank=True, null=True)
    selfie = models.ImageField(upload_to='leads/selfies/')
    created_at = models.DateTimeField(auto_now_add=True)

    def get_secure_selfie_token(self):
        from django.core.signing import Signer
        return Signer().sign(f"{self.id}:selfie")

    def __str__(self):
        return f"{self.name} - {self.event.name}"


def personal_link_selfie_upload_path(instance, filename):
    event_slug = instance.event.slug if instance.event else 'unassigned'
    return f'personal_links/{event_slug}/{instance.token}_{filename}'


class PersonalGalleryLink(models.Model):
    """
    A persistent personal gallery link generated when a user or guest searches
    via AI selfie search. It allows them to access their matched photos anytime
    until the event (or share link) expires.
    """
    token = models.UUIDField(default=uuid.uuid4, unique=True, editable=False)
    event = models.ForeignKey(Event, on_delete=models.CASCADE, related_name='personal_gallery_links')
    share_link = models.ForeignKey(EventShareLink, on_delete=models.SET_NULL, null=True, blank=True, related_name='personal_links')
    guest_lead = models.ForeignKey(GuestLead, on_delete=models.SET_NULL, null=True, blank=True, related_name='personal_links')

    searcher_name = models.CharField(max_length=255, blank=True, default='')
    selfie_image = models.ImageField(upload_to=personal_link_selfie_upload_path, null=True, blank=True)
    selfie_thumbnail_b64 = models.TextField(blank=True, default='')

    # Matched images & scores
    images = models.ManyToManyField(GalleryImage, related_name='personal_links', blank=True)
    match_scores = models.JSONField(default=dict, blank=True)  # {str(image_id): float(score)}
    query_embedding = models.JSONField(null=True, blank=True)  # 512-d normalized face vector for instant auto-sync

    created_at = models.DateTimeField(auto_now_add=True)
    expires_at = models.DateTimeField(null=True, blank=True, help_text="Expiry date inherited from event or share link.")

    view_count = models.IntegerField(default=0)
    last_viewed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ['-created_at']

    def __str__(self):
        name = self.searcher_name or (self.guest_lead.name if self.guest_lead else 'Guest')
        return f"Personal Gallery: {name} - {self.event.name} ({self.token})"

    @property
    def is_expired(self):
        from django.utils import timezone
        now = timezone.now()
        # 1. Direct expiry on this personal link
        if self.expires_at and now > self.expires_at:
            return True
        # 2. Event expiry
        if self.event and self.event.expires_at and now > self.event.expires_at:
            return True
        # 3. Share link expiry (if searched through a share link)
        if self.share_link and self.share_link.is_expired:
            return True
        return False

    @property
    def is_accessible(self):
        return not self.is_expired

    def get_effective_expires_at(self):
        """Return the nearest expiry datetime among link, event, and share_link."""
        candidates = []
        if self.expires_at:
            candidates.append(self.expires_at)
        if self.event and self.event.expires_at:
            candidates.append(self.event.expires_at)
        if self.share_link and self.share_link.expires_at:
            candidates.append(self.share_link.expires_at)
        return min(candidates) if candidates else None


