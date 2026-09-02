from django.contrib import admin

from .models import (
    BillingError, PickingList, PickingListBatch, PurchaseActivity,
    PurchaseRequest, PurchaseRequestItem, SupplierQuote,
)


class PurchaseRequestItemInline(admin.TabularInline):
    model = PurchaseRequestItem
    extra = 1


class SupplierQuoteInline(admin.TabularInline):
    model = SupplierQuote
    extra = 0


@admin.register(PurchaseRequest)
class PurchaseRequestAdmin(admin.ModelAdmin):
    list_display = ["display_ref", "requester_name", "department", "status", "created_at", "handled_by"]
    list_filter = ["status", "urgency"]
    inlines = [PurchaseRequestItemInline, SupplierQuoteInline]


class PickingListInline(admin.TabularInline):
    model = PickingList
    extra = 0


@admin.register(PickingListBatch)
class PickingListBatchAdmin(admin.ModelAdmin):
    list_display = ["shipped_on", "created_at"]
    inlines = [PickingListInline]


class BillingErrorInline(admin.TabularInline):
    model = BillingError
    extra = 0


@admin.register(PickingList)
class PickingListAdmin(admin.ModelAdmin):
    list_display = ["number", "batch", "status", "handed_off_at", "invoiced_at", "handled_by"]
    list_filter = ["status"]
    inlines = [BillingErrorInline]


admin.site.register(PurchaseActivity)
admin.site.register(BillingError)
