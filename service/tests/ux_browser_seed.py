"""Seed only the isolated browser runner's disposable database."""
import json
import os
import tempfile
from pathlib import Path

from deskbot_server.dao import device_mapper
from deskbot_server.dao.conversation_mapper import set_conversation
from deskbot_server.db import init_database
from deskbot_server.service.user_service import UserService


def seed():
    path = Path(os.environ["DESKBOT_DB_PATH"]).resolve()
    if not path.is_relative_to(Path(tempfile.gettempdir()).resolve()):
        raise RuntimeError("Browser fixtures require a temporary database")
    init_database()
    user = UserService().register("browser-ux@example.com", "browser-password-123")
    for device, label in (("agent-browser-device", "A"), ("agent-browser-device-b", "B")):
        device_mapper.insert("pk-" + label, device, user.id, "Agent " + label)
        set_conversation(device, "flecket", json.dumps({"flecket": {
            "prompt": "Device " + label + " prompt", "api_key": "isolated-test-key",
            "url": "ws://localhost:9110/v1/realtime?model=agent_" + label,
        }}))


if __name__ == "__main__":
    seed()
