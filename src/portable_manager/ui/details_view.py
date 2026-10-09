"""Read-only, friendly details panel for the selected managed program."""

from __future__ import annotations

import html
import logging
import os
from pathlib import Path

from PySide6.QtCore import QUrl
from PySide6.QtGui import QColor, QDesktopServices, QPalette
from PySide6.QtWidgets import QMessageBox, QTextBrowser

from ..models import ManagedProgram
from .programs_model import friendly_time, policy_summary, program_status, source_label
from .theme import status_color

log = logging.getLogger(__name__)

_CHANNEL_WORDS = {"latest_release": "Stable releases", "prerelease": "Pre-releases"}


class ProgramDetailsView(QTextBrowser):
    """Shows one program in plain language. Links open the folder or web pages."""

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setOpenLinks(False)
        self.setOpenExternalLinks(False)
        self.anchorClicked.connect(self._on_anchor_clicked)
        self._folder: str | None = None
        self.show_program(None)

    def show_program(
        self,
        program: ManagedProgram | None,
        running: list[str] | None = None,
        permission_problem: bool = False,
    ) -> None:
        self._folder = program.install_dir if program else None
        if program is None:
            muted = status_color("muted").name()
            self.setHtml(
                f'<p style="color:{muted}; margin-top:32px; text-align:center">'
                "Select a program to see its details.</p>"
            )
            return
        self.setHtml(self._build_html(program, running or [], permission_problem))

    # ------------------------------------------------------------------
    # HTML
    # ------------------------------------------------------------------

    def _tint(self, kind: str) -> str:
        """A solid background that is the status colour blended into the window colour."""
        accent = status_color(kind)
        base = self.palette().color(QPalette.ColorRole.Window)
        mixed = QColor(
            round(accent.red() * 0.18 + base.red() * 0.82),
            round(accent.green() * 0.18 + base.green() * 0.82),
            round(accent.blue() * 0.18 + base.blue() * 0.82),
        )
        return mixed.name()

    def _notice(self, kind: str, title: str, body: str) -> str:
        colour = status_color(kind).name()
        return (
            f'<table width="100%" cellspacing="0" cellpadding="10" bgcolor="{self._tint(kind)}">'
            f"<tr><td>"
            f'<span style="color:{colour}; font-weight:600">{html.escape(title)}</span><br>'
            f"{html.escape(body)}</td></tr></table>"
        )

    def _build_html(self, program: ManagedProgram, running: list[str], permission_problem: bool) -> str:
        esc = html.escape
        muted = status_color("muted").name()
        accent = status_color("accent").name()
        policy = program.update_policy
        folder_missing = not Path(program.install_dir).is_dir()
        info = program_status(
            program,
            folder_missing=folder_missing,
            running=running,
            permission_problem=permission_problem,
        )

        parts: list[str] = []
        parts.append(f'<div style="font-size:15pt; font-weight:600">{esc(program.name)}</div>')
        subtitle = " · ".join(
            part for part in (f"Version {program.version}" if program.version else "", source_label(program)) if part
        )
        parts.append(f'<div style="color:{muted}">{esc(subtitle)}</div>')
        parts.append(
            f'<p style="margin-top:8px"><span style="color:{status_color(info.kind).name()}; '
            f'font-weight:600">{esc(info.text)}</span></p>'
        )

        if permission_problem:
            parts.append(self._notice(
                "error",
                "Permissions need repair",
                "Some files can't be opened by your Windows account. Use More > Repair permissions.",
            ))
        if program.last_update_status in ("check_failed", "update_failed", "no_compatible_asset") and (
            program.last_error_message
        ):
            title = {
                "check_failed": "Last update check failed",
                "update_failed": "Last update failed",
                "no_compatible_asset": "No Windows build in the latest release",
            }[program.last_update_status]
            when = friendly_time(program.last_error_at)
            body = program.last_error_message + (f"\n{when}" if when else "")
            parts.append(self._notice(info.kind if info.kind in ("error", "warning") else "error", title, body))

        rows: list[tuple[str, str]] = []

        folder_text = f'<a href="folder:open" style="color:{accent}">{esc(program.install_dir)}</a>'
        if folder_missing:
            folder_text += f' <span style="color:{status_color("error").name()}">(folder missing)</span>'
        rows.append(("Installed in", folder_text))

        if program.launch_path:
            launch = esc(Path(program.launch_path).name)
            if program.launch_args:
                launch += f" <span style='color:{muted}'>{esc(program.launch_args)}</span>"
            if program.run_as_admin:
                launch += f" <span style='color:{muted}'>(as administrator)</span>"
        else:
            launch = f'<span style="color:{muted}">Not set. Use Edit to choose the file to launch.</span>'
        rows.append(("Launches", launch))

        if program.repo_full_name:
            repo = esc(program.repo_full_name)
            source = f'GitHub · <a href="https://github.com/{repo}" style="color:{accent}">{repo}</a>'
        else:
            source = esc(source_label(program))
        rows.append(("Source", source))

        channel = _CHANNEL_WORDS.get(policy.channel, policy.channel or "Stable releases")
        rows.append(("Release channel", esc(channel)))
        rows.append(("Update checks", esc(policy_summary(policy))))

        latest = program.latest_upstream_version
        if program.update_available:
            latest_text = f'<span style="color:{status_color("update").name()}">{esc(latest or "A newer release")} is available</span>'
        elif latest:
            latest_text = f"Up to date with {esc(latest)}"
        else:
            latest_text = f'<span style="color:{muted}">Not checked yet</span>'
        rows.append(("Latest release", latest_text))

        if program.pending_update_version:
            rows.append((
                "Downloaded update",
                f'<span style="color:{status_color("update").name()}">'
                f"{esc(program.pending_update_version)} is ready to install</span>",
            ))

        rows.append(("Last checked", esc(friendly_time(program.last_checked_at)) or f'<span style="color:{muted}">Never</span>'))
        rows.append(("Last run", esc(friendly_time(program.last_run_at)) or f'<span style="color:{muted}">Never</span>'))
        rows.append(("Installed", esc(friendly_time(program.installed_at)) or f'<span style="color:{muted}">Unknown</span>'))
        if running:
            rows.append(("Running now", esc(", ".join(running))))

        if program.notes and program.notes.strip():
            notes = esc(program.notes.strip()).replace("\n", "<br>")
        else:
            notes = f'<span style="color:{muted}">No notes</span>'
        rows.append(("Notes", notes))

        table_rows = "".join(
            f'<tr><td valign="top" width="30%" style="color:{muted}; padding:4px 12px 4px 0">{esc(label)}</td>'
            f'<td valign="top" style="padding:4px 0">{value}</td></tr>'
            for label, value in rows
        )
        parts.append(f'<table width="100%" cellspacing="0" cellpadding="0" style="margin-top:10px">{table_rows}</table>')
        return "".join(parts)

    # ------------------------------------------------------------------
    # Links
    # ------------------------------------------------------------------

    def _on_anchor_clicked(self, url: QUrl) -> None:
        scheme = url.scheme().lower()
        if scheme in ("http", "https"):
            QDesktopServices.openUrl(url)
        elif scheme == "folder":
            self._open_folder()

    def _open_folder(self) -> None:
        folder = self._folder
        if not folder or not Path(folder).is_dir():
            QMessageBox.warning(self, "Portable Program Manager", f"Folder not found:\n{folder or ''}")
            return
        try:
            os.startfile(folder)
        except OSError as exc:
            log.warning("Could not open folder %s: %s", folder, exc)
            QMessageBox.warning(self, "Portable Program Manager", f"Could not open folder: {exc}")
