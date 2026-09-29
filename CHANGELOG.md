# Changelog

本 Skill 遵循 [语义化版本](https://semver.org/lang/zh-CN/)：主版本号 = 不兼容的
结构/工作流变更；次版本号 = 向后兼容的新功能（新规则、新脚本参数）；修订号 =
文档修正与缺陷修复。每次变更在下方新增条目，禁止改写历史条目。

## [1.0.0] - 2026-09-22

### Added
- 初始版本：SKILL.md 主控入口（triggers / knowledge / workflow / 异常处理 /
  质量检查清单 / 硬性约束）。
- `scripts/extract_pdf.py`：PyMuPDF 论文解析（元数据、分页文本、候选代码
  区块启发式评分、开源链接双通道扫描），显式退出码处理损坏/加密/扫描件等
  边界异常。
- `scripts/generate_pdf.py`：Markdown → PDF 渲染，中文字体注入 + Pygments
  语法高亮，排版参数全部来自 `assets/config.json`，渲染失败自动降级输出
  HTML。
- `scripts/check_env.py`：Python/依赖/中文字体三段式环境自检。
- `references/`：领域架构抽象规则（CV/导航/机械）、代码提取规则、报告组装
  模板。
- `assets/`：config.json 排版配置、report-template.html 注入式模板、fonts/
  字体目录。

### 记录模板（新增条目时复制此格式）

```
## [X.Y.Z] - YYYY-MM-DD

### Added（新增功能）
- ...

### Changed（调整，需说明迁移方式）
- ...

### Fixed（缺陷修复）
- ...

### Removed（移除，需说明替代方案）
- ...
```
