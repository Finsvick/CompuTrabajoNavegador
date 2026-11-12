"""Tests de TAREA-051: CVs viejos de la cuenta (sin cv_file), copias de más de 30 días y pendientes que ya tienen respuesta. Todo falso."""
import os
import time
from datetime import datetime, timedelta, timezone

import pytest

from fakesite import APPLICATIONS_URL, BASE_CV, FakePage, standard_site
from test_apply import cli_env, seeded_db  # noqa: F401
from test_apply_live import MODEL, SALARY, TODAY, FakeClient, add_offer, conn, known_reply, make_profile, reply, rows, status_of  # noqa: F401
from test_cv_slots import applied, cards, days_ago, nine_and_default, report, saved_cv  # noqa: F401
from compuapply import apply, browser, cli, cv, db, profile

OLD_NAME = "CV-Giovanni-Bianchi-Titular-Viejo.pdf"  # lo que daba `pdf_name` con el titular del CV guardado
NEW_NAME = "CV-Giovanni-Bianchi-Oferta-5-Empresa-5.pdf"


def saved_with_headline(conn, tmp_path, id16, headline="Titular Viejo", version=None):
    html = tmp_path / "cvs" / f"{id16}-dev.html"
    html.parent.mkdir(exist_ok=True)
    html.write_text(f'<html><body><div class="headline">{headline}</div></body></html>', encoding="utf-8")
    html.with_suffix(".pdf").write_bytes(b"%PDF-" + b"0" * 100)
    db.save_cv(conn, id16, "dev", MODEL, version or cv.CV_PROMPT_VERSION, str(html), str(html.with_suffix(".pdf")), 0.0, 1, 1)


def old_pending(conn, tmp_path, n, status="pending_question", headline="Titular Viejo", points=80):
    """Una oferta con un CV subido antes de la 042: fila de `applications` sin `cv_file` ni versión."""
    id16 = add_offer(conn, n, points=points, title=f"Oferta {n}", company=f"Empresa {n}")
    saved_with_headline(conn, tmp_path, id16, headline)
    db.save_application(conn, id16, "dev", points, status, "x", [], None, None, None)
    db.mark_status(conn, id16, "pending_question")
    return id16


def cvs_of(*names):
    return [{"name": name, "default": False, "id": f"fc{n}"} for n, name in enumerate(names, 1)]


# ── completar cv_file ────────────────────────────────────────────────────────

def test_a_pending_row_without_cv_file_gets_the_old_name_when_the_account_has_that_pdf(conn, tmp_path):
    id16 = old_pending(conn, tmp_path, 2)
    assert apply.complete_cv_files(conn, make_profile(), cvs_of(BASE_CV, OLD_NAME)) == 1
    assert rows(conn, "SELECT cv_file, cv_version FROM applications WHERE id16 = '%s'" % id16) == [(OLD_NAME, cv.CV_PROMPT_VERSION)]


def test_a_cv_uploaded_row_is_completed_too(conn, tmp_path):
    old_pending(conn, tmp_path, 2, status="cv_uploaded")
    assert apply.complete_cv_files(conn, make_profile(), cvs_of(OLD_NAME)) == 1


def test_nothing_is_completed_if_the_account_does_not_have_that_pdf_or_the_saved_cv_has_another_version(conn, tmp_path):
    old_pending(conn, tmp_path, 2)
    assert apply.complete_cv_files(conn, make_profile(), cvs_of(BASE_CV)) == 0
    other = add_offer(conn, 3)
    saved_with_headline(conn, tmp_path, other, "Otro", version="c4")
    db.save_application(conn, other, "dev", 80, "pending_question", "x", [], None, None, None)
    assert apply.complete_cv_files(conn, make_profile(), cvs_of(OLD_NAME, "CV-Giovanni-Bianchi-Otro.pdf")) == 1  # solo la de la versión actual


def test_two_offers_with_the_same_old_name_are_not_completed(conn, tmp_path):
    old_pending(conn, tmp_path, 2)
    old_pending(conn, tmp_path, 3)
    assert apply.complete_cv_files(conn, make_profile(), cvs_of(OLD_NAME, OLD_NAME)) == 0
    assert rows(conn, "SELECT cv_file FROM applications WHERE cv_file IS NOT NULL AND cv_file != ''") == []


