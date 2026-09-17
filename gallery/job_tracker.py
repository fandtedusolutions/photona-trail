import json
from django.core.cache import cache

def get_job(job_id):
    return cache.get(job_id, {})

def update_job(job_id, updates):
    job = cache.get(job_id, {})
    
    updates_copy = updates.copy()
    if 'new_photo' in updates_copy:
        new_photo = updates_copy.pop('new_photo')
        job.setdefault('new_photos', []).append(new_photo)
        
    job.update(updates_copy)
    cache.set(job_id, job, timeout=86400)
