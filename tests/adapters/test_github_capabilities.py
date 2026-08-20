from weftmark.adapters.github import GithubForgeAdapter


def test_github_adapter_inherits_complete_read_capabilities() -> None:
    capabilities = GithubForgeAdapter("team/repo").capabilities()
    assert capabilities.change_requests is True
    assert capabilities.checks is True
    assert capabilities.workflow_runs is True
    assert capabilities.reviews is True
    assert capabilities.comments is True
    assert capabilities.changed_files is True
