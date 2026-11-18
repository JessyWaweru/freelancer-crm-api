"""Billing rules: generating invoices from projects and working out what needs sending.

Kept out of views.py so the rules are easy to read and test on their own.
"""
import calendar
import secrets
from datetime import date, datetime, time, timedelta
from decimal import Decimal

from django.db import transaction
from django.db.models import Exists, OuterRef
from django.utils import timezone

from .models import BusinessProfile, Invoice, InvoiceItem, Payment, Project, ProjectTodo, RetainerPayment

# Safety cap on how many months a retainer schedule is walked (50 years).
MAX_PERIODS = 600


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


# ── Monthly retainers ────────────────────────────────────────────────
# A retainer is a schedule of monthly due dates: retainer_start, then the billing_day of each
# following month, up to retainer_end if the project has ended. Each date is one payment.
# From its date onward it counts as unpaid until a RetainerPayment exists for it (the user
# marks it paid, or pays the invoice for that month).

def scheduled_date(project, i):
    """The i-th due date of a retainer (0 = retainer_start)."""
    if i == 0:
        return project.retainer_start
    return add_months(project.retainer_start, i, project.billing_day)


def retainer_schedule(project, until):
    """Due dates from the start of the retainer up to and including `until`."""
    if not project.is_recurring or not project.retainer_start:
        return []
    end = min(until, project.retainer_end) if project.retainer_end else until
    dates = []
    for i in range(MAX_PERIODS):
        d = scheduled_date(project, i)
        if d > end:
            break
        dates.append(d)
    return dates


def period_end(project, start):
    """Last day covered by the month that starts on `start`."""
    return add_months(start, 1, project.billing_day) - timedelta(days=1)


def paid_periods(project):
    # .all() so a prefetch_related("retainer_payments") is reused.
    return {rp.period_start for rp in project.retainer_payments.all()}


def unpaid_periods(project, today=None):
    """Due dates up to today that haven't been paid."""
    if project.monthly_amount <= 0:
        return []
    paid = paid_periods(project)
    return [d for d in retainer_schedule(project, today or timezone.localdate()) if d not in paid]


def _open_monthly_invoices(project):
    return Invoice.objects.filter(project=project, kind="monthly").exclude(status="void").exclude(period_start=None)


def sync_next_invoice_date(project):
    """Point next_invoice_date at the first month that is neither invoiced nor paid."""
    if not project.is_recurring or not project.retainer_start:
        return
    # Fresh queries, not paid_periods(): the project may carry a prefetched (now stale) payment list.
    paid = set(RetainerPayment.objects.filter(project=project).values_list("period_start", flat=True))
    covered = paid | set(_open_monthly_invoices(project).values_list("period_start", flat=True))
    nxt = None
    for i in range(MAX_PERIODS):
        d = scheduled_date(project, i)
        if project.retainer_end and d > project.retainer_end:
            break
        if d not in covered:
            nxt = d
            break
    if project.next_invoice_date != nxt:
        project.next_invoice_date = nxt
        project.save(update_fields=["next_invoice_date"])


@transaction.atomic
def set_retainer_paid(project, period, paid=True):
    """Mark one month of a retainer paid (or undo it). Also settles (or reopens) that month's invoice."""
    today = timezone.localdate()
    if period not in retainer_schedule(project, today + timedelta(days=366)):
        raise ValueError("That date isn't one of this retainer's payment dates.")
    invoice = _open_monthly_invoices(project).filter(period_start=period).first()
    if paid:
        if invoice and invoice.status != "paid":
            if invoice.balance > 0:
                # A payment for what's left; it pays the month through record_invoice_paid.
                invoice = record_payment(invoice, invoice.balance, method="other")
            else:
                invoice.status = "paid"
                invoice.paid_at = invoice.paid_at or timezone.now()
                invoice.save(update_fields=["status", "paid_at"])
        RetainerPayment.objects.get_or_create(
            project=project, period_start=period,
            defaults={"amount": invoice.total if invoice else project.monthly_amount, "invoice": invoice},
        )
    else:
        RetainerPayment.objects.filter(project=project, period_start=period).delete()
        if invoice and invoice.status == "paid":
            # Undoing the month undoes the payment on its invoice too, so earnings stay right.
            invoice.payments.all().delete()
            invoice.status, invoice.paid_at = "sent", None
            invoice.save(update_fields=["status", "paid_at"])
    sync_next_invoice_date(project)


