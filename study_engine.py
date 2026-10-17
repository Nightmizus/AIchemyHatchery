"""HatcheryStudy：直连 OpenAI 兼容网关的学习教练。

复刻 studypanel 的机制：
- study_coach.md（完整教学技能提示词）作为 system 角色注入，每次调用都完整带上，
  不依赖模型上下文记忆，因此不会被对话历史淹没；
- 模型输出 {"title","markdown"} 结构化课程，前端按 studypanel 学习视图渲染
  （Markdown + KaTeX 数学）；
- 学习进度由模型通过 write_progress 函数调用自行维护，用户不可见；
- 不经过 DeepSeek Harness，直接 POST /chat/completions。
"""
from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from pathlib import Path

import personal_api

ROOT = Path(__file__).resolve().parent
COACH_PATH = ROOT / "study_coach.md"

# studypanel 的展示契约（internal/study/worker.go 的 instructions() 等价物）
PRESENTATION_CONTRACT = """
StudyPanel presentation contract:
You are teaching in a web learning session. The learner's text comes from a web composer.
Follow the study-coach teaching methods above. Opening a session is not a request to start an interview. Answer the learner's actual question first.
学习进度由你通过 write_progress 工具维护，学习者看不到进度文件内容：需要记录或更新掌握证据、错因、复习排期时调用它，写入完整 Markdown（覆盖式）。不要在回答正文里输出进度内容，也不要向学习者索要文件路径。
Your FINAL response MUST be a JSON object with exactly two strings: title and markdown.
markdown is the lesson in Markdown. Do not wrap the entire answer in a Markdown code fence. Use headings, lists, tables, blockquotes and fenced code blocks where appropriate.
Mathematical expressions are rendered by KaTeX. Use $...$ or \\(...\\) for inline math, and $$...$$ or \\[...\\] for display math. Keep LaTeX out of code fences unless explaining its source.
Answer in the learner's language and keep the layout readable on phones.
"""

_TOOL_INSTRUCTION = """
学习者看不到进度文件。若下方「当前学习进度记录」为非空，说明此前已记录过证据。
本轮只输出面向学习者的课程正文，不要调用任何工具；学习进度会在本轮结束后单独更新。
"""

# 单独的进度更新指令：课程回答完成后，用第二轮调用让模型维护进度。
PROGRESS_UPDATE_INSTRUCTION = """刚才这轮辅导已经结束。现在只做一件事：更新学习进度记录。

调用 write_progress 工具，传入更新后的**完整**进度 Markdown（覆盖式写入，保留此前已有内容和 ID，只增补本轮证据）。
严格按 study-coach 第七节的结构组织内容（档案与约定、目标与材料、知识点总览、错因与补强、复习队列、会话日志、两周复盘）。
本轮学习者消息与你的回答摘要：
---
learner: {question}
coach: {answer}
---
只调用工具，不要输出任何面向学习者的文字。"""


def load_coach() -> str:
    """读取教学技能提示词；文件缺失时用最小可用提示词兜底。"""
    try:
        return COACH_PATH.read_text(encoding="utf-8")
    except OSError:
        return "# 学习教练\n\n依据学习证据开展主动式个性化辅导。"


_coach_cache: str | None = None


def coach_prompt() -> str:
    global _coach_cache
    if _coach_cache is None:
        _coach_cache = load_coach()
    return _coach_cache


def system_prompt() -> str:
    """system 角色正文：教学技能 + 展示契约 + 进度工具说明。"""
    return coach_prompt() + PRESENTATION_CONTRACT + _TOOL_INSTRUCTION


def gateway_config() -> tuple[str, str, str]:
    """返回 (base_url, api_key, model)。沿用 hatchery 现有网关与 Key 配置。"""
    base = os.environ.get("HATCHERY_DEEPSEEK_BASE_URL", "").strip().rstrip("/")
    if not base:
        base = "https://api.deepseek.com/v1"
    key = os.environ.get("DEEPSEEK_API_KEY", "").strip()
    model = os.environ.get("DEEPSEEK_MODEL", "").strip() or "DeepSeek-V4.1-Flash"
    return base, key, model


# ===== 自定义 provider（只给站长自用，简陋实现：一个 JSON 文件）=====
PROVIDER_FILE = ROOT / "study_providers.json"


