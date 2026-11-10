"""Upstream image authorities kept independent from optional consumer mirrors."""
from types import MappingProxyType

NAMESPACE = "ghcr.io/emulebb/emulebb-build-ci"
UPSTREAM_ALPINE = "docker.io/library/alpine:3.24@sha256:3f4d6b32e26703c6082839234f43769d5046b3ac7c910033911d1e71d8cd630a"
UPSTREAM_RUST = MappingProxyType({
    "gnu": "docker.io/library/rust:1.98.1-bookworm@sha256:93ce27a88655056a51dbdd8f5f2d7ddc071c7b0070fb288a37b5a285fc83971e",
    "musl": "docker.io/library/rust:1.98.1-alpine3.24@sha256:7cc1c22d77d9432f7fe012a70e6d3e555af54c2a6832700ed7d553f1769ae89f",
})
UPSTREAM_RUNTIME = MappingProxyType({
    "musl": UPSTREAM_ALPINE,
    "gnu": "docker.io/library/ubuntu:26.04@sha256:6a06111c3b0a8600feadb270aec771dc239bf407b25625f93f44ab00e11f4d3c",
})


def select_image(role: str, upstream: str, enabled: bool) -> str:
    """An explicit mirror selection preserves the upstream digest, with no fallback."""
    expected = {"rust-gnu": UPSTREAM_RUST["gnu"], "rust-musl": UPSTREAM_RUST["musl"],
                "ubuntu-runtime": UPSTREAM_RUNTIME["gnu"], "alpine-runtime": UPSTREAM_ALPINE}
    if role not in expected or upstream != expected[role]:
        raise ValueError("Consumer image must match its maintained upstream authority")
    return NAMESPACE + "/" + role + "@" + upstream.rsplit("@", 1)[1] if enabled else upstream
