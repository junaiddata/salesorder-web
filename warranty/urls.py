from django.urls import path
from . import views

app_name = 'warranty'

urlpatterns = [
    path('', views.warranty_list, name='list'),
    path('new/', views.warranty_form, name='new'),
    path('<int:pk>/edit/', views.warranty_form, name='edit'),
    path('<int:pk>/', views.warranty_detail, name='detail'),
    path('<int:pk>/delete/', views.warranty_delete, name='delete'),
    path('save/', views.warranty_save, name='save'),
    path('<int:pk>/submit/', views.warranty_submit, name='submit'),
    path('<int:pk>/withdraw/', views.warranty_withdraw, name='withdraw'),
    path('<int:pk>/approval/', views.warranty_update_approval, name='update_approval'),
    path('<int:pk>/signature/save/', views.warranty_save_signature, name='save_signature'),
    path('<int:pk>/signature/clear/', views.warranty_clear_signature, name='clear_signature'),
    path('<int:pk>/stamp/save/', views.warranty_save_stamp, name='save_stamp'),
    path('<int:pk>/stamp/clear/', views.warranty_clear_stamp, name='clear_stamp'),
    path('<int:pk>/generate-pdf/', views.warranty_generate_pdf, name='generate_pdf'),
    path('api/suggest-ref-no/', views.api_suggest_ref_no, name='api_suggest_ref_no'),
]