def record_invoice_paid(invoice):
    """Paying a monthly invoice pays that month of the retainer."""
    project = invoice.project
    if invoice.kind != "monthly" or project is None or invoice.period_start is None:
        return
    RetainerPayment.objects.get_or_create(
        project=project, period_start=invoice.period_start,
        defaults={
            "amount": invoice.total, "invoice": invoice,
            "paid_on": timezone.localdate(invoice.paid_at) if invoice.paid_at else timezone.localdate(),
        },
    )
    sync_next_invoice_date(project)


# ── Payments ────────────────────────────────────────────────
# An invoice is paid when its payments add up to its total. Every change of payments goes
# through sync_invoice_status so the invoice, the project's fee status and the retainer month agree.

def _as_datetime(d):
    """A payment date as a timestamp: now for today, midday otherwise."""
    if d == timezone.localdate():
        return timezone.now()
    return timezone.make_aware(datetime.combine(d, time(12)))


def sync_invoice_status(invoice):
    """Mark the invoice paid once covered, or back to sent if a payment was removed."""
    invoice = Invoice.objects.select_related("project").prefetch_related("items", "payments").get(pk=invoice.pk)
    if invoice.status == "void":
        return invoice
    paid, total, project = invoice.amount_paid, invoice.total, invoice.project
    if paid > 0 and paid >= total:
        if invoice.status != "paid":
            last = list(invoice.payments.all())[-1].paid_on
            invoice.status = "paid"
            invoice.paid_at = _as_datetime(last)
            invoice.sent_at = invoice.sent_at or invoice.paid_at
            invoice.save(update_fields=["status", "paid_at", "sent_at"])
        if project and invoice.kind == "fixed":
            Project.objects.filter(pk=project.pk).update(payment_status="paid")
        record_invoice_paid(invoice)
    else:
        if invoice.status == "paid":
            invoice.status, invoice.paid_at = "sent", None
            invoice.save(update_fields=["status", "paid_at"])
            if invoice.kind == "monthly" and project:
                RetainerPayment.objects.filter(project=project, invoice=invoice).delete()
                sync_next_invoice_date(project)
        if project and invoice.kind == "fixed":
            Project.objects.filter(pk=project.pk).update(payment_status="partial" if paid > 0 else "unpaid")
    return invoice


@transaction.atomic
def record_payment(invoice, amount, paid_on=None, method="mpesa", reference=""):
    """Record money received against an invoice. Raises ValueError for amounts it can't take."""
    invoice = Invoice.objects.prefetch_related("items", "payments").get(pk=invoice.pk)
    amount = Decimal(str(amount)).quantize(Decimal("0.01"))
    if invoice.status == "void":
        raise ValueError("This invoice is void.")
    if amount <= 0:
        raise ValueError("Enter an amount above zero.")
    balance = invoice.balance
    if balance <= 0:
        raise ValueError("This invoice is already paid in full.")
    if amount > balance:
        raise ValueError(f"That's more than the {invoice.currency} {balance:,.2f} still owed.")
    if invoice.status == "draft":
        # Getting paid means the client has it.
        invoice.status, invoice.sent_at = "sent", invoice.sent_at or timezone.now()
        invoice.save(update_fields=["status", "sent_at"])
    Payment.objects.create(
        invoice=invoice, amount=amount, paid_on=paid_on or timezone.localdate(),
        method=method or "other", reference=reference or "",
    )
    return sync_invoice_status(invoice)


@transaction.atomic
def delete_payment(payment):
    invoice = payment.invoice
    payment.delete()
    return sync_invoice_status(invoice)


def ensure_public_token(invoice):
    if not invoice.public_token:
        invoice.public_token = secrets.token_urlsafe(18)
        invoice.save(update_fields=["public_token"])
    return invoice.public_token


@transaction.atomic
def duplicate_invoice(invoice):
    """A fresh draft with the same client, lines, tax and notes, dated today."""
    profile = get_profile(invoice.owner)
    today = timezone.localdate()
    copy = Invoice.objects.create(
        owner=invoice.owner, client=invoice.client, project=invoice.project, kind="custom",
        currency=invoice.currency, issue_date=today, due_date=today + timedelta(days=profile.payment_terms_days),
        notes=invoice.notes, discount=invoice.discount, tax_rate=invoice.tax_rate, tax_label=invoice.tax_label,
        number=next_invoice_number(invoice.owner),
    )
    InvoiceItem.objects.bulk_create([
        InvoiceItem(invoice=copy, description=i.description, quantity=i.quantity, unit_price=i.unit_price)
        for i in invoice.items.all()
    ])
    return copy


