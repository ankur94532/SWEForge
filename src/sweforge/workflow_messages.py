"""Deterministic application-owned workflow interaction messages."""

import hashlib

UNSOLICITED_ACK_MESSAGE = (
    "Got it. I've saved this suggestion. SWEForge is currently working on the "
    "active workflow, so I won't interrupt the work already in progress. I'll "
    "review this suggestion at the next safe revision boundary and incorporate "
    "it if applicable."
)

PLAN_FOOTER = (
    "Reply with `@agent approve` to execute this exact plan, or reply with "
    "feedback specifically related to this plan.\n\n"
    "Please wait until the current workflow completes before sending separate "
    "requests. If you send a separate suggestion now, SWEForge will save it for "
    "the follow-up revision loop."
)

RESULT_FOOTER = (
    "Reply with `@agent approve` to accept this validated result, or reply with "
    "feedback specifically related to this implementation.\n\n"
    "Separate requests will be saved for the follow-up revision loop rather than "
    "changing the current result."
)

PLAN_DEFERRED_FEEDBACK_MESSAGE = (
    "This suggestion is separate from the current plan, so the active plan has "
    "not been changed.\n\n"
    "Please reply with `@agent approve` to proceed with the current plan, or send "
    "feedback specifically related to this plan.\n\n"
    "I've saved the separate suggestion and will review it in the follow-up "
    "revision loop after the current workflow completes."
)

RESULT_DEFERRED_FEEDBACK_MESSAGE = (
    "This suggestion is separate from the current validated result, so the "
    "current result has not been changed.\n\n"
    "Please reply with `@agent approve` to accept the current result, or send "
    "feedback specifically related to this implementation.\n\n"
    "I've saved the separate suggestion and will review it in the follow-up "
    "revision loop after the current workflow completes."
)


def revision_ack_marker(revision_input_id: str) -> str:
    return f"<!-- sweforge:revision-input-ack:{revision_input_id} -->"


def deferred_feedback_marker(feedback_review_id: str) -> str:
    return f"<!-- sweforge:feedback-deferred:{feedback_review_id} -->"


def outbox_id_for(*, source_event_key: str, message_kind: str) -> str:
    digest = hashlib.sha256(f"{source_event_key}\0{message_kind}".encode()).hexdigest()[
        :24
    ]
    return f"workflow-message-{digest}"
