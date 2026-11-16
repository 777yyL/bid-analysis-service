"""Standalone read-only file service, deployed beside the main service's data directory."""
import hashlib
import secrets
from pathlib import Path

from fastapi import Depends, FastAPI, Header, HTTPException, Query
from fastapi.responses import FileResponse, Response
from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

from analyzer.material_files import scoped_path


class FileShareSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix='FILE_SHARE_', env_file='.env', extra='ignore')
    root: str = '/data/bidinfo'
    api_key: SecretStr = SecretStr('')


def create_file_app(root, api_key):
    root = Path(root).resolve(strict=True)
    if not root.is_dir() or not api_key:
        raise ValueError('文件分享服务必须配置有效根目录及 FILE_SHARE_API_KEY')
    app = FastAPI(title='招标材料只读分享服务')

    def auth(x_api_key: str = Header(default='')):
        if not secrets.compare_digest(x_api_key.encode(), api_key.encode()):
            raise HTTPException(401, '文件分享密钥无效')

    @app.get('/healthz')
    def health():
        return {'ok': True}

    @app.get('/files', dependencies=[Depends(auth)])
    def download(path: str = Query(min_length=1, max_length=4096),
                 if_none_match: str = Header(default='')):
        try:
            target = scoped_path(root, path)
            if not target.is_file():
                raise HTTPException(404, '文件不存在')
            stat = target.stat()
        except ValueError:
            raise HTTPException(400, '只允许下载根目录内的相对文件路径') from None
        except OSError:
            raise HTTPException(404, '文件不存在或不可读') from None
        etag = '"' + hashlib.sha256(f'{stat.st_mtime_ns}:{stat.st_size}'.encode()).hexdigest() + '"'
        headers = {'ETag': etag, 'Cache-Control': 'private, no-cache'}
        if if_none_match == etag:
            return Response(status_code=304, headers=headers)
        return FileResponse(target, filename=target.name, headers=headers,
                            media_type='application/octet-stream')

    return app


def file_app():
    cfg = FileShareSettings()
    return create_file_app(cfg.root, cfg.api_key.get_secret_value())
