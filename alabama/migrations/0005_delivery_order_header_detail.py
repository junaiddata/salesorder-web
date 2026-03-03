# Migration: Refactor Delivery Order to header + detail model

import django.db.models.deletion
from django.db import migrations, models


def migrate_line_to_header_detail(apps, schema_editor):
    """Migrate AlabamaDeliveryOrderLine data to AlabamaDeliveryOrder + AlabamaDeliveryOrderItem."""
    AlabamaDeliveryOrderLine = apps.get_model('alabama', 'AlabamaDeliveryOrderLine')
    AlabamaDeliveryOrder = apps.get_model('alabama', 'AlabamaDeliveryOrder')
    AlabamaDeliveryOrderItem = apps.get_model('alabama', 'AlabamaDeliveryOrderItem')

    # Group lines by (do_number, date, customer, sales_person, city, area, lpo, remarks, invoice)
    from collections import defaultdict
    groups = defaultdict(list)
    for line in AlabamaDeliveryOrderLine.objects.all().order_by('id'):
        key = (
            line.do_number, line.date, line.customer_id,
            line.sales_person or '', line.city or '', line.area or '',
            line.lpo or '', line.remarks or '', line.invoice or ''
        )
        groups[key].append(line)

    for key, lines in groups.items():
        first = lines[0]
        do = AlabamaDeliveryOrder.objects.create(
            do_number=first.do_number,
            date=first.date,
            customer_id=first.customer_id,
            sales_person=first.sales_person or None,
            city=first.city or None,
            area=first.area or None,
            lpo=first.lpo or None,
            remarks=first.remarks or None,
            invoice=first.invoice or None,
        )
        for line in lines:
            AlabamaDeliveryOrderItem.objects.create(
                delivery_order=do,
                item_id=line.item_id,
                item_description=line.item_description,
                quantity=line.quantity,
                price=line.price,
                amount=line.amount,
            )


def reverse_migrate(apps, schema_editor):
    """Reverse: create AlabamaDeliveryOrderLine from header+items."""
    AlabamaDeliveryOrderLine = apps.get_model('alabama', 'AlabamaDeliveryOrderLine')
    AlabamaDeliveryOrder = apps.get_model('alabama', 'AlabamaDeliveryOrder')
    AlabamaDeliveryOrderItem = apps.get_model('alabama', 'AlabamaDeliveryOrderItem')

    for do in AlabamaDeliveryOrder.objects.all():
        for item in AlabamaDeliveryOrderItem.objects.filter(delivery_order=do):
            AlabamaDeliveryOrderLine.objects.create(
                do_number=do.do_number,
                date=do.date,
                customer_id=do.customer_id,
                sales_person=do.sales_person,
                city=do.city,
                area=do.area,
                lpo=do.lpo,
                remarks=do.remarks,
                invoice=do.invoice,
                item_id=item.item_id,
                item_description=item.item_description,
                quantity=item.quantity,
                price=item.price,
                amount=item.amount,
            )


class Migration(migrations.Migration):

    dependencies = [
        ('alabama', '0004_add_delivery_order'),
        ('so', '0088_set_alabama_users_company'),
    ]

    operations = [
        migrations.CreateModel(
            name='AlabamaDeliveryOrder',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('do_number', models.CharField(db_index=True, help_text='DO number', max_length=100, unique=True)),
                ('date', models.DateField(db_index=True)),
                ('sales_person', models.CharField(blank=True, max_length=255, null=True)),
                ('city', models.CharField(blank=True, max_length=255, null=True)),
                ('area', models.CharField(blank=True, max_length=255, null=True)),
                ('lpo', models.CharField(blank=True, max_length=255, null=True)),
                ('remarks', models.TextField(blank=True, null=True)),
                ('invoice', models.CharField(blank=True, max_length=100, null=True)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('customer', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='alabama_delivery_orders', to='so.customer')),
            ],
            options={
                'ordering': ['-date', '-do_number'],
            },
        ),
        migrations.CreateModel(
            name='AlabamaDeliveryOrderItem',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('item_description', models.CharField(blank=True, max_length=500, null=True)),
                ('quantity', models.DecimalField(decimal_places=2, default=0, max_digits=12)),
                ('price', models.DecimalField(decimal_places=2, default=0, max_digits=12)),
                ('amount', models.DecimalField(decimal_places=2, default=0, max_digits=12)),
                ('delivery_order', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='items', to='alabama.alabamadeliveryorder')),
                ('item', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='alabama_delivery_order_items', to='so.items')),
            ],
        ),
        migrations.RunPython(migrate_line_to_header_detail, reverse_migrate),
        migrations.DeleteModel(name='AlabamaDeliveryOrderLine'),
    ]
