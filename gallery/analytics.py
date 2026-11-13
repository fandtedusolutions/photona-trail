"""
Photona Analytics Helper Utilities.
Fast, non-blocking telemetry helpers for parsing user-agents, IP addresses, geolocations, and traffic sources.
"""
import re


def parse_user_agent(ua_string):
    """
    Fast regex-based user-agent parser without external heavy libraries.
    Returns (device_type, os_name, browser_name).
    """
    if not ua_string:
        return 'Desktop', 'Unknown', 'Unknown'

    ua = ua_string.lower()

    # 1. Device Type
    if 'tablet' in ua or 'ipad' in ua or ('android' in ua and 'mobile' not in ua):
        device_type = 'Tablet'
    elif 'mobile' in ua or 'iphone' in ua or 'ipod' in ua or 'android' in ua:
        device_type = 'Mobile'
    else:
        device_type = 'Desktop'

    # 2. Operating System
    if 'iphone' in ua or 'ipad' in ua or 'ipod' in ua:
        os_name = 'iOS'
    elif 'android' in ua:
        os_name = 'Android'
    elif 'mac os' in ua or 'macintosh' in ua:
        os_name = 'macOS'
    elif 'windows' in ua:
        os_name = 'Windows'
    elif 'linux' in ua:
        os_name = 'Linux'
    else:
        os_name = 'Other'

    # 3. Browser
    if 'whatsapp' in ua:
        browser_name = 'WhatsApp In-App'
    elif 'instagram' in ua:
        browser_name = 'Instagram In-App'
    elif 'edg' in ua:
        browser_name = 'Edge'
    elif 'samsungbrowser' in ua:
        browser_name = 'Samsung Internet'
    elif 'chrome' in ua and 'crios' not in ua and 'edg' not in ua:
        browser_name = 'Chrome'
    elif 'crios' in ua:
        browser_name = 'Chrome (iOS)'
    elif 'safari' in ua and 'chrome' not in ua and 'crios' not in ua:
        browser_name = 'Safari'
    elif 'firefox' in ua or 'fxios' in ua:
        browser_name = 'Firefox'
    else:
        browser_name = 'Other'

    return device_type, os_name, browser_name


def detect_traffic_source(request, query_src=None):
    """
    Detect the traffic source from query param, referrer, or direct.
    Returns: 'QR Code', 'WhatsApp', 'Instagram', 'Facebook', 'Google', or 'Direct'.
    """
    if query_src:
        q = str(query_src).lower()
        if 'qr' in q:
            return 'QR Code'
        if 'wa' in q or 'whatsapp' in q:
            return 'WhatsApp'
        if 'insta' in q or 'ig' in q:
            return 'Instagram'
        if 'fb' in q or 'facebook' in q:
            return 'Facebook'

    referer = request.META.get('HTTP_REFERER', '').lower()
    if referer:
        if 'whatsapp' in referer or 'wa.me' in referer:
            return 'WhatsApp'
        if 'instagram' in referer or 'l.instagram.com' in referer:
            return 'Instagram'
        if 'facebook' in referer or 'l.facebook.com' in referer:
            return 'Facebook'
        if 'google.' in referer:
            return 'Google'

    return 'Direct'


def get_client_ip(request):
    """Extract real client IP considering reverse proxies / Cloudflare."""
    x_forwarded_for = request.META.get('HTTP_X_FORWARDED_FOR')
    if x_forwarded_for:
        ip = x_forwarded_for.split(',')[0].strip()
    else:
        ip = request.META.get('REMOTE_ADDR')
    return ip or '127.0.0.1'


def get_client_location(request):
    """
    Extract city and country from reverse proxy headers (e.g. Cloudflare / Nginx).
    Falls back gracefully to 'Local / Unknown' or 'India' if running in development.
    """
    country = request.META.get('HTTP_CF_IPCOUNTRY')
    city = request.META.get('HTTP_CF_IPCITY')

    if not country:
        ip = get_client_ip(request)
        if ip in ['127.0.0.1', 'localhost', '::1']:
            return 'Localhost', 'India'
        country = 'India'

    if not city:
        city = 'Unknown'

    return city, country


def format_duration_display(seconds):
    """
    Format raw seconds into clean, human-friendly duration strings:
    - 0 -> '0s'
    - 46 -> '46s'
    - 60 -> '1m'
    - 234 -> '3m 54s'
    - 3600 -> '1h'
    - 3665 -> '1h 1m'
    - 13469 -> '3h 44m'
    """
    if seconds is None:
        return '0s'
    try:
        seconds = int(round(float(seconds)))
    except (ValueError, TypeError):
        return '0s'

    if seconds <= 0:
        return '0s'

    hours = seconds // 3600
    rem = seconds % 3600
    mins = rem // 60
    secs = rem % 60

    if hours > 0:
        return f"{hours}h {mins}m" if mins > 0 else f"{hours}h"
    if mins > 0:
        return f"{mins}m {secs}s" if secs > 0 else f"{mins}m"
    return f"{secs}s"

