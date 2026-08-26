import os
import zipfile
import shutil
import threading
from django.shortcuts import render, redirect, get_object_or_404
from django.http import JsonResponse, HttpResponseForbidden
from django.conf import settings
from django.core.files import File
from django.core.files.storage import default_storage
from django.core.files.base import ContentFile
from django.views.decorators.http import require_POST
from django.core.paginator import Paginator
from django.contrib.auth import authenticate, login, logout
from django.contrib.auth.decorators import login_required, user_passes_test
from django.contrib.auth.models import User

from django.utils.text import slugify
from config import VALID_IMAGE_EXTENSIONS
from .models import GalleryImage, FaceEmbedding, Event, UserProfile, SubscriptionPlan, EventShareLink
from .utils import process_gallery_image, search_person_by_selfie
from .tasks import process_image_upload_task, process_zip_upload_task, process_gdrive_import_task
# -------------------------------------------------------------
# Authentication Views
# -------------------------------------------------------------
def user_login(request):
    if request.user.is_authenticated:
        if request.user.is_superuser:
            return redirect('gallery:super_admin_dashboard')
        return redirect('gallery:dashboard')
        
    error = None
    if request.method == 'POST':
        username = request.POST.get('username')
        password = request.POST.get('password')
        user = authenticate(request, username=username, password=password)
        if user is not None:
            login(request, user)
            if user.is_superuser:
                return redirect('gallery:super_admin_dashboard')
            return redirect('gallery:dashboard')
        else:
            error = "Invalid username or password."
            
    return render(request, 'registration/login.html', {'error': error})

def user_logout(request):
    logout(request)
    return redirect('/')

def user_register(request):
    if request.user.is_authenticated:
        return redirect('gallery:dashboard')
        
    error = None
    if request.method == 'POST':
        username = request.POST.get('username', '').strip()
        email = request.POST.get('email', '').strip()
        password = request.POST.get('password', '').strip()
        password_confirm = request.POST.get('password_confirm', '').strip()
        role = request.POST.get('role', 'studio_man')
        plan_id = request.POST.get('plan_id')

        if not username or not password:
            error = "Username and password are required."
        elif password != password_confirm:
            error = "Passwords do not match."
        elif User.objects.filter(username=username).exists():
            error = "Username is already taken."
        else:
            user = User.objects.create_user(username=username, email=email, password=password)
            plan = None
            if plan_id:
                plan = SubscriptionPlan.objects.filter(id=plan_id).first()
            if not plan:
                plan = SubscriptionPlan.objects.first()
                
            UserProfile.objects.create(user=user, role=role, subscription_plan=plan)
            login(request, user)
            return redirect('gallery:dashboard')

    plans = SubscriptionPlan.objects.all()
    roles = UserProfile.ROLE_CHOICES
    return render(request, 'registration/register.html', {
        'error': error,
        'plans': plans,
        'roles': roles
    })

# -------------------------------------------------------------
# Tenant Dashboard
def recalculate_user_storage(user):
    if not user or not hasattr(user, 'profile') or not user.profile:
        return 0.0
    user_images = GalleryImage.objects.filter(event__owner=user)
    total_bytes = 0
    for img in user_images:
        try:
            if img.file and hasattr(img.file, 'size'):
                total_bytes += img.file.size
            if img.thumbnail and hasattr(img.thumbnail, 'size'):
                total_bytes += img.thumbnail.size
        except Exception:
            pass
    actual_mb = total_bytes / (1024 * 1024)
    user.profile.used_storage_mb = actual_mb
    user.profile.save()
    return actual_mb

# -------------------------------------------------------------
def dashboard(request):
    if not request.user.is_authenticated:
        plans = SubscriptionPlan.objects.all()
        return render(request, 'gallery/landing.html', {'plans': plans})
        
    if request.user.is_superuser:
        return redirect('gallery:super_admin_dashboard')
        
    # Get or create profile for tenant
    profile, _ = UserProfile.objects.get_or_create(user=request.user, defaults={'role': 'event_organizer'})
    
    # Recalculate actual storage used from disk files
    recalculate_user_storage(request.user)
    profile.refresh_from_db()
    
    total_images = GalleryImage.objects.filter(event__owner=request.user).count()
    total_faces = FaceEmbedding.objects.filter(image__event__owner=request.user).count()
    events = Event.objects.filter(owner=request.user).order_by('-created_at')
    
    return render(request, 'gallery/dashboard.html', {
        'total_images': total_images,
        'total_faces': total_faces,
        'events': events,
        'profile': profile,
    })

# -------------------------------------------------------------
# Super Admin Views
# -------------------------------------------------------------
@user_passes_test(lambda u: u.is_superuser)
def super_admin_dashboard(request):
    admins = User.objects.filter(is_superuser=False).select_related('profile')
    plans = SubscriptionPlan.objects.all()
    roles = UserProfile.ROLE_CHOICES
    
    total_tenants = admins.count()
    total_plans = plans.count()
    total_storage_used = sum(admin.profile.used_storage_mb for admin in admins if hasattr(admin, 'profile') and admin.profile)
    
    return render(request, 'gallery/super_admin.html', {
        'admins': admins,
        'plans': plans,
        'roles': roles,
        'total_tenants': total_tenants,
        'total_plans': total_plans,
        'total_storage_used': total_storage_used,
    })

