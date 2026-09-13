from django.shortcuts import redirect
from django.urls import reverse

class TrialEnforcementMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        if not request.user.is_authenticated:
            return self.get_response(request)
            
        if request.user.is_superuser:
            return self.get_response(request)

        # Allow certain paths even if trial is expired
        try:
            allowed_paths = [
                reverse('gallery:user_logout'),
                reverse('gallery:trial_expired'),
            ]
        except Exception:
            allowed_paths = []
        
        # Also allow public share links / API calls for attendees
        if request.path.startswith('/share/') or request.path.startswith('/api/public-photos/'):
            return self.get_response(request)
            
        if request.path in allowed_paths:
            return self.get_response(request)
            
        if hasattr(request.user, 'profile') and request.user.profile:
            if not request.user.profile.is_active_trial:
                return redirect('gallery:trial_expired')
                
        return self.get_response(request)
