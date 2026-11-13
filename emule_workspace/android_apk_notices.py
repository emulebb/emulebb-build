"""Audit the app runtime graph and package the notices for its actual inputs."""
from __future__ import annotations

import hashlib
import io
import json
from pathlib import Path, PurePosixPath
import re
import shutil
import xml.etree.ElementTree as ET
import zipfile
from urllib.parse import urlsplit

from .python_runtime import download_verified, sha256

APACHE_SHA = "cfc7749b96f63bd31c3c42b5c471bf756814053e847c10f3eb003417bc523d30"
KOTLIN_NOTICES = {
    "Kotlin-upstream-licensing.txt": ("README.md", "8cba83fcded6d166fc4398eb8cb9c1a12ad8b87ff624a404956242d22231fbc9"),
    "Kotlin-ThreeTenBP-LICENSE": ("third_party/threetenbp_license.txt", "d1bc53b493a3ab387b42717ed5c4b1976a5048996f81154278100bff86d39331"),
    "Kotlin-Boost-LICENSE": ("third_party/boost_LICENSE.txt", "8d8291caf1cee26d23acf3eb67c9f9a2d58f1c681b16a4fbe8cbfb9e3c0b5a9b"),
}


def stage_source_notices(rust: Path, mini: Path, pcp: Path, destination: Path) -> None:
    """Use canonical license texts, including Windows checkouts without symlinks."""
    for source, name in ((rust / "LICENSE", "eMuleBB-LICENSE"),
                         (mini / "LICENSE", "MiniUPnPc-LICENSE"),
                         (pcp / "COPYING", "libpcpnatpmp-COPYING")):
        # WHY: miniupnpc/LICENSE is an upstream ../LICENSE symlink. Windows
        # checkouts can expose that ten-byte placeholder instead of license text.
        if source.is_symlink() or not source.is_file() or source.stat().st_size > 1024 * 1024:
            raise RuntimeError("APK requires a canonical regular source license: " + name)
        content = source.read_bytes()
        if len(content) < 64 or b"license" not in content.lower() or b"copyright" not in content.lower():
            raise RuntimeError("APK source notice is not complete license text: " + name)
        (destination / name).write_bytes(content)


def _file(root: Path, descriptor: object) -> Path:
    if not isinstance(descriptor, dict):
        raise RuntimeError("Runtime artifact needs a checksum-bound file")
    name, digest = descriptor.get("path"), descriptor.get("sha256")
    if (not isinstance(name, str) or PurePosixPath(name).is_absolute() or ".." in PurePosixPath(name).parts
            or "\\" in name or not name or not isinstance(digest, str) or re.fullmatch(r"[0-9a-f]{64}", digest) is None):
        raise RuntimeError("Runtime artifact needs a safe relative path and SHA-256")
    path = root / name
    if (path.is_symlink() or not path.resolve().is_relative_to(root.resolve()) or not path.is_file()
            or path.stat().st_size > 128 * 1024 * 1024 or sha256(path) != digest):
        raise RuntimeError("Runtime artifact differs from its Gradle receipt")
    if "sizeBytes" in descriptor and (type(descriptor["sizeBytes"]) is not int or descriptor["sizeBytes"] != path.stat().st_size):
        raise RuntimeError("Runtime artifact size differs from its Gradle receipt")
    return path


def _declared_license(pom: Path, group: str, name: str, version: str) -> str:
    if pom.stat().st_size > 1024 * 1024:
        raise RuntimeError("Runtime Maven POM exceeds the notice metadata limit")
    root = ET.fromstring(pom.read_bytes())
    ns = {"m": "http://maven.apache.org/POM/4.0.0"}
    # These directly resolved Maven publications declare concrete coordinates;
    # do not guess inherited properties or a license from the artifact's name.
    if tuple(root.findtext("m:" + field, namespaces=ns) for field in ("groupId", "artifactId", "version")) != (group, name, version):
        raise RuntimeError("Runtime Maven POM identity differs from the resolved component")
    licenses = root.findall("m:licenses/m:license", ns)
    urls = [urlsplit(node.findtext("m:url", "", ns)) for node in licenses]
    if not urls or any(url.scheme not in {"https", "http"} or url.netloc not in {"www.apache.org", "apache.org"}
                       or url.path not in {"/licenses/LICENSE-2.0", "/licenses/LICENSE-2.0.txt", "/licenses/LICENSE-2.0.html"}
                       or url.query or url.fragment for url in urls):
        raise RuntimeError("Review the newly selected runtime component license before APK production")
    return "Apache-2.0"


