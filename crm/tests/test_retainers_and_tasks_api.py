"""Monthly retainer payments, project price breakdowns, notes, to-dos and deleting invoices."""
from datetime import date, timedelta
from decimal import Decimal

import pytest
from django.contrib.auth import get_user_model
from django.utils import timezone
from rest_framework.test import APIClient

from crm.billing import add_months
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
    return Client.objects.create(name="GoHerbal", owner=user, phone="+254700000000")


def make_retainer(api, client_obj, start, amount="6000"):
    r = api.post("/api/projects/", {
        "title": "Retainer", "client": client_obj.id, "billing_type": "monthly",
        "monthly_amount": amount, "retainer_start": start.isoformat(), "payment_currency": "KES",
    }, format="json")
    assert r.status_code == 201, r.data
    return r.data


@pytest.mark.django_db
def test_retainer_months_are_unpaid_from_their_date_until_marked_paid(api, client_obj):
    today = timezone.localdate()
    start = add_months(today, -2)  # two months ago: that month, last month and this month are due
    data = make_retainer(api, client_obj, start)
    assert data["billing_day"] == start.day
    assert len(data["unpaid_periods"]) == 3
    assert Decimal(data["unpaid_retainer_total"]) == Decimal("18000")

    r = api.post(f"/api/projects/{data['id']}/retainer/", {"period": start.isoformat(), "paid": True}, format="json")
    assert r.status_code == 200
    assert len(r.data["project"]["unpaid_periods"]) == 2
    statuses = [m["status"] for m in r.data["months"]]
    assert statuses.count("paid") == 1 and statuses[0] == "upcoming"

    # Undo
    r = api.post(f"/api/projects/{data['id']}/retainer/", {"period": start.isoformat(), "paid": False}, format="json")
    assert len(r.data["project"]["unpaid_periods"]) == 3


@pytest.mark.django_db
def test_future_retainer_is_not_unpaid_yet(api, client_obj):
    data = make_retainer(api, client_obj, timezone.localdate() + timedelta(days=5))
    assert data["unpaid_periods"] == []
    assert data["next_invoice_date"] == (timezone.localdate() + timedelta(days=5)).isoformat()


@pytest.mark.django_db
def test_paying_monthly_invoice_pays_that_month(api, client_obj):
    today = timezone.localdate()
    data = make_retainer(api, client_obj, today)
    inv = api.post(f"/api/projects/{data['id']}/generate_invoice/", {"kind": "monthly"}, format="json").data
    assert inv["period_start"] == today.isoformat()
    api.post(f"/api/invoices/{inv['id']}/mark_sent/")
    api.post(f"/api/invoices/{inv['id']}/mark_paid/")
    p = api.get(f"/api/projects/{data['id']}/").data
    assert p["unpaid_periods"] == []
    assert p["next_invoice_date"] == add_months(today, 1, today.day).isoformat()


@pytest.mark.django_db
def test_marking_month_paid_settles_its_invoice(api, client_obj):
    today = timezone.localdate()
    data = make_retainer(api, client_obj, today)
    inv = api.post(f"/api/projects/{data['id']}/generate_invoice/", {"kind": "monthly"}, format="json").data
    api.post(f"/api/projects/{data['id']}/retainer/", {"period": today.isoformat(), "paid": True}, format="json")
    assert Invoice.objects.get(pk=inv["id"]).status == "paid"


@pytest.mark.django_db
def test_unpaid_retainer_reminder(api, client_obj):
    today = timezone.localdate()
    data = make_retainer(api, client_obj, add_months(today, -1))
    # Last month was never invoiced: next_invoice_date points at it (monthly_due),
    # and this month shows as an unpaid payment.
    types = sorted(x["type"] for x in api.get("/api/reminders/").data)
    assert types == ["monthly_due", "retainer_unpaid"]
    api.post(f"/api/projects/{data['id']}/retainer/", {"period": add_months(today, -1).isoformat()}, format="json")
    api.post(f"/api/projects/{data['id']}/retainer/", {"period": today.isoformat()}, format="json")
    assert api.get("/api/reminders/").data == []