def test_a_row_that_already_has_cv_file_is_not_touched(conn, tmp_path):
    id16 = old_pending(conn, tmp_path, 2)
    conn.execute("UPDATE applications SET cv_file = 'Otro.pdf' WHERE id16 = ?", (id16,))
    assert apply.complete_cv_files(conn, make_profile(), cvs_of(OLD_NAME)) == 0
    assert rows(conn, "SELECT cv_file FROM applications") == [("Otro.pdf",)]


def run(conn, tmp_path, site, *, prof=None, archive=None, client=None):
    page = FakePage(site)
    result = apply.run_live(conn, prof or make_profile({"salary": SALARY}), MODEL, client or FakeClient(known_reply()), page, TODAY,
                            stop_file=tmp_path / "STOP", pause=lambda: None, cv_dir=tmp_path / "cvs", archive_dir=archive,
                            cv_provider=apply.make_cv_provider(conn, prof or make_profile({"salary": SALARY}), None, MODEL, tmp_path / "cvs"))
    result["page"] = page
    return result


def test_with_3_old_copies_it_completes_deletes_one_copy_and_reuses_the_cv_without_uploading_it_again(conn, tmp_path):
    new = add_offer(conn, 5, points=90, title="Oferta 5", company="Empresa 5")
    saved_cv(conn, tmp_path, new)
    pending = old_pending(conn, tmp_path, 2, points=80)
    db.mark_status(conn, pending, "new")  # el usuario ya contestó: vuelve a la cola
    conn.execute("DELETE FROM pending_questions")
    names = (BASE_CV, OLD_NAME, OLD_NAME, OLD_NAME, *[f"Relleno-{n}.pdf" for n in range(6)])
    site = standard_site(cvs=names, default=BASE_CV, max_docs=10, pages={APPLICATIONS_URL: cards()})
    result = run(conn, tmp_path, site)
    assert result["halted"] is None and result["completed"] == 1 and sorted(i["id16"] for i in result["sent"]) == sorted([new, pending])
    assert site.deleted == [OLD_NAME] and site.cvs.count(OLD_NAME) == 2  # la regla de las copias de una oferta no enviada
    assert site.uploads == ["CV-Giovanni-Bianchi-X.pdf"]  # solo el CV de la oferta nueva: el de la pendiente se reusó
    assert result["pdfs"]["reused"] == 1 and rows(conn, "SELECT cv_file FROM applications WHERE id16 = '%s'" % pending) == [(OLD_NAME,)]
    assert "- Filas de applications completadas con el archivo ya subido (cv_file): 1" in report(result, tmp_path)


def test_a_retry_after_completing_does_not_upload_another_copy(conn, tmp_path):
    pending = old_pending(conn, tmp_path, 2, points=85)
    db.mark_status(conn, pending, "new")
    site = standard_site(cvs=(BASE_CV, OLD_NAME), default=BASE_CV)
    result = run(conn, tmp_path, site)
    assert [i["id16"] for i in result["sent"]] == [pending] and site.uploads == [] and result["pdfs"]["reused"] == 1


# ── 30 días en la PC ─────────────────────────────────────────────────────────

def make_pdf(folder, name, days):
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / name
    path.write_bytes(b"%PDF-1")
    stamp = time.time() - days * 86400
    os.utime(path, (stamp, stamp))
    return path


def test_prune_archive_deletes_the_pdfs_of_more_than_30_days_and_keeps_the_rest(tmp_path):
    folder = tmp_path / "cv" / "postulaciones"
    make_pdf(folder, "viejo.pdf", 31)
    make_pdf(folder, "reciente.pdf", 29)
    (folder / "notas.txt").write_text("x", encoding="utf-8")
    os.utime(folder / "notas.txt", (1, 1))
    assert apply.prune_archive(folder) == 1
    assert sorted(p.name for p in folder.iterdir()) == ["notas.txt", "reciente.pdf"]
    assert apply.prune_archive(tmp_path / "no-existe") == 0


