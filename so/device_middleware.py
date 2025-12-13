from django.shortcuts import redirect
from django.urls import reverse
from .models import TrustedDevice

class DeviceRestrictionMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        if not request.user.is_authenticated or request.path.startswith('/static/') or request.path.startswith('/media/'):
            return self.get_response(request)

        # Allow registration pages
        allowed_urls = [
            reverse('register_device'),
            reverse('approve_device'), # The form submission
            reverse('device_pending'), # ✅ New "Waiting" page
            reverse('logout'),
            '/admin/',
        ]

        for url in allowed_urls:
            if request.path.startswith(url):
                return self.get_response(request)

        cookie_token = request.COOKIES.get('trusted_device_token')

        if cookie_token:
            try:
                device = TrustedDevice.objects.get(user=request.user, device_token=cookie_token)
                
                # ✅ CHECK 1: Is it approved?
                if device.is_approved:
                    return self.get_response(request)
                
                # ❌ CHECK 2: Token exists but NOT approved yet
                else:
                    return redirect('device_pending')

            except TrustedDevice.DoesNotExist:
                pass # Token in cookie is invalid/deleted from DB

        # No token found -> Go to register
        return redirect('register_device')