#!/usr/bin/env python3
"""Generate weekly git report and send to Feishu."""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable
from urllib import error, request


@dataclass
class ScriptConfig:
    webhook_url: str = ""
    repos: list[str] | None = None
    title: str | None = None
    deepseek_enabled: bool = False
    deepseek_api_key: str = ""
    deepseek_base_url: str = "https://api.deepseek.com"
    deepseek_model: str = "deepseek-chat"
    deepseek_temperature: float = 0.4


@dataclass
class Commit:
    repo_name: str
    commit_id: str
    author: str
    commit_time: dt.datetime
    message: str


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="按周汇总多个仓库的 Git 提交，并发送飞书周报。"
    )
    parser.add_argument(
        "repos",
        nargs="*",
        help="仓库路径（支持 1..N 个）。如果不传，会进入交互输入。",
    )
    parser.add_argument(
        "--config",
        default="config.yaml",
        help="YAML 配置文件路径，默认 ./config.yaml。",
    )
    parser.add_argument(
        "--webhook-url",
        default="",
        help="飞书机器人 webhook URL（优先于 YAML 配置和环境变量）。",
    )
    parser.add_argument(
        "--title",
        default="",
        help="飞书周报标题（优先于 YAML 配置）。",
    )
    parser.add_argument(
        "--since",
        help="开始时间（ISO 格式，例如 2026-04-20T00:00:00）。默认本周一 00:00:00。",
    )
    parser.add_argument(
        "--until",
        help="结束时间（ISO 格式）。默认当前时间。",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="仅打印周报，不发送飞书。",
    )
    parser.add_argument(
        "--yes",
        action="store_true",
        help="跳过发送前确认，直接发送（仅建议自动化场景使用）。",
    )
    parser.add_argument(
        "--no-ai-polish",
        action="store_true",
        help="关闭 DeepSeek 润色，直接使用原始周报内容。",
    )
    return parser.parse_args()


def _to_bool(value: object, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "y", "on"}
    return default


