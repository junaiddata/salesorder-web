from django.contrib import admin

from .models import AgentRun, EmailAgentSyncState, EmailAttachment, EnquiryItem, TrackedEmail


class EmailAttachmentInline(admin.TabularInline):
    model = EmailAttachment
    extra = 0
    readonly_fields = ['gmail_attachment_id', 'filename', 'content_type', 'size_bytes', 'included_in_classification']


class EnquiryItemInline(admin.TabularInline):
    model = EnquiryItem
    extra = 0


@admin.register(TrackedEmail)
class TrackedEmailAdmin(admin.ModelAdmin):
    list_display = ['subject', 'sender', 'status', 'classification_confidence', 'received_at']
    list_filter = ['status']
    search_fields = ['subject', 'sender', 'body_text']
    readonly_fields = ['gmail_message_id', 'thread_id', 'raw_headers', 'created_at']
    inlines = [EnquiryItemInline, EmailAttachmentInline]


@admin.register(EmailAgentSyncState)
class EmailAgentSyncStateAdmin(admin.ModelAdmin):
    list_display = ['last_history_id', 'last_run_at', 'updated_at']


@admin.register(AgentRun)
class AgentRunAdmin(admin.ModelAdmin):
    list_display = ['agent_name', 'status', 'summary', 'tracked_email', 'quotation', 'duration_ms', 'created_at']
    list_filter = ['agent_name', 'status']
    search_fields = ['summary', 'error']
    readonly_fields = ['agent_name', 'tracked_email', 'quotation', 'status', 'started_at', 'finished_at',
                        'duration_ms', 'summary', 'issues', 'error', 'created_at']
