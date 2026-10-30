import os
import zipfile
import shutil
import threading
from django.shortcuts import render, redirect, get_object_or_404
from django.urls import reverse
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
from django.contrib import messages
from django.utils import timezone
import datetime

from django.utils.text import slugify
from config import VALID_IMAGE_EXTENSIONS
from .models import GalleryImage, FaceEmbedding, Event, UserProfile, SubscriptionPlan, EventShareLink, GuestLead
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

    plans = SubscriptionPlan.objects.filter(is_active=True)
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
        plans = SubscriptionPlan.objects.filter(is_active=True)
        return render(request, 'gallery/landing.html', {'plans': plans})
        
    if request.user.is_superuser:
        return redirect('gallery:super_admin_dashboard')
        
    # Get or create profile for tenant
    profile, _ = UserProfile.objects.get_or_create(user=request.user, defaults={'role': 'event_organizer'})

    
    total_images = GalleryImage.objects.filter(event__owner=request.user).count()
    total_faces = FaceEmbedding.objects.filter(image__event__owner=request.user).count()
    events = Event.objects.filter(owner=request.user).order_by('-created_at')
    total_events = events.count()
    
    # Calculate Events with Photos
    from django.db.models import Count
    events_with_photos = Event.objects.filter(owner=request.user).annotate(image_count=Count('images')).filter(image_count__gt=0).count()
    
    # Total Users (Guest Leads)
    from .models import GuestLead
    total_users = GuestLead.objects.filter(event__owner=request.user).count()
    
    # Quota calculations
    plan = profile.subscription_plan
    storage_limit = 0
    # Assuming storage_limit_mb roughly corresponds to number of allowed photos for this metric
    # But usually a photo is ~1-5MB. We'll show the actual MB limit for now, or just limit count.
    # We will simulate "Gross Photos Uploaded / Allowed"
    # Actually, we can use a hardcoded 50,000 or the plan limit for the UI
    allowed_uploads = 50000 
    if plan:
        # Convert MB to approx photo count (assuming 2MB per photo)
        allowed_uploads = int((plan.storage_limit_mb * 1024 * 1024) / (2 * 1024 * 1024))
        
    upload_percentage = 0
    if allowed_uploads > 0:
        upload_percentage = (total_images / allowed_uploads) * 100
        
    upload_percentage = min(upload_percentage, 100) # cap at 100%

    # Get recent events (up to 3) for the dashboard carousel
    recent_events = events[:3]
    
    return render(request, 'gallery/dashboard.html', {
        'total_images': total_images,
        'total_faces': total_faces,
        'events': events,
        'recent_events': recent_events,
        'total_events': total_events,
        'profile': profile,
        'events_with_photos': events_with_photos,
        'total_users': total_users,
        'allowed_uploads': allowed_uploads,
        'upload_percentage': upload_percentage,
    })

@login_required
def events_list(request):
    if request.user.is_superuser:
        return redirect('gallery:super_admin_dashboard')
        
    profile = request.user.profile
    events = Event.objects.filter(owner=request.user).order_by('-created_at')
    
    return render(request, 'gallery/events_list.html', {
        'events': events,
        'profile': profile,
    })

@login_required
def plans_view(request):
    if request.user.is_superuser:
        return redirect('gallery:super_admin_dashboard')
        
    profile = UserProfile.objects.filter(user=request.user).first()
    plan = profile.subscription_plan if profile else None
    
    # Calculate storage
    used_storage = 0
    upload_percentage = 0
    if profile:
        used_storage = profile.used_storage_mb
        upload_percentage = profile.storage_percentage
        
    context = {
        'plan': plan,
        'used_storage': used_storage,
        'upload_percentage': upload_percentage,
    }
    return render(request, 'gallery/plans.html', context)

# -------------------------------------------------------------
# Super Admin Views
# -------------------------------------------------------------
def _get_super_admin_context(request):
    admins = User.objects.filter(is_superuser=False).select_related('profile')
    plans = SubscriptionPlan.objects.filter(is_active=True)
    roles = UserProfile.ROLE_CHOICES
    
    total_tenants = admins.count()
    total_plans = plans.count()
    total_storage_used = 0
    for admin in admins:
        try:
            total_storage_used += admin.profile.used_storage_mb
        except Exception:
            pass
    
    leads = GuestLead.objects.all().select_related('event', 'event__owner').order_by('-created_at')
    
    search_q = request.GET.get('search_q')
    organizer_id = request.GET.get('organizer_id')
    event_id = request.GET.get('event_id')
    start_date = request.GET.get('start_date')
    end_date = request.GET.get('end_date')
    
    if search_q:
        from django.db.models import Q
        leads = leads.filter(Q(name__icontains=search_q) | Q(phone__icontains=search_q) | Q(email__icontains=search_q))
    if organizer_id:
        leads = leads.filter(event__owner_id=organizer_id)
    if event_id:
        leads = leads.filter(event_id=event_id)
    if start_date:
        leads = leads.filter(created_at__date__gte=start_date)
    if end_date:
        leads = leads.filter(created_at__date__lte=end_date)
        
    all_events = Event.objects.all().select_related('owner')
    
    return {
        'admins': admins,
        'plans': plans,
        'roles': roles,
        'total_tenants': total_tenants,
        'total_plans': total_plans,
        'total_storage_used': total_storage_used,
        'leads': leads,
        'all_events': all_events,
    }

@user_passes_test(lambda u: u.is_superuser)
def super_admin_dashboard(request):
    context = _get_super_admin_context(request)
    context['active_tab'] = 'overview'
    return render(request, 'gallery/super_admin.html', context)

@user_passes_test(lambda u: u.is_superuser)
def super_admin_studio_admins(request):
    context = _get_super_admin_context(request)
    context['active_tab'] = 'tenants'
    return render(request, 'gallery/super_admin.html', context)

@user_passes_test(lambda u: u.is_superuser)
def super_admin_guest_leads(request):
    context = _get_super_admin_context(request)
    context['active_tab'] = 'leads'
    return render(request, 'gallery/super_admin.html', context)

