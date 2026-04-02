"""
Add unique constraint on (salesorder, line_no) for SAPSalesorderItem.
Cleans up any duplicate pairs first, then enforces the constraint.
Replaces the old composite index with a UniqueConstraint.
"""
from django.db import migrations, models


def deduplicate_so_items(apps, schema_editor):
    """Remove duplicate (salesorder_id, line_no) rows, keeping the one with revised_price (or highest id)."""
    SAPSalesorderItem = apps.get_model('so', 'SAPSalesorderItem')
    from django.db.models import Count
    dupes = (
        SAPSalesorderItem.objects.values('salesorder_id', 'line_no')
        .annotate(cnt=Count('id'))
        .filter(cnt__gt=1)
    )
    for d in dupes:
        items = list(
            SAPSalesorderItem.objects.filter(
                salesorder_id=d['salesorder_id'], line_no=d['line_no']
            ).order_by('-id')
        )
        keep = items[0]
        for item in items:
            if item.revised_price is not None:
                keep = item
                break
        SAPSalesorderItem.objects.filter(
            salesorder_id=d['salesorder_id'],
            line_no=d['line_no'],
        ).exclude(id=keep.id).delete()


class Migration(migrations.Migration):

    dependencies = [
        ('so', '0103_customer_pending_invoice'),
    ]

    operations = [
        migrations.RunPython(deduplicate_so_items, migrations.RunPython.noop),
        migrations.RemoveIndex(
            model_name='sapsalesorderitem',
            name='so_sapsales_salesor_2556c6_idx',
        ),
        migrations.AddConstraint(
            model_name='sapsalesorderitem',
            constraint=models.UniqueConstraint(
                fields=['salesorder', 'line_no'], name='unique_so_line'
            ),
        ),
    ]
