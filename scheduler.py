from __future__ import annotations

import hashlib
import json
import logging
import re
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urljoin

from apscheduler.schedulers.background import BackgroundScheduler
from lxml import html
from requests import Session
from zeep import Client as SoapClient
from zeep.helpers import serialize_object
from zeep.transports import Transport

from models import Client, InsolvencyCase, InsolvencyChange, InsolvencyDocument, SessionLocal
from storage_paths import DOCUMENTS_DIR


ISIR_WSDL = "https://isir.justice.cz:8443/isir_cuzk_ws/IsirWsCuzkService?wsdl"
ISIR_PUBLIC_WSDL = "https://isir.justice.cz:8443/isir_public_ws/IsirWsPublicService?wsdl"
PUBLIC_EVENT_LOOKBACK = 500
USE_PUBLIC_EVENT_WS = False
DOCUMENT_STORAGE = DOCUMENTS_DIR

logger = logging.getLogger(__name__)


def make_soap_client() -> SoapClient:
    session = Session()
    session.trust_env = False
    transport = Transport(session=session, timeout=20, operation_timeout=30)
    return SoapClient(wsdl=ISIR_WSDL, transport=transport)


def make_public_soap_client() -> SoapClient:
    session = Session()
    session.trust_env = False
    transport = Transport(session=session, timeout=10, operation_timeout=12)
    return SoapClient(wsdl=ISIR_PUBLIC_WSDL, transport=transport)


def _as_list(value: Any) -> list[Any]:
    if value is None:
        return []
    if isinstance(value, list):
        return value
    return [value]


def _find_result_rows(data: Any) -> list[dict[str, Any]]:
    if isinstance(data, list):
        return [item for value in data for item in _find_result_rows(value)]
    if not isinstance(data, dict):
        return []

    likely_row_keys = {"druhStavKonkursu", "urlDetailRizeni", "relevanceVysledku", "nazevOsoby"}
    if likely_row_keys.intersection(data.keys()):
        return [data]

    rows: list[dict[str, Any]] = []
    for value in data.values():
        rows.extend(_find_result_rows(value))
    return rows


def _stable_json(data: Any) -> str:
    return json.dumps(data, ensure_ascii=False, sort_keys=True, default=str)


def _result_hash(rows: list[dict[str, Any]]) -> str:
    important = [
        {
            "cisloSenatu": row.get("cisloSenatu"),
            "druhVec": row.get("druhVec"),
            "bcVec": row.get("bcVec"),
            "rocnik": row.get("rocnik"),
            "stav": row.get("druhStavKonkursu"),
            "url": row.get("urlDetailRizeni"),
            "zahajeni": row.get("datumPmZahajeniUpadku"),
            "ukonceni": row.get("datumPmUkonceniUpadku"),
        }
        for row in rows
    ]
    payload = _stable_json(important)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _case_label(row: dict[str, Any]) -> str:
    senat = row.get("cisloSenatu")
    druh_vec = row.get("druhVec") or "INS"
    bc_vec = row.get("bcVec")
    rocnik = row.get("rocnik")
    if senat and bc_vec and rocnik:
        return f"{senat} {druh_vec} {bc_vec}/{rocnik}"
    if bc_vec and rocnik:
        return f"{druh_vec} {bc_vec}/{rocnik}"
    return "bez spisové značky"


def _address(row: dict[str, Any]) -> str:
    parts = [
        row.get("ulice"),
        row.get("cisloPopisne"),
        row.get("mesto"),
        row.get("psc"),
        row.get("okres"),
        row.get("zeme"),
    ]
    return ", ".join(str(part) for part in parts if part)


def _debtor_name(row: dict[str, Any]) -> str:
    if row.get("nazevOrganizace"):
        return str(row["nazevOrganizace"])

    parts = [
        row.get("titulPred"),
        row.get("jmeno"),
        row.get("nazevOsoby"),
        row.get("titulZa"),
    ]
    return " ".join(str(part) for part in parts if part)


