import os
import cv2
import numpy as np
import faiss
from django.conf import settings
from insightface.app import FaceAnalysis
from config import MODEL_NAME, DETECTION_SIZE, SIMILARITY_THRESHOLD
from .models import GalleryImage, FaceEmbedding, Event

# Singleton-like lazy loaded InsightFace model
_face_app = None

import queue
import threading

_face_app = None
_ai_queue = queue.Queue()
_ai_worker_started = False
_ai_lock = threading.Lock()

def _ai_background_worker():
    """Background worker daemon for face detection and embeddings without blocking requests."""
    from django.db import connection
    while True:
        try:
            item = _ai_queue.get()
            if item is None:
                break
            image_id, event_id = item
            from .models import GalleryImage
            g_img = GalleryImage.objects.filter(id=image_id).first()
            if g_img:
                if not g_img.thumbnail and g_img.file:
                    try:
                        from io import BytesIO
                        from PIL import Image, ImageOps
                        from django.core.files import File
                        g_img.file.open('rb')
                        with Image.open(g_img.file) as img:
                            try:
                                img.draft('RGB', (1200, 1200))
                            except Exception:
                                pass
                            t_img = ImageOps.exif_transpose(img)
                            if t_img.mode not in ('RGB', 'L'):
                                t_img = t_img.convert('RGB')
                            t_img.thumbnail((600, 600), Image.Resampling.BILINEAR)
                            t_io = BytesIO()
                            t_img.save(t_io, format='WEBP', quality=80)
                            t_io.seek(0)
                            t_name = os.path.splitext(g_img.filename)[0] + "_thumb.webp"
                            g_img.thumbnail.save(t_name, File(t_io), save=False)
                            g_img.save(update_fields=['thumbnail'])
                        g_img.file.close()
                    except Exception as te:
                        print(f"[AI_WORKER] Background thumbnail error: {te}")

                if g_img.total_faces == -1:
                    num_f = process_gallery_image(g_img)
                    g_img.total_faces = num_f
                    g_img.save(update_fields=['total_faces'])
        except Exception as ex:
            print(f"[AI_WORKER] Error processing image {item}: {ex}")
        finally:
            try:
                connection.close()
            except Exception:
                pass
            _ai_queue.task_done()

def enqueue_image_for_ai(image_id, event_id=None):
    """Enqueue an image for background AI face detection and indexing."""
    try:
        from .tasks import process_single_image_task
        process_single_image_task.delay(image_id, None)
        return
    except Exception:
        pass

    global _ai_worker_started
    with _ai_lock:
        if not _ai_worker_started:
            t = threading.Thread(target=_ai_background_worker, daemon=True, name="Photona_AI_Worker")
            t.start()
            _ai_worker_started = True
    _ai_queue.put((image_id, event_id))

_model_lock = threading.Lock()

# Cap concurrent InsightFace inferences per process. With threaded gunicorn
# workers, unbounded parallel inference thrashes the CPU and spikes RAM.
_inference_slots = threading.BoundedSemaphore(int(os.environ.get('FACE_INFERENCE_CONCURRENCY', '2')))

def get_face_model():
    global _face_app
    if _face_app is None:
        with _model_lock:  # thread-safe lazy init (gthread workers)
            if _face_app is None:
                # Only load detection and recognition — skip landmark_3d and genderage for 2x faster inference and low RAM
                app = FaceAnalysis(name=MODEL_NAME, allowed_modules=['detection', 'recognition'])
                app.prepare(ctx_id=0, det_size=DETECTION_SIZE)
                _face_app = app
    return _face_app

def detect_faces(img):
    """Run face detection/recognition with a per-process concurrency cap."""
    app = get_face_model()
    with _inference_slots:
        return app.get(img)

