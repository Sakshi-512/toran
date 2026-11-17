"""Install the pinned GoDaddy CLI (gddy) into the Docker image, checking its SHA-256 first.

Used only by the Dockerfile: python3 scripts/install_gddy.py /usr/local/bin
The version and checksums are pinned here from the official release (github.com/godaddy/cli,
gddy-checksums-sha256.txt). To upgrade: change GDDY_VERSION and both checksums together.
"""

import hashlib
import io
import platform
import sys
import tarfile
import urllib.request
from pathlib import Path

GDDY_VERSION = "0.2.37"
RELEASE_URL = "https://github.com/godaddy/cli/releases/download/v{version}/{asset}"
# Linux builds by machine type: (release asset, its SHA-256).
ASSETS = {
    "x86_64": (
        "gddy-x86_64-unknown-linux-gnu.tar.gz",
        "6b94a47ca3c89d4e65448a9f38e5f48451778b07889420de0d75ab1ce79231ba",
    ),
    "aarch64": (
        "gddy-aarch64-unknown-linux-gnu.tar.gz",
        "6ed6f1a20bff3cbb0367366cec3c092f40e4206bd20dd5f28f94d64931ba1404",
    ),
}
ASSETS["amd64"] = ASSETS["x86_64"]
ASSETS["arm64"] = ASSETS["aarch64"]
BINARY = "gddy"
TIMEOUT_SECONDS = 120


class InstallError(Exception):
    """The download is missing, for another machine, or does not match its checksum."""


def asset_for(machine: str) -> tuple[str, str]:
    """(asset name, SHA-256) for this machine type."""
    try:
        return ASSETS[machine.lower()]
    except KeyError as exc:
        raise InstallError(f"No pinned gddy build for machine type {machine!r}") from exc


def download(url: str) -> bytes:
    """Fetch one release file."""
    with urllib.request.urlopen(url, timeout=TIMEOUT_SECONDS) as response:  # noqa: S310 - fixed https URL
        return bytes(response.read())


def verified(data: bytes, expected_sha256: str) -> bytes:
    """Return data only if its SHA-256 matches the pinned value."""
    actual = hashlib.sha256(data).hexdigest()
    if actual != expected_sha256:
        raise InstallError(f"gddy checksum mismatch: expected {expected_sha256}, got {actual}")
    return data


def extract_binary(archive: bytes, target_dir: Path) -> Path:
    """Write only the gddy binary from the archive to target_dir (no other paths)."""
    with tarfile.open(fileobj=io.BytesIO(archive), mode="r:gz") as tar:
        member = next((m for m in tar.getmembers() if m.name == BINARY and m.isfile()), None)
        if member is None:
            raise InstallError("The archive has no gddy binary")
        source = tar.extractfile(member)
        assert source is not None  # noqa: S101 - a regular file always has content
        target = target_dir / BINARY
        target.write_bytes(source.read())
    target.chmod(0o755)
    return target


def install(target_dir: Path, machine: str | None = None) -> Path:
    """Download, verify and install the pinned gddy for this machine."""
    asset, sha256 = asset_for(machine or platform.machine())
    url = RELEASE_URL.format(version=GDDY_VERSION, asset=asset)
    return extract_binary(verified(download(url), sha256), target_dir)


def main(argv: list[str]) -> int:
    """Command-line entry point."""
    target_dir = Path(argv[0]) if argv else Path("/usr/local/bin")
    try:
        path = install(target_dir)
    except (InstallError, OSError) as exc:
        print(f"gddy install failed: {exc}", file=sys.stderr)
        return 1
    print(f"Installed gddy {GDDY_VERSION} at {path}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