def stage_runtime_notices(inventory: Path, destination: Path, cache: Path, *, variant: str) -> dict:
    """Bind notices to actual runtimeClasspath artifacts, never Gradle's plugin cache."""
    value = json.loads((inventory / "report.json").read_text(encoding="utf-8"))
    components = value.get("components")
    if (value.get("schema") != "emulebb.android.runtime-classpath/1" or value.get("variant") != variant
            or value.get("configuration") != variant + "RuntimeClasspath" or not isinstance(components, list)
            or not 1 <= len(components) <= 128):
        raise RuntimeError("APK needs the actual selected Gradle runtimeClasspath inventory")
    apache = cache / APACHE_SHA / "Apache-2.0-LICENSE"
    download_verified("https://www.apache.org/licenses/LICENSE-2.0.txt", apache, APACHE_SHA)
    shutil.copyfile(apache, destination / apache.name)
    result, coordinates = [], set()
    for component in components:
        group, name, version = (component.get(key) for key in ("group", "name", "version"))
        if any(not isinstance(item, str) or re.fullmatch(r"[A-Za-z0-9_.+-]+", item) is None for item in (group, name, version)):
            raise RuntimeError("Runtime component has invalid Maven coordinates")
        coordinate = f"{group}:{name}:{version}"
        if coordinate in coordinates:
            raise RuntimeError("Runtime graph repeats a component identity")
        coordinates.add(coordinate)
        pom = _file(inventory, component.get("pom"))
        license_id = _declared_license(pom, group, name, version)
        if group == "org.jetbrains.kotlin" and name == "kotlin-stdlib":
            if version != "2.2.10":
                raise RuntimeError("Review the selected Kotlin JVM runtime's upstream third-party notices")
            for notice_name, (relative, digest) in KOTLIN_NOTICES.items():
                source = cache / digest / notice_name
                download_verified("https://raw.githubusercontent.com/JetBrains/kotlin/v2.2.10/license/" + relative, source, digest)
                shutil.copyfile(source, destination / notice_name)
            license_id = "Apache-2.0 AND BSD-3-Clause AND BSL-1.0"
        artifacts = component.get("artifacts")
        if not isinstance(artifacts, list) or not 1 <= len(artifacts) <= 16:
            raise RuntimeError("Runtime component needs its actual packaged artifacts")
        audited = []
        for descriptor in artifacts:
            artifact = _file(inventory, descriptor)
            if artifact.suffix not in {".jar", ".aar"}:
                raise RuntimeError("Review the newly selected APK runtime artifact format")
            embedded = []
            def embedded_notices(archive, prefix=""):
                for entry in archive.infolist():
                    if artifact.suffix == ".aar" and not prefix and (entry.filename == "classes.jar" or entry.filename.startswith("libs/") and entry.filename.endswith(".jar")):
                        if entry.file_size > 128 * 1024 * 1024:
                            raise RuntimeError("Runtime AAR nested JAR exceeds its notice audit limit")
                        with zipfile.ZipFile(io.BytesIO(archive.read(entry))) as nested:
                            embedded_notices(nested, entry.filename + "/")
                    path = PurePosixPath(entry.filename)
                    if (entry.is_dir() or not path.parts or path.parts[0] != "META-INF"
                            or re.match(r"(?i)(LICENSE|NOTICE|COPYRIGHT)(?:[._-]|$)", path.name) is None):
                        continue
                    if path.is_absolute() or ".." in path.parts or entry.file_size > 1024 * 1024:
                        raise RuntimeError("Runtime artifact contains an unsafe notice")
                    content = archive.read(entry)
                    source_name = prefix + entry.filename
                    target = "runtime-" + hashlib.sha256((coordinate + "/" + source_name).encode()).hexdigest()[:16] + ".txt"
                    (destination / target).write_bytes(content)
                    embedded.append({"source": source_name, "path": target, "sha256": hashlib.sha256(content).hexdigest()})
            with zipfile.ZipFile(artifact) as archive:
                embedded_notices(archive)
            audited.append({**descriptor, "embeddedNotices": embedded})
        result.append({"group": group, "name": name, "version": version, "license": license_id,
                       "pom": component["pom"], "artifacts": audited})
    return {"schema": "emulebb.android.runtime-notices/1", "configuration": value["configuration"],
            "components": result}


def notice_inventory(directory: Path) -> dict[str, str]:
    """Describe exact license assets and reject incomplete placeholder notices."""
    paths = sorted(directory.iterdir())
    if not paths or any(path.is_symlink() or not path.is_file() for path in paths):
        raise RuntimeError("APK license assets must be regular owned notice files")
    return {path.name: sha256(path) for path in paths}


def audit_packaged_notices(apk: Path, expected: dict[str, str]) -> None:
    with zipfile.ZipFile(apk) as archive:
        entries = [entry for entry in archive.infolist() if entry.filename.startswith("assets/licenses/") and not entry.is_dir()]
        actual = {entry.filename.removeprefix("assets/licenses/"): hashlib.sha256(archive.read(entry)).hexdigest() for entry in entries}
        if len(entries) != len(actual):
            raise RuntimeError("Packaged APK repeats a license asset")
    if actual != expected:
        raise RuntimeError("Packaged APK license assets differ from the reviewed runtime/source notices")


def spdx_components(runtime: dict) -> list[dict]:
    """Add precisely the runtime graph to the existing source-bound SPDX document."""
    from .release import _component_spdx_package
    result = []
    for component in runtime["components"]:
        coordinate = component["group"] + ":" + component["name"]
        package = _component_spdx_package(name=coordinate, declared_license=component["license"],
            version=component["version"], checksums=[{"algorithm": "SHA256", "checksumValue": item["sha256"]}
                                                   for item in component["artifacts"]],
            comment="Selected app runtimeClasspath; plugin/compiler dependencies are excluded.")
        package["externalRefs"] = [{"referenceCategory": "PACKAGE-MANAGER", "referenceType": "purl",
            "referenceLocator": "pkg:maven/" + component["group"] + "/" + component["name"] + "@" + component["version"]}]
        result.append(package)
    return result
