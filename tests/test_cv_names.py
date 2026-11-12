"""TAREA-070: el CV se sube con un nombre natural (la búsqueda como puesto) y la copia local conserva puesto y empresa."""
import shutil
from datetime import datetime

from fakesite import APPLICATIONS_URL, BASE_CV, FakePage, standard_site
from test_apply_live import MODEL, SALARY, TODAY, FakeClient, add_offer, conn, known_reply, make_profile, rows  # noqa: F401
from test_cv_slots import PERSON, applied, cards, days_ago, full_site, saved_cv  # noqa: F401
from compuapply import apply, cv, db

UP = "CV-Giovanni-Bianchi-"


def set_keyword(conn, id16, keyword):
    conn.execute("UPDATE offers SET keyword = ? WHERE id16 = ?", (keyword, id16))
    conn.commit()


def live(conn, tmp_path, site, archive=None):
    """run_live con el proveedor real (el CV ya guardado de cada oferta, sin llamar al modelo)."""
    page = FakePage(site)
    result = apply.run_live(conn, make_profile({"salary": SALARY}), MODEL, FakeClient(known_reply()), page, TODAY, stop_file=tmp_path / "STOP",
                            pause=lambda: None, archive_dir=archive, cv_dir=tmp_path / "cvs")
    result["page"] = page
    return result


# ── el nombre ────────────────────────────────────────────────────────────────

def test_the_natural_name_is_the_search_with_capitals_no_accents_and_hyphens():
    assert cv.natural_pdf_name(PERSON, "atención al cliente", "ventas") == UP + "Atencion-al-Cliente.pdf"
    assert cv.natural_pdf_name(PERSON, "cajero", "ventas") == UP + "Cajero.pdf"
    assert cv.natural_pdf_name(PERSON, "ATENCIÓN AL CLIENTE", "x") == UP + "Atencion-al-Cliente.pdf"


def test_without_a_search_it_uses_the_variant_id():
    assert cv.natural_pdf_name(PERSON, None, "ventas") == UP + "Ventas.pdf"
    assert cv.natural_pdf_name(PERSON, "  ", "ventas") == UP + "Ventas.pdf"


def test_numbered_name_takes_the_first_free_number():
    name = UP + "Cajero.pdf"
    assert cv.numbered_name(name, lambda n: False) == name
    assert cv.numbered_name(name, lambda n: n == name) == UP + "Cajero-2.pdf"
    assert cv.numbered_name(name, lambda n: n in (name, UP + "Cajero-2.pdf")) == UP + "Cajero-3.pdf"
    assert cv.numbered_name(name, lambda n: n in (name, UP + "Cajero-3.pdf")) == UP + "Cajero-2.pdf"
    long = cv.natural_pdf_name(PERSON, "palabra " * 20, "x")
    assert len(cv.numbered_name(long, lambda n: n == long)) - 4 <= cv.NAME_MAX


# ── subida y copia local ─────────────────────────────────────────────────────

def test_the_upload_is_the_search_and_the_local_copy_has_the_position_and_the_company(conn, tmp_path):
    id16 = add_offer(conn, 1, title="Cajero/a con Experiencia Importante", company="FABRIC SUSHI")
    set_keyword(conn, id16, "atención al cliente")
    saved_cv(conn, tmp_path, id16)
    archive = tmp_path / "cv" / "postulaciones"
    site = standard_site()
    result = live(conn, tmp_path, site, archive)
    local = "CV-Giovanni-Bianchi-Cajero-a-con-Experiencia-Importante-FABRIC-SUSHI.pdf"
    assert result["halted"] is None and site.uploads == [UP + "Atencion-al-Cliente.pdf"]
    assert "FABRIC" not in site.uploads[0] and "Cajero" not in site.uploads[0]
    assert [p.name for p in archive.iterdir()] == [local]
    assert rows(conn, "SELECT cv_file, local_file FROM applications") == [(UP + "Atencion-al-Cliente.pdf", local)]
    [sent] = result["sent"]
    assert sent["file"] == UP + "Atencion-al-Cliente.pdf" and sent["local"] == local


def test_without_a_search_it_uploads_the_variant(conn, tmp_path):
    id16 = add_offer(conn, 1, title="Vendedor", company="Acme")
    set_keyword(conn, id16, "")
    saved_cv(conn, tmp_path, id16)
    site = standard_site()
    live(conn, tmp_path, site)
    assert site.uploads == [UP + "Dev.pdf"]


def test_a_second_offer_of_the_same_search_gets_the_first_free_number(conn, tmp_path):
    ids = [add_offer(conn, n, title=f"Cajero {n}", company=f"Empresa {n}") for n in (1, 2)]
    for id16 in ids:
        set_keyword(conn, id16, "cajero")
        saved_cv(conn, tmp_path, id16)
    site = standard_site()
    result = live(conn, tmp_path, site)
    assert result["halted"] is None and sorted(site.uploads) == [UP + "Cajero-2.pdf", UP + "Cajero.pdf"]


def test_with_cajero_and_cajero_2_in_the_account_the_next_is_cajero_3(conn, tmp_path):
    applied(conn, 11, UP + "Cajero.pdf", "IM11")
    applied(conn, 12, UP + "Cajero-2.pdf", "IM12")
    new = add_offer(conn, 5, title="Cajero 5")
    set_keyword(conn, new, "cajero")
    saved_cv(conn, tmp_path, new)
    site = standard_site(cvs=(BASE_CV, UP + "Cajero.pdf", UP + "Cajero-2.pdf"), default=BASE_CV)
    live(conn, tmp_path, site)
    assert site.uploads == [UP + "Cajero-3.pdf"]


