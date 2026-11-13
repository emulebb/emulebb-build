"""APK notices follow the selected runtime graph and actual packaged bytes."""
import hashlib
import io
import json
from pathlib import Path
import zipfile

import pytest

from emule_workspace import android_apk_notices as notices


def descriptor(root, path):
    return {"path": path.name, "sha256": hashlib.sha256(path.read_bytes()).hexdigest(), "sizeBytes": path.stat().st_size}


@pytest.fixture
def runtime(tmp_path, monkeypatch):
    root, target, cache = tmp_path / "inventory", tmp_path / "notices", tmp_path / "cache"
    root.mkdir()
    target.mkdir()
    artifact = root / "annotations-13.0.jar"
    with zipfile.ZipFile(artifact, "w") as archive:
        archive.writestr("META-INF/NOTICE.txt", "Synthetic runtime notice")
        archive.writestr("Example.class", "synthetic bytecode")
    pom = root / "annotations-13.0.pom"
    pom.write_text('''<project xmlns="http://maven.apache.org/POM/4.0.0"><groupId>org.jetbrains</groupId>
        <artifactId>annotations</artifactId><version>13.0</version><licenses><license>
        <name>Apache License 2.0</name><url>https://www.apache.org/licenses/LICENSE-2.0.txt</url>
        </license></licenses></project>''')
    data = {"schema": "emulebb.android.runtime-classpath/1", "variant": "release", "configuration": "releaseRuntimeClasspath",
        "components": [{"group": "org.jetbrains", "name": "annotations", "version": "13.0",
                        "pom": descriptor(root, pom), "artifacts": [descriptor(root, artifact)]}]}
    (root / "report.json").write_text(json.dumps(data))
    downloads = []
    def download(url, path, digest):
        downloads.append((url, digest))
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("Synthetic reviewed upstream license text")
    monkeypatch.setattr(notices, "download_verified", download)
    return root, target, cache, data, downloads


def test_runtime_graph_drives_notices_and_spdx_without_build_plugins(runtime):
    root, target, cache, data, downloads = runtime
    result = notices.stage_runtime_notices(root, target, cache, variant="release")
    assert [item["name"] for item in result["components"]] == ["annotations"]
    assert result["components"][0]["artifacts"][0]["embeddedNotices"][0]["source"] == "META-INF/NOTICE.txt"
    packages = notices.spdx_components(result)
    assert len(packages) == 1 and packages[0]["versionInfo"] == "13.0"
    assert packages[0]["checksums"][0]["checksumValue"] == data["components"][0]["artifacts"][0]["sha256"]
    assert downloads == [("https://www.apache.org/licenses/LICENSE-2.0.txt", notices.APACHE_SHA)]
    assert len(notices.notice_inventory(target)) == 2


def test_aar_preserves_license_notices_from_its_actual_nested_runtime_jar(runtime):
    root, target, cache, data, _downloads = runtime
    nested = io.BytesIO()
    with zipfile.ZipFile(nested, "w") as archive:
        archive.writestr("META-INF/NOTICE", "Nested runtime notice")
    artifact = root / "annotations-13.0.aar"
    with zipfile.ZipFile(artifact, "w") as archive:
        archive.writestr("classes.jar", nested.getvalue())
    data["components"][0]["artifacts"] = [descriptor(root, artifact)]
    (root / "report.json").write_text(json.dumps(data))
    result = notices.stage_runtime_notices(root, target, cache, variant="release")
    embedded = result["components"][0]["artifacts"][0]["embeddedNotices"]
    assert embedded[0]["source"] == "classes.jar/META-INF/NOTICE"
    assert (target / embedded[0]["path"]).read_text() == "Nested runtime notice"


