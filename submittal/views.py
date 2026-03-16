import json

from django.shortcuts import render, redirect, get_object_or_404
from django.contrib import messages
from django.http import HttpResponse, JsonResponse, FileResponse
from django.contrib.auth.decorators import login_required
from django.views.decorators.http import require_GET, require_POST
from django.core.files.base import ContentFile
from django.utils import timezone

from .models import (
    Submittal, SubmittalMaterial, SubmittalBrand, ProjectContractorHistory,
    SubmittalSectionUpload, ComplianceOption, RemarkOption,
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

    existing_compliance_rows = []
    existing_compliance_brand_id = None

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
        existing_compliance_rows = submittal.compliance_rows or []
        existing_compliance_brand_id = submittal.compliance_brand_id

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
    existing_warranty_brand_id = submittal.warranty_brand_id if submittal else None
    existing_warranty_date_type = getattr(submittal, 'warranty_date_type', 'toc') or 'toc' if submittal else 'toc'
    existing_warranty_materials_columns = submittal.warranty_materials_columns if submittal else []
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

    compliance_options = list(ComplianceOption.objects.values('pk', 'label'))
    brands = SubmittalBrand.objects.order_by('display_order', 'name')
    remark_options_by_brand = {
        b.pk: list(b.remark_options.values('pk', 'label'))
        for b in brands
    }
    warranty_brands_with_format = [b.pk for b in brands if getattr(b, 'use_generated_warranty', False)]

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
        # Compliance statement
        'brands_json': json.dumps([{'pk': b.pk, 'name': b.name} for b in brands]),
        'compliance_options_json': json.dumps(compliance_options),
        'remark_options_by_brand_json': json.dumps(remark_options_by_brand),
        'existing_compliance_rows_json': json.dumps(existing_compliance_rows),
        'existing_compliance_brand_id': existing_compliance_brand_id or '',
        # Warranty section
        'existing_warranty_brand_id': existing_warranty_brand_id or '',
        'existing_warranty_date_type': existing_warranty_date_type,
        'existing_warranty_materials_columns': json.dumps(existing_warranty_materials_columns),
        'warranty_brands_with_format': json.dumps(warranty_brands_with_format),
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

    compliance_rows_json = request.POST.get('compliance_rows_json', '')
    if compliance_rows_json:
        try:
            submittal.compliance_rows = json.loads(compliance_rows_json)
        except (ValueError, TypeError):
            submittal.compliance_rows = []
    else:
        submittal.compliance_rows = []

    compliance_brand_id = request.POST.get('compliance_brand_id', '')
    if compliance_brand_id and compliance_brand_id.isdigit():
        submittal.compliance_brand_id = int(compliance_brand_id)
    else:
        submittal.compliance_brand_id = None

    # Warranty section
    warranty_brand_id = request.POST.get('warranty_brand_id', '')
    if warranty_brand_id and warranty_brand_id.isdigit():
        submittal.warranty_brand_id = int(warranty_brand_id)
    else:
        submittal.warranty_brand_id = None
    warranty_date_type = request.POST.get('warranty_date_type', 'toc')
    if warranty_date_type in ('toc', 'invoice'):
        submittal.warranty_date_type = warranty_date_type
    warranty_materials_columns_json = request.POST.get('warranty_materials_columns_json', '')
    if warranty_materials_columns_json:
        try:
            submittal.warranty_materials_columns = json.loads(warranty_materials_columns_json)
        except (ValueError, TypeError):
            submittal.warranty_materials_columns = []
    else:
        submittal.warranty_materials_columns = []

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
            'brand': m.get('brand') or (m.brand.name if m.brand else ''),
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
    materials_with_rows = []
    for mat in materials:
        row = []
        for col in cols:
            k = col.get('key', '')
            row.append(mat.model_no if k == 'model_no' else mat.get(k, ''))
        materials_with_rows.append((mat, row))
    return render(request, 'submittal/items_list.html', {
        'brands': brands,
        'brand': brand,
        'materials_with_rows': materials_with_rows,
        'columns': cols,
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


# ---------------------------------------------------------------------------
# Compliance Settings page
# ---------------------------------------------------------------------------

@login_required
def submittal_settings(request):
    """In-app settings page for Compliance Options and Remark Options per brand."""
    from .models import ComplianceOption, RemarkOption

    if request.method == 'POST':
        action = request.POST.get('action', '')

        # ── Compliance Options ──
        if action == 'add_compliance':
            label = request.POST.get('label', '').strip()
            if label:
                order = ComplianceOption.objects.count()
                ComplianceOption.objects.get_or_create(label=label, defaults={'display_order': order})
            return redirect('submittal:settings')

        if action == 'delete_compliance':
            pk = request.POST.get('pk')
            if pk:
                ComplianceOption.objects.filter(pk=pk).delete()
            return redirect('submittal:settings')

        if action == 'reorder_compliance':
            order_data = request.POST.getlist('order[]')
            for i, pk in enumerate(order_data):
                ComplianceOption.objects.filter(pk=pk).update(display_order=i)
            return JsonResponse({'ok': True})

        # ── Remark Options ──
        if action == 'add_remark':
            brand_id = request.POST.get('brand_id')
            label = request.POST.get('label', '').strip()
            if brand_id and label:
                order = RemarkOption.objects.filter(brand_id=brand_id).count()
                RemarkOption.objects.create(brand_id=brand_id, label=label, display_order=order)
            return redirect('submittal:settings')

        if action == 'delete_remark':
            pk = request.POST.get('pk')
            if pk:
                RemarkOption.objects.filter(pk=pk).delete()
            return redirect('submittal:settings')

        if action == 'reorder_remark':
            order_data = request.POST.getlist('order[]')
            for i, pk in enumerate(order_data):
                RemarkOption.objects.filter(pk=pk).update(display_order=i)
            return JsonResponse({'ok': True})

    brands = SubmittalBrand.objects.order_by('display_order', 'name')
    compliance_options = ComplianceOption.objects.all()
    remark_options_by_brand = {
        b.pk: list(b.remark_options.values('pk', 'label', 'display_order'))
        for b in brands
    }

    return render(request, 'submittal/settings.html', {
        'brands': brands,
        'compliance_options': compliance_options,
        'remark_options_by_brand': json.dumps(remark_options_by_brand),
    })


@require_GET
@login_required
def api_remark_options(request):
    """Return remark options for a given brand_id."""
    from .models import RemarkOption
    brand_id = request.GET.get('brand_id')
    if not brand_id:
        return JsonResponse({'options': []})
    opts = list(RemarkOption.objects.filter(brand_id=brand_id).values('pk', 'label'))
    return JsonResponse({'options': opts})


# ---------------------------------------------------------------------------
# In-App Admin Panel
# ---------------------------------------------------------------------------

@login_required
def admin_index(request):
    """Admin dashboard."""
    submittal_count = Submittal.objects.count()
    material_count = SubmittalMaterial.objects.count()
    brand_count = SubmittalBrand.objects.count()
    return render(request, 'submittal/admin_index.html', {
        'submittal_count': submittal_count,
        'material_count': material_count,
        'brand_count': brand_count,
    })


@login_required
def admin_submittals(request):
    """Admin submittals list with search and delete."""
    q = request.GET.get('q', '').strip()
    submittals = Submittal.objects.all().order_by('-created_at')
    if q:
        from django.db.models import Q
        submittals = submittals.filter(
            Q(project__icontains=q) | Q(client__icontains=q) | Q(product__icontains=q)
        )
    return render(request, 'submittal/admin_submittals.html', {
        'submittals': submittals,
        'search_q': q,
    })


@require_POST
@login_required
def submittal_delete(request, pk):
    """Delete submittal and its generated PDF. Called via POST with JS confirmation."""
    submittal = get_object_or_404(Submittal, pk=pk)
    project_name = (submittal.project or '')[:50]
    if submittal.generated_pdf and submittal.generated_pdf.name:
        try:
            submittal.generated_pdf.delete(save=False)
        except Exception:
            pass
    _delete_temp_upload_files(submittal)
    submittal.delete()
    messages.success(request, f'Submittal "{project_name}..." has been deleted.')
    return redirect('submittal:admin_submittals')


@login_required
def admin_items(request):
    """Admin materials list - redirect to items with brand filter or show all."""
    brands = SubmittalBrand.objects.order_by('display_order', 'name')
    brand_code = request.GET.get('brand', '')
    if brand_code:
        brand = get_object_or_404(SubmittalBrand, code=brand_code)
        materials = SubmittalMaterial.objects.filter(brand=brand).order_by('display_order', 'model_no')
    else:
        brand = None
        materials = SubmittalMaterial.objects.select_related('brand').all().order_by('display_order', 'model_no')

    cols = (brand.column_definitions if brand and brand.column_definitions else []) or [
        {'key': 'model_no', 'label': 'Model No.'}, {'key': 'item_description', 'label': 'Item Description'},
        {'key': 'material', 'label': 'Material'}, {'key': 'size', 'label': 'Size'},
        {'key': 'wras_number', 'label': 'WRAS No.'}, {'key': 'brand', 'label': 'Brand'},
    ]
    materials_with_rows = []
    for mat in materials:
        row = []
        for col in cols:
            k = col.get('key', '')
            row.append(mat.model_no if k == 'model_no' else mat.get(k, ''))
        materials_with_rows.append((mat, row))

    return render(request, 'submittal/admin_items.html', {
        'brands': brands,
        'brand': brand,
        'materials_with_rows': materials_with_rows,
        'columns': cols,
    })


@login_required
def admin_item_detail(request, pk):
    """Item/material detail page with uploads for catalogue, technical PDF, and certifications."""
    from .models import MaterialCertification

    material = get_object_or_404(SubmittalMaterial, pk=pk)
    material.brand  # ensure prefetch

    if request.method == 'POST':
        action = request.POST.get('action', '')
        f = request.FILES.get('file')

        if action == 'catalogue' and f:
            if material.catalogue_pdf and material.catalogue_pdf.name:
                material.catalogue_pdf.delete(save=False)
            material.catalogue_pdf = f
            material.save()
            messages.success(request, 'Catalogue PDF updated.')

        elif action == 'technical' and f:
            if material.technical_pdf and material.technical_pdf.name:
                material.technical_pdf.delete(save=False)
            material.technical_pdf = f
            material.save()
            messages.success(request, 'Technical PDF updated.')

        elif action == 'cert' and f:
            cert_type = request.POST.get('cert_type', '')
            if cert_type in ['test_certificate', 'country_of_origin', 'previous_approval']:
                MaterialCertification.objects.create(
                    material=material,
                    cert_type=cert_type,
                    file=f,
                    description=request.POST.get('description', '')[:255],
                )
                messages.success(request, f'{dict(MaterialCertification.CERT_TYPE_CHOICES).get(cert_type)} added.')

        elif action == 'delete_cert':
            cert_id = request.POST.get('cert_id')
            if cert_id:
                cert = MaterialCertification.objects.filter(pk=cert_id, material=material).first()
                if cert:
                    if cert.file and cert.file.name:
                        cert.file.delete(save=False)
                    cert.delete()
                    messages.success(request, 'Certificate removed.')

        return redirect('submittal:admin_item_detail', pk=pk)

    certifications = material.certifications.all().order_by('cert_type', 'uploaded_at')
    cols = (material.brand.column_definitions or []) or [
        {'key': 'model_no', 'label': 'Model No.'}, {'key': 'item_description', 'label': 'Item Description'},
        {'key': 'material', 'label': 'Material'}, {'key': 'size', 'label': 'Size'},
    ]
    detail_rows = []
    for col in cols:
        k = col.get('key', '')
        val = material.model_no if k == 'model_no' else material.get(k, '')
        detail_rows.append({'label': col.get('label', k), 'value': val})

    return render(request, 'submittal/admin_item_detail.html', {
        'material': material,
        'certifications': certifications,
        'detail_rows': detail_rows,
    })


@login_required
def admin_brands(request):
    """Admin brands list and CRUD."""
    brands = SubmittalBrand.objects.order_by('display_order', 'name')
    return render(request, 'submittal/admin_brands.html', {'brands': brands})


@login_required
def admin_brand_add(request):
    """Add new brand."""
    if request.method == 'POST':
        name = request.POST.get('name', '').strip()
        code = request.POST.get('code', '').strip().upper().replace(' ', '_')
        if name and code:
            if SubmittalBrand.objects.filter(code=code).exists():
                messages.error(request, f'Brand with code "{code}" already exists.')
            else:
                use_generated_warranty = request.POST.get('use_generated_warranty') == 'on'
                SubmittalBrand.objects.create(name=name, code=code, use_generated_warranty=use_generated_warranty)
                messages.success(request, f'Brand "{name}" added.')
                return redirect('submittal:admin_brands')
        else:
            messages.error(request, 'Name and code are required.')
    return render(request, 'submittal/admin_brand_form.html', {'brand': None})


@login_required
def admin_brand_edit(request, pk):
    """Edit brand."""
    brand = get_object_or_404(SubmittalBrand, pk=pk)
    if request.method == 'POST':
        name = request.POST.get('name', '').strip()
        code = request.POST.get('code', '').strip().upper().replace(' ', '_')
        if name and code:
            if SubmittalBrand.objects.filter(code=code).exclude(pk=pk).exists():
                messages.error(request, f'Brand with code "{code}" already exists.')
            else:
                brand.name = name
                brand.code = code
                brand.use_generated_warranty = request.POST.get('use_generated_warranty') == 'on'
                brand.save()
                messages.success(request, f'Brand "{name}" updated.')
                return redirect('submittal:admin_brands')
        else:
            messages.error(request, 'Name and code are required.')
    return render(request, 'submittal/admin_brand_form.html', {'brand': brand})


@login_required
def admin_brand_delete(request, pk):
    """Delete brand (only if no materials)."""
    if request.method != 'POST':
        return redirect('submittal:admin_brands')
    brand = get_object_or_404(SubmittalBrand, pk=pk)
    if brand.materials.exists():
        messages.error(request, f'Cannot delete "{brand.name}" - it has materials. Delete materials first.')
        return redirect('submittal:admin_brands')
    name = brand.name
    brand.delete()
    messages.success(request, f'Brand "{name}" deleted.')
    return redirect('submittal:admin_brands')


@login_required
def admin_company_docs(request):
    """Manage company documents (profile, trade license)."""
    import os
    from .models import CompanyDocuments
    docs = CompanyDocuments.get_instance()
    if request.method == 'POST':
        field = request.POST.get('field', '')
        f = request.FILES.get('file')
        if field and f:
            valid = ['company_profile_pdf', 'trade_license_pdf', 'index_standard_pdf']
            if field in valid:
                old = getattr(docs, field)
                if old and old.name:
                    old.delete(save=False)
                setattr(docs, field, f)
                docs.save()
                messages.success(request, f'Updated {field.replace("_", " ").title()}.')
        return redirect('submittal:admin_company_docs')

    def _basename(ff):
        return os.path.basename(ff.name) if ff and ff.name else None

    upload_fields = [
        ('company_profile_pdf', 'Company Profile PDF', _basename(docs.company_profile_pdf)),
        ('trade_license_pdf', 'Trade License PDF', _basename(docs.trade_license_pdf)),
        ('index_standard_pdf', 'Index Standard PDF', _basename(docs.index_standard_pdf)),
    ]
    return render(request, 'submittal/admin_company_docs.html', {
        'docs': docs,
        'upload_fields': upload_fields,
        'company_profile_name': _basename(docs.company_profile_pdf),
        'trade_license_name': _basename(docs.trade_license_pdf),
        'index_standard_name': _basename(docs.index_standard_pdf),
    })
