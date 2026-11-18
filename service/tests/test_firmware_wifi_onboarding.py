from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
WIFI_PORTAL_CPP = REPO_ROOT / "hardware" / "firmware" / "wifi_portal.cpp"
WIFI_PORTAL_HTML = REPO_ROOT / "hardware" / "firmware" / "utils" / "html.cpp"


def test_wifi_provision_page_is_compact_device_settings():
    src = WIFI_PORTAL_HTML.read_text(encoding="utf-8")

    assert "给小歪连上家里的" not in src
    assert src.index('id="device-config-card"') < src.index('id="wifi-settings-card"')
    assert src.index('id="wifi-settings-card"') < src.index('id="continue-boot-btn"')
    assert 'id="copy-device-id-btn"' in src
    assert "复制设备 ID 可用于绑定设备" in src
    assert 'id="manual-ssid-input"' in src
    assert "隐藏网络" in src
    assert "fetch('/device-config')" in src
    assert "'/save-wifi'" in src
    assert "fetch('/device-config/continue-boot'" in src


def test_wifi_provision_http_api_exposes_status_and_captive_fallback():
    src = WIFI_PORTAL_CPP.read_text(encoding="utf-8")

    assert 'g_wifi_server.on("/status", HTTP_GET' in src
    assert '\\"ap_ssid\\"' in src
    assert '\\"ap_ip\\"' in src
    assert '\\"device_id\\"' in src
    assert "server.onNotFound" in src
    assert 'sendHeader("Location", "/"' in src