@pytest.mark.parametrize("version", ["2.2.10", "2.4.21"])
def test_kotlin_jvm_carries_reviewed_third_party_licenses_and_requires_new_version_review(runtime, version):
    root, target, cache, data, downloads = runtime
    component = data["components"][0]
    component.update(group="org.jetbrains.kotlin", name="kotlin-stdlib", version=version)
    pom = root / component["pom"]["path"]
    pom.write_text(pom.read_text().replace("org.jetbrains</", "org.jetbrains.kotlin</").replace("annotations</", "kotlin-stdlib</").replace("13.0</", version + "</"))
    component["pom"] = descriptor(root, pom)
    (root / "report.json").write_text(json.dumps(data))
    if version == "2.4.21":
        with pytest.raises(RuntimeError, match="third-party notices"):
            notices.stage_runtime_notices(root, target, cache, variant="release")
        return
    result = notices.stage_runtime_notices(root, target, cache, variant="release")
    assert result["components"][0]["license"] == "Apache-2.0 AND BSD-3-Clause AND BSL-1.0"
    assert len(downloads) == 4
    assert all((target / filename).is_file() for filename in notices.KOTLIN_NOTICES)


@pytest.mark.parametrize("change", ["jar-bytes", "pom-bytes", "path", "variant", "duplicate", "no-artifacts", "unknown-license", "pom-identity"])
def test_changed_or_unreviewed_runtime_input_fails_closed(runtime, change):
    root, target, cache, data, _downloads = runtime
    component = data["components"][0]
    if change == "jar-bytes":
        (root / component["artifacts"][0]["path"]).write_bytes(b"changed")
    elif change == "pom-bytes":
        (root / component["pom"]["path"]).write_bytes(b"changed")
    elif change == "path":
        component["artifacts"][0]["path"] = "../outside.jar"
    elif change == "variant":
        data["configuration"] = "pluginClasspath"
    elif change == "duplicate":
        data["components"] *= 2
    elif change == "no-artifacts":
        component["artifacts"] = []
    else:
        pom = root / component["pom"]["path"]
        text = pom.read_text().replace("apache.org", "unreviewed.invalid") if change == "unknown-license" else pom.read_text().replace("13.0", "99.0")
        pom.write_text(text)
        component["pom"] = descriptor(root, pom)
    (root / "report.json").write_text(json.dumps(data))
    with pytest.raises(RuntimeError):
        notices.stage_runtime_notices(root, target, cache, variant="release")


def test_source_notice_uses_real_miniupnp_root_instead_of_windows_symlink_placeholder(tmp_path):
    rust, mini, pcp, target = (tmp_path / name for name in ("rust", "mini", "pcp", "notices"))
    for path in (rust, mini / "miniupnpc", pcp, target):
        path.mkdir(parents=True)
    text = "License\nCopyright Synthetic Authors\n" + "redistribution permitted " * 4
    (rust / "LICENSE").write_text(text)
    (mini / "LICENSE").write_text(text)
    (mini / "miniupnpc/LICENSE").write_text("../LICENSE")
    bsd_notice = ("Copyright Synthetic Authors. All rights reserved.\n"
        "Redistribution and use in source and binary forms, with or without modification, are permitted.\n"
        "THIS SOFTWARE IS PROVIDED AS IS WITHOUT WARRANTY.\n")
    assert "license" not in bsd_notice.lower()
    (pcp / "COPYING").write_text(bsd_notice)
    notices.stage_source_notices(rust, mini, pcp, target)
    assert (target / "MiniUPnPc-LICENSE").read_text() == text
    assert (target / "libpcpnatpmp-COPYING").read_text() == bsd_notice
    (mini / "LICENSE").write_text("../LICENSE")
    with pytest.raises(RuntimeError, match="complete license"):
        notices.stage_source_notices(rust, mini, pcp, target)


def test_packaged_license_audit_detects_missing_changed_and_extra_assets(tmp_path):
    apk = tmp_path / "app.apk"
    content = b"Synthetic complete license"
    expected = {"LICENSE": hashlib.sha256(content).hexdigest()}
    with zipfile.ZipFile(apk, "w") as archive:
        archive.writestr("assets/licenses/LICENSE", content)
    notices.audit_packaged_notices(apk, expected)
    for value in ({}, {"LICENSE": "a" * 64}, {**expected, "missing": "b" * 64}):
        with pytest.raises(RuntimeError, match="license assets differ"):
            notices.audit_packaged_notices(apk, value)
