"""Explicit candidate inventories; full publication keeps every supported platform."""

PLATFORM_PROFILES = ("full", "windows-linux")

_PACKAGE_SUFFIXES = {
    "windows-x64": ("windows-x64.zip",),
    "windows-arm64": ("windows-arm64.zip",),
    "linux-x64": ("linux-amd64.deb", "linux-x86_64.AppImage"),
    "linux-arm64": ("linux-arm64.deb", "linux-aarch64.AppImage"),
    "macos-x64": ("macos-x64.dmg",),
    "macos-arm64": ("macos-arm64.dmg",),
    "linux-x64-musl": ("linux-amd64-musl.tar.zst",),
    "linux-arm64-musl": ("linux-arm64-musl.tar.zst",),
}


def package_layout(version: str, platform_profile: str = "full") -> dict[str, tuple[str, ...]]:
    """Return a caller-selected exact inventory, never infer it from present files."""
    if platform_profile not in PLATFORM_PROFILES:
        raise RuntimeError("Unsupported candidate platform profile")
    return {
        platform: tuple(f"emulebb-rust-v{version}-{suffix}" for suffix in suffixes)
        for platform, suffixes in _PACKAGE_SUFFIXES.items()
        if platform_profile == "full" or not platform.startswith("macos-")
    }
