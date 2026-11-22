"""Simple, intuitive single-screen Textual TUI for fetch-ota."""

from __future__ import annotations

from collections import OrderedDict
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from textual import on, work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import (
    Button,
    DataTable,
    Footer,
    Header,
    Input,
    Label,
    Select,
    Static,
)

from .api import OxygenUpdaterApi, pick_default_full_method
from .config import AppSettings, save_settings
from .downloader import DownloadManager
from .oplus_api import OplusOtaClient
from .models import (
    Device,
    DownloadJob,
    DownloadStatus,
    UpdateData,
    UpdateMethod,
    format_bytes,
    format_eta,
    format_speed,
    format_speed_limit,
    parse_speed_limit,
)


class SettingsModal(ModalScreen[dict[str, int] | None]):
    """Compact modal to configure parallel downloads, connections, and speed limits."""

    CSS = """
    SettingsModal {
        align: center middle;
    }
    #settings-dialog {
        width: 58;
        height: auto;
        padding: 1 2;
        background: $surface;
        border: round $primary;
    }
    #settings-title {
        text-style: bold;
        margin-bottom: 1;
    }
    .setting-row {
        height: 3;
        align: left middle;
    }
    .setting-label {
        width: 30;
        color: $text-muted;
    }
    .setting-row Input {
        width: 1fr;
    }
    #settings-hint {
        color: $text-muted;
        height: 1;
        margin-top: 1;
    }
    #settings-error {
        color: $error;
        height: auto;
    }
    #settings-buttons {
        height: auto;
        margin-top: 1;
        align: right middle;
    }
    #settings-buttons Button {
        margin-left: 1;
        height: 3;
        border: none;
    }
    """

    BINDINGS = [Binding("escape", "cancel", "Cancel")]

    def __init__(
        self,
        *,
        max_parallel: int,
        connections: int,
        global_limit_bps: int,
        per_file_limit_bps: int,
    ) -> None:
        super().__init__()
        self._max_parallel = max_parallel
        self._connections = connections
        self._global_limit_bps = global_limit_bps
        self._per_file_limit_bps = per_file_limit_bps

    @staticmethod
    def _bps_to_input(bps: int) -> str:
        if bps <= 0:
            return "0"
        mb = bps / (1024 * 1024)
        return f"{mb:.2f}".rstrip("0").rstrip(".")

    def compose(self) -> ComposeResult:
        with Vertical(id="settings-dialog"):
            yield Static("Download Settings", id="settings-title")

            with Horizontal(classes="setting-row"):
                yield Label("Max parallel downloads", classes="setting-label")
                yield Input(value=str(self._max_parallel), id="inp-parallel")

            with Horizontal(classes="setting-row"):
                yield Label("Connections per download", classes="setting-label")
                yield Input(value=str(self._connections), id="inp-conns")

            with Horizontal(classes="setting-row"):
                yield Label("Global speed limit (MB/s)", classes="setting-label")
                yield Input(
                    value=self._bps_to_input(self._global_limit_bps),
                    id="inp-global-limit",
                )

            with Horizontal(classes="setting-row"):
                yield Label("Per-file speed limit (MB/s)", classes="setting-label")
                yield Input(
                    value=self._bps_to_input(self._per_file_limit_bps),
                    id="inp-file-limit",
                )

            yield Static("Speed: 0 = unlimited, or use K/M/G suffix (e.g. 500K, 10M)", id="settings-hint")
            yield Static("", id="settings-error")
            with Horizontal(id="settings-buttons"):
                yield Button("Cancel", flat=True, id="btn-cancel")
                yield Button("Save", variant="primary", flat=True, id="btn-save")

    def action_cancel(self) -> None:
        self.dismiss(None)

    @on(Button.Pressed, "#btn-cancel")
    def _on_cancel(self) -> None:
        self.dismiss(None)

    @on(Button.Pressed, "#btn-save")
    def _on_save(self) -> None:
        try:
            parallel = int(self.query_one("#inp-parallel", Input).value.strip())
            conns = int(self.query_one("#inp-conns", Input).value.strip())
            if parallel < 1 or conns < 1:
                raise ValueError("Parallel downloads and connections must be >= 1.")
            global_bps = parse_speed_limit(
                self.query_one("#inp-global-limit", Input).value
            )
            file_bps = parse_speed_limit(
                self.query_one("#inp-file-limit", Input).value
            )
        except ValueError as exc:
            self.query_one("#settings-error", Static).update(str(exc))
            return

        self.dismiss(
            {
                "max_parallel": parallel,
                "connections": conns,
                "global_limit_bps": global_bps,
                "per_file_limit_bps": file_bps,
            }
        )


