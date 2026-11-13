"""SPDX declarations follow the canonical source license, without relicensing."""
from types import SimpleNamespace

from emule_workspace import release


def test_libpcpnatpmp_copying_declares_its_two_clause_bsd_license(tmp_path, monkeypatch):
    mini, pcp = tmp_path / "mini", tmp_path / "pcp"
    monkeypatch.setattr(release, "_emulebb_rust_native_source_roots", lambda layout: (mini, pcp))
    monkeypatch.setattr(release, "_repo_spdx_package", lambda name, root, declared_license="NOASSERTION":
                        {"name": name, "licenseDeclared": declared_license})
    layout = SimpleNamespace(build_repo_root=tmp_path / "build", tooling_repo_root=tmp_path / "tooling")
    components = release._emulebb_rust_sbom_components(layout, tmp_path / "rust")
    assert next(item for item in components if item["name"] == "eMuleBB libpcpnatpmp fork")["licenseDeclared"] == "BSD-2-Clause"
