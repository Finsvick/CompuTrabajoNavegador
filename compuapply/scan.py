"""Descubrimiento de ofertas en el listado de ar.computrabajo.com (solo lectura)."""
import json
import random
import re
import sqlite3
import time
import unicodedata
from pathlib import Path
from urllib.parse import parse_qs, urljoin, urlsplit

import httpx
from bs4 import BeautifulSoup

from compuapply import db
from compuapply.profile import in_zone

BASE = "https://ar.computrabajo.com"
DETAIL_PREFIX = f"{BASE}/ofertas-de-trabajo/"
TIMEOUT = 30
HEADERS = {
    "Accept": "text/html",
    "Accept-Language": "es-AR,es;q=0.9",
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
    ),
}


def slug(keyword: str) -> str:
    """Palabra clave a slug de URL: sin acentos, minúsculas, el resto a guiones."""
    ascii_ = unicodedata.normalize("NFKD", keyword).encode("ascii", "ignore").decode("ascii")
    return re.sub(r"[^a-z0-9]+", "-", ascii_.lower()).strip("-")


ZONE_SLUGS = {"capital federal": "capital-federal", "buenos aires-gba": "buenos-aires-gba"}  # zona (como la muestra el sitio) a su slug de URL


def zone_slugs(zones) -> list[str | None]:
    """Los slugs con que se busca por zona. Con `zones` vacía o con una zona sin slug conocido, `[None]`: la búsqueda nacional."""
    found = [ZONE_SLUGS.get(" ".join(str(z).split()).casefold()) for z in zones or []]
    return found if found and all(found) else [None]


def search_url(keyword: str, page: int, zone: str | None = None) -> str:
    """URL del listado para una palabra clave, un número de página (desde 1) y, si hay, el slug de una zona (`-en-<zona>`)."""
    return f"{BASE}/trabajo-de-{slug(keyword)}" + (f"-en-{zone}" if zone else "") + f"?p={page}"


def _clean(text: str) -> str:
    return " ".join(text.split())


def parse_listing(html: str) -> list[dict]:
    """Ofertas de una página de listado (`article[data-id]`)."""
    offers = []
    for art in BeautifulSoup(html, "html.parser").select("article[data-id]"):
        data_id = art.get("data-id") or ""
        if not data_id:
            continue
        title_a = art.select_one("h2 a")
        company_a = art.select_one("a[offer-grid-article-company-url]") or art.select_one("p a.t_ellipsis")
        # El rating (`span.fx_none.mr10`) viene antes que la ubicación y nunca lo es.
        location_el = art.select_one("span.mr10:not(.fx_none)")
        href = (title_a.get("href") or "").split("#")[0] if title_a else ""
        offers.append({
            "id16": data_id[:16],
            "data_id": data_id,
            "title": _clean(title_a.get_text()) if title_a else "",
            "company": _clean(company_a.get_text()) if company_a else "",
            "location": _clean(location_el.get_text()) if location_el else "",
            "url": urljoin(BASE, href) if href else "",
        })
    return offers


NO_OFFERS_TEXT = "parece que no hay ofertas"  # lo que dice el listado de una búsqueda sin resultados (con 200)


def no_offers_notice(html: str) -> bool:
    """Si el listado dice «Parece que no hay ofertas para el empleo que buscas»: una búsqueda sin resultados, no un cambio del sitio."""
    return NO_OFFERS_TEXT in " ".join(BeautifulSoup(html, "html.parser").get_text(" ").split()).casefold()


def _check_stop(stop_file: Path, antes_de: str) -> None:
    if stop_file.exists():
        raise RuntimeError(
            f"Existe el archivo {stop_file}: se frena la corrida antes de pedir {antes_de}. "
            "Borralo para volver a correr."
        )


class Moved(RuntimeError):
    """El detalle de una oferta respondió 301/302: la oferta venció."""


def _get(client: httpx.Client, url: str, que: str) -> str:
    """GET sin reintentos ni redirecciones; devuelve el HTML o lanza RuntimeError."""
    return _check(_request(client, url, que), que)


def _request(client: httpx.Client, url: str, que: str):
    try:
        return client.get(url, headers=HEADERS, follow_redirects=False)
    except httpx.HTTPError as e:
        raise RuntimeError(f"No se pudo leer {que}: {e}. Revisá la conexión.") from e


def _check(resp, que: str) -> str:
    """El HTML de una respuesta 200; si no, RuntimeError (o `Moved` si es el detalle de una oferta que venció)."""
    code = resp.status_code
    if code in (301, 302) and que.startswith("el detalle"):
        raise Moved(f"Computrabajo respondió {code} en {que}: la oferta ya no está.")
    if code != 200:
        motivo = {403: "acceso bloqueado", 429: "demasiadas solicitudes"}.get(
            code, "error del servidor" if code >= 500 else "respuesta inesperada")
        raise RuntimeError(
            f"Computrabajo respondió {code} ({motivo}) en {que}. "
            "Se frena la corrida, sin reintentar. Esperá un rato antes de volver a correr."
        )
    return resp.text