@user_passes_test(lambda u: u.is_superuser)
@require_POST
def create_admin(request):
    username = request.POST.get('username')
    email = request.POST.get('email')
    password = request.POST.get('password')
    role = request.POST.get('role')
    plan_id = request.POST.get('plan_id')
    
    if User.objects.filter(username=username).exists():
        return JsonResponse({'success': False, 'message': 'Username already exists.'})
        
    user = User.objects.create_user(username=username, email=email, password=password)
    plan = SubscriptionPlan.objects.filter(id=plan_id).first()
    UserProfile.objects.create(user=user, role=role, subscription_plan=plan)
    
    return JsonResponse({'success': True, 'message': f'Admin {username} created successfully.'})

@user_passes_test(lambda u: u.is_superuser)
@require_POST
def delete_admin(request, user_id):
    user = get_object_or_404(User, id=user_id)
    user.delete()
    return JsonResponse({'success': True, 'message': 'Admin deleted successfully.'})

@user_passes_test(lambda u: u.is_superuser)
@require_POST
def update_admin(request, user_id):
    user = get_object_or_404(User, id=user_id)
    username = request.POST.get('username', '').strip()
    email = request.POST.get('email', '').strip()
    role = request.POST.get('role', '').strip()
    plan_id = request.POST.get('plan_id', '').strip()
    password = request.POST.get('password', '').strip()

    if username and username != user.username:
        if User.objects.filter(username=username).exclude(id=user_id).exists():
            return JsonResponse({'success': False, 'message': 'Username already taken.'})
        user.username = username
    if email:
        user.email = email
    if password:
        user.set_password(password)
    user.save()

    profile = user.profile
    if role:
        profile.role = role
    if plan_id:
        plan = SubscriptionPlan.objects.filter(id=plan_id).first()
        profile.subscription_plan = plan
    profile.save()

    return JsonResponse({'success': True, 'message': f'Tenant {user.username} updated successfully.'})

@user_passes_test(lambda u: u.is_superuser)
@require_POST
def create_plan(request):
    name = request.POST.get('name')
    limit = request.POST.get('storage_limit_mb')
    if name and limit:
        SubscriptionPlan.objects.create(name=name, storage_limit_mb=int(limit))
        return JsonResponse({'success': True, 'message': f'Subscription Plan {name} created successfully.'})
    return JsonResponse({'success': False, 'message': 'Invalid plan details.'})

@user_passes_test(lambda u: u.is_superuser)
@require_POST
def update_plan(request, plan_id):
    plan = get_object_or_404(SubscriptionPlan, id=plan_id)
    name = request.POST.get('name', '').strip()
    limit = request.POST.get('storage_limit_mb', '').strip()
    if name:
        plan.name = name
    if limit:
        plan.storage_limit_mb = int(limit)
    plan.save()
    return JsonResponse({'success': True, 'message': f'Plan "{plan.name}" updated successfully.'})

@user_passes_test(lambda u: u.is_superuser)
@require_POST
def delete_plan(request, plan_id):
    plan = get_object_or_404(SubscriptionPlan, id=plan_id)
    plan_name = plan.name
    plan.delete()
    return JsonResponse({'success': True, 'message': f'Plan "{plan_name}" deleted successfully.'})


# -------------------------------------------------------------
# Tenant Event CRUD
# -------------------------------------------------------------
@login_required
@require_POST
def create_event(request):
    name = request.POST.get('name')
    description = request.POST.get('description')
    if name:
        Event.objects.create(name=name, description=description, owner=request.user)
    return redirect('gallery:dashboard')

@login_required
@require_POST
def update_event(request, slug):
    event = get_object_or_404(Event, slug=slug, owner=request.user)
    name = request.POST.get('name')
    description = request.POST.get('description')
    if name:
        event.name = name
        event.description = description
        event.save()
    return redirect('gallery:event_detail', slug=event.slug)

@login_required
@require_POST
def delete_event(request, slug):
    if request.user.is_superuser:
        event = get_object_or_404(Event, slug=slug)
    else:
        event = get_object_or_404(Event, slug=slug, owner=request.user)

    owner = event.owner
    for img in event.images.all():
        if img.file:
            try:
                img.file.delete(save=False)
            except Exception:
                pass
        if img.thumbnail:
            try:
                img.thumbnail.delete(save=False)
            except Exception:
                pass
    event.delete()
    if owner:
        recalculate_user_storage(owner)
    return redirect('gallery:dashboard')

@login_required
def event_detail(request, slug):
    event = get_object_or_404(Event, slug=slug)
    if not request.user.is_superuser and event.owner != request.user:
        return HttpResponseForbidden("You do not have permission to access this Event.")
        
    from .clustering import get_cached_clusters_for_event, run_clustering_on_upload
    clusters = get_cached_clusters_for_event(event)
    
    if not clusters and event.images.exists():
        run_clustering_on_upload(event)
        clusters = get_cached_clusters_for_event(event)
        
    return render(request, 'gallery/event_detail.html', {
        'event': event,
        'clusters': clusters
    })

