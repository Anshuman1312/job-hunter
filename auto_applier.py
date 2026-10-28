"""Playwright automation for LinkedIn / Indeed one-click applications.

The automation is intentionally defensive: every unknown state ends in a
``FAILED`` or ``EXTERNAL`` result so the orchestrator can fall back to email.
"""

from __future__ import annotations

import logging
import re
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from config_loader import AppConfig
from models import ApplyResult, Job
from utils import human_delay, safe_filename, safe_str

try:  # Imported lazily by main.py, but keep module import friendly for tests.
    from playwright.sync_api import (
        BrowserContext,
        Locator,
        Page,
        TimeoutError as PlaywrightTimeoutError,
        sync_playwright,
    )
except ImportError:  # pragma: no cover
    BrowserContext = Any  # type: ignore[assignment,misc]
    Locator = Any  # type: ignore[assignment,misc]
    Page = Any  # type: ignore[assignment,misc]
    PlaywrightTimeoutError = TimeoutError  # type: ignore[assignment,misc]
    sync_playwright = None  # type: ignore[assignment]


_SUCCESS_PATTERNS = (
    "application sent",
    "application submitted",
    "your application was sent",
    "your application has been submitted",
    "thanks for applying",
    "thank you for applying",
    "you have applied",
    "you've applied",
    "successfully applied",
)

_LINKEDIN_NEXT_SELECTORS = (
    'footer button[aria-label*="Continue" i]',
    'footer button[aria-label*="Review" i]',
    'button[aria-label*="Continue to next step" i]',
    'button[aria-label*="Review your application" i]',
    "div.jobs-easy-apply-modal footer button.artdeco-button--primary",
    "div.jobs-easy-apply-modal button:has-text('Next')",
    "div.jobs-easy-apply-modal button:has-text('Review')",
)

_LINKEDIN_SUBMIT_SELECTORS = (
    'button[aria-label*="Submit application" i]',
    "div.jobs-easy-apply-modal button:has-text('Submit application')",
)

_INDEED_SUBMIT_SELECTORS = (
    'button:has-text("Submit your application")',
    'button:has-text("Submit application")',
    'button:has-text("Submit")',
)

_INDEED_NEXT_SELECTORS = (
    'button:has-text("Continue")',
    'button:has-text("Next")',
    'button:has-text("Review")',
    'button[type="submit"]',
)


