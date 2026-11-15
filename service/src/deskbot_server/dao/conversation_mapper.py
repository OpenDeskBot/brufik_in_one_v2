"""Device conversation selection; independent of existing ASR/LLM/TTS fields."""

from deskbot_server.dao.decorators import select_one, update


@select_one("SELECT conversation_provider, conversation_param FROM devices WHERE device_id=:device_id")
def get_conversation(device_id: str) -> dict | None:
    """Read device conversation settings."""


@update("UPDATE devices SET conversation_provider=:provider, conversation_param=:params WHERE device_id=:device_id")
def set_conversation(device_id: str, provider: str, params: str) -> int:
    """Atomically replace selection and parameters."""