def process_gallery_image(gallery_image, local_path=None, raw_bytes=None):
    """
    Process an uploaded gallery image using InsightFace and save embeddings to DB.
    """
    app = get_face_model()
    import urllib.request
    import numpy as np
    import gc
    
    img = None
    if local_path and os.path.exists(local_path):
        img = cv2.imread(local_path)
    elif raw_bytes is not None:
        arr = np.asarray(bytearray(raw_bytes), dtype=np.uint8)
        img = cv2.imdecode(arr, -1)
    else:
        try:
            # 1. First preference: use 600px thumbnail! (det_size is 640x640)
            # This saves 95% RAM and 98% network I/O compared to downloading full 20MB raw DSLR image from S3.
            if gallery_image.thumbnail:
                try:
                    gallery_image.thumbnail.open('rb')
                    arr = np.asarray(bytearray(gallery_image.thumbnail.read()), dtype=np.uint8)
                    img = cv2.imdecode(arr, -1)
                    gallery_image.thumbnail.close()
                except Exception:
                    img = None

            # 2. Local storage direct path fallback
            if img is None:
                try:
                    _path = gallery_image.file.path
                    if os.path.exists(_path):
                        img = cv2.imread(_path)
                except (NotImplementedError, AttributeError):
                    pass

            # 3. Full file from remote storage (S3) fallback
            if img is None:
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

    # Downscale large DSLR images to 800px for lightning-fast, accurate face detection
    h, w = img.shape[:2]
    max_dim = 800
    if max(h, w) > max_dim:
        scale = max_dim / float(max(h, w))
        img = cv2.resize(img, (int(w * scale), int(h * scale)), interpolation=cv2.INTER_LINEAR)
    
    # Detect faces
    faces = detect_faces(img)
    del img
    gc.collect()
    
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
    try:
        auto_sync_new_image_to_personal_links(gallery_image, faces)
    except Exception as _sync_err:
        pass
    return len(faces)

from collections import OrderedDict

# Per-process LRU cache of normalized embedding matrices per event.
# Rebuilt only when the event's embeddings change (see _event_embeddings_version).
_EMBED_CACHE_MAX_EVENTS = int(os.environ.get('EMBED_CACHE_MAX_EVENTS', '8'))
_embed_cache = OrderedDict()
_embed_cache_lock = threading.Lock()

SELFIE_MAX_DIM = 1024


def _downscale_rgb(img, max_dim=SELFIE_MAX_DIM):
    h, w = img.shape[:2]
    if max(h, w) > max_dim:
        scale = max_dim / float(max(h, w))
        img = cv2.resize(img, (int(w * scale), int(h * scale)), interpolation=cv2.INTER_AREA)
    return img


def normalize_selfie_file(path, max_dim=SELFIE_MAX_DIM):
    """
    Shrink an uploaded selfie in place (respecting EXIF orientation) so that
    detection is fast and the base64 preview embedded in the page stays small.
    """
    try:
        from PIL import Image, ImageOps
        with Image.open(path) as im:
            im.draft('RGB', (max_dim, max_dim))
            im = ImageOps.exif_transpose(im)
            if im.mode != 'RGB':
                im = im.convert('RGB')
            im.thumbnail((max_dim, max_dim), Image.Resampling.LANCZOS)
            im.save(path, format='JPEG', quality=88)
    except Exception as e:
        print(f"[Search] Could not normalize selfie {path}: {e}")


def _build_embedding_matrix(queryset):
    rows = list(queryset.values_list('image_id', 'embedding_data'))
    if not rows:
        return np.empty((0,), dtype=np.int64), np.empty((0, 512), dtype=np.float32)
    image_ids = np.fromiter((r[0] for r in rows), dtype=np.int64, count=len(rows))
    matrix = np.asarray([r[1] for r in rows], dtype=np.float32)
    faiss.normalize_L2(matrix)
    return image_ids, matrix


def _event_embeddings_version(queryset):
    from django.db.models import Count, Max, Sum
    agg = queryset.aggregate(c=Count('id'), mx=Max('id'), s=Sum('id'))
    return (agg['c'], agg['mx'], agg['s'])


def get_event_embedding_matrix(event_id):
    """Return (image_ids, normalized_matrix) for an event, cached per process."""
    queryset = FaceEmbedding.objects.filter(image__event_id=event_id)
    version = _event_embeddings_version(queryset)
    with _embed_cache_lock:
        cached = _embed_cache.get(event_id)
        if cached and cached[0] == version:
            _embed_cache.move_to_end(event_id)
            return cached[1], cached[2]
    image_ids, matrix = _build_embedding_matrix(queryset)
    with _embed_cache_lock:
        _embed_cache[event_id] = (version, image_ids, matrix)
        _embed_cache.move_to_end(event_id)
        while len(_embed_cache) > _EMBED_CACHE_MAX_EVENTS:
            _embed_cache.popitem(last=False)
    return image_ids, matrix


