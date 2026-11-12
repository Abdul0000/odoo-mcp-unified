from __future__ import annotations

import asyncio
import base64
import json
import os
import time
from pathlib import Path
from typing import Any
from urllib.parse import quote, urljoin, urlparse
from urllib.request import Request, urlopen

from ..config import settings
from ..schemas import BrowserCheckResult, PosFlowCheckRequest, PosFlowCheckResult, PosFlowStepResult


class CdpBrowserService:
    """Lightweight browser diagnostics through Chrome DevTools Protocol.

    The connector no longer bundles Chromium. Set ODOO_AGENT_CDP_URL to an
    external browser's remote-debugging endpoint, for example http://host:9222.
    """

    async def screenshot_odoo_page(
        self,
        base_url: str | None,
        login: str | None,
        password: str | None,
        start_path: str,
        screenshot_name: str,
        timeout_seconds: int = 45,
        full_page: bool = True,
    ) -> BrowserCheckResult:
        return await asyncio.to_thread(
            self._check_page_sync,
            base_url,
            login,
            password,
            start_path,
            screenshot_name,
            timeout_seconds,
            full_page,
        )

    async def run_pos_flow(self, request: PosFlowCheckRequest) -> PosFlowCheckResult:
        return await asyncio.to_thread(self._run_pos_flow_sync, request)

    def _check_page_sync(
        self,
        base_url: str | None,
        login: str | None,
        password: str | None,
        start_path: str,
        screenshot_name: str,
        timeout_seconds: int,
        full_page: bool,
    ) -> BrowserCheckResult:
        url_base = (base_url or os.environ.get("ODOO_BASE_URL") or settings.odoo_base_url or "").rstrip("/")
        user = login or os.environ.get("ODOO_LOGIN") or settings.odoo_login
        secret = password or os.environ.get("ODOO_PASSWORD") or settings.odoo_password
        if not url_base:
            return BrowserCheckResult(status="failed", error="base_url or ODOO_BASE_URL is required")

        page = _CdpPage.new(timeout_seconds)
        if page.error:
            return BrowserCheckResult(status="failed", error=page.error)
        try:
            page.enable()
            self._login(page, url_base, user, secret, timeout_seconds)
            target_url = urljoin(url_base + "/", start_path.lstrip("/"))
            page.navigate(target_url, timeout_seconds)
            page.drain(1.0)
            markers = self._detect_odoo_breakage(page)
            page.console_errors.extend(markers)
            screenshot_path = self._capture_screenshot(page, screenshot_name, full_page) if screenshot_name else None
            title = page.eval_value("document.title")
            return BrowserCheckResult(
                status="failed" if markers else "ok",
                url=page.eval_value("location.href"),
                title=str(title) if title is not None else None,
                screenshot_path=screenshot_path,
                console_errors=page.console_errors[-50:],
                console_all=page.console_all[-500:],
                network_errors=page.network_errors[-50:],
                error="; ".join(markers) if markers else None,
            )
        except Exception as exc:  # noqa: BLE001
            return BrowserCheckResult(
                status="failed",
                url=page.best_effort_url(),
                title=page.best_effort_title(),
                console_errors=page.console_errors[-50:],
                console_all=page.console_all[-500:],
                network_errors=page.network_errors[-50:],
                error=str(exc),
            )
        finally:
            page.close()

    def _run_pos_flow_sync(self, request: PosFlowCheckRequest) -> PosFlowCheckResult:
        url_base = (request.base_url or os.environ.get("ODOO_BASE_URL") or settings.odoo_base_url or "").rstrip("/")
        user = request.login or os.environ.get("ODOO_LOGIN") or settings.odoo_login
        secret = request.password or os.environ.get("ODOO_PASSWORD") or settings.odoo_password
        if not url_base:
            return PosFlowCheckResult(status="failed", error="base_url or ODOO_BASE_URL is required")

        page = _CdpPage.new(request.timeout_seconds)
        if page.error:
            return PosFlowCheckResult(status="failed", error=page.error)

        steps: list[PosFlowStepResult] = []
        all_console: list[str] = []
        all_console_all: list[str] = []
        all_network: list[str] = []
        overall_failed = False

        def record_step(name: str, status: str, error: str | None = None) -> None:
            nonlocal overall_failed
            console_delta, console_all_delta, network_delta = page.snapshot_and_clear()
            all_console.extend(console_delta)
            all_console_all.extend(console_all_delta)
            all_network.extend(network_delta)
            if status == "failed":
                overall_failed = True
            steps.append(
                PosFlowStepResult(
                    step=name,
                    status=status,  # type: ignore[arg-type]
                    console_errors=console_delta[-30:],
                    console_all=console_all_delta[-200:],
                    network_errors=network_delta[-30:],
                    error=error,
                )
            )

        try:
            page.enable()
            try:
                self._login(page, url_base, user, secret, request.timeout_seconds)
                record_step("login", "ok")
            except Exception as exc:  # noqa: BLE001
                record_step("login", "failed", str(exc))
                return PosFlowCheckResult(status="failed", steps=steps, console_errors=all_console, console_all=all_console_all[-1000:], network_errors=all_network, error=f"Login failed: {exc}")

            try:
                page.navigate(urljoin(url_base + "/", "odoo/point-of-sale"), request.timeout_seconds)
                if request.pos_config_name:
                    page.click_text(request.pos_config_name)
                    page.drain(0.5)
                record_step("open_pos_backend", "ok")
            except Exception as exc:  # noqa: BLE001
                record_step("open_pos_backend", "failed", str(exc))

            try:
                opened = page.click_first_text(["New Session", "Resume", "Continue Selling", "Open Register"])
                page.wait_for_selector(".product-card, .o_pos_product, article.product, .products-widget, .product-list-container", request.timeout_seconds)
                current_url = page.best_effort_url() or ""
                record_step("open_pos_session", "ok" if opened or "pos/ui" in current_url or "pos/web" in current_url else "skipped")
            except Exception as exc:  # noqa: BLE001
                record_step("open_pos_session", "failed", str(exc))

            try:
                clicked = page.click_text(request.product_name) if request.product_name else page.click_selector(".product-card, .o_pos_product, article.product")
                if clicked:
                    record_step("click_product", "ok")
                else:
                    record_step("click_product", "failed", "No product tile found on screen — check product_name or POS catalog visibility.")
            except Exception as exc:  # noqa: BLE001
                record_step("click_product", "failed", str(exc))

            try:
                clicked = page.click_first_text(["Payment"]) or page.click_selector(".o_pos_payment_button, button.pay-order-button")
                if clicked:
                    page.drain(0.5)
                    record_step("open_payment_screen", "ok")
                else:
                    record_step("open_payment_screen", "failed", "No Payment button found — order may not have a line, or button label differs in this version.")
            except Exception as exc:  # noqa: BLE001
                record_step("open_payment_screen", "failed", str(exc))

            try:
                if request.payment_method_name:
                    page.click_text(request.payment_method_name)
                else:
                    page.click_selector(".paymentmethod, .payment-method-button")
                validate_clicked = page.click_first_text(["Validate"])
                page.drain(0.5)
                record_step("validate_order", "ok" if validate_clicked else "skipped")
            except Exception as exc:  # noqa: BLE001
                record_step("validate_order", "failed", str(exc))

            markers = self._detect_odoo_breakage(page)
            if markers:
                overall_failed = True
                all_console.extend(markers)
                all_console_all.extend(markers)

            return PosFlowCheckResult(
                status="failed" if overall_failed else "ok",
                steps=steps,
                console_errors=sorted(set(all_console), key=all_console.index)[-100:],
                console_all=all_console_all[-1000:],
                network_errors=sorted(set(all_network), key=all_network.index)[-100:],
            )
        except Exception as exc:  # noqa: BLE001
            return PosFlowCheckResult(status="failed", steps=steps, console_errors=all_console, console_all=all_console_all[-1000:], network_errors=all_network, error=str(exc))
        finally:
            page.close()

    def _login(self, page: "_CdpPage", url_base: str, user: str | None, secret: str | None, timeout_seconds: int) -> None:
        page.navigate(urljoin(url_base + "/", "web/login"), timeout_seconds)
        if not user or not secret:
            return
        page.evaluate(
            """
            (async () => {
              const setValue = (selector, value) => {
                const el = document.querySelector(selector);
                if (!el) return false;
                el.focus();
                el.value = value;
                el.dispatchEvent(new Event('input', {bubbles: true}));
                el.dispatchEvent(new Event('change', {bubbles: true}));
                return true;
              };
              setValue('input[name="login"]', %s);
              setValue('input[name="password"]', %s);
              const button = document.querySelector('button[type="submit"], input[type="submit"]');
              if (button) button.click();
              return !!button;
            })()
            """
            % (json.dumps(user), json.dumps(secret)),
            await_promise=True,
        )
        page.wait_for_condition("!location.pathname.endsWith('/web/login') || document.querySelector('.o_web_client')", min(10, timeout_seconds))

    def _capture_screenshot(self, page: "_CdpPage", screenshot_name: str, full_page: bool) -> str | None:
        safe_name = "".join(ch if ch.isalnum() or ch in "._-" else "_" for ch in (screenshot_name or "odoo_page.png"))
        if not safe_name.lower().endswith((".png", ".jpg", ".jpeg")):
            safe_name += ".png"
        output_dir = Path(settings.screenshot_dir).expanduser().resolve()
        output_dir.mkdir(parents=True, exist_ok=True)
        output_path = output_dir / safe_name
        params: dict[str, Any] = {"format": "png", "captureBeyondViewport": bool(full_page)}
        result = page.call("Page.captureScreenshot", params, timeout=5)
        data = result.get("data")
        if not data:
            return None
        output_path.write_bytes(base64.b64decode(data))
        return str(output_path)

    def _detect_odoo_breakage(self, page: "_CdpPage") -> list[str]:
        markers = page.evaluate(
            """
            (() => {
              const markers = [];
              for (const selector of ['.o_error_dialog', '.o_dialog_error', '.o_technical_modal', '.o_notification_manager .border-danger', '.o_notification_manager .text-danger']) {
                if (document.querySelector(selector)) markers.push(`odoo_ui_error_selector:${selector}`);
              }
              const body = (document.body && document.body.innerText || '').slice(0, 5000);
              for (const token of ['Odoo Client Error', 'shell_ERROR', 'UncaughtPromiseError', 'Traceback', 'Internal Server Error']) {
                if (body.includes(token)) markers.push(`odoo_ui_error_text:${token}`);
              }
              return markers;
            })()
            """
        )
        value = markers.get("result", {}).get("value", [])
        return [str(item) for item in value] if isinstance(value, list) else []


