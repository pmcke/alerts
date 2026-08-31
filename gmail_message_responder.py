#!/usr/bin/env python3
"""
gmail_message_responder.py

Monitors a Gmail inbox using IMAP. When it finds a message from the configured
sender with the configured subject, it extracts an email address from the body
and sends a templated response through Mailjet.

After a successful Mailjet send, the incoming message is moved from INBOX to
the configured IMAP folder, for example "Processed".

Place this file in the same folder as:

    camera_monitor.py
    config.ini
    gmail_responder.ini
    templates/gmail_thank_you.html

Mailjet credentials are read from the existing [mailjet] section of config.ini.
Gmail settings and matching rules are read from gmail_responder.ini.

This program is independent of camera_monitor.py. It does not use station
records, the stations database, alert state, or unsubscribe links.
"""

from __future__ import annotations

import configparser
import email
import html
import imaplib
import logging
import re
import signal
import sys
import time
from email.header import decode_header, make_header
from email.message import Message
from pathlib import Path
from typing import Iterable

from mailjet_rest import Client


# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

BASE_DIR = Path(__file__).resolve().parent

RESPONDER_CONFIG_FILE = BASE_DIR / "gmail_responder.ini"
CAMERA_CONFIG_FILE = BASE_DIR / "config.ini"

TEMPLATE_DIR_PRIMARY = BASE_DIR / "templates"
TEMPLATE_DIR_FALLBACK = Path("/etc/camera-monitor/templates")


# ---------------------------------------------------------------------------
# Logging and shutdown handling
# ---------------------------------------------------------------------------

logger = logging.getLogger("gmail_message_responder")
running = True


def stop_requested(signum, frame):
    """Request a clean shutdown after the current mailbox check."""
    global running
    logger.info("Stop requested; exiting after the current check")
    running = False


# ---------------------------------------------------------------------------
# Configuration helpers
# ---------------------------------------------------------------------------

def load_ini(path: Path) -> configparser.ConfigParser:
    """Load an INI file without ConfigParser interpolation."""
    config = configparser.ConfigParser(interpolation=None)

    if not config.read(path):
        raise FileNotFoundError(f"Could not read configuration file: {path}")

    return config


# ---------------------------------------------------------------------------
# Email parsing helpers
# ---------------------------------------------------------------------------

def decode_mime_header(value: str | None) -> str:
    """Decode an encoded email header such as Subject or From."""
    if not value:
        return ""

    try:
        return str(make_header(decode_header(value))).strip()
    except Exception:
        return str(value).strip()


def normalise_subject(value: str) -> str:
    """Collapse whitespace and make a subject suitable for comparison."""
    return " ".join((value or "").split()).casefold()


def extract_sender_address(from_header: str) -> str:
    """Extract the first email address from a From header."""
    match = re.search(
        r"[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}",
        from_header or "",
        flags=re.IGNORECASE,
    )

    if not match:
        return ""

    return match.group(0).casefold()


def extract_text_parts(message: Message) -> tuple[str, str]:
    """
    Return the plain-text and HTML portions of a message.

    Attachments are ignored.
    """
    plain_parts: list[str] = []
    html_parts: list[str] = []

    parts: Iterable[Message]

    if message.is_multipart():
        parts = message.walk()
    else:
        parts = [message]

    for part in parts:
        if part.is_multipart():
            continue

        disposition = (part.get_content_disposition() or "").lower()

        if disposition == "attachment":
            continue

        content_type = part.get_content_type().lower()

        if content_type not in {"text/plain", "text/html"}:
            continue

        try:
            payload = part.get_payload(decode=True)

            if payload is None:
                text = str(part.get_payload() or "")
            else:
                charset = part.get_content_charset() or "utf-8"
                text = payload.decode(charset, errors="replace")

        except Exception:
            logger.exception("Could not decode one message body part")
            continue

        if content_type == "text/plain":
            plain_parts.append(text)
        else:
            html_parts.append(text)

    return "\n".join(plain_parts), "\n".join(html_parts)


def html_to_text(value: str) -> str:
    """Convert enough HTML to text to locate an email address."""
    text = re.sub(
        r"(?is)<(script|style).*?>.*?</\1>",
        " ",
        value or "",
    )
    text = re.sub(r"(?s)<[^>]+>", " ", text)

    return html.unescape(text)


