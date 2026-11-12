# Enables full CRUD over your models through API calls.
from rest_framework import serializers
from django.db import transaction
from django.utils import timezone
from .models import (
    Client, Project, ProjectItem, ProjectNote, ProjectTodo, BusinessProfile, Invoice, InvoiceItem,
)
from . import billing
from .billing import first_billing_date, next_invoice_number
from django.contrib.auth import get_user_model

User = get_user_model()


class ClientSerializer(serializers.ModelSerializer):
    # serializers.ModelSerializer → A DRF shortcut that creates serializer fields based
    #  on your model fields automatically.

    class Meta:
        # Meta is Django’s convention for passing model-related settings to a
        #  class — it keeps things clean and readable.
        model = Client
        fields = "__all__"
        # fields = "__all__" → Include all fields from the model in the serializer output.
        read_only_fields = ("owner", "created_at")
        # read_only_fields → Prevent clients from manually setting these when posting data.


class ProjectItemSerializer(serializers.ModelSerializer):
    class Meta:
        model = ProjectItem
        fields = ("id", "description", "amount", "recurring")
        read_only_fields = ("id",)
        # Blank rows from the form are dropped when saving rather than rejected.
        extra_kwargs = {"description": {"allow_blank": True}}


class ProjectSerializer(serializers.ModelSerializer):
    # Extra fields beyond the model:
    #   client_name → human-readable name of the client
    #   client_id → the FK id for linking back to Client
    client_name = serializers.CharField(source="client.name", read_only=True)
    client_id = serializers.IntegerField(source="client.id", read_only=True)
    # Price breakdown used for invoice lines, e.g. developer fees + hosting + domain.
    items = ProjectItemSerializer(many=True, required=False)
    # Retainer months that are due and not paid yet (dates), and what they add up to.
    unpaid_periods = serializers.SerializerMethodField()
    unpaid_retainer_total = serializers.SerializerMethodField()

    class Meta:
        model = Project
        fields = "__all__"
        # Worked out from the retainer schedule and what's been invoiced or paid.
        read_only_fields = ("next_invoice_date",)

    def get_unpaid_periods(self, obj):
        return billing.unpaid_periods(obj)

    def get_unpaid_retainer_total(self, obj):
        return str(obj.monthly_amount * len(billing.unpaid_periods(obj)))

    def validate_client(self, client):
        # A project can only be attached to one of the logged-in user's own clients.
        if client.owner_id != self.context["request"].user.id:
            raise serializers.ValidationError("Client not found.")
        return client

    def validate_billing_day(self, value):
        if not 1 <= value <= 31:
            raise serializers.ValidationError("Pick a day between 1 and 31.")
        return value

    def validate(self, attrs):
        # The first payment date also fixes the day of the month every later payment falls on.
        if attrs.get("retainer_start"):
            attrs["billing_day"] = attrs["retainer_start"].day
        return attrs

    def _save_items(self, project, items):
        if items is None:
            return
        project.items.all().delete()
        def is_recurring(i):
            return {"fixed": False, "monthly": True}.get(project.billing_type, bool(i.get("recurring")))
        rows = [
            ProjectItem(project=project, description=i["description"].strip(), amount=i.get("amount") or 0,
                        recurring=is_recurring(i))
            for i in items if i.get("description", "").strip()
        ]
        ProjectItem.objects.bulk_create(rows)
        # When there's a breakdown, the totals are its sums.
        fixed = [r.amount for r in rows if not r.recurring]
        monthly = [r.amount for r in rows if r.recurring]
        if fixed and project.has_fixed_fee:
            project.payment_amount = sum(fixed)
        if monthly and project.is_recurring:
            project.monthly_amount = sum(monthly)
        project.save(update_fields=["payment_amount", "monthly_amount"])

    def _sync_retainer(self, project):
        if project.is_recurring and project.retainer_start is None:
            project.retainer_start = first_billing_date(project.billing_day, timezone.localdate())
            project.save(update_fields=["retainer_start"])
        billing.sync_next_invoice_date(project)
        if not project.is_recurring and project.next_invoice_date is not None:
            project.next_invoice_date = None
            project.save(update_fields=["next_invoice_date"])
        return project

    @transaction.atomic
    def create(self, validated_data):
        items = validated_data.pop("items", None)
        project = super().create(validated_data)
        self._save_items(project, items)
        return self._sync_retainer(project)

    @transaction.atomic
    def update(self, instance, validated_data):
        items = validated_data.pop("items", None)
        old_status = instance.status
        new_status = validated_data.get("status", old_status)
        if new_status != old_status and instance.is_recurring:
            # Completing a project stops new retainer months; reopening it resumes them.
            if new_status == "completed" and instance.retainer_end is None:
                validated_data.setdefault("retainer_end", timezone.localdate())
            elif new_status == "active":
                validated_data.setdefault("retainer_end", None)
        project = super().update(instance, validated_data)
        self._save_items(project, items)
        return self._sync_retainer(project)


