from django.urls import path
from . import views
from . import delivery_order_views
from . import item_analysis_views
from . import customer_analysis_views
from . import sales_analysis_views
from . import salesorder_views

app_name = 'alabama'

urlpatterns = [
    path('', views.home, name='home'),
    path('sales-home/', views.alabama_sales_home, name='alabama_sales_home'),
    path('settings/', views.settings_page, name='settings'),
    path('sales-summary/', views.sales_summary_list, name='sales_summary_list'),
    path('sales-summary/upload/', views.sales_summary_upload, name='sales_summary_upload'),
    path(
        'sales-summary/<str:doc_type_slug>/<str:document_number>/',
        views.sales_summary_detail,
        name='sales_summary_detail',
    ),
    path('sales-analysis/', sales_analysis_views.sales_analysis_dashboard, name='sales_analysis_dashboard'),
    path('purchase-summary/', views.purchase_summary_list, name='purchase_summary_list'),
    path('purchase-summary/upload/', views.purchase_summary_upload, name='purchase_summary_upload'),
    path('purchase-summary/<str:document_number>/', views.purchase_summary_detail, name='purchase_summary_detail'),
    path('quotations/', views.quotation_list, name='quotation_list'),
    path('quotations/upload/', views.quotation_upload, name='quotation_upload'),
    path('quotations/<str:q_number>/', views.quotation_detail, name='quotation_detail'),
    path('sales-orders/', salesorder_views.salesorder_list, name='salesorder_list'),
    path('sales-orders/upload/', salesorder_views.salesorder_upload, name='salesorder_upload'),
    path('sales-orders/<str:so_number>/', salesorder_views.salesorder_detail, name='salesorder_detail'),
    path('delivery-orders/', delivery_order_views.delivery_order_list, name='delivery_order_list'),
    path('delivery-orders/upload/', delivery_order_views.delivery_order_upload, name='delivery_order_upload'),
    path('delivery-orders/<str:do_number>/', delivery_order_views.delivery_order_detail, name='delivery_order_detail'),
    path('item-analysis/', item_analysis_views.item_analysis, name='item_analysis'),
    path('item-analysis/export-pdf/', item_analysis_views.export_item_analysis_pdf, name='export_item_analysis_pdf'),
    path('item-analysis/export-excel/', item_analysis_views.export_item_analysis_excel, name='export_item_analysis_excel'),
    path('customer-analysis/', customer_analysis_views.customer_analysis, name='customer_analysis'),
    path('customer-analysis/export-pdf/', customer_analysis_views.export_customer_analysis_pdf, name='export_customer_analysis_pdf'),
]