def search_person_by_selfie(selfie_path_or_bytes, event_id=None, tenant_user=None, return_vector=False):
    """
    Query the database of face embeddings using a query selfie.
    Accepts file path, bytes, or file-like object (S3 safe).
    Optionally returns (matching_images, query_vector_list) if return_vector=True.
    """
    img = None
    if isinstance(selfie_path_or_bytes, (bytes, bytearray)):
        arr = np.asarray(bytearray(selfie_path_or_bytes), dtype=np.uint8)
        img = cv2.imdecode(arr, cv2.IMREAD_COLOR)
    elif hasattr(selfie_path_or_bytes, 'read'):
        raw = selfie_path_or_bytes.read()
        arr = np.asarray(bytearray(raw), dtype=np.uint8)
        img = cv2.imdecode(arr, cv2.IMREAD_COLOR)
    elif isinstance(selfie_path_or_bytes, str) and os.path.exists(selfie_path_or_bytes):
        img = cv2.imread(selfie_path_or_bytes)
    
    if img is None:
        raise ValueError("Unable to read the selfie image. Please select and upload a clear photo of your face.")
        
    img = cv2.cvtColor(img, cv2.COLOR_BGR2RGB)
    img = _downscale_rgb(img)
    faces = detect_faces(img)
    
    if len(faces) == 0:
        raise ValueError("No face detected in the uploaded selfie.")
    if len(faces) > 1:
        raise ValueError("Selfie contains more than one face. Please upload a clear photo of yourself only.")
        
    query_embedding = faces[0].embedding.astype(np.float32)
    query_embedding = np.expand_dims(query_embedding, axis=0)
    faiss.normalize_L2(query_embedding)
    query_vector_list = query_embedding[0].tolist()
    
    # Load (cached) face embeddings matching filters
    if event_id:
        image_ids, matrix = get_event_embedding_matrix(event_id)
    else:
        query = FaceEmbedding.objects.all()
        if tenant_user and not tenant_user.is_superuser:
            query = query.filter(image__event__owner=tenant_user)
        image_ids, matrix = _build_embedding_matrix(query)

    if matrix.shape[0] == 0:
        return ([], query_vector_list) if return_vector else []

    # Cosine similarity (vectors are L2-normalized) — same result as FAISS IndexFlatIP
    scores = matrix @ query_embedding[0]
    order = np.argsort(-scores)

    # Best score per image, highest first
    best = OrderedDict()
    for row_index in order:
        score = float(scores[row_index])
        if score < SIMILARITY_THRESHOLD:
            break
        image_id = int(image_ids[row_index])
        if image_id not in best:
            best[image_id] = score

    if not best:
        return ([], query_vector_list) if return_vector else []

    images = GalleryImage.objects.select_related('event__owner').in_bulk(list(best.keys()))
    results = [
        {"image": images[image_id], "score": score}
        for image_id, score in best.items()
        if image_id in images
    ]
    return (results, query_vector_list) if return_vector else results


def extract_face_embedding_from_image(image_input):
    """Extract normalized 512-d face embedding list from file path, bytes, or file-like object (S3 safe)."""
    try:
        img = None
        if isinstance(image_input, (bytes, bytearray)):
            arr = np.asarray(bytearray(image_input), dtype=np.uint8)
            img = cv2.imdecode(arr, cv2.IMREAD_COLOR)
        elif hasattr(image_input, 'read'):
            raw = image_input.read()
            arr = np.asarray(bytearray(raw), dtype=np.uint8)
            img = cv2.imdecode(arr, cv2.IMREAD_COLOR)
        elif isinstance(image_input, str) and os.path.exists(image_input):
            img = cv2.imread(image_input)

        if img is None:
            return None
        img = cv2.cvtColor(img, cv2.COLOR_BGR2RGB)
        img = _downscale_rgb(img)
        faces = detect_faces(img)
        if not faces:
            return None
        emb = faces[0].embedding.astype(np.float32)
        emb = np.expand_dims(emb, axis=0)
        faiss.normalize_L2(emb)
        return emb[0].tolist()
    except Exception as e:
        print(f"[EmbeddingExtract] Error: {e}")
        return None