def _to_float(value: object, default: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _load_yaml_config(config_path: str) -> ScriptConfig:
    path = Path(config_path).expanduser().resolve()
    if not path.exists():
        return ScriptConfig()

    try:
        import yaml  # type: ignore
    except ImportError as exc:
        raise RuntimeError("缺少依赖 PyYAML，请先安装: pip install pyyaml") from exc

    try:
        loaded = yaml.safe_load(path.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001
        raise RuntimeError(f"读取 YAML 配置失败 [{path}]: {exc}") from exc

    data = loaded if isinstance(loaded, dict) else {}
    feishu = data.get("feishu") if isinstance(data.get("feishu"), dict) else {}
    deepseek = data.get("deepseek") if isinstance(data.get("deepseek"), dict) else {}
    raw_repos = data.get("projects")
    repos: list[str] | None = None
    if isinstance(raw_repos, list):
        repos = [str(item).strip() for item in raw_repos if str(item).strip()]

    title = data.get("title")
    return ScriptConfig(
        webhook_url=str(feishu.get("webhook_url", "")).strip(),
        repos=repos,
        title=str(title).strip() if isinstance(title, str) and title.strip() else None,
        deepseek_enabled=_to_bool(deepseek.get("enabled"), False),
        deepseek_api_key=str(deepseek.get("api_key", "")).strip(),
        deepseek_base_url=str(deepseek.get("base_url", "https://api.deepseek.com")).strip()
        or "https://api.deepseek.com",
        deepseek_model=str(deepseek.get("model", "deepseek-chat")).strip() or "deepseek-chat",
        deepseek_temperature=_to_float(deepseek.get("temperature"), 0.4),
    )


def _resolve_time_range(since: str | None, until: str | None) -> tuple[dt.datetime, dt.datetime]:
    now = dt.datetime.now().replace(microsecond=0)
    if since:
        since_dt = dt.datetime.fromisoformat(since)
    else:
        monday = now - dt.timedelta(days=now.weekday())
        since_dt = monday.replace(hour=0, minute=0, second=0)

    until_dt = dt.datetime.fromisoformat(until) if until else now

    if since_dt > until_dt:
        raise ValueError("--since 不能晚于 --until")

    return since_dt, until_dt


def _prompt_repos() -> list[str]:
    raw = input("请输入仓库路径（多个路径用英文逗号分隔）: ").strip()
    if not raw:
        return []
    return [item.strip() for item in raw.split(",") if item.strip()]


def _confirm_send() -> bool:
    answer = input("\n以上为周报预览，确认发送到飞书吗？[y/N]: ").strip().lower()
    return answer in {"y", "yes"}


def _validate_repo_path(repo_path: str) -> Path:
    path = Path(repo_path).expanduser().resolve()
    if not path.exists() or not path.is_dir():
        raise ValueError(f"仓库路径不存在: {repo_path}")
    if not (path / ".git").exists():
        raise ValueError(f"不是 Git 仓库: {repo_path}")
    return path


def _run_git_log(repo: Path, since_dt: dt.datetime, until_dt: dt.datetime) -> list[Commit]:
    pretty = "%H%x1f%an%x1f%aI%x1f%s"
    cmd = [
        "git",
        "-C",
        str(repo),
        "log",
        "--since",
        since_dt.isoformat(sep=" "),
        "--until",
        until_dt.isoformat(sep=" "),
        f"--pretty=format:{pretty}",
    ]

    result = subprocess.run(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        check=False,
    )

    if result.returncode != 0:
        raise RuntimeError(
            f"读取 Git 日志失败 [{repo}]: {result.stderr.strip() or 'unknown error'}"
        )

    commits: list[Commit] = []
    for line in result.stdout.splitlines():
        parts = line.split("\x1f")
        if len(parts) != 4:
            continue
        commit_time = dt.datetime.fromisoformat(parts[2].replace("Z", "+00:00"))
        commits.append(
            Commit(
                repo_name=repo.name,
                commit_id=parts[0],
                author=parts[1],
                commit_time=commit_time,
                message=parts[3],
            )
        )
    return commits


def _collect_commits(repos: Iterable[Path], since_dt: dt.datetime, until_dt: dt.datetime) -> list[Commit]:
    all_commits: list[Commit] = []
    for repo in repos:
        all_commits.extend(_run_git_log(repo, since_dt, until_dt))
    all_commits.sort(key=lambda c: c.commit_time, reverse=True)
    return all_commits


def _build_report_text(commits: list[Commit], since_dt: dt.datetime, until_dt: dt.datetime) -> str:
    header = [
        "# 本周 Git 提交周报",
        f"统计区间: {since_dt:%Y-%m-%d %H:%M:%S} ~ {until_dt:%Y-%m-%d %H:%M:%S}",
        f"提交总数: {len(commits)}",
        "",
    ]

    if not commits:
        header.append("本周暂无提交记录。")
        return "\n".join(header)

    repo_counter: dict[str, int] = {}
    for c in commits:
        repo_counter[c.repo_name] = repo_counter.get(c.repo_name, 0) + 1

    summary_lines = ["## 按仓库统计"]
    for repo_name, count in sorted(repo_counter.items(), key=lambda x: x[1], reverse=True):
        summary_lines.append(f"- {repo_name}: {count} 次提交")

    detail_lines = ["", "## 提交明细"]
    for c in commits:
        detail_lines.append(
            f"- [{c.repo_name}] {c.message} ({c.author}, {c.commit_time:%Y-%m-%d %H:%M}, {c.commit_id[:8]})"
        )

    return "\n".join(header + summary_lines + detail_lines)


def _truncate_text(text: str, max_len: int) -> str:
    if len(text) <= max_len:
        return text
    suffix = "\n\n> 内容过长，已截断显示。"
    return text[: max_len - len(suffix)] + suffix


def _build_feishu_payload(title: str, report_text: str) -> dict:
    card_markdown = _truncate_text(report_text.strip(), 18000)

    return {
        "msg_type": "interactive",
        "card": {
            "config": {"wide_screen_mode": True},
            "header": {
                "template": "blue",
                "title": {"tag": "plain_text", "content": title},
            },
            "elements": [
                {"tag": "markdown", "content": card_markdown},
                {
                    "tag": "note",
                    "elements": [
                        {
                            "tag": "plain_text",
                            "content": "由 WeeklyReportScript 自动生成",
                        }
                    ],
                },
            ],
        },
    }


def _send_to_feishu(webhook_url: str, payload: dict) -> None:
    body = json.dumps(payload).encode("utf-8")
    req = request.Request(
        webhook_url,
        data=body,
        headers={"Content-Type": "application/json; charset=utf-8"},
        method="POST",
    )

    try:
        with request.urlopen(req, timeout=15) as resp:
            resp_body = resp.read().decode("utf-8", errors="replace")
    except error.URLError as exc:
        raise RuntimeError(f"飞书发送失败: {exc}") from exc

    try:
        data = json.loads(resp_body)
    except json.JSONDecodeError:
        raise RuntimeError(f"飞书返回非 JSON 响应: {resp_body}")

    if data.get("code") not in (0, "0"):
        raise RuntimeError(f"飞书返回错误: {data}")


def _build_deepseek_url(base_url: str) -> str:
    return f"{base_url.rstrip('/')}/chat/completions"


def _polish_report_with_deepseek(
    *,
    report_text: str,
    since_dt: dt.datetime,
    until_dt: dt.datetime,
    api_key: str,
    base_url: str,
    model: str,
    temperature: float,
) -> str:
    if not api_key:
        raise RuntimeError("DeepSeek API Key 为空，无法进行润色。")

    prompt = (
        "请将以下周报内容进行专业润色，输出中文。要求：\n"
        "1) 保留事实，不杜撰；\n"
        "2) 先给一个简洁总结；\n"
        "3) 再给按仓库统计和关键提交点；\n"
        "4) 风格适合向团队汇报；\n"
        "5) 输出使用纯文本/Markdown，不要代码块。\n\n"
        f"统计区间: {since_dt:%Y-%m-%d %H:%M:%S} ~ {until_dt:%Y-%m-%d %H:%M:%S}\n\n"
        "原始周报:\n"
        f"{report_text}"
    )

    payload = {
        "model": model,
        "temperature": temperature,
        "messages": [
            {
                "role": "system",
                "content": "你是严谨的研发周报助手，只做语言优化和结构化总结。",
            },
            {"role": "user", "content": prompt},
        ],
    }
    body = json.dumps(payload).encode("utf-8")

    req = request.Request(
        _build_deepseek_url(base_url),
        data=body,
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json; charset=utf-8",
        },
        method="POST",
    )

    try:
        with request.urlopen(req, timeout=60) as resp:
            resp_body = resp.read().decode("utf-8", errors="replace")
    except error.URLError as exc:
        raise RuntimeError(f"DeepSeek 调用失败: {exc}") from exc

    try:
        data = json.loads(resp_body)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"DeepSeek 返回非 JSON 响应: {resp_body}") from exc

    choices = data.get("choices")
    if not isinstance(choices, list) or not choices:
        raise RuntimeError(f"DeepSeek 响应缺少 choices: {data}")

    first = choices[0] if isinstance(choices[0], dict) else {}
    message = first.get("message") if isinstance(first.get("message"), dict) else {}
    content = message.get("content")

    if not isinstance(content, str) or not content.strip():
        raise RuntimeError(f"DeepSeek 响应缺少可用内容: {data}")

    return content.strip()


