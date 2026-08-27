from __future__ import annotations

from typing import Any

import httpx

from dsh_novel.domain import OutlineResult, ReviewVerdict
from dsh_novel.providers.base import (
    ExtractionRequest,
    OutlineRequest,
    ReviewRequest,
    WriterRequest,
    parse_extraction_payload,
    parse_outline_payload,
    parse_review_payload,
)
from dsh_novel.util import canonical_json

# Output cap for the review call only; verdicts are small JSON objects, but
# the model can waste tokens re-stating the task, so give it generous room
# (capped by the global model_max_output_tokens in the payload).
_REVIEW_MAX_TOKENS = 8192

EXTRACTION_SYSTEM_PROMPT = (
    "你是中文小说章节状态抽取器。读取一章已定稿正文，抽取续写所需的结构化核心信息。"
    "只报告正文里真实发生的内容，不得臆造。逐项输出："
    "1) character_changes：本章人物关系或状态的实质变化，每项含 "
    "{character（人物名）, before（章节开始时的状态）, after（章节结束时的状态）}；"
    "2) hooks_status：本章每个伏笔的真实状态，每项含 "
    "{hook（伏笔内容）, status（planted/advanced/resolved 之一）, evidence（正文依据，一句话）}；"
    "3) twist：本章反转/转折点，无则空字符串；"
    "4) next_chapter_hook：章末留下的人物状态或悬念（下一章续写锚点），无则空字符串。"
    "直接输出一个 JSON 对象，禁止任何解释、分析或标签。JSON 格式："
    '{"character_changes":[{"character":"...","before":"...","after":"..."}],'
    '"hooks_status":[{"hook":"...","status":"planted","evidence":"..."}],'
    '"twist":"...","next_chapter_hook":"..."}'
)

REVIEW_SYSTEM_PROMPT = (
    "你是严格的中文小说审稿编辑，在章节定稿前对照全书蓝图审查单章正文。逐项核对："
    "1) 合同契合：本章是否完成合同 purpose；required_events 是否全部覆盖；"
    "hooks_to_plant 与 hooks_to_advance 的伏笔种植/推进是否符合规划；章末衔接是否落实；"
    "2) 蓝图一致：人物关系与性格是否与已定稿章节及蓝图设定一致；"
    "本章反转或转折是否落在蓝图规划的位置；"
    "3) 年代质感：器物、称谓、语言风格是否符合故事年代与社会环境；"
    "4) 叙事流畅：视角稳定、节奏合理、无机械重复的对话循环、无整段复述前文、结尾不残缺；"
    "5) 对话流水账：禁止连续八行以上一问一答的短促对话堆砌（如“嗯。”“疼吗？”“不疼。”），"
    "若出现则必须判 blocked，要求作者把短对话融进动作、神态、环境描写组成的叙事段落。"
    "直接输出一个 JSON 对象。禁止输出任何解释、分析、思考过程、复述任务或标签，"
    "JSON 必须是回复的全部内容。JSON 格式："
    '{"verdict":"pass或blocked",'
    '"issues":[{"severity":"blocker或warning","type":"问题类型","description":"具体说明"}],'
    '"scores":{"contract_adherence":0到10,"era_authenticity":0到10,"flow":0到10}}。'
    "仅当存在必须重写整章才能修复的严重问题时才给 blocked，"
    "且此时 issues 至少包含一条 severity=blocker；轻微问题记 warning 并给 pass。"
)

OUTLINE_SYSTEM_PROMPT = (
    "你是严格的中文长篇小说大纲编辑。依据书名、前提、硬性规则与目标章节数，"
    "产出全书结构化大纲。要求：1) story_spine 概括核心冲突、阶段划分与结局约束；"
    "2) chapters 必须覆盖全部目标章节，chapter_number 从 1 开始连续编号到 N；"
    "3) 每章给出 title（章节标题）、purpose（本章叙事目的）、"
    "required_events（本章必须发生的事件）、hooks_to_plant（本章要埋设的伏笔）、"
    "hooks_to_advance（本章要推进的已有伏笔）、target_words（本章目标字数，整数）、"
    "characters（本章涉及的人物及其关系/状态，如“赵峥（壮熊班长，外表钢铁直男）”、"
    "“我（小熊新兵）”，含人物关系在章节中的变化）、"
    "twist（本章反转/转折点，可为空字符串）、"
    "handoff（章末衔接：本章结尾如何导向下一章，可为空字符串）。"
    "只输出一个 JSON 对象，禁止解释、分析、思考过程或任何标签。JSON 格式："
    '{"story_spine":{"central_conflict":"...","acts":["..."],"ending_constraint":"..."},'
    '"chapters":[{"chapter_number":1,"title":"...","purpose":"...",'
    '"required_events":["..."],"hooks_to_plant":["..."],"hooks_to_advance":["..."],'
    '"target_words":4000,"characters":["..."],"twist":"...","handoff":"..."}]}'
)


