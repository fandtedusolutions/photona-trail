from django.core.management.base import BaseCommand
from gallery.models import GalleryImage
from django.core.files.base import File
import os
from io import BytesIO
from PIL import Image, ImageOps

class Command(BaseCommand):
    help = 'Backfill missing thumbnails for images imported via Google Drive.'

    def handle(self, *args, **kwargs):
        # Find images that have a file but NO thumbnail
        images = GalleryImage.objects.filter(thumbnail='').exclude(file='')
        total = images.count()
        self.stdout.write(self.style.WARNING(f"Found {total} images missing thumbnails."))
        
        for i, img in enumerate(images, 1):
            try:
                # Use img.file directly to support S3 / remote backends
                with img.file.open('rb') as f:
                    with Image.open(f) as pil_img:
                        pil_img = ImageOps.exif_transpose(pil_img)
                        pil_img.thumbnail((600, 600), Image.Resampling.LANCZOS)
                        
                        thumb_io = BytesIO()
                        ext = os.path.splitext(img.filename)[1].lower()
                        
                        fmt = 'JPEG'
                        if ext in ['.png']: fmt = 'PNG'
                        elif ext in ['.webp']: fmt = 'WEBP'
                        
                        if pil_img.mode in ('RGBA', 'P') and fmt == 'JPEG':
                            pil_img = pil_img.convert('RGB')
                            
                        pil_img.save(thumb_io, format=fmt, quality=85)
                        thumb_io.seek(0)
                        
                        thumb_name = os.path.splitext(img.filename)[0] + "_thumb" + ext
                        img.thumbnail.save(thumb_name, File(thumb_io), save=True)
                        
                self.stdout.write(self.style.SUCCESS(f"[{i}/{total}] Created thumbnail for {img.filename}"))
            except Exception as e:
                self.stdout.write(self.style.ERROR(f"[{i}/{total}] Error on {img.id}: {e}"))
                
        self.stdout.write(self.style.SUCCESS("Thumbnail backfill complete!"))