def _upsert_case(client: Client, row: dict[str, Any]) -> None:
    spisova_znacka = _case_label(row)
    if spisova_znacka == "bez spisové značky":
        return

    case = next((item for item in client.cases if item.spisova_znacka == spisova_znacka), None)
    if case is None:
        case = InsolvencyCase(spisova_znacka=spisova_znacka)
        client.cases.append(case)

    case.debtor_name = _debtor_name(row)
    case.address = _address(row)
    case.state = row.get("druhStavKonkursu")
    case.detail_url = row.get("urlDetailRizeni")
    case.started_at = row.get("datumPmZahajeniUpadku")
    case.ended_at = row.get("datumPmUkonceniUpadku")
    case.raw_result = _stable_json(row)


def _parse_czech_datetime(value: str) -> datetime | None:
    value = " ".join(value.split())
    for date_format in ("%d.%m.%Y %H:%M", "%d.%m.%Y"):
        try:
            return datetime.strptime(value, date_format)
        except ValueError:
            pass
    return None


def _add_months(value: datetime, months: int) -> datetime:
    month = value.month - 1 + months
    year = value.year + month // 12
    month = month % 12 + 1
    days_in_month = [31, 29 if year % 4 == 0 and (year % 100 != 0 or year % 400 == 0) else 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31]
    day = min(value.day, days_in_month[month - 1])
    return value.replace(year=year, month=month, day=day)


def _safe_filename(value: str) -> str:
    value = re.sub(r"[^\w.-]+", "_", value, flags=re.UNICODE).strip("_")
    return value[:90] or "dokument"


def _document_folder_name(document: InsolvencyDocument) -> str:
    case = document.case
    client = case.client if case is not None else None
    if client is None:
        case_id = document.case_id or (case.id if case else None)
        return f"nezarazeno_{case_id or 'bez_cisla'}"

    birth_date = client.birth_date.isoformat() if client.birth_date else "bez_data"
    return _safe_filename(f"{client.last_name}_{client.first_name}_{birth_date}")


def _download_document(session: Session, document: InsolvencyDocument) -> None:
    case_dir = DOCUMENT_STORAGE / _document_folder_name(document)
    case_dir.mkdir(parents=True, exist_ok=True)
    url_hash = hashlib.sha1(document.source_url.encode("utf-8")).hexdigest()[:10]
    filename = f"{url_hash}_{_safe_filename(document.title)}.pdf"
    target = case_dir / filename

    if document.local_path:
        current = Path(document.local_path)
        if current.exists():
            if current.resolve() != target.resolve():
                target.write_bytes(current.read_bytes())
                try:
                    current.unlink()
                except OSError:
                    pass
                document.local_path = str(target)
            return

    response = session.get(document.source_url, timeout=30)
    response.raise_for_status()
    target.write_bytes(response.content)

    document.local_path = str(target)
    document.file_size = len(response.content)


def _upsert_document(
    case: InsolvencyCase,
    event_at: datetime | None,
    title: str,
    document_type: str,
    source_url: str,
) -> InsolvencyDocument:
    document = next((item for item in case.documents if item.source_url == source_url), None)
    if document is None:
        document = InsolvencyDocument(source_url=source_url)
        case.documents.append(document)

    document.event_at = event_at
    document.title = title or "Dokument"
    document.document_type = document_type
    return document


def _update_claims_summary(case: InsolvencyCase) -> None:
    claim_documents = [
        document
        for document in case.documents
        if "přihláška pohledávky" in (document.title or "").casefold()
    ]
    if claim_documents:
        case.claims_count = len(claim_documents)

    if not case.claims_deadline:
        bankruptcy_decision = next(
            (
                document
                for document in sorted(case.documents, key=lambda item: item.event_at or datetime.max)
                if document.event_at and "usnesení o úpadku" in (document.title or "").casefold()
            ),
            None,
        )
        if bankruptcy_decision:
            case.claims_deadline = _add_months(bankruptcy_decision.event_at, 2).strftime("%d.%m.%Y")


