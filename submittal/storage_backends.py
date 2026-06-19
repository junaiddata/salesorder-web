"""
Storage backends for the submittal app.

The generated submittal PDF is stored in a Supabase S3 bucket (configured via
.env), while every other file (catalogues, certificates, uploads, company docs)
stays on the local filesystem because the PDF builder reads those via `.path`,
which object storage does not support.
"""
from django.conf import settings
from django.core.files.storage import default_storage


def _s3_configured():
    return all([
        getattr(settings, 'AWS_ACCESS_KEY_ID', None),
        getattr(settings, 'AWS_SECRET_ACCESS_KEY', None),
        getattr(settings, 'AWS_STORAGE_BUCKET_NAME', None),
        getattr(settings, 'AWS_S3_ENDPOINT_URL', None),
    ])


def generated_pdf_storage():
    """
    Callable storage for the generated_pdf FileField.

    Returns a Supabase-backed S3 storage when credentials are present in the
    environment, otherwise falls back to the default (local) storage so the app
    keeps working in environments without S3 configured.

    Using a callable keeps credentials out of migration files and avoids
    instantiating boto3 at migration time.
    """
    if not _s3_configured():
        return default_storage

    from storages.backends.s3boto3 import S3Boto3Storage

    class SupabasePDFStorage(S3Boto3Storage):
        bucket_name = settings.AWS_STORAGE_BUCKET_NAME
        endpoint_url = settings.AWS_S3_ENDPOINT_URL
        region_name = getattr(settings, 'AWS_S3_REGION_NAME', None)
        access_key = settings.AWS_ACCESS_KEY_ID
        secret_key = settings.AWS_SECRET_ACCESS_KEY
        # Supabase S3 needs path-style addressing + SigV4
        addressing_style = 'path'
        signature_version = 's3v4'
        # Bucket is private: overwrite same key, serve via signed URLs
        file_overwrite = True
        default_acl = None
        querystring_auth = True
        # Signed URL validity (seconds)
        querystring_expire = 3600

    return SupabasePDFStorage()
