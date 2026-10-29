"""Read-only adapters for the existing bid-info APIs and optional PostgreSQL source."""

import asyncio
import hashlib
import json
import math
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path, PureWindowsPath
from typing import Any

import httpx
import psycopg
from bs4 import BeautifulSoup
from psycopg.rows import dict_row
from pydantic import ValidationError

from analyzer.config import Settings
from analyzer.schemas import (
    Attachment,
    BusinessLine,
    Keyword,
    KnowledgeInput,
    Lot,
    NoticeInput,
    TextCache,
)


class SourceError(Exception):
    def __init__(self, message: str, retryable: bool = False):
        super().__init__(message)
        self.retryable = retryable


def _text(value: Any) -> str:
    if value is None:
        return ""
    if not isinstance(value, (str, int, float)) or isinstance(value, bool):
        raise SourceError("来源文本字段类型不符合预期")
    return str(value)


def _plain(value: Any) -> str:
    text = _text(value)
    if "<" not in text or ">" not in text:
        return text.strip()
    soup = BeautifulSoup(text, "html.parser")
    for element in soup(["script", "style"]):
        element.decompose()
    return "\n".join(line.strip() for line in soup.get_text("\n").splitlines() if line.strip())


def _envelopes(payload: Any) -> list[dict]:
    """Keep the outer envelopes because lots/attachments may be siblings of notice."""
    if not isinstance(payload, dict):
        raise SourceError("来源详情不是 JSON 对象")
    envelopes = [payload]
    for _ in range(6):
        current = envelopes[-1]
        next_item = next((current[key] for key in ("data", "notice", "record")
                          if isinstance(current.get(key), dict)), None)
        if next_item is None:
            return envelopes
        envelopes.append(next_item)
    raise SourceError("来源详情封装层数异常")


def _items(payload: Any, names: tuple[str, ...]) -> tuple[list[dict], int | None]:
    if isinstance(payload, list):
        rows, total = payload, None
    elif isinstance(payload, dict):
        total = None
        current = payload
        rows = None
        for _ in range(6):
            for key in ("total", "total_count"):
                if current.get(key) is not None:
                    raw = current[key]
                    if isinstance(raw, bool) or not str(raw).isdigit():
                        raise SourceError("来源分页总数无效")
                    total = int(raw)
            rows = next((current[key] for key in names if isinstance(current.get(key), list)), None)
            if rows is not None:
                break
            if isinstance(current.get("data"), list):
                rows = current["data"]
                break
            if isinstance(current.get("data"), dict):
                current = current["data"]
            else:
                break
        if rows is None:
            raise SourceError("无法识别来源列表结构；需要 items/notices/keywords/data 数组")
    else:
        raise SourceError("来源列表不是 JSON 数组或对象")
    if not all(isinstance(row, dict) for row in rows):
        raise SourceError("来源列表包含非对象条目")
    return rows, total


def _positive_id(value: Any, label: str) -> int:
    if isinstance(value, bool) or not str(value).isdigit() or int(value) <= 0:
        raise SourceError(f"来源 {label} 无效")
    return int(value)