def _extract_claims_info_from_text(case: InsolvencyCase, text: str) -> None:
    compact_text = " ".join(text.split())

    deadline_patterns = [
        r"přihláš(?:ek|ky)\s+pohledávek.{0,120}?(?:do|ve lhůtě do)\s+(\d{1,2}\.\d{1,2}\.\d{4})",
        r"lhůt[ay]\s+pro\s+podávání\s+přihlášek\s+pohledávek.{0,120}?(\d{1,2}\.\d{1,2}\.\d{4})",
        r"věřitelé.{0,80}?přihlásit.{0,80}?do\s+(\d{1,2}\.\d{1,2}\.\d{4})",
    ]
    for pattern in deadline_patterns:
        match = re.search(pattern, compact_text, flags=re.IGNORECASE)
        if match:
            case.claims_deadline = match.group(1)
            break

    total_patterns = [
        r"celkov[áa]\s+výše\s+přihlášených\s+pohledávek.{0,80}?([0-9\s.,]+(?:Kč|CZK))",
        r"přihlášen[ée]\s+pohledávky\s+celkem.{0,80}?([0-9\s.,]+(?:Kč|CZK))",
    ]
    for pattern in total_patterns:
        match = re.search(pattern, compact_text, flags=re.IGNORECASE)
        if match:
            case.claims_total_amount = " ".join(match.group(1).split())
            break


def enrich_case_from_detail_page(case: InsolvencyCase) -> None:
    if not case.detail_url:
        return

    session = Session()
    session.trust_env = False
    response = session.get(case.detail_url, timeout=15)
    response.raise_for_status()

    tree = html.fromstring(response.content)
    _extract_claims_info_from_text(case, tree.text_content())
    document_rows = []
    all_pdf_links = []
    for row in tree.xpath("//tr"):
        cells = [" ".join(cell.text_content().split()) for cell in row.xpath("./td|./TD")]
        pdf_anchors = [a for a in row.xpath(".//a") if "/isir/doc/dokument.PDF" in (a.get("href") or "")]
        pdf_links = [urljoin(case.detail_url, anchor.get("href")) for anchor in pdf_anchors]
        all_pdf_links.extend(pdf_links)

        if len(cells) < 4 or not pdf_links:
            continue

        event_at = None
        if len(cells) > 2:
            event_at = _parse_czech_datetime(f"{cells[1]} {cells[2]}") or _parse_czech_datetime(cells[1])

        is_main_case_event = not cells[0].startswith("P")
        for index, pdf_url in enumerate(pdf_links):
            document_type = "hlavní dokument" if index == 0 else "vedlejší dokument"
            document = _upsert_document(case, event_at, cells[3], document_type, pdf_url)
            document_rows.append(
                {
                    "event_at": event_at,
                    "description": cells[3],
                    "url": pdf_url,
                    "is_main_case_event": is_main_case_event,
                    "document": document,
                }
            )

    case.document_count = len(all_pdf_links)
    _update_claims_summary(case)
    for document in case.documents:
        _download_document(session, document)

    main_document_rows = [row for row in document_rows if row["is_main_case_event"]]
    latest_document = max(
        main_document_rows or document_rows,
        key=lambda row: row["event_at"] or datetime.min,
        default=None,
    )
    if latest_document:
        case.document_url = latest_document["url"]
        case.last_event_at = latest_document["event_at"]
        case.last_event_description = latest_document["description"]

    for row in tree.xpath("//tr"):
        cells = [" ".join(cell.text_content().split()) for cell in row.xpath("./td|./TD")]
        row_datetime = None
        for index, cell in enumerate(cells):
            row_datetime = _parse_czech_datetime(cell)
            if row_datetime:
                if index + 1 < len(cells) and ":" in cells[index + 1]:
                    row_datetime = _parse_czech_datetime(f"{cell} {cells[index + 1]}") or row_datetime
                break

        if row_datetime and case.last_event_at is None:
            case.last_event_at = row_datetime
            descriptions = [
                cell
                for cell in cells
                if cell
                and not _parse_czech_datetime(cell)
                and ":" not in cell
                and "plný text" not in cell
                and "kB" not in cell
            ]
            if descriptions:
                case.last_event_description = descriptions[0]

        row_text = " ".join(row.text_content().split())
        if "Vyhláška o zahájení insolvenčního řízení" not in row_text:
            continue

        for index, cell in enumerate(cells):
            if _parse_czech_datetime(cell):
                combined = cell
                if index + 1 < len(cells) and ":" in cells[index + 1]:
                    combined = f"{cell} {cells[index + 1]}"
                case.proceeding_started_at = _parse_czech_datetime(combined) or _parse_czech_datetime(cell)
                return


