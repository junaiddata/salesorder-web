# Generated manually - add indexes to AlabamaDeliveryOrder

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('alabama', '0005_delivery_order_header_detail'),
    ]

    operations = [
        migrations.AddIndex(
            model_name='alabamadeliveryorder',
            index=models.Index(fields=['date'], name='alabama_ala_date_idx'),
        ),
        migrations.AddIndex(
            model_name='alabamadeliveryorder',
            index=models.Index(fields=['sales_person'], name='alabama_ala_sales_p_idx'),
        ),
    ]