@login_required
def photos_api(request):
    event_slug = request.GET.get('event_slug')
    page_num = request.GET.get('page', 1)
    person_label = request.GET.get('person_label', '').strip() or None
    
    event = get_object_or_404(Event, slug=event_slug)
    if not request.user.is_superuser and event.owner != request.user:
        return HttpResponseForbidden("Unauthorized")
        
    if person_label:
        images = GalleryImage.objects.filter(
            event=event,
            embeddings__person_label=person_label
        ).distinct().order_by('-uploaded_at')
    else:
        images = GalleryImage.objects.filter(event=event).order_by('-uploaded_at')
        
    paginator = Paginator(images, 12)
    page = paginator.get_page(page_num)
    
    data = []
    for img in page:
        data.append({
            'id': img.id,
            'filename': img.filename,
            'event_name': img.event.name if img.event else "Event",
            'organiser_name': img.event.owner.username.title() if (img.event and img.event.owner) else "Studio",
            'url': img.thumbnail.url if img.thumbnail else img.file.url,
            'total_faces': img.total_faces,
            'uploaded_at': img.uploaded_at.strftime("%b %d, %H:%M")
        })
        
    return JsonResponse({
        'images': data,
        'has_next': page.has_next()
    })


# -------------------------------------------------------------
# Views Actions
# -------------------------------------------------------------
@login_required
def upload_photos(request):
    if request.method == 'POST':
        files = request.FILES.getlist('photos')
        event_id = request.POST.get('event_id')
        event = get_object_or_404(Event, id=event_id, owner=request.user)
        
        # 1. Storage Limit Check
        profile = request.user.profile
        limit_mb = profile.subscription_plan.storage_limit_mb if profile.subscription_plan else 0
        
        # Calculate incoming upload size
        incoming_size_mb = 0
        for file in files:
            incoming_size_mb += file.size / (1024 * 1024)
            
        if profile.used_storage_mb + incoming_size_mb > limit_mb:
            return JsonResponse({
                'success': False,
                'message': f"Upload failed. This exceeds your subscription storage limit ({limit_mb} MB). You have used {profile.used_storage_mb:.2f} MB."
            }, status=400)
            
        # Deduct / increase user storage
        profile.used_storage_mb += incoming_size_mb
        profile.save()
            
        has_zip = False
        has_background_images = False
        temp_paths = []
        original_filenames = []
        
        import time
        temp_dir = os.path.join(settings.MEDIA_ROOT, 'temp')
        os.makedirs(temp_dir, exist_ok=True)
        
        for file in files:
            ext = os.path.splitext(file.name)[1].lower()
            
            if ext == '.zip':
                has_zip = True
                temp_zip_path = os.path.join(temp_dir, file.name)
                with open(temp_zip_path, 'wb') as f:
                    for chunk in file.chunks():
                        f.write(chunk)
                
                process_zip_upload_task.delay(temp_zip_path, event_id)
            else:
                has_background_images = True
                safe_name = file.name.replace(' ', '_')
                temp_path = os.path.join(temp_dir, f"tmp_{int(time.time()*1000)}_{safe_name}")
                with open(temp_path, 'wb') as f:
                    for chunk in file.chunks():
                        f.write(chunk)
                temp_paths.append(temp_path)
                original_filenames.append(file.name)
                
        if has_background_images:
            process_image_upload_task.delay(temp_paths, event_id, original_filenames)
            
        return JsonResponse({
            'success': True,
            'images': [],
            'has_zip': has_zip,
            'is_background': has_background_images,
            'message': 'Upload successful. Photos are processing in the background.'
        })
        
    return render(request, 'gallery/upload.html')

@login_required
@require_POST
def upload_single_photo(request):
    file = request.FILES.get('photo')
    event_id = request.POST.get('event_id')
    
    if not file or not event_id:
        return JsonResponse({'success': False, 'message': 'File and event ID required.'})
        
    try:
        if request.user.is_superuser:
            event = get_object_or_404(Event, id=event_id)
        else:
            event = get_object_or_404(Event, id=event_id, owner=request.user)

        # Handle user profile & storage check
        if hasattr(request.user, 'profile') and request.user.profile and not request.user.is_superuser:
            profile = request.user.profile
            if profile.subscription_plan:
                limit_mb = profile.subscription_plan.storage_limit_mb
                file_size_mb = file.size / (1024 * 1024)
                if (profile.used_storage_mb + file_size_mb) > limit_mb:
                    return JsonResponse({
                        'success': False,
                        'message': f"Storage limit reached ({limit_mb} MB limit)."
                    })
                profile.used_storage_mb += file_size_mb
                profile.save()

        gallery_image = GalleryImage(filename=file.name, event=event)
        gallery_image.file.save(file.name, file, save=True)
        
        num_faces = process_gallery_image(gallery_image)
        
        return JsonResponse({
            'success': True,
            'image': {
                'id': gallery_image.id,
                'filename': gallery_image.filename,
                'event_name': event.name if event else "Event",
                'organiser_name': event.owner.username.title() if (event and event.owner) else "Studio",
                'url': gallery_image.thumbnail.url if gallery_image.thumbnail else gallery_image.file.url,
                'total_faces': gallery_image.total_faces,
                'uploaded_at': gallery_image.uploaded_at.strftime("%b %d, %H:%M")
            }
        })
    except Exception as e:
        print(f"[DEBUG] upload_single_photo error: {e}")
        return JsonResponse({'success': False, 'message': str(e)})

@login_required
@require_POST
def trigger_clustering(request, slug):
    if request.user.is_superuser:
        event = get_object_or_404(Event, slug=slug)
    else:
        event = get_object_or_404(Event, slug=slug, owner=request.user)
        
    from .clustering import run_clustering_on_upload
    try:
        run_clustering_on_upload(event)
        return JsonResponse({'success': True, 'message': 'Clustering complete.'})
    except Exception as e:
        return JsonResponse({'success': False, 'message': str(e)})