def extract_recipient_email(
    body_text: str,
    extraction_regex: str,
    allowed_domains: list[str],
) -> str | None:
    """
    Extract the observer's email address from a fireball report.

    Forwarding can alter line breaks or join the word "Local" directly to the
    address. To avoid that, isolate the text between "Contact of the observer"
    and "Local Date & Time", then extract an address only from that section.

    The configured extraction_regex remains available as a fallback.
    """
    text = html.unescape(body_text or "")
    text = text.replace("\r\n", "\n").replace("\r", "\n")

    section_match = re.search(
        r"Contact\s+of\s+the\s+observer"
        r"(?P<section>.*?)"
        r"Local\s+Date\s*(?:&|and)?\s*Time",
        text,
        flags=re.IGNORECASE | re.DOTALL,
    )

    candidate = ""

    if section_match:
        observer_section = section_match.group("section")

        email_match = re.search(
            r"-\s*(?P<email>"
            r"[A-Z0-9._%+-]+"
            r"@[A-Z0-9.-]+"
            r"\.[A-Z]{2,24}"
            r")",
            observer_section,
            flags=re.IGNORECASE,
        )

        if not email_match:
            email_match = re.search(
                r"(?P<email>"
                r"[A-Z0-9._%+-]+"
                r"@[A-Z0-9.-]+"
                r"\.[A-Z]{2,24}"
                r")",
                observer_section,
                flags=re.IGNORECASE,
            )

        if email_match:
            candidate = email_match.group("email").strip()

    if not candidate:
        try:
            match = re.search(
                extraction_regex,
                text,
                flags=re.IGNORECASE | re.MULTILINE | re.DOTALL,
            )
        except re.error as exc:
            raise ValueError(
                f"Invalid recipient_regex in gmail_responder.ini: {exc}"
            ) from exc

        if not match:
            return None

        if "email" in match.groupdict():
            candidate = match.group("email")
        elif match.lastindex:
            candidate = match.group(1)
        else:
            candidate = match.group(0)

    candidate = (candidate or "").strip().strip(
        "<>()[]{}'\".,;:"
    )

    if not re.fullmatch(
        r"[^@\s]+@[^@\s]+\.[^@\s]+",
        candidate,
    ):
        return None

    if allowed_domains:
        domain = candidate.rsplit("@", 1)[1].casefold()

        if domain not in allowed_domains:
            logger.warning(
                "Extracted address domain is not permitted: %s",
                domain,
            )
            return None

    logger.info("Extracted observer email address: %s", candidate)
    return candidate


# ---------------------------------------------------------------------------
# Template and Mailjet helpers
# ---------------------------------------------------------------------------

def load_template(template_filename: str) -> str:
    """Load an HTML email template."""
    candidates = [
        TEMPLATE_DIR_PRIMARY / template_filename,
        TEMPLATE_DIR_FALLBACK / template_filename,
    ]

    for path in candidates:
        try:
            return path.read_text(encoding="utf-8")

        except FileNotFoundError:
            continue

        except Exception:
            logger.exception("Could not read template: %s", path)
            return ""

    logger.error(
        "Template %s was not found in %s or %s",
        template_filename,
        TEMPLATE_DIR_PRIMARY,
        TEMPLATE_DIR_FALLBACK,
    )

    return ""


def send_email(
    mailjet: Client,
    from_email: str,
    from_name: str,
    recipient_email: str,
    subject: str,
    template_filename: str,
    template_vars: dict[str, str] | None = None,
    bcc_addresses: list[str] | None = None,
) -> bool:
    """
    Send one generic HTML email through Mailjet.

    This function has no knowledge of stations, databases, alerts, or
    unsubscribe links.
    """
    template = load_template(template_filename)

    if not template:
        return False

    merged_vars = dict(template_vars or {})

    merged_vars.setdefault(
        "recipient_email",
        html.escape(recipient_email),
    )

    try:
        body_html = template.format(**merged_vars)

    except KeyError as exc:
        logger.error(
            "Template %s contains an unknown placeholder: %s",
            template_filename,
            exc,
        )
        return False

    except Exception:
        logger.exception(
            "Template formatting failed for %s",
            template_filename,
        )
        return False

    message = {
        "From": {
            "Email": from_email,
            "Name": from_name,
        },
        "To": [
            {
                "Email": recipient_email,
            }
        ],
        "Subject": subject,
        "HTMLPart": body_html,
    }

    if bcc_addresses:
        message["Bcc"] = [
            {"Email": address}
            for address in bcc_addresses
        ]

    data = {
        "Messages": [
            message,
        ]
    }

    max_attempts = 3
    retry_delay_seconds = 10

    for attempt in range(1, max_attempts + 1):
        try:
            result = mailjet.send.create(data=data)
            break

        except Exception:
            if attempt >= max_attempts:
                logger.exception(
                    "Mailjet send failed after %d attempt(s)",
                    max_attempts,
                )
                return False

            logger.exception(
                "Mailjet send attempt %d/%d failed; retrying in %d seconds",
                attempt,
                max_attempts,
                retry_delay_seconds,
            )
            time.sleep(retry_delay_seconds)

    if result.status_code >= 300:
        try:
            response_body = result.json()
        except Exception:
            response_body = getattr(result, "text", "")

        logger.error(
            "Mailjet error %s: %s",
            result.status_code,
            response_body,
        )
        return False

    logger.info(
        "Mailjet email sent to %s with subject %r",
        recipient_email,
        subject,
    )

    return True


