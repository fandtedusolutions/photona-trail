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

from config import VALID_IMAGE_EXTENSIONS
from .models import GalleryImage, Event

@shared_task
def process_image_upload_task(temp_paths, event_id, original_filenames):
    event = Event.objects.filter(id=event_id).first()
    image_ids = []
    for temp_path, file_name in zip(temp_paths, original_filenames):
        if os.path.exists(temp_path):
            with open(temp_path, 'rb') as img_f:
                gallery_image = GalleryImage(filename=file_name, event=event, total_faces=-1)
                gallery_image.file.save(file_name, File(img_f), save=False)
                
                thumb_io, ext = create_thumbnail(temp_path)
                if thumb_io:
                    thumb_name = os.path.splitext(file_name)[0] + "_thumb" + ext
                    gallery_image.thumbnail.save(thumb_name, File(thumb_io), save=False)
                    
                gallery_image.save()
                image_ids.append(gallery_image.id)
            os.remove(temp_path)
    
    if image_ids:
        from .utils import process_gallery_image
        for img_id in image_ids:
            img = GalleryImage.objects.filter(id=img_id).first()
            if img:
                process_gallery_image(img)
        
        if event:
            from .clustering import run_clustering_on_upload
            run_clustering_on_upload(event)

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
            for root, dirs, extracted_files in os.walk(extract_path):
                for file_name in extracted_files:
                    file_ext = os.path.splitext(file_name)[1].lower()
                    if file_ext in VALID_IMAGE_EXTENSIONS:
                        full_img_path = os.path.join(root, file_name)
                        with open(full_img_path, 'rb') as img_f:
                            gallery_image = GalleryImage(filename=file_name, event=event, total_faces=-1)
                            gallery_image.file.save(file_name, File(img_f), save=False)
                            
                            thumb_io, ext = create_thumbnail(full_img_path)
                            if thumb_io:
                                thumb_name = os.path.splitext(file_name)[0] + "_thumb" + ext
                                gallery_image.thumbnail.save(thumb_name, File(thumb_io), save=False)
                                
                            gallery_image.save()
                            image_ids.append(gallery_image.id)
                            
            from .utils import process_gallery_image
            for img_id in image_ids:
                img = GalleryImage.objects.filter(id=img_id).first()
                if img:
                    num_faces = process_gallery_image(img)
                    img.total_faces = num_faces
                    img.save(update_fields=['total_faces'])
                    
            if event:
                from .clustering import run_clustering_on_upload
                run_clustering_on_upload(event)
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
