from __future__ import annotations

import json
import os
import shutil
import tempfile
import time
import calendar
import re
from datetime import datetime, date
from pathlib import Path
from contextlib import contextmanager

from app_settings import get_gemini_api_key
from google import genai
from google.genai import types
from requests import Session

from models import InsolvencyCase, SessionLocal


GEMINI_MODEL = "gemini-2.5-flash"
MAX_CASE_STUDY_PDFS = 14
MAX_DATA_VERIFICATION_NON_CLAIM_PDFS = 10
PROXY_ENV_KEYS = (
    "HTTP_PROXY",
    "HTTPS_PROXY",
    "ALL_PROXY",
    "http_proxy",
    "https_proxy",
    "all_proxy",
)


PROMPT = """
Přečti PDF z insolvenčního rejstříku jako administrativní asistent pro sociálního pracovníka.

Cíl:
Najít pouze praktické povinnosti dlužníka a dopady pro klienta.

Schéma:
{
  "category": "typ dokumentu",
  "debtor_obligations_summary": "maximálně 5 krátkých vět jednoduchým jazykem",
  "explicit_obligations": [
    {
      "obligation": "co má dlužník udělat",
      "recipient": "komu nebo kam",
      "deadline": "lhůta nebo datum, pokud je uvedeno",
      "source_certainty": "výslovně uvedeno | odvozeno | neuvedeno"
    }
  ],
  "deadlines": ["konkrétní lhůty nebo data"],
  "risks_if_ignored": ["praktická rizika při nesplnění"],
  "social_worker_checklist": ["co má pracovník s klientem ověřit"],
  "not_found": ["důležité věci, které dokument neuvádí"],
  "confidence": "nízká | střední | vysoká"
}

Pravidla:
- Vrať pouze validní JSON.
- Nevyvozuj právní závěry.
- Nepřidávej povinnosti, které v dokumentu nejsou.
- Pokud dokument žádnou konkrétní povinnost dlužníka neobsahuje, napiš to jasně.
- Piš česky, jednoduše, bez paragrafů a právnických formulací.
- Každou povinnost označ, zda je výslovně uvedená nebo jen odvozená.
- Pokud si nejsi jistý, nastav confidence na nízká.
"""


CASE_STUDY_PROMPT = """
Přečti dostupné PDF dokumenty z insolvenčního rejstříku a vytvoř stručnou kazuistiku pro sociálního pracovníka.

Cíl:
Shrnout celkový dosavadní průběh insolvenčního řízení a praktické dopady pro práci s klientem.

Schéma:
{
  "case_study": "souvislý text na 6 až 10 krátkých vět",
  "timeline": ["datum – co se stalo"],
  "current_state": "aktuální srozumitelný stav řízení",
  "claims_deadline": "lhůta pro podávání přihlášek pohledávek, pokud je v dokumentech uvedena",
  "claims_total_amount": "součet částek z pole V. Pohledávky celkem ze všech dokumentů Přihláška pohledávky",
  "claims_count": "počet dokumentů Přihláška pohledávky zahrnutých do součtu",
  "debtor_obligations": ["praktické povinnosti dlužníka"],
  "social_worker_next_steps": ["co má sociální pracovník s klientem ověřit nebo připravit"],
  "risks": ["praktická rizika, pokud klient nebude plnit povinnosti"],
  "confidence": "nízká | střední | vysoká"
}

Pravidla:
- Vrať pouze validní JSON.
- Piš česky, jednoduše a věcně.
- Zaměř se na průběh řízení, povinnosti dlužníka a praktické kroky.
- U přihlášek pohledávek hledej zejména lhůtu pro podávání přihlášek, počet přihlášek a celkovou výši přihlášených pohledávek.
- Výši přihlášených pohledávek počítej výhradně z hlavních dokumentů, které se jmenují Přihláška pohledávky.
- V každé Přihlášce pohledávky použij částku z pole V. Pohledávky celkem. Nepoužívej částky z jiných částí formuláře.
- Výsledná výše pohledávek je součet pole V. Pohledávky celkem ze všech přiložených hlavních dokumentů Přihláška pohledávky.
- Do claims_count vrať počet Přihlášek pohledávky zahrnutých do součtu.
- Nepiš právní rady a nevyvozuj závěry, které nejsou v dokumentech.
- Pokud některá informace v dokumentech není, napiš to jednoduše.
- Neopakuj stejné informace vícekrát.
"""