def retainer_overview(project):
    """Every month so far plus the next one, newest first, with paid/unpaid/upcoming status."""
    if not project.is_recurring or not project.retainer_start:
        return []
    today = timezone.localdate()
    dates = retainer_schedule(project, today)
    # Include the next upcoming month so it can be paid in advance.
    nxt = scheduled_date(project, len(dates))
    if len(dates) < MAX_PERIODS and (not project.retainer_end or nxt <= project.retainer_end):
        dates.append(nxt)
    payments = {rp.period_start: rp for rp in project.retainer_payments.all()}
    invoices = {inv.period_start: inv for inv in _open_monthly_invoices(project)}
    rows = []
    for d in reversed(dates):
        rp, inv = payments.get(d), invoices.get(d)
        rows.append({
            "period": d,
            "period_end": period_end(project, d),
            "amount": rp.amount if rp else project.monthly_amount,
            "status": "paid" if rp else "upcoming" if d > today else "unpaid",
            "paid_on": rp.paid_on if rp else None,
            "invoice": {"id": inv.id, "number": inv.number, "status": inv.status} if inv else None,
        })
    return rows


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
        tax_rate=profile.default_tax_rate,
        tax_label=profile.tax_label,
    )

    if kind == "monthly":
        if not project.is_recurring:
            raise ValueError("This project has no monthly billing.")
        if project.retainer_start and project.next_invoice_date is None:
            raise ValueError("Every month of this retainer is already invoiced or paid.")
        start = project.next_invoice_date or first_billing_date(project.billing_day, today)
        end = period_end(project, start)
        invoice.period_start, invoice.period_end = start, end
        span = f"{_fmt(start)} to {_fmt(end)}"
        parts = [i for i in project.items.all() if i.recurring]
        lines = [(f"{i.description} ({span})", i.amount) for i in parts] or [
            (f"{project.title}: monthly retainer ({span})", project.monthly_amount)
        ]
    elif kind == "fixed":
        if not project.has_fixed_fee:
            raise ValueError("This project has no fixed fee.")
        parts = [i for i in project.items.all() if not i.recurring]
        lines = [(i.description, i.amount) for i in parts] or [(f"{project.title}: project fee", project.payment_amount)]
    else:
        raise ValueError("kind must be 'fixed' or 'monthly'.")

    invoice.number = next_invoice_number(user)
    invoice.save()
    InvoiceItem.objects.bulk_create(
        [InvoiceItem(invoice=invoice, description=d, unit_price=a) for d, a in lines]
    )

    if kind == "monthly":
        if project.retainer_start:
            sync_next_invoice_date(project)
        else:
            # Older projects without a schedule: the next retainer is one month after this one.
            project.next_invoice_date = add_months(start, 1, project.billing_day)
            project.save(update_fields=["next_invoice_date"])

    return invoice