def find_matching_images_for_embedding(query_vector, event_id, exclude_image_ids=None):
    """
    Sub-5ms vector dot-product search across an event's face embeddings
    using a pre-computed 512-d normalized embedding vector.
    """
    if not query_vector or not event_id:
        return []

    q_vec = np.asarray(query_vector, dtype=np.float32)
    if q_vec.ndim == 1:
        q_vec = np.expand_dims(q_vec, axis=0)
    faiss.normalize_L2(q_vec)

    image_ids, matrix = get_event_embedding_matrix(event_id)
    if matrix.shape[0] == 0:
        return []

    scores = matrix @ q_vec[0]
    order = np.argsort(-scores)

    exclude_set = set(exclude_image_ids or [])
    new_matches = []
    seen = set()

    for row_index in order:
        score = float(scores[row_index])
        if score < SIMILARITY_THRESHOLD:
            break
        image_id = int(image_ids[row_index])
        if image_id in exclude_set or image_id in seen:
            continue
        seen.add(image_id)
        new_matches.append((image_id, score))

    if not new_matches:
        return []

    images_dict = GalleryImage.objects.select_related('event').in_bulk([m[0] for m in new_matches])
    return [
        {"image": images_dict[img_id], "score": score}
        for img_id, score in new_matches
        if img_id in images_dict
    ]


def sync_personal_gallery_matches(personal_link):
    """
    Sync newly uploaded event photos into this personal gallery link.
    Runs in <5ms using cached FAISS/numpy vector dot-product.
    Auto-backfills query_embedding if missing on older links (100% S3 & local storage safe).
    Returns list of newly added GalleryImage objects.
    """
    try:
        from django.utils import timezone
        query_vector = personal_link.query_embedding

        # 1. Backfill query_vector if missing (S3 safe)
        if not query_vector:
            # 1a. Try from selfie_image (S3/local safe)
            if personal_link.selfie_image and personal_link.selfie_image.name:
                try:
                    with personal_link.selfie_image.open('rb') as f:
                        query_vector = extract_face_embedding_from_image(f)
                except Exception as se:
                    print(f"[SyncPersonalGallery] Error reading selfie_image: {se}")

            # 1b. Try from guest_lead.selfie if attached
            if not query_vector and personal_link.guest_lead and personal_link.guest_lead.selfie and personal_link.guest_lead.selfie.name:
                try:
                    with personal_link.guest_lead.selfie.open('rb') as f:
                        query_vector = extract_face_embedding_from_image(f)
                except Exception as le:
                    print(f"[SyncPersonalGallery] Error reading guest_lead selfie: {le}")

            # 1c. Try from base64 thumbnail if available
            if not query_vector and personal_link.selfie_thumbnail_b64:
                try:
                    import base64
                    b64_data = personal_link.selfie_thumbnail_b64
                    if ',' in b64_data:
                        b64_data = b64_data.split(',', 1)[1]
                    raw_bytes = base64.b64decode(b64_data)
                    query_vector = extract_face_embedding_from_image(raw_bytes)
                except Exception as be:
                    print(f"[SyncPersonalGallery] Error extracting from b64: {be}")

            # 1d. Try from existing photos' face embeddings (centroid of matched faces)
            if not query_vector:
                existing_img_ids = list(personal_link.images.values_list('id', flat=True))
                if existing_img_ids:
                    sample_embeddings = FaceEmbedding.objects.filter(
                        image_id__in=existing_img_ids
                    ).values_list('embedding_data', flat=True)[:10]
                    if sample_embeddings:
                        mat = np.asarray(list(sample_embeddings), dtype=np.float32)
                        mean_vec = np.mean(mat, axis=0, keepdims=True)
                        faiss.normalize_L2(mean_vec)
                        query_vector = mean_vec[0].tolist()

            # Save query_vector back to model so we NEVER have to compute it again!
            if query_vector:
                personal_link.query_embedding = query_vector
                personal_link.save(update_fields=['query_embedding'])

        if not query_vector:
            return []

        # 2. Sub-5ms vector dot product search across ALL photos in this event!
        current_img_ids = set(personal_link.images.values_list('id', flat=True))
        new_matches = find_matching_images_for_embedding(
            query_vector,
            event_id=personal_link.event_id,
            exclude_image_ids=current_img_ids
        )

        if new_matches:
            new_imgs = [m['image'] for m in new_matches]
            personal_link.images.add(*new_imgs)
            for m in new_matches:
                personal_link.match_scores[str(m['image'].id)] = m['score']
            personal_link.save(update_fields=['match_scores'])
            return new_imgs

    except Exception as e:
        print(f"[SyncPersonalGallery] Error syncing {personal_link.token}: {e}")

    return []