@dataclass(slots=True)
class DeviceGroup:
    base_name: str
    brand: str
    devices: list[Device]

    @property
    def models_str(self) -> str:
        seen: list[str] = []
        for d in self.devices:
            for p in d.base_models:
                if p not in seen:
                    seen.append(p)
        return ", ".join(seen)


class DirectOppoModal(ModalScreen[dict[str, Any] | None]):
    """Modal for querying OPPO's official update/v3 servers directly."""

    CSS = """
    DirectOppoModal {
        align: center middle;
    }
    #direct-dialog {
        width: 62;
        height: auto;
        padding: 1 2;
        background: $surface;
        border: round $primary;
    }
    #direct-title {
        text-style: bold;
        margin-bottom: 1;
    }
    .direct-row {
        height: 3;
        align: left middle;
    }
    .direct-label {
        width: 24;
        color: $text-muted;
    }
    .direct-row Input, .direct-row Select {
        width: 1fr;
    }
    #direct-error {
        color: $error;
        height: auto;
    }
    #direct-buttons {
        height: auto;
        margin-top: 1;
        align: right middle;
    }
    #direct-buttons Button {
        margin-left: 1;
        height: 3;
        border: none;
    }
    """

    BINDINGS = [Binding("escape", "cancel", "Cancel")]

    def compose(self) -> ComposeResult:
        with Vertical(id="direct-dialog"):
            yield Static("Direct OPPO Server Query (update/v3)", id="direct-title")

            with Horizontal(classes="direct-row"):
                yield Label("Model (e.g. CPH2791)", classes="direct-label")
                yield Input(placeholder="CPH2791", id="inp-model")

            with Horizontal(classes="direct-row"):
                yield Label("Region", classes="direct-label")
                yield Select(
                    [("Global (GL)", "GL"), ("Europe (EU)", "EU"), ("India (IN)", "IN"), ("China (CN)", "CN")],
                    value="GL",
                    allow_blank=False,
                    id="inp-region",
                )

            with Horizontal(classes="direct-row"):
                yield Label("ColorOS Major (11-16)", classes="direct-label")
                yield Input(value="15", id="inp-coloros")

            with Horizontal(classes="direct-row"):
                yield Label("OTA Version (optional)", classes="direct-label")
                yield Input(placeholder="Auto-generated if blank", id="inp-otaver")

            with Horizontal(classes="direct-row"):
                yield Label("NV Carrier ID (optional)", classes="direct-label")
                yield Input(placeholder="e.g. 00110011 (auto if blank)", id="inp-nvid")

            yield Static("", id="direct-error")
            with Horizontal(id="direct-buttons"):
                yield Button("Cancel", flat=True, id="btn-direct-cancel")
                yield Button("Query", variant="primary", flat=True, id="btn-direct-query")

    def action_cancel(self) -> None:
        self.dismiss(None)

    @on(Button.Pressed, "#btn-direct-cancel")
    def _on_cancel(self) -> None:
        self.dismiss(None)

    @on(Button.Pressed, "#btn-direct-query")
    def _on_query(self) -> None:
        model = self.query_one("#inp-model", Input).value.strip().upper()
        region = str(self.query_one("#inp-region", Select).value)
        ota_ver = self.query_one("#inp-otaver", Input).value.strip() or None
        nv_id = self.query_one("#inp-nvid", Input).value.strip() or None
        try:
            coloros = int(self.query_one("#inp-coloros", Input).value.strip())
            if not model:
                raise ValueError("Model number is required (e.g. CPH2791).")
            if not (11 <= coloros <= 16):
                raise ValueError("ColorOS major version must be 11-16.")
        except ValueError as exc:
            self.query_one("#direct-error", Static).update(str(exc))
            return

        self.dismiss(
            {
                "model": model,
                "region": region,
                "coloros_major": coloros,
                "ota_version": ota_ver,
                "nv_id": nv_id,
            }
        )


