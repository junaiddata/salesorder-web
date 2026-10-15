from django.urls import path

from . import views

app_name = 'bangladeshpi'

urlpatterns = [
    path('', views.pi_list, name='list'),
    path('new/', views.pi_form, name='new'),
    path('api/items-search/', views.items_search, name='items_search'),
    path('signature-stamp/', views.seal_settings, name='seal_settings'),
    path('<int:pk>/edit/', views.pi_form, name='edit'),
    path('<int:pk>/delete/', views.pi_delete, name='delete'),
    path('<int:pk>/pdf/', views.pi_pdf, name='pdf'),
]
