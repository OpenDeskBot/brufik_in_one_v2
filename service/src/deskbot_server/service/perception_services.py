"""Manage only the robot's two independent perception engines."""

from deskbot_server.service.external.manager import ExternalServiceManager

PERCEPTION_SERVICES = frozenset({"insightface-engine", "wespeaker-resnet34"})


def build_perception_services():
    manager = ExternalServiceManager(allowed_names=PERCEPTION_SERVICES)
    manager.discover()
    return manager
