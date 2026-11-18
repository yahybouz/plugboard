"""A personal key's name never hides an invisible character (PB-AUTH-01).

Every hostile character is built with ``chr`` at runtime: the source stays
plain ASCII and readable.
"""

import pytest

from plugboard import admin_people
from tests import people_helpers as ph

_REFUSED = "Name must be 1 to 64 printable characters"

INVISIBLE = {
    # Unicode format characters (Cf): zero width space, soft hyphen,
    # right-to-left override, byte order mark, a tag character.
    "cf-zero-width-space": "person" + chr(0x200B) + "one",
    "cf-soft-hyphen": "person" + chr(0x00AD) + "one",
    "cf-rtl-override": chr(0x202E) + "person-one",
    "cf-bom": "person-one" + chr(0xFEFF),
    "cf-tag": "person-one" + chr(0xE0041),
    # Controls (Cc), private use (Co), a lone surrogate (Cs).
    "cc-bell": "person" + chr(0x07) + "one",
    "cc-next-line": "person" + chr(0x85) + "one",
    "co-private-use": "person" + chr(0xE000),
    "co-plane-15": "person" + chr(0xF0000),
    "cs-surrogate": "person" + chr(0xD800),
    # Line and paragraph separators (Zl, Zp).
    "zl-line-separator": "person" + chr(0x2028) + "one",
    "zp-paragraph-separator": "person" + chr(0x2029) + "one",
    # Variation selectors, both blocks.
    "vs-1": "person" + chr(0xFE00),
    "vs-16": "person" + chr(0xFE0F),
    "vs-17": "person" + chr(0xE0100),
    "vs-256": "person" + chr(0xE01EF),
    # Combining grapheme joiner.
    "cgj": "person" + chr(0x034F) + "one",
    # Hangul fillers and the braille blank: letters that print as nothing.
    "hangul-choseong-filler": "person" + chr(0x115F),
    "hangul-jungseong-filler": "person" + chr(0x1160),
    "hangul-filler": "person" + chr(0x3164),
    "halfwidth-hangul-filler": "person" + chr(0xFFA0),
    "braille-blank": "person" + chr(0x2800) + "one",
    # A combining mark (Mn, Me) that opens the name has nothing to sit on.
    "mn-at-start": chr(0x0301) + "person",
    "me-at-start": chr(0x20DD) + "person",
    # Spaces: two in a row inside the name.
    "two-spaces": "person  one",
    "three-spaces": "person   one",
}


@pytest.fixture
def env(tmp_path, monkeypatch):
    yield from ph.isolate(tmp_path, monkeypatch)


@pytest.mark.parametrize("name", list(INVISIBLE.values()), ids=list(INVISIBLE))
def test_a_name_with_an_invisible_character_is_refused(env, name):
    with pytest.raises(admin_people.PersonError) as err:
        admin_people.create(name, created_by="admin-key")
    assert err.value.code == "invalid_name"
    assert str(err.value) == _REFUSED
    assert not env.store.exists()


@pytest.mark.parametrize(
    "name",
    [
        "person-one",
        "person one",
        "Zoe" + chr(0x0301),  # a combining mark after its base letter
        "e" + chr(0x0301) + "lise",
        chr(0x00C9) + "lise",
        chr(0x5C71) + chr(0x7530),  # CJK letters
    ],
)
def test_visible_names_are_accepted(env, name):
    assert ph.new_person(name)["person"]["name"] == name


def test_outer_spaces_are_trimmed_never_stored(env):
    stored = ph.new_person("  person-one  ")["person"]["name"]
    assert stored == "person-one"
    assert stored == stored.strip(" ")


async def test_the_api_refuses_an_invisible_name_with_422(admin_client, env):
    admin_client.cookies.clear()
    r = await admin_client.post(
        "/admin/people",
        json={"name": "person" + chr(0x200B) + "one"},
        headers=ph.ADMIN,
    )
    assert r.status_code == 422 and r.json()["detail"] == _REFUSED
    r = await admin_client.post(
        "/admin/people", json={"name": "person" + chr(0xFE0F)}, headers=ph.ADMIN
    )
    assert r.status_code == 422 and r.json()["detail"] == _REFUSED
    assert not env.store.exists()
