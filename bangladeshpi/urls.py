from django.urls import path

from . import commercial_views, views

app_name = 'bangladeshpi'

urlpatterns = [
    path('', views.pi_list, name='list'),
    path('new/', views.pi_form, name='new'),
    path('api/items-search/', views.items_search, name='items_search'),
    path('signature-stamp/', views.seal_settings, name='seal_settings'),
    path('<int:pk>/edit/', views.pi_form, name='edit'),
    path('<int:pk>/delete/', views.pi_delete, name='delete'),
    path('<int:pk>/pdf/', views.pi_pdf, name='pdf'),
    path('<int:pk>/excel/', views.pi_excel, name='excel'),
    path('<int:pk>/commercial-invoice/', commercial_views.ci_edit, name='ci_edit'),
    path('<int:pk>/commercial-invoice/pdf/', commercial_views.ci_pdf, name='ci_pdf'),
]
