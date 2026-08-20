from __future__ import annotations

import pytest

from weftmark.adapters.gitlab import GitlabAdapterError, GitlabForgeAdapter


def test_gitlab_adapter_rejects_header_injection_and_bad_project_paths() -> None:
    with pytest.raises(GitlabAdapterError):
        GitlabForgeAdapter("group/repo", token="token\nInjected: yes")
    with pytest.raises(GitlabAdapterError):
        GitlabForgeAdapter("group/../repo")
    with pytest.raises(GitlabAdapterError):
        GitlabForgeAdapter("repo")


def test_gitlab_adapter_is_read_side_only() -> None:
    value = GitlabForgeAdapter("group/repo")
    assert not hasattr(value, "merge")
    assert not hasattr(value, "approve")
    assert not hasattr(value, "comment_create")
    assert not hasattr(value, "trigger_pipeline")
