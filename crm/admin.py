from django.contrib import admin
from .models import Client, Project, BusinessProfile, Invoice, InvoiceItem

admin.site.register(Client)
admin.site.register(Project)
admin.site.register(BusinessProfile)


class InvoiceItemInline(admin.TabularInline):
    model = InvoiceItem
    extra = 0


@admin.register(Invoice)
class InvoiceAdmin(admin.ModelAdmin):
    list_display = ("number", "client", "status", "issue_date", "due_date")
    list_filter = ("status", "kind")
    inlines = [InvoiceItemInline]
