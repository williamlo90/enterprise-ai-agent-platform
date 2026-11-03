"""Build a Linux Lambda ZIP locally. Does not provision cloud resources or read secrets."""

from pathlib import Path
import subprocess
import sys
from uuid import uuid4
from zipfile import ZipFile, ZIP_DEFLATED


def main():
    root = Path(__file__).resolve().parents[2]
    build = root / "local" / "oidc" / ("lambda-build-" + uuid4().hex[:10])
    target = build / "package"
    target.mkdir(parents=True)
    requirements = build / "requirements.txt"
    subprocess.run([sys.executable, "-m", "uv", "export", "--frozen", "--project", str(root / "backend"),
                    "--no-dev", "--no-emit-project", "--output-file", str(requirements)],
                   check=True, capture_output=True)
    subprocess.run([sys.executable, "-m", "uv", "pip", "install", "--target", str(target),
                    "--python-platform", "x86_64-manylinux_2_28", "--python-version", "3.13",
                    "--only-binary", ":all:", "--require-hashes", "-r", str(requirements)], check=True)
    artifact = build / "oidc-spike.zip"
    package = root / "backend" / "src" / "enterprise_ai_platform"
    with ZipFile(artifact, "w", ZIP_DEFLATED) as archive:
        for source in target.rglob("*"):
            if source.is_file() and "__pycache__" not in source.parts and source.suffix != ".pyc":
                archive.write(source, source.relative_to(target).as_posix())
        for source in package.rglob("*"):
            if source.is_file() and source.suffix in {".py", ".sql"} and "__pycache__" not in source.parts:
                archive.write(source, "enterprise_ai_platform/" + source.relative_to(package).as_posix())
    with ZipFile(artifact) as archive:
        assert "enterprise_ai_platform/oidc_spike_lambda.py" in archive.namelist()
        assert not any(Path(name).name.startswith(".env") for name in archive.namelist())
        assert sum(item.file_size for item in archive.infolist()) < 250 * 1024 * 1024
    print("Lambda ZIP ready:", artifact)
    print("No cloud resources created. No provider credential loaded or included.")


if __name__ == "__main__":
    main()
