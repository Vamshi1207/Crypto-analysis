# Archived ML training code

Retired on 2026-08-21 when the platform moved from "train our own classifier" to
"compose pretrained models and agents" (see `plans/implementation_plan.md`).

Nothing here is on the product path. It is kept only as a record of the earlier
XGBoost / Teacher-Student approach and of which feature columns were explored.

Do not import from this directory. The replacement is `python_server/decision/`,
which produces a `DecisionCard` from pretrained inference plus gate logic and
performs no training.
