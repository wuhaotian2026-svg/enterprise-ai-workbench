from __future__ import annotations

from policy_api.retrieval import repository as repository_module
from policy_api.retrieval.repository import (
    _LEXICAL_PROBE_LIMIT,
    SqlAlchemyRetrievalRepository,
    _lexical_probes,
)


def test_lexical_probes_keep_existing_fact_preserving_behavior() -> None:
    assert _lexical_probes("  annual   leave  ") == (
        "annual leave",
        "annual",
        "leave",
    )
    assert _lexical_probes("差旅报销") == (
        "差旅报销",
        "差旅",
        "旅报",
        "报销",
    )


def test_lexical_select_uses_fixed_typed_null_probe_slots() -> None:
    query = "annual"
    statement = SqlAlchemyRetrievalRepository(None)._lexical_select(
        query,
        limit=20,
        variant_index=0,
    )

    parameter_values = tuple(statement.compile().params.values())

    assert parameter_values.count("annual") == 1
    assert parameter_values.count(None) == _LEXICAL_PROBE_LIMIT - len(
        _lexical_probes(query)
    )


def test_lexical_select_cache_key_is_stable_across_real_probe_counts() -> None:
    short_query = "annual leave"
    long_query = "请说明差旅住宿报销标准以及审批要求"
    assert len(_lexical_probes(short_query)) != len(_lexical_probes(long_query))

    repository = SqlAlchemyRetrievalRepository(None)
    short_key = repository._lexical_select(
        short_query,
        limit=20,
        variant_index=0,
    )._generate_cache_key()
    long_key = repository._lexical_select(
        long_query,
        limit=20,
        variant_index=0,
    )._generate_cache_key()

    assert short_key is not None
    assert long_key is not None
    assert short_key.key == long_key.key


def test_lexical_statement_template_is_reused_without_query_values() -> None:
    statement_template = getattr(
        repository_module,
        "_lexical_statement_template",
    )
    statement_template.cache_clear()

    first = statement_template(2)
    second = statement_template(2)
    different_shape = statement_template(3)

    assert first is second
    assert first is not different_shape
    compiled_parameters = first.compile().params
    probe_parameters = {
        name: value
        for name, value in compiled_parameters.items()
        if name.startswith("variant_") and "_probe_" in name
    }
    assert len(probe_parameters) == 2 * _LEXICAL_PROBE_LIMIT
    assert set(probe_parameters.values()) == {None}


def test_lexical_statement_parameters_are_request_scoped() -> None:
    statement_parameters = getattr(
        repository_module,
        "_lexical_statement_parameters",
    )

    first = statement_parameters(("annual leave", "差旅报销"), limit=20)
    second = statement_parameters(("compensatory leave", "采购审批"), limit=10)

    assert first["lexical_limit"] == 20
    assert second["lexical_limit"] == 10
    assert first["variant_0_probe_0"] == "annual leave"
    assert second["variant_0_probe_0"] == "compensatory leave"
    assert first["variant_1_probe_0"] == "差旅报销"
    assert second["variant_1_probe_0"] == "采购审批"
    assert first["variant_0_probe_23"] is None
    assert second["variant_1_probe_23"] is None