@login_required
def search_person(request):
    results = None
    error_message = None
    selected_event_slug = request.GET.get('event') or request.POST.get('event_slug')
    
    if not selected_event_slug:
        return redirect('gallery:dashboard')

    if request.user.is_superuser:
        selected_event = get_object_or_404(Event, slug=selected_event_slug)
    else:
        selected_event = get_object_or_404(Event, slug=selected_event_slug, owner=request.user)

    if request.method == 'POST' and request.FILES.get('selfie'):
        selfie = request.FILES['selfie']
        import time
        temp_dir = os.path.join(settings.MEDIA_ROOT, 'temp')
        os.makedirs(temp_dir, exist_ok=True)
        safe_name = selfie.name.replace(' ', '_')
        full_temp_path = os.path.join(temp_dir, f"search_{int(time.time()*1000)}_{safe_name}")
        with open(full_temp_path, 'wb') as f:
            for chunk in selfie.chunks():
                f.write(chunk)
        
        try:
            results = search_person_by_selfie(full_temp_path, event_id=selected_event.id, tenant_user=request.user)
            if not request.user.is_superuser:
                results = [res for res in results if res['image'].event and res['image'].event.owner == request.user]
            for res in results:
                name_only, ext = os.path.splitext(res['image'].filename)
                res['download_filename'] = f"{name_only}_{selected_event.slug}{ext}"
        except Exception as e:
            error_message = str(e)
        finally:
            if os.path.exists(full_temp_path):
                os.remove(full_temp_path)
                
    return render(request, 'gallery/search.html', {
        'results': results,
        'error_message': error_message,
        'selected_event': selected_event,
        'event': selected_event,
    })

@login_required
@require_POST
def delete_image(request, image_id):
    gallery_image = get_object_or_404(GalleryImage, id=image_id)
    if not request.user.is_superuser and gallery_image.event.owner != request.user:
        return HttpResponseForbidden("Unauthorized")
        
    owner = gallery_image.event.owner if gallery_image.event else None

    if gallery_image.file:
        try:
            gallery_image.file.delete(save=False)
        except Exception:
            pass
            
    if gallery_image.thumbnail:
        try:
            gallery_image.thumbnail.delete(save=False)
        except Exception:
            pass
        
    gallery_image.delete()
    if owner:
        recalculate_user_storage(owner)
    return JsonResponse({'success': True, 'message': 'Image deleted successfully.'})

@login_required
@require_POST
def bulk_delete_images(request):
    import json
    try:
        raw_ids = request.POST.get('image_ids')
        if raw_ids:
            try:
                image_ids = json.loads(raw_ids)
            except Exception:
                image_ids = [i.strip() for i in raw_ids.split(',') if i.strip()]
        else:
            image_ids = request.POST.getlist('image_ids[]')
            
        if not image_ids:
            return JsonResponse({'success': False, 'message': 'No image IDs provided.'})

        deleted_ids = []
        owner = None
        for img_id in image_ids:
            try:
                gallery_image = GalleryImage.objects.get(id=img_id)
                if not request.user.is_superuser and gallery_image.event.owner != request.user:
                    continue
                
                if gallery_image.event:
                    owner = gallery_image.event.owner
                    
                if gallery_image.file:
                    try:
                        gallery_image.file.delete(save=False)
                    except Exception:
                        pass
                if gallery_image.thumbnail:
                    try:
                        gallery_image.thumbnail.delete(save=False)
                    except Exception:
                        pass
                gallery_image.delete()
                deleted_ids.append(int(img_id))
            except (GalleryImage.DoesNotExist, ValueError):
                continue

        if owner:
            recalculate_user_storage(owner)

        return JsonResponse({
            'success': True,
            'deleted_ids': deleted_ids,
            'message': f'{len(deleted_ids)} photos deleted successfully.'
        })
    except Exception as e:
        return JsonResponse({'success': False, 'message': str(e)})

from .job_tracker import get_job, update_job

@login_required
@require_POST
def gdrive_import(request):
    url = request.POST.get('gdrive_url')
    event_id = request.POST.get('event_id')
    
    if not url:
        return JsonResponse({'success': False, 'message': 'No Google Drive URL provided.'})
        
    event = None
    if event_id:
        event = Event.objects.filter(id=event_id, owner=request.user).first()
    if not event:
        event = Event.objects.filter(owner=request.user).first()
        if not event:
            event = Event.objects.create(name="Drive Imports", description="Imported from Google Drive", owner=request.user)
        
    try:
        import threading
        from .utils import download_and_index_gdrive_link
        
        target_event_id = event.id
        target_event_name = event.name
        job_id = f"import_{event.id}"
        update_job(job_id, {
            'active': True,
            'current': 0,
            'total': 0,
            'percent': 0,
            'message': 'Connecting to Google Drive...',
            'new_photos': []
        })
        
        def run_import():
            try:
                indexed, faces = download_and_index_gdrive_link(url, event_id=target_event_id, job_id=job_id)
                print(f"[SUCCESS] Google Drive import finished for '{target_event_name}': {indexed} images, {faces} faces indexed.")
                
                # Run AI clustering on the newly imported faces
                from .clustering import run_clustering_on_upload
                re_fetched_event = Event.objects.filter(id=target_event_id).first()
                if re_fetched_event:
                    run_clustering_on_upload(re_fetched_event)
                    
                update_job(job_id, {
                    'active': False,
                    'percent': 100,
                    'message': 'Import finished!'
                })
                    
            except Exception as ex:
                print(f"[DEBUG] Background GDrive import thread error: {ex}")
                update_job(job_id, {
                    'active': False,
                    'message': f"Import error: {ex}"
                })
                
        thread = threading.Thread(target=run_import)
        thread.daemon = True
        thread.start()
        
        return JsonResponse({
            'success': True,
            'message': f"Google Drive import started for '{event.name}'!",
            'event_slug': event.slug,
            'job_id': job_id
        })
    except Exception as e:
        print(f"[DEBUG] Google Drive import spawn error: {e}")
        return JsonResponse({'success': False, 'message': str(e)})

