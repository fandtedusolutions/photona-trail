import re
import json
import logging
import requests
from django.conf import settings

logger = logging.getLogger(__name__)


def format_whatsapp_phone(raw_phone):
    """
    Normalizes a phone number to standard format with country code (no leading +).
    Example:
        '8594045760'     -> '918594045760'
        '+91 8594045760' -> '918594045760'
        '08594045760'    -> '918594045760'
        '918594045760'   -> '918594045760'
    """
    if not raw_phone:
        return None

    digits = re.sub(r'\D', '', str(raw_phone))
    if not digits:
        return None

    # 10-digit Indian standard mobile number -> prepend India code 91
    if len(digits) == 10:
        return f"91{digits}"

    # 11-digit starting with 0 (e.g. 08594045760) -> drop 0 and prepend 91
    if len(digits) == 11 and digits.startswith('0'):
        return f"91{digits[1:]}"

    # 12-digit already starting with 91 -> return as is
    if len(digits) == 12 and digits.startswith('91'):
        return digits

    # Other lengths (international numbers entered with country code)
    return digits


def send_whatsapp_template_message(phone, name, link_url, template=None, language=None):
    """
    Sends a template WhatsApp message via Natdemy WhatsApp API.

    API endpoint:
        POST https://wapp.natdemy.com/api/v1/messages/template
    Headers:
        X-API-Key: ...
        X-API-Secret: ...
        Content-Type: application/json
        Accept: application/json
    Payload:
        {
            "phone": "918594045760",
            "name": "Athish",
            "template": "sahodhaya_1",
            "language": "en_US",
            "body_params": [
                "Athish",
                "https://photona.weddinghillage.com/my-photos/<token>/"
            ]
        }
    """
    formatted_phone = format_whatsapp_phone(phone)
    if not formatted_phone:
        logger.warning(f"[WhatsApp] Invalid or missing phone number: {phone}")
        return False, "Invalid phone number"

    api_url = getattr(settings, 'WAPP_API_URL', 'https://wapp.natdemy.com/api/v1/messages/template')
    api_key = getattr(settings, 'WAPP_API_KEY', 'nd_91e5ef319e5084ddbf6576e4549f6206dd250808d29c01d4')
    api_secret = getattr(settings, 'WAPP_API_SECRET', 'nds_93943acac9c5820fca7e7fe2f51387ca6adb0909162a43e3eaa5776c4bf13853')
    template_name = template or getattr(settings, 'WAPP_TEMPLATE_NAME', 'sahodhaya_1')
    language_code = language or getattr(settings, 'WAPP_LANGUAGE', 'en_US')

    contact_name = (name or 'Guest').strip()

    headers = {
        'X-API-Key': api_key,
        'X-API-Secret': api_secret,
        'Content-Type': 'application/json',
        'Accept': 'application/json',
    }

    payload = {
        'phone': formatted_phone,
        'name': contact_name,
        'template': template_name,
        'language': language_code,
        'body_params': [
            contact_name,
            str(link_url)
        ]
    }

    try:
        logger.info(f"[WhatsApp] Sending template '{template_name}' to {formatted_phone} for '{contact_name}'")
        response = requests.post(
            api_url,
            headers=headers,
            json=payload,
            timeout=12
        )
        resp_data = response.json() if response.content else {}

        if response.status_code in (200, 201) and resp_data.get('success', True):
            logger.info(f"[WhatsApp] Message successfully sent to {formatted_phone}: {resp_data.get('message')}")
            return True, resp_data
        else:
            err_msg = resp_data.get('message') or response.text or f"HTTP {response.status_code}"
            logger.error(f"[WhatsApp] Failed to send message to {formatted_phone}: {err_msg}")
            return False, err_msg
    except Exception as e:
        logger.exception(f"[WhatsApp] Exception sending message to {formatted_phone}: {e}")
        return False, str(e)


def send_whatsapp_selfie_link_async(phone, name, link_url, template=None, language=None):
    """
    Dispatches WhatsApp message sending in background.
    Tries Celery task first; falls back to a daemon thread if Celery/broker is not active.
    """
    if not phone or not link_url:
        return False

    try:
        from .tasks import send_whatsapp_template_task
        send_whatsapp_template_task.delay(
            phone=phone,
            name=name,
            link_url=link_url,
            template=template,
            language=language
        )
        return True
    except Exception as celery_err:
        logger.info(f"[WhatsApp] Celery dispatch fallback to thread ({celery_err})")
        import threading
        t = threading.Thread(
            target=send_whatsapp_template_message,
            args=(phone, name, link_url),
            kwargs={'template': template, 'language': language},
            daemon=True
        )
        t.start()
        return True
