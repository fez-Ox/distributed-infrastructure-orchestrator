from __future__ import annotations

from typing import Any

from langgraph.checkpoint.memory import MemorySaver
from langgraph.graph import END, StateGraph

from orchestrator.models import AgentState, Intent, Plan


ALLOWED_TYPES = [
    ("orchestrator.models", "ActionType"),
    ("orchestrator.models", "Intent"),
    ("orchestrator.models", "Plan"),
    ("orchestrator.models", "WorkflowStep"),
    ("orchestrator.models", "NodeSelector"),
    ("orchestrator.models", "TaskStatus"),
    ("orchestrator.models", "PlanStatus"),
    ("orchestrator.models", "StepStatus"),
]


def create_graphs(checkpointer: MemorySaver, nodes: Any) -> tuple[StateGraph, StateGraph]:
    checkpointer = checkpointer.with_allowlist(ALLOWED_TYPES)
    template = StateGraph(AgentState)
    template.add_node("plan", nodes.plan_node)
    template.add_node("validate", nodes.validate_node)
    template.add_node("render", nodes.render_node)
    template.add_node("syntax_check", nodes.syntax_check_node)
    template.add_node("execute", nodes.execute_node)

    template.set_entry_point("plan")
    template.add_edge("plan", "validate")
    template.add_conditional_edges(
        "validate",
        nodes.route_after_validate,
        {"render": "render", "__end__": END},
    )
    template.add_edge("render", "syntax_check")
    template.add_conditional_edges(
        "syntax_check",
        nodes.route_after_syntax,
        {"execute": "execute", "__end__": END},
    )
    template.add_edge("execute", END)

    template_graph = template.compile(
        checkpointer=checkpointer,
        name="template_graph",
    )

    freeform = StateGraph(AgentState)
    freeform.add_node("plan_workflow", nodes.plan_workflow_node)
    freeform.add_node("execute_workflow", nodes.execute_workflow_node)

    freeform.set_entry_point("plan_workflow")
    freeform.add_conditional_edges(
        "plan_workflow",
        nodes.route_after_workflow_plan,
        {"execute_workflow": "execute_workflow", "__end__": END},
    )
    freeform.add_edge("execute_workflow", END)

    freeform_graph = freeform.compile(
        checkpointer=checkpointer,
        name="freeform_graph",
    )

    return template_graph, freeform_graph
