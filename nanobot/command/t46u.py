"""Deterministic desktop dial intent and Yealink T46U control.

The agent loop calls this module before contacting a language model. Configuration
belongs to each desktop workspace, never to the packaged skill or chat history.
"""

from __future__ import annotations

import base64
import json
import re
import ssl
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path

SKILL_NAME = "call"
LEGACY_SKILL_NAME = "t46u-dial"
DIRECTORY_NAME = "内部分机信息表.md"
BUNDLED_DIRECTORY = Path(__file__).resolve().parent.parent / "skills" / SKILL_NAME / DIRECTORY_NAME
DEFAULT_PBX_IP = "12.100.10.9"


@dataclass(frozen=True)
class DialIntent:
    action: str
    target: str = ""
    caller: str = ""


def parse_dial_intent(text: str) -> DialIntent | None:
    """Only accept a complete, direct phone instruction; leave discussion to the LLM."""
    raw = text.strip().rstrip("。！! ")
    slash = re.fullmatch(r"/call(?:\s+(.+))?", raw, flags=re.IGNORECASE)
    if slash:
        argument = (slash.group(1) or "").strip()
        if not argument:
            return DialIntent("help")
        if argument.lower() in {"hangup", "挂断", "挂机", "挂断电话"}:
            return DialIntent("hangup")
        if argument.startswith("/"):
            return DialIntent("help")
        direct = parse_dial_intent(argument)
        return direct or parse_dial_intent(f"拨打{argument}") or DialIntent("help")
    raw = re.sub(
        r"^(?:(?:请|现在|立即|麻烦你?|帮我|请帮我|用(?:这个)?技能|使用(?:这个)?技能|使用\s*skill\s*技能|用我的电话)\s*)+",
        "", raw, flags=re.IGNORECASE,
    )
    if re.fullmatch(r"(?:请)?(?:挂断|挂机|结束通话)(?:电话|当前通话)?", raw):
        return DialIntent("hangup")

    caller = ""
    match = re.fullmatch(r"用\s*([0-9]{4}|[\u4e00-\u9fff]{2,12})\s*分机\s*拨打\s*(.+)", raw)
    if match:
        caller, raw = match.groups()
        raw = "拨打" + raw

    match = (
        re.fullmatch(r"(?:拨打|拨号(?:给)?|打给|打电话给|呼叫)\s*(.+)", raw)
        or re.fullmatch(r"给\s*(.+?)\s*打(?:个)?电话", raw)
    )
    if not match:
        return None
    target = re.sub(r"(?:的)?(?:分机|电话|号码)$", "", match.group(1).strip()).strip()
    if re.search(r"怎么|如何|为什么|为何|不生效|失败|问题|怎么办|查询|设置", target):
        return None
    if not re.fullmatch(r"[\u4e00-\u9fff·]{2,12}|[A-Za-z][A-Za-z .'-]{1,39}|\+?[\d\s()（）-]{3,24}", target):
        return None
    return DialIntent("dial", target=target, caller=caller)


def _load_json(path: Path) -> dict:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def _rows(path: Path) -> list[dict[str, str]]:
    try:
        lines = path.read_text(encoding="utf-8-sig").splitlines()
    except OSError:
        return []
    header: list[str] | None = None
    result = []
    for line in lines:
        if not line.strip().startswith("|"):
            continue
        cells = [cell.strip() for cell in line.strip().strip("|").split("|")]
        if header is None:
            header = cells
        elif len(cells) == len(header) and not all(re.fullmatch(r":?-{3,}:?", c) for c in cells):
            result.append(dict(zip(header, cells)))
    return result


def _lookup(rows: list[dict[str, str]], value: str) -> tuple[dict[str, str] | None, str]:
    query = value.strip()
    keys = ("分机短号", "显示名称", "显示名称(英文)")
    exact = [r for r in rows if any(r.get(key, "").strip().casefold() == query.casefold() for key in keys)]
    if len(exact) == 1:
        return exact[0], ""
    if len(exact) > 1:
        return None, f"「{query}」对应多条记录，请提供分机号。"
    partial = [r for r in rows if any(query.casefold() in r.get(key, "").casefold() for key in keys[1:])]
    if len(partial) == 1:
        return partial[0], ""
    if len(partial) > 1:
        return None, f"「{query}」匹配多个人，请提供完整姓名或分机号。"
    return None, f"通讯录里没有「{query}」，请检查姓名或直接提供号码。"


def _phone_number(target: str, rows: list[dict[str, str]], prefix: str) -> tuple[str, str]:
    cleaned = re.sub(r"[\s()（）-]", "", target)
    if re.fullmatch(r"\+?\d{3,20}", cleaned):
        if cleaned.startswith("+"):
            return "", "国际号码请先按话机拨号规则提供完整号码。"
        if len(cleaned) != 4 and prefix and not cleaned.startswith(prefix):
            cleaned = prefix + cleaned
        return cleaned, ""
    row, error = _lookup(rows, target)
    if error:
        return "", error
    number = (row or {}).get("分机短号", "").strip()
    if not re.fullmatch(r"\d{4}", number):
        return "", f"「{target}」没有有效的四位分机号。"
    return number, ""


