# WorkTwin Collector 1.1.0 验收范围

macOS v1.1.0 增补：在 macOS ARM/Intel Runner 验证实际 **DMG 容器完整性、挂载 App 的 codesign --verify --deep --strict、复制 App 后代码签名、打包程序的 HTTP/SQLite 启停**，并明确记录 Gatekeeper 对 ad-hoc 签名的拒绝。该组检查不能替代 Apple Developer ID 签名、公证或用户实际首次下载双击测试；见 [安装排查](MACOS_INSTALL.md)。

所有自动数据为临时合成资料，模型为确定性测试实现或本机模拟 HTTP 供应商，不调用真实付费模型。测试运行真实 SQLite、FastAPI/uvicorn 与浏览器脚本，不以静态页面替代后端。

| 验收 | 实现与检查 |
|---|---|
| 后端回归 | 66 项 pytest（包含个人配置、密钥持久保存、失败回滚、管理员权限、员工撤销和草稿准入的新回归）：采集/适配器、证据、更新/养护、任务恢复、来源权限、问答范围、共享与撤销、预算、链接和 Markdown 安全 |
| 合成回答评测 | 8 项：知识引用、无依据拒答、撤权、隐私隔离；结果不代表生产准确率 |
| 批量与 HTTP | 80 份合成资料增量/幂等扫描；独立 HTTP 网关模拟供应商调用 |
| 浏览器主流程 | 授权目录、编辑知识、创建分身、勾选授权、来源撤销 |
| 浏览器知识更新 | 新旧对照、接受/忽略、版本和失效提案 |
| 浏览器共享闭环 | UI 连接企业服务、Markdown 显示、发布/创建链接、接收者问答、本人进程退出后继续问答、刷新保留访问、点击引用、远程撤销后拒绝访问 |
| 浏览器设置 | 个人真实模拟模型测试、失败保留配置、未保存保护、分身一并保存、小窗口设置、管理员模型与员工 Token |
| 分发 | wheel/sdist/source；macOS arm64/x86_64、Windows x64 打包启动、知识写入、Markdown 渲染与正常退出 smoke |

发布必须通过仓库 `.github/workflows/worktwin-build.yml` 的 Python、四个浏览器脚本和三个平台打包启动任务。成功后 main 的工作流生成 Release 与 SHA256 文件。运行状态以对应 GitHub Actions 记录为准；这里记录验收定义，不预先宣称未运行任务成功。

```bash
python -m pip install -e '.[test]' build playwright
./scripts/verify_release.sh
python -m playwright install --with-deps chromium
PYTHONPATH=. python scripts/ui_acceptance.py
PYTHONPATH=. python scripts/ui_proposals_acceptance.py
PYTHONPATH=. python scripts/ui_sharing_acceptance.py
PYTHONPATH=. python scripts/ui_settings_acceptance.py
```

没有验收：真实付费模型提炼/养护质量、真实员工资料的准确率和归因率、组织 SSO、DLP、物理 Mac/Windows 长期运行/睡眠/唤醒与系统弹窗、已签名/公证安装包、自动升级。打包程序 smoke 证明其能启动 API、写入知识、渲染 Markdown 并正常退出，不证明上述系统行为。

## 1.1 本地运行记录（2026-10-09）

66 项后端回归通过，8/8 合成回答断言通过，80 份增量扫描与真实 HTTP 验收通过。四个浏览器脚本已在 Chrome Headless Shell 中实际执行并通过，未发现 JavaScript 错误；受本地进程限制，浏览器用单进程模式，主流程/更新流通过本地 HTTP 桥接，设置/接收者页面同时验证实际 origin、刷新和 sessionStorage。Python wheel/sdist 构建通过。

GitHub 上传被自动审批拦截，尚未运行这一提交的 CI 原生安装包构建；上述记录不宣称 macOS/Windows 新安装包、物理系统钥匙串提示或 Apple 公证已验证。获得上传授权后，继续执行 CI 四组浏览器验收和三个平台的原生打包检查。
