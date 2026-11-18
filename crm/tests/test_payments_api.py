from datetime import timedelta
from decimal import Decimal

import pytest
from django.contrib.auth import get_user_model
from django.utils import timezone
from rest_framework.test import APIClient

from crm.billing import add_months
from crm.models import Client, Invoice, Project, RetainerPayment

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


def make_invoice(api, client_obj, price="10000", **extra):
    r = api.post("/api/invoices/", {
        "client": client_obj.id, "currency": "KES", "due_date": (timezone.localdate() + timedelta(days=14)).isoformat(),
        "items": [{"description": "Website", "quantity": 1, "unit_price": price}], **extra,
    }, format="json")
    assert r.status_code == 201, r.data
    return r.data


def pay(api, inv, amount, **extra):
    return api.post(f"/api/invoices/{inv['id']}/payments/", {"amount": amount, **extra}, format="json")


@pytest.mark.django_db
def test_part_payments_until_paid(api, client_obj):
    inv = make_invoice(api, client_obj)
    r = pay(api, inv, "4000", method="mpesa", reference="QK12AB")
    assert r.status_code == 200, r.data
    # A draft that gets paid has obviously been sent.
    assert r.data["status"] == "sent"
    assert Decimal(r.data["amount_paid"]) == Decimal("4000") and Decimal(r.data["balance"]) == Decimal("6000")
    assert r.data["payments"][0]["reference"] == "QK12AB"

    d = api.get("/api/dashboard/").data
    assert Decimal(str(d["totals"][0]["outstanding"])) == Decimal("6000")
    assert Decimal(str(d["earnings"][0]["all_time"])) == Decimal("4000")

    r = pay(api, inv, "6000")
    assert r.data["status"] == "paid" and r.data["paid_at"] is not None
    assert Decimal(r.data["balance"]) == 0
    assert Decimal(str(api.get("/api/dashboard/").data["earnings"][0]["all_time"])) == Decimal("10000")


@pytest.mark.django_db
def test_cannot_overpay_or_pay_nothing(api, client_obj):
    inv = make_invoice(api, client_obj)
    assert pay(api, inv, "10000.01").status_code == 400
    assert pay(api, inv, "0").status_code == 400
    pay(api, inv, "10000")
    assert pay(api, inv, "1").status_code == 400  # already paid in full


@pytest.mark.django_db
def test_removing_a_payment_reopens_the_invoice(api, client_obj):
    inv = make_invoice(api, client_obj)
    paid = pay(api, inv, "10000").data
    assert paid["status"] == "paid"
    r = api.delete(f"/api/invoices/{inv['id']}/payments/{paid['payments'][0]['id']}/")
    assert r.status_code == 200
    assert r.data["status"] == "sent" and r.data["paid_at"] is None and Decimal(r.data["balance"]) == Decimal("10000")


@pytest.mark.django_db
def test_mark_paid_records_the_balance(api, client_obj):
    inv = make_invoice(api, client_obj)
    pay(api, inv, "2500")
    r = api.post(f"/api/invoices/{inv['id']}/mark_paid/")
    assert r.data["status"] == "paid"
    assert [Decimal(p["amount"]) for p in r.data["payments"]] == [Decimal("2500"), Decimal("7500")]


@pytest.mark.django_db
def test_fixed_fee_project_tracks_part_payment(api, client_obj):
    p = Project.objects.create(title="Site", client=client_obj, billing_type="fixed",
                               payment_amount=Decimal("8000"), payment_currency="KES")
    inv = api.post(f"/api/projects/{p.id}/generate_invoice/", {"kind": "fixed"}, format="json").data
    pay(api, inv, "3000")
    assert Project.objects.get(pk=p.id).payment_status == "partial"
    pay(api, inv, "5000")
    assert Project.objects.get(pk=p.id).payment_status == "paid"
    # Counted once in earnings, through the payments.
    e = api.get("/api/dashboard/").data["earnings"][0]
    assert Decimal(str(e["all_time"])) == Decimal("8000") and e["payments"] == 2


@pytest.mark.django_db
def test_tax_and_discount(api, client_obj, user):
    api.put("/api/profile/", {"default_tax_rate": "16", "tax_label": "VAT"}, format="json")
    inv = make_invoice(api, client_obj, price="10000", discount="1000")
    assert Decimal(inv["tax_rate"]) == Decimal("16") and inv["tax_label"] == "VAT"
    assert Decimal(inv["subtotal"]) == Decimal("10000")
    assert Decimal(inv["tax_amount"]) == Decimal("1440")  # 16% of 9000
    assert Decimal(inv["total"]) == Decimal("10440")
    assert api.patch(f"/api/invoices/{inv['id']}/", {"tax_rate": "120"}, format="json").status_code == 400
    assert api.patch(f"/api/invoices/{inv['id']}/", {"discount": "-5"}, format="json").status_code == 400


