import pytest
from django.urls import reverse
from rest_framework.test import APIClient
from django.contrib.auth import get_user_model
from crm.models import Client, Project

User = get_user_model()


@pytest.mark.django_db
def test_list_projects():
    user = User.objects.create_user(username="user1", password="pass1234")
    client_obj = Client.objects.create(name="Client A", owner=user)

    Project.objects.create(title="Project 1", status="ongoing", client=client_obj)
    Project.objects.create(title="Project 2", status="completed", client=client_obj)

    client = APIClient()
    client.force_authenticate(user=user)

    url = reverse("project-list")
    response = client.get(url)

    assert response.status_code == 200
    assert len(response.data) == 2
    assert response.data[0]["title"] == "Project 1"


@pytest.mark.django_db
def test_filter_projects_by_client():
    user = User.objects.create_user(username="user2", password="pass1234")

    c1 = Client.objects.create(name="Client A", owner=user)
    c2 = Client.objects.create(name="Client B", owner=user)

    Project.objects.create(title="A1", status="ongoing", client=c1)
    Project.objects.create(title="B1", status="ongoing", client=c2)

    client = APIClient()
    client.force_authenticate(user=user)

    url = reverse("project-list") + f"?client={c1.id}"
    response = client.get(url)

    assert response.status_code == 200
    assert len(response.data) == 1
    assert response.data[0]["title"] == "A1"


@pytest.mark.django_db
def test_create_project():
    user = User.objects.create_user(username="user3", password="pass1234")
    c1 = Client.objects.create(name="Client A", owner=user)

    client = APIClient()
    client.force_authenticate(user=user)

    payload = {
        "title": "New Project",
        "status": "ongoing",
        "client": c1.id
    }

    url = reverse("project-list")
    response = client.post(url, payload, format="json")

    assert response.status_code == 201
    assert response.data["title"] == "New Project"
    assert response.data["client"] == c1.id


@pytest.mark.django_db
def test_delete_project():
    user = User.objects.create_user(username="user4", password="pass1234")
    c1 = Client.objects.create(name="Client A", owner=user)
    project = Project.objects.create(title="DeleteMe", status="ongoing", client=c1)

    client = APIClient()
    client.force_authenticate(user=user)

    url = reverse("project-detail", kwargs={"pk": project.id})
    response = client.delete(url)

    assert response.status_code == 204
    assert Project.objects.count() == 0


@pytest.mark.django_db
def test_cannot_attach_project_to_another_users_client():
    owner = User.objects.create_user(username="owner", password="pass1234")
    intruder = User.objects.create_user(username="intruder", password="pass1234")
    their_client = Client.objects.create(name="Not Yours", owner=owner)
    my_client = Client.objects.create(name="Mine", owner=intruder)
    my_project = Project.objects.create(title="Mine", status="ongoing", client=my_client)

    client = APIClient()
    client.force_authenticate(user=intruder)

    response = client.post(reverse("project-list"), {"title": "Sneaky", "status": "ongoing", "client": their_client.id}, format="json")
    assert response.status_code == 400

    response = client.patch(reverse("project-detail", args=[my_project.id]), {"client": their_client.id}, format="json")
    assert response.status_code == 400
    assert not Project.objects.filter(client=their_client).exists()