LISTING_REDIRECTS = (301, 308)
_NATIONAL_ZONE = re.compile(r"-en-(?:capital-federal|buenos-aires-gba)$")


def canonical_path(location: str, page: int, zone: str | None) -> str | None:
    """El camino canónico de un listado al que el sitio redirige, o None si no se puede seguir: tiene que ser del mismo host, empezar con
    `/trabajo-de-`, terminar con el mismo sufijo de zona (`-en-<zona>`; sin zona, sin sufijo de zona) y conservar `p=<página>`."""
    parts, base = urlsplit(urljoin(BASE, location or "")), urlsplit(BASE)
    path = parts.path
    if (parts.scheme, parts.netloc) != (base.scheme, base.netloc) or not path.startswith("/trabajo-de-") or len(path) <= len("/trabajo-de-"):
        return None
    if (not path.endswith(f"-en-{zone}")) if zone else _NATIONAL_ZONE.search(path):
        return None
    return path if parse_qs(parts.query).get("p") == [str(page)] else None


def site_slug(path: str, zone: str | None) -> str:
    """El slug de la búsqueda que usa el sitio (sin `/trabajo-de-` ni el sufijo de zona)."""
    body = path.removeprefix("/trabajo-de-")
    return body.removesuffix(f"-en-{zone}") if zone else body


def get_listing(client: httpx.Client, keyword: str, page: int, zone: str | None, que: str, canon: dict) -> str:
    """El HTML de una página de listado. Si el sitio responde 301 o 308 a su camino canónico (`canonical_path`), se sigue **una sola vez** y el
    camino queda en `canon[(slug, zona)]` para las páginas siguientes. Cualquier otra redirección, o una segunda, frena como siempre."""
    key = (slug(keyword), zone)
    url = f"{BASE}{canon[key]}?p={page}" if key in canon else search_url(keyword, page, zone)
    resp = _request(client, url, que)
    if resp.status_code in LISTING_REDIRECTS:
        path = canonical_path(resp.headers.get("location", ""), page, zone)
        if path is not None:
            html = _check(_request(client, f"{BASE}{path}?p={page}", que), que)  # un segundo 301 frena
            canon[key] = path
            return html
    return _check(resp, que)


def collect(keyword: str, pages: int, client: httpx.Client | None = None,
            stop_file: Path = Path("STOP"), zones=()) -> tuple[list[dict], int]:
    """Recorre el listado y devuelve (ofertas sin repetir por id16, páginas leídas). Con `zones` (ver `zone_slugs`) lee `pages` páginas de
    cada zona; sin zonas o con una sin slug, la búsqueda nacional.

    Frena con RuntimeError ante 403/429/5xx, respuesta inesperada, página 1 vacía
    o archivo STOP. Nunca reintenta; solo sigue (una vez) la redirección de un listado a su camino canónico (`get_listing`). Una página vacía que
    corta la búsqueda cuenta como leída.
    """
    if not slug(keyword):
        raise RuntimeError("La palabra clave está vacía. Pasá --keyword con al menos una letra o número.")
    if pages < 1:
        raise RuntimeError("--pages tiene que ser 1 o más.")
    own_client = client is None
    client = client or httpx.Client(timeout=TIMEOUT)
    offers: dict[str, dict] = {}
    pages_read, canon = 0, {}
    try:
        for zone in zone_slugs(zones):
            for page in range(1, pages + 1):
                if page > 1 or pages_read:
                    time.sleep(random.uniform(0.7, 1.5))
                _check_stop(stop_file, f"la página {page}")
                html = get_listing(client, keyword, page, zone, f"la página {page}", canon)
                pages_read += 1
                found = parse_listing(html)
                if not found:
                    if page == 1 and not no_offers_notice(html):
                        raise RuntimeError(
                            "La página 1 no trajo ofertas (posible cambio de layout o desafío). "
                            "Se frena la corrida; revisá el sitio a mano."
                        )
                    break
                for offer in found:
                    offers.setdefault(offer["id16"], offer)
    finally:
        if own_client:
            client.close()
    return list(offers.values()), pages_read


class DetailMissing(RuntimeError):
    """El detalle no trae la descripción ni en el JSON-LD ni en el HTML."""


def _clean_lines(text: str) -> str:
    """Texto con cada línea recortada y sin líneas vacías repetidas."""
    lines = []
    for line in text.splitlines():
        line = line.strip()
        if line or (lines and lines[-1]):
            lines.append(line)
    return "\n".join(lines).strip()


def _description_text(raw: str) -> str:
    """Descripción del JSON-LD (trae `<br/>`) a texto plano, sin líneas vacías repetidas."""
    return _clean_lines(BeautifulSoup(raw, "html.parser").get_text("\n"))


def _posting(soup: BeautifulSoup) -> dict:
    """El `JobPosting` del primer JSON-LD, o `{}` si no hay script, el JSON está roto o no trae uno."""
    script = soup.select_one('script[type="application/ld+json"]')
    if script is None:
        return {}
    try:
        data = json.loads(script.string or "")
    except json.JSONDecodeError:
        return {}
    if isinstance(data, dict):
        data = data.get("@graph", [data])
    postings = [i for i in data if isinstance(i, dict) and i.get("@type") == "JobPosting"] if isinstance(data, list) else []
    return postings[0] if postings else {}


