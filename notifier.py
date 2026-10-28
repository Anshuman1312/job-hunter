"""HTML + plain-text email notifications with optional PDF attachments."""

from __future__ import annotations

import html
import logging
import os
import smtplib
import ssl
import time
from datetime import datetime
from email.message import EmailMessage
from email.utils import formataddr, formatdate, make_msgid
from pathlib import Path
from typing import Iterable

from config_loader import AppConfig
from models import Job, MatchResult
from utils import local_timestamp, safe_filename, truncate

_ACCENT = "#1f6feb"
_BG = "#f6f8fa"
_BORDER = "#d0d7de"
_TEXT = "#1f2328"
_MUTED = "#57606a"


class Notifier:
    """SMTP sender for auto-apply confirmations and manual-apply alerts."""

    def __init__(
        self,
        config: AppConfig,
        logger: logging.Logger | None = None,
        force_dry_run: bool = False,
    ) -> None:
        self.app_config = config
        self.config = config.email
        self.logger = logger or logging.getLogger("job_hunter.notifier")
        self.dry_run = force_dry_run or config.email.dry_run
        self.password = (config.email.password or os.getenv(config.email.password_env, "")).strip()

    # ------------------------------------------------------------------ #
    # Public notifications
    # ------------------------------------------------------------------ #
    def send_manual_apply(self, job: Job, reason: str = "") -> bool:
        """Case B: a human must finish this application."""

        subject = f"\U0001f6a8 [Manual Apply Required] {job.title} - {job.company}"
        apply_url = job.job_url_direct or job.job_url
        rows = [
            ("Job title", job.title),
            ("Company", job.company),
            ("Location", job.display_location),
            ("Remote", "Yes" if job.is_remote else "No"),
            ("Platform", job.site or "unknown"),
            ("Direct application link", apply_url),
            ("Detected", local_timestamp()),
        ]
        if reason:
            rows.append(("Why manual action is needed", reason))
        intro = (
            "A matching job could not be applied to automatically. "
            "Use the direct link below to submit your application."
        )
        text = self._plain_text("Manual application required", rows, apply_url, intro)
        body = self._html_document(
            title="Manual application required",
            intro=intro,
            rows=rows,
            button_label="Open application",
            button_url=apply_url,
        )

        attachments: list[Path] = []
        attach_requested = self.config.attach_resume_on_fallback
        if attach_requested:
            attachments.append(self.app_config.application.resume_path)
        return self._send(subject, text, body, attachments=attachments)

    def send_auto_applied(self, job: Job, notes: str = "") -> bool:
        subject = f"[AUTO-APPLIED] {job.title} at {job.company}"
        rows = [
            ("Job title", job.title),
            ("Company", job.company),
            ("Location", job.display_location),
            ("Remote", "Yes" if job.is_remote else "No"),
            ("Platform", job.site or "unknown"),
            ("Job link", job.job_url),
            ("Applied at", local_timestamp()),
        ]
        if notes:
            rows.append(("Match notes", notes))
        intro = "Your application was submitted automatically. Review the job link below for reference."
        text = self._plain_text("Application submitted", rows, job.job_url, intro)
        body = self._html_document(
            title="Application submitted automatically",
            intro=intro,
            rows=rows,
            button_label="View job",
            button_url=job.job_url,
            success=True,
        )

        attachments: list[Path] = []
        if self.config.attach_resume_on_success:
            attachments.append(self.app_config.application.resume_path)
        return self._send(subject, text, body, attachments=attachments)

    def send_run_summary(self, stats: dict[str, object]) -> bool:
        subject = f"[Job Hunter] Run summary - {datetime.now().strftime('%d %b %Y %H:%M')}"
        rows = [(str(key).replace("_", " ").title(), str(value)) for key, value in stats.items()]
        intro = "This is the summary of the latest job-hunter run."
        text = self._plain_text("Job hunter run summary", rows, None, intro)
        body = self._html_document(
            title="Job hunter run summary",
            intro=intro,
            rows=rows,
            button_label=None,
            button_url=None,
        )
        return self._send(subject, text, body)

    def send_job_digest(
        self,
        matches: list[tuple[Job, MatchResult]],
        total_matched: int | None = None,
        omitted: int = 0,
    ) -> bool:
        """One email listing every newly matched job (no auto-application)."""

        total = total_matched if total_matched is not None else len(matches)
        stamp = datetime.now().strftime("%d %b %Y")
        referral_count = sum(
            1 for job, _match in matches if self.app_config.referral_for(job.company)
        )
        subject = f"[Job Hunter] {total} new Python / Data Engineer job(s) - {stamp}"

        text_lines = [
            "NEW JOB MATCHES",
            "=" * 15,
            "",
            f"{total} new role(s) matched Python / Data Engineering at 2-3.5 years.",
        ]
        if referral_count:
            text_lines.append(
                f"{referral_count} of them are at companies where you have a referral."
            )
        text_lines.append("")
        cards: list[str] = []
        for index, (job, match) in enumerate(matches, start=1):
            apply_url = job.job_url_direct or job.job_url
            experience = self._experience_label(job, match)
            source = (job.site or "web").upper()
            snippet = truncate(" ".join(job.plain_description.split()), 260)
            referral = self.app_config.referral_for(job.company)

            text_lines.extend(
                [
                    f"{index}. {job.title} - {job.company}",
                    f"   Location : {job.display_location}",
                    f"   Source   : {source}",
                    f"   Exp band : {experience}",
                    f"   Score    : {match.score}",
                    f"   Apply    : {apply_url}",
                ]
            )
            if referral:
                text_lines.append(f"   Referral : yes - you know someone at {referral}")
            if snippet:
                text_lines.extend([f"   About    : {snippet}"])
            text_lines.append("")

            cards.append(
                self._digest_card(
                    index=index,
                    job=job,
                    match=match,
                    apply_url=apply_url,
                    source=source,
                    experience=experience,
                    snippet=snippet,
                    referral=referral,
                )
            )

        if omitted:
            note = f"{omitted} more matching role(s) were not included in this email."
            text_lines.extend([note, ""])
            cards.append(
                f'<p style="margin:16px 0;color:{_MUTED};font-size:13px;">'
                f"{html.escape(note)}</p>"
            )

        if matches:
            text_lines.extend(["", "ALL APPLY LINKS", "-" * 15, ""])
            for index, (job, _match) in enumerate(matches, start=1):
                apply_url = job.job_url_direct or job.job_url
                text_lines.append(f"{index:02d}. {job.title} - {job.company}")
                text_lines.append(f"    {apply_url}")
                if job.job_url and job.job_url != apply_url:
                    text_lines.append(f"    Job page: {job.job_url}")
                text_lines.append("")

        text_lines.append("Sent by job-hunter. Apply from the links above.")
        body = self._digest_document(total, "".join(cards), omitted, referral_count)
        return self._send(subject, "\n".join(text_lines), body)

    @staticmethod
    def _experience_label(job: Job, match: MatchResult) -> str:
        if match.yoe_known:
            if match.required_min_years is not None and match.required_max_years is not None:
                return f"{match.required_min_years}-{match.required_max_years} years"
            if match.required_min_years is not None:
                return f"{match.required_min_years}+ years"
        if job.experience_range:
            return job.experience_range
        return "not stated (accepted by policy)"

    @staticmethod
    def _digest_card(
        index: int,
        job: Job,
        match: MatchResult,
        apply_url: str,
        source: str,
        experience: str,
        snippet: str,
        referral: str | None = None,
    ) -> str:
        safe_url = html.escape(apply_url, quote=True)
        title = html.escape(job.title)
        company = html.escape(job.company or "Unknown company")
        location = html.escape(job.display_location)
        referral_badge = (
            f'<span style="background:#dafbe1;color:#1a7f37;border-radius:4px;'
            f'padding:2px 7px;font-size:11px;font-weight:700;letter-spacing:0.4px;'
            f'margin-left:8px;">REFERRAL: {html.escape(referral.upper())}</span>'
            if referral
            else ""
        )
        page_link = ""
        if job.job_url and job.job_url != apply_url:
            safe_page = html.escape(job.job_url, quote=True)
            page_link = (
                f'<p style="margin:6px 0 0 0;font-size:12px;color:{_MUTED};'
                f'word-break:break-all;">'
                f'Job page: <a href="{safe_page}" style="color:{_ACCENT};">'
                f"{html.escape(job.job_url)}</a></p>"
            )
        snippet_html = (
            f'<p style="margin:10px 0 0 0;font-size:13px;line-height:1.55;'
            f'color:{_MUTED};">{html.escape(snippet)}</p>'
            if snippet
            else ""
        )
        return f"""
        <div style="border:1px solid {_BORDER};border-radius:10px;padding:18px 20px;
                    margin:0 0 16px 0;background:#ffffff;">
          <div style="font-size:12px;color:{_MUTED};letter-spacing:0.6px;
                      text-transform:uppercase;font-weight:600;">
            {index:02d} &middot; {html.escape(source)}{referral_badge}
          </div>
          <h2 style="margin:6px 0 2px 0;font-size:18px;line-height:1.35;color:{_TEXT};">
            <a href="{safe_url}" style="color:{_ACCENT};text-decoration:none;">{title}</a>
          </h2>
          <div style="font-size:14px;color:{_TEXT};font-weight:600;">{company}</div>
          <div style="font-size:13px;color:{_MUTED};margin-top:4px;">
            {location} &nbsp;&middot;&nbsp; {html.escape(experience)}
            &nbsp;&middot;&nbsp; score {match.score}
          </div>
          {snippet_html}
          <p style="margin:14px 0 0 0;">
            <a href="{safe_url}" style="background:{_ACCENT};color:#ffffff;
               text-decoration:none;padding:9px 16px;border-radius:6px;font-size:13px;
               font-weight:600;display:inline-block;">Apply now</a>
          </p>
          <p style="margin:10px 0 0 0;font-size:12px;color:{_MUTED};
                    word-break:break-all;">
            Apply link: <a href="{safe_url}" style="color:{_ACCENT};">{html.escape(apply_url)}</a>
          </p>
          {page_link}
        </div>"""

    def _digest_document(
        self, total: int, cards_html: str, omitted: int, referral_count: int = 0
    ) -> str:
        generated = local_timestamp()
        referral_note = (
            f" &middot; <strong>{referral_count} with a referral</strong>"
            if referral_count
            else ""
        )
        extra = (
            f'<p style="margin:0 0 16px 0;font-size:13px;color:{_MUTED};">'
            f"{omitted} additional match(es) were left out; the next run will "
            f"pick up anything still unseen.</p>"
            if omitted
            else ""
        )
        return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>New job matches</title>