@user_passes_test(lambda u: u.is_superuser)
def super_admin_manage_plans(request):
    context = _get_super_admin_context(request)
    context['active_tab'] = 'plans'
    return render(request, 'gallery/super_admin.html', context)

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
    
    custom_storage_gb = request.POST.get('custom_storage_gb')
    custom_mb = None
    if custom_storage_gb and custom_storage_gb.strip():
        try:
            custom_mb = int(float(custom_storage_gb) * 1024)
        except ValueError:
            pass

    expire_date_str = request.POST.get('expire_date', '').strip()
    trial_days_str = request.POST.get('trial_duration_days', '').strip()
    
    now = timezone.now()
    trial_start = now
    trial_days = 7
    
    if expire_date_str:
        try:
            chosen_date = datetime.datetime.strptime(expire_date_str, '%Y-%m-%d').date()
            end_of_day = timezone.make_aware(datetime.datetime.combine(chosen_date, datetime.time(23, 59, 59)))
            if end_of_day >= now:
                trial_days = max(1, (chosen_date - now.date()).days)
                trial_start = now
            else:
                trial_days = 7
                trial_start = end_of_day - datetime.timedelta(days=7)
        except Exception:
            trial_days = 7
    elif trial_days_str:
        try:
            trial_days = max(1, int(trial_days_str))
        except (ValueError, TypeError):
            trial_days = 7
            
    UserProfile.objects.create(
        user=user,
        role=role,
        subscription_plan=plan,
        custom_storage_limit_mb=custom_mb,
        raw_password=password,
        trial_duration_days=trial_days,
        trial_start_date=trial_start
    )
    
    return JsonResponse({'success': True, 'message': f'Admin {username} created successfully with {trial_days}-day trial.'})

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
        if hasattr(user, 'profile'):
            user.profile.raw_password = password
            user.profile.save()
    user.save()
    
    custom_storage_gb = request.POST.get('custom_storage_gb')
    custom_mb = None
    if custom_storage_gb and custom_storage_gb.strip():
        try:
            custom_mb = int(float(custom_storage_gb) * 1024)
        except ValueError:
            pass

    profile = getattr(user, 'profile', None)
    if not profile:
        profile = UserProfile.objects.create(user=user, role=role or 'studio_man')

    profile.custom_storage_limit_mb = custom_mb
    if role:
        profile.role = role
    if plan_id:
        plan = SubscriptionPlan.objects.filter(id=plan_id).first()
        profile.subscription_plan = plan

    expire_date_str = request.POST.get('expire_date', '').strip()
    trial_days_str = request.POST.get('trial_duration_days', '').strip()
    
    now = timezone.now()
    if expire_date_str:
        try:
            chosen_date = datetime.datetime.strptime(expire_date_str, '%Y-%m-%d').date()
            end_of_day = timezone.make_aware(datetime.datetime.combine(chosen_date, datetime.time(23, 59, 59)))
            if end_of_day >= now:
                days_diff = max(1, (chosen_date - now.date()).days)
                profile.trial_start_date = now
                profile.trial_duration_days = days_diff
            else:
                profile.trial_start_date = end_of_day - datetime.timedelta(days=7)
                profile.trial_duration_days = 7
        except Exception:
            pass
    elif trial_days_str:
        try:
            days = max(1, int(trial_days_str))
            profile.trial_start_date = now
            profile.trial_duration_days = days
        except (ValueError, TypeError):
            pass

    profile.save()

    return JsonResponse({'success': True, 'message': f'Tenant {user.username} updated successfully.'})

@user_passes_test(lambda u: u.is_superuser)
@require_POST
def create_plan(request):
    name = request.POST.get('name')
    limit = request.POST.get('storage_limit_mb')
    is_active = request.POST.get('is_active') == 'on'
    if name and limit:
        SubscriptionPlan.objects.create(name=name, storage_limit_mb=int(limit), is_active=is_active)
        return JsonResponse({'success': True, 'message': f'Subscription Plan {name} created successfully.'})
    return JsonResponse({'success': False, 'message': 'Invalid plan details.'})

@user_passes_test(lambda u: u.is_superuser)
@require_POST
def update_plan(request, plan_id):
    plan = get_object_or_404(SubscriptionPlan, id=plan_id)
    name = request.POST.get('name', '').strip()
    limit = request.POST.get('storage_limit_mb', '').strip()
    is_active = request.POST.get('is_active') == 'on'
    if name:
        plan.name = name
    if limit:
        plan.storage_limit_mb = int(limit)
    plan.is_active = is_active
    plan.save()
    return JsonResponse({'success': True, 'message': f'Plan "{plan.name}" updated successfully.'})

@user_passes_test(lambda u: u.is_superuser)
@require_POST

@user_passes_test(lambda u: u.is_superuser)
@require_POST
def toggle_plan_status(request, plan_id):
    plan = get_object_or_404(SubscriptionPlan, id=plan_id)
    plan.is_active = not plan.is_active
    plan.save()
    status = "enabled" if plan.is_active else "disabled"
    return JsonResponse({'success': True, 'message': f'Plan "{plan.name}" has been {status}.'})

def delete_plan(request, plan_id):
    plan = SubscriptionPlan.objects.filter(id=plan_id).first()
    if plan:
        plan.delete()
        return JsonResponse({'success': True, 'message': 'Plan deleted.'})
    return JsonResponse({'success': False, 'message': 'Plan not found.'})

@user_passes_test(lambda u: u.is_superuser)
@require_POST
def edit_lead(request, lead_id):
    lead = GuestLead.objects.filter(id=lead_id).first()
    if not lead:
        return JsonResponse({'success': False, 'message': 'Lead not found.'})
    
    lead.name = request.POST.get('name', lead.name)
    lead.phone = request.POST.get('phone', lead.phone)
    lead.email = request.POST.get('email', lead.email)
    
    age = request.POST.get('age')
    if age and age.isdigit():
        lead.age = int(age)
        
    lead.save()
    return JsonResponse({'success': True, 'message': 'Lead updated successfully.'})

@user_passes_test(lambda u: u.is_superuser)
@require_POST
def delete_lead(request, lead_id):
    lead = GuestLead.objects.filter(id=lead_id).first()
    if not lead:
        return JsonResponse({'success': False, 'message': 'Lead not found.'})
    lead.delete()
    return JsonResponse({'success': True, 'message': 'Lead deleted successfully.'})


# -------------------------------------------------------------
# Tenant Event CRUD
# -------------------------------------------------------------
@login_required
@require_POST
def create_event(request):
    name = request.POST.get('name')
    description = request.POST.get('description')
    cover_image = request.FILES.get('cover_image')
    event_date = request.POST.get('event_date')
    compression_percentage = request.POST.get('compression_percentage', 100)
    try:
        compression_percentage = int(compression_percentage)
    except ValueError:
        compression_percentage = 100
        
    if not event_date:
        event_date = None
    if name:
        Event.objects.create(name=name, description=description, owner=request.user, cover_image=cover_image, event_date=event_date, compression_percentage=compression_percentage)
    return redirect('gallery:dashboard')

