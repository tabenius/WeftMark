import importlib.util
from pathlib import Path

import pytest

SCRIPT = Path(__file__).parents[1] / "scripts/package_version.py"
spec = importlib.util.spec_from_file_location("package_version", SCRIPT)
assert spec is not None and spec.loader is not None
package_version = importlib.util.module_from_spec(spec)
spec.loader.exec_module(package_version)


@pytest.mark.parametrize(
    ("version", "expected"),
    [
        ("0.1.0", "0.1.0"),
        ("0.1.0a1", "0.1.0~a1"),
        ("0.1.0b2", "0.1.0~b2"),
        ("0.1.0rc3", "0.1.0~rc3"),
    ],
)
def test_debian_version_normalizes_pep440_prereleases(version, expected):
    assert package_version.debian_version(version) == expected


@pytest.mark.parametrize(
    "version", ["0.1", "0.1.0.post1", "0.1.0.dev1", "v0.1.0", "0.1.0a"]
)
def test_debian_version_refuses_unsupported_versions(version):
    with pytest.raises(ValueError):
        package_version.debian_version(version)
