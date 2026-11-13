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
            img.draft('RGB', (1200, 1200))
            img = ImageOps.exif_transpose(img)
            img.thumbnail(max_size, Image.Resampling.BILINEAR)
            
            thumb_io = BytesIO()
            if img.mode not in ('RGB', 'L'): 
                img = img.convert('RGB')
            img.save(thumb_io, format='WEBP', quality=80)
            thumb_io.seek(0)
            return thumb_io, '.webp'
    except Exception as e:
        print(f"Error creating thumbnail: {e}")
        return None, None

def compress_image_bytes(img_bytes, filename, compression_percentage):
    if not compression_percentage or compression_percentage >= 100:
        return img_bytes
    try:
        from PIL import Image, ImageOps
        with Image.open(BytesIO(img_bytes)) as img:
            img = ImageOps.exif_transpose(img)
            icc_profile = img.info.get('icc_profile')
            scale_factor = float(compression_percentage) / 100.0
            new_width = max(1, int(img.width * scale_factor))
            new_height = max(1, int(img.height * scale_factor))
            if new_width < img.width and new_height < img.height:
                img = img.resize((new_width, new_height), Image.Resampling.BILINEAR)
            if img.mode in ('RGBA', 'P'):
                img = img.convert('RGB')
            ext = os.path.splitext(filename)[1].lower()
            fmt = 'WEBP' if ext == '.webp' else 'JPEG'
            out_io = BytesIO()
            save_kwargs = {'format': fmt, 'quality': 88}
            if icc_profile:
                save_kwargs['icc_profile'] = icc_profile
            img.save(out_io, **save_kwargs)
            return out_io.getvalue()
    except Exception as e:
        print(f"[ZIP] Compression error: {e}")
        return img_bytes

