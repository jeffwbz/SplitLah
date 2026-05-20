def calculate_shares(
    total_base: float,
    participants: list[int],
    split_mode: str,
    split_values: list[float] | None = None,
) -> dict[int, float]:
    """
    Returns {user_id: share_in_base_currency}.
    split_mode: "equal" | "ratio" | "percentage" | "exact"
    split_values: required for ratio / percentage / exact modes.
    """
    n = len(participants)

    if split_mode == "equal":
        per_head = round(total_base / n, 2)
        shares = {uid: per_head for uid in participants}
        # Assign rounding remainder to the first participant
        diff = round(total_base - sum(shares.values()), 2)
        shares[participants[0]] = round(shares[participants[0]] + diff, 2)
        return shares

    if split_values is None or len(split_values) != n:
        raise ValueError("split_values must match participants length")

    if split_mode == "ratio":
        total_ratio = sum(split_values)
        if total_ratio == 0:
            raise ValueError("Ratios must not all be zero")
        raw = {uid: total_base * (r / total_ratio) for uid, r in zip(participants, split_values)}

    elif split_mode == "percentage":
        total_pct = sum(split_values)
        if abs(total_pct - 100) > 0.5:
            raise ValueError(f"Percentages must sum to 100, got {total_pct}")
        raw = {uid: total_base * (p / 100) for uid, p in zip(participants, split_values)}

    elif split_mode == "exact":
        total_exact = sum(split_values)
        if abs(total_exact - total_base) > 0.02:
            raise ValueError(
                f"Exact amounts ({total_exact:.2f}) must sum to the total ({total_base:.2f})"
            )
        return {uid: round(v, 2) for uid, v in zip(participants, split_values)}

    else:
        raise ValueError(f"Unknown split mode: {split_mode!r}")

    # Round and fix remainder for ratio / percentage
    shares = {uid: round(v, 2) for uid, v in raw.items()}
    diff = round(total_base - sum(shares.values()), 2)
    shares[participants[0]] = round(shares[participants[0]] + diff, 2)
    return shares


def parse_split_values(text: str, n: int) -> list[float]:
    """Parse space- or comma-separated numbers from user input."""
    parts = text.replace(",", " ").split()
    if len(parts) != n:
        raise ValueError(f"Expected {n} values, got {len(parts)}")
    try:
        values = [float(p) for p in parts]
    except ValueError:
        raise ValueError("All values must be numbers")
    if any(v < 0 for v in values):
        raise ValueError("Values must be positive")
    return values
