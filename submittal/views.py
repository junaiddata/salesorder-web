import json

from django.shortcuts import render, redirect, get_object_or_404
from django.contrib import messages
from django.http import HttpResponse, JsonResponse, FileResponse
from django.contrib.auth.decorators import login_required
from django.views.decorators.http import require_GET
from django.core.files.base import ContentFile
from django.utils import timezone

from .models import (
    Submittal, SubmittalMaterial, SubmittalBrand, ProjectContractorHistory,
    SubmittalSectionUpload,
)
from .forms import TitlePageForm
from .services import get_history_values
from .pdf_builder import build_submittal_pdf, DEFAULT_INDEX_ITEMS, needs_upload


@login_required
def submittal_list(request):
    submittals = Submittal.objects.all()
    return render(request, 'submittal/submittal_list.html', {'submittals': submittals})


@login_required
def submittal_wizard(request, pk=None):
    submittal = get_object_or_404(Submittal, pk=pk) if pk else None
    materials = SubmittalMaterial.objects.select_related('brand').all().order_by('display_order', 'model_no')

    initial_title = {}
    selected_material_ids = []
    existing_index_items = []
    existing_uploads = {}

    if submittal:
        initial_title = {
            'project': submittal.project,
            'client': submittal.client,
            'consultant': submittal.consultant,
            'main_contractor': submittal.main_contractor,
            'mep_contractor': submittal.mep_contractor,
            'product': submittal.product,
        }
        selected_material_ids = list(submittal.materials.values_list('pk', flat=True))
        existing_index_items = submittal.index_items or []
        for up in submittal.section_uploads.all():
            existing_uploads[up.index_label] = up.file.name.split('/')[-1] if up.file else ''

    title_form = TitlePageForm(initial=initial_title)

    history = {
        'project': get_history_values('project'),
        'client': get_history_values('client'),
        'consultant': get_history_values('consultant'),
        'main_contractor': get_history_values('main_contractor'),
        'mep_contractor': get_history_values('mep_contractor'),
        'product': get_history_values('product'),
    }

    existing_materials_columns = submittal.materials_columns if submittal else []
    # Column definitions from all brands (for wizard column selector)
    brands = SubmittalBrand.objects.order_by('display_order')
    all_columns = []
    seen = set()
    for b in brands:
        for col in (b.column_definitions or []):
            k = col.get('key')
            if k and k not in seen:
                seen.add(k)
                all_columns.append({'key': k, 'label': col.get('label', k)})

    context = {
        'submittal': submittal,
        'title_form': title_form,
        'materials': materials,
        'selected_material_ids': json.dumps(selected_material_ids),
        'history': {k: json.dumps(v) for k, v in history.items()},
        'default_index_items': json.dumps(list(DEFAULT_INDEX_ITEMS)),
        'existing_index_items': json.dumps(existing_index_items),
        'existing_uploads': json.dumps(existing_uploads),
        'existing_materials_columns': json.dumps(existing_materials_columns),
        'all_column_definitions': json.dumps(all_columns),
    }
    return render(request, 'submittal/wizard.html', context)


@login_required
def submittal_save(request):
    if request.method != 'POST':
        return redirect('submittal:wizard')

    title_form = TitlePageForm(request.POST)
    if not title_form.is_valid():
        return JsonResponse({'error': 'Title page validation failed', 'errors': title_form.errors}, status=400)

    pk = request.POST.get('submittal_id')
    submittal = get_object_or_404(Submittal, pk=pk) if pk else Submittal()

    submittal.project = title_form.cleaned_data['project']
    submittal.client = title_form.cleaned_data['client']
    submittal.consultant = title_form.cleaned_data['consultant']
    submittal.main_contractor = title_form.cleaned_data['main_contractor']
    submittal.mep_contractor = title_form.cleaned_data['mep_contractor']
    submittal.product = title_form.cleaned_data['product']

    index_items_json = request.POST.get('index_items_json', '')
    if index_items_json:
        try:
            submittal.index_items = json.loads(index_items_json)
        except (ValueError, TypeError):
            pass

    materials_columns_json = request.POST.get('materials_columns_json', '')
    if materials_columns_json:
        try:
            submittal.materials_columns = json.loads(materials_columns_json)
        except (ValueError, TypeError):
            pass

    # Invalidate stored PDF on edit
    if submittal.pk and submittal.generated_pdf:
        submittal.generated_pdf.delete(save=False)
        submittal.generated_pdf = None
        submittal.pdf_generated_at = None

    submittal.save()

    # Materials (M2M)
    material_ids = request.POST.getlist('material_ids')
    if material_ids:
        submittal.materials.set(material_ids)
    else:
        submittal.materials.clear()

    # Section uploads — files keyed by "section_upload_<label>"
    _save_section_uploads(submittal, request.FILES)

    ProjectContractorHistory.objects.create(
        project=submittal.project,
        client=submittal.client,
        consultant=submittal.consultant,
        main_contractor=submittal.main_contractor,
        mep_contractor=submittal.mep_contractor,
        product=submittal.product,
    )

    return JsonResponse({
        'success': True,
        'submittal_id': submittal.pk,
        'redirect': f'/submittal/{submittal.pk}/',
    })