def parse_detail(html: str) -> dict:
    """Descripción y fechas del detalle: del `JobPosting` del JSON-LD o, si no hay, del bloque de la oferta en el HTML.

    Las ofertas de empresa confidencial no traen `JobPosting`: su descripción está en `div[div-link="oferta"]`
    y no tienen fechas ni tipo de contrato (quedan en ""). Lanza `DetailMissing` si no hay descripción en ningún lado.
    """
    soup = BeautifulSoup(html, "html.parser")
    posting = _posting(soup)
    description = _description_text(str(posting.get("description") or ""))
    if description:
        return {
            "description": description,
            "date_posted": str(posting.get("datePosted") or ""),
            "valid_through": str(posting.get("validThrough") or ""),
            "employment_type": str(posting.get("employmentType") or ""),
        }
    block = soup.select_one('div[div-link="oferta"]')
    description = _clean_lines(block.get_text("\n")) if block else ""
    heading, _, rest = description.partition("\n")
    if heading == "Descripción de la oferta":
        description = rest.strip()
    if not description:
        raise DetailMissing("El detalle no trae un JobPosting con descripción ni el bloque de la oferta: posible cambio de layout.")
    return {"description": description, "date_posted": "", "valid_through": "", "employment_type": ""}


def fetch_details(conn: sqlite3.Connection, limit: int, client: httpx.Client | None = None,
                  stop_file: Path = Path("STOP"), zones=(), only: set | None = None) -> int:
    """Baja el detalle de hasta `limit` ofertas pendientes y devuelve cuántas guardó.

    Con `zones` (provincias, ver `profile.in_zone`) no baja el detalle de las ofertas de otra zona ni de las que no traen lugar.
    Con `only` (un conjunto de `id16`) solo baja el de esas ofertas (la tanda).

    El `limit` se reparte entre las keywords pendientes por turnos: una oferta de cada keyword por vuelta, y dentro de cada
    keyword la de `first_seen` más nueva primero (las de keyword vacía van como una keyword más).
    Guarda cada detalle apenas lo baja, así que si frena a mitad lo ya bajado queda.
    Solo pide URLs de `DETAIL_PREFIX`: una oferta con otra URL (o vacía) se marca
    `bad_url`, no se pide y no cuenta para `limit`; 3 seguidas frenan la corrida.
    Una oferta cuyo detalle no trae descripción (`DetailMissing`) se marca `no_detail`, tampoco cuenta
    para `limit` y se sigue; 3 seguidas frenan la corrida. Un 301/302 en el detalle (oferta vencida) la marca `gone`, tampoco cuenta
    y se sigue; 3 seguidas frenan con «posible bloqueo o cambio del sitio». Un detalle guardado reinicia los contadores.
    """
    if limit < 0:
        raise RuntimeError("--max-details no puede ser negativo.")
    groups: dict[str, list] = {}
    for id16, url, keyword, location in conn.execute(
            "SELECT id16, url, keyword, location FROM offers WHERE detail_fetched_at IS NULL AND status = 'new' "
            "ORDER BY first_seen DESC, rowid"):
        if not in_zone(location, zones) or (only is not None and id16 not in only):
            continue
        groups.setdefault(" ".join((keyword or "").split()).casefold(), []).append((id16, url))
    rounds = max(map(len, groups.values()), default=0)
    pending = [group[n] for n in range(rounds) for group in groups.values() if n < len(group)]
    own_client = client is None
    client = client or httpx.Client(timeout=TIMEOUT)
    saved = bad_streak = missing_streak = gone_streak = 0
    try:
        for id16, url in pending:
            if saved >= limit:
                break
            valid = (url or "").startswith(DETAIL_PREFIX)
            if valid:
                time.sleep(random.uniform(1.0, 2.5))
            _check_stop(stop_file, f"el detalle de la oferta {id16}")
            if not valid:
                db.mark_status(conn, id16, "bad_url")
                bad_streak += 1
                if bad_streak == 3:
                    raise RuntimeError(
                        "3 URLs inválidas seguidas: posible cambio de layout en el listado. Se frena la corrida."
                    )
                continue
            try:
                html = _get(client, url, f"el detalle de la oferta {id16}")
            except Moved:
                db.mark_status(conn, id16, "gone")
                gone_streak += 1
                if gone_streak == 3:
                    raise RuntimeError("3 ofertas seguidas respondieron 301/302 en el detalle: posible bloqueo o cambio del sitio") from None
                continue
            gone_streak = 0
            try:
                detail = parse_detail(html)
            except DetailMissing:
                db.mark_status(conn, id16, "no_detail")
                missing_streak += 1
                if missing_streak == 3:
                    raise RuntimeError("3 ofertas seguidas sin descripción: posible cambio de layout") from None
                continue
            db.save_detail(conn, id16, detail)
            saved += 1
            bad_streak = missing_streak = 0
    finally:
        if own_client:
            client.close()
    return saved
