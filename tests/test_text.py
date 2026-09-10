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


def test_literal_target_is_inserted_verbatim():
    # A literal rule's target is text, not an re template: backslash escapes must
    # survive and group references must not be expanded (or rejected).
    assert apply_replacements("open new line here", [["new line", r"C:\new"]]) == r"open C:\new here"
    assert apply_replacements("say foo", [["foo", r"\1bar"]]) == r"say \1bar"
    assert apply_replacements("a b", [["a", "x\\y"]]) == "x\\y b"


def test_literal_rule_matches_phrases_with_non_word_edges():
    assert apply_replacements("I write c++ daily", [["c++", "C++"]]) == "I write C++ daily"
    assert apply_replacements("obs bot. yes", [["obs bot.", "OBS Bot."]]) == "OBS Bot. yes"
    assert apply_replacements("(paren) here", [["(paren)", "PAREN"]]) == "PAREN here"


def test_non_word_edges_do_not_widen_the_word_boundary_on_the_other_side():
    # The leading edge is still guarded: "c++" has a word char at the front.
    assert apply_replacements("abc++ daily", [["c++", "C++"]]) == "abc++ daily"