@login_required
def gdrive_import_status(request, slug):
    event = get_object_or_404(Event, slug=slug)
    job_id = f"import_{event.id}"
    job_data = get_job(job_id)
    if not job_data:
        job_data = {'active': False, 'current': 0, 'total': 0, 'percent': 0, 'message': '', 'new_photos': []}
    return JsonResponse(job_data)

@login_required
@require_POST
def cancel_gdrive_import(request):
    event_id = request.POST.get('event_id')
    slug = request.POST.get('event_slug')
    if slug and not event_id:
        event = Event.objects.filter(slug=slug, owner=request.user).first()
        if event:
            event_id = event.id
            
    if not event_id:
        return JsonResponse({'success': False, 'message': 'Event ID or slug required.'})
        
    job_id = f"import_{event_id}"
    job_data = get_job(job_id)
    if job_data:
        update_job(job_id, {
            'cancelled': True,
            'active': False,
            'message': 'Import cancelled by user.'
        })
        return JsonResponse({'success': True, 'message': 'Import cancelled.'})
    return JsonResponse({'success': False, 'message': 'No active import found.'})

@login_required
def active_imports_api(request):
    if request.user.is_superuser:
        user_events = Event.objects.all()
    else:
        user_events = Event.objects.filter(owner=request.user)
        
    active_job = None
    for ev in user_events:
        job_id = f"import_{ev.id}"
        job = get_job(job_id)
        if job and job.get('active'):
            active_job = {
                'event_id': ev.id,
                'event_name': ev.name,
                'event_slug': ev.slug,
                'current': job.get('current', 0),
                'total': job.get('total', 0),
                'percent': job.get('percent', 0),
                'message': job.get('message', ''),
                'new_photos': job.get('new_photos', [])
            }
            break
            
    return JsonResponse({'active': active_job is not None, 'job': active_job})

@login_required
def group_by_face(request):
    results = None
    error_message = None
    selected_event_slug = request.GET.get('event') or request.POST.get('event_slug')
    
    if not selected_event_slug:
        return redirect('gallery:dashboard')

    if request.user.is_superuser:
        selected_event = get_object_or_404(Event, slug=selected_event_slug)
    else:
        selected_event = get_object_or_404(Event, slug=selected_event_slug, owner=request.user)

    if request.method == 'POST' and request.FILES.get('selfie'):
        selfie = request.FILES['selfie']
        import time
        temp_dir = os.path.join(settings.MEDIA_ROOT, 'temp')
        os.makedirs(temp_dir, exist_ok=True)
        safe_name = selfie.name.replace(' ', '_')
        full_temp_path = os.path.join(temp_dir, f"group_{int(time.time()*1000)}_{safe_name}")
        with open(full_temp_path, 'wb') as f:
            for chunk in selfie.chunks():
                f.write(chunk)
        
        try:
            results = search_person_by_selfie(full_temp_path, event_id=selected_event.id, tenant_user=request.user)
            if not request.user.is_superuser:
                results = [res for res in results if res['image'].event and res['image'].event.owner == request.user]
            for res in results:
                name_only, ext = os.path.splitext(res['image'].filename)
                res['download_filename'] = f"{name_only}_{selected_event.slug}{ext}"
        except Exception as e:
            error_message = str(e)
        finally:
            if os.path.exists(full_temp_path):
                os.remove(full_temp_path)
                
    return render(request, 'gallery/group_by_face.html', {
        'results': results,
        'error_message': error_message,
        'selected_event': selected_event,
        'event': selected_event,
    })

@login_required
@require_POST
def rename_face_group(request, slug):
    event = get_object_or_404(Event, slug=slug, owner=request.user)
    old_label = request.POST.get('old_label')
    new_label = request.POST.get('new_label')
    
    if old_label and new_label:
        safe_event_name = "".join([c if c.isalnum() else "_" for c in event.name])
        old_dir = os.path.join(settings.MEDIA_ROOT, 'sorted', f"{safe_event_name}_{event.id}", old_label)
        new_dir = os.path.join(settings.MEDIA_ROOT, 'sorted', f"{safe_event_name}_{event.id}", new_label)
        
        if os.path.exists(old_dir):
            try:
                os.rename(old_dir, new_dir)
            except Exception as e:
                print(f"[ERROR] Rename dir failed: {e}")
                
        # Update database records
        FaceEmbedding.objects.filter(image__event=event, person_label=old_label).update(person_label=new_label)
        
        return JsonResponse({'success': True, 'message': f'Group renamed to {new_label}'})
    return JsonResponse({'success': False, 'message': 'Missing parameters.'})

