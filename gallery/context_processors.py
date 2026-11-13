import os

def analytics_settings(request):
    """Expose Google Analytics ID to templates if configured in .env"""
    return {
        'GOOGLE_ANALYTICS_ID': os.environ.get('GOOGLE_ANALYTICS_ID', '').strip(),
    }