# ---------------------------------------------------------------------------
# IMAP folder handling
# ---------------------------------------------------------------------------

def quote_mailbox_name(name: str) -> str:
    """Quote an IMAP mailbox name safely."""
    escaped = (name or "").replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def ensure_processed_folder(
    mailbox: imaplib.IMAP4_SSL,
    processed_folder: str,
) -> bool:
    """
    Ensure that the destination folder exists.

    Gmail labels appear as IMAP folders. We list all folders and look for the
    configured name. If it is absent, CREATE makes the Gmail label.
    """
    status, folders = mailbox.list()

    if status == "OK" and folders:
        wanted = processed_folder.casefold()

        for item in folders:
            if not item:
                continue

            decoded = item.decode("utf-8", errors="replace").casefold()

            # Gmail's LIST response includes flags, delimiter and mailbox name.
            # Matching the final mailbox name exactly is awkward because mailbox
            # names may be quoted, so accept a quoted or unquoted ending.
            if (
                decoded.rstrip().endswith(f'"{wanted}"')
                or decoded.rstrip().endswith(wanted)
            ):
                return True

    logger.info(
        "Processed folder %r was not found; attempting to create it",
        processed_folder,
    )

    status, response = mailbox.create(processed_folder)

    if status == "OK":
        logger.info("Created IMAP folder %r", processed_folder)
        return True

    # Some servers return NO if another process created the folder between
    # LIST and CREATE. Re-list before treating that as a hard failure.
    status, folders = mailbox.list()

    if status == "OK" and folders:
        wanted = processed_folder.casefold()

        for item in folders:
            if not item:
                continue

            decoded = item.decode("utf-8", errors="replace").casefold()

            if (
                decoded.rstrip().endswith(f'"{wanted}"')
                or decoded.rstrip().endswith(wanted)
            ):
                return True

    logger.error(
        "Could not create IMAP folder %r: %r",
        processed_folder,
        response,
    )
    return False


def move_processed_message(
    mailbox: imaplib.IMAP4_SSL,
    message_id: bytes,
    processed_folder: str,
) -> bool:
    """
    Move a successfully processed message out of the Inbox.

    Prefer the IMAP MOVE command when supported. Otherwise use COPY followed by
    marking the original as deleted and EXPUNGE.
    """
    destination = quote_mailbox_name(processed_folder)

    capabilities = {
        item.decode("ascii", errors="ignore").upper()
        if isinstance(item, bytes)
        else str(item).upper()
        for item in getattr(mailbox, "capabilities", set())
    }

    if "MOVE" in capabilities:
        try:
            status, response = mailbox.uid(
                "MOVE",
                message_id,
                destination,
            )

            if status == "OK":
                logger.info(
                    "Moved incoming message %r to %s",
                    message_id,
                    processed_folder,
                )
                return True

            logger.warning(
                "IMAP MOVE failed for message %r: %r; using fallback",
                message_id,
                response,
            )

        except Exception:
            logger.exception(
                "IMAP MOVE failed for message %r; using fallback",
                message_id,
            )

    status, response = mailbox.uid(
        "COPY",
        message_id,
        destination,
    )

    if status != "OK":
        logger.error(
            "Could not copy message %r to %s: %r",
            message_id,
            processed_folder,
            response,
        )
        return False

    status, response = mailbox.uid(
        "STORE",
        message_id,
        "+FLAGS",
        r"(\Deleted)",
    )

    if status != "OK":
        logger.error(
            "Message %r was copied to %s, but the Inbox copy could not "
            "be marked deleted: %r",
            message_id,
            processed_folder,
            response,
        )
        return False

    status, response = mailbox.expunge()

    if status != "OK":
        logger.error(
            "Message %r was copied and marked deleted, but EXPUNGE failed: %r",
            message_id,
            response,
        )
        return False

    logger.info(
        "Moved incoming message %r to %s using COPY/DELETE fallback",
        message_id,
        processed_folder,
    )

    return True