@login_required
@require_POST
def delete_face_group(request, slug):
    event = get_object_or_404(Event, slug=slug, owner=request.user)
    group_label = request.POST.get('group_label')
    
    if group_label:
        safe_event_name = "".join([c if c.isalnum() else "_" for c in event.name])
        group_dir = os.path.join(settings.MEDIA_ROOT, 'sorted', f"{safe_event_name}_{event.id}", group_label)
        
        if os.path.exists(group_dir):
            shutil.rmtree(group_dir)
            
        # Delete matching face records
        FaceEmbedding.objects.filter(image__event=event, person_label=group_label).delete()
        
        return JsonResponse({'success': True, 'message': 'Group deleted successfully.'})
    return JsonResponse({'success': False, 'message': 'Missing parameters.'})

from io import BytesIO
from django.http import HttpResponse

@login_required
def download_event_zip(request, slug):
    if request.user.is_superuser:
        event = get_object_or_404(Event, slug=slug)
    else:
        event = get_object_or_404(Event, slug=slug, owner=request.user)
        
    images = event.images.all()
    if not images:
        return redirect('gallery:event_detail', slug=slug)
        
    zip_buffer = BytesIO()
    with zipfile.ZipFile(zip_buffer, 'w') as zip_file:
        import urllib.request
        for img in images:
            if img.file:
                name_only, ext = os.path.splitext(img.filename)
                zip_filename = f"{name_only}_{event.slug}{ext}"
                content = None
                if hasattr(img.file, 'path') and os.path.exists(img.file.path):
                    with open(img.file.path, 'rb') as f:
                        content = f.read()
                else:
                    try:
                        req = urllib.request.urlopen(img.file.url)
                        content = req.read()
                    except Exception as e:
                        print(f"Error zipping image from URL: {e}")
                        
                if content:
                    zip_file.writestr(zip_filename, content)
                
    zip_buffer.seek(0)
    evt_slug = slugify(event.name) if event.name else event.slug
    zip_filename = f"{evt_slug}_photos.zip"
    response = HttpResponse(zip_buffer.read(), content_type='application/zip')
    response['Content-Disposition'] = f'attachment; filename="{zip_filename}"'
    return response

def download_single_image(request, image_id):
    img = get_object_or_404(GalleryImage, id=image_id)
    
    is_authorized = False
    if request.user.is_authenticated:
        if request.user.is_superuser or (img.event and img.event.owner == request.user):
            is_authorized = True
            
    token = request.GET.get('token')
    if token and not is_authorized and img.event:
        share_link = EventShareLink.objects.filter(token=token, event=img.event).first()
        if share_link and share_link.is_accessible:
            is_authorized = True
            
    if not is_authorized:
        return HttpResponseForbidden("Unauthorized")
        
    name_only, ext = os.path.splitext(img.filename)
    evt_tag = slugify(img.event.name) if (img.event and img.event.name) else (img.event.slug if img.event else 'photo')
    download_name = f"{name_only}_{evt_tag}{ext}"
    
    content = None
    if hasattr(img.file, 'path') and os.path.exists(img.file.path):
        with open(img.file.path, 'rb') as f:
            content = f.read()
    else:
        import urllib.request
        try:
            req = urllib.request.urlopen(img.file.url)
            content = req.read()
        except Exception as e:
            print(f"Error fetching single image file: {e}")
            
    if content is None:
        return HttpResponse("Failed to download image", status=500)
        
    response = HttpResponse(content, content_type='application/octet-stream')
    response['Content-Disposition'] = f'attachment; filename="{download_name}"'
    return response

def download_images_zip(request):
    token = request.GET.get('token') or request.POST.get('token')
    image_ids = request.POST.getlist('image_ids')
    if not image_ids:
        image_ids_str = request.GET.get('ids', '')
        if image_ids_str:
            image_ids = [i.strip() for i in image_ids_str.split(',') if i.strip()]
            
    if not image_ids and token:
        share_link = EventShareLink.objects.filter(token=token).first()
        if share_link and share_link.is_accessible:
            has_session = hasattr(request, 'session')
            if share_link.password and (not has_session or not request.session.get(f'share_auth_{share_link.token}')):
                return HttpResponseForbidden("Password required for share link.")
            if share_link.person_label:
                image_ids = list(GalleryImage.objects.filter(
                    event=share_link.event,
                    embeddings__person_label=share_link.person_label
                ).distinct().values_list('id', flat=True))
            else:
                image_ids = list(GalleryImage.objects.filter(event=share_link.event).values_list('id', flat=True))

    if not image_ids:
        return redirect('gallery:dashboard')

    zip_buffer = BytesIO()
    file_added = False
    added_events = set()
    with zipfile.ZipFile(zip_buffer, 'w') as zip_file:
        for img_id in image_ids:
            try:
                img = GalleryImage.objects.select_related('event').get(id=img_id)
                
                is_authorized = False
                if hasattr(request, 'user') and request.user.is_authenticated:
                    if request.user.is_superuser or (img.event and img.event.owner == request.user):
                        is_authorized = True
                
                if token and not is_authorized and img.event:
                    share_link = EventShareLink.objects.filter(token=token, event=img.event).first()
                    if share_link and share_link.is_accessible:
                        is_authorized = True
                        
                if not is_authorized:
                    continue

                if img.file:
                    name_only, ext = os.path.splitext(img.filename)
                    evt_tag = slugify(img.event.name) if (img.event and img.event.name) else (img.event.slug if img.event else 'photo')
                    zip_filename = f"{name_only}_{evt_tag}{ext}"
                    content = None
                    if hasattr(img.file, 'path') and os.path.exists(img.file.path):
                        with open(img.file.path, 'rb') as f:
                            content = f.read()
                    else:
                        import urllib.request
                        try:
                            req = urllib.request.urlopen(img.file.url)
                            content = req.read()
                        except Exception as e:
                            print(f"Error fetching zip image file: {e}")

                    if content:
                        zip_file.writestr(zip_filename, content)
                        file_added = True
                        if img.event:
                            added_events.add(img.event)
            except GalleryImage.DoesNotExist:
                continue

    if not file_added:
        return HttpResponseForbidden("Unauthorized or no valid images selected.")

    if len(added_events) == 1:
        evt = list(added_events)[0]
        if token:
            link_obj = EventShareLink.objects.filter(token=token).first()
            if link_obj and link_obj.person_label:
                person_tag = slugify(link_obj.person_label)
                download_zip_name = f"{person_tag}_photos.zip"
            else:
                evt_slug = slugify(evt.name) if evt.name else evt.slug
                download_zip_name = f"{evt_slug}_photos.zip"
        else:
            evt_slug = slugify(evt.name) if evt.name else evt.slug
            download_zip_name = f"{evt_slug}_photos.zip"
    elif len(added_events) > 1:
        download_zip_name = "photona_multiple_events_photos.zip"
    else:
        download_zip_name = "photona_photos.zip"

    zip_buffer.seek(0)
    response = HttpResponse(zip_buffer.getvalue(), content_type='application/zip')
    response['Content-Disposition'] = f'attachment; filename="{download_zip_name}"'
    return response


