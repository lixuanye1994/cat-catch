---
name: commit
description: 按 Conventional Commits（约定式提交）规范分析改动、生成提交信息并执行 git commit。当用户要求“提交代码 / 提交一下 / commit / 写提交信息 / 生成 commit message”时使用。
---

# 规范提交

本项目所有提交遵循 **Conventional Commits 1.0**。目标：提交历史可读、可自动生成变更日志、便于回溯。

## 一、提交信息格式

```
<type>(<scope>): <subject>

<body 可选：说明「为什么」，不重复「做了什么」>

<footer 可选：BREAKING CHANGE / 关联 issue>
```

### type（必填，从下表选一个）

| type | 用途 |
|---|---|
| `feat` | 新功能 |
| `fix` | 修复 bug |
| `refactor` | 重构，不改变外部行为 |
| `perf` | 性能优化 |
| `docs` | 仅文档（README、注释） |
| `style` | 格式调整，不影响逻辑（空格、缩进等） |
| `test` | 新增或修改测试 |
| `build` | 构建系统、依赖（requirements.txt 等） |
| `ci` | CI 配置 |
| `chore` | 杂项、不影响源码的琐碎改动 |
| `revert` | 回滚历史提交 |

### scope（可选，本项目建议使用）

- `m3u8`：M3U8 解析、下载页
- `sniff`：通用嗅探（browser / sniff-view）
- `bilibili`：B 站适配器
- `download`：下载器、任务/台账管理
- `config`：配置与启动项
- `docs`：README 等文档

拿不准影响范围时**省略 scope**，不要硬凑。

### subject（必填）

- 一句话说清「做了什么」，祈使语气；中文描述即可
- 结尾**不加句号**，建议控制在 50 字符以内
- 首字母无需大写（中文无此问题）

### body / footer

- 只在改动有非显而易见的动机、取舍时写 body，说明 **why**
- 破坏性变更必须在 footer 标注：`BREAKING CHANGE: <影响与迁移说明>`

## 二、执行步骤

1. 先看清楚改了什么：
   - `git status`
   - `git diff`（未暂存）和 `git diff --staged`（已暂存）
   - 必要时 `git log --oneline -10` 对齐已有风格
2. **判断改动是否聚焦于一件事**。若包含多个互不相关的改动（例如同时加功能和改无关格式），先建议用户分文件 `git add`、拆成多次提交，不要打包成一个。
3. 据此确定 `type`、`scope`，写 `subject`，需要时补 `body`。
4. **安全检查**：确认提交中不含密钥、凭据、个人数据。本项目 `data/`（含 B 站 SESSDATA、config.json）已在 `.gitignore` 中忽略；若发现敏感文件被暂存，停止并告知用户。
5. 把拟定的完整提交信息**展示给用户确认**，同意后再执行 `git commit`（多行信息用多个 `-m` 或 heredoc）。
6. **不要自动 `git push`**，除非用户明确要求。
7. 提交后用 `git status` / `git log --oneline -1` 确认结果并简要汇报。

## 三、示例

```
feat(m3u8): 新增电影/电视剧/动漫命名模板，支持信息自动解析
fix(sniff): 修复刷新页面后无法停止运行中的嗅探任务
refactor(sniff): 移除模拟手机 UA 与无头模式选项
feat: 静态资源响应增加 no-cache，避免浏览器缓存旧文件
docs: 更新 README，补充命名模板与启动说明
build: 升级 httpx 与 playwright 依赖
```

含破坏性变更：

```
refactor(download): 统一各资源类型的输出后缀

BREAKING CHANGE: mpd 下载输出由 .mkv 改为 .mp4，历史脚本中的路径判断需更新
```
