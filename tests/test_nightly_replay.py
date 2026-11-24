import hashlib
import zipfile

import pytest

from emule_workspace import nightly_replay as replay


def test_selects_only_artifacts_available_by_the_completed_attempt():
    run = {"id": 42, "head_sha": "a" * 40, "updated_at": "2026-11-24T20:51:16Z"}
    artifact = {"name": "payload", "id": 1, "expired": False, "size_in_bytes": 100,
                "created_at": "2026-11-24T19:30:00Z", "digest": "sha256:" + "b" * 64,
                "workflow_run": {"id": run["id"], "head_sha": run["head_sha"]}}
    later = {**artifact, "id": 2, "created_at": "2026-11-24T20:52:00Z"}
    assert replay._select_artifacts(run, {"artifacts": [artifact, later], "total_count": 2}, {"payload"}) == {
        "payload": artifact,
    }
    foreign = {**artifact, "workflow_run": {"id": 41, "head_sha": run["head_sha"]}}
    with pytest.raises(RuntimeError, match="expected run, source and digest"):
        replay._select_artifacts(run, {"artifacts": [foreign], "total_count": 1}, {"payload"})
    with pytest.raises(RuntimeError, match="Missing retained inputs"):
        replay._select_artifacts(run, {"artifacts": [later], "total_count": 1}, {"payload"})


def test_cached_artifact_must_match_github_digest(tmp_path):
    blob = b"original"
    digest = hashlib.sha256(blob).hexdigest()
    path = tmp_path / f"1-{digest}.zip"
    path.write_bytes(blob)
    artifact = {"name": "payload", "id": 1, "digest": "sha256:" + digest, "size_in_bytes": len(blob)}
    assert replay._archive(artifact, tmp_path) == path
    path.write_bytes(b"modified")
    with pytest.raises(RuntimeError, match="GitHub SHA-256 or size"):
        replay._archive(artifact, tmp_path)


def test_extract_rejects_collisions_between_retained_artifacts(tmp_path):
    first, second = tmp_path / "first.zip", tmp_path / "second.zip"
    with zipfile.ZipFile(first, "w") as archive:
        archive.writestr("package.zip", b"first")
    with zipfile.ZipFile(second, "w") as archive:
        archive.writestr("PACKAGE.zip", b"second")
    destination = tmp_path / "release"
    replay._extract(first, destination)
    with pytest.raises(RuntimeError, match="duplicate flat"):
        replay._extract(second, destination)
    assert (destination / "package.zip").read_bytes() == b"first"
