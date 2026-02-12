"""Markdown-to-DOCX command implementation for `word md`."""

from __future__ import annotations

import os
import ssl
from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib import error as urlerror
from urllib import request as urlrequest
from urllib.parse import urlparse

import typer
from rich import print

try:
    from ._cli_output import error
except ImportError:  # pragma: no cover - direct script execution fallback
    from tools._cli_output import error


CLOUDCONVERT_MARKDOWN_SYNTAX = "github"
CLOUDCONVERT_API_KEY_ENV = "CLOUDCONVERT_API_KEY"


class CloudConvertError(RuntimeError):
    """Raised for CloudConvert conversion failures."""


def register(app: typer.Typer) -> None:
    """Register `md` subcommand on a parent Typer app."""
    app.command("md")(md)


def _step(message: str, enabled: bool = True) -> None:
    if enabled:
        print(f"[bold cyan]md[/bold cyan] {message}")


def _mask_key(key: str) -> str:
    if len(key) < 10:
        return "*" * len(key)
    return f"{key[:6]}...{key[-4:]}"


def _require_cloudconvert_api_key(api_key: Optional[str]) -> str:
    key = (api_key or "").strip()
    if key:
        return key
    raise typer.BadParameter(
        f"Missing CloudConvert API key. Provide --api-key or set {CLOUDCONVERT_API_KEY_ENV}."
    )


def _load_cloudconvert_sdk():
    try:
        import cloudconvert  # type: ignore
    except Exception as exc:
        raise CloudConvertError(
            "The 'cloudconvert' package is required. Install it with: pip install cloudconvert"
        ) from exc
    return cloudconvert


def _get_job_task_id(tasks: List[Dict[str, Any]], task_name: str) -> str:
    for task in tasks:
        if task.get("name") != task_name:
            continue
        task_id = str(task.get("id", "")).strip()
        if task_id:
            return task_id
    raise CloudConvertError(f"CloudConvert did not return task '{task_name}'.")


def _download_file_with_certifi_tls(download_url: str, output_path: Path, verbose: bool) -> None:
    _step("Step 8/9: downloading output DOCX with certifi TLS bundle", verbose)

    try:
        import certifi  # type: ignore
    except Exception as exc:
        raise CloudConvertError(
            "The 'certifi' package is required for secure download verification."
        ) from exc

    output_path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = output_path.with_suffix(output_path.suffix + ".tmp")
    ssl_context = ssl.create_default_context(cafile=certifi.where())
    ok = False

    try:
        req = urlrequest.Request(download_url, method="GET")
        with urlrequest.urlopen(req, timeout=300, context=ssl_context) as response, temp_path.open(
            "wb"
        ) as target:
            while True:
                chunk = response.read(65536)
                if not chunk:
                    break
                target.write(chunk)
        ok = True
    except urlerror.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace").strip()
        raise CloudConvertError(
            f"Download HTTP error ({exc.code}): {body or 'no response body'}"
        ) from exc
    except urlerror.URLError as exc:
        reason = getattr(exc, "reason", exc)
        raise CloudConvertError(
            "Download URL/TLS error. "
            f"Reason: {reason}. certifi bundle: {certifi.where()}"
        ) from exc
    except Exception as exc:
        raise CloudConvertError(f"Download failed: {exc}") from exc
    finally:
        if (not ok) and temp_path.exists():
            try:
                temp_path.unlink()
            except Exception:
                pass

    temp_path.replace(output_path)


