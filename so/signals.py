from django.db.models.signals import post_save, post_delete
from django.dispatch import receiver
from django.core.cache import cache
from .models import Items

@receiver([post_save, post_delete], sender=Items)
def clear_firm_items_cache(sender, instance, **kwargs):
    # Clear cache for this firm
    cache.delete(f'items_firm_{instance.item_firm}')
    # Clear cache for "all" if it exists
    cache.delete('items_firm_all')