@login_required
@require_POST
def update_event(request, slug):
    event = get_object_or_404(Event, slug=slug, owner=request.user)
    name = request.POST.get('name')
    description = request.POST.get('description')
    cover_image = request.FILES.get('cover_image')
    event_date = request.POST.get('event_date')
    compression_percentage = request.POST.get('compression_percentage', event.compression_percentage)
    try:
        compression_percentage = int(compression_percentage)
    except ValueError:
        compression_percentage = event.compression_percentage
        
    if not event_date:
        event_date = None
    if name:
        event.name = name
        event.description = description
        event.event_date = event_date
        event.compression_percentage = compression_percentage
        if cover_image:
            event.cover_image = cover_image

        # Watermark settings
        watermark_enabled = request.POST.get('watermark_enabled')
        if watermark_enabled is not None:
            event.watermark_enabled = (watermark_enabled in ['true', 'True', '1', 'on'])
            event.watermark_type = request.POST.get('watermark_type', event.watermark_type)
            event.watermark_text = request.POST.get('watermark_text', event.watermark_text)
            event.watermark_position = request.POST.get('watermark_position', event.watermark_position)
            if request.POST.get('watermark_opacity'):
                try: event.watermark_opacity = max(10, min(100, int(request.POST.get('watermark_opacity'))))
                except (TypeError, ValueError): pass
            if request.POST.get('watermark_size'):
                try: event.watermark_size = max(10, min(60, int(request.POST.get('watermark_size'))))
                except (TypeError, ValueError): pass
            if 'watermark_image' in request.FILES:
                event.watermark_image = request.FILES['watermark_image']

        event.save()
    return redirect('gallery:event_detail', slug=event.slug)

@login_required
@require_POST
def update_event_compression(request, slug):
    from django.http import JsonResponse
    event = get_object_or_404(Event, slug=slug, owner=request.user)
    try:
        value = int(request.POST.get('compression_percentage', ''))
    except (TypeError, ValueError):
        return JsonResponse({'success': False, 'error': 'Enter a number between 1 and 100.'}, status=400)
    if not 1 <= value <= 100:
        return JsonResponse({'success': False, 'error': 'Value must be between 1 and 100.'}, status=400)
    event.compression_percentage = value
    event.save(update_fields=['compression_percentage'])
    return JsonResponse({'success': True, 'compression_percentage': value})

