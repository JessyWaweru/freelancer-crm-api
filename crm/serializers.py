# Enables full CRUD over your models through API calls.
from rest_framework import serializers
from django.db import transaction
from django.utils import timezone
from .models import Client, Project, BusinessProfile, Invoice, InvoiceItem
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


class ProjectSerializer(serializers.ModelSerializer):
    # Extra fields beyond the model:
    #   client_name → human-readable name of the client
    #   client_id → the FK id for linking back to Client
    client_name = serializers.CharField(source="client.name", read_only=True)
    client_id = serializers.IntegerField(source="client.id", read_only=True)

    class Meta:
        model = Project
        fields = "__all__"

    def validate_client(self, client):
        # A project can only be attached to one of the logged-in user's own clients.
        if client.owner_id != self.context["request"].user.id:
            raise serializers.ValidationError("Client not found.")
        return client

    def validate_billing_day(self, value):
        if not 1 <= value <= 28:
            raise serializers.ValidationError("Pick a day between 1 and 28.")
        return value

    def _set_first_invoice_date(self, project):
        # A new retainer gets its first invoice date from the billing day.
        if project.is_recurring and project.next_invoice_date is None:
            project.next_invoice_date = first_billing_date(project.billing_day, timezone.localdate())
            project.save(update_fields=["next_invoice_date"])
        return project

    def create(self, validated_data):
        return self._set_first_invoice_date(super().create(validated_data))

    def update(self, instance, validated_data):
        old_day = instance.billing_day
        project = super().update(instance, validated_data)
        if project.is_recurring and "next_invoice_date" not in validated_data and project.billing_day != old_day:
            project.next_invoice_date = None  # recompute for the new billing day
        return self._set_first_invoice_date(project)


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


