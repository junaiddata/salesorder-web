from django.db import migrations, models

import bangladeshpi.models


class Migration(migrations.Migration):

    dependencies = [
        ('bangladeshpi', '0002_bank_copy'),
    ]

    operations = [
        migrations.AddField(
            model_name='bangladeshpi',
            name='add_signature',
            field=models.BooleanField(default=False),
        ),
        migrations.AddField(
            model_name='bangladeshpi',
            name='add_stamp',
            field=models.BooleanField(default=False),
        ),
        migrations.CreateModel(
            name='BangladeshPISeal',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('signature', models.ImageField(blank=True, null=True, upload_to=bangladeshpi.models.signature_upload_to)),
                ('stamp', models.ImageField(blank=True, null=True, upload_to=bangladeshpi.models.stamp_upload_to)),
            ],
        ),
    ]
