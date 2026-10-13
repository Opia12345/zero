"""
Quote-quality strategy for Deriv digit contracts.

Last digits on Deriv's synthetic indices are independent and uniform, so no
pattern in past digits predicts the next one. The only thing that actually
varies, and so the only thing worth optimising, is how much house edge each
live quote carries.

For a contract with true win probability p, stake s and quoted payout P:

    RTP = p * P / s          (expected return per unit staked)

RTP < 1 means a negative expected value. This module computes RTP for every
digit contract on a symbol and picks the least costly one. It cannot make the
expected value positive; it only avoids paying more edge than necessary, and
refuses to trade when even the best quote is too expensive.
"""

from dataclasses import dataclass


@dataclass(frozen=True)
class Contract:
    contract_type: str
    barrier: str | None  # None for contracts without a barrier (EVEN/ODD)
    win_prob: float

    @property
    def label(self) -> str:
        names = {
            "DIGITOVER": "over",
            "DIGITUNDER": "under",
            "DIGITDIFF": "differs from",
            "DIGITMATCH": "matches",
        }
        if self.contract_type == "DIGITEVEN":
            return "even"
        if self.contract_type == "DIGITODD":
            return "odd"
        return f"{names[self.contract_type]} {self.barrier}"


def contract_menu() -> list[Contract]:
    """Every digit contract with its exact win probability."""
    menu = [Contract("DIGITOVER", str(b), (9 - b) / 10) for b in range(0, 9)]
    menu += [Contract("DIGITUNDER", str(b), b / 10) for b in range(1, 10)]
    menu += [Contract("DIGITDIFF", str(b), 0.9) for b in range(10)]
    menu += [Contract("DIGITMATCH", str(b), 0.1) for b in range(10)]
    menu += [Contract("DIGITEVEN", None, 0.5), Contract("DIGITODD", None, 0.5)]
    return menu


def rtp(win_prob: float, stake: float, payout: float) -> float:
    if stake <= 0:
        raise ValueError("stake must be positive")
    return win_prob * payout / stake


@dataclass(frozen=True)
class Quote:
    symbol: str
    contract: Contract
    stake: float
    payout: float

    @property
    def rtp(self) -> float:
        return rtp(self.contract.win_prob, self.stake, self.payout)


def best_quote(quotes: list[Quote], min_rtp: float) -> Quote | None:
    """Highest-RTP quote, or None if nothing reaches min_rtp. Ties go to the
    higher win probability, which has the lower variance."""
    if not quotes:
        return None
    best = max(quotes, key=lambda q: (round(q.rtp, 6), q.contract.win_prob))
    return best if best.rtp >= min_rtp else None


def break_even_win_rate(stake: float, payout: float) -> float:
    """Win rate needed for zero expected value at this quote."""
    return stake / payout


def expected_cost(rtp_value: float, stake: float) -> float:
    """Expected loss per trade."""
    return (1 - rtp_value) * stake


def win_rate_z(wins: int, win_probs: list[float]) -> float:
    """z-score of the realised win count against the true win probability of
    each contract actually traded. Large |z| means the feed or the contract
    mapping is not what we think (a bug or a changed product), not that the
    bot has found an edge."""
    mean = sum(win_probs)
    var = sum(p * (1 - p) for p in win_probs)
    if var == 0:
        return 0.0
    return (wins - mean) / var**0.5
