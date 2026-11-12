from rest_framework import viewsets, permissions, mixins, generics
# viewsets → A DRF shortcut that gives you list, retrieve, create, update, and delete
#  actions automatically for a model.
# generics.ListAPIView → Quick way to build read-only list endpoints (like nested routes).

from django.utils import timezone
from rest_framework import status
from rest_framework.decorators import action
from datetime import date

from .models import Client, Project, Invoice, ProjectNote, ProjectTodo
from .serializers import (
    ClientSerializer, ProjectSerializer, InvoiceSerializer, BusinessProfileSerializer,
    ProjectNoteSerializer, ProjectTodoSerializer,
)
from . import billing
from django.contrib.auth import get_user_model
from django.http import JsonResponse
from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework.permissions import AllowAny

class HealthCheckView(APIView):
    permission_classes = [AllowAny]
    authentication_classes = []

    def get(self, request):
        return Response({"status": "ok"})

User = get_user_model()


class IsOwner(permissions.BasePermission):
    def has_object_permission(self, request, view, obj):
        # only for Client; Project/Invoice are tied via Client
        return getattr(obj, "owner_id", None) == request.user.id
    # Purpose: Make sure only the user who owns the object can view/edit it
    # has_object_permission → Runs for requests to specific objects (like GET /clients/5/
    #  or DELETE /clients/5/).
    # getattr(obj, "owner_id", None) → Safely get the object's owner_id attribute,
    #  return None if it doesn’t exist.
    # Checks if that owner_id matches request.user.id.


class ClientViewSet(viewsets.ModelViewSet):
    # ModelViewSet → Gives you CRUD (Create, Read, Update, Delete) without writing them
    #  manually.

    serializer_class = ClientSerializer
    permission_classes = [permissions.IsAuthenticated, IsOwner]
    # permissions.IsAuthenticated → Only logged-in users can use it.
    # IsOwner → On top of being logged in, you must own the client record.

    def get_queryset(self):
        # Limits query results to only the logged-in user’s clients.
        # Orders newest first.
        return Client.objects.filter(owner=self.request.user).order_by("-created_at")

    def perform_create(self, serializer):
        serializer.save(owner=self.request.user)
        # Saves the client with the logged-in user as the owner.


class ProjectViewSet(viewsets.ModelViewSet):
    serializer_class = ProjectSerializer
    permission_classes = [permissions.IsAuthenticated]

    def get_queryset(self):
        # A project must belong to a client whose owner is the logged-in user.
        # client__owner is Django ORM’s way of filtering across relationships.
        qs = (
            Project.objects
            .select_related("client")  # select_related → avoid extra queries when accessing client
            .prefetch_related("items", "retainer_payments")
            .filter(client__owner=self.request.user)
        )

        # Optional filter: /api/projects?client=<id>
        # Lets the UI ask for a specific client's projects without needing a nested route.
        client_id = self.request.query_params.get("client")
        if client_id:
            qs = qs.filter(client_id=client_id)

        return qs

    @action(detail=True, methods=["post"])
    def generate_invoice(self, request, pk=None):
        # POST /api/projects/<id>/generate_invoice/  {"kind": "fixed" | "monthly"}
        project = self.get_object()
        try:
            invoice = billing.generate_invoice(project, request.data.get("kind", "fixed"))
        except ValueError as e:
            return Response({"detail": str(e)}, status=status.HTTP_400_BAD_REQUEST)
        return Response(
            InvoiceSerializer(invoice, context={"request": request}).data,
            status=status.HTTP_201_CREATED,
        )


    @action(detail=True, methods=["get", "post"])
    def retainer(self, request, pk=None):
        # GET  /api/projects/<id>/retainer/  every month so far + the next, with paid/unpaid status
        # POST /api/projects/<id>/retainer/  {"period": "2026-11-05", "paid": true|false}
        project = self.get_object()
        if request.method == "POST":
            try:
                period = date.fromisoformat(str(request.data.get("period")))
            except ValueError:
                return Response({"detail": "Send the month as a date, e.g. 2026-11-05."}, status=status.HTTP_400_BAD_REQUEST)
            try:
                billing.set_retainer_paid(project, period, bool(request.data.get("paid", True)))
            except ValueError as e:
                return Response({"detail": str(e)}, status=status.HTTP_400_BAD_REQUEST)
            project = self.get_queryset().get(pk=project.pk)  # fresh payments
        return Response({
            "project": self.get_serializer(project).data,
            "months": billing.retainer_overview(project),
        })