def test_prune_archive_only_touches_that_folder(tmp_path):
    folder = tmp_path / "cv" / "postulaciones"
    folder.mkdir(parents=True)
    outside = make_pdf(tmp_path / "cv", "CV-Real.pdf", 400)
    nested = make_pdf(folder / "sub", "dentro.pdf", 400)
    assert apply.prune_archive(folder) == 0 and outside.is_file() and nested.is_file()


def test_run_live_prunes_the_archive_at_the_start_and_the_report_says_how_many(conn, tmp_path):
    folder = tmp_path / "cv" / "postulaciones"
    make_pdf(folder, "viejo.pdf", 31)
    make_pdf(folder, "reciente.pdf", 29)
    result = run(conn, tmp_path, standard_site(), archive=folder)  # sin ofertas que enviar
    assert [p.name for p in folder.iterdir()] == ["reciente.pdf"] and result["plan"]["archive_pruned"] == 1
    assert "- Copias de más de 30 días borradas de cv/postulaciones/: 1" in report(result, tmp_path)


def test_the_dry_run_also_prunes_the_archive_and_says_so(cli_env, capsys):
    seeded_db(cli_env).close()
    folder = cli_env / "cv" / "postulaciones"
    make_pdf(folder, "viejo.pdf", 31)
    make_pdf(folder, "reciente.pdf", 29)
    from test_apply import cli_args
    assert cli.main(cli_args(cli_env)) == 0
    assert [p.name for p in folder.iterdir()] == ["reciente.pdf"]
    [report_path] = (cli_env / "data" / "runs").glob("*-apply-seco.md")
    assert "- Copias de más de 30 días borradas de cv/postulaciones/: 1" in report_path.read_text(encoding="utf-8")
    assert "Copias de más de 30 días borradas de cv/postulaciones/: 1" in capsys.readouterr().out


# ── 30 días en la cuenta ─────────────────────────────────────────────────────

def full(*extra, apps=()):
    return standard_site(cvs=nine_and_default(*extra), default=BASE_CV, max_docs=10, pages={APPLICATIONS_URL: cards(*apps)})


def run_new(conn, tmp_path, site):
    add_offer(conn, 5, title="Oferta 5", company="Empresa 5")
    from test_apply_live import stub_cv
    page = FakePage(site)
    result = apply.run_live(conn, make_profile({"salary": SALARY}), MODEL, FakeClient(known_reply()), page, TODAY, stop_file=tmp_path / "STOP",
                            pause=lambda: None, cv_provider=stub_cv(name=NEW_NAME))
    return result


def test_a_postulado_cv_of_31_days_is_deletable_when_a_slot_is_needed(conn, tmp_path):
    applied(conn, 11, "CV-Vieja.pdf", "IM11", days_ago(31))
    site = full("CV-Vieja.pdf", apps=(("IM11", "A", "Acme", "Postulado"),))
    result = run_new(conn, tmp_path, site)
    assert result["halted"] is None and site.deleted == ["CV-Vieja.pdf"]
    assert [d["name"] for d in result["deleted"]] == ["CV-Vieja.pdf"] and result["deleted"][0]["reason"].startswith("rotación: el CV de la postulación más vieja (")


def test_a_postulado_cv_of_10_days_is_rotated_too(conn, tmp_path):
    applied(conn, 11, "CV-Reciente.pdf", "IM11", days_ago(10))
    site = full("CV-Reciente.pdf", apps=(("IM11", "A", "Acme", "Postulado"),))
    result = run_new(conn, tmp_path, site)
    assert result["halted"] is None and site.deleted == ["CV-Reciente.pdf"] and len(site.uploads) == 1  # TAREA-062: rotación en cualquier estado


def test_the_rotation_ignores_the_state_and_takes_the_oldest(conn, tmp_path):
    applied(conn, 11, "CV-Vieja.pdf", "IM11", days_ago(90))
    applied(conn, 12, "CV-Terminada.pdf", "IM12", days_ago(20))
    site = full("CV-Vieja.pdf", "CV-Terminada.pdf", apps=(("IM11", "A", "Acme", "Postulado"), ("IM12", "B", "Acme", "Proceso finalizado")))
    run_new(conn, tmp_path, site)
    assert site.deleted == ["CV-Vieja.pdf"]


