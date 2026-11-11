"""Exercise selected assembly inventories without native builds or real artifacts."""
import json

import pytest
from click.testing import CliRunner

from emule_workspace import cli, release
from emule_workspace.platform_profiles import package_layout


@pytest.mark.parametrize("profile,version,count", [
    ("full", "0.1.0-beta.2", 10),
    ("windows-linux", "0.1.0-beta.2", 8),
    ("full", "0.1.0-beta.2.nightly.20261111.g1234567", 12),
])
def test_assembler_checks_selected_payload_bytes_and_manifests(tmp_path, monkeypatch, profile, version, count):
    names = [name for values in package_layout(version, profile).values() for name in values]
    if ".nightly." in version:
        names.extend(f"emulebb-rust-v{version}-android-{abi}.tar.zst" for abi in ("arm64-v8a", "x86_64"))
    assert len(names) == count
    monkeypatch.setattr(release, "_emulebb_rust_artifact_origin", lambda: {"origin": "local-development"})
    for name in names:
        payload = tmp_path / name
        payload.write_bytes(b"Synthetic package")
        sbom = tmp_path / (name + ".sbom.spdx.json")
        sbom.write_text("{}", encoding="utf-8")
        (tmp_path / (name + ".manifest.json")).write_text(json.dumps({
            "schema": "emulebb.rust.package/2", "sourceDirty": False,
            "version": version, "asset": name, "sha256": release._sha256(payload),
            "sbom": sbom.name, "sbomSha256": release._sha256(sbom),
        }), encoding="utf-8")
    assert release.assemble_emulebb_rust_release_assets(tmp_path, version, platform_profile=profile).is_file()
    (tmp_path / names[-1]).write_bytes(b"Changed synthetic package")
    with pytest.raises(RuntimeError, match="provenance mismatch"):
        release.assemble_emulebb_rust_release_assets(tmp_path, version, platform_profile=profile)


def test_unknown_profile_cannot_choose_inventory_from_present_files():
    with pytest.raises(RuntimeError, match="Unsupported"):
        package_layout("0.1.0-beta.2", "whatever-is-present")


def test_scoped_profile_cannot_assemble_a_nightly(tmp_path):
    with pytest.raises(RuntimeError, match="Nightly release assembly requires the full"):
        release.assemble_emulebb_rust_release_assets(
            tmp_path, "0.1.0-beta.2.nightly.20261111.g1234567", platform_profile="windows-linux",
        )


@pytest.mark.parametrize("options,expected", [
    ([], "full"),
    (["--platform-profile", "windows-linux"], "windows-linux"),
])
def test_assembly_cli_preserves_default_and_forwards_explicit_profile(tmp_path, monkeypatch, options, expected):
    assets = tmp_path / "assets"
    assets.mkdir()
    monkeypatch.setenv("EMULEBB_WORKSPACE_OUTPUT_ROOT", str(tmp_path))
    captured = []

    def assemble(directory, version, *, platform_profile):
        captured.append((directory, version, platform_profile))
        return directory / "SHA256SUMS"

    monkeypatch.setattr(cli, "assemble_emulebb_rust_release_assets", assemble)
    result = CliRunner().invoke(cli.assemble_emulebb_rust_release_ci, [
        "--release-version", "0.1.0-beta.2", "--assets-dir", str(assets), *options,
    ])
    assert result.exit_code == 0, result.output
    assert captured == [(assets.resolve(), "0.1.0-beta.2", expected)]
