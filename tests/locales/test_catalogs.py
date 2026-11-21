from typing import get_args

from derp.locales import LOCALES, catalog, placeholders, resolve, t
from derp.memory.reply_rules import ExactGrant, OutputTool


def test_catalogs_have_identical_keys_and_placeholders() -> None:
    reference = catalog("en")
    for locale in LOCALES:
        current = catalog(locale)
        assert current.keys() == reference.keys(), locale
        for key, text in current.items():
            assert placeholders(text) == placeholders(reference[key]), (locale, key)


def test_resolve_and_fallback() -> None:
    assert resolve("ru-RU") == "ru"
    assert resolve("de") == "en"
    assert resolve(None) == "en"
    assert t("ru", "start.private").startswith("**Дерп**")


def test_every_available_auto_reply_permission_has_a_label_in_each_language() -> None:
    allowed = {*get_args(OutputTool), *get_args(ExactGrant.model_fields["tool"].annotation)}
    assert allowed
    for locale in LOCALES:
        for tool in allowed:
            assert catalog(locale).get(f"auto.tools.{tool}"), (locale, tool)
