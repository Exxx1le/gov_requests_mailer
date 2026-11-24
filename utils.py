import logging
import os
from datetime import timedelta
from pathlib import Path
from stat import S_ISDIR, S_ISREG
from time import sleep, time

import docx
import requests
from docx import Document  # Для создания DOC-файла

from constants import AUTHORITY_ID, AUTHORITY_NAMES

# URL для запроса документов
API_URL = "http://publication.pravo.gov.ru/api/Documents"
logger = logging.getLogger(__name__)


def fetch_documents_by_date(date, authority_id):
    """
    Fetches documents for a specified date and government authority.
    Excludes resolutions (documentTypeId=7ff5b3b5-3757-44f1-bb76-3766cabe3593)
    for all authorities.

    Args:
        date (str): Date in DD.MM.YYYY format
        authority_id (str): GUID of the government authority

    Returns:
        list: List of found documents (excluding resolutions)

    Raises:
        requests.exceptions.RequestException: The request failed.
        ValueError: The response was not valid JSON or had an unexpected structure.
    """
    params = {
        "periodType": "day",
        "date": date,
        "SignatoryAuthorityId": authority_id
    }

    max_attempts = 3
    for attempt in range(1, max_attempts + 1):
        try:
            response = requests.get(API_URL, params=params, timeout=30)
            response.raise_for_status()
            data = response.json()
            items = data.get("items") if isinstance(data, dict) else None
            if not isinstance(items, list) or any(not isinstance(doc, dict) for doc in items):
                raise ValueError("Ответ API должен содержать список объектов в поле 'items'")

            # Фильтруем результаты, исключая распоряжения
            return [
                doc for doc in items
                if doc.get("documentTypeId") != "7ff5b3b5-3757-44f1-bb76-3766cabe3593"
            ]
        except (requests.exceptions.RequestException, ValueError) as error:
            if isinstance(error, requests.exceptions.HTTPError) and error.response is not None:
                status = error.response.status_code
                retryable = status == 429 or 500 <= status < 600
            else:
                retryable = isinstance(error, (
                    requests.exceptions.Timeout,
                    requests.exceptions.ConnectionError,
                    requests.exceptions.ChunkedEncodingError,
                )) and not isinstance(error, requests.exceptions.SSLError)

            will_retry = retryable and attempt < max_attempts
            logger.log(
                logging.WARNING if will_retry else logging.ERROR,
                "Ошибка запроса за %s, орган %s, попытка %d/%d: %s",
                date, authority_id, attempt, max_attempts, error,
            )
            if not will_retry:
                raise
            sleep(2 ** (attempt - 1))


def add_hyperlink(paragraph, text, url):
    """
    Adds a clickable hyperlink to a paragraph in a Word document.

    Args:
        paragraph: Document paragraph object
        text (str): Display text for the hyperlink
        url (str): URL for the hyperlink

    Returns:
        OxmlElement: Created hyperlink element
    """
    # Получаем связь с word документом
    part = paragraph.part
    r_id = part.relate_to(url, docx.opc.constants.RELATIONSHIP_TYPE.HYPERLINK, is_external=True)

    # Создаем гиперссылку
    hyperlink = docx.oxml.shared.OxmlElement("w:hyperlink")
    hyperlink.set(docx.oxml.shared.qn("r:id"), r_id)

    # Создаем элемент run
    new_run = docx.oxml.shared.OxmlElement("w:r")
    rPr = docx.oxml.shared.OxmlElement("w:rPr")

    # Добавляем стиль для гиперссылки
    rStyle = docx.oxml.shared.OxmlElement("w:rStyle")
    rStyle.set(docx.oxml.shared.qn("w:val"), "Hyperlink")
    rPr.append(rStyle)

    # Добавляем текст
    new_run.append(rPr)
    new_run.text = text
    hyperlink.append(new_run)

    paragraph._p.append(hyperlink)
    return hyperlink


