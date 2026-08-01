from dataclasses import dataclass

from exit_service import round_up_to_cent


MAX_CONTRACT_COST = 150.0
QUANTITY = 1
ORDER_TYPE = "LIMIT"
TIME_IN_FORCE = "DAY"


@dataclass
class PaperOrderPreview:
    ticker: str
    contract_symbol: str
    side: str
    quantity: int
    order_type: str
    time_in_force: str
    limit_price: float
    estimated_cost: float
    eligible: bool
    reasons: list[str]


def build_paper_order_preview(
    ticker: str,
    contract_symbol: str,
    final_decision_status: str,
    limit_price: float,
    daily_limits_passed: bool,
    market_session_passed: bool,
) -> PaperOrderPreview:
    limit_price = round_up_to_cent(limit_price)
    estimated_cost = limit_price * 100 * QUANTITY
    reasons = []

    if final_decision_status != "PASS":
        reasons.append(
            f"Final CTS decision is {final_decision_status}, not PASS"
        )

    if not daily_limits_passed:
        reasons.append("Daily anti-overtrading limits returned BLOCK")

    if not market_session_passed:
        reasons.append("CTS market entry window is closed")

    if not contract_symbol.strip():
        reasons.append("Option contract symbol is missing")

    if limit_price <= 0:
        reasons.append("Limit price is not usable")

    if estimated_cost > MAX_CONTRACT_COST:
        reasons.append(
            f"Estimated cost exceeds ${MAX_CONTRACT_COST:.0f} cap"
        )

    return PaperOrderPreview(
        ticker=ticker.upper(),
        contract_symbol=contract_symbol,
        side="BUY",
        quantity=QUANTITY,
        order_type=ORDER_TYPE,
        time_in_force=TIME_IN_FORCE,
        limit_price=limit_price,
        estimated_cost=estimated_cost,
        eligible=not reasons,
        reasons=reasons or ["All preview-only safety checks passed"],
    )


def show_order_preview(preview: PaperOrderPreview) -> None:
    status = "READY" if preview.eligible else "REFUSED"

    print(f"\n{preview.ticker} PAPER ORDER PREVIEW: {status}")
    print(f"Contract: {preview.contract_symbol or 'MISSING'}")
    print(
        f"{preview.side} {preview.quantity} | {preview.order_type} "
        f"${preview.limit_price:.2f} | {preview.time_in_force}"
    )
    print(f"Estimated maximum cost: ${preview.estimated_cost:,.2f}")

    for reason in preview.reasons:
        print(f"- {reason}")

    print("Preview only. No broker request was created or submitted.")


def show_order_preview_simulation() -> None:
    print("\nCTS READ-ONLY PAPER ORDER PREVIEW")
    print("No Alpaca order client is imported or used.")

    cases = [
        (
            "ALL GATES PASS",
            build_paper_order_preview(
                "TEST",
                "TEST260807C00100000",
                "PASS",
                0.35,
                True,
                True,
            ),
        ),
        (
            "FINAL DECISION REVIEW",
            build_paper_order_preview(
                "TEST",
                "TEST260807C00100000",
                "REVIEW",
                0.35,
                True,
                True,
            ),
        ),
        (
            "CONTRACT ABOVE CAP",
            build_paper_order_preview(
                "TEST",
                "TEST260807C00100000",
                "PASS",
                1.51,
                True,
                True,
            ),
        ),
        (
            "DAILY LIMIT BLOCK",
            build_paper_order_preview(
                "TEST",
                "TEST260807C00100000",
                "PASS",
                0.35,
                False,
                True,
            ),
        ),
        (
            "ENTRY WINDOW CLOSED",
            build_paper_order_preview(
                "TEST",
                "TEST260807C00100000",
                "PASS",
                0.35,
                True,
                False,
            ),
        ),
    ]

    for name, preview in cases:
        print(f"\nSCENARIO: {name}")
        show_order_preview(preview)

    print("\nSimulation completed. No order was submitted.")
