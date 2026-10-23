from django.urls import path

from . import customer_views, email_views, product_views, views

app_name = 'quotation'

urlpatterns = [
    path('', views.quotation_list, name='list'),
    path('new/', views.quotation_form, name='new'),
    path('<int:pk>/edit/', views.quotation_form, name='edit'),
    path('<int:pk>/delete/', views.quotation_delete, name='delete'),
    path('<int:pk>/pdf/', views.quotation_pdf, name='pdf'),
    path('<int:pk>/email/', email_views.quotation_email, name='email'),
    path('photo/<int:item_pk>/', views.item_photo, name='photo'),
    path('customers/search/', views.customer_search, name='customer_search'),
    path('stamp/', views.stamp_settings, name='stamp_settings'),
    # Customer list (imported from Excel; fills the customer details when the name is picked)
    path('customer-list/', customer_views.customer_list, name='customer_list'),
    path('customer-list/new/', customer_views.customer_form, name='customer_new'),
    path('customer-list/template/', customer_views.customer_template, name='customer_template'),
    path('customer-list/import-excel/', customer_views.customer_import_excel, name='customer_import_excel'),
    path('customer-list/<int:pk>/edit/', customer_views.customer_form, name='customer_edit'),
    path('customer-list/<int:pk>/delete/', customer_views.customer_delete, name='customer_delete'),
    # Product library (filled once, picked by Model No in the quotation form)
    path('products/', product_views.product_list, name='product_list'),
    path('products/new/', product_views.product_form, name='product_new'),
    path('products/search/', product_views.product_search, name='product_search'),
    path('products/template/', product_views.product_template, name='product_template'),
    path('products/import-excel/', product_views.product_import_excel, name='product_import_excel'),
    path('products/import-files/', product_views.product_import_files, name='product_import_files'),
    path('products/<int:pk>/edit/', product_views.product_form, name='product_edit'),
    path('products/<int:pk>/delete/', product_views.product_delete, name='product_delete'),
    path('products/<int:pk>/photo/', product_views.product_photo, name='product_photo'),
    path('products/<int:pk>/datasheet/', product_views.product_datasheet, name='product_datasheet'),
    # Public (no login): the datasheet a customer reaches from the PDF's QR code / link.
    path('ds/<str:token>/', views.datasheet_public, name='datasheet'),
]
