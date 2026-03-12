from django.urls import path
from . import views

app_name = 'submittal'

urlpatterns = [
    path('', views.submittal_list, name='list'),
    path('new/', views.submittal_wizard, name='wizard'),
    path('<int:pk>/', views.submittal_detail, name='detail'),
    path('<int:pk>/edit/', views.submittal_wizard, name='edit'),
    path('save/', views.submittal_save, name='save'),
    path('<int:pk>/generate-pdf/', views.submittal_generate_pdf, name='generate_pdf'),

    # AJAX
    path('api/materials-search/', views.api_materials_search, name='api_materials_search'),
    path('api/history-suggestions/', views.api_history_suggestions, name='api_history_suggestions'),
]