# ---------------------------------------------------------------------------
# Gmail processing
# ---------------------------------------------------------------------------

def process_one_message(
    mailbox: imaplib.IMAP4_SSL,
    message_id: bytes,
    required_sender: str,
    required_subject: str,
    exact_subject: bool,
    recipient_regex: str,
    allowed_domains: list[str],
    mailjet: Client,
    from_email: str,
    from_name: str,
    outgoing_subject: str,
    template_filename: str,
    bcc_addresses: list[str],
    processed_folder: str,
) -> None:
    """Fetch, validate, respond to, and move one Gmail message."""
    status, fetched = mailbox.uid(
        "FETCH",
        message_id,
        "(BODY.PEEK[])",
    )

    if status != "OK" or not fetched:
        logger.warning(
            "Could not fetch Gmail message %r",
            message_id,
        )
        return

    raw_message = None

    for item in fetched:
        if isinstance(item, tuple) and len(item) >= 2:
            raw_message = item[1]
            break

    if not raw_message:
        logger.warning(
            "Gmail message %r contained no RFC822 body",
            message_id,
        )
        return

    message = email.message_from_bytes(raw_message)

    sender_header = decode_mime_header(message.get("From"))
    sender_address = extract_sender_address(sender_header)

    actual_subject = decode_mime_header(message.get("Subject"))

    if sender_address != required_sender:
        return

    if exact_subject:
        subject_matches = (
            normalise_subject(actual_subject)
            == normalise_subject(required_subject)
        )
    else:
        subject_matches = (
            normalise_subject(required_subject)
            in normalise_subject(actual_subject)
        )

    if not subject_matches:
        return

    plain_text, html_text = extract_text_parts(message)
    body_text = plain_text.strip() or html_to_text(html_text)

    recipient_email = extract_recipient_email(
        body_text,
        recipient_regex,
        allowed_domains,
    )

    if not recipient_email:
        logger.warning(
            "Matching Gmail message %r contained no permitted recipient address",
            decode_mime_header(message.get("Message-ID")),
        )
        return

    template_vars = {
        "original_sender": html.escape(sender_address),
        "original_subject": html.escape(actual_subject),
    }

    sent = send_email(
        mailjet=mailjet,
        from_email=from_email,
        from_name=from_name,
        recipient_email=recipient_email,
        subject=outgoing_subject,
        template_filename=template_filename,
        template_vars=template_vars,
        bcc_addresses=bcc_addresses,
    )

    if not sent:
        return

    moved = move_processed_message(
        mailbox=mailbox,
        message_id=message_id,
        processed_folder=processed_folder,
    )

    if not moved:
        logger.warning(
            "Mailjet response was sent, but incoming message %r remains in INBOX",
            message_id,
        )


# ---------------------------------------------------------------------------
# Main monitoring loop
# ---------------------------------------------------------------------------