def load_providers() -> list[dict]:
    """读取自定义 provider 列表；文件不存在或损坏时返回空列表。"""
    try:
        raw = json.loads(PROVIDER_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    if not isinstance(raw, list):
        return []
    out = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        pid = str(item.get("id", "")).strip()
        base = str(item.get("base_url", "")).strip()
        if not pid or not base:
            continue
        out.append({
            "id": pid,
            "label": str(item.get("label", "")).strip() or pid,
            "base_url": base.rstrip("/"),
            "api_key": str(item.get("api_key", "")).strip(),
            "model": str(item.get("model", "")).strip(),
        })
    return out


def save_providers(items: list[dict]) -> None:
    """原子写入并收紧权限（含明文 Key）。"""
    tmp = PROVIDER_FILE.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(items, ensure_ascii=False, indent=2), encoding="utf-8")
    os.chmod(tmp, 0o600)
    os.replace(tmp, PROVIDER_FILE)


def find_provider(pid: str) -> dict | None:
    for item in load_providers():
        if item["id"] == pid:
            return item
    return None


def resolve_endpoint(provider_id: str = "") -> tuple[str, str, str, str]:
    """把 provider 选择解析成 (base_url, api_key, model, 展示名)。

    provider_id 为空或 'default' 时用 hatchery 自己的配置。
    """
    pid = (provider_id or "").strip()
    if pid and pid != "default":
        item = find_provider(pid)
        if item is None:
            raise StudyGatewayError(f"自定义 provider 不存在：{pid}")
        model = item["model"] or gateway_config()[2]
        return item["base_url"], item["api_key"], model, item["label"]
    base, key, model = gateway_config()
    return base, key, model, "默认（Hatchery）"


def fetch_models(base_url: str, api_key: str, timeout: int = 30, *, personal: bool = False) -> list[str]:
    """查询某个 endpoint 的模型列表。"""
    request = urllib.request.Request(
        base_url.rstrip("/") + "/models",
        headers={"Authorization": f"Bearer {api_key}"},
    )
    try:
        with (personal_api.open_request(request, timeout=timeout) if personal else urllib.request.urlopen(request, timeout=timeout)) as response:
            data = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as error:
        raise StudyGatewayError(f"模型列表请求失败（{error.code}）") from error
    except (urllib.error.URLError, TimeoutError) as error:
        raise StudyGatewayError(f"无法连接该 endpoint：{error}") from error
    items = data.get("data") if isinstance(data, dict) else data
    if not isinstance(items, list):
        return []
    out = []
    for item in items:
        if isinstance(item, dict) and item.get("id"):
            out.append(str(item["id"]))
        elif isinstance(item, str):
            out.append(item)
    return sorted(out)


PROGRESS_TOOL = {
    "type": "function",
    "function": {
        "name": "write_progress",
        "description": (
            "写入或更新学习者的完整学习进度 Markdown。每次调用都会完整覆盖之前的进度文件。"
            "用于记录学习目标、掌握证据、错因、复习队列和下次检查日期。学习者看不到这些内容。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "markdown": {
                    "type": "string",
                    "description": "完整的学习进度 Markdown 正文，包含本次更新后的全部内容。",
                }
            },
            "required": ["markdown"],
        },
    },
}


def build_messages(progress: str, history: list[dict], user_text: str) -> list[dict]:
    """组装请求消息。

    system 角色承载完整教学提示词，每次调用都完整重发——这是提示词不被淹没的关键。
    """
    messages: list[dict] = [{"role": "system", "content": system_prompt()}]
    # 当前进度作为独立上下文，让模型明确「已经教到哪」。
    messages.append({
        "role": "system",
        "content": "当前学习进度记录（由你维护，学习者不可见）：\n\n" + (progress.strip() or "（尚无记录，这是第一次学习）"),
    })
    for item in history:
        role = item.get("role")
        text = str(item.get("text", ""))
        if role in ("user", "assistant") and text:
            messages.append({"role": role, "content": text})
    messages.append({"role": "user", "content": user_text})
    return messages


class StudyGatewayError(RuntimeError):
    pass


