from django.urls import path
from . import views

app_name = 'submittal'

urlpatterns = [
    path('', views.submittal_list, name='list'),
    path('new/', views.submittal_wizard, name='wizard'),
    path('<int:pk>/', views.submittal_detail, name='detail'),
    path('<int:pk>/edit/', views.submittal_wizard, name='edit'),
    path('<int:pk>/delete/', views.submittal_delete, name='delete'),
    path('save/', views.submittal_save, name='save'),
    path('<int:pk>/generate-pdf/', views.submittal_generate_pdf, name='generate_pdf'),

    # Admin panel
    path('admin/', views.admin_index, name='admin_index'),
    path('admin/submittals/', views.admin_submittals, name='admin_submittals'),
    path('admin/items/', views.admin_items, name='admin_items'),
    path('admin/items/<int:pk>/', views.admin_item_detail, name='admin_item_detail'),
    path('admin/brands/', views.admin_brands, name='admin_brands'),
    path('admin/brands/add/', views.admin_brand_add, name='admin_brand_add'),
    path('admin/brands/<int:pk>/edit/', views.admin_brand_edit, name='admin_brand_edit'),
    path('admin/brands/<int:pk>/delete/', views.admin_brand_delete, name='admin_brand_delete'),
    path('admin/company-docs/', views.admin_company_docs, name='admin_company_docs'),

    path('items/', views.submittal_items_list, name='items'),
    path('items/<slug:brand_code>/', views.submittal_items_list, name='items_by_brand'),
    path('items/<slug:brand_code>/import/', views.submittal_items_import, name='items_import'),

    # Settings
    path('settings/', views.submittal_settings, name='settings'),

    # AJAX
    path('api/materials-search/', views.api_materials_search, name='api_materials_search'),
    path('api/history-suggestions/', views.api_history_suggestions, name='api_history_suggestions'),
    path('api/remark-options/', views.api_remark_options, name='api_remark_options'),
]
