"""The News learning plane: frozen datasets, the cold compiler, evaluation, and canary control.

Nothing here runs on the online Event route. Human truth acquisition is not here either: #202 moved the
ReviewDesk and its drafter to `tracefold.news.review`, because a queue served over HTTP against production
and an offline optimization over a frozen export are two lifecycles, and naming them one package made one
set of permissions stand for both.
"""
