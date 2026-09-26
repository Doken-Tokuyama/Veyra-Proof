from veyra_proof.models import PlatformOS
from veyra_proof.platform_info import normalize_arch, normalize_os


def test_normalize_os():
    assert normalize_os("Windows") == PlatformOS.WINDOWS
    assert normalize_os("Darwin") == PlatformOS.MACOS
    assert normalize_os("Linux") == PlatformOS.LINUX
    assert normalize_os("FreeBSD") == PlatformOS.OTHER


def test_normalize_arch():
    assert normalize_arch("x86_64") == "x86_64"
    assert normalize_arch("amd64") == "x86_64"
    assert normalize_arch("arm64") == "aarch64"
    assert normalize_arch("aarch64") == "aarch64"
