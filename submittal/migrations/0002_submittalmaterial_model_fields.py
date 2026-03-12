# Generated manually for model-based fields

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('submittal', '0001_initial'),
    ]

    operations = [
        migrations.RenameField(
            model_name='submittalmaterial',
            old_name='item_code',
            new_name='model_no',
        ),
        migrations.RenameField(
            model_name='submittalmaterial',
            old_name='description',
            new_name='item_description',
        ),
        migrations.AddField(
            model_name='submittalmaterial',
            name='material',
            field=models.CharField(blank=True, default='', help_text='Material (e.g. Bronze, Ductile Iron)', max_length=100),
        ),
        migrations.AddField(
            model_name='submittalmaterial',
            name='pressure_rating',
            field=models.CharField(blank=True, default='', help_text='Pressure rating (e.g. PN20, PN16)', max_length=50),
        ),
        migrations.AddField(
            model_name='submittalmaterial',
            name='area_of_application',
            field=models.CharField(blank=True, default='', help_text='Area of application (e.g. As per approved Drawing)', max_length=255),
        ),
        migrations.AlterField(
            model_name='submittalmaterial',
            name='item_description',
            field=models.CharField(blank=True, default='', help_text='Item Description (e.g. Gate Valve)', max_length=255),
        ),
        migrations.AlterModelOptions(
            name='submittalmaterial',
            options={'ordering': ['display_order', 'item_description']},
        ),
    ]
