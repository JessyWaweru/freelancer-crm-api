<div align="center">

# 💼 Freelancer CRM · API

### Know who owes you what, and when to bill them.

**Django REST API for freelancers to track clients, projects, invoices and payments, with reminders when an invoice is due and each user's data fully walled off from everyone else's.**

[![Live Demo](https://img.shields.io/badge/🔴_Live_Demo-Freelancer_CRM-7c3aed?style=for-the-badge)](https://jessywaweru.github.io/freelancer-crm-ui/)
[![Frontend](https://img.shields.io/badge/Frontend_Repo-freelancer--crm--ui-0f172a?style=for-the-badge&logo=github)](https://github.com/JessyWaweru/freelancer-crm-ui)

![Python](https://img.shields.io/badge/Python-3776AB?logo=python&logoColor=white)
![Django](https://img.shields.io/badge/Django_5.2-092E20?logo=django&logoColor=white)
![DRF](https://img.shields.io/badge/DRF-A30000?logo=django&logoColor=white)
![JWT](https://img.shields.io/badge/SimpleJWT-000000?logo=jsonwebtokens&logoColor=white)
![PostgreSQL](https://img.shields.io/badge/PostgreSQL-4169E1?logo=postgresql&logoColor=white)
![pytest](https://img.shields.io/badge/pytest-0A9EDC?logo=pytest&logoColor=white)

<sub>⏳ API on Render's free tier: the first request may take ~45s to wake up. Invoices wait for no one, but this one needs a minute. ☕</sub>

</div>

---

## 🔒 Your clients are *your* clients

Multi-tenant data isolation is enforced at **three** layers, not just in the UI:

| Layer | How |
|---|---|
| **Querysets** | `Client`, `Project` and `Invoice` queries are always filtered by `owner` / `client__owner`, so other users' rows are never even fetched |
| **Object permission** | A custom `IsOwner` permission guards every client detail route |
| **Write validation** | Projects and invoices can only be attached to clients **you** own. Point one at someone else's client ID and the API rejects it |

Every one of these is covered by tests.

## 💸 What it tracks

- 👥 **Clients**: name, email, phone, company
- 📁 **Projects**: status (active / completed / on-hold), start & due dates, linked to a client
- 💱 **Payments in USD, KES, EUR or GBP**, with status `paid` / `unpaid` / `partial`
- 🔎 Filter projects per client with `?client=<id>`
- 🔁 **Billing per project**: a fixed fee, a monthly retainer billed on a chosen day, or both
- 🧾 **Invoices** with line items and per-user numbering (`INV-0001`…), moving draft → sent → paid (or void). Paying a fixed-fee invoice marks its project paid
- ⚡ **One-click generation** from a project: the fixed fee, or the next month's retainer (which moves the next invoice date forward a month)
- 🔔 **Reminders feed**: retainers coming due, project fees not yet invoiced, drafts not sent, and sent invoices past their due date, most urgent first
- 📊 **Dashboard totals** per currency (never mixed): outstanding, overdue, paid this month, monthly retainer income

## 📡 API

| Method | Endpoint | What it does | Access |
|:---:|---|---|:---:|
| `POST` | `/api/register/` | Create an account | 🌍 |
| `POST` | `/api/auth/token/` · `/api/auth/refresh/` | JWT login / refresh | 🌍 |
| `GET` `POST` `PATCH` `DELETE` | `/api/clients/…` | Your clients | 🔐 |
| `GET` `POST` `PATCH` `DELETE` | `/api/projects/…?client=<id>` | Your projects | 🔐 |
| `POST` | `/api/projects/<id>/generate_invoice/` | Draft an invoice: `{"kind": "fixed" \| "monthly"}` | 🔐 |
| `GET` `POST` `PATCH` `DELETE` | `/api/invoices/…?client=&project=&status=` | Your invoices, with nested `items` | 🔐 |
| `POST` | `/api/invoices/<id>/mark_sent/` · `mark_paid/` · `void/` | Move an invoice along | 🔐 |
| `GET` | `/api/reminders/` | What needs invoicing or chasing | 🔐 |
| `GET` | `/api/dashboard/` | Money totals per currency | 🔐 |
| `GET` `PUT` | `/api/profile/` | Business details on invoices, numbering, terms, reminder lead time | 🔐 |
| `GET` | `/api/health/` | Uptime ping | 🌍 |

## 🚀 Run it locally

```bash
git clone https://github.com/JessyWaweru/freelancer-crm-api.git
cd freelancer-crm-api
python -m venv venv && source venv/bin/activate
pip install -r requirements.txt
echo "DEBUG=True" > .env
python manage.py migrate
python manage.py runserver        # → http://127.0.0.1:8000/api/
```

Production: `SECRET_KEY`, `DATABASE_URL` (Postgres via `dj-database-url`), `DEBUG=False`. Render starts it with the [`Procfile`](Procfile) (Gunicorn).

## 🧪 Tests

```bash
pip install pytest pytest-django pytest-cov
pytest            # auth, clients, projects, invoices, billing rules and cross-user isolation
```

---

<div align="center">

**Built by Jessy Bryan Waweru**: actuary by training, full-stack engineer by obsession.

[![LinkedIn](https://img.shields.io/badge/LinkedIn-0A66C2?logo=linkedin&logoColor=white)](https://www.linkedin.com/in/jessy-waweru-b8051a288)
[![Email](https://img.shields.io/badge/Email-EA4335?logo=gmail&logoColor=white)](mailto:bryanjessywaweru@gmail.com)
[![GitHub](https://img.shields.io/badge/More_projects-181717?logo=github)](https://github.com/JessyWaweru)

</div>
