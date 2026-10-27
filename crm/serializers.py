# Enables full CRUD over your models through API calls.
from rest_framework import serializers
from .models import Client, Project
from django.contrib.auth import get_user_model

User = get_user_model()


class ClientSerializer(serializers.ModelSerializer):
    # serializers.ModelSerializer → A DRF shortcut that creates serializer fields based
    #  on your model fields automatically.

    class Meta:
        # Meta is Django’s convention for passing model-related settings to a
        #  class — it keeps things clean and readable.
        model = Client
        fields = "__all__"
        # fields = "__all__" → Include all fields from the model in the serializer output.
        read_only_fields = ("owner", "created_at")
        # read_only_fields → Prevent clients from manually setting these when posting data.


class ProjectSerializer(serializers.ModelSerializer):
    # Extra fields beyond the model:
    #   client_name → human-readable name of the client
    #   client_id → the FK id for linking back to Client
    client_name = serializers.CharField(source="client.name", read_only=True)
    client_id = serializers.IntegerField(source="client.id", read_only=True)

    class Meta:
        model = Project
        fields = "__all__"

    def validate_client(self, client):
        # A project can only be attached to one of the logged-in user's own clients.
        if client.owner_id != self.context["request"].user.id:
            raise serializers.ValidationError("Client not found.")
        return client
        # fields = "__all__" ensures all Project fields are included
        # plus the extra client_name + client_id we defined above.


