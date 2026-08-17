from reverse_string import reverse_string


def test_reverse_string_basic():
    assert reverse_string("hello") == "olleh"


def test_reverse_string_empty():
    assert reverse_string("") == ""
