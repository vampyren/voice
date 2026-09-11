from voice.text import HOTWORDS_MAX_CHARS, apply_replacements, build_hotwords, normalize_text


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


def test_an_empty_source_is_skipped():
    # An empty pattern matches at every position and would splice the target
    # between every character of the transcript.
    assert apply_replacements("hello there", [["", "x"]]) == "hello there"
    assert apply_replacements("hello there", [["", "x", "regex"]]) == "hello there"
    assert apply_replacements("hello there", [["", "x"], ["there", "world"]]) == "hello world"


# -- the vocabulary handed to the model -------------------------------------

def test_hotwords_come_from_both_the_word_list_and_the_replacement_targets():
    """One list to maintain: the spelling the user asked for in a replacement is
    also the spelling they want the model to hear."""
    assert build_hotwords(["Keychron"], [["cachy os", "CachyOS", "icase"], ["obs bot", "OBSBOT"]]) == (
        "Keychron, CachyOS, OBSBOT")


def test_hotwords_keep_their_order_and_drop_duplicates_however_they_are_spelled():
    words = ["CachyOS", "Keychron", "cachyos"]
    rules = [["cachy os", "CachyOS", "icase"], ["key chron", "Keychron"], ["obs bot", "OBSBOT"]]
    assert build_hotwords(words, rules) == "CachyOS, Keychron, OBSBOT"


def test_hotwords_ignore_blank_entries_and_regex_targets():
    """A regex rule's target is a template ("\\1"), not a word anyone said."""
    rules = [["", "Nothing"], ["x", "  "], [r"(\d+) percent", r"\1%", "regex"], ["short"]]
    assert build_hotwords(["  ", "", "Keychron  "], rules) == "Keychron"


def test_no_vocabulary_at_all_is_the_empty_string():
    assert build_hotwords([], []) == ""
    assert build_hotwords(None, None) == ""


def test_hotwords_are_capped_at_whole_words():
    words = [f"Word{i:02d}" for i in range(40)]          # 6 chars each, ", " between
    out = build_hotwords(words, [])
    assert len(out) == 198                               # 25 * 6 + 24 * 2; 26 would be 206
    assert out.endswith("Word24")
    assert "Word25" not in out                           # dropped whole, never cut in half


def test_a_single_entry_longer_than_the_cap_is_dropped_rather_than_cut():
    assert build_hotwords(["x" * (HOTWORDS_MAX_CHARS + 1), "Keychron"], []) == "Keychron"