class ProjectNoteSerializer(serializers.ModelSerializer):
    class Meta:
        model = ProjectNote
        fields = ("id", "project", "body", "created_at", "updated_at")
        read_only_fields = ("created_at", "updated_at")

    def validate_project(self, project):
        if project.client.owner_id != self.context["request"].user.id:
            raise serializers.ValidationError("Project not found.")
        return project


class ProjectTodoSerializer(serializers.ModelSerializer):
    class Meta:
        model = ProjectTodo
        fields = ("id", "project", "title", "done", "due_date", "done_at", "created_at")
        read_only_fields = ("done_at", "created_at")

    validate_project = ProjectNoteSerializer.validate_project

    def save(self, **kwargs):
        done = self.validated_data.get("done")
        if done is not None and (self.instance is None or self.instance.done != done):
            kwargs["done_at"] = timezone.now() if done else None
        return super().save(**kwargs)


class BusinessProfileSerializer(serializers.ModelSerializer):
    class Meta:
        model = BusinessProfile
        exclude = ("id", "owner")


class InvoiceItemSerializer(serializers.ModelSerializer):
    id = serializers.IntegerField(required=False)
    amount = serializers.DecimalField(max_digits=14, decimal_places=2, read_only=True)

    class Meta:
        model = InvoiceItem
        fields = ("id", "description", "quantity", "unit_price", "amount")


class InvoiceSerializer(serializers.ModelSerializer):
    items = InvoiceItemSerializer(many=True)
    total = serializers.DecimalField(max_digits=14, decimal_places=2, read_only=True)
    is_overdue = serializers.BooleanField(read_only=True)
    client_name = serializers.CharField(source="client.name", read_only=True)
    client_company = serializers.CharField(source="client.company", read_only=True)
    client_email = serializers.CharField(source="client.email", read_only=True)
    client_phone = serializers.CharField(source="client.phone", read_only=True)
    project_title = serializers.CharField(source="project.title", read_only=True, default=None)

    class Meta:
        model = Invoice
        fields = "__all__"
        read_only_fields = ("owner", "number", "sent_at", "paid_at", "created_at")

    def validate_client(self, client):
        if client.owner_id != self.context["request"].user.id:
            raise serializers.ValidationError("Client not found.")
        return client

    def validate(self, attrs):
        client = attrs.get("client") or getattr(self.instance, "client", None)
        project = attrs.get("project", getattr(self.instance, "project", None))
        if project is not None and project.client_id != getattr(client, "id", None):
            raise serializers.ValidationError({"project": "That project belongs to a different client."})
        issue = attrs.get("issue_date") or getattr(self.instance, "issue_date", None) or timezone.localdate()
        due = attrs.get("due_date") or getattr(self.instance, "due_date", None)
        if due and due < issue:
            raise serializers.ValidationError({"due_date": "Due date can't be before the issue date."})
        if self.instance is None and not attrs.get("items"):
            raise serializers.ValidationError({"items": "Add at least one line item."})
        return attrs

    @transaction.atomic
    def create(self, validated_data):
        items = validated_data.pop("items")
        user = self.context["request"].user
        invoice = Invoice.objects.create(owner=user, number=next_invoice_number(user), **validated_data)
        InvoiceItem.objects.bulk_create(
            [InvoiceItem(invoice=invoice, **{k: v for k, v in i.items() if k != "id"}) for i in items]
        )
        return invoice

    @transaction.atomic
    def update(self, instance, validated_data):
        items = validated_data.pop("items", None)
        invoice = super().update(instance, validated_data)
        if items is not None:
            # Replace the line items wholesale: simpler than diffing and invoices are small.
            invoice.items.all().delete()
            InvoiceItem.objects.bulk_create(
                [InvoiceItem(invoice=invoice, **{k: v for k, v in i.items() if k != "id"}) for i in items]
            )
        return invoice


