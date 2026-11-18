"""Serial status parsing must survive interleaved scalar logger output."""

import importlib.util
from pathlib import Path

import pytest

spec = importlib.util.spec_from_file_location(
    "device_diagnostics", Path(__file__).resolve().parents[1] / "tools/device_diagnostics.py"
)
diagnostics = importlib.util.module_from_spec(spec)
spec.loader.exec_module(diagnostics)


@pytest.mark.parametrize(
    "line, expected",
    [
        ('{"type":"audio_status","playing":false}', {"type": "audio_status", "playing": False}),
        ('[00:05:02.579] [WARN ] {"type":"resource_status","ready":true}', {"type": "resource_status", "ready": True}),
        (
            '[00:05:02.579] [WARN ] {"type":"resource_status","ready":true} [next log]',
            {"type": "resource_status", "ready": True},
        ),
        (
            '[00:08:15.384] [WARN ] [PB_WAIT_END] display_q=1{"type":"resource_status","ready":true}',
            {"type": "resource_status", "ready": True},
        ),
    ],
)
def test_plain_and_logger_prefixed_typed_status(line, expected):
    assert diagnostics.parse_diagnostic_line(line) == expected


@pytest.mark.parametrize(
    "line",
    [
        '[00:05:02.579] [WARN ] [PB] text {"type":"pb_start","idx":0}',
        '[00:05:02.579] [WARN ] {"type":',
        '[00:05:02.579] [WARN ] {"ready":true}',
        'ordinary prose {"type":"resource_status","ready":true}',
        "[1,2,3]",
    ],
)
def test_logs_malformed_and_untyped_objects_remain_logs(line):
    assert diagnostics.parse_diagnostic_line(line) == {"line": line}
