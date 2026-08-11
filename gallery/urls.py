from django.urls import path
from . import views

app_name = 'gallery'

urlpatterns = [
    # Authentication
    path('login/', views.user_login, name='user_login'),
    path('logout/', views.user_logout, name='user_logout'),

    # Dashboards
    path('', views.dashboard, name='dashboard'),
    path('super-admin/', views.super_admin_dashboard, name='super_admin_dashboard'),
    
    # Super Admin Actions
    path('super-admin/create-admin/', views.create_admin, name='create_admin'),
    path('super-admin/delete-admin/<int:user_id>/', views.delete_admin, name='delete_admin'),
    path('super-admin/update-admin/<int:user_id>/', views.update_admin, name='update_admin'),
    path('super-admin/create-plan/', views.create_plan, name='create_plan'),
    path('super-admin/update-plan/<int:plan_id>/', views.update_plan, name='update_plan'),
    path('super-admin/delete-plan/<int:plan_id>/', views.delete_plan, name='delete_plan'),

    # Event CRUD (Tenant)
    path('event/create/', views.create_event, name='create_event'),
    path('event/update/<slug:slug>/', views.update_event, name='update_event'),
    path('event/delete/<slug:slug>/', views.delete_event, name='delete_event'),
    path('event/<slug:slug>/', views.event_detail, name='event_detail'),
    path('event/<slug:slug>/group/rename/', views.rename_face_group, name='rename_face_group'),
    path('event/<slug:slug>/group/delete/', views.delete_face_group, name='delete_face_group'),

    # Event Share Links (Organizer CRUD)
    path('event/<slug:slug>/shares/', views.manage_shares, name='manage_shares'),
    path('event/<slug:slug>/shares/create/', views.create_share, name='create_share'),
    path('event/<slug:slug>/shares/<int:share_id>/update/', views.update_share, name='update_share'),
    path('event/<slug:slug>/shares/<int:share_id>/delete/', views.delete_share, name='delete_share'),

    # Public Share Views (no login required)
    path('share/<uuid:token>/', views.public_event, name='public_event'),
    path('share/<uuid:token>/auth/', views.public_event_auth, name='public_event_auth'),
    path('api/public-photos/', views.public_photos_api, name='public_photos_api'),

    # Core Upload & Search actions
    path('upload/', views.upload_photos, name='upload'),
    path('search/', views.search_person, name='search'),
    path('delete/<int:image_id>/', views.delete_image, name='delete_image'),
    path('gdrive-import/', views.gdrive_import, name='gdrive_import'),
    path('api/photos/', views.photos_api, name='photos_api'),
    path('group-by-face/', views.group_by_face, name='group_by_face'),
    path('event/<slug:slug>/download/', views.download_event_zip, name='download_event_zip'),
    path('download-zip/', views.download_images_zip, name='download_images_zip'),
]
