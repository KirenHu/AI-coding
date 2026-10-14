# 开发与验证

应用代码位于 `worktwin-collector/worktwin/`，测试在 `tests/`，验收与打包脚本在 `scripts/`。从[入门指南](GETTING_STARTED.md#从源码运行)完成源码安装后，在 `worktwin-collector` 目录执行：

```bash
python -m pip install -e '.[test]' build playwright
./scripts/verify_release.sh
python -m playwright install --with-deps chromium
PYTHONPATH=. python scripts/ui_acceptance.py
PYTHONPATH=. python scripts/ui_proposals_acceptance.py
PYTHONPATH=. python scripts/ui_sharing_acceptance.py
PYTHONPATH=. python scripts/ui_settings_acceptance.py
python -m build
```

以上脚本命令采用 POSIX shell。更多专项验收及平台结果见[验收文档](ACCEPTANCE.md)和 [1.5.9 交付记录](P0_P1_DELIVERY_1_5_9.md)。

自动验收大量使用合成资料和确定性模型，验证处理流程、HTTP、SQLite、浏览器与打包启动。真实模型效果、长期稳定性与实际费用需要另行评估；单个模型或样本通过不代表整体知识质量。

## 发布方式

日常 PR / push 验收与正式发布分开。签名验收产物不自动成为 GitHub Release；正式发布要求 main、双 macOS 架构及 `signed_release=true`。以仓库 `.github/workflows/` 中当前工作流为准。

macOS 正式交付需要 Developer ID 签名、Apple 公证、票据与 Gatekeeper 验证。Windows 当前测试产物未签名。配置与验收要求见 [macOS 签名文档](MACOS_SIGNING.md)。

## 进一步阅读

- [架构](ARCHITECTURE.md)：模块与数据流。
- [文章维护](ARTICLE_MAINTENANCE_1_5.md)：材料、编写、审校和增量处理。
- [MCP](MCP.md)：分身权限与外部读取接口。
- [浏览器持续采集](BROWSER_CONTINUOUS_CAPTURE.md)：白名单、事件和平台边界。
- [开源组件](OSS.md)：依赖、移植与许可。

[返回文档导航](README.md)