def stream_completion(
    messages: list[dict],
    *,
    tools: list[dict] | None = None,
    tool_choice: str | None = "auto",
    timeout: int = 600,
    provider_id: str = "",
    model_override: str = "",
    api_config: dict | None = None,
):
    """向网关发起流式请求，逐块 yield 解析后的增量对象。

    每个增量形如：
      {"content": "文本增量", "tool": {"name":..., "arguments": "..."} }
    tool_choice="none" 时仍下发工具定义（否则模型会自己编造 <｜DSML｜> 之类的
    伪调用文本），但不允许真正调用，保证本轮只产出正文。
    """
    if api_config:
        base, key, model = api_config["base_url"], api_config["api_key"], api_config["model"]
    else:
        base, key, model, _label = resolve_endpoint(provider_id)
    if model_override.strip():
        model = model_override.strip()
    if not key:
        raise StudyGatewayError("尚未配置 DEEPSEEK_API_KEY，无法使用学习教练")

    payload = {
        "model": model,
        "messages": messages,
        "stream": True,
        "max_tokens": _max_tokens(),
    }
    if tools:
        payload["tools"] = tools
        payload["tool_choice"] = tool_choice or "auto"

    request = urllib.request.Request(
        base + "/chat/completions",
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {key}",
            "Content-Type": "application/json",
            "Accept": "text/event-stream",
        },
        method="POST",
    )
    try:
        with (personal_api.open_request(request, timeout=timeout) if api_config else urllib.request.urlopen(request, timeout=timeout)) as response:
            for raw in response:
                line = raw.decode("utf-8", "replace").strip()
                if not line or not line.startswith("data:"):
                    continue
                chunk = line[5:].strip()
                if chunk == "[DONE]":
                    break
                try:
                    event = json.loads(chunk)
                except json.JSONDecodeError:
                    continue
                for choice in event.get("choices") or []:
                    delta = choice.get("delta") or {}
                    content = delta.get("content")
                    if content:
                        yield {"content": content}
                    for call in delta.get("tool_calls") or []:
                        function = call.get("function") or {}
                        # 工具名只在首个增量里出现，后续增量只带 arguments 片段；
                        # 这里原样透出，由调用方按下标合并，避免空名覆盖真实名称。
                        name = function.get("name") or ""
                        arguments = function.get("arguments")
                        if name or arguments:
                            yield {"tool": {"name": name,
                                            "index": call.get("index", 0),
                                            "arguments": arguments or ""}}
    except urllib.error.HTTPError as error:
        if api_config:
            raise StudyGatewayError(f"个人 API 返回错误（HTTP {error.code}），请检查 Key、模型和接口地址") from None
        detail = ""
        try:
            detail = error.read().decode("utf-8", "replace")[:400]
        except Exception:
            pass
        raise StudyGatewayError(f"AI 服务返回错误（{error.code}）：{detail or error.reason}") from error
    except urllib.error.URLError as error:
        raise StudyGatewayError(f"无法连接 AI 服务：{error.reason}") from error
    except TimeoutError as error:
        raise StudyGatewayError("AI 服务响应超时，请稍后重试") from error
    except ValueError as error:
        if api_config:
            raise StudyGatewayError("个人 API 地址校验失败，请检查配置是否仍指向公网 HTTPS 接口") from None
        raise


def _max_tokens() -> int:
    try:
        return max(1024, min(131072, int(os.environ.get("DEEPSEEK_MAX_TOKENS", "32768"))))
    except ValueError:
        return 32768


def parse_lesson_answer(text: str) -> dict:
    """把模型返回的 {"title","markdown"} 解析出来；解析失败时原文当 Markdown。"""
    candidate = text.strip()
    if not candidate:
        return {"title": "", "markdown": ""}
    # 允许模型用 ```json 包裹整个对象
    fence = None
    if candidate.startswith("```"):
        lines = candidate.splitlines()
        if len(lines) >= 3:
            fence = "\n".join(lines[1:-1]) if lines[-1].strip().startswith("```") else "\n".join(lines[1:])
    for source in (candidate, fence):
        if not source:
            continue
        try:
            value = json.loads(source)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            title = str(value.get("title") or "").strip()
            markdown = value.get("markdown")
            if isinstance(markdown, str) and markdown.strip():
                return {"title": title, "markdown": markdown}
    return {"title": "", "markdown": candidate}
