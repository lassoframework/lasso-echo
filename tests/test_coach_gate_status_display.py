from agent import __main__ as agent_main


def test_status_labels_coach_gates_retired(monkeypatch, capsys):
    monkeypatch.setattr(agent_main.config, "gbp_coach_screen_enabled", lambda: False)
    monkeypatch.setattr(agent_main.config, "coach_screen_first_month_enabled", lambda: False)

    agent_main._status()

    output = capsys.readouterr().out
    assert "gbp_coach_scrn : False  (retired; predicate is always false;" in output
    assert "coach_scrn_m1  : False  (retired; predicate is always false;" in output
    coach_lines = [line for line in output.splitlines()
                   if "gbp_coach_scrn" in line or "coach_scrn_m1" in line]
    assert len(coach_lines) == 2
    assert all("default ON" not in line for line in coach_lines)
