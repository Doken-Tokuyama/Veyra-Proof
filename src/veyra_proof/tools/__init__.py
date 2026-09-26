"""Tool plugins for Veyra Proof."""

from veyra_proof.tools.anti_theater import AntiTheaterTool
from veyra_proof.tools.audit import CargoAuditTool
from veyra_proof.tools.cargo_check import CargoCheckTool
from veyra_proof.tools.cargo_test import CargoTestTool
from veyra_proof.tools.check_external_types import CheckExternalTypesTool
from veyra_proof.tools.clippy import ClippyTool
from veyra_proof.tools.deny import CargoDenyTool
from veyra_proof.tools.dylint import DylintTool
from veyra_proof.tools.fuzz import CargoFuzzTool
from veyra_proof.tools.geiger import CargoGeigerTool
from veyra_proof.tools.kani import KaniTool
from veyra_proof.tools.llvm_cov import LlvmCovTool
from veyra_proof.tools.machete import CargoMacheteTool
from veyra_proof.tools.miri import MiriTool
from veyra_proof.tools.mutants import CargoMutantsTool
from veyra_proof.tools.nextest import NextestTool
from veyra_proof.tools.reproducible_build import ReproducibleBuildTool
from veyra_proof.tools.rustfmt import RustfmtTool
from veyra_proof.tools.sast_optional import CodeQLTool, SemgrepTool
from veyra_proof.tools.semver_checks import SemverChecksTool
from veyra_proof.tools.vet import CargoVetTool

ALL_TOOL_CLASSES = [
    CargoCheckTool,
    RustfmtTool,
    ClippyTool,
    CargoTestTool,
    AntiTheaterTool,
    NextestTool,
    LlvmCovTool,
    CargoAuditTool,
    CargoDenyTool,
    CargoGeigerTool,
    CargoMacheteTool,
    DylintTool,
    SemverChecksTool,
    MiriTool,
    CargoMutantsTool,
    CargoFuzzTool,
    KaniTool,
    CargoVetTool,
    CheckExternalTypesTool,
    ReproducibleBuildTool,
    SemgrepTool,
    CodeQLTool,
]

__all__ = ["ALL_TOOL_CLASSES"]
