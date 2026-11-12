from datetime import date, timedelta
from decimal import Decimal

import pytest
from django.contrib.auth import get_user_model
from django.utils import timezone
from rest_framework.test import APIClient

from crm.billing import add_months, first_billing_date
from crm.models import Client, Invoice, Project

User = get_user_model()


@pytest.fixture
def user():
    return User.objects.create_user(username="freelancer", password="pass1234")


@pytest.fixture
def api(user):
    c = APIClient()
    c.force_authenticate(user=user)
    return c


@pytest.fixture
def client_obj(user):
    return Client.objects.create(name="Acme", owner=user, phone="+254700000000")


def test_add_months_clamps_to_month_end():
    assert add_months(date(2026, 1, 31), 1) == date(2026, 2, 28)
    assert add_months(date(2026, 12, 15), 1) == date(2027, 1, 15)
    assert add_months(date(2026, 3, 10), 1, 5) == date(2026, 4, 5)


def test_first_billing_date():
    assert first_billing_date(15, date(2026, 11, 12)) == date(2026, 11, 15)
    assert first_billing_date(1, date(2026, 11, 12)) == date(2026, 12, 1)
    assert first_billing_date(12, date(2026, 11, 12)) == date(2026, 11, 12)


@pytest.mark.django_db
def test_monthly_project_gets_first_invoice_date(api, client_obj):
    r = api.post("/api/projects/", {
        "title": "Retainer", "client": client_obj.id, "billing_type": "monthly",
        "monthly_amount": "6000", "billing_day": 1, "payment_currency": "KES",
    }, format="json")
    assert r.status_code == 201, r.data
    expected = first_billing_date(1, timezone.localdate())
    assert r.data["next_invoice_date"] == expected.isoformat()


@pytest.mark.django_db
def test_cannot_create_project_for_someone_elses_client(api):
    other = User.objects.create_user(username="other", password="pass1234")
    theirs = Client.objects.create(name="Theirs", owner=other, phone="1")
    r = api.post("/api/projects/", {"title": "x", "client": theirs.id}, format="json")
    assert r.status_code == 400


@pytest.mark.django_db
def test_generate_monthly_invoice_advances_next_date(api, client_obj):
    start = timezone.localdate()
    p = Project.objects.create(
        title="Assistant", client=client_obj, billing_type="both", payment_amount=Decimal("25000"),
        monthly_amount=Decimal("6000"), billing_day=start.day if start.day <= 28 else 28,
        next_invoice_date=start, payment_currency="KES",
    )
    r = api.post(f"/api/projects/{p.id}/generate_invoice/", {"kind": "monthly"}, format="json")
    assert r.status_code == 201, r.data
    assert r.data["number"] == "INV-0001"
    assert r.data["status"] == "draft"
    assert Decimal(r.data["total"]) == Decimal("6000.00")
    assert r.data["period_start"] == start.isoformat()
    p.refresh_from_db()
    assert p.next_invoice_date == add_months(start, 1, p.billing_day)

    r2 = api.post(f"/api/projects/{p.id}/generate_invoice/", {"kind": "fixed"}, format="json")
    assert r2.data["number"] == "INV-0002"
    assert Decimal(r2.data["total"]) == Decimal("25000.00")


@pytest.mark.django_db
def test_generate_rejects_wrong_kind(api, client_obj):
    p = Project.objects.create(title="One-off", client=client_obj, billing_type="fixed")
    r = api.post(f"/api/projects/{p.id}/generate_invoice/", {"kind": "monthly"}, format="json")
    assert r.status_code == 400


@pytest.mark.django_db
def test_create_custom_invoice_with_items_and_pay_it(api, client_obj):
    r = api.post("/api/invoices/", {
        "client": client_obj.id, "currency": "KES", "due_date": (timezone.localdate() + timedelta(days=7)).isoformat(),
        "items": [
            {"description": "Logo", "quantity": "1", "unit_price": "5000"},
            {"description": "Revisions", "quantity": "2", "unit_price": "1500.50"},
        ],
    }, format="json")
    assert r.status_code == 201, r.data
    assert Decimal(r.data["total"]) == Decimal("8001.00")
    inv_id = r.data["id"]

    assert api.post(f"/api/invoices/{inv_id}/mark_sent/").data["status"] == "sent"
    paid = api.post(f"/api/invoices/{inv_id}/mark_paid/").data
    assert paid["status"] == "paid" and paid["paid_at"]


