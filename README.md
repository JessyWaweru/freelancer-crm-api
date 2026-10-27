<div align="center">

# 💼 Freelancer CRM · API

### Know who owes you what, in any currency.

**Django REST API for freelancers to track clients, projects and payments, with each user's data fully walled off from everyone else's.**

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
| **Querysets** | `Client` and `Project` queries are always filtered by `owner` / `client__owner`, so other users' rows are never even fetched |
| **Object permission** | A custom `IsOwner` permission guards every client detail route |
| **Write validation** | A project can only be attached to a client **you** own. Point it at someone else's client ID and the API rejects it |

Every one of these is covered by tests.

## 💸 What it tracks

- 👥 **Clients**: name, email, phone, company
- 📁 **Projects**: status (active / completed / on-hold), start & due dates, linked to a client
- 💱 **Payments in USD, KES, EUR or GBP**, with status `paid` / `unpaid` / `partial`
- 🔎 Filter projects per client with `?client=<id>`

## 📡 API

| Method | Endpoint | What it does | Access |
|:---:|---|---|:---:|
| `POST` | `/api/register/` | Create an account | 🌍 |
| `POST` | `/api/auth/token/` · `/api/auth/refresh/` | JWT login / refresh | 🌍 |
| `GET` `POST` `PATCH` `DELETE` | `/api/clients/…` | Your clients | 🔐 |
| `GET` `POST` `PATCH` `DELETE` | `/api/projects/…?client=<id>` | Your projects | 🔐 |
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
pytest            # auth, clients, projects and cross-user isolation, with coverage
```

---

<div align="center">

**Built by Jessy Bryan Waweru**: actuary by training, full-stack engineer by obsession.

[![LinkedIn](https://img.shields.io/badge/LinkedIn-0A66C2?logo=linkedin&logoColor=white)](https://www.linkedin.com/in/jessy-waweru-b8051a288)
[![Email](https://img.shields.io/badge/Email-EA4335?logo=gmail&logoColor=white)](mailto:bryanjessywaweru@gmail.com)
[![GitHub](https://img.shields.io/badge/More_projects-181717?logo=github)](https://github.com/JessyWaweru)

</div>
