"""Production selection contract, independent from the frozen v1/v2 experiments."""

import json

from huipi_cloud.modules.grading import prompts_v4
from huipi_cloud.modules.grading.protocol import GradeInputDocument

PROMPT_VERSION = "rubric-candidate-quote-selection-v1"
LOCATOR_VERSION = "segment-unique-exact-v1"

# Preserve v4's scoring semantics; replace only its offset-specific evidence rules.
SYSTEM_PROMPT = (
    prompts_v4.SYSTEM_PROMPT[: prompts_v4.SYSTEM_PROMPT.index("quote对象必须")].replace(
        "正分项至少有一条有效quote。", "正分项至少有一条有效quote_selection。"
    )
    + """
证据使用kind=quote_selection，且只能包含kind、segment_index、quote三个字段。
不要输出text_start或text_end。Python负责计算偏移，你只选择学生逐字原文。
quote必须在指定segment_index的text内恰好出现一次，保留空格、标点、大小写和Unicode字符。
不得跨片段查找、改写、删空格或用标准答案补写学生证据。重复时选择足够长的原文消歧；
无法唯一选择必要证据时abstain，不能猜测位置。相同文字可在不同片段出现，但必须指定正确片段。
no_evidence只能包含kind、reason两个字段，禁止附加片段、偏移或quote。正分不能只有no_evidence。
一般答错或缺步骤按明确Rubric评分；必要来源不确定时弃权，不自动当作学生零分。
找到原文不等于原文支持给分，仍需检查内容正确、条件完整、理由与分数一致。
只返回一个JSON对象，无Markdown、注释、额外字段。每项恰好一次，总分为各项Decimal精确和。
"""
)


def build_user_prompt(document: GradeInputDocument, *, retry_reason: str | None = None) -> str:
    payload = json.loads(prompts_v4.build_user_prompt(document))
    contract = payload["response_contract"]
    contract["schema_name"] = "huipi.grading.quote-selection"
    contract["candidate"]["required_fields"].append("schema_name")
    contract["evidence_references"].pop("quote")
    contract["evidence_references"]["quote_selection"] = {
        "kind": "quote_selection",
        "required_fields": ["kind", "segment_index", "quote"],
        "additional_fields_allowed": False,
        "rule": "指定学生片段内唯一逐字原文；禁止偏移、改写和跨片段匹配",
    }
    contract["abstain"]["schema_name"] = "huipi.grading.quote-selection"
    payload["task"] = "按Rubric逐项评分，选择指定学生片段内唯一原文；不能可靠判断时弃权。"
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
