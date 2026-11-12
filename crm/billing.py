"""Billing rules: generating invoices from projects and working out what needs sending.

Kept out of views.py so the rules are easy to read and test on their own.
"""
import calendar
from datetime import date, timedelta
from decimal import Decimal

from django.db import transaction
from django.db.models import Exists, OuterRef
from django.utils import timezone

from .models import BusinessProfile, Invoice, InvoiceItem, Project


def get_profile(user):
    profile, _ = BusinessProfile.objects.get_or_create(owner=user)
    return profile


def add_months(d, months, day=None):
    """Move a date by whole months, clamping the day to the target month's length."""
    month_index = d.month - 1 + months
    year = d.year + month_index // 12
    month = month_index % 12 + 1
    last_day = calendar.monthrange(year, month)[1]
    return date(year, month, min(day or d.day, last_day))


def first_billing_date(billing_day, start):
    """The first date on or after `start` that falls on `billing_day`."""
    candidate = add_months(start, 0, billing_day)
    if candidate < start:
        candidate = add_months(start, 1, billing_day)
    return candidate


def next_invoice_number(user):
    """Reserve the next invoice number for this user (safe under concurrent requests)."""
    with transaction.atomic():
        profile = BusinessProfile.objects.select_for_update().get(pk=get_profile(user).pk)
        while True:
            number = f"{profile.invoice_prefix}-{profile.next_invoice_number:04d}"
            profile.next_invoice_number += 1
            if not Invoice.objects.filter(owner=user, number=number).exists():
                break
        profile.save(update_fields=["next_invoice_number"])
        return number


def _fmt(d):
    return f"{d.day} {d:%b %Y}"


@transaction.atomic
def generate_invoice(project: Project, kind: str) -> Invoice:
    """Create a draft invoice for a project's fixed fee or its next monthly retainer."""
    user = project.client.owner
    profile = get_profile(user)
    today = timezone.localdate()

    invoice = Invoice(
        owner=user,
        client=project.client,
        project=project,
        kind=kind,
        currency=project.payment_currency,
        issue_date=today,
        due_date=today + timedelta(days=profile.payment_terms_days),
    )

    if kind == "monthly":
        if not project.is_recurring:
            raise ValueError("This project has no monthly billing.")
        period_start = project.next_invoice_date or first_billing_date(project.billing_day, today)
        period_end = add_months(period_start, 1, project.billing_day) - timedelta(days=1)
        invoice.period_start, invoice.period_end = period_start, period_end
        description = f"{project.title}: monthly retainer ({_fmt(period_start)} to {_fmt(period_end)})"
        amount = project.monthly_amount
    elif kind == "fixed":
        if not project.has_fixed_fee:
            raise ValueError("This project has no fixed fee.")
        description = f"{project.title}: project fee"
        amount = project.payment_amount
    else:
        raise ValueError("kind must be 'fixed' or 'monthly'.")

    invoice.number = next_invoice_number(user)
    invoice.save()
    InvoiceItem.objects.create(invoice=invoice, description=description, unit_price=amount)

    if kind == "monthly":
        # Next retainer is due one month after this period started.
        project.next_invoice_date = add_months(invoice.period_start, 1, project.billing_day)
        project.save(update_fields=["next_invoice_date"])

    return invoice


