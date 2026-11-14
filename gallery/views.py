import os
import uuid
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
from .models import GalleryImage, FaceEmbedding, Event, UserProfile, SubscriptionPlan, EventShareLink, GuestLead, PersonalGalleryLink, VisitorSession, PageViewEvent, PhotoInteraction
from .utils import process_gallery_image, search_person_by_selfie, normalize_selfie_file
from .analytics import format_duration_display
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
        'profile': profile,
        'plan': plan,
        'used_storage': used_storage,
        'upload_percentage': upload_percentage,
        'total_events': request.user.events.count(),
    }
    return render(request, 'gallery/plans.html', context)

# -------------------------------------------------------------
# Super Admin Views
# -------------------------------------------------------------
def _get_super_admin_context(request):
    admins = User.objects.filter(is_superuser=False).select_related('profile')
    plans = SubscriptionPlan.objects.all().order_by('id')
    roles = UserProfile.ROLE_CHOICES
    
    total_tenants = admins.count()
    total_plans = plans.count()
    total_storage_used = 0
    for admin in admins:
        try:
            total_storage_used += admin.profile.used_storage_mb
        except Exception:
            pass
    
    leads = GuestLead.objects.all().select_related('event', 'event__owner', 'personal_gallery_link').prefetch_related('personal_links__images', 'personal_gallery_link__images').order_by('-created_at')
    
    search_q = request.GET.get('search_q')
    organizer_id = request.GET.get('organizer_id')
    event_id = request.GET.get('event_id')
    start_date = request.GET.get('start_date')
    end_date = request.GET.get('end_date')
    
    selected_event = None
    selected_event_total_leads = 0
    if event_id and str(event_id).strip():
        selected_event = Event.objects.filter(id=event_id).first()
        if selected_event:
            selected_event_total_leads = GuestLead.objects.filter(event_id=event_id).count()

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
        
    from django.db.models import Count
    all_events = Event.objects.all().select_related('owner').annotate(leads_count=Count('leads')).order_by('-created_at')

    from django.urls import reverse
    leads_list = list(leads)
    for lead in leads_list:
        pl = getattr(lead, 'personal_gallery_link', None) or lead.personal_links.first()
        if not pl:
            pl = lead.event.personal_gallery_links.filter(searcher_name=lead.name).first()
        if not pl and lead.phone:
            sibling = GuestLead.objects.filter(event=lead.event, phone=lead.phone).exclude(id=lead.id).filter(personal_gallery_link__isnull=False).first()
            if sibling and sibling.personal_gallery_link:
                pl = sibling.personal_gallery_link
                try:
                    lead.personal_gallery_link = pl
                    lead.save(update_fields=['personal_gallery_link'])
                except Exception:
                    pass
            else:
                sibling_pl = PersonalGalleryLink.objects.filter(event=lead.event, guest_lead__phone=lead.phone).first()
                if sibling_pl:
                    pl = sibling_pl
                    try:
                        lead.personal_gallery_link = pl
                        lead.save(update_fields=['personal_gallery_link'])
                    except Exception:
                        pass
        if pl:
            try:
                lead.personal_gallery_url = request.build_absolute_uri(
                    reverse('gallery:personal_gallery', args=[pl.token])
                )
            except Exception:
                lead.personal_gallery_url = reverse('gallery:personal_gallery', args=[pl.token])
            lead.matched_photos_count = len(pl.images.all())
            lead.personal_link_obj = pl
            lead.is_link_active = getattr(pl, 'is_active', True)
        else:
            lead.personal_gallery_url = None
            lead.matched_photos_count = 0
            lead.personal_link_obj = None
            lead.is_link_active = False
    
    return {
        'admins': admins,
        'plans': plans,
        'roles': roles,
        'total_tenants': total_tenants,
        'total_plans': total_plans,
        'total_storage_used': total_storage_used,
        'leads': leads_list,
        'all_events': all_events,
        'selected_event': selected_event,
        'selected_event_total_leads': selected_event_total_leads,
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

    custom_album_limit = request.POST.get('custom_album_limit')
    custom_albums = None
    if custom_album_limit and custom_album_limit.strip():
        try:
            custom_albums = max(0, int(custom_album_limit.strip()))
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
        custom_album_limit=custom_albums,
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

    custom_album_limit = request.POST.get('custom_album_limit')
    
    profile = getattr(user, 'profile', None)
    if not profile:
        profile = UserProfile.objects.create(user=user, role=role or 'studio_man')

    profile.custom_storage_limit_mb = custom_mb
    if custom_album_limit is not None:
        if custom_album_limit.strip():
            try:
                profile.custom_album_limit = max(0, int(custom_album_limit.strip()))
            except ValueError:
                pass
        else:
            profile.custom_album_limit = None

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
    name = request.POST.get('name', '').strip()
    limit = request.POST.get('storage_limit_mb', '').strip()
    album_limit = request.POST.get('album_limit', 0)
    is_active = request.POST.get('is_active') == 'on' or request.POST.get('is_active') == 'true' or 'is_active' not in request.POST
    
    try:
        album_limit_val = max(0, int(album_limit)) if album_limit is not None and str(album_limit).strip() != '' else 0
    except (ValueError, TypeError):
        album_limit_val = 0

    if name and limit:
        SubscriptionPlan.objects.create(
            name=name,
            storage_limit_mb=int(limit),
            album_limit=album_limit_val,
            is_active=is_active
        )
        return JsonResponse({'success': True, 'message': f'Subscription Plan "{name}" created successfully.'})
    return JsonResponse({'success': False, 'message': 'Invalid plan details.'})

@user_passes_test(lambda u: u.is_superuser)
@require_POST
def update_plan(request, plan_id):
    plan = get_object_or_404(SubscriptionPlan, id=plan_id)
    name = request.POST.get('name', '').strip()
    limit = request.POST.get('storage_limit_mb', '').strip()
    album_limit = request.POST.get('album_limit')
    is_active = request.POST.get('is_active') == 'on' or request.POST.get('is_active') == 'true'
    
    if name:
        plan.name = name
    if limit:
        plan.storage_limit_mb = int(limit)
    if album_limit is not None and str(album_limit).strip() != '':
        try:
            plan.album_limit = max(0, int(album_limit))
        except (ValueError, TypeError):
            pass
    if 'is_active' in request.POST:
        plan.is_active = is_active
    plan.save()
    return JsonResponse({'success': True, 'message': f'Plan "{plan.name}" updated successfully.'})

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
    # Remove associated personal gallery links so this person immediately loses all gallery access
    PersonalGalleryLink.objects.filter(guest_lead=lead).delete()
    if lead.personal_gallery_link:
        pl = lead.personal_gallery_link
        if not GuestLead.objects.filter(personal_gallery_link=pl).exclude(id=lead.id).exists():
            pl.delete()
    PersonalGalleryLink.objects.filter(event=lead.event, searcher_name=lead.name).delete()
    lead.delete()
    return JsonResponse({'success': True, 'message': 'Lead and associated gallery link deleted successfully.'})

@user_passes_test(lambda u: u.is_superuser)
@require_POST
def toggle_lead_link_status(request, lead_id):
    lead = GuestLead.objects.filter(id=lead_id).first()
    if not lead:
        return JsonResponse({'success': False, 'message': 'Lead not found.'})

    is_active_str = request.POST.get('is_active')
    new_is_active = is_active_str.lower() in ['true', '1', 'yes'] if is_active_str is not None else True

    links = PersonalGalleryLink.objects.filter(guest_lead=lead)
    if not links.exists() and lead.personal_gallery_link:
        links = PersonalGalleryLink.objects.filter(id=lead.personal_gallery_link_id)
    if not links.exists():
        links = PersonalGalleryLink.objects.filter(event=lead.event, searcher_name=lead.name)

    if not links.exists():
        return JsonResponse({'success': False, 'message': 'No personal gallery link found for this lead.'})

    links.update(is_active=new_is_active)
    status_text = "activated" if new_is_active else "deactivated"
    return JsonResponse({
        'success': True,
        'is_active': new_is_active,
        'message': f"Personal gallery link for {lead.name} has been {status_text}."
    })


# -------------------------------------------------------------
# Tenant Event CRUD
# -------------------------------------------------------------
@login_required
@require_POST
def create_event(request):
    if not request.user.is_superuser:
        profile = getattr(request.user, 'profile', None)
        if profile and profile.has_reached_album_limit:
            messages.error(
                request,
                f"Album limit reached ({profile.total_albums_count}/{profile.effective_album_limit}). "
                f"Please upgrade your subscription plan to create more albums."
            )
            return redirect('gallery:dashboard')

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
        messages.success(request, f"Album '{name}' created successfully.")
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

def preset_has_edits(preset_name, preset_settings):
    """True if apply_preset_and_crop_to_image would actually modify the image."""
    if not preset_name and not preset_settings:
        return False
    is_original = (not preset_name or preset_name.lower() in ('original', 'none', 'default', ''))
    crop = preset_settings.get('crop') if isinstance(preset_settings, dict) else None
    has_crop = bool(crop and (
        crop.get('cropRect') or crop.get('normCrop') or
        crop.get('rotation') or crop.get('straightenAngle') or
        crop.get('flipH') or crop.get('flipV') or
        (crop.get('aspectRatio') and crop.get('aspectRatio') != 'original') or
        (crop.get('zoom') and float(crop.get('zoom')) > 1.01)
    ))
    fine_tune = preset_settings if isinstance(preset_settings, dict) else {}
    has_fine_tune = any(fine_tune.get(k) for k in ('exposure', 'contrast', 'saturation', 'temp', 'tint', 'highlights', 'shadows', 'bw'))
    return not (is_original and not has_crop and not has_fine_tune)

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
    total_images = GalleryImage.objects.filter(event=event).count()
        
    return render(request, 'gallery/event_detail.html', {
        'event': event,
        'clusters': clusters,
        'total_images': total_images,
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
        thumb_token = img.get_secure_thumbnail_token()
        data.append({
            'id': img.id,
            'filename': img.filename,
            'event_name': evt_name,
            'organiser_name': org_name,
            'url': reverse('gallery:secure_image', args=[thumb_token]),
            'full_url': reverse('gallery:secure_image', args=[file_token]),
            'total_faces': img.total_faces,
            'uploaded_at': timezone.localtime(img.uploaded_at).strftime("%b %d, %I:%M %p"),
            'preset_name': img.preset_name or 'original',
            'preset_settings': img.preset_settings or {},
        })
        
    return JsonResponse({
        'images': data,
        'has_next': page.has_next(),
        'total_count': paginator.count,
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
                    for chunk in file.chunks(chunk_size=2 * 1024 * 1024):
                        f.write(chunk)
                
                from .job_tracker import reset_job
                job_id = f"zip_{event_id}"
                reset_job(job_id)

                dispatched = False
                try:
                    process_zip_upload_task.delay(temp_zip_path, event_id, job_id)
                    dispatched = True
                except Exception as ce:
                    print(f"[ERROR] Celery dispatch error for zip: {ce}")

                if not dispatched:
                    import threading
                    t = threading.Thread(target=process_zip_upload_task, args=(temp_zip_path, event_id, job_id))
                    t.daemon = True
                    t.start()
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
            'job_id': f"zip_{event_id}" if has_zip else None,
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
        safe_filename = os.path.basename(file.name)
        gallery_image = GalleryImage(filename=safe_filename, event=event, total_faces=-1)
        
        # Fast Stream Save directly to storage (S3 / disk) without synchronous image decoding
        file.seek(0)
        gallery_image.file.save(safe_filename, file, save=False)

        # Pre-generate lightweight WebP thumbnail from uploaded file stream so Celery never needs to re-download 20MB raw file from S3
        file.seek(0)
        try:
            from .tasks import create_thumbnail
            from django.core.files import File
            thumb_io, ext = create_thumbnail(file)
            if thumb_io:
                thumb_name = os.path.splitext(safe_filename)[0] + "_thumb" + ext
                gallery_image.thumbnail.save(thumb_name, File(thumb_io), save=False)
        except Exception as te:
            print(f"[Upload] Thumbnail pre-generation error: {te}")

        gallery_image.save()
        
        # Enqueue background thumbnail generation, InsightFace AI face detection, and auto-sync
        from .utils import enqueue_image_for_ai
        enqueue_image_for_ai(gallery_image.id, event.id)
        
        from django.urls import reverse
        file_token = gallery_image.get_secure_file_token()
        thumb_token = gallery_image.get_secure_thumbnail_token()
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
                'uploaded_at': timezone.localtime(gallery_image.uploaded_at).strftime("%b %d, %I:%M %p"),
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
        
    try:
        from .tasks import trigger_event_fair_indexing
        from .clustering import run_clustering_on_upload

        # If event has pending unindexed images, kick off fair round-robin indexing!
        if event.images.filter(total_faces=-1).exists():
            trigger_event_fair_indexing(event.id)
            return JsonResponse({'success': True, 'message': 'AI face indexing active.'})

        # Otherwise trigger clustering directly
        import threading
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
    personal_link = None
    personal_gallery_url = None
    search_image_b64 = None
    whatsapp_share_text = None
    whatsapp_sent = False
    phone_entered = None
    
    if not selected_event_slug:
        return redirect('gallery:dashboard')

    if request.user.is_superuser:
        selected_event = get_object_or_404(Event, slug=selected_event_slug)
    else:
        selected_event = get_object_or_404(Event, slug=selected_event_slug, owner=request.user)

    if request.method == 'POST' and request.FILES.get('selfie'):
        selfie = request.FILES['selfie']
        import time
        import base64
        temp_dir = os.path.join(settings.MEDIA_ROOT, 'temp')
        os.makedirs(temp_dir, exist_ok=True)
        safe_name = selfie.name.replace(' ', '_')
        full_temp_path = os.path.join(temp_dir, f"search_{uuid.uuid4().hex}.jpg")
        with open(full_temp_path, 'wb') as f:
            for chunk in selfie.chunks():
                f.write(chunk)
        
        try:
            normalize_selfie_file(full_temp_path)
            with open(full_temp_path, 'rb') as img_f:
                search_image_b64 = base64.b64encode(img_f.read()).decode('utf-8')

            from .utils import find_or_create_personal_gallery_link
            results, query_vector = search_person_by_selfie(
                full_temp_path,
                event_id=selected_event.id,
                tenant_user=request.user,
                return_vector=True
            )
            if not request.user.is_superuser:
                results = [res for res in results if res['image'].event and res['image'].event.owner == request.user]
            for res in results:
                name_only, ext = os.path.splitext(res['image'].filename)
                res['download_filename'] = f"{name_only}_{selected_event.slug}{ext}"

            if results or query_vector:
                personal_link, is_new = find_or_create_personal_gallery_link(
                    event=selected_event,
                    query_vector=query_vector,
                    results=results or [],
                    searcher_name=request.user.get_full_name() or request.user.username,
                    selfie_path=full_temp_path,
                    selfie_thumbnail_b64=search_image_b64 or '',
                    expires_at=selected_event.expires_at
                )

                personal_gallery_url = request.build_absolute_uri(
                    reverse('gallery:personal_gallery', args=[personal_link.token])
                )
                from urllib.parse import quote
                raw_wa = f"Here are my photos from {selected_event.name}: {personal_gallery_url}"
                whatsapp_share_text = quote(raw_wa)

                phone = request.POST.get('phone') or request.POST.get('guest_phone')
                if phone:
                    phone_entered = phone
                    from .models import GuestLead
                    user_lead = GuestLead.objects.filter(event=selected_event, phone=phone).first()
                    if not user_lead:
                        user_lead = GuestLead.objects.create(
                            event=selected_event,
                            name=request.user.get_full_name() or request.user.username,
                            phone=phone,
                            age=25
                        )
                    if user_lead and personal_link:
                        user_lead.personal_gallery_link = personal_link
                        try:
                            user_lead.save(update_fields=['personal_gallery_link'])
                        except Exception:
                            pass
                    telemetry_session_id = request.POST.get('telemetry_session_id')
                    if telemetry_session_id:
                        VisitorSession.objects.filter(session_id=telemetry_session_id).update(guest_lead=user_lead)
                    from .whatsapp import send_whatsapp_selfie_link_async
                    send_whatsapp_selfie_link_async(
                        phone=phone,
                        name=request.user.get_full_name() or request.user.username,
                        link_url=personal_gallery_url
                    )
                    whatsapp_sent = True
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
        'personal_link': personal_link,
        'personal_gallery_url': personal_gallery_url,
        'search_image_b64': search_image_b64,
        'whatsapp_share_text': whatsapp_share_text,
        'whatsapp_sent': whatsapp_sent,
        'whatsapp_enabled': getattr(settings, 'ENABLE_WHATSAPP_LINK_SEND', False),
        'phone': phone_entered,
    })

@login_required
@require_POST
def delete_image(request, image_id):
    gallery_image = get_object_or_404(GalleryImage, id=image_id)
    if not request.user.is_superuser and gallery_image.event.owner != request.user:
        return HttpResponseForbidden("Unauthorized")
        
    event = gallery_image.event
    owner = event.owner if event else None

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

    if event:
        from django.core.cache import cache
        cache.delete(f"event_clusters_v2_{event.id}")
        if not event.images.exists():
            import shutil
            safe_event_name = "".join([c if c.isalnum() else "_" for c in event.name])
            sorted_base_dir = os.path.join(settings.MEDIA_ROOT, 'sorted', f"{safe_event_name}_{event.id}")
            if os.path.exists(sorted_base_dir):
                shutil.rmtree(sorted_base_dir, ignore_errors=True)

    if owner:
        recalculate_user_storage(owner)
    return JsonResponse({'success': True, 'message': 'Image deleted successfully.'})

@login_required
def event_photo_ids(request, slug):
    if request.user.is_superuser:
        event = get_object_or_404(Event, slug=slug)
    else:
        event = get_object_or_404(Event, slug=slug, owner=request.user)

    person_label = request.GET.get('person_label', '').strip() or None
    if person_label:
        ids = list(GalleryImage.objects.filter(event=event, embeddings__person_label=person_label).distinct().values_list('id', flat=True))
    else:
        ids = list(GalleryImage.objects.filter(event=event).values_list('id', flat=True))

    return JsonResponse({'ids': ids, 'total': len(ids)})

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

        int_ids = []
        for x in image_ids:
            try:
                int_ids.append(int(x))
            except (ValueError, TypeError):
                pass

        if not int_ids:
            return JsonResponse({'success': False, 'message': 'No valid image IDs provided.'})

        # Fast bulk lookup (1 query)
        images_to_delete = list(GalleryImage.objects.filter(id__in=int_ids).select_related('event', 'event__owner'))
        if not images_to_delete:
            return JsonResponse({'success': True, 'deleted_ids': [], 'message': 'No photos found to delete.'})

        deleted_ids = []
        affected_events = set()
        owners = set()
        files_to_remove = []

        for img in images_to_delete:
            if not request.user.is_superuser and img.event and img.event.owner != request.user:
                continue

            if img.event:
                affected_events.add(img.event)
                if img.event.owner:
                    owners.add(img.event.owner)

            if img.file:
                try:
                    files_to_remove.append(img.file.path)
                except Exception:
                    pass
            if img.thumbnail:
                try:
                    files_to_remove.append(img.thumbnail.path)
                except Exception:
                    pass

            deleted_ids.append(img.id)

        # Fast atomic bulk delete from database (1 query)
        if deleted_ids:
            GalleryImage.objects.filter(id__in=deleted_ids).delete()

        # Delete physical files on disk
        for fpath in files_to_remove:
            try:
                if os.path.exists(fpath):
                    os.remove(fpath)
            except Exception:
                pass

        # Invalidate cluster cache and clean empty sorted folders for affected events
        from django.core.cache import cache
        import shutil
        for event in affected_events:
            cache.delete(f"event_clusters_v2_{event.id}")
            if not event.images.exists():
                safe_event_name = "".join([c if c.isalnum() else "_" for c in event.name])
                sorted_base_dir = os.path.join(settings.MEDIA_ROOT, 'sorted', f"{safe_event_name}_{event.id}")
                if os.path.exists(sorted_base_dir):
                    shutil.rmtree(sorted_base_dir, ignore_errors=True)

        for owner in owners:
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
                    update_job(job_id, {
                        'current': indexed,
                        'percent': 99,
                        'message': f'⚡ All {indexed} photos downloaded! Finalizing AI face albums...'
                    })
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
def zip_extract_status(request, slug):
    if request.user.is_superuser:
        event = get_object_or_404(Event, slug=slug)
    else:
        event = get_object_or_404(Event, slug=slug, owner=request.user)
    job_id = f"zip_{event.id}"
    from gallery.job_tracker import get_job
    job = get_job(job_id)
    if not job:
        return JsonResponse({'active': False})
    return JsonResponse(job)

@login_required
def event_ai_status(request, slug):
    """Returns live status of background AI face detection & indexing for an event."""
    if request.user.is_superuser:
        event = get_object_or_404(Event, slug=slug)
    else:
        event = get_object_or_404(Event, slug=slug, owner=request.user)

    total = event.images.count()
    from .job_tracker import get_job
    import_job = get_job(f"import_{event.id}")
    zip_job = get_job(f"zip_{event.id}")

    is_import_active = bool(import_job and import_job.get('active'))
    is_zip_active = bool(zip_job and zip_job.get('active'))
    is_importing = is_import_active or is_zip_active

    pending = event.images.filter(total_faces=-1).count()
    processed = max(0, total - pending)

    # Calculate real overall expected count when importing or unzipping
    expected_total = total
    if is_import_active and import_job.get('total', 0) > 0:
        expected_total = max(total, import_job.get('total', 0))
    elif is_zip_active and zip_job.get('total', 0) > 0:
        expected_total = max(total, zip_job.get('total', 0))

    if expected_total == 0 and not is_importing:
        return JsonResponse({
            'total': 0,
            'processed': 0,
            'pending': 0,
            'percent': 100,
            'is_indexing': False,
            'is_importing': False,
            'clusters_count': 0
        })

    is_indexing = is_importing or (pending > 0)
    percent = int((processed / expected_total) * 100) if expected_total > 0 else 100
    if is_indexing and percent >= 100:
        percent = 99

    from .clustering import get_cached_clusters_for_event
    clusters = get_cached_clusters_for_event(event)
    clusters_count = len(clusters) if clusters else 0

    return JsonResponse({
        'total': expected_total,
        'processed': processed,
        'pending': pending + max(0, expected_total - total),
        'percent': percent,
        'is_indexing': is_indexing,
        'is_importing': is_importing,
        'clusters_count': clusters_count
    })

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
from .media_cache import rendered_cache_path, write_atomic, serve_local_file, local_path_of, serve_storage_field

def secure_image_serve(request, token):
    try:
        data = Signer().unsign(token)
        image_id_str, img_type = data.split(':', 1)
        image_id = int(image_id_str)
    except (BadSignature, ValueError):
        return HttpResponse('Invalid token', status=403)
        
    img = get_object_or_404(GalleryImage.objects.select_related('event'), id=image_id)
    
    is_processed = (img_type in ('processed', 'processed_thumbnail')) or (request.GET.get('processed') == '1')
    is_download = bool(request.GET.get('download'))
    
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

        # Watermark is burned on processed full view and downloads
        apply_wm = (img_type == 'processed' or is_download) and bool(img.event and img.event.watermark_enabled)
        wm_config = img.event.get_watermark_config() if apply_wm else None
        variant = img_type if img_type in ('processed', 'processed_thumbnail') else f"{img_type}_processed"

        download_name = None
        if is_download:
            name_only, ext = os.path.splitext(img.filename)
            evt_tag = slugify(img.event.name) if (img.event and img.event.name) else (img.event.slug if img.event else 'photo')
            download_name = f"{name_only}_{evt_tag}{ext}"

            # Server-side fallback: record photo download interaction
            try:
                sid = request.COOKIES.get('photona_sid')
                sess = VisitorSession.objects.filter(session_id=sid).first() if sid else None
                PhotoInteraction.objects.create(
                    image=img,
                    event=img.event,
                    session=sess,
                    action='download',
                    dwell_seconds=1
                )
            except Exception:
                pass

        cache_control = 'public, max-age=2592000, immutable'

        # Fast path: unedited photo without watermark → stored file IS the output.
        # Direct 302 redirect straight to S3 (or local nginx) frees Gunicorn in 1ms!
        has_edits = preset_has_edits(img.preset_name, img.preset_settings)
        if not has_edits:
            # 1. Thumbnail view: serve thumbnail if available, or original file directly
            if img_type == 'processed_thumbnail':
                thumb_target = img.thumbnail if img.thumbnail else img.file
                return serve_storage_field(thumb_target, cache_control, download_name)

            # 2. Lightbox / full-screen view (non-download): watermark is rendered via CSS overlay
            if not is_download:
                return serve_storage_field(img.file, cache_control, download_name)

            # 3. Direct download when watermark is disabled: S3 presigned attachment redirect
            if not apply_wm:
                return serve_storage_field(img.file, cache_control, download_name)

        # Serve the cached render when available (no PIL work, no duplicate read)
        cached_path = rendered_cache_path(img, variant, wm_config)
        if os.path.exists(cached_path):
            return serve_local_file(cached_path, cache_control, 'image/jpeg', download_name)

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
                        if t_img.mode not in ('RGB', 'L'):
                            t_img = t_img.convert('RGB')
                        buf = BytesIO()
                        t_img.save(buf, format='JPEG', quality=88)
                        content = buf.getvalue()
                except Exception as te:
                    print(f"Error downscaling thumbnail: {te}")

            # 2. Non-destructively apply watermark (on processed full view and downloads)
            if apply_wm:
                content = apply_watermark_to_image(content, img.event)

            # 3. Store the render so subsequent requests are served from disk / nginx
            try:
                write_atomic(cached_path, content)
                return serve_local_file(cached_path, cache_control, 'image/jpeg', download_name)
            except Exception as ce:
                print(f"Render cache write failed for image {img.id}: {ce}")

            response = HttpResponse(content, content_type='image/jpeg')
            response['Cache-Control'] = cache_control
            if download_name:
                response['Content-Disposition'] = f'attachment; filename="{download_name}"'
            return response
        except Exception as e:
            print(f"Error serving processed image: {e}")
            return HttpResponse('Error processing image', status=500)

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
                    try:
                        pil_img.draft('RGB', (1200, 1200))
                    except Exception:
                        pass
                    pil_img = ImageOps.exif_transpose(pil_img)
                    pil_img.thumbnail((600, 600), Image.Resampling.BILINEAR)
                    if pil_img.mode not in ('RGB', 'L'):
                        pil_img = pil_img.convert('RGB')
                    buf = BytesIO()
                    pil_img.save(buf, format='WEBP', quality=80)
                    buf.seek(0)
                    thumb_name = os.path.splitext(img.filename)[0] + "_thumb.webp"
                    img.thumbnail.save(thumb_name, ContentFile(buf.read()), save=True)
                    target_field = img.thumbnail
        except Exception as e:
            print(f"[Thumbnail] On-demand fallback failed for img {img.id}: {e}")
            target_field = img.file

    if not target_field or not target_field.name:
        return HttpResponse('Image not found', status=404)

    if img_type in ('thumbnail', 'processed_thumbnail'):
        cache_control = 'public, max-age=2592000, immutable'
    else:
        cache_control = 'public, max-age=86400'
    download_name = img.filename if is_download else None

    try:
        return serve_storage_field(target_field, cache_control, download_name)
    except Exception as e:
        return HttpResponse('Error accessing file', status=500)

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
        full_temp_path = os.path.join(temp_dir, f"group_{uuid.uuid4().hex}.jpg")
        with open(full_temp_path, 'wb') as f:
            for chunk in selfie.chunks():
                f.write(chunk)
        
        try:
            normalize_selfie_file(full_temp_path)
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
                    else:
                        p_link = PersonalGalleryLink.objects.filter(token=token, event=img.event).first()
                        if p_link and p_link.is_accessible and p_link.images.filter(id=img.id).exists():
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
                        try:
                            sid = request.COOKIES.get('photona_sid')
                            sess = VisitorSession.objects.filter(session_id=sid).first() if sid else None
                            PhotoInteraction.objects.create(
                                image=img,
                                event=img.event,
                                session=sess,
                                action='download',
                                dwell_seconds=1
                            )
                        except Exception:
                            pass
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
            'search_only': lnk.search_only,
            'allow_upload': getattr(lnk, 'allow_upload', True),
            'allow_back_camera': getattr(lnk, 'allow_back_camera', True),
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
    allow_upload = request.POST.get('allow_upload', 'true') != 'false'
    allow_back_camera = request.POST.get('allow_back_camera', 'true') != 'false'

    link = EventShareLink.objects.create(
        event=event,
        label=label,
        person_label=person_label,
        password=hashed,
        expires_at=expires_dt,
        search_only=search_only,
        allow_upload=allow_upload,
        allow_back_camera=allow_back_camera,
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
        'allow_upload': link.allow_upload,
        'allow_back_camera': link.allow_back_camera,
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

    allow_upload = request.POST.get('allow_upload', None)
    if allow_upload is not None:
        link.allow_upload = (allow_upload == 'true')

    allow_back_camera = request.POST.get('allow_back_camera', None)
    if allow_back_camera is not None:
        link.allow_back_camera = (allow_back_camera == 'true')

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
    personal_link = None
    personal_gallery_url = None
    whatsapp_share_text = None
    created_lead = None
    whatsapp_sent = False
    guest_phone_entered = None

    if request.method == 'POST':
        selfie = request.FILES.get('selfie')
        if not selfie or getattr(selfie, 'size', 0) == 0:
            # Browser reload/refresh on POST page without re-transmitting file bytes
            cached_token = request.session.get(f'last_personal_token_{link.token}')
            if cached_token:
                pl = PersonalGalleryLink.objects.filter(token=cached_token).first()
                if pl and pl.is_accessible:
                    return redirect('gallery:personal_gallery', token=pl.token)
            # If no cached gallery, cleanly reload the search form
            return redirect('gallery:public_search', token=link.token)

        # 1. Save the Guest Lead
        name = request.POST.get('guest_name')
        age = request.POST.get('guest_age')
        phone = request.POST.get('guest_phone')
        email = request.POST.get('guest_email', '')
        guest_phone_entered = phone
        
        if name and age and phone:
            created_lead = GuestLead.objects.create(
                event=event,
                name=name,
                age=age,
                phone=phone,
                email=email,
                selfie=selfie
            )
            # Reset file pointer after saving to DB so the chunk reading below works correctly
            selfie.seek(0)

            # Immediately link the visitor's telemetry session to this new GuestLead
            telemetry_session_id = request.POST.get('telemetry_session_id')
            if telemetry_session_id:
                VisitorSession.objects.filter(session_id=telemetry_session_id).update(guest_lead=created_lead)

        # 2. Process FAISS Search via temp file
        import time
        import base64
        temp_dir = os.path.join(settings.MEDIA_ROOT, 'temp')
        os.makedirs(temp_dir, exist_ok=True)
        safe_name = selfie.name.replace(' ', '_')
        full_temp_path = os.path.join(temp_dir, f"public_search_{uuid.uuid4().hex}.jpg")

        with open(full_temp_path, 'wb') as f:
            for chunk in selfie.chunks():
                f.write(chunk)

        try:
            if not os.path.exists(full_temp_path) or os.path.getsize(full_temp_path) == 0:
                raise ValueError("No selfie image data was received. Please select and upload your selfie photo.")

            normalize_selfie_file(full_temp_path)
            with open(full_temp_path, 'rb') as img_f:
                search_image_b64 = base64.b64encode(img_f.read()).decode('utf-8')

            from .utils import find_or_create_personal_gallery_link
            results, query_vector = search_person_by_selfie(
                full_temp_path,
                event_id=event.id,
                return_vector=True
            )
            if link.person_label:
                # One query instead of one query per result
                allowed_ids = set(
                    FaceEmbedding.objects.filter(
                        image_id__in=[res['image'].id for res in results],
                        person_label=link.person_label,
                    ).values_list('image_id', flat=True)
                )
                results = [res for res in results if res['image'].id in allowed_ids]
            for res in results:
                name_only, ext = os.path.splitext(res['image'].filename)
                res['download_filename'] = f"{name_only}_{event.slug}{ext}"

            if results or query_vector:
                effective_expiry = link.expires_at or event.expires_at
                personal_link, is_new = find_or_create_personal_gallery_link(
                    event=event,
                    query_vector=query_vector,
                    results=results or [],
                    searcher_name=name or 'Guest',
                    selfie_path=full_temp_path,
                    selfie_thumbnail_b64=search_image_b64 or '',
                    share_link=link,
                    guest_lead=created_lead,
                    expires_at=effective_expiry
                )
                if created_lead and personal_link:
                    created_lead.personal_gallery_link = personal_link
                    try:
                        created_lead.save(update_fields=['personal_gallery_link'])
                    except Exception:
                        pass
                # Cache token in session so page refresh preserves personal gallery
                request.session[f'last_personal_token_{link.token}'] = str(personal_link.token)

                personal_gallery_url = request.build_absolute_uri(
                    reverse('gallery:personal_gallery', args=[personal_link.token])
                )
                from urllib.parse import quote
                raw_wa = f"Here are my photos from {event.name}: {personal_gallery_url}"
                whatsapp_share_text = quote(raw_wa)

                # Send WhatsApp notification via Natdemy WhatsApp Template API
                if phone:
                    from .whatsapp import send_whatsapp_selfie_link_async
                    send_whatsapp_selfie_link_async(
                        phone=phone,
                        name=name or 'Guest',
                        link_url=personal_gallery_url
                    )
                    whatsapp_sent = True
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
        'allow_upload': getattr(link, 'allow_upload', True),
        'allow_back_camera': getattr(link, 'allow_back_camera', True),
        'search_image_b64': search_image_b64,
        'personal_link': personal_link,
        'personal_gallery_url': personal_gallery_url,
        'whatsapp_share_text': whatsapp_share_text,
        'whatsapp_sent': whatsapp_sent,
        'whatsapp_enabled': getattr(settings, 'ENABLE_WHATSAPP_LINK_SEND', False),
        'guest_phone': guest_phone_entered,
    })


def personal_gallery(request, token):
    """
    Public accessible personal gallery for a user/guest who performed an AI search.
    Accessible anytime until the event or share link expires.
    """
    from django.db.models import F
    from urllib.parse import quote

    personal_link = get_object_or_404(
        PersonalGalleryLink.objects.select_related('event__owner', 'share_link', 'guest_lead'),
        token=token
    )
    event = personal_link.event

    if not personal_link.is_accessible:
        return render(request, 'gallery/personal_gallery_expired.html', {
            'personal_link': personal_link,
            'event': event,
            'expires_at': personal_link.get_effective_expires_at(),
        })

    # Increment view counter
    PersonalGalleryLink.objects.filter(id=personal_link.id).update(
        view_count=F('view_count') + 1,
        last_viewed_at=timezone.now()
    )

    # Fetch matched images directly (instant sub-20ms database read)
    images = personal_link.images.select_related('event').order_by('-uploaded_at')
    results = []
    for img in images:
        name_only, ext = os.path.splitext(img.filename)
        results.append({
            'image': img,
            'score': personal_link.match_scores.get(str(img.id), 0.0),
            'download_filename': f"{name_only}_{event.slug}{ext}"
        })

    current_url = request.build_absolute_uri()
    raw_wa = f"Here are my photos from {event.name}: {current_url}"
    whatsapp_share_text = quote(raw_wa)

    return render(request, 'gallery/personal_gallery.html', {
        'personal_link': personal_link,
        'event': event,
        'results': results,
        'total_results': len(results),
        'newly_added_count': 0,
        'personal_gallery_url': current_url,
        'whatsapp_share_text': whatsapp_share_text,
        'expires_at': personal_link.get_effective_expires_at(),
    })


def download_personal_gallery_zip(request, token):
    """Download all photos belonging to a personal gallery as a single ZIP."""
    personal_link = get_object_or_404(
        PersonalGalleryLink.objects.select_related('event'),
        token=token
    )
    if not personal_link.is_accessible:
        return HttpResponseForbidden("This personal gallery has expired.")

    event = personal_link.event
    images = personal_link.images.select_related('event').all()
    if not images:
        return redirect('gallery:personal_gallery', token=token)

    import zipfile
    from io import BytesIO
    zip_buffer = BytesIO()

    with zipfile.ZipFile(zip_buffer, 'w') as zip_file:
        for img in images:
            try:
                content = None
                # Check for rendered version with watermark/presets
                if img.preset_name or img.preset_settings or (event and event.watermark_enabled):
                    from .media_cache import rendered_cache_path
                    apply_wm = bool(event and event.watermark_enabled)
                    wm_config = event.get_watermark_config() if apply_wm else None
                    cached = rendered_cache_path(img, 'processed', wm_config)
                    if os.path.exists(cached):
                        with open(cached, 'rb') as f:
                            content = f.read()

                if content is None:
                    try:
                        _p = getattr(img.file, 'path', None)
                        if _p and os.path.exists(_p):
                            with open(_p, 'rb') as f:
                                content = f.read()
                    except Exception:
                        pass

                if content is None:
                    with img.file.open('rb') as f:
                        content = f.read()

                if content:
                    name_only, ext = os.path.splitext(img.filename)
                    out_name = f"{name_only}_{event.slug}{ext}"
                    zip_file.writestr(out_name, content)
            except Exception as ze:
                print(f"Error zipping image {img.id}: {ze}")

    zip_buffer.seek(0)
    searcher_safe = slugify(personal_link.searcher_name or 'my')
    zip_filename = f"{searcher_safe}_{event.slug}_photos.zip"
    response = FileResponse(zip_buffer, content_type='application/zip')
    response['Content-Disposition'] = f'attachment; filename="{zip_filename}"'
    return response


@require_POST
def scan_new_personal_photos(request, token):
    """Re-scan the event for any newly uploaded photos matching this person's face (100% S3 and local safe)."""
    personal_link = get_object_or_404(
        PersonalGalleryLink.objects.select_related('event'),
        token=token
    )
    if not personal_link.is_accessible:
        return JsonResponse({'success': False, 'message': 'This personal gallery has expired.'}, status=403)

    try:
        from .utils import sync_personal_gallery_matches
        new_images = sync_personal_gallery_matches(personal_link)

        return JsonResponse({
            'success': True,
            'new_count': len(new_images),
            'total_count': personal_link.images.count(),
            'message': f"Found {len(new_images)} new photo(s)!" if new_images else "You have all available photos."
        })
    except Exception as e:
        return JsonResponse({'success': False, 'message': str(e)}, status=500)



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
            thumb_token = img.get_secure_thumbnail_token()
            data.append({
                'id': img.id,
                'url': request.build_absolute_uri(reverse('gallery:secure_image', args=[thumb_token])),
                'full_url': request.build_absolute_uri(reverse('gallery:secure_image', args=[file_token])),
                'processed_url': request.build_absolute_uri(reverse('gallery:secure_image', args=[processed_token])),
                'filename': img.filename,
                'preset_name': img.preset_name or 'original',
                'preset_settings': img.preset_settings or {},
                'uploaded_at': timezone.localtime(img.uploaded_at).strftime('%b %d, %I:%M %p') if img.uploaded_at else '',
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


from django.views.decorators.csrf import csrf_exempt

@csrf_exempt
@require_POST
def check_selfie_face(request):
    """
    Ultra-fast pre-flight face count check before photo upload or form submission.
    Accepts image file or base64 data.
    Returns:
      valid: bool
      face_count: int
      error: optional str
    """
    import base64
    import json
    import cv2
    import numpy as np
    from .utils import detect_faces, _downscale_rgb

    file_obj = request.FILES.get('image') or request.FILES.get('selfie')
    raw_bytes = None

    if file_obj:
        raw_bytes = file_obj.read()
    else:
        b64_data = request.POST.get('image_b64') or request.POST.get('image_data') or request.POST.get('image')
        if not b64_data and 'application/json' in request.content_type.lower():
            try:
                body_json = json.loads(request.body.decode('utf-8'))
                b64_data = body_json.get('image_b64') or body_json.get('image_data') or body_json.get('image')
            except Exception:
                pass
        if b64_data:
            if ',' in b64_data:
                b64_data = b64_data.split(',', 1)[1]
            try:
                raw_bytes = base64.b64decode(b64_data)
            except Exception:
                return JsonResponse({'valid': False, 'face_count': 0, 'error': 'Invalid image data.'}, status=400)

    if not raw_bytes:
        return JsonResponse({'valid': False, 'face_count': 0, 'error': 'No image provided.'}, status=400)

    try:
        arr = np.asarray(bytearray(raw_bytes), dtype=np.uint8)
        img = cv2.imdecode(arr, cv2.IMREAD_COLOR)
        if img is None:
            return JsonResponse({'valid': False, 'face_count': 0, 'error': 'Could not decode image.'}, status=400)

        img = cv2.cvtColor(img, cv2.COLOR_BGR2RGB)
        img = _downscale_rgb(img, max_dim=640)
        faces = detect_faces(img)
        count = len(faces)

        if count == 1:
            return JsonResponse({
                'valid': True,
                'face_count': 1,
                'message': '1 face verified successfully.'
            })
        elif count == 0:
            return JsonResponse({
                'valid': False,
                'face_count': 0,
                'error': 'No face detected in the photo. Please make sure your face is clearly visible and well-lit.'
            })
        else:
            return JsonResponse({
                'valid': False,
                'face_count': count,
                'error': f'Multiple faces detected ({count} faces found). Please upload or take a photo with only yourself.'
            })
    except Exception as e:
        return JsonResponse({'valid': False, 'face_count': 0, 'error': f'Face validation failed: {str(e)}'}, status=500)


# ═════════════════════════════════════════════════════════════════
# 📊 PHOTONA REAL-TIME ANALYTICS API & DASHBOARD
# ═════════════════════════════════════════════════════════════════

@csrf_exempt
@require_POST
def analytics_track_page(request):
    """
    Non-blocking endpoint to track pageviews, user-agent telemetry, and traffic acquisition.
    Runs via navigator.sendBeacon or fetch().
    """
    import json
    from .analytics import parse_user_agent, detect_traffic_source, get_client_ip, get_client_location

    try:
        data = {}
        if request.content_type == 'application/json' and request.body:
            try:
                data = json.loads(request.body.decode('utf-8'))
            except Exception:
                pass
        if not data:
            data = request.POST

        session_id = data.get('session_id') or str(uuid.uuid4())
        url_path = data.get('url_path', request.path)
        page_type = data.get('page_type', 'other')
        page_title = data.get('page_title', '')
        event_slug = data.get('event_slug')
        query_src = data.get('traffic_src')
        lead_id = data.get('lead_id')

        # Ignore staff/admin console views and non-guest internal paths
        if (request.user.is_authenticated and (request.user.is_staff or request.user.is_superuser)) or \
           url_path.startswith('/super-admin') or url_path.startswith('/admin') or url_path.startswith('/preview-error'):
            return JsonResponse({'status': 'ignored', 'reason': 'admin_internal_path'})

        # 1. Resolve Event if provided
        event = None
        if event_slug:
            event = Event.objects.filter(slug=event_slug).first()

        # 2. Extract telemetry
        ua = request.META.get('HTTP_USER_AGENT', '')
        device_type, os_name, browser_name = parse_user_agent(ua)
        traffic_source = detect_traffic_source(request, query_src)
        ip = get_client_ip(request)
        city, country = get_client_location(request)

        # 3. Get or create VisitorSession
        session, created = VisitorSession.objects.get_or_create(
            session_id=session_id,
            defaults={
                'ip_address': ip,
                'country': country,
                'city': city,
                'device_type': device_type,
                'os_name': os_name,
                'browser_name': browser_name,
                'traffic_source': traffic_source,
            }
        )
        if not created:
            session.last_seen = timezone.now()
            if session.city == 'Unknown' and city != 'Unknown':
                session.city = city
                session.country = country
            if session.traffic_source == 'Direct' and traffic_source != 'Direct':
                session.traffic_source = traffic_source

        if lead_id and not session.guest_lead_id:
            session.guest_lead_id = lead_id

        # Also auto-resolve guest lead if visiting personal gallery (/my-photos/<token>/)
        if not session.guest_lead_id and '/my-photos/' in url_path:
            import re
            m = re.search(r'/my-photos/([0-9a-fA-F-]{36})', url_path)
            if m:
                token_str = m.group(1)
                pl = PersonalGalleryLink.objects.filter(token=token_str).select_related('guest_lead').first()
                if pl and pl.guest_lead:
                    session.guest_lead = pl.guest_lead

        session.save()

        # 4. Record PageViewEvent
        page_view = PageViewEvent.objects.create(
            session=session,
            event=event,
            page_type=page_type,
            url_path=url_path[:500],
            page_title=page_title[:255],
            duration_seconds=0
        )

        return JsonResponse({
            'status': 'ok',
            'view_id': page_view.id,
            'session_id': session.session_id
        })
    except Exception as e:
        return JsonResponse({'status': 'error', 'message': str(e)}, status=400)


@csrf_exempt
@require_POST
def analytics_heartbeat(request):
    """
    Records dwell time and scroll depth periodically or on page exit (sendBeacon).
    """
    import json
    try:
        data = {}
        if request.content_type == 'application/json' and request.body:
            try:
                data = json.loads(request.body.decode('utf-8'))
            except Exception:
                pass
        if not data:
            data = request.POST

        view_id = data.get('view_id')
        session_id = data.get('session_id')
        duration_inc = int(data.get('duration_seconds', 0) or 0)
        scroll_depth = int(data.get('scroll_depth', 0) or 0)

        if view_id:
            pv = PageViewEvent.objects.filter(id=view_id).first()
            if pv:
                pv.duration_seconds = max(pv.duration_seconds, duration_inc)
                if scroll_depth > pv.scroll_depth:
                    pv.scroll_depth = min(scroll_depth, 100)
                pv.save(update_fields=['duration_seconds', 'scroll_depth'])

        if session_id:
            session = VisitorSession.objects.filter(session_id=session_id).first()
            if session:
                session.last_seen = timezone.now()
                session.total_time_seconds += duration_inc
                session.save(update_fields=['last_seen', 'total_time_seconds'])

        return JsonResponse({'status': 'ok'})
    except Exception as e:
        return JsonResponse({'status': 'error', 'message': str(e)}, status=400)


@csrf_exempt
@require_POST
def analytics_photo_interaction(request):
    """
    Tracks engagement with individual photos (grid views, lightbox zoom, downloads).
    """
    import json
    try:
        data = {}
        if request.content_type == 'application/json' and request.body:
            try:
                data = json.loads(request.body.decode('utf-8'))
            except Exception:
                pass
        if not data:
            data = request.POST

        image_id = data.get('photo_id') or data.get('image_id')
        session_id = data.get('session_id')
        action = data.get('action', 'view')
        dwell = int(data.get('dwell_seconds', 0) or 0)

        if not image_id:
            return JsonResponse({'status': 'error', 'message': 'Missing image_id'}, status=400)

        img = GalleryImage.objects.filter(id=image_id).select_related('event').first()
        if not img:
            return JsonResponse({'status': 'error', 'message': 'Image not found'}, status=404)

        session = None
        if session_id:
            session = VisitorSession.objects.filter(session_id=session_id).first()

        PhotoInteraction.objects.create(
            image=img,
            event=img.event,
            session=session,
            action=action,
            dwell_seconds=dwell
        )
        return JsonResponse({'status': 'ok'})
    except Exception as e:
        return JsonResponse({'status': 'error', 'message': str(e)}, status=400)


@user_passes_test(lambda u: u.is_superuser)
def analytics_live_feed(request):
    """
    High-frequency real-time polling API for the Super Admin Analytics Dashboard.
    Streams new visitor pageviews and photo interactions (downloads & zooms) live without page reloads.
    """
    from django.db.models import Count, Sum, Q
    from datetime import timedelta
    from django.utils.timesince import timesince

    now = timezone.now()
    organizer_id = request.GET.get('organizer_id', '').strip()
    event_id = request.GET.get('event_id', '').strip()
    time_range = request.GET.get('range', '7d').strip().lower()

    views_qs = PageViewEvent.objects.select_related('session', 'session__guest_lead', 'event')
    interactions_qs = PhotoInteraction.objects.select_related('session', 'session__guest_lead', 'image', 'event')
    sessions_qs = VisitorSession.objects.all()

    if event_id and event_id != 'all':
        views_qs = views_qs.filter(event_id=event_id)
        sessions_qs = sessions_qs.filter(page_views__event_id=event_id).distinct()
        interactions_qs = interactions_qs.filter(event_id=event_id)
    elif organizer_id and organizer_id != 'all':
        views_qs = views_qs.filter(event__owner_id=organizer_id)
        sessions_qs = sessions_qs.filter(page_views__event__owner_id=organizer_id).distinct()
        interactions_qs = interactions_qs.filter(event__owner_id=organizer_id)

    if time_range == 'today':
        views_qs = views_qs.filter(timestamp__date=now.date())
        interactions_qs = interactions_qs.filter(timestamp__date=now.date())
        sessions_qs = sessions_qs.filter(last_seen__date=now.date())
    elif time_range == '7d':
        cutoff = now - timedelta(days=7)
        views_qs = views_qs.filter(timestamp__gte=cutoff)
        interactions_qs = interactions_qs.filter(timestamp__gte=cutoff)
        sessions_qs = sessions_qs.filter(last_seen__gte=cutoff)
    elif time_range == '30d':
        cutoff = now - timedelta(days=30)
        views_qs = views_qs.filter(timestamp__gte=cutoff)
        interactions_qs = interactions_qs.filter(timestamp__gte=cutoff)
        sessions_qs = sessions_qs.filter(last_seen__gte=cutoff)

    live_cutoff = now - timedelta(minutes=5)
    live_visitors = VisitorSession.objects.filter(last_seen__gte=live_cutoff).count()

    total_pageviews = views_qs.count()
    unique_visitors = sessions_qs.count()
    funnel_visits = views_qs.filter(page_type__in=['event_gallery', 'landing', 'search', 'other']).count()
    funnel_searches = views_qs.filter(page_type='search').count()
    funnel_matches = views_qs.filter(page_type='personal_gallery').count()
    funnel_downloads = views_qs.filter(page_type='zip_download').count() + interactions_qs.filter(action='download').count()

    raw_views = list(views_qs.order_by('-timestamp')[:20])
    raw_interactions = list(interactions_qs.order_by('-timestamp')[:20])

    activities = []
    for v in raw_views:
        lead_name = v.session.guest_lead.name if v.session.guest_lead else 'Guest (Visitor)'
        badge_text = 'SEARCH' if v.page_type == 'search' else ('GALLERY' if v.page_type == 'personal_gallery' else 'PAGE')
        badge_color = '#2563eb' if v.page_type == 'search' else ('#7c3aed' if v.page_type == 'personal_gallery' else '#6366f1')
        badge_bg = 'rgba(37, 99, 235, 0.12)' if v.page_type == 'search' else ('rgba(124, 58, 237, 0.12)' if v.page_type == 'personal_gallery' else 'rgba(99, 102, 241, 0.12)')
        icon = '🔍' if v.page_type == 'search' else ('📸' if v.page_type == 'personal_gallery' else '📄')
        
        activities.append({
            'id': f'v_{v.id}',
            'lead_name': lead_name,
            'is_lead': bool(v.session.guest_lead),
            'title': v.exact_page_name,
            'badge_text': badge_text,
            'badge_color': badge_color,
            'badge_bg': badge_bg,
            'icon': icon,
            'event_name': v.event.name if v.event else 'Global',
            'device': f'{v.session.device_type} ({v.session.os_name})',
            'city': v.session.city if v.session.city != 'Unknown' else '',
            'duration_str': format_duration_display(v.duration_seconds) if v.duration_seconds > 0 else '',
            'time_ago': f'{timesince(v.timestamp)} ago',
            'timestamp': v.timestamp.isoformat()
        })

    for i in raw_interactions:
        lead_name = i.session.guest_lead.name if (i.session and i.session.guest_lead) else 'Guest (Visitor)'
        is_dl = (i.action == 'download')
        badge_text = 'DOWNLOAD' if is_dl else 'ZOOM'
        badge_color = '#059669' if is_dl else '#d97706'
        badge_bg = 'rgba(16, 185, 129, 0.15)' if is_dl else 'rgba(217, 119, 6, 0.15)'
        icon = '⬇️' if is_dl else '👁️'
        action_desc = f"{icon} Downloaded photo" if is_dl else f"{icon} Viewed in lightbox"

        activities.append({
            'id': f'i_{i.id}',
            'lead_name': lead_name,
            'is_lead': bool(i.session and i.session.guest_lead),
            'title': f"{action_desc}: {i.image.filename}",
            'badge_text': badge_text,
            'badge_color': badge_color,
            'badge_bg': badge_bg,
            'icon': icon,
            'event_name': i.event.name if i.event else (i.image.event.name if i.image.event else 'Event'),
            'device': f'{i.session.device_type} ({i.session.os_name})' if i.session else 'Mobile',
            'city': i.session.city if (i.session and i.session.city != 'Unknown') else '',
            'duration_str': format_duration_display(i.dwell_seconds) if i.dwell_seconds > 0 else '',
            'time_ago': f'{timesince(i.timestamp)} ago',
            'timestamp': i.timestamp.isoformat()
        })

    activities.sort(key=lambda x: x['timestamp'], reverse=True)
    activities = activities[:25]

    # Top Photos (live leaderboard sync)
    top_photo_ids = interactions_qs.values('image_id').annotate(
        views=Count('id', filter=Q(action__in=['view', 'lightbox'])),
        downloads=Count('id', filter=Q(action='download')),
        dwell=Sum('dwell_seconds')
    ).annotate(
        score=Count('id', filter=Q(action='download')) * 5 + Count('id', filter=Q(action__in=['view', 'lightbox'])) * 2
    ).order_by('-score', '-downloads', '-views', '-dwell')[:20]

    top_photos = []
    for tp in top_photo_ids:
        img = GalleryImage.objects.filter(id=tp['image_id']).first()
        if img:
            thumb_url = img.thumbnail.url if img.thumbnail else (img.file.url if img.file else '')
            dwell = tp['dwell'] or 0
            top_photos.append({
                'id': img.id,
                'filename': img.filename,
                'thumbnail_url': thumb_url,
                'views': tp['views'],
                'downloads': tp['downloads'],
                'dwell_sec': dwell,
                'dwell_display': format_duration_display(dwell),
            })

    return JsonResponse({
        'status': 'ok',
        'live_visitors': live_visitors,
        'total_pageviews': total_pageviews,
        'unique_visitors': unique_visitors,
        'funnel_visits': funnel_visits,
        'funnel_searches': funnel_searches,
        'funnel_matches': funnel_matches,
        'funnel_downloads': funnel_downloads,
        'activities': activities,
        'top_photos': top_photos,
    })


@user_passes_test(lambda u: u.is_superuser)
def super_admin_analytics_dashboard(request):
    """
    Massive Analytics Dashboard: Real-time visitors, dwell times, visitor journeys, top viral photos, funnels.
    Supports hierarchical filtering: All Organizers ➔ Specific Organizer ➔ All Events / Specific Event.
    """
    from django.db.models import Count, Sum, Avg, Q
    from datetime import timedelta
    from django.contrib.auth.models import User

    now = timezone.now()
    organizer_id = request.GET.get('organizer_id', '').strip()
    event_id = request.GET.get('event_id', '').strip()
    time_range = request.GET.get('range', '7d').strip().lower()

    # Organizers query (users who are studio admins / event owners)
    all_organizers = User.objects.filter(Q(is_superuser=False) | Q(events__isnull=False)).distinct().order_by('username')
    selected_organizer = None
    if organizer_id and organizer_id != 'all':
        selected_organizer = all_organizers.filter(id=organizer_id).first()

    # Events query (filtered by organizer if selected)
    events_qs = Event.objects.all().select_related('owner').order_by('-created_at')
    if selected_organizer:
        events_qs = events_qs.filter(owner=selected_organizer)
    available_events = list(events_qs)

    selected_event = None

    views_qs = PageViewEvent.objects.all()
    sessions_qs = VisitorSession.objects.all()
    interactions_qs = PhotoInteraction.objects.all()

    # 1. Filter by Event or Organizer
    if event_id and event_id != 'all':
        selected_event = Event.objects.filter(id=event_id).first()
        if selected_event:
            views_qs = views_qs.filter(event=selected_event)
            sessions_qs = sessions_qs.filter(page_views__event=selected_event).distinct()
            interactions_qs = interactions_qs.filter(event=selected_event)
    elif selected_organizer:
        views_qs = views_qs.filter(event__owner=selected_organizer)
        sessions_qs = sessions_qs.filter(page_views__event__owner=selected_organizer).distinct()
        interactions_qs = interactions_qs.filter(event__owner=selected_organizer)

    # 2. Filter by Time Range
    if time_range == 'today':
        views_qs = views_qs.filter(timestamp__date=now.date())
        sessions_qs = sessions_qs.filter(last_seen__date=now.date())
        interactions_qs = interactions_qs.filter(timestamp__date=now.date())
    elif time_range == '7d':
        cutoff = now - timedelta(days=7)
        views_qs = views_qs.filter(timestamp__gte=cutoff)
        sessions_qs = sessions_qs.filter(last_seen__gte=cutoff)
        interactions_qs = interactions_qs.filter(timestamp__gte=cutoff)
    elif time_range == '30d':
        cutoff = now - timedelta(days=30)
        views_qs = views_qs.filter(timestamp__gte=cutoff)
        sessions_qs = sessions_qs.filter(last_seen__gte=cutoff)
        interactions_qs = interactions_qs.filter(timestamp__gte=cutoff)

    # 3. Key Executive Metrics
    total_pageviews = views_qs.count()
    unique_visitors = sessions_qs.count()
    total_time_sec = views_qs.aggregate(s=Sum('duration_seconds'))['s'] or 0
    avg_dwell_sec = round(total_time_sec / unique_visitors) if unique_visitors else 0

    # Live Visitors Right Now (active within last 5 minutes)
    live_cutoff = now - timedelta(minutes=5)
    live_visitors = VisitorSession.objects.filter(last_seen__gte=live_cutoff).count()

    # Funnel Counts
    funnel_visits = views_qs.filter(page_type__in=['event_gallery', 'landing', 'search', 'other']).count()
    funnel_searches = views_qs.filter(page_type='search').count()
    funnel_matches = views_qs.filter(page_type='personal_gallery').count()
    funnel_downloads = views_qs.filter(page_type='zip_download').count() + interactions_qs.filter(action='download').count()

    # 4. Per-Page Time Spent Breakdown with Exact Page Names
    page_aggregation = {}
    for p in views_qs:
        # Standardize key so guest funnel actions are neatly grouped, while custom pages show their exact path
        if p.page_type in ('search', 'personal_gallery', 'event_gallery', 'landing', 'zip_download'):
            path_key = p.page_type
        else:
            path_key = (p.url_path or 'other').strip()

        if path_key not in page_aggregation:
            page_aggregation[path_key] = {
                'label': p.exact_page_name,
                'page_type': p.page_type,
                'url_path': p.url_path or '',
                'url_description': p.url_description,
                'views_count': 0,
                'visitors': set(),
                'total_seconds': 0,
            }
        page_aggregation[path_key]['views_count'] += 1
        page_aggregation[path_key]['visitors'].add(p.session_id)
        page_aggregation[path_key]['total_seconds'] += (p.duration_seconds or 0)

    page_stats = []
    for k, item in sorted(page_aggregation.items(), key=lambda x: x[1]['views_count'], reverse=True):
        views_c = item['views_count']
        tot_sec = item['total_seconds']
        avg_p_sec = round(tot_sec / views_c) if views_c else 0

        mins = avg_p_sec // 60
        secs = avg_p_sec % 60
        time_display = f"{mins}m {secs}s" if mins > 0 else f"{secs}s"

        page_stats.append({
            'page_type': item['page_type'],
            'url_path': item['url_path'],
            'url_description': item.get('url_description', ''),
            'label': item['label'],
            'views_count': views_c,
            'visitors_count': len(item['visitors']),
            'avg_time_display': time_display,
            'total_time_display': f"{tot_sec // 60}m {tot_sec % 60}s"
        })

    # 5. Top 10 Most Popular & Viewed Photos (Ranked by downloads & views)
    top_photo_ids = interactions_qs.values('image_id').annotate(
        views=Count('id', filter=Q(action__in=['view', 'lightbox'])),
        downloads=Count('id', filter=Q(action='download')),
        dwell=Sum('dwell_seconds')
    ).annotate(
        score=Count('id', filter=Q(action='download')) * 5 + Count('id', filter=Q(action__in=['view', 'lightbox'])) * 2
    ).order_by('-score', '-downloads', '-views', '-dwell')[:20]

    top_photos = []
    for tp in top_photo_ids:
        img = GalleryImage.objects.filter(id=tp['image_id']).first()
        if img:
            dwell = tp['dwell'] or 0
            top_photos.append({
                'image': img,
                'views': tp['views'],
                'downloads': tp['downloads'],
                'dwell_sec': dwell,
                'dwell_display': format_duration_display(dwell),
            })

    # 6. Device, OS, Browser, Source Breakdowns
    device_breakdown = list(sessions_qs.values('device_type').annotate(count=Count('id')).order_by('-count'))
    os_breakdown = list(sessions_qs.values('os_name').annotate(count=Count('id')).order_by('-count')[:5])
    browser_breakdown = list(sessions_qs.values('browser_name').annotate(count=Count('id')).order_by('-count')[:5])
    sources_breakdown = list(sessions_qs.values('traffic_source').annotate(count=Count('id')).order_by('-count')[:5])
    top_cities = list(sessions_qs.exclude(city='Unknown').values('city', 'country').annotate(count=Count('id')).order_by('-count')[:6])

    # 7. Live Real-Time Activity Feed (Combined Pageviews + Photo Downloads/Zooms)
    from django.utils.timesince import timesince
    raw_views = list(views_qs.select_related('session', 'session__guest_lead', 'event').order_by('-timestamp')[:25])
    raw_interactions = list(interactions_qs.select_related('session', 'session__guest_lead', 'image', 'event').order_by('-timestamp')[:25])
    recent_activities = []
    for v in raw_views:
        lead_name = v.session.guest_lead.name if v.session.guest_lead else 'Guest (Visitor)'
        badge_text = 'SEARCH' if v.page_type == 'search' else ('GALLERY' if v.page_type == 'personal_gallery' else 'PAGE')
        badge_color = '#2563eb' if v.page_type == 'search' else ('#7c3aed' if v.page_type == 'personal_gallery' else '#6366f1')
        badge_bg = 'rgba(37, 99, 235, 0.12)' if v.page_type == 'search' else ('rgba(124, 58, 237, 0.12)' if v.page_type == 'personal_gallery' else 'rgba(99, 102, 241, 0.12)')
        icon = '🔍' if v.page_type == 'search' else ('📸' if v.page_type == 'personal_gallery' else '📄')
        recent_activities.append({
            'id': f'v_{v.id}',
            'lead_name': lead_name,
            'is_lead': bool(v.session.guest_lead),
            'title': v.exact_page_name,
            'badge_text': badge_text,
            'badge_color': badge_color,
            'badge_bg': badge_bg,
            'icon': icon,
            'event_name': v.event.name if v.event else 'Global',
            'device': f'{v.session.device_type} ({v.session.os_name})',
            'city': v.session.city if v.session.city != 'Unknown' else '',
            'duration_str': format_duration_display(v.duration_seconds) if v.duration_seconds > 0 else '',
            'time_ago': f'{timesince(v.timestamp)} ago',
            'timestamp': v.timestamp
        })

    for i in raw_interactions:
        lead_name = i.session.guest_lead.name if (i.session and i.session.guest_lead) else 'Guest (Visitor)'
        is_dl = (i.action == 'download')
        badge_text = 'DOWNLOAD' if is_dl else 'ZOOM'
        badge_color = '#059669' if is_dl else '#d97706'
        badge_bg = 'rgba(16, 185, 129, 0.15)' if is_dl else 'rgba(217, 119, 6, 0.15)'
        icon = '⬇️' if is_dl else '👁️'
        action_desc = f"{icon} Downloaded photo" if is_dl else f"{icon} Viewed in lightbox"
        recent_activities.append({
            'id': f'i_{i.id}',
            'lead_name': lead_name,
            'is_lead': bool(i.session and i.session.guest_lead),
            'title': f"{action_desc}: {i.image.filename}",
            'badge_text': badge_text,
            'badge_color': badge_color,
            'badge_bg': badge_bg,
            'icon': icon,
            'event_name': i.event.name if i.event else (i.image.event.name if i.image.event else 'Event'),
            'device': f'{i.session.device_type} ({i.session.os_name})' if i.session else 'Mobile',
            'city': i.session.city if (i.session and i.session.city != 'Unknown') else '',
            'duration_str': format_duration_display(i.dwell_seconds) if i.dwell_seconds > 0 else '',
            'time_ago': f'{timesince(i.timestamp)} ago',
            'timestamp': i.timestamp
        })

    recent_activities.sort(key=lambda x: x['timestamp'], reverse=True)
    recent_activities = recent_activities[:25]

    # 8. Recent Guest Sessions with Timelines
    recent_sessions = sessions_qs.select_related('guest_lead').prefetch_related('page_views').order_by('-last_seen')[:15]

    # Format average total dwell time
    avg_mins = avg_dwell_sec // 60
    avg_secs = avg_dwell_sec % 60
    avg_time_str = f"{avg_mins}m {avg_secs}s" if avg_mins > 0 else f"{avg_secs}s"

    context = {
        'active_tab': 'analytics',
        'all_organizers': all_organizers,
        'selected_organizer': selected_organizer,
        'selected_organizer_id': organizer_id,
        'available_events': available_events,
        'selected_event': selected_event,
        'selected_event_id': event_id,
        'time_range': time_range,
        'total_pageviews': total_pageviews,
        'unique_visitors': unique_visitors,
        'avg_time_str': avg_time_str,
        'live_visitors': live_visitors,
        'funnel': {
            'visits': funnel_visits,
            'searches': funnel_searches,
            'matches': funnel_matches,
            'downloads': funnel_downloads,
        },
        'page_stats': page_stats,
        'top_photos': top_photos,
        'device_breakdown': device_breakdown,
        'os_breakdown': os_breakdown,
        'browser_breakdown': browser_breakdown,
        'sources_breakdown': sources_breakdown,
        'top_cities': top_cities,
        'recent_activities': recent_activities,
        'recent_sessions': recent_sessions,
    }
    return render(request, 'gallery/analytics_dashboard.html', context)


@user_passes_test(lambda u: u.is_superuser)
def export_analytics_csv(request):
    """Export pageview and visitor analytics to a CSV file."""
    import csv
    from django.http import HttpResponse
    from datetime import timedelta

    now = timezone.now()
    organizer_id = request.GET.get('organizer_id', '').strip()
    event_id = request.GET.get('event_id', '').strip()
    time_range = request.GET.get('range', '7d').strip().lower()

    views_qs = PageViewEvent.objects.select_related('session', 'session__guest_lead', 'event', 'event__owner').order_by('-timestamp')

    if event_id and event_id != 'all':
        views_qs = views_qs.filter(event_id=event_id)
    elif organizer_id and organizer_id != 'all':
        views_qs = views_qs.filter(event__owner_id=organizer_id)

    if time_range == 'today':
        views_qs = views_qs.filter(timestamp__date=now.date())
    elif time_range == '7d':
        views_qs = views_qs.filter(timestamp__gte=now - timedelta(days=7))
    elif time_range == '30d':
        views_qs = views_qs.filter(timestamp__gte=now - timedelta(days=30))

    response = HttpResponse(content_type='text/csv')
    response['Content-Disposition'] = f'attachment; filename="photona_analytics_{timezone.now().strftime("%Y%m%d_%H%M")}.csv"'

    writer = csv.writer(response)
    writer.writerow(['Timestamp', 'Session ID', 'Organizer', 'Event Name', 'Page Type', 'URL Path', 'Duration (Seconds)', 'Device', 'OS', 'Browser', 'City', 'Traffic Source', 'Guest Name'])

    views = views_qs[:5000]
    for v in views:
        sess = v.session
        lead_name = sess.guest_lead.name if sess.guest_lead else ''
        organizer_name = v.event.owner.username if v.event and v.event.owner else 'Global'
        writer.writerow([
            timezone.localtime(v.timestamp).strftime('%Y-%m-%d %H:%M:%S'),
            sess.session_id[:12],
            organizer_name,
            v.event.name if v.event else 'Global',
            v.page_type,
            v.url_path,
            v.duration_seconds,
            sess.device_type,
            sess.os_name,
            sess.browser_name,
            sess.city,
            sess.traffic_source,
            lead_name
        ])

    return response


# -----------------------------------------------------------------------------
# Classic Error Handlers & Preview Views
# -----------------------------------------------------------------------------

def custom_page_not_found(request, exception=None):
    """Classic 404 Not Found error handler."""
    return render(request, '404.html', status=404)


def custom_permission_denied(request, exception=None):
    """Classic 403 Forbidden error handler."""
    return render(request, '403.html', status=403)


def custom_server_error(request):
    """Classic 500 Internal Server Error handler."""
    return render(request, '500.html', status=500)


def custom_bad_request(request, exception=None):
    """Classic 400 Bad Request error handler."""
    return render(request, '400.html', status=400)


def preview_error_404(request):
    """Developer preview for 404 page."""
    return render(request, '404.html')


def preview_error_403(request):
    """Developer preview for 403 page."""
    return render(request, '403.html')


def preview_error_500(request):
    """Developer preview for 500 page."""
    return render(request, '500.html')


def preview_error_400(request):
    """Developer preview for 400 page."""
    return render(request, '400.html')



