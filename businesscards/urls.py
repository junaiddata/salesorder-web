from django.urls import path
from . import views

app_name = 'businesscards'

urlpatterns = [
    # Authenticated views
    path('dashboard/', views.dashboard, name='dashboard'),
    path('upload-excel/', views.upload_excel, name='upload_excel'),
    path('salesmen/', views.salesmen_list, name='salesmen_list'),
    path('salesmen/<slug:slug>/', views.salesman_detail, name='salesman_detail'),
    path('salesmen/<slug:slug>/qr/', views.download_qr, name='download_qr'),
    
    # Public URLs (no auth required)
    path('card/<slug:slug>/', views.card_public, name='card_public'),
    path('vcard/<slug:slug>.vcf', views.vcard_download, name='vcard_download'),
]