class _CdpPage:
    def __init__(self, ws_url: str | None = None, tab_id: str | None = None, timeout_seconds: int = 45, error: str | None = None) -> None:
        self.ws_url = ws_url
        self.tab_id = tab_id
        self.timeout_seconds = timeout_seconds
        self.error = error
        self._ws = None
        self._next_id = 0
        self.console_errors: list[str] = []
        self.console_all: list[str] = []
        self.network_errors: list[str] = []
        self._step_console_errors: list[str] = []
        self._step_console_all: list[str] = []
        self._step_network_errors: list[str] = []

    @classmethod
    def new(cls, timeout_seconds: int) -> "_CdpPage":
        cdp_url = (os.environ.get("ODOO_AGENT_CDP_URL") or settings.cdp_url or "").rstrip("/")
        if not cdp_url:
            return cls(error="CDP browser is not configured. Set ODOO_AGENT_CDP_URL to a Chrome remote debugging endpoint.")
        try:
            payload = _cdp_json(cdp_url, "/json/new?about:blank", timeout_seconds)
            ws_url = payload.get("webSocketDebuggerUrl")
            tab_id = payload.get("id")
            if not ws_url:
                return cls(error="CDP browser did not return webSocketDebuggerUrl from /json/new")
            page = cls(ws_url=ws_url, tab_id=tab_id, timeout_seconds=timeout_seconds)
            page._connect()
            return page
        except Exception as exc:  # noqa: BLE001
            return cls(error=f"Could not open CDP browser tab: {exc}")

    def _connect(self) -> None:
        import websocket

        self._ws = websocket.create_connection(self.ws_url, timeout=max(1, min(self.timeout_seconds, 30)))

    def enable(self) -> None:
        self.call("Page.enable")
        self.call("Runtime.enable")
        self.call("Log.enable")
        self.call("Network.enable")

    def call(self, method: str, params: dict[str, Any] | None = None, timeout: float | None = None) -> dict[str, Any]:
        if self._ws is None:
            raise RuntimeError("CDP socket is not connected")
        self._next_id += 1
        message_id = self._next_id
        self._ws.send(json.dumps({"id": message_id, "method": method, "params": params or {}}))
        deadline = time.monotonic() + (timeout or self.timeout_seconds)
        while time.monotonic() < deadline:
            raw = self._ws.recv()
            message = json.loads(raw)
            if "method" in message:
                self._handle_event(message)
                continue
            if message.get("id") == message_id:
                if "error" in message:
                    raise RuntimeError(message["error"].get("message", str(message["error"])))
                return message.get("result") or {}
        raise TimeoutError(f"Timed out waiting for CDP response to {method}")

    def navigate(self, url: str, timeout_seconds: int) -> None:
        self.call("Page.navigate", {"url": url}, timeout=timeout_seconds)
        self.wait_for_event("Page.loadEventFired", min(timeout_seconds, 30))
        self.drain(0.5)

    def wait_for_event(self, event_name: str, timeout_seconds: float) -> bool:
        if self._ws is None:
            return False
        deadline = time.monotonic() + timeout_seconds
        while time.monotonic() < deadline:
            try:
                raw = self._ws.recv()
            except Exception:
                return False
            message = json.loads(raw)
            if message.get("method") == event_name:
                return True
            if "method" in message:
                self._handle_event(message)
        return False

    def drain(self, seconds: float) -> None:
        if self._ws is None:
            return
        old_timeout = getattr(self._ws, "timeout", None)
        self._ws.settimeout(0.2)
        deadline = time.monotonic() + seconds
        try:
            while time.monotonic() < deadline:
                try:
                    raw = self._ws.recv()
                except Exception:
                    continue
                message = json.loads(raw)
                if "method" in message:
                    self._handle_event(message)
        finally:
            self._ws.settimeout(old_timeout)

    def evaluate(self, expression: str, await_promise: bool = False) -> dict[str, Any]:
        return self.call(
            "Runtime.evaluate",
            {"expression": expression, "awaitPromise": await_promise, "returnByValue": True},
        )

    def eval_value(self, expression: str) -> Any:
        try:
            return self.evaluate(expression).get("result", {}).get("value")
        except Exception:
            return None

    def wait_for_condition(self, expression: str, timeout_seconds: int) -> bool:
        deadline = time.monotonic() + timeout_seconds
        while time.monotonic() < deadline:
            if self.eval_value(f"Boolean({expression})"):
                return True
            self.drain(0.25)
        return False

    def wait_for_selector(self, selector: str, timeout_seconds: int) -> bool:
        escaped = json.dumps(selector)
        ok = self.wait_for_condition(f"document.querySelector({escaped})", timeout_seconds)
        if not ok:
            raise TimeoutError(f"Selector not found: {selector}")
        return True

    def click_selector(self, selector: str) -> bool:
        return bool(self.eval_value(
            """
            (() => {
              const el = document.querySelector(%s);
              if (!el) return false;
              el.click();
              return true;
            })()
            """
            % json.dumps(selector)
        ))

    def click_text(self, text: str | None) -> bool:
        if not text:
            return False
        return bool(self.eval_value(_click_text_expression([text])))

    def click_first_text(self, labels: list[str]) -> bool:
        return bool(self.eval_value(_click_text_expression(labels)))

    def snapshot_and_clear(self) -> tuple[list[str], list[str], list[str]]:
        console_errors = list(self._step_console_errors)
        console_all = list(self._step_console_all)
        network_errors = list(self._step_network_errors)
        self._step_console_errors.clear()
        self._step_console_all.clear()
        self._step_network_errors.clear()
        return console_errors, console_all, network_errors

    def best_effort_url(self) -> str | None:
        value = self.eval_value("location.href")
        return str(value) if value else None

    def best_effort_title(self) -> str | None:
        value = self.eval_value("document.title")
        return str(value) if value else None

    def close(self) -> None:
        try:
            if self._ws is not None:
                self._ws.close()
        finally:
            cdp_url = (os.environ.get("ODOO_AGENT_CDP_URL") or settings.cdp_url or "").rstrip("/")
            if cdp_url and self.tab_id:
                try:
                    _cdp_json(cdp_url, f"/json/close/{quote(self.tab_id)}", 5)
                except Exception:
                    pass

    def _handle_event(self, message: dict[str, Any]) -> None:
        method = message.get("method")
        params = message.get("params") or {}
        if method == "Runtime.consoleAPICalled":
            level = str(params.get("type") or "log")
            args = params.get("args") or []
            text = " ".join(_remote_object_text(item) for item in args) or level
            self._record_console(f"{level}: {text}", level)
        elif method == "Runtime.exceptionThrown":
            details = params.get("exceptionDetails") or {}
            text = details.get("text") or details.get("exception", {}).get("description") or json.dumps(details, default=str)
            self._record_console(f"pageerror: {text}", "error")
        elif method == "Log.entryAdded":
            entry = params.get("entry") or {}
            level = str(entry.get("level") or "info")
            text = str(entry.get("text") or entry)
            self._record_console(f"log.{level}: {text}", level)
        elif method == "Network.responseReceived":
            response = params.get("response") or {}
            status = int(response.get("status") or 0)
            if status >= 400:
                self._record_network(f"{status} {response.get('url')}")
        elif method == "Network.loadingFailed":
            self._record_network(f"failed {params.get('errorText')} {params.get('requestId')}")

    def _record_console(self, entry: str, level: str) -> None:
        self.console_all.append(entry)
        self._step_console_all.append(entry)
        if level in {"error", "warning", "warn"}:
            self.console_errors.append(entry)
            self._step_console_errors.append(entry)

    def _record_network(self, entry: str) -> None:
        self.network_errors.append(entry)
        self._step_network_errors.append(entry)


