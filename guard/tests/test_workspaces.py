def test_match_returns_workspace_and_longest_key(tmp_path):
    from treant_guard.workspaces import match, resolve

    a, b = tmp_path / "a", tmp_path / "a" / "b"
    b.mkdir(parents=True)
    folders = {str(a): "outer", str(b): "inner"}
    assert match(str(b / "x"), folders) == ("inner", str(b))
    assert match(str(a / "x"), folders) == ("outer", str(a))
    assert match(str(tmp_path / "z"), folders) is None and match("", folders) is None
    assert resolve(str(b), folders) == "inner"