def _status_from_rows(rows: list[dict[str, Any]]) -> str:
    if not rows:
        return "Bez nalezeného řízení"
    states = sorted({str(row.get("druhStavKonkursu") or "stav neuveden") for row in rows})
    return f"Nalezeno řízení: {', '.join(states)}"


def _change_description(rows: list[dict[str, Any]]) -> str:
    if not rows:
        return "Nově nebylo nalezeno žádné insolvenční řízení."

    parts = []
    for row in rows[:5]:
        state = row.get("druhStavKonkursu") or "stav neuveden"
        detail_url = row.get("urlDetailRizeni")
        label = f"{_case_label(row)} - {state}"
        if detail_url:
            label = f"{label} ({detail_url})"
        parts.append(label)
    return "Nový stav v ISIR: " + "; ".join(parts)


def check_client(client: Client, soap_client: SoapClient | None = None) -> None:
    soap_client = soap_client or make_soap_client()

    request = {
        "nazevOsoby": client.last_name,
        "jmeno": client.first_name,
        "datumNarozeni": client.birth_date.isoformat(),
        "maxPocetVysledku": 20,
        "filtrAktualniRizeni": "F",
        "vyhledatPresnouShoduJmen": "T",
        "maxRelevanceVysledku": 4,
    }

    response = soap_client.service.getIsirWsCuzkData(**request)
    serialized = serialize_object(response)
    rows = _find_result_rows(serialized)
    new_hash = _result_hash(rows)

    client.last_checked_at = datetime.utcnow()
    client.insolvency_status = _status_from_rows(rows)

    for row in rows:
        _upsert_case(client, row)

    if client.last_result_hash != new_hash:
        description = _change_description(rows)
        client.last_result_hash = new_hash
        client.last_found_change = description
        client.changes.append(
            InsolvencyChange(
                description=description,
                raw_result=_stable_json(serialized),
            )
        )


def _public_events_from_response(response: Any) -> list[dict[str, Any]]:
    serialized = serialize_object(response)
    data = serialized.get("data") if isinstance(serialized, dict) else None
    return [event for event in _as_list(data) if isinstance(event, dict)]


def enrich_cases_with_public_events(
    cases: list[InsolvencyCase],
    public_client: SoapClient | None = None,
) -> None:
    if not cases:
        return

    public_client = public_client or make_public_soap_client()
    posledni = serialize_object(public_client.service.getIsirWsPublicPodnetPosledniId())
    latest_values = posledni.get("cisloPosledniId") if isinstance(posledni, dict) else None
    latest_ids = [int(value) for value in _as_list(latest_values) if value is not None]
    if not latest_ids:
        return

    start_id = max(max(latest_ids) - PUBLIC_EVENT_LOOKBACK, 0)
    events = _public_events_from_response(public_client.service.getIsirWsPublicPodnetId(start_id))

    for event in events:
        event_spis = str(event.get("spisovaZnacka") or "")
        case = next((item for item in cases if item.spisova_znacka in event_spis), None)
        if case is None:
            continue

        event_id = event.get("id")
        if case.last_event_id is not None and event_id is not None and int(event_id) <= case.last_event_id:
            continue

        case.last_event_id = int(event_id) if event_id is not None else None
        case.last_event_at = event.get("datumZverejneniUdalosti") or event.get("datumZalozeniUdalosti")
        case.last_event_type = event.get("typUdalosti")
        case.last_event_description = event.get("popisUdalosti") or event.get("poznamka")
        case.document_url = event.get("dokumentUrl")


