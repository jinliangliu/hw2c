"""
mapper.py — merge hardware.yaml + task.yaml + bind.yaml into unified context dict.

Produces the same dict shape that ``build_context()`` expects, so templates and
existing code generation pipeline require zero changes.
"""

from __future__ import annotations

import logging
from typing import Optional

import yaml

logger = logging.getLogger("hw2c.mapper")


def merge(
    hardware_yaml: str,
    task_yaml: str = "",
    bind_yaml: str = "",
) -> dict:
    """Merge the three-layer YAML into a unified hardware dict.

    Backward compatibility:
    - If task_yaml is empty, extract app_tasks/behavior from hardware_yaml.
    - If bind_yaml is empty, create an empty bind context.

    Args:
        hardware_yaml: hardware.yaml content (mcu, pins, peripherals, sleep, clock, bootloader, hil).
        task_yaml: task.yaml content (project, app_tasks, behavior).
        bind_yaml: bind.yaml content (interrupt, peripheral_assign, routing).

    Returns:
        Merged dict compatible with ``build_context(hw, project_name, hil_mode)``.
    """
    hw = yaml.safe_load(hardware_yaml) or {}
    task = yaml.safe_load(task_yaml) if task_yaml else {}
    bind = yaml.safe_load(bind_yaml) if bind_yaml else {}

    if not isinstance(task, dict):
        task = {}
    if not isinstance(bind, dict):
        bind = {}

    merged: dict = dict(hw)

    # ---- Extract project name ----
    project = task.get("project", {})
    if isinstance(project, dict) and project.get("name"):
        merged["project_name"] = project["name"]

    # ---- Extract project version ----
    #
    # 这一条曾经缺失：`project.name` 被取走了，`project.version` 被丢掉，于是
    # task.yaml 里写 `version: "1.0.1"` 对固件**完全没有影响** —— banner 与
    # `version` 命令各自打印一个字面量（`v1.0` / `1.0.0`），差分 OTA 升级完
    # 版本号不变。版本号必须在链路上传到底，见 generator/context/builder.py
    # 的 _parse_project_version()。
    if isinstance(project, dict) and project.get("version") is not None:
        merged["project_version"] = project["version"]

    # ---- Merge app_tasks ----
    app_tasks = task.get("app_tasks", [])
    if not app_tasks and "app_tasks" in hw:
        # Backward compat: app_tasks in old hardware.yaml
        app_tasks = hw["app_tasks"]
    if app_tasks:
        merged["app_tasks"] = app_tasks

    # ---- Merge behavior ----
    bf = task.get("behavior", {})
    if not bf and "behavior" in hw:
        # Backward compat: behavior in old hardware.yaml
        bf = hw["behavior"]
    if bf:
        merged["behavior"] = bf

    # ---- Merge periodic_events ----
    periodic = task.get("periodic_events", [])
    if periodic:
        merged["periodic_events"] = periodic

    # ---- Apply bind: interrupt → notify_task on pins ----
    interrupts = bind.get("interrupt", [])
    if interrupts and "pins" in merged:
        _apply_interrupt_bindings(merged["pins"], interrupts)

    # ---- Apply bind: peripheral_assign → features ----
    periph_assigns = bind.get("peripheral_assign", [])
    if periph_assigns and "peripherals" in merged:
        _apply_peripheral_assign(merged["peripherals"], periph_assigns)

    # ---- Apply bind: routing → signals on app_tasks ----
    routings = bind.get("routing", [])
    if routings and app_tasks:
        merged["bind_routings"] = routings

    return merged


def _apply_interrupt_bindings(
    pins: list,
    interrupts: list,
) -> None:
    """Set notify_task on pins from bind interrupt entries."""
    pin_map = {p.get("id", "").upper(): p for p in pins if isinstance(p, dict)}

    for binding in interrupts:
        if not isinstance(binding, dict):
            continue
        pin_id = binding.get("pin", "").upper()
        task_name = binding.get("task", "")
        event = binding.get("event", "")

        if pin_id in pin_map:
            pin_map[pin_id]["notify_task"] = task_name
            if event:
                pin_map[pin_id]["bind_event"] = event
        else:
            logger.warning("Bind interrupt: pin %s not found in hardware.yaml", pin_id)