class _ProjectChildViewSet(viewsets.ModelViewSet):
    """Notes and to-dos: scoped to the user's projects, filterable with ?project=<id>."""
    permission_classes = [permissions.IsAuthenticated]
    model = None

    def get_queryset(self):
        qs = self.model.objects.filter(project__client__owner=self.request.user)
        project_id = self.request.query_params.get("project")
        if project_id:
            qs = qs.filter(project_id=project_id)
        return qs


class ProjectNoteViewSet(_ProjectChildViewSet):
    serializer_class = ProjectNoteSerializer
    model = ProjectNote


class ProjectTodoViewSet(_ProjectChildViewSet):
    serializer_class = ProjectTodoSerializer
    model = ProjectTodo


class InvoiceViewSet(viewsets.ModelViewSet):
    serializer_class = InvoiceSerializer
    permission_classes = [permissions.IsAuthenticated]

    def get_queryset(self):
        qs = (
            Invoice.objects.filter(owner=self.request.user)
            .select_related("client", "project")
            .prefetch_related("items")
        )
        for param in ("client", "project", "status"):
            value = self.request.query_params.get(param)
            if value:
                qs = qs.filter(**{param: value})
        return qs

    def _set_status(self, request, new_status, stamp_field):
        invoice = self.get_object()
        invoice.status = new_status
        if stamp_field and getattr(invoice, stamp_field) is None:
            setattr(invoice, stamp_field, timezone.now())
        invoice.save()
        # Paying the fixed-fee invoice marks the project as paid too.
        if new_status == "paid" and invoice.kind == "fixed" and invoice.project_id:
            Project.objects.filter(pk=invoice.project_id).update(payment_status="paid")
        # Paying a monthly invoice pays that month of the retainer.
        if new_status == "paid":
            billing.record_invoice_paid(invoice)
        # Voiding a monthly invoice frees its month to be invoiced again.
        if new_status == "void" and invoice.kind == "monthly" and invoice.project:
            billing.sync_next_invoice_date(invoice.project)
        return Response(self.get_serializer(invoice).data)

    def perform_destroy(self, invoice):
        # Deleting a monthly invoice frees its month to be invoiced again.
        project = invoice.project if invoice.kind == "monthly" else None
        invoice.delete()
        if project:
            billing.sync_next_invoice_date(project)

    @action(detail=True, methods=["post"])
    def mark_sent(self, request, pk=None):
        return self._set_status(request, "sent", "sent_at")

    @action(detail=True, methods=["post"])
    def mark_paid(self, request, pk=None):
        return self._set_status(request, "paid", "paid_at")

    @action(detail=True, methods=["post"])
    def void(self, request, pk=None):
        return self._set_status(request, "void", None)


class BusinessProfileView(APIView):
    # GET/PUT/PATCH /api/profile/ : the freelancer's details printed on invoices.
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request):
        return Response(BusinessProfileSerializer(billing.get_profile(request.user)).data)

    def put(self, request):
        serializer = BusinessProfileSerializer(
            billing.get_profile(request.user), data=request.data, partial=True
        )
        serializer.is_valid(raise_exception=True)
        serializer.save()
        return Response(serializer.data)

    patch = put


class RemindersView(APIView):
    # GET /api/reminders/ : invoices to send and payments to chase, most urgent first.
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request):
        return Response(billing.build_reminders(request.user))


class DashboardView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request):
        data = billing.dashboard_totals(request.user)
        data["counts"] = {
            "clients": Client.objects.filter(owner=request.user).count(),
            "active_projects": Project.objects.filter(client__owner=request.user, status="active").count(),
            "open_invoices": Invoice.objects.filter(owner=request.user, status__in=["draft", "sent"]).count(),
        }
        return Response(data)

