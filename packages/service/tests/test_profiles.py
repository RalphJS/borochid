import json

import pytest

from borochid.service.profiles import ProfileError, ProfileStore


def test_starts_with_a_default_profile(tmp_path):
    store = ProfileStore(tmp_path / "profiles.json")
    assert store.snapshot() == {"profiles": [{"id": "default", "name": "Default"}], "active": "default"}


def test_add_rename_remove_and_persist(tmp_path):
    changes = []
    store = ProfileStore(tmp_path / "profiles.json", lambda: changes.append(store.active))
    games = store.add("  Games  ", duplicate=True)
    blank = store.add("Blank")
    assert (games.name, games.copy_of, blank.copy_of) == ("Games", "default", None)
    assert store.active == blank.id and changes == [games.id, blank.id]
    store.rename(games.id, "Gaming")
    store.select(games.id)
    store.remove("default")
    assert store.get(games.id).copy_of is None  # its source is gone: devices use defaults

    again = ProfileStore(tmp_path / "profiles.json")
    assert again.snapshot() == store.snapshot()
    assert [p.name for p in again.items] == ["Gaming", "Blank"] and again.active == games.id


def test_removing_the_active_profile_picks_its_neighbour(tmp_path):
    store = ProfileStore(tmp_path / "p.json")
    a = store.add("A")
    store.add("B")
    store.select(a.id)
    store.remove(a.id)
    assert store.current.name == "Default"


@pytest.mark.parametrize(
    "call",
    [
        lambda s: s.remove("default"),  # the last one
        lambda s: s.add(""),
        lambda s: s.add("x" * 40),
        lambda s: s.add("Default"),  # duplicate name
        lambda s: s.select("nope"),
        lambda s: s.rename("nope", "X"),
    ],
)
def test_bad_edits_are_refused(tmp_path, call):
    with pytest.raises(ProfileError):
        call(ProfileStore(tmp_path / "p.json"))


def test_damaged_file_falls_back(tmp_path):
    path = tmp_path / "p.json"
    path.write_text(json.dumps({"profiles": [{"id": "a", "name": 5}, {"id": "b", "name": "Ok"}, {"id": "b", "name": "Dup"}], "active": "zzz"}))
    store = ProfileStore(path)
    assert [p.name for p in store.items] == ["Ok"] and store.active == "b"
    path.write_text("{not json")
    assert ProfileStore(path).current.name == "Default"
