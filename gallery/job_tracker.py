import os
import json
from django.conf import settings

def _get_job_file(job_id):
    jobs_dir = os.path.join(settings.MEDIA_ROOT, 'jobs')
    os.makedirs(jobs_dir, exist_ok=True)
    return os.path.join(jobs_dir, f"{job_id}.json")

def get_job(job_id):
    file_path = _get_job_file(job_id)
    if os.path.exists(file_path):
        try:
            with open(file_path, 'r') as f:
                return json.load(f)
        except:
            pass
    return {}
def reset_job(job_id):
    file_path = _get_job_file(job_id)
    job = {
        'active': True,
        'current': 0,
        'total': 0,
        'percent': 0,
        'message': 'Connecting to Google Drive...',
        'new_photos': [],
        'cancelled': False
    }
    try:
        with open(file_path, 'w') as f:
            json.dump(job, f)
    except:
        pass
    return job

def update_job(job_id, updates):
    job = get_job(job_id)
    
    updates_copy = updates.copy()
    if 'new_photo' in updates_copy:
        new_photo = updates_copy.pop('new_photo')
        job.setdefault('new_photos', []).append(new_photo)
        
    job.update(updates_copy)
    
    file_path = _get_job_file(job_id)
    try:
        with open(file_path, 'w') as f:
            json.dump(job, f)
    except:
        pass
