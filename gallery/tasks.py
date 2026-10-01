import os
import zipfile
import shutil
from celery import shared_task
from django.conf import settings
from django.core.files import File
from io import BytesIO
from PIL import Image, ImageOps

def create_thumbnail(image_path, max_size=(600, 600)):
    try:
        with Image.open(image_path) as img:
            img = ImageOps.exif_transpose(img)
            img.thumbnail(max_size, Image.Resampling.LANCZOS)
            
            thumb_io = BytesIO()
            if img.mode in ('RGBA', 'P'): 
                img = img.convert('RGB')
            img.save(thumb_io, format='WEBP', quality=80)
            thumb_io.seek(0)
            return thumb_io, '.webp'
    except Exception as e:
        print(f"Error creating thumbnail: {e}")
        return None, None

def compress_image_if_needed(image_path, compression_percentage):
    if not compression_percentage or compression_percentage >= 100:
        return
    try:
        from PIL import Image, ImageOps
        import os
        with Image.open(image_path) as img:
            img = ImageOps.exif_transpose(img)
            if img.mode in ('RGBA', 'P'): 
                img = img.convert('RGB')
            ext = os.path.splitext(image_path)[1].lower()
            fmt = 'JPEG'
            if ext == '.webp': fmt = 'WEBP'
            # Force convert PNG to JPEG if compression is requested
            if ext == '.png': 
                fmt = 'JPEG'
            
            import uuid, shutil
            temp_save_path = f"{image_path}.{uuid.uuid4().hex}.tmp"
            img.save(temp_save_path, format=fmt, quality=int(compression_percentage), optimize=True)
            
        shutil.move(temp_save_path, image_path)
    except Exception as e:
        print(f"Error compressing image: {e}")

from config import VALID_IMAGE_EXTENSIONS
from .models import GalleryImage, Event

@shared_task
def process_single_image_task(gallery_image_id, local_path=None):
    import os
    from .models import GalleryImage
    from .utils import process_gallery_image
    gallery_image = GalleryImage.objects.filter(id=gallery_image_id).first()
    if gallery_image:
        num_faces = process_gallery_image(gallery_image, local_path=local_path)
        gallery_image.total_faces = num_faces
        gallery_image.save(update_fields=['total_faces'])
        
        # Check if we should run clustering
        remaining = GalleryImage.objects.filter(event_id=gallery_image.event_id, total_faces=-1).count()
        if remaining == 0:
            from .clustering import run_clustering_on_upload
            run_clustering_on_upload(gallery_image.event)
            
    if local_path and os.path.exists(local_path):
        try:
            os.remove(local_path)
        except:
            pass

@shared_task
def process_image_upload_task(temp_paths, event_id, original_filenames):
    event = Event.objects.filter(id=event_id).first()
    image_ids = []
    uploaded_images = []
    
    compression = event.compression_percentage if event else 100
    
    for temp_path, file_name in zip(temp_paths, original_filenames):
        if os.path.exists(temp_path):
            compress_image_if_needed(temp_path, compression)
            with open(temp_path, 'rb') as img_f:
                safe_file_name = os.path.basename(file_name)
                gallery_image = GalleryImage(filename=safe_file_name, event=event, total_faces=-1)
                gallery_image.file.save(safe_file_name, File(img_f), save=False)
                
                thumb_io, ext = create_thumbnail(temp_path)
                if thumb_io:
                    thumb_name = os.path.splitext(file_name)[0] + "_thumb" + ext
                    gallery_image.thumbnail.save(thumb_name, File(thumb_io), save=False)
                    
                gallery_image.save()
                
            process_single_image_task.delay(gallery_image.id, None)
            os.remove(temp_path)

@shared_task
def process_zip_upload_task(temp_zip_path, event_id=None):
    event = None
    if event_id:
        event = Event.objects.filter(id=event_id).first()
        
    temp_dir = os.path.dirname(temp_zip_path)
    extract_path = temp_zip_path.replace('.zip', '_extracted')
    
    try:
        if zipfile.is_zipfile(temp_zip_path):
            with zipfile.ZipFile(temp_zip_path, 'r') as zip_ref:
                zip_ref.extractall(extract_path)
                
            image_ids = []
            uploaded_images = []
            
            compression = event.compression_percentage if event else 100
            
            for root, dirs, extracted_files in os.walk(extract_path):
                for file_name in extracted_files:
                    file_ext = os.path.splitext(file_name)[1].lower()
                    if file_ext in VALID_IMAGE_EXTENSIONS:
                        full_img_path = os.path.join(root, file_name)
                        compress_image_if_needed(full_img_path, compression)
                        with open(full_img_path, 'rb') as img_f:
                            gallery_image = GalleryImage(filename=file_name, event=event, total_faces=-1)
                            gallery_image.file.save(file_name, File(img_f), save=False)
                            
                            thumb_io, ext = create_thumbnail(full_img_path)
                            if thumb_io:
                                thumb_name = os.path.splitext(file_name)[0] + "_thumb" + ext
                                gallery_image.thumbnail.save(thumb_name, File(thumb_io), save=False)
                                
                            gallery_image.save()
                            
                        process_single_image_task.delay(gallery_image.id, None)
                        
    except Exception as e:
        print(f"[ERROR] Celery ZIP processing error: {e}")
    finally:
        if os.path.exists(extract_path):
            shutil.rmtree(extract_path)
        if os.path.exists(temp_zip_path):
            os.remove(temp_zip_path)

@shared_task
def process_gdrive_import_task(url, event_id=None):
    try:
        event = Event.objects.filter(id=event_id).first()
        if not event or not event.owner:
            return
            
        profile = event.owner.profile
        
        # Measure size before
        size_before = sum([img.file.size for img in event.images.all() if img.file and os.path.exists(img.file.path)])
        
        from .utils import download_and_index_gdrive_link
        download_and_index_gdrive_link(url, event_id=event_id)
        
        # Measure size after
        size_after = sum([img.file.size for img in event.images.all() if img.file and os.path.exists(img.file.path)])
        diff_mb = (size_after - size_before) / (1024 * 1024)
        
        profile.used_storage_mb += diff_mb
        profile.save()
        
        from .clustering import run_clustering_on_upload
        run_clustering_on_upload(event)
    except Exception as e:
        print(f"[ERROR] Celery GDrive import error: {e}")
