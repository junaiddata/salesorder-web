from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('bangladeshpi', '0008_commercial_invoice'),
    ]

    operations = [
        migrations.AddField(model_name='bangladeshcommercialinvoice', name='pl_no',
                            field=models.CharField(blank=True, max_length=80)),
        migrations.AddField(model_name='bangladeshcommercialinvoice', name='our_delivery',
                            field=models.CharField(blank=True, max_length=60)),
        migrations.AddField(model_name='bangladeshcommercialinvoice', name='ref_our_ord_no',
                            field=models.CharField(blank=True, max_length=60)),
    ]
