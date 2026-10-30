"""Build a source + prebuilt UI release without runtime data or secrets."""

import hashlib
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile

root = Path(__file__).resolve().parent.parent
target = root / "dist" / "bid-analysis-service-0.4.0.zip"
ui = root / "frontend" / "dist" / "index.html"
if not ui.is_file():
    raise SystemExit("Build frontend first: cd frontend && npm ci && npm run build")
target.parent.mkdir(exist_ok=True)
files = [root / name for name in (
    ".env.example", ".gitignore", ".gitattributes", "requirements.in", "requirements.txt",
    "requirements-dev.in", "requirements-dev.txt", "pyproject.toml", "Dockerfile",
    "compose.yaml", ".dockerignore", ".env.docker.example")]
for folder in ("analyzer", "tests", "examples", "scripts", "frontend/src", "frontend/dist"):
    files.extend(p for p in (root / folder).rglob("*")
                 if p.is_file() and "__pycache__" not in p.parts and p.suffix != ".pyc")
files.extend(root / "frontend" / name for name in (
    ".gitignore", "index.html", "package.json", "pnpm-lock.yaml", "pnpm-workspace.yaml",
    "tsconfig.json", "vite.config.ts") if (root / "frontend" / name).is_file())
with ZipFile(target, "w", ZIP_DEFLATED) as archive:
    for path in sorted(set(files)):
        archive.write(path, "bid-analysis-service/" + path.relative_to(root).as_posix())
checksum = hashlib.sha256(target.read_bytes()).hexdigest()
target.with_suffix(".zip.sha256").write_text(f"{checksum}  {target.name}\n", encoding="ascii")
print(f"{target}\nSHA256 {checksum}\n{target.stat().st_size:,} bytes")