</head>
<body style="margin:0;padding:0;background:{_BG};font-family:-apple-system,BlinkMacSystemFont,
             'Segoe UI',Roboto,Helvetica,Arial,sans-serif;">
  <table role="presentation" width="100%" cellpadding="0" cellspacing="0"
         style="background:{_BG};padding:24px 12px;">
    <tr>
      <td align="center">
        <table role="presentation" width="100%" cellpadding="0" cellspacing="0"
               style="max-width:680px;">
          <tr>
            <td style="padding:22px 24px;background:#ffffff;border:1px solid {_BORDER};
                       border-top:4px solid {_ACCENT};border-radius:10px 10px 0 0;">
              <div style="font-size:12px;letter-spacing:1.2px;text-transform:uppercase;
                          color:{_MUTED};font-weight:600;">Job Hunter</div>
              <h1 style="margin:6px 0 4px 0;font-size:23px;line-height:1.3;color:{_TEXT};">
                {total} new Python / Data Engineer role(s)
              </h1>
              <p style="margin:0;font-size:13px;color:{_MUTED};">
                Target: Python backend &amp; data engineering, 2-3.5 years &middot;
                {html.escape(generated)}{referral_note}
              </p>
            </td>
          </tr>
          <tr>
            <td style="padding:20px 20px 4px 20px;">
              {extra}
              {cards_html}
            </td>
          </tr>
          <tr>
            <td style="padding:14px 24px 22px 24px;color:{_MUTED};font-size:12px;
                       line-height:1.5;">
              Sent by job-hunter. Sources: YC Work at a Startup, Wellfound,
              Cutshort, Foundit, LinkedIn, Indeed, Glassdoor, Naukri, Google
              Jobs, ZipRecruiter, plus the Accenture, Capgemini, Wipro and HCL
              career sites.
            </td>
          </tr>
        </table>
      </td>
    </tr>
  </table>