def build_reminders(user):
    """Everything that needs action today: invoices to send, drafts to send, payments to chase."""
    profile = get_profile(user)
    today = timezone.localdate()
    horizon = today + timedelta(days=profile.reminder_days_before)
    reminders = []

    projects = Project.objects.select_related("client").filter(client__owner=user, status="active")

    # 1. Monthly retainers coming up.
    for p in projects.filter(billing_type__in=["monthly", "both"], next_invoice_date__lte=horizon):
        if p.monthly_amount <= 0:
            continue
        reminders.append({
            "id": f"monthly-{p.id}-{p.next_invoice_date}",
            "type": "monthly_due",
            "severity": "overdue" if p.next_invoice_date < today else "due" if p.next_invoice_date == today else "upcoming",
            "date": p.next_invoice_date,
            "title": f"Send {p.next_invoice_date:%B} retainer to {p.client.name}",
            "detail": p.title,
            "amount": p.monthly_amount,
            "currency": p.payment_currency,
            "project": p.id,
            "invoice": None,
            "action": "generate_monthly",
        })

    # 2. Fixed fees not invoiced yet, once the project is due soon (or has no due date but is done).
    has_fixed_invoice = Invoice.objects.filter(project=OuterRef("pk"), kind="fixed").exclude(status="void")
    fixed_projects = (
        Project.objects.select_related("client")
        .filter(client__owner=user, billing_type__in=["fixed", "both"], payment_amount__gt=0)
        .exclude(payment_status="paid")
        .annotate(invoiced=Exists(has_fixed_invoice))
        .filter(invoiced=False)
    )
    for p in fixed_projects:
        when = p.due_date
        if p.status == "completed":
            when = when or today
        if when is None or when > horizon:
            continue
        reminders.append({
            "id": f"fixed-{p.id}",
            "type": "fixed_due",
            "severity": "overdue" if when < today else "due" if when == today else "upcoming",
            "date": when,
            "title": f"Invoice {p.client.name} for {p.title}",
            "detail": "Project complete" if p.status == "completed" else f"Project due {_fmt(p.due_date)}",
            "amount": p.payment_amount,
            "currency": p.payment_currency,
            "project": p.id,
            "invoice": None,
            "action": "generate_fixed",
        })

    invoices = Invoice.objects.select_related("client").prefetch_related("items").filter(owner=user)

    # 3. Drafts waiting to go out.
    for inv in invoices.filter(status="draft"):
        reminders.append({
            "id": f"draft-{inv.id}",
            "type": "draft",
            "severity": "due",
            "date": inv.issue_date,
            "title": f"{inv.number} for {inv.client.name} is still a draft",
            "detail": "Review it and send it",
            "amount": inv.total,
            "currency": inv.currency,
            "project": inv.project_id,
            "invoice": inv.id,
            "action": "send",
        })

    # 4. Sent but not paid past the due date.
    for inv in invoices.filter(status="sent", due_date__lt=today):
        days = (today - inv.due_date).days
        reminders.append({
            "id": f"overdue-{inv.id}",
            "type": "overdue",
            "severity": "overdue",
            "date": inv.due_date,
            "title": f"{inv.client.name} hasn't paid {inv.number}",
            "detail": f"{days} day{'s' if days != 1 else ''} overdue",
            "amount": inv.total,
            "currency": inv.currency,
            "project": inv.project_id,
            "invoice": inv.id,
            "action": "follow_up",
        })

    order = {"overdue": 0, "due": 1, "upcoming": 2}
    reminders.sort(key=lambda r: (order[r["severity"]], r["date"]))
    return reminders


def dashboard_totals(user):
    """Money totals per currency (never mixed across currencies)."""
    today = timezone.localdate()
    month_start = today.replace(day=1)
    totals = {}
    for inv in Invoice.objects.prefetch_related("items").filter(owner=user).exclude(status="void"):
        t = totals.setdefault(inv.currency, {
            "currency": inv.currency,
            "outstanding": Decimal("0.00"),
            "overdue": Decimal("0.00"),
            "paid_this_month": Decimal("0.00"),
            "draft": Decimal("0.00"),
        })
        total = inv.total
        if inv.status == "sent":
            t["outstanding"] += total
            if inv.due_date < today:
                t["overdue"] += total
        elif inv.status == "draft":
            t["draft"] += total
        elif inv.status == "paid" and inv.paid_at and timezone.localdate(inv.paid_at) >= month_start:
            t["paid_this_month"] += total

    # Monthly recurring revenue from active retainers.
    mrr = {}
    for p in Project.objects.filter(client__owner=user, status="active", billing_type__in=["monthly", "both"]):
        mrr[p.payment_currency] = mrr.get(p.payment_currency, Decimal("0.00")) + p.monthly_amount

    return {
        "totals": list(totals.values()),
        "mrr": [{"currency": c, "amount": a} for c, a in mrr.items() if a > 0],
    }
