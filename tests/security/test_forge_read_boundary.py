from weftmark.application.ports.forge import ForgeAvailability, ForgeResult


def test_non_available_observations_are_not_boolean_test_outcomes() -> None:
    missing = ForgeResult.missing("no run")
    unsupported = ForgeResult.unsupported("feature disabled")
    unavailable = ForgeResult.unavailable("provider down")

    assert missing.availability is ForgeAvailability.MISSING
    assert unsupported.availability is ForgeAvailability.UNSUPPORTED
    assert unavailable.availability is ForgeAvailability.UNAVAILABLE
    for result in (missing, unsupported, unavailable):
        assert result.value is None
        assert not hasattr(result, "passed")
        assert not hasattr(result, "failed")
