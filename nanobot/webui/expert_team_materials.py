"""Local evidence requests and portable prompts for an existing research run."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from markdown_it import MarkdownIt

from nanobot.graph.workflows.asset_research import MEMBER_NODES
from nanobot.webui.expert_teams import (
    ASSET_RESEARCH_TEAM_ID,
    ExpertTeamError,
    _manifest,
    _members,
    _safe_child,
)

_MARKDOWN = MarkdownIt("commonmark").enable("table")
_GAP = re.compile(
    r"缺失|待补|未披露|待核验|需核[实验]|未.{0,18}(?:核验|验证|获取|返回|拆解)|"
    r"数据不足|数据缺口|空值|口径不一致|来源冲突|仅.{0,12}(?:获得|见于)|missing|unverified", re.I,
)
_RESOLVED = re.compile(
    r"已[^|。；，\n未待尚仍但]{0,25}(?:补充|补齐|核验|验证|获取|更新)|多源一致|以.{0,30}为准|无(?:需|须)补充|"
    r"(?:^|\|\s*)(?:一致|无冲突|已补齐|已解决|不适用)(?:\s*\||$)"
)
_OPERATIONS = re.compile(
    r"iFinD|Juyuan|Caihui|AnySearch|DuckDuckGo|MCP|聚源|财汇|同花顺|"
    r"工具|限流|超限|额度|停用|超时|429|runtime|deadline|TaskUpdate|SendMessage|Bash|"
    r"spawn|team-lead|\bDAG\b|write_file|read_file|TaskCreate", re.I,
)
_LOCAL = re.compile(
    r"(?:file://|/Users/|/home/|/tmp/|[A-Za-z]:[\\/]|reports/|\.team-runs/|\.team-revisions/)\S+|"
    r"\b[\w-]+\.(?:md|html|json|py)\b", re.I,
)
_DATES = re.compile(r"(?:19|20)\d{2}(?:\s*[-–/]\s*(?:\d{1,2}|(?:19|20)\d{2})){0,2}(?:\s*(?:H[12]|Q[1-4]|年(?:中报|年报|上半年|下半年)?))?", re.I)
_ROLE_TOPICS = {
    "business-analyst": r"业务|分部|产品|客户|渠道|品牌|护城河|定价|继续率|退保|新业务价值|NBV|综合成本率|COR|市场份额|管理层|高管持股",
    "financial-analyst": r"营收|收入|利润|现金流|资本开支|股本|EPS|BVPS|ROE|ROA|估值|市值|净资产|内含价值|偿付|负债|现金|毛利率|净利率|一致预期",
    "industry-researcher": r"行业|市场份额|市占率|市场规模|竞争|同业|销量|产能|渗透率|产业链|政策|海外|渠道",
    "risk-assessor": r"风险|债务|负债|现金|到期|担保|诉讼|监管|管理层|高管|持股|股权|关联交易|资本配置|偿付|久期|利差|敞口|减值",
}
_ROLE_LABELS = dict(zip(MEMBER_NODES, ("商业分析师", "财务分析师", "行业研究员", "风险评估师")))
_VIEWS = dict(zip(MEMBER_NODES, ("段永平视角", "巴菲特视角", "芒格视角", "李录视角")))
_SOURCE = "公司年报/中报及附注、交易所公告；注明链接、发布日期与页码"
# Recommendations are explicitly separate from gaps recorded in a role report.
_SUGGESTIONS = {
    "business-analyst": [
        ("业务构成与盈利来源", "各主营业务收入、占比、分部利润及同比变化", "判断收入来源、盈利驱动与业务协同"),
        ("客户、渠道与定价权", "主要客户集中度、渠道构成、续购/留存指标及产品价格变化", "核验客户黏性与定价权，区分品牌知名度和实际竞争优势"),
        ("竞争优势的经营证据", "核心产品与可比公司的价格、成本、市场份额及其变化", "验证护城河及其可持续性"),
    ],
    "financial-analyst": [
        ("利润与现金流", "近 3-5 年及最近一期收入、归母净利润、经营现金流、资本开支及非经常性损益", "判断盈利质量、现金转化与自由现金流"),
        ("资产负债与偿债能力", "现金及受限资金、有息负债、到期结构、利息支出与减值附注", "核验流动性和资产负债表质量"),
        ("估值计算底表", "同一估值日的股价、股本、每股收益、每股净资产及历史/同业估值", "复核估值及安全边际，列明公式、币种和假设"),
    ],
    "industry-researcher": [
        ("行业规模与竞争格局", "行业规模、增长率、主要竞争者份额与统计口径", "判断市场集中度与公司相对位置"),
        ("细分市场与竞争者", "各细分市场需求、可比公司经营指标及竞争策略变化", "核验增长空间与竞争威胁"),
        ("产业链与政策变化", "上下游价格/利润分配、关键技术替代及已公布政策的生效时间", "判断结构变化及对盈利的影响"),
    ],
    "risk-assessor": [
        ("流动性与债务到期", "现金及受限资金、有息负债、未来一年到期额与融资承诺", "评估资金缺口及永久损失风险"),
        ("治理与管理层", "高管履历及持股、关联交易、历史资本配置、分红回购记录", "核验管理层诚信、能力与股东利益一致性"),
        ("或有负债与经营风险", "重大担保、诉讼、监管处罚、减值及海外/汇率敞口", "识别下行情景、触发条件与可能损失"),
    ],
}
_INSURANCE = {
    "business-analyst": [
        ("保险业务构成与利润", "寿险、财险、再保险等分部的保险服务收入、分部利润及同比，注明内部抵销", "拆解利润来源及承保和投资的贡献"),
        ("寿险新业务与渠道质量", "新业务价值及价值率、个险/银保渠道构成、继续率及退保率，注明价值率分母", "判断增长质量、渠道黏性与竞争优势"),
        ("财险承保质量", "境内外财险分地区综合成本率、赔付率、费用率及保费份额", "核验承保盈利及各地区业务竞争力"),
    ],
    "financial-analyst": [
        ("保险盈利与价值底表", "保险服务业绩、净/总投资收益率、合同服务边际、内含价值及假设敏感性", "区分承保利润、投资波动和精算假设的影响"),
        ("偿付能力与资产质量", "主要保险子公司核心/综合偿付能力充足率、投资资产分类及减值", "核验资本充足程度与资产损失风险"),
        _SUGGESTIONS["financial-analyst"][2],
    ],
    "risk-assessor": [
        ("偿付能力与资产负债匹配", "各保险子公司核心/综合偿付能力充足率、资产负债久期、保证利率成本及压力测试", "判断利差损、再投资风险与资本补充压力"),
        _SUGGESTIONS["risk-assessor"][1],
        _SUGGESTIONS["risk-assessor"][2],
    ],
}


@dataclass
class Record:
    heading: str
    cells: list[str]
    headers: tuple[str, ...] = ()

    @property
    def text(self) -> str:
        return " | ".join(self.cells)

    @property
    def subject(self) -> str:
        for index, header in enumerate(self.headers):
            if re.fullmatch(r"(?:缺失/问题)?字段|数据项|指标|项目|待核验项目|缺失字段", header):
                return self.cells[index] if index < len(self.cells) else ""
        return self.cells[0]


def records(body: str) -> list[Record]:
    """Parse tables as rows, retaining headings and column meanings for gap evidence."""
    tokens = _MARKDOWN.parse(body[:120_000])
    result: list[Record] = []
    headings: dict[int, str] = {}
    headers: tuple[str, ...] = ()
    row: list[str] | None = None
    in_header = False
    for index, token in enumerate(tokens):
        if token.type == "table_open":
            headers = ()
        elif token.type == "thead_open":
            in_header = True
        elif token.type == "thead_close":
            in_header = False
        elif token.type == "tr_open":
            row = []
        elif token.type == "tr_close":
            if in_header:
                headers = tuple(row or [])
            elif row:
                result.append(Record(" / ".join(headings.values()), row, headers))
            row = None
        elif token.type == "inline":
            plain = "".join(
                child.content if child.type in {"text", "code_inline"} else "\n"
                if child.type in {"softbreak", "hardbreak"} else ""
                for child in token.children or []
            ).strip()
            previous = tokens[index - 1]
            if previous.type == "heading_open":
                level = int(previous.tag[1])
                headings = {key: value for key, value in headings.items() if key < level}
                headings[level] = plain
            elif row is not None:
                row.append(plain)
            elif plain:
                result.append(Record(" / ".join(headings.values()), [plain]))
    return result


def _portable(text: str) -> str:
    return _LOCAL.sub("原资料", text).strip()[:600]


def _relevant(text: str, role_id: str, *, own: bool) -> bool:
    if re.search(_ROLE_TOPICS[role_id], text, re.I):
        return True
    return own and not any(re.search(pattern, text, re.I) for pattern in _ROLE_TOPICS.values())


def _frameworks() -> dict[str, dict[str, str]]:
    try:
        root, manifest = _manifest(ASSET_RESEARCH_TEAM_ID)
        team_root = _safe_child(root, ASSET_RESEARCH_TEAM_ID)
        source_root = _safe_child(team_root, str(manifest.get("source_root") or ""))
        members = {member["id"]: member for member in _members(manifest.get("members"), source_root)}
        path = _safe_child(source_root, "skills/investment-team.md")
        workflow = path.read_text(encoding="utf-8") if path.is_file() else ""
    except (ExpertTeamError, OSError):
        members, workflow = {}, ""
    tasks = records(workflow)
    result = {}
    for index, role_id in enumerate(MEMBER_NODES, 1):
        member = members.get(role_id, {})
        instructions = member.get("instructions", "")
        candidates = records(instructions) if instructions else [
            item for item in tasks if re.search(rf"任务{index}[：:]", item.heading)
        ]
        lines = []
        for item in candidates:
            line = item.text
            if "金融严谨性验证" in line:
                line = "用可复现的计算核验市值、估值、跨来源误差及三情景假设，列明公式和输入。"
            if (line.startswith(("subject:", "description")) or _OPERATIONS.search(line)
                    or _LOCAL.search(line) or "工具输出" in line):
                continue
            lines.append(line)
        from_team = bool(lines)
        if not from_team:
            lines = [item[2] for item in _SUGGESTIONS[role_id]]
        result[role_id] = {
            "name": member.get("name") or _ROLE_LABELS[role_id],
            "framework": member.get("framework") or _VIEWS[role_id],
            "framework_prompt": "\n".join(f"{i}. {line}" for i, line in enumerate(lines[:20], 1)),
            "framework_source": "团队角色研究要求" if from_team else "角色通用研究框架",
        }
    return result


def _period(items: list[Record]) -> str:
    for item in items:
        for line in item.text.splitlines():
            if re.search(r"数据截止|资料期间|研究期间|报告期|基准日", line) and _DATES.search(line):
                text = line.replace(" | ", "：")
                return _portable(re.split(r"[：:]", text, maxsplit=1)[-1])[:240]
    return "原报告对应期间；未明确时请先注明查询的报告期和数据截止日"


def _identity(target: str, base: list[Record]) -> str:
    # Use only the base package's identity, never codes mentioned in mismatch/peer sections.
    identity = []
    for item in base:
        if (re.search(r"证券身份|基本信息", item.heading)
                and item.cells[0] in {"证券全称", "公司全称", "股票代码", "证券代码", "上市市场"}
                and len(item.cells) > 1):
            identity.append(f"{item.cells[0]}：{_portable(item.cells[1])}")
    if not identity:
        for item in base[:4]:
            if target in item.text and not _OPERATIONS.search(item.text):
                match = re.search(re.escape(target) + r"[（(]([A-Za-z0-9.:-]{2,20})[）)]", item.text)
                if match:
                    identity.append(f"原数据包证券代码：{match[1]}")
                    break
    return "；".join(identity) or "原记录未提供可确认的证券代码，请先核对公司全称、代码及上市市场"


def _requests(role_id: str, own: list[Record], shared: list[Record], period: str,
              insurance: bool) -> list[dict[str, str]]:
    requests: dict[str, dict[str, str]] = {}
    # A newer role report supersedes old shared gaps, including fields it has already verified.
    has_role_content = any(item.headers or _relevant(item.text, role_id, own=False)
                           for item in own if not _OPERATIONS.search(item.text))
    sources = ((own, True),) if has_role_content else ((shared, False),)
    for items, is_own in sources:
        for item in items:
            text = item.text
            is_table = bool(item.headers)
            gap_section = bool(_GAP.search(item.heading) or any(_GAP.search(h) for h in item.headers))
            if not _GAP.search(text) and not (is_table and gap_section):
                continue
            resolved = list(_RESOLVED.finditer(text))
            if resolved and not _GAP.search(text[resolved[-1].end():]):
                continue
            subject = item.subject if is_table else re.split(r"[：:]", text, maxsplit=1)[0]
            if (len(subject) > 90 or not subject or subject.isnumeric() or _OPERATIONS.search(subject)
                    or _LOCAL.search(subject) or not _relevant(subject, role_id, own=is_own)):
                continue
            if not is_table and (subject == text or not _GAP.search(text)):
                continue
            if insurance and re.search(r"销售.*(?:毛利率|净利率)", subject):
                continue
            if any(other in text for other in MEMBER_NODES if other != role_id):
                continue
            field_name = re.sub(r"\s*(?:约|为)?[+-]?\d+(?:\.\d+)?\s*%$", "", subject)
            normalized = re.sub(r"[\W_]+", "", field_name).casefold()
            if normalized in requests and "影响" not in item.headers:
                continue
            columns = dict(zip(item.headers, item.cells))
            purpose = columns.get("影响") or columns.get("用途") or columns.get("影响结论")
            if not purpose or _OPERATIONS.search(purpose) or _LOCAL.search(purpose):
                purpose = _SUGGESTIONS[role_id][0][2]
            scope = (columns.get("期间") or columns.get("报告期")
                     or "；".join(dict.fromkeys(_DATES.findall(subject + " " + item.heading))) or period)
            source = "行业协会/监管机构统计、公司公告及可比公司披露；注明链接、日期与页码" if role_id == "industry-researcher" or re.search(r"份额|市占率", subject) else _SOURCE
            requests[normalized] = {
                "title": _portable(subject), "status": "reported", "period": scope,
                "data": f"补充并核验{_portable(subject)}的原始披露值或事实；注明单位、币种、统计范围及计算口径。未披露的项目请明确列出。",
                "purpose": _portable(purpose), "source": source,
                "basis": "当前角色报告明确待核验" if is_own else "原研究记录明确待核验",
            }
    result = list(requests.values())[:12]
    suggestions = _INSURANCE.get(role_id, _SUGGESTIONS[role_id]) if insurance else _SUGGESTIONS[role_id]
    if not result:
        result = [{
            "title": title, "status": "suggested", "data": data, "period": period,
            "purpose": purpose, "source": _SOURCE if role_id != "industry-researcher" else "行业协会/监管机构统计、公司及可比公司公告；注明链接、日期与页码",
            "basis": "按研究框架建议核验，尚未确认缺失",
        } for title, data, purpose in suggestions]
    return result


def material_guidance(target: str, evidence: dict[str, tuple[str, str]]) -> dict[str, Any]:
    parsed = {key: records(body) for key, (_, body) in evidence.items()
              if key != "report" or not re.match(r"\s*<(?:!DOCTYPE|html)", body, re.I)}
    base = parsed.get("data-package", [])
    report = parsed.get("report-markdown", parsed.get("report", []))
    period = _period(base + report)
    identity = _identity(target, base)
    insurance = "保险" in target + identity
    result: dict[str, Any] = {"research_period": period, "security_identity": identity, "roles": {}}
    for role_id, framework in _frameworks().items():
        own = parsed.get(role_id, [])
        role_period = _period(own + base + report)
        requests = _requests(role_id, own, base + report, role_period, insurance)
        # Context contains only bounded role facts; no raw tool traces or local file references.
        facts = [item.text[:300] for item in own if item.headers
                 and _relevant(item.subject, role_id, own=False)
                 and not _GAP.search(item.text) and not _OPERATIONS.search(item.text)
                 and not _LOCAL.search(item.text)][:5]
        prompt = [
            f"请为 {target} 补充投资研究资料。",
            f"证券身份：{identity}",
            "先核对证券身份；不得把名称相近的公司或其他上市主体的数据混入。",
            f"原报告期间：{role_period}",
            f"研究角色：{framework['name']} · {framework['framework']}",
            "\n研究框架：", framework["framework_prompt"],
            "\n本次资料清单（标为建议核验的项目并不代表原报告缺失）：",
        ]
        for index, item in enumerate(requests, 1):
            prompt.append(
                f"{index}. {item['title']} [{item['basis']}]\n"
                f"   需要：{item['data']}\n   期间：{item['period']}\n"
                f"   用途：{item['purpose']}\n   建议来源：{item['source']}"
            )
        if facts:
            prompt += ["\n原角色记录摘要（仅供定位，仍需核验）：", *facts]
        prompt += [
            "\n交付要求：",
            "1. 优先查询公司/交易所原始披露及监管或行业统计；关键数据尽量用两个独立来源核验。转载同一材料不算独立来源。",
            "2. 输出 Markdown 资料表：数据项 | 数值或事实 | 期间 | 单位、币种及口径 | 来源链接与页码 | 发布日期 | 核验状态。",
            "3. 按上述框架说明新增证据影响哪些判断，区分事实、计算值和推断；计算值列明公式、输入及假设。",
            "4. 来源冲突分别列出；无法找到、未披露、不适用的项目及原因逐项说明。联网失败时如实说明，禁止用训练知识或推测填补。",
            "5. 聚焦本角色的补充与核验，返回可直接粘贴的 Markdown；无须重做完整团队报告。",
        ]
        result["roles"][role_id] = {
            **framework, "material_requests": requests, "research_prompt": "\n".join(prompt),
        }
    return result
