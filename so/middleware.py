# so/middleware.py

import uuid
from django.utils import timezone

from .models import Device          # adjust import if needed
from .utils import get_client_ip, parse_device_info  # your helpers

DEVICE_COOKIE_NAME = "qsys_device_id"


class DeviceTrackingMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        # ✅ ALWAYS define it, so views can safely access it
        request.device_obj = None

        try:
            device_id = request.COOKIES.get(DEVICE_COOKIE_NAME)

            if device_id:
                # existing device
                device = Device.objects.filter(id=device_id, is_active=True).first()
                if device:
                    ip = get_client_ip(request)
                    ua_str = request.META.get("HTTP_USER_AGENT", "")[:500]
                    device_type, device_os, device_browser = parse_device_info(ua_str)

                    device.last_ip = ip
                    device.user_agent = ua_str
                    device.device_type = device_type
                    device.device_os = device_os
                    device.device_browser = device_browser
                    device.last_seen = timezone.now()

                    if hasattr(request, "user") and request.user.is_authenticated and device.user is None:
                        device.user = request.user

                    device.save(update_fields=[
                        "last_ip", "user_agent", "device_type",
                        "device_os", "device_browser", "last_seen", "user"
                    ])

                    request.device_obj = device

            else:
                # Only create a Device for AUTHENTICATED users
                if hasattr(request, "user") and request.user.is_authenticated:
                    new_id = uuid.uuid4()
                    ip = get_client_ip(request)
                    ua_str = request.META.get("HTTP_USER_AGENT", "")[:500]
                    device_type, device_os, device_browser = parse_device_info(ua_str)

                    device = Device.objects.create(
                        id=new_id,
                        user=request.user,
                        first_ip=ip,
                        last_ip=ip,
                        user_agent=ua_str,
                        device_type=device_type,
                        device_os=device_os,
                        device_browser=device_browser,
                    )
                    request.device_obj = device
                else:
                    # anonymous visitor – don’t create a device
                    request.device_obj = None

        except Exception:
            # Never break the site because of device tracking
            request.device_obj = None

        # normal request flow
        response = self.get_response(request)

        # Set cookie for new device
        if DEVICE_COOKIE_NAME not in request.COOKIES and request.device_obj:
            max_age = 365 * 24 * 60 * 60  # 1 year
            response.set_cookie(
                DEVICE_COOKIE_NAME,
                str(request.device_obj.id),
                max_age=max_age,
                httponly=True,
                samesite="Lax",
            )

        return response