</body>
</html>"""

    def send_test(self) -> bool:
        subject = "[Job Hunter] SMTP test"
        rows = [
            ("SMTP host", f"{self.config.smtp_host}:{self.config.smtp_port}"),
            ("Sender", self.config.sender_email),
            ("Recipient", self.config.recipient_email),
            ("Sent at", local_timestamp()),
        ]
        intro = "SMTP is configured correctly if you are reading this message."
        text = self._plain_text("SMTP test", rows, None, intro)
        body = self._html_document(
            title="SMTP test successful",
            intro=intro,
            rows=rows,
            button_label=None,
            button_url=None,
            success=True,
        )
        return self._send(subject, text, body)

    # ------------------------------------------------------------------ #
    # Delivery
    # ------------------------------------------------------------------ #
    def _send(
        self,
        subject: str,
        text_body: str,
        html_body: str,
        attachments: Iterable[Path] | None = None,
    ) -> bool:
        recipients = self.config.recipients
        if not recipients:
            self.logger.error("No email recipient configured")
            return False

        message = EmailMessage()
        message["From"] = formataddr((self.config.sender_name, self.config.sender_email))
        message["To"] = ", ".join(recipients)
        message["Subject"] = subject
        message["Date"] = formatdate(localtime=True)
        message["Message-ID"] = make_msgid(domain="job-hunter.local")
        message.set_content(text_body, subtype="plain", charset="utf-8")
        message.add_alternative(html_body, subtype="html", charset="utf-8")

        attached = 0
        for path in attachments or []:
            if self._attach_file(message, Path(path)):
                attached += 1
        if attached:
            self.logger.info("Attached %d file(s) to email", attached)

        if self.dry_run:
            saved = self._write_eml(message, subject)
            self.logger.info("[dry-run] Email not sent; saved to %s", saved)
            return True

        if not self.password:
            self.logger.error(
                "SMTP password missing. Set %s in .env (Gmail App Password).",
                self.config.password_env,
            )
            return False

        return self._deliver(message, recipients)

    def _attach_file(self, message: EmailMessage, path: Path) -> bool:
        if not path.is_file():
            self.logger.error(
                "Cannot attach %s: file not found. Email will be sent without it.", path
            )
            return False
        try:
            data = path.read_bytes()
        except OSError as exc:
            self.logger.error("Cannot read attachment %s: %s", path, exc)
            return False
        filename = self.config.attachment_filename or path.name
        if path.suffix.lower() == ".pdf":
            maintype, subtype = "application", "pdf"
        else:
            maintype, subtype = "application", "octet-stream"
        message.add_attachment(data, maintype=maintype, subtype=subtype, filename=filename)
        return True

    def _deliver(self, message: EmailMessage, recipients: list[str]) -> bool:
        attempts = max(1, int(self.config.max_retries) + 1)
        for attempt in range(1, attempts + 1):
            try:
                self._send_once(message, recipients)
                self.logger.info(
                    "Email sent to %s (subject=%r)", ", ".join(recipients), message["Subject"]
                )
                return True
            except smtplib.SMTPAuthenticationError as exc:
                self.logger.error(
                    "SMTP authentication failed. Check that %s is a valid Gmail App Password "
                    "(not your normal password). Details: %s",
                    self.config.password_env,
                    exc,
                )
                return False
            except (smtplib.SMTPException, OSError, ssl.SSLError) as exc:
                self.logger.warning(
                    "Email attempt %d/%d failed: %s: %s",
                    attempt,
                    attempts,
                    type(exc).__name__,
                    exc,
                )
                if attempt < attempts:
                    time.sleep(min(10, 2 * attempt))
        self.logger.error("Email delivery failed after %d attempt(s)", attempts)
        return False

    def _send_once(self, message: EmailMessage, recipients: list[str]) -> None:
        timeout = max(5, int(self.config.timeout_seconds))
        if self.config.use_ssl:
            server: smtplib.SMTP = smtplib.SMTP_SSL(
                self.config.smtp_host,
                self.config.smtp_port,
                timeout=timeout,
                context=ssl.create_default_context(),
            )
        else:
            server = smtplib.SMTP(
                self.config.smtp_host,
                self.config.smtp_port,
                timeout=timeout,
            )
        try:
            server.ehlo()
            if not self.config.use_ssl and self.config.use_starttls:
                server.starttls(context=ssl.create_default_context())
                server.ehlo()
            server.login(self.config.sender_email, self.password)
            server.send_message(
                message,
                from_addr=self.config.sender_email,
                to_addrs=recipients,
            )
        finally:
            try:
                server.quit()
            except Exception:
                try:
                    server.close()
                except Exception:
                    pass

    def _write_eml(self, message: EmailMessage, subject: str) -> Path:
        outbox = self.config.outbox_dir
        outbox.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
        name = safe_filename(subject, fallback="email", max_length=60)
        path = outbox / f"{stamp}_{name}.eml"
        path.write_bytes(message.as_bytes())
        return path

    # ------------------------------------------------------------------ #
    # Rendering
    # ------------------------------------------------------------------ #
    @staticmethod
    def _plain_text(
        heading: str,
        rows: list[tuple[str, str]],
        link: str | None,
        intro: str,
    ) -> str:
        lines = [heading.upper(), "=" * len(heading), "", intro, ""]
        for label, value in rows:
            lines.append(f"{label}: {value}")
        if link:
            lines.extend(["", f"Apply / view: {link}"])
        lines.extend(["", "Sent by job-hunter."])
        return "\n".join(lines)

    @staticmethod
    def _html_document(
        title: str,
        intro: str,
        rows: list[tuple[str, str]],
        button_label: str | None,
        button_url: str | None,
        success: bool = False,
    ) -> str:
        accent = "#1a7f37" if success else _ACCENT
        row_html = "\n".join(
            f"""
            <tr>
              <td style="padding:10px 12px;border-bottom:1px solid {_BORDER};color:{_MUTED};
                         font-size:13px;width:38%;vertical-align:top;">{html.escape(label)}</td>
              <td style="padding:10px 12px;border-bottom:1px solid {_BORDER};color:{_TEXT};
                         font-size:14px;word-break:break-word;">{Notifier._format_value(label, value)}</td>
            </tr>"""
            for label, value in rows
        )
        button = ""
        if button_label and button_url:
            safe_url = html.escape(button_url, quote=True)
            button = f"""
            <p style="margin:26px 0 8px 0;">
              <a href="{safe_url}" style="background:{accent};color:#ffffff;text-decoration:none;
                 padding:12px 20px;border-radius:6px;font-size:14px;font-weight:600;
                 display:inline-block;">{html.escape(button_label)}</a>
            </p>
            <p style="margin:8px 0 0 0;color:{_MUTED};font-size:12px;word-break:break-all;">
              {safe_url}
            </p>"""

        return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{html.escape(title)}</title>
</head>
<body style="margin:0;padding:0;background:{_BG};font-family:-apple-system,BlinkMacSystemFont,
             'Segoe UI',Roboto,Helvetica,Arial,sans-serif;">
  <table role="presentation" width="100%" cellpadding="0" cellspacing="0"
         style="background:{_BG};padding:24px 12px;">
    <tr>
      <td align="center">
        <table role="presentation" width="100%" cellpadding="0" cellspacing="0"
               style="max-width:640px;background:#ffffff;border:1px solid {_BORDER};
                      border-radius:10px;overflow:hidden;">
          <tr>
            <td style="padding:22px 24px;border-bottom:1px solid {_BORDER};
                       border-top:4px solid {accent};">
              <div style="font-size:12px;letter-spacing:1.2px;text-transform:uppercase;
                          color:{_MUTED};font-weight:600;">Job Hunter</div>
              <h1 style="margin:6px 0 0 0;font-size:21px;line-height:1.3;color:{_TEXT};">
                {html.escape(title)}
              </h1>
            </td>
          </tr>
          <tr>
            <td style="padding:22px 24px;">
              <p style="margin:0 0 18px 0;font-size:14px;line-height:1.6;color:{_TEXT};">
                {html.escape(intro)}
              </p>
              <table role="presentation" width="100%" cellpadding="0" cellspacing="0"
                     style="border:1px solid {_BORDER};border-radius:8px;border-collapse:separate;
                            border-spacing:0;overflow:hidden;">
                {row_html}
              </table>
              {button}
            </td>
          </tr>
          <tr>
            <td style="padding:16px 24px;background:{_BG};color:{_MUTED};font-size:12px;
                       line-height:1.5;">
              Sent by job-hunter on {html.escape(local_timestamp())}.
              This is an automated message about your own job search.
            </td>
          </tr>
        </table>
      </td>
    </tr>
  </table>
</body>
</html>"""

    @staticmethod
    def _format_value(label: str, value: str) -> str:
        text = str(value)
        if text.startswith(("http://", "https://")):
            safe = html.escape(text, quote=True)
            return f'<a href="{safe}" style="color:{_ACCENT};">{safe}</a>'
        return html.escape(text).replace("\n", "<br>")
