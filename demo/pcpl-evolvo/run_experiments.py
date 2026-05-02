#!/usr/bin/env python3
"""Run PCPL evolvo experiments (single-run or continuous resumable mode)."""

from __future__ import annotations

import argparse
import concurrent.futures
import dataclasses
import itertools
import json
import math
import multiprocessing
import os
import random
import shutil
import subprocess
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
import sys

from config import (
    DEFAULT_MODE,
    DEFAULT_PROFILE,
    available_modes,
    mode_summary,
    resolve_defaults,
)

PROJECT_DIR = Path(__file__).resolve().parent
SRC_DIR = PROJECT_DIR / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))
try:
    # Ensure child-process progress logs flush line-by-line in continuous runs.
    sys.stdout.reconfigure(line_buffering=True)  # type: ignore[attr-defined]
    sys.stderr.reconfigure(line_buffering=True)  # type: ignore[attr-defined]
except Exception:
    pass

from pcpl_evolvo.experiment import (
    ExperimentConfig,
    materialize_existing_run_views,
    run_experiment,
)

DEFAULT_FITNESS_SCHEMA_VERSION = "auto"
EXPERIMENT_SUITE_CHOICES = ("single", "precision")
PRECISION_TRACK_CHOICES = (
    "baseline",
    "supervisor",
    "lane-pressure",
    "evaluability",
    "random-research",
)

_KOMPUTE_SELFTEST_SHADER_SOURCE = """#version 450
layout(local_size_x = 1, local_size_y = 1, local_size_z = 1) in;
layout(binding = 0) buffer BufA { float a[]; };
layout(binding = 1) buffer BufB { float b[]; };
layout(binding = 2) buffer BufOut { float outv[]; };
void main() {
    uint i = gl_GlobalInvocationID.x;
    outv[i] = a[i] + b[i];
}
"""


def _safe_int(value: int, minimum: int = 1) -> int:
    return max(minimum, int(value))


def _detect_kompute_glsl_compiler() -> Optional[str]:
    env_path = os.environ.get("EVOLVO_GLSL_COMPILER", "").strip()
    if env_path:
        expanded = os.path.expanduser(env_path)
        if os.path.isfile(expanded) and os.access(expanded, os.X_OK):
            return expanded
    for candidate in ("glslangValidator", "glslc"):
        resolved = shutil.which(candidate)
        if resolved:
            return resolved
    return None


def _env_int(name: str) -> Optional[int]:
    raw = os.environ.get(name)
    if raw is None:
        return None
    text = str(raw).strip()
    if not text:
        return None
    try:
        return int(text)
    except ValueError as exc:
        raise RuntimeError(f"Invalid integer env {name}={raw!r}") from exc


def _vulkan_device_sort_key(index: int, descriptor: Any) -> Tuple[int, int, int]:
    name = ""
    if isinstance(descriptor, dict):
        name = str(descriptor.get("device_name", ""))
    elif descriptor is not None:
        name = str(descriptor)
    name_lc = name.strip().lower()
    is_software = any(
        token in name_lc for token in ("llvmpipe", "lavapipe", "swiftshader", "software")
    )
    looks_amd = any(token in name_lc for token in ("amd", "radeon", "navi", "gfx"))
    # Prefer hardware accelerators over software ICDs; among hardware devices, favor AMD names.
    return (1 if is_software else 0, 0 if looks_amd else 1, int(index))


def _selftest_candidate_device_indices(kp_module: Any) -> List[int]:
    configured = _env_int("EVOLVO_KOMPUTE_DEVICE_INDEX")
    if configured is not None:
        return [max(0, int(configured))]
    try:
        probe_manager = kp_module.Manager(0)
        listed = probe_manager.list_devices()
        if isinstance(listed, list) and listed:
            ranked = sorted(
                list(enumerate(listed)),
                key=lambda item: _vulkan_device_sort_key(int(item[0]), item[1]),
            )
            return [int(idx) for idx, _item in ranked]
    except Exception:
        pass
    return [0, 1, 2, 3]


def _selftest_candidate_queue_families() -> List[Optional[int]]:
    configured = _env_int("EVOLVO_KOMPUTE_QUEUE_FAMILY")
    if configured is not None:
        return [max(0, int(configured))]
    # Try the binding default first, then explicit queue family 0.
    return [None, 0]


def _kp_sync_ops(kp_module: Any) -> Tuple[Optional[Any], Optional[Any]]:
    sync_device = getattr(kp_module, "OpSyncDevice", None)
    sync_local = getattr(kp_module, "OpSyncLocal", None)
    if sync_device is None:
        sync_device = getattr(kp_module, "OpTensorSyncDevice", None)
    if sync_local is None:
        sync_local = getattr(kp_module, "OpTensorSyncLocal", None)
    return sync_device, sync_local


def _kp_shared_memory_type(kp_module: Any) -> Optional[Any]:
    memory_types = getattr(kp_module, "MemoryTypes", None)
    if memory_types is not None and hasattr(memory_types, "deviceAndHost"):
        return memory_types.deviceAndHost
    if memory_types is not None and hasattr(memory_types, "host"):
        return memory_types.host
    if hasattr(kp_module, "deviceAndHost"):
        return getattr(kp_module, "deviceAndHost")
    if hasattr(kp_module, "host"):
        return getattr(kp_module, "host")
    return None


def _probe_manager_sync(kp_module: Any, manager: Any) -> None:
    import numpy as np

    sync_device_op, sync_local_op = _kp_sync_ops(kp_module)
    if sync_device_op is not None and sync_local_op is not None:
        tensor = manager.tensor(np.array([1.0], dtype=np.float32))
        sequence = manager.sequence()
        sequence.record(sync_device_op([tensor]))
        sequence.record(sync_local_op([tensor]))
        sequence.eval()
        _ = float(tensor.data()[0])
        return

    shared_memory_type = _kp_shared_memory_type(kp_module)
    if shared_memory_type is None:
        # No sync API and no shared tensors: manager creation itself is the best lightweight probe.
        return
    tensor = manager.tensor(np.array([1.0], dtype=np.float32), shared_memory_type)
    # Avoid empty sequence eval() here: on some AMD Vulkan stacks this can stall indefinitely.
    _ = float(tensor.data()[0])


def _create_selftest_manager(kp_module: Any) -> Tuple[Any, int, Optional[int], str]:
    errors: List[str] = []
    for device_index in _selftest_candidate_device_indices(kp_module):
        for queue_family in _selftest_candidate_queue_families():
            try:
                if queue_family is None:
                    manager = kp_module.Manager(int(device_index))
                else:
                    manager = kp_module.Manager(
                        device=int(device_index),
                        family_queue_indices=[int(queue_family)],
                        desired_extensions=[],
                    )
                _probe_manager_sync(kp_module, manager)
                props = manager.get_device_properties()
                device_name = str(props.get("device_name", "unknown"))
                return manager, int(device_index), queue_family, device_name
            except Exception as exc:
                errors.append(
                    "device={device} queue_family={queue} -> {err}".format(
                        device=int(device_index),
                        queue=("default" if queue_family is None else int(queue_family)),
                        err=str(exc),
                    )
                )
    tail = "; ".join(errors[-4:]) if errors else "no attempts"
    raise RuntimeError(
        "unable to initialize Vulkan manager. "
        "Run `--kompute-check-libs` and set EVOLVO_KOMPUTE_DEVICE_INDEX and/or EVOLVO_KOMPUTE_QUEUE_FAMILY. "
        f"recent attempts: {tail}"
    )


def _compile_kompute_selftest_spirv(compiler_path: str) -> bytes:
    compiler_name = os.path.basename(compiler_path)
    with tempfile.TemporaryDirectory(prefix="pcpl-kompute-selftest-") as tmp_dir:
        src_path = os.path.join(tmp_dir, "selftest.comp")
        spv_path = os.path.join(tmp_dir, "selftest.spv")
        with open(src_path, "w", encoding="utf-8") as handle:
            handle.write(_KOMPUTE_SELFTEST_SHADER_SOURCE)
        if compiler_name == "glslc":
            cmd = [
                compiler_path,
                src_path,
                "-o",
                spv_path,
                "-fshader-stage=compute",
            ]
        else:
            cmd = [
                compiler_path,
                "-V",
                "-S",
                "comp",
                src_path,
                "-o",
                spv_path,
            ]
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            check=False,
        )
        if proc.returncode != 0:
            detail = (proc.stderr or "").strip() or (proc.stdout or "").strip() or "unknown compiler error"
            raise RuntimeError(f"GLSL->SPIR-V compilation failed: {detail}")
        with open(spv_path, "rb") as handle:
            return handle.read()


def _run_kompute_self_test() -> bool:
    print("[pcpl-evolvo][kompute-self-test] starting")
    try:
        import numpy as np
        import kp  # type: ignore
    except Exception as exc:
        print(f"[pcpl-evolvo][kompute-self-test] FAILED: cannot import kp/numpy ({exc})")
        return False

    compiler_path = _detect_kompute_glsl_compiler()
    if not compiler_path:
        print(
            "[pcpl-evolvo][kompute-self-test] FAILED: no GLSL compiler found "
            "(`glslangValidator` or `glslc`)."
        )
        return False

    kp_version = getattr(kp, "__version__", "unknown")
    print(f"[pcpl-evolvo][kompute-self-test] kp={kp_version} compiler={compiler_path}")
    try:
        spirv = _compile_kompute_selftest_spirv(compiler_path)
    except Exception as exc:
        print(f"[pcpl-evolvo][kompute-self-test] FAILED: shader compile error ({exc})")
        return False

    try:
        manager, device_index, queue_family, device_name = _create_selftest_manager(kp)
        queue_label = "default" if queue_family is None else str(int(queue_family))
        sync_device_op, sync_local_op = _kp_sync_ops(kp)
        use_explicit_sync = bool(sync_device_op is not None and sync_local_op is not None)
        tensor_memory_type = _kp_shared_memory_type(kp) if not use_explicit_sync else None
        api_mode = "explicit-sync" if use_explicit_sync else "shared-memory"
        if not use_explicit_sync and tensor_memory_type is None:
            raise RuntimeError(
                "kp build has no sync ops (OpSyncDevice/OpSyncLocal or "
                "OpTensorSyncDevice/OpTensorSyncLocal) and no shared memory type; "
                "cannot run raw dispatch self-test."
            )
        print(
            "[pcpl-evolvo][kompute-self-test] manager device_index={device} queue_family={queue} device_name={name} api_mode={mode}".format(
                device=int(device_index),
                queue=queue_label,
                name=device_name,
                mode=api_mode,
            )
        )
        if use_explicit_sync:
            a = manager.tensor(np.array([1.5], dtype=np.float32))
            b = manager.tensor(np.array([2.0], dtype=np.float32))
            out = manager.tensor(np.array([0.0], dtype=np.float32))
        else:
            a = manager.tensor(np.array([1.5], dtype=np.float32), tensor_memory_type)
            b = manager.tensor(np.array([2.0], dtype=np.float32), tensor_memory_type)
            out = manager.tensor(np.array([0.0], dtype=np.float32), tensor_memory_type)
        algorithm = manager.algorithm([a, b, out], spirv, [1, 1, 1], [], [])
        sequence = manager.sequence()
        if use_explicit_sync:
            sequence.record(sync_device_op([a, b, out]))  # type: ignore[misc]
        sequence.record(kp.OpAlgoDispatch(algorithm))
        if use_explicit_sync:
            sequence.record(sync_local_op([out]))  # type: ignore[misc]
        sequence.eval()
        native_out = float(out.data()[0])
        if abs(native_out - 3.5) > 1e-5:
            raise RuntimeError(f"unexpected raw kp result {native_out:.8f} (expected 3.5)")
        print(
            "[pcpl-evolvo][kompute-self-test] raw-kp-dispatch=ok result={:.6f}".format(
                native_out
            )
        )
    except Exception as exc:
        print(f"[pcpl-evolvo][kompute-self-test] FAILED: raw kp dispatch failed ({exc})")
        return False

    try:
        from pcpl_evolvo.bootstrap import ensure_evolvo_importable

        ensure_evolvo_importable()
        from evolvo import (
            Category,
            DataType,
            GFSLExecutor,
            GFSLGenome,
            GFSLInstruction,
            Operation,
            pack_type_index,
        )

        instruction = GFSLInstruction(
            [
                int(Category.VARIABLE),
                pack_type_index(DataType.DECIMAL, 0),
                int(Operation.ADD),
                int(Category.VARIABLE),
                pack_type_index(DataType.DECIMAL, 1),
                int(Category.VARIABLE),
                pack_type_index(DataType.DECIMAL, 2),
            ]
        )
        genome = GFSLGenome()
        genome.instructions = [instruction]
        genome.outputs = [(Category.VARIABLE, DataType.DECIMAL, 0)]
        executor = GFSLExecutor(
            compute_backend="kompute",
            kompute_runtime_mode="native",
            kompute_fail_hard=True,
            kompute_warn_on_fallback=True,
        )
        outputs = executor.execute(
            genome,
            inputs={"d$1": 4.0, "d$2": 5.0},
        )
        result = float(outputs.get("d$0", 0.0))
        if abs(result - 9.0) > 1e-5:
            raise RuntimeError(f"unexpected GFSL native result {result:.8f} (expected 9.0)")
        print(
            "[pcpl-evolvo][kompute-self-test] evolvo-native=ok result={:.6f}".format(
                result
            )
        )
    except Exception as exc:
        print(f"[pcpl-evolvo][kompute-self-test] FAILED: evolvo native check failed ({exc})")
        return False

    print("[pcpl-evolvo][kompute-self-test] PASSED")
    return True


def _run_cmd_capture(
    cmd: List[str],
    *,
    timeout_seconds: float = 20.0,
) -> Tuple[int, str, str]:
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            check=False,
            timeout=max(1.0, float(timeout_seconds)),
        )
        return int(proc.returncode), str(proc.stdout or ""), str(proc.stderr or "")
    except subprocess.TimeoutExpired as exc:
        stdout = str(getattr(exc, "stdout", "") or "")
        stderr = str(getattr(exc, "stderr", "") or "")
        return 124, stdout, stderr
    except Exception as exc:
        return 127, "", str(exc)


def _truncate_lines(text: str, *, max_lines: int = 12) -> str:
    lines = [line.rstrip() for line in str(text or "").splitlines() if line.strip()]
    if not lines:
        return ""
    if len(lines) <= int(max_lines):
        return "\n".join(lines)
    head = lines[: int(max_lines)]
    return "\n".join(head + [f"... ({len(lines) - int(max_lines)} more lines)"])


def _read_os_release() -> Dict[str, str]:
    path = Path("/etc/os-release")
    if not path.exists():
        return {}
    parsed: Dict[str, str] = {}
    for raw in path.read_text(encoding="utf-8", errors="ignore").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        parsed[str(key).strip()] = str(value).strip().strip('"')
    return parsed


def _ldconfig_library_map() -> Dict[str, List[str]]:
    ldconfig_path = shutil.which("ldconfig")
    if not ldconfig_path:
        return {}
    rc, stdout, _stderr = _run_cmd_capture([ldconfig_path, "-p"], timeout_seconds=8.0)
    if rc != 0:
        return {}
    libs: Dict[str, List[str]] = {}
    for line in stdout.splitlines():
        if "=>" not in line:
            continue
        left, right = line.split("=>", 1)
        name = left.strip().split(" ", 1)[0]
        target = right.strip()
        if not name or not target:
            continue
        libs.setdefault(name, []).append(target)
    return libs


def _collect_vulkan_icd_files() -> List[Path]:
    directories = [
        Path("/etc/vulkan/icd.d"),
        Path("/usr/share/vulkan/icd.d"),
        Path("/usr/local/share/vulkan/icd.d"),
    ]
    found: List[Path] = []
    seen = set()
    for directory in directories:
        if not directory.is_dir():
            continue
        for entry in sorted(directory.glob("*.json")):
            resolved = entry.resolve()
            if resolved in seen:
                continue
            seen.add(resolved)
            found.append(resolved)
    return found


def _icd_library_path_from_json(path: Path) -> Tuple[Optional[str], Optional[str]]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        return None, f"invalid json ({exc})"
    if not isinstance(payload, dict):
        return None, "json root is not an object"
    candidate: Optional[str] = None
    icd_section = payload.get("ICD")
    if isinstance(icd_section, dict):
        maybe = icd_section.get("library_path")
        if isinstance(maybe, str) and maybe.strip():
            candidate = maybe.strip()
    if not candidate:
        maybe = payload.get("library_path")
        if isinstance(maybe, str) and maybe.strip():
            candidate = maybe.strip()
    if not candidate:
        return None, "missing `library_path`"
    return candidate, None


