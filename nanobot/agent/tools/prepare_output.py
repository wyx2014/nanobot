"""Allocate a final deliverable filename and an isolated scratch directory."""

from __future__ import annotations

from typing import Any

from nanobot.agent.tools.base import tool_parameters
from nanobot.agent.tools.filesystem import _FsTool
from nanobot.agent.tools.schema import StringSchema, tool_parameters_schema
from nanobot.utils.output_paths import prepare_output_paths


@tool_parameters(tool_parameters_schema(
    filename=StringSchema(
        "Business filename with extension, e.g. 周报.docx or 持仓分析.xlsx. "
        "The runtime adds YYYYMMDDHH and v2/v3 for same-hour versions.", min_length=1,
    ),
    directory=StringSchema(
        "Final output directory, relative to the current workspace. Defaults to the workspace root. "
        "Use an external directory only when explicitly requested by the user.", nullable=True,
    ),
    required=["filename"],
))
class PrepareOutputTool(_FsTool):
    _scopes = {"core", "subagent"}

    @property
    def name(self) -> str:
        return "prepare_output"

    @property
    def description(self) -> str:
        return (
            "Before generating or revising a deliverable, reserve its versioned output path "
            "and get a workspace tmp directory for scripts, drafts, data and validation files. "
            "Use the returned output_path exactly with create_docx/create_pdf or your generator. "
            "Never overwrite previous versions. This prepares paths; it does not generate a file."
        )

    async def execute(self, filename: str, directory: str | None = None, **kwargs: Any):
        try:
            workspace = self._display_workspace()
            if workspace is None:
                return "Error: a workspace is required to prepare an output"
            # Both writes go through the same workspace security policy as file tools.
            destination = self._resolve_write(directory or ".")
            self._resolve_write("tmp/.output-reservations")
            result = prepare_output_paths(
                workspace, destination, filename, timezone=getattr(self, "_timezone", None),
            )
            return {
                **result,
                "next_step": (
                    "Put generation code, drafts, intermediate data and validation files in tmp_dir. "
                    "Write only the validated final deliverable to output_path. Reuse these paths "
                    "while preparing this version; call prepare_output again for a new revision. "
                    "After success, summarize the actual changes and provide a clickable link "
                    "to the generated file. Do not claim success before the file exists and is checked."
                ),
            }
        except (OSError, ValueError) as exc:
            return f"Error preparing output: {exc}"