DATA_VERIFICATION_PROMPT = """
Přečti přiložené PDF dokumenty z insolvenčního rejstříku a ověř, zda uložené údaje v aplikaci odpovídají dokumentům.

Cíl:
Zkontrolovat údaje v horním panelu detailu klienta. Zvláštní pozornost věnuj lhůtě pro podávání přihlášek pohledávek, protože bývá často chybně.

Schéma:
{
  "overall_result": "v pořádku | nalezeny rozpory | nelze ověřit",
  "fields": [
    {
      "field": "název kontrolovaného údaje",
      "stored_value": "hodnota uložená v aplikaci",
      "pdf_value": "hodnota zjištěná z PDF nebo neuvedeno",
      "status": "souhlasí | liší se | nelze ověřit",
      "source": "název nebo datum dokumentu, ze kterého údaj vyplývá",
      "note": "krátké vysvětlení"
    }
  ],
  "recommended_corrections": ["konkrétní doporučené opravy údajů v aplikaci"],
  "claims_deadline": {
    "stored_value": "uložená lhůta přihlášek",
    "pdf_value": "lhůta podle PDF",
    "status": "souhlasí | liší se | nelze ověřit",
    "confidence": "nízká | střední | vysoká",
    "source": "dokument, ze kterého lhůta vyplývá",
    "note": "stručné vysvětlení"
  },
  "claims_total_amount": {
    "stored_value": "uložená výše pohledávek",
    "pdf_value": "součet pole V. Pohledávky celkem ze všech Přihlášek pohledávky",
    "status": "souhlasí | liší se | nelze ověřit",
    "claims_count": "počet Přihlášek pohledávky zahrnutých do součtu",
    "source": "seznam přihlášek nebo souhrn zdrojů",
    "note": "stručné vysvětlení"
  },
  "confidence": "nízká | střední | vysoká"
}

Pravidla:
- Vrať pouze validní JSON.
- Piš česky, stručně a věcně.
- Nehádej. Pokud údaj v PDF nevidíš, napiš nelze ověřit.
- U každého rozporu uveď hodnotu z PDF a zdroj.
- U lhůty přihlášek hledej zejména usnesení o úpadku, vyhlášku o zahájení řízení a dokumenty týkající se přihlášek pohledávek.
- Lhůta přihlášek je lhůta stanovená soudem nebo insolvenčním správcem pro podávání přihlášek pohledávek.
- Výše pohledávek znamená jen částky z reálně podaných přihlášek pohledávek. Nepoužívej odhad závazků z insolvenčního návrhu jako výši přihlášených pohledávek.
- Výši pohledávek čerpej výhradně z hlavních dokumentů s názvem Přihláška pohledávky.
- V každé Přihlášce pohledávky vezmi částku z pole V. Pohledávky celkem a tyto částky sečti.
- Vedlejší dokumenty nebo přílohy k přihlášce pro součet nepoužívej.
- Jestliže je přiloženo 5 hlavních dokumentů Přihláška pohledávky, zkontroluj všech 5 a ve výsledku uveď počet 5.
- Nepřebírej částku z insolvenčního návrhu, ze soupisu závazků ani z odhadu dlužníka.
- Neposkytuj právní rady, pouze porovnej uložené údaje s dokumenty.
"""


def _download_pdf(url: str) -> str:
    session = Session()
    session.trust_env = False
    response = session.get(url, timeout=30)
    response.raise_for_status()

    handle = tempfile.NamedTemporaryFile(delete=False, suffix=".pdf")
    try:
        handle.write(response.content)
        return handle.name
    finally:
        handle.close()


