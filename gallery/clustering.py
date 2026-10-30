import os
import shutil
import numpy as np
from django.conf import settings
from config import SIMILARITY_THRESHOLD
from .models import GalleryImage, FaceEmbedding

def run_clustering_on_upload(event):
    """
    Cluster all face embeddings for the event, update their labels in the DB, 
    and physically structure them in subfolders on disk.
    Called only during uploads/imports.
    """
    images = event.images.all()
    if not images.exists():
        return

    face_records = list(FaceEmbedding.objects.filter(image__in=images))
    if not face_records:
        return

    clusters = []
    CLUSTERING_THRESHOLD = max(SIMILARITY_THRESHOLD, 0.48)

    for record in face_records:
        vec = record.get_numpy_embedding()
        vec = vec / np.linalg.norm(vec)
        
        best_cluster = None
        best_sim = -1
        
        for cluster in clusters:
            sim = np.dot(vec, cluster['centroid'])
            if sim > best_sim:
                best_sim = sim
                best_cluster = cluster
                
        if best_cluster and best_sim >= CLUSTERING_THRESHOLD:
            best_cluster['faces'].append(record)
            best_cluster['image_ids'].add(record.image.id)
            all_vecs = np.array([r.get_numpy_embedding() for r in best_cluster['faces']])
            mean_vec = np.mean(all_vecs, axis=0)
            best_cluster['centroid'] = mean_vec / np.linalg.norm(mean_vec)
        else:
            clusters.append({
                'centroid': vec,
                'faces': [record],
                'image_ids': {record.image.id}
            })

    # Sort clusters by size
    clusters.sort(key=lambda c: len(c['image_ids']), reverse=True)

    # Recreate the sorted base folder on disk
    safe_event_name = "".join([c if c.isalnum() else "_" for c in event.name])
    sorted_base_dir = os.path.join(settings.MEDIA_ROOT, 'sorted', f"{safe_event_name}_{event.id}")
    
    if os.path.exists(sorted_base_dir):
        shutil.rmtree(sorted_base_dir)
    os.makedirs(sorted_base_dir, exist_ok=True)

    # Save cluster labels in the DB and copy files
    for idx, cluster in enumerate(clusters, start=1):
        person_name = f"Person_{idx}"
        person_dir = os.path.join(sorted_base_dir, person_name)
        os.makedirs(person_dir, exist_ok=True)
        
        # Update person_label in DB for all faces in this cluster
        for face_rec in cluster['faces']:
            face_rec.person_label = person_name
            face_rec.save(update_fields=['person_label'])

from django.core.cache import cache
from django.db.models import Count, Min

def get_cached_clusters_for_event(event):
    """
    Extremely fast DB aggregation and caching to retrieve pre-clustered face groups for event page detail loads.
    Runs in 1-2 fast queries instead of dozens of loops, cached for instant page rendering.
    """
    cache_key = f"event_clusters_v2_{event.id}"
    try:
        cached_data = cache.get(cache_key)
        if cached_data is not None:
            return cached_data
    except Exception:
        pass

    # 1. Single aggregated DB query to get all person labels, counts, and a sample cover image ID
    cluster_stats = FaceEmbedding.objects.filter(
        image__event=event
    ).exclude(
        person_label__isnull=True
    ).exclude(
        person_label=''
    ).values('person_label').annotate(
        img_count=Count('image_id', distinct=True),
        cover_image_id=Min('image_id')
    ).order_by('-img_count')

    if not cluster_stats:
        try:
            cache.set(cache_key, [], 180)
        except Exception:
            pass
        return []

    # 2. Bulk fetch only the sample cover images (1 query)
    cover_ids = [c['cover_image_id'] for c in cluster_stats if c['cover_image_id']]
    images_by_id = {img.id: img for img in GalleryImage.objects.filter(id__in=cover_ids).only('id', 'filename', 'thumbnail', 'file')}

    safe_event_name = "".join([c if c.isalnum() else "_" for c in event.name])
    clusters = []
    for c in cluster_stats:
        label = c['person_label']
        cover_img = images_by_id.get(c['cover_image_id'])
        person_dir = os.path.join(settings.MEDIA_ROOT, 'sorted', f"{safe_event_name}_{event.id}", label)
        clusters.append({
            'name': label,
            'image_count': c['img_count'],
            'cover_image': cover_img,
            'first_img': cover_img,
            'images': [cover_img] if cover_img else [],
            'folder_path': os.path.relpath(person_dir, settings.BASE_DIR) if os.path.exists(person_dir) else ""
        })

    # Cache for 5 minutes (300 seconds)
    try:
        cache.set(cache_key, clusters, 300)
    except Exception:
        pass
    return clusters