class OpenAICompatibleProvider:
    name = "openai_compatible"

    def __init__(
        self,
        *,
        endpoint: str,
        model: str,
        api_key: str | None,
        timeout_seconds: float,
        max_output_tokens: int,
        review_timeout_seconds: float = 120.0,
        outline_timeout_seconds: float = 180.0,
    ) -> None:
        self.endpoint = endpoint.rstrip("/")
        self.model = model
        self.api_key = api_key
        self.timeout_seconds = timeout_seconds
        self.max_output_tokens = max_output_tokens
        self.review_timeout_seconds = review_timeout_seconds
        self.outline_timeout_seconds = outline_timeout_seconds

    def _headers(self) -> dict[str, str]:
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        return headers

    def _chat_completion(self, payload: dict[str, Any], timeout_seconds: float) -> str:
        """POST one chat completion and return the message content."""
        try:
            with httpx.Client(timeout=timeout_seconds) as client:
                response = client.post(
                    f"{self.endpoint}/chat/completions",
                    headers=self._headers(),
                    json=payload,
                )
                response.raise_for_status()
                data = response.json()
            content = data["choices"][0]["message"]["content"]
        except (httpx.HTTPError, KeyError, IndexError, TypeError, ValueError) as exc:
            raise RuntimeError(f"model request failed: {exc}") from exc
        if not isinstance(content, str) or not content.strip():
            raise RuntimeError("model returned empty response")
        return content.strip()

    def generate_chapter(self, request: WriterRequest) -> str:
        context_text = "\n\n".join(
            f"[{block.kind}]\n{block.content}" for block in request.context.blocks
        )
        system_content = (
            "你是小说正文写作者。只输出本章正文，不输出分析、系统说明、JSON、"
            "提示词标签或审稿意见。严格遵循章节合同，不重复已有段落。"
            "禁止连续多行一问一答的短促对话堆砌（如“嗯。”“疼吗？”“不疼。”）；"
            "对话必须融进动作、神态、环境描写组成的叙事段落。"
        )
        user_content = (
            f"小说：{request.project_title}\n"
            f"目标字数：约{request.contract.target_words}字\n\n{context_text}"
        )
        if request.revision_feedback:
            # 0.5.0 targeted revision: surface the previous draft's blocking
            # review comments and scores so the rewrite addresses them.
            lines = "\n".join(
                f"- [{item.get('type', 'unknown')}] {item.get('description', '')}".strip()
                for item in request.revision_feedback
            )
            feedback_section = f"上一稿审稿意见，本次必须针对性解决：\n{lines}"
            if request.previous_scores:
                scores_text = ", ".join(
                    f"{name}={value}" for name, value in request.previous_scores.items()
                )
                feedback_section += f"\n上一稿分数：{scores_text}（本次须逐项提高）"
            user_content += f"\n\n{feedback_section}"
            system_content += (
                "用户消息中若附有上一稿审稿意见，必须逐条针对性解决后再输出正文。"
            )
        payload = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system_content},
                {"role": "user", "content": user_content},
            ],
            "temperature": 0.8,
            "max_tokens": self.max_output_tokens,
        }
        return self._chat_completion(payload, self.timeout_seconds)

    def review_chapter(self, request: ReviewRequest) -> ReviewVerdict:
        """Ask the model for a strict JSON review verdict of one chapter."""
        contract_lines = [
            f"小说：{request.project_title}",
            f"章节：第{request.contract.chapter_number}章《{request.contract.title}》",
            f"章节目的：{request.contract.purpose}",
        ]
        if request.contract.required_events:
            contract_lines.append("必须事件：" + "、".join(request.contract.required_events))
        if request.contract.hooks_to_plant:
            contract_lines.append("需埋设伏笔：" + "、".join(request.contract.hooks_to_plant))
        if request.contract.hooks_to_advance:
            contract_lines.append("需推进伏笔：" + "、".join(request.contract.hooks_to_advance))
        if request.contract.required_state_changes:
            contract_lines.append(
                "必须状态变化：" + canonical_json(request.contract.required_state_changes)
            )
        if request.contract.forbidden_changes:
            contract_lines.append(
                "禁止发生的变化：" + canonical_json(request.contract.forbidden_changes)
            )
        if request.contract.handoff:
            contract_lines.append(f"章末衔接：{request.contract.handoff}")
        contract_lines.append(f"目标字数：约{request.contract.target_words}字")

        sections = ["\n".join(contract_lines)]
        if request.blueprint:
            sections.append("全书蓝图（story spine 摘要）：\n" + canonical_json(request.blueprint))
        if request.recent_chapters:
            digests = "\n".join(
                f"第{item.get('chapter_number', '?')}章摘要：{item.get('digest', '')}"
                for item in request.recent_chapters
            )
            sections.append(f"最近章节摘要（用于查重、人物一致性与衔接判断）：\n{digests}")
        sections.append(f"待审正文：\n{request.content}")

        payload = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": REVIEW_SYSTEM_PROMPT},
                {"role": "user", "content": "\n\n".join(sections)},
            ],
            "temperature": 0.2,
            "max_tokens": min(self.max_output_tokens, _REVIEW_MAX_TOKENS),
        }
        content = self._chat_completion(payload, self.review_timeout_seconds)
        return parse_review_payload(content)

    def extract_chapter_state(self, request: ExtractionRequest) -> dict[str, Any]:
        """Ask the model to extract structured core info from a finalised chapter.

        Produces the *real* per-chapter state the continuation mechanism needs:
        character relationship changes, hook status transitions and the twist.
        Fail-open: on any error return an empty extraction (service falls back
        to contract-echo deltas) so a long run is never blocked by this step.
        """
        sections = [
            f"小说：{request.project_title}",
            f"章节：第{request.contract.chapter_number}章《{request.contract.title}》",
            f"章节目的：{request.contract.purpose}",
            f"本章人物蓝图：{'、'.join(request.contract.characters) or '（未提供）'}",
            f"本章反转计划：{request.contract.twist or '（无）'}",
            f"本章伏笔（种植/推进）："
            f"{'、'.join(request.contract.hooks_to_plant + request.contract.hooks_to_advance) or '（无）'}",
        ]
        if request.previous_delta:
            prev_chars = request.previous_delta.get("character_changes") or []
            prev_hooks = request.previous_delta.get("hooks_status") or []
            if prev_chars or prev_hooks:
                sections.append(
                    "上一章结束时的核心状态（用于判断本章相对变化）：\n"
                    + canonical_json({"character_changes": prev_chars, "hooks_status": prev_hooks})
                )
        sections.append(f"本章正文：\n{request.content}")

        payload = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": EXTRACTION_SYSTEM_PROMPT},
                {"role": "user", "content": "\n\n".join(sections)},
            ],
            "temperature": 0.2,
            "max_tokens": min(self.max_output_tokens, _REVIEW_MAX_TOKENS),
        }
        raw = self._chat_completion(payload, self.review_timeout_seconds)
        try:
            return parse_extraction_payload(raw)
        except ValueError:
            # Fail-open: the pipeline must never stall on extraction.
            return {
                "character_changes": [],
                "hooks_status": [],
                "twist": "",
                "next_chapter_hook": "",
            }

    def generate_outline(self, request: OutlineRequest) -> OutlineResult:
        """Generate the whole-book structured outline; retries parsing once."""
        rules = "\n".join(f"- {rule}" for rule in request.hard_rules) or "-（无）"
        user_content = (
            f"书名：{request.title}\n"
            f"前提：{request.premise or '（未提供）'}\n"
            f"硬性规则：\n{rules}\n"
            f"目标章节数：{request.target_chapters}\n"
            f"每章目标字数：约{request.target_words}字\n\n"
            "请输出覆盖全部章节的结构化大纲 JSON。"
        )
        payload = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": OUTLINE_SYSTEM_PROMPT},
                {"role": "user", "content": user_content},
            ],
            "temperature": 0.4,
            "max_tokens": self.max_output_tokens,
        }
        problems: list[str] = []
        for attempt_index in (1, 2):
            try:
                raw = self._chat_completion(payload, self.outline_timeout_seconds)
                return parse_outline_payload(raw, target_chapters=request.target_chapters)
            except (RuntimeError, ValueError) as exc:
                problems.append(f"attempt {attempt_index}: {exc}")
        raise RuntimeError(
            "outline generation failed after one retry: " + " | ".join(problems)
        )