def _icd_library_exists(
    lib_path: str,
    *,
    icd_file: Path,
    ld_map: Dict[str, List[str]],
) -> bool:
    candidate = str(lib_path).strip()
    if not candidate:
        return False
    if os.path.isabs(candidate):
        return Path(candidate).exists()
    if "/" in candidate:
        joined = (icd_file.parent / candidate).resolve()
        if joined.exists():
            return True
    basename = os.path.basename(candidate)
    mapped = ld_map.get(basename, [])
    for item in mapped:
        if Path(item).exists():
            return True
    return False


def _run_kompute_library_check() -> bool:
    print("[pcpl-evolvo][kompute-check-libs] starting")
    ok = True
    linux_mode = sys.platform.startswith("linux")

    os_release = _read_os_release()
    distro = os_release.get("PRETTY_NAME") or os_release.get("NAME") or "unknown"
    kernel = ""
    try:
        kernel = os.uname().release  # type: ignore[attr-defined]
    except Exception:
        kernel = "unknown"
    print(
        "[pcpl-evolvo][kompute-check-libs] host platform={platform} distro={distro} kernel={kernel}".format(
            platform=sys.platform,
            distro=distro,
            kernel=kernel,
        )
    )

    loader_map: Dict[str, List[str]] = {}
    if linux_mode:
        loader_map = _ldconfig_library_map()
        loader_paths = loader_map.get("libvulkan.so.1", [])
        if loader_paths:
            print(
                "[pcpl-evolvo][kompute-check-libs] vulkan-loader=ok count={count} sample={sample}".format(
                    count=len(loader_paths),
                    sample=loader_paths[0],
                )
            )
        else:
            ok = False
            print(
                "[pcpl-evolvo][kompute-check-libs] vulkan-loader=MISSING "
                "(libvulkan.so.1 not found in ldconfig cache)."
            )

        icd_files = _collect_vulkan_icd_files()
        if not icd_files:
            ok = False
            print(
                "[pcpl-evolvo][kompute-check-libs] icd-json=MISSING "
                "(no files in /etc|/usr/share|/usr/local/share vulkan icd.d)"
            )
        else:
            print(f"[pcpl-evolvo][kompute-check-libs] icd-json count={len(icd_files)}")
            for icd_file in icd_files:
                lib_path, err = _icd_library_path_from_json(icd_file)
                if err:
                    ok = False
                    print(f"[pcpl-evolvo][kompute-check-libs] icd INVALID {icd_file}: {err}")
                    continue
                assert lib_path is not None
                exists = _icd_library_exists(lib_path, icd_file=icd_file, ld_map=loader_map)
                if exists:
                    print(f"[pcpl-evolvo][kompute-check-libs] icd OK {icd_file.name} -> {lib_path}")
                else:
                    ok = False
                    print(
                        f"[pcpl-evolvo][kompute-check-libs] icd BROKEN {icd_file.name} -> {lib_path} "
                        "(library not found)"
                    )
    else:
        print(
            "[pcpl-evolvo][kompute-check-libs] non-linux host: skipping ldconfig/icd filesystem checks."
        )

    vk_icd_filenames = os.environ.get("VK_ICD_FILENAMES", "").strip()
    if vk_icd_filenames:
        missing: List[str] = []
        entries = [item.strip() for item in vk_icd_filenames.split(":") if item.strip()]
        for item in entries:
            if not Path(item).exists():
                missing.append(item)
        if missing:
            ok = False
            print(
                "[pcpl-evolvo][kompute-check-libs] env VK_ICD_FILENAMES invalid entries={missing}".format(
                    missing=",".join(missing)
                )
            )
        else:
            print(
                "[pcpl-evolvo][kompute-check-libs] env VK_ICD_FILENAMES=ok entries={count}".format(
                    count=len(entries)
                )
            )
    else:
        print("[pcpl-evolvo][kompute-check-libs] env VK_ICD_FILENAMES not set (using loader defaults)")

    vulkaninfo_path = shutil.which("vulkaninfo")
    if vulkaninfo_path:
        rc, stdout, stderr = _run_cmd_capture(
            [vulkaninfo_path, "--summary"],
            timeout_seconds=25.0,
        )
        if rc == 0:
            print("[pcpl-evolvo][kompute-check-libs] vulkaninfo=ok")
        else:
            ok = False
            merged = "\n".join([stdout, stderr]).strip()
            print(
                "[pcpl-evolvo][kompute-check-libs] vulkaninfo=FAILED rc={rc}\n{snippet}".format(
                    rc=rc,
                    snippet=_truncate_lines(merged, max_lines=14),
                )
            )
            if "Invalid instance" in merged:
                print(
                    "[pcpl-evolvo][kompute-check-libs] hint: Vulkan loader created an invalid instance. "
                    "Check ICD jsons and try forcing a known-good driver with VK_ICD_FILENAMES."
                )
    else:
        print("[pcpl-evolvo][kompute-check-libs] vulkaninfo not found (optional but recommended)")

    try:
        import kp  # type: ignore
    except Exception as exc:
        ok = False
        print(f"[pcpl-evolvo][kompute-check-libs] kp-import=FAILED ({exc})")
    else:
        sync_device_op, sync_local_op = _kp_sync_ops(kp)
        shared_memory_type = _kp_shared_memory_type(kp)
        api_mode = (
            "explicit-sync"
            if (sync_device_op is not None and sync_local_op is not None)
            else (
                "shared-memory"
                if shared_memory_type is not None
                else "unsupported"
            )
        )
        print(
            "[pcpl-evolvo][kompute-check-libs] kp-api mode={mode} has_sync_device={has_sd} has_sync_local={has_sl} has_shared_memory={has_sm}".format(
                mode=api_mode,
                has_sd=bool(sync_device_op is not None),
                has_sl=bool(sync_local_op is not None),
                has_sm=bool(shared_memory_type is not None),
            )
        )
        try:
            _manager, device_index, queue_family, device_name = _create_selftest_manager(kp)
            queue_label = "default" if queue_family is None else str(int(queue_family))
            print(
                "[pcpl-evolvo][kompute-check-libs] kp-manager=ok device_index={device} queue_family={queue} device_name={name}".format(
                    device=int(device_index),
                    queue=queue_label,
                    name=device_name,
                )
            )
        except Exception as exc:
            ok = False
            text = str(exc)
            print(f"[pcpl-evolvo][kompute-check-libs] kp-manager=FAILED ({text})")
            if "Invalid instance" in text:
                print(
                    "[pcpl-evolvo][kompute-check-libs] hint: this usually means broken Vulkan loader/ICD setup."
                )

    if ok:
        print("[pcpl-evolvo][kompute-check-libs] PASSED")
        return True

    print("[pcpl-evolvo][kompute-check-libs] FAILED")
    print(
        "[pcpl-evolvo][kompute-check-libs] next: verify Vulkan packages/ICD jsons, then run "
        "`python run_experiments.py --kompute-self-test`."
    )
    return False


def _param_choices(base: int, *, minimum: int = 1, high_factor: float = 1.5) -> List[int]:
    base = _safe_int(base, minimum=minimum)
    high = _safe_int(round(base * high_factor), minimum=minimum)
    high = max(high, base + 1)
    if high == base:
        return [base]
    return [base, high]


def _clamp_float(value: float, low: float, high: float) -> float:
    return max(float(low), min(float(high), float(value)))


def _parse_hidden_layers_spec(value: Any) -> List[int]:
    if value is None:
        return []
    raw_tokens: List[Any]
    if isinstance(value, str):
        text = value.strip()
        if not text or text.lower() in {"auto", "default", "none", "off"}:
            return []
        raw_tokens = [chunk.strip() for chunk in text.split(",")]
    elif isinstance(value, (list, tuple)):
        raw_tokens = list(value)
    else:
        raw_tokens = [value]

    parsed: List[int] = []
    for token in raw_tokens:
        if token is None:
            continue
        token_str = str(token).strip()
        if not token_str:
            continue
        try:
            width = int(token_str)
        except ValueError as exc:
            raise ValueError(
                f"Invalid hidden layer width `{token}` in supervised-hidden-layers."
            ) from exc
        if width <= 0:
            raise ValueError("supervised-hidden-layers values must be > 0")
        parsed.append(width)
    return parsed[:5]


def _parse_precision_tracks_spec(value: Any) -> List[str]:
    if value is None:
        return list(PRECISION_TRACK_CHOICES)
    text = str(value).strip()
    if not text or text.lower() in {"all", "default", "auto"}:
        return list(PRECISION_TRACK_CHOICES)
    parsed: List[str] = []
    seen = set()
    for raw_item in text.split(","):
        item = str(raw_item).strip().lower()
        if not item:
            continue
        if item not in PRECISION_TRACK_CHOICES:
            allowed = ", ".join(PRECISION_TRACK_CHOICES)
            raise ValueError(
                f"Unknown precision track `{raw_item}`. Allowed: {allowed}"
            )
        if item in seen:
            continue
        seen.add(item)
        parsed.append(item)
    if not parsed:
        return list(PRECISION_TRACK_CHOICES)
    return parsed


def _base_strategy_profile(args: argparse.Namespace) -> Dict[str, Any]:
    return {
        "parent_pool_ratio": float(args.parent_pool_ratio),
        "stagnation_patience": int(args.stagnation_patience),
        "mutation_floor": float(args.mutation_floor),
        "mutation_ceiling": float(args.mutation_ceiling),
        "mutation_step": float(args.mutation_step),
        "quick_cycle_fraction": float(args.quick_cycle_fraction),
        "mid_cycle_fraction": float(args.mid_cycle_fraction),
        "quick_keep_ratio": float(args.quick_keep_ratio),
        "mid_keep_ratio": float(args.mid_keep_ratio),
        "key_variants": int(args.key_variants),
        "novelty_bonus": float(args.novelty_bonus),
        "predictive_penalty": float(args.predictive_penalty),
        "sync_loss_gate_percentile": float(args.sync_loss_gate_percentile),
        "sync_loss_gate_penalty": float(args.sync_loss_gate_penalty),
        "sync_loss_gate_flat_boost": float(args.sync_loss_gate_flat_boost),
        "anti_neutrality_window": int(args.anti_neutrality_window),
        "anti_neutrality_penalty": float(args.anti_neutrality_penalty),
        "anti_neutrality_bonus": float(args.anti_neutrality_bonus),
        "attacker_panel_size": int(args.attacker_panel_size),
        "attacker_panel_penalty": float(args.attacker_panel_penalty),
        "target_generation_seconds": float(args.target_generation_seconds),
        "max_eval_cache_entries": int(args.max_eval_cache_entries),
        "statistical_predictive": bool(args.statistical_predictive),
        "auto_statistical_tuning": bool(args.auto_statistical_tuning),
        "parallel_backend": str(args.parallel_backend),
        "round_parallelism": int(args.round_parallelism),
        "minimum_parallel_rounds": int(args.minimum_parallel_rounds),
        "device_mhz": float(args.device_mhz),
        "provider_mhz": float(args.provider_mhz),
        "max_test_time_seconds": float(args.max_test_seconds),
        "debug_eval_timeout_seconds": float(args.debug_eval_timeout_seconds),
        "debug_eval_log_interval_seconds": float(args.debug_eval_log_interval_seconds),
    }


def _precision_strategy_profiles(args: argparse.Namespace) -> List[Dict[str, Any]]:
    base = _base_strategy_profile(args)
    baseline = {
        **base,
        "strategy": "baseline",
        "description": (
            "Resolved user configuration kept intact as the reference precision lane."
        ),
    }
    supervisor = {
        **base,
        "strategy": "supervisor",
        "description": (
            "Long-horizon supervision lane: stronger sync pressure, longer timing horizon, "
            "and more attacker coupling."
        ),
        "key_variants": max(6, int(base["key_variants"])),
        "sync_loss_gate_percentile": _clamp_float(
            min(float(base["sync_loss_gate_percentile"]), 0.52),
            0.0,
            1.0,
        ),
        "sync_loss_gate_penalty": max(0.14, float(base["sync_loss_gate_penalty"])),
        "sync_loss_gate_flat_boost": max(
            0.10,
            float(base["sync_loss_gate_flat_boost"]),
        ),
        "attacker_panel_size": max(4, int(base["attacker_panel_size"])),
        "attacker_panel_penalty": max(0.18, float(base["attacker_panel_penalty"])),
        "target_generation_seconds": max(
            3.2,
            float(base["target_generation_seconds"]),
        ),
        "max_eval_cache_entries": max(
            int(base["max_eval_cache_entries"]),
            int(round(float(base["max_eval_cache_entries"]) * 1.20)),
        ),
        "max_test_time_seconds": max(30.0, float(base["max_test_time_seconds"])),
    }
    lane_pressure = {
        **base,
        "strategy": "lane-pressure",
        "description": (
            "Lane/route inference lane: larger attacker panels and more key variants to "
            "stress schedule predictability rather than only token guessing."
        ),
        "key_variants": max(6, int(base["key_variants"])),
        "novelty_bonus": max(0.16, float(base["novelty_bonus"])),
        "predictive_penalty": max(0.10, float(base["predictive_penalty"])),
        "sync_loss_gate_percentile": _clamp_float(
            min(float(base["sync_loss_gate_percentile"]), 0.58),
            0.0,
            1.0,
        ),
        "sync_loss_gate_penalty": max(0.12, float(base["sync_loss_gate_penalty"])),
        "attacker_panel_size": max(5, int(base["attacker_panel_size"])),
        "attacker_panel_penalty": max(0.22, float(base["attacker_panel_penalty"])),
        "max_test_time_seconds": max(15.0, float(base["max_test_time_seconds"])),
    }
    evaluability = {
        **base,
        "strategy": "evaluability",
        "description": (
            "Evaluability audit lane: disables predictive shortcuts and reduces blind "
            "parallel cut paths so final metrics are more trustworthy."
        ),
        "statistical_predictive": False,
        "auto_statistical_tuning": False,
        "parallel_backend": "thread",
        "round_parallelism": 1,
        "minimum_parallel_rounds": 1,
        "target_generation_seconds": max(
            3.0,
            float(base["target_generation_seconds"]),
        ),
        "max_eval_cache_entries": max(
            int(base["max_eval_cache_entries"]),
            int(round(float(base["max_eval_cache_entries"]) * 1.15)),
        ),
        "debug_eval_timeout_seconds": max(
            60.0,
            float(base["debug_eval_timeout_seconds"]),
        ),
        "debug_eval_log_interval_seconds": max(
            15.0,
            float(base["debug_eval_log_interval_seconds"]),
        ),
    }
    random_research = {
        **base,
        "strategy": "random-research",
        "description": (
            "Stochastic-first lane: high novelty/mutation pressure, broader attacker panel, "
            "and stricter sync-loss gating for random-search discovery."
        ),
        "parent_pool_ratio": _clamp_float(
            min(float(base["parent_pool_ratio"]), 0.34),
            0.20,
            0.50,
        ),
        "stagnation_patience": 1,
        "mutation_floor": _clamp_float(max(0.30, float(base["mutation_floor"])), 0.18, 0.95),
        "mutation_ceiling": _clamp_float(max(0.96, float(base["mutation_ceiling"])), 0.72, 0.99),
        "mutation_step": _clamp_float(max(0.16, float(base["mutation_step"])), 0.06, 0.35),
        "quick_cycle_fraction": _clamp_float(min(float(base["quick_cycle_fraction"]), 0.10), 0.04, 0.30),
        "mid_cycle_fraction": _clamp_float(min(float(base["mid_cycle_fraction"]), 0.34), 0.16, 0.70),
        "quick_keep_ratio": _clamp_float(min(float(base["quick_keep_ratio"]), 0.48), 0.18, 0.72),
        "mid_keep_ratio": _clamp_float(min(float(base["mid_keep_ratio"]), 0.20), 0.08, 0.45),
        "key_variants": max(6, int(base["key_variants"])),
        "novelty_bonus": max(0.22, float(base["novelty_bonus"])),
        "predictive_penalty": max(0.11, float(base["predictive_penalty"])),
        "sync_loss_gate_percentile": _clamp_float(
            min(float(base["sync_loss_gate_percentile"]), 0.54),
            0.0,
            1.0,
        ),
        "sync_loss_gate_penalty": max(0.15, float(base["sync_loss_gate_penalty"])),
        "sync_loss_gate_flat_boost": max(
            0.11,
            float(base["sync_loss_gate_flat_boost"]),
        ),
        "anti_neutrality_window": max(6, min(int(base["anti_neutrality_window"]), 9)),
        "anti_neutrality_penalty": max(0.035, float(base["anti_neutrality_penalty"])),
        "anti_neutrality_bonus": max(0.018, float(base["anti_neutrality_bonus"])),
        "attacker_panel_size": max(5, int(base["attacker_panel_size"])),
        "attacker_panel_penalty": max(0.24, float(base["attacker_panel_penalty"])),
        "target_generation_seconds": max(
            3.0,
            float(base["target_generation_seconds"]),
        ),
        "max_eval_cache_entries": max(
            int(base["max_eval_cache_entries"]),
            int(round(float(base["max_eval_cache_entries"]) * 1.30)),
        ),
        "max_test_time_seconds": max(20.0, float(base["max_test_time_seconds"])),
        "debug_eval_timeout_seconds": max(
            75.0,
            float(base["debug_eval_timeout_seconds"]),
        ),
        "debug_eval_log_interval_seconds": max(
            15.0,
            float(base["debug_eval_log_interval_seconds"]),
        ),
    }
    return [baseline, supervisor, lane_pressure, evaluability, random_research]


