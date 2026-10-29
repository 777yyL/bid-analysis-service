import React from 'react';
import ReactDOM from 'react-dom/client';
import {ConfigProvider, App} from 'antd';
import zhCN from 'antd/locale/zh_CN';
import Workbench from './workbench';
import './style.css';
ReactDOM.createRoot(document.getElementById('root')!).render(
  <React.StrictMode><ConfigProvider locale={zhCN} theme={{token: {colorPrimary: '#087d7c', colorInfo: '#087d7c', colorSuccess: '#16806e', colorWarning: '#ce8529', borderRadius: 5, fontFamily: '"Microsoft YaHei", "PingFang SC", sans-serif', colorText: '#203648', colorBorder: '#dbe3e8', colorBgLayout: '#f2f5f7'}, components: {Table: {headerBg: '#f4f7f9', headerColor: '#607484'}, Menu: {itemBorderRadius: 5}, Card: {headerFontSize: 15}}}}><App><Workbench/></App></ConfigProvider></React.StrictMode>
);
