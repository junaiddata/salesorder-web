import os
import shutil

from django.conf import settings
from django.db import migrations, models

import bangladeshpi.models
import bangladeshpi.storage


def move_seals_to_private(apps, schema_editor):
    """Move any already-uploaded signature/stamp out of the public media folder."""
    Seal = apps.get_model('bangladeshpi', 'BangladeshPISeal')
    private_root = bangladeshpi.storage.PrivateSealStorage().location
    for seal in Seal.objects.all():
        for field in ('signature', 'stamp'):
            name = getattr(seal, field).name
            if not name:
                continue
            src = os.path.join(settings.MEDIA_ROOT, name)
            dst = os.path.join(private_root, name)
            if os.path.isfile(src):
                os.makedirs(os.path.dirname(dst), exist_ok=True)
                shutil.move(src, dst)


class Migration(migrations.Migration):

    dependencies = [
        ('bangladeshpi', '0003_signature_stamp'),
    ]

    operations = [
        migrations.AlterField(
            model_name='bangladeshpiseal',
            name='signature',
            field=models.ImageField(blank=True, null=True,
                                    storage=bangladeshpi.storage.get_private_storage,
                                    upload_to=bangladeshpi.models.signature_upload_to),
        ),
        migrations.AlterField(
            model_name='bangladeshpiseal',
            name='stamp',
            field=models.ImageField(blank=True, null=True,
                                    storage=bangladeshpi.storage.get_private_storage,
                                    upload_to=bangladeshpi.models.stamp_upload_to),
        ),
        migrations.RunPython(move_seals_to_private, migrations.RunPython.noop),
    ]