def _remote_object_text(item: dict[str, Any]) -> str:
    if "value" in item:
        return str(item["value"])
    if "description" in item:
        return str(item["description"])
    return json.dumps(item, default=str)


def _click_text_expression(labels: list[str]) -> str:
    labels_json = json.dumps([label.lower() for label in labels])
    return f"""
    (() => {{
      const labels = {labels_json};
      const candidates = Array.from(document.querySelectorAll('button, a, [role="button"], .product-card, .o_pos_product, article.product, .paymentmethod, .payment-method-button'));
      const match = candidates.find((el) => {{
        const text = (el.innerText || el.textContent || '').toLowerCase();
        return labels.some((label) => text.includes(label));
      }});
      if (!match) return false;
      match.click();
      return true;
    }})()
    """


def _cdp_json(cdp_url: str, path: str, timeout_seconds: int) -> dict[str, Any]:
    parsed = urlparse(cdp_url)
    if parsed.scheme not in {"http", "https"}:
        raise ValueError("ODOO_AGENT_CDP_URL must be an http(s) Chrome remote debugging endpoint")
    url = cdp_url.rstrip("/") + path
    errors: list[str] = []
    for method in ("PUT", "POST", "GET"):
        try:
            req = Request(url, method=method)
            with urlopen(req, timeout=max(1, min(timeout_seconds, 30))) as response:
                raw = response.read().decode("utf-8")
                return json.loads(raw) if raw.strip().startswith(("{", "[")) else {"result": raw}
        except Exception as exc:  # noqa: BLE001
            errors.append(f"{method}: {exc}")
    raise RuntimeError("; ".join(errors))