def build_reminders(user):
    """Everything that needs action today: invoices to send, drafts to send, payments to chase."""
    profile = get_profile(user)
    today = timezone.localdate()
    horizon = today + timedelta(days=profile.reminder_days_before)
    reminders = []

    def severity(d):
        return "overdue" if d < today else "due" if d == today else "upcoming"

    projects = (
        Project.objects.select_related("client").prefetch_related("retainer_payments")
        .filter(client__owner=user, status="active")
    )

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
            "period": p.next_invoice_date,
        })

    # 1b. Retainer months that are due and unpaid. Skips the month covered by the reminder above
    #     and months that already have an open invoice (the draft/overdue reminders cover those).
    open_periods = set(
        Invoice.objects.filter(owner=user, kind="monthly", status__in=["draft", "sent"])
        .values_list("project_id", "period_start")
    )
    recurring = (
        Project.objects.select_related("client").prefetch_related("retainer_payments")
        .filter(client__owner=user, billing_type__in=["monthly", "both"], monthly_amount__gt=0)
        .exclude(retainer_start=None)
    )
    for p in recurring:
        for d in unpaid_periods(p, today):
            if (p.id, d) in open_periods or (p.status == "active" and d == p.next_invoice_date):
                continue
            reminders.append({
                "id": f"retainer-{p.id}-{d}",
                "type": "retainer_unpaid",
                "severity": severity(d),
                "date": d,
                "title": f"{p.client.name}: {d:%B} payment unpaid",
                "detail": p.title,
                "amount": p.monthly_amount,
                "currency": p.payment_currency,
                "project": p.id,
                "invoice": None,
                "action": "mark_retainer_paid",
                "period": d,
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

    # 2b. Project deadlines.
    for p in projects.filter(due_date__lte=horizon):
        reminders.append({
            "id": f"deadline-{p.id}-{p.due_date}",
            "type": "deadline",
            "severity": severity(p.due_date),
            "date": p.due_date,
            "title": f"{p.title} deadline",
            "detail": p.client.name,
            "amount": None,
            "currency": p.payment_currency,
            "project": p.id,
            "invoice": None,
            "action": "open_project",
        })

    # 2c. To-dos with a due date.
    todos = (
        ProjectTodo.objects.select_related("project")
        .filter(project__client__owner=user, done=False, due_date__lte=horizon)
    )
    for t in todos:
        reminders.append({
            "id": f"todo-{t.id}-{t.due_date}",
            "type": "todo",
            "severity": severity(t.due_date),
            "date": t.due_date,
            "title": t.title,
            "detail": f"To-do · {t.project.title}",
            "amount": None,
            "currency": t.project.payment_currency,
            "project": t.project_id,
            "invoice": None,
            "action": "open_project",
        })

    invoices = Invoice.objects.select_related("client").prefetch_related("items", "payments").filter(owner=user)

    def owed_detail(inv, text):
        paid = inv.amount_paid
        return f"{text} · {inv.currency} {paid:,.0f} paid so far" if paid > 0 else text

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
            "detail": owed_detail(inv, f"{days} day{'s' if days != 1 else ''} overdue"),
            "amount": inv.balance,
            "currency": inv.currency,
            "project": inv.project_id,
            "invoice": inv.id,
            "action": "follow_up",
        })

    # 5. Sent invoices falling due soon.
    for inv in invoices.filter(status="sent", due_date__gte=today, due_date__lte=horizon):
        reminders.append({
            "id": f"due-{inv.id}-{inv.due_date}",
            "type": "invoice_due",
            "severity": severity(inv.due_date),
            "date": inv.due_date,
            "title": f"{inv.number} from {inv.client.name} is due",
            "detail": owed_detail(inv, "Waiting for payment"),
            "amount": inv.balance,
            "currency": inv.currency,
            "project": inv.project_id,
            "invoice": inv.id,
            "action": "follow_up",
        })

    order = {"overdue": 0, "due": 1, "upcoming": 2}
    reminders.sort(key=lambda r: (order[r["severity"]], r["date"]))
    return reminders


def earnings_ledger(user):
    """Every payment received, as (date, currency, amount, client_id, client_name).

    Counted once each: payments on invoices (on the day they came in, so part payments land in
    the right month); retainer months marked paid without a paid invoice; fixed fees marked paid
    on the project without any payment on an invoice for them.
    """
    rows = []
    paid_fee_projects = set()
    payments = Payment.objects.filter(invoice__owner=user).select_related("invoice__client")
    for pay in payments:
        inv = pay.invoice
        rows.append((pay.paid_on, inv.currency, pay.amount, inv.client_id, inv.client.name))
        if inv.kind != "monthly" and inv.project_id:
            paid_fee_projects.add(inv.project_id)

    retainer_payments = RetainerPayment.objects.filter(project__client__owner=user).select_related("project__client", "invoice")
    for rp in retainer_payments:
        if rp.invoice and rp.invoice.status == "paid":
            continue  # already counted with its invoice
        c = rp.project.client
        rows.append((rp.paid_on, rp.project.payment_currency, rp.amount, c.id, c.name))

    fees = (
        Project.objects.select_related("client")
        .filter(client__owner=user, payment_status="paid", billing_type__in=["fixed", "both"], payment_amount__gt=0)
        .exclude(id__in=paid_fee_projects)
    )
    for p in fees:
        # No payment date is stored for these; the deadline (or start) is the best guess.
        rows.append((p.due_date or p.start_date, p.payment_currency, p.payment_amount, p.client_id, p.client.name))
    return rows