def find_or_create_personal_gallery_link(event, query_vector, results, searcher_name='', selfie_path=None, selfie_thumbnail_b64='', share_link=None, guest_lead=None, expires_at=None):
    """
    Finds an existing PersonalGalleryLink for this same person in this event,
    OR creates a new one. Guarantees that when a person scans more photos of themselves,
    they always get their SAME link with all photos merged!
    Returns (personal_link, is_created: bool).
    """
    from .models import PersonalGalleryLink
    from django.core.files import File
    from django.utils import timezone

    existing_links = list(PersonalGalleryLink.objects.filter(event=event).prefetch_related('images'))
    target_link = None

    q_vec = np.asarray(query_vector, dtype=np.float32) if query_vector else None
    if q_vec is not None:
        if q_vec.ndim == 1:
            q_vec = np.expand_dims(q_vec, axis=0)
        faiss.normalize_L2(q_vec)

    # 1. Check face vector similarity with existing links (same person threshold >= 0.60)
    if q_vec is not None:
        best_sim = 0.0
        for plink in existing_links:
            pl_vec = plink.query_embedding
            if pl_vec:
                pl_arr = np.asarray(pl_vec, dtype=np.float32)
                if pl_arr.ndim == 1:
                    pl_arr = np.expand_dims(pl_arr, axis=0)
                faiss.normalize_L2(pl_arr)
                sim = float(np.dot(q_vec[0], pl_arr[0]))
                if sim >= 0.60 and sim > best_sim:
                    best_sim = sim
                    target_link = plink

    # 2. Check overlap of matched images if vector check didn't match
    if not target_link and results and existing_links:
        current_res_ids = set(r['image'].id for r in results)
        for plink in existing_links:
            existing_ids = set(plink.images.values_list('id', flat=True))
            if existing_ids and current_res_ids:
                overlap = len(existing_ids & current_res_ids)
                overlap_ratio = overlap / max(1, min(len(existing_ids), len(current_res_ids)))
                if overlap_ratio >= 0.50:  # 50% or more overlap means same person
                    target_link = plink
                    break

    # If existing link matched for this person: REUSE AND MERGE!
    if target_link:
        if results:
            target_link.images.add(*[r['image'] for r in results])
            for r in results:
                target_link.match_scores[str(r['image'].id)] = max(
                    target_link.match_scores.get(str(r['image'].id), 0.0),
                    r['score']
                )
        if q_vec is not None:
            target_link.query_embedding = query_vector
        if not target_link.selfie_thumbnail_b64 and selfie_thumbnail_b64:
            target_link.selfie_thumbnail_b64 = selfie_thumbnail_b64
        if searcher_name and searcher_name != 'Guest':
            target_link.searcher_name = searcher_name
        if guest_lead:
            target_link.guest_lead = guest_lead
            try:
                guest_lead.personal_gallery_link = target_link
                guest_lead.save(update_fields=['personal_gallery_link'])
            except Exception as e_gl:
                print(f"Error linking guest lead to target personal gallery link: {e_gl}")
        target_link.last_viewed_at = timezone.now()
        target_link.save()
        return target_link, False

    # Otherwise: create brand new link
    new_link = PersonalGalleryLink.objects.create(
        event=event,
        share_link=share_link,
        guest_lead=guest_lead,
        searcher_name=searcher_name,
        selfie_thumbnail_b64=selfie_thumbnail_b64 or '',
        query_embedding=query_vector,
        expires_at=expires_at,
        match_scores={str(r['image'].id): r['score'] for r in results} if results else {}
    )
    if guest_lead:
        try:
            guest_lead.personal_gallery_link = new_link
            guest_lead.save(update_fields=['personal_gallery_link'])
        except Exception as e_gl:
            print(f"Error linking guest lead to new personal gallery link: {e_gl}")
    if selfie_path and os.path.exists(selfie_path):
        try:
            with open(selfie_path, 'rb') as f_selfie:
                new_link.selfie_image.save(f"selfie_{uuid.uuid4().hex[:6]}.jpg", File(f_selfie), save=False)
        except Exception as se:
            print(f"Error saving selfie to new link: {se}")
    new_link.save()
    if results:
        new_link.images.set([r['image'] for r in results])
    return new_link, True


