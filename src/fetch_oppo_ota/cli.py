"""CLI entrypoint for fetch-ota (launches TUI by default, or headless subcommands)."""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

from rich.console import Console
from rich.progress import (
    BarColumn,
    DownloadColumn,
    Progress,
    TaskID,
    TextColumn,
    TimeRemainingColumn,
    TransferSpeedColumn,
)
from rich.table import Table

from .api import OxygenUpdaterApi, pick_default_full_method
from .config import load_settings
from .downloader import DownloadManager
from .models import DownloadJob, DownloadStatus, format_bytes, parse_speed_limit
from .oplus_api import OplusOtaClient
from .tui import FetchOtaApp

console = Console()


async def _cmd_list(brand: str, query: str | None, as_json: bool) -> int:
    async with OxygenUpdaterApi() as api:
        devices = await api.fetch_devices("enabled")

    if brand.lower() != "all":
        devices = [d for d in devices if d.brand.lower() == brand.lower()]
    if query:
        q = query.lower()
        devices = [
            d
            for d in devices
            if q in d.name.lower() or q in d.products_str.lower()
        ]

    if as_json:
        print(
            json.dumps(
                [
                    {
                        "id": d.id,
                        "name": d.name,
                        "brand": d.brand,
                        "product_names": d.product_names,
                    }
                    for d in devices
                ],
                indent=2,
            )
        )
        return 0

    table = Table(title=f"Supported Devices ({len(devices)})")
    table.add_column("ID", style="cyan", justify="right")
    table.add_column("Brand", style="magenta")
    table.add_column("Device", style="bold")
    table.add_column("Models", style="dim")
    for d in devices:
        table.add_row(str(d.id), d.brand, d.name, d.products_str)
    console.print(table)
    return 0


async def _cmd_info(device_id: int, method_id: int | None, as_json: bool) -> int:
    async with OxygenUpdaterApi() as api:
        methods = await api.fetch_update_methods(device_id)
        if not methods:
            console.print(f"[red]No update methods found for device {device_id}[/red]")
            return 1
        if method_id is not None:
            method = next((m for m in methods if m.id == method_id), None)
            if not method:
                console.print(f"[red]Method {method_id} not found[/red]")
                return 1
        else:
            method = pick_default_full_method(methods) or methods[0]

        update = await api.fetch_most_recent_update_data(device_id, method.id)
        fresh_url = (
            await api.get_fresh_download_url(device_id, method.id, update)
            if update.download_url
            else None
        )

    if as_json:
        print(
            json.dumps(
                {
                    "device_id": device_id,
                    "method": {"id": method.id, "name": method.name},
                    "version_number": update.version_number,
                    "ota_version_number": update.ota_version_number,
                    "filename": update.filename,
                    "download_size": update.download_size,
                    "md5sum": update.md5sum,
                    "download_url": fresh_url or update.download_url,
                    "changelog": update.changelog,
                },
                indent=2,
            )
        )
        return 0

    console.print(f"[bold]Device ID:[/bold]    {device_id}")
    console.print(f"[bold]Method:[/bold]       {method.name} (id={method.id})")
    console.print(f"[bold]Version:[/bold]      {update.version_number}")
    console.print(f"[bold]OTA Version:[/bold]  {update.ota_version_number}")
    console.print(f"[bold]Filename:[/bold]     {update.filename}")
    console.print(f"[bold]Size:[/bold]         {format_bytes(update.download_size)}")
    console.print(f"[bold]MD5:[/bold]          {update.md5sum}")
    console.print(f"[bold]Download URL:[/bold] {fresh_url or update.download_url}")
    if update.changelog:
        console.print(f"\n[bold]Changelog:[/bold]\n{update.changelog}")
    return 0