# -------------------------------------------------------------
# Event Share Link Views
# -------------------------------------------------------------

@login_required
def manage_shares(request, slug):
    """Return JSON list of all share links for an event."""
    event = get_object_or_404(Event, slug=slug, owner=request.user)
    links = event.share_links.order_by('-created_at')
    data = []
    for lnk in links:
        data.append({
            'id': lnk.id,
            'label': lnk.label,
            'token': str(lnk.token),
            'is_active': lnk.is_active,
            'has_password': bool(lnk.password),
            'created_at': lnk.created_at.strftime('%b %d, %Y'),
            'expires_at': lnk.expires_at.strftime('%Y-%m-%dT%H:%M') if lnk.expires_at else '',
            'is_expired': lnk.is_expired,
        })
    return JsonResponse({'links': data})


@login_required
@require_POST
def create_share(request, slug):
    """Create a new share link for the event (optionally limited to a specific person)."""
    from django.contrib.auth.hashers import make_password
    from django.utils import timezone

    event = get_object_or_404(Event, slug=slug, owner=request.user)
    label = request.POST.get('label', 'Share Link').strip() or 'Share Link'
    person_label = request.POST.get('person_label', '').strip() or None
    raw_password = request.POST.get('password', '').strip()
    expires_raw = request.POST.get('expires_at', '').strip()

    hashed = make_password(raw_password) if raw_password else None
    expires_dt = None
    if expires_raw:
        try:
            from django.utils.dateparse import parse_datetime
            from django.utils.timezone import make_aware
            expires_dt = parse_datetime(expires_raw)
            if expires_dt and timezone.is_naive(expires_dt):
                expires_dt = make_aware(expires_dt)
        except Exception:
            return JsonResponse({'success': False, 'message': 'Invalid expiry date format.'})

    link = EventShareLink.objects.create(
        event=event,
        label=label,
        person_label=person_label,
        password=hashed,
        expires_at=expires_dt,
    )
    return JsonResponse({
        'success': True,
        'message': f'Share link "{label}" created.',
        'id': link.id,
        'token': str(link.token),
        'label': link.label,
        'person_label': link.person_label or '',
        'is_active': link.is_active,
        'has_password': bool(link.password),
        'created_at': link.created_at.strftime('%b %d, %Y'),
        'expires_at': link.expires_at.strftime('%Y-%m-%dT%H:%M') if link.expires_at else '',
        'is_expired': False,
    })


@login_required
@require_POST
def update_share(request, slug, share_id):
    """Update label, password, active status, or expiry of a share link."""
    from django.contrib.auth.hashers import make_password
    from django.utils import timezone

    event = get_object_or_404(Event, slug=slug, owner=request.user)
    link = get_object_or_404(EventShareLink, id=share_id, event=event)

    label = request.POST.get('label', '').strip()
    if label:
        link.label = label

    person_label = request.POST.get('person_label', None)
    if person_label is not None:
        link.person_label = person_label.strip() or None

    is_active = request.POST.get('is_active', None)
    if is_active is not None:
        link.is_active = (is_active == 'true')

    raw_password = request.POST.get('password', None)
    if raw_password is not None:
        link.password = make_password(raw_password) if raw_password.strip() else None

    expires_raw = request.POST.get('expires_at', None)
    if expires_raw is not None:
        if expires_raw.strip() == '':
            link.expires_at = None
        else:
            try:
                from django.utils.dateparse import parse_datetime
                from django.utils.timezone import make_aware
                expires_dt = parse_datetime(expires_raw)
                if expires_dt and timezone.is_naive(expires_dt):
                    expires_dt = make_aware(expires_dt)
                link.expires_at = expires_dt
            except Exception:
                return JsonResponse({'success': False, 'message': 'Invalid expiry date format.'})

    link.save()
    return JsonResponse({'success': True, 'message': f'Share link "{link.label}" updated.'})


