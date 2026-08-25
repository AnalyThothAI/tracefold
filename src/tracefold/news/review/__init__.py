"""Human truth acquisition: the ReviewDesk queue, its submissions, and the drafter that seeds them.

This is where the only Gold this system has comes from — an accepted `news_review_v4` written by a person.
It sat under `learning` until #202, which is why an online HTTP route serving a review queue and an offline
GEPA run reading a frozen corpus looked like one lifecycle and shared one package's permissions. They are
not one lifecycle: this plane runs continuously against production, holds a database session, and is read
by the operator console; the optimizer runs on a frozen export and holds nothing.

Nothing here optimizes, evaluates, registers, arms or promotes anything.
"""