@pytest.mark.django_db
def test_share_link_is_public_and_counts_views(api, client_obj):
    inv = make_invoice(api, client_obj)
    token = api.post(f"/api/invoices/{inv['id']}/share/").data["public_token"]
    assert token and api.post(f"/api/invoices/{inv['id']}/share/").data["public_token"] == token  # stable

    anon = APIClient()
    r = anon.get(f"/api/public/invoices/{token}/")
    assert r.status_code == 200
    assert r.data["number"] == inv["number"] and r.data["client_name"] == "GoHerbal"
    assert "owner" not in r.data and "id" not in r.data and "public_token" not in r.data
    anon.get(f"/api/public/invoices/{token}/")
    anon.get(f"/api/public/invoices/{token}/?preview=1")  # the freelancer's own look doesn't count

    seen = api.get(f"/api/invoices/{inv['id']}/").data
    assert seen["view_count"] == 2 and seen["first_viewed_at"] and seen["last_viewed_at"]
    assert anon.get("/api/public/invoices/not-a-real-token/").status_code == 404


@pytest.mark.django_db
def test_duplicate_makes_a_fresh_draft(api, client_obj):
    inv = make_invoice(api, client_obj, discount="500")
    pay(api, inv, "10000")
    r = api.post(f"/api/invoices/{inv['id']}/duplicate/")
    assert r.status_code == 201
    assert r.data["status"] == "draft" and r.data["number"] != inv["number"]
    assert r.data["payments"] == [] and Decimal(r.data["discount"]) == Decimal("500")
    assert [i["description"] for i in r.data["items"]] == ["Website"]
    assert r.data["issue_date"] == timezone.localdate().isoformat()


@pytest.mark.django_db
def test_undoing_a_month_reopens_its_paid_invoice(api, client_obj):
    today = timezone.localdate()
    p = api.post("/api/projects/", {
        "title": "Care plan", "client": client_obj.id, "billing_type": "monthly", "monthly_amount": "6000",
        "payment_currency": "KES", "retainer_start": today.isoformat(),
    }, format="json").data
    inv = api.post(f"/api/projects/{p['id']}/generate_invoice/", {"kind": "monthly"}, format="json").data
    api.post(f"/api/projects/{p['id']}/retainer/", {"period": today.isoformat(), "paid": True}, format="json")
    assert Invoice.objects.get(pk=inv["id"]).status == "paid"
    assert Decimal(str(api.get("/api/dashboard/").data["earnings"][0]["all_time"])) == Decimal("6000")

    api.post(f"/api/projects/{p['id']}/retainer/", {"period": today.isoformat(), "paid": False}, format="json")
    assert Invoice.objects.get(pk=inv["id"]).status == "sent"
    assert not RetainerPayment.objects.filter(project_id=p["id"]).exists()
    assert api.get("/api/dashboard/").data["earnings"] == []


@pytest.mark.django_db
def test_forecast_counts_sent_balances_and_upcoming_months(api, client_obj):
    today = timezone.localdate()
    inv = make_invoice(api, client_obj, price="5000")
    pay(api, inv, "1000")  # 4000 left, due in 14 days
    late = make_invoice(api, client_obj, price="700")
    api.post(f"/api/invoices/{late['id']}/mark_sent/")
    Invoice.objects.filter(pk=late["id"]).update(issue_date=today - timedelta(days=20), due_date=today - timedelta(days=3))
    make_invoice(api, client_obj, price="999")  # a draft isn't expected money yet
    api.post("/api/projects/", {
        "title": "Care plan", "client": client_obj.id, "billing_type": "monthly", "monthly_amount": "2000",
        "payment_currency": "KES", "retainer_start": (today + timedelta(days=5)).isoformat(),
    }, format="json")

    f = api.get("/api/dashboard/").data["forecast"][0]
    assert f["currency"] == "KES"
    assert Decimal(str(f["late"])) == Decimal("700")
    expected = Decimal("4000") + Decimal("2000") * len(
        [d for d in [today + timedelta(days=5), add_months(today + timedelta(days=5), 1)] if d <= today + timedelta(days=30)]
    )
    assert Decimal(str(f["next_30_days"])) == expected
    assert [i["date"] for i in f["items"]] == sorted(i["date"] for i in f["items"])


@pytest.mark.django_db
def test_client_summary_counts_every_kind_of_payment(api, client_obj, user):
    inv = make_invoice(api, client_obj, price="5000")
    pay(api, inv, "2000")
    Project.objects.create(title="Logo", client=client_obj, billing_type="fixed", payment_amount=Decimal("3000"),
                           payment_status="paid", payment_currency="KES", due_date=timezone.localdate())
    other = Client.objects.create(name="Other", owner=user, phone="2")
    make_invoice(api, other, price="999")
    e = api.get(f"/api/clients/{client_obj.id}/summary/").data["earned"]
    assert len(e) == 1 and Decimal(str(e[0]["paid"])) == Decimal("5000") and e[0]["payments"] == 2
    intruder = APIClient()
    intruder.force_authenticate(User.objects.create_user(username="x", password="x"))
    assert intruder.get(f"/api/clients/{client_obj.id}/summary/").status_code == 404
