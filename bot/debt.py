def simplify_debts(
    balances: dict[int, float],
) -> list[tuple[int, int, float]]:
    """
    Greedy debt-simplification algorithm.
    balances: {user_id: net_balance}  positive = owed money, negative = owes money
    Returns: list of (from_user_id, to_user_id, amount) payments that clear all debts.
    """
    EPS = 0.005

    creditors: list[list] = sorted(
        [[uid, bal] for uid, bal in balances.items() if bal > EPS],
        key=lambda x: -x[1],
    )
    debtors: list[list] = sorted(
        [[uid, -bal] for uid, bal in balances.items() if bal < -EPS],
        key=lambda x: -x[1],
    )

    transactions: list[tuple[int, int, float]] = []
    i = j = 0

    while i < len(creditors) and j < len(debtors):
        cred_uid, credit = creditors[i]
        debt_uid, debt = debtors[j]

        amount = min(credit, debt)
        if amount > EPS:
            transactions.append((debt_uid, cred_uid, round(amount, 2)))

        creditors[i][1] = credit - amount
        debtors[j][1] = debt - amount

        if creditors[i][1] <= EPS:
            i += 1
        if debtors[j][1] <= EPS:
            j += 1

    return transactions
