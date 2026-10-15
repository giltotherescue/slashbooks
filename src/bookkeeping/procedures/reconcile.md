---
name: reconcile
version: 1
title: Reconcile balances
---
- Reconcile an account only against a balance and a statement or provider record that the source supplied.
- Pass the source balance, opening balance, activity total, snapshot time and evidence reference to the engine command. Do not estimate any of them.
- If the evidence is missing or does not match, report the difference. Do not adjust a balance to make it match.
- A missing statement for one account does not block unrelated categorization.
- Decide a possible duplicate with the engine duplicate commands. Mark it duplicate or distinct only when the evidence shows which.
