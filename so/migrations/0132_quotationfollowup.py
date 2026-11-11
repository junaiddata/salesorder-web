import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('so', '0131_sapquotation_customer_phone'),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name='QuotationFollowUp',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('note', models.TextField()),
                ('outcome', models.CharField(blank=True, choices=[('PENDING', 'Pending'), ('WAITING', 'Waiting for customer'), ('NEGOTIATING', 'Negotiating'), ('WON', 'Won'), ('LOST', 'Lost')], default='', max_length=20)),
                ('next_followup_date', models.DateField(blank=True, null=True)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('created_by', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='quotation_followups', to=settings.AUTH_USER_MODEL)),
                ('quotation', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='followups', to='so.quotation')),
            ],
            options={
                'ordering': ['-created_at', '-id'],
            },
        ),
    ]
