import redis
import json
from django.conf import settings

_redis_client = None
_local_jobs = {} # Fallback for local development without Redis

def get_redis():
    global _redis_client
    if _redis_client is None:
        try:
            _redis_client = redis.Redis.from_url(settings.CELERY_BROKER_URL)
        except Exception as e:
            pass
    return _redis_client

def get_job(job_id):
    r = get_redis()
    if r:
        try:
            val = r.get(job_id)
            if val:
                return json.loads(val)
        except Exception as e:
            # Redis is not running (e.g. local dev), fallback to memory
            return _local_jobs.get(job_id, {})
    return _local_jobs.get(job_id, {})

def update_job(job_id, updates):
    r = get_redis()
    
    # Always update local memory as fallback
    if job_id not in _local_jobs:
        _local_jobs[job_id] = {}
        
    updates_copy = updates.copy()
    if 'new_photo' in updates_copy:
        new_photo = updates_copy.pop('new_photo')
        _local_jobs[job_id].setdefault('new_photos', []).append(new_photo)
        
    _local_jobs[job_id].update(updates_copy)
    
    if r:
        try:
            val = r.get(job_id)
            data = json.loads(val) if val else {}
            
            if 'new_photo' in updates:
                new_photos = data.get('new_photos', [])
                new_photos.append(updates.pop('new_photo'))
                data['new_photos'] = new_photos
                
            data.update(updates)
            r.setex(job_id, 86400, json.dumps(data))
        except Exception as e:
            # Fallback to local memory already handled above
            pass
