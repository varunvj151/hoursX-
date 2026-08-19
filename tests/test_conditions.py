"""The post-condition language: observation, comparison, and safe failure."""

import pytest

from hoursx.remediation.conditions import (
    Condition,
    Operator,
    ProbeKind,
    _as_bool,
    _as_number,
    _compare,
    _loosely_equal,
    all_met,
    evaluate_conditions,
)


def _condition(**kwargs) -> Condition:
    kwargs.setdefault("probe", ProbeKind.MEMORY_USED_PERCENT)
    kwargs.setdefault("operator", Operator.LT)
    kwargs.setdefault("value", 100)
    return Condition(**kwargs)


# ----------------------------------------------------------------- description


def test_condition_describes_itself_readably():
    condition = _condition(probe=ProbeKind.LOAD_1M, operator=Operator.LTE, value=4.0)
    assert condition.describe() == "load_1m <= 4.0"


def test_targeted_condition_includes_its_target():
    condition = _condition(
        probe=ProbeKind.SYSCTL_VALUE, target="vm.swappiness", operator=Operator.EQ, value="10"
    )
    assert condition.describe() == "sysctl_value(vm.swappiness) == 10"


def test_condition_is_json_serialisable():
    """It has to survive the ledger and the approval prompt."""
    payload = _condition().model_dump(mode="json")
    assert Condition.model_validate(payload) == _condition()


# ------------------------------------------------------------------ comparison


@pytest.mark.parametrize(
    ("observed", "operator", "expected", "result"),
    [
        (5, Operator.LT, 10, True),
        (15, Operator.LT, 10, False),
        (10, Operator.LTE, 10, True),
        (15, Operator.GT, 10, True),
        (10, Operator.GTE, 10, True),
        (10, Operator.EQ, 10, True),
        (10, Operator.NE, 10, False),
    ],
)
def test_numeric_comparisons(observed, operator, expected, result):
    assert _compare(observed, operator, expected) is result


def test_kernel_strings_compare_as_numbers():
    """/proc returns "10", the model writes 10 — both must work."""
    assert _compare("10", Operator.EQ, 10)
    assert _compare("10", Operator.LT, 20)
    assert _compare(10, Operator.EQ, "10")


def test_service_state_strings_compare_as_booleans():
    assert _compare("active", Operator.EQ, True)
    assert _compare(True, Operator.EQ, "active")
    assert _compare("inactive", Operator.EQ, False)


def test_string_equality_falls_back_to_text():
    assert _compare("performance", Operator.EQ, "performance")
    assert not _compare("performance", Operator.EQ, "powersave")


def test_ordering_non_numeric_values_is_an_error_not_a_silent_false():
    """Silently returning False would let a nonsense condition 'pass' on revert."""
    from hoursx.remediation.conditions import _Unobservable

    with pytest.raises(_Unobservable):
        _compare("performance", Operator.LT, "powersave")


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("1", True),
        ("true", True),
        ("yes", True),
        ("active", True),
        ("on", True),
        ("0", False),
        ("inactive", False),
        ("no", False),
        ("", False),
    ],
)
def test_boolean_coercion(value, expected):
    assert _as_bool(value) is expected


def test_number_coercion_rejects_booleans():
    """True must not silently become 1.0 in an ordering comparison."""
    assert _as_number(True) is None
    assert _as_number("4.5") == 4.5
    assert _as_number("not a number") is None


def test_loose_equality_handles_whitespace():
    assert _loosely_equal(" active\n", "active")


# ------------------------------------------------------------------ evaluation


async def test_satisfiable_condition_is_met():
    results = await evaluate_conditions(
        [_condition(probe=ProbeKind.PROCESS_COUNT, operator=Operator.GT, value=0)]
    )
    assert len(results) == 1 and results[0].met
    assert all_met(results)


async def test_unsatisfiable_condition_is_not_met():
    results = await evaluate_conditions(
        [_condition(probe=ProbeKind.PROCESS_COUNT, operator=Operator.LT, value=0)]
    )
    assert not results[0].met
    assert not all_met(results)


async def test_missing_target_is_reported_not_raised():
    results = await evaluate_conditions(
        [_condition(probe=ProbeKind.SYSCTL_VALUE, operator=Operator.EQ, value="x")]
    )
    assert not results[0].met
    assert "requires a target" in results[0].detail


async def test_unreadable_probe_counts_as_unmet():
    """A change whose effect cannot be confirmed must not be treated as verified."""
    results = await evaluate_conditions(
        [
            _condition(
                probe=ProbeKind.SYSCTL_VALUE,
                target="not.a.real.parameter",
                operator=Operator.EQ,
                value="1",
            )
        ]
    )
    assert not results[0].met
    assert "not readable" in results[0].detail


async def test_missing_mountpoint_is_reported():
    results = await evaluate_conditions(
        [
            _condition(
                probe=ProbeKind.DISK_USED_PERCENT,
                target="/definitely/not/mounted",
                operator=Operator.LT,
                value=90,
            )
        ]
    )
    assert not results[0].met and "no mounted filesystem" in results[0].detail


async def test_invalid_port_target_is_reported():
    results = await evaluate_conditions(
        [
            _condition(
                probe=ProbeKind.PORT_LISTENING,
                target="not-a-port",
                operator=Operator.EQ,
                value=True,
            )
        ]
    )
    assert not results[0].met and "not a port number" in results[0].detail


async def test_all_conditions_are_evaluated_even_after_a_failure():
    """One bad probe must not hide the state of the others."""
    results = await evaluate_conditions(
        [
            _condition(probe=ProbeKind.SYSCTL_VALUE, operator=Operator.EQ, value="x"),
            _condition(probe=ProbeKind.PROCESS_COUNT, operator=Operator.GT, value=0),
        ]
    )
    assert len(results) == 2
    assert not results[0].met and results[1].met


async def test_empty_condition_list_is_vacuously_met():
    assert all_met(await evaluate_conditions([]))


async def test_result_description_names_the_verdict_and_observation():
    results = await evaluate_conditions(
        [_condition(probe=ProbeKind.PROCESS_COUNT, operator=Operator.LT, value=0)]
    )
    description = results[0].describe()
    assert "FAILED" in description and "observed" in description


async def test_sysctl_probe_reads_a_real_parameter():
    results = await evaluate_conditions(
        [
            _condition(
                probe=ProbeKind.SYSCTL_VALUE,
                target="kernel.ostype",
                operator=Operator.EQ,
                value="Linux",
            )
        ]
    )
    # Restricted environments legitimately cannot read it; either is acceptable,
    # but it must never claim to have verified something it did not observe.
    assert results[0].met or "not readable" in results[0].detail