def earnings_summary(user, months=12):
    """All-time, this year and this month earnings per currency, plus the last `months` months and top clients."""
    today = timezone.localdate()
    month_keys = [add_months(today.replace(day=1), -i) for i in range(months - 1, -1, -1)]
    out = {}
    for when, currency, amount, client_id, client_name in earnings_ledger(user):
        e = out.setdefault(currency, {
            "currency": currency,
            "all_time": Decimal("0.00"),
            "this_year": Decimal("0.00"),
            "this_month": Decimal("0.00"),
            "payments": 0,
            "by_month": {m: Decimal("0.00") for m in month_keys},
            "by_client": {},
        })
        e["all_time"] += amount
        e["payments"] += 1
        if when.year == today.year:
            e["this_year"] += amount
            if when.month == today.month:
                e["this_month"] += amount
        key = when.replace(day=1)
        if key in e["by_month"]:
            e["by_month"][key] += amount
        cl = e["by_client"].setdefault(client_id, {"client": client_id, "name": client_name, "amount": Decimal("0.00")})
        cl["amount"] += amount
    for e in out.values():
        e["by_month"] = [{"month": m, "amount": a} for m, a in e["by_month"].items()]
        e["by_client"] = sorted(e["by_client"].values(), key=lambda c: c["amount"], reverse=True)[:5]
    return list(out.values())


def dashboard_totals(user):
    """Money totals per currency (never mixed across currencies)."""
    today = timezone.localdate()
    totals = {}
    invoices = list(
        Invoice.objects.select_related("client").prefetch_related("items", "payments")
        .filter(owner=user).exclude(status="void")
    )
    for inv in invoices:
        t = totals.setdefault(inv.currency, {
            "currency": inv.currency,
            "outstanding": Decimal("0.00"),
            "overdue": Decimal("0.00"),
            "paid_this_month": Decimal("0.00"),
            "draft": Decimal("0.00"),
        })
        if inv.status == "sent":
            balance = inv.balance
            t["outstanding"] += balance
            if inv.due_date < today:
                t["overdue"] += balance
        elif inv.status == "draft":
            t["draft"] += inv.total

    # Monthly recurring revenue from active retainers.
    mrr = {}
    for p in Project.objects.filter(client__owner=user, status="active", billing_type__in=["monthly", "both"]):
        mrr[p.payment_currency] = mrr.get(p.payment_currency, Decimal("0.00")) + p.monthly_amount

    earnings = earnings_summary(user)
    # "Paid this month" counts every payment, not only invoices.
    for e in earnings:
        totals.setdefault(e["currency"], {
            "currency": e["currency"], "outstanding": Decimal("0.00"), "overdue": Decimal("0.00"),
            "paid_this_month": Decimal("0.00"), "draft": Decimal("0.00"),
        })["paid_this_month"] = e["this_month"]

    return {
        "earnings": earnings,
        "totals": list(totals.values()),
        "mrr": [{"currency": c, "amount": a} for c, a in mrr.items() if a > 0],
        "forecast": cash_forecast(user, invoices),
    }


FORECAST_DAYS = 30


def cash_forecast(user, invoices=None):
    """Money expected in the next 30 days per currency, and what's already late.

    Counts sent invoices (their balance) and retainer months that aren't invoiced yet.
    """
    today = timezone.localdate()
    horizon = today + timedelta(days=FORECAST_DAYS)
    if invoices is None:
        invoices = Invoice.objects.select_related("client").prefetch_related("items", "payments").filter(owner=user)
    out = {}

    def add(currency, when, amount, label, detail, link):
        f = out.setdefault(currency, {
            "currency": currency, "late": Decimal("0.00"), "next_30_days": Decimal("0.00"), "items": [],
        })
        f["late" if when < today else "next_30_days"] += amount
        f["items"].append({"date": when, "amount": amount, "label": label, "detail": detail, "link": link})

    invoiced_months = set()
    for inv in invoices:
        if inv.kind == "monthly" and inv.status != "void":
            invoiced_months.add((inv.project_id, inv.period_start))
        if inv.status != "sent" or inv.due_date > horizon:
            continue
        balance = inv.balance
        if balance > 0:
            add(inv.currency, inv.due_date, balance, inv.client.name, inv.number, f"/app/invoices/{inv.id}")

    recurring = (
        Project.objects.select_related("client").prefetch_related("retainer_payments")
        .filter(client__owner=user, status="active", billing_type__in=["monthly", "both"], monthly_amount__gt=0)
        .exclude(retainer_start=None)
    )
    for p in recurring:
        paid = paid_periods(p)
        for d in retainer_schedule(p, horizon):
            if d in paid or (p.id, d) in invoiced_months:
                continue
            add(p.payment_currency, d, p.monthly_amount, p.client.name, f"{d:%B} retainer · {p.title}", f"/app/projects/{p.id}")

    for f in out.values():
        f["items"].sort(key=lambda i: i["date"])
    return list(out.values())