def main() -> int:
    args = _parse_args()

    try:
        cfg = _load_yaml_config(args.config)
    except RuntimeError as exc:
        print(str(exc), file=sys.stderr)
        return 2

    try:
        since_dt, until_dt = _resolve_time_range(args.since, args.until)
    except ValueError as exc:
        print(f"时间参数错误: {exc}", file=sys.stderr)
        return 2

    repo_inputs = args.repos or (cfg.repos or []) or _prompt_repos()
    if not repo_inputs:
        print("未提供任何仓库路径。", file=sys.stderr)
        return 2

    repos: list[Path] = []
    for item in repo_inputs:
        try:
            repos.append(_validate_repo_path(item))
        except ValueError as exc:
            print(f"路径校验失败: {exc}", file=sys.stderr)
            return 2

    try:
        commits = _collect_commits(repos, since_dt, until_dt)
    except RuntimeError as exc:
        print(str(exc), file=sys.stderr)
        return 1

    report_text = _build_report_text(commits, since_dt, until_dt)
    ai_enabled = cfg.deepseek_enabled and not args.no_ai_polish
    deepseek_api_key = cfg.deepseek_api_key or os.getenv("DEEPSEEK_API_KEY", "")

    if ai_enabled:
        try:
            report_text = _polish_report_with_deepseek(
                report_text=report_text,
                since_dt=since_dt,
                until_dt=until_dt,
                api_key=deepseek_api_key,
                base_url=cfg.deepseek_base_url,
                model=cfg.deepseek_model,
                temperature=cfg.deepseek_temperature,
            )
            print("\n已完成 DeepSeek 润色。")
        except RuntimeError as exc:
            print(f"DeepSeek 润色失败，已回退到原始周报: {exc}", file=sys.stderr)

    print("\n========== 周报预览开始 ==========")
    print(report_text)
    print("========== 周报预览结束 ==========")

    if args.dry_run:
        return 0

    webhook_url = args.webhook_url or cfg.webhook_url or os.getenv("FEISHU_WEBHOOK_URL", "")
    if not webhook_url:
        print(
            "未提供飞书 webhook。请通过 --webhook-url、config.yaml 或环境变量 FEISHU_WEBHOOK_URL 提供。",
            file=sys.stderr,
        )
        return 2

    if not args.yes and not _confirm_send():
        print("已取消发送。")
        return 0

    title = args.title if args.title else (cfg.title or "本周工作周报")
    payload = _build_feishu_payload(title, report_text)

    try:
        _send_to_feishu(webhook_url, payload)
    except RuntimeError as exc:
        print(str(exc), file=sys.stderr)
        return 1

    print("\n飞书周报发送成功。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