ProgressCallback = Callable[[str, dict[str, Any]], None]


def _notify_progress(progress_callback: ProgressCallback | None, event: str, **payload: Any) -> None:
    if progress_callback is None:
        return
    try:
        progress_callback(event, payload)
    except Exception:
        logger.warning("Aktualizace prubehu kontroly selhala")


def check_client_with_retry(client: Client, soap_client: SoapClient | None) -> None:
    last_error: Exception | None = None
    for attempt in range(2):
        try:
            check_client(client, soap_client)
            return
        except Exception as exc:
            last_error = exc
            if attempt == 0:
                time.sleep(2)
    if last_error is not None:
        raise last_error


def check_all_clients(
    progress_callback: ProgressCallback | None = None,
    client_ids: list[int] | None = None,
) -> None:
    session = SessionLocal()
    soap_client: SoapClient | None = None
    try:
        query = session.query(Client).order_by(Client.id)
        if client_ids is not None:
            query = query.filter(Client.id.in_(client_ids))
        clients = query.all()
        _notify_progress(progress_callback, "started", total=len(clients))
        if clients:
            try:
                soap_client = make_soap_client()
            except Exception as exc:
                logger.exception("Nepodarilo se pripojit k ISIR SOAP sluzbe")
                now = datetime.utcnow()
                message = f"Kontrola selhala: nepodařilo se připojit k ISIR ({exc.__class__.__name__})"
                for client in clients:
                    client.last_checked_at = now
                    client.insolvency_status = message
                    _notify_progress(progress_callback, "client_error", client=client, error=message)
                session.commit()
                _notify_progress(progress_callback, "finished")
                return

        for client in clients:
            _notify_progress(progress_callback, "client_started", client=client)
            try:
                before_document_urls = {
                    document.source_url
                    for case in client.cases
                    for document in case.documents
                    if document.source_url
                }
                check_client_with_retry(client, soap_client)
                for case in client.cases:
                    try:
                        enrich_case_from_detail_page(case)
                    except Exception:
                        logger.warning("Nacteni detailu ISIR selhalo pro spis %s", case.spisova_znacka)
                if USE_PUBLIC_EVENT_WS:
                    try:
                        enrich_cases_with_public_events(client.cases)
                    except Exception:
                        logger.warning("Dohledani detailu ISIR_PUBLIC_WS selhalo pro klienta id=%s", client.id)
                session.commit()
                new_documents = [
                    document
                    for case in client.cases
                    for document in case.documents
                    if document.source_url and document.source_url not in before_document_urls
                ]
                current_documents = [
                    document
                    for case in client.cases
                    for document in case.documents
                    if document.source_url
                ]
                _notify_progress(
                    progress_callback,
                    "client_success",
                    client=client,
                    new_document_count=len(new_documents),
                    document_count=len(current_documents),
                    new_document_titles=[document.title for document in new_documents[:5]],
                )
            except Exception as exc:
                logger.exception("ISIR kontrola selhala pro klienta id=%s", client.id)
                client.last_checked_at = datetime.utcnow()
                error_message = f"Kontrola selhala: {exc.__class__.__name__} - {exc}"
                client.insolvency_status = error_message
                session.commit()
                _notify_progress(progress_callback, "client_error", client=client, error=error_message)
            time.sleep(1.5)
        _notify_progress(progress_callback, "finished")
    finally:
        session.close()


def start_scheduler() -> BackgroundScheduler:
    scheduler = BackgroundScheduler(timezone="Europe/Prague")
    scheduler.add_job(
        check_all_clients,
        "cron",
        day_of_week="mon-fri",
        hour=10,
        minute=0,
        id="weekday_isir_check_10",
        replace_existing=True,
        max_instances=1,
        coalesce=True,
    )
    scheduler.add_job(
        check_all_clients,
        "cron",
        day_of_week="mon-fri",
        hour=14,
        minute=0,
        id="weekday_isir_check_14",
        replace_existing=True,
        max_instances=1,
        coalesce=True,
    )
    scheduler.start()
    return scheduler


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    check_all_clients()