def compress_image_if_needed(image_path, compression_percentage):
    if not compression_percentage or compression_percentage >= 100:
        return
    try:
        from PIL import Image, ImageOps
        import os, uuid, shutil
        with Image.open(image_path) as img:
            img = ImageOps.exif_transpose(img)
            icc_profile = img.info.get('icc_profile')
            
            # Downscale dimensions to compress file size while maintaining pixel quality
            scale_factor = float(compression_percentage) / 100.0
            new_width = max(1, int(img.width * scale_factor))
            new_height = max(1, int(img.height * scale_factor))
            
            if new_width < img.width and new_height < img.height:
                img = img.resize((new_width, new_height), Image.Resampling.BILINEAR)
                
            if img.mode in ('RGBA', 'P'): 
                img = img.convert('RGB')
                
            ext = os.path.splitext(image_path)[1].lower()
            fmt = 'JPEG'
            if ext == '.webp': fmt = 'WEBP'
            
            temp_save_path = f"{image_path}.{uuid.uuid4().hex}.tmp"
            
            save_kwargs = {'format': fmt, 'quality': 88}
            if icc_profile:
                save_kwargs['icc_profile'] = icc_profile
                
            img.save(temp_save_path, **save_kwargs)
            
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
        # 1. Background Thumbnail Generation (if not already generated)
        if not gallery_image.thumbnail and gallery_image.file:
            try:
                from io import BytesIO
                from django.core.files import File
                src_stream = None
                if local_path and os.path.exists(local_path):
                    src_stream = local_path
                else:
                    try:
                        gallery_image.file.open('rb')
                        src_stream = gallery_image.file
                    except Exception:
                        src_stream = None

                if src_stream:
                    thumb_io, ext = create_thumbnail(src_stream)
                    if thumb_io:
                        thumb_name = os.path.splitext(gallery_image.filename)[0] + "_thumb" + ext
                        gallery_image.thumbnail.save(thumb_name, File(thumb_io), save=False)
                        gallery_image.save(update_fields=['thumbnail'])
                    if hasattr(src_stream, 'close') and src_stream is not local_path:
                        try:
                            src_stream.close()
                        except Exception:
                            pass
            except Exception as te:
                print(f"[Celery] Background thumbnail generation error: {te}")

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
def process_zip_upload_task(temp_zip_path, event_id=None, job_id=None):
    from concurrent.futures import ThreadPoolExecutor
    import threading
    from io import BytesIO
    from django.core.files.base import ContentFile
    from .job_tracker import update_job
    from .clustering import run_clustering_on_upload
    from .models import GalleryImage, Event
    from config import VALID_IMAGE_EXTENSIONS

    event = None
    if event_id:
        event = Event.objects.filter(id=event_id).first()
        
    if not job_id and event_id:
        job_id = f"zip_{event_id}"

    try:
        if zipfile.is_zipfile(temp_zip_path):
            # Fast index read directly from the ZIP central directory (< 5ms)
            valid_entries = []
            with zipfile.ZipFile(temp_zip_path, 'r') as zf_index:
                for info in zf_index.infolist():
                    if info.is_dir():
                        continue
                    base_name = os.path.basename(info.filename)
                    if base_name.startswith('.') or '__MACOSX' in info.filename:
                        continue
                    file_ext = os.path.splitext(base_name)[1].lower()
                    if file_ext in VALID_IMAGE_EXTENSIONS:
                        valid_entries.append((info.filename, base_name))
            
            total_files = len(valid_entries)
            print(f"[ZIP] Discovered {total_files} photos in ZIP in milliseconds. Starting parallel streaming...")
            
            if job_id:
                update_job(job_id, {
                    'active': True,
                    'total': total_files,
                    'current': 0,
                    'percent': 0,
                    'message': f"Found {total_files} photos. Extracting & uploading..."
                })

            if total_files > 0:
                compression = event.compression_percentage if event else 100
                processed_counter = [0]
                counter_lock = threading.Lock()

                def process_one_zip_entry(item):
                    from django.db import connection
                    zip_entry_path, file_name = item
                    try:
                        # Stream photo bytes on-the-fly without heavy sequential extractall disk writes
                        with zipfile.ZipFile(temp_zip_path, 'r') as zf_worker:
                            img_bytes = zf_worker.read(zip_entry_path)

                        if compression and compression < 100:
                            img_bytes = compress_image_bytes(img_bytes, file_name, compression)

                        gallery_image = GalleryImage(filename=file_name, event=event, total_faces=-1)
                        gallery_image.file.save(file_name, ContentFile(img_bytes), save=False)
                        
                        thumb_io, ext = create_thumbnail(BytesIO(img_bytes))
                        if thumb_io:
                            thumb_name = os.path.splitext(file_name)[0] + "_thumb" + ext
                            gallery_image.thumbnail.save(thumb_name, File(thumb_io), save=False)
                            
                        gallery_image.save()
                        process_single_image_task.delay(gallery_image.id, None)

                        with counter_lock:
                            processed_counter[0] += 1
                            curr = processed_counter[0]
                            pct = int((curr / total_files) * 100)
                            if job_id:
                                update_job(job_id, {
                                    'active': True,
                                    'total': total_files,
                                    'current': curr,
                                    'percent': pct,
                                    'message': f"Extracted {curr} of {total_files} photos ({pct}%)"
                                })
                    except Exception as ex:
                        print(f"[ZIP] Error processing {file_name}: {ex}")
                    finally:
                        try:
                            connection.close()
                        except Exception:
                            pass

                # Parallel workers for 4x speed throughput
                with ThreadPoolExecutor(max_workers=4) as executor:
                    list(executor.map(process_one_zip_entry, valid_entries))

                if job_id:
                    update_job(job_id, {
                        'active': False,
                        'total': total_files,
                        'current': total_files,
                        'percent': 100,
                        'message': f"All {total_files} photos extracted successfully!"
                    })

                # Trigger AI face clustering now that all images are saved
                if event:
                    run_clustering_on_upload(event)

    except Exception as e:
        print(f"[ERROR] Celery ZIP processing error: {e}")
        if job_id:
            update_job(job_id, {
                'active': False,
                'message': f"ZIP processing error: {e}"
            })
    finally:
        if os.path.exists(temp_zip_path):
            try:
                os.remove(temp_zip_path)
            except Exception:
                pass

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

@shared_task
def send_whatsapp_template_task(phone, name, link_url, template=None, language=None):
    from .whatsapp import send_whatsapp_template_message
    return send_whatsapp_template_message(phone, name, link_url, template=template, language=language)