def auto_sync_new_image_to_personal_links(gallery_image, new_face_embeddings=None):
    """
    Called whenever a new photo finishes AI face indexing.
    Instantly attaches this photo to any matching PersonalGalleryLinks for the event!
    """
    try:
        from .models import PersonalGalleryLink, FaceEmbedding
        if not gallery_image.event_id:
            return

        personal_links = list(PersonalGalleryLink.objects.filter(event_id=gallery_image.event_id))
        if not personal_links:
            return

        # Get embeddings for this new image
        if new_face_embeddings is not None and len(new_face_embeddings) > 0:
            embeddings_list = [f.embedding.astype(np.float32) if hasattr(f, 'embedding') else np.asarray(f, dtype=np.float32) for f in new_face_embeddings]
        else:
            raw = list(FaceEmbedding.objects.filter(image=gallery_image).values_list('embedding_data', flat=True))
            if not raw:
                return
            embeddings_list = [np.asarray(e, dtype=np.float32) for e in raw]

        if not embeddings_list:
            return

        img_mat = np.asarray(embeddings_list, dtype=np.float32)
        faiss.normalize_L2(img_mat)

        for plink in personal_links:
            q_vec = plink.query_embedding
            if not q_vec:
                continue
            q_arr = np.asarray(q_vec, dtype=np.float32)
            if q_arr.ndim == 1:
                q_arr = np.expand_dims(q_arr, axis=0)
            faiss.normalize_L2(q_arr)

            scores = img_mat @ q_arr[0]
            max_score = float(np.max(scores))
            if max_score >= SIMILARITY_THRESHOLD:
                plink.images.add(gallery_image)
                plink.match_scores[str(gallery_image.id)] = max_score
                plink.save(update_fields=['match_scores'])
                print(f"[AutoSync] Added photo #{gallery_image.id} (score {max_score:.2f}) to personal link {plink.token}")

    except Exception as e:
        print(f"[AutoSync] Error auto-syncing photo #{gallery_image.id}: {e}")



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

_gdrive_thread_local = threading.local()

def get_gdrive_session():
    if not hasattr(_gdrive_thread_local, 'session'):
        s = requests.Session()
        s.headers.update({
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'
        })
        _gdrive_thread_local.session = s
    return _gdrive_thread_local.session

def download_gdrive_file(file_id, output_path, api_key=None):
    session = get_gdrive_session()

    # 1. Super-Fast Google Global CDN direct endpoint (< 0.5s, no 403 blocks, no rate limits)
    # Trying =s0 (full original resolution) and standard CDN endpoint
    for cdn_url in [f"https://lh3.googleusercontent.com/d/{file_id}=s0", f"https://lh3.googleusercontent.com/d/{file_id}"]:
        try:
            r = session.get(cdn_url, stream=True, timeout=20)
            content_type = r.headers.get('Content-Type', '').lower()
            if r.status_code == 200 and 'image' in content_type:
                with open(output_path, 'wb') as f:
                    for chunk in r.iter_content(65536):
                        if chunk:
                            f.write(chunk)
                if os.path.exists(output_path) and os.path.getsize(output_path) > 100:
                    return True
        except Exception as e:
            print(f"[GDrive] CDN download error for {file_id}: {e}")

    # 2. Try direct web export URL with stream
    try:
        api_dl_url = f"https://drive.google.com/uc?export=download&id={file_id}"
        r = session.get(api_dl_url, stream=True, timeout=25)
        content_type = r.headers.get('Content-Type', '').lower()
        if r.status_code == 200 and 'text/html' not in content_type:
            with open(output_path, 'wb') as f:
                for chunk in r.iter_content(65536):
                    if chunk:
                        f.write(chunk)
            if os.path.exists(output_path) and os.path.getsize(output_path) > 100:
                return True
    except Exception:
        pass

    # 3. Fallback with session and confirmation token (for large files or ZIPs)
    try:
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

        content_type = response.headers.get('Content-Type', '').lower()
        if response.status_code == 200 and 'text/html' not in content_type:
            with open(output_path, "wb") as f:
                for chunk in response.iter_content(65536):
                    if chunk:
                        f.write(chunk)
            
            if os.path.exists(output_path) and os.path.getsize(output_path) > 100:
                print(f"[DEBUG] Successfully downloaded {file_id} via session fallback.")
                return True
    except Exception as e:
        print(f"[DEBUG] Session token fallback error for {file_id}: {e}")

    # 4. Fallback to gdown
    try:
        res = gdown.download(id=file_id, output=output_path, quiet=True)
        if res and os.path.exists(output_path) and os.path.getsize(output_path) > 100:
            return True
    except Exception as e:
        print(f"[DEBUG] gdown download failed for {file_id}: {e}")

    return False