def _as_text_list(value) -> str:
    if isinstance(value, list):
        return "\n".join(f"- {item}" for item in value)
    if value is None:
        return ""
    return str(value)


def _format_obligations(value) -> str:
    if not isinstance(value, list):
        return _as_text_list(value)

    rows = []
    for item in value:
        if not isinstance(item, dict):
            rows.append(f"- {item}")
            continue

        obligation = item.get("obligation") or "Povinnost není popsána"
        recipient = item.get("recipient")
        deadline = item.get("deadline")
        certainty = item.get("source_certainty")
        details = []
        if recipient:
            details.append(f"komu/kam: {recipient}")
        if deadline:
            details.append(f"lhůta: {deadline}")
        if certainty:
            details.append(f"jistota: {certainty}")
        suffix = f" ({'; '.join(details)})" if details else ""
        rows.append(f"- {obligation}{suffix}")
    return "\n".join(rows)


def _upload_with_retry(client: genai.Client, path: str | Path):
    last_error = None
    for _ in range(3):
        try:
            return client.files.upload(file=path)
        except Exception as exc:
            last_error = exc
            time.sleep(2)
    raise last_error


@contextmanager
def _without_proxy_env():
    saved = {key: os.environ.get(key) for key in PROXY_ENV_KEYS}
    try:
        for key in PROXY_ENV_KEYS:
            os.environ.pop(key, None)
        yield
    finally:
        for key, value in saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def _make_gemini_client(api_key: str) -> genai.Client:
    return genai.Client(
        api_key=api_key,
        http_options=types.HttpOptions(
            client_args={"trust_env": False},
            async_client_args={"trust_env": False},
        ),
    )


def _make_ascii_pdf_copy(path: str | Path) -> str:
    source = Path(path)
    handle = tempfile.NamedTemporaryFile(
        delete=False,
        prefix="gemini_pdf_",
        suffix=".pdf",
    )
    try:
        handle.close()
        shutil.copyfile(source, handle.name)
        return handle.name
    except Exception:
        try:
            os.unlink(handle.name)
        except OSError:
            pass
        raise


def analyze_case_latest_document(case: InsolvencyCase, api_key: str | None = None) -> None:
    api_key = api_key or get_gemini_api_key()
    if not api_key:
        raise RuntimeError("Chybí proměnná prostředí GEMINI_API_KEY.")
    if not case.document_url:
        raise RuntimeError("U řízení není uložený odkaz na PDF dokument.")

    pdf_path = _download_pdf(case.document_url)
    uploaded_file = None
    try:
        with _without_proxy_env():
            client = _make_gemini_client(api_key)
            uploaded_file = _upload_with_retry(client, pdf_path)

            response = client.models.generate_content(
                model=GEMINI_MODEL,
                contents=[uploaded_file, PROMPT],
                config=types.GenerateContentConfig(response_mime_type="application/json"),
            )
        payload = json.loads(response.text)

        case.ai_checked_at = datetime.utcnow()
        case.ai_model = GEMINI_MODEL
        case.ai_category = payload.get("category")
        case.ai_summary = payload.get("debtor_obligations_summary") or payload.get("summary")
        case.ai_key_points = _format_obligations(payload.get("explicit_obligations"))
        case.ai_deadlines = _as_text_list(payload.get("deadlines"))
        checklist = _as_text_list(payload.get("social_worker_checklist"))
        risks = _as_text_list(payload.get("risks_if_ignored"))
        not_found = _as_text_list(payload.get("not_found"))
        confidence = payload.get("confidence")
        parts = []
        if checklist:
            parts.append(f"Checklist:\n{checklist}")
        if risks:
            parts.append(f"Rizika:\n{risks}")
        if not_found:
            parts.append(f"Neuvedeno:\n{not_found}")
        if confidence:
            parts.append(f"Jistota: {confidence}")
        case.ai_recommended_action = "\n\n".join(parts)
        case.ai_raw_result = json.dumps(payload, ensure_ascii=False, indent=2)
    finally:
        if uploaded_file is not None:
            try:
                client.files.delete(name=uploaded_file.name)
            except Exception:
                pass
        try:
            os.unlink(pdf_path)
        except OSError:
            pass


