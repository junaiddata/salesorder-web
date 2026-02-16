from django.db import models
from django.contrib.auth.models import User
from django.utils.text import slugify
from django.urls import reverse
import os


def get_photo_upload_path(instance, filename):
    """Generate upload path for salesman photos"""
    # Use slug if available, otherwise use name
    identifier = instance.slug if instance.slug else slugify(instance.name)
    return f'businesscards/photos/{identifier}_{filename}'


def get_logo_upload_path(instance, filename):
    """Generate upload path for company logos"""
    return f'businesscards/logos/{filename}'


class SalesmanCard(models.Model):
    """Model for storing salesman digital business card information"""
    
    name = models.CharField(max_length=200, help_text="Full name of the salesman")
    phone = models.CharField(max_length=50, help_text="Phone number")
    email = models.EmailField(help_text="Email address")
    designation = models.CharField(max_length=200, help_text="Job title/designation")
    department = models.CharField(max_length=200, help_text="Department name")
    photo = models.ImageField(
        upload_to=get_photo_upload_path,
        blank=True,
        null=True,
        help_text="Profile photo (optional)"
    )
    slug = models.SlugField(
        max_length=255,
        unique=True,
        blank=True,
        help_text="URL-friendly identifier (auto-generated from name)"
    )
    company_name = models.CharField(
        max_length=200,
        default="Company Name",
        help_text="Company name for branding"
    )
    company_logo = models.ImageField(
        upload_to=get_logo_upload_path,
        blank=True,
        null=True,
        help_text="Company logo (optional)"
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    created_by = models.ForeignKey(
        User,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='created_cards'
    )

    class Meta:
        verbose_name = "Salesman Card"
        verbose_name_plural = "Salesman Cards"
        ordering = ['-created_at']
        indexes = [
            models.Index(fields=['slug']),
            models.Index(fields=['email']),
            models.Index(fields=['department']),
        ]

    def __str__(self):
        return f"{self.name} - {self.designation}"

    def save(self, *args, **kwargs):
        """Auto-generate slug from name if not provided"""
        if not self.slug:
            base_slug = slugify(self.name)
            slug = base_slug
            counter = 1
            # Handle slug collisions
            while SalesmanCard.objects.filter(slug=slug).exclude(pk=self.pk).exists():
                slug = f"{base_slug}-{counter}"
                counter += 1
            self.slug = slug
        super().save(*args, **kwargs)

    def get_absolute_url(self):
        """Get public card URL"""
        return reverse('businesscards:card_public', kwargs={'slug': self.slug})

    def get_vcard_url(self):
        """Get vCard download URL"""
        return reverse('businesscards:vcard_download', kwargs={'slug': self.slug})

    def get_qr_code_path(self):
        """Get QR code file path"""
        return os.path.join('qr', f'{self.slug}.png')

    def get_qr_code_url(self):
        """Get QR code media URL"""
        from django.conf import settings
        return f"{settings.MEDIA_URL}{self.get_qr_code_path()}"