def test_when_the_rotation_deletes_cajero_the_next_one_uses_that_name_again(conn, tmp_path):
    applied(conn, 11, UP + "Cajero.pdf", "IM11", days_ago(30))
    applied(conn, 12, UP + "Cajero-2.pdf", "IM12", days_ago(3))
    apps = (("IM11", "A", "Acme", "Postulado"), ("IM12", "B", "Beta", "Postulado"))
    new = add_offer(conn, 5, title="Cajero 5")
    set_keyword(conn, new, "cajero")
    saved_cv(conn, tmp_path, new)
    site = full_site(UP + "Cajero.pdf", UP + "Cajero-2.pdf", apps=apps)
    result = live(conn, tmp_path, site)
    assert result["halted"] is None and site.deleted == [UP + "Cajero.pdf"] and site.uploads == [UP + "Cajero.pdf"]


def test_the_rotation_deletes_by_the_uploaded_name_and_the_row_keeps_the_local_copy(conn, tmp_path):
    old = applied(conn, 11, UP + "Cajero.pdf", "IM11", days_ago(30))
    conn.execute("UPDATE applications SET local_file = 'CV-Giovanni-Bianchi-Cajero-Acme.pdf' WHERE id16 = ?", (old,))
    conn.commit()
    new = add_offer(conn, 5, title="Cajero 5")
    saved_cv(conn, tmp_path, new)
    site = full_site(UP + "Cajero.pdf", apps=(("IM11", "A", "Acme", "Postulado"),))
    result = live(conn, tmp_path, site)
    assert site.deleted == [UP + "Cajero.pdf"]
    assert result["deleted"][0]["name"] == UP + "Cajero.pdf" and result["deleted"][0]["local"] == "CV-Giovanni-Bianchi-Cajero-Acme.pdf"
    assert rows(conn, f"SELECT cv_file, local_file FROM applications WHERE id16 = '{old}'") == [(UP + "Cajero.pdf", "CV-Giovanni-Bianchi-Cajero-Acme.pdf")]
    meta = {"now": datetime(2026, 11, 7, 10, 0, 0), "threshold": 70, "daily_cap": 10, "applied_today": 0, "model": MODEL}
    text = apply.write_live_report(result, meta, tmp_path / "runs").read_text(encoding="utf-8")
    assert f"CV borrado de la cuenta: {UP}Cajero.pdf, copia local CV-Giovanni-Bianchi-Cajero-Acme.pdf" in text


def test_an_old_row_is_migrated_with_both_names_equal(tmp_path):
    path = tmp_path / "old.db"
    conn = db.connect(path)
    conn.execute("ALTER TABLE applications DROP COLUMN local_file")
    conn.execute("INSERT INTO applications (id16, variant_id, score, status, reason, applied_at, qa, cv_file) "
                 "VALUES ('A', 'dev', 80, 'applied', 'ok', '2026-10-01', '[]', 'CV-Viejo.pdf')")
    conn.commit()
    conn.close()
    conn = db.connect(path)
    assert rows(conn, "SELECT cv_file, local_file FROM applications") == [("CV-Viejo.pdf", "CV-Viejo.pdf")]
    conn.close()


def test_save_application_without_local_uses_the_uploaded_name(conn):
    db.save_application(conn, "A", "dev", 80, "applied", "ok", [], "CV-X.pdf", None, "c8")
    db.save_application(conn, "B", "dev", 80, "applied", "ok", [], "CV-Y.pdf", None, "c8", "CV-Y-Acme.pdf")
    assert rows(conn, "SELECT id16, cv_file, local_file FROM applications ORDER BY id16") == [("A", "CV-X.pdf", "CV-X.pdf"), ("B", "CV-Y.pdf", "CV-Y-Acme.pdf")]


def test_reusing_the_cv_of_an_offer_works_with_both_names(conn, tmp_path):
    id16 = add_offer(conn, 1, title="Cajero 1")
    set_keyword(conn, id16, "cajero")
    saved_cv(conn, tmp_path, id16)
    db.save_application(conn, id16, "dev", 85, "pending_question", "x", [], UP + "Cajero.pdf", None, cv.CV_PROMPT_VERSION, "CV-Giovanni-Bianchi-Cajero-1-Empresa-1.pdf")
    db.mark_status(conn, id16, "new")
    archive = tmp_path / "cv" / "postulaciones"
    archive.mkdir(parents=True)
    (archive / "CV-Giovanni-Bianchi-Cajero-1-Empresa-1.pdf").write_bytes(b"%PDF-" + b"0" * 100)
    site = standard_site(cvs=(BASE_CV, UP + "Cajero.pdf"), default=BASE_CV)
    result = live(conn, tmp_path, site, archive)
    assert result["halted"] is None and site.uploads == [] and result["pdfs"]["reused"] == 1
    assert rows(conn, "SELECT cv_file, local_file FROM applications") == [(UP + "Cajero.pdf", "CV-Giovanni-Bianchi-Cajero-1-Empresa-1.pdf")]


def test_the_report_shows_uploaded_as_and_local_copy(conn, tmp_path):
    id16 = add_offer(conn, 1, title="Cajero 1", company="Acme")
    set_keyword(conn, id16, "cajero")
    saved_cv(conn, tmp_path, id16)
    result = live(conn, tmp_path, standard_site(), tmp_path / "cv" / "postulaciones")
    meta = {"now": datetime(2026, 11, 7, 10, 0, 0), "threshold": 70, "daily_cap": 10, "applied_today": 0, "model": MODEL}
    text = apply.write_live_report(result, meta, tmp_path / "runs").read_text(encoding="utf-8")
    assert f"subido como {UP}Cajero.pdf · copia local {UP}Cajero-1-Acme.pdf" in text
