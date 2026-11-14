"""Official catalog checksums bootstrap source-reviewed SHA-256 guest inputs."""
import hashlib
import io
import json
import zipfile
from types import SimpleNamespace

import pytest

from emule_workspace import android_toolchain as android, config


@pytest.fixture
def inputs(tmp_path, monkeypatch):
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as guest:
        guest.writestr("x86_64/source.properties", "Pkg.Revision=12\nAndroidVersion.ApiLevel=34\n")
    payload = buffer.getvalue()
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
    assert record == {"version": item["version"], "url": item["url"], "sha256": digest, "kind": "zip", "packageRevision": "12"}
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


def test_authoring_selects_only_explicit_unique_catalog_images(inputs):
    layout, _payload, _item, calls = inputs
    report = android.author_apk_guest_inputs(layout, images=("apk34",))
    assert set(json.loads(report.read_text())["assets"]) == {"system-image-apk34"}
    assert len(calls) == 1
    for images in (("apk34", "apk34"), ("apk31",)):
        with pytest.raises(ValueError, match="unique authorable"):
            android.author_apk_guest_inputs(layout, images=images)
    assert len(calls) == 1


@pytest.mark.parametrize("properties", [
    "Pkg.Revision=0\nAndroidVersion.ApiLevel=34\n",
    "Pkg.Revision=12\nPkg.Revision=13\nAndroidVersion.ApiLevel=34\n",
    "Pkg.Revision=12\nAndroidVersion.ApiLevel=33\n",
])
def test_authoring_rejects_invalid_or_ambiguous_verified_package_metadata(inputs, monkeypatch, properties):
    layout, _payload, item, _calls = inputs
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as guest:
        guest.writestr("x86_64/source.properties", properties)
    payload = buffer.getvalue()
    item.update(sha1=hashlib.sha1(payload).hexdigest(), size=len(payload))
    android.APK_GUEST_AUTHORING.write_text(json.dumps({"source": "https://example.invalid/catalog.xml", "assets": {"system-image-apk34": item}}))
    monkeypatch.setattr(android.urllib.request, "urlopen", lambda *args, **kwargs: io.BytesIO(payload))
    with pytest.raises(RuntimeError, match="metadata"):
        android.author_apk_guest_inputs(layout)
    assert not list(layout.output_root.rglob("*.part"))
    assert not (layout.output_root / "cache/android-emulator-inputs").exists()


def test_authoring_maps_fractional_platform_api_to_major_role_without_losing_exact_api(inputs, monkeypatch):
    layout, _payload, _item, _calls = inputs
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as guest:
        guest.writestr("x86_64/source.properties", "Pkg.Revision=7\nAndroidVersion.ApiLevel=37.2\n")
    payload = buffer.getvalue()
    item = {"version": "37.2-r7", "url": "https://example.invalid/x86_64-ps16k-37.2_r07.zip",
            "sha1": hashlib.sha1(payload).hexdigest(), "size": len(payload)}
    android.APK_GUEST_AUTHORING.write_text(json.dumps({"source": "https://example.invalid/catalog.xml", "assets": {"system-image-apk37": item}}))
    monkeypatch.setattr(android.urllib.request, "urlopen", lambda *args, **kwargs: io.BytesIO(payload))
    report = android.author_apk_guest_inputs(layout, images=("apk37",))
    assert json.loads(report.read_text())["assets"]["system-image-apk37"]["packageRevision"] == "7"