def _format_case_study(payload: dict, claim_collection_running: bool = False) -> str:
    parts = []
    case_study = payload.get("case_study")
    if case_study:
        parts.append(str(case_study))

    timeline = _as_text_list(payload.get("timeline"))
    current_state = payload.get("current_state")
    claims_deadline = payload.get("claims_deadline")
    claims_total_amount = payload.get("claims_total_amount")
    claims_count = payload.get("claims_count")
    obligations = _as_text_list(payload.get("debtor_obligations"))
    next_steps = _as_text_list(payload.get("social_worker_next_steps"))
    risks = _as_text_list(payload.get("risks"))
    confidence = payload.get("confidence")

    if timeline:
        parts.append(f"Průběh:\n{timeline}")
    if current_state:
        parts.append(f"Aktuální stav:\n{current_state}")
    claim_parts = []
    if claims_deadline:
        claim_parts.append(f"Lhůta pro přihlášky: {claims_deadline}")
    if claims_total_amount:
        claim_parts.append(f"Celková výše přihlášených pohledávek: {claims_total_amount}")
    if claims_count:
        claim_parts.append(f"Počet přihlášek: {claims_count}")
    if claim_collection_running:
        claim_parts.append("! Sběr přihlášek stále probíhá, počet přihlášek i výše pohledávek jsou průběžné údaje a nemusí být konečné.")
    if claim_parts:
        parts.append("Přihlášky pohledávek:\n" + "\n".join(f"- {item}" for item in claim_parts))
    if obligations:
        parts.append(f"Povinnosti dlužníka:\n{obligations}")
    if next_steps:
        parts.append(f"Co ověřit:\n{next_steps}")
    if risks:
        parts.append(f"Rizika:\n{risks}")
    if confidence:
        parts.append(f"Jistota shrnutí: {confidence}")
    return "\n\n".join(parts)


def _normalized_text(value: str | None) -> str:
    return str(value or "").casefold()


def _case_is_closed(case: InsolvencyCase) -> bool:
    status = _normalized_text(case.state)
    return "odškrtnuta" in status or "odskrtnuta" in status or "od krtnuta" in status or "zruš" in status or "zrus" in status


def _closure_paragraph(case: InsolvencyCase) -> str:
    documents = sorted(
        [
            document
            for document in case.documents
            if document.title or document.event_at
        ],
        key=lambda item: (item.event_at or datetime.min, item.id or 0),
        reverse=True,
    )[:5]
    document_lines = []
    for document in documents:
        date_text = document.event_at.strftime("%d.%m.%Y") if document.event_at else "bez data"
        document_lines.append(f"{date_text}: {document.title or 'dokument bez názvu'}")

    parts = []
    if case.state:
        parts.append(f"Řízení je v ISIR vedené jako {case.state}.")
    if case.last_event_at or case.last_event_description:
        date_text = case.last_event_at.strftime("%d.%m.%Y") if case.last_event_at else "bez data"
        description = case.last_event_description or case.last_event_type or "poslední událost bez popisu"
        parts.append(f"Poslední zaznamenaná událost je {date_text}: {description}.")
    if document_lines:
        parts.append("Poslední dostupné dokumenty: " + "; ".join(document_lines) + ".")
    if not parts:
        parts.append("Řízení je označené jako ukončené, ale v uložených dokumentech není k dispozici bližší popis posledních kroků.")

    return "Ukončení insolvence:\n" + " ".join(parts)


def _clean_claim_value(value) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text or text.lower() in {"neuvedeno", "není uvedeno", "nezjištěno"}:
        return None
    return text[:2000]


