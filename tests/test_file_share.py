import time
from contextlib import contextmanager
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from analyzer.file_share import create_file_app
from analyzer.material_files import MaterialFiles
from analyzer.source import SourceError


def test_share_auth_scope_binary_and_conditional_download(tmp_path):
    (tmp_path / 'attachments').mkdir()
    data = b'\x00\xfforiginal'
    (tmp_path / 'attachments' / 'test.pdf').write_bytes(data)
    with TestClient(create_file_app(tmp_path, 'share-key')) as client:
        url = '/files?path=attachments/test.pdf'
        assert client.get(url).status_code == 401
        headers = {'X-API-Key': 'share-key'}
        response = client.get(url, headers=headers)
        assert response.content == data
        assert client.get(url, headers={**headers, 'If-None-Match': response.headers['etag']}).status_code == 304
        for path in ('../secret', '/etc/passwd', 'C:/secret', 'a\\b', 'a/../secret'):
            assert client.get('/files', params={'path': path}, headers=headers).status_code == 400
        assert client.get('/files', params={'path': 'missing'}, headers=headers).status_code == 404


def test_share_rejects_symlink_outside_root(tmp_path):
    root = tmp_path / 'root'
    root.mkdir()
    (tmp_path / 'secret').write_text('secret')
    try:
        (root / 'escape').symlink_to(tmp_path / 'secret')
    except OSError:
        pytest.skip('symlinks unavailable')
    with TestClient(create_file_app(root, 'key')) as client:
        assert client.get('/files?path=escape', headers={'X-API-Key':'key'}).status_code == 400


def test_download_cache_revalidates_and_never_keeps_partial_file(tmp_path):
    calls = []
    def respond(request):
        calls.append(request)
        assert request.headers['X-API-Key'] == 'key'
        assert request.url.params['path'] == 'body.txt'
        if request.headers.get('if-none-match') == 'v1':
            return httpx.Response(304)
        return httpx.Response(200, content='采购需求：访客管理'.encode(), headers={'etag':'v1'})
    cfg = {'source_download_url':'http://share', 'source_download_key':'key',
           'source_cache_root':str(tmp_path), 'source_dsn':'scope'}
    with httpx.Client(transport=httpx.MockTransport(respond)) as client:
        files = MaterialFiles(cfg, client)
        path = files.fetch('body.txt')
        assert path.read_text(encoding='utf-8') == '采购需求：访客管理'
        assert files.fetch('body.txt') == path
        assert len(calls) == 1
        assert files.fetch('body.txt', refresh=True) == path
        assert len(calls) == 2
        cfg['_material_snapshot_at'] = time.time()
        assert files.fetch('body.txt') == path
        assert len(calls) == 3
        assert files.fetch('body.txt') == path
        assert len(calls) == 3
    with httpx.Client(transport=httpx.MockTransport(lambda r:httpx.Response(200, content=b'x'*30))) as client:
        with pytest.raises(SourceError, match='上限'):
            MaterialFiles(cfg, client).fetch('oversize.txt', max_bytes=20)
    assert not list(tmp_path.rglob('*.part'))
    assert not list(tmp_path.rglob('oversize.txt'))


def test_download_errors_hide_secrets_and_do_not_silently_use_stale_cache(tmp_path):
    cfg = {'source_download_url':'http://share', 'source_download_key':'secret',
           'source_cache_root':str(tmp_path), 'source_dsn':'scope'}
    with httpx.Client(transport=httpx.MockTransport(lambda r:httpx.Response(403, text='secret'))) as client:
        with pytest.raises(SourceError, match='403') as error:
            MaterialFiles(cfg, client).fetch('body.txt')
    assert 'secret' not in str(error.value)
    assert not list(Path(tmp_path).rglob('body.txt'))


def test_snapshot_only_downloads_selected_files_for_present_projects(tmp_path, monkeypatch):
    from analyzer.sales_models import RuntimeSettings
    from analyzer.sales_sync import SnapshotSource

    class Rows:
        def __init__(self, rows):
            self.rows = rows
        def fetchall(self):
            return self.rows

    class LocalView:
        def execute(self, sql, params):
            if 'mv_project' in sql:
                return Rows([{'project_key':'p', 'channel':'采购公告'}] if params == ('p',) else [])
            if 'FROM public.notices' in sql:
                return Rows([
                    {'notice_id':1,'title':'采购软件','channel':'采购公告','revoke_status':'active','content_path':'body.txt'},
                    {'notice_id':2,'title':'采购意向','channel':'采购意向','revoke_status':'active','content_path':'unused.txt'},
                    {'notice_id':3,'title':'中标结果','channel':'结果公告','revoke_status':'active','content_path':'result.txt'}])
            if 'FROM public.attachments' in sql:
                return Rows([{'att_id':4, 'notice_id':1, 'filename':'spec.pdf',
                              'text_path':'spec.pdf.mineru.txt', 'local_path':'spec.pdf'}])
            raise AssertionError(sql)

    cfg = RuntimeSettings(source_download_url='http://share', source_cache_root=str(tmp_path)).model_dump()
    source = SnapshotSource(cfg, None)
    source.snapshot_at = 1
    @contextmanager
    def connection():
        yield LocalView()
    source._connection = connection
    calls = []
    def fetch(self, relative, max_bytes=None, refresh=False):
        calls.append(relative)
        target = tmp_path / relative
        target.write_text('采购需求：访客管理软件', encoding='utf-8')
        return target
    monkeypatch.setattr(MaterialFiles, 'fetch', fetch)
    material = source.project_material('p')
    assert material['status'] == 'ready'
    assert set(calls) == {'body.txt', 'spec.pdf', 'spec.pdf.mineru.txt'}
    assert len(material['documents']) == 2
    calls.clear()
    assert source.project_material('outside')['status'] == 'stage_filtered'
    assert calls == []