async def _cmd_download(
    device_ids: list[int],
    method_id: int | None,
    output_dir: Path,
    connections: int,
    max_parallel: int,
    global_limit_bps: int,
    file_limit_bps: int,
    verify_md5: bool,
) -> int:
    progress = Progress(
        TextColumn("[bold blue]{task.fields[filename]}", justify="right"),
        BarColumn(bar_width=None),
        "[progress.percentage]{task.percentage:>5.1f}%",
        "•",
        DownloadColumn(),
        "•",
        TransferSpeedColumn(),
        "•",
        TimeRemainingColumn(),
        TextColumn("[dim]{task.fields[status]}[/dim]"),
        console=console,
    )
    task_map: dict[str, TaskID] = {}

    def on_update(job: DownloadJob) -> None:
        tid = task_map.get(job.job_id)
        if tid is None:
            return
        status_text = (
            f"{job.status.value} ({job.active_connections}/{job.connections})"
            if job.status == DownloadStatus.DOWNLOADING
            else job.status.value
        )
        progress.update(
            tid,
            total=job.total_bytes or None,
            completed=job.downloaded_bytes,
            status=status_text,
        )

    async with OxygenUpdaterApi() as api:
        manager = DownloadManager(
            api,
            max_parallel=max_parallel,
            default_connections=connections,
            global_speed_limit_bps=global_limit_bps,
            per_file_speed_limit_bps=file_limit_bps,
            on_update=on_update,
        )
        try:
            with progress:
                for dev_id in device_ids:
                    methods = await api.fetch_update_methods(dev_id)
                    if not methods:
                        console.print(
                            f"[yellow]Skipping device {dev_id}: no update methods[/yellow]"
                        )
                        continue
                    method = (
                        next((m for m in methods if m.id == method_id), None)
                        if method_id is not None
                        else pick_default_full_method(methods)
                    ) or methods[0]
                    update = await api.fetch_most_recent_update_data(dev_id, method.id)
                    if not update.download_url:
                        console.print(
                            f"[yellow]Skipping device {dev_id}: no download URL[/yellow]"
                        )
                        continue
                    job = manager.enqueue(
                        device_id=dev_id,
                        device_name=f"Device {dev_id}",
                        method_id=method.id,
                        method_name=method.name,
                        update_data=update,
                        output_dir=output_dir,
                        connections=connections,
                        verify_md5=verify_md5,
                    )
                    task_map[job.job_id] = progress.add_task(
                        job.job_id,
                        filename=job.output_path.name,
                        total=job.total_bytes or None,
                        status=job.status.value,
                    )

                await manager.wait_all()

            failed = [j for j in manager.jobs if j.status == DownloadStatus.FAILED]
            for j in failed:
                console.print(f"[red]Failed {j.output_path.name}: {j.error}[/red]")
            return 1 if failed else 0
        finally:
            await manager.aclose()