def _save_section_uploads(submittal, files):
    """Save uploaded files into SubmittalSectionUpload, keyed by index label."""
    for key, f in files.items():
        if not key.startswith('section_upload__'):
            continue
        label = key[len('section_upload__'):]
        if not label:
            continue
        obj, _ = SubmittalSectionUpload.objects.get_or_create(
            submittal=submittal, index_label=label,
        )
        if obj.file:
            obj.file.delete(save=False)
        obj.file = f
        obj.save()


def _get_detail_columns(submittal):
    """Return list of (key, label) for detail page materials table."""
    from .pdf_builder import _get_effective_columns
    return _get_effective_columns(submittal)


@login_required
def submittal_detail(request, pk):
    submittal = get_object_or_404(Submittal, pk=pk)
    materials = submittal.materials.select_related('brand').all().order_by('display_order', 'model_no')
    cols = _get_detail_columns(submittal)
    # Build rows: list of value lists (same order as cols)
    materials_rows = []
    for mat in materials:
        row = []
        for key, _ in cols:
            row.append(mat.model_no if key == 'model_no' else mat.get(key, ''))
        materials_rows.append(row)
    return render(request, 'submittal/submittal_detail.html', {
        'submittal': submittal,
        'materials': materials,
        'materials_columns': cols,
        'materials_rows': materials_rows,
    })


def _delete_temp_upload_files(submittal):
    """Delete per-submittal upload files (SubmittalSectionUpload + legacy fields)."""
    for up in submittal.section_uploads.all():
        if up.file and up.file.name:
            try:
                up.file.delete(save=False)
            except Exception:
                pass
        up.delete()

    for field_name in ('vendor_list_pdf', 'comply_statement_file',
                       'area_of_application_pdf', 'warranty_draft_pdf'):
        field = getattr(submittal, field_name)
        if field and field.name:
            try:
                field.delete(save=False)
            except Exception:
                pass
            setattr(submittal, field_name, None)
    submittal.save(update_fields=['vendor_list_pdf', 'comply_statement_file',
                                  'area_of_application_pdf', 'warranty_draft_pdf'])


@login_required
def submittal_generate_pdf(request, pk):
    submittal = get_object_or_404(Submittal, pk=pk)
    force_regenerate = request.GET.get('regenerate') == '1'
    filename_dl = f"Submittal_{submittal.project[:30].replace(' ', '_')}_{submittal.pk}.pdf"

    if not force_regenerate and submittal.generated_pdf and submittal.generated_pdf.name:
        try:
            return FileResponse(
                submittal.generated_pdf.open('rb'),
                content_type='application/pdf',
                as_attachment=True,
                filename=filename_dl,
            )
        except (ValueError, FileNotFoundError, OSError):
            pass

    pdf_buf = build_submittal_pdf(submittal.pk)
    pdf_buf.seek(0)

    stored_name = f"Submittal_{submittal.pk}.pdf"
    submittal.generated_pdf.save(stored_name, ContentFile(pdf_buf.read()), save=True)
    submittal.pdf_generated_at = timezone.now()
    submittal.save(update_fields=['pdf_generated_at'])

    _delete_temp_upload_files(submittal)

    return FileResponse(
        submittal.generated_pdf.open('rb'),
        content_type='application/pdf',
        as_attachment=True,
        filename=filename_dl,
    )


@require_GET
@login_required
def api_materials_search(request):
    q = request.GET.get('q', '').strip()
    ids_param = request.GET.get('ids', '').strip()

    if ids_param:
        ids = [x.strip() for x in ids_param.split(',') if x.strip().isdigit()]
        if ids:
            qs = SubmittalMaterial.objects.select_related('brand').filter(pk__in=ids).order_by('display_order', 'model_no')
        else:
            qs = SubmittalMaterial.objects.none()
    elif len(q) < 2:
        return JsonResponse({'results': []})
    else:
        from django.db.models import Q
        qs = SubmittalMaterial.objects.select_related('brand').filter(
            Q(model_no__icontains=q) |
            Q(brand__name__icontains=q) |
            Q(brand__code__icontains=q)
        ).order_by('display_order', 'model_no')[:20]

    def mat_data(m):
        d = m.data or {}
        return {
            'id': m.pk,
            'model_no': m.model_no,
            'item_description': d.get('item_description', ''),
            'material': d.get('material', ''),
            'size': d.get('size', ''),
            'wras_number': d.get('wras_number', ''),
            'brand': m.brand.name if m.brand else '',
            'pressure_rating': d.get('pressure_rating', ''),
            'area_of_application': d.get('area_of_application', ''),
        }
    results = [mat_data(m) for m in qs]

    return JsonResponse({'results': results})


