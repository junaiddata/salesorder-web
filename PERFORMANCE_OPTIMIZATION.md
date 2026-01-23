# Performance Optimization Guide for Frequent Sales Order Sync (5-10 minutes)

## Current Sync Strategy
- Fetch open orders (1 API call with pagination)
- Fetch last 3 days (3 API calls with pagination) - today + last 3 days = 4 days total
- Total: ~4 API calls per sync

## Recommended Optimizations

### 1. **Database Indexes** (CRITICAL - Do First)

Add missing indexes to improve query performance:

```python
# In models.py - SAPSalesorderItem
class Meta:
    indexes = [
        models.Index(fields=['salesorder', 'line_no']),  # Already exists
        models.Index(fields=['item_no']),  # ADD THIS - for manufacturer lookup
        models.Index(fields=['pending_amount']),  # ADD THIS - for pending_total calculation
        models.Index(fields=['row_status']),  # ADD THIS - for status filtering
    ]

# In models.py - SAPSalesorder
class Meta:
    indexes = [
        models.Index(fields=["posting_date"]),  # Already exists
        models.Index(fields=["salesman_name"]),  # Already exists
        models.Index(fields=["customer_name"]),  # Already exists
        models.Index(fields=["status"]),  # Already exists
        models.Index(fields=["so_number"]),  # ADD THIS - unique but index helps
        models.Index(fields=["customer_code"]),  # ADD THIS - for filtering
        models.Index(fields=["created_at"]),  # ADD THIS - for sorting
    ]
```

**Migration Command:**
```bash
python manage.py makemigrations
python manage.py migrate
```

### 2. **Incremental Sync Strategy** (HIGH IMPACT)

Instead of fetching all open orders every time, track last sync time:

```python
# Add to SAPSalesorder model
last_synced_at = models.DateTimeField(null=True, blank=True)

# Modify sync strategy:
# - First sync: Fetch all open orders + last 5 days
# - Subsequent syncs: Only fetch orders modified since last_synced_at
# - Still fetch last 5 days for new orders
```

**Benefits:**
- Reduces API calls from 6 to 2-3 per sync
- Faster processing (fewer orders to process)
- Less database load

### 3. **Optimize Manufacturer Lookup** (MEDIUM IMPACT)

Cache manufacturer lookups to avoid repeated database queries:

```python
# In api_client.py - Add caching
from django.core.cache import cache

def _get_manufacturer_from_item_code(self, item_code: str) -> str:
    """Looks up manufacturer from Items model by item_code with caching."""
    if not item_code:
        return ""
    
    # Check cache first (5 minute TTL)
    cache_key = f"item_manufacturer_{item_code}"
    cached = cache.get(cache_key)
    if cached is not None:
        return cached
    
    try:
        item = Items.objects.only('item_firm').get(item_code=item_code)
        manufacturer = item.item_firm or ""
        # Cache for 5 minutes
        cache.set(cache_key, manufacturer, 300)
        return manufacturer
    except Items.DoesNotExist:
        logger.debug(f"Item not found: {item_code}")
        cache.set(cache_key, "", 300)  # Cache miss to avoid repeated lookups
        return ""
```

### 4. **Optimize Bulk Operations** (MEDIUM IMPACT)

Current batch sizes are good, but we can optimize:

```python
# In sync_salesorders_api_receive
# Increase batch sizes for better performance
SAPSalesorder.objects.bulk_create(to_create, batch_size=5000)  # Was 2000
SAPSalesorder.objects.bulk_update(to_update, fields=update_fields, batch_size=5000)  # Was 2000
SAPSalesorderItem.objects.bulk_create(items_to_create, batch_size=20000)  # Was 10000
```

### 5. **Use select_related/prefetch_related** (LOW-MEDIUM IMPACT)

Optimize queries in views:

```python
# In salesorder_list view
qs = SAPSalesorder.objects.select_related().prefetch_related('items').filter(...)

# This reduces database queries when accessing related items
```

### 6. **Connection Pooling** (MEDIUM IMPACT)

Configure database connection pooling in settings.py:

```python
# For MySQL/MariaDB
DATABASES = {
    'default': {
        # ... existing config ...
        'CONN_MAX_AGE': 600,  # Keep connections alive for 10 minutes
        'OPTIONS': {
            'init_command': "SET sql_mode='STRICT_TRANS_TABLES'",
            'charset': 'utf8mb4',
        }
    }
}
```

### 7. **Selective Field Updates** (MEDIUM IMPACT)

Only update fields that actually changed:

```python
# Track which fields changed and only update those
# This reduces database write operations
```

### 8. **Reduce Logging in Production** (LOW IMPACT)

Set logging level to WARNING in production:

```python
# In settings.py
LOGGING = {
    'version': 1,
    'disable_existing_loggers': False,
    'handlers': {
        'console': {
            'class': 'logging.StreamHandler',
        },
    },
    'root': {
        'handlers': ['console'],
        'level': 'WARNING',  # Change from INFO/DEBUG
    },
}
```

### 9. **Background Task Processing** (HIGH IMPACT - Optional)

Use Celery for async processing:

```python
# Install: pip install celery
# Create tasks.py
from celery import shared_task

@shared_task
def sync_salesorders_async():
    # Run sync in background
    # This prevents blocking the main process
    pass
```

### 10. **Database Query Optimization** (MEDIUM IMPACT)

Optimize the pending_total annotation:

```python
# Use Subquery instead of annotation for better performance
from django.db.models import Subquery, OuterRef

pending_total_subquery = SAPSalesorderItem.objects.filter(
    salesorder=OuterRef('pk')
).aggregate(
    total=Sum('pending_amount')
)['total']

qs = qs.annotate(
    pending_total=Coalesce(
        Subquery(pending_total_subquery),
        Value(0, output_field=DecimalField())
    )
)
```

## Implementation Priority

### Phase 1 (Immediate - Do Now):
1. ✅ Add database indexes (Migration)
2. ✅ Optimize manufacturer lookup with caching
3. ✅ Increase batch sizes

### Phase 2 (Short-term - This Week):
4. ✅ Implement incremental sync strategy
5. ✅ Add connection pooling
6. ✅ Optimize queries with select_related

### Phase 3 (Long-term - Optional):
7. ✅ Background task processing (Celery)
8. ✅ Advanced query optimizations

## Monitoring

Add performance metrics:

```python
import time
from django.core.cache import cache

def sync_with_metrics():
    start_time = time.time()
    # ... sync logic ...
    duration = time.time() - start_time
    
    # Store metrics
    cache.set('last_sync_duration', duration, 3600)
    cache.set('last_sync_time', time.time(), 3600)
```

## Expected Performance Improvements

- **Before**: ~30-60 seconds per sync (depending on data volume)
- **After Phase 1**: ~15-30 seconds per sync
- **After Phase 2**: ~5-10 seconds per sync (with incremental sync)

## Testing

Test with:
1. Small dataset (100 orders)
2. Medium dataset (1000 orders)
3. Large dataset (10000+ orders)

Monitor:
- Database query count
- API call count
- Total sync duration
- Memory usage
