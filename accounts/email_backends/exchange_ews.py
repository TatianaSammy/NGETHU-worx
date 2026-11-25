"""Django email backend for on-premises Microsoft Exchange EWS."""

from __future__ import annotations

import base64
import logging
import xml.etree.ElementTree as ET
from email.utils import parseaddr
from urllib.parse import urlparse

import requests
from django.conf import settings
from django.core.exceptions import ImproperlyConfigured
from django.core.mail.backends.base import BaseEmailBackend
from requests_ntlm import HttpNtlmAuth

logger = logging.getLogger(__name__)

SOAP_NS = "http://schemas.xmlsoap.org/soap/envelope/"
MESSAGES_NS = "http://schemas.microsoft.com/exchange/services/2006/messages"
TYPES_NS = "http://schemas.microsoft.com/exchange/services/2006/types"

ET.register_namespace("soap", SOAP_NS)
ET.register_namespace("m", MESSAGES_NS)
ET.register_namespace("t", TYPES_NS)


class ExchangeEwsDeliveryError(RuntimeError):
    """Raised when Exchange rejects or cannot accept an EWS message."""


class ExchangeEWSBackend(BaseEmailBackend):
    """Send Django email messages through Exchange Web Services over HTTPS/NTLM."""

    def __init__(self, fail_silently=False, **kwargs):
        super().__init__(fail_silently=fail_silently)
        self.endpoint = settings.EXCHANGE_EWS_URL
        self.username = settings.EXCHANGE_USERNAME
        self.password = settings.EXCHANGE_PASSWORD
        self.domain = settings.EXCHANGE_DOMAIN
        self.timeout = max(1, int(settings.EXCHANGE_TIMEOUT))
        self.verify_ssl = settings.EXCHANGE_VERIFY_SSL
        self.ca_bundle = settings.EXCHANGE_CA_BUNDLE

        if not self.endpoint or urlparse(self.endpoint).scheme.lower() != "https":
            raise ImproperlyConfigured("EXCHANGE_EWS_URL must be an HTTPS EWS endpoint.")
        if not self.username or not self.password or not self.domain:
            raise ImproperlyConfigured(
                "EXCHANGE_USERNAME, EXCHANGE_PASSWORD, and EXCHANGE_DOMAIN are required."
            )
        if not self.verify_ssl:
            raise ImproperlyConfigured(
                "Exchange TLS certificate verification must remain enabled. "
                "Configure EXCHANGE_CA_BUNDLE for a private certificate authority."
            )

    @staticmethod
    def _tag(namespace, name):
        return f"{{{namespace}}}{name}"

    @staticmethod
    def _extract_body(message):
        for alternative in getattr(message, "alternatives", ()) or ():
            content = getattr(alternative, "content", None)
            mimetype = getattr(alternative, "mimetype", None)
            if isinstance(alternative, tuple) and len(alternative) >= 2:
                content, mimetype = alternative[0], alternative[1]
            if mimetype == "text/html":
                return str(content or ""), "HTML"

        subtype = getattr(message, "content_subtype", "plain")
        body_type = "HTML" if subtype.lower() == "html" else "Text"
        return str(getattr(message, "body", "") or ""), body_type

    @staticmethod
    def _email_address(value):
        _, address = parseaddr(str(value or ""))
        return address or str(value or "").strip()

    @staticmethod
    def _attachment_parts(attachment, encoding):
        if isinstance(attachment, tuple):
            if len(attachment) == 2:
                filename, content = attachment
                mimetype = "application/octet-stream"
            else:
                filename, content, mimetype = attachment[:3]
        else:
            filename = getattr(attachment, "filename", None) or "attachment"
            content = getattr(attachment, "content", b"")
            mimetype = getattr(attachment, "mimetype", None) or "application/octet-stream"

        if isinstance(content, str):
            content = content.encode(encoding or "utf-8")
        elif not isinstance(content, bytes):
            content = bytes(content)
        return str(filename), content, str(mimetype or "application/octet-stream")

    def _build_request(self, email_message):
        envelope = ET.Element(self._tag(SOAP_NS, "Envelope"))
        header = ET.SubElement(envelope, self._tag(SOAP_NS, "Header"))
        ET.SubElement(
            header,
            self._tag(TYPES_NS, "RequestServerVersion"),
            {"Version": "Exchange2013"},
        )

        body = ET.SubElement(envelope, self._tag(SOAP_NS, "Body"))
        create_item = ET.SubElement(
            body,
            self._tag(MESSAGES_NS, "CreateItem"),
            {"MessageDisposition": "SendAndSaveCopy"},
        )
        saved_folder = ET.SubElement(create_item, self._tag(MESSAGES_NS, "SavedItemFolderId"))
        ET.SubElement(
            saved_folder,
            self._tag(TYPES_NS, "DistinguishedFolderId"),
            {"Id": "sentitems"},
        )
        items = ET.SubElement(create_item, self._tag(MESSAGES_NS, "Items"))
        message = ET.SubElement(items, self._tag(TYPES_NS, "Message"))
        ET.SubElement(message, self._tag(TYPES_NS, "Subject")).text = str(
            getattr(email_message, "subject", "") or ""
        )

        body_content, body_type = self._extract_body(email_message)
        ET.SubElement(
            message,
            self._tag(TYPES_NS, "Body"),
            {"BodyType": body_type},
        ).text = body_content

        attachments = getattr(email_message, "attachments", ()) or ()
        if attachments:
            attachment_list = ET.SubElement(message, self._tag(TYPES_NS, "Attachments"))
            encoding = getattr(email_message, "encoding", None) or settings.DEFAULT_CHARSET
            for attachment in attachments:
                filename, content, mimetype = self._attachment_parts(attachment, encoding)
                file_attachment = ET.SubElement(
                    attachment_list, self._tag(TYPES_NS, "FileAttachment")
                )
                ET.SubElement(file_attachment, self._tag(TYPES_NS, "Name")).text = filename
                ET.SubElement(file_attachment, self._tag(TYPES_NS, "ContentType")).text = mimetype
                ET.SubElement(file_attachment, self._tag(TYPES_NS, "Content")).text = (
                    base64.b64encode(content).decode("ascii")
                )

        for field_name, element_name in (
            ("to", "ToRecipients"),
            ("cc", "CcRecipients"),
            ("bcc", "BccRecipients"),
            ("reply_to", "ReplyTo"),
        ):
            addresses = getattr(email_message, field_name, ()) or ()
            if not addresses:
                continue
            recipient_list = ET.SubElement(message, self._tag(TYPES_NS, element_name))
            for recipient in addresses:
                address = self._email_address(recipient)
                if not address:
                    continue
                mailbox = ET.SubElement(recipient_list, self._tag(TYPES_NS, "Mailbox"))
                ET.SubElement(mailbox, self._tag(TYPES_NS, "EmailAddress")).text = address

        return ET.tostring(envelope, encoding="utf-8", xml_declaration=True)

    @staticmethod
    def _response_code(response_content):
        try:
            root = ET.fromstring(response_content)
        except ET.ParseError as exc:
            raise ExchangeEwsDeliveryError("Exchange returned an invalid EWS response.") from exc

        for element in root.iter():
            if element.tag.rsplit("}", 1)[-1] == "CreateItemResponseMessage":
                for child in element.iter():
                    if child.tag.rsplit("}", 1)[-1] == "ResponseCode":
                        return child.text or "Unknown"
        return "Unknown"

    def _send_one(self, session, email_message):
        payload = self._build_request(email_message)
        try:
            response = session.post(
                self.endpoint,
                data=payload,
                headers={"Content-Type": "text/xml; charset=utf-8"},
                timeout=(8, self.timeout),
            )
        except requests.RequestException as exc:
            raise ExchangeEwsDeliveryError(
                f"Exchange EWS transport failed ({type(exc).__name__})."
            ) from exc

        if response.status_code < 200 or response.status_code >= 300:
            raise ExchangeEwsDeliveryError(
                f"Exchange EWS returned HTTP {response.status_code}."
            )

        response_code = self._response_code(response.content)
        if response_code != "NoError":
            raise ExchangeEwsDeliveryError(
                f"Exchange EWS rejected the message ({response_code})."
            )

    def send_messages(self, email_messages):
        messages = list(email_messages or ())
        if not messages:
            return 0

        username = self.username
        if "\\" not in username and "@" not in username:
            username = f"{self.domain}\\{username}"

        session = requests.Session()
        session.auth = HttpNtlmAuth(username, self.password)
        session.verify = self.ca_bundle or True
        session.headers["User-Agent"] = "QAD-Django-Exchange-EWS/1.0"

        sent_count = 0
        try:
            for email_message in messages:
                if not email_message.recipients():
                    continue
                try:
                    self._send_one(session, email_message)
                    sent_count += 1
                except ExchangeEwsDeliveryError as exc:
                    if not self.fail_silently:
                        raise
                    logger.warning("Exchange EWS delivery failed (%s)", type(exc).__name__)
        finally:
            session.close()

        return sent_count
