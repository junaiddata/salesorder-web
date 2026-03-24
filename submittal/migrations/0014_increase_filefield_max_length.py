# Generated manually - increase FileField max_length for long PDF filenames

import submittal.models
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('submittal', '0013_warranty_brand_setup'),
    ]

    operations = [
        migrations.AlterField(
            model_name='materialcertification',
            name='file',
            field=models.FileField(max_length=255, upload_to=submittal.models.material_cert_path),
        ),
        migrations.AlterField(
            model_name='submittalmaterial',
            name='catalogue_pdf',
            field=models.FileField(blank=True, help_text='Product catalogue PDF (Section 9)', max_length=255, null=True, upload_to=submittal.models.catalogue_upload_path),
        ),
        migrations.AlterField(
            model_name='submittalmaterial',
            name='technical_pdf',
            field=models.FileField(blank=True, help_text='Technical details PDF (Section 10)', max_length=255, null=True, upload_to=submittal.models.catalogue_upload_path),
        ),
    ]
