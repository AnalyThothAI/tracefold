# Tracefold

Tracefold keeps auditable News and Trading facts while preserving the authority and provenance of every decision derived from them.

## News review language

**Gold**:
A review reference explicitly accepted into the durable ledger. Gold names an acceptance state, not independent human accuracy or universal ground truth.
_Avoid_: Human label, ground truth

**Proposal**:
A suggested review or taxonomy that has not been accepted and therefore carries no truth authority.
_Avoid_: Draft Gold, provisional truth

**Teacher Proposal**:
A proposal produced by a more deliberative model route to help a reviewer inspect another prediction. It remains a Proposal until accepted.
_Avoid_: Teacher truth, automatic Gold

**AI Adjudicator**:
An identified nonhuman reviewer to which the owner delegates evidence inspection. Its identity and conclusions remain explicit and must never be described as human review.
_Avoid_: Human reviewer, operator simulation

**Stable Prediction**:
The production prediction already recorded for the active Stable cohort and evaluated without rerunning the model.
_Avoid_: Teacher label, fresh inference

**Review UI Projection**:
A replaceable presentation of evidence, Proposals, and decisions for review. It carries no truth authority of its own.
_Avoid_: Annotation database, second review ledger
