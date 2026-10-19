from __future__ import annotations

import pytest

from emule_workspace import product_family


def test_product_family_toolchain_reports_supported_versions(monkeypatch: pytest.MonkeyPatch) -> None:
    versions = {
        "node.exe": "v24.4.1\n",
        "npm.cmd": "11.1.0\n",
        "rustc.exe": "rustc 1.86.0 (abc 2025-01-01)\n",
        "cargo.exe": "cargo 1.86.0 (abc 2025-01-01)\n",
    }

    monkeypatch.setattr(product_family.platform, "python_version", lambda: "3.11.8")
    monkeypatch.setattr(product_family.shutil, "which", lambda name: name if name in versions else None)
    monkeypatch.setattr(
        product_family,
        "run_captured",
        lambda command, **_: versions[command[0]],
    )

    payload = product_family.audit_product_family_toolchain()

    assert payload["totals"] == {"ok": 5, "warning": 0, "missing": 0}
    assert {check["name"] for check in payload["checks"]} == {
        "python",
        "node",
        "npm",
        "rustc",
        "cargo",
    }


def test_product_family_toolchain_warns_on_unapproved_node_major(monkeypatch: pytest.MonkeyPatch) -> None:
    versions = {
        "node.exe": "v25.8.1\n",
        "npm.cmd": "11.11.0\n",
        "rustc.exe": "rustc 1.86.0 (abc 2025-01-01)\n",
        "cargo.exe": "cargo 1.86.0 (abc 2025-01-01)\n",
    }

    monkeypatch.setattr(product_family.platform, "python_version", lambda: "3.11.8")
    monkeypatch.setattr(product_family.shutil, "which", lambda name: name if name in versions else None)
    monkeypatch.setattr(
        product_family,
        "run_captured",
        lambda command, **_: versions[command[0]],
    )

    payload = product_family.audit_product_family_toolchain()
    node = next(check for check in payload["checks"] if check["name"] == "node")

    assert payload["totals"]["warning"] == 1
    assert node["status"] == "warning"
    assert "outside the supported set" in node["note"]


def test_product_family_toolchain_strict_fails_on_warning(monkeypatch: pytest.MonkeyPatch) -> None:
    versions = {
        "node.exe": "v25.8.1\n",
        "npm.cmd": "11.11.0\n",
        "rustc.exe": "rustc 1.86.0 (abc 2025-01-01)\n",
        "cargo.exe": "cargo 1.86.0 (abc 2025-01-01)\n",
    }

    monkeypatch.setattr(product_family.platform, "python_version", lambda: "3.11.8")
    monkeypatch.setattr(product_family.shutil, "which", lambda name: name if name in versions else None)
    monkeypatch.setattr(
        product_family,
        "run_captured",
        lambda command, **_: versions[command[0]],
    )

    with pytest.raises(RuntimeError, match="node"):
        product_family.audit_product_family_toolchain(strict=True)
