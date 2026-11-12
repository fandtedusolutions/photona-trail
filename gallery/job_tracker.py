import os
import json
import threading
from django.conf import settings
from django.core.cache import cache

JOB_CACHE_TIMEOUT = 86400  # 24 hours
_job_lock = threading.Lock()

def _get_job_file(job_id):
    jobs_dir = os.path.join(settings.MEDIA_ROOT, 'jobs')
    os.makedirs(jobs_dir, exist_ok=True)
    return os.path.join(jobs_dir, f"{job_id}.json")

def get_job(job_id):
    cache_key = f"job_track_{job_id}"
    try:
        cached = cache.get(cache_key)
        if cached is not None and isinstance(cached, dict):
            return cached
    except Exception:
        pass

    file_path = _get_job_file(job_id)
    if os.path.exists(file_path):
        try:
            with open(file_path, 'r') as f:
                data = json.load(f)
                try:
                    cache.set(cache_key, data, JOB_CACHE_TIMEOUT)
                except Exception:
                    pass
                return data
        except Exception:
            pass
    return {}

def reset_job(job_id):
    job = {
        'active': True,
        'current': 0,
        'total': 0,
        'percent': 0,
        'message': 'Starting extraction...',
        'new_photos': [],
        'cancelled': False
    }
    cache_key = f"job_track_{job_id}"
    try:
        cache.set(cache_key, job, JOB_CACHE_TIMEOUT)
    except Exception:
        pass

    file_path = _get_job_file(job_id)
    tmp_path = f"{file_path}.tmp"
    try:
        with open(tmp_path, 'w') as f:
            json.dump(job, f)
        os.replace(tmp_path, file_path)
    except Exception:
        pass
    return job

def update_job(job_id, updates):
    with _job_lock:
        job = get_job(job_id) or {}
        
        updates_copy = updates.copy()
        if 'new_photo' in updates_copy:
            new_photo = updates_copy.pop('new_photo')
            photos_list = job.setdefault('new_photos', [])
            photos_list.append(new_photo)
            if len(photos_list) > 10:
                job['new_photos'] = photos_list[-10:]
            
        job.update(updates_copy)
        
        cache_key = f"job_track_{job_id}"
        try:
            cache.set(cache_key, job, JOB_CACHE_TIMEOUT)
        except Exception:
            pass

        file_path = _get_job_file(job_id)
        tmp_path = f"{file_path}.tmp"
        try:
            with open(tmp_path, 'w') as f:
                json.dump(job, f)
            os.replace(tmp_path, file_path)
        except Exception:
            pass
        return job
