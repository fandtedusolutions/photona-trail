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

def process_gallery_image(gallery_image, local_path=None):
    """
    Process an uploaded gallery image using InsightFace and save embeddings to DB.
    """
    app = get_face_model()
    import urllib.request
    import numpy as np
    
    img = None
    if local_path and os.path.exists(local_path):
        img = cv2.imread(local_path)
    elif hasattr(gallery_image.file, 'path') and os.path.exists(gallery_image.file.path):
        img = cv2.imread(gallery_image.file.path)
    else:
        try:
            req = urllib.request.urlopen(gallery_image.file.url)
            arr = np.asarray(bytearray(req.read()), dtype=np.uint8)
            img = cv2.imdecode(arr, -1)
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

    # Downscale very large images (DSLR photos) for fast, robust face detection
    h, w = img.shape[:2]
    max_dim = 1920
    if max(h, w) > max_dim:
        scale = max_dim / float(max(h, w))
        img = cv2.resize(img, (int(w * scale), int(h * scale)), interpolation=cv2.INTER_AREA)
    
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
    session = requests.Session()
    session.headers.update({
        'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'
    })
    
    urls_to_try = [
        f"https://drive.usercontent.google.com/download?id={file_id}&export=download",
        f"https://drive.google.com/uc?export=download&id={file_id}"
    ]
    
    for dl_url in urls_to_try:
        try:
            response = session.get(dl_url, stream=True, timeout=30)
            token = None
            for key, value in response.cookies.items():
                if key.startswith('download_warning'):
                    token = value
                    break
                    
            if token:
                response = session.get(f"{dl_url}&confirm={token}", stream=True, timeout=30)
                
            if response.status_code == 200 and len(response.content) > 100:
                with open(output_path, 'wb') as f:
                    for chunk in response.iter_content(chunk_size=65536):
                        if chunk:
                            f.write(chunk)
                return True
        except Exception as ex:
            print(f"[DEBUG] Failed downloading file {file_id} via {dl_url}: {ex}")
            
    return False