def _convert_markdown_to_docx_cloudconvert(input_path: Path, api_key: str, verbose: bool = True) -> Path:
    _step("Step 1/9: loading cloudconvert SDK", verbose)
    cloudconvert = _load_cloudconvert_sdk()

    _step("Step 2/9: configuring cloudconvert client", verbose)
    cloudconvert.configure(api_key=api_key, sandbox=False)

    output_path = input_path.with_suffix(".docx")
    payload: Dict[str, Any] = {
        "tasks": {
            "import-md": {
                "operation": "import/upload",
            },
            "convert-md": {
                "operation": "convert",
                "input": "import-md",
                "input_format": "md",
                "output_format": "docx",
                "input_markdown_syntax": CLOUDCONVERT_MARKDOWN_SYNTAX,
            },
            "export-docx": {
                "operation": "export/url",
                "input": "convert-md",
            },
        }
    }

    _step(
        "Step 3/9: creating CloudConvert job (import/upload -> convert md->docx -> export/url)",
        verbose,
    )
    try:
        job = cloudconvert.Job.create(payload=payload)
    except Exception as exc:
        raise CloudConvertError(f"Failed to create CloudConvert job: {exc}") from exc

    job_id = str(job.get("id", "")).strip()
    if job_id:
        _step(f"Created job id: {job_id}", verbose)

    tasks = job.get("tasks", [])
    if not isinstance(tasks, list):
        raise CloudConvertError("CloudConvert returned an invalid tasks list.")

    _step("Step 4/9: resolving job task ids", verbose)
    import_task_id = _get_job_task_id(tasks, "import-md")
    export_task_id = _get_job_task_id(tasks, "export-docx")
    _step(f"import task id: {import_task_id}", verbose)
    _step(f"export task id: {export_task_id}", verbose)

    _step("Step 5/9: fetching upload task details", verbose)
    try:
        import_task = cloudconvert.Task.find(id=import_task_id)
    except Exception as exc:
        raise CloudConvertError(f"Failed to fetch upload task: {exc}") from exc

    _step(f"Step 6/9: uploading markdown file ({input_path.name})", verbose)
    try:
        uploaded = cloudconvert.Task.upload(file_name=str(input_path), task=import_task)
    except Exception as exc:
        raise CloudConvertError(f"CloudConvert upload failed: {exc}") from exc
    if not uploaded:
        raise CloudConvertError("CloudConvert upload failed.")

    _step("Step 7/9: waiting for conversion/export task to finish", verbose)
    try:
        export_task = cloudconvert.Task.wait(id=export_task_id)
    except Exception as exc:
        raise CloudConvertError(f"CloudConvert conversion failed: {exc}") from exc

    status = str(export_task.get("status", "")).strip()
    if status and status != "finished":
        message = str(export_task.get("message") or "").strip()
        if message:
            raise CloudConvertError(
                f"CloudConvert export task ended with status '{status}': {message}"
            )
        raise CloudConvertError(f"CloudConvert export task ended with status '{status}'.")

    files = export_task.get("result", {}).get("files", [])
    if not isinstance(files, list) or not files:
        raise CloudConvertError("CloudConvert did not return any output file.")

    file_name = str(files[0].get("filename", "")).strip()
    download_url = str(files[0].get("url", "")).strip()
    if not download_url:
        raise CloudConvertError("CloudConvert did not return an export URL.")
    _step(f"CloudConvert output file: {file_name or '(unknown filename)'}", verbose)
    _step(f"Download host: {urlparse(download_url).netloc}", verbose)

    _download_file_with_certifi_tls(download_url, output_path, verbose)
    _step("Step 9/9: conversion finished", verbose)
    return output_path


def md(
    md_path: str = typer.Argument(..., help="Path to Markdown (.md) file to convert"),
    api_key: Optional[str] = typer.Option(
        None,
        "--api-key",
        envvar=CLOUDCONVERT_API_KEY_ENV,
        help=f"CloudConvert API key (or set {CLOUDCONVERT_API_KEY_ENV}).",
    ),
    verbose: bool = typer.Option(
        True,
        "--verbose/--quiet",
        help="Show detailed step-by-step progress for CloudConvert conversion.",
    ),
) -> None:
    """Convert one Markdown file to DOCX using CloudConvert."""
    _step("Step 0/9: validating input file path", verbose)
    input_path = Path(os.path.expanduser(md_path))
    if input_path.suffix.lower() != ".md":
        raise typer.BadParameter(f"Input file must use a .md extension: {input_path}")
    if not input_path.is_file():
        raise typer.BadParameter(f"Markdown file not found: {input_path}")

    _step("Resolving CloudConvert API key from CLI option or environment", verbose)
    resolved_key = _require_cloudconvert_api_key(api_key)
    _step(f"Using API key: {_mask_key(resolved_key)}", verbose)
    print(f"Converting markdown to DOCX: {input_path}")
    try:
        output_path = _convert_markdown_to_docx_cloudconvert(input_path, resolved_key, verbose=verbose)
    except CloudConvertError as exc:
        error(str(exc))
        raise typer.Exit(code=1)

    print(f"[green]Created DOCX[/green]: {output_path}")
