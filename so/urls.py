from django.urls import path
from . import views
from so.views import *
from . import views_quotation

urlpatterns = [
    path('upload-items/', views.upload_items, name='upload_items'),
    path('upload-customers/', views.upload_customers, name='upload_customers'),
    # path('create-sales-order/', views.create_sales_order, name='create_sales_order'),
    path('create/', views.create_sales_order, name='create_sales_order'),
    path('get_items_by_firm/', views.get_items_by_firm, name='get_items_by_firm'),
    path('get_item_stock/', views.get_item_stock, name='get_item_stock'),
    path('sales-orders/', views.view_sales_orders, name='view_sales_orders'),
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

    path('api/get-last-location/<int:customer_id>/', get_last_location, name='get_last_location'),
    path('sales-orders/<int:order_id>/so-created/', views.mark_so_created, name='mark_so_created'),

    path('api/items-search/', views.items_search, name='items_search'),
    path('api/get-item-details/', views.get_item_details, name='get_item_details'),

    # Quotation URLs
    path('quotations/', views_quotation.view_quotations, name='view_quotations'),
    path('quotations/create/', views_quotation.create_quotation, name='create_quotation'),
    path('quotations/<int:quotation_id>/details/', views_quotation.view_quotation_details, name='view_quotation_details'),
    path('quotations/<int:quotation_id>/edit/', views_quotation.edit_quotation, name='edit_quotation'),
    path('quotations/<int:quotation_id>/export/', views_quotation.export_quotation_to_pdf, name='export_quotation_to_pdf'),

]

# Quotation URLs (order matters: ajax BEFORE detail)
urlpatterns += [
    path('sapquotations/upload/', views.upload_quotations, name='upload_quotations'),
    path('sapquotations/', views.quotation_list, name='quotation_list'),
    path('sapquotations/ajax/', views.quotation_search, name='quotation_search'),
    path('sapquotations/<str:q_number>/', views.quotation_detail, name='quotation_detail'),
    path('sapquotations/<str:q_number>/export/', views.export_sap_quotation_pdf, name='export_sap_quotation_pdf'),
    path("sapquotations/<str:q_number>/remarks/", views.quotation_update_remarks, name="quotation_update_remarks"),



    ## API endpoint for fast search items 
    path('api/items-search/', items_search_api, name='api_items_search'),

]