class FetchOtaApp(App[None]):
    """Clean single-screen TUI for browsing and downloading OTAs."""

    TITLE = "fetch-oppo-ota"
    SUB_TITLE = "OPPO / OnePlus / realme Full OTA Downloader"

    CSS = """
    Screen {
        layout: vertical;
    }
    #main {
        height: 1fr;
    }
    #devices-pane {
        width: 45%;
        padding: 1 1 0 1;
        border-right: solid $primary-darken-2;
    }
    #search-bar {
        height: auto;
        margin-bottom: 1;
    }
    #brand-select {
        width: 18;
        margin-right: 1;
    }
    #search-input {
        width: 1fr;
    }
    #device-table {
        height: 1fr;
    }
    #details-pane {
        width: 55%;
        padding: 1 1 0 1;
    }
    #ota-summary {
        height: auto;
        padding: 1;
        margin-bottom: 1;
        background: $panel;
        border: round $primary-darken-1;
    }
    #controls-row {
        height: auto;
        margin-bottom: 1;
        align: left middle;
    }
    #region-select {
        width: 22;
        margin-right: 1;
    }
    #region-select.hidden {
        display: none;
    }
    #method-select {
        width: 1fr;
        margin-right: 1;
    }
    #btn-download {
        width: 16;
        height: 3;
        border: none;
    }
    #changelog-scroll {
        height: 1fr;
        padding: 0 1;
        border: round $surface-lighten-1;
    }
    #downloads-pane {
        height: 10;
        border-top: solid $primary-darken-2;
        padding: 0 1;
    }
    #downloads-header {
        height: 1;
        color: $text-muted;
        text-style: bold;
    }
    #downloads-table {
        height: 1fr;
    }
    """

    BINDINGS = [
        Binding("/", "focus_search", "Search", show=True),
        Binding("ctrl+o", "open_direct_oppo", "Direct OPPO", show=True),
        Binding("d", "queue_download", "Download", show=True),
        Binding("p", "toggle_pause", "Pause/Resume", show=True),
        Binding("x", "cancel_download", "Cancel", show=True),
        Binding("ctrl+s", "open_settings", "Settings", show=True),
        Binding("q", "quit", "Quit", show=True),
    ]

    def __init__(
        self,
        *,
        output_dir: Path = Path("downloads"),
        connections: int = 8,
        max_parallel: int = 3,
        global_speed_limit_bps: int = 0,
        per_file_speed_limit_bps: int = 0,
        verify_md5: bool = True,
    ) -> None:
        super().__init__()
        self.output_dir = output_dir
        self.connections = connections
        self.max_parallel = max_parallel
        self.global_speed_limit_bps = global_speed_limit_bps
        self.per_file_speed_limit_bps = per_file_speed_limit_bps
        self.verify_md5 = verify_md5

        self.api = OxygenUpdaterApi()
        self.oplus_api = OplusOtaClient()
        self.manager = DownloadManager(
            self.api,
            max_parallel=max_parallel,
            default_connections=connections,
            global_speed_limit_bps=global_speed_limit_bps,
            per_file_speed_limit_bps=per_file_speed_limit_bps,
            on_update=self._on_download_update,
        )

        self._all_devices: list[Device] = []
        self._groups: list[DeviceGroup] = []
        self._selected_group: DeviceGroup | None = None
        self._selected_device: Device | None = None
        self._methods: list[UpdateMethod] = []
        self._selected_method: UpdateMethod | None = None
        self._current_update: UpdateData | None = None

    def compose(self) -> ComposeResult:
        yield Header(show_clock=False)

        with Horizontal(id="main"):
            with Vertical(id="devices-pane"):
                with Horizontal(id="search-bar"):
                    yield Select(
                        [
                            ("OPPO", "OPPO"),
                            ("OnePlus", "OnePlus"),
                            ("realme", "realme"),
                            ("All", "ALL"),
                        ],
                        value="OPPO",
                        allow_blank=False,
                        id="brand-select",
                    )
                    yield Input(
                        placeholder="Search device or model...",
                        id="search-input",
                    )
                yield DataTable(id="device-table", cursor_type="row")

            with Vertical(id="details-pane"):
                yield Static(
                    "Select a device on the left to view its latest Full OTA.",
                    id="ota-summary",
                )
                with Horizontal(id="controls-row"):
                    yield Select(
                        [("Region...", -1)],
                        value=-1,
                        allow_blank=False,
                        id="region-select",
                        classes="hidden",
                    )
                    yield Select(
                        [("Update track...", -1)],
                        value=-1,
                        allow_blank=False,
                        id="method-select",
                    )
                    yield Button(
                        "Download",
                        variant="primary",
                        flat=True,
                        id="btn-download",
                        disabled=True,
                    )
                with VerticalScroll(id="changelog-scroll"):
                    yield Static("", id="changelog-text")

        with Vertical(id="downloads-pane"):
            yield Static(self._downloads_header_text(), id="downloads-header")
            yield DataTable(id="downloads-table", cursor_type="row")

        yield Footer(show_command_palette=False)

    async def on_mount(self) -> None:
        dev_table = self.query_one("#device-table", DataTable)
        dev_table.add_columns("Device", "Model")

        dl_table = self.query_one("#downloads-table", DataTable)
        dl_table.add_column("Model", width=16)
        dl_table.add_column("File", width=30)
        dl_table.add_column("Status", width=22)
        dl_table.add_column("Progress", width=28)
        dl_table.add_column("Speed", width=14)
        dl_table.add_column("ETA", width=8)

        self.query_one("#search-input", Input).focus()
        self._load_devices()

    async def on_unmount(self) -> None:
        await self.manager.aclose()
        await self.api.aclose()
        await self.oplus_api.aclose()

    # ── Device Browsing & Grouping ─────────────────────────────────────

    @work(exclusive=True)
    async def _load_devices(self) -> None:
        summary = self.query_one("#ota-summary", Static)
        summary.update("Loading devices from Oxygen Updater...")
        try:
            self._all_devices = await self.api.fetch_devices("enabled")
            self._apply_device_filter()
            summary.update(
                f"Loaded {len(self._all_devices)} devices. Pick a model on the left."
            )
        except Exception as exc:
            summary.update(f"[red]Failed to load devices:[/red] {exc}")

    def _apply_device_filter(self) -> None:
        brand = str(self.query_one("#brand-select", Select).value)
        query = self.query_one("#search-input", Input).value.strip().lower()

        devices = self._all_devices
        if brand != "ALL":
            devices = [d for d in devices if d.brand == brand]
        if query:
            devices = [
                d
                for d in devices
                if query in d.base_name.lower()
                or query in d.name.lower()
                or query in d.products_str.lower()
            ]

        grouped: OrderedDict[str, DeviceGroup] = OrderedDict()
        for d in devices:
            key = d.base_name
            if key not in grouped:
                grouped[key] = DeviceGroup(base_name=key, brand=d.brand, devices=[])
            grouped[key].devices.append(d)

        self._groups = list(grouped.values())
        table = self.query_one("#device-table", DataTable)
        table.clear()
        for idx, g in enumerate(self._groups):
            table.add_row(g.base_name, g.models_str, key=str(idx))

    @on(Select.Changed, "#brand-select")
    def _on_brand_changed(self) -> None:
        self._apply_device_filter()

    @on(Input.Changed, "#search-input")
    def _on_search_changed(self) -> None:
        self._apply_device_filter()

    @on(Input.Submitted, "#search-input")
    def _on_search_submitted(self) -> None:
        table = self.query_one("#device-table", DataTable)
        if table.row_count > 0:
            table.focus()

    @on(DataTable.RowSelected, "#device-table")
    def _on_group_selected(self, event: DataTable.RowSelected) -> None:
        if event.row_key is None:
            return
        idx = int(str(event.row_key.value))
        if idx < 0 or idx >= len(self._groups):
            return

        group = self._groups[idx]
        self._selected_group = group
        region_select = self.query_one("#region-select", Select)

        if len(group.devices) > 1 or any(d.region for d in group.devices):
            options = [
                (d.region or "Default", d.id) for d in group.devices
            ]
            region_select.set_options(options)
            region_select.value = group.devices[0].id
            region_select.remove_class("hidden")
        else:
            region_select.add_class("hidden")

        self._selected_device = group.devices[0]
        self._load_methods_for_device(group.devices[0])

    @on(Select.Changed, "#region-select")
    def _on_region_changed(self, event: Select.Changed) -> None:
        if self._selected_group is None or event.value in (Select.BLANK, -1):
            return
        dev_id = int(str(event.value))
        device = next(
            (d for d in self._selected_group.devices if d.id == dev_id), None
        )
        if device and device != self._selected_device:
            self._selected_device = device
            self._load_methods_for_device(device)

    @work(exclusive=True)
    async def _load_methods_for_device(self, device: Device) -> None:
        self.query_one("#btn-download", Button).disabled = True
        self.query_one("#ota-summary", Static).update(
            f"Loading OTA info for [b]{device.name}[/b]..."
        )
        try:
            methods = await self.api.fetch_update_methods(device.id)
            self._methods = methods
            select = self.query_one("#method-select", Select)
            if not methods:
                select.set_options([("No tracks available", -1)])
                self.query_one("#ota-summary", Static).update(
                    f"[yellow]No update tracks found for {device.name}.[/yellow]"
                )
                return

            default_m = pick_default_full_method(methods) or methods[0]
            select.set_options([(m.name, m.id) for m in methods])
            select.value = default_m.id
            self._selected_method = default_m
            self._load_update_data(device, default_m)
        except Exception as exc:
            self.query_one("#ota-summary", Static).update(
                f"[red]Error loading tracks:[/red] {exc}"
            )

    @on(Select.Changed, "#method-select")
    def _on_method_changed(self, event: Select.Changed) -> None:
        if self._selected_device is None or event.value in (Select.BLANK, -1):
            return
        method_id = int(str(event.value))
        method = next((m for m in self._methods if m.id == method_id), None)
        if method and method != self._selected_method:
            self._selected_method = method
            self._load_update_data(self._selected_device, method)

    @work(exclusive=True)
    async def _load_update_data(self, device: Device, method: UpdateMethod) -> None:
        summary = self.query_one("#ota-summary", Static)
        changelog = self.query_one("#changelog-text", Static)
        btn = self.query_one("#btn-download", Button)
        btn.disabled = True
        summary.update(f"Fetching [b]{device.name}[/b] ({method.name})...")
        changelog.update("")

        try:
            update = await self.api.fetch_most_recent_update_data(device.id, method.id)
            self._current_update = update
            region_tag = f" [{device.region}]" if device.region else ""
            summary.update(
                f"[b]{device.base_name}{region_tag}[/b]  •  {update.version_number or 'Unknown version'}\n"
                f"OTA: {update.ota_version_number or 'N/A'}  •  "
                f"Size: [b]{format_bytes(update.download_size)}[/b]\n"
                f"File: {update.filename or 'N/A'}"
            )
            changelog.update(
                update.changelog or update.description or "(No changelog)"
            )
            btn.disabled = not bool(update.download_url)
        except Exception as exc:
            summary.update(f"[red]Error fetching OTA:[/red] {exc}")

    # ── Downloads ──────────────────────────────────────────────────────

    @on(Button.Pressed, "#btn-download")
    def _on_download_pressed(self) -> None:
        self.action_queue_download()

    def action_open_direct_oppo(self) -> None:
        def _on_result(params: dict[str, Any] | None) -> None:
            if not params:
                return
            self._run_direct_oppo_query(params)

        self.push_screen(DirectOppoModal(), _on_result)

    @work(exclusive=True)
    async def _run_direct_oppo_query(self, params: dict[str, Any]) -> None:
        summary = self.query_one("#ota-summary", Static)
        changelog = self.query_one("#changelog-text", Static)
        btn = self.query_one("#btn-download", Button)
        btn.disabled = True
        model = params["model"]
        region = params["region"]
        summary.update(f"Querying OPPO {region} server directly for [b]{model}[/b]...")
        changelog.update("")

        try:
            update = await self.oplus_api.query_latest_ota(
                model=model,
                region=region,
                ota_version=params.get("ota_version"),
                coloros_major=params["coloros_major"],
                nv_id=params.get("nv_id"),
            )
            self._selected_device = Device(
                id=0,
                name=f"{model} ({region})",
                product_names=[model],
            )
            self._selected_method = UpdateMethod(id=0, name=f"Direct OPPO ({region})")
            self._current_update = update
            summary.update(
                f"[b]{model} [{region}][/b]  •  {update.version_number or 'Unknown version'}  [dim](Direct OPPO)[/dim]\n"
                f"OTA: {update.ota_version_number or 'N/A'}  •  "
                f"Size: [b]{format_bytes(update.download_size)}[/b]\n"
                f"File: {update.filename or 'N/A'}"
            )
            changelog.update(update.changelog or "(No details)")
            btn.disabled = not bool(update.download_url)
            self.notify(f"Found {update.version_number or model} from OPPO {region}")
        except Exception as exc:
            summary.update(f"[red]Direct OPPO query failed:[/red] {exc}")

    def action_focus_search(self) -> None:
        self.query_one("#search-input", Input).focus()

    def action_queue_download(self) -> None:
        if not (
            self._selected_device
            and self._selected_method
            and self._current_update
            and self._current_update.download_url
        ):
            return
        model_label = ", ".join(self._selected_device.base_models) or self._selected_device.base_name
        if self._selected_device.region:
            model_label = f"{model_label} ({self._selected_device.region})"
        job = self.manager.enqueue(
            device_id=self._selected_device.id,
            device_name=self._selected_device.name,
            method_id=self._selected_method.id,
            method_name=self._selected_method.name,
            update_data=self._current_update,
            output_dir=self.output_dir,
            model_number=model_label,
            connections=self.connections,
            verify_md5=self.verify_md5,
        )
        self._refresh_downloads_table()
        self.notify(f"Started {job.output_path.name}")

    def _on_download_update(self, job: DownloadJob) -> None:
        self._refresh_downloads_table()

    def _refresh_downloads_table(self) -> None:
        table = self.query_one("#downloads-table", DataTable)
        existing_keys = {str(k.value) for k in table.rows}

        for job in self.manager.jobs:
            if job.status == DownloadStatus.DOWNLOADING:
                status = f"Downloading ({job.active_connections}/{job.connections})"
            elif job.status == DownloadStatus.FAILED and job.error:
                status = f"Failed: {job.error[:32]}"
            else:
                status = job.status.value

            progress = (
                f"{job.progress_pct:5.1f}%  ({format_bytes(job.downloaded_bytes)} / {format_bytes(job.total_bytes)})"
                if job.total_bytes > 0
                else f"{format_bytes(job.downloaded_bytes)}"
            )
            speed = (
                format_speed(job.speed_bps)
                if job.status == DownloadStatus.DOWNLOADING or job.speed_bps > 0
                else "-"
            )
            eta = format_eta(job.eta_seconds)

            row_values = (
                job.model_number or job.device_name,
                job.output_path.name,
                status,
                progress,
                speed,
                eta,
            )
            if job.job_id not in existing_keys:
                table.add_row(*row_values, key=job.job_id)
            else:
                col_keys = list(table.columns.keys())
                for col_key, val in zip(col_keys, row_values):
                    table.update_cell(job.job_id, col_key, val, update_width=True)

    def _selected_job_id(self) -> str | None:
        table = self.query_one("#downloads-table", DataTable)
        if table.row_count == 0 or table.cursor_row is None:
            return None
        row_key = table.coordinate_to_cell_key((table.cursor_row, 0)).row_key
        return str(row_key.value) if row_key is not None else None

    def action_toggle_pause(self) -> None:
        job_id = self._selected_job_id()
        if not job_id:
            return
        job = self.manager.get_job(job_id)
        if not job:
            return
        if job.status in (DownloadStatus.PAUSED, DownloadStatus.FAILED):
            self.manager.resume(job_id)
        else:
            self.manager.pause(job_id)

    def _downloads_header_text(self) -> str:
        return (
            f"Downloads  ({self.output_dir}/  •  {self.connections} conns  •  "
            f"{self.max_parallel} parallel  •  "
            f"Global: {format_speed_limit(self.global_speed_limit_bps)}  •  "
            f"Per-file: {format_speed_limit(self.per_file_speed_limit_bps)})"
        )

    def action_open_settings(self) -> None:
        def _apply(result: dict[str, int] | None) -> None:
            if not result:
                return
            self.max_parallel = result["max_parallel"]
            self.connections = result["connections"]
            self.global_speed_limit_bps = result["global_limit_bps"]
            self.per_file_speed_limit_bps = result["per_file_limit_bps"]
            self.query_one("#downloads-header", Static).update(
                self._downloads_header_text()
            )
            self._apply_manager_settings()
            save_settings(
                AppSettings(
                    max_parallel=self.max_parallel,
                    connections=self.connections,
                    global_speed_limit_bps=self.global_speed_limit_bps,
                    per_file_speed_limit_bps=self.per_file_speed_limit_bps,
                    output_dir=str(self.output_dir),
                    verify_md5=self.verify_md5,
                )
            )
            self.notify("Download settings saved")

        self.push_screen(
            SettingsModal(
                max_parallel=self.max_parallel,
                connections=self.connections,
                global_limit_bps=self.global_speed_limit_bps,
                per_file_limit_bps=self.per_file_speed_limit_bps,
            ),
            _apply,
        )

    @work(exclusive=True)
    async def _apply_manager_settings(self) -> None:
        await self.manager.update_settings(
            max_parallel=self.max_parallel,
            default_connections=self.connections,
            global_speed_limit_bps=self.global_speed_limit_bps,
            per_file_speed_limit_bps=self.per_file_speed_limit_bps,
        )

    def action_cancel_download(self) -> None:
        job_id = self._selected_job_id()
        if job_id:
            self.manager.cancel(job_id)
