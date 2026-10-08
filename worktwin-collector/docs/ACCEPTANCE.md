# WorkTwin Collector 1.0 验收范围

所有自动数据为临时合成资料，模型为确定性测试实现或本机模拟 HTTP 供应商，不调用真实付费模型。测试运行真实 SQLite、FastAPI/uvicorn 与浏览器脚本，不以静态页面替代后端。

| 验收 | 实现与检查 |
|---|---|
| 后端回归 | 61 项 pytest：采集/适配器、证据、更新/养护、任务恢复、来源权限、问答范围、共享与撤销、预算、链接和 Markdown 安全 |
| 合成回答评测 | 8 项：知识引用、无依据拒答、撤权、隐私隔离；结果不代表生产准确率 |
| 批量与 HTTP | 80 份合成资料增量/幂等扫描；独立 HTTP 网关模拟供应商调用 |
| 浏览器主流程 | 授权目录、编辑知识、创建分身、勾选授权、来源撤销 |
| 浏览器知识更新 | 新旧对照、接受/忽略、版本和失效提案 |
| 浏览器共享闭环 | UI 连接企业服务、Markdown 显示、发布/创建链接、接收者问答、本人进程退出后继续问答、远程撤销后拒绝访问 |
| 分发 | wheel/sdist/source；macOS arm64/x86_64、Windows x64 打包启动、知识写入、Markdown 渲染与正常退出 smoke |

发布必须通过仓库 `.github/workflows/worktwin-build.yml` 的 Python、三个浏览器脚本和三个平台打包启动任务。成功后 main 的工作流生成 Release 与 SHA256 文件。运行状态以对应 GitHub Actions 记录为准；这里记录验收定义，不预先宣称未运行任务成功。

```bash
python -m pip install -e '.[test]' build playwright
./scripts/verify_release.sh
python -m playwright install --with-deps chromium
PYTHONPATH=. python scripts/ui_acceptance.py
PYTHONPATH=. python scripts/ui_proposals_acceptance.py
PYTHONPATH=. python scripts/ui_sharing_acceptance.py
```

没有验收：真实付费模型提炼/养护质量、真实员工资料的准确率和归因率、组织 SSO、DLP、物理 Mac/Windows 长期运行/睡眠/唤醒与系统弹窗、已签名/公证安装包、自动升级。打包程序 smoke 证明其能启动 API、写入知识、渲染 Markdown 并正常退出，不证明上述系统行为。
