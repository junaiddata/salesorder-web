from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('emailagent', '0036_upload_customer_salesman_fk'),
    ]

    operations = [
        migrations.AddField(
            model_name='trackedemail',
            name='upload_customer_contact_number',
            field=models.CharField(blank=True, default='', max_length=100),
        ),
        migrations.AddField(
            model_name='trackedemail',
            name='upload_customer_email',
            field=models.CharField(blank=True, default='', max_length=255),
        ),
    ]