def _parse_claim_deadline(value: str | None) -> date | None:
    if not value:
        return None
    match = re.search(r"(\d{1,2})\.(\d{1,2})\.(\d{4})", str(value))
    if not match:
        return None
    day, month, year = (int(part) for part in match.groups())
    try:
        return date(year, month, day)
    except ValueError:
        return None


def _add_months(value: date, months: int) -> date:
    month_index = value.month - 1 + months
    year = value.year + month_index // 12
    month = month_index % 12 + 1
    day = min(value.day, calendar.monthrange(year, month)[1])
    return date(year, month, day)


def _effective_claim_deadline(case: InsolvencyCase) -> date | None:
    explicit_deadline = _parse_claim_deadline(case.claims_deadline)
    if explicit_deadline:
        return explicit_deadline
    if case.proceeding_started_at:
        return _add_months(case.proceeding_started_at.date(), 2)
    if case.started_at:
        return _add_months(case.started_at, 2)
    return None


def _claim_collection_running(case: InsolvencyCase) -> bool:
    deadline = _effective_claim_deadline(case)
    return bool(deadline and datetime.now().date() <= deadline)


def _claim_document_count(case: InsolvencyCase) -> int:
    return sum(
        1
        for document in case.documents
        if _is_claim_document(document)
    )


def _is_claim_document(document) -> bool:
    text = " ".join(
        part
        for part in [getattr(document, "title", None), getattr(document, "document_type", None)]
        if part
    ).casefold()
    document_type = (getattr(document, "document_type", None) or "").casefold()
    title = (getattr(document, "title", None) or "").casefold()
    if "přihláška pohledávky" not in text:
        return False
    if "vedlejší dokument" in document_type or "vedlejší dokument" in title:
        return False
    return True


def _important_non_claim_documents(available_documents: list) -> list:
    important_words = (
        "usnesení",
        "vyhláška",
        "sdělení insolvenčního správce",
        "zpráva",
        "přezkumn",
        "seznam",
        "oddlužení",
        "opatření",
        "soupis",
    )
    return [
        document
        for document in available_documents
        if not _is_claim_document(document)
        and any(word in (document.title or "").casefold() for word in important_words)
    ]


def _case_study_documents(case: InsolvencyCase) -> list:
    available_documents = [
        document
        for document in sorted(case.documents, key=lambda item: item.event_at or datetime.min)
        if document.local_path and Path(document.local_path).exists()
    ]
    if len(available_documents) <= MAX_CASE_STUDY_PDFS:
        return available_documents

    claim_documents = [document for document in available_documents if _is_claim_document(document)]
    important_documents = _important_non_claim_documents(available_documents)

    selected = []
    for document in claim_documents:
        if document not in selected:
            selected.append(document)

    for document in important_documents:
        if document not in selected:
            selected.append(document)

    for document in available_documents:
        if len(selected) >= max(MAX_CASE_STUDY_PDFS, len(claim_documents)):
            break
        if document not in selected:
            selected.append(document)

    return sorted(selected[: max(MAX_CASE_STUDY_PDFS, len(claim_documents))], key=lambda item: item.event_at or datetime.min)


def _case_data_verification_documents(case: InsolvencyCase) -> list:
    available_documents = [
        document
        for document in sorted(case.documents, key=lambda item: item.event_at or datetime.min)
        if document.local_path and Path(document.local_path).exists()
    ]
    claim_documents = [document for document in available_documents if _is_claim_document(document)]
    selected = list(claim_documents)

    for document in _important_non_claim_documents(available_documents):
        if len(selected) >= len(claim_documents) + MAX_DATA_VERIFICATION_NON_CLAIM_PDFS:
            break
        if document not in selected:
            selected.append(document)

    for document in available_documents:
        if len(selected) >= len(claim_documents) + MAX_DATA_VERIFICATION_NON_CLAIM_PDFS:
            break
        if document not in selected:
            selected.append(document)

    return sorted(selected, key=lambda item: item.event_at or datetime.min)


