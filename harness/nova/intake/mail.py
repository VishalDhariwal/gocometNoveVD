"""Read one .eml: headers, the body split into the sender's own words and the text it quotes, the attachments, and
the email's page text exactly as the grader prints it (so quotes from an email body always resolve).

Identity is the From *address*, lower-cased. The display name is kept for reading, never for authority.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from email import policy
from email.message import EmailMessage
from email.parser import BytesParser
from email.utils import getaddresses, parseaddr, parsedate_to_datetime

from .. import normalize as norm

_REPLY_ATTRIBUTION = re.compile(r"^\s*On\b.{0,200}\bwrote:\s*$")
_FORWARD_MARKER = re.compile(
    r"^\s*(?:-{2,}\s*(?:Forwarded|Original)\s+message\s*-{2,}|-{3,}\s*Original Message\s*-{3,}|Begin forwarded message:)",
    re.IGNORECASE)
_HEADER_LINE = re.compile(r"^\s*(From|Sent|Date|To|Cc|Subject):\s", re.IGNORECASE)


@dataclass
class ParsedEmail:
    message_id: str | None
    from_addr: str
    from_name: str | None
    to_addrs: list[str]
    cc_addrs: list[str]
    subject: str | None
    in_reply_to: str | None
    received_at: str | None          # UTC, from the Date header; None when it's missing or unreadable
    body: str
    authored: str
    quoted: str
    page_text: str                   # identical to grader/extract.py's email page
    attachments: list[tuple[str, bytes]] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


def _starts_header_block(lines: list[str], i: int) -> bool:
    """An Outlook-style quoted header: 'From: ...' followed within a few lines by Sent/Date and To/Subject."""
    if not re.match(r"^\s*From:\s", lines[i], re.IGNORECASE):
        return False
    following = {m.group(1).lower() for line in lines[i + 1:i + 5] if (m := _HEADER_LINE.match(line))}
    return bool(following & {"sent", "date"}) and bool(following & {"to", "subject"})


def split_quoted(body: str) -> tuple[str, str]:
    """-> (authored, quoted).

    Quoted: lines starting with '>', the 'On ... wrote:' attribution line, and everything after a forward marker or a
    quoted header block. Authored text after a '>' block (an inline reply) stays authored. When in doubt, text lands
    in `quoted`, which only ever lowers its authority, never raises it.
    """
    lines = body.split("\n")
    authored: list[str] = []
    quoted: list[str] = []
    forwarded = False
    for i, line in enumerate(lines):
        if not forwarded and (_FORWARD_MARKER.match(line) or _starts_header_block(lines, i)):
            forwarded = True
        if forwarded or line.lstrip().startswith(">") or _REPLY_ATTRIBUTION.match(line):
            quoted.append(line)
        else:
            authored.append(line)
    return "\n".join(authored).strip("\n"), "\n".join(quoted).strip("\n")


def _grader_page(msg: EmailMessage, body_lines: list[str], names: list[str], warnings: list[str]) -> str:
    """Same lines, same order, same formatting as grader/extract.py's email_lines. Note: like the grader, the Date
    line prints the header's clock time with a 'Z', without converting to UTC. That's only for quote matching;
    received_at below is converted properly."""
    name, addr = parseaddr(str(msg["From"]))
    out = [f"From: {name} <{addr}>", f"To: {msg['To']}"]
    if msg["Cc"]:
        out.append(f"Cc: {msg['Cc']}")
    try:
        out.append(f"Date: {parsedate_to_datetime(str(msg['Date'])).strftime('%Y-%m-%dT%H:%M:%SZ')}")
    except (TypeError, ValueError):
        warnings.append(f"unreadable Date header {msg['Date']!r}")
        out.append(f"Date: {msg['Date']}")
    out += [f"Subject: {msg['Subject']}", ""]
    out += body_lines
    if names:
        out.append("Attachments: " + ", ".join(names))
    return "\n".join(out)


def parse_email(data: bytes) -> ParsedEmail:
    msg = BytesParser(policy=policy.default).parsebytes(data)
    warnings: list[str] = []

    from_name, from_addr = parseaddr(str(msg["From"] or ""))
    if not from_addr:
        warnings.append("no From address")

    received_at = None
    try:
        received_at = norm.utc_iso(parsedate_to_datetime(str(msg["Date"])))
    except (TypeError, ValueError):
        warnings.append(f"no usable Date header ({msg['Date']!r})")

    body_part = msg.get_body(preferencelist=("plain",))
    if body_part is not None:
        body = body_part.get_content().replace("\r\n", "\n").rstrip("\n")
    else:
        html_part = msg.get_body(preferencelist=("html",))
        body = re.sub(r"<[^>]+>", " ", html_part.get_content()) if html_part is not None else ""
        warnings.append("no plain-text body" + ("; used the HTML part" if html_part is not None else ""))
    body_lines = body.split("\n") if body_part is not None else []

    attachments = [(part.get_filename() or "", part.get_content()) for part in msg.iter_attachments()]
    authored, quoted = split_quoted(body)

    return ParsedEmail(
        message_id=str(msg["Message-ID"]).strip() if msg["Message-ID"] else None,
        from_addr=norm.email(from_addr),
        from_name=from_name or None,
        to_addrs=[norm.email(a) for _, a in getaddresses([str(v) for v in msg.get_all("To", [])]) if a],
        cc_addrs=[norm.email(a) for _, a in getaddresses([str(v) for v in msg.get_all("Cc", [])]) if a],
        subject=str(msg["Subject"]) if msg["Subject"] is not None else None,
        in_reply_to=str(msg["In-Reply-To"]).strip() if msg["In-Reply-To"] else None,
        received_at=received_at,
        body=body,
        authored=authored,
        quoted=quoted,
        page_text=_grader_page(msg, body_lines, [n for n, _ in attachments], warnings),
        attachments=attachments,
        warnings=warnings,
    )