def save_to_doc(documents_by_authority, filename):
    """
    Saves documents to a .docx file, grouping them by government authority.
    Only authorities with documents will be included in the output file.

    Args:
        documents_by_authority (dict): Dictionary where keys are authority IDs and values are lists of documents
        filename (str): Name of the output file

    Returns:
        None
    """
    doc = Document()

    for authority_id, documents in documents_by_authority.items():
        # Используем словарь для получения названия органа власти
        authority_name = AUTHORITY_NAMES.get(authority_id, authority_id)
        doc.add_heading(authority_name, level=1)

        for doc_info in documents:
            eo_number = doc_info.get("eoNumber", "N/A")
            document_name = doc_info.get("complexName", "Название не указано")
            document_date = doc_info.get("documentDate", "Дата не указана")

            # Добавляем название документа
            doc.add_paragraph(f"{document_name} ({document_date})")

            # Создаем параграф для ссылки
            p = doc.add_paragraph()
            link = f"http://publication.pravo.gov.ru/document/{eo_number}"
            add_hyperlink(p, f"Ссылка на документ", link)

            # Добавляем пустую строку между документами
            doc.add_paragraph("")

        # Добавляем разделитель между органами власти
        doc.add_page_break()

    os.makedirs('generated', exist_ok=True)

    output_path = os.path.join('generated', filename)

    doc.save(output_path)

    return output_path


def process_documents(start_date, end_date):
    """
    Main function for fetching and processing documents.
    Creates a Word document with documents grouped by authority.

    Args:
        start_date (datetime.date): Start date for document search
        end_date (datetime.date): End date for document search

    Returns:
        str: Path to generated DOCX file
    """

    current_date = start_date
    all_documents_by_authority = {}

    while current_date <= end_date:
        formatted_date = current_date.strftime("%d.%m.%Y")

        for authority_id in AUTHORITY_ID:
            documents = fetch_documents_by_date(formatted_date, authority_id)

            if documents:
                if authority_id not in all_documents_by_authority:
                    all_documents_by_authority[authority_id] = []

                all_documents_by_authority[authority_id].extend(documents)

        current_date += timedelta(days=1)

    filename = (
        f"Документы за "
        f"{start_date.strftime('%Y-%m-%d')} - "
        f"{end_date.strftime('%Y-%m-%d')}.docx"
    )

    output_path = save_to_doc(
        all_documents_by_authority,
        filename
    )

    return output_path


def _generated_directory():
    directory = Path.cwd().resolve() / "generated"
    try:
        mode = directory.lstat().st_mode
    except FileNotFoundError:
        return None

    if not S_ISDIR(mode) or directory.resolve() != directory:
        raise ValueError(f"Ожидалась обычная директория: {directory}")
    return directory


def _unlink_generated_docx(path):
    if path.resolve() != path:
        raise ValueError(f"Путь к документу проходит через ссылку: {path}")
    path.unlink(missing_ok=True)
    logger.info("Удалён сформированный документ: %s", path)


def cleanup_generated_file(file_path):
    """Remove a regular DOCX file within generated/ if it exists."""
    path = Path(file_path)
    try:
        directory = _generated_directory()
        if directory is None:
            return
        if not path.is_absolute():
            path = directory.parent / path
        if (
            path.suffix.lower() != ".docx"
            or directory not in path.parents
            or ".." in path.parts
            or (os.name == "nt" and ":" in path.name)
        ):
            raise ValueError(f"Ожидался путь к файлу .docx внутри generated/: {path}")

        try:
            mode = path.lstat().st_mode
        except FileNotFoundError:
            return
        if not S_ISREG(mode):
            raise ValueError(f"Ожидался обычный файл .docx: {path}")
        _unlink_generated_docx(path)
    except (OSError, ValueError, RuntimeError):
        logger.exception("Не удалось удалить сформированный документ: %s", path)
        raise


def cleanup_old_files(max_age_days=7):
    """Remove top-level DOCX files in generated/ older than the given age by mtime."""
    if max_age_days < 0:
        raise ValueError("Возраст документов не может быть отрицательным")
    cutoff = time() - timedelta(days=max_age_days).total_seconds()

    path = Path("generated")
    try:
        directory = _generated_directory()
        if directory is None:
            return
        path = directory
        try:
            entries = list(directory.iterdir())
        except FileNotFoundError:
            return

        for path in entries:
            if path.suffix.lower() != ".docx":
                continue
            try:
                metadata = path.lstat()
            except FileNotFoundError:
                continue
            if S_ISREG(metadata.st_mode) and metadata.st_mtime < cutoff:
                _unlink_generated_docx(path)
    except (OSError, ValueError, RuntimeError):
        logger.exception("Не удалось очистить сформированные документы: %s", path)
        raise