def analyze_case_study(case: InsolvencyCase, api_key: str | None = None) -> None:
    api_key = api_key or get_gemini_api_key()
    if not api_key:
        raise RuntimeError("Chybí proměnná prostředí GEMINI_API_KEY.")

    all_documents = [
        document
        for document in sorted(case.documents, key=lambda item: item.event_at or datetime.min)
        if document.local_path and Path(document.local_path).exists()
    ]
    documents = _case_study_documents(case)
    if not documents:
        raise RuntimeError("Nejsou uložené žádné PDF dokumenty pro vytvoření kazuistiky.")
    claim_collection_running = _claim_collection_running(case)

    uploaded_files = []
    temp_paths = []
    try:
        with _without_proxy_env():
            client = _make_gemini_client(api_key)
            for document in documents:
                temp_path = _make_ascii_pdf_copy(document.local_path)
                temp_paths.append(temp_path)
                uploaded_files.append(_upload_with_retry(client, temp_path))

            document_list = "\n".join(
                f"- {document.event_at.strftime('%d.%m.%Y') if document.event_at else 'bez data'}: {document.title}"
                for document in all_documents
            )
            uploaded_document_list = "\n".join(
                f"- {document.event_at.strftime('%d.%m.%Y') if document.event_at else 'bez data'}: {document.title}"
                for document in documents
            )
            claim_status_note = (
                "Pozor: lhůta pro podávání přihlášek podle uložených údajů stále běží. "
                "Počet přihlášek a celkovou výši pohledávek označ jako průběžné údaje, ne jako konečný stav.\n\n"
                if claim_collection_running
                else ""
            )
            prompt = (
                f"{CASE_STUDY_PROMPT}\n\n"
                f"V řízení je celkem {len(all_documents)} PDF dokumentů. "
                f"Počet dokumentů typu Přihláška pohledávky podle ISIR: {_claim_document_count(case)}.\n\n"
                f"Kompletní seznam dokumentů podle ISIR:\n{document_list}\n\n"
                f"Obsahově přiložené PDF dokumenty pro analýzu:\n{uploaded_document_list}"
            )
            response = client.models.generate_content(
                model=GEMINI_MODEL,
                contents=[*uploaded_files, f"{claim_status_note}{prompt}"],
                config=types.GenerateContentConfig(response_mime_type="application/json"),
            )
        payload = json.loads(response.text)
        case.ai_case_study_at = datetime.utcnow()
        formatted_case_study = _format_case_study(payload, claim_collection_running=claim_collection_running)
        if _case_is_closed(case):
            formatted_case_study = "\n\n".join(
                part for part in [formatted_case_study, _closure_paragraph(case)] if part
            )
        case.ai_case_study = formatted_case_study
        claims_deadline = _clean_claim_value(payload.get("claims_deadline"))
        claims_total_amount = _clean_claim_value(payload.get("claims_total_amount"))
        claims_count = payload.get("claims_count")
        if claims_deadline:
            case.claims_deadline = claims_deadline
        if claims_total_amount:
            case.claims_total_amount = claims_total_amount
        try:
            parsed_claims_count = int(claims_count)
        except (TypeError, ValueError):
            parsed_claims_count = _claim_document_count(case)
        if parsed_claims_count:
            case.claims_count = parsed_claims_count
    finally:
        if uploaded_files:
            for uploaded_file in uploaded_files:
                try:
                    client.files.delete(name=uploaded_file.name)
                except Exception:
                    pass
        for temp_path in temp_paths:
            try:
                os.unlink(temp_path)
            except OSError:
                pass


def _format_stored_datetime(value) -> str:
    if not value:
        return "-"
    if isinstance(value, datetime):
        return value.strftime("%d.%m.%Y %H:%M")
    if isinstance(value, date):
        return value.strftime("%d.%m.%Y")
    return str(value)


