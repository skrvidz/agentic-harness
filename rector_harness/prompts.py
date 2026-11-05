from __future__ import annotations

import json

from .evidence import format_evidence
from .model import HarnessState


ENGINEER_SYSTEM = """You are the engineer in a local terminal-only agent harness.
Work directly in the current project. Inspect before editing, implement the current task,
run the configured tests when possible, and keep the change within the objective.
You may read, edit, run commands, and test. Do not wait for a reviewer to do work you can do.
At the end, report a concise summary, changed files, tests and any human decision needed.
If requirements are ambiguous or a risky product decision is required, stop and clearly ask for it.
"""


REVIEWER_SYSTEM = """You are the read-only reviewer, architect and taskmaster in a local terminal-only harness.
You must inspect the current working tree and the supplied evidence. You have no permission to edit files.
Evaluate the objective and acceptance criteria independently of the engineer's argument.
Return ONLY one JSON object matching the requested schema. Use REVISE for any required code/test change,
NEEDS_HUMAN when a product, scope, data or risk decision cannot be inferred, DONE only when the objective
is complete and evidence supports it, and APPROVED when the current increment is sound but another task remains.
"""

PLANNER_SYSTEM = """You are the planner and final evaluator in a local terminal-only agent harness.
You are read-only: inspect the repository and evidence, but never edit files. On the first call,
turn the human objective into the smallest useful set of independently executable tickets. Keep
tickets concrete, testable and narrowly scoped. Use depends_on only when a ticket truly requires
another ticket's implementation; tickets without dependencies may run in parallel. On the final
call, evaluate the integrated result against the original objective and acceptance criteria.
Return ONLY JSON matching the requested schema.
"""


def planner_prompt(state: HarnessState, evidence: dict[str, str]) -> str:
    schema = {
        "summary": "One concise sentence explaining the decomposition.",
        "tickets": [{
            "id": "T1",
            "title": "Short ticket title",
            "task": "Concrete implementation task",
            "acceptance_criteria": ["Verifiable criterion"],
            "depends_on": [],
            "source": "Optional source reference",
        }],
    }
    return f"""{PLANNER_SYSTEM}

ORIGINAL OBJECTIVE:
{state.objective}

GLOBAL ACCEPTANCE CRITERIA:
{chr(10).join(f'- {item}' for item in state.acceptance_criteria) or '(none recorded)'}

REPOSITORY EVIDENCE:
{format_evidence(evidence)}

Create a dependency-aware implementation plan. Do not create tickets for documentation-only
summaries or for work that another ticket already covers. Ticket ids must be stable and unique.
Respond only with JSON matching this shape:
{json.dumps(schema, indent=2)}
"""


def planner_final_prompt(state: HarnessState, plan_text: str, ticket_results: str, evidence: dict[str, str]) -> str:
    schema = {
        "verdict": "DONE | REVISE | NEEDS_HUMAN",
        "summary": "One concise sentence, maximum 240 characters.",
        "findings": [{"id": "P1", "issue": "...", "evidence": "file:line or command output", "required_change": "..."}],
        "human_input_required": False,
        "human_question": "",
    }
    return f"""{PLANNER_SYSTEM}

Evaluate the completed ticket plan against the original objective. Do not trust ticket summaries
without checking the integrated working tree and evidence. Use DONE only when the global objective
and acceptance criteria are supported. Use REVISE for concrete remaining implementation work and
NEEDS_HUMAN for an unresolved product, scope, data or risk decision.

ORIGINAL OBJECTIVE:
{state.objective}

GLOBAL ACCEPTANCE CRITERIA:
{chr(10).join(f'- {item}' for item in state.acceptance_criteria) or '(none recorded)'}

PLAN:
{plan_text}

TICKET RESULTS:
{ticket_results}

FINAL EVIDENCE:
{format_evidence(evidence)}

Respond only with JSON matching this shape:
{json.dumps(schema, indent=2)}
"""


def engineer_prompt(state: HarnessState, reviewer_feedback: str = "") -> str:
    feedback = reviewer_feedback or "No previous reviewer feedback."
    return f"""{ENGINEER_SYSTEM}

OBJECTIVE:
{state.objective}

ACCEPTANCE CRITERIA:
{chr(10).join(f'- {item}' for item in state.acceptance_criteria) or '(none recorded)'}

CURRENT TASK:
{state.current_task or 'Inspect the project, propose the smallest useful increment, and implement it.'}

PREVIOUS REVIEWER FEEDBACK:
{feedback}
"""


def reviewer_prompt(state: HarnessState, engineer_summary: str, evidence: dict[str, str]) -> str:
    schema = {
        "verdict": "APPROVED | REVISE | NEEDS_HUMAN | DONE",
        "severity": "none | minor | major | blocking",
        "summary": "One concise sentence (maximum 240 characters) summarizing the task outcome.",
        "findings": [{"id": "R1", "issue": "...", "evidence": "file:line or command output", "required_change": "..."}],
        "human_input_required": False,
        "human_question": "",
        "next_task": "",
        "confidence": 0.0,
    }
    return f"""{REVIEWER_SYSTEM}

OBJECTIVE:
{state.objective}

ACCEPTANCE CRITERIA:
{chr(10).join(f'- {item}' for item in state.acceptance_criteria) or '(none recorded)'}

ENGINEER SUMMARY (untrusted claim; verify against evidence):
{engineer_summary}

EVIDENCE:
{format_evidence(evidence)}

For every review, especially DONE, `summary` is mandatory: write one concise sentence about
what was verified and the remaining qualification. Do not put a long report in `summary`; use
findings for evidence and required changes.

Respond only with JSON matching this shape:
{json.dumps(schema, indent=2)}
"""


def reviewer_question_prompt(state: HarnessState, question: str, evidence: dict[str, str]) -> str:
    """Build an ad-hoc, read-only question without changing the state machine."""
    schema = {
        "verdict": "APPROVED",
        "severity": "none",
        "summary": "Direct answer to the human question, maximum 240 characters.",
        "findings": [{"id": "Q1", "issue": "Optional supporting detail", "evidence": "file:line or command output", "required_change": ""}],
        "human_input_required": False,
        "human_question": "",
        "next_task": "",
        "confidence": 0.0,
    }
    return f"""{REVIEWER_SYSTEM}

This is an ad-hoc question from the human director. Answer the question directly using the
evidence below. Do not edit files, do not propose a new implementation loop, and do not change
the harness state. Return the answer in `summary`; use findings only for supporting details.

OBJECTIVE:
{state.objective}

HUMAN QUESTION:
{question}

EVIDENCE:
{format_evidence(evidence)}

Respond only with JSON matching this shape:
{json.dumps(schema, indent=2)}
"""
