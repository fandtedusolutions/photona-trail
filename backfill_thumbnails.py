import os
import django
from io import BytesIO
import urllib.request
from PIL import Image, ImageOps
from django.core.files.base import ContentFile

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'photona_project.settings')
django.setup()

from gallery.models import GalleryImage

def backfill_thumbnails():
    images = GalleryImage.objects.filter(thumbnail='')
    total = images.count()
    print(f"Found {total} images without thumbnails. Starting backfill...")
    
    for i, img in enumerate(images):
        try:
            print(f"[{i+1}/{total}] Processing {img.filename}...")
            
            # Download original from S3 into memory
            req = urllib.request.urlopen(img.file.url)
            img_data = req.read()
            
            with Image.open(BytesIO(img_data)) as pillow_img:
                pillow_img = ImageOps.exif_transpose(pillow_img)
                pillow_img.thumbnail((600, 600), Image.Resampling.LANCZOS)
                
                thumb_io = BytesIO()
                if pillow_img.mode in ('RGBA', 'P'): 
                    pillow_img = pillow_img.convert('RGB')
                
                pillow_img.save(thumb_io, format='WEBP', quality=80)
                thumb_io.seek(0)
                
                thumb_name = os.path.splitext(img.filename)[0] + "_thumb.webp"
                img.thumbnail.save(thumb_name, ContentFile(thumb_io.read()), save=True)
                print(f"  -> Saved thumbnail {thumb_name}")
                
        except Exception as e:
            print(f"  -> Failed to process {img.filename}: {e}")

if __name__ == "__main__":
    backfill_thumbnails()
    print("Backfill complete!")