def download_and_index_gdrive_link(url, event_id=None, job_id=None, jobs_dict=None):
    event = None
    if event_id:
        event = Event.objects.filter(id=event_id).first()
        
    temp_dir = os.path.join(settings.MEDIA_ROOT, 'temp', f"gdrive_{uuid.uuid4().hex[:8]}")
    os.makedirs(temp_dir, exist_ok=True)
    
    total_indexed = 0
    total_faces = 0
    
    try:
        file_ids = []
        is_folder = "/folders/" in url or "/drive/folders/" in url or ("id=" in url and "folder" in url.lower())
        
        if is_folder:
            folder_id = extract_gdrive_id(url)
            if not folder_id:
                raise ValueError("Could not extract Google Drive folder ID from URL.")
                
            session = requests.Session()
            session.headers.update({
                'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'
            })
            folder_page_url = f"https://drive.google.com/drive/folders/{folder_id}?usp=sharing"
            resp = session.get(folder_page_url, timeout=20)
            
            extracted = set(re.findall(r'\"(1[a-zA-Z0-9-_]{32})\"', resp.text))
            file_ids = [fid for fid in extracted if fid != folder_id]
            print(f"[GDrive] Discovered {len(file_ids)} public files in folder {folder_id}")
        else:
            file_id = extract_gdrive_id(url)
            if file_id:
                file_ids = [file_id]

        if not file_ids:
            raise ValueError("No public files found in the Google Drive link.")

        total_files = len(file_ids)
        if jobs_dict is not None and job_id in jobs_dict:
            jobs_dict[job_id]['total'] = total_files
            jobs_dict[job_id]['message'] = f"Found {total_files} files. Downloading & indexing..."

        for fid in file_ids:
            # Check if job was cancelled by user
            if job_id and jobs_dict and jobs_dict.get(job_id, {}).get('cancelled'):
                print(f"[GDrive] Import job {job_id} was cancelled by user. Stopping download loop.")
                break

            tmp_file_path = os.path.join(temp_dir, f"{fid}.tmp")
            success = download_gdrive_file_by_id(fid, tmp_file_path)
            if not success or not os.path.exists(tmp_file_path):
                continue
                
            # Re-fetch event inside thread loop to avoid cross-thread DB session issues
            event = Event.objects.filter(id=event_id).first() if event_id else None
                
            # Check if file is a ZIP archive
            if zipfile.is_zipfile(tmp_file_path):
                try:
                    extract_zip_path = os.path.join(temp_dir, f"zip_{fid}")
                    with zipfile.ZipFile(tmp_file_path, 'r') as zip_ref:
                        zip_ref.extractall(extract_zip_path)
                        
                    for root, dirs, files in os.walk(extract_zip_path):
                        for file_name in files:
                            f_ext = os.path.splitext(file_name)[1].lower()
                            if f_ext in VALID_IMAGE_EXTENSIONS:
                                full_img_path = os.path.join(root, file_name)
                                img = cv2.imread(full_img_path)
                                if img is not None:
                                    if event and event.owner and hasattr(event.owner, 'profile'):
                                        profile = event.owner.profile
                                        f_size_mb = os.path.getsize(full_img_path) / (1024 * 1024)
                                        if profile.subscription_plan and (profile.used_storage_mb + f_size_mb) > profile.subscription_plan.storage_limit_mb:
                                            continue
                                        profile.used_storage_mb += f_size_mb
                                        profile.save()

                                    with open(full_img_path, 'rb') as img_f:
                                        gallery_image = GalleryImage(filename=file_name, event=event)
                                        gallery_image.file.save(file_name, File(img_f), save=True)
                                        
                                    num_faces = process_gallery_image(gallery_image, local_path=full_img_path)
                                    total_faces += num_faces
                                    total_indexed += 1

                                    if jobs_dict is not None and job_id in jobs_dict:
                                        pct = int((total_indexed / total_files) * 100)
                                        jobs_dict[job_id]['current'] = total_indexed
                                        jobs_dict[job_id]['percent'] = min(99, pct)
                                        jobs_dict[job_id]['message'] = f"Imported {total_indexed} of {total_files} photos ({pct}%)"
                                        jobs_dict[job_id]['new_photos'].append({
                                            'id': gallery_image.id,
                                            'url': gallery_image.thumbnail.url if gallery_image.thumbnail else gallery_image.file.url,
                                            'filename': gallery_image.filename,
                                            'total_faces': gallery_image.total_faces
                                        })
                except Exception as ze:
                    print(f"[DEBUG] Error extracting inner ZIP {fid}: {ze}")
            else:
                img = cv2.imread(tmp_file_path)
                if img is not None:
                    clean_event_name = event.name if event else "Imported"
                    file_name = f"{clean_event_name}_Photo_{total_indexed + 1}.jpg"
                    if event and event.owner and hasattr(event.owner, 'profile'):
                        profile = event.owner.profile
                        file_size_mb = os.path.getsize(tmp_file_path) / (1024 * 1024)
                        if profile.subscription_plan and (profile.used_storage_mb + file_size_mb) > profile.subscription_plan.storage_limit_mb:
                            continue
                        profile.used_storage_mb += file_size_mb
                        profile.save()

                    with open(tmp_file_path, 'rb') as img_f:
                        gallery_image = GalleryImage(filename=file_name, event=event)
                        gallery_image.file.save(file_name, File(img_f), save=True)
                        
                    num_faces = process_gallery_image(gallery_image, local_path=tmp_file_path)
                    total_faces += num_faces
                    total_indexed += 1

                    if jobs_dict is not None and job_id in jobs_dict:
                        pct = int((total_indexed / total_files) * 100)
                        jobs_dict[job_id]['current'] = total_indexed
                        jobs_dict[job_id]['percent'] = min(99, pct)
                        jobs_dict[job_id]['message'] = f"Imported {total_indexed} of {total_files} photos ({pct}%)"
                        jobs_dict[job_id]['new_photos'].append({
                            'id': gallery_image.id,
                            'url': gallery_image.thumbnail.url if gallery_image.thumbnail else gallery_image.file.url,
                            'filename': gallery_image.filename,
                            'event_name': event.name if event else "Event",
                            'organiser_name': event.owner.username.title() if (event and event.owner) else "Studio",
                            'total_faces': gallery_image.total_faces
                        })

    except Exception as e:
        print(f"[DEBUG] Google Drive import error: {e}")
        raise e
    finally:
        if job_id and jobs_dict and job_id in jobs_dict:
            jobs_dict[job_id]['active'] = False
            jobs_dict[job_id]['percent'] = 100
            jobs_dict[job_id]['message'] = "Import finished!"
        if os.path.exists(temp_dir):
            shutil.rmtree(temp_dir, ignore_errors=True)
            
    return total_indexed, total_faces