def _calc_wm_coords(W, H, w, h, margin, pos):
    if pos == 'top-left': return (margin, margin)
    elif pos == 'top-center': return ((W - w) // 2, margin)
    elif pos == 'top-right': return (W - w - margin, margin)
    elif pos == 'center-left': return (margin, (H - h) // 2)
    elif pos == 'center': return ((W - w) // 2, (H - h) // 2)
    elif pos == 'center-right': return (W - w - margin, (H - h) // 2)
    elif pos == 'bottom-left': return (margin, H - h - margin)
    elif pos == 'bottom-center': return ((W - w) // 2, H - h - margin)
    else: return (W - w - margin, H - h - margin) # default bottom-right

def apply_preset_and_crop_to_image(image_bytes, preset_name, preset_settings):
    """
    Non-destructively applies preset color adjustments and framing/crop to image bytes
    for user downloads, preserving the untouched original file on disk.
    """
    if not preset_name and not preset_settings:
        return image_bytes

    is_original = (not preset_name or preset_name.lower() in ('original', 'none', 'default', ''))
    crop = preset_settings.get('crop') if isinstance(preset_settings, dict) else None

    # Check if there are any crop settings to apply
    has_crop = bool(crop and (
        crop.get('cropRect') or crop.get('normCrop') or
        crop.get('rotation') or crop.get('straightenAngle') or
        crop.get('flipH') or crop.get('flipV') or
        (crop.get('aspectRatio') and crop.get('aspectRatio') != 'original') or
        (crop.get('zoom') and float(crop.get('zoom')) > 1.01)
    ))

    fine_tune = preset_settings if isinstance(preset_settings, dict) else {}
    has_fine_tune = any(fine_tune.get(k) for k in ('exposure', 'contrast', 'saturation', 'temp', 'tint', 'highlights', 'shadows', 'bw'))

    if is_original and not has_crop and not has_fine_tune:
        return image_bytes

    try:
        from io import BytesIO
        from PIL import Image, ImageOps, ImageEnhance
        import numpy as np

        img = Image.open(BytesIO(image_bytes))
        img = ImageOps.exif_transpose(img)
        if img.mode != 'RGB':
            img = img.convert('RGB')

        # 1. Apply Orientation & Crop
        if crop and isinstance(crop, dict):
            # Rotation
            rotation = int(crop.get('rotation') or 0)
            if rotation % 360 != 0:
                img = img.rotate(-rotation, expand=True)

            # Straighten
            straighten = float(crop.get('straightenAngle') or 0)
            if abs(straighten) > 0.01:
                img = img.rotate(-straighten, resample=Image.Resampling.BICUBIC, expand=False)

            # Flips
            if crop.get('flipH'):
                img = ImageOps.mirror(img)
            if crop.get('flipV'):
                img = ImageOps.flip(img)

            # Crop rectangle
            norm_crop = crop.get('normCrop')
            crop_rect = crop.get('cropRect')
            if norm_crop and isinstance(norm_crop, dict) and norm_crop.get('width', 1) < 0.999:
                w, h = img.size
                x1 = max(0, min(w - 10, int(norm_crop.get('x', 0) * w)))
                y1 = max(0, min(h - 10, int(norm_crop.get('y', 0) * h)))
                cw = max(10, min(w - x1, int(norm_crop.get('width', 1) * w)))
                ch = max(10, min(h - y1, int(norm_crop.get('height', 1) * h)))
                img = img.crop((x1, y1, x1 + cw, y1 + ch))
            elif crop_rect and isinstance(crop_rect, dict) and crop_rect.get('width', 0) > 20:
                w, h = img.size
                rx = int(crop_rect.get('x', 0))
                ry = int(crop_rect.get('y', 0))
                rw = int(crop_rect.get('width', w))
                rh = int(crop_rect.get('height', h))
                x1 = max(0, min(w - 10, rx))
                y1 = max(0, min(h - 10, ry))
                x2 = max(x1 + 10, min(w, x1 + rw))
                y2 = max(y1 + 10, min(h, y1 + rh))
                img = img.crop((x1, y1, x2, y2))
            elif crop.get('aspectRatio') and crop.get('aspectRatio') != 'original':
                aspect_map = {
                    '1:1': 1.0,
                    '4:5': 4.0 / 5.0,
                    '16:9': 16.0 / 9.0,
                    '9:16': 9.0 / 16.0,
                    '3:2': 3.0 / 2.0,
                }
                target_aspect = aspect_map.get(crop.get('aspectRatio'))
                if target_aspect:
                    w, h = img.size
                    zoom = max(1.0, float(crop.get('zoom') or 1.0))
                    norm_pan_x = float(crop.get('normPanX') or 0.0)
                    norm_pan_y = float(crop.get('normPanY') or 0.0)

                    if (w / h) >= target_aspect:
                        box_h = h / zoom
                        box_w = box_h * target_aspect
                    else:
                        box_w = w / zoom
                        box_h = box_w / target_aspect

                    cx = (w / 2.0) - (norm_pan_x * w)
                    cy = (h / 2.0) - (norm_pan_y * h)
                    x1 = max(0, min(w - box_w, cx - box_w / 2.0))
                    y1 = max(0, min(h - box_h, cy - box_h / 2.0))
                    img = img.crop((int(x1), int(y1), int(x1 + box_w), int(y1 + box_h)))

        # 2. Preset Default Baseline
        PRESET_PROFILES = {
            'bw': {'exposure': 2, 'contrast': 26, 'saturation': -100, 'bw': True},
            'warm': {'exposure': 4, 'contrast': 6, 'saturation': 25, 'temp': 20, 'tint': -4},
            'editorial': {'exposure': 3, 'contrast': 24, 'saturation': -8, 'temp': -2, 'tint': 2},
            'cinematic': {'exposure': -2, 'contrast': 18, 'saturation': 12, 'temp': -8, 'tint': 8},
            'natural': {'exposure': 4, 'contrast': 8, 'saturation': 10, 'temp': 2, 'tint': 0},
            'film': {'exposure': 5, 'contrast': 10, 'saturation': 14, 'temp': 8, 'tint': 4},
            'vintage': {'exposure': 2, 'contrast': 12, 'saturation': 25, 'temp': 25, 'tint': -8},
            'moody': {'exposure': -8, 'contrast': 30, 'saturation': -15, 'temp': 0},
        }

        active_settings = {}
        if preset_name and preset_name.lower() in PRESET_PROFILES:
            active_settings.update(PRESET_PROFILES[preset_name.lower()])

        if isinstance(preset_settings, dict):
            for k in ('exposure', 'contrast', 'saturation', 'temp', 'tint', 'highlights', 'shadows', 'bw'):
                if k in preset_settings:
                    active_settings[k] = preset_settings[k]

        # 3. Apply Color Adjustments
        is_bw = active_settings.get('bw') or active_settings.get('saturation') == -100 or (preset_name and preset_name.lower() == 'bw')
        if is_bw:
            img = ImageOps.grayscale(img).convert('RGB')

        # Brightness / Exposure
        exp = float(active_settings.get('exposure') or 0)
        if abs(exp) > 0.01:
            b_factor = max(0.2, min(2.5, 1.0 + (exp / 50.0) * 0.35))
            img = ImageEnhance.Brightness(img).enhance(b_factor)

        # Contrast
        cnt = float(active_settings.get('contrast') or 0)
        if abs(cnt) > 0.01:
            c_factor = max(0.2, min(2.5, 1.0 + (cnt / 50.0) * 0.45))
            img = ImageEnhance.Contrast(img).enhance(c_factor)

        # Saturation (if not B&W)
        if not is_bw:
            sat = float(active_settings.get('saturation') or 0)
            if abs(sat) > 0.01:
                s_factor = max(0.0, min(3.0, 1.0 + (sat / 50.0) * 0.55))
                img = ImageEnhance.Color(img).enhance(s_factor)

        # Warmth (Temp) and Tint using numpy
        temp = float(active_settings.get('temp') or 0)
        tint = float(active_settings.get('tint') or 0)
        if abs(temp) > 0.01 or abs(tint) > 0.01:
            arr = np.array(img, dtype=np.float32)
            if abs(temp) > 0.01:
                w_delta = (temp / 50.0) * 22.0
                arr[:, :, 0] += w_delta       # Red
                arr[:, :, 2] -= w_delta       # Blue
            if abs(tint) > 0.01:
                t_delta = (tint / 50.0) * 16.0
                arr[:, :, 1] -= t_delta       # Green
                arr[:, :, 0] += t_delta * 0.5 # Red
                arr[:, :, 2] += t_delta * 0.5 # Blue
            arr = np.clip(arr, 0, 255).astype(np.uint8)
            img = Image.fromarray(arr)

        out_io = BytesIO()
        img.save(out_io, format='JPEG', quality=95, optimize=False)
        return out_io.getvalue()
    except Exception as e:
        print(f"[ERROR] apply_preset_and_crop_to_image failed: {e}")
        return image_bytes


def apply_watermark_to_image(image_bytes, event):
    """
    Dynamically burns the event watermark into image bytes for guest downloads.
    Original disk files remain completely untouched and pristine.
    """
    if not event or not event.watermark_enabled:
        return image_bytes
    try:
        import os
        from io import BytesIO
        from PIL import Image, ImageDraw, ImageFont

        base_img = Image.open(BytesIO(image_bytes))
        is_rgba = (base_img.mode == 'RGBA')
        base_rgba = base_img.convert('RGBA')
        W, H = base_rgba.size

        overlay = Image.new('RGBA', (W, H), (255, 255, 255, 0))
        draw = ImageDraw.Draw(overlay)
        margin = max(16, int(min(W, H) * 0.035))

        pos = event.watermark_position or 'bottom-right'
        opacity_ratio = max(0.1, min(1.0, (event.watermark_opacity or 75) / 100.0))
        size_ratio = max(0.1, min(0.6, (event.watermark_size or 25) / 100.0))

        if event.watermark_type == 'image' and event.watermark_image:
            try:
                logo = Image.open(event.watermark_image.path).convert('RGBA')
                target_w = max(40, int(W * size_ratio))
                target_h = max(20, int(logo.height * (target_w / max(1, logo.width))))
                if target_h > int(H * 0.45):
                    target_h = int(H * 0.45)
                    target_w = max(20, int(logo.width * (target_h / max(1, logo.height))))

                logo = logo.resize((target_w, target_h), Image.Resampling.LANCZOS)

                # Apply opacity to alpha channel
                r, g, b, a = logo.split()
                a = a.point(lambda p: int(p * opacity_ratio))
                logo.putalpha(a)

                x, y = _calc_wm_coords(W, H, target_w, target_h, margin, pos)
                overlay.paste(logo, (x, y), logo)
            except Exception as e:
                print(f"Error rendering logo watermark: {e}")
        else:
            # Text Watermark
            text = (event.watermark_text or event.name or 'PHOTONA').strip()
            font_size = max(18, int(min(W, H) * 0.045 * (event.watermark_size / 25.0)))
            font = None
            try:
                font_paths = [
                    "/System/Library/Fonts/Supplemental/Arial.ttf",
                    "/System/Library/Fonts/Helvetica.ttc",
                    "/Library/Fonts/Arial.ttf",
                    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"
                ]
                for fp in font_paths:
                    if os.path.exists(fp):
                        font = ImageFont.truetype(fp, font_size)
                        break
            except Exception:
                pass
            if not font:
                font = ImageFont.load_default()

            bbox = draw.textbbox((0, 0), text, font=font)
            tw = bbox[2] - bbox[0]
            th = bbox[3] - bbox[1]

            x, y = _calc_wm_coords(W, H, tw, th, margin, pos)

            # Elegant pill background for legibility on all photo backgrounds
            pad_x = max(12, int(font_size * 0.35))
            pad_y = max(6, int(font_size * 0.2))
            badge_rect = [x - pad_x, y - pad_y, x + tw + pad_x, y + th + pad_y]
            badge_alpha = int(130 * opacity_ratio)
            draw.rounded_rectangle(badge_rect, radius=max(6, int(font_size * 0.25)), fill=(15, 23, 42, badge_alpha))

            # Drop shadow & crisp text
            text_alpha = int(255 * opacity_ratio)
            draw.text((x + 1, y + 1), text, font=font, fill=(0, 0, 0, int(text_alpha * 0.5)))
            draw.text((x, y), text, font=font, fill=(255, 255, 255, text_alpha))

        composited = Image.alpha_composite(base_rgba, overlay)
        out = BytesIO()
        if is_rgba:
            composited.save(out, format='PNG')
        else:
            composited.convert('RGB').save(out, format='JPEG', quality=92, optimize=True)
        return out.getvalue()
    except Exception as e:
        print(f"Error applying watermark: {e}")
        return image_bytes

@login_required
@require_POST
def update_event_watermark(request, slug):
    from django.http import JsonResponse
    event = get_object_or_404(Event, slug=slug, owner=request.user)

    enabled_val = request.POST.get('watermark_enabled')
    event.watermark_enabled = (enabled_val in ['true', 'True', '1', 'on'])

    wm_type = request.POST.get('watermark_type', 'text').strip()
    if wm_type in ['text', 'image']:
        event.watermark_type = wm_type

    wm_text = request.POST.get('watermark_text', '').strip()
    event.watermark_text = wm_text

    wm_pos = request.POST.get('watermark_position', 'bottom-right').strip()
    valid_positions = ['top-left', 'top-center', 'top-right', 'center-left', 'center', 'center-right', 'bottom-left', 'bottom-center', 'bottom-right']
    if wm_pos in valid_positions:
        event.watermark_position = wm_pos

    try:
        event.watermark_opacity = max(10, min(100, int(request.POST.get('watermark_opacity', 75))))
    except (TypeError, ValueError):
        pass

    try:
        event.watermark_size = max(10, min(60, int(request.POST.get('watermark_size', 25))))
    except (TypeError, ValueError):
        pass

    if 'watermark_image' in request.FILES:
        event.watermark_image = request.FILES['watermark_image']

    event.save()

    cfg = event.get_watermark_config() or {
        'enabled': False,
        'type': event.watermark_type,
        'text': event.watermark_text,
        'position': event.watermark_position,
        'opacity': event.watermark_opacity,
        'size': event.watermark_size,
        'image_url': event.watermark_image.url if event.watermark_image else None,
    }

    return JsonResponse({
        'success': True,
        'message': 'Watermark settings saved successfully!',
        'watermark': cfg,
    })

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
        
    from .clustering import get_cached_clusters_for_event
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
    
    photo_id = request.GET.get('photo_id')
    
    event = get_object_or_404(Event.objects.select_related('owner'), slug=event_slug)
    if not request.user.is_superuser and event.owner != request.user:
        return HttpResponseForbidden("Unauthorized")
        
    if photo_id:
        images = GalleryImage.objects.filter(event=event, id=photo_id)
    elif person_label:
        images = GalleryImage.objects.filter(
            event=event,
            embeddings__person_label=person_label
        ).distinct().order_by('-uploaded_at')
    else:
        images = GalleryImage.objects.filter(event=event).order_by('-uploaded_at')
        
    images = images.only('id', 'filename', 'thumbnail', 'file', 'total_faces', 'uploaded_at', 'preset_name', 'preset_settings')
    paginator = Paginator(images, 36)
    page = paginator.get_page(page_num)
    
    data = []
    evt_name = event.name or "Event"
    org_name = event.owner.username.title() if (event and event.owner) else "Studio"
    
    for img in page:
        file_token = img.get_secure_file_token()
        thumb_token = img.get_secure_thumbnail_token() if img.thumbnail else file_token
        data.append({
            'id': img.id,
            'filename': img.filename,
            'event_name': evt_name,
            'organiser_name': org_name,
            'url': reverse('gallery:secure_image', args=[thumb_token]),
            'full_url': reverse('gallery:secure_image', args=[file_token]),
            'total_faces': img.total_faces,
            'uploaded_at': img.uploaded_at.strftime("%b %d, %H:%M"),
            'preset_name': img.preset_name or 'original',
            'preset_settings': img.preset_settings or {},
        })
        
    return JsonResponse({
        'images': data,
        'has_next': page.has_next(),
        'watermark': event.get_watermark_config()
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
        limit_mb = profile.effective_storage_limit_mb if profile else 0
        
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
                
                try:
                    process_zip_upload_task.delay(temp_zip_path, event_id)
                except Exception as ce:
                    print(f"[ERROR] Celery dispatch error for zip: {ce}")
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
            try:
                process_image_upload_task.delay(temp_paths, event_id, original_filenames)
            except Exception as ce:
                print(f"[ERROR] Celery dispatch error for images: {ce}")
            
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
            limit_mb = profile.effective_storage_limit_mb if profile else 0
            file_size_mb = file.size / (1024 * 1024)
            if (profile.used_storage_mb + file_size_mb) > limit_mb:
                return JsonResponse({
                    'success': False,
                    'message': f"Storage limit reached ({limit_mb} MB limit)."
                })
            profile.used_storage_mb += file_size_mb
            profile.save()

        import os
        from io import BytesIO
        from PIL import Image, ImageOps
        from django.core.files import File

        safe_filename = os.path.basename(file.name)
        gallery_image = GalleryImage(filename=safe_filename, event=event)
        
        file.seek(0)
        raw_bytes = None
        
        # High-Speed Single-Pass Image Processing
        try:
            with Image.open(file) as img:
                img = ImageOps.exif_transpose(img)
                icc_profile = img.info.get('icc_profile')
                if img.mode in ('RGBA', 'P'):
                    img = img.convert('RGB')

                # 1. Fast in-memory thumbnail (no duplicate disk read or re-decoding)
                try:
                    thumb_img = img.copy()
                    thumb_img.thumbnail((600, 600), Image.Resampling.BILINEAR)
                    thumb_io = BytesIO()
                    thumb_img.save(thumb_io, format='WEBP', quality=80)
                    thumb_io.seek(0)
                    thumb_name = os.path.splitext(safe_filename)[0] + "_thumb.webp"
                    gallery_image.thumbnail.save(thumb_name, File(thumb_io), save=False)
                except Exception as te:
                    print(f"[DEBUG] thumbnail error: {te}")

                # 2. Fast dimension compression if event compression is set < 100%
                compression_percentage = event.compression_percentage if event else 100
                if compression_percentage and compression_percentage < 100:
                    scale_factor = float(compression_percentage) / 100.0
                    new_width = max(1, int(img.width * scale_factor))
                    new_height = max(1, int(img.height * scale_factor))
                    if new_width < img.width and new_height < img.height:
                        img = img.resize((new_width, new_height), Image.Resampling.BILINEAR)

                    compressed_io = BytesIO()
                    save_kwargs = {'format': 'JPEG', 'quality': 88}
                    if icc_profile:
                        save_kwargs['icc_profile'] = icc_profile

                    img.save(compressed_io, **save_kwargs)
                    compressed_io.seek(0)
                    raw_bytes = compressed_io.getvalue()
                    gallery_image.file.save(safe_filename, File(compressed_io), save=False)
                else:
                    file.seek(0)
                    raw_bytes = file.read()
                    file.seek(0)
                    gallery_image.file.save(safe_filename, file, save=False)
        except Exception as e:
            print(f"[DEBUG] single upload processing fallback: {e}")
            file.seek(0)
            raw_bytes = file.read()
            file.seek(0)
            gallery_image.file.save(safe_filename, file, save=False)
        gallery_image.total_faces = -1
        gallery_image.save()
        
        from .utils import enqueue_image_for_ai
        enqueue_image_for_ai(gallery_image.id, event.id)
        
        from django.urls import reverse
        file_token = gallery_image.get_secure_file_token()
        thumb_token = gallery_image.get_secure_thumbnail_token() if gallery_image.thumbnail else file_token
        return JsonResponse({
            'success': True,
            'image': {
                'id': gallery_image.id,
                'filename': gallery_image.filename,
                'event_name': event.name if event else "Event",
                'organiser_name': event.owner.username.title() if (event and event.owner) else "Studio",
                'url': request.build_absolute_uri(reverse('gallery:secure_image', args=[thumb_token])),
                'full_url': request.build_absolute_uri(reverse('gallery:secure_image', args=[file_token])),
                'total_faces': gallery_image.total_faces,
                'uploaded_at': gallery_image.uploaded_at.strftime("%b %d, %H:%M"),
                'preset_name': gallery_image.preset_name or 'original',
                'preset_settings': gallery_image.preset_settings or {},
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
        
    import threading
    from .clustering import run_clustering_on_upload
    try:
        def async_cluster(evt_id):
            try:
                from .models import Event
                e = Event.objects.filter(id=evt_id).first()
                if e:
                    run_clustering_on_upload(e)
            except Exception as ce:
                print(f"[ERROR] Background clustering error: {ce}")

        t = threading.Thread(target=async_cluster, args=(event.id,))
        t.daemon = True
        t.start()
        return JsonResponse({'success': True, 'message': 'Clustering started in background.'})
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

from .job_tracker import get_job, update_job, reset_job

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
            
    # Storage Limit Check
    profile = request.user.profile
    limit_mb = profile.effective_storage_limit_mb if profile else 0
    if profile.used_storage_mb >= limit_mb:
        return JsonResponse({'success': False, 'message': 'Storage limit exceeded. Please upgrade your plan.'})
        
    try:
        import threading
        from .utils import download_and_index_gdrive_link
        
        target_event_id = event.id
        target_event_name = event.name
        job_id = f"import_{event.id}"
        reset_job(job_id)
        
        def run_import():
            try:
                indexed, faces = download_and_index_gdrive_link(url, event_id=target_event_id, job_id=job_id)
                print(f"[SUCCESS] Google Drive import finished for '{target_event_name}': {indexed} images, {faces} faces indexed.")
                
                if indexed > 0:
                    # Run AI clustering on the newly imported faces
                    from .clustering import run_clustering_on_upload
                    re_fetched_event = Event.objects.filter(id=target_event_id).first()
                    if re_fetched_event:
                        run_clustering_on_upload(re_fetched_event)
                        
                    update_job(job_id, {
                        'active': False,
                        'percent': 100,
                        'message': f'Import finished! ({indexed} photos imported)'
                    })
                else:
                    update_job(job_id, {
                        'active': False,
                        'percent': 0,
                        'message': 'Import error: No photos could be downloaded. Please verify the link sharing is set to "Anyone with the link can view".'
                    })
                    
            except Exception as ex:
                print(f"[DEBUG] Background GDrive import thread error: {ex}")
                update_job(job_id, {
                    'active': False,
                    'percent': 0,
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
    job_id = f"import_{Event.objects.get(slug=slug).id}"
    from gallery.job_tracker import get_job
    job = get_job(job_id)
    print(f"[DEBUG] Poll {job_id} -> {job}")
    if not job:
        return JsonResponse({'active': False})
    return JsonResponse(job)

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

# ------------------------------------------------------------------
# Secure Image Streaming
# ------------------------------------------------------------------
from django.core.signing import Signer, BadSignature
from django.http import FileResponse

def secure_image_serve(request, token):
    try:
        data = Signer().unsign(token)
        image_id_str, img_type = data.split(':', 1)
        image_id = int(image_id_str)
    except (BadSignature, ValueError):
        return HttpResponse('Invalid token', status=403)
        
    img = get_object_or_404(GalleryImage, id=image_id)
    
    is_processed = (img_type in ('processed', 'processed_thumbnail')) or (request.GET.get('processed') == '1')
    
    if is_processed:
        # Determine source field
        if img_type == 'processed_thumbnail':
            has_absolute_crop_rect = bool(
                img.preset_settings and isinstance(img.preset_settings, dict) and
                img.preset_settings.get('crop') and
                img.preset_settings['crop'].get('cropRect') and
                not img.preset_settings['crop'].get('normCrop')
            )
            target_field = img.file if has_absolute_crop_rect else (img.thumbnail or img.file)
        else:
            target_field = img.file

        if not target_field or not target_field.name:
            return HttpResponse('Image not found', status=404)

        try:
            content = None
            try:
                _path = getattr(target_field, 'path', None)
                if _path and os.path.exists(_path):
                    with open(_path, 'rb') as f:
                        content = f.read()
            except (NotImplementedError, AttributeError):
                pass
            if content is None:
                with target_field.open('rb') as f:
                    content = f.read()

            if content is None:
                return HttpResponse('Failed to read image', status=500)

            # 1. Non-destructively apply preset and crop
            if img.preset_name or img.preset_settings:
                content = apply_preset_and_crop_to_image(content, img.preset_name, img.preset_settings)

            # Downscale if thumbnail requested from full-res image
            if img_type == 'processed_thumbnail':
                try:
                    from io import BytesIO
                    from PIL import Image
                    t_img = Image.open(BytesIO(content))
                    if max(t_img.size) > 600:
                        t_img.thumbnail((600, 600), Image.Resampling.LANCZOS)
                        buf = BytesIO()
                        t_img.save(buf, format='JPEG', quality=88)
                        content = buf.getvalue()
                except Exception as te:
                    print(f"Error downscaling thumbnail: {te}")

            # 2. Non-destructively apply watermark (on processed full view and downloads)
            # If processed full image or download is requested, burn watermark if enabled on event
            if img_type == 'processed' or request.GET.get('download'):
                if img.event and img.event.watermark_enabled:
                    content = apply_watermark_to_image(content, img.event)

            response = HttpResponse(content, content_type='image/jpeg')
            response['Cache-Control'] = 'public, max-age=2592000, immutable'
            if request.GET.get('download'):
                name_only, ext = os.path.splitext(img.filename)
                evt_tag = slugify(img.event.name) if (img.event and img.event.name) else (img.event.slug if img.event else 'photo')
                download_name = f"{name_only}_{evt_tag}{ext}"
                response['Content-Disposition'] = f'attachment; filename="{download_name}"'
            return response
        except Exception as e:
            print(f"Error serving processed image: {e}")
            return HttpResponse(f'Error processing image: {str(e)}', status=500)

    # Standard / raw serve (used by Preset Studio editor & raw asset endpoints)
    target_field = img.thumbnail if img_type == 'thumbnail' and img.thumbnail else img.file
    
    # On-demand thumbnail generation for images missing a pre-generated thumbnail
    if img_type in ('thumbnail', 'processed_thumbnail') and not img.thumbnail and img.file:
        try:
            from io import BytesIO
            from PIL import Image, ImageOps
            from django.core.files.base import ContentFile
            with img.file.open('rb') as src_file:
                with Image.open(src_file) as pil_img:
                    pil_img = ImageOps.exif_transpose(pil_img)
                    pil_img.thumbnail((600, 600), Image.Resampling.LANCZOS)
                    if pil_img.mode in ('RGBA', 'P'):
                        pil_img = pil_img.convert('RGB')
                    buf = BytesIO()
                    pil_img.save(buf, format='JPEG', quality=82)
                    buf.seek(0)
                    thumb_name = os.path.splitext(img.filename)[0] + "_thumb.jpg"
                    img.thumbnail.save(thumb_name, ContentFile(buf.read()), save=True)
                    target_field = img.thumbnail
        except Exception as e:
            target_field = img.file

    if not target_field or not target_field.name:
        return HttpResponse('Image not found', status=404)
        
    try:
        f = target_field.open('rb')
        response = FileResponse(f, content_type='image/jpeg')
        if img_type in ('thumbnail', 'processed_thumbnail'):
            response['Cache-Control'] = 'public, max-age=2592000, immutable'
        else:
            response['Cache-Control'] = 'public, max-age=86400'
        if request.GET.get('download'):
            response['Content-Disposition'] = f'attachment; filename="{img.filename}"'
        return response
    except Exception as e:
        return HttpResponse(f'Error accessing file: {str(e)}', status=500)

def secure_selfie_serve(request, token):
    try:
        data = Signer().unsign(token)
        lead_id_str, img_type = data.split(':', 1)
        lead_id = int(lead_id_str)
    except (BadSignature, ValueError):
        return HttpResponse('Invalid token', status=403)
        
    from .models import GuestLead
    lead = get_object_or_404(GuestLead, id=lead_id)
    
    if not lead.selfie or not lead.selfie.name:
        return HttpResponse('Selfie not found', status=404)
        
    try:
        f = lead.selfie.open('rb')
        response = FileResponse(f, content_type='image/jpeg')
        return response
    except Exception as e:
        print(f"Error serving secure image: {e}")
        return HttpResponse('Error reading image', status=500)

# -------------------------------------------------------------
# Trial Management Views
# -------------------------------------------------------------
@login_required
def trial_expired(request):
    if request.user.is_superuser or (hasattr(request.user, 'profile') and request.user.profile.is_active_trial):
        return redirect('gallery:dashboard')
    return render(request, 'gallery/trial_expired.html')

@user_passes_test(lambda u: u.is_superuser)
def extend_trial(request, user_id):
    user = get_object_or_404(User, id=user_id)
    profile = getattr(user, 'profile', None)
    now = timezone.now()
    if not profile:
        profile = UserProfile.objects.create(
            user=user,
            role='studio_man',
            trial_duration_days=7,
            trial_start_date=now
        )
    
    days = profile.trial_duration_days if profile.trial_duration_days is not None else 7
    if not profile.trial_start_date:
        profile.trial_start_date = now
        profile.trial_duration_days = 7
    else:
        current_expiry = profile.trial_start_date + datetime.timedelta(days=days)
        if current_expiry < now:
            # If already expired, grant 7 days starting from now
            profile.trial_start_date = now
            profile.trial_duration_days = 7
        else:
            # If still active, add 7 more days to current duration
            profile.trial_duration_days = days + 7
            
    profile.save()
    messages.success(request, f"Extended trial by 7 days for {user.username}.")
    
    if request.headers.get('x-requested-with') == 'XMLHttpRequest':
        new_expiry_str = profile.trial_expire_date.strftime('%d %b %Y') if profile.trial_expire_date else ''
        return JsonResponse({
            'success': True,
            'message': f"Extended trial by 7 days for {user.username}.",
            'new_expiry': new_expiry_str
        })
        
    redirect_url = request.META.get('HTTP_REFERER') or reverse('gallery:super_admin_dashboard')
    return redirect(redirect_url)

@user_passes_test(lambda u: u.is_superuser)
def toggle_paid_status(request, user_id):
    user = get_object_or_404(User, id=user_id)
    profile = getattr(user, 'profile', None)
    if not profile:
        profile = UserProfile.objects.create(user=user, role='studio_man')
        
    profile.is_paid = not profile.is_paid
    profile.save()
    status = "Paid" if profile.is_paid else "Unpaid (Trial limit applies)"
    messages.success(request, f"Updated {user.username} payment status to: {status}.")
    
    if request.headers.get('x-requested-with') == 'XMLHttpRequest':
        return JsonResponse({
            'success': True,
            'is_paid': profile.is_paid,
            'message': f"Updated {user.username} payment status to: {status}."
        })
        
    redirect_url = request.META.get('HTTP_REFERER') or reverse('gallery:super_admin_dashboard')
    return redirect(redirect_url)

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
        from django.core.cache import cache
        cache.delete(f"event_clusters_v2_{event.id}")
        
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
        from django.core.cache import cache
        cache.delete(f"event_clusters_v2_{event.id}")
        
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
                try:
                    try:
                        _path = img.file.path
                        if os.path.exists(_path):
                            with open(_path, 'rb') as f:
                                content = f.read()
                    except (NotImplementedError, AttributeError):
                        pass
                        
                    if content is None:
                        with img.file.open('rb') as f:
                            content = f.read()
                except Exception as e:
                    print(f"Error reading image from storage: {e}")
                        
                if content:
                    if event.watermark_enabled:
                        content = apply_watermark_to_image(content, event)
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
    try:
        try:
            _path = img.file.path
            if os.path.exists(_path):
                with open(_path, 'rb') as f:
                    content = f.read()
        except (NotImplementedError, AttributeError):
            pass
            
        if content is None:
            with img.file.open('rb') as f:
                content = f.read()
    except Exception as e:
        print(f"Error fetching single image file: {e}")
            
    if content is None:
        return HttpResponse("Failed to download image", status=500)

    # 1. Apply preset adjustments and crop non-destructively
    if img.preset_name or img.preset_settings:
        content = apply_preset_and_crop_to_image(content, img.preset_name, img.preset_settings)

    # 2. Apply watermark non-destructively if watermark is enabled on the event
    if img.event and img.event.watermark_enabled:
        content = apply_watermark_to_image(content, img.event)
        
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
                    try:
                        try:
                            _path = img.file.path
                            if os.path.exists(_path):
                                with open(_path, 'rb') as f:
                                    content = f.read()
                        except (NotImplementedError, AttributeError):
                            pass
                            
                        if content is None:
                            with img.file.open('rb') as f:
                                content = f.read()
                    except Exception as e:
                        print(f"Error fetching zip image file: {e}")

                    if content:
                        if img.preset_name or img.preset_settings:
                            content = apply_preset_and_crop_to_image(content, img.preset_name, img.preset_settings)
                        if img.event and img.event.watermark_enabled:
                            content = apply_watermark_to_image(content, img.event)
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

    search_only = request.POST.get('search_only') == 'true'

    link = EventShareLink.objects.create(
        event=event,
        label=label,
        person_label=person_label,
        password=hashed,
        expires_at=expires_dt,
        search_only=search_only,
    )
    return JsonResponse({
        'success': True,
        'message': f'Share link "{label}" created.',
        'id': link.id,
        'token': str(link.token),
        'label': link.label,
        'person_label': link.person_label or '',
        'is_active': link.is_active,
        'search_only': link.search_only,
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

    search_only = request.POST.get('search_only', None)
    if search_only is not None:
        link.search_only = (search_only == 'true')

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

    if link.search_only:
        return redirect('gallery:public_search', token=link.token)

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
        
        # 1. Save the Guest Lead
        name = request.POST.get('guest_name')
        age = request.POST.get('guest_age')
        phone = request.POST.get('guest_phone')
        email = request.POST.get('guest_email', '')
        
        if name and age and phone:
            GuestLead.objects.create(
                event=event,
                name=name,
                age=age,
                phone=phone,
                email=email,
                selfie=selfie
            )
            # Reset file pointer after saving to DB so the chunk reading below works correctly
            selfie.seek(0)

        # 2. Process FAISS Search via temp file
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
    try:
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
            file_token = img.get_secure_file_token()
            processed_token = img.get_secure_processed_token()
            thumb_token = img.get_secure_thumbnail_token() if img.thumbnail else file_token
            data.append({
                'id': img.id,
                'url': request.build_absolute_uri(reverse('gallery:secure_image', args=[thumb_token])),
                'full_url': request.build_absolute_uri(reverse('gallery:secure_image', args=[file_token])),
                'processed_url': request.build_absolute_uri(reverse('gallery:secure_image', args=[processed_token])),
                'filename': img.filename,
                'preset_name': img.preset_name or 'original',
                'preset_settings': img.preset_settings or {},
                'uploaded_at': img.uploaded_at.strftime('%b %d, %H:%M') if img.uploaded_at else '',
            })
        return JsonResponse({
            'images': data,
            'has_next': page_obj.has_next(),
            'watermark': link.event.get_watermark_config(),
        })
    except Exception as e:
        import traceback
        return JsonResponse({'error': str(e), 'traceback': traceback.format_exc()}, status=500)


@login_required
@require_POST
def apply_preset_api(request):
    """
    Non-Destructive Preset & Fine-Tune Settings API.
    Does NOT modify the physical original photo file on disk.
    Instead, saves the preset name and fine-tuning adjustment settings as metadata.
    This guarantees that the original camera photo remains 100% untouched and pristine.
    Users can revert back to 'original' at any time with zero loss of quality or data.
    """
    import json
    try:
        photo_id = request.POST.get('photo_id')
        event_slug = request.POST.get('event_slug')
        preset_name = request.POST.get('preset_name', 'original').strip()
        preset_settings_raw = request.POST.get('preset_settings', '{}')
        apply_all = request.POST.get('apply_all') == 'true'

        try:
            preset_settings = json.loads(preset_settings_raw) if isinstance(preset_settings_raw, str) else (preset_settings_raw or {})
        except Exception:
            preset_settings = {}

        event = None
        if event_slug:
            event = Event.objects.filter(slug=event_slug).first()

        # Normalise original
        is_original = preset_name.lower() in ['original', 'none', 'default', '']
        if is_original:
            preset_name = 'original'
            if not isinstance(preset_settings, dict):
                preset_settings = {}

        if apply_all and event:
            if not request.user.is_superuser and event.owner != request.user:
                return HttpResponseForbidden("Unauthorized")

            # Presets applied across all photos should only include color/tone fine-tuning, NEVER individual crops
            global_settings = {k: v for k, v in preset_settings.items() if k != 'crop'} if isinstance(preset_settings, dict) else {}

            # Save default preset to the event settings
            event.default_preset = preset_name
            event.default_preset_settings = global_settings
            event.save(update_fields=['default_preset', 'default_preset_settings'])

            # Update all images in event (non-destructive) preserving individual crops if they already have one
            images = event.images.all()
            count = images.count()
            for img in images:
                img_crop = (img.preset_settings or {}).get('crop')
                new_settings = {**global_settings}
                if img_crop and not is_original:
                    new_settings['crop'] = img_crop
                img.preset_name = preset_name
                img.preset_settings = new_settings
                img.save(update_fields=['preset_name', 'preset_settings'])

            msg = f'All {count} photos reverted to original camera files.' if (is_original and not global_settings) else f'Preset "{preset_name}" saved across all {count} photos (Non-destructive: original files preserved).'

            return JsonResponse({
                'success': True,
                'count': count,
                'preset_name': preset_name,
                'preset_settings': global_settings,
                'message': msg,
            })

        elif photo_id:
            img = get_object_or_404(GalleryImage, id=photo_id)
            if not request.user.is_superuser and (img.event and img.event.owner != request.user):
                return HttpResponseForbidden("Unauthorized")

            # Save preset settings onto the image (non-destructive)
            img.preset_name = preset_name
            img.preset_settings = preset_settings
            img.save(update_fields=['preset_name', 'preset_settings'])

            file_tok = img.get_secure_file_token()
            thumb_tok = img.get_secure_thumbnail_token()

            has_crop = bool(preset_settings and preset_settings.get('crop', {}).get('cropRect'))
            if is_original and not preset_settings:
                msg = "Restored to original camera photo (untouched)."
            elif has_crop and is_original:
                msg = "Crop & framing saved successfully!"
            elif has_crop:
                msg = f'Preset "{preset_name}" and crop framing saved! (Original camera file preserved).'
            else:
                msg = f'Preset "{preset_name}" saved! (Original camera file preserved).'

            return JsonResponse({
                'success': True,
                'count': 1,
                'photo_id': img.id,
                'preset_name': preset_name,
                'preset_settings': preset_settings,
                'new_url': request.build_absolute_uri(reverse('gallery:secure_image', args=[thumb_tok])),
                'new_full_url': request.build_absolute_uri(reverse('gallery:secure_image', args=[file_tok])),
                'message': msg
            })
        else:
            return JsonResponse({'success': False, 'message': 'Missing photo_id or event_slug.'}, status=400)

    except Exception as ex:
        import traceback
        return JsonResponse({'success': False, 'error': str(ex), 'traceback': traceback.format_exc()}, status=500)