@pytest.mark.django_db
def test_invoice_requires_items(api, client_obj):
    r = api.post("/api/invoices/", {
        "client": client_obj.id, "due_date": timezone.localdate().isoformat(), "items": [],
    }, format="json")
    assert r.status_code == 400


@pytest.mark.django_db
def test_invoices_are_private(api, client_obj):
    other = User.objects.create_user(username="other", password="pass1234")
    theirs = Client.objects.create(name="Theirs", owner=other, phone="1")
    inv = Invoice.objects.create(owner=other, client=theirs, number="INV-0001", due_date=timezone.localdate())
    assert api.get(f"/api/invoices/{inv.id}/").status_code == 404
    assert api.get("/api/invoices/").data == []


@pytest.mark.django_db
def test_reminders_cover_monthly_fixed_draft_and_overdue(api, user, client_obj):
    today = timezone.localdate()
    Project.objects.create(
        title="Retainer", client=client_obj, billing_type="monthly", monthly_amount=Decimal("6000"),
        next_invoice_date=today + timedelta(days=2),
    )
    Project.objects.create(
        title="Website", client=client_obj, billing_type="fixed", payment_amount=Decimal("25000"),
        due_date=today - timedelta(days=1),
    )
    # Far-off retainer should not show yet.
    Project.objects.create(
        title="Later", client=client_obj, billing_type="monthly", monthly_amount=Decimal("1000"),
        next_invoice_date=today + timedelta(days=20),
    )
    Invoice.objects.create(owner=user, client=client_obj, number="INV-0009", due_date=today)
    Invoice.objects.create(
        owner=user, client=client_obj, number="INV-0010", status="sent", due_date=today - timedelta(days=5),
    )

    r = api.get("/api/reminders/")
    types = [x["type"] for x in r.data]
    assert sorted(types) == ["draft", "fixed_due", "monthly_due", "overdue"]
    assert r.data[0]["severity"] == "overdue"


@pytest.mark.django_db
def test_paying_fixed_invoice_clears_fixed_reminder(api, client_obj):
    today = timezone.localdate()
    p = Project.objects.create(
        title="Website", client=client_obj, billing_type="fixed", payment_amount=Decimal("25000"), due_date=today,
    )
    inv_id = api.post(f"/api/projects/{p.id}/generate_invoice/", {"kind": "fixed"}, format="json").data["id"]
    # Once invoiced, it becomes a draft reminder instead of a fixed_due one.
    assert [x["type"] for x in api.get("/api/reminders/").data] == ["draft"]
    api.post(f"/api/invoices/{inv_id}/mark_sent/")
    api.post(f"/api/invoices/{inv_id}/mark_paid/")
    assert api.get("/api/reminders/").data == []
    p.refresh_from_db()
    assert p.payment_status == "paid"


@pytest.mark.django_db
def test_profile_and_dashboard(api, client_obj, user):
    r = api.put("/api/profile/", {"business_name": "JBN Media", "invoice_prefix": "JBN"}, format="json")
    assert r.status_code == 200 and r.data["business_name"] == "JBN Media"
    p = Project.objects.create(
        title="R", client=client_obj, billing_type="monthly", monthly_amount=Decimal("6000"), payment_currency="KES",
    )
    inv = api.post(f"/api/projects/{p.id}/generate_invoice/", {"kind": "monthly"}, format="json").data
    assert inv["number"] == "JBN-0001"
    api.post(f"/api/invoices/{inv['id']}/mark_sent/")
    d = api.get("/api/dashboard/").data
    assert d["counts"]["clients"] == 1
    assert d["totals"][0]["currency"] == "KES"
    assert Decimal(d["totals"][0]["outstanding"]) == Decimal("6000.00")
    assert Decimal(d["mrr"][0]["amount"]) == Decimal("6000.00")
