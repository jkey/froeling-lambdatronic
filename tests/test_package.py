"""Package metadata tests."""

from importlib.metadata import version

import froeling_lambdatronic


def test_version_matches_metadata() -> None:
    assert froeling_lambdatronic.__version__ == version("froeling-lambdatronic")
