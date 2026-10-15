from derp.locales import LOCALES, catalog, placeholders, resolve, t


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
