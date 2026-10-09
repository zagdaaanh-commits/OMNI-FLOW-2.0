"""Persistent, user-owned chat history and explicit campaign-mode routing."""
from __future__ import annotations

from uuid import uuid4

from fastapi.testclient import TestClient

from app.main import app


client = TestClient(app)


def _register(label: str):
    response = client.post(
        "/auth/register",
        json={
            "email": f"chat_{label}_{uuid4().hex[:8]}@example.com",
            "password": "Passw0rd!",
            "full_name": f"Chat {label}",
        },
    )
    assert response.status_code == 200
    body = response.json()
    return body["user"], {"Authorization": f"Bearer {body['token']}"}


def test_chat_mode_persists_messages_without_creating_a_campaign():
    user, headers = _register("owner")
    before = client.get("/campaigns", headers=headers).json()

    response = client.post(
        "/api/chat",
        headers=headers,
        json={"message": "Create new campaign Autumn Push", "mode": "chat"},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["type"] == "chat"
    assert body["conversation_id"]
    assert "no campaign was created" in body["reply"].lower()
    assert client.get("/campaigns", headers=headers).json() == before

    conversations = client.get("/api/conversations", headers=headers)
    assert conversations.status_code == 200
    assert conversations.json()[0]["id"] == body["conversation_id"]
    assert conversations.json()[0]["user_id"] == user["id"]
    assert conversations.json()[0]["title"] == "Create new campaign Autumn Push"

    messages = client.get(
        f"/api/conversations/{body['conversation_id']}/messages",
        headers=headers,
    )
    assert messages.status_code == 200
    assert [item["role"] for item in messages.json()] == ["user", "assistant"]
    assert messages.json()[0]["content"] == "Create new campaign Autumn Push"
    assert messages.json()[1]["content"] == body["reply"]


def test_conversation_continuation_sorting_and_user_isolation():
    _, owner_headers = _register("primary")
    _, other_headers = _register("other")

    first = client.post(
        "/api/chat",
        headers=owner_headers,
        json={"message": "First thread", "mode": "chat"},
    ).json()
    second = client.post(
        "/api/chat",
        headers=owner_headers,
        json={"message": "Second thread", "mode": "chat"},
    ).json()
    continued = client.post(
        "/api/chat",
        headers=owner_headers,
        json={
            "message": "Continue the first thread",
            "conversation_id": first["conversation_id"],
            "mode": "chat",
        },
    )
    assert continued.status_code == 200

    conversations = client.get("/api/conversations", headers=owner_headers).json()
    assert conversations[0]["id"] == first["conversation_id"]
    assert {item["id"] for item in conversations[:2]} == {
        first["conversation_id"],
        second["conversation_id"],
    }

    messages = client.get(
        f"/api/conversations/{first['conversation_id']}/messages",
        headers=owner_headers,
    ).json()
    assert [item["role"] for item in messages] == ["user", "assistant", "user", "assistant"]

    hidden = client.get(
        f"/api/conversations/{first['conversation_id']}/messages",
        headers=other_headers,
    )
    assert hidden.status_code == 404
    assert client.get("/api/conversations", headers=other_headers).json() == []


def test_invalid_or_foreign_conversation_ids_are_rejected():
    _, headers = _register("invalid")
    malformed = client.post(
        "/api/chat",
        headers=headers,
        json={"message": "Hello", "conversation_id": "not-a-uuid", "mode": "chat"},
    )
    assert malformed.status_code == 422

    missing = client.post(
        "/api/chat",
        headers=headers,
        json={"message": "Hello", "conversation_id": str(uuid4()), "mode": "chat"},
    )
    assert missing.status_code == 404


def test_explicit_campaign_mode_can_create_a_campaign():
    _, headers = _register("campaign")
    before = client.get("/campaigns", headers=headers).json()

    response = client.post(
        "/api/chat",
        headers=headers,
        json={"message": "Create new campaign Autumn Push", "mode": "campaign"},
    )

    assert response.status_code == 200
    assert response.json()["type"] == "campaign"
    after = client.get("/campaigns", headers=headers).json()
    assert len(after) == len(before) + 1
