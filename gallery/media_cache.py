"""
High-throughput media serving helpers.

1. Rendered-output cache: processed images (preset/crop/thumbnail/watermark) are
   rendered once and stored under MEDIA_ROOT/rendered/. Later requests read
   the cached file and skip the PIL work. The cache key includes every input
   that affects the output, so an edit or watermark change produces a new key.

2. X-Accel-Redirect: when USE_X_ACCEL_REDIRECT=True (and nginx has the matching
   `internal` location, see deploy/nginx_photona.conf), Django only checks the
   signed token and nginx streams the bytes. The Gunicorn worker is freed right
   away, which is what allows hundreds of guests to load photos at once.
"""
import hashlib
import json
import mimetypes
import os
import uuid
from urllib.parse import quote

from django.conf import settings
from django.http import FileResponse, HttpResponse

RENDER_SUBDIR = 'rendered'


def _media_root():
    return os.path.abspath(str(settings.MEDIA_ROOT))


def rendered_cache_path(img, variant, watermark_config=None):
    """Return the absolute cache path for a rendered variant of an image."""
    key_src = json.dumps(
        [
            img.id,
            variant,
            img.file.name if img.file else '',
            img.thumbnail.name if img.thumbnail else '',
            img.preset_name or '',
            img.preset_settings or {},
            watermark_config or None,
        ],
        sort_keys=True,
        default=str,
    )
    digest = hashlib.sha1(key_src.encode('utf-8')).hexdigest()[:20]
    rel = os.path.join(RENDER_SUBDIR, f"{img.id % 1000:03d}", f"{img.id}_{variant}_{digest}.jpg")
    return os.path.join(_media_root(), rel)


def write_atomic(abs_path, content):
    """Write bytes atomically so concurrent readers never see partial files."""
    os.makedirs(os.path.dirname(abs_path), exist_ok=True)
    tmp_path = f"{abs_path}.{uuid.uuid4().hex}.tmp"
    with open(tmp_path, 'wb') as f:
        f.write(content)
    os.replace(tmp_path, abs_path)


def purge_rendered_for_image(image_id):
    """Delete cached renders for an image (call on delete/edit to reclaim disk)."""
    folder = os.path.join(_media_root(), RENDER_SUBDIR, f"{image_id % 1000:03d}")
    prefix = f"{image_id}_"
    try:
        for name in os.listdir(folder):
            if name.startswith(prefix):
                try:
                    os.remove(os.path.join(folder, name))
                except OSError:
                    pass
    except FileNotFoundError:
        pass


def local_path_of(field):
    """Absolute local path of a FileField if stored on local disk, else None."""
    try:
        path = field.path
    except (NotImplementedError, AttributeError, ValueError):
        return None
    return path if path and os.path.exists(path) else None


def serve_local_file(abs_path, cache_control, content_type=None, download_name=None):
    """
    Serve a file from local disk. With X-Accel-Redirect enabled (and the file
    inside MEDIA_ROOT) nginx sends the bytes; otherwise Django streams them.
    """
    if not content_type:
        content_type = mimetypes.guess_type(abs_path)[0] or 'image/jpeg'

    media_root = _media_root()
    abs_path = os.path.abspath(abs_path)
    use_accel = getattr(settings, 'USE_X_ACCEL_REDIRECT', False) and abs_path.startswith(media_root + os.sep)

    if use_accel:
        rel = os.path.relpath(abs_path, media_root).replace(os.sep, '/')
        prefix = getattr(settings, 'X_ACCEL_REDIRECT_PREFIX', '/protected-media/').rstrip('/') + '/'
        response = HttpResponse(content_type=content_type)
        response['X-Accel-Redirect'] = prefix + quote(rel)
    else:
        response = FileResponse(open(abs_path, 'rb'), content_type=content_type)

    response['Cache-Control'] = cache_control
    if download_name:
        safe_name = download_name.replace('"', '')
        response['Content-Disposition'] = f'attachment; filename="{safe_name}"'
    return response


def serve_storage_field(target_field, cache_control, download_name=None):
    """
    High-performance serve for a Model ImageField/FileField.
    1. If local disk: serve via X-Accel-Redirect (nginx) or direct FileResponse.
    2. If AWS S3 (USE_S3=True): issue a 302 redirect directly to S3 URL
       (with presigned attachment download if requested), completely bypassing EC2!
    """
    from django.http import HttpResponseRedirect

    # 1. Check local file path
    local_path = local_path_of(target_field)
    if local_path:
        return serve_local_file(local_path, cache_control, None, download_name)

    # 2. Check S3 / remote storage
    if getattr(settings, 'USE_S3', False):
        try:
            if download_name and hasattr(target_field.storage, 'connection'):
                try:
                    s3_client = target_field.storage.connection.meta.client
                    url = s3_client.generate_presigned_url(
                        'get_object',
                        Params={
                            'Bucket': target_field.storage.bucket_name,
                            'Key': target_field.name,
                            'ResponseContentDisposition': f'attachment; filename="{download_name}"',
                        },
                        ExpiresIn=3600
                    )
                    return HttpResponseRedirect(url)
                except Exception:
                    pass
            return HttpResponseRedirect(target_field.url)
        except Exception:
            pass

    # 3. Fallback: stream through Django
    f = target_field.open('rb')
    response = FileResponse(f, content_type='image/jpeg')
    response['Cache-Control'] = cache_control
    if download_name:
        response['Content-Disposition'] = f'attachment; filename="{download_name}"'
    return response

