# Data migration: populate data JSON and brand from legacy fields

from django.db import migrations


PEGLER_COLUMNS = [
    {"key": "model_no", "label": "Model No.", "order": 1},
    {"key": "item_description", "label": "Item Description", "order": 2},
    {"key": "material", "label": "Material", "order": 3},
    {"key": "size", "label": "Size", "order": 4},
    {"key": "wras_number", "label": "WRAS NUMBER", "order": 5},
    {"key": "brand", "label": "BRAND", "order": 6},
    {"key": "pressure_rating", "label": "PRESSURE RATING", "order": 7},
    {"key": "area_of_application", "label": "Area of Application", "order": 8},
]


def create_brands(apps, schema_editor):
    SubmittalBrand = apps.get_model('submittal', 'SubmittalBrand')
    brands = [
        ('Pegler', 'pegler', PEGLER_COLUMNS, 0),
        ('Raktherm', 'raktherm', [], 1),
        ('Cosmoplast', 'cosmoplast', [], 2),
        ('Ariston', 'ariston', [], 3),
        ('Hepworth', 'hepworth', [], 4),
    ]
    for name, code, cols, order in brands:
        SubmittalBrand.objects.get_or_create(code=code, defaults={
            'name': name, 'column_definitions': cols, 'display_order': order,
        })


def migrate_materials(apps, schema_editor):
    SubmittalBrand = apps.get_model('submittal', 'SubmittalBrand')
    SubmittalMaterial = apps.get_model('submittal', 'SubmittalMaterial')
    pegler = SubmittalBrand.objects.filter(code='pegler').first()
    if not pegler:
        return

    for mat in SubmittalMaterial.objects.all():
        mat.data = {
            'item_description': mat.item_description or '',
            'material': mat.material or '',
            'size': mat.size or '',
            'wras_number': mat.wras_number or '',
            'brand': mat.brand_legacy or 'PEGLER - UK',
            'pressure_rating': mat.pressure_rating or '',
            'area_of_application': mat.area_of_application or '',
        }
        mat.brand = pegler
        mat.save(update_fields=['data', 'brand'])


def reverse_migrate(apps, schema_editor):
    SubmittalMaterial = apps.get_model('submittal', 'SubmittalMaterial')
    for mat in SubmittalMaterial.objects.all():
        d = mat.data or {}
        mat.item_description = d.get('item_description', '')
        mat.material = d.get('material', '')
        mat.size = d.get('size', '')
        mat.wras_number = d.get('wras_number', '')
        mat.brand_legacy = d.get('brand', '')
        mat.pressure_rating = d.get('pressure_rating', '')
        mat.area_of_application = d.get('area_of_application', '')
        mat.brand = None
        mat.save(update_fields=['item_description', 'material', 'size', 'wras_number',
                                'brand_legacy', 'pressure_rating', 'area_of_application', 'brand'])


class Migration(migrations.Migration):

    dependencies = [
        ('submittal', '0008_add_brand_and_data'),
    ]

    operations = [
        migrations.RunPython(create_brands, migrations.RunPython.noop),
        migrations.RunPython(migrate_materials, reverse_migrate),
    ]