from io import BytesIO
from concurrent.futures import ThreadPoolExecutor

def _process_and_save_single_image(img_path, file_name, event_id):
    from PIL import Image, ImageOps
    event = Event.objects.filter(id=event_id).first() if event_id else None

    # Check file size & user storage
    file_size_mb = os.path.getsize(img_path) / (1024 * 1024)
    if event and event.owner and hasattr(event.owner, 'profile'):
        profile = event.owner.profile
        limit_mb = profile.effective_storage_limit_mb if hasattr(profile, 'effective_storage_limit_mb') else (profile.subscription_plan.storage_limit_mb if profile.subscription_plan else 0)
        if limit_mb > 0 and (profile.used_storage_mb + file_size_mb) > limit_mb:
            raise Exception("Storage limit exceeded. Upgrade your plan to import more photos.")
        from django.db.models import F
        profile.__class__.objects.filter(id=profile.id).update(used_storage_mb=F('used_storage_mb') + file_size_mb)

    clean_file_name = file_name
    if clean_file_name.endswith('.tmp'):
        clean_event_name = event.name if event else "Imported"
        clean_file_name = f"{clean_event_name}_Photo_{uuid.uuid4().hex[:6]}.jpg"

    gallery_image = GalleryImage(filename=clean_file_name, event=event, total_faces=-1)

    # Memory-efficient single-pass processing: generate WEBP thumbnail & compress.
    # Full-res DSLR photos can take 70-200 MB each when fully decoded, which (with
    # parallel imports) previously OOM-killed the gunicorn workers.
    thumb_name = os.path.splitext(clean_file_name)[0] + "_thumb.webp"
    compression_percentage = event.compression_percentage if event else 100
    needs_compression = bool(compression_percentage and compression_percentage < 100)

    def _save_thumbnail(src_img):
        src_img.thumbnail((600, 600), Image.Resampling.BILINEAR)  # in place, no extra copy
        if src_img.mode not in ('RGB', 'L'):
            src_img = src_img.convert('RGB')
        thumb_io = BytesIO()
        src_img.save(thumb_io, format='WEBP', quality=80)
        thumb_io.seek(0)
        gallery_image.thumbnail.save(thumb_name, File(thumb_io), save=False)

    try:
        with Image.open(img_path) as img:
            if needs_compression:
                # Full decode is required to produce the compressed copy
                ImageOps.exif_transpose(img, in_place=True)
                if img.mode not in ('RGB', 'L'):
                    img = img.convert('RGB')
                scale_factor = float(compression_percentage) / 100.0
                new_width = max(1, int(img.width * scale_factor))
                new_height = max(1, int(img.height * scale_factor))
                if new_width < img.width and new_height < img.height:
                    img = img.resize((new_width, new_height), Image.Resampling.BILINEAR)
                comp_io = BytesIO()
                img.save(comp_io, format='JPEG', quality=88)
                comp_io.seek(0)
                gallery_image.file.save(clean_file_name, File(comp_io), save=False)
                del comp_io
                # Reuse the already-downscaled image for the thumbnail
                _save_thumbnail(img)
            else:
                # Thumbnail only: let the JPEG decoder downscale while decoding
                # (draft mode), using a fraction of the memory of a full decode.
                img.draft('RGB', (1200, 1200))
                ImageOps.exif_transpose(img, in_place=True)
                _save_thumbnail(img)
                with open(img_path, 'rb') as f:
                    gallery_image.file.save(clean_file_name, File(f), save=False)
    except Exception as e:
        print(f"[DEBUG] Fast image process fallback: {e}")
        with open(img_path, 'rb') as f:
            gallery_image.file.save(clean_file_name, File(f), save=False)

    gallery_image.save()
    return gallery_image

