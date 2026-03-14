import json

from django.shortcuts import render, redirect, get_object_or_404
from django.http import HttpResponse, JsonResponse, FileResponse
from django.contrib.auth.decorators import login_required
from django.views.decorators.http import require_GET
from django.core.files.base import ContentFile
from django.utils import timezone

from .models import (
    Submittal, SubmittalMaterial, ProjectContractorHistory,
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
    materials = SubmittalMaterial.objects.all().order_by('display_order', 'item_description')

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

    context = {
        'submittal': submittal,
        'title_form': title_form,
        'materials': materials,
        'selected_material_ids': json.dumps(selected_material_ids),
        'history': {k: json.dumps(v) for k, v in history.items()},
        'default_index_items': json.dumps(list(DEFAULT_INDEX_ITEMS)),
        'existing_index_items': json.dumps(existing_index_items),
        'existing_uploads': json.dumps(existing_uploads),
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


@login_required
def submittal_detail(request, pk):
    submittal = get_object_or_404(Submittal, pk=pk)
    materials = submittal.materials.all().order_by('display_order')
    return render(request, 'submittal/submittal_detail.html', {
        'submittal': submittal,
        'materials': materials,
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
    if len(q) < 2:
        return JsonResponse({'results': []})

    from django.db.models import Q
    qs = SubmittalMaterial.objects.filter(
        Q(item_description__icontains=q) |
        Q(model_no__icontains=q) |
        Q(brand__icontains=q) |
        Q(material__icontains=q)
    )[:20]

    results = [{
        'id': m.pk,
        'model_no': m.model_no,
        'item_description': m.item_description,
        'material': m.material,
        'size': m.size,
        'wras_number': m.wras_number,
        'brand': m.brand,
        'pressure_rating': m.pressure_rating,
        'area_of_application': m.area_of_application,
    } for m in qs]

    return JsonResponse({'results': results})


@require_GET
@login_required
def api_history_suggestions(request):
    field = request.GET.get('field', '')
    valid_fields = ['project', 'client', 'consultant', 'main_contractor', 'mep_contractor', 'product']
    if field not in valid_fields:
        return JsonResponse({'values': []})
    return JsonResponse({'values': get_history_values(field)})