def _case_data_snapshot(case: InsolvencyCase) -> dict[str, str]:
    client = case.client
    return {
        "Klient": f"{client.last_name} {client.first_name}" if client else "-",
        "Dlužník": case.debtor_name or "-",
        "Poslední událost přihlášky": case.last_event_description or case.last_event_type or "-",
        "Datum narození": _format_stored_datetime(client.birth_date) if client else "-",
        "Poslední kontrola": _format_stored_datetime(client.last_checked_at) if client else "-",
        "Spisová značka": case.spisova_znacka or "-",
        "Počet dokumentů": str(case.document_count if case.document_count is not None else "-"),
        "Stav řízení": case.state or "-",
        "Datum zahájení": _format_stored_datetime(case.proceeding_started_at or case.started_at),
        "Lhůta přihlášek": case.claims_deadline or _format_stored_datetime(_effective_claim_deadline(case)) or "-",
        "Počet přihlášek": str(case.claims_count if case.claims_count is not None else "-"),
        "Výše přihlášených pohledávek": case.claims_total_amount or "-",
    }


def _format_verification_fields(fields) -> str:
    if not isinstance(fields, list):
        return _as_text_list(fields)

    rows = []
    for item in fields:
        if not isinstance(item, dict):
            rows.append(f"- {item}")
            continue
        field = item.get("field") or "Údaj"
        status = item.get("status") or "nelze ověřit"
        stored_value = item.get("stored_value") or "-"
        pdf_value = item.get("pdf_value") or "-"
        source = item.get("source") or "zdroj neuveden"
        note = item.get("note") or ""
        line = f"- {field}: {status}. Uloženo: {stored_value}; PDF: {pdf_value}; zdroj: {source}"
        if note:
            line = f"{line}. {note}"
        rows.append(line)
    return "\n".join(rows)


def _format_claims_deadline_verification(value) -> str:
    if not isinstance(value, dict):
        return _as_text_list(value)
    parts = [
        f"Stav: {value.get('status') or 'nelze ověřit'}",
        f"Uloženo: {value.get('stored_value') or '-'}",
        f"PDF: {value.get('pdf_value') or '-'}",
        f"Jistota: {value.get('confidence') or '-'}",
        f"Zdroj: {value.get('source') or '-'}",
    ]
    note = value.get("note")
    if note:
        parts.append(f"Poznámka: {note}")
    return "\n".join(parts)


def _format_claims_amount_verification(value) -> str:
    if not isinstance(value, dict):
        return _as_text_list(value)
    parts = [
        f"Stav: {value.get('status') or 'nelze ověřit'}",
        f"Uloženo: {value.get('stored_value') or '-'}",
        f"PDF / součet V. Pohledávky celkem: {value.get('pdf_value') or '-'}",
        f"Počet zahrnutých přihlášek: {value.get('claims_count') or '-'}",
        f"Zdroj: {value.get('source') or '-'}",
    ]
    note = value.get("note")
    if note:
        parts.append(f"Poznámka: {note}")
    return "\n".join(parts)


