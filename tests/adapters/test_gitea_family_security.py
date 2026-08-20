from __future__ import annotations

import pytest

from weftmark.adapters.forgejo import ForgejoAdapterError, ForgejoForgeAdapter
from weftmark.adapters.gitea import GiteaAdapterError, GiteaForgeAdapter


@pytest.mark.parametrize("adapter,error", [(GiteaForgeAdapter, GiteaAdapterError), (ForgejoForgeAdapter, ForgejoAdapterError)])
def test_family_adapters_reject_bad_repo_and_header_injection(adapter, error) -> None:
    with pytest.raises(error):
        adapter("team/../repo")
    with pytest.raises(error):
        adapter("team/repo", token="token\nInjected: yes")


@pytest.mark.parametrize("adapter", [GiteaForgeAdapter, ForgejoForgeAdapter])
def test_family_adapters_remain_read_side_only(adapter) -> None:
    value = adapter("team/repo")
    assert not hasattr(value, "merge")
    assert not hasattr(value, "approve")
    assert not hasattr(value, "comment_create")
    assert not hasattr(value, "trigger_workflow")
    assert not hasattr(value, "release")
