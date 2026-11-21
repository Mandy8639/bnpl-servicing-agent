"""The agent's mandate: what it may do, how risky each action is, and when it must stop.

This module is pure data. The guardrail (``bnpl.guardrail``) enforces it, and the
README and dashboard render it, so the spec and the enforcement cannot drift apart.
"""

from dataclasses import dataclass
from datetime import timedelta
from enum import StrEnum


class Action(StrEnum):
    NUDGE = "nudge"
    OFFER_PLAN = "offer_plan"
    ESCALATE_COLLECTIONS = "escalate_collections"
    HOLD_FOR_REVIEW = "hold_for_review"
    NO_ACTION = "no_action"


class Risk(StrEnum):
    LOW = "Low"
    MEDIUM = "Medium"
    HIGH = "High"
    NONE = "—"


@dataclass(frozen=True)
class ActionRule:
    action: Action
    meaning: str
    risk: Risk
    handling: str


AGENT_ACTOR = "agent"
ESCALATION_OWNER = "servicing lead"
REVIEW_SLA = timedelta(hours=24)

NUDGES_PER_CUSTOMER_PER_DAY = 1
MAX_PLAN_EXTENSION_MONTHS = 3

CONTROL_WINDOW = 100
CONTROL_MIN_SAMPLE = 20
MAX_AUTO_ACTION_RATE = 0.80
MAX_OFFER_PLAN_RATE = 0.25
MAX_CONSECUTIVE_BLOCKS = 3

MANDATE: tuple[ActionRule, ...] = (
    ActionRule(
        Action.NUDGE,
        "Send a payment reminder to the customer",
        Risk.LOW,
        f"Auto, at most {NUDGES_PER_CUSTOMER_PER_DAY} per customer per day",
    ),
    ActionRule(
        Action.OFFER_PLAN,
        "Offer a revised payment plan (longer term / lower instalment)",
        Risk.MEDIUM,
        f"Auto only inside bounds (term extension ≤ {MAX_PLAN_EXTENSION_MONTHS} months, "
        "no fee waiver); otherwise HOLD",
    ),
    ActionRule(
        Action.ESCALATE_COLLECTIONS,
        "Hand the case to human collections",
        Risk.HIGH,
        f"Always HOLD for the {ESCALATION_OWNER}",
    ),
    ActionRule(
        Action.HOLD_FOR_REVIEW,
        "Agent abstains — the case is ambiguous",
        Risk.NONE,
        "Goes to the review queue",
    ),
    ActionRule(
        Action.NO_ACTION,
        "Case is resolved or a false alarm",
        Risk.LOW,
        "Auto, with a logged reason, only when nothing is overdue on the record; otherwise HOLD",
    ),
)


# --- Merchant report agent -------------------------------------------------------------


class ReportAction(StrEnum):
    NO_CHANGE = "no_change"
    MONITOR = "monitor"
    REVIEW_WITH_MERCHANT = "review_with_merchant"
    ESCALATE_TO_RISK = "escalate_to_risk"


class RiskLevel(StrEnum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


@dataclass(frozen=True)
class ReportRule:
    action: ReportAction
    meaning: str
    risk: Risk
    handling: str


REPORT_OWNER = "portfolio risk lead"
REPORT_MEDIUM_DELINQUENCY = 0.05
REPORT_HIGH_DELINQUENCY = 0.20
REPORT_HIGH_PLAN_UPTAKE = 0.25
REPORT_DISPUTES_FOR_REVIEW = 2
REPORT_FRAUD_FLAGS_FOR_ESCALATION = 2

REPORT_MANDATE: tuple[ReportRule, ...] = (
    ReportRule(
        ReportAction.NO_CHANGE,
        "Portfolio is healthy; publish the summary",
        Risk.LOW,
        "Auto-publish if every number is grounded",
    ),
    ReportRule(
        ReportAction.MONITOR,
        "Watch a rising metric next week",
        Risk.LOW,
        "Auto-publish if every number is grounded",
    ),
    ReportRule(
        ReportAction.REVIEW_WITH_MERCHANT,
        "Raise disputes or arrears with the merchant",
        Risk.MEDIUM,
        f"Always HOLD for the {REPORT_OWNER}: it is an external conversation",
    ),
    ReportRule(
        ReportAction.ESCALATE_TO_RISK,
        "Escalate the merchant to portfolio risk",
        Risk.HIGH,
        f"Always HOLD for the {REPORT_OWNER}",
    ),
)