def analyze_case_data_verification(case: InsolvencyCase, api_key: str | None = None) -> None:
    api_key = api_key or get_gemini_api_key()
    if not api_key:
        raise RuntimeError("Chybí proměnná prostředí GEMINI_API_KEY.")

    all_documents = [
        document
        for document in sorted(case.documents, key=lambda item: item.event_at or datetime.min)
        if document.local_path and Path(document.local_path).exists()
    ]
    documents = _case_data_verification_documents(case)
    if not documents:
        raise RuntimeError("Nejsou uložené žádné PDF dokumenty pro ověření údajů.")

    stored_values = _case_data_snapshot(case)
    uploaded_files = []
    temp_paths = []
    try:
        with _without_proxy_env():
            client = _make_gemini_client(api_key)
            for document in documents:
                temp_path = _make_ascii_pdf_copy(document.local_path)
                temp_paths.append(temp_path)
                uploaded_files.append(_upload_with_retry(client, temp_path))

            stored_values_text = "\n".join(f"- {key}: {value}" for key, value in stored_values.items())
            document_list = "\n".join(
                f"- {document.event_at.strftime('%d.%m.%Y') if document.event_at else 'bez data'}: {document.title}"
                for document in all_documents
            )
            uploaded_document_list = "\n".join(
                f"- {document.event_at.strftime('%d.%m.%Y') if document.event_at else 'bez data'}: {document.title}"
                for document in documents
            )
            uploaded_claim_count = sum(1 for document in documents if _is_claim_document(document))
            prompt = (
                f"{DATA_VERIFICATION_PROMPT}\n\n"
                f"Údaje uložené v aplikaci:\n{stored_values_text}\n\n"
                f"Kompletní seznam PDF dokumentů podle ISIR:\n{document_list}\n\n"
                f"Počet přiložených hlavních dokumentů Přihláška pohledávky: {uploaded_claim_count}. "
                f"Pro výši pohledávek musíš použít všech {uploaded_claim_count} hlavních přihlášek a pole V. Pohledávky celkem. "
                f"Vedlejší dokumenty a přílohy do součtu nezahrnuj.\n\n"
                f"Obsahově přiložené PDF dokumenty pro ověření:\n{uploaded_document_list}"
            )
            response = client.models.generate_content(
                model=GEMINI_MODEL,
                contents=[*uploaded_files, prompt],
                config=types.GenerateContentConfig(response_mime_type="application/json"),
            )
        payload = json.loads(response.text)

        case.ai_checked_at = datetime.utcnow()
        case.ai_model = GEMINI_MODEL
        case.ai_category = "AI kontrola údajů z PDF"
        result = payload.get("overall_result") or "Výsledek neuveden"
        confidence = payload.get("confidence")
        summary_parts = [f"Výsledek kontroly: {result}."]
        if confidence:
            summary_parts.append(f"Celková jistota: {confidence}.")
        case.ai_summary = " ".join(summary_parts)
        case.ai_key_points = _format_verification_fields(payload.get("fields"))
        case.ai_deadlines = _format_claims_deadline_verification(payload.get("claims_deadline"))
        corrections = _as_text_list(payload.get("recommended_corrections"))
        claims_amount = _format_claims_amount_verification(payload.get("claims_total_amount"))
        action_parts = []
        if claims_amount:
            action_parts.append(f"Výše přihlášených pohledávek:\n{claims_amount}")
        if corrections:
            action_parts.append(f"Doporučené opravy:\n{corrections}")
        case.ai_recommended_action = "\n\n".join(action_parts)
        case.ai_raw_result = json.dumps(payload, ensure_ascii=False, indent=2)
    finally:
        if uploaded_files:
            for uploaded_file in uploaded_files:
                try:
                    client.files.delete(name=uploaded_file.name)
                except Exception:
                    pass
        for temp_path in temp_paths:
            try:
                os.unlink(temp_path)
            except OSError:
                pass


def analyze_case_latest_document_job(case_id: int) -> None:
    session = SessionLocal()
    try:
        case = session.get(InsolvencyCase, case_id)
        if case is None:
            return

        try:
            analyze_case_latest_document(case)
        except Exception as exc:
            case.ai_checked_at = datetime.utcnow()
            case.ai_category = "Analýza selhala"
            case.ai_summary = str(exc)
        session.commit()
    finally:
        session.close()


def analyze_case_study_job(case_id: int) -> None:
    session = SessionLocal()
    try:
        case = session.get(InsolvencyCase, case_id)
        if case is None:
            return

        try:
            analyze_case_study(case)
        except Exception as exc:
            case.ai_case_study_at = datetime.utcnow()
            case.ai_case_study = f"Kazuistiku se nepodařilo vytvořit: {exc}"
        session.commit()
    finally:
        session.close()


def analyze_case_data_verification_job(case_id: int) -> None:
    session = SessionLocal()
    try:
        case = session.get(InsolvencyCase, case_id)
        if case is None:
            return

        try:
            analyze_case_data_verification(case)
        except Exception as exc:
            case.ai_checked_at = datetime.utcnow()
            case.ai_category = "AI kontrola údajů selhala"
            case.ai_summary = str(exc)
        session.commit()
    finally:
        session.close()
