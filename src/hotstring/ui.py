"""NiceGUI management UI.

The UI is intentionally a single page: a dashboard with the HotString table,
first-run onboarding, and settings.  Closing the native window hides it; the
tray icon and keyboard listener keep running.
"""

from __future__ import annotations

import logging
import os
import subprocess
import sys
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from nicegui import run, ui

from . import config, db
from .executor import (
    CommandError,
    CommandType,
    get_powershell_display_name,
    parse_command,
    validate_command,
)
from .models import (
    DuplicateTriggerError,
    HotString,
    HotStringError,
    TriggerValidationError,
    suffix_conflicts,
    validate_trigger,
)

if TYPE_CHECKING:  # pragma: no cover
    from .app import HotStringApp

logger = logging.getLogger(__name__)

CUSTOM_CSS = """
<style>
  body {
    font-family: 'Segoe UI Variable Text', 'Segoe UI', system-ui, -apple-system, sans-serif;
    background: #f8fafc;
  }
  .nicegui-content { padding: 0; }
  .hs-mono {
    font-family: 'Cascadia Code', 'Cascadia Mono', Consolas, 'Courier New', monospace;
  }
  .hs-clamp {
    display: -webkit-box;
    -webkit-line-clamp: 2;
    -webkit-box-orient: vertical;
    overflow: hidden;
  }
  /* Editor dialog: the body scrolls, the header and action bar stay visible. */
  .hs-editor-card {
    max-height: 92vh;
  }
  .hs-editor-card.hs-editor-expanded {
    width: min(1100px, 95vw);
    max-height: 96vh;
  }
  .hs-editor-body {
    overflow-y: auto;
    min-height: 0;
    overscroll-behavior: contain;
  }
  .hs-editor-command textarea {
    max-height: 34vh;
    overflow-y: auto;
  }
</style>
"""

COMMAND_EXAMPLES = [
    ("Type text", "t", ";em", "tmy.name@example.com"),
    ("PowerShell", "s", ";np", "sStart-Process notepad"),
    ("App action", "a", ";hs", "aopen"),
]


def format_timestamp(value: datetime | None) -> str:
    """Format a database timestamp for display in local time."""

    if value is None:
        return "—"
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone().strftime("%Y-%m-%d %H:%M")


class HotStringUI:
    """Build the NiceGUI page(s) for a running application."""

    def __init__(self, app: HotStringApp) -> None:
        self.app = app

    def build(self) -> None:
        """Register the dashboard page.  Call before ``ui.run``."""

        @ui.page("/", title="HotString")
        def dashboard() -> None:
            DashboardPage(self.app).build()