@login_required
def submittal_items_list(request, brand_code=None):
    """List submittal materials, optionally filtered by brand."""
    from .models import SubmittalBrand
    brands = SubmittalBrand.objects.order_by('display_order', 'name')
    brand = None
    if brand_code:
        brand = get_object_or_404(SubmittalBrand, code=brand_code)
        materials = SubmittalMaterial.objects.filter(brand=brand).order_by('display_order', 'model_no')
    else:
        materials = SubmittalMaterial.objects.select_related('brand').all().order_by('display_order', 'model_no')

    cols = (brand.column_definitions if brand and brand.column_definitions else []) or [
        {'key': 'model_no', 'label': 'Model No.'}, {'key': 'item_description', 'label': 'Item Description'},
        {'key': 'material', 'label': 'Material'}, {'key': 'size', 'label': 'Size'},
        {'key': 'wras_number', 'label': 'WRAS No.'}, {'key': 'brand', 'label': 'Brand'},
        {'key': 'pressure_rating', 'label': 'Pressure Rating'}, {'key': 'area_of_application', 'label': 'Area of Application'},
    ]
    materials_rows = []
    for mat in materials:
        row = []
        for col in cols:
            k = col.get('key', '')
            row.append(mat.model_no if k == 'model_no' else mat.get(k, ''))
        materials_rows.append(row)
    return render(request, 'submittal/items_list.html', {
        'brands': brands,
        'brand': brand,
        'materials': materials,
        'columns': cols,
        'materials_rows': materials_rows,
    })


@login_required
def submittal_items_import(request, brand_code):
    """Import materials from Excel for a brand. GET: show form. POST: process upload."""
    from .models import SubmittalBrand
    import pandas as pd

    brand = get_object_or_404(SubmittalBrand, code=brand_code)
    if not brand.column_definitions:
        return render(request, 'submittal/items_import.html', {
            'brand': brand,
            'error': f'Brand {brand.name} has no column definitions. Configure in Admin.',
        })

    if request.method != 'POST':
        return render(request, 'submittal/items_import.html', {'brand': brand})

    if not request.FILES.get('excel_file'):
        return render(request, 'submittal/items_import.html', {
            'brand': brand,
            'error': 'Please select an Excel file.',
        })

    try:
        df = pd.read_excel(request.FILES['excel_file'], header=0)
    except Exception as e:
        return render(request, 'submittal/items_import.html', {
            'brand': brand,
            'error': f'Invalid Excel file: {e}',
        })

    # Map: our key -> possible Excel header names
    EXCEL_HEADER_MAP = {
        'model_no': ['Model No', 'Model No.', 'model_no', 'ModelNo'],
        'item_description': ['Item Description', 'item_description', 'Item Desc'],
        'material': ['Material', 'material'],
        'size': ['Size', 'size'],
        'wras_number': ['WRAS NUMBER', 'WRAS No', 'wras_number', 'WRAS'],
        'brand': ['BRAND', 'Brand', 'brand'],
        'pressure_rating': ['PRESSURE RATING', 'Pressure Rating', 'pressure_rating'],
        'area_of_application': ['Area of Application', 'area_of_application'],
    }
    excel_cols = {str(c).strip(): c for c in df.columns}

    def find_col(key):
        for name in EXCEL_HEADER_MAP.get(key, [key]):
            for ex, orig in excel_cols.items():
                if name.lower() in ex.lower() or ex.lower() in name.lower():
                    return orig
        return None

    created = updated = 0
    model_col = find_col('model_no') or next((excel_cols.get(c) for c in ['Model No', 'Model No.', 'model_no'] if c in excel_cols), None)
    for _, row in df.iterrows():
        model_no = ''
        if model_col:
            v = row.get(model_col, '')
            if pd.notna(v):
                model_no = str(v).strip()
        if not model_no or model_no == 'nan':
            continue

        data = {}
        for col_def in brand.column_definitions:
            key = col_def.get('key')
            if not key or key == 'model_no':
                continue
            orig = find_col(key)
            if orig:
                val = row.get(orig, '')
                if pd.notna(val):
                    data[key] = str(val).strip()

        mat, created_flag = SubmittalMaterial.objects.update_or_create(
            brand=brand, model_no=model_no,
            defaults={'data': data}
        )
        if created_flag:
            created += 1
        else:
            updated += 1

    messages.success(request, f'Import complete: {created} created, {updated} updated.')
    return redirect('submittal:items_by_brand', brand_code=brand_code)


@require_GET
@login_required
def api_history_suggestions(request):
    field = request.GET.get('field', '')
    valid_fields = ['project', 'client', 'consultant', 'main_contractor', 'mep_contractor', 'product']
    if field not in valid_fields:
        return JsonResponse({'values': []})
    return JsonResponse({'values': get_history_values(field)})