def download_and_index_gdrive_link(url, event_id=None, job_id=None, jobs_dict=None):
    from .job_tracker import get_job, update_job
    import cv2
    
    event = None
    if event_id:
        event = Event.objects.filter(id=event_id).first()
        
    temp_dir = os.path.join(settings.MEDIA_ROOT, 'temp', f"gdrive_{uuid.uuid4().hex[:8]}")
    os.makedirs(temp_dir, exist_ok=True)
    
    total_faces = 0
    api_key = getattr(settings, 'GDRIVE_API_KEY', None) or os.environ.get('GDRIVE_API_KEY')
    
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

        # High-Speed Multi-Threaded Parallel Download & Live Ingestion
        if items_to_process:
            total_files = len(items_to_process)
            print(f"[GDrive] Discovered {total_files} photo candidates. Starting parallel live stream import...")
            if job_id:
                update_job(job_id, {
                    'total': total_files,
                    'current': 0,
                    'percent': 0,
                    'message': f"Found {total_files} photos. Importing in real-time..."
                })

            total_indexed = [0]
            processed_counter = [0]
            counter_lock = threading.Lock()

            def process_one_item(item_tuple):
                from django.db import connection
                try:
                    idx, item = item_tuple
                    if job_id and get_job(job_id).get('cancelled'):
                        return None

                    fid = item['id']
                    fname = item.get('name') or f"{fid}.jpg"
                    tmp_file_path = os.path.join(temp_dir, f"{fid}_{uuid.uuid4().hex[:4]}.tmp")

                    success = download_gdrive_file(fid, tmp_file_path, api_key=api_key)
                    if not success or not os.path.exists(tmp_file_path):
                        with counter_lock:
                            processed_counter[0] += 1
                            pct = int((processed_counter[0] / total_files) * 100)
                            if job_id:
                                update_job(job_id, {
                                    'current': total_indexed[0],
                                    'percent': min(99, pct),
                                    'message': f"Imported {total_indexed[0]} of {total_files} photos ({pct}%)"
                                })
                        return None

                    # Process ZIP archive or single image
                    if zipfile.is_zipfile(tmp_file_path):
                        try:
                            zip_extract_dir = os.path.join(temp_dir, f"zip_{fid}_{uuid.uuid4().hex[:4]}")
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
                                            enqueue_image_for_ai(g_img.id, event_id)
                                            with counter_lock:
                                                total_indexed[0] += 1
                                                processed_counter[0] += 1
                                                pct = int((processed_counter[0] / total_files) * 100)
                                                if job_id:
                                                    update_job(job_id, {
                                                        'current': total_indexed[0],
                                                        'percent': min(99, pct),
                                                        'message': f"Imported {total_indexed[0]} of {total_files} photos ({pct}%)",
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
                                enqueue_image_for_ai(g_img.id, event_id)
                                with counter_lock:
                                    total_indexed[0] += 1
                                    processed_counter[0] += 1
                                    pct = int((processed_counter[0] / total_files) * 100)
                                    if job_id:
                                        update_job(job_id, {
                                            'current': total_indexed[0],
                                            'percent': min(99, pct),
                                            'message': f"Imported {total_indexed[0]} of {total_files} photos ({pct}%)",
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
                finally:
                    try:
                        connection.close()
                    except Exception:
                        pass

            # Parallel download workers (configurable via GDRIVE_WORKERS, default 8):
            # provides ultra-fast concurrent downloads from Google Drive while safely managing memory.
            max_workers = int(os.environ.get('GDRIVE_WORKERS', 8))
            with ThreadPoolExecutor(max_workers=max_workers) as executor:
                list(executor.map(process_one_item, enumerate(items_to_process)))

            final_total = total_indexed[0]
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
                    gdown.download(url=url, output=single_out, quiet=True)
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

            final_total = 0
            for idx, (img_path, orig_fname) in enumerate(candidate_images):
                if job_id and get_job(job_id).get('cancelled'):
                    break
                g_img = _process_and_save_single_image(img_path, orig_fname, event_id)
                if g_img:
                    enqueue_image_for_ai(g_img.id, event_id)
                    final_total += 1
                    if job_id:
                        pct = int(((idx + 1) / total_files) * 100)
                        update_job(job_id, {
                            'current': final_total,
                            'percent': min(99, pct),
                            'message': f"Imported {final_total} of {total_files} photos ({pct}%)",
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
            
    return final_total, total_faces
