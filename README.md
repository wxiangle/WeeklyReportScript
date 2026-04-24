# WeeklyReportScript

每周汇总 1..N 个 Git 项目的提交记录，生成飞书格式周报并发送。

## 功能

- 支持输入 1 个、2 个或 N 个仓库路径
- 支持通过 YAML 文件统一配置 webhook 和目标项目目录
- 支持使用 DeepSeek 对周报进行总结润色
- 默认统计“本周一 00:00:00 到当前时间”的提交
- 自动输出周报文本与按仓库汇总
- 使用飞书机器人 Webhook 发送飞书交互卡片周报
- 发送前强制预览，确认无误后再提交
- 支持 `--dry-run` 仅预览不发送

## 运行要求

- macOS / Linux / Windows
- Python 3.9+
- 已安装 Git，且可在命令行执行 `git`
- 安装依赖：`pip install -r requirements.txt`

## 快速开始

1. 一次性初始化（安装依赖、生成配置）

```bash
chmod +x setup.sh run_weekly.sh
./setup.sh
```

1. 准备 YAML 配置

```bash
cp config.example.yaml config.yaml
# 编辑 config.yaml，填写 webhook 和项目目录
```

1. 运行脚本（优先使用 config.yaml 中的项目目录）

```bash
python3 weekly_report.py
```

也可以使用一键脚本：

```bash
./run_weekly.sh
```

1. 也可临时传入仓库目录（会覆盖 YAML 的 projects）

```bash
python3 weekly_report.py /path/repo-a /path/repo-b /path/repo-c
```

1. 不发送，仅查看内容

```bash
python3 weekly_report.py --dry-run
```

## 参数说明

- `repos`: 仓库路径列表，支持 1..N 个（优先级最高）
- `--config`: YAML 配置文件路径，默认 `./config.yaml`
- `--webhook-url`: 飞书 webhook URL（优先于 YAML 和环境变量）
- `--title`: 飞书周报标题（优先于 YAML）
- `--since`: 开始时间，ISO 格式，例如 `2026-04-20T00:00:00`
- `--until`: 结束时间，ISO 格式
- `--dry-run`: 仅打印周报，不发送飞书
- `--yes`: 跳过发送前确认，直接发送
- `--no-ai-polish`: 关闭 DeepSeek 润色，直接发送原始周报

如果不传 `repos`，脚本按顺序读取：`config.yaml` 的 `projects` -> 交互输入。

Webhook 读取顺序：`--webhook-url` -> `config.yaml` -> `FEISHU_WEBHOOK_URL`。

DeepSeek API Key 读取顺序：`config.yaml` 的 `deepseek.api_key` -> `DEEPSEEK_API_KEY`。

## YAML 配置示例

```yaml
feishu:
  webhook_url: "https://open.feishu.cn/open-apis/bot/v2/hook/replace_me"

title: "本周工作周报"

projects:
  - "/path/to/repo-a"
  - "/path/to/repo-b"
```

## DeepSeek 配置示例

```yaml
deepseek:
  enabled: true
  api_key: ""
  base_url: "https://api.deepseek.com"
  model: "deepseek-chat"
  temperature: 0.4
```

说明：

- `enabled: true` 时会先进行 AI 润色，再进入预览确认流程
- 若 DeepSeek 调用失败，脚本会自动回退到原始周报
- `deepseek.api_key` 建议留空，优先使用环境变量 `DEEPSEEK_API_KEY`

设置环境变量（zsh 示例）：

```bash
export DEEPSEEK_API_KEY="your_deepseek_api_key"
```

## 发送前确认

脚本会先输出完整周报预览，再提示是否发送：

```text
以上为周报预览，确认发送到飞书吗？[y/N]:
```

输入 `y` 或 `yes` 才会真正发送；其他输入均取消发送。

发送到飞书时，消息将以交互卡片（interactive card）格式展示，阅读层次更清晰。

## 建议每周五执行

可以用 crontab 每周五自动执行：

```bash
crontab -e
```

```cron
0 18 * * 5 /绝对路径/run_weekly.sh --yes
```

说明：上面的时间是每周五 18:00，`--yes` 表示自动化场景跳过人工确认。

也可以让初始化脚本自动写入 crontab：

```bash
./setup.sh --install-cron
```

自定义时间表达式：

```bash
./setup.sh --install-cron --cron-schedule "0 18 * * 5"
```

## setup.sh 参数

- `--python /path/to/python`: 指定 Python 解释器
- `--install-cron`: 自动写入定时任务
- `--cron-schedule "expr"`: 指定 crontab 表达式
- `--force`: 覆盖已存在的 `config.yaml`