def _request(phone_ip: str, user: str, password: str, params: dict[str, str], *, scheme: str) -> tuple[int | None, str]:
    url = f"{scheme}://{phone_ip}/cgi-bin/ConfigManApp.com?{urllib.parse.urlencode(params)}"
    token = base64.b64encode(f"{user}:{password}".encode()).decode("ascii")
    request = urllib.request.Request(url, headers={"Authorization": f"Basic {token}"})
    context = ssl._create_unverified_context() if scheme == "https" else None
    # A corporate HTTPS_PROXY must never intercept calls to a LAN desk phone.
    handlers: list = [urllib.request.ProxyHandler({})]
    if context is not None:
        handlers.append(urllib.request.HTTPSHandler(context=context))
    opener = urllib.request.build_opener(*handlers)
    try:
        with opener.open(request, timeout=8) as response:
            return response.status, response.read(2048).decode("utf-8", "replace")
    except urllib.error.HTTPError as error:
        return error.code, error.read(2048).decode("utf-8", "replace")
    except (OSError, ValueError) as error:
        return None, str(error)


def execute_dial_intent(workspace: Path, intent: DialIntent) -> str:
    """Execute one call and return an honest user-facing status message."""
    if intent.action == "help":
        return "用法：/call 姓名或号码；/call 挂断。也可以直接说“拨打某人电话”。"
    skill_dir = workspace / "skills" / SKILL_NAME
    config_path = skill_dir / "config.json"
    config = _load_json(config_path)
    legacy_skill_dir = workspace / "skills" / LEGACY_SKILL_NAME
    caller_input = intent.caller or str(config.get("caller") or "").strip()
    if not caller_input:
        return (
            "还没有设置这台电脑的主叫分机。请在工作区 skills/call/config.json "
            "配置 caller（四位分机号）、phone_ip、pbx_ip 和话机登录凭据。"
        )

    table_value = config.get("directory")
    if table_value:
        table = Path(str(table_value)).expanduser()
        if not table.is_absolute():
            table = skill_dir / table
    else:
        table = skill_dir / DIRECTORY_NAME
        if not table.exists():
            table = legacy_skill_dir / DIRECTORY_NAME
        if not table.exists():
            table = BUNDLED_DIRECTORY
    rows = _rows(table)
    caller_row, caller_error = _lookup(rows, caller_input)
    caller = (caller_row or {}).get("分机短号", "").strip() or caller_input
    if not re.fullmatch(r"\d{4}", caller):
        return caller_error or "主叫分机必须是有效的四位号码。"
    phone_ip = str(config.get("phone_ip") or (caller_row or {}).get("终端IP") or "").strip()
    if not phone_ip or phone_ip.lower() in {"none", "xxx.xxx.1.1"}:
        if caller_error and not config.get("phone_ip"):
            return f"通讯录里没有主叫分机 {caller} 的有效话机 IP；请补全通讯录或在 skills/call/config.json 设置 phone_ip。"
        return (
            f"找不到主叫分机 {caller} 的话机 IP。请更新通讯录，"
            "或在 skills/call/config.json 设置 phone_ip。"
        )
    if not re.fullmatch(r"[\w.:-]+", phone_ip):
        return "话机 IP 配置格式无效。"

    pbx_ip = str(config.get("pbx_ip") or DEFAULT_PBX_IP).strip()
    user = str(config.get("username") or "").strip()
    password = str(config.get("password") or "")
    if not user or not password:
        return "尚未配置本机话机的 username/password，请填写 skills/call/config.json。"
    scheme = str(config.get("scheme") or "https").lower()
    if scheme not in {"http", "https"} or not re.fullmatch(r"[\w.:-]+", pbx_ip):
        return "PBX 地址或话机协议配置无效。"

    if intent.action == "hangup":
        params = {"key": "CALLEND"}
    else:
        number, error = _phone_number(intent.target, rows, str(config.get("outgoing_prefix", "9")))
        if error:
            return error
        params = {"number": number, "outgoing_url": f"{caller}@{pbx_ip}"}

    status, body = _request(phone_ip, user, password, params, scheme=scheme)
    if status != 200 or re.search(r"\b(?:error|failed|invalid|login)\b|<html", body, re.IGNORECASE):
        detail = re.sub(r"[\r\n]+", " ", body)[:120]
        return f"话机指令发送失败（HTTP {status or '无响应'}）：{detail or '请检查话机网络和登录配置'}"
    if intent.action == "hangup":
        return f"已向主叫分机 {caller} 的话机发送挂断指令。"
    return f"已向主叫分机 {caller} 的话机发送拨打 {intent.target} 的指令。是否接通请以话机状态为准。"
