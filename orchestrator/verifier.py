from __future__ import annotations

import enum
from typing import Any

from langchain_core.messages import HumanMessage, SystemMessage
from langchain_litellm import ChatLiteLLM

from orchestrator.primitives import PrimitiveCall

VERIFIER_SYSTEM_PROMPT = """You are a safety verifier for infrastructure automation. Your job is to check whether a proposed action sequence matches the original user request.

You have NO context from the planner. You see only:
1. The original user request
2. The proposed primitive action sequence
3. The cluster topology context

Output exactly one of these on the first line:
MATCH
SCOPE_MISMATCH <reason>
AMBIGUOUS <reason>

Check specifically:
- Does the action sequence do what the user asked? Not more, not less.
- Are the target nodes/groups correct based on the request?
- Does the primitive count/type match the request's apparent complexity?
- Are any actions touching nodes with role=critical without explicit mention?
- Is the request so vague that multiple interpretations are valid?

If the request mentions specific node IDs or groups, verify the sequence targets them.
If the request is vague or incomplete, mark AMBIGUOUS.
If the sequence does something the user didn't ask for, mark SCOPE_MISMATCH.
If everything looks correct, mark MATCH."""


class VerifierResult(str, enum.Enum):
    MATCH = "MATCH"
    SCOPE_MISMATCH = "SCOPE_MISMATCH"
    AMBIGUOUS = "AMBIGUOUS"


def verify_primitive_sequence(
    original_request: str,
    primitives: list[PrimitiveCall],
    topology_context: str,
    llm: ChatLiteLLM | None = None,
) -> tuple[VerifierResult, str]:
    if llm is None:
        return VerifierResult.MATCH, "No verifier LLM configured; assuming MATCH"

    primitive_lines = "\n".join(
        f"  {i}. {p.action}({', '.join(f'{k}={v!r}' for k, v in p.params.items())})"
        for i, p in enumerate(primitives)
    )

    user_message = (
        f"## Original Request\n{original_request}\n\n"
        f"## Proposed Action Sequence\n{primitive_lines}\n\n"
        f"## Cluster Topology\n{topology_context}\n"
    )

    try:
        response = llm.invoke([
            SystemMessage(content=VERIFIER_SYSTEM_PROMPT),
            HumanMessage(content=user_message),
        ])
        text = response.content.strip() if hasattr(response, "content") else str(response).strip()
    except Exception as e:
        return VerifierResult.AMBIGUOUS, f"Verifier LLM error: {e}"

    first_line = text.split("\n", 1)[0].strip()
    reason = text[len(first_line):].strip() if len(text) > len(first_line) else ""

    for result in VerifierResult:
        if first_line.startswith(result.value):
            return result, reason

    return VerifierResult.AMBIGUOUS, f"Unrecognized verifier output: {text[:200]}"


def format_primitives_for_verifier(primitives: list[PrimitiveCall]) -> str:
    return "\n".join(
        f"{i}. {p.action}({', '.join(f'{k}={v!r}' for k, v in p.params.items())})"
        for i, p in enumerate(primitives)
    )
