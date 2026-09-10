from voice.text import apply_replacements, normalize_text


def test_plain_rule_is_word_bounded_and_case_sensitive():
    rules = [["cachy os", "CachyOS"]]
    assert apply_replacements("I use cachy os daily", rules) == "I use CachyOS daily"
    assert apply_replacements("Cachy OS", rules) == "Cachy OS"
    assert apply_replacements("mycachy os", rules) == "mycachy os"


def test_icase_flag():
    assert apply_replacements("Obs Bot is here", [["obs bot", "OBSBOT", "icase"]]) == "OBSBOT is here"


def test_regex_flag_and_ordering():
    rules = [[r"(\d+) percent", r"\1%", "regex"], ["%", " percent", "regex"]]
    assert apply_replacements("50 percent", rules) == "50 percent"     # second rule undoes first: order respected


def test_bad_regex_is_skipped():
    assert apply_replacements("keep", [["(", "x", "regex"]]) == "keep"


def test_normalize_text():
    assert normalize_text("  “Hi”  there ’s \n more ") == '"Hi" there \'s more'
