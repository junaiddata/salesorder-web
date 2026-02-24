from django.urls import path
from . import views
from so.views import *
from . import views_quotation
from . import sap_salesorder_views
from . import sap_purchaseorder_views
from . import sap_purchaseorder_pdf_export
from . import finance_statement_views
from . import finance_statement_pdf_export
from . import customer_analysis_views
from . import credit_memo_analysis_views
from . import purchase_stock_requirement_views
from . import quotation_item_analysis_views
from . import item_quoted_analysis_pdf_export

urlpatterns = [
    path('upload-items/', views.upload_items, name='upload_items'),
    path('upload-customers/', views.upload_customers, name='upload_customers'),
    # path('create-sales-order/', views.create_sales_order, name='create_sales_order'),
    path('create/', views.create_sales_order, name='create_sales_order'),
    path('get_items_by_firm/', views.get_items_by_firm, name='get_items_by_firm'),
    path('get_item_stock/', views.get_item_stock, name='get_item_stock'),
    path('sales-orders/', views.view_sales_orders, name='view_sales_orders'),
    path('sales-orders/ajax/', views.view_sales_orders_ajax, name='view_sales_orders_ajax'),
    path('sales-order-details/<int:order_id>/', views.view_sales_order_details, name='view_sales_order_details'),
    path('get_customers_by_salesman/', views.get_customers_by_salesman, name='get_customers_by_salesman'),
    path('get_item_price/', views.get_item_price, name='get_item_price'),
    path('', views.login_view, name='login'),
    path('home/', views.home, name='home'),
    path('logout/', views.logout_view, name='logout'),
    path('orders/<int:order_id>/export/', views.export_sales_order_to_excel, name='export_sales_order_to_excel'),
    path('orders/<int:order_id>/export_pdf/', views.export_sales_order_to_pdf, name='export_sales_order_to_pdf'),
    path('sales_home/', views.sales_home, name='sales_home'),

    ############### CUSTOMER MANAGEMENT ##################
    path('customers/', views.customer_list, name='customer_list'),
    path('customers/add/', views.add_customer, name='add_customer'),
    path('customers/delete/<int:customer_id>/', views.delete_customer, name='delete_customer'),
    path('customers/sync-finance/', views.sync_customer_finance_summary, name='sync_customer_finance_summary'),
    path('customers/sync-finance-form/', views.sync_customer_finance_form, name='sync_customer_finance_form'),
    path('customers/sync-finance-api-receive/', views.sync_customer_finance_api_receive, name='sync_customer_finance_api_receive'),

    path('settings/', views.sync_settings, name='sync_settings'),
    path('settings/sync/', views.sync_settings_form, name='sync_settings_form'),
    path('settings/sync-all/', views.sync_all_sales_data_form, name='sync_all_sales_data'),

    ############### FINANCE STATEMENT ##################
    path('finance-statement/', finance_statement_views.finance_statement_list, name='finance_statement_list'),
    path('finance-statement/<int:customer_id>/', finance_statement_views.finance_statement_detail, name='finance_statement_detail'),
    path('finance-statement/<int:customer_id>/save-credit-edit/', finance_statement_views.save_finance_credit_edit, name='save_finance_credit_edit'),
    path('finance-statement/<int:customer_id>/save-internal-remarks/', finance_statement_views.save_finance_internal_remarks, name='save_finance_internal_remarks'),
    path('finance-statement/credit-edits/', finance_statement_views.finance_credit_edit_list, name='finance_credit_edit_list'),
    path('finance-statement/export/', finance_statement_views.export_finance_statement_list_excel, name='export_finance_statement_list_excel'),
    path('finance-statement/export-pdf/', finance_statement_pdf_export.export_finance_statement_list_pdf, name='export_finance_statement_list_pdf'),
    path('finance-statement/<int:customer_id>/export/', finance_statement_views.export_finance_statement_detail_excel, name='export_finance_statement_detail_excel'),
    path('finance-statement/<int:customer_id>/export-pdf/', finance_statement_pdf_export.export_finance_statement_detail_pdf, name='export_finance_statement_detail_pdf'),
    path('finance-statement/credit-edits/export-pdf/', finance_statement_pdf_export.export_finance_credit_edit_list_pdf, name='export_finance_credit_edit_list_pdf'),

    ################ ITEM MANAGEMENT ##################
    path('items/', views.item_list, name='item_list'),
    path('items/add/', views.item_create, name='item_create'),
    path('items/edit/<int:pk>/', views.item_edit, name='item_edit'),
    path('items/delete/<int:pk>/', views.item_delete, name='item_delete'),
    path('ajax/item-list/', views.item_list_ajax, name='item_list_ajax'),


    path('detail6145/', views.details, name='detail6145'),



    #######REST API ENDPOINTS########
    path('api/salesorder/', SalesOrderCreateView.as_view()),
    path('api/customers_by_salesman/', CustomerBySalesmanView.as_view()),
    path('api/items_by_firm/', ItemsByFirm.as_view()),
    path('api/get_item_price/', ItemPriceView.as_view()),
    path('api/firms/', UniqueFirms.as_view()),
    path('api/salesmen/', SalesmanList.as_view()),
    path('api/salesorders/', SalesOrderListAPI.as_view()),
    path('api/salesorders/<int:pk>/', SalesOrderDetailAPI.as_view()),
    path('api/login/', LoginView.as_view(), name='api-login'),


    path('api/customers/', CustomerListView.as_view(), name='customer-list'),
    path('api/customers/create/', CreateCustomerView.as_view(), name='customer-create'),
    path('api/customers/<int:pk>/', CustomerDetailView.as_view(), name='customer-detail'),


    path('upload/credit/', views.upload_customer_credit_excel, name='upload_customer_credit_excel'),
    path('api/total-outstanding/', total_outstanding_sum, name='total_outstanding_api'),
    path("api/items-search/", views.items_search, name="items_search"),
    path('sales-order/<int:order_id>/edit/', views.edit_sales_order, name='edit_sales_order'),
    path('sales-order/<int:order_id>/delete/', views.delete_sales_order, name='delete_sales_order'),

    path('api/get-last-location/<int:customer_id>/', get_last_location, name='get_last_location'),
    path('sales-orders/<int:order_id>/so-created/', views.mark_so_created, name='mark_so_created'),

    path('api/items-search/', views.items_search, name='items_search'),
    path('api/get-item-details/', views.get_item_details, name='get_item_details'),

    # Quotation URLs
    path('quotations/', views_quotation.view_quotations, name='view_quotations'),
    path('quotations/ajax/', views_quotation.view_quotations_ajax, name='view_quotations_ajax'),
    path('quotations/create/', views_quotation.create_quotation, name='create_quotation'),
    path('quotations/<int:quotation_id>/details/', views_quotation.view_quotation_details, name='view_quotation_details'),
    path('quotations/<int:quotation_id>/edit/', views_quotation.edit_quotation, name='edit_quotation'),
    path('quotations/<int:quotation_id>/export/', views_quotation.export_quotation_to_pdf, name='export_quotation_to_pdf'),
    path('quotations/<int:quotation_id>/convert-to-so/', views_quotation.convert_quotation_to_sales_order, name='convert_quotation_to_sales_order'),

]

