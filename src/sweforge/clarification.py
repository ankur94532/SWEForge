"""Bounded provider-neutral semantic clarification classification."""

import json
from typing import Literal

from langchain.chat_models import init_chat_model
from pydantic import BaseModel, Field


class ClarificationClassification(BaseModel):
    relationship: Literal[
        "ANSWERS_CLARIFICATION",
        "CHANGES_SCOPE",
        "UNRELATED_FOLLOWUP",
        "AMBIGUOUS",
    ]
    extracted_answer: str = Field(default="", max_length=4_000)
    residual_followup: str = Field(default="", max_length=4_000)


_PROMPT = """Classify one user reply to a pending software-engineering clarification.
Return only the structured result. Do not infer repository facts or execute tools.
ANSWERS_CLARIFICATION means the reply answers the question; extract only the answer.
CHANGES_SCOPE means it changes or adds requested work.
UNRELATED_FOLLOWUP means separate work that should be deferred.
AMBIGUOUS means it cannot safely be classified.

Question: {question}
Answer type: {answer_type}
Choices: {choices}
User reply: {reply}
Source provenance: {provenance}
"""


def build_clarification_classifier(model: str):
    """Build a no-tools structured classifier; model invocation is bounded."""
    classifier = init_chat_model(
        model, max_retries=0, timeout=60
    ).with_structured_output(ClarificationClassification)

    def classify(*, clarification, event: dict) -> dict:
        response = classifier.invoke(
            _PROMPT.format(
                question=clarification.question[:4_000],
                answer_type=clarification.answer_type,
                choices=clarification.choices_json[:2_000],
                reply=str(event.get("body", ""))[:4_000],
                provenance=json.dumps(
                    {
                        "event_key": event.get("event_key"),
                        "origin_surface": event.get("origin_surface"),
                        "subject_number": event.get("subject_number"),
                    },
                    sort_keys=True,
                ),
            )
        )
        if isinstance(response, ClarificationClassification):
            return response.model_dump()
        return ClarificationClassification.model_validate(response).model_dump()

    return classify
