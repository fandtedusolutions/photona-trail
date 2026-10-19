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

def download_gdrive_file(file_id, output_path, api_key=None):
    # 1. Try official Google Drive API with api_key if available
    if api_key:
        try:
            import requests
            session = requests.Session()
            session.headers.update({
                'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'
            })
            api_dl_url = f"https://www.googleapis.com/drive/v3/files/{file_id}?alt=media&key={api_key}"
            r = session.get(api_dl_url, stream=True, timeout=35)
            content_type = r.headers.get('Content-Type', '')
            if r.status_code == 200 and 'text/html' not in content_type:
                with open(output_path, 'wb') as f:
                    for chunk in r.iter_content(32768):
                        if chunk:
                            f.write(chunk)
                if os.path.exists(output_path) and os.path.getsize(output_path) > 100:
                    return True
            else:
                print(f"[GDrive] API download status {r.status_code} ({content_type}) for {file_id}")
        except Exception as e:
            print(f"[GDrive] API download error for {file_id}: {e}")

    # 2. Try gdown
    try:
        res = gdown.download(id=file_id, output=output_path, quiet=True, fuzzy=True)
        if res and os.path.exists(output_path) and os.path.getsize(output_path) > 100:
            return True
    except Exception as e:
        print(f"[DEBUG] gdown download failed for {file_id}: {e}")

    # 3. Fallback with requests session and confirmation token
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
    except Exception as e:
        print(f"[DEBUG] requests fallback failed for {file_id}: {e}")
            
    return False