class DashboardPage:
    """One connected dashboard client."""

    def __init__(self, app: HotStringApp) -> None:
        self.app = app
        self.table: ui.table | None = None
        self.onboarding: ui.column | None = None
        self.error_banner: ui.column | None = None
        self.empty_hint: ui.label | None = None
        self.search: ui.input | None = None
        self.count_badge: ui.badge | None = None
        self.status_badge: ui.label | None = None
        self.autostart_switch: ui.switch | None = None
        self.pause_switch: ui.switch | None = None
        self._last_paused: bool | None = None
        self._last_autostart: bool | None = None
        self._syncing = False
        self._ticks = 0

    # -- page construction -------------------------------------------------

    def build(self) -> None:
        ui.add_head_html(CUSTOM_CSS)
        ui.colors(primary="#2563eb", positive="#16a34a", negative="#dc2626", warning="#d97706")
        self._build_header()

        with ui.column().classes("w-full max-w-6xl mx-auto px-4 md:px-6 py-6 gap-6"):
            self.onboarding = ui.column().classes("w-full gap-0")
            self._render_onboarding()
            self.error_banner = ui.column().classes("w-full gap-0")
            self._render_listener_error()
            self._build_hotstrings_card()
            self._build_settings_card()
            self._build_footer()

        ui.timer(0.75, self._sync_external_state)
        ui.timer(1.0, self._drain_errors)
        self._update_status()
        logger.info("Dashboard page ready")

    def _build_header(self) -> None:
        with ui.header().classes(
            "bg-slate-900 text-white px-4 md:px-6 py-3 items-center justify-between shadow-md"
        ):
            with ui.row().classes("items-center gap-3"):
                with ui.element("div").classes(
                    "w-9 h-9 rounded-lg bg-blue-500 flex items-center justify-center shrink-0"
                ):
                    ui.icon("bolt", size="1.5rem").classes("text-white")
                with ui.column().classes("gap-0"):
                    ui.label("HotString").classes("text-xl font-semibold leading-tight")
                    ui.label("Type less. Do more.").classes("text-xs text-slate-400 leading-tight")
            with ui.row().classes("items-center gap-3"):
                self.status_badge = ui.label("Listening").classes(
                    "text-xs font-medium px-2.5 py-1 rounded-full bg-green-500/20 text-green-300"
                )
                ui.button(icon="refresh", on_click=self.refresh_all).props(
                    "flat round dense color=white"
                ).tooltip("Reload from database")

    def _page_title(self, title: str, subtitle: str) -> None:
        with ui.column().classes("gap-1"):
            ui.label(title).classes("text-2xl font-semibold text-slate-900")
            ui.label(subtitle).classes("text-sm text-slate-500")

    # -- onboarding --------------------------------------------------------

    def _render_onboarding(self) -> None:
        if self.onboarding is None:
            return
        self.onboarding.clear()
        if not self.app.needs_onboarding():
            self.onboarding.set_visibility(False)
            return

        self.onboarding.set_visibility(True)
        existing_triggers = {item.trigger for item in db.list_hotstrings()}
        suggested_trigger = self._suggest_open_trigger(existing_triggers)
        has_other_hotstrings = bool(existing_triggers)
        title = "One more step" if has_other_hotstrings else "Welcome to HotString"
        lead = (
            "Create a shortcut that opens this window, so you can get back here from any app."
            if has_other_hotstrings
            else "Turn short triggers into text, scripts and actions — anywhere on Windows."
        )

        with self.onboarding:
            with ui.card().classes("w-full p-0 overflow-hidden shadow-sm border border-blue-100"):
                with ui.column().classes("w-full p-6 md:p-8 bg-blue-600 text-white gap-2"):
                    ui.label(title).classes("text-2xl md:text-3xl font-bold")
                    ui.label(lead).classes("text-blue-50 max-w-3xl")
                with ui.column().classes("w-full p-6 md:p-8 gap-6"):
                    with ui.row().classes("w-full gap-3 flex-wrap"):
                        for label, prefix, trigger, command in COMMAND_EXAMPLES:
                            with ui.card().classes(
                                "flex-1 min-w-[210px] p-4 gap-2 bg-slate-50 border border-slate-200 shadow-none"
                            ):
                                with ui.row().classes("items-center gap-2"):
                                    ui.label(prefix).classes(
                                        "hs-mono w-6 h-6 rounded bg-blue-600 text-white text-center font-bold"
                                    )
                                    ui.label(label).classes("font-semibold text-slate-800")
                                ui.label(f"{trigger} → {command}").classes(
                                    "hs-mono text-xs text-slate-600 bg-white border border-slate-200 rounded px-2 py-1"
                                )

                    ui.label(
                        "A trigger is 2–8 characters with no spaces. After you set it up, type it in any "
                        "application and HotString will erase it and run your command."
                    ).classes("text-sm text-slate-600")

                    with ui.row().classes("w-full gap-3 items-start"):
                        trigger_input = ui.input("Trigger", value=suggested_trigger).props(
                            "outlined dense"
                        ).classes("w-40")
                        description_input = ui.input("Description", value="Open HotString").props(
                            "outlined dense"
                        ).classes("flex-1 min-w-[220px]")
                    command_input = ui.input("Command", value="aopen").props("outlined dense").classes(
                        "w-full"
                    )
                    ui.label(
                        "The pre-filled aopen command is an internal action that shows this window. "
                        "You can add more HotStrings after setup."
                    ).classes("text-xs text-slate-500")
                    start_with_windows = ui.checkbox(
                        "Start HotString when Windows starts", value=True
                    ).classes("text-sm")
                    onboarding_error = ui.label("").classes("text-sm text-red-600")
                    onboarding_error.set_visibility(False)

                    def create_first() -> None:
                        try:
                            validate_trigger((trigger_input.value or "").strip())
                            validate_command(command_input.value or "")
                            hotstring = self.app.create_hotstring(
                                trigger=(trigger_input.value or "").strip(),
                                description=description_input.value or "",
                                command=command_input.value or "",
                                enabled=True,
                            )
                        except (TriggerValidationError, CommandError, DuplicateTriggerError) as error:
                            onboarding_error.set_text(str(error))
                            onboarding_error.set_visibility(True)
                            return
                        except Exception as error:
                            logger.exception("Could not create the first HotString")
                            onboarding_error.set_text(f"Could not save: {error}")
                            onboarding_error.set_visibility(True)
                            return

                        if start_with_windows.value:
                            try:
                                self.app.set_autostart(True)
                            except Exception as error:
                                ui.notify(
                                    f"HotString was created, but start with Windows failed: {error}",
                                    type="warning",
                                )
                        ui.notify(
                            f"Ready! Type {hotstring.trigger} anywhere to open HotString.",
                            type="positive",
                            position="top",
                        )
                        self.refresh_all()

                    with ui.row().classes("w-full items-center justify-between gap-3 pt-1"):
                        ui.label(
                            "You can change all of this later in the table and settings below."
                        ).classes("text-xs text-slate-500")
                        ui.button("Create HotString", icon="check", on_click=create_first).props(
                            "unelevated color=primary"
                        )

    def _render_listener_error(self) -> None:
        """Show a persistent banner when the global hook could not start."""

        if self.error_banner is None:
            return
        self.error_banner.clear()
        message = self.app.listener_error
        if not message:
            self.error_banner.set_visibility(False)
            return
        self.error_banner.set_visibility(True)
        with self.error_banner:
            with ui.card().classes(
                "w-full bg-red-50 border border-red-200 shadow-none p-4 gap-1"
            ):
                with ui.row().classes("items-center gap-2"):
                    ui.icon("error").classes("text-red-600")
                    ui.label("Listening is unavailable").classes(
                        "font-semibold text-red-800"
                    )
                ui.label(message).classes("text-sm text-red-700")
                ui.label(
                    "HotStrings will not expand until the hook is fixed and HotString is restarted. "
                    "Check the log in the data folder for details."
                ).classes("text-xs text-red-600")

    @staticmethod
    def _suggest_open_trigger(existing: set[str]) -> str:
        if ";hs" not in existing:
            return ";hs"
        for index in range(2, 100):
            candidate = f";hs{index}"
            if len(candidate) <= 8 and candidate not in existing:
                return candidate
        return ";hs"

    # -- table -------------------------------------------------------------

    def _build_hotstrings_card(self) -> None:
        with ui.card().classes("w-full p-0 overflow-hidden shadow-sm border border-slate-200"):
            with ui.column().classes("w-full p-5 gap-4"):
                with ui.row().classes("w-full items-start justify-between gap-3 flex-wrap"):
                    with ui.column().classes("gap-1"):
                        with ui.row().classes("items-center gap-2"):
                            ui.label("Your HotStrings").classes("text-xl font-semibold text-slate-900")
                            self.count_badge = ui.badge("0", color="blue-1").classes(
                                "text-blue-700 text-xs"
                            )
                        ui.label(
                            "Type a trigger anywhere. Case matters and the active keyboard layout decides the characters."
                        ).classes("text-sm text-slate-500")
                    with ui.row().classes("items-center gap-2"):
                        self.search = (
                            ui.input(placeholder="Search…")
                            .props("outlined dense clearable")
                            .classes("w-60")
                        )
                        self.search.on_value_change(lambda _event: self._refresh_table())
                        ui.button("Add", icon="add", on_click=self.open_create).props(
                            "unelevated color=primary"
                        )

                self.table = (
                    ui.table(
                        rows=[],
                        columns=[
                            {
                                "name": "trigger",
                                "label": "Trigger",
                                "field": "trigger",
                                "align": "left",
                                "sortable": True,
                                "style": "width: 130px",
                            },
                            {
                                "name": "description",
                                "label": "Description",
                                "field": "description",
                                "align": "left",
                                "sortable": True,
                                "style": "min-width: 180px",
                            },
                            {
                                "name": "command",
                                "label": "Command",
                                "field": "command",
                                "align": "left",
                                "style": "min-width: 260px",
                            },
                            {
                                "name": "enabled",
                                "label": "Enabled",
                                "field": "enabled",
                                "align": "center",
                                "style": "width: 96px",
                            },
                            {
                                "name": "actions",
                                "label": "Actions",
                                "field": "actions",
                                "align": "right",
                                "style": "width: 110px",
                            },
                        ],
                        row_key="id",
                        pagination=0,
                    )
                    .props("flat bordered hide-no-data")
                    .classes("w-full")
                )

                self.table.add_slot(
                    "body-cell-trigger",
                    """
                    <q-td :props="props">
                      <q-chip dense square
                              :color="props.row.enabled ? 'primary' : 'grey-5'"
                              text-color="white"
                              class="hs-mono text-xs">
                        {{ props.row.trigger }}
                      </q-chip>
                    </q-td>
                    """,
                )
                self.table.add_slot(
                    "body-cell-description",
                    """
                    <q-td :props="props">
                      <div class="text-sm text-slate-700">{{ props.row.description || '—' }}</div>
                      <div class="text-[11px] text-slate-400">Updated {{ props.row.updated }}</div>
                    </q-td>
                    """,
                )
                self.table.add_slot(
                    "body-cell-command",
                    """
                    <q-td :props="props">
                      <div class="hs-mono text-xs text-slate-600 whitespace-pre-wrap break-all hs-clamp"
                           style="max-width: 420px">{{ props.row.command }}</div>
                    </q-td>
                    """,
                )
                self.table.add_slot(
                    "body-cell-enabled",
                    """
                    <q-td :props="props" class="text-center">
                      <q-toggle :model-value="props.row.enabled" color="primary" dense
                                @update:model-value="() => $parent.$emit('toggle', props.row)">
                        <q-tooltip>{{ props.row.enabled ? 'Disable' : 'Enable' }} this HotString</q-tooltip>
                      </q-toggle>
                    </q-td>
                    """,
                )
                self.table.add_slot(
                    "body-cell-actions",
                    """
                    <q-td :props="props" class="text-right">
                      <q-btn flat round dense icon="edit" color="primary"
                             @click="() => $parent.$emit('edit', props.row)">
                        <q-tooltip>Edit</q-tooltip>
                      </q-btn>
                      <q-btn flat round dense icon="delete" color="negative"
                             @click="() => $parent.$emit('delete', props.row)">
                        <q-tooltip>Delete</q-tooltip>
                      </q-btn>
                    </q-td>
                    """,
                )
                self.table.on("edit", lambda event: self.open_edit(event.args))
                self.table.on("delete", lambda event: self.confirm_delete(event.args))
                self.table.on("toggle", lambda event: self.toggle_row(event.args))

                self.empty_hint = ui.label(
                    "No HotStrings yet. Add your first one below — for example ;em → tmy.name@example.com."
                ).classes("text-sm text-slate-500 text-center py-2")

                ui.button("Add HotString", icon="add", on_click=self.open_create).props(
                    "outline color=primary"
                ).classes("w-full")

        self._refresh_table()

    def _rows(self) -> list[dict[str, Any]]:
        search_text = (self.search.value or "").strip().lower() if self.search else ""
        rows: list[dict[str, Any]] = []
        for item in db.list_hotstrings():
            haystack = f"{item.trigger}\n{item.description}\n{item.command}".lower()
            if search_text and search_text not in haystack:
                continue
            rows.append(
                {
                    "id": item.id,
                    "trigger": item.trigger,
                    "description": item.description,
                    "command": item.command,
                    "enabled": item.enabled,
                    "updated": format_timestamp(item.updated_at),
                }
            )
        return rows

    def _refresh_table(self) -> None:
        if self.table is None:
            return
        rows = self._rows()
        self.table.rows = rows
        self.table.update()
        if self.empty_hint is not None:
            self.empty_hint.set_visibility(len(rows) == 0)
        if self.count_badge is not None:
            self.count_badge.set_text(str(db.count_hotstrings()))

    # -- row actions -------------------------------------------------------

    def toggle_row(self, row: dict[str, Any]) -> None:
        try:
            hotstring_id = int(row["id"])
            current = bool(row.get("enabled"))
            self.app.set_enabled(hotstring_id, not current)
            state = "enabled" if not current else "disabled"
            ui.notify(f"HotString {row.get('trigger')} {state}.", type="info")
        except Exception as error:
            logger.exception("Could not toggle HotString")
            ui.notify(f"Could not update the HotString: {error}", type="negative")
        self._refresh_table()

    def open_create(self) -> None:
        self._open_editor()

    def open_edit(self, row: dict[str, Any]) -> None:
        hotstring = db.get_hotstring(int(row["id"]))
        if hotstring is None:
            ui.notify("That HotString no longer exists.", type="warning")
            self._refresh_table()
            return
        self._open_editor(hotstring)

    def _open_editor(self, hotstring: HotString | None = None) -> None:
        editing = hotstring is not None
        title = "Edit HotString" if editing else "New HotString"

        with ui.dialog() as dialog, ui.card().classes(
            "hs-editor-card w-[640px] max-w-[95vw] p-0 overflow-hidden shadow-xl flex flex-col"
        ) as card:
            expanded = False

            def toggle_expand() -> None:
                nonlocal expanded
                expanded = not expanded
                card.classes(toggle="hs-editor-expanded")
                expand_button.props(
                    f"icon={'close_fullscreen' if expanded else 'open_in_full'}"
                )

            with ui.row().classes(
                "w-full px-6 py-4 bg-slate-900 text-white items-center gap-2 shrink-0"
            ):
                ui.icon("edit" if editing else "add").classes("text-blue-300")
                ui.label(title).classes("text-lg font-semibold")
                ui.space()
                expand_button = (
                    ui.button(icon="open_in_full", on_click=toggle_expand)
                    .props("flat round dense color=white")
                    .tooltip("Expand or restore the editor")
                )

            # Only this body scrolls.  The header and action bar stay in view,
            # so a long command can never push Save below the window edge.
            with ui.column().classes(
                "hs-editor-body w-full p-6 gap-4 overflow-y-auto min-h-0"
            ):
                with ui.row().classes("w-full gap-3 items-start"):
                    trigger_input = (
                        ui.input("Trigger", value=hotstring.trigger if hotstring else "")
                        .props("outlined dense maxlength=8")
                        .classes("w-40")
                    )
                    description_input = (
                        ui.input(
                            "Description",
                            value=hotstring.description if hotstring else "",
                        )
                        .props("outlined dense")
                        .classes("flex-1 min-w-[220px]")
                    )
                command_input = (
                    ui.textarea(
                        "Command",
                        value=hotstring.command if editing else "t",
                    )
                    .props("outlined autogrow input-class=hs-mono")
                    .classes("w-full hs-mono hs-editor-command")
                )
                command_hint = ui.label("").classes("text-xs text-slate-500")
                suffix_warning = ui.label("").classes("text-xs text-amber-700 bg-amber-50 rounded p-2")
                suffix_warning.set_visibility(False)

                with ui.row().classes("w-full items-center justify-between"):
                    enabled_switch = ui.switch(
                        "Enabled", value=hotstring.enabled if editing else True
                    )
                    ui.label("One optional space or ':' after the prefix is ignored.").classes(
                        "text-xs text-slate-400"
                    )

                test_output = ui.label("").classes(
                    "w-full whitespace-pre-wrap hs-mono text-xs text-slate-700 bg-slate-50 "
                    "border border-slate-200 rounded p-3"
                )
                test_output.set_visibility(False)

                def reveal_test_output() -> None:
                    """Scroll the editor body so the test result stays visible."""

                    ui.run_javascript(
                        f"getHtmlElement({test_output.id})?.scrollIntoView("
                        "{block: 'nearest', behavior: 'smooth'})"
                    )

                def update_hints() -> None:
                    raw_trigger = (trigger_input.value or "").strip()
                    others = [
                        item.trigger
                        for item in db.list_hotstrings()
                        if hotstring is None or item.id != hotstring.id
                    ]
                    try:
                        validate_trigger(raw_trigger)
                    except TriggerValidationError as error:
                        command_hint.set_text(str(error))
                        suffix_warning.set_visibility(False)
                        return
                    conflicts = suffix_conflicts(raw_trigger, others)
                    if conflicts:
                        shown = ", ".join(f"'{item}'" for item in conflicts[:4])
                        suffix_warning.set_text(
                            f"Suffix conflict with {shown}. The shorter trigger fires first, "
                            "so one of these may be unreachable."
                        )
                        suffix_warning.set_visibility(True)
                    else:
                        suffix_warning.set_visibility(False)

                    try:
                        parsed = parse_command(command_input.value or "")
                        command_hint.set_text(f"Detected: {self._command_type_label(parsed.type)}")
                    except CommandError as error:
                        command_hint.set_text(str(error))

                trigger_input.on_value_change(lambda _event: update_hints())
                command_input.on_value_change(lambda _event: update_hints())
                update_hints()

                def show_error(message: str) -> None:
                    error_label.set_text(message)
                    error_label.set_visibility(True)

                def clear_error() -> None:
                    error_label.set_visibility(False)

                async def test_command() -> None:
                    clear_error()
                    try:
                        validate_command(command_input.value or "")
                    except CommandError as error:
                        show_error(str(error))
                        return
                    parsed = validate_command(command_input.value or "")
                    test_output.set_text(
                        "Running PowerShell…" if parsed.type is CommandType.POWERSHELL else "Preparing preview…"
                    )
                    test_output.set_visibility(True)
                    reveal_test_output()
                    try:
                        if parsed.type is CommandType.POWERSHELL:
                            result = await run.io_bound(self.app.test_command, command_input.value or "")
                        else:
                            result = self.app.test_command(command_input.value or "")
                        test_output.set_text(result)
                        reveal_test_output()
                    except Exception as error:
                        logger.exception("Command test failed")
                        test_output.set_text(f"Test failed: {error}")
                        reveal_test_output()

                def save() -> None:
                    clear_error()
                    try:
                        trigger = validate_trigger((trigger_input.value or "").strip())
                        validate_command(command_input.value or "")
                        others = [
                            item.trigger
                            for item in db.list_hotstrings()
                            if hotstring is None or item.id != hotstring.id
                        ]
                        conflicts = suffix_conflicts(trigger, others)
                        if conflicts:
                            logger.info(
                                "Saving %r despite suffix conflicts with %s", trigger, conflicts
                            )
                        if editing and hotstring is not None and hotstring.id is not None:
                            saved = self.app.update_hotstring(
                                hotstring.id,
                                trigger=trigger,
                                description=description_input.value or "",
                                command=command_input.value or "",
                                enabled=bool(enabled_switch.value),
                            )
                            message = f"Updated {saved.trigger}."
                        else:
                            saved = self.app.create_hotstring(
                                trigger=trigger,
                                description=description_input.value or "",
                                command=command_input.value or "",
                                enabled=bool(enabled_switch.value),
                            )
                            message = f"Created {saved.trigger}."
                    except TriggerValidationError as error:
                        show_error(str(error))
                        return
                    except CommandError as error:
                        show_error(str(error))
                        return
                    except DuplicateTriggerError as error:
                        show_error(str(error))
                        return
                    except HotStringError as error:
                        show_error(str(error))
                        return
                    except Exception as error:
                        logger.exception("Could not save HotString")
                        show_error(f"Could not save: {error}")
                        return

                    dialog.close()
                    ui.notify(message, type="positive")
                    self.refresh_all()

            # The action bar lives outside the scrolling body, so Save is
            # always reachable no matter how long the command is.
            with ui.column().classes(
                "w-full px-6 py-4 gap-2 border-t border-slate-200 bg-white shrink-0"
            ):
                error_label = ui.label("").classes("text-sm text-red-600")
                error_label.set_visibility(False)
                with ui.row().classes("w-full items-center justify-between gap-2 flex-wrap"):
                    with ui.row().classes("gap-2"):
                        ui.button("Test", icon="play_arrow", on_click=test_command).props(
                            "outline color=primary"
                        ).tooltip("Preview text commands; run PowerShell commands for real")
                        ui.button("Cancel", on_click=dialog.close).props("flat")
                    ui.button("Save", icon="save", on_click=save).props(
                        "unelevated color=primary"
                    )

        dialog.open()
        if not editing:
            ui.timer(0.05, lambda: trigger_input.run_method("focus"), once=True)

    @staticmethod
    def _command_type_label(command_type: CommandType) -> str:
        return {
            CommandType.TEXT: "type text",
            CommandType.POWERSHELL: "PowerShell script",
            CommandType.ACTION: "internal action",
        }[command_type]

    def confirm_delete(self, row: dict[str, Any]) -> None:
        with ui.dialog() as dialog, ui.card().classes(
            "w-[460px] max-w-[95vw] max-h-[90vh] overflow-y-auto p-6 gap-4"
        ):
            with ui.row().classes("items-center gap-3"):
                ui.icon("delete_forever", size="2rem").classes("text-red-500")
                with ui.column().classes("gap-0"):
                    ui.label("Delete this HotString?").classes("text-lg font-semibold")
                    ui.label(
                        f"{row.get('trigger')} — {row.get('description') or 'no description'}"
                    ).classes("text-sm text-slate-500 break-words")
            ui.label("This cannot be undone.").classes("text-sm text-slate-600")
            with ui.row().classes("w-full justify-end gap-2"):
                ui.button("Cancel", on_click=dialog.close).props("flat")
                ui.button(
                    "Delete",
                    icon="delete",
                    on_click=lambda: self._delete_row(row, dialog),
                ).props("unelevated color=negative")
        dialog.open()

    def _delete_row(self, row: dict[str, Any], dialog: ui.dialog) -> None:
        try:
            deleted = self.app.delete_hotstring(int(row["id"]))
            if deleted:
                ui.notify(f"Deleted {row.get('trigger')}.", type="info")
        except Exception as error:
            logger.exception("Could not delete HotString")
            ui.notify(f"Could not delete: {error}", type="negative")
        dialog.close()
        self.refresh_all()

    # -- settings ----------------------------------------------------------

    def _build_settings_card(self) -> None:
        with ui.card().classes("w-full p-0 overflow-hidden shadow-sm border border-slate-200"):
            with ui.row().classes("w-full px-5 py-3 bg-slate-50 border-b border-slate-200 items-center gap-2"):
                ui.icon("settings").classes("text-slate-500")
                ui.label("Settings").classes("font-semibold text-slate-800")
            with ui.column().classes("w-full p-5 gap-5"):
                with ui.row().classes("w-full items-center justify-between gap-4"):
                    with ui.column().classes("gap-0 flex-1"):
                        ui.label("Start with Windows").classes("font-medium text-slate-800")
                        ui.label(
                            "Launch HotString silently at login using the current user's Run key."
                        ).classes("text-sm text-slate-500")
                    self.autostart_switch = ui.switch(
                        value=self.app.autostart_enabled(),
                        on_change=self._on_autostart_change,
                    )
                ui.separator()
                with ui.row().classes("w-full items-center justify-between gap-4"):
                    with ui.column().classes("gap-0 flex-1"):
                        ui.label("Pause listening").classes("font-medium text-slate-800")
                        ui.label(
                            "Keep HotString in the tray without expanding any triggers."
                        ).classes("text-sm text-slate-500")
                    self.pause_switch = ui.switch(
                        value=self.app.is_paused(),
                        on_change=self._on_pause_change,
                    )
                ui.separator()
                with ui.row().classes("w-full items-center justify-between gap-4"):
                    with ui.column().classes("gap-0 flex-1"):
                        ui.label("PowerShell for scripts").classes("font-medium text-slate-800")
                        ui.label(
                            "Used by s commands. HotString prefers PowerShell 7+ (pwsh) when available."
                        ).classes("text-sm text-slate-500")
                    ui.label(get_powershell_display_name()).classes(
                        "text-xs text-slate-700 bg-slate-100 border border-slate-200 rounded px-3 py-2 hs-mono"
                    )
                ui.separator()
                with ui.row().classes("w-full items-center justify-between gap-3 flex-wrap"):
                    with ui.column().classes("gap-0"):
                        ui.label("Data and logs").classes("font-medium text-slate-800")
                        ui.label(str(config.get_db_path())).classes("text-xs text-slate-500 hs-mono")
                        ui.label(str(config.get_log_path())).classes("text-xs text-slate-500 hs-mono")
                    with ui.row().classes("gap-2"):
                        ui.button(
                            "Open data folder",
                            icon="folder_open",
                            on_click=self._open_data_folder,
                        ).props("outline color=primary")
                        ui.button(
                            "Quit HotString",
                            icon="logout",
                            on_click=self._confirm_quit,
                        ).props("outline color=negative")
                ui.label(
                    "Security note: s commands run PowerShell with your user permissions. "
                    "Only use scripts you understand."
                ).classes("text-xs text-amber-800 bg-amber-50 border border-amber-200 rounded p-3")

    def _on_autostart_change(self, event: Any) -> None:
        if self._syncing:
            return
        desired = bool(event.value)
        try:
            self.app.set_autostart(desired)
        except Exception as error:
            logger.exception("Could not change autostart")
            ui.notify(f"Could not change start with Windows: {error}", type="negative")
            if self.autostart_switch is not None:
                self._syncing = True
                self.autostart_switch.value = not desired
                self._syncing = False
            return
        self._last_autostart = desired
        ui.notify(
            "HotString will start with Windows." if desired else "Start with Windows disabled.",
            type="positive" if desired else "info",
        )
        self.app.refresh_tray_menu()

    def _on_pause_change(self, event: Any) -> None:
        if self._syncing:
            return
        paused = bool(event.value)
        self.app.set_paused(paused)
        self._last_paused = paused
        self._update_status()
        ui.notify("Listening paused." if paused else "Listening resumed.", type="warning" if paused else "positive")

    def _update_status(self) -> None:
        if self.status_badge is None:
            return
        paused = self.app.is_paused()
        self._last_paused = paused
        if paused:
            self.status_badge.set_text("Paused")
            self.status_badge.classes(
                remove="bg-green-500/20 text-green-300",
                add="bg-amber-500/20 text-amber-200",
            )
        else:
            self.status_badge.set_text("Listening")
            self.status_badge.classes(
                remove="bg-amber-500/20 text-amber-200",
                add="bg-green-500/20 text-green-300",
            )

    def _sync_external_state(self) -> None:
        """Reflect changes made from the tray or a second instance."""

        paused = self.app.is_paused()
        if paused != self._last_paused:
            self._update_status()
            if self.pause_switch is not None and bool(self.pause_switch.value) != paused:
                self._syncing = True
                self.pause_switch.value = paused
                self._syncing = False

        self._ticks += 1
        if self._ticks % 8 == 0:
            enabled = self.app.autostart_enabled()
            if enabled != self._last_autostart:
                self._last_autostart = enabled
                if self.autostart_switch is not None and bool(self.autostart_switch.value) != enabled:
                    self._syncing = True
                    self.autostart_switch.value = enabled
                    self._syncing = False

    def _open_data_folder(self) -> None:
        path = config.get_data_dir()
        try:
            if sys.platform == "win32":
                os.startfile(path)  # type: ignore[attr-defined]
            else:
                subprocess.Popen(["xdg-open", str(path)])
        except Exception as error:
            logger.exception("Could not open data folder")
            ui.notify(f"Could not open the data folder: {error}", type="negative")

    def _confirm_quit(self) -> None:
        with ui.dialog() as dialog, ui.card().classes("w-[420px] max-w-[95vw] p-6 gap-3"):
            ui.label("Quit HotString?").classes("text-lg font-semibold")
            ui.label(
                "The keyboard listener will stop. You can restart HotString from the Start menu "
                "or by running 'uv run hotstring'."
            ).classes("text-sm text-slate-600")
            with ui.row().classes("w-full justify-end gap-2 pt-2"):
                ui.button("Cancel", on_click=dialog.close).props("flat")
                ui.button(
                    "Quit",
                    icon="logout",
                    on_click=lambda: (dialog.close(), self.app.request_exit()),
                ).props("unelevated color=negative")
        dialog.open()

    # -- footer ------------------------------------------------------------

    def _build_footer(self) -> None:
        with ui.column().classes("w-full items-center gap-1 py-4 text-center"):
            ui.label(
                f"HotString {config.VERSION} · Closing this window keeps HotString running in the system tray."
            ).classes("text-xs text-slate-400")
            ui.label(
                "Tip: use ;hs → aopen as an emergency shortcut to reopen this window."
            ).classes("text-xs text-slate-400")

    # -- refresh -----------------------------------------------------------

    def refresh_all(self) -> None:
        self._refresh_table()
        self._render_onboarding()
        self._render_listener_error()
        self._update_status()

    def _drain_errors(self) -> None:
        """Show queued background errors as toasts in the connected client."""

        for _ in range(10):
            message = self.app.pop_ui_error()
            if message is None:
                break
            ui.notify(message, type="negative", position="top", timeout=10000)


__all__ = ["HotStringUI", "format_timestamp"]
