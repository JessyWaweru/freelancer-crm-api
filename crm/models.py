from decimal import Decimal
from django.db import models
from django.contrib.auth import get_user_model
from django.utils import timezone
#get_user_model() → Returns the active User model.

User = get_user_model()


CURRENCY_CHOICES = [
    ("USD", "USD - US Dollar"),
    ("KES", "KES - Kenyan Shilling"),
    ("EUR", "EUR - Euro"),
    ("GBP", "GBP - British Pound"),
    # add more as needed
]

PAYMENT_CHOICES = [
        ("paid", "Paid"),
        ("unpaid", "Unpaid"),
        ("partial", "Partially paid"),
    ]

# How a project is billed:
#   fixed   → one invoice for the agreed project fee (payment_amount)
#   monthly → a retainer invoice every month (monthly_amount)
#   both    → a one-off fee plus a monthly retainer
BILLING_CHOICES = [
    ("fixed", "Fixed fee"),
    ("monthly", "Monthly retainer"),
    ("both", "Fixed fee + monthly"),
]

class Client(models.Model):
    #You’re creating a Python class (Client) that inherits from models.Model.
    #This inheritance is what gives your class all the database superpowers.
    #Each class you create that inherits from models.Model becomes a table in your database.
    #Each attribute inside the class (like name, email, etc.) becomes a column in that table.
    owner = models.ForeignKey(User, on_delete=models.CASCADE, related_name="clients")
    #ForeignKey(User) → Links the client to the user who owns them (like the freelancer).
    #on_delete=models.CASCADE → If the user is deleted, all their clients will also be 
    # deleted.
    name = models.CharField(max_length=200)
    email = models.EmailField(blank=True)
    #blank=True means the field can be left empty in forms.
    phone = models.CharField(max_length=50)
    company = models.CharField(max_length=255, blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)

class Project(models.Model):
    client = models.ForeignKey(Client, on_delete=models.CASCADE, related_name="projects")
    title = models.CharField(max_length=200)
    status = models.CharField( default="active")  # active/completed/on-hold
   
    due_date = models.DateField(null=True, blank=True)
    start_date = models.DateField(auto_now_add=True, null=False, blank=False)
     # NEW field: currency (3-letter ISO code). default prevents migration prompt
    payment_currency = models.CharField(max_length=3, choices=CURRENCY_CHOICES, default="USD")


    payment_status = models.CharField(max_length=10, choices=PAYMENT_CHOICES, default="unpaid")


    payment_amount = models.DecimalField(
        max_digits=10, decimal_places=2, default=Decimal("0.00")
    )
    # payment_amount is the fixed project fee. The fields below drive recurring billing.
    billing_type = models.CharField(max_length=10, choices=BILLING_CHOICES, default="fixed")
    monthly_amount = models.DecimalField(max_digits=10, decimal_places=2, default=Decimal("0.00"))
    # Day of the month the retainer invoice goes out (1–28 so it exists in every month).
    billing_day = models.PositiveSmallIntegerField(default=1)
    # The date the next monthly invoice should be sent. Moves forward a month each time one is generated.
    next_invoice_date = models.DateField(null=True, blank=True)

    @property
    def is_recurring(self):
        return self.billing_type in ("monthly", "both")

    @property
    def has_fixed_fee(self):
        return self.billing_type in ("fixed", "both")


class BusinessProfile(models.Model):
    """The freelancer's own details, printed on every invoice."""
    owner = models.OneToOneField(User, on_delete=models.CASCADE, related_name="business_profile")
    business_name = models.CharField(max_length=200, blank=True, default="")
    email = models.EmailField(blank=True, default="")
    phone = models.CharField(max_length=50, blank=True, default="")
    address = models.TextField(blank=True, default="")
    # e.g. M-Pesa till / paybill, bank details. Shown at the bottom of invoices.
    payment_details = models.TextField(blank=True, default="")
    invoice_prefix = models.CharField(max_length=10, default="INV")
    next_invoice_number = models.PositiveIntegerField(default=1)
    default_currency = models.CharField(max_length=3, choices=CURRENCY_CHOICES, default="KES")
    payment_terms_days = models.PositiveSmallIntegerField(default=14)
    # How many days before an invoice is due to be sent the reminder appears.
    reminder_days_before = models.PositiveSmallIntegerField(default=3)


INVOICE_STATUS_CHOICES = [
    ("draft", "Draft"),
    ("sent", "Sent"),
    ("paid", "Paid"),
    ("void", "Void"),
]

INVOICE_KIND_CHOICES = [
    ("fixed", "Fixed fee"),
    ("monthly", "Monthly retainer"),
    ("custom", "Custom"),
]


class Invoice(models.Model):
    owner = models.ForeignKey(User, on_delete=models.CASCADE, related_name="invoices")
    client = models.ForeignKey(Client, on_delete=models.CASCADE, related_name="invoices")
    project = models.ForeignKey(
        Project, on_delete=models.SET_NULL, null=True, blank=True, related_name="invoices"
    )
    number = models.CharField(max_length=30)
    kind = models.CharField(max_length=10, choices=INVOICE_KIND_CHOICES, default="custom")
    status = models.CharField(max_length=10, choices=INVOICE_STATUS_CHOICES, default="draft")
    currency = models.CharField(max_length=3, choices=CURRENCY_CHOICES, default="USD")
    issue_date = models.DateField(default=timezone.localdate)
    due_date = models.DateField()
    # Only for monthly invoices: the month being billed.
    period_start = models.DateField(null=True, blank=True)
    period_end = models.DateField(null=True, blank=True)
    notes = models.TextField(blank=True, default="")
    sent_at = models.DateTimeField(null=True, blank=True)
    paid_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-issue_date", "-id"]
        constraints = [
            models.UniqueConstraint(fields=["owner", "number"], name="unique_invoice_number_per_owner"),
        ]

    @property
    def total(self):
        return sum((item.amount for item in self.items.all()), Decimal("0.00"))

    @property
    def is_overdue(self):
        return self.status == "sent" and self.due_date < timezone.localdate()

    def __str__(self):
        return self.number


class InvoiceItem(models.Model):
    invoice = models.ForeignKey(Invoice, on_delete=models.CASCADE, related_name="items")
    description = models.CharField(max_length=300)
    quantity = models.DecimalField(max_digits=10, decimal_places=2, default=Decimal("1.00"))
    unit_price = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal("0.00"))

    class Meta:
        ordering = ["id"]

    @property
    def amount(self):
        return (self.quantity * self.unit_price).quantize(Decimal("0.01"))