def monitor_gmail() -> int:
    """Load configuration and continuously monitor Gmail."""
    responder_config = load_ini(RESPONDER_CONFIG_FILE)
    camera_config = load_ini(CAMERA_CONFIG_FILE)

    gmail_username = responder_config.get(
        "gmail",
        "username",
        fallback="",
    ).strip()

    gmail_password = responder_config.get(
        "gmail",
        "password",
        fallback="",
    ).strip()

    imap_host = responder_config.get(
        "gmail",
        "imap_host",
        fallback="imap.gmail.com",
    ).strip()

    mailbox_name = responder_config.get(
        "gmail",
        "mailbox",
        fallback="INBOX",
    ).strip()

    required_sender = responder_config.get(
        "filter",
        "from_address",
        fallback="",
    ).strip().casefold()

    required_subject = responder_config.get(
        "filter",
        "subject",
        fallback="",
    ).strip()

    exact_subject = responder_config.getboolean(
        "filter",
        "exact_subject",
        fallback=True,
    )

    only_unseen = responder_config.getboolean(
        "filter",
        "only_unseen",
        fallback=True,
    )

    recipient_regex = responder_config.get(
        "extraction",
        "recipient_regex",
        fallback=(
            r"(?P<email>"
            r"[A-Z0-9._%+-]+"
            r"@[A-Z0-9.-]+"
            r"\.[A-Z]{2,}"
            r")"
        ),
    )

    allowed_domains = [
        item.strip().casefold()
        for item in responder_config.get(
            "extraction",
            "allowed_domains",
            fallback="",
        ).split(",")
        if item.strip()
    ]

    outgoing_subject = responder_config.get(
        "response",
        "subject",
        fallback="Thank you for your message",
    ).strip()

    template_filename = responder_config.get(
        "response",
        "template",
        fallback="gmail_thank_you.html",
    ).strip()

    poll_seconds = max(
        10,
        responder_config.getint(
            "monitor",
            "poll_seconds",
            fallback=60,
        ),
    )

    processed_folder = responder_config.get(
        "monitor",
        "processed_folder",
        fallback="Processed",
    ).strip()

    mailjet_api_key = camera_config.get(
        "mailjet",
        "api_key",
        fallback="",
    ).strip()

    mailjet_secret = camera_config.get(
        "mailjet",
        "api_secret",
        fallback="",
    ).strip()

    from_email = camera_config.get(
        "mailjet",
        "from_email",
        fallback="",
    ).strip()

    from_name = camera_config.get(
        "mailjet",
        "from_name",
        fallback="Camera Alerts",
    ).strip()

    bcc_addresses = [
        item.strip()
        for item in camera_config.get(
            "mailjet",
            "bcc_emails",
            fallback="",
        ).split(",")
        if item.strip()
    ]

    required_values = {
        "gmail.username": gmail_username,
        "gmail.password": gmail_password,
        "filter.from_address": required_sender,
        "filter.subject": required_subject,
        "response.template": template_filename,
        "monitor.processed_folder": processed_folder,
        "mailjet.api_key": mailjet_api_key,
        "mailjet.api_secret": mailjet_secret,
        "mailjet.from_email": from_email,
    }

    missing = [
        name
        for name, value in required_values.items()
        if not value
    ]

    if missing:
        raise ValueError(
            "Missing configuration value(s): "
            + ", ".join(missing)
        )

    mailjet = Client(
        auth=(mailjet_api_key, mailjet_secret),
        version="v3.1",
    )

    logger.info(
        "Monitoring %s/%s for sender=%s subject=%r every %s seconds",
        imap_host,
        mailbox_name,
        required_sender,
        required_subject,
        poll_seconds,
    )

    while running:
        mailbox: imaplib.IMAP4_SSL | None = None

        try:
            mailbox = imaplib.IMAP4_SSL(imap_host)
            mailbox.login(gmail_username, gmail_password)

            status, response = mailbox.select(
                quote_mailbox_name(mailbox_name)
            )

            if status != "OK":
                raise RuntimeError(
                    f"Could not select mailbox {mailbox_name!r}: "
                    f"{response!r}"
                )

            if not ensure_processed_folder(
                mailbox,
                processed_folder,
            ):
                raise RuntimeError(
                    f"Processed folder {processed_folder!r} is unavailable"
                )

            search_criteria = ["UNSEEN"] if only_unseen else ["ALL"]

            status, search_data = mailbox.uid(
                "SEARCH",
                None,
                *search_criteria,
            )

            if status != "OK":
                raise RuntimeError("Gmail IMAP search failed")

            message_ids = search_data[0].split()

            if message_ids:
                logger.info(
                    "Found %d candidate message(s)",
                    len(message_ids),
                )

            for message_id in message_ids:
                if not running:
                    break

                process_one_message(
                    mailbox=mailbox,
                    message_id=message_id,
                    required_sender=required_sender,
                    required_subject=required_subject,
                    exact_subject=exact_subject,
                    recipient_regex=recipient_regex,
                    allowed_domains=allowed_domains,
                    mailjet=mailjet,
                    from_email=from_email,
                    from_name=from_name,
                    outgoing_subject=outgoing_subject,
                    template_filename=template_filename,
                    bcc_addresses=bcc_addresses,
                    processed_folder=processed_folder,
                )

            try:
                mailbox.close()
            except Exception:
                pass

            mailbox.logout()
            mailbox = None

        except imaplib.IMAP4.error:
            logger.exception(
                "Gmail IMAP login or mailbox error. "
                "For Gmail, use an app password rather than "
                "the normal account password."
            )

        except Exception:
            logger.exception("Error while checking Gmail")

        finally:
            if mailbox is not None:
                try:
                    mailbox.logout()
                except Exception:
                    pass

        for _ in range(poll_seconds):
            if not running:
                break

            time.sleep(1)

    logger.info("Gmail responder stopped")
    return 0


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )

    signal.signal(signal.SIGTERM, stop_requested)
    signal.signal(signal.SIGINT, stop_requested)

    try:
        return monitor_gmail()

    except Exception:
        logger.exception(
            "Fatal configuration or startup error"
        )
        return 1


if __name__ == "__main__":
    sys.exit(main())
