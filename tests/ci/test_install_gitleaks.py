import hashlib
import io
import os
from pathlib import Path
import subprocess
import tarfile


SCRIPT = Path(__file__).resolve().parents[2] / "ci" / "install_gitleaks.sh"


def _release_archive(path: Path, payload: bytes = b"#!/bin/sh\necho gitleaks-test\n") -> str:
    info = tarfile.TarInfo("gitleaks")
    info.mode = 0o755
    info.size = len(payload)
    with tarfile.open(path, "w:gz") as archive:
        archive.addfile(info, io.BytesIO(payload))
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _env(tmp_path: Path, digest: str) -> dict[str, str]:
    return {
        **os.environ,
        "GITLEAKS_VERSION": "8.21.2",
        "GITLEAKS_SHA256": digest,
        "GITLEAKS_CACHE_ROOT": str(tmp_path / "cache"),
        "GITLEAKS_TOOL_ROOT": str(tmp_path / "tool"),
    }


def test_verified_cache_hit_never_calls_network(tmp_path):
    cache = tmp_path / "cache"
    cache.mkdir()
    archive = cache / "gitleaks_8.21.2_linux_x64.tar.gz"
    digest = _release_archive(archive)
    result = subprocess.run(
        ["sh", str(SCRIPT)], text=True, capture_output=True,
        env={**_env(tmp_path, digest), "CURL_BIN": "/bin/false"}, check=True,
    )
    tool = Path(result.stdout.strip())
    assert tool.is_file() and os.access(tool, os.X_OK)
    assert "using verified" in result.stderr


def test_corrupt_cache_is_replaced_only_by_digest_verified_download(tmp_path):
    fixture = tmp_path / "release.tar.gz"
    digest = _release_archive(fixture)
    cache = tmp_path / "cache"
    cache.mkdir()
    (cache / "gitleaks_8.21.2_linux_x64.tar.gz").write_bytes(b"poisoned")
    downloader = tmp_path / "curl"
    downloader.write_text(
        "#!/bin/sh\n"
        "while [ \"$1\" != --output ]; do shift; done\n"
        "cp \"$FIXTURE\" \"$2\"\n"
    )
    downloader.chmod(0o755)
    result = subprocess.run(
        ["sh", str(SCRIPT)], text=True, capture_output=True,
        env={**_env(tmp_path, digest), "CURL_BIN": str(downloader), "FIXTURE": str(fixture)},
        check=True,
    )
    assert "populated verified" in result.stderr
    cached = cache / "gitleaks_8.21.2_linux_x64.tar.gz"
    assert hashlib.sha256(cached.read_bytes()).hexdigest() == digest


def test_download_with_wrong_digest_fails_closed(tmp_path):
    fixture = tmp_path / "release.tar.gz"
    _release_archive(fixture)
    downloader = tmp_path / "curl"
    downloader.write_text(
        "#!/bin/sh\n"
        "while [ \"$1\" != --output ]; do shift; done\n"
        "cp \"$FIXTURE\" \"$2\"\n"
    )
    downloader.chmod(0o755)
    result = subprocess.run(
        ["sh", str(SCRIPT)], text=True, capture_output=True,
        env={**_env(tmp_path, "0" * 64), "CURL_BIN": str(downloader), "FIXTURE": str(fixture)},
    )
    assert result.returncode != 0
    assert not (tmp_path / "cache" / "gitleaks_8.21.2_linux_x64.tar.gz").exists()
