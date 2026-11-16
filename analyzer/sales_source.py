"""Read project metadata and raw texts; the main service is strictly read-only."""

import json
import math
from contextlib import contextmanager
from pathlib import Path, PureWindowsPath

import psycopg
from psycopg.rows import dict_row

from analyzer.project_selection import ANALYSIS_STAGES, select_project
from analyzer.schemas import NoticeInput
from analyzer.source import SourceError, _plain

EARLY_PHASES = sorted(ANALYSIS_STAGES)
MAX_NOTICES = 25000


def parse_vector(value):
    try:
        values = json.loads(value) if isinstance(value, str) else value
        if not isinstance(values, (list, tuple)) or not values:
            raise ValueError()
        if any(isinstance(v, bool) or not isinstance(v, (int, float)) for v in values):
            raise ValueError()
        result = [float(v) for v in values]
        if not all(math.isfinite(v) for v in result) or not any(result):
            raise ValueError()
        return result
    except (ValueError, TypeError, OverflowError):
        raise SourceError("来源分片向量为空、格式错误或包含非有限值") from None


def _serialize(row):
    return {key: value.isoformat() if hasattr(value, "isoformat") else value for key, value in row.items()}


class SalesSource:
    def __init__(self, settings):
        self.settings = settings

    @contextmanager
    def _connection(self):
        dsn = self.settings.get("source_dsn")
        if not dsn:
            raise SourceError("请先在系统配置中保存主服务数据库连接")
        try:
            with psycopg.connect(dsn, row_factory=dict_row, connect_timeout=10,
                    options="-c default_transaction_read_only=on -c statement_timeout=30000") as conn:
                conn.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY")
                yield conn
        except psycopg.Error:
            raise SourceError("主服务数据库只读查询失败，请检查连接、权限及表结构", retryable=True) from None

    def profile(self):
        with self._connection() as conn:
            phases = conn.execute("SELECT channel, count(*) AS count FROM public.mv_project GROUP BY channel").fetchall()
        return {'phases': [dict(row) for row in phases], 'storage': 'local',
                'collect_attachments': self.settings.get('collect_attachments', True)}

    def list_projects(self, after_key="", limit=20, allow_later=False):
        if not isinstance(after_key, str) or isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 500:
            raise SourceError("项目游标或批次数量无效")
        query = "SELECT project_key, channel FROM public.mv_project WHERE project_key > %s"
        params = [after_key]
        if not allow_later:
            query += " AND channel = ANY(%s)"
            params.append(EARLY_PHASES)
        else:
            query += " AND channel IS NOT NULL"
        query += " ORDER BY project_key LIMIT %s"
        params.append(limit)
        with self._connection() as conn:
            return [dict(row) for row in conn.execute(query, tuple(params)).fetchall()]

    def _notice_columns(self, conn):
        return {row["column_name"] for row in conn.execute("""SELECT column_name
            FROM information_schema.columns WHERE table_schema = 'public' AND table_name = 'notices'""").fetchall()}

    def _missing_project(self, project_key):
        raise SourceError('未找到该项目，请确认项目标识及数据同步范围')

    def project_material(self, project_key, allow_later=False):
        if not isinstance(project_key, str) or not project_key.strip():
            raise SourceError("项目键不能为空")
        with self._connection() as conn:
            projects = conn.execute("SELECT project_key, channel FROM public.mv_project WHERE project_key = %s",
                                    (project_key,)).fetchall()
            if not projects:
                return self._missing_project(project_key)
            channel = projects[0]["channel"]
            base = {"project_key": project_key, "channel": channel, "title": project_key,
                    "main_notice_id": None, "correction_notice_ids": [], "selection_reason": "",
                    "warnings": [], "chunks": [], "notices": [], "units": [],
                    "material_storage": "local", "collect_attachments": self.settings.get('collect_attachments', True)}
            if not channel or not allow_later and channel not in ANALYSIS_STAGES:
                return {**base, "status": "stage_filtered"}
            columns = self._notice_columns(conn)
            required = {"notice_id", "title", "channel", "project_key_name", "revoke_status"}
            if not required <= columns:
                raise SourceError("主服务 notices 缺少项目、公告阶段或撤销状态字段")
            selected = [name for name in ("notice_id", "title", "channel", "buyer", "publish_at", "revoke_status",
                "round_key", "lot_key", "related_notice_id", "content_path") if name in columns]
            metadata = conn.execute("SELECT " + ", ".join(selected) +
                " FROM public.notices WHERE project_key_name = %s ORDER BY notice_id LIMIT %s",
                (project_key, MAX_NOTICES + 1)).fetchall()
            if len(metadata) > MAX_NOTICES:
                raise SourceError("单项目公告超过读取上限，未截断分析，请核验项目关联")
            ids = [row["notice_id"] for row in metadata if row["channel"] in ANALYSIS_STAGES
                   and row["revoke_status"] == "active"]
            attachments = []
            if ids and self.settings.get('collect_attachments', True):
                attachments = conn.execute("""SELECT att_id, notice_id, filename, text_path, local_path
                    FROM public.attachments WHERE notice_id = ANY(%s) ORDER BY att_id""", (ids,)).fetchall()
        text, warnings = {}, []
        def read(path, label):
            if not path:
                warnings.append(f'{label}没有解析文本路径')
                return ''
            try:
                return self._read_text(path)
            except SourceError as error:
                warnings.append(f'{label}：{error}')
                return ''
        for row in metadata:
            if row['channel'] == '更正公告' and row['revoke_status'] == 'active':
                text[row['notice_id']] = read(row.get('content_path'), f"更正公告{row['notice_id']}")
        attachment_text = {}
        if self.settings.get('collect_attachments', True):
            for att in attachments:
                if any(n['notice_id'] == att['notice_id'] and n['channel'] == '更正公告' for n in metadata):
                    attachment_text[att['att_id']] = read(att.get('text_path'), f"附件{att['att_id']}")
        notices = []
        for row in metadata:
            values = {key: row[key] for key in ("round_key", "lot_key", "related_notice_id") if row.get(key) is not None}
            try:
                notices.append(NoticeInput(notice_id=row["notice_id"], title=row["title"],
                    notice_type=row["channel"], project_key=project_key, project_channel=channel,
                    buyer=row.get("buyer") or "", revoke_status=row["revoke_status"],
                    publish_at=str(row["publish_at"]) if row.get("publish_at") else None,
                    body="\n".join([text.get(row['notice_id'], ''), *[attachment_text.get(a['att_id'], '') for a in attachments if a['notice_id'] == row['notice_id']]]), **values))
            except ValueError:
                raise SourceError("主服务公告元数据格式无效") from None
        plans = select_project(notices)
        chosen = set()
        mains, corrections, reasons, units = [], [], [], []
        for plan in plans:
            warnings.extend(plan["warnings"])
            if not plan["main"]:
                continue
            main = plan["main"]
            mains.append(main.notice_id)
            correction_ids = [n.notice_id for n in plan["corrections"]]
            corrections.extend(correction_ids)
            chosen.update([main.notice_id, *correction_ids])
            reasons.append(plan["selection_reason"])
            units.append({"unit_key": plan["unit_key"], "main_notice_id": main.notice_id,
                          "correction_notice_ids": correction_ids, "round_key": plan["round_key"], "lot_key": plan["lot_key"]})
        documents = []
        for row in metadata:
            if row['notice_id'] not in chosen:
                continue
            content = text.get(row['notice_id'])
            if content is None:
                content = read(row.get('content_path'), f"公告{row['notice_id']}")
            if content:
                documents.append({'notice_id': row['notice_id'], 'source_type': 'notice',
                    'attachment_id': None, 'chunk_title': row['title'], 'content': content})
        for att in attachments:
            if att['notice_id'] not in chosen:
                continue
            content = attachment_text.get(att['att_id'])
            if content is None:
                content = read(att.get('text_path'), f"附件{att['att_id']}")
            if content:
                documents.append({'notice_id': att['notice_id'], 'source_type': 'attachment',
                    'attachment_id': att['att_id'], 'chunk_title': att['filename'], 'content': content})
        if len(mains) > 1:
            warnings.append("项目存在多个明确采购单元；返回各单元主公告与更正，不混淆来源")
        title = next((n.title for n in notices if mains and n.notice_id == mains[0]), project_key)
        return {**base, "title": title, "main_notice_id": mains[0] if mains else None,
                "main_notice_ids": mains, "correction_notice_ids": list(dict.fromkeys(corrections)),
                "selection_reason": "；".join(dict.fromkeys(reasons)), "warnings": list(dict.fromkeys(warnings)),
                "units": units, "documents": documents, "chunks": [], "notices": [_serialize(row) for row in metadata],
                "attachments": [_serialize(a) for a in attachments if a['notice_id'] in chosen],
                "status": "ready" if documents else "missing_material" if mains else "missing_primary"}

    def _read_text(self, relative):
        try:
            target = self._resolve_file(relative)
            if target.stat().st_size > 20 * 1024 * 1024:
                raise SourceError('原文超过20MB，未截断向量化')
            with target.open('rb') as handle:
                raw = handle.read(20 * 1024 * 1024 + 1)
            if len(raw) > 20 * 1024 * 1024:
                raise SourceError('原文超过20MB，未截断向量化')
            return _plain(raw.decode('utf-8-sig').replace('\x00', ''))
        except (OSError, UnicodeError, ValueError):
            raise SourceError('正文或附件解析文本不可读，请检查路径、挂载及UTF-8编码') from None

    def _resolve_file(self, relative):
        root_value = self.settings.get('source_file_root')
        if not root_value:
            raise SourceError('请在系统配置中设置主服务文本根目录，并挂载原文目录')
        try:
            root = Path(root_value).resolve(strict=True)
            if not relative or Path(relative).is_absolute() or PureWindowsPath(relative).drive or '\\' in relative:
                raise ValueError()
            target = (root / relative).resolve(strict=True)
            if not target.is_relative_to(root) or not target.is_file():
                raise ValueError()
            return target
        except (OSError, UnicodeError, ValueError):
            raise SourceError('正文或附件解析文本不可读，请检查路径、挂载及UTF-8编码') from None
