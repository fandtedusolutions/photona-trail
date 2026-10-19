import cv2
import numpy as np
import faiss
from django.conf import settings
from insightface.app import FaceAnalysis
from config import MODEL_NAME, DETECTION_SIZE, SIMILARITY_THRESHOLD
from .models import GalleryImage, FaceEmbedding, Event

# Singleton-like lazy loaded InsightFace model
_face_app = None

def get_face_model():
    global _face_app
    if _face_app is None:
        _face_app = FaceAnalysis(name=MODEL_NAME)
        _face_app.prepare(ctx_id=0, det_size=DETECTION_SIZE)
    return _face_app

def process_gallery_image(gallery_image, local_path=None, raw_bytes=None):
    """
    Process an uploaded gallery image using InsightFace and save embeddings to DB.
    """
    app = get_face_model()
    import urllib.request
    import numpy as np
    
    img = None
    if local_path and os.path.exists(local_path):
        img = cv2.imread(local_path)
    elif raw_bytes is not None:
        arr = np.asarray(bytearray(raw_bytes), dtype=np.uint8)
        img = cv2.imdecode(arr, -1)
    else:
        try:
            try:
                # For local storage
                _path = gallery_image.file.path
                if os.path.exists(_path):
                    img = cv2.imread(_path)
            except (NotImplementedError, AttributeError):
                pass
                
            if img is None:
                # For remote storage (like S3)
                gallery_image.file.open('rb')
                arr = np.asarray(bytearray(gallery_image.file.read()), dtype=np.uint8)
                img = cv2.imdecode(arr, -1)
                gallery_image.file.close()
        except Exception as e:
            print(f"Error downloading image for face detection: {e}")
            return 0
        
    if img is None:
        return 0
    
    # Handle color channel formats safely
    if len(img.shape) == 2:
        img = cv2.cvtColor(img, cv2.COLOR_GRAY2RGB)
    elif img.shape[2] == 4:
        img = cv2.cvtColor(img, cv2.COLOR_BGRA2RGB)
    elif img.shape[2] == 3:
        img = cv2.cvtColor(img, cv2.COLOR_BGR2RGB)

    # Downscale large DSLR images for lightning-fast, accurate face detection
    h, w = img.shape[:2]
    max_dim = 1280
    if max(h, w) > max_dim:
        scale = max_dim / float(max(h, w))
        img = cv2.resize(img, (int(w * scale), int(h * scale)), interpolation=cv2.INTER_LINEAR)
    
    # Detect faces
    faces = app.get(img)
    
    # Save face embeddings
    embeddings = []
    for face in faces:
        embedding_list = face.embedding.astype(float).tolist()
        embeddings.append(FaceEmbedding(
            image=gallery_image,
            embedding_data=embedding_list
        ))
    if embeddings:
        FaceEmbedding.objects.bulk_create(embeddings)
        
    # Update total faces count
    gallery_image.total_faces = len(faces)
    gallery_image.save()
    return len(faces)

def search_person_by_selfie(selfie_path, event_id=None, tenant_user=None):
    """
    Query the database of face embeddings using a query selfie.
    """
    app = get_face_model()
    
    # Read query selfie
    img = cv2.imread(selfie_path)
    if img is None:
        raise ValueError("Could not read query selfie image.")
        
    img = cv2.cvtColor(img, cv2.COLOR_BGR2RGB)
    faces = app.get(img)
    
    if len(faces) == 0:
        raise ValueError("No face detected in the uploaded selfie.")
    if len(faces) > 1:
        raise ValueError("Selfie contains more than one face. Please upload a clear photo of yourself only.")
        
    query_embedding = faces[0].embedding.astype(np.float32)
    query_embedding = np.expand_dims(query_embedding, axis=0)
    faiss.normalize_L2(query_embedding)
    
    # Load face embeddings from DB matching filters
    query = FaceEmbedding.objects.all()
    if event_id:
        query = query.filter(image__event_id=event_id)
    elif tenant_user and not tenant_user.is_superuser:
        query = query.filter(image__event__owner=tenant_user)
        
    face_records = list(query)
    if not face_records:
        return []
        
    embeddings_list = [record.get_numpy_embedding() for record in face_records]
    embeddings_matrix = np.array(embeddings_list, dtype=np.float32)
    faiss.normalize_L2(embeddings_matrix)
    
    # Create FAISS Index
    dimension = embeddings_matrix.shape[1]
    index = faiss.IndexFlatIP(dimension)
    index.add(embeddings_matrix)
    
    # Search index
    distances, indices = index.search(query_embedding, index.ntotal)
    
    # Collect matching images
    matching_images = []
    seen_images = set()
    
    for distance, row_index in zip(distances[0], indices[0]):
        if row_index == -1:
            continue
        if distance < SIMILARITY_THRESHOLD:
            continue
            
        record = face_records[row_index]
        image = record.image
        
        if image.id in seen_images:
            continue
            
        seen_images.add(image.id)
        matching_images.append({
            "image": image,
            "score": float(distance)
        })
        
    return matching_images