class SourceClient:
    def __init__(self, settings: Settings, text_cache: TextCache | None = None,
                 client: httpx.AsyncClient | None = None):
        self.settings = settings
        self.text_cache = text_cache
        self._owned_client = client is None
        self.client = client or httpx.AsyncClient(timeout=settings.source_timeout_seconds,
                                                 follow_redirects=False,
                                                 trust_env=settings.http_trust_env)

    async def aclose(self) -> None:
        if self._owned_client:
            await self.client.aclose()

    async def _get(self, path: str, params: dict | None = None) -> tuple[bytes, str]:
        if not path.startswith("/") or path.startswith("//"):
            raise SourceError("来源接口路径必须是以 / 开始的站内路径")
        url = self.settings.source_base_url.rstrip("/") + path
        headers = {}
        token = self.settings.source_api_token.get_secret_value()
        if token:
            name = self.settings.source_auth_header
            headers[name] = f"Bearer {token}" if name.lower() == "authorization" else token
        try:
            async with self.client.stream("GET", url, params=params, headers=headers,
                                          timeout=self.settings.source_timeout_seconds,
                                          follow_redirects=False) as response:
                if response.status_code != 200:
                    code = response.status_code
                    raise SourceError(f"来源接口返回 HTTP {code}", retryable=code == 429 or code >= 500)
                data = bytearray()
                async for chunk in response.aiter_bytes():
                    data.extend(chunk)
                    if len(data) > self.settings.source_max_response_bytes:
                        raise SourceError("来源响应超过 SOURCE_MAX_RESPONSE_BYTES，未截断处理")
                return bytes(data), response.headers.get("content-type", "")
        except httpx.HTTPError:
            raise SourceError("来源接口连接失败或超时", retryable=True) from None

    async def _json(self, path: str, params: dict | None = None) -> Any:
        data, _ = await self._get(path, params)
        try:
            return json.loads(data)
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise SourceError("来源接口未返回合法 JSON") from None

    def _page_params(self, page: int, page_size: int) -> dict[str, int]:
        if page < 1 or not 1 <= page_size <= 1000:
            raise SourceError("分页参数无效")
        return {self.settings.source_page_param: page,
                self.settings.source_page_size_param: page_size}

    async def list_notice_ids(self, page: int, page_size: int) -> list[int]:
        payload = await self._json(self.settings.source_notices_path,
                                   self._page_params(page, page_size))
        rows, _ = _items(payload, ("items", "notices", "results"))
        ids = [_positive_id(row.get("notice_id"), "notice_id") for row in rows]
        if len(ids) != len(set(ids)):
            raise SourceError("来源同一页包含重复 notice_id")
        return ids

    @staticmethod
    def _collection(envelopes: list[dict], key: str) -> list[dict]:
        collections = [envelope[key] for envelope in reversed(envelopes)
                       if envelope.get(key) is not None]
        if not collections:
            return []
        if any(not isinstance(items, list) or not all(isinstance(row, dict) for row in items)
               for items in collections):
            raise SourceError(f"来源 {key} 必须是对象数组")
        # Both levels can carry data; merge exact duplicates without losing outer-only records.
        result, seen = [], set()
        for items in collections:
            for row in items:
                marker = json.dumps(row, sort_keys=True, ensure_ascii=False)
                if marker not in seen:
                    result.append(row)
                    seen.add(marker)
        return result

    def _body(self, envelopes: list[dict]) -> str:
        field = self.settings.source_body_field
        if field:
            for envelope in reversed(envelopes):
                value: Any = envelope
                for part in field.split("."):
                    value = value.get(part) if isinstance(value, dict) else None
                if value is not None:
                    return _plain(value)
            raise SourceError("SOURCE_BODY_FIELD 在来源公告中不存在")
        for envelope in reversed(envelopes):
            for key in ("body", "body_html", "content_html", "content", "body_text"):
                if isinstance(envelope.get(key), str) and envelope[key].strip():
                    return _plain(envelope[key])
        return ""

    async def _attachment_text(self, att_id: int) -> str:
        data, content_type = await self._get(
            self.settings.source_attachment_path.format(att_id=att_id))
        try:
            if "json" in content_type:
                envelopes = _envelopes(json.loads(data))
                for envelope in reversed(envelopes):
                    for key in ("text", "content", "body"):
                        if isinstance(envelope.get(key), str):
                            return _plain(envelope[key])
                raise SourceError("附件文本接口 JSON 缺少 text/content 字段")
            return _plain(data.decode("utf-8-sig"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise SourceError("附件文本不是有效 UTF-8 或 JSON") from None

    async def fetch_notice(self, notice_id: int) -> NoticeInput:
        notice_id = _positive_id(notice_id, "notice_id")
        if self._file_database_enabled():
            return (await self.fetch_notice_batch([notice_id]))[0]
        payload = await self._json(self.settings.source_notice_path.format(notice_id=notice_id))
        envelopes = _envelopes(payload)
        metadata = {}
        for envelope in envelopes:
            metadata.update(envelope)
        actual_id = _positive_id(metadata.get("notice_id"), "notice_id")
        if actual_id != notice_id:
            raise SourceError("来源详情 notice_id 与请求不一致")
        title = _text(metadata.get("title")).strip()
        if not title:
            raise SourceError("来源公告缺少标题")
        warnings = []
        body = self._body(envelopes)
        if not body:
            warnings.append("公告正文缺失或接口字段未识别；请检查 SOURCE_BODY_FIELD")
        lots = [Lot(lot_no=_text(row.get("lot_no")), lot_name=_text(row.get("lot_name")),
                    description=_text(row.get("description")))
                for row in self._collection(envelopes, "lots")]
        attachments = []
        attachment_rows = self._collection(envelopes, "attachments")
        seen_ids = set()
        for row in attachment_rows:
            att_id = _positive_id(row.get("att_id"), "att_id")
            if att_id in seen_ids:
                raise SourceError("来源详情包含冲突的重复 att_id")
            seen_ids.add(att_id)
            sha = _text(row.get("sha256")).strip().lower() or None
            cache_key = f"source-text-v1:{self.settings.source_base_url.rstrip('/')}:{sha}" if sha else None
            text = self.text_cache.get_text(cache_key) if self.text_cache and cache_key else None
            complete = bool(text)
            if not text:
                transient_error = None
                try:
                    text = await self._attachment_text(att_id)
                    complete = row.get("extract_status") in (None, "ok") and bool(text)
                except SourceError as error:
                    text = ""
                    if error.retryable:
                        transient_error = error
                if not text and sha and self.settings.source_db_dsn.get_secret_value():
                    try:
                        duplicate_id = await asyncio.to_thread(self._find_duplicate, sha, att_id)
                        if duplicate_id is not None:
                            text = await self._attachment_text(duplicate_id)
                            complete = bool(text)
                    except SourceError as error:
                        if error.retryable:
                            transient_error = error
                        warnings.append(f"附件 {att_id} 的重复副本读取失败")
                if not text and transient_error is not None:
                    raise SourceError(f"附件 {att_id} 暂时不可读，等待重试", retryable=True)
                if complete and self.text_cache and cache_key:
                    self.text_cache.set_text(cache_key, text)
            if not text:
                warnings.append(f"附件 {att_id} 缺少可读文本")
            elif not complete:
                warnings.append(f"附件 {att_id} 文本解析状态不是完整成功")
            attachments.append(Attachment(
                att_id=att_id, filename=_text(row.get("filename")), sha256=sha,
                text=text or "", summary=_text(row.get("summary")),
                extract_status="ok" if complete else row.get("extract_status"),
            ))
        try:
            return NoticeInput(
                notice_id=notice_id, title=title, buyer=_text(metadata.get("buyer")),
                notice_type=_text(metadata.get("notice_type") or metadata.get("channel")),
                project_key=metadata.get("project_key_name") or metadata.get("project_key"),
                publish_at=metadata.get("publish_at") or None,
                body=body, scope_summary=_text(metadata.get("scope_summary")), lots=lots,
                attachments=attachments, warnings=warnings,
            )
        except ValidationError:
            raise SourceError("来源公告字段不符合分析输入契约") from None

    def _file_database_enabled(self) -> bool:
        return bool(self.settings.source_db_dsn.get_secret_value()
                    and getattr(self.settings, "source_file_root", ""))

    async def fetch_notice_batch(self, notice_ids: list[int]) -> list[NoticeInput]:
        ids = list(dict.fromkeys(_positive_id(value, "notice_id") for value in notice_ids))
        if not ids:
            return []
        if len(ids) > 1000:
            raise SourceError("单批公告不能超过 1000 条")
        if self._file_database_enabled():
            return await asyncio.to_thread(self._database_notices, ids, 0, len(ids), None)
        # Keep API concurrency bounded; source hosts may share a constrained connection pool.
        gate = asyncio.Semaphore(4)

        async def fetch(nid):
            async with gate:
                return await self.fetch_notice(nid)

        return list(await asyncio.gather(*(fetch(nid) for nid in ids)))

    async def list_notice_batch(self, after_id: int = 0, limit: int = 100,
                                updated_since: float | None = None) -> list[NoticeInput]:
        if isinstance(after_id, bool) or after_id < 0 or not 1 <= limit <= 1000:
            raise SourceError("批量游标或批次大小无效")
        if not self._file_database_enabled():
            raise SourceError("游标批量同步需要配置 SOURCE_DB_DSN 和 SOURCE_FILE_ROOT；接口来源请使用分页同步")
        if updated_since is not None and (not math.isfinite(updated_since) or updated_since < 0):
            raise SourceError("增量更新时间无效")
        return await asyncio.to_thread(self._database_notices, None, after_id, limit, updated_since)

    def _read_source_file(self, relative: Any, warnings: list[str], label: str) -> str:
        if not relative:
            warnings.append(f"{label}缺少文本路径")
            return ""
        try:
            raw = _text(relative)
            # Source paths are portable relative paths. Reject Windows rooted paths on Unix too.
            win_path = PureWindowsPath(raw)
            path = Path(raw.replace("\\", "/"))
            if path.is_absolute() or win_path.drive or win_path.root:
                raise ValueError("absolute source path")
            root = Path(getattr(self.settings, "source_file_root", "")).resolve(strict=True)
            resolved = (root / path).resolve(strict=True)
            if not resolved.is_relative_to(root) or not resolved.is_file():
                raise ValueError("outside file root")
            if resolved.stat().st_size > self.settings.source_max_response_bytes:
                warnings.append(f"{label}文本超过读取上限，未截断处理")
                return ""
            with resolved.open("rb") as handle:
                content = handle.read(self.settings.source_max_response_bytes + 1)
            if len(content) > self.settings.source_max_response_bytes:
                warnings.append(f"{label}文本超过读取上限，未截断处理")
                return ""
            return content.decode("utf-8-sig").strip()
        except (OSError, ValueError, UnicodeDecodeError):
            warnings.append(f"{label}文本文件不存在、不可读或路径越界")
            return ""

    @staticmethod
    def _source_columns(connection) -> dict[str, set[str]]:
        rows = connection.execute(
            "SELECT table_name, column_name FROM information_schema.columns "
            "WHERE table_schema = 'public' AND table_name IN ('notices','attachments','lots')"
        ).fetchall()
        columns: dict[str, set[str]] = {}
        for row in rows:
            columns.setdefault(row["table_name"], set()).add(row["column_name"])
        if not {"notice_id", "title", "content_path"} <= columns.get("notices", set()):
            raise SourceError("来源 notices 缺少 notice_id/title/content_path 必需字段")
        if not {"att_id", "notice_id", "text_path", "extract_status"} <= columns.get("attachments", set()):
            raise SourceError("来源 attachments 缺少附件元数据必需字段，不能判定材料是否完整")
        return columns

    def _database_notices(self, ids: list[int] | None, after_id: int,
                          limit: int, updated_since: float | None) -> list[NoticeInput]:
        try:
            with self._connect() as connection:
                connection.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY")
                columns = self._source_columns(connection)
                available = columns["notices"]
                selected = [name for name in (
                    "notice_id", "title", "buyer", "notice_type", "channel", "project_key_name",
                    "project_key", "scope_summary", "content_path", "revoke_status", "publish_at"
                ) if name in available]
                # Identifiers come exclusively from the hardcoded allowlist above; values are bound.
                query = "SELECT " + ", ".join(selected) + " FROM public.notices WHERE "
                if ids is not None:
                    query += "notice_id = ANY(%s)"
                    params: list[Any] = [ids]
                else:
                    query += "notice_id > %s"
                    params = [after_id]
                if updated_since is not None:
                    if "updated_at" not in available:
                        raise SourceError("来源没有可靠的 updated_at 更新时间字段；请执行补偿扫描")
                    query += " AND updated_at >= %s"
                    params.append(datetime.fromtimestamp(updated_since, timezone.utc))
                query += " ORDER BY notice_id LIMIT %s"
                params.append(limit)
                notices = connection.execute(query, tuple(params)).fetchall()
                found = [int(row["notice_id"]) for row in notices]
                if ids is not None and set(ids) != set(found):
                    raise SourceError("请求的部分来源公告不存在，未静默跳过")
                attachments: list[dict] = []
                lots: list[dict] = []
                duplicate_paths: dict[str, list[str]] = {}
                if found:
                    for table, fields, target in (
                        ("attachments", ("att_id", "notice_id", "filename", "sha256", "text_path",
                                         "extract_status", "summary", "skip_reason"), attachments),
                        ("lots", ("notice_id", "lot_no", "lot_name", "description"), lots),
                    ):
                        actual = columns.get(table, set())
                        if "notice_id" not in actual:
                            continue
                        fields_present = [name for name in fields if name in actual]
                        target.extend(connection.execute(
                            "SELECT " + ", ".join(fields_present) + f" FROM public.{table} "
                            "WHERE notice_id = ANY(%s)", (found,)
                        ).fetchall())
                    hashes = list({att["sha256"] for att in attachments if att.get("sha256")
                                   and (not att.get("text_path") or att.get("extract_status") != "ok")})
                    if hashes and "sha256" in columns["attachments"]:
                        copies = connection.execute(
                            "SELECT sha256, text_path FROM public.attachments "
                            "WHERE sha256 = ANY(%s) AND extract_status = 'ok' "
                            "AND text_path IS NOT NULL ORDER BY att_id", (hashes,)
                        ).fetchall()
                        for copy in copies:
                            duplicate_paths.setdefault(copy["sha256"], []).append(copy["text_path"])
            attachments_by_notice, lots_by_notice = defaultdict(list), defaultdict(list)
            for att in attachments:
                attachments_by_notice[int(att["notice_id"])].append(att)
            for lot in lots:
                lots_by_notice[int(lot["notice_id"])].append(lot)
            results = []
            for row in notices:
                nid = int(row["notice_id"])
                warnings: list[str] = []
                body = self._read_source_file(row.get("content_path"), warnings, "公告正文")
                if row.get("revoke_status") not in (None, "active"):
                    warnings.append("来源公告已撤销；历史标签仅供追溯")
                att_items = []
                for att in attachments_by_notice[nid]:
                    att_id = _positive_id(att.get("att_id"), "att_id")
                    att_warnings: list[str] = []
                    att_text = self._read_source_file(att.get("text_path"), att_warnings, f"附件 {att_id}")
                    extraction = att.get("extract_status")
                    if not att_text:
                        for copy_path in duplicate_paths.get(att.get("sha256"), []):
                            att_text = self._read_source_file(copy_path, [], f"附件 {att_id}")
                            if att_text:
                                extraction = "ok"
                                att_warnings = []
                                break
                    warnings.extend(att_warnings)
                    if att_text and extraction not in (None, "ok"):
                        warnings.append(f"附件 {att_id} 文本解析状态不是完整成功")
                    att_items.append(Attachment(
                        att_id=att_id, filename=_text(att.get("filename")),
                        sha256=att.get("sha256"), text=att_text,
                        summary=_text(att.get("summary")), extract_status=extraction,
                    ))
                results.append(NoticeInput(
                    notice_id=nid, title=_text(row.get("title")), buyer=_text(row.get("buyer")),
                    notice_type=_text(row.get("notice_type") or row.get("channel")),
                    project_key=row.get("project_key_name") or row.get("project_key"),
                    publish_at=row["publish_at"].isoformat() if isinstance(row.get("publish_at"), datetime)
                    else row.get("publish_at") or None,
                    scope_summary=_text(row.get("scope_summary")), body=body,
                    attachments=att_items, warnings=warnings,
                    lots=[Lot(lot_no=_text(lot.get("lot_no")), lot_name=_text(lot.get("lot_name")),
                              description=_text(lot.get("description")))
                          for lot in lots_by_notice[nid]],
                ))
            if ids is not None:
                by_id = {notice.notice_id: notice for notice in results}
                return [by_id[nid] for nid in ids]
            return results
        except psycopg.Error:
            raise SourceError("来源只读数据库批量查询失败", retryable=True) from None
        except ValidationError:
            raise SourceError("来源批量公告字段不符合分析输入契约") from None

    def _connect(self):
        return psycopg.connect(
            self.settings.source_db_dsn.get_secret_value(), row_factory=dict_row,
            connect_timeout=max(1, math.ceil(self.settings.source_timeout_seconds)),
            options="-c default_transaction_read_only=on -c statement_timeout=15000",
        )

    def _find_duplicate(self, sha: str, att_id: int) -> int | None:
        try:
            with self._connect() as connection:
                row = connection.execute(
                    "SELECT att_id FROM attachments WHERE sha256 = %s AND att_id <> %s "
                    "AND extract_status = 'ok' AND text_path IS NOT NULL ORDER BY att_id LIMIT 1",
                    (sha, att_id),
                ).fetchone()
                return int(row["att_id"]) if row else None
        except psycopg.Error:
            raise SourceError("来源只读数据库查询失败", retryable=True) from None

    def _database_knowledge(self) -> KnowledgeInput:
        try:
            with self._connect() as connection:
                connection.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY")
                rubric = connection.execute(
                    "SELECT rubric_version, weights, thresholds, exceptions, boundary_cases, negative_signals "
                    "FROM judge_rubric WHERE status = 'published' "
                    "ORDER BY published_at DESC NULLS LAST, rubric_version DESC LIMIT 1"
                ).fetchone()
                if not rubric:
                    raise SourceError("来源数据库没有已发布的判据")
                version = rubric["rubric_version"]
                line_rows = connection.execute(
                    "SELECT d.domain_name AS business_domain, d.line_name AS business_line, k.body_md "
                    "FROM dim_business_line d LEFT JOIN biz_knowledge k "
                    "ON k.business_line = d.line_name AND k.rubric_version = %s ORDER BY d.line_name",
                    (version,),
                ).fetchall()
                keyword_rows = connection.execute(
                    "SELECT business_line, word, kind, term_family, enabled "
                    "FROM biz_keyword WHERE enabled = true ORDER BY business_line, kind, word"
                ).fetchall()
            lines = [BusinessLine(**{**row, "body_md": row["body_md"] or ""}) for row in line_rows]
            complete = bool(lines) and all(line.body_md.strip() for line in lines)
            return KnowledgeInput(
                version=version, lines=lines, keywords=[Keyword(**row) for row in keyword_rows],
                quality="full" if complete else "keywords_only", source_rubric=dict(rubric),
                warnings=[] if complete else ["来源部分业务线缺少模块知识，结果需要复核"],
            )
        except psycopg.Error:
            raise SourceError("来源只读数据库知识同步失败", retryable=True) from None
        except ValidationError:
            raise SourceError("来源数据库知识不符合输入契约，请检查业务线与关键词") from None

    async def fetch_knowledge(self) -> KnowledgeInput:
        if self.settings.source_db_dsn.get_secret_value():
            return await asyncio.to_thread(self._database_knowledge)
        rows, seen, total_seen = [], set(), 0
        version = "keywords-api"
        for page in range(1, 1001):
            payload = await self._json(self.settings.source_keywords_path, self._page_params(page, 500))
            items, total = _items(payload, ("items", "keywords", "results"))
            if isinstance(payload, dict):
                version = _text(payload.get("rubric_version") or version)
            if not items:
                if total is not None and total_seen < total:
                    raise SourceError("来源关键词分页提前结束，拒绝同步不完整词表")
                break
            signature = hashlib.sha256(json.dumps(items, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
            if signature in seen:
                raise SourceError("来源关键词分页重复，可能忽略了分页参数")
            seen.add(signature)
            rows.extend(items)
            total_seen += len(items)
            if total is not None and total_seen >= total:
                break
        else:
            raise SourceError("来源关键词分页超出上限，拒绝同步不完整词表")
        if not rows:
            raise SourceError("来源关键词为空，请导入完整知识或配置 SOURCE_DB_DSN")
        lines = {}
        keywords = []
        seen_keywords = set()
        try:
            for row in rows:
                if row.get("enabled") is False:
                    continue
                domain = row.get("business_domain") or row.get("domain_name")
                line = row.get("business_line")
                if not domain:
                    raise SourceError("关键词接口缺少业务域，请导入完整知识或配置 SOURCE_DB_DSN")
                item = BusinessLine(business_domain=domain, business_line=line)
                if line in lines and lines[line].business_domain != domain:
                    raise SourceError("来源关键词同一业务线对应多个业务域")
                lines[line] = item
                keyword = Keyword(**{key: row[key] for key in Keyword.model_fields if key in row})
                key = (keyword.business_line, keyword.word, keyword.kind)
                if key in seen_keywords:
                    raise SourceError("来源关键词分页条目重复，拒绝同步可能不完整的词表")
                seen_keywords.add(key)
                keywords.append(keyword)
            return KnowledgeInput(version=version, lines=list(lines.values()), keywords=keywords,
                                  quality="keywords_only", warnings=[
                                      "仅同步了关键词，未获取完整模块知识；请导入完整知识或配置 SOURCE_DB_DSN"
                                  ])
        except ValidationError:
            raise SourceError("来源关键词字段不符合输入契约") from None
