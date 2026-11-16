
import httpx
import pytest

from analyzer.sales_models import RuntimeSettings, SalesModelClient
from analyzer.sales_source import SalesSource
from analyzer.source import SourceError


def test_text_reader_cleans_html_nul_and_rejects_escape(tmp_path):
    root = tmp_path / 'data'
    root.mkdir()
    (root / 'body.html').write_text('<p>采购访客\x00预约</p><script>忽略</script>', encoding='utf-8-sig')
    (tmp_path / 'outside.txt').write_text('secret')
    source = SalesSource({'source_file_root': str(root)})
    assert source._read_text('body.html') == '采购访客预约'
    for path in ('../outside.txt', str(tmp_path / 'outside.txt'), 'C:\\outside.txt'):
        with pytest.raises(SourceError):
            source._read_text(path)


@pytest.mark.asyncio
async def test_document_embedding_uses_document_prefix_and_batches():
    import json
    requests = []
    def respond(request):
        payload = json.loads(request.content)
        requests.append(payload['input'])
        return httpx.Response(200, json={'data': [
            {'index': i, 'embedding': [1, i + 1]} for i in range(len(payload['input']))]})
    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
        client = SalesModelClient(RuntimeSettings(embedding_base_url='https://example/v1',
            embedding_model='m', embedding_batch_size=2, embedding_document_prefix='D:'), http)
        vectors = await client.embed_documents(['正文', '附件一', '附件二'])
    assert len(vectors) == 3
    assert requests == [['D:正文', 'D:附件一'], ['D:附件二']]
