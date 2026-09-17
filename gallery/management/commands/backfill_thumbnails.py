from django.core.management.base import BaseCommand
from gallery.models import GalleryImage
from gallery.tasks import create_thumbnail
from django.core.files.base import File
import os

class Command(BaseCommand):
    help = 'Backfill missing thumbnails for images imported via Google Drive.'

    def handle(self, *args, **kwargs):
        # Find images that have a file but NO thumbnail
        images = GalleryImage.objects.filter(thumbnail='').exclude(file='')
        total = images.count()
        self.stdout.write(self.style.WARNING(f"Found {total} images missing thumbnails."))
        
        for i, img in enumerate(images, 1):
            try:
                full_img_path = img.file.path
                if not os.path.exists(full_img_path):
                    self.stdout.write(self.style.ERROR(f"[{i}/{total}] Skipping {img.id} - File missing on disk."))
                    continue
                    
                thumb_io, ext = create_thumbnail(full_img_path)
                if thumb_io:
                    thumb_name = os.path.splitext(img.filename)[0] + "_thumb" + ext
                    img.thumbnail.save(thumb_name, File(thumb_io), save=True)
                    self.stdout.write(self.style.SUCCESS(f"[{i}/{total}] Created thumbnail for {img.filename}"))
                else:
                    self.stdout.write(self.style.ERROR(f"[{i}/{total}] Failed to generate thumbnail for {img.filename}"))
            except Exception as e:
                self.stdout.write(self.style.ERROR(f"[{i}/{total}] Error on {img.id}: {e}"))
                
        self.stdout.write(self.style.SUCCESS("Thumbnail backfill complete!"))
