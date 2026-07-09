# Move RemarkOption from brand-level to material (item) level.
# Brand-level remarks are discarded (replaced by item-level remarks).

from django.db import migrations, models
import django.db.models.deletion


def clear_brand_remarks(apps, schema_editor):
    # Existing remarks are brand-scoped and cannot be mapped to a material,
    # so they are removed as part of the move to item-level remarks.
    apps.get_model('submittal', 'RemarkOption').objects.all().delete()


class Migration(migrations.Migration):

    dependencies = [
        ('submittal', '0018_remove_submittalbrand_code'),
    ]

    operations = [
        migrations.RunPython(clear_brand_remarks, migrations.RunPython.noop),
        migrations.AlterModelOptions(
            name='remarkoption',
            options={
                'ordering': ['material', 'display_order', 'label'],
                'verbose_name': 'Remark Option',
                'verbose_name_plural': 'Remark Options',
            },
        ),
        migrations.RemoveField(
            model_name='remarkoption',
            name='brand',
        ),
        migrations.AddField(
            model_name='remarkoption',
            name='material',
            field=models.ForeignKey(
                default=0,
                on_delete=django.db.models.deletion.CASCADE,
                related_name='remark_options',
                to='submittal.submittalmaterial',
                help_text='Material (item) this remark belongs to',
            ),
            preserve_default=False,
        ),
    ]