@pytest.mark.django_db
def test_completing_project_stops_new_retainer_months(api, client_obj):
    today = timezone.localdate()
    data = make_retainer(api, client_obj, add_months(today, -1))
    p = api.patch(f"/api/projects/{data['id']}/", {"status": "completed"}, format="json").data
    assert p["retainer_end"] == today.isoformat()
    project = Project.objects.get(pk=data["id"])
    project.retainer_end = add_months(today, -1)
    project.save()
    assert len(api.get(f"/api/projects/{data['id']}/").data["unpaid_periods"]) == 1


@pytest.mark.django_db
def test_price_breakdown_sets_fee_and_invoice_lines(api, client_obj):
    r = api.post("/api/projects/", {
        "title": "GoHerbal site", "client": client_obj.id, "billing_type": "fixed", "payment_currency": "KES",
        "items": [
            {"description": "Developer fees", "amount": "5000"},
            {"description": "Hosting", "amount": "4000"},
            {"description": "Domain", "amount": "1000"},
            {"description": "  ", "amount": "99"},  # blank lines are dropped
        ],
    }, format="json")
    assert r.status_code == 201, r.data
    assert Decimal(r.data["payment_amount"]) == Decimal("10000")
    assert [i["description"] for i in r.data["items"]] == ["Developer fees", "Hosting", "Domain"]

    inv = api.post(f"/api/projects/{r.data['id']}/generate_invoice/", {"kind": "fixed"}, format="json").data
    assert [(i["description"], Decimal(i["unit_price"])) for i in inv["items"]] == [
        ("Developer fees", Decimal("5000")), ("Hosting", Decimal("4000")), ("Domain", Decimal("1000")),
    ]
    assert Decimal(inv["total"]) == Decimal("10000")


@pytest.mark.django_db
def test_both_billing_splits_breakdown(api, client_obj):
    r = api.post("/api/projects/", {
        "title": "Shop", "client": client_obj.id, "billing_type": "both", "payment_currency": "KES",
        "retainer_start": (timezone.localdate() + timedelta(days=3)).isoformat(),
        "items": [
            {"description": "Build", "amount": "5000", "recurring": False},
            {"description": "Hosting", "amount": "4000", "recurring": True},
            {"description": "Support", "amount": "1000", "recurring": True},
        ],
    }, format="json")
    assert Decimal(r.data["payment_amount"]) == Decimal("5000")
    assert Decimal(r.data["monthly_amount"]) == Decimal("5000")
    inv = api.post(f"/api/projects/{r.data['id']}/generate_invoice/", {"kind": "monthly"}, format="json").data
    assert [i["description"].split(" (")[0] for i in inv["items"]] == ["Hosting", "Support"]


@pytest.mark.django_db
def test_notes_and_todos(api, client_obj, user):
    p = Project.objects.create(title="Site", client=client_obj)
    note = api.post("/api/notes/", {"project": p.id, "body": "Client wants green"}, format="json")
    assert note.status_code == 201
    assert len(api.get(f"/api/notes/?project={p.id}").data) == 1

    today = timezone.localdate()
    todo = api.post("/api/todos/", {"project": p.id, "title": "Send mockups", "due_date": today.isoformat()}, format="json").data
    reminders = api.get("/api/reminders/").data
    assert [r["type"] for r in reminders] == ["todo"] and reminders[0]["severity"] == "due"

    done = api.patch(f"/api/todos/{todo['id']}/", {"done": True}, format="json").data
    assert done["done_at"] is not None
    assert api.get("/api/reminders/").data == []

    # Other users can't see or attach to this project.
    other = APIClient()
    other.force_authenticate(User.objects.create_user(username="other", password="x"))
    assert other.get(f"/api/notes/?project={p.id}").data == []
    assert other.post("/api/todos/", {"project": p.id, "title": "x"}, format="json").status_code == 400


@pytest.mark.django_db
def test_deleting_monthly_invoice_frees_the_month(api, client_obj):
    today = timezone.localdate()
    data = make_retainer(api, client_obj, today)
    inv = api.post(f"/api/projects/{data['id']}/generate_invoice/", {"kind": "monthly"}, format="json").data
    assert api.get(f"/api/projects/{data['id']}/").data["next_invoice_date"] != today.isoformat()
    assert api.delete(f"/api/invoices/{inv['id']}/").status_code == 204
    assert api.get(f"/api/projects/{data['id']}/").data["next_invoice_date"] == today.isoformat()