def test_the_oldest_postulation_goes_first(conn, tmp_path):
    applied(conn, 11, "CV-A.pdf", "IM11", days_ago(40))
    applied(conn, 12, "CV-B.pdf", "IM12", days_ago(80))
    site = full("CV-A.pdf", "CV-B.pdf", apps=(("IM11", "A", "Acme", "Postulado"), ("IM12", "B", "Acme", "Postulado")))
    run_new(conn, tmp_path, site)
    assert site.deleted == ["CV-B.pdf"]


def test_a_shared_file_belongs_to_its_newest_row_so_the_pending_one_goes_first_and_the_shared_one_stays(conn, tmp_path):
    applied(conn, 11, "CV-Compartida.pdf", "IM11", days_ago(60))
    applied(conn, 12, "CV-Compartida.pdf", "IM12", days_ago(3))  # una de menos de 30 días usa el mismo archivo
    applied(conn, 13, "CV-ConPendiente.pdf", "IM13", days_ago(60))
    pending = add_offer(conn, 14)
    db.mark_status(conn, pending, "pending_question")
    db.save_application(conn, pending, "dev", 80, "pending_question", "x", [], "CV-ConPendiente.pdf", None, "c8")
    apps = (("IM11", "A", "Acme", "Postulado"), ("IM12", "B", "Acme", "Postulado"), ("IM13", "C", "Acme", "Postulado"))
    site = full("CV-Compartida.pdf", "CV-ConPendiente.pdf", apps=apps)
    result = run_new(conn, tmp_path, site)
    assert result["halted"] is None and site.deleted == ["CV-ConPendiente.pdf"]  # el archivo es de la fila más nueva (la pendiente, sin enviar)


# ── pendientes que ya tienen respuesta ───────────────────────────────────────

def pending_offer(conn, n, question, status="pending_question"):
    id16 = add_offer(conn, n)
    db.mark_status(conn, id16, status)
    db.save_pending_question(conn, id16, question, [])
    return id16


def test_a_pending_pay_question_goes_back_to_the_queue_when_salary_is_loaded(conn):
    id16 = pending_offer(conn, 1, "Indicar remuneración pretendida")
    assert apply.requeue_answerable(conn, make_profile({"salary": SALARY})) == 1
    assert status_of(conn, id16) == "new" and rows(conn, "SELECT * FROM pending_questions") == []


def test_without_salary_the_pay_question_stays_pending(conn):
    id16 = pending_offer(conn, 1, "Indicar remuneración pretendida")
    assert apply.requeue_answerable(conn, make_profile()) == 0 and status_of(conn, id16) == "pending_question"
    assert len(rows(conn, "SELECT * FROM pending_questions")) == 1


def test_a_question_whose_key_is_in_answers_goes_back(conn):
    question = "¿Tenés auto propio?"
    id16 = pending_offer(conn, 1, question)
    assert apply.requeue_answerable(conn, make_profile({profile.normalize_question(question): "Sí"})) == 1 and status_of(conn, id16) == "new"


def test_an_offer_with_one_answerable_and_one_not_stays_pending(conn):
    id16 = pending_offer(conn, 1, "Indicar remuneración pretendida")
    db.save_pending_question(conn, id16, "¿Tenés auto propio?", [])
    assert apply.requeue_answerable(conn, make_profile({"salary": SALARY})) == 0
    assert status_of(conn, id16) == "pending_question" and len(rows(conn, "SELECT * FROM pending_questions")) == 2


def test_only_offers_in_pending_question_are_requeued(conn):
    id16 = pending_offer(conn, 1, "Indicar remuneración pretendida", status="applied")
    assert apply.requeue_answerable(conn, make_profile({"salary": SALARY})) == 0 and status_of(conn, id16) == "applied"


def test_run_live_requeues_before_the_plan_and_the_report_says_how_many(conn, tmp_path):
    id16 = pending_offer(conn, 1, "Indicar remuneración pretendida")
    result = run(conn, tmp_path, standard_site(cvs=(BASE_CV,), default=BASE_CV))
    assert result["requeued"] == 1 and [i["id16"] for i in result["plan"]["send"]] == [id16]
    assert "- Ofertas con pregunta que ya tiene respuesta o que quedaron pendientes con instrucciones viejas, de vuelta a la cola: 1" in report(result, tmp_path)
