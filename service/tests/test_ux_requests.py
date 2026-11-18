from concurrent.futures import ThreadPoolExecutor

from deskbot_server.dao import device_mapper
from deskbot_server.dao.conversation_mapper import get_conversation
from deskbot_server.service.memory_service import add_memory, list_memory_for_device
from tests.test_controller_auth import _login
from tests.test_controller_auth import api_client as api_client


def test_page_device_binding_survives_session_switch(api_client):
    device_mapper.insert("second", "device-second", "owner", "Second")
    _login(api_client, current_device_id="device-second")
    response = api_client.post("/api/robot-settings/conversation", headers={"X-Deskbot-Device": "device-owner"},
                               json={"provider": "flecket", "api_key": "test-only-key", "prompt": "A"})
    assert response.status_code == 200
    assert get_conversation("device-owner")["conversation_provider"] == "flecket"
    assert get_conversation("device-second")["conversation_provider"] == "flecket"
    assert api_client.get("/api/robot-settings", headers={"X-Deskbot-Device": "device-other"}).status_code == 403
    assert api_client.post("/api/robot-settings/conversation", headers={"X-Deskbot-Device": "device-other"},
                           json={"provider": "local"}).status_code == 403
    assert get_conversation("device-other")["conversation_provider"] == "flecket"


def test_memory_retry_key_is_idempotent_and_device_scoped(api_client):
    _login(api_client, current_device_id="device-owner")
    url = "/app/api/memories"
    headers = {"Idempotency-Key": "retry-key", "X-Deskbot-Device": "device-owner"}
    first = api_client.post(url, headers=headers, json={"text": "Remember"})
    second = api_client.post(url, headers=headers, json={"text": "Remember"})
    assert first.status_code == second.status_code == 200
    assert first.json()["memory"]["id"] == second.json()["memory"]["id"]
    assert len(list_memory_for_device("device-owner")) == 1
    assert api_client.post(url, headers=headers, json={"text": "Different"}).status_code == 400
    assert api_client.post(url, headers={**headers, "Idempotency-Key": "another"}, json={"text": "Remember"}).status_code == 200
    assert len(list_memory_for_device("device-owner")) == 1


def test_concurrent_memory_retries_are_one_insert(api_client):
    del api_client
    with ThreadPoolExecutor(max_workers=4) as pool:
        records = list(pool.map(lambda _: add_memory("Same", device_id="device-owner", request_key="concurrent"), range(4)))
    assert len({row["id"] for row in records}) == 1
    assert len(list_memory_for_device("device-owner")) == 1


def test_unbind_bound_device_does_not_recheck_removed_ownership(api_client):
    _login(api_client, current_device_id="device-owner")
    response = api_client.delete("/app/api/devices/device-owner", headers={"X-Deskbot-Device": "device-owner"})
    assert response.status_code == 200
    listing = api_client.get("/app/api/devices").json()
    assert listing["current_device_id"] is None