@login_required
@require_POST
def delete_share(request, slug, share_id):
    """Delete a share link."""
    event = get_object_or_404(Event, slug=slug, owner=request.user)
    link = get_object_or_404(EventShareLink, id=share_id, event=event)
    label = link.label
    link.delete()
    return JsonResponse({'success': True, 'message': f'Share link "{label}" deleted.'})


def public_event(request, token):
    """Public read-only view of an event — accessible via share link token."""
    link = get_object_or_404(EventShareLink, token=token)

    if not link.is_active:
        return render(request, 'gallery/public_event.html', {'error': 'disabled', 'link': link})
    if link.is_expired:
        return render(request, 'gallery/public_event.html', {'error': 'expired', 'link': link})

    if link.password:
        session_key = f'share_auth_{link.token}'
        if not request.session.get(session_key):
            return render(request, 'gallery/public_event.html', {
                'requires_password': True,
                'link': link,
                'pw_error': request.GET.get('pw_error', ''),
            })

    event = link.event
    from .clustering import get_cached_clusters_for_event
    clusters = get_cached_clusters_for_event(event)

    # If sharing a specific person only, filter clusters in context
    if link.person_label:
        clusters = [c for c in clusters if c['name'] == link.person_label]

    return render(request, 'gallery/public_event.html', {
        'link': link,
        'event': event,
        'clusters': clusters,
    })


@require_POST
def public_event_auth(request, token):
    """Handle password submission for a protected share link."""
    from django.contrib.auth.hashers import check_password

    link = get_object_or_404(EventShareLink, token=token)
    if not link.is_accessible:
        return redirect(f'/share/{token}/')

    raw = request.POST.get('password', '')
    if link.password and check_password(raw, link.password):
        request.session[f'share_auth_{link.token}'] = True
        return redirect(f'/share/{token}/')
    return redirect(f'/share/{token}/?pw_error=wrong')


def public_search_person(request, token):
    """Public face search for an event share link."""
    link = get_object_or_404(EventShareLink, token=token)

    if not link.is_active:
        return render(request, 'gallery/public_event.html', {'error': 'disabled', 'link': link})
    if link.is_expired:
        return render(request, 'gallery/public_event.html', {'error': 'expired', 'link': link})

    if link.password:
        session_key = f'share_auth_{link.token}'
        if not request.session.get(session_key):
            return render(request, 'gallery/public_event.html', {
                'requires_password': True,
                'link': link,
                'pw_error': request.GET.get('pw_error', ''),
            })

    event = link.event
    results = None
    error_message = None
    search_image_b64 = None

    if request.method == 'POST' and request.FILES.get('selfie'):
        selfie = request.FILES['selfie']
        import time
        import base64
        temp_dir = os.path.join(settings.MEDIA_ROOT, 'temp')
        os.makedirs(temp_dir, exist_ok=True)
        safe_name = selfie.name.replace(' ', '_')
        full_temp_path = os.path.join(temp_dir, f"public_search_{int(time.time()*1000)}_{safe_name}")

        with open(full_temp_path, 'wb') as f:
            for chunk in selfie.chunks():
                f.write(chunk)

        try:
            with open(full_temp_path, 'rb') as img_f:
                search_image_b64 = base64.b64encode(img_f.read()).decode('utf-8')

            results = search_person_by_selfie(full_temp_path, event_id=event.id)
            if link.person_label:
                results = [
                    res for res in results
                    if res['image'].embeddings.filter(person_label=link.person_label).exists()
                ]
            for res in results:
                name_only, ext = os.path.splitext(res['image'].filename)
                res['download_filename'] = f"{name_only}_{event.slug}{ext}"
        except Exception as e:
            error_message = str(e)
        finally:
            if os.path.exists(full_temp_path):
                os.remove(full_temp_path)

    return render(request, 'gallery/public_search.html', {
        'results': results,
        'error_message': error_message,
        'selected_event': event,
        'event': event,
        'link': link,
        'search_image_b64': search_image_b64,
    })



def public_photos_api(request):
    """Paginated photos for public view — gated by share token."""
    token = request.GET.get('token')
    page_num = request.GET.get('page', 1)

    link = get_object_or_404(EventShareLink, token=token)

    if not link.is_accessible:
        return JsonResponse({'images': [], 'has_next': False})

    if link.password:
        session_key = f'share_auth_{link.token}'
        if not request.session.get(session_key):
            return JsonResponse({'error': 'auth_required'}, status=403)

    if link.person_label:
        # Filter images to only those containing the shared person's embeddings
        images = GalleryImage.objects.filter(
            event=link.event,
            embeddings__person_label=link.person_label
        ).distinct().order_by('-uploaded_at')
    else:
        images = GalleryImage.objects.filter(event=link.event).order_by('-uploaded_at')

    paginator = Paginator(images, 20)
    page_obj = paginator.get_page(page_num)

    data = []
    for img in page_obj.object_list:
        data.append({
            'id': img.id,
            'url': request.build_absolute_uri(img.thumbnail.url if img.thumbnail else img.file.url),
            'filename': img.filename,
            'uploaded_at': img.uploaded_at.strftime('%b %d, %H:%M'),
        })
    return JsonResponse({'images': data, 'has_next': page_obj.has_next()})