import re
import requests
import zipfile
import os
from django.core.files import File
from config import VALID_IMAGE_EXTENSIONS

import uuid
import shutil
import gdown

def extract_gdrive_id(url):
    # Regex to find file or folder ID in typical sharing URL formats
    match = re.search(r'(?:id=|(?:/d/|/folders/|e/))([a-zA-Z0-9-_]{20,})', url)
    return match.group(1) if match else None

def download_gdrive_file_by_id(file_id, output_path):
    # 1. Try gdown first
    try:
        res = gdown.download(id=file_id, output=output_path, quiet=True, fuzzy=True)
        if res and os.path.exists(output_path) and os.path.getsize(output_path) > 100:
            return True
    except Exception as e:
        print(f"[DEBUG] gdown download failed for {file_id}: {e}")

    # 2. Fallback with requests session and confirmation token
    try:
        import requests
        session = requests.Session()
        session.headers.update({
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'
        })
        URL = "https://docs.google.com/uc?export=download"
        response = session.get(URL, params={'id': file_id}, stream=True, timeout=30)

        token = None
        for key, value in session.cookies.items():
            if key.startswith('download_warning'):
                token = value
                break
        if not token and response.status_code == 200:
            match = re.search(r'confirm=([0-9A-Za-z_]+)', response.text)
            if match:
                token = match.group(1)

        if token:
            response = session.get(URL, params={'id': file_id, 'confirm': token}, stream=True, timeout=30)

        content_type = response.headers.get('Content-Type', '')
        if response.status_code == 200 and 'text/html' not in content_type:
            with open(output_path, "wb") as f:
                for chunk in response.iter_content(32768):
                    if chunk:
                        f.write(chunk)
            
            if os.path.exists(output_path) and os.path.getsize(output_path) > 100:
                print(f"[DEBUG] Successfully downloaded {file_id} via requests fallback.")
                return True
        else:
            print(f"[DEBUG] requests fallback failed: status {response.status_code}, content_type {content_type}")
    except Exception as e:
        print(f"[DEBUG] requests fallback also failed for {file_id}: {e}")
            
    return False

