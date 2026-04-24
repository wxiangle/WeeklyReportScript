#!/usr/bin/env python3
"""Generate weekly git report and send to Feishu."""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
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
    deepseek_polish_mode: str = "concise"  # "concise" | "detailed"
    project_name_rules: list[tuple[str, list[str]]] | None = None


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
    parser.add_argument(
        "--refresh-ai-cache",
        action="store_true",
        help="忽略本周 AI 缓存，重新调用 DeepSeek 并覆盖缓存。",
    )
    parser.add_argument(
        "--polish-mode",
        choices=["concise", "detailed"],
        default=None,
        dest="polish_mode",
        help="AI 润色模式：concise（简洁，≤25字/条，≤6条）或 detailed（详细，完整描述）。优先于 YAML 配置。",
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
    raw_name_rules = data.get("project_name_rules")
    raw_repos = data.get("projects")
    repos: list[str] | None = None
    if isinstance(raw_repos, list):
        repos = [str(item).strip() for item in raw_repos if str(item).strip()]

    project_name_rules: list[tuple[str, list[str]]] = []
    if isinstance(raw_name_rules, list):
        for item in raw_name_rules:
            if not isinstance(item, dict):
                continue
            name = item.get("name")
            keywords = item.get("keywords")
            if not isinstance(name, str) or not name.strip():
                continue
            if not isinstance(keywords, list):
                continue
            cleaned_keywords = [
                str(k).strip().lower() for k in keywords if str(k).strip()
            ]
            if not cleaned_keywords:
                continue
            project_name_rules.append((name.strip(), cleaned_keywords))

    title = data.get("title")
    raw_mode = str(deepseek.get("polish_mode", "concise")).strip().lower()
    polish_mode = raw_mode if raw_mode in {"concise", "detailed"} else "concise"
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
        deepseek_polish_mode=polish_mode,
        project_name_rules=project_name_rules,
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


def _resolve_project_display_name(repo: Path, cfg: ScriptConfig) -> str:
    rules = cfg.project_name_rules or []
    target = str(repo).lower()
    for display_name, keywords in rules:
        for kw in keywords:
            if kw in target:
                return display_name
    return repo.name


def _collect_commits(
    repos: Iterable[Path],
    since_dt: dt.datetime,
    until_dt: dt.datetime,
    cfg: ScriptConfig,
) -> list[Commit]:
    all_commits: list[Commit] = []
    for repo in repos:
        display_name = _resolve_project_display_name(repo, cfg)
        repo_commits = _run_git_log(repo, since_dt, until_dt)
        for commit in repo_commits:
            commit.repo_name = display_name
        all_commits.extend(repo_commits)
    all_commits.sort(key=lambda c: c.commit_time, reverse=True)
    return all_commits


def _build_report_text(commits: list[Commit], since_dt: dt.datetime, until_dt: dt.datetime) -> str:
    _ = (since_dt, until_dt)
    lines = ["# 本周 Git 提交周报"]

    if not commits:
        lines.append("本周暂无提交记录。")
        return "\n".join(lines)

    lines.append("## 提交明细")
    for c in commits:
        lines.append(
            f"- [{c.repo_name}] {c.message} ({c.author}, {c.commit_time:%Y-%m-%d %H:%M}, {c.commit_id[:8]})"
        )

    return "\n".join(lines)


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

    code = data.get("code")
    if code not in (0, "0"):
        if code == 11232:
            raise RuntimeError(
                "飞书机器人频率限制。请稍后（通常几秒到几分钟）后重试。"
                "如频繁遇到此问题，可联系飞书管理员调整机器人配额。"
            )
        msg = data.get("msg", "未知错误")
        raise RuntimeError(f"飞书返回错误 (code {code}): {msg}")


def _build_deepseek_url(base_url: str) -> str:
    return f"{base_url.rstrip('/')}/chat/completions"


def _build_ai_cache_path(repos: list[Path], since_dt: dt.datetime, seed_text: str) -> Path:
    # 缓存粒度按“周 + 仓库集合”，减少重复调用 DeepSeek。
    week_start = (since_dt - dt.timedelta(days=since_dt.weekday())).date()
    repo_signature = "|".join(sorted(str(repo) for repo in repos))
    seed_hash = hashlib.sha1(seed_text.encode("utf-8")).hexdigest()[:10]
    repo_hash = hashlib.sha1(repo_signature.encode("utf-8")).hexdigest()[:10]
    cache_dir = Path(".cache")
    cache_name = f"deepseek_{week_start:%Y%m%d}_{repo_hash}_{seed_hash}.md"
    return cache_dir / cache_name


def _load_ai_cache(cache_path: Path) -> str | None:
    if not cache_path.exists():
        return None
    try:
        text = cache_path.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    return text or None


def _save_ai_cache(cache_path: Path, report_text: str) -> None:
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    cache_path.write_text(report_text, encoding="utf-8")


def _extract_project_names(commits: list[Commit]) -> list[str]:
    names: list[str] = []
    seen: set[str] = set()
    for commit in commits:
        if commit.repo_name not in seen:
            names.append(commit.repo_name)
            seen.add(commit.repo_name)
    return names


def _build_polish_prompt(
    report_text: str,
    since_dt: dt.datetime,
    until_dt: dt.datetime,
    project_names: list[str],
    polish_mode: str,
) -> str:
    section_template = "\n".join(f"## {name}" for name in project_names) if project_names else "## 项目一"
    project_name_rules_str = (
        "\n".join(f"- {name}" for name in project_names)
        if project_names
        else "- （无）"
    )
    common_rules = (
        "请将以下周报内容进行专业润色，输出中文。要求：\n"
        "1) 保留事实，不杜撰；\n"
    )
    if polish_mode == "detailed":
        style_rules = (
            "2) 完整保留每条工作项的技术细节，语言流畅即可，不压缩信息；\n"
            "3) 每个项目段落条数不限，各条之间保持适当分段；\n"
        )
    else:  # concise
        style_rules = (
            "2) 内容高度精炼，每条工作项控制在 25 字以内，用最简短的动宾短语表达；\n"
            "3) 条数不限，保留所有工作项，不得遗漏；\n"
        )
    structure_rules = (
        "4) 不要包含「统计区间」「提交总数」「按仓库统计」「关键提交点」这些标题或段落；\n"
        "5) 必须严格使用下面给定的项目名称作为分段标题，不能改写成「Android 端/Flutter 端」等泛化名称；\n"
        "6) 每个项目都要有独立段落，标题格式必须是二级标题；\n"
        "7) 风格适合向团队汇报；\n"
        "8) 输出使用纯文本/Markdown，不要代码块。\n\n"
    )
    return (
        common_rules
        + style_rules
        + structure_rules
        + "必须原样使用的项目名称：\n"
        + f"{project_name_rules_str}\n\n"
        + "输出模板（标题请替换为上面的项目名，并保持二级标题格式）：\n"
        + f"{section_template}\n\n"
        + f"统计区间: {since_dt:%Y-%m-%d %H:%M:%S} ~ {until_dt:%Y-%m-%d %H:%M:%S}\n\n"
        + "原始周报:\n"
        + report_text
    )


def _polish_report_with_deepseek(
    *,
    report_text: str,
    since_dt: dt.datetime,
    until_dt: dt.datetime,
    project_names: list[str],
    api_key: str,
    base_url: str,
    model: str,
    temperature: float,
    polish_mode: str = "concise",
) -> str:
    if not api_key:
        raise RuntimeError("DeepSeek API Key 为空，无法进行润色。")

    prompt = _build_polish_prompt(report_text, since_dt, until_dt, project_names, polish_mode)

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
        commits = _collect_commits(repos, since_dt, until_dt, cfg)
    except RuntimeError as exc:
        print(str(exc), file=sys.stderr)
        return 1

    report_text = _build_report_text(commits, since_dt, until_dt)
    ai_enabled = cfg.deepseek_enabled and not args.no_ai_polish
    deepseek_api_key = cfg.deepseek_api_key or os.getenv("DEEPSEEK_API_KEY", "")
    project_names = _extract_project_names(commits)
    polish_mode = args.polish_mode or cfg.deepseek_polish_mode
    cache_seed = report_text + "\n__PROMPT_SCHEMA_V4__" + "|".join(project_names) + "|mode=" + polish_mode
    ai_cache_path = _build_ai_cache_path(repos, since_dt, cache_seed)

    if ai_enabled:
        used_cache = False
        if not args.refresh_ai_cache:
            cached_report = _load_ai_cache(ai_cache_path)
            if cached_report:
                report_text = cached_report
                used_cache = True
                print(f"\n已使用本周 DeepSeek 缓存: {ai_cache_path}")

        if not used_cache:
            try:
                report_text = _polish_report_with_deepseek(
                    report_text=report_text,
                    since_dt=since_dt,
                    until_dt=until_dt,
                    project_names=project_names,
                    api_key=deepseek_api_key,
                    base_url=cfg.deepseek_base_url,
                    model=cfg.deepseek_model,
                    temperature=cfg.deepseek_temperature,
                    polish_mode=polish_mode,
                )
                _save_ai_cache(ai_cache_path, report_text)
                if args.refresh_ai_cache:
                    print(f"\n已刷新 DeepSeek 缓存: {ai_cache_path}")
                else:
                    print(f"\n已完成 DeepSeek 润色并写入缓存: {ai_cache_path}")
            except RuntimeError as exc:
                print(f"DeepSeek 润色失败，已回退到原始周报: {exc}", file=sys.stderr)
            except OSError as exc:
                print(f"缓存写入失败，但不影响发送: {exc}", file=sys.stderr)

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
