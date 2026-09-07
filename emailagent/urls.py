from django.urls import path

from . import stock_shortage_export, views

app_name = 'emailagent'

urlpatterns = [
    path('review/', views.review_queue, name='review_queue'),
    path('review/<int:pk>/confirm/', views.review_confirm, name='review_confirm'),
    path('review/<int:pk>/reject/', views.review_reject, name='review_reject'),
    path('emails/', views.email_list, name='email_list'),
    path('emails/<int:pk>/', views.email_detail, name='email_detail'),
    path('emails/<int:pk>/generate-submittal/', views.submittal_draft_selected, name='submittal_draft_selected'),
    path('emails/<int:pk>/open-webmail/', views.open_webmail, name='open_webmail'),
    path('attachments/<int:pk>/', views.attachment_download, name='attachment_download'),
    path('quotations/', views.quotation_draft_queue, name='quotation_draft_queue'),
    path('quotations/<int:pk>/', views.quotation_draft_review, name='quotation_draft_review'),
    path('submittals/', views.submittal_draft_queue, name='submittal_draft_queue'),
    path('lpo/', views.lpo_request_queue, name='lpo_request_queue'),
    path('lpo/<int:pk>/', views.lpo_request_review, name='lpo_request_review'),
    path('lpo/<int:pk>/convert/', views.lpo_request_convert, name='lpo_request_convert'),
    path('lpo/<int:pk>/recheck/', views.lpo_request_recheck_match, name='lpo_request_recheck_match'),
    path('lpo/<int:pk>/dismiss/', views.lpo_request_dismiss, name='lpo_request_dismiss'),
    path('stock-shortages/', views.stock_shortage_report, name='stock_shortage_report'),
    path('stock-shortages/refresh/', views.stock_shortage_report_refresh, name='stock_shortage_report_refresh'),
    path('stock-shortages/export/excel/', stock_shortage_export.export_stock_shortage_excel, name='stock_shortage_export_excel'),
    path('stock-shortages/export/pdf/', stock_shortage_export.export_stock_shortage_pdf, name='stock_shortage_export_pdf'),
    path('agent-activity/', views.agent_activity, name='agent_activity'),
    path('gmail/webhook/', views.gmail_push_webhook, name='gmail_push_webhook'),
]