def _process_and_save_single_image(img_path, file_name, event_id):
    import cv2
    event = Event.objects.filter(id=event_id).first() if event_id else None
    img = cv2.imread(img_path)
    if img is None:
        return None

    if event and event.owner and hasattr(event.owner, 'profile'):
        profile = event.owner.profile
        file_size_mb = os.path.getsize(img_path) / (1024 * 1024)
        limit_mb = profile.effective_storage_limit_mb if hasattr(profile, 'effective_storage_limit_mb') else (profile.subscription_plan.storage_limit_mb if profile.subscription_plan else 0)
        if limit_mb > 0 and (profile.used_storage_mb + file_size_mb) > limit_mb:
            raise Exception("Storage limit exceeded. Upgrade your plan to import more photos.")
        profile.used_storage_mb += file_size_mb
        profile.save(update_fields=['used_storage_mb'])

    compression = event.compression_percentage if event else 100
    from .tasks import compress_image_if_needed
    compress_image_if_needed(img_path, compression)

    clean_file_name = file_name
    if clean_file_name.endswith('.tmp'):
        clean_event_name = event.name if event else "Imported"
        clean_file_name = f"{clean_event_name}_Photo_{uuid.uuid4().hex[:6]}.jpg"

    with open(img_path, 'rb') as img_f:
        gallery_image = GalleryImage(filename=clean_file_name, event=event, total_faces=-1)
        gallery_image.file.save(clean_file_name, File(img_f), save=False)

        from .tasks import create_thumbnail
        thumb_io, ext = create_thumbnail(img_path)
        if thumb_io:
            thumb_name = os.path.splitext(clean_file_name)[0] + "_thumb" + ext
            gallery_image.thumbnail.save(thumb_name, File(thumb_io), save=False)

        gallery_image.save()

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

    return gallery_image

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
    api_key = getattr(settings, 'GDRIVE_API_KEY', None)
    
    try:
        is_folder = "/folders/" in url or "/drive/folders/" in url or ("id=" in url and "folder" in url.lower())
        items_to_process = []
        
        if is_folder:
            folder_id = extract_gdrive_id(url)
            if not folder_id:
                raise ValueError("Could not extract Google Drive folder ID from URL.")
                
            print(f"[GDrive] Processing folder {folder_id}...")
            
            # 1. Fetch file list using official Google Drive API key if available
            if api_key:
                try:
                    print("[GDrive] Querying Google Drive API for folder contents...")
                    page_token = ""
                    while True:
                        api_url = f"https://www.googleapis.com/drive/v3/files?q='{folder_id}'+in+parents+and+trashed=false&key={api_key}&pageToken={page_token}&pageSize=1000&fields=nextPageToken,files(id,name,mimeType)"
                        res = requests.get(api_url, timeout=25).json()
                        if 'files' in res:
                            for f in res['files']:
                                fname = f.get('name', '')
                                ext = os.path.splitext(fname)[1].lower()
                                mime = f.get('mimeType', '').lower()
                                if ext in VALID_IMAGE_EXTENSIONS or 'image/' in mime or ext == '.zip':
                                    items_to_process.append({'id': f['id'], 'name': fname or f"{f['id']}.jpg"})
                        if 'error' in res or not res.get('nextPageToken'):
                            break
                        page_token = res.get('nextPageToken')
                except Exception as api_err:
                    print(f"[GDrive] API pagination failed: {api_err}")

            # 2. If no API key or API didn't return files, try gdown folder discovery
            if not items_to_process:
                print("[GDrive] Trying gdown folder discovery...")
                folder_url = f"https://drive.google.com/drive/folders/{folder_id}"
                try:
                    discovered = gdown.download_folder(url=folder_url, output=temp_dir, skip_download=True, quiet=True, use_cookies=False)
                    if discovered:
                        for d_file in discovered:
                            if hasattr(d_file, 'id') and d_file.id:
                                d_path = getattr(d_file, 'path', '') or ''
                                d_ext = os.path.splitext(d_path)[1].lower()
                                if not d_ext or d_ext in VALID_IMAGE_EXTENSIONS or d_ext == '.zip':
                                    items_to_process.append({'id': d_file.id, 'name': os.path.basename(d_path) or f"{d_file.id}.jpg"})
                except Exception as gd_disc_err:
                    print(f"[GDrive] gdown folder discovery failed: {gd_disc_err}")

        else:
            # Single file or ZIP link
            file_id = extract_gdrive_id(url)
            if not file_id:
                raise ValueError("Could not extract Google Drive file ID from URL.")
            items_to_process.append({'id': file_id, 'name': f"{file_id}.jpg"})

        # If items were discovered, perform streaming per-file import
        if items_to_process:
            total_files = len(items_to_process)
            print(f"[GDrive] Discovered {total_files} photo candidates. Starting streaming live import...")
            if job_id:
                update_job(job_id, {
                    'total': total_files,
                    'current': 0,
                    'percent': 0,
                    'message': f"Found {total_files} photos. Importing in real-time..."
                })

            for idx, item in enumerate(items_to_process):
                # Check cancellation
                if job_id and get_job(job_id).get('cancelled'):
                    print(f"[GDrive] Import job {job_id} was cancelled by user.")
                    break

                fid = item['id']
                fname = item.get('name') or f"{fid}.jpg"
                tmp_file_path = os.path.join(temp_dir, f"{fid}.tmp")

                success = download_gdrive_file(fid, tmp_file_path, api_key=api_key)
                if not success or not os.path.exists(tmp_file_path):
                    if job_id:
                        pct = int(((idx + 1) / total_files) * 100)
                        update_job(job_id, {
                            'current': total_indexed,
                            'percent': min(99, pct),
                            'message': f"Imported {total_indexed} of {total_files} photos ({pct}%)"
                        })
                    continue

                # Process ZIP archive or single image
                if zipfile.is_zipfile(tmp_file_path):
                    try:
                        zip_extract_dir = os.path.join(temp_dir, f"zip_{fid}")
                        with zipfile.ZipFile(tmp_file_path, 'r') as zip_ref:
                            zip_ref.extractall(zip_extract_dir)
                        for zroot, zdirs, zfiles in os.walk(zip_extract_dir):
                            for zf_name in sorted(zfiles):
                                if zf_name.startswith('.'):
                                    continue
                                zext = os.path.splitext(zf_name)[1].lower()
                                if zext in VALID_IMAGE_EXTENSIONS:
                                    zimg_path = os.path.join(zroot, zf_name)
                                    g_img = _process_and_save_single_image(zimg_path, zf_name, event_id)
                                    if g_img:
                                        total_indexed += 1
                                        if job_id:
                                            pct = int(((idx + 1) / total_files) * 100)
                                            update_job(job_id, {
                                                'current': total_indexed,
                                                'percent': min(99, pct),
                                                'message': f"Imported {total_indexed} of {total_files} photos ({pct}%)",
                                                'new_photo': {
                                                    'id': g_img.id,
                                                    'url': f"/secure-media/{g_img.get_secure_thumbnail_token()}/",
                                                    'full_url': f"/secure-media/{g_img.get_secure_file_token()}/",
                                                    'filename': g_img.filename,
                                                    'total_faces': g_img.total_faces,
                                                    'preset_name': g_img.preset_name or 'original',
                                                    'preset_settings': g_img.preset_settings or {},
                                                }
                                            })
                    except Exception as ze:
                        print(f"[DEBUG] Error extracting ZIP {fid}: {ze}")
                    finally:
                        if os.path.exists(tmp_file_path):
                            try: os.remove(tmp_file_path)
                            except: pass
                else:
                    try:
                        g_img = _process_and_save_single_image(tmp_file_path, fname, event_id)
                        if g_img:
                            total_indexed += 1
                            if job_id:
                                pct = int(((idx + 1) / total_files) * 100)
                                update_job(job_id, {
                                    'current': total_indexed,
                                    'percent': min(99, pct),
                                    'message': f"Imported {total_indexed} of {total_files} photos ({pct}%)",
                                    'new_photo': {
                                        'id': g_img.id,
                                        'url': f"/secure-media/{g_img.get_secure_thumbnail_token()}/",
                                        'full_url': f"/secure-media/{g_img.get_secure_file_token()}/",
                                        'filename': g_img.filename,
                                        'total_faces': g_img.total_faces,
                                        'preset_name': g_img.preset_name or 'original',
                                        'preset_settings': g_img.preset_settings or {},
                                    }
                                })
                    finally:
                        if os.path.exists(tmp_file_path):
                            try: os.remove(tmp_file_path)
                            except: pass

        else:
            # Fallback: full folder download via gdown when listing was not possible
            print("[GDrive] Falling back to gdown full folder download...")
            folder_url = f"https://drive.google.com/drive/folders/{folder_id}" if is_folder else url
            if job_id:
                update_job(job_id, {'message': 'Downloading files from Google Drive...'})
            try:
                if is_folder:
                    gdown.download_folder(url=folder_url, output=temp_dir, quiet=True, use_cookies=False)
                else:
                    single_out = os.path.join(temp_dir, "download.tmp")
                    gdown.download(url=url, output=single_out, quiet=True, fuzzy=True)
            except Exception as gd_err:
                print(f"[GDrive] gdown folder download error: {gd_err}")
                err_text = str(gd_err)
                if '404' in err_text or 'permission' in err_text.lower() or 'cannot retrieve' in err_text.lower():
                    raise ValueError("Could not access Google Drive folder. Please ensure the link sharing is set to 'Anyone with the link can view'.")
                raise ValueError(f"Google Drive folder download failed: {gd_err}")

            # Collect downloaded files in temp_dir
            candidate_images = []
            for root, dirs, files in os.walk(temp_dir):
                for f_name in sorted(files):
                    if f_name.startswith('.'):
                        continue
                    f_path = os.path.join(root, f_name)
                    ext = os.path.splitext(f_name)[1].lower()
                    if ext in VALID_IMAGE_EXTENSIONS:
                        candidate_images.append((f_path, f_name))
                    elif ext == '.tmp':
                        test_img = cv2.imread(f_path)
                        if test_img is not None:
                            candidate_images.append((f_path, f_name))

            total_files = len(candidate_images)
            if total_files == 0:
                raise ValueError("No photos found in Google Drive link. Please make sure the folder contains image files (JPG, PNG, WEBP) and link sharing is set to 'Anyone with the link can view'.")

            if job_id:
                update_job(job_id, {
                    'total': total_files,
                    'current': 0,
                    'percent': 0,
                    'message': f"Found {total_files} photos. Importing..."
                })

            for idx, (img_path, orig_fname) in enumerate(candidate_images):
                if job_id and get_job(job_id).get('cancelled'):
                    break
                g_img = _process_and_save_single_image(img_path, orig_fname, event_id)
                if g_img:
                    total_indexed += 1
                    if job_id:
                        pct = int(((idx + 1) / total_files) * 100)
                        update_job(job_id, {
                            'current': total_indexed,
                            'percent': min(99, pct),
                            'message': f"Imported {total_indexed} of {total_files} photos ({pct}%)",
                            'new_photo': {
                                'id': g_img.id,
                                'url': f"/secure-media/{g_img.get_secure_thumbnail_token()}/",
                                'full_url': f"/secure-media/{g_img.get_secure_file_token()}/",
                                'filename': g_img.filename,
                                'total_faces': g_img.total_faces,
                                'preset_name': g_img.preset_name or 'original',
                                'preset_settings': g_img.preset_settings or {},
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
