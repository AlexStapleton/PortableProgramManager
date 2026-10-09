"""Add / Edit dialog for one code-hosting source, and the connection test it offers."""

from __future__ import annotations

import re
from collections.abc import Iterable
from urllib.parse import urlsplit

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QVBoxLayout,
)

from ..models import SourceConfig
from ..sources.base import build_provider
from . import settings_dialog as settings_ui
from .theme import status_color

ADDRESS_HINTS = {
    "github": "https://github.com",
    "gitlab": "https://gitlab.com",
    "gitea": "https://codeberg.org",
}
TOKEN_HELP = {
    "github": "Optional. A fine-grained token with public read access is enough.",
    "gitlab": "Optional. Create a personal access token with read_api.",
    "gitea": "Optional. Create an access token with read:repository.",
}


def normalise_address(text: str) -> str:
    return text.strip().rstrip("/")


def address_problem(address: str) -> str | None:
    """Why *address* can't be used as a site address, or None if it can."""
    if not address.startswith("https://") or not urlsplit(address).hostname:
        return "The address must start with https:// and include a host name, for example https://gitlab.com."
    return None


def slugify(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")


def unique_source_id(name: str, taken: Iterable[str]) -> str:
    """Slug of *name*, with -2, -3... appended until it isn't in *taken*."""
    taken = set(taken)
    base = slugify(name) or "source"
    candidate, number = base, 2
    while candidate in taken:
        candidate = f"{base}-{number}"
        number += 1
    return candidate


def _error_text(exc: Exception, token: str) -> str:
    text = str(exc).strip() or type(exc).__name__
    return text.replace(token, "***") if token else text


def _close_quietly(provider) -> None:
    try:
        provider.close()
    except Exception:
        pass


def check_source_connection(config: SourceConfig) -> tuple[str, str]:
    """Run a one-result search on *config*. Returns ``(message, status kind)``.

    The message never contains the token.
    """
    if address_problem(config.base_url):
        return "✗ Enter an https:// address first.", "error"
    QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
    try:
        provider = build_provider(config)
        if provider is None:
            return "✗ This source type isn't available in this build", "error"
        try:
            provider.search_repositories("test", limit=1)
            remaining = provider.rate_limit_remaining
        finally:
            _close_quietly(provider)
    except Exception as exc:
        return f"✗ {_error_text(exc, config.token)}", "error"
    finally:
        QApplication.restoreOverrideCursor()
    message = f"✓ Connected to {config.name.strip() or config.base_url}"
    if remaining is not None:
        message += f" · {remaining} requests left"
    return message, "ok"


class SourceDialog(QDialog):
    """Add a new source, or edit ``source``.

    ``others`` are the sources already in the list, excluding the one being
    edited. They keep new ids unique and catch a duplicate address.
    """

    def __init__(
        self,
        source: SourceConfig | None = None,
        others: list[SourceConfig] | None = None,
        parent=None,
    ) -> None:
        super().__init__(parent)
        self._source = source
        self._others = list(others or [])
        self.setWindowTitle("Edit source" if source is not None else "Add source")
        self.setMinimumWidth(480)

        self.name_edit = QLineEdit(source.name if source is not None else "")
        self.name_edit.setPlaceholderText("For example: Work GitLab")

        self.type_combo = QComboBox()
        settings_ui.fill_combo(self.type_combo, settings_ui.SOURCE_TYPE_CHOICES)
        settings_ui.select_data(self.type_combo, source.kind if source is not None else "github")
        self.type_combo.currentIndexChanged.connect(self._on_type_changed)

        self.address_edit = QLineEdit(source.base_url if source is not None else "")
        self.address_edit.textChanged.connect(self._clear_result)

        self.token_edit = QLineEdit(source.token if source is not None else "")
        self.token_edit.setEchoMode(QLineEdit.EchoMode.Password)
        self.token_edit.textChanged.connect(self._clear_result)
        self.show_token = QPushButton("Show")
        self.show_token.setCheckable(True)
        self.show_token.setProperty("variant", "secondary")
        self.show_token.toggled.connect(self._toggle_token_visible)
        self.token_help = settings_ui.subtle_label()

        self.gh_cli_button = settings_ui.make_button("Use my GitHub CLI login", slot=self._import_gh_cli_token)
        self.gh_cli_button.setToolTip(
            "If you're signed in with the GitHub CLI (gh auth login), copy its token here."
        )

        self.enabled_check = QCheckBox("Enabled")
        self.enabled_check.setChecked(source.enabled if source is not None else True)
        self.searchable_check = QCheckBox("Include in search")
        self.searchable_check.setChecked(source.searchable if source is not None else True)

        self.test_button = settings_ui.make_button("Test connection", slot=self._test_connection)
        self.test_result = QLabel()
        self.test_result.setWordWrap(True)

        form = QFormLayout()
        form.addRow("Name", self.name_edit)
        form.addRow("Type", self.type_combo)
        form.addRow("Address", self.address_edit)
        form.addRow("Token", settings_ui.hbox(self.token_edit, self.show_token))
        form.addRow("", self.token_help)
        form.addRow("", self.gh_cli_button)
        form.addRow("", settings_ui.hbox(self.enabled_check, self.searchable_check))
        form.addRow("", self.test_button)
        form.addRow("", self.test_result)

        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        ok_button = buttons.button(QDialogButtonBox.Ok)
        ok_button.setDefault(True)
        ok_button.setProperty("variant", "primary")
        buttons.button(QDialogButtonBox.Cancel).setProperty("variant", "secondary")
        buttons.accepted.connect(self._validate_and_accept)
        buttons.rejected.connect(self.reject)

        layout = QVBoxLayout(self)
        layout.addLayout(form)
        layout.addWidget(buttons)

        self._on_type_changed()

    # ------------------------------------------------------------------
    # Field helpers
    # ------------------------------------------------------------------

    def _current_kind(self) -> str:
        return self.type_combo.currentData()

    def _on_type_changed(self, *_args) -> None:
        kind = self._current_kind()
        self.address_edit.setPlaceholderText(ADDRESS_HINTS[kind])
        self.token_help.setText(TOKEN_HELP[kind])
        self.gh_cli_button.setVisible(kind == "github" and settings_ui.find_gh_cli() is not None)
        self.test_result.clear()

    def _toggle_token_visible(self, checked: bool) -> None:
        self.token_edit.setEchoMode(QLineEdit.EchoMode.Normal if checked else QLineEdit.EchoMode.Password)
        self.show_token.setText("Hide" if checked else "Show")

    def _clear_result(self, *_args) -> None:
        self.test_result.clear()

    def _show_result(self, text: str, kind: str) -> None:
        self.test_result.setText(text)
        self.test_result.setStyleSheet(f"color: {status_color(kind).name()};")

    def _import_gh_cli_token(self) -> None:
        """Fill the token from ``gh auth token`` (the token itself is never shown or logged)."""
        QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        try:
            token, error = settings_ui.read_gh_cli_token()
        finally:
            QApplication.restoreOverrideCursor()
        if not token:
            self._show_result(error, "error")
            return
        self.token_edit.setText(token)  # clears the result, so show ours afterwards
        self._show_result("✓ Copied the token from your GitHub CLI login. Click OK to save it.", "ok")

    def _draft_config(self, source_id: str) -> SourceConfig:
        return SourceConfig(
            id=source_id,
            name=self.name_edit.text().strip(),
            kind=self._current_kind(),
            base_url=normalise_address(self.address_edit.text()),
            token=self.token_edit.text().strip(),
            enabled=self.enabled_check.isChecked(),
            searchable=self.searchable_check.isChecked(),
        )

    def _test_connection(self) -> None:
        source_id = self._source.id if self._source is not None else ""
        message, kind = check_source_connection(self._draft_config(source_id))
        self._show_result(message, kind)

    # ------------------------------------------------------------------
    # Accept / result
    # ------------------------------------------------------------------

    def _validate_and_accept(self) -> None:
        name = self.name_edit.text().strip()
        address = normalise_address(self.address_edit.text())
        kind = self._current_kind()

        errors: list[str] = []
        if not name:
            errors.append("Give the source a name.")
        problem = address_problem(address)
        if problem:
            errors.append(problem)
        else:
            twin = next(
                (
                    other
                    for other in self._others
                    if other.kind == kind and normalise_address(other.base_url).lower() == address.lower()
                ),
                None,
            )
            if twin is not None:
                errors.append(
                    f"\"{twin.name}\" already uses {address} as a "
                    f"{settings_ui.SOURCE_TYPE_NAMES[kind]} source. Edit that one instead."
                )
        if errors:
            QMessageBox.warning(self, "Source", "\n\n".join(errors))
            return
        self.accept()

    def get_config(self) -> SourceConfig:
        """The source as entered. A new source gets an id made from its name."""
        if self._source is not None:
            source_id = self._source.id
        else:
            source_id = unique_source_id(self.name_edit.text(), (other.id for other in self._others))
        return self._draft_config(source_id)