def _apply_peripheral_assign(
    peripherals: list,
    assigns: list,
) -> None:
    """Set owning task on peripherals from bind peripheral_assign entries."""
    peri_map = {}
    for p in peripherals:
        if isinstance(p, dict):
            peri_map[p.get("name", "").lower()] = p

    for assign in assigns:
        if not isinstance(assign, dict):
            continue
        peri_name = assign.get("peripheral", "").lower()
        task_name = assign.get("task", "")
        role = assign.get("role", "")

        if peri_name in peri_map:
            peri_map[peri_name]["bind_task"] = task_name
            if role:
                peri_map[peri_name]["bind_role"] = role
        else:
            logger.warning("Bind peripheral_assign: %s not found in hardware.yaml", peri_name)


# ---------------------------------------------------------------------------
# Legacy split: extract hardware + task + bind from old monolithic YAML
# ---------------------------------------------------------------------------

def split_legacy(monolithic_yaml: str) -> tuple:
    """Split old monolithic hardware.yaml into (hardware_yaml, task_yaml, bind_yaml).

    Used by hw2c-web backend for backward compat with old-format YAML.

    Returns:
        (hardware_yaml: str, task_yaml: str, bind_yaml: str)
    """
    doc = yaml.safe_load(monolithic_yaml) or {}

    hw_keys = ("mcu", "pins", "peripherals", "sleep", "clock",
               "bootloader", "hil", "heap_size", "stack_size")
    sw_keys = ("app_tasks", "behavior")

    # Hardware-only
    hw_doc: dict = {}
    for key in hw_keys:
        if key in doc:
            hw_doc[key] = doc[key]

    # Task
    task_doc: dict = {
        "project": {"name": doc.get("project_name", "untitled"),
                     "version": "0.1.0"},
    }
    for key in sw_keys:
        if key in doc:
            task_doc[key] = doc[key]

    # Strip triggers/signals/run_mode from app_tasks
    if "app_tasks" in task_doc:
        raw_tasks = task_doc["app_tasks"]
        clean_tasks = []
        bind_interrupt: list = []
        bind_routing: list = []

        for t in raw_tasks:
            task_name = t.get("name", "")
            clean = {"name": task_name,
                     "priority": t.get("priority", 1),
                     "stack_size": t.get("stack_size", 128)}
            clean_tasks.append(clean)

            # Extract triggers → bind interrupt
            for trigger in t.get("triggers", []):
                if trigger.get("type") == "interrupt":
                    bind_interrupt.append({
                        "pin": trigger.get("source", ""),
                        "task": task_name,
                        "event": trigger.get("event", ""),
                    })

            # Extract signals → bind routing
            for signal in t.get("signals", []):
                entry = {"from": task_name,
                         "signal": signal.get("name", ""),
                         "condition": signal.get("condition", None)}
                target = signal.get("target", "")
                if target:
                    entry["to"] = target
                bind_routing.append(entry)

        task_doc["app_tasks"] = clean_tasks

        # Bind doc
        if bind_interrupt or bind_routing:
            bind_doc = {
                "version": 1,
                "interrupt": bind_interrupt,
                "routing": bind_routing,
            }
        else:
            bind_doc = None
    else:
        bind_doc = None

    # Also extract notify_task from pins
    if "pins" in hw_doc:
        for p in hw_doc.get("pins", []):
            notify = p.pop("notify_task", None)
            if notify:
                if bind_doc is None:
                    bind_doc = {"version": 1, "interrupt": [], "routing": []}
                bind_doc["interrupt"].append({
                    "pin": p.get("id", ""),
                    "task": notify,
                })

    hw_str = yaml.dump(hw_doc, default_flow_style=False,
                       sort_keys=False, allow_unicode=True)
    task_str = yaml.dump(task_doc, default_flow_style=False,
                         sort_keys=False, allow_unicode=True)
    bind_str = ""
    if bind_doc:
        bind_str = yaml.dump(bind_doc, default_flow_style=False,
                             sort_keys=False, allow_unicode=True)

    return hw_str, task_str, bind_str