class AutoApplier:
    """Drives a persistent Chromium profile through Easy Apply flows."""

    def __init__(self, config: AppConfig, logger: logging.Logger | None = None) -> None:
        self.app_config = config
        self.config = config.application
        self.logger = logger or logging.getLogger("job_hunter.auto_applier")
        self._playwright: Any = None
        self.context: BrowserContext | None = None
        self.page: Page | None = None

    # ------------------------------------------------------------------ #
    # Lifecycle
    # ------------------------------------------------------------------ #
    def start(self) -> None:
        if self.context is not None:
            return
        if sync_playwright is None:
            raise RuntimeError(
                "Playwright is not installed. Run: pip install -r requirements.txt "
                "&& playwright install chromium"
            )

        profile_dir = self.config.browser_profile_dir
        profile_dir.mkdir(parents=True, exist_ok=True)
        self.config.screenshot_dir.mkdir(parents=True, exist_ok=True)

        self._playwright = sync_playwright().start()
        launch_kwargs: dict[str, Any] = {
            "user_data_dir": str(profile_dir),
            "headless": bool(self.config.headless),
            "locale": self.config.locale,
            "timezone_id": self.config.timezone_id,
            "accept_downloads": True,
            "args": [
                "--disable-blink-features=AutomationControlled",
                "--start-maximized",
            ],
            "viewport": {"width": 1440, "height": 900}
            if self.config.headless
            else None,
        }

        channel = self.config.browser_channel
        try:
            if channel:
                self.context = self._playwright.chromium.launch_persistent_context(
                    channel=channel, **launch_kwargs
                )
            else:
                self.context = self._playwright.chromium.launch_persistent_context(
                    **launch_kwargs
                )
        except Exception as exc:
            if channel:
                self.logger.warning(
                    "Browser channel %r unavailable (%s); falling back to bundled Chromium",
                    channel,
                    exc,
                )
                try:
                    self.context = self._playwright.chromium.launch_persistent_context(
                        **launch_kwargs
                    )
                except Exception:
                    self.stop()
                    raise
            else:
                self.stop()
                raise

        self.context.set_default_timeout(self.config.step_timeout_ms)
        self.context.set_default_navigation_timeout(self.config.navigation_timeout_ms)
        self.page = self.context.pages[0] if self.context.pages else self.context.new_page()
        self.logger.info(
            "Browser started (profile=%s, headless=%s)",
            profile_dir,
            self.config.headless,
        )

    def stop(self) -> None:
        if self.context is not None:
            try:
                self.context.close()
            except Exception as exc:
                self.logger.debug("Error closing browser context: %s", exc)
            finally:
                self.context = None
                self.page = None
        if self._playwright is not None:
            try:
                self._playwright.stop()
            except Exception as exc:
                self.logger.debug("Error stopping Playwright: %s", exc)
            finally:
                self._playwright = None

    def __enter__(self) -> "AutoApplier":
        self.start()
        return self

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        self.stop()

    def open_login_pages(self) -> None:
        """Open LinkedIn and Indeed so the user can sign in once."""

        self.start()
        assert self.context is not None
        for url in (
            "https://www.linkedin.com/login",
            "https://www.indeed.com/",
            "https://www.naukri.com/nlogin/login",
            "https://www.glassdoor.co.in/profile/login_input.htm",
        ):
            try:
                page = self.context.new_page()
                page.goto(url, wait_until="domcontentloaded")
            except Exception as exc:
                self.logger.warning("Could not open %s: %s", url, exc)

    # ------------------------------------------------------------------ #
    # Entry point
    # ------------------------------------------------------------------ #
    def apply(self, job: Job) -> ApplyResult:
        if self.context is None or self.page is None:
            raise RuntimeError("AutoApplier.start() must be called before apply()")

        host = urlsplit(job.job_url).netloc.lower()
        self.logger.info("Trying auto-apply: %s at %s (%s)", job.title, job.company, host)
        try:
            if "linkedin.com" in host or job.site.lower() == "linkedin":
                return self._apply_linkedin(job)
            if "indeed.com" in host or job.site.lower() == "indeed":
                return self._apply_indeed(job)
            return ApplyResult(
                status="EXTERNAL",
                code="unsupported_site",
                message=f"No automated form handler for {host or 'this site'}",
            )
        except PlaywrightTimeoutError as exc:
            return self._fail(job, f"Timed out waiting for the application form: {exc}", "timeout")
        except Exception as exc:
            return self._fail(
                job,
                f"{type(exc).__name__}: {exc}",
                "form_error",
            )

    # ------------------------------------------------------------------ #
    # LinkedIn
    # ------------------------------------------------------------------ #
    def _apply_linkedin(self, job: Job) -> ApplyResult:
        page = self.page
        assert page is not None

        page.goto(job.job_url, wait_until="domcontentloaded")
        self._settle(page)

        if self._is_auth_wall(page, "linkedin"):
            return ApplyResult(
                status="SKIPPED",
                code="login_required",
                message="LinkedIn session is not signed in. Run `python main.py login`.",
            )

        self._dismiss_overlays(page)

        if self._linkedin_already_applied(page):
            return ApplyResult(
                status="SKIPPED",
                code="already_applied",
                message="LinkedIn shows this job as already applied.",
            )

        button = self._find_visible(
            page,
            (
                "button.jobs-apply-button",
                'button[aria-label*="Easy Apply" i]',
                'a[aria-label*="Easy Apply" i]',
                "button:has-text('Easy Apply')",
                ".jobs-apply-button",
                'button[aria-label*="Apply" i]',
                'a[aria-label*="Apply" i]',
            ),
            require_enabled=False,
        )
        if button is None:
            return ApplyResult(
                status="EXTERNAL",
                code="no_easy_apply",
                message="No Easy Apply button was found on the LinkedIn job page.",
            )

        button_text = self._text_of(button)
        if "easy apply" not in button_text and button_text.strip() not in {"apply"}:
            # LinkedIn sometimes leaves the label empty; fall through and test
            # for the modal instead of assuming it is external.
            self.logger.debug("Ambiguous LinkedIn apply button label: %r", button_text)

        pages_before = set(self.context.pages) if self.context else set()
        self._click(button, "LinkedIn apply button")
        self._delay()

        if self.context:
            new_pages = [p for p in self.context.pages if p not in pages_before]
            if new_pages:
                for new_page in new_pages:
                    try:
                        new_page.close()
                    except Exception:
                        pass
                return ApplyResult(
                    status="EXTERNAL",
                    code="external_link",
                    message="LinkedIn opened an external application page.",
                )

        if self._is_auth_wall(page, "linkedin"):
            return ApplyResult(
                status="SKIPPED",
                code="login_required",
                message="LinkedIn asked for a sign-in before applying. Run `python main.py login`.",
            )

        # An in-page login dialog is not an Easy Apply form.
        try:
            password_visible = page.locator("input[type='password']").first.is_visible()
        except Exception:
            password_visible = False
        if password_visible:
            return ApplyResult(
                status="SKIPPED",
                code="login_required",
                message="LinkedIn opened a sign-in dialog. Run `python main.py login`.",
            )

        current_host = urlsplit(page.url).netloc.lower()
        if "linkedin.com" not in current_host:
            return ApplyResult(
                status="EXTERNAL",
                code="external_link",
                message=f"LinkedIn redirected to an external site: {current_host}",
            )

        modal = page.locator("div.jobs-easy-apply-modal, div[role='dialog']").first
        try:
            modal.wait_for(state="visible", timeout=self.config.step_timeout_ms)
        except PlaywrightTimeoutError:
            if self._application_sent(page):
                return ApplyResult(
                    status="APPLIED_AUTO",
                    code="applied",
                    message="Application submitted (confirmation detected).",
                )
            return ApplyResult(
                status="EXTERNAL",
                code="no_modal",
                message="Easy Apply dialog did not open; treating as external.",
            )

        for step in range(1, self.config.max_form_steps + 1):
            self._delay()
            self.logger.debug("LinkedIn Easy Apply step %d", step)
            self._fill_scope(modal)

            submit = self._find_visible(
                modal, _LINKEDIN_SUBMIT_SELECTORS, require_enabled=True
            )
            if submit is not None:
                self._click(submit, "LinkedIn submit")
                self._delay()
                if self._wait_for_success(page, modal):
                    return ApplyResult(
                        status="APPLIED_AUTO",
                        code="applied",
                        message="Application submitted through LinkedIn Easy Apply.",
                        extra={"steps": str(step)},
                    )
                error = self._read_error(modal)
                return self._fail(
                    job,
                    "Submit was clicked but no confirmation appeared"
                    + (f": {error}" if error else "."),
                    "no_confirmation",
                )

            next_button = self._find_visible(
                modal, _LINKEDIN_NEXT_SELECTORS, require_enabled=True
            )
            if next_button is not None:
                self._click(next_button, "LinkedIn next")
                self._delay()
                error = self._read_error(modal)
                if error:
                    return self._fail(
                        job,
                        f"LinkedIn rejected a form step: {error}",
                        "validation_error",
                    )
                continue

            disabled = self._find_visible(
                modal, _LINKEDIN_NEXT_SELECTORS, require_enabled=False
            )
            if disabled is not None and not self._is_enabled(disabled):
                error = self._read_error(modal)
                return self._fail(
                    job,
                    "A required screening question could not be answered"
                    + (f": {error}" if error else "."),
                    "missing_answer",
                )

            if self._application_sent(page):
                return ApplyResult(
                    status="APPLIED_AUTO",
                    code="applied",
                    message="Application submitted (confirmation detected).",
                )

            return self._fail(
                job,
                "Could not find a Next/Review/Submit button in the Easy Apply dialog.",
                "no_navigation",
            )

        return self._fail(
            job,
            f"Application form exceeded {self.config.max_form_steps} steps.",
            "too_many_steps",
        )

    def _linkedin_already_applied(self, page: Page) -> bool:
        try:
            if page.locator("button.jobs-apply-button:has-text('Applied')").count():
                return True
            body = page.locator("body").inner_text(timeout=3000).lower()
            return "already applied" in body and "apply" in body
        except Exception:
            return False

    # ------------------------------------------------------------------ #
    # Indeed
    # ------------------------------------------------------------------ #
    def _apply_indeed(self, job: Job) -> ApplyResult:
        page = self.page
        assert page is not None

        page.goto(job.job_url, wait_until="domcontentloaded")
        self._settle(page)

        if self._is_auth_wall(page, "indeed"):
            return ApplyResult(
                status="SKIPPED",
                code="login_required",
                message="Indeed session is not signed in. Run `python main.py login`.",
            )

        self._dismiss_overlays(page)

        try:
            body = page.locator("body").inner_text(timeout=3000).lower()
        except Exception:
            body = ""
        if "you have already applied" in body or "already applied" in body:
            return ApplyResult(
                status="SKIPPED",
                code="already_applied",
                message="Indeed shows this job as already applied.",
            )

        button = self._find_visible(
            page,
            (
                "#indeedApplyButton",
                'button:has-text("Apply with Indeed")',
                'button:has-text("Apply now")',
                'a:has-text("Apply with Indeed")',
                'button:has-text("Apply on company site")',
                'a:has-text("Apply on company site")',
                'a:has-text("Apply now")',
            ),
            require_enabled=False,
        )
        if button is None:
            return ApplyResult(
                status="EXTERNAL",
                code="no_easy_apply",
                message="No Indeed Apply button was found on the job page.",
            )

        label = self._text_of(button)
        if "company site" in label:
            return ApplyResult(
                status="EXTERNAL",
                code="external_link",
                message="Indeed redirects this application to the company website.",
            )

        pages_before = set(self.context.pages) if self.context else set()
        self._click(button, "Indeed apply button")
        self._delay()

        form_page = page
        if self.context:
            new_pages = [p for p in self.context.pages if p not in pages_before]
            if new_pages:
                form_page = new_pages[-1]
                form_page.wait_for_load_state("domcontentloaded")

        form_host = urlsplit(form_page.url).netloc.lower()
        if form_host and "indeed.com" not in form_host:
            return ApplyResult(
                status="EXTERNAL",
                code="external_link",
                message=f"Indeed redirected to an external site: {form_host}",
            )
        if self._is_auth_wall(form_page, "indeed"):
            return ApplyResult(
                status="SKIPPED",
                code="login_required",
                message="Indeed asked for a sign-in. Run `python main.py login`.",
            )

        for step in range(1, self.config.max_form_steps + 1):
            self._delay()
            self.logger.debug("Indeed Apply step %d", step)
            self._fill_scope(form_page)

            submit = self._find_visible(
                form_page, _INDEED_SUBMIT_SELECTORS, require_enabled=True
            )
            if submit is not None:
                self._click(submit, "Indeed submit")
                self._delay()
                if self._wait_for_success(form_page, None):
                    return ApplyResult(
                        status="APPLIED_AUTO",
                        code="applied",
                        message="Application submitted through Indeed Apply.",
                        extra={"steps": str(step)},
                    )
                error = self._read_error(form_page)
                if error:
                    return self._fail(
                        job,
                        f"Indeed rejected the submission: {error}",
                        "validation_error",
                    )
                # Some flows show another review screen after Submit.
                if self._find_visible(
                    form_page, _INDEED_NEXT_SELECTORS, require_enabled=True
                ):
                    continue
                return self._fail(
                    job, "Indeed submission did not show a confirmation.", "no_confirmation"
                )

            next_button = self._find_visible(
                form_page, _INDEED_NEXT_SELECTORS, require_enabled=True
            )
            if next_button is not None:
                self._click(next_button, "Indeed continue")
                self._delay()
                error = self._read_error(form_page)
                if error:
                    return self._fail(
                        job,
                        f"Indeed rejected a form step: {error}",
                        "validation_error",
                    )
                if self._application_sent(form_page):
                    return ApplyResult(
                        status="APPLIED_AUTO",
                        code="applied",
                        message="Application submitted (confirmation detected).",
                    )
                continue

            if self._application_sent(form_page):
                return ApplyResult(
                    status="APPLIED_AUTO",
                    code="applied",
                    message="Application submitted (confirmation detected).",
                )

            return self._fail(
                job,
                "Could not find a Continue/Submit button on the Indeed application form.",
                "no_navigation",
            )

        return self._fail(
            job,
            f"Indeed application exceeded {self.config.max_form_steps} steps.",
            "too_many_steps",
        )

    # ------------------------------------------------------------------ #
    # Form filling
    # ------------------------------------------------------------------ #
    def _fill_scope(self, scope: Page | Locator) -> None:
        self._upload_resume(scope)
        self._fill_text_fields(scope)
        self._fill_selects(scope)
        self._fill_radios(scope)

    def _upload_resume(self, scope: Page | Locator) -> None:
        resume = self.app_config.application.resume_path
        if not resume.is_file():
            self.logger.warning("Resume not found at %s; skipping upload", resume)
            return

        inputs = scope.locator("input[type='file']")
        try:
            count = inputs.count()
        except Exception:
            return
        if count == 0:
            return

        chosen: Locator | None = None
        for index in range(min(count, 6)):
            candidate = inputs.nth(index)
            hint = self._file_input_hint(candidate)
            if any(word in hint for word in ("resume", "cv", "curriculum")):
                chosen = candidate
                break
        if chosen is None:
            chosen = inputs.first

        try:
            chosen.set_input_files(str(resume))
            self.logger.info("Uploaded resume: %s", resume.name)
            self._delay()
        except Exception as exc:
            self.logger.warning("Resume upload failed: %s", exc)

    def _file_input_hint(self, locator: Locator) -> str:
        parts: list[str] = []
        for attribute in ("name", "id", "accept", "aria-label"):
            value = safe_str(locator.get_attribute(attribute))
            if value:
                parts.append(value)
        try:
            label = locator.evaluate(
                "el => Array.from(el.labels || []).map(l => l.innerText || '').join(' ')"
            )
            if label:
                parts.append(label)
        except Exception:
            pass
        return " ".join(parts).lower()

    def _fill_text_fields(self, scope: Page | Locator) -> None:
        selector = (
            "input:not([type='file']):not([type='hidden']):not([type='radio'])"
            ":not([type='checkbox']):not([type='submit']):not([type='button'])"
            ":not([type='reset']):not([type='image']), textarea"
        )
        fields = scope.locator(selector)
        try:
            count = fields.count()
        except Exception:
            return

        for index in range(min(count, 40)):
            field = fields.nth(index)
            try:
                if not field.is_visible():
                    continue
                input_type = (field.get_attribute("type") or "text").lower()
                if input_type == "password":
                    continue
                label = self._label_for(field)
                if not label:
                    continue
                answer = self._answer_for(label, input_type)
                if answer is None or str(answer).strip() == "":
                    continue
                existing = field.input_value()
                if existing and existing.strip():
                    continue
                field.fill(str(answer))
                self.logger.debug("Filled field %r with %r", label[:60], answer)
                self._delay()
            except Exception as exc:
                self.logger.debug("Could not fill one field: %s", exc)

    def _fill_selects(self, scope: Page | Locator) -> None:
        selects = scope.locator("select")
        try:
            count = selects.count()
        except Exception:
            return

        for index in range(min(count, 20)):
            select = selects.nth(index)
            try:
                if not select.is_visible():
                    continue
                label = self._label_for(select)
                answer = self._answer_for(label, "select")
                if answer is None or str(answer).strip() == "":
                    if self.config.unknown_radio_policy == "skip":
                        continue
                    answer = None

                options = select.locator("option")
                option_count = options.count()
                chosen_index: int | None = None
                first_real_index: int | None = None
                for option_index in range(option_count):
                    option = options.nth(option_index)
                    text = safe_str(option.inner_text())
                    value = safe_str(option.get_attribute("value"))
                    if not text or text.lower().startswith("select"):
                        continue
                    if first_real_index is None:
                        first_real_index = option_index
                    if answer is not None and self._option_matches(text, answer):
                        chosen_index = option_index
                        break

                if chosen_index is None and answer is None:
                    chosen_index = first_real_index
                if chosen_index is None:
                    continue

                option = options.nth(chosen_index)
                value = option.get_attribute("value")
                if value not in (None, ""):
                    select.select_option(value=value)
                else:
                    select.select_option(index=chosen_index)
                self.logger.debug("Selected %r for %r", safe_str(option.inner_text()), label[:60])
                self._delay()
            except Exception as exc:
                self.logger.debug("Could not fill one select: %s", exc)

    def _fill_radios(self, scope: Page | Locator) -> None:
        radios = scope.locator("input[type='radio']")
        try:
            count = radios.count()
        except Exception:
            return
        if count == 0:
            return

        groups: dict[str, list[Locator]] = {}
        for index in range(min(count, 60)):
            radio = radios.nth(index)
            name = safe_str(radio.get_attribute("name")) or f"__group_{index}"
            groups.setdefault(name, []).append(radio)

        for name, group in groups.items():
            try:
                if any(self._is_checked(radio) for radio in group):
                    continue
                question = self._radio_question(group[0])
                answer = self._answer_for(question, "radio") if question else None

                target: Locator | None = None
                if answer is not None and str(answer).strip():
                    for radio in group:
                        label = self._radio_label(radio)
                        if label and self._option_matches(label, answer):
                            target = radio
                            break

                if target is None:
                    policy = self.config.unknown_radio_policy
                    if policy == "skip":
                        self.logger.warning("Unanswered radio group: %s", question[:80] or name)
                        continue
                    enabled = [radio for radio in group if self._is_enabled(radio)]
                    if not enabled:
                        continue
                    target = enabled[0]
                    if policy == "yes":
                        for radio in enabled:
                            if "yes" in self._radio_label(radio).lower():
                                target = radio
                                break

                self._select_radio(target)
                self.logger.debug("Selected radio for %r", question[:80] or name)
                self._delay()
            except Exception as exc:
                self.logger.debug("Could not fill radio group %r: %s", name, exc)

    def _select_radio(self, radio: Locator) -> None:
        try:
            radio.check(force=True, timeout=4000)
            return
        except Exception:
            pass
        try:
            label = radio.locator("xpath=ancestor::label[1]")
            if label.count():
                label.first.click(timeout=4000)
                return
        except Exception:
            pass
        try:
            radio.evaluate("el => el.click()")
        except Exception:
            pass

    def _radio_question(self, radio: Locator) -> str:
        try:
            fieldset = radio.locator("xpath=ancestor::fieldset[1]")
            if fieldset.count():
                legend = fieldset.first.locator("legend")
                if legend.count():
                    text = safe_str(legend.first.inner_text())
                    if text:
                        return text
        except Exception:
            pass
        return self._label_for(radio)

    def _radio_label(self, radio: Locator) -> str:
        try:
            text = radio.evaluate(
                "el => el.labels && el.labels.length ? (el.labels[0].innerText || '') "
                ": (el.closest('label') ? el.closest('label').innerText : '')"
            )
            if safe_str(text):
                return safe_str(text)
        except Exception:
            pass
        return self._label_for(radio)

    def _label_for(self, field: Locator) -> str:
        parts: list[str] = []
        for attribute in ("aria-label", "placeholder"):
            value = safe_str(field.get_attribute(attribute))
            if value:
                parts.append(value)
        try:
            labels = field.evaluate(
                "el => Array.from(el.labels || []).map(l => l.innerText || l.textContent || '').join(' ')"
            )
            if labels:
                parts.append(safe_str(labels))
        except Exception:
            pass
        if not parts:
            for attribute in ("name", "id"):
                value = safe_str(field.get_attribute(attribute))
                if value:
                    parts.append(value)
        try:
            container = field.locator(
                "xpath=ancestor::*[self::div or self::fieldset][1]"
            )
            if container.count():
                text = safe_str(container.first.inner_text(timeout=700))
                if text:
                    parts.append(text[:400])
        except Exception:
            pass
        return " ".join(parts).lower().strip()

    # ------------------------------------------------------------------ #
    # Screening answers
    # ------------------------------------------------------------------ #
    def _answer_for(self, label: str, input_type: str = "text") -> str | None:
        """Map a form question to a configured screening answer."""

        question = re.sub(r"\s+", " ", label.lower())
        answers = self.config.screening
        candidate = self.app_config.candidate

        def value(key: str) -> str | None:
            raw = answers.get(key)
            if raw is None or str(raw).strip() == "":
                return None
            return str(raw).strip()

        # Identity / contact
        if "email" in question:
            return value("email") or candidate.email
        if "phone" in question or "mobile" in question:
            return value("phone") or candidate.phone
        if "first name" in question:
            return candidate.full_name.split()[0] if candidate.full_name else None
        if "last name" in question or "surname" in question:
            return candidate.full_name.split()[-1] if candidate.full_name else None
        if (
            "full name" in question
            or (
                "name" in question
                and not any(word in question for word in ("company", "employer", "user", "file"))
            )
        ):
            return candidate.full_name or value("full_name")
        if "linkedin" in question:
            return value("linkedin_url") or candidate.linkedin
        if "github" in question or "portfolio" in question or "website" in question:
            return value("github") or value("portfolio") or candidate.github or candidate.portfolio
        if (
            "city" in question
            or "current location" in question
            or ("location" in question and "company" not in question)
        ):
            return value("city") or candidate.city
        if "state" in question or "province" in question:
            return value("state") or candidate.state
        if "country" in question and "code" not in question:
            return value("country") or candidate.country
        if "postal" in question or "zip" in question or "pin code" in question:
            return value("postal_code")

        # Employment / compensation
        if "current company" in question or "employer" in question:
            return value("current_company")
        if "current" in question and any(
            word in question for word in ("ctc", "salary", "compensation", "pay")
        ):
            return value("current_ctc")
        if any(word in question for word in ("expected", "desired")) and any(
            word in question for word in ("ctc", "salary", "compensation", "pay")
        ):
            return value("expected_ctc")
        if "notice" in question:
            return value("notice_period_days")
        if "sponsor" in question:
            return value("require_sponsorship")
        if any(
            phrase in question
            for phrase in (
                "authorized to work",
                "authorised to work",
                "legally",
                "work permit",
                "eligible to work",
                "right to work",
            )
        ):
            return value("authorized_to_work")
        if "relocat" in question:
            return value("willing_to_relocate")
        if "start" in question and any(
            word in question for word in ("when", "date", "availability")
        ):
            notice = value("notice_period_days")
            return f"{notice} days" if notice else value("start_date")

        # Years of experience
        if "year" in question or "experience" in question:
            if input_type == "radio" and not any(
                word in question for word in ("how many", "number", "years", "yrs")
            ):
                # "Do you have experience with Django?" is a yes/no question,
                # not a numeric years field.
                return "Yes"
            if "python" in question:
                return value("years_python")
            if "django" in question:
                return value("years_django")
            if "fastapi" in question or "fast api" in question:
                return value("years_fastapi")
            if "aws" in question or "amazon web services" in question:
                return value("years_aws")
            if any(
                phrase in question
                for phrase in ("total", "overall", "professional", "software", "backend", "back end")
            ):
                return value("total_years")
            if input_type == "number":
                return value("total_years")
            return value("total_years")

        return None

    @staticmethod
    def _option_matches(option_text: str, answer: str) -> bool:
        option = re.sub(r"\s+", " ", option_text.lower()).strip()
        answer_text = re.sub(r"\s+", " ", str(answer).lower()).strip()
        if not option or not answer_text:
            return False
        if answer_text in option or option in answer_text:
            return True

        if answer_text in {"yes", "no"}:
            return option.startswith(answer_text) or option == answer_text

        try:
            number = float(answer_text)
        except ValueError:
            number = None
        if number is not None:
            numbers = [float(item) for item in re.findall(r"\d+(?:\.\d+)?", option)]
            if len(numbers) >= 2 and "-" in option:
                low, high = numbers[0], numbers[1]
                if low <= number <= high:
                    return True
            if numbers and any(abs(value - number) < 0.01 for value in numbers):
                return True
        return False

    # ------------------------------------------------------------------ #
    # Browser helpers
    # ------------------------------------------------------------------ #
    def _settle(self, page: Page) -> None:
        self._delay()
        try:
            page.wait_for_load_state("domcontentloaded", timeout=8000)
        except Exception:
            pass

    def _delay(self) -> None:
        human_delay(
            self.config.min_delay_seconds,
            self.config.max_delay_seconds,
            self.logger,
        )

    def _dismiss_overlays(self, page: Page) -> None:
        for selector in (
            'button[aria-label="Dismiss"]',
            "button.artdeco-modal__dismiss",
            'button[aria-label="Close"]',
        ):
            try:
                locator = page.locator(selector).first
                if locator.count() and locator.is_visible():
                    locator.click(timeout=2000)
                    self._delay()
            except Exception:
                continue

    def _find_visible(
        self,
        scope: Page | Locator,
        selectors: tuple[str, ...],
        require_enabled: bool = True,
    ) -> Locator | None:
        for selector in selectors:
            try:
                locator = scope.locator(selector)
                count = locator.count()
            except Exception:
                continue
            for index in range(min(count, 8)):
                candidate = locator.nth(index)
                try:
                    if not candidate.is_visible():
                        continue
                    if require_enabled and not candidate.is_enabled():
                        continue
                    return candidate
                except Exception:
                    continue
        return None

    def _is_enabled(self, locator: Locator) -> bool:
        try:
            return bool(locator.is_enabled())
        except Exception:
            return False

    def _is_checked(self, locator: Locator) -> bool:
        try:
            return bool(locator.is_checked())
        except Exception:
            return False

    def _text_of(self, locator: Locator) -> str:
        parts: list[str] = []
        try:
            parts.append(locator.inner_text(timeout=1500) or "")
        except Exception:
            pass
        try:
            parts.append(locator.get_attribute("aria-label") or "")
        except Exception:
            pass
        return " ".join(parts).strip().lower()

    def _click(self, locator: Locator, label: str = "element") -> None:
        try:
            locator.scroll_into_view_if_needed(timeout=3000)
        except Exception:
            pass
        try:
            locator.click(timeout=8000)
            return
        except Exception as exc:
            self.logger.debug("Normal click failed for %s (%s); forcing", label, exc)
        locator.click(force=True, timeout=5000)

    def _read_error(self, scope: Page | Locator) -> str:
        selectors = (
            ".artdeco-inline-feedback--error",
            ".fb-dash-form-element__error",
            "[role='alert']",
            "[data-testid='inline-error']",
        )
        for selector in selectors:
            try:
                locator = scope.locator(selector)
                count = locator.count()
            except Exception:
                continue
            for index in range(min(count, 5)):
                item = locator.nth(index)
                try:
                    if not item.is_visible():
                        continue
                    text = safe_str(item.inner_text(timeout=1000))
                    if text:
                        return text[:300]
                except Exception:
                    continue
        return ""

    def _application_sent(self, page: Page) -> bool:
        try:
            body = page.locator("body").inner_text(timeout=3000).lower()
        except Exception:
            return False
        return any(pattern in body for pattern in _SUCCESS_PATTERNS)

    def _wait_for_success(self, page: Page, modal: Locator | None) -> bool:
        if modal is not None:
            try:
                modal.wait_for(state="hidden", timeout=8000)
                return True
            except Exception:
                pass
        for _ in range(4):
            if self._application_sent(page):
                return True
            page.wait_for_timeout(1000)
        return False

    def _is_auth_wall(self, page: Page, site: str) -> bool:
        url = page.url.lower()
        if any(
            marker in url
            for marker in ("/login", "/authwall", "/checkpoint", "/signin", "secure.indeed.com")
        ):
            return True
        try:
            body = page.locator("body").inner_text(timeout=2000).lower()
        except Exception:
            return False
        if site == "linkedin":
            return "sign in" in body and "email or phone" in body
        if site == "indeed":
            return "sign in" in body and "email address" in body
        return False

    def _fail(
        self,
        job: Job,
        message: str,
        code: str,
    ) -> ApplyResult:
        screenshot = self._screenshot(job)
        self.logger.warning("Auto-apply failed for %r: %s", job.id, message)
        return ApplyResult(
            status="FAILED",
            code=code,
            message=message,
            screenshot=screenshot,
        )

    def _screenshot(self, job: Job) -> str | None:
        if self.page is None:
            return None
        try:
            self.config.screenshot_dir.mkdir(parents=True, exist_ok=True)
            stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            name = safe_filename(job.id)
            path = self.config.screenshot_dir / f"{name}_{stamp}.png"
            self.page.screenshot(path=str(path), full_page=False)
            return str(path)
        except Exception:
            return None