# Quotation URLs (order matters: ajax BEFORE detail)
from . import sap_quotation_views

urlpatterns += [
    path('sapquotations/upload/', views.upload_quotations, name='upload_quotations'),
    path('sapquotations/sync-api/', sap_quotation_views.sync_quotations_from_api, name='sync_quotations_api'),
    path('sapquotations/sync-api-receive/', sap_quotation_views.sync_quotations_api_receive, name='sync_quotations_api_receive'),
    path('sapquotations/', views.quotation_list, name='quotation_list'),
    path('sapquotations/ajax/', views.quotation_search, name='quotation_search'),
    path('sapquotations/<str:q_number>/', views.quotation_detail, name='quotation_detail'),
    path('sapquotations/<str:q_number>/export/', views.export_sap_quotation_pdf, name='export_sap_quotation_pdf'),
    path('old-pi/<str:q_number>/export/', sap_salesorder_views.export_old_pi_pdf, name='export_old_pi_pdf'),
    path("sapquotations/<str:q_number>/remarks/", views.quotation_update_remarks, name="quotation_update_remarks"),
    path("sapquotations/<str:q_number>/close/", views.quotation_close, name="quotation_close"),
    path('update-device-location/', update_device_location, name='update_device_location'),
        # 1. Export the List (Summary)
    path('quotations/export-list/', views.export_quotation_list_pdf, name='export_quotation_list_pdf'),

    # SAP Salesorder URLs
    path('sapsalesorders/upload/', sap_salesorder_views.upload_salesorders, name='upload_salesorders'),
    path('sapsalesorders/upload-nfref/', sap_salesorder_views.upload_nfref_excel, name='upload_nfref_excel'),
    path('sapsalesorders/sync-api/', sap_salesorder_views.sync_salesorders_from_api, name='sync_salesorders_api'),
    path('sapsalesorders/sync-api-receive/', sap_salesorder_views.sync_salesorders_api_receive, name='sync_salesorders_api_receive'),
    path('sappurchaseorders/sync-api/', sap_purchaseorder_views.sync_purchaseorders_from_api, name='sync_purchaseorders_api'),
    path('sappurchaseorders/sync-api-receive/', sap_purchaseorder_views.sync_purchaseorders_api_receive, name='sync_purchaseorders_api_receive'),
    path('sappurchaseorders/api/item-totals/', sap_purchaseorder_views.api_open_purchaseorder_item_totals, name='api_open_purchaseorder_item_totals'),
    path('sappurchaseorders/', sap_purchaseorder_views.purchaseorder_list, name='purchaseorder_list'),
    path('sappurchaseorders/ajax/', sap_purchaseorder_views.purchaseorder_search, name='purchaseorder_search'),
    path('sappurchaseorders/export-excel/', sap_purchaseorder_views.export_purchaseorder_list_excel, name='export_purchaseorder_list_excel'),
    path('sappurchaseorders/export-pdf/', sap_purchaseorder_pdf_export.export_purchaseorder_list_pdf, name='export_purchaseorder_list_pdf'),
    path('sappurchaseorders/<str:po_number>/', sap_purchaseorder_views.purchaseorder_detail, name='purchaseorder_detail'),
    path('sappurchaseorders/<str:po_number>/export/', sap_purchaseorder_views.export_sap_purchaseorder_pdf, name='export_sap_purchaseorder_pdf'),
    path('sappurchaseorders/<str:po_number>/export-open/', sap_purchaseorder_views.export_sap_purchaseorder_open_items_pdf, name='export_sap_purchaseorder_open_items_pdf'),
    path('sapsalesorders/', sap_salesorder_views.salesorder_list, name='salesorder_list'),
    path('sapsalesorders/ajax/', sap_salesorder_views.salesorder_search, name='salesorder_search'),
    path('sapsalesorders/<str:so_number>/', sap_salesorder_views.salesorder_detail, name='salesorder_detail'),
    path('sapsalesorders/<str:so_number>/export/', sap_salesorder_views.export_sap_salesorder_pdf, name='export_sap_salesorder_pdf'),
    path('sapsalesorders/<str:so_number>/export-open/', sap_salesorder_views.export_sap_salesorder_open_items_pdf, name='export_sap_salesorder_open_items_pdf'),
    path('sapsalesorders/<str:so_number>/remarks/', sap_salesorder_views.salesorder_update_remarks, name='salesorder_update_remarks'),
    path('sapsalesorders/export-list/', sap_salesorder_views.export_salesorder_list_pdf, name='export_salesorder_list_pdf'),
    
    # SAP AR Invoice URLs
    path('saparinvoices/sync-api/', sap_salesorder_views.sync_arinvoices_from_api, name='sync_arinvoices_api'),
    path('saparinvoices/sync-api-receive/', sap_salesorder_views.sync_arinvoices_api_receive, name='sync_arinvoices_api_receive'),
    path('saparinvoices/export-excel/', sap_salesorder_views.export_arinvoice_list_excel, name='export_arinvoice_list_excel'),
    # Combined Sales Invoices URLs (must come before detail route)
    path('saparinvoices/analysis/', sap_salesorder_views.sales_analysis_dashboard, name='sales_analysis_dashboard'),
    path('saparinvoices/item-analysis/', sap_salesorder_views.item_analysis, name='item_analysis'),
    path('saparinvoices/item-classification/', sap_salesorder_views.item_classification_summary, name='item_classification_summary'),
    path('saparinvoices/purchase-stock-requirement/', purchase_stock_requirement_views.purchase_stock_requirement, name='purchase_stock_requirement'),
    path('saparinvoices/purchase-stock-requirement/export-excel/', purchase_stock_requirement_views.export_purchase_stock_requirement_excel, name='export_purchase_stock_requirement_excel'),
    path('saparinvoices/quotation-conversion/', sap_salesorder_views.quotation_conversion_analysis, name='quotation_conversion_analysis'),
    path('saparinvoices/item-analysis/export-pdf/', sap_salesorder_views.export_item_analysis_pdf, name='export_item_analysis_pdf'),
    path('saparinvoices/customer-analysis/', customer_analysis_views.customer_analysis, name='customer_analysis'),
    path('saparinvoices/customer-analysis/export-pdf/', customer_analysis_views.export_customer_analysis_pdf, name='export_customer_analysis_pdf'),
    path('saparinvoices/credit-memo-analysis/', credit_memo_analysis_views.itemwise_credit_memo_analysis, name='credit_memo_analysis'),
    path('saparinvoices/item-quoted-analysis/', quotation_item_analysis_views.item_quoted_analysis, name='item_quoted_analysis'),
    path('saparinvoices/item-quoted-analysis/export-pdf/', item_quoted_analysis_pdf_export.export_item_quoted_analysis_pdf, name='export_item_quoted_analysis_pdf'),
    path('saparinvoices/item-quoted-analysis/save-proposed-qty/', quotation_item_analysis_views.save_proposed_quantity, name='save_proposed_quantity'),
    path('saparinvoices/item-quoted-analysis/reset-proposed-qty/', quotation_item_analysis_views.reset_proposed_quantities, name='reset_proposed_quantities'),
    path('saparinvoices/combined/export-excel/', sap_salesorder_views.export_combined_sales_invoices_excel, name='export_combined_sales_invoices_excel'),
    path('saparinvoices/combined/', sap_salesorder_views.combined_sales_invoices_list, name='combined_sales_invoices_list'),
    path('saparinvoices/', sap_salesorder_views.arinvoice_list, name='arinvoice_list'),
    path('saparinvoices/<str:invoice_number>/', sap_salesorder_views.arinvoice_detail, name='arinvoice_detail'),
    
    # SAP AR Credit Memo URLs
    path('saparcreditmemos/sync-api/', sap_salesorder_views.sync_arcreditmemos_from_api, name='sync_arcreditmemos_api'),
    path('saparcreditmemos/sync-api-receive/', sap_salesorder_views.sync_arcreditmemos_api_receive, name='sync_arcreditmemos_api_receive'),
    path('saparcreditmemos/', sap_salesorder_views.arcreditmemo_list, name='arcreditmemo_list'),
    path('saparcreditmemos/<str:credit_memo_number>/', sap_salesorder_views.arcreditmemo_detail, name='arcreditmemo_detail'),
    
    # Proforma Invoice (PI) URLs
    path('proformainvoices/', sap_salesorder_views.pi_list, name='pi_list'),
    path('proformainvoices/old/', sap_salesorder_views.old_pi_list, name='old_pi_list'),
    path('sapsalesorders/<str:so_number>/pi/create/', sap_salesorder_views.create_pi, name='create_pi'),
    # More specific routes first
    path('pi/<str:pi_number>/edit/', sap_salesorder_views.edit_pi, name='edit_pi'),
    path('pi/<str:pi_number>/export/', sap_salesorder_views.export_pi_pdf, name='export_pi_pdf'),
    path('pi/<str:pi_number>/cancel/', sap_salesorder_views.cancel_pi, name='cancel_pi'),
    path('pi/<str:pi_number>/upload-cheque/', sap_salesorder_views.upload_cheque, name='upload_cheque'),
    path('pi/<str:pi_number>/remove-cheque/', sap_salesorder_views.remove_cheque, name='remove_cheque'),
    path('pi/<str:pi_number>/', sap_salesorder_views.pi_detail, name='pi_detail'),



    ## API endpoint for fast search items 
    path('api/items-search/', items_search_api, name='api_items_search'),



    path('dashboard/open-so/', views.open_so_dashboard, name='open_so_dashboard'),
    path('dashboard/upload-so/', views.upload_so_data, name='upload_so_data'), # New path
    path('dashboard/open-so/pdf/', views.export_so_pdf, name='export_so_pdf'), # Add this


    path('register-device/', views.register_device, name='register_device'),
    path('approve-device/', views.approve_device, name='approve_device'),
    path('device-pending/', views.device_pending, name='device_pending'),
    
]