async def _cmd_direct(
    *,
    model: str,
    region: str,
    ota_version: str | None,
    coloros_major: int,
    nv_id: str | None,
    do_download: bool,
    output_dir: Path,
    connections: int,
    max_parallel: int,
    global_limit_bps: int,
    file_limit_bps: int,
    verify_md5: bool,
) -> int:
    async with OplusOtaClient() as client:
        update = await client.query_latest_ota(
            model=model,
            region=region,
            ota_version=ota_version,
            coloros_major=coloros_major,
            nv_id=nv_id,
        )

    console.print(f"[bold]Model:[/bold]        {model.upper()} ({region.upper()})")
    console.print(f"[bold]Version:[/bold]      {update.version_number}")
    console.print(f"[bold]OTA Version:[/bold]  {update.ota_version_number}")
    console.print(f"[bold]Size:[/bold]         {format_bytes(update.download_size)}")
    console.print(f"[bold]MD5:[/bold]          {update.md5sum}")
    console.print(f"[bold]Download URL:[/bold] {update.download_url}")
    if update.changelog:
        console.print(f"\n[bold]Info:[/bold]\n{update.changelog}")

    if not do_download:
        return 0

    progress = Progress(
        TextColumn("[bold blue]{task.fields[filename]}", justify="right"),
        BarColumn(bar_width=None),
        "[progress.percentage]{task.percentage:>5.1f}%",
        "•",
        DownloadColumn(),
        "•",
        TransferSpeedColumn(),
        "•",
        TimeRemainingColumn(),
        TextColumn("[dim]{task.fields[status]}[/dim]"),
        console=console,
    )
    task_map: dict[str, TaskID] = {}

    def on_update(job: DownloadJob) -> None:
        tid = task_map.get(job.job_id)
        if tid is None:
            return
        status_text = (
            f"{job.status.value} ({job.active_connections}/{job.connections})"
            if job.status == DownloadStatus.DOWNLOADING
            else job.status.value
        )
        progress.update(
            tid,
            total=job.total_bytes or None,
            completed=job.downloaded_bytes,
            status=status_text,
        )

    async with OxygenUpdaterApi() as api:
        manager = DownloadManager(
            api,
            max_parallel=max_parallel,
            default_connections=connections,
            global_speed_limit_bps=global_limit_bps,
            per_file_speed_limit_bps=file_limit_bps,
            on_update=on_update,
        )
        try:
            with progress:
                job = manager.enqueue(
                    device_id=0,
                    device_name=f"{model.upper()} ({region.upper()})",
                    method_id=0,
                    method_name=f"Direct OPPO ({region.upper()})",
                    update_data=update,
                    output_dir=output_dir,
                    model_number=f"{model.upper()} ({region.upper()})",
                    connections=connections,
                    verify_md5=verify_md5,
                )
                task_map[job.job_id] = progress.add_task(
                    job.job_id,
                    filename=job.output_path.name,
                    total=job.total_bytes or None,
                    status=job.status.value,
                )
                await manager.wait_all()
            if job.status == DownloadStatus.FAILED:
                console.print(f"[red]Failed: {job.error}[/red]")
                return 1
            return 0
        finally:
            await manager.aclose()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="fetch-oppo-ota",
        description="OPPO / OnePlus / realme OTA downloader with TUI, multi-connection, resume, and parallel downloads.",
    )
    parser.add_argument(
        "-o",
        "--output-dir",
        type=Path,
        default=None,
        help="Download directory (default: saved setting or ./downloads)",
    )
    parser.add_argument(
        "-c",
        "--connections",
        type=int,
        default=None,
        help="Connections per file for segmented download (default: saved setting or 8)",
    )
    parser.add_argument(
        "-p",
        "--parallel",
        type=int,
        default=None,
        help="Max concurrent OTA downloads (default: saved setting or 3)",
    )
    parser.add_argument(
        "--global-limit",
        default=None,
        help="Global speed limit (e.g. 10M, 500K, 0=unlimited)",
    )
    parser.add_argument(
        "--file-limit",
        default=None,
        help="Per-file speed limit (e.g. 5M, 500K, 0=unlimited)",
    )
    parser.add_argument(
        "--no-verify-md5",
        action="store_true",
        help="Disable MD5 verification after download",
    )

    sub = parser.add_subparsers(dest="command")

    p_list = sub.add_parser("list", help="List supported devices")
    p_list.add_argument(
        "--brand",
        default="oppo",
        choices=["oppo", "oneplus", "realme", "all"],
        help="Filter by brand (default: oppo)",
    )
    p_list.add_argument("-q", "--query", help="Search substring in name or model")
    p_list.add_argument("--json", action="store_true", help="Output JSON")

    p_info = sub.add_parser("info", help="Show latest Full OTA metadata for a device")
    p_info.add_argument("--device-id", type=int, required=True)
    p_info.add_argument("--method-id", type=int)
    p_info.add_argument("--json", action="store_true", help="Output JSON")

    p_dl = sub.add_parser(
        "download",
        help="Headless parallel multi-connection download for one or more device IDs",
    )
    p_dl.add_argument(
        "--device-id",
        type=int,
        action="append",
        required=True,
        help="Device ID to download (repeatable for parallel downloads)",
    )
    p_dl.add_argument("--method-id", type=int, help="Optional explicit method ID")

    p_direct = sub.add_parser(
        "direct",
        help="Query OPPO's official update/v3 server directly (no Oxygen Updater)",
    )
    p_direct.add_argument("--model", required=True, help="Device model (e.g. CPH2791)")
    p_direct.add_argument(
        "--region",
        default="GL",
        choices=["GL", "EU", "IN", "CN"],
        help="Server region (default: GL)",
    )
    p_direct.add_argument("--ota-version", help="Optional base OTA version string")
    p_direct.add_argument(
        "--nv-id",
        help="Optional NV carrier ID (e.g. 00110011 for ID, 01000100 for EU)",
    )
    p_direct.add_argument(
        "--coloros",
        type=int,
        default=15,
        help="ColorOS major version 11-16 (default: 15)",
    )
    p_direct.add_argument(
        "--download",
        action="store_true",
        help="Download the OTA immediately after querying",
    )

    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()

    saved = load_settings()
    output_dir = args.output_dir if args.output_dir is not None else Path(saved.output_dir)
    connections = args.connections if args.connections is not None else saved.connections
    max_parallel = args.parallel if args.parallel is not None else saved.max_parallel
    global_limit_bps = (
        parse_speed_limit(args.global_limit)
        if args.global_limit is not None
        else saved.global_speed_limit_bps
    )
    file_limit_bps = (
        parse_speed_limit(args.file_limit)
        if args.file_limit is not None
        else saved.per_file_speed_limit_bps
    )
    verify_md5 = False if args.no_verify_md5 else saved.verify_md5

    if args.command is None:
        app = FetchOtaApp(
            output_dir=output_dir,
            connections=connections,
            max_parallel=max_parallel,
            global_speed_limit_bps=global_limit_bps,
            per_file_speed_limit_bps=file_limit_bps,
            verify_md5=verify_md5,
        )
        app.run()
        return 0

    if args.command == "list":
        return asyncio.run(_cmd_list(args.brand, args.query, args.json))
    if args.command == "info":
        return asyncio.run(_cmd_info(args.device_id, args.method_id, args.json))
    if args.command == "download":
        return asyncio.run(
            _cmd_download(
                device_ids=args.device_id,
                method_id=args.method_id,
                output_dir=output_dir,
                connections=connections,
                max_parallel=max_parallel,
                global_limit_bps=global_limit_bps,
                file_limit_bps=file_limit_bps,
                verify_md5=verify_md5,
            )
        )
    if args.command == "direct":
        return asyncio.run(
            _cmd_direct(
                model=args.model,
                region=args.region,
                ota_version=args.ota_version,
                coloros_major=args.coloros,
                nv_id=args.nv_id,
                do_download=args.download,
                output_dir=output_dir,
                connections=connections,
                max_parallel=max_parallel,
                global_limit_bps=global_limit_bps,
                file_limit_bps=file_limit_bps,
                verify_md5=verify_md5,
            )
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
