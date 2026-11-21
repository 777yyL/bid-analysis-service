"""Build an atomic read model. Run inside analyzer: python -m scripts.refresh_presales."""
import argparse
from sqlalchemy import create_engine, text
from analyzer.config import Settings

DDL = {
    'project': 'project_key text primary key, stage text, has_demand boolean, has_software boolean, has_hardware boolean',
    'notice': 'notice_id bigint primary key, project_key text not null, title text, channel text, publish_at timestamp, province text, city text, buyer text, demand text, source_url text',
    'software': 'project_key text, item_id text, name text, method text, primary key(project_key,item_id)',
    'hardware': 'notice_id bigint, l1 text, l2 text, hits smallint, evidence text, tagged_at timestamp, primary key(notice_id,l1,l2)',
    'project_hardware': 'project_key text, l1 text, l2 text, primary key(project_key,l1,l2)',
    'winner': 'project_key text, notice_id bigint, name text, lot_id bigint, lot_no text',
    'snapshot': 'refreshed_at timestamptz not null',
}

def refresh(engine, schema):
    with engine.begin() as c:
        run=c.exec_driver_sql
        run('SET TRANSACTION ISOLATION LEVEL REPEATABLE READ')
        run("SET LOCAL TIME ZONE 'Asia/Shanghai'")
        if not run("SELECT pg_try_advisory_xact_lock(435216789)").scalar():
            raise RuntimeError('另一刷新正在运行')
        prefix=c.dialect.identifier_preparer.quote_schema(schema or 'public')
        # Optional DBA-owned normalized view for installations with different tag field names.
        adapter=run("SELECT to_regclass('public.presales_notice_hw_input') IS NOT NULL").scalar()
        hw_src='public.presales_notice_hw_input' if adapter else 'public.notice_product_tag'
        required={'notice_id','tag_l1','tag_l2','word_weak','hits','evidence','tagged_at'}
        columns=dict(c.execute(text("""SELECT column_name,data_type FROM information_schema.columns
            WHERE table_schema='public' AND table_name=:t"""),{'t':hw_src.split('.')[1]}).all())
        if not required<=columns.keys() or columns.get('word_weak')!='boolean':
            raise RuntimeError('公告硬件来源缺字段或 word_weak 不是 boolean；请按部署手册提供 public.presales_notice_hw_input，不允许用项目标签猜测公告。')
        scope=run(f"SELECT value->>'value' FROM {prefix}.sales_runtime_v1 WHERE key='active_source_revision'").scalar()
        if not scope:
            raise RuntimeError('缺当前分析源版本，保留旧快照')
        run('CREATE SCHEMA IF NOT EXISTS presales')
        for name,definition in DDL.items():
            run(f'CREATE TABLE IF NOT EXISTS presales.{name} ({definition})')
            run(f'CREATE TEMP TABLE build_{name} (LIKE presales.{name} INCLUDING INDEXES) ON COMMIT DROP')
        run(f"""INSERT INTO build_software SELECT p.project_key,i.id,i.name,m.method FROM {prefix}.sales_projects_v1 p
            JOIN {prefix}.sales_matches_v1 m ON m.project_key=p.project_key AND m.revision=p.revision
            JOIN {prefix}.sales_items_v1 i ON i.id=m.sales_item_id AND i.version=m.item_version AND i.enabled
            WHERE m.status='confirmed' AND p.status IN ('matched','needs_review')
            AND p.material->>'_source_revision'=(SELECT value->>'value' FROM {prefix}.sales_runtime_v1 WHERE key='active_source_revision')""")
        run("""INSERT INTO build_project_hardware SELECT project_key,tag_l1,tag_l2 FROM public.project_product_tag""")
        run("""INSERT INTO build_project SELECT k.project_key,v.channel,bool_or(k.d),bool_or(k.s),bool_or(k.h)
            FROM (SELECT DISTINCT project_key_name AS project_key,true d,false s,false h FROM public.notices
                  WHERE revoke_status='active' AND has_procurement_content AND nullif(btrim(procurement_content),'') IS NOT NULL
                  UNION ALL SELECT project_key,false,true,false FROM build_software
                  UNION ALL SELECT DISTINCT project_key,false,false,true FROM build_project_hardware) k
            JOIN public.mv_project v USING(project_key) GROUP BY k.project_key,v.channel""")
        source_cols=set(run("SELECT column_name FROM information_schema.columns WHERE table_schema='public' AND table_name='notices'").scalars())
        url_col=next((k for k in ("source_url","notice_url","detail_url","url") if k in source_cols),None)
        url_expr="n."+url_col if url_col else "NULL::text"
        run(f"""INSERT INTO build_notice SELECT n.notice_id,p.project_key,n.title,n.channel,n.publish_at,
            coalesce(nullif(trim(n.province_code),''),left(nullif(trim(n.region_code),''),2)||'0000'),
            nullif(trim(n.region_code),''),nullif(btrim(n.buyer),''),
            CASE WHEN n.has_procurement_content THEN nullif(btrim(n.procurement_content),'') END,{url_expr}
            FROM build_project p JOIN public.notices n ON n.project_key_name=p.project_key WHERE n.revoke_status='active'""")
        run(f"""INSERT INTO build_hardware SELECT DISTINCT h.notice_id,h.tag_l1,h.tag_l2,h.hits,h.evidence,h.tagged_at FROM {hw_src} h
            JOIN build_notice n USING(notice_id) JOIN build_project_hardware t ON t.project_key=n.project_key
            AND t.l1=h.tag_l1 AND t.l2=h.tag_l2 WHERE h.word_weak IS FALSE""")
        valid="NOT IN ('','/','-','无','暂无','详见附件','详见公告')"
        run(f"""INSERT INTO build_winner SELECT DISTINCT b.project_key,b.notice_id,coalesce(w.name,btrim(n.winner)),w.lot_id,w.lot_no
            FROM build_notice b JOIN public.notices n USING(notice_id)
            LEFT JOIN LATERAL (SELECT l.lot_id,l.lot_no,btrim(l.winner) AS name FROM public.lots l
                WHERE l.notice_id=b.notice_id AND btrim(l.winner) {valid}) w ON true
            WHERE b.channel='结果公告' AND concat_ws(' ',b.title,n.notice_type) !~ '候选|废标|流标|终止'
            AND coalesce(w.name,btrim(n.winner)) {valid}""")
        for name in ('software','project_hardware'):
            run(f'DELETE FROM build_{name} t WHERE NOT EXISTS (SELECT FROM build_project p WHERE p.project_key=t.project_key)')
        run('INSERT INTO build_snapshot VALUES(clock_timestamp())')
        counts={}
        for name in DDL:
            run(f'DELETE FROM presales.{name}')
            counts[name]=run(f'INSERT INTO presales.{name} SELECT * FROM build_{name}').rowcount
        for table,cols in [('notice','province,publish_at'),('notice','city,publish_at'),('notice','project_key,publish_at'),('notice','publish_at'),('winner','project_key'),('hardware','l1,l2,notice_id')]:
            run(f'CREATE INDEX IF NOT EXISTS ix_ps_{table}_{cols.replace(",","_")} ON presales.{table}({cols})')
        for name in DDL:
            run(f'ANALYZE presales.{name}')
        return counts

if __name__=='__main__':
    argparse.ArgumentParser(description=__doc__).parse_args()
    cfg=Settings()
    if cfg.source_mode!='shared':
        raise SystemExit('仅支持 shared 模式')
    engine=create_engine(cfg.database_url)
    try:
        print(refresh(engine,cfg.analysis_schema))
    finally:
        engine.dispose()
