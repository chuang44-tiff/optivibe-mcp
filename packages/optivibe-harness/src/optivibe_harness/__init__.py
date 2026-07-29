"""optivibe_harness — ZOS-API automation, typed tools, MCP server, and agent.

The harness drives Zemax OpticStudio through the typed ZOS-API (.NET, via
pythonnet / ``clr``); the typed API surface is the syntax-safety layer.

This package currently ships the infrastructure tier: the durability
substrate (``_io``), trial statistics (``mc_stats``), the interaction log, the
optimization journal, and the artifact snapshot sink. These are pure-Python,
backend-free, and stdlib-only.
"""

from ._io import (
    FSYNC_ENABLED,
    append_line_fsync,
    atomic_write_bytes,
    safe_repr,
    utc_now_iso,
)
from .errors import (
    AnalysisResultError,
    HarnessError,
    SessionClosedError,
    OptimizeError,
    SessionConnectError,
    SessionError,
    SurfaceWriteError,
    ToolError,
    ToolParamError,
    UnknownToolError,
    map_dotnet_exception,
)
from .enums import _resolve_enum, MERIT_OPERAND_TYPE_MEMBERS
from .process_reaper import (
    ENGINE_PROCESS_NAME,
    ProcInfo,
    ReapResult,
    diff_spawned,
    poll_pids_gone,
    snapshot_engine_pids,
    terminate_tracked,
)
from .server import (
    Dispatcher,
    ToolSpec,
)
from .session import ZemaxSession
from .tools.get_system_info import get_system_info
from .tools.lens_surface import (
    insert_surface,
    read_surface,
    remove_surface,
    set_stop_surface,
    set_surface,
    surface_count,
)
from .tools.lens_system import set_aperture, set_field, set_wavelength
from .tools.lens_glass import list_glass_catalog, substitute_glass
from .tools.lens_spec import (
    LensSpec,
    SurfaceSpec,
    apply_lens_spec,
    read_lens_spec,
)
from .tools.save_snapshot import save_snapshot
from .tools.analysis_mtf import get_mtf
from .tools.analysis_spot import get_spot
from .tools.analysis_raytrace import trace_rays
from .tools.analysis_operand import get_operand
from .tools.analysis_graphic import capture_graphic
from .tools.optimize_variable import clear_variable, set_variable
from .tools.optimize_merit import add_operand, build_merit, dump_merit_function
from .tools.optimize_run import dry_run, optimize
from .artifact_sink import (
    ArtifactSink,
    RunIdCollisionError,
    SaveAsFn,
    SnapshotResult,
)
from .interaction_log import (
    InteractionLog,
    InteractionRecord,
)
from .interaction_log import (
    load as load_interaction_log,
)
from .journal import (
    Journal,
    JournalEntry,
    JournalSchemaError,
    MeritSnapshot,
)
from .journal import (
    load as load_journal,
)
from .mc_stats import (
    TrialSummary,
    WilsonInterval,
    percentile,
    percentile_rank,
    wilson_interval,
)

__version__ = "0.1.2"

__all__ = [
    "__version__",
    # _io
    "FSYNC_ENABLED",
    "utc_now_iso",
    "safe_repr",
    "atomic_write_bytes",
    "append_line_fsync",
    # mc_stats
    "WilsonInterval",
    "TrialSummary",
    "wilson_interval",
    "percentile",
    "percentile_rank",
    # interaction_log
    "InteractionRecord",
    "InteractionLog",
    "load_interaction_log",
    # journal
    "MeritSnapshot",
    "JournalEntry",
    "JournalSchemaError",
    "Journal",
    "load_journal",
    # artifact_sink
    "RunIdCollisionError",
    "SaveAsFn",
    "SnapshotResult",
    "ArtifactSink",
    # errors
    "HarnessError",
    "SessionError",
    "SessionClosedError",
    "SessionConnectError",
    "ToolError",
    "UnknownToolError",
    "ToolParamError",
    "SurfaceWriteError",
    "AnalysisResultError",
    "OptimizeError",
    "map_dotnet_exception",
    # enums
    "_resolve_enum",
    # enums
    "MERIT_OPERAND_TYPE_MEMBERS",
    # process_reaper
    "ENGINE_PROCESS_NAME",
    "ProcInfo",
    "ReapResult",
    "snapshot_engine_pids",
    "diff_spawned",
    "poll_pids_gone",
    "terminate_tracked",
    # session
    "ZemaxSession",
    # server / dispatch
    "ToolSpec",
    "Dispatcher",
    # tools
    "get_system_info",
    # tools (lens)
    "read_surface",
    "surface_count",
    "set_surface",
    "insert_surface",
    "remove_surface",
    "set_stop_surface",
    "set_field",
    "set_wavelength",
    "set_aperture",
    "substitute_glass",
    "list_glass_catalog",
    "save_snapshot",
    # tools (analysis)
    "get_mtf",
    "get_spot",
    "trace_rays",
    "get_operand",
    "capture_graphic",
    # tools (optimize)
    "set_variable",
    "clear_variable",
    "build_merit",
    "add_operand",
    "dump_merit_function",
    "dry_run",
    "optimize",
    # lens_spec
    "SurfaceSpec",
    "LensSpec",
    "read_lens_spec",
    "apply_lens_spec",
]