def _continuous_strategy_profiles(args: argparse.Namespace) -> List[Dict[str, Any]]:
    if str(getattr(args, "experiment_suite", "single")).strip().lower() == "precision":
        allowed = {
            str(item).strip().lower()
            for item in getattr(args, "precision_tracks", PRECISION_TRACK_CHOICES)
        }
        profiles = [
            profile
            for profile in _precision_strategy_profiles(args)
            if str(profile.get("strategy", "")).strip().lower() in allowed
        ]
        if not profiles:
            raise RuntimeError("Precision suite selected but no tracks were enabled")
        return profiles

    mode = str(getattr(args, "mode", "")).lower()
    base = {
        **_base_strategy_profile(args),
        "strategy": "base",
        "description": "Resolved runner defaults as-is.",
    }
    if mode not in {"paper", "random-research"}:
        return [base]

    dynamic = {
        "strategy": "dynamic",
        "parent_pool_ratio": _clamp_float(base["parent_pool_ratio"], 0.35, 0.65),
        "stagnation_patience": max(1, base["stagnation_patience"]),
        "mutation_floor": _clamp_float(max(0.18, base["mutation_floor"]), 0.10, 0.90),
        "mutation_ceiling": _clamp_float(max(base["mutation_ceiling"], base["mutation_floor"] + 0.18), 0.55, 0.99),
        "mutation_step": _clamp_float(max(0.10, base["mutation_step"]), 0.03, 0.30),
        "quick_cycle_fraction": _clamp_float(base["quick_cycle_fraction"] + 0.01, 0.05, 0.35),
        "mid_cycle_fraction": _clamp_float(base["mid_cycle_fraction"] + 0.03, 0.18, 0.75),
        "quick_keep_ratio": _clamp_float(base["quick_keep_ratio"] + 0.03, 0.20, 0.82),
        "mid_keep_ratio": _clamp_float(base["mid_keep_ratio"] + 0.02, 0.08, 0.50),
        "key_variants": max(3, base["key_variants"]),
        "novelty_bonus": _clamp_float(max(0.10, base["novelty_bonus"]), 0.03, 0.30),
        "predictive_penalty": _clamp_float(max(0.06, base["predictive_penalty"]), 0.03, 0.22),
        "sync_loss_gate_percentile": _clamp_float(min(base["sync_loss_gate_percentile"], 0.62), 0.30, 0.90),
        "sync_loss_gate_penalty": _clamp_float(max(0.08, base["sync_loss_gate_penalty"]), 0.02, 0.35),
        "sync_loss_gate_flat_boost": _clamp_float(max(0.05, base["sync_loss_gate_flat_boost"]), 0.00, 0.30),
        "anti_neutrality_window": max(6, base["anti_neutrality_window"]),
        "anti_neutrality_penalty": _clamp_float(max(0.02, base["anti_neutrality_penalty"]), 0.00, 0.20),
        "anti_neutrality_bonus": _clamp_float(max(0.01, base["anti_neutrality_bonus"]), 0.00, 0.15),
        "attacker_panel_size": max(2, base["attacker_panel_size"]),
        "attacker_panel_penalty": _clamp_float(max(0.10, base["attacker_panel_penalty"]), 0.00, 0.40),
        "target_generation_seconds": _clamp_float(base["target_generation_seconds"] * 0.95, 0.70, 4.0),
        "max_eval_cache_entries": max(15000, int(round(base["max_eval_cache_entries"] * 1.10))),
    }
    explorer = {
        "strategy": "explorer",
        "parent_pool_ratio": _clamp_float(min(base["parent_pool_ratio"], 0.36), 0.25, 0.55),
        "stagnation_patience": 1,
        "mutation_floor": _clamp_float(max(0.26, base["mutation_floor"]), 0.18, 0.95),
        "mutation_ceiling": _clamp_float(max(0.94, base["mutation_ceiling"]), 0.70, 0.99),
        "mutation_step": _clamp_float(max(0.14, base["mutation_step"]), 0.05, 0.35),
        "quick_cycle_fraction": _clamp_float(min(base["quick_cycle_fraction"], 0.10), 0.05, 0.30),
        "mid_cycle_fraction": _clamp_float(min(base["mid_cycle_fraction"], 0.36), 0.18, 0.70),
        "quick_keep_ratio": _clamp_float(min(base["quick_keep_ratio"], 0.42), 0.20, 0.75),
        "mid_keep_ratio": _clamp_float(min(base["mid_keep_ratio"], 0.16), 0.08, 0.40),
        "key_variants": max(4, base["key_variants"]),
        "novelty_bonus": _clamp_float(max(0.14, base["novelty_bonus"]), 0.05, 0.35),
        "predictive_penalty": _clamp_float(max(0.10, base["predictive_penalty"]), 0.04, 0.25),
        "sync_loss_gate_percentile": _clamp_float(min(base["sync_loss_gate_percentile"], 0.58), 0.30, 0.90),
        "sync_loss_gate_penalty": _clamp_float(max(0.10, base["sync_loss_gate_penalty"]), 0.02, 0.35),
        "sync_loss_gate_flat_boost": _clamp_float(max(0.06, base["sync_loss_gate_flat_boost"]), 0.00, 0.30),
        "anti_neutrality_window": max(6, base["anti_neutrality_window"] - 1),
        "anti_neutrality_penalty": _clamp_float(max(0.03, base["anti_neutrality_penalty"]), 0.00, 0.20),
        "anti_neutrality_bonus": _clamp_float(max(0.01, base["anti_neutrality_bonus"]), 0.00, 0.15),
        "attacker_panel_size": max(3, base["attacker_panel_size"]),
        "attacker_panel_penalty": _clamp_float(max(0.12, base["attacker_panel_penalty"]), 0.00, 0.40),
        "target_generation_seconds": _clamp_float(base["target_generation_seconds"] * 0.88, 0.60, 4.0),
        "max_eval_cache_entries": max(15000, int(round(base["max_eval_cache_entries"] * 1.18))),
    }
    random_research = {
        **base,
        "strategy": "random-research",
        "description": (
            "Stochastic-first continuous lane: high novelty pressure with stricter sync/evaluability gates."
        ),
        "parent_pool_ratio": _clamp_float(min(base["parent_pool_ratio"], 0.32), 0.20, 0.50),
        "stagnation_patience": 1,
        "mutation_floor": _clamp_float(max(0.30, base["mutation_floor"]), 0.18, 0.95),
        "mutation_ceiling": _clamp_float(max(0.96, base["mutation_ceiling"]), 0.72, 0.99),
        "mutation_step": _clamp_float(max(0.16, base["mutation_step"]), 0.06, 0.35),
        "quick_cycle_fraction": _clamp_float(min(base["quick_cycle_fraction"], 0.10), 0.04, 0.30),
        "mid_cycle_fraction": _clamp_float(min(base["mid_cycle_fraction"], 0.34), 0.16, 0.70),
        "quick_keep_ratio": _clamp_float(min(base["quick_keep_ratio"], 0.48), 0.18, 0.72),
        "mid_keep_ratio": _clamp_float(min(base["mid_keep_ratio"], 0.20), 0.08, 0.45),
        "key_variants": max(6, base["key_variants"]),
        "novelty_bonus": _clamp_float(max(0.22, base["novelty_bonus"]), 0.08, 0.35),
        "predictive_penalty": _clamp_float(max(0.11, base["predictive_penalty"]), 0.04, 0.28),
        "sync_loss_gate_percentile": _clamp_float(min(base["sync_loss_gate_percentile"], 0.54), 0.30, 0.90),
        "sync_loss_gate_penalty": _clamp_float(max(0.15, base["sync_loss_gate_penalty"]), 0.02, 0.40),
        "sync_loss_gate_flat_boost": _clamp_float(max(0.11, base["sync_loss_gate_flat_boost"]), 0.00, 0.40),
        "anti_neutrality_window": max(6, min(base["anti_neutrality_window"], 9)),
        "anti_neutrality_penalty": _clamp_float(max(0.035, base["anti_neutrality_penalty"]), 0.00, 0.30),
        "anti_neutrality_bonus": _clamp_float(max(0.018, base["anti_neutrality_bonus"]), 0.00, 0.20),
        "attacker_panel_size": max(5, base["attacker_panel_size"]),
        "attacker_panel_penalty": _clamp_float(max(0.24, base["attacker_panel_penalty"]), 0.00, 0.45),
        "target_generation_seconds": _clamp_float(max(3.0, base["target_generation_seconds"]), 1.0, 6.0),
        "max_eval_cache_entries": max(18000, int(round(base["max_eval_cache_entries"] * 1.30))),
        "max_test_time_seconds": max(20.0, base["max_test_time_seconds"]),
        "debug_eval_timeout_seconds": max(75.0, base["debug_eval_timeout_seconds"]),
        "debug_eval_log_interval_seconds": max(15.0, base["debug_eval_log_interval_seconds"]),
    }
    random_research_audit = {
        **random_research,
        "strategy": "random-research-audit",
        "description": (
            "Random-search audit lane: same stochastic pressure with reduced predictive shortcuts for evaluability."
        ),
        "statistical_predictive": False,
        "auto_statistical_tuning": False,
        "parallel_backend": "thread",
        "round_parallelism": 1,
        "minimum_parallel_rounds": 1,
    }
    if mode == "random-research":
        return [random_research, random_research_audit]
    return [dynamic, explorer]


def _combo_label(combo: Dict[str, Any]) -> str:
    strategy = str(combo.get("strategy", "")).strip()
    strategy_suffix = f"-s{strategy}" if strategy else ""
    return (
        f"p{combo['population_size']}-g{combo['generations']}"
        f"-i{combo['initial_instructions']}"
        f"-ap{combo['attacker_population_size']}"
        f"-ag{combo['attacker_generations']}"
        f"-e{combo['elite_pool']}"
        f"{strategy_suffix}"
    )


def _build_continuous_grid(args: argparse.Namespace) -> List[Dict[str, Any]]:
    """Generate a finite exhaustive parameter grid for continuous sweeps."""
    precision_suite = (
        str(getattr(args, "experiment_suite", "single")).strip().lower() == "precision"
    )
    if precision_suite:
        populations = [_safe_int(args.population_size, minimum=4)]
        generations = [_safe_int(args.generations, minimum=1)]
        instructions = [_safe_int(args.initial_instructions, minimum=3)]
        attacker_populations = [_safe_int(args.attacker_population_size, minimum=3)]
        attacker_generations = [_safe_int(args.attacker_generations, minimum=1)]
        elites = [_safe_int(args.elite_pool, minimum=4)]
    else:
        populations = _param_choices(args.population_size, minimum=4, high_factor=1.5)
        generations = _param_choices(args.generations, minimum=1, high_factor=1.6)
        instructions = _param_choices(args.initial_instructions, minimum=3, high_factor=1.5)
        attacker_populations = _param_choices(
            args.attacker_population_size,
            minimum=3,
            high_factor=1.5,
        )
        attacker_generations = _param_choices(
            args.attacker_generations,
            minimum=1,
            high_factor=1.6,
        )
        elites = _param_choices(args.elite_pool, minimum=4, high_factor=1.4)
    strategies = _continuous_strategy_profiles(args)

    combos: List[Dict[str, Any]] = []
    for values in itertools.product(
        populations,
        generations,
        instructions,
        attacker_populations,
        attacker_generations,
        elites,
    ):
        pop, gen, instr, apop, agen, elite = values
        for strategy in strategies:
            combos.append(
                {
                    "population_size": pop,
                    "generations": gen,
                    "initial_instructions": instr,
                    "attacker_population_size": apop,
                    "attacker_generations": agen,
                    "elite_pool": min(pop, elite),
                    "archive_limit": max(args.archive_limit, min(pop * 6, args.archive_limit * 2)),
                    "strategy": str(strategy.get("strategy", "base")),
                    "parent_pool_ratio": float(strategy["parent_pool_ratio"]),
                    "stagnation_patience": int(strategy["stagnation_patience"]),
                    "mutation_floor": float(strategy["mutation_floor"]),
                    "mutation_ceiling": float(strategy["mutation_ceiling"]),
                    "mutation_step": float(strategy["mutation_step"]),
                    "quick_cycle_fraction": float(strategy["quick_cycle_fraction"]),
                    "mid_cycle_fraction": float(strategy["mid_cycle_fraction"]),
                    "quick_keep_ratio": float(strategy["quick_keep_ratio"]),
                    "mid_keep_ratio": float(strategy["mid_keep_ratio"]),
                    "key_variants": int(strategy["key_variants"]),
                    "novelty_bonus": float(strategy["novelty_bonus"]),
                    "predictive_penalty": float(strategy["predictive_penalty"]),
                    "sync_loss_gate_percentile": float(strategy["sync_loss_gate_percentile"]),
                    "sync_loss_gate_penalty": float(strategy["sync_loss_gate_penalty"]),
                    "sync_loss_gate_flat_boost": float(strategy["sync_loss_gate_flat_boost"]),
                    "anti_neutrality_window": int(strategy["anti_neutrality_window"]),
                    "anti_neutrality_penalty": float(strategy["anti_neutrality_penalty"]),
                    "anti_neutrality_bonus": float(strategy["anti_neutrality_bonus"]),
                    "attacker_panel_size": int(strategy["attacker_panel_size"]),
                    "attacker_panel_penalty": float(strategy["attacker_panel_penalty"]),
                    "target_generation_seconds": float(strategy["target_generation_seconds"]),
                    "max_eval_cache_entries": int(strategy["max_eval_cache_entries"]),
                    "statistical_predictive": bool(
                        strategy.get("statistical_predictive", args.statistical_predictive)
                    ),
                    "auto_statistical_tuning": bool(
                        strategy.get(
                            "auto_statistical_tuning",
                            args.auto_statistical_tuning,
                        )
                    ),
                    "parallel_backend": str(
                        strategy.get("parallel_backend", args.parallel_backend)
                    ),
                    "round_parallelism": int(
                        strategy.get("round_parallelism", args.round_parallelism)
                    ),
                    "minimum_parallel_rounds": int(
                        strategy.get(
                            "minimum_parallel_rounds",
                            args.minimum_parallel_rounds,
                        )
                    ),
                    "device_mhz": float(strategy.get("device_mhz", args.device_mhz)),
                    "provider_mhz": float(
                        strategy.get("provider_mhz", args.provider_mhz)
                    ),
                    "max_test_time_seconds": float(
                        strategy.get(
                            "max_test_time_seconds",
                            args.max_test_seconds,
                        )
                    ),
                    "debug_eval_timeout_seconds": float(
                        strategy.get(
                            "debug_eval_timeout_seconds",
                            args.debug_eval_timeout_seconds,
                        )
                    ),
                    "debug_eval_log_interval_seconds": float(
                        strategy.get(
                            "debug_eval_log_interval_seconds",
                            args.debug_eval_log_interval_seconds,
                        )
                    ),
                    "description": str(strategy.get("description", "")),
                }
            )

    dedup: Dict[str, Dict[str, Any]] = {}
    for combo in combos:
        dedup[_combo_label(combo)] = combo
    return list(dedup.values())


