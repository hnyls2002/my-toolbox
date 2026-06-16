"""Textual TUI to browse and batch-delete your ChatGPT conversation history.

All backend calls go through your logged-in Chrome (see browser.py), so there is
no access token to manage: the browser session authenticates and clears
Cloudflare. macOS + Google Chrome, with "Allow JavaScript from Apple Events"
enabled (Chrome menu -> View -> Developer).
"""

from __future__ import annotations

from textual import on, work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import (
    Button,
    Footer,
    Header,
    Input,
    Label,
    ProgressBar,
    SelectionList,
    Static,
)
from textual.widgets.selection_list import Selection

from my_toolbox.gpt_kit.browser import BrowserClient, BrowserError, Conversation


class ConfirmScreen(ModalScreen[bool]):
    """Confirm a destructive batch delete."""

    BINDINGS = [Binding("escape", "cancel", "Cancel")]

    def __init__(self, count: int):
        super().__init__()
        self.count = count

    def compose(self) -> ComposeResult:
        with Vertical(id="confirm-box"):
            yield Label(f"Delete {self.count} conversation(s)?", id="confirm-title")
            yield Static(
                "This removes them from ChatGPT (same as the web delete). "
                "It cannot be undone from this tool.",
                id="confirm-help",
            )
            with Horizontal(id="confirm-buttons"):
                yield Button("Delete", variant="error", id="confirm-yes")
                yield Button("Cancel", variant="primary", id="confirm-no")

    @on(Button.Pressed, "#confirm-yes")
    def yes(self) -> None:
        self.dismiss(True)

    @on(Button.Pressed, "#confirm-no")
    def action_cancel(self) -> None:
        self.dismiss(False)


class DeleteScreen(ModalScreen[tuple[list[str], list[tuple[str, str]]]]):
    """Run the deletions with a progress bar; returns (succeeded, errors)."""

    def __init__(self, client: BrowserClient, conversations: list[Conversation]):
        super().__init__()
        self.client = client
        self.conversations = conversations

    def compose(self) -> ComposeResult:
        with Vertical(id="delete-box"):
            yield Label("Deleting...", id="delete-title")
            yield ProgressBar(total=len(self.conversations), id="delete-bar")
            yield Static("", id="delete-status")

    def on_mount(self) -> None:
        self.run_delete()

    @work
    async def run_delete(self) -> None:
        bar = self.query_one("#delete-bar", ProgressBar)
        status = self.query_one("#delete-status", Static)
        ids = [c.id for c in self.conversations]

        def on_progress(done: int, total: int) -> None:
            bar.update(progress=done)
            status.update(f"{done}/{total}")

        try:
            succeeded, errors = await self.client.delete_many(ids, progress=on_progress)
        except BrowserError as exc:
            self.dismiss(([], [("", str(exc))]))
            return
        self.dismiss((succeeded, errors))


class HistoryApp(App):
    CSS = """
    #search { margin: 0 1; }
    #status { height: 1; padding: 0 1; color: $text-muted; }
    #list { height: 1fr; margin: 0 1; border: round $panel; }

    ConfirmScreen, DeleteScreen { align: center middle; }

    #confirm-box, #delete-box {
        width: 72;
        height: auto;
        padding: 1 2;
        border: thick $primary;
        background: $surface;
    }
    #confirm-help { color: $text-muted; margin: 1 0; }
    #confirm-buttons { height: auto; align-horizontal: right; }
    #confirm-buttons Button { margin-left: 2; }
    #delete-status { margin-top: 1; color: $text-muted; }
    """

    BINDINGS = [
        Binding("slash", "focus_search", "Search"),
        Binding("a", "select_all", "Select all"),
        Binding("n", "select_none", "Clear"),
        Binding("d", "delete", "Delete"),
        Binding("r", "refresh", "Refresh"),
        Binding("q", "quit", "Quit"),
    ]

    def __init__(self):
        super().__init__()
        self.client: BrowserClient | None = None
        self.conversations: list[Conversation] = []
        self.selected_ids: set[str] = set()
        self.filter_text = ""
        self._visible_ids: set[str] = set()
        self._building = False

    def compose(self) -> ComposeResult:
        yield Header()
        yield Input(placeholder="Filter by title... (press / to focus)", id="search")
        yield Static("", id="status")
        yield SelectionList(id="list")
        yield Footer()

    def on_mount(self) -> None:
        self.title = "gpt-kit history"
        self.set_status("Starting...")
        self.startup()

    # --- data loading -----------------------------------------------------

    @work(exclusive=True)
    async def startup(self) -> None:
        if self.client is None:
            self.client = BrowserClient()
        self.set_status("Loading via Chrome...")
        try:
            convs = await self.client.list_all(
                progress=lambda n, t: self.set_status(f"Loaded {n}/{t}")
            )
        except BrowserError as exc:
            self.set_status(str(exc))
            return
        self.conversations = convs
        self.refresh_list()

    # --- list rendering ---------------------------------------------------

    def _sync_selection(self) -> None:
        if self._building:
            return
        widget = self.query_one("#list", SelectionList)
        self.selected_ids = (self.selected_ids - self._visible_ids) | set(
            widget.selected
        )

    def refresh_list(self) -> None:
        widget = self.query_one("#list", SelectionList)
        ft = self.filter_text.lower()
        visible = [c for c in self.conversations if not ft or ft in c.title.lower()]
        self._visible_ids = {c.id for c in visible}

        self._building = True
        widget.clear_options()
        widget.add_options(
            [Selection(c.label, c.id, c.id in self.selected_ids) for c in visible]
        )
        self._building = False
        self.update_status()

    def update_status(self) -> None:
        self.query_one("#status", Static).update(
            f"Total: {len(self.conversations)}   "
            f"Shown: {len(self._visible_ids)}   "
            f"Selected: {len(self.selected_ids)}"
        )

    def set_status(self, message: str) -> None:
        self.query_one("#status", Static).update(message)

    # --- events -----------------------------------------------------------

    @on(SelectionList.SelectedChanged, "#list")
    def on_selection_changed(self) -> None:
        if self._building:
            return
        self._sync_selection()
        self.update_status()

    @on(Input.Changed, "#search")
    def on_search_changed(self, event: Input.Changed) -> None:
        self._sync_selection()
        self.filter_text = event.value.strip()
        self.refresh_list()

    # --- actions ----------------------------------------------------------

    def action_focus_search(self) -> None:
        self.query_one("#search", Input).focus()

    def action_select_all(self) -> None:
        self._sync_selection()
        self.selected_ids |= self._visible_ids
        self.refresh_list()

    def action_select_none(self) -> None:
        self.selected_ids.clear()
        self.refresh_list()

    def action_refresh(self) -> None:
        self.startup()

    def action_delete(self) -> None:
        self._sync_selection()
        self.run_delete_flow()

    @work(exclusive=True)
    async def run_delete_flow(self) -> None:
        if not self.selected_ids:
            self.set_status("Nothing selected.")
            return
        if self.client is None:
            self.set_status("Not connected.")
            return
        targets = [c for c in self.conversations if c.id in self.selected_ids]
        if not await self.push_screen_wait(ConfirmScreen(len(targets))):
            return
        succeeded, errors = await self.push_screen_wait(
            DeleteScreen(self.client, targets)
        )
        done = set(succeeded)
        self.conversations = [c for c in self.conversations if c.id not in done]
        self.selected_ids -= done
        self.refresh_list()
        message = f"Deleted {len(succeeded)}."
        if errors:
            message += f" {len(errors)} failed (e.g. {errors[0][1]})."
        self.set_status(message)


def run() -> None:
    HistoryApp().run()
