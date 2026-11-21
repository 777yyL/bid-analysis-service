"""Build a source + prebuilt UI release without runtime data or secrets."""

import hashlib
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile

root = Path(__file__).resolve().parent.parent
target = root / "dist" / "bid-analysis-service-0.8.25.zip"
ui = root / "frontend" / "dist" / "index.html"
if not ui.is_file():
    raise SystemExit("Build frontend first: cd frontend && npm ci && npm run build")
target.parent.mkdir(exist_ok=True)
files = [root / name for name in (
    ".env.example", ".gitignore", ".gitattributes", "requirements.in", "requirements.txt",
    "requirements-dev.in", "requirements-dev.txt", "pyproject.toml", "Dockerfile",
    "compose.yaml", "compose.files.yaml", "compose.shared.yaml", ".env.shared.example", "docs/shared-workbench-deployment.md", "docs/agent-mcp.md", "docs/full-tenders.md", "docs/structured-facts.md", "docs/presales-snapshot.md", "reports/presales-snapshot-100.json", ".env.files.example", ".dockerignore", ".env.docker.example")]
for folder in ("analyzer", "tests", "examples", "scripts", "skills", "vendor/mcp-wheels", "frontend/src", "frontend/scripts", "frontend/dist"):
    files.extend(p for p in (root / folder).rglob("*")
                 if p.is_file() and "__pycache__" not in p.parts and p.suffix != ".pyc" and p.name != "connection.private.json")
files.extend(root / "frontend" / name for name in (
    ".gitignore", "index.html", "package.json", "pnpm-lock.yaml", "pnpm-workspace.yaml",
    "tsconfig.json", "vite.config.ts", "components.json") if (root / "frontend" / name).is_file())
with ZipFile(target, "w", ZIP_DEFLATED) as archive:
    for path in sorted(set(files)):
        archive.write(path, "bid-analysis-service/" + path.relative_to(root).as_posix())
checksum = hashlib.sha256(target.read_bytes()).hexdigest()
target.with_suffix(".zip.sha256").write_text(f"{checksum}  {target.name}\n", encoding="ascii")
print(f"{target}\nSHA256 {checksum}\n{target.stat().st_size:,} bytes")

skill=root/'skills'/'tender-presales'
skill_zip=target.with_name('招采雷达-0.8.25.zip')
with ZipFile(skill_zip,'w',ZIP_DEFLATED) as archive:
    for f in sorted(skill.rglob('*')):
        if f.is_file() and '__pycache__' not in f.parts and f.name!='connection.private.json':
            archive.write(f,'tender-presales/'+f.relative_to(skill).as_posix())
skill_zip.with_suffix('.zip.sha256').write_text(hashlib.sha256(skill_zip.read_bytes()).hexdigest()+'  '+skill_zip.name+'\n', encoding='utf-8')
print(skill_zip)

builder=target.with_name('招采雷达-内网打包器.pyz')
with ZipFile(builder,'w',ZIP_DEFLATED) as archive:
    archive.write(root/'scripts/package_private_skill.py','__main__.py')
    for name in ('SKILL.md','agents/openai.yaml','scripts/tender_cli.py','references/business-scope.md'):
        archive.write(skill/name,'tender-presales/'+name)
print(builder)
