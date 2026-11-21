"""Fixed vocabulary for the synthetic ledger: fictional merchants and servicing copy.

Every merchant name here is invented. None refers to a real company.
"""

from typing import NamedTuple


class MerchantSpec(NamedTuple):
    name: str
    sector: str
    under_review: bool = False


MERCHANTS: tuple[MerchantSpec, ...] = (
    MerchantSpec("Velora Air Charters", "Travel"),
    MerchantSpec("Tidewell Getaways", "Travel"),
    MerchantSpec("Orrin Bay Cruises", "Travel"),
    MerchantSpec("Calder Skin Clinic", "Medical"),
    MerchantSpec("Brisa Dental Studio", "Medical"),
    MerchantSpec("Halvorsen Vision Care", "Medical"),
    MerchantSpec("Marlow & Finch Jewelers", "Retail"),
    MerchantSpec("Quillon Audio", "Retail", under_review=True),
    MerchantSpec("Ridgeback Outdoor", "Retail"),
    MerchantSpec("Fernhollow Roofing", "Home Improvement"),
    MerchantSpec("Sunvale Solar", "Home Improvement"),
    MerchantSpec("Ashgrove Kitchens", "Home Improvement"),
)

# Principal range and the most likely ticket size, in dollars, by sector.
PRINCIPAL_BY_SECTOR: dict[str, tuple[float, float, float]] = {
    "Travel": (120.0, 8_000.0, 900.0),
    "Medical": (250.0, 15_000.0, 2_400.0),
    "Retail": (50.0, 18_000.0, 600.0),
    "Home Improvement": (1_500.0, 50_000.0, 12_000.0),
}

HARDSHIP_REPLIES: tuple[str, ...] = (
    "Got my hours cut this month. Can I split this payment in two?",
    "I'm between jobs right now. Could we push the date back a few weeks?",
    "Medical bills landed at the same time as this. I want to pay, I just need more time.",
    "Can the monthly amount come down? I can manage about half of it.",
    "Our household income dropped after a layoff. Is there a hardship option?",
)

DISPUTE_CLAIMS: dict[str, tuple[str, ...]] = {
    "Travel": (
        "My flight was cancelled by {merchant} and they told me a refund is on the way.",
        "I changed the booking and {merchant} said the fare difference would be credited.",
    ),
    "Medical": (
        "The procedure at {merchant} was rescheduled and never happened. Why am I paying?",
        "{merchant} billed my insurance for part of this. The balance should be lower.",
    ),
    "Retail": (
        "I returned the order to {merchant} on {date} and have the return receipt.",
        "Half the order from {merchant} never arrived. I'm not paying for items I didn't get.",
    ),
    "Home Improvement": (
        "{merchant} hasn't finished the job. I'm holding payment until the work is complete.",
        "The install from {merchant} failed inspection and they owe me a fix first.",
    ),
}

MERCHANT_DISPUTE_RESPONSES: tuple[str, ...] = (
    "Merchant response: no refund has been issued; goods/services delivered as agreed.",
    "Merchant response: partial refund approved, amount not yet confirmed.",
    "Merchant response: still investigating, no answer yet.",
)
