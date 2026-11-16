from analyzer.sales_categories import category_matches, category_path


def test_explicit_category_paths_and_parent_filters():
    assert category_path('视频应用 / 视频监控')==['视频应用','视频监控']
    assert category_matches(['视频应用>视频监控'],'视频应用')
    assert category_matches(['视频应用>视频监控'],'视频应用/视频监控')
    assert not category_matches(['视频应用/视频监控'],'视频应用/设备网络管理')
    assert not category_matches(['视频应用','视频监控'],'视频应用/视频监控')
