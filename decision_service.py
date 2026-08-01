from dataclasses import dataclass


VALID_RISK_STATUSES = {"PASS", "REVIEW", "BLOCK"}


@dataclass
class FinalDecision:
    ticker: str
    status: str
    automatic_paper_eligible: bool
    reasons: list[str]


def evaluate_final_decision(
    ticker: str,
    technical_passed: bool,
    options_passed: bool,
    risk_plan_passed: bool,
    news_status: str | None,
    earnings_status: str | None,
    market_session_passed: bool = True,
) -> FinalDecision:
    blocking_reasons = []
    review_reasons = []

    if not technical_passed:
        blocking_reasons.append("Technical setup did not pass 4/4")

    if not options_passed:
        blocking_reasons.append(
            "Options liquidity or affordability did not pass"
        )

    if not risk_plan_passed:
        blocking_reasons.append("CTS risk plan did not pass")

    if not market_session_passed:
        blocking_reasons.append("CTS entry window is closed")

    if news_status not in VALID_RISK_STATUSES:
        blocking_reasons.append("News risk data is unavailable")
    elif news_status == "BLOCK":
        blocking_reasons.append("News risk gate returned BLOCK")
    elif news_status == "REVIEW":
        review_reasons.append("News requires human or AI review")

    if earnings_status not in VALID_RISK_STATUSES:
        blocking_reasons.append("Earnings risk data is unavailable")
    elif earnings_status == "BLOCK":
        blocking_reasons.append("Earnings risk gate returned BLOCK")
    elif earnings_status == "REVIEW":
        review_reasons.append("Earnings date requires verification")

    if blocking_reasons:
        status = "BLOCK"
        reasons = blocking_reasons + review_reasons
    elif review_reasons:
        status = "REVIEW"
        reasons = review_reasons
    else:
        status = "PASS"
        reasons = ["Every CTS technical and risk gate passed"]

    return FinalDecision(
        ticker=ticker,
        status=status,
        automatic_paper_eligible=(status == "PASS"),
        reasons=reasons,
    )


def show_final_decision(decision: FinalDecision) -> None:
    print(f"\n{decision.ticker} FINAL CTS DECISION: {decision.status}")

    for reason in decision.reasons:
        print(f"- {reason}")

    if decision.automatic_paper_eligible:
        print("Eligible for a future paper-order simulation only.")
    elif decision.status == "REVIEW":
        print("Not eligible until the review is resolved.")
    else:
        print("Not eligible for a paper-order simulation.")

    print("Decision only. No order was submitted.")
