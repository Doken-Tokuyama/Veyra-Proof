from veyra_proof.tool_registry import select_tools


def test_profile_quick_selection():
    tools = select_tools("quick")
    ids = [t.id for t in tools]
    assert "clippy" in ids
    assert "rustfmt" in ids
    assert "miri" not in ids
    assert "mutants" not in ids


def test_include_exclude():
    tools = select_tools(
        "standard",
        include=["clippy", "rustfmt", "miri"],
        exclude=["miri"],
    )
    ids = set(t.id for t in tools)
    assert ids == {"rustfmt", "clippy"}
