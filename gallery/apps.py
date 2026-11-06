from django.apps import AppConfig


class GalleryConfig(AppConfig):
    name = 'gallery'

    def ready(self):
        from django.db.models.signals import post_delete, post_save
        from .models import GalleryImage
        from .media_cache import purge_rendered_for_image

        def _purge_renders(sender, instance, **kwargs):
            # Drop stale cached renders (preset edits / deletions) to reclaim disk.
            # On save we only purge when preset data changed (update_fields hint),
            # or for full saves; cache keys already guarantee correctness.
            update_fields = kwargs.get('update_fields')
            if update_fields is not None and not ({'preset_name', 'preset_settings', 'file', 'thumbnail'} & set(update_fields)):
                return
            if kwargs.get('created'):
                return
            purge_rendered_for_image(instance.id)

        post_save.connect(_purge_renders, sender=GalleryImage, dispatch_uid='gallery_purge_renders_save')
        post_delete.connect(_purge_renders, sender=GalleryImage, dispatch_uid='gallery_purge_renders_delete')
