from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('bangladeshpi', '0009_packing_list'),
    ]

    operations = [
        migrations.AddField(model_name='bangladeshcommercialinvoice', name='issuing_bank_bin',
                            field=models.CharField(blank=True, max_length=40)),
    ]
