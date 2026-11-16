"""Bounded downloads into a source-scoped local cache; no remote directory scanning."""
import hashlib
import json
import os
import tempfile
import time
from pathlib import Path, PurePosixPath, PureWindowsPath

import httpx

from analyzer.source import SourceError
from analyzer.task_trace import emit


def relative_path(value):
    if (not isinstance(value, str) or not value or '\\' in value or '\x00' in value
            or PurePosixPath(value).is_absolute() or PureWindowsPath(value).drive
            or any(p in ('', '.', '..') for p in value.split('/'))):
        raise ValueError('仅允许根目录内的相对文件路径')
    return value


def scoped_path(root, relative):
    relative_path(relative)
    target = (root / relative).resolve()
    if not target.is_relative_to(root):
        raise ValueError('文件路径越出允许的根目录')
    return target


class MaterialFiles:
    def __init__(self, cfg, client=None):
        self.cfg, self.client = cfg, client

    def fetch(self, relative, max_bytes=None, refresh=False):
        relative_path(relative)
        cfg = self.cfg
        limit = max_bytes or int(cfg.get('source_download_max_mb', 200)) * 1024 * 1024
        scope = hashlib.sha256(json.dumps([cfg.get('source_dsn'), cfg['source_download_url']],
                                        ensure_ascii=False).encode()).hexdigest()
        root = (Path(cfg.get('source_cache_root', './runtime/materials')) / scope).resolve()
        target = scoped_path(root, relative)
        meta = root / '.metadata' / (hashlib.sha256(relative.encode()).hexdigest() + '.json')
        headers = {'X-API-Key': cfg.get('source_download_key', '')}
        validator = {}
        if meta.is_file():
            try:
                validator = json.loads(meta.read_text(encoding='utf-8'))
                if not isinstance(validator, dict):
                    validator = {}
            except (ValueError, OSError):
                pass
        if target.is_file() and target.stat().st_size <= limit and meta.is_file():
            checked_at = validator.get('checked_at', 0)
            if (not refresh and isinstance(checked_at, (int, float))
                    and checked_at >= cfg.get('_material_snapshot_at', 0)):
                emit('file_cached', path=relative, bytes=target.stat().st_size)
                return target
            if validator.get('etag'):
                headers['If-None-Match'] = validator['etag']
        emit('downloading_file', path=relative)
        try:
            if self.client:
                return self._download(self.client, relative, headers, target, meta, limit)
            with httpx.Client(trust_env=False, follow_redirects=False,
                              timeout=cfg.get('source_download_timeout', 120)) as client:
                return self._download(client, relative, headers, target, meta, limit)
        except httpx.HTTPError:
            raise SourceError('文件分享服务连接失败或下载中断', retryable=True) from None
        except OSError:
            raise SourceError('本地材料缓存不可写或磁盘空间不足') from None

    def _download(self, client, relative, headers, target, meta, limit):
        url = self.cfg['source_download_url'].rstrip('/') + '/files'
        with client.stream('GET', url, params={'path': relative}, headers=headers) as response:
            if response.status_code == 304 and target.is_file():
                self._validator(meta, headers.get('If-None-Match'))
                emit('file_cached', path=relative, bytes=target.stat().st_size)
                return target
            if response.status_code != 200:
                code = response.status_code
                raise SourceError(f'文件下载返回 HTTP {code}', retryable=code in (408, 429) or code >= 500)
            length = response.headers.get('content-length', '')
            if length.isdigit() and int(length) > limit:
                raise SourceError(f'文件超过下载上限 {limit // 1024 // 1024} MB')
            target.parent.mkdir(parents=True, exist_ok=True)
            fd, temporary = tempfile.mkstemp(dir=target.parent, suffix='.part')
            try:
                size = 0
                with os.fdopen(fd, 'wb') as stream:
                    for block in response.iter_bytes(65536):
                        size += len(block)
                        if size > limit:
                            raise SourceError(f'文件超过下载上限 {limit // 1024 // 1024} MB')
                        stream.write(block)
                os.replace(temporary, target)
            finally:
                Path(temporary).unlink(missing_ok=True)
            self._validator(meta, response.headers.get('etag'))
            emit('file_downloaded', path=relative, bytes=size)
            return target

    @staticmethod
    def _validator(meta, etag):
        meta.parent.mkdir(parents=True, exist_ok=True)
        fd, temporary = tempfile.mkstemp(dir=meta.parent, suffix='.part')
        try:
            with os.fdopen(fd, 'w', encoding='utf-8') as stream:
                json.dump({'etag':etag, 'checked_at':time.time()}, stream)
            os.replace(temporary, meta)
        finally:
            Path(temporary).unlink(missing_ok=True)
