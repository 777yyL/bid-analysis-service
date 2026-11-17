from datetime import date, datetime, timezone

from analyzer.sales_reporting import classify_demand, classify_progress, day_bounds, summarize


def notice(nid, channel='采购公告', flag=True, text='采购访客管理软件', **extra):
    return dict(notice_id=nid, project_key_name='p', title='项目', channel=channel,
                revoke_status='active', publish_at=f'2026-11-{nid:02} 09:00:00',
                has_procurement_content=flag, has_content=bool(text), procurement_content=text, **extra)


def test_demand_selects_current_primary_instead_of_any_old_intent():
    assert classify_demand('p', [notice(1, '采购意向'), notice(2, flag=False)])['demand'] == 'no'
    assert classify_demand('p', [notice(1), notice(2, flag=None)])['demand'] == 'pending'
    assert classify_demand('p', [notice(1), notice(2, text='')])['demand'] == 'pending'


def test_lots_missing_primary_and_revoked_are_not_silent_no_matches():
    assert classify_demand('p', [notice(1, '更正公告')])['demand'] == 'missing_primary'
    a, b = notice(1, lot_key='a'), notice(2, lot_key='b', flag=None)
    assert classify_demand('p', [a, b])['demand'] == 'pending'
    a['revoke_status'] = 'revoked'
    assert classify_demand('p', [a])['demand'] == 'missing_primary'


def test_latest_round_overrides_older_procurement_notice():
    rows = [notice(1, round_key='r1'), notice(2, '采购意向', flag=False, round_key='r2')]
    assert classify_demand('p', rows)['demand'] == 'no'


def test_progress_is_independent_of_old_confirmed_tags():
    assert classify_progress('yes', 'matched', 'running') == 'running'
    assert classify_progress('yes', None, 'failed') == 'failed'
    assert classify_progress('yes', None, None) == 'pending'
    assert classify_progress('pending', None, None) == 'awaiting'
    assert classify_progress('yes', 'needs_review', 'completed') == 'review_pending'
    assert classify_progress('yes', 'no_match', 'completed') == 'completed'


def test_counts_partition_demand_and_do_not_count_waiting_as_no_match():
    rows = [dict(project_key='a', notices=2, is_new=True, demand='yes', progress='completed', hit=True),
            dict(project_key='b', notices=1, is_new=False, demand='yes', progress='pending', hit=False),
            dict(project_key='c', notices=1, is_new=True, demand='pending', progress='awaiting', hit=False)]
    m = summarize(rows)
    assert (m['notices'], m['all'], m['new'], m['existing']) == (4, 3, 2, 1)
    assert (m['matched'], m['no_match'], m['pending']) == (1, 0, 1)
    assert m['eligible'] == m['demand'] + m['no_demand'] + m['unextracted'] + m['missing_primary']


def test_beijing_day_bounds_use_half_open_interval():
    start, end = day_bounds(date(2026, 11, 16), 'timestamp with time zone', 'Asia/Shanghai')
    assert start.astimezone(timezone.utc) == datetime(2026, 11, 15, 16, tzinfo=timezone.utc)
    assert end.astimezone(timezone.utc) == datetime(2026, 11, 16, 16, tzinfo=timezone.utc)
    start, _ = day_bounds(date(2026, 11, 16), 'timestamp without time zone', 'Asia/Shanghai')
    assert start == datetime(2026, 11, 16)


def test_current_tags_and_latest_job_are_separate_and_filters_paginate(tmp_path):
    from analyzer.config import Settings
    from analyzer.store import Store, digest
    from analyzer.sales_service import SalesService
    from analyzer.sales_store import SalesProject, SalesMatch, SalesJob
    from analyzer.sales_reporting import DailyReports
    cfg=Settings(_env_file=None,database_url=f"sqlite:///{tmp_path/'data.db'}",sales_secret_key_file=str(tmp_path/'key'),worker_enabled=False)
    store=Store(cfg.database_url)
    store.initialize(sales_only=True)
    service=SalesService(store,cfg)
    service.save_settings({'retrieval_backend':'maas'})
    service.import_items([{'id':'visitor','name':'访客管理','description':'预约审批','categories':['人员通行'],'products':['园区']}])
    item=service.items()['items'][0]
    with store.session.begin() as s:
        s.add(SalesProject(project_key='p',title='访客',channel='采购公告',status='matched',revision='v1',
            material={'_source_revision':digest('')},updated_at=1))
        s.add(SalesMatch(id='current',project_key='p',sales_item_id='visitor',revision='v1',item_version=item['version'],status='confirmed',method='model',score=.9,reason='依据',evidence=[]))
        s.add(SalesMatch(id='stale',project_key='p',sales_item_id='visitor',revision='v0',item_version=item['version'],status='confirmed',method='model',score=.99,reason='旧结果',evidence=[]))
    service.enqueue('analyze',{'project_key':'p','force':True})
    rows=[{'project_key':'p','demand':'yes','status':None,'hit':False,'matches':[],'recorded':False,
        'title':'访客','is_new':True,'notices':1,'publish_at':'2026-11-16','channel':'采购公告'}]
    reports=DailyReports(service)
    reports._results(rows,service.settings())
    assert rows[0]['progress']=='pending' and rows[0]['hit']
    assert len(rows[0]['matches'])==1 and rows[0]['recorded']
    reports.load=lambda day:{'items':rows,'generated_at':1}
    assert reports.projects(date(2026,11,16),slice='matched',category='人员通行')['total']==1
    assert reports.projects(date(2026,11,16),slice='matched',min_score=.9)['total']==0
    assert reports.projects(date(2026,11,16),slice='no_match')['total']==0
    service.set_enabled('visitor',False)
    other=[{**rows[0],'hit':False,'matches':[]}]
    reports._results(other,service.settings())
    assert not other[0]['hit']
    store.close()