def _build_experiment_config(
    args: argparse.Namespace,
    *,
    out_dir: Path,
    seed: int,
    population_size: int,
    generations: int,
    initial_instructions: int,
    rounds: int,
    attacker_population_size: int,
    attacker_generations: int,
    elite_pool: int,
    archive_limit: int,
    resume: bool,
    workers: int,
    parent_pool_ratio: Optional[float] = None,
    stagnation_patience: Optional[int] = None,
    mutation_floor: Optional[float] = None,
    mutation_ceiling: Optional[float] = None,
    mutation_step: Optional[float] = None,
    quick_cycle_fraction: Optional[float] = None,
    mid_cycle_fraction: Optional[float] = None,
    quick_keep_ratio: Optional[float] = None,
    mid_keep_ratio: Optional[float] = None,
    key_variants: Optional[int] = None,
    novelty_bonus: Optional[float] = None,
    predictive_penalty: Optional[float] = None,
    sync_loss_gate_percentile: Optional[float] = None,
    sync_loss_gate_penalty: Optional[float] = None,
    sync_loss_gate_flat_boost: Optional[float] = None,
    anti_neutrality_window: Optional[int] = None,
    anti_neutrality_penalty: Optional[float] = None,
    anti_neutrality_bonus: Optional[float] = None,
    attacker_panel_size: Optional[int] = None,
    attacker_panel_penalty: Optional[float] = None,
    target_generation_seconds: Optional[float] = None,
    max_eval_cache_entries: Optional[int] = None,
    parallel_backend: Optional[str] = None,
    round_parallelism: Optional[int] = None,
    minimum_parallel_rounds: Optional[int] = None,
    statistical_predictive: Optional[bool] = None,
    auto_statistical_tuning: Optional[bool] = None,
    device_mhz: Optional[float] = None,
    provider_mhz: Optional[float] = None,
    max_test_time_seconds: Optional[float] = None,
    debug_eval_timeout_seconds: Optional[float] = None,
    debug_eval_log_interval_seconds: Optional[float] = None,
) -> ExperimentConfig:
    return ExperimentConfig(
        out_dir=out_dir,
        profile=args.profile,
        seed=seed,
        population_size=population_size,
        generations=generations,
        initial_instructions=initial_instructions,
        rounds=rounds,
        attacker_population_size=attacker_population_size,
        attacker_generations=attacker_generations,
        elite_pool=elite_pool,
        archive_limit=archive_limit,
        resume=resume,
        parallel_workers=workers,
        parallel_backend=(
            str(args.parallel_backend)
            if parallel_backend is None
            else str(parallel_backend)
        ),
        round_parallelism=(
            int(args.round_parallelism)
            if round_parallelism is None
            else int(round_parallelism)
        ),
        minimum_parallel_rounds=(
            int(args.minimum_parallel_rounds)
            if minimum_parallel_rounds is None
            else int(minimum_parallel_rounds)
        ),
        max_cpu_utilization=float(args.max_cpu_utilization),
        max_gpu_utilization=float(args.max_gpu_utilization),
        round_state_sync=str(args.round_state_sync),
        executor_backend=args.executor_backend,
        kompute_runtime_mode=args.kompute_runtime_mode,
        kompute_warn_on_fallback=bool(args.kompute_warn_on_fallback),
        kompute_fail_hard=bool(args.kompute_fail_hard),
        kompute_keep_vram_state=bool(args.kompute_keep_vram_state),
        kompute_min_native_stage_count=int(args.kompute_min_native_stage_count),
        kompute_min_native_stage_share=float(args.kompute_min_native_stage_share),
        kompute_max_unsupported_count=int(args.kompute_max_unsupported_count),
        kompute_max_unsupported_share=float(args.kompute_max_unsupported_share),
        kompute_force_cpu_on_partial_coverage=bool(
            args.kompute_force_cpu_on_partial_coverage
        ),
        kompute_native_enable_decimal=bool(args.kompute_native_enable_decimal),
        kompute_native_enable_boolean_compare=bool(
            args.kompute_native_enable_boolean_compare
        ),
        kompute_native_enable_boolean_logic=bool(args.kompute_native_enable_boolean_logic),
        kompute_native_enable_list_query=bool(args.kompute_native_enable_list_query),
        kompute_allow_process_pool=bool(args.kompute_allow_process_pool),
        use_supervised_guide=bool(args.use_supervised_guide),
        supervised_end_round_only=bool(args.supervised_end_round_only),
        preferred_device=args.device,
        supervised_hidden_layers=tuple(int(width) for width in args.supervised_hidden_layers),
        supervised_epochs=int(args.supervised_epochs),
        supervised_candidate_pool=int(args.supervised_candidate_pool),
        supervised_capacity_auto_tune=bool(args.supervised_capacity_auto_tune),
        parent_pool_ratio=args.parent_pool_ratio if parent_pool_ratio is None else float(parent_pool_ratio),
        stagnation_patience=args.stagnation_patience if stagnation_patience is None else int(stagnation_patience),
        mutation_floor=args.mutation_floor if mutation_floor is None else float(mutation_floor),
        mutation_ceiling=args.mutation_ceiling if mutation_ceiling is None else float(mutation_ceiling),
        mutation_step=args.mutation_step if mutation_step is None else float(mutation_step),
        statistical_predictive=(
            bool(args.statistical_predictive)
            if statistical_predictive is None
            else bool(statistical_predictive)
        ),
        quick_cycle_fraction=args.quick_cycle_fraction if quick_cycle_fraction is None else float(quick_cycle_fraction),
        mid_cycle_fraction=args.mid_cycle_fraction if mid_cycle_fraction is None else float(mid_cycle_fraction),
        quick_keep_ratio=args.quick_keep_ratio if quick_keep_ratio is None else float(quick_keep_ratio),
        mid_keep_ratio=args.mid_keep_ratio if mid_keep_ratio is None else float(mid_keep_ratio),
        key_variant_count=args.key_variants if key_variants is None else int(key_variants),
        novelty_bonus=args.novelty_bonus if novelty_bonus is None else float(novelty_bonus),
        predictive_penalty=args.predictive_penalty if predictive_penalty is None else float(predictive_penalty),
        sync_loss_gate_percentile=(
            args.sync_loss_gate_percentile
            if sync_loss_gate_percentile is None
            else float(sync_loss_gate_percentile)
        ),
        sync_loss_gate_penalty=(
            args.sync_loss_gate_penalty
            if sync_loss_gate_penalty is None
            else float(sync_loss_gate_penalty)
        ),
        sync_loss_gate_flat_boost=(
            args.sync_loss_gate_flat_boost
            if sync_loss_gate_flat_boost is None
            else float(sync_loss_gate_flat_boost)
        ),
        anti_neutrality_window=(
            args.anti_neutrality_window
            if anti_neutrality_window is None
            else int(anti_neutrality_window)
        ),
        anti_neutrality_penalty=(
            args.anti_neutrality_penalty
            if anti_neutrality_penalty is None
            else float(anti_neutrality_penalty)
        ),
        anti_neutrality_bonus=(
            args.anti_neutrality_bonus
            if anti_neutrality_bonus is None
            else float(anti_neutrality_bonus)
        ),
        attacker_panel_size=(
            args.attacker_panel_size
            if attacker_panel_size is None
            else int(attacker_panel_size)
        ),
        attacker_panel_penalty=(
            args.attacker_panel_penalty
            if attacker_panel_penalty is None
            else float(attacker_panel_penalty)
        ),
        auto_statistical_tuning=(
            bool(args.auto_statistical_tuning)
            if auto_statistical_tuning is None
            else bool(auto_statistical_tuning)
        ),
        target_generation_seconds=(
            args.target_generation_seconds
            if target_generation_seconds is None
            else float(target_generation_seconds)
        ),
        max_eval_cache_entries=(
            args.max_eval_cache_entries
            if max_eval_cache_entries is None
            else int(max_eval_cache_entries)
        ),
        device_mhz=(
            float(args.device_mhz) if device_mhz is None else float(device_mhz)
        ),
        provider_mhz=(
            float(args.provider_mhz)
            if provider_mhz is None
            else float(provider_mhz)
        ),
        max_test_time_seconds=(
            float(args.max_test_seconds)
            if max_test_time_seconds is None
            else float(max_test_time_seconds)
        ),
        debug_eval_timeout_seconds=(
            float(args.debug_eval_timeout_seconds)
            if debug_eval_timeout_seconds is None
            else float(debug_eval_timeout_seconds)
        ),
        debug_eval_log_interval_seconds=(
            float(args.debug_eval_log_interval_seconds)
            if debug_eval_log_interval_seconds is None
            else float(debug_eval_log_interval_seconds)
        ),
    )


def _build_experiment_config_from_args(
    args: argparse.Namespace,
    *,
    out_dir: Path,
    seed: int,
    resume: bool,
    workers: int,
    overrides: Optional[Dict[str, Any]] = None,
) -> ExperimentConfig:
    values = dict(overrides or {})
    return _build_experiment_config(
        args,
        out_dir=out_dir,
        seed=int(values.get("seed", seed)),
        population_size=int(values.get("population_size", args.population_size)),
        generations=int(values.get("generations", args.generations)),
        initial_instructions=int(
            values.get("initial_instructions", args.initial_instructions)
        ),
        rounds=int(values.get("rounds", args.rounds)),
        attacker_population_size=int(
            values.get(
                "attacker_population_size",
                args.attacker_population_size,
            )
        ),
        attacker_generations=int(
            values.get("attacker_generations", args.attacker_generations)
        ),
        elite_pool=int(values.get("elite_pool", args.elite_pool)),
        archive_limit=int(values.get("archive_limit", args.archive_limit)),
        resume=resume,
        workers=int(values.get("workers", workers)),
        parent_pool_ratio=values.get("parent_pool_ratio"),
        stagnation_patience=values.get("stagnation_patience"),
        mutation_floor=values.get("mutation_floor"),
        mutation_ceiling=values.get("mutation_ceiling"),
        mutation_step=values.get("mutation_step"),
        quick_cycle_fraction=values.get("quick_cycle_fraction"),
        mid_cycle_fraction=values.get("mid_cycle_fraction"),
        quick_keep_ratio=values.get("quick_keep_ratio"),
        mid_keep_ratio=values.get("mid_keep_ratio"),
        key_variants=values.get("key_variants"),
        novelty_bonus=values.get("novelty_bonus"),
        predictive_penalty=values.get("predictive_penalty"),
        sync_loss_gate_percentile=values.get("sync_loss_gate_percentile"),
        sync_loss_gate_penalty=values.get("sync_loss_gate_penalty"),
        sync_loss_gate_flat_boost=values.get("sync_loss_gate_flat_boost"),
        anti_neutrality_window=values.get("anti_neutrality_window"),
        anti_neutrality_penalty=values.get("anti_neutrality_penalty"),
        anti_neutrality_bonus=values.get("anti_neutrality_bonus"),
        attacker_panel_size=values.get("attacker_panel_size"),
        attacker_panel_penalty=values.get("attacker_panel_penalty"),
        target_generation_seconds=values.get("target_generation_seconds"),
        max_eval_cache_entries=values.get("max_eval_cache_entries"),
        parallel_backend=values.get("parallel_backend"),
        round_parallelism=values.get("round_parallelism"),
        minimum_parallel_rounds=values.get("minimum_parallel_rounds"),
        statistical_predictive=values.get("statistical_predictive"),
        auto_statistical_tuning=values.get("auto_statistical_tuning"),
        device_mhz=values.get("device_mhz"),
        provider_mhz=values.get("provider_mhz"),
        max_test_time_seconds=values.get("max_test_time_seconds"),
        debug_eval_timeout_seconds=values.get("debug_eval_timeout_seconds"),
        debug_eval_log_interval_seconds=values.get(
            "debug_eval_log_interval_seconds"
        ),
    )