def download_and_index_gdrive_link(url, event_id=None, job_id=None, jobs_dict=None):
    from .job_tracker import get_job, update_job
    import cv2
    event = None
    if event_id:
        event = Event.objects.filter(id=event_id).first()
        
    temp_dir = os.path.join(settings.MEDIA_ROOT, 'temp', f"gdrive_{uuid.uuid4().hex[:8]}")
    os.makedirs(temp_dir, exist_ok=True)
    
    total_indexed = 0
    total_faces = 0
    
    try:
        is_folder = "/folders/" in url or "/drive/folders/" in url or ("id=" in url and "folder" in url.lower())
        
        if is_folder:
            folder_id = extract_gdrive_id(url)
            if not folder_id:
                raise ValueError("Could not extract Google Drive folder ID from URL.")
                
            print(f"[GDrive] Processing folder {folder_id}...")
            
            # 1. If official GDRIVE_API_KEY is configured, try it
            api_downloaded = False
            if hasattr(settings, 'GDRIVE_API_KEY') and settings.GDRIVE_API_KEY:
                try:
                    print("[GDrive] Using official Google Drive API for pagination...")
                    api_key = settings.GDRIVE_API_KEY
                    page_token = ""
                    api_files = []
                    while True:
                        api_url = f"https://www.googleapis.com/drive/v3/files?q='{folder_id}'+in+parents+and+trashed=false&key={api_key}&pageToken={page_token}&pageSize=1000&fields=nextPageToken,files(id,name)"
                        res = requests.get(api_url, timeout=20).json()
                        if 'files' in res:
                            api_files.extend(res['files'])
                        if 'error' in res or not res.get('nextPageToken'):
                            break
                        page_token = res.get('nextPageToken')
                    
                    if api_files:
                        if job_id:
                            update_job(job_id, {'message': f'Found {len(api_files)} files. Downloading...'})
                        for item in api_files:
                            fid = item['id']
                            fname = item.get('name', f"{fid}.jpg")
                            out_path = os.path.join(temp_dir, fname)
                            download_gdrive_file_by_id(fid, out_path)
                        api_downloaded = True
                except Exception as api_err:
                    print(f"[GDrive] API pagination failed: {api_err}")

            # 2. Use gdown to download folder contents directly
            if not api_downloaded or not any(os.scandir(temp_dir)):
                print("[GDrive] Downloading folder via gdown...")
                folder_url = f"https://drive.google.com/drive/folders/{folder_id}"
                if job_id:
                    update_job(job_id, {'message': 'Downloading photos from Google Drive folder...'})
                try:
                    gdown.download_folder(url=folder_url, output=temp_dir, quiet=True, use_cookies=False)
                except Exception as gd_err:
                    print(f"[GDrive] gdown folder download error: {gd_err}")
                    err_text = str(gd_err)
                    if '404' in err_text or 'permission' in err_text.lower() or 'cannot retrieve' in err_text.lower():
                        raise ValueError("Could not access Google Drive folder. Please ensure the link sharing is set to 'Anyone with the link can view'.")
                    raise ValueError(f"Google Drive folder download failed: {gd_err}")

        else:
            # Single file or ZIP link
            file_id = extract_gdrive_id(url)
            if not file_id:
                raise ValueError("Could not extract Google Drive file ID from URL.")
                
            if job_id:
                update_job(job_id, {'message': 'Downloading file from Google Drive...'})
                
            tmp_file_path = os.path.join(temp_dir, f"{file_id}.tmp")
            success = download_gdrive_file_by_id(file_id, tmp_file_path)
            if not success:
                try:
                    gdown.download(url=url, output=tmp_file_path, quiet=True, fuzzy=True)
                except Exception as ge:
                    print(f"[GDrive] gdown single file download failed: {ge}")
                    
            if not os.path.exists(tmp_file_path) or os.path.getsize(tmp_file_path) < 100:
                raise ValueError("Could not download file from Google Drive. Please ensure the link sharing is set to 'Anyone with the link can view'.")

        # Extract any downloaded ZIP archives in temp_dir
        raw_files = []
        for root, dirs, files in os.walk(temp_dir):
            for file_name in files:
                if not file_name.startswith('.'):
                    raw_files.append(os.path.join(root, file_name))

        for fpath in raw_files:
            if fpath.lower().endswith('.zip') or zipfile.is_zipfile(fpath):
                try:
                    extract_zip_path = os.path.join(temp_dir, f"zip_{uuid.uuid4().hex[:6]}")
                    with zipfile.ZipFile(fpath, 'r') as zip_ref:
                        zip_ref.extractall(extract_zip_path)
                except Exception as ze:
                    print(f"[DEBUG] Error extracting ZIP {fpath}: {ze}")

        # Gather all valid images
        valid_images = []
        for root, dirs, files in os.walk(temp_dir):
            for file_name in sorted(files):
                if file_name.startswith('.') or file_name.lower().endswith('.zip'):
                    continue
                full_path = os.path.join(root, file_name)
                ext = os.path.splitext(file_name)[1].lower()
                if ext in VALID_IMAGE_EXTENSIONS:
                    valid_images.append(full_path)
                elif ext == '.tmp':
                    test_img = cv2.imread(full_path)
                    if test_img is not None:
                        valid_images.append(full_path)

        total_files = len(valid_images)
        if total_files == 0:
            raise ValueError("No photos found in Google Drive link. Please make sure the folder contains image files (JPG, PNG, WEBP) and link sharing is set to 'Anyone with the link can view'.")

        print(f"[GDrive] Found {total_files} images to process.")
        if job_id:
            update_job(job_id, {
                'total': total_files,
                'current': 0,
                'percent': 0,
                'message': f"Found {total_files} photos. Importing & indexing..."
            })

        for idx, img_path in enumerate(valid_images):
            # Check if job was cancelled by user
            if job_id and get_job(job_id).get('cancelled'):
                print(f"[GDrive] Import job {job_id} was cancelled by user. Stopping.")
                break

            # Re-fetch event inside thread loop to avoid cross-thread DB session issues
            event = Event.objects.filter(id=event_id).first() if event_id else None

            # Storage limit check
            if event and event.owner and hasattr(event.owner, 'profile'):
                profile = event.owner.profile
                file_size_mb = os.path.getsize(img_path) / (1024 * 1024)
                limit_mb = profile.effective_storage_limit_mb if hasattr(profile, 'effective_storage_limit_mb') else (profile.subscription_plan.storage_limit_mb if profile.subscription_plan else 0)
                if limit_mb > 0 and (profile.used_storage_mb + file_size_mb) > limit_mb:
                    raise Exception("Storage limit exceeded. Upgrade your plan to import more photos.")
                profile.used_storage_mb += file_size_mb
                profile.save(update_fields=['used_storage_mb'])

            # Compression
            compression = event.compression_percentage if event else 100
            from .tasks import compress_image_if_needed
            compress_image_if_needed(img_path, compression)

            file_name = os.path.basename(img_path)
            if file_name.endswith('.tmp'):
                clean_event_name = event.name if event else "Imported"
                file_name = f"{clean_event_name}_Photo_{total_indexed + 1}.jpg"

            with open(img_path, 'rb') as img_f:
                gallery_image = GalleryImage(filename=file_name, event=event, total_faces=-1)
                gallery_image.file.save(file_name, File(img_f), save=False)
                
                from .tasks import create_thumbnail
                thumb_io, ext = create_thumbnail(img_path)
                if thumb_io:
                    thumb_name = os.path.splitext(file_name)[0] + "_thumb" + ext
                    gallery_image.thumbnail.save(thumb_name, File(thumb_io), save=False)
                    
                gallery_image.save()

            # Process faces
            try:
                from .tasks import process_single_image_task
                process_single_image_task.delay(gallery_image.id, None)
            except Exception:
                try:
                    num_f = process_gallery_image(gallery_image)
                    gallery_image.total_faces = num_f
                    gallery_image.save(update_fields=['total_faces'])
                except Exception as pe:
                    print(f"[DEBUG] Direct processing fallback error: {pe}")

            total_indexed += 1

            if job_id:
                pct = int((total_indexed / total_files) * 100)
                update_job(job_id, {
                    'current': total_indexed,
                    'percent': min(99, pct),
                    'message': f"Imported {total_indexed} of {total_files} photos ({pct}%)",
                    'new_photo': {
                        'id': gallery_image.id,
                        'url': f"/secure-media/{gallery_image.get_secure_thumbnail_token()}/",
                        'full_url': f"/secure-media/{gallery_image.get_secure_file_token()}/",
                        'filename': gallery_image.filename,
                        'total_faces': gallery_image.total_faces,
                        'preset_name': gallery_image.preset_name or 'original',
                        'preset_settings': gallery_image.preset_settings or {},
                    }
                })

    except Exception as e:
        print(f"[DEBUG] Google Drive import error: {e}")
        raise e
    finally:
        # Clean up temp folder
        if os.path.exists(temp_dir):
            shutil.rmtree(temp_dir, ignore_errors=True)
            
    return total_indexed, total_faces


