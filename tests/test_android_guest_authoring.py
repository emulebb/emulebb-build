"""Official catalog checksums bootstrap source-reviewed SHA-256 guest inputs."""
import hashlib
import io
import json
from types import SimpleNamespace

import pytest

from emule_workspace import android_toolchain as android, config


@pytest.fixture
def inputs(tmp_path, monkeypatch):
    payload = b"synthetic official immutable image archive"
    item = {"version": "34-r14", "url": "https://example.invalid/x86_64-34_r14.zip",
            "sha1": hashlib.sha1(payload).hexdigest(), "size": len(payload)}
    declaration = tmp_path / "declaration.json"
    declaration.write_text(json.dumps({"source": "https://example.invalid/catalog.xml", "assets": {"system-image-apk34": item}}))
    monkeypatch.setattr(android, "APK_GUEST_AUTHORING", declaration)
    monkeypatch.setattr(config, "resolve_required_workspace_roots", lambda: (tmp_path / "workspace", tmp_path))
    calls = []
    monkeypatch.setattr(android.urllib.request, "urlopen", lambda *args, **kwargs: calls.append(args) or io.BytesIO(payload))
    return SimpleNamespace(output_root=tmp_path), payload, item, calls


def test_authoring_verifies_official_bytes_and_reuses_production_sha256_cache(inputs):
    layout, payload, item, calls = inputs
    report = android.author_apk_guest_inputs(layout)
    digest = hashlib.sha256(payload).hexdigest()
    record = json.loads(report.read_text())["assets"]["system-image-apk34"]
    assert record == {"version": item["version"], "url": item["url"], "sha256": digest, "kind": "zip"}
    assert (layout.output_root / "cache/android-emulator-inputs" / digest / "x86_64-34_r14.zip").read_bytes() == payload
    android.author_apk_guest_inputs(layout)
    assert len(calls) == 1


def test_authoring_checksum_failure_never_publishes_a_pin_or_cached_archive(inputs, monkeypatch):
    layout, _payload, _item, _calls = inputs
    monkeypatch.setattr(android.urllib.request, "urlopen", lambda *args, **kwargs: io.BytesIO(b"tampered"))
    with pytest.raises(RuntimeError, match="official catalog checksum"):
        android.author_apk_guest_inputs(layout)
    assert not list(layout.output_root.rglob("*.part"))
    assert not (layout.output_root / "cache/android-emulator-inputs").exists()
