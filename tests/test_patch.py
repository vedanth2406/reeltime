"""The --patch expression grammar."""

import pytest

from reeltime.core import patch as patch_mod
from reeltime.core.patch import apply_to_body, parse, parse_all
from reeltime.errors import TapeConfigError


# -- parsing -------------------------------------------------------------


def test_the_three_documented_examples_parse():
    model = parse("llm.model=claude-sonnet-4-5")
    assert (model.kind, model.field, model.op, model.value) == (
        "llm", "model", "=", "claude-sonnet-4-5")

    system = parse('llm.system+="Ask before destructive actions."')
    assert (system.kind, system.field, system.op) == ("llm", "system", "+=")
    assert system.value == "Ask before destructive actions."

    tool = parse('tool.read_file.result="<empty file>"')
    assert (tool.kind, tool.name, tool.field, tool.op) == (
        "tool", "read_file", "result", "=")
    assert tool.value == "<empty file>"
    assert tool.substitutes_result


def test_values_parse_as_json_when_they_are_json():
    assert parse("llm.temperature=0.2").value == 0.2
    assert parse("llm.max_tokens=512").value == 512
    assert parse('llm.model="gpt-4o"').value == "gpt-4o"
    assert parse("tool.x.result=null").value is None
    assert parse('tool.x.result={"a": 1}').value == {"a": 1}


def test_a_bare_word_is_a_string():
    # Model names are not valid JSON, and quoting them every time would be a
    # tax on the most common patch there is.
    assert parse("llm.model=gpt-4o-mini").value == "gpt-4o-mini"


def test_a_dotted_tool_name_is_kept_whole():
    assert parse("tool.pkg.read_file.result=x").name == "pkg.read_file"


def test_regex_substitution_parses_both_spellings():
    for expression in ("llm.system~=/careful/CAREFUL/",
                       "llm.system~=s/careful/CAREFUL/"):
        assert parse(expression).value == ("careful", "CAREFUL")


def test_parse_all_reads_a_list():
    assert len(parse_all(["llm.model=a", "llm.temperature=0.1"])) == 2
    assert parse_all([]) == []


# -- parse errors --------------------------------------------------------


@pytest.mark.parametrize(
    "expression,expected",
    [
        ("", "empty"),
        ("nonsense", "Expected <kind>"),
        ("llm=gpt-4o", "missing a field"),
        ("wizard.model=x", "unknown kind"),
        ("llm.wingspan=3", "has no field"),
        ("llm.model=", "has no value"),
        ("llm.system~=careful", "expects /pattern/replacement/"),
        ("llm.system~=/a/b/c/", "exactly one"),
    ],
)
def test_bad_expressions_explain_themselves(expression, expected):
    with pytest.raises(TapeConfigError) as caught:
        parse(expression)
    message = str(caught.value)
    assert expected in message
    assert "Traceback" not in message


def test_an_unknown_field_lists_the_known_ones():
    with pytest.raises(TapeConfigError) as caught:
        parse("tool.read_file.retunr=x")
    assert "result" in str(caught.value)


# -- operators -----------------------------------------------------------


def test_replace_takes_the_new_value():
    assert parse("llm.model=gpt-4o").apply("old") == "gpt-4o"


def test_append_joins_strings_with_a_space():
    assert parse('llm.system+="Be careful."').apply("Be terse.") == \
        "Be terse. Be careful."


def test_append_does_not_double_a_separator():
    assert parse('llm.system+="Be careful."').apply("Be terse.\n") == \
        "Be terse.\nBe careful."


def test_append_onto_nothing_is_the_value():
    assert parse('llm.system+="Be careful."').apply(None) == "Be careful."


def test_append_adds_numbers():
    assert parse("llm.temperature+=0.3").apply(0.2) == pytest.approx(0.5)


def test_append_extends_lists():
    assert parse('tool.x.result+=["b"]').apply(["a"]) == ["a", "b"]


def test_append_refuses_a_nonsense_combination():
    with pytest.raises(TapeConfigError, match="cannot append"):
        parse("llm.temperature+=0.3").apply("not a number")


def test_regex_substitution_rewrites_text():
    assert parse("llm.system~=/terse/verbose/").apply("Be terse.") == "Be verbose."


def test_regex_substitution_supports_groups():
    assert parse(r"llm.system~=/(\w+) files/\1 documents/").apply(
        "delete files now") == "delete documents now"


def test_regex_substitution_needs_text():
    with pytest.raises(TapeConfigError, match="needs text"):
        parse("llm.temperature~=/a/b/").apply(0.5)


def test_a_bad_regex_is_reported_not_raised_raw():
    with pytest.raises(TapeConfigError, match="bad regex"):
        parse("llm.system~=/([unclosed/x/").apply("text")


# -- applying to a request body ------------------------------------------


def test_model_is_replaced_on_the_body():
    body = apply_to_body(parse("llm.model=claude-sonnet-4-5"),
                         {"model": "gpt-4o", "messages": []})
    assert body["model"] == "claude-sonnet-4-5"


def test_the_openai_system_message_is_found_and_appended_to():
    body = apply_to_body(
        parse('llm.system+="Ask first."'),
        {"messages": [{"role": "system", "content": "Be terse."},
                      {"role": "user", "content": "hi"}]},
    )
    assert body["messages"][0]["content"] == "Be terse. Ask first."
    assert body["messages"][1] == {"role": "user", "content": "hi"}


def test_the_anthropic_system_field_is_found_and_appended_to():
    # One expression has to work against both providers, which is the whole
    # reason the patch names `system` rather than a path into the body.
    body = apply_to_body(parse('llm.system+="Ask first."'),
                         {"system": "Be terse.", "messages": []})
    assert body["system"] == "Be terse. Ask first."


def test_a_system_prompt_is_added_when_there_is_none():
    body = apply_to_body(parse('llm.system="Be careful."'),
                         {"messages": [{"role": "user", "content": "hi"}]})
    assert body["messages"][0] == {"role": "system", "content": "Be careful."}
    assert len(body["messages"]) == 2


def test_the_original_body_is_not_mutated():
    original = {"model": "gpt-4o", "messages": []}
    apply_to_body(parse("llm.model=other"), original)
    assert original["model"] == "gpt-4o"


def test_temperature_and_max_tokens_are_settable():
    body = apply_to_body(parse("llm.max_tokens=512"), {"max_tokens": 16})
    assert body["max_tokens"] == 512


# -- matching ------------------------------------------------------------


def test_a_patch_matches_its_kind():
    assert parse("llm.model=x").matches("llm", None)
    assert not parse("llm.model=x").matches("tool", "read_file")


def test_an_http_patch_also_matches_a_decoded_llm_event():
    # `llm` is a label a decoder puts on an http event; a patch written either
    # way should find it.
    assert parse("http.url=x").matches("llm", None)


def test_a_named_patch_only_matches_that_name():
    named = parse("tool.read_file.result=x")
    assert named.matches("tool", "read_file")
    assert not named.matches("tool", "write_file")


def test_an_unnamed_patch_matches_any_name():
    assert parse("tool.result=x").matches("tool", "anything")
