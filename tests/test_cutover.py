from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from tracefold.app.cutover import execute_hard_cut


def test_hard_cut_refuses_to_touch_postgres_without_snapshot_confirmation(
    tmp_path: Path,
) -> None:
    settings = SimpleNamespace()
    with (
        patch("tracefold.app.cutover.connect_postgres") as connect,
        pytest.raises(
            ValueError,
            match="hard_cut_snapshot_confirmation_required",
        ),
    ):
        execute_hard_cut(
            settings=settings,
            bootstrap_dsn="postgresql://tracefold_app@postgres/tracefold",
            bootstrap_password_file=tmp_path / "postgres_password",
            snapshot_confirmed=False,
        )
    connect.assert_not_called()
