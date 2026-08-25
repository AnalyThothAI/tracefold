"""The release plane: what may be considered, what is exposed, and what ships.

Registration, canary control and promotion (#202 §4.3). Split from `learning` because optimizing two
instructions and deciding whether they reach a reader are different lifecycles with different permissions
— and while they shared a package, one set of boundaries stood for both.

It reads frozen datasets; the learning plane never reaches back into this one.
"""