def _resolve_continuous_lane_plan(
    *,
    grid_size: int,
    workers_arg: int,
    parallel_backend: str,
    max_cpu_utilization: float = 0.75,
) -> Dict[str, int]:
    cpu_count = max(1, int(os.cpu_count() or 1))
    cpu_budget_ratio = _clamp_float(float(max_cpu_utilization), 0.10, 1.0)
    cpu_budget_workers = max(1, min(cpu_count, int(float(cpu_count) * cpu_budget_ratio)))
    requested_workers = max(1, int(workers_arg)) if int(workers_arg) > 0 else cpu_budget_workers
    total_workers = max(1, min(requested_workers, cpu_budget_workers))
    backend = str(parallel_backend).lower()
    if backend not in {"auto", "process", "thread", "off"}:
        backend = "auto"

    if backend == "off" or grid_size <= 1 or total_workers <= 1:
        return {
            "cpu_count": cpu_count,
            "total_workers": total_workers,
            "lanes": 1,
            "workers_per_lane": max(1, total_workers),
        }

    # Keep parallel lanes wide enough to saturate CPUs inside each experiment lane.
    if backend == "process":
        # Prefer wider process lanes to avoid tiny 4-worker islands on high-core hosts.
        if total_workers >= 24:
            min_workers_per_lane = 8
            max_lanes = 3
        elif total_workers >= 12:
            min_workers_per_lane = 6
            max_lanes = 3
        elif total_workers >= 8:
            min_workers_per_lane = 4
            max_lanes = 2
        else:
            min_workers_per_lane = 2
            max_lanes = 2
    else:
        min_workers_per_lane = 3 if total_workers >= 6 else 2
        max_lanes = 12
    lanes = max(1, total_workers // max(1, min_workers_per_lane))
    lanes = min(lanes, grid_size, max_lanes)
    lanes = max(1, lanes)
    while lanes > 1 and (total_workers // lanes) < min_workers_per_lane:
        lanes -= 1
    workers_per_lane = max(1, total_workers // lanes)
    return {
        "cpu_count": cpu_count,
        "total_workers": total_workers,
        "lanes": lanes,
        "workers_per_lane": workers_per_lane,
    }


def _outer_mp_context() -> Optional[str]:
    if os.name == "nt":
        return None
    if sys.platform == "darwin":
        return "spawn"
    return "fork"


def _resolve_runtime_config(args: argparse.Namespace) -> Dict[str, Any]:
    defaults = resolve_defaults(profile=str(args.profile), mode=str(args.mode))
    key_map = {
        "seed": "seed",
        "population_size": "population_size",
        "generations": "generations",
        "initial_instructions": "initial_instructions",
        "rounds": "rounds",
        "attacker_population_size": "attacker_population_size",
        "attacker_generations": "attacker_generations",
        "elite_pool": "elite_pool",
        "archive_limit": "archive_limit",
        "continuous_max_iterations": "continuous_max_iterations",
        "workers": "workers",
        "parallel_backend": "parallel_backend",
        "round_parallelism": "round_parallelism",
        "minimum_parallel_rounds": "minimum_parallel_rounds",
        "max_cpu_utilization": "max_cpu_utilization",
        "max_gpu_utilization": "max_gpu_utilization",
        "round_state_sync": "round_state_sync",
        "executor_backend": "executor_backend",
        "kompute_runtime_mode": "kompute_runtime_mode",
        "kompute_warn_on_fallback": "kompute_warn_on_fallback",
        "kompute_fail_hard": "kompute_fail_hard",
        "kompute_keep_vram_state": "kompute_keep_vram_state",
        "kompute_min_native_stage_count": "kompute_min_native_stage_count",
        "kompute_min_native_stage_share": "kompute_min_native_stage_share",
        "kompute_max_unsupported_count": "kompute_max_unsupported_count",
        "kompute_max_unsupported_share": "kompute_max_unsupported_share",
        "kompute_force_cpu_on_partial_coverage": "kompute_force_cpu_on_partial_coverage",
        "kompute_native_enable_decimal": "kompute_native_enable_decimal",
        "kompute_native_enable_boolean_compare": "kompute_native_enable_boolean_compare",
        "kompute_native_enable_boolean_logic": "kompute_native_enable_boolean_logic",
        "kompute_native_enable_list_query": "kompute_native_enable_list_query",
        "kompute_allow_process_pool": "kompute_allow_process_pool",
        "device": "preferred_device",
        "supervised_hidden_layers": "supervised_hidden_layers",
        "supervised_epochs": "supervised_epochs",
        "supervised_candidate_pool": "supervised_candidate_pool",
        "parent_pool_ratio": "parent_pool_ratio",
        "stagnation_patience": "stagnation_patience",
        "mutation_floor": "mutation_floor",
        "mutation_ceiling": "mutation_ceiling",
        "mutation_step": "mutation_step",
        "quick_cycle_fraction": "quick_cycle_fraction",
        "mid_cycle_fraction": "mid_cycle_fraction",
        "quick_keep_ratio": "quick_keep_ratio",
        "mid_keep_ratio": "mid_keep_ratio",
        "key_variants": "key_variants",
        "novelty_bonus": "novelty_bonus",
        "predictive_penalty": "predictive_penalty",
        "sync_loss_gate_percentile": "sync_loss_gate_percentile",
        "sync_loss_gate_penalty": "sync_loss_gate_penalty",
        "sync_loss_gate_flat_boost": "sync_loss_gate_flat_boost",
        "anti_neutrality_window": "anti_neutrality_window",
        "anti_neutrality_penalty": "anti_neutrality_penalty",
        "anti_neutrality_bonus": "anti_neutrality_bonus",
        "attacker_panel_size": "attacker_panel_size",
        "attacker_panel_penalty": "attacker_panel_penalty",
        "target_generation_seconds": "target_generation_seconds",
        "max_eval_cache_entries": "max_eval_cache_entries",
        "device_mhz": "device_mhz",
        "provider_mhz": "provider_mhz",
        "max_test_seconds": "max_test_seconds",
    }

    resolved: Dict[str, Any] = {}
    for arg_key, cfg_key in key_map.items():
        value = getattr(args, arg_key)
        resolved[arg_key] = defaults[cfg_key] if value is None else value

    resolved["use_supervised_guide"] = bool(defaults["use_supervised_guide"]) and not bool(
        args.no_supervised_guide
    )
    if args.supervised_end_round_only is None:
        resolved["supervised_end_round_only"] = bool(
            defaults.get("supervised_end_round_only", False)
        )
    else:
        resolved["supervised_end_round_only"] = bool(args.supervised_end_round_only)
    if args.supervised_capacity_auto_tune is None:
        resolved["supervised_capacity_auto_tune"] = bool(
            defaults.get("supervised_capacity_auto_tune", True)
        )
    else:
        resolved["supervised_capacity_auto_tune"] = bool(args.supervised_capacity_auto_tune)
    if not resolved["use_supervised_guide"]:
        resolved["supervised_end_round_only"] = False
    resolved["supervised_hidden_layers"] = _parse_hidden_layers_spec(
        resolved.get("supervised_hidden_layers")
    )
    backend = str(resolved.get("executor_backend", "auto")).strip().lower()
    if backend not in {"auto", "cpu", "kompute", "kompute-sim"}:
        backend = "auto"
    resolved["executor_backend"] = backend
    kompute_runtime_mode = str(
        resolved.get("kompute_runtime_mode", "native")
    ).strip().lower()
    if kompute_runtime_mode not in {"native", "simulated", "auto"}:
        kompute_runtime_mode = "native"
    resolved["kompute_runtime_mode"] = kompute_runtime_mode
    resolved["kompute_warn_on_fallback"] = bool(
        resolved.get("kompute_warn_on_fallback", True)
    )
    resolved["kompute_fail_hard"] = bool(
        resolved.get("kompute_fail_hard", False)
    )
    resolved["kompute_keep_vram_state"] = bool(
        resolved.get("kompute_keep_vram_state", True)
    )
    resolved["kompute_min_native_stage_count"] = max(
        0,
        int(resolved.get("kompute_min_native_stage_count", 1)),
    )
    resolved["kompute_min_native_stage_share"] = _clamp_float(
        float(resolved.get("kompute_min_native_stage_share", 0.0)),
        0.0,
        1.0,
    )
    resolved["kompute_max_unsupported_count"] = max(
        -1,
        int(resolved.get("kompute_max_unsupported_count", -1)),
    )
    resolved["kompute_max_unsupported_share"] = _clamp_float(
        float(resolved.get("kompute_max_unsupported_share", 1.0)),
        0.0,
        1.0,
    )
    resolved["kompute_force_cpu_on_partial_coverage"] = bool(
        resolved.get("kompute_force_cpu_on_partial_coverage", False)
    )
    resolved["kompute_native_enable_decimal"] = bool(
        resolved.get("kompute_native_enable_decimal", True)
    )
    resolved["kompute_native_enable_boolean_compare"] = bool(
        resolved.get("kompute_native_enable_boolean_compare", True)
    )
    resolved["kompute_native_enable_boolean_logic"] = bool(
        resolved.get("kompute_native_enable_boolean_logic", True)
    )
    resolved["kompute_native_enable_list_query"] = bool(
        resolved.get("kompute_native_enable_list_query", True)
    )
    resolved["kompute_allow_process_pool"] = bool(
        resolved.get("kompute_allow_process_pool", False)
    )
    resolved["round_parallelism"] = max(
        0,
        int(resolved.get("round_parallelism", 0)),
    )
    resolved["minimum_parallel_rounds"] = max(
        1,
        int(resolved.get("minimum_parallel_rounds", 1)),
    )
    resolved["max_cpu_utilization"] = _clamp_float(
        float(resolved.get("max_cpu_utilization", 0.75)),
        0.10,
        1.0,
    )
    resolved["max_gpu_utilization"] = _clamp_float(
        float(resolved.get("max_gpu_utilization", 0.75)),
        0.10,
        1.0,
    )
    round_state_sync = str(resolved.get("round_state_sync", "batch-start")).strip().lower()
    if round_state_sync not in {"batch-start", "batch", "start-only", "round-start"}:
        round_state_sync = "batch-start"
    resolved["round_state_sync"] = round_state_sync
    resolved["supervised_epochs"] = max(0, int(resolved.get("supervised_epochs", 0)))
    resolved["supervised_candidate_pool"] = max(0, int(resolved.get("supervised_candidate_pool", 0)))
    resolved["sync_loss_gate_percentile"] = _clamp_float(
        float(resolved.get("sync_loss_gate_percentile", 0.60)),
        0.0,
        1.0,
    )
    resolved["sync_loss_gate_penalty"] = max(
        0.0,
        float(resolved.get("sync_loss_gate_penalty", 0.10)),
    )
    resolved["sync_loss_gate_flat_boost"] = max(
        0.0,
        float(resolved.get("sync_loss_gate_flat_boost", 0.06)),
    )
    resolved["anti_neutrality_window"] = max(
        1,
        int(resolved.get("anti_neutrality_window", 10)),
    )
    resolved["anti_neutrality_penalty"] = max(
        0.0,
        float(resolved.get("anti_neutrality_penalty", 0.03)),
    )
    resolved["anti_neutrality_bonus"] = max(
        0.0,
        float(resolved.get("anti_neutrality_bonus", 0.015)),
    )
    resolved["attacker_panel_size"] = max(
        1,
        int(resolved.get("attacker_panel_size", 3)),
    )
    resolved["attacker_panel_penalty"] = max(
        0.0,
        float(resolved.get("attacker_panel_penalty", 0.16)),
    )
    resolved["debug_eval_timeout_seconds"] = max(
        0.0,
        float(getattr(args, "debug_eval_timeout_seconds", 0.0)),
    )
    resolved["debug_eval_log_interval_seconds"] = max(
        0.0,
        float(getattr(args, "debug_eval_log_interval_seconds", 0.0)),
    )
    resolved["statistical_predictive"] = bool(defaults["statistical_predictive"]) and not bool(
        args.no_statistical_predictive
    )
    resolved["auto_statistical_tuning"] = bool(defaults["auto_statistical_tuning"]) and not bool(
        args.no_auto_statistical_tuning
    )
    resolved["resume"] = bool(defaults["resume"]) and not bool(args.no_resume)
    resolved["mode"] = str(args.mode)
    resolved["profile"] = str(args.profile)
    resolved["mode_summary"] = mode_summary(str(args.mode))
    return resolved


def _apply_runtime_config(args: argparse.Namespace, resolved: Dict[str, Any]) -> None:
    for key in (
        "seed",
        "population_size",
        "generations",
        "initial_instructions",
        "rounds",
        "attacker_population_size",
        "attacker_generations",
        "elite_pool",
        "archive_limit",
        "continuous_max_iterations",
        "workers",
        "parallel_backend",
        "round_parallelism",
        "minimum_parallel_rounds",
        "max_cpu_utilization",
        "max_gpu_utilization",
        "round_state_sync",
        "executor_backend",
        "kompute_runtime_mode",
        "kompute_warn_on_fallback",
        "kompute_fail_hard",
        "kompute_keep_vram_state",
        "kompute_min_native_stage_count",
        "kompute_min_native_stage_share",
        "kompute_max_unsupported_count",
        "kompute_max_unsupported_share",
        "kompute_force_cpu_on_partial_coverage",
        "kompute_native_enable_decimal",
        "kompute_native_enable_boolean_compare",
        "kompute_native_enable_boolean_logic",
        "kompute_native_enable_list_query",
        "kompute_allow_process_pool",
        "device",
        "supervised_hidden_layers",
        "supervised_epochs",
        "supervised_candidate_pool",
        "parent_pool_ratio",
        "stagnation_patience",
        "mutation_floor",
        "mutation_ceiling",
        "mutation_step",
        "quick_cycle_fraction",
        "mid_cycle_fraction",
        "quick_keep_ratio",
        "mid_keep_ratio",
        "key_variants",
        "novelty_bonus",
        "predictive_penalty",
        "sync_loss_gate_percentile",
        "sync_loss_gate_penalty",
        "sync_loss_gate_flat_boost",
        "anti_neutrality_window",
        "anti_neutrality_penalty",
        "anti_neutrality_bonus",
        "attacker_panel_size",
        "attacker_panel_penalty",
        "debug_eval_timeout_seconds",
        "debug_eval_log_interval_seconds",
        "target_generation_seconds",
        "max_eval_cache_entries",
        "device_mhz",
        "provider_mhz",
        "max_test_seconds",
    ):
        setattr(args, key, resolved[key])
    setattr(args, "use_supervised_guide", bool(resolved["use_supervised_guide"]))
    setattr(
        args,
        "supervised_end_round_only",
        bool(resolved["supervised_end_round_only"]),
    )
    setattr(
        args,
        "supervised_capacity_auto_tune",
        bool(resolved["supervised_capacity_auto_tune"]),
    )
    setattr(args, "statistical_predictive", bool(resolved["statistical_predictive"]))
    setattr(args, "auto_statistical_tuning", bool(resolved["auto_statistical_tuning"]))
    setattr(args, "resume", bool(resolved["resume"]))


def _print_effective_config(resolved: Dict[str, Any]) -> None:
    print(
        "[pcpl-evolvo] config profile={profile} mode={mode} ({summary})".format(
            profile=resolved["profile"],
            mode=resolved["mode"],
            summary=resolved["mode_summary"],
        )
    )
    print(
        "[pcpl-evolvo] evolve pop={pop} gen={gen} rounds={rounds} init={init} atk_pop={apop} atk_gen={agen} elite={elite}".format(
            pop=resolved["population_size"],
            gen=resolved["generations"],
            rounds=resolved["rounds"],
            init=resolved["initial_instructions"],
            apop=resolved["attacker_population_size"],
            agen=resolved["attacker_generations"],
            elite=resolved["elite_pool"],
        )
    )
    print(
        "[pcpl-evolvo] round-parallel lanes={lanes} minimum={minimum} caps(cpu={cpu:.2f},gpu={gpu:.2f}) learned-sync={sync}".format(
            lanes=int(resolved["round_parallelism"]),
            minimum=int(resolved["minimum_parallel_rounds"]),
            cpu=float(resolved["max_cpu_utilization"]),
            gpu=float(resolved["max_gpu_utilization"]),
            sync=str(resolved["round_state_sync"]),
        )
    )
    print(
        "[pcpl-evolvo] dynamics parent_pool={pp:.2f} stagnation={stag} mutation={mf:.2f}..{mc:.2f} step={ms:.2f}".format(
            pp=float(resolved["parent_pool_ratio"]),
            stag=int(resolved["stagnation_patience"]),
            mf=float(resolved["mutation_floor"]),
            mc=float(resolved["mutation_ceiling"]),
            ms=float(resolved["mutation_step"]),
        )
    )
    print(
        "[pcpl-evolvo] executor backend={backend}".format(
            backend=str(resolved["executor_backend"]),
        )
    )
    print(
        "[pcpl-evolvo] kompute mode={mode} keep_vram={keep_vram} warn={warn} fail_hard={fail_hard} "
        "policy(min_stage={min_stage}, min_share={min_share:.2f}, max_unsup={max_unsup}, max_unsup_share={max_unsup_share:.2f}, force_cpu_partial={force_cpu_partial}) "
        "native_families(decimal={decimal}, bool_cmp={bool_cmp}, bool_logic={bool_logic}, list_query={list_query}) "
        "process_pool_allowed={pool_allowed}".format(
            mode=str(resolved["kompute_runtime_mode"]),
            keep_vram=bool(resolved["kompute_keep_vram_state"]),
            warn=bool(resolved["kompute_warn_on_fallback"]),
            fail_hard=bool(resolved["kompute_fail_hard"]),
            min_stage=int(resolved["kompute_min_native_stage_count"]),
            min_share=float(resolved["kompute_min_native_stage_share"]),
            max_unsup=int(resolved["kompute_max_unsupported_count"]),
            max_unsup_share=float(resolved["kompute_max_unsupported_share"]),
            force_cpu_partial=bool(resolved["kompute_force_cpu_on_partial_coverage"]),
            decimal=bool(resolved["kompute_native_enable_decimal"]),
            bool_cmp=bool(resolved["kompute_native_enable_boolean_compare"]),
            bool_logic=bool(resolved["kompute_native_enable_boolean_logic"]),
            list_query=bool(resolved["kompute_native_enable_list_query"]),
            pool_allowed=bool(resolved["kompute_allow_process_pool"]),
        )
    )
    print(
        "[pcpl-evolvo] staged quick={qf:.2f}/{qk:.2f} mid={mf:.2f}/{mk:.2f} key_variants={kv} novelty={nov:.3f} penalty={pen:.3f}".format(
            qf=float(resolved["quick_cycle_fraction"]),
            qk=float(resolved["quick_keep_ratio"]),
            mf=float(resolved["mid_cycle_fraction"]),
            mk=float(resolved["mid_keep_ratio"]),
            kv=int(resolved["key_variants"]),
            nov=float(resolved["novelty_bonus"]),
            pen=float(resolved["predictive_penalty"]),
        )
    )
    print(
        "[pcpl-evolvo] sync-gate pct={pct:.2f} penalty={pen:.3f}+{flat:.3f} anti-neutrality={aw}({ap:.3f}/{ab:.3f}) attacker-panel={panel}@{panel_pen:.3f}".format(
            pct=float(resolved["sync_loss_gate_percentile"]),
            pen=float(resolved["sync_loss_gate_penalty"]),
            flat=float(resolved["sync_loss_gate_flat_boost"]),
            aw=int(resolved["anti_neutrality_window"]),
            ap=float(resolved["anti_neutrality_penalty"]),
            ab=float(resolved["anti_neutrality_bonus"]),
            panel=int(resolved["attacker_panel_size"]),
            panel_pen=float(resolved["attacker_panel_penalty"]),
        )
    )
    supervised_mode = "disabled"
    if bool(resolved["use_supervised_guide"]):
        supervised_mode = (
            "end-round-only"
            if bool(resolved["supervised_end_round_only"])
            else "per-generation"
        )
    print(f"[pcpl-evolvo] supervised guide: {supervised_mode}")
    print(
        "[pcpl-evolvo] supervised tuning layers={layers} epochs={epochs} candidate_pool={pool} capacity_auto_tune={capacity}".format(
            layers=(
                ",".join(str(int(width)) for width in resolved.get("supervised_hidden_layers", []))
                or "auto"
            ),
            epochs=(
                int(resolved.get("supervised_epochs", 0))
                if int(resolved.get("supervised_epochs", 0)) > 0
                else "auto"
            ),
            pool=(
                int(resolved.get("supervised_candidate_pool", 0))
                if int(resolved.get("supervised_candidate_pool", 0)) > 0
                else "auto"
            ),
            capacity=(
                "enabled"
                if bool(resolved.get("supervised_capacity_auto_tune", True))
                else "disabled"
            ),
        )
    )
    print(
        "[pcpl-evolvo] runtime target_gen_s={target:.2f} eval_cache={cache}".format(
            target=float(resolved["target_generation_seconds"]),
            cache=int(resolved["max_eval_cache_entries"]),
        )
    )
    debug_timeout = float(resolved.get("debug_eval_timeout_seconds", 0.0))
    debug_interval = float(resolved.get("debug_eval_log_interval_seconds", 0.0))
    if debug_timeout > 0.0 or debug_interval > 0.0:
        print(
            "[pcpl-evolvo] debug eval monitor timeout_s={timeout:.1f} log_interval_s={interval:.1f}".format(
                timeout=debug_timeout,
                interval=debug_interval,
            )
        )
    if "fitness_schema_version" in resolved:
        print(
            "[pcpl-evolvo] conclusions fitness_schema={schema} analysis_tag={tag} replicates={reps} replicate_ref={rep_ref}".format(
                schema=str(resolved.get("fitness_schema_version", "")),
                tag=str(resolved.get("analysis_tag", "")),
                reps=int(resolved.get("replicates", 1)),
                rep_ref=str(resolved.get("replicate_reference", "best")),
            )
        )
    if str(resolved.get("experiment_suite", "single")) != "single":
        print(
            "[pcpl-evolvo] experiment suite: {suite} tracks={tracks}".format(
                suite=str(resolved.get("experiment_suite", "single")),
                tracks=",".join(
                    str(item) for item in resolved.get("precision_tracks", [])
                )
                or "default",
            )
        )


def _write_json(path: Path, payload: Dict[str, Any]) -> None:
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")


def _normalize_json(value: Any) -> Any:
    if isinstance(value, Path):
        return str(value)
    if dataclasses.is_dataclass(value):
        return _normalize_json(dataclasses.asdict(value))
    if isinstance(value, dict):
        return {str(k): _normalize_json(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_normalize_json(item) for item in value]
    return value


def _git_revision() -> str:
    try:
        output = subprocess.check_output(
            ["git", "rev-parse", "HEAD"],
            cwd=str(PROJECT_DIR.parent),
            stderr=subprocess.DEVNULL,
            text=True,
        ).strip()
        return str(output) if output else "unknown"
    except Exception:
        return "unknown"


def _score_schema_fingerprint() -> str:
    import hashlib

    hasher = hashlib.blake2b(digest_size=8)
    schema_sources = [
        SRC_DIR / "pcpl_evolvo" / "simulation.py",
        SRC_DIR / "pcpl_evolvo" / "experiment.py",
    ]
    for path in schema_sources:
        hasher.update(str(path.name).encode("utf-8"))
        if path.exists():
            hasher.update(path.read_bytes())
        else:
            hasher.update(b"<missing>")
    return hasher.hexdigest()


def _resolve_fitness_schema_version(
    *,
    requested: str,
    mode: str,
    profile: str,
) -> str:
    value = str(requested or "").strip()
    if value and value.lower() != "auto":
        return value
    fp = _score_schema_fingerprint()
    return f"auto-{mode}-{profile}-{fp}"


def _run_metadata_payload(
    *,
    config: ExperimentConfig,
    summary: Dict[str, Any],
    meta: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    payload: Dict[str, Any] = {
        "timestamp": datetime.now().isoformat(),
        "git_revision": _git_revision(),
        "experiment_config": _normalize_json(dataclasses.asdict(config)),
        "summary": _normalize_json(summary),
    }
    if meta:
        payload["launcher_meta"] = _normalize_json(meta)
    return payload


def _run_once(
    config: ExperimentConfig,
    *,
    meta: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    summary = run_experiment(config)
    out_dir = Path(summary["out_dir"])
    summary_path = out_dir / "summary.json"
    _write_json(summary_path, summary)
    run_meta_path = out_dir / "run-metadata.json"
    _write_json(
        run_meta_path,
        _run_metadata_payload(
            config=config,
            summary=summary,
            meta=meta,
        ),
    )
    summary["run_metadata_path"] = str(run_meta_path)
    return summary


def _select_replicate_reference_index(
    summaries: List[Dict[str, Any]],
    *,
    policy: str,
) -> int:
    if not summaries:
        return 0
    normalized = str(policy or "best").strip().lower()
    if normalized == "first":
        return 0
    indexed = list(enumerate(summaries))
    if normalized == "median":
        ordered = sorted(indexed, key=lambda item: float(item[1].get("best_score", float("-inf"))))
        return int(ordered[len(ordered) // 2][0])
    best = max(indexed, key=lambda item: float(item[1].get("best_score", float("-inf"))))
    return int(best[0])


def _print_summary(summary: Dict[str, Any]) -> None:
    print("[pcpl-evolvo] completed")
    print(f"[pcpl-evolvo] out_dir={summary['out_dir']}")
    print(f"[pcpl-evolvo] best_score={summary['best_score']:.6f}")
    print(f"[pcpl-evolvo] best_attacker_score={summary['best_attacker_score']:.6f}")
    if summary.get("reference_score") is not None:
        ref = float(summary["reference_score"])
        print(f"[pcpl-evolvo] reference_score={ref:.6f}")
        print(f"[pcpl-evolvo] delta_vs_reference={(float(summary['best_score']) - ref):+.6f}")
    print(f"[pcpl-evolvo] rounds_completed={summary['rounds_completed']}")
    print(f"[pcpl-evolvo] results={summary['results_json']}")
    print(f"[pcpl-evolvo] report={summary['report_path']}")
    print(f"[pcpl-evolvo] archive={summary['archive_path']}")
    if "resource_plan" in summary:
        plan = summary["resource_plan"]
        per_round = plan.get("per_round_resource_plan", {}) if isinstance(plan, dict) else {}
        if not isinstance(per_round, dict):
            per_round = {}
        round_parallel = plan.get("round_parallel_plan", {}) if isinstance(plan, dict) else {}
        if not isinstance(round_parallel, dict):
            round_parallel = {}
        print(
            "[pcpl-evolvo] resources backend={backend} workers={workers} gpu={gpu} exec={exec_backend}".format(
                backend=per_round.get("parallel_backend", plan.get("parallel_backend")),
                workers=per_round.get("parallel_workers", plan.get("parallel_workers")),
                gpu=plan.get("gpu_backend"),
                exec_backend=plan.get("executor_backend", "cpu"),
            )
        )
        if round_parallel:
            print(
                "[pcpl-evolvo] round-parallel lanes={lanes} workers-per-round={workers} sync={sync}".format(
                    lanes=round_parallel.get("lanes", 1),
                    workers=round_parallel.get(
                        "workers_per_round",
                        per_round.get("parallel_workers", plan.get("parallel_workers", 1)),
                    ),
                    sync=round_parallel.get("learning_sync", "batch-start"),
                )
            )
    if "index_path" in summary:
        print(f"[pcpl-evolvo] index={summary['index_path']}")
    if "conclusion_path" in summary:
        print(f"[pcpl-evolvo] conclusions={summary['conclusion_path']}")
    if "run_metadata_path" in summary:
        print(f"[pcpl-evolvo] run_metadata={summary['run_metadata_path']}")


def _run_noncontinuous_campaign(
    args: argparse.Namespace,
    *,
    out_dir: Path,
    base_meta: Dict[str, Any],
    config_overrides: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    if int(args.replicates) == 1:
        config = _build_experiment_config_from_args(
            args,
            out_dir=out_dir,
            seed=int(args.seed),
            resume=bool(args.resume),
            workers=int(args.workers),
            overrides=config_overrides,
        )
        summary = _run_once(config, meta=base_meta)
        _print_summary(summary)
        return {"kind": "single", "summary": summary}

    out_dir.mkdir(parents=True, exist_ok=True)
    replicate_summaries: List[Dict[str, Any]] = []
    base_seed = int(args.seed)
    seed_step = int(args.replicate_seed_step)
    print(
        "[pcpl-evolvo] replicate campaign: n={n} base_seed={seed} step={step} root={root}".format(
            n=int(args.replicates),
            seed=base_seed,
            step=seed_step,
            root=out_dir,
        )
    )
    for rep in range(int(args.replicates)):
        rep_seed = base_seed + (rep * seed_step)
        rep_out_dir = (out_dir / f"rep-{rep:03d}").resolve()
        config = _build_experiment_config_from_args(
            args,
            out_dir=rep_out_dir,
            seed=rep_seed,
            resume=False,
            workers=int(args.workers),
            overrides=config_overrides,
        )
        rep_meta = dict(base_meta)
        rep_meta["replicate_index"] = rep
        rep_meta["replicate_seed"] = rep_seed
        rep_meta["replicate_count"] = int(args.replicates)
        summary = _run_once(config, meta=rep_meta)
        _print_summary(summary)
        replicate_summaries.append(summary)

    best_scores = [float(item["best_score"]) for item in replicate_summaries]
    attacker_scores = [
        float(item["best_attacker_score"]) for item in replicate_summaries
    ]
    round_counts = [
        int(item.get("rounds_completed", 0)) for item in replicate_summaries
    ]
    ref_idx = _select_replicate_reference_index(
        replicate_summaries,
        policy=str(args.replicate_reference),
    )
    ref_summary = replicate_summaries[ref_idx]
    campaign_summary: Dict[str, Any] = {
        "timestamp": datetime.now().isoformat(),
        "analysis_tag": str(args.analysis_tag or ""),
        "fitness_schema_version": str(args.fitness_schema_version),
        "mode": str(args.mode),
        "profile": str(args.profile),
        "replicate_count": int(args.replicates),
        "base_seed": base_seed,
        "seed_step": seed_step,
        "reference_policy": str(args.replicate_reference),
        "reference_replicate_index": int(ref_idx),
        "reference_out_dir": str(ref_summary.get("out_dir", "")),
        "reference_best_score": float(
            ref_summary.get("best_score", float("-inf"))
        ),
        "best_score_mean": sum(best_scores) / float(max(1, len(best_scores))),
        "best_score_min": min(best_scores) if best_scores else None,
        "best_score_max": max(best_scores) if best_scores else None,
        "best_attacker_score_mean": sum(attacker_scores)
        / float(max(1, len(attacker_scores))),
        "rounds_completed_mean": sum(round_counts)
        / float(max(1, len(round_counts))),
        "runs": [
            {
                "out_dir": item.get("out_dir"),
                "best_score": item.get("best_score"),
                "best_attacker_score": item.get("best_attacker_score"),
                "rounds_completed": item.get("rounds_completed"),
                "archive_path": item.get("archive_path"),
                "report_path": item.get("report_path"),
                "run_metadata_path": item.get("run_metadata_path"),
            }
            for item in replicate_summaries
        ],
    }
    campaign_path = out_dir / "replicates-summary.json"
    _write_json(campaign_path, campaign_summary)
    reference_path = out_dir / "reference-run.json"
    _write_json(
        reference_path,
        {
            "timestamp": datetime.now().isoformat(),
            "analysis_tag": str(args.analysis_tag or ""),
            "fitness_schema_version": str(args.fitness_schema_version),
            "policy": str(args.replicate_reference),
            "replicate_index": int(ref_idx),
            "summary": ref_summary,
        },
    )
    print(f"[pcpl-evolvo] replicates_summary={campaign_path}")
    print(f"[pcpl-evolvo] replicate_reference={reference_path}")
    return {
        "kind": "replicates",
        "campaign_summary": campaign_summary,
        "reference_summary": ref_summary,
        "campaign_path": str(campaign_path),
        "reference_path": str(reference_path),
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="PCPL continuous empirical/evolutionary runner (Evolvo-backed)."
    )
    parser.add_argument(
        "--mode",
        choices=available_modes(),
        default=DEFAULT_MODE,
        help=(
            "High-level evolution mode loaded from config.py. "
            "Use this instead of tuning many low-level flags."
        ),
    )
    parser.add_argument(
        "--list-modes",
        action="store_true",
        help="Print available modes from config.py and exit.",
    )
    parser.add_argument(
        "--print-effective-config",
        action="store_true",
        help="Print resolved config (mode + profile + CLI overrides) before run.",
    )
    parser.add_argument(
        "--kompute-check-libs",
        action="store_true",
        help=(
            "Run Vulkan/Kompute dependency diagnostics (loader, ICDs, vulkaninfo, kp manager init) and exit."
        ),
    )
    parser.add_argument(
        "--kompute-self-test",
        action="store_true",
        help=(
            "Run a fast native Kompute smoke test (raw kp dispatch + evolvo native executor) and exit."
        ),
    )
    parser.add_argument(
        "--summarize-run",
        type=str,
        default="",
        help=(
            "Regenerate leaderboards, best snapshots, conclusions, and evidence-summary.json "
            "for an existing run directory, then exit without running evolution."
        ),
    )
    parser.add_argument(
        "--profile",
        choices=("fast", "full"),
        default=DEFAULT_PROFILE,
        help=(
            "Scenario profile loaded from config.py default. "
            "full is preferred for robust conclusions."
        ),
    )
    parser.add_argument("--seed", type=int, default=None, help="Random seed.")
    parser.add_argument(
        "--population-size",
        type=int,
        default=None,
        help="Defender population size.",
    )
    parser.add_argument(
        "--generations",
        type=int,
        default=None,
        help="Defender generations per round.",
    )
    parser.add_argument(
        "--initial-instructions",
        type=int,
        default=None,
        help="Max random seed instruction count.",
    )
    parser.add_argument(
        "--rounds",
        type=int,
        default=None,
        help="Continuous rounds to run in this invocation.",
    )
    parser.add_argument(
        "--attacker-population-size",
        type=int,
        default=None,
        help="Attacker population size per round.",
    )
    parser.add_argument(
        "--attacker-generations",
        type=int,
        default=None,
        help="Attacker generations per round.",
    )
    parser.add_argument(
        "--elite-pool",
        type=int,
        default=None,
        help="Number of top archived genomes used to seed each new round.",
    )
    parser.add_argument(
        "--archive-limit",
        type=int,
        default=None,
        help="Max defender/attacker elites kept in archive.",
    )
    parser.add_argument(
        "--no-resume",
        action="store_true",
        help="Do not load previous archive state from --out-dir.",
    )
    parser.add_argument(
        "--out-dir",
        type=str,
        default="",
        help=(
            "Output directory. Default: "
            "demo/pcpl-evolvo/runs/<timestamp>-<profile>"
        ),
    )
    parser.add_argument(
        "--continuous",
        action="store_true",
        help=(
            "Run forever (until Ctrl+C), sweeping all generated parameter "
            "combinations and continuously saving archives/stats."
        ),
    )
    parser.add_argument(
        "--continuous-max-iterations",
        type=int,
        default=None,
        help=(
            "Optional cap for continuous mode iterations; 0 means infinite "
            "(until user stop)."
        ),
    )
    parser.add_argument(
        "--workers",
        type=int,
        default=None,
        help=(
            "Parallel fitness workers. 0 means auto (use all available CPU cores)."
        ),
    )
    parser.add_argument(
        "--parallel-backend",
        choices=("auto", "process", "thread", "off"),
        default=None,
        help="Parallel backend for fitness evaluation.",
    )
    parser.add_argument(
        "--round-parallelism",
        type=int,
        default=None,
        help=(
            "Concurrent round lanes. 0 means auto (safe batch-start snapshots, merged in round order)."
        ),
    )
    parser.add_argument(
        "--minimum-parallel-rounds",
        type=int,
        default=None,
        help=(
            "Minimum concurrent round lanes to enforce (best effort, bounded by rounds/workers)."
        ),
    )
    parser.add_argument(
        "--max-cpu-utilization",
        type=float,
        default=None,
        help="Upper CPU utilization budget in [0,1] used to cap worker planning.",
    )
    parser.add_argument(
        "--max-gpu-utilization",
        type=float,
        default=None,
        help="Upper GPU utilization budget in [0,1] used to cap worker planning.",
    )
    parser.add_argument(
        "--round-state-sync",
        choices=("batch-start", "batch", "start-only", "round-start"),
        default=None,
        help=(
            "When running concurrent round lanes, share learned/archive state only at batch start."
        ),
    )
    parser.add_argument(
        "--executor-backend",
        choices=("auto", "cpu", "kompute", "kompute-sim"),
        default=None,
        help=(
            "Execution backend for GFSL program evaluation. "
            "`kompute-sim` runs Kompute compatibility/planning checks plus simulated execution."
        ),
    )
    parser.add_argument(
        "--kompute-runtime-mode",
        choices=("native", "simulated", "auto"),
        default=None,
        help="Kompute runtime mode for `kompute` backend.",
    )
    parser.add_argument(
        "--kompute-min-native-stage-count",
        type=int,
        default=None,
        help="Minimum native GPU stage count required before allowing Kompute hybrid execution.",
    )
    parser.add_argument(
        "--kompute-min-native-stage-share",
        type=float,
        default=None,
        help="Minimum native stage share (0..1) required before allowing Kompute hybrid execution.",
    )
    parser.add_argument(
        "--kompute-max-unsupported-count",
        type=int,
        default=None,
        help="Maximum unsupported stage count allowed in Kompute hybrid mode (-1 disables check).",
    )
    parser.add_argument(
        "--kompute-max-unsupported-share",
        type=float,
        default=None,
        help="Maximum unsupported stage share (0..1) allowed in Kompute hybrid mode.",
    )
    kompute_warn_group = parser.add_mutually_exclusive_group()
    kompute_warn_group.add_argument(
        "--kompute-warn-on-fallback",
        dest="kompute_warn_on_fallback",
        action="store_true",
        default=None,
        help="Enable Kompute fallback warnings.",
    )
    kompute_warn_group.add_argument(
        "--no-kompute-warn-on-fallback",
        dest="kompute_warn_on_fallback",
        action="store_false",
        help="Disable Kompute fallback warnings.",
    )
    kompute_fail_group = parser.add_mutually_exclusive_group()
    kompute_fail_group.add_argument(
        "--kompute-fail-hard",
        dest="kompute_fail_hard",
        action="store_true",
        default=None,
        help="Fail immediately when Kompute initialization/execution cannot proceed.",
    )
    kompute_fail_group.add_argument(
        "--no-kompute-fail-hard",
        dest="kompute_fail_hard",
        action="store_false",
        help="Allow CPU fallback when Kompute cannot proceed.",
    )
    kompute_vram_group = parser.add_mutually_exclusive_group()
    kompute_vram_group.add_argument(
        "--kompute-keep-vram-state",
        dest="kompute_keep_vram_state",
        action="store_true",
        default=None,
        help="Keep VRAM-backed state between Kompute stages when possible.",
    )
    kompute_vram_group.add_argument(
        "--no-kompute-keep-vram-state",
        dest="kompute_keep_vram_state",
        action="store_false",
        help="Disable persistent VRAM state for Kompute stages.",
    )
    kompute_partial_group = parser.add_mutually_exclusive_group()
    kompute_partial_group.add_argument(
        "--kompute-force-cpu-on-partial-coverage",
        dest="kompute_force_cpu_on_partial_coverage",
        action="store_true",
        default=None,
        help="Force CPU execution whenever Kompute compatibility reports unsupported stages.",
    )
    kompute_partial_group.add_argument(
        "--no-kompute-force-cpu-on-partial-coverage",
        dest="kompute_force_cpu_on_partial_coverage",
        action="store_false",
        help="Allow hybrid Kompute+CPU execution on partial coverage.",
    )
    kompute_native_decimal_group = parser.add_mutually_exclusive_group()
    kompute_native_decimal_group.add_argument(
        "--kompute-native-enable-decimal",
        dest="kompute_native_enable_decimal",
        action="store_true",
        default=None,
        help="Enable native Kompute decimal shader family.",
    )
    kompute_native_decimal_group.add_argument(
        "--no-kompute-native-enable-decimal",
        dest="kompute_native_enable_decimal",
        action="store_false",
        help="Disable native Kompute decimal shader family (force CPU fallback).",
    )
    kompute_native_bool_cmp_group = parser.add_mutually_exclusive_group()
    kompute_native_bool_cmp_group.add_argument(
        "--kompute-native-enable-boolean-compare",
        dest="kompute_native_enable_boolean_compare",
        action="store_true",
        default=None,
        help="Enable native Kompute boolean-compare shader family.",
    )
    kompute_native_bool_cmp_group.add_argument(
        "--no-kompute-native-enable-boolean-compare",
        dest="kompute_native_enable_boolean_compare",
        action="store_false",
        help="Disable native Kompute boolean-compare shader family (force CPU fallback).",
    )
    kompute_native_bool_logic_group = parser.add_mutually_exclusive_group()
    kompute_native_bool_logic_group.add_argument(
        "--kompute-native-enable-boolean-logic",
        dest="kompute_native_enable_boolean_logic",
        action="store_true",
        default=None,
        help="Enable native Kompute boolean-logic shader family.",
    )
    kompute_native_bool_logic_group.add_argument(
        "--no-kompute-native-enable-boolean-logic",
        dest="kompute_native_enable_boolean_logic",
        action="store_false",
        help="Disable native Kompute boolean-logic shader family (force CPU fallback).",
    )
    kompute_native_list_query_group = parser.add_mutually_exclusive_group()
    kompute_native_list_query_group.add_argument(
        "--kompute-native-enable-list-query",
        dest="kompute_native_enable_list_query",
        action="store_true",
        default=None,
        help="Enable native Kompute list-query shader family.",
    )
    kompute_native_list_query_group.add_argument(
        "--no-kompute-native-enable-list-query",
        dest="kompute_native_enable_list_query",
        action="store_false",
        help="Disable native Kompute list-query shader family (force CPU fallback).",
    )
    kompute_pool_group = parser.add_mutually_exclusive_group()
    kompute_pool_group.add_argument(
        "--kompute-allow-process-pool",
        dest="kompute_allow_process_pool",
        action="store_true",
        default=None,
        help="Allow process-pool backend in Kompute mode (advanced; may be unstable).",
    )
    kompute_pool_group.add_argument(
        "--no-kompute-allow-process-pool",
        dest="kompute_allow_process_pool",
        action="store_false",
        help="Auto-downgrade process-pool to threads in Kompute mode.",
    )
    parser.add_argument(
        "--no-supervised-guide",
        action="store_true",
        help="Disable optional supervised guide acceleration.",
    )
    supervised_group = parser.add_mutually_exclusive_group()
    supervised_group.add_argument(
        "--supervised-end-round-only",
        dest="supervised_end_round_only",
        action="store_true",
        default=None,
        help="Train supervised guide only once per round (after all generations).",
    )
    supervised_group.add_argument(
        "--no-supervised-end-round-only",
        dest="supervised_end_round_only",
        action="store_false",
        help="Use supervised guide each generation (higher overhead).",
    )
    parser.add_argument(
        "--device",
        choices=("auto", "cpu", "cuda", "rocm", "mps"),
        default=None,
        help="Preferred compute device for supervised guide.",
    )
    parser.add_argument(
        "--supervised-hidden-layers",
        type=str,
        default=None,
        help=(
            "Comma-separated hidden layer widths for supervised model "
            "(example: 384,256,160,96). Use 'auto' to keep profile defaults."
        ),
    )
    parser.add_argument(
        "--supervised-epochs",
        type=int,
        default=None,
        help="Override supervised training epochs per update (0/omit keeps auto defaults).",
    )
    parser.add_argument(
        "--supervised-candidate-pool",
        type=int,
        default=None,
        help="Override supervised mutation candidate pool size (0/omit keeps auto defaults).",
    )
    supervised_capacity_group = parser.add_mutually_exclusive_group()
    supervised_capacity_group.add_argument(
        "--supervised-capacity-auto-tune",
        dest="supervised_capacity_auto_tune",
        action="store_true",
        default=None,
        help="Enable adaptive supervised model capacity control (recommended).",
    )
    supervised_capacity_group.add_argument(
        "--no-supervised-capacity-auto-tune",
        dest="supervised_capacity_auto_tune",
        action="store_false",
        help="Disable adaptive supervised model capacity control.",
    )
    parser.add_argument(
        "--parent-pool-ratio",
        type=float,
        default=None,
        help="Fraction of top genomes used as parent pool (less random dispersivity).",
    )
    parser.add_argument(
        "--stagnation-patience",
        type=int,
        default=None,
        help="Generations without improvement before increasing mutation pressure.",
    )
    parser.add_argument(
        "--mutation-floor",
        type=float,
        default=None,
        help="Minimum adaptive mutation rate.",
    )
    parser.add_argument(
        "--mutation-ceiling",
        type=float,
        default=None,
        help="Maximum adaptive mutation rate.",
    )
    parser.add_argument(
        "--mutation-step",
        type=float,
        default=None,
        help="Adaptive mutation step when stagnating/improving.",
    )
    parser.add_argument(
        "--no-statistical-predictive",
        action="store_true",
        help="Disable staged statistical evaluation and run full evaluation on all genomes.",
    )
    parser.add_argument(
        "--quick-cycle-fraction",
        type=float,
        default=None,
        help="Initial fraction of cycles used by quick stage (auto-tuned at runtime).",
    )
    parser.add_argument(
        "--mid-cycle-fraction",
        type=float,
        default=None,
        help="Initial fraction of cycles used by medium stage (auto-tuned at runtime).",
    )
    parser.add_argument(
        "--quick-keep-ratio",
        type=float,
        default=None,
        help="Initial fraction of genomes kept after quick stage (auto-tuned at runtime).",
    )
    parser.add_argument(
        "--mid-keep-ratio",
        type=float,
        default=None,
        help="Initial fraction of genomes kept after medium stage (auto-tuned at runtime).",
    )
    parser.add_argument(
        "--key-variants",
        type=int,
        default=None,
        help="Initial key generation/sharing variants per scenario (auto-tuned at runtime).",
    )
    parser.add_argument(
        "--no-auto-statistical-tuning",
        action="store_true",
        help="Keep staged fractions/ratios fixed to CLI values (disable real-time auto-tuning).",
    )
    parser.add_argument(
        "--novelty-bonus",
        type=float,
        default=None,
        help="Fitness bonus for novel non-duplicate genomes during staged ranking.",
    )
    parser.add_argument(
        "--predictive-penalty",
        type=float,
        default=None,
        help="Penalty applied when a genome is cut by predictive stages.",
    )
    parser.add_argument(
        "--sync-loss-gate-percentile",
        type=float,
        default=None,
        help="Defender full-stage percentile threshold for projected_sync_loss_rate gate.",
    )
    parser.add_argument(
        "--sync-loss-gate-penalty",
        type=float,
        default=None,
        help="Base fitness penalty for defenders above sync-loss gate threshold.",
    )
    parser.add_argument(
        "--sync-loss-gate-flat-boost",
        type=float,
        default=None,
        help="Extra sync-loss gate penalty added when recent generations are flat.",
    )
    parser.add_argument(
        "--anti-neutrality-window",
        type=int,
        default=None,
        help="Defender generations window used to detect repeated phenotype fingerprints.",
    )
    parser.add_argument(
        "--anti-neutrality-penalty",
        type=float,
        default=None,
        help="Penalty for defender candidates repeating recent phenotype fingerprint.",
    )
    parser.add_argument(
        "--anti-neutrality-bonus",
        type=float,
        default=None,
        help="Bonus for defender candidates improving sync/horizon while escaping repeats.",
    )
    parser.add_argument(
        "--attacker-panel-size",
        type=int,
        default=None,
        help="Round-selection attacker panel size used for defender robust ranking.",
    )
    parser.add_argument(
        "--attacker-panel-penalty",
        type=float,
        default=None,
        help="Penalty multiplier for worst attacker advantage in defender panel ranking.",
    )
    parser.add_argument(
        "--target-generation-seconds",
        type=float,
        default=None,
        help="Target max wall-time per generation batch used by auto-tuning/early-stop.",
    )
    parser.add_argument(
        "--max-eval-cache-entries",
        type=int,
        default=None,
        help="Per-round dedup cache capacity for reuse of evaluated genome signatures.",
    )
    parser.add_argument(
        "--debug-eval-timeout-seconds",
        type=float,
        default=0.0,
        help=(
            "Debug-only stall timeout watchdog for parallel evaluators. "
            "When > 0, prints timeout diagnostics if no task completes for this duration."
        ),
    )
    parser.add_argument(
        "--debug-eval-log-interval-seconds",
        type=float,
        default=0.0,
        help=(
            "Debug-only heartbeat interval for parallel evaluator progress logs. "
            "0 disables heartbeat logs."
        ),
    )
    parser.add_argument(
        "--device-mhz",
        type=float,
        default=None,
        help="Simulated consumer device frequency in MHz.",
    )
    parser.add_argument(
        "--provider-mhz",
        type=float,
        default=None,
        help="Simulated provider frequency in MHz.",
    )
    parser.add_argument(
        "--max-test-seconds",
        type=float,
        default=None,
        help="Long-horizon timing projection target (seconds).",
    )
    parser.add_argument(
        "--analysis-tag",
        type=str,
        default="",
        help="Optional label stored in run metadata for later conclusion aggregation.",
    )
    parser.add_argument(
        "--experiment-suite",
        choices=EXPERIMENT_SUITE_CHOICES,
        default="single",
        help=(
            "Runner orchestration style: `single` keeps current behavior; "
            "`precision` launches targeted tracks that reduce blind shortcuts and "
            "separate baseline, supervision, lane-pressure, evaluability, and random-research lanes."
        ),
    )
    parser.add_argument(
        "--precision-tracks",
        type=str,
        default=",".join(PRECISION_TRACK_CHOICES),
        help=(
            "Comma-separated track subset used when --experiment-suite precision. "
            f"Supported: {', '.join(PRECISION_TRACK_CHOICES)}."
        ),
    )
    parser.add_argument(
        "--fitness-schema-version",
        type=str,
        default=DEFAULT_FITNESS_SCHEMA_VERSION,
        help=(
            "Fitness/scoring schema label stored with each run. "
            "Use 'auto' (default) to derive a deterministic schema reference from current scoring sources."
        ),
    )
    parser.add_argument(
        "--replicates",
        type=int,
        default=1,
        help=(
            "Number of independent single-run replications (same config, different seeds, separate out dirs)."
        ),
    )
    parser.add_argument(
        "--replicate-seed-step",
        type=int,
        default=7919,
        help="Seed increment between replicates when --replicates > 1.",
    )
    parser.add_argument(
        "--replicate-reference",
        choices=("best", "median", "first"),
        default="best",
        help=(
            "Canonical replicate selector for multi-run campaigns: "
            "best (max score), median (middle score), or first (rep-000)."
        ),
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    rounds_explicit = args.rounds is not None
    if args.list_modes:
        print("[pcpl-evolvo] available modes:")
        for name in available_modes():
            print(f"- {name}: {mode_summary(name)}")
        return
    if args.kompute_check_libs:
        check_ok = _run_kompute_library_check()
        if not check_ok:
            raise SystemExit(2)
        if not args.kompute_self_test:
            return
    if args.kompute_self_test:
        success = _run_kompute_self_test()
        if not success:
            raise SystemExit(2)
        return
    if str(args.summarize_run or "").strip():
        summary = materialize_existing_run_views(Path(args.summarize_run))
        print("[pcpl-evolvo] summarized existing run")
        print(f"[pcpl-evolvo] out_dir={summary['out_dir']}")
        print(
            "[pcpl-evolvo] rounds completed={completed} valid={valid} skipped={skipped}".format(
                completed=int(summary.get("rounds_completed", 0)),
                valid=int(summary.get("valid_rounds", 0)),
                skipped=int(summary.get("skipped_rounds", 0)),
            )
        )
        if math.isfinite(float(summary.get("best_score", float("-inf")))):
            print(f"[pcpl-evolvo] best_score={float(summary['best_score']):.6f}")
        if math.isfinite(float(summary.get("best_attacker_score", float("-inf")))):
            print(
                f"[pcpl-evolvo] best_attacker_score={float(summary['best_attacker_score']):.6f}"
            )
        if summary.get("score_delta_vs_reference") is not None:
            print(
                "[pcpl-evolvo] delta_vs_reference={delta:+.6f}".format(
                    delta=float(summary["score_delta_vs_reference"]),
                )
            )
        print(f"[pcpl-evolvo] conclusions={summary['conclusion_path']}")
        print(f"[pcpl-evolvo] evidence={summary['evidence_summary_path']}")
        print(f"[pcpl-evolvo] index={summary['index_path']}")
        print(f"[pcpl-evolvo] materialized_summary={summary['materialized_summary_path']}")
        return

    resolved = _resolve_runtime_config(args)
    _apply_runtime_config(args, resolved)
    args.fitness_schema_version = _resolve_fitness_schema_version(
        requested=str(args.fitness_schema_version),
        mode=str(args.mode),
        profile=str(args.profile),
    )
    args.precision_tracks = _parse_precision_tracks_spec(args.precision_tracks)
    if int(args.replicate_seed_step) <= 0:
        raise ValueError("--replicate-seed-step must be > 0")
    if int(args.supervised_epochs) < 0:
        raise ValueError("--supervised-epochs must be >= 0")
    if int(args.supervised_candidate_pool) < 0:
        raise ValueError("--supervised-candidate-pool must be >= 0")
    if not (0.0 <= float(args.sync_loss_gate_percentile) <= 1.0):
        raise ValueError("--sync-loss-gate-percentile must be in [0, 1]")
    if float(args.sync_loss_gate_penalty) < 0.0:
        raise ValueError("--sync-loss-gate-penalty must be >= 0")
    if float(args.sync_loss_gate_flat_boost) < 0.0:
        raise ValueError("--sync-loss-gate-flat-boost must be >= 0")
    if int(args.anti_neutrality_window) < 1:
        raise ValueError("--anti-neutrality-window must be >= 1")
    if float(args.anti_neutrality_penalty) < 0.0:
        raise ValueError("--anti-neutrality-penalty must be >= 0")
    if float(args.anti_neutrality_bonus) < 0.0:
        raise ValueError("--anti-neutrality-bonus must be >= 0")
    if int(args.attacker_panel_size) < 1:
        raise ValueError("--attacker-panel-size must be >= 1")
    if float(args.attacker_panel_penalty) < 0.0:
        raise ValueError("--attacker-panel-penalty must be >= 0")
    if float(args.debug_eval_timeout_seconds) < 0.0:
        raise ValueError("--debug-eval-timeout-seconds must be >= 0")
    if float(args.debug_eval_log_interval_seconds) < 0.0:
        raise ValueError("--debug-eval-log-interval-seconds must be >= 0")
    if int(args.round_parallelism) < 0:
        raise ValueError("--round-parallelism must be >= 0")
    if int(args.minimum_parallel_rounds) < 1:
        raise ValueError("--minimum-parallel-rounds must be >= 1")
    if not (0.0 < float(args.max_cpu_utilization) <= 1.0):
        raise ValueError("--max-cpu-utilization must be in (0, 1]")
    if not (0.0 < float(args.max_gpu_utilization) <= 1.0):
        raise ValueError("--max-gpu-utilization must be in (0, 1]")
    resolved["fitness_schema_version"] = str(args.fitness_schema_version)
    resolved["analysis_tag"] = str(args.analysis_tag or "")
    resolved["replicates"] = int(args.replicates)
    resolved["replicate_reference"] = str(args.replicate_reference)
    resolved["experiment_suite"] = str(args.experiment_suite)
    resolved["precision_tracks"] = list(args.precision_tracks)
    if (
        args.continuous
        and str(args.experiment_suite).lower() == "precision"
        and not rounds_explicit
    ):
        args.rounds = 1
        resolved["rounds"] = 1
        print(
            "[pcpl-evolvo] precision continuous default: using rounds=1 per iteration for targeted cadence (set --rounds to override)."
        )
    elif args.continuous and str(args.mode).lower() == "paper" and not rounds_explicit:
        # In continuous paper sweeps, prioritize cadence across many combos/strategies.
        args.rounds = 1
        resolved["rounds"] = 1
        print(
            "[pcpl-evolvo] paper continuous default: using rounds=1 per iteration for faster signal cadence (set --rounds to override)."
        )
    _print_effective_config(resolved)
    if args.print_effective_config:
        return

    if args.continuous and int(args.replicates) != 1:
        print("[pcpl-evolvo] warning: --replicates is ignored in --continuous mode")

    if args.out_dir:
        out_dir = Path(args.out_dir).expanduser().resolve()
    else:
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        out_dir = (PROJECT_DIR / "runs" / f"{stamp}-{args.profile}").resolve()

    if not args.continuous:
        if int(args.replicates) < 1:
            raise ValueError("--replicates must be >= 1")

        base_meta = {
            "analysis_tag": str(args.analysis_tag or ""),
            "fitness_schema_version": str(args.fitness_schema_version),
            "mode": str(args.mode),
            "profile": str(args.profile),
            "replicate_reference_policy": str(args.replicate_reference),
            "resolved_config": resolved,
            "continuous": False,
            "launcher": "run_experiments.py",
        }
        if str(args.experiment_suite).lower() == "precision":
            out_dir.mkdir(parents=True, exist_ok=True)
            precision_profiles = [
                profile
                for profile in _precision_strategy_profiles(args)
                if str(profile.get("strategy", "")) in set(args.precision_tracks)
            ]
            if not precision_profiles:
                raise RuntimeError("Precision suite selected but no tracks were enabled")
            print(
                "[pcpl-evolvo] precision suite: tracks={tracks} root={root}".format(
                    tracks=",".join(
                        str(profile.get("strategy", "")) for profile in precision_profiles
                    ),
                    root=out_dir,
                )
            )
            suite_results: List[Dict[str, Any]] = []
            for profile in precision_profiles:
                track_name = str(profile.get("strategy", "baseline"))
                track_dir = (out_dir / track_name).resolve()
                print(
                    "[pcpl-evolvo] precision track={track} out_dir={out} desc={desc}".format(
                        track=track_name,
                        out=track_dir,
                        desc=str(profile.get("description", "")).strip() or "n/a",
                    )
                )
                track_meta = dict(base_meta)
                track_meta["experiment_suite"] = "precision"
                track_meta["suite_track"] = track_name
                track_meta["suite_track_description"] = str(
                    profile.get("description", "")
                )
                effective_tag = str(args.analysis_tag or "").strip()
                track_meta["effective_analysis_tag"] = (
                    f"{effective_tag}:{track_name}" if effective_tag else track_name
                )
                campaign_result = _run_noncontinuous_campaign(
                    args,
                    out_dir=track_dir,
                    base_meta=track_meta,
                    config_overrides=profile,
                )
                if campaign_result["kind"] == "single":
                    summary = campaign_result["summary"]
                    suite_results.append(
                        {
                            "track": track_name,
                            "description": str(profile.get("description", "")),
                            "kind": "single",
                            "best_score": float(summary.get("best_score", float("-inf"))),
                            "best_attacker_score": float(
                                summary.get("best_attacker_score", float("-inf"))
                            ),
                            "rounds_completed": int(summary.get("rounds_completed", 0)),
                            "out_dir": str(track_dir),
                            "report_path": summary.get("report_path"),
                            "archive_path": summary.get("archive_path"),
                            "run_metadata_path": summary.get("run_metadata_path"),
                            "overrides": {
                                str(k): _normalize_json(v)
                                for k, v in profile.items()
                                if str(k) not in {"strategy", "description"}
                            },
                        }
                    )
                else:
                    campaign_summary = campaign_result["campaign_summary"]
                    reference_summary = campaign_result["reference_summary"]
                    suite_results.append(
                        {
                            "track": track_name,
                            "description": str(profile.get("description", "")),
                            "kind": "replicates",
                            "reference_best_score": float(
                                campaign_summary.get(
                                    "reference_best_score",
                                    float("-inf"),
                                )
                            ),
                            "best_score_mean": float(
                                campaign_summary.get("best_score_mean", float("-inf"))
                            ),
                            "best_attacker_score_mean": float(
                                campaign_summary.get(
                                    "best_attacker_score_mean",
                                    float("-inf"),
                                )
                            ),
                            "rounds_completed_mean": float(
                                campaign_summary.get(
                                    "rounds_completed_mean",
                                    0.0,
                                )
                            ),
                            "out_dir": str(track_dir),
                            "campaign_path": campaign_result.get("campaign_path"),
                            "reference_path": campaign_result.get("reference_path"),
                            "reference_out_dir": reference_summary.get("out_dir"),
                            "overrides": {
                                str(k): _normalize_json(v)
                                for k, v in profile.items()
                                if str(k) not in {"strategy", "description"}
                            },
                        }
                    )
            suite_summary = {
                "timestamp": datetime.now().isoformat(),
                "analysis_tag": str(args.analysis_tag or ""),
                "fitness_schema_version": str(args.fitness_schema_version),
                "mode": str(args.mode),
                "profile": str(args.profile),
                "experiment_suite": "precision",
                "tracks": suite_results,
            }
            suite_path = out_dir / "precision-suite-summary.json"
            _write_json(suite_path, suite_summary)
            print(f"[pcpl-evolvo] precision_suite_summary={suite_path}")
            return
        _run_noncontinuous_campaign(
            args,
            out_dir=out_dir,
            base_meta=base_meta,
        )
        return

    if args.no_resume:
        print("[pcpl-evolvo] warning: --no-resume ignored in --continuous mode")

    out_dir.mkdir(parents=True, exist_ok=True)
    runs_root = out_dir / "continuous-runs"
    runs_root.mkdir(parents=True, exist_ok=True)

    grid = _build_continuous_grid(args)
    if not grid:
        raise RuntimeError("Continuous grid is empty")
    strategy_counts: Dict[str, int] = {}
    for combo in grid:
        strategy = str(combo.get("strategy", "base"))
        strategy_counts[strategy] = strategy_counts.get(strategy, 0) + 1

    state_path = out_dir / "continuous-state.json"
    leaderboard_path = out_dir / "continuous-leaderboard.json"
    log_path = out_dir / "continuous.log"

    rng = random.Random(args.seed)
    order = list(range(len(grid)))
    rng.shuffle(order)

    leaderboard: Dict[str, Dict[str, Any]] = {}
    total_iterations = 0
    total_sweeps = 0
    lane_plan = _resolve_continuous_lane_plan(
        grid_size=len(grid),
        workers_arg=int(args.workers),
        parallel_backend=str(args.parallel_backend),
        max_cpu_utilization=float(args.max_cpu_utilization),
    )
    lane_count = int(lane_plan["lanes"])
    workers_per_lane = int(lane_plan["workers_per_lane"])

    print(
        "[pcpl-evolvo] continuous mode: combos={count} rounds-per-iteration={rounds} output={out} lanes={lanes} workers-per-lane={lane_workers} total-workers={total_workers}".format(
            count=len(grid),
            rounds=max(1, args.rounds),
            out=out_dir,
            lanes=lane_count,
            lane_workers=workers_per_lane,
            total_workers=int(lane_plan["total_workers"]),
        )
    )
    if len(strategy_counts) > 1:
        details = ", ".join(
            f"{name}={count}" for name, count in sorted(strategy_counts.items())
        )
        print(f"[pcpl-evolvo] continuous strategies: {details}")

    outer_kwargs: Dict[str, Any] = {}
    mp_ctx_name = _outer_mp_context()
    if mp_ctx_name is not None:
        try:
            outer_kwargs["mp_context"] = multiprocessing.get_context(mp_ctx_name)
        except Exception:
            pass

    try:
        with concurrent.futures.ProcessPoolExecutor(
            max_workers=lane_count,
            **outer_kwargs,
        ) as combo_pool:
            while True:
                stop_requested = False
                next_slot = 0
                pending: Dict[concurrent.futures.Future, Dict[str, Any]] = {}
                while next_slot < len(order) or pending:
                    while next_slot < len(order) and len(pending) < lane_count:
                        if (
                            args.continuous_max_iterations > 0
                            and (total_iterations + len(pending))
                            >= args.continuous_max_iterations
                        ):
                            break
                        combo_idx = order[next_slot]
                        combo = grid[combo_idx]
                        combo_name = _combo_label(combo)
                        combo_dir = runs_root / combo_name
                        combo_dir.mkdir(parents=True, exist_ok=True)

                        iteration_index = total_iterations + len(pending)
                        run_seed = args.seed + (iteration_index * 7_919) + next_slot
                        config = _build_experiment_config_from_args(
                            args,
                            out_dir=combo_dir,
                            seed=run_seed,
                            resume=True,
                            workers=workers_per_lane,
                            overrides={
                                **combo,
                                "rounds": max(1, int(args.rounds)),
                            },
                        )

                        print(
                            "[pcpl-evolvo] launch iter={iter} sweep={sweep} combo={combo} strategy={strategy} seed={seed} lane={lane}/{lanes} lane-workers={lane_workers} target_gen_s={target:.2f} cache={cache}".format(
                                iter=iteration_index,
                                sweep=total_sweeps,
                                combo=combo_name,
                                strategy=str(combo.get("strategy", "base")),
                                seed=run_seed,
                                lane=len(pending) + 1,
                                lanes=lane_count,
                                lane_workers=workers_per_lane,
                                target=float(combo.get("target_generation_seconds", args.target_generation_seconds)),
                                cache=int(combo.get("max_eval_cache_entries", args.max_eval_cache_entries)),
                            )
                        )
                        combo_meta = {
                            "analysis_tag": str(args.analysis_tag or ""),
                            "fitness_schema_version": str(args.fitness_schema_version),
                            "mode": str(args.mode),
                            "profile": str(args.profile),
                            "replicate_reference_policy": "single",
                            "strategy": str(combo.get("strategy", "base")),
                            "combo_label": combo_name,
                            "combo_description": str(combo.get("description", "")),
                            "continuous": True,
                            "launcher": "run_experiments.py",
                        }
                        future = combo_pool.submit(_run_once, config, meta=combo_meta)
                        pending[future] = {
                            "combo": combo,
                            "combo_name": combo_name,
                            "combo_dir": str(combo_dir),
                        }
                        next_slot += 1

                    if (
                        args.continuous_max_iterations > 0
                        and total_iterations >= args.continuous_max_iterations
                        and not pending
                    ):
                        stop_requested = True
                        break

                    if not pending:
                        break

                    done, _ = concurrent.futures.wait(
                        list(pending.keys()),
                        return_when=concurrent.futures.FIRST_COMPLETED,
                    )
                    for future in done:
                        meta = pending.pop(future)
                        combo = meta["combo"]
                        combo_name = meta["combo_name"]
                        success = True
                        try:
                            summary = future.result()
                        except Exception as exc:
                            success = False
                            summary = {
                                "out_dir": meta["combo_dir"],
                                "error": str(exc),
                                "best_score": float("-inf"),
                                "best_attacker_score": float("-inf"),
                                "rounds_completed": 0,
                            }
                            print(
                                "[pcpl-evolvo] combo failed combo={combo} error={error}".format(
                                    combo=combo_name,
                                    error=exc,
                                )
                            )

                        if success:
                            leaderboard[combo_name] = {
                                "combo": combo,
                                "strategy": str(combo.get("strategy", "base")),
                                "best_score": summary["best_score"],
                                "best_signature": summary["best_signature"],
                                "best_attacker_score": summary["best_attacker_score"],
                                "best_attacker_signature": summary["best_attacker_signature"],
                                "rounds_completed": summary["rounds_completed"],
                                "archive_path": summary["archive_path"],
                                "updated_at": datetime.now().isoformat(),
                            }

                        top = sorted(
                            leaderboard.values(),
                            key=lambda item: float(item["best_score"]),
                            reverse=True,
                        )[:20]
                        state_payload = {
                            "continuous": True,
                            "profile": args.profile,
                            "mode": str(args.mode),
                            "analysis_tag": str(args.analysis_tag or ""),
                            "fitness_schema_version": str(args.fitness_schema_version),
                            "iterations_completed": total_iterations + 1,
                            "sweeps_completed": total_sweeps,
                            "grid_size": len(grid),
                            "latest_combo": combo_name,
                            "latest_summary": summary,
                            "updated_at": datetime.now().isoformat(),
                        }
                        _write_json(state_path, state_payload)
                        _write_json(
                            leaderboard_path,
                            {
                                "updated_at": datetime.now().isoformat(),
                                "analysis_tag": str(args.analysis_tag or ""),
                                "fitness_schema_version": str(args.fitness_schema_version),
                                "leaders": top,
                            },
                        )

                        with log_path.open("a", encoding="utf-8") as handle:
                            if success:
                                handle.write(
                                    "{ts} iter={iter} sweep={sweep} combo={combo} score={score:.6f} attacker={attack:.6f} rounds={rounds}\n".format(
                                        ts=datetime.now().isoformat(),
                                        iter=total_iterations,
                                        sweep=total_sweeps,
                                        combo=combo_name,
                                        score=float(summary["best_score"]),
                                        attack=float(summary["best_attacker_score"]),
                                        rounds=int(summary["rounds_completed"]),
                                    )
                                )
                            else:
                                handle.write(
                                    "{ts} iter={iter} sweep={sweep} combo={combo} status=error\n".format(
                                        ts=datetime.now().isoformat(),
                                        iter=total_iterations,
                                        sweep=total_sweeps,
                                        combo=combo_name,
                                    )
                                )

                        if success:
                            _print_summary(summary)
                        total_iterations += 1

                        if (
                            args.continuous_max_iterations > 0
                            and total_iterations >= args.continuous_max_iterations
                        ):
                            print(
                                "[pcpl-evolvo] continuous stop: reached --continuous-max-iterations="
                                f"{args.continuous_max_iterations}"
                            )
                            stop_requested = True
                            break

                    if stop_requested:
                        for pending_future in list(pending.keys()):
                            pending_future.cancel()
                        break

                if stop_requested:
                    return

                total_sweeps += 1
                rng.shuffle(order)
                print(
                    "[pcpl-evolvo] sweep complete: sweeps={sweeps} iterations={iters}".format(
                        sweeps=total_sweeps,
                        iters=total_iterations,
                    )
                )
    except KeyboardInterrupt:
        print("[pcpl-evolvo] continuous mode stopped by user (Ctrl+C)")
        print(f"[pcpl-evolvo] state={state_path}")
        print(f"[pcpl-evolvo] leaderboard={leaderboard_path}")
        print(f"[pcpl-evolvo] log={log_path}")


if __name__ == "__main__":
    main()
