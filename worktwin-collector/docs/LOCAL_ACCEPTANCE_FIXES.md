# 1.3.1 本机验收修复

## 测试预审

原 MV3 验收只包含顶层页面按钮，不足以证明弹窗内 iframe 能采集；增加真实普通弹窗、动态同站点 iframe、srcdoc、Shadow DOM、敏感输入、跨域 iframe 及同文档停止重启场景。
原 UI 测试不覆盖 15 秒定时重绘；增加保留输入/DOM/导航节点、显式刷新、后台更新后的过期点击与保存冲突。来源测试从任意路径输入改为“自动检测 + 高级路径 + 实际格式核验”。保留原文件夹授权用例和旧 API 客户端兼容。

## 知识不可用的诊断边界

截图明确显示 scope=unknown 的提示；旧版 scoped_knowledge_v1 迁移会设置 unknown/uncertain/review_hold，autonomous_legacy_scoping_v1 只处理特定 source: 前缀与已允许 AI 的单来源记录。不能仅凭截图断言是某个迁移分支造成，更不能批量改成 confirmed。
应在用户本机只读核对 scope/quality/status/review_hold/needs_review、有效引用、来源 allow_ai 与处理任务状态，再确定按来源重整或修复迁移的范围。本次不更改该数据政策。

## 验收记录

本地回归和 CI 结果随提交记录更新；浏览器及原生包必须以当前提交的 CI 为准。Apple 公证仍不执行。
