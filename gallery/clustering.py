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

def get_cached_clusters_for_event(event):
    """
    Extremely fast DB query to retrieve pre-clustered face groups for event page detail loads.
    Does not copy files or perform vector calculations.
    """
    labels = FaceEmbedding.objects.filter(image__event=event).exclude(person_label__isnull=True).values_list('person_label', flat=True).distinct()
    
    clusters = []
    safe_event_name = "".join([c if c.isalnum() else "_" for c in event.name])
    
    for label in labels:
        images = GalleryImage.objects.filter(event=event, embeddings__person_label=label).distinct()
        person_dir = os.path.join(settings.MEDIA_ROOT, 'sorted', f"{safe_event_name}_{event.id}", label)
        
        clusters.append({
            'name': label,
            'image_count': len(images),
            'images': images,
            'folder_path': os.path.relpath(person_dir, settings.BASE_DIR)
        })
        
    clusters.sort(key=lambda c: c['image_count'], reverse=True)
    return clusters
