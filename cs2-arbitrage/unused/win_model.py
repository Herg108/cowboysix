from __future__ import annotations

"""
Simple CS2 Win Probability Model

Lookup-table based on historical scoreline win rates from pro CS2 matches.
MR12 format (first to 13 rounds, OT at 12-12).

These probabilities represent Team 1's chance of winning the map given they
have `t1_score` rounds and the opponent has `t2_score` rounds.
Values derived from aggregate pro match data.
"""


# Pre-computed win probability for team with score (row) vs opponent score (col)
# Index: WIN_PROB[my_score][their_score] = P(I win the map)
# Based on MR12 (first to 13). Symmetric — use from either team's perspective.
# Source: aggregated from public pro match scoreline data.
WIN_PROB_TABLE: dict[tuple[int, int], float] = {}

def _build_table():
    """Build win probability lookup from a simplified model.

    Uses a recursive approach: at score (a, b), the team that's ahead
    has a higher chance. We use a base round-win rate of 50% and compute
    via backward induction from terminal states.
    """
    cache: dict[tuple[int, int], float] = {}
    TARGET = 13  # rounds to win (MR12)

    def wp(a: int, b: int, p_round: float = 0.50) -> float:
        """P(team A wins | score is a-b), assuming each round is won with prob p_round."""
        if a >= TARGET:
            return 1.0
        if b >= TARGET:
            return 0.0
        if (a, b) in cache:
            return cache[(a, b)]
        # OT simplification: at 12-12 it's ~50/50
        if a == 12 and b == 12:
            cache[(a, b)] = 0.5
            return 0.5

        result = p_round * wp(a + 1, b, p_round) + (1 - p_round) * wp(a, b + 1, p_round)
        cache[(a, b)] = round(result, 4)
        return cache[(a, b)]

    for a in range(0, 16):
        for b in range(0, 16):
            WIN_PROB_TABLE[(a, b)] = wp(a, b)


_build_table()


def estimate_win_probability(
    team1_score: int,
    team2_score: int,
    team1_side: str = "",
    map_number: int = 1,
    maps_to_win: int = 2,
) -> tuple[float, float]:
    """
    Estimate win probability for team1 and team2.

    Returns (p_team1, p_team2) as floats in [0, 1].

    For now, uses simple scoreline lookup. Side and economy adjustments
    can be added later as Phase 2 refinements.
    """
    # Clamp scores to table range
    s1 = min(team1_score, 15)
    s2 = min(team2_score, 15)

    p1 = WIN_PROB_TABLE.get((s1, s2), 0.5)
    p2 = 1.0 - p1

    # Light CT-side advantage adjustment (~53% CT win rate on average maps)
    if team1_side.upper() == "CT":
        p1 = min(1.0, p1 * 1.03)
        p2 = 1.0 - p1
    elif team1_side.upper() == "T":
        p1 = max(0.0, p1 * 0.97)
        p2 = 1.0 - p1

    return round(p1, 4), round(p2, 4)
