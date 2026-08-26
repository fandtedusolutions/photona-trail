import redis
import json
from django.conf import settings

_redis_client = None
def get_redis():
    global _redis_client
    if _redis_client is None:
        try:
            _redis_client = redis.Redis.from_url(settings.CELERY_BROKER_URL)
        except Exception as e:
            print(f"[DEBUG] Redis connection failed: {e}")
    return _redis_client

def get_job(job_id):
    r = get_redis()
    if r:
        try:
            val = r.get(job_id)
            if val:
                return json.loads(val)
        except Exception as e:
            print(f"[DEBUG] Redis get_job error: {e}")
    return {}

def update_job(job_id, updates):
    r = get_redis()
    if r:
        try:
            val = r.get(job_id)
            data = json.loads(val) if val else {}
            
            # Special handling for new_photos list to append instead of overwrite if needed
            # but wait, the frontend just checks length of new_photos.
            # Actually, `updates` might contain `new_photos`. 
            # In utils.py: jobs_dict[job_id]['new_photos'].append(...)
            if 'new_photo' in updates:
                new_photos = data.get('new_photos', [])
                new_photos.append(updates.pop('new_photo'))
                data['new_photos'] = new_photos
                
            data.update(updates)
            r.setex(job_id, 86400, json.dumps(data))
        except Exception as e:
            print(f"[DEBUG] Redis update_job error: {e}")
