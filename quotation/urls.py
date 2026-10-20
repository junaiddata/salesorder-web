from django.urls import path

from . import views

app_name = 'quotation'

urlpatterns = [
    path('', views.quotation_list, name='list'),
    path('new/', views.quotation_form, name='new'),
    path('<int:pk>/edit/', views.quotation_form, name='edit'),
    path('<int:pk>/delete/', views.quotation_delete, name='delete'),
    path('<int:pk>/pdf/', views.quotation_pdf, name='pdf'),
    path('photo/<int:item_pk>/', views.item_photo, name='photo'),
    # Public (no login): the datasheet a customer reaches from the PDF's QR code / link.
    path('ds/<str:token>/', views.datasheet_public, name='datasheet'),
]
