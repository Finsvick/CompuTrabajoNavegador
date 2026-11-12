"""TAREA-082: `pending` lista sin repetir y manda las respuestas al banco de verdades (todo en tmp_path)."""
import pytest

from test_apply_live import add_offer, conn  # noqa: F401
from test_cv import write_workspace
from compuapply import cli, db, ui_logic

Q = "¿Tenés carnet de conducir? ¿Qué categoría?"


@pytest.fixture
def ws(tmp_path):
    write_workspace(tmp_path, key=False)
    return tmp_path, tmp_path / "profile.yaml"


def pend(conn, n, question, reason="el modelo dijo unknown"):
    id16 = add_offer(conn, n)
    db.mark_status(conn, id16, "pending_question")
    db.save_pending_question(conn, id16, question, [], reason=reason)
    return id16


def run(conn, prof, answers, ask=True):
    from compuapply import pending
    lines, it = [], iter(answers)
    done = pending.run(conn, "backend", prof, ask, input_fn=lambda prompt: next(it), out=lines.append)
    return done, lines


def status(conn, id16):
    return conn.execute("SELECT status FROM offers WHERE id16 = ?", (id16,)).fetchone()[0]


def test_list_has_no_repeats_and_counts_offers(conn, ws):
    _, prof = ws
    pend(conn, 1, Q), pend(conn, 2, "¿TENÉS CARNET DE CONDUCIR? ¿QUÉ CATEGORÍA?"), pend(conn, 3, "¿Tenés auto?")
    done, lines = run(conn, prof, [], ask=False)
    assert done == 0 and len(lines) == 2 and "[2 ofertas]" in lines[0] and "el modelo dijo unknown" in lines[0]


def test_answer_adds_the_fact_and_requeues(conn, ws):
    _, prof = ws
    a, b = pend(conn, 1, Q), pend(conn, 2, Q)
    done, _ = run(conn, prof, ["Sí, categoría B"])
    assert done == 1 and status(conn, a) == "new" and status(conn, b) == "new"
    [fact] = ui_logic.load_truths("backend", prof)
    assert "Sí, categoría B" in fact and ":" not in fact and fact.startswith("Tenés carnet de conducir")
    assert conn.execute("SELECT COUNT(*) FROM pending_questions").fetchone()[0] == 0


def test_a_dash_adds_nothing_and_is_not_listed_again(conn, ws):
    _, prof = ws
    a = pend(conn, 1, Q)
    done, _ = run(conn, prof, ["-"])
    assert done == 0 and ui_logic.load_truths("backend", prof) == [] and status(conn, a) == "pending_question"
    _, lines = run(conn, prof, [], ask=False)
    assert lines == ["No hay preguntas pendientes."]


def test_enter_skips_without_marking(conn, ws):
    _, prof = ws
    pend(conn, 1, Q)
    run(conn, prof, [""])
    _, lines = run(conn, prof, [], ask=False)
    assert len(lines) == 1


def test_a_cuil_question_does_not_ask_and_never_saves(conn, ws):
    _, prof = ws
    a = pend(conn, 1, "¿Cuál es tu CUIL?")
    done, lines = run(conn, prof, [])  # si pidiera respuesta, next() fallaría
    assert done == 0 and "contestalo vos en la oferta" in lines[0] and ui_logic.load_truths("backend", prof) == []
    from compuapply import pending
    with pytest.raises(RuntimeError):
        pending.answer(conn, pending.groups(conn)[0], "20-1", "backend", prof)
    assert status(conn, a) == "pending_question"


def test_the_cli_command_lists(ws, capsys):
    tmp, prof = ws
    c = db.connect(tmp / "t.db")
    pend(c, 1, Q)
    c.close()
    assert cli.main(["pending", "--db", str(tmp / "t.db"), "--profile", str(prof)]) == 0
    assert "carnet de conducir" in capsys.readouterr().out
