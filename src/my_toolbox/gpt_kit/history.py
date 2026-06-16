"""Textual TUI to browse and batch-delete your ChatGPT conversation history.

All backend calls go through your logged-in Chrome (see browser.py), so there is
no access token to manage: the browser session authenticates and clears
Cloudflare. macOS + Google Chrome, with "Allow JavaScript from Apple Events"
enabled (Chrome menu -> View -> Developer).
"""

from __future__ import annotations

from rich.segment import Segment
from textual import events, on, work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.strip import Strip
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
from textual.widgets.option_list import OptionDoesNotExist
from textual.widgets.selection_list import Selection

from my_toolbox.gpt_kit.browser import BrowserClient, BrowserError, Conversation


class VimSelectionList(SelectionList):
    """SelectionList with vim navigation and visual range-select.

    j/k move, gg/G jump to top/bottom. `V` enters visual mode (the current row
    is the anchor); moving extends a contiguous selection from it. `V` again
    commits the range, `Esc` cancels it. Rows toggled by hand are never
    clobbered by the range.
    """

    COMPONENT_CLASSES = {"vim-selection-list--selected-row"}

    DEFAULT_CSS = """
    VimSelectionList > .vim-selection-list--selected-row {
        background: $success 25%;
    }
    """

    BINDINGS = [
        Binding("j", "cursor_down", "Down", show=False),
        Binding("k", "cursor_up", "Up", show=False),
        Binding("G", "last", "Bottom", show=False),
        Binding("V", "toggle_visual", "Visual select", show=False),
    ]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._g_pending = False  # armed by the first `g` of a `gg` motion
        self._visual = False
        self._anchor: int | None = None
        self._visual_added: set[str] = set()  # values this visual drag selected

    def on_key(self, event: events.Key) -> None:
        if event.key == "escape" and self._visual:
            event.prevent_default()
            event.stop()
            self._end_visual(commit=False)  # Esc cancels the range
            return
        if event.key == "g":
            event.prevent_default()
            event.stop()
            if self._g_pending:
                self._g_pending = False
                self.action_first()
            else:
                self._g_pending = True
            return
        self._g_pending = False

    # Movement actions move the cursor, then extend the range if visual is on.
    def action_cursor_down(self) -> None:
        super().action_cursor_down()
        self._extend_visual()

    def action_cursor_up(self) -> None:
        super().action_cursor_up()
        self._extend_visual()

    def action_first(self) -> None:
        super().action_first()
        self._extend_visual()

    def action_last(self) -> None:
        super().action_last()
        self._extend_visual()

    def action_toggle_visual(self) -> None:
        if self._visual:
            self._end_visual(commit=True)  # `V` again keeps the range
            return
        self._visual = True
        self._anchor = self.highlighted if self.highlighted is not None else 0
        self._visual_added = set()
        self._extend_visual()

    def _end_visual(self, commit: bool) -> None:
        if not commit:  # cancel: undo only what this drag added
            for value in self._visual_added:
                self.deselect(value)
        self._visual = False
        self._anchor = None
        self._visual_added = set()

    def _extend_visual(self) -> None:
        # Visual mode owns only the rows it selects, so toggling other rows by
        # hand (space) is never clobbered when the range grows or shrinks.
        if not self._visual or self._anchor is None or self.highlighted is None:
            return
        lo, hi = sorted((self._anchor, self.highlighted))
        in_range = {self.get_option_at_index(i).value for i in range(lo, hi + 1)}
        selected = set(self.selected)
        for value in in_range - selected:  # rows entering the range
            self.select(value)
            self._visual_added.add(value)
        for value in self._visual_added - in_range:  # rows leaving the range
            self.deselect(value)
            self._visual_added.discard(value)

    def clear_options(self):
        # Reloading the list invalidates anchor indices; drop visual state.
        self._end_visual(commit=True)
        self._g_pending = False
        return super().clear_options()

    def render_line(self, y: int) -> Strip:
        # SelectionList only highlights the cursor row; also tint selected rows
        # so a multi-row selection is visible at a glance.
        strip = super().render_line(y)
        _, scroll_y = self.scroll_offset
        index = scroll_y + y
        try:
            option = self.get_option_at_index(index)
        except OptionDoesNotExist:
            return strip
        if index != self.highlighted and option.value in self._selected:
            tint = self.get_component_rich_style("vim-selection-list--selected-row")
            # Apply the tint ON TOP of each segment so its background wins
            # (Strip.apply_style would keep the segment's own background).
            strip = Strip(
                [
                    Segment(
                        seg.text,
                        (
                            None
                            if seg.control
                            else (seg.style + tint if seg.style else tint)
                        ),
                        seg.control,
                    )
                    for seg in strip
                ],
                strip.cell_length,
            )
        return strip


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
        Binding("escape", "leave_search", "Back to list", show=False),
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
        yield VimSelectionList(id="list")
        yield Footer()

    def on_mount(self) -> None:
        self.title = "gpt-kit history"
        self.set_status("Starting...")
        self.startup()
        # Default focus on the list (command mode); `/` enters the filter.
        self.query_one("#list", SelectionList).focus()

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
        # Keep a visible cursor at the top (clear_options drops the highlight).
        if visible and (
            widget.highlighted is None or widget.highlighted >= len(visible)
        ):
            widget.highlighted = 0
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

    @on(Input.Submitted, "#search")
    def on_search_submitted(self) -> None:
        self.query_one("#list", SelectionList).focus()

    # --- actions ----------------------------------------------------------

    def action_focus_search(self) -> None:
        self.query_one("#search", Input).focus()

    def action_leave_search(self) -> None:
        self.query_one("#list", SelectionList).focus()

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
