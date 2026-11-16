from analyzer.procurement_extraction import extract_requirements


def test_extract_demand_before_embedding_excludes_process_and_admin_fields():
    text = ('一、项目基本情况\n预算金额：100万元\n二、采购需求\n'
            '访客管理提供预约、审批、登记和签离功能。\n'
            '三、供应商资格要求\n投标人须提供视频监控项目业绩。\n'
            '四、开标方式\n采用视频会议远程开标。\n联系人：张某，电话123456。')
    result = extract_requirements(text)
    kept = '\n'.join(s['text'] for s in result['segments'])
    assert '访客管理' in kept
    assert all(word not in kept for word in ('视频监控', '视频会议', '联系人', '预算金额'))
    assert result['demand_chars'] > 0 and result['removed_chars'] > 0
    for segment in result['segments']:
        assert text[segment['start']:segment['end']] == segment['text']


def test_demand_table_and_nested_heading_are_not_lost():
    text = ('## 一、采购需求\n|货物名称|数量|\n|访客管理|1套|\n'
            '1. 功能模块\n支持预约审批、通行授权。\n'
            '## 二、评标办法\n按平台业绩证明评分。')
    result = extract_requirements(text)
    kept = '\n'.join(s['text'] for s in result['segments'])
    assert '|访客管理|1套|' in kept and '支持预约审批' in kept
    assert '评分' not in kept


def test_headingless_actual_purchase_is_extracted():
    text = '本次采购访客管理软件，实现预约和授权。采购文件通过交易平台领取。'
    kept = '\n'.join(s['text'] for s in extract_requirements(text)['segments'])
    assert '本次采购访客管理软件' in kept and '领取' not in kept


def test_process_only_text_does_not_fall_back_to_full_document():
    text = '供应商须自备电脑参加远程开标。投标文件通过电子交易平台上传。'
    assert extract_requirements(text)['segments'] == []


def test_attachment_reference_is_reported_without_embedding_a_pointer():
    result = extract_requirements('采购需求：详见附件。')
    assert result['segments'] == [] and result['warnings']


def test_unclear_capability_is_retained_as_uncertain():
    result = extract_requirements('支持预约审批和通行授权。')
    assert result['segments'] and all(s['role']=='uncertain' for s in result['segments'])


def test_mixed_process_and_demand_cannot_be_promoted_to_definite_demand():
    result = extract_requirements('采购内容与投标文件上传要求：视频会议相关事项。')
    assert result['segments'] and all(s['role']=='uncertain' for s in result['segments'])


def test_correction_cancellation_is_kept_with_original_offsets():
    text = '采购需求更正：取消视频监控模块，保留访客管理。投标截止时间延期。'
    result = extract_requirements(text, correction=True)
    assert any('取消视频监控' in s['text'] for s in result['segments'])
    assert not any('截止时间' in s['text'] for s in result['segments'])


def test_actual_software_capability_is_not_mistaken_for_supplier_experience():
    result = extract_requirements('采购需求：建设业务管理系统。\n支持项目业绩管理、合同登记功能。')
    assert any('支持项目业绩管理' in s['text'] for s in result['segments'])


def test_removed_clauses_never_reappear_when_segments_are_grouped():
    text = '采购内容：访客预约系统。联系人：张某。采购内容：增加访客签离功能。'
    result = extract_requirements(text)
    assert len(result['segments']) >= 2
    assert not any('联系人' in s['text'] for s in result['segments'])


def test_document_acquisition_section_does_not_leak_srm_platform_instructions():
    text = ('### **2.采购范围及相关要求**\n'
            '2.1 采购范围：本项目主要采购电客车继电器备件。\n'
            '### **4.采购文件的获取**\n'
            '4.2.1供应商登陆阳光采购平台（SRM）网站注册用户，获得账号及密码，'
            '供应商可在阳光采购平台（SRM）网站“平台首页-服务中心”中下载《SRM云平台操作手册》；\n'
            '具体操作请在阳光采购平台服务中心中下载《操作手册》，如有问题请联系平台服务热线400-116-0808。')
    result = extract_requirements(text)
    kept = '\n'.join(s['text'] for s in result['segments'])
    assert '电客车继电器备件' in kept
    assert all(word not in kept for word in ('SRM', '操作手册', '平台注册', '服务热线', '账号及密码'))


def test_headingless_supplier_platform_registration_is_process():
    text = '供应商登录阳光采购平台注册账号，并在服务中心下载操作手册。'
    assert extract_requirements(text)['segments'] == []


def test_actual_purchase_of_srm_platform_and_registration_features_survives():
    text = ('采购需求：建设SRM供应商管理平台。\n'
            '支持供应商账号注册。\n'
            '支持采购文件获取、投标文件管理功能。\n'
            '提供操作手册在线查阅功能。')
    kept = '\n'.join(s['text'] for s in extract_requirements(text)['segments'])
    assert all(word in kept for word in ('建设SRM', '账号注册', '投标文件管理功能', '操作手册在线查阅功能'))


def test_explicit_real_purchase_in_process_section_is_still_retained_uncertain():
    text = '四、采购文件获取\n本次采购访客管理软件，实现预约审批。'
    segments = extract_requirements(text)['segments']
    assert any('采购访客管理软件' in s['text'] and s['role']=='uncertain' for s in segments)


def test_multi_level_numbered_admin_fields_do_not_become_requirements():
    text = '1.1 采购项目名称：车辆备件采购。\n1.2 采购人：某公司。\n1.3 采购项目资金落实情况：已落实。\n1.4 采购项目概况：本次采购车辆继电器备件。'
    kept = '\n'.join(s['text'] for s in extract_requirements(text)['segments'])
    assert '本次采购车辆继电器备件' in kept
    assert all(word not in kept for word in ('采购项目名称', '采购人：', '已落实'))


def test_participation_in_this_procurement_is_not_a_purchase_statement():
    text = '三、供应商资格要求\n3.3 单位负责人为同一人或者存在控股、管理关系的不同单位，不得同时参加本项目采购。'
    assert extract_requirements(text)['segments'] == []


def test_flattened_sections_still_exclude_document_acquisition():
    text = ('采购公告  ## **2.采购范围**  2.1 采购范围：本次采购电客车继电器备件。  '
            '## **4.采购文件的获取**  4.2.1供应商登陆阳光采购平台（SRM）网站注册用户，'
            '在服务中心下载操作手册。  4.2.2获得账号及密码后申请成为合作伙伴。')
    kept = '\n'.join(s['text'] for s in extract_requirements(text)['segments'])
    assert '电客车继电器备件' in kept
    assert all(word not in kept for word in ('SRM', '操作手册', '合作伙伴', '账号及密码'))
