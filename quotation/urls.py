from django.urls import path

from . import product_views, views

app_name = 'quotation'

urlpatterns = [
    path('', views.quotation_list, name='list'),
    path('new/', views.quotation_form, name='new'),
    path('<int:pk>/edit/', views.quotation_form, name='edit'),
    path('<int:pk>/delete/', views.quotation_delete, name='delete'),
    path('<int:pk>/pdf/', views.quotation_pdf, name='pdf'),
    path('photo/<int:item_pk>/', views.item_photo, name='photo'),
    path('stamp/', views.stamp_settings, name='stamp_settings'),
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
    # Public (no login): the datasheet a customer reaches from the PDF's QR code / link.
    path('ds/<str:token>/', views.datasheet_public, name='datasheet'),
]
