"""Minimal test to verify dependency injection in generate_project()."""

def test_generate_with_mock_validator():
    """Verify that generate_project accepts and uses an injected validator."""
    from generator.generate import generate_project

    # Track that mock was called
    called_validator = []
    called_loader = []

    def mock_validate(hw):
        called_validator.append(True)
        return [{"severity": "ERROR", "message": "Mock validation error"}]

    def mock_load(path):
        called_loader.append(path)
        return {"mcu": {"part": "STM32G0B1RET6"}, "peripherals": []}

    def mock_build(hw, name, hil=False):
        return {"project_name": name, "mcu": {}}

    try:
        generate_project(
            yaml_path="dummy.yaml",
            output_dir="/tmp/test_output_mock",
            validate_fn=mock_validate,
            build_context_fn=mock_build,
            load_yaml_fn=mock_load,
        )
    except SystemExit:
        pass

    assert len(called_loader) == 1, "Mock loader was not called"
    assert len(called_validator) == 1, "Mock validator was not called"


def test_generate_with_mock_context_builder():
    """Verify that generate_project accepts and uses an injected context builder."""
    from generator.generate import generate_project

    called_builder = []

    def mock_validate(hw):
        return []  # no errors, proceed to context building

    def mock_load(path):
        return {"mcu": {"part": "STM32G0B1RET6"}, "peripherals": []}

    def mock_build(hw, name, hil=False):
        called_builder.append((name, hil))
        # Return empty context that will cause template rendering to fail,
        # but we only care that the builder was called.
        return {}

    # Template rendering will fail with empty context, expect SystemExit or exception
    try:
        generate_project(
            yaml_path="dummy.yaml",
            output_dir="/tmp/test_output_mock",
            validate_fn=mock_validate,
            build_context_fn=mock_build,
            load_yaml_fn=mock_load,
        )
    except (SystemExit, Exception):
        pass

    assert len(called_builder) == 1, "Mock context builder was not called"
    assert called_builder[0] == ("test_output_mock", False), \
        f"Expected ('test_output_mock', False), got {called_builder[0]}"


def test_software_layer_fields_survive_the_real_pipeline(tmp_path):
    """task.yaml's `project.version` must actually reach `build_context()`.

    `generate_project()` re-derives `hw` from hardware.yaml through the Pydantic
    model and then merged the software layer back through a **hand-written
    whitelist** (`app_tasks` / `behavior` / `periodic_events` / `bind_routings`).
    When `mapper.merge()` started carrying `project.name` / `project.version`,
    nobody added them, so `version: "1.0.1"` written in task.yaml was silently
    dropped and the firmware fell back to the default `1.0.0`.  Nothing errored
    at generation time; the symptom was that a successful differential OTA left
    the reported version unchanged, i.e. looked like "the upgrade did nothing"
    (FR-14.7).

    The previous unit test stayed green because it checked `merge()` against
    `HardwareModel` in isolation — the drop happened *between* them.  So this
    test observes the dict that really reaches the context builder, and it
    derives the expected key set from `merge()` instead of listing it a second
    time: any field the mapper starts carrying must survive without anyone
    remembering to edit the generator.
    """
    import yaml

    from generator.generate import generate_project
    from generator.mapper import merge

    hardware = {"mcu": {"part": "STM32G0B1RET6"}, "peripherals": []}
    task = {
        "project": {"name": "ver_probe", "version": "9.8.7"},
        "app_tasks": [{"name": "probe_task", "priority": 3}],
        "behavior": {"initial_state": "idle", "states": {}},
    }

    def load(path):
        return task if str(path).endswith("task.yaml") else hardware

    seen = {}

    def build_context_fn(hw, name, hil=False):
        seen.update(hw)
        # An empty context makes rendering blow up, which is fine: the argument
        # handed to the builder is the whole subject of this test.
        return {}

    try:
        generate_project(
            yaml_path="hardware.yaml",
            output_dir=str(tmp_path / "ver_probe_out"),
            task_yaml_path="task.yaml",
            validate_fn=lambda hw: [],
            build_context_fn=build_context_fn,
            load_yaml_fn=load,
        )
    except (SystemExit, Exception):
        pass

    assert seen, "build_context() was never called — this check is dead"

    expected = merge(yaml.dump(hardware), yaml.dump(task), "")
    software_keys = sorted(set(expected) - set(hardware))
    assert software_keys, (
        "merge() no longer contributes anything beyond hardware.yaml, so this "
        "test cannot observe the drop it is guarding against")

    missing = [k for k in software_keys if k not in seen]
    assert not missing, (
        "these merged software-layer fields never reach build_context(): %s — "
        "generate.py is dropping them again (a whitelist that nobody extended, "
        "or a new field added to mapper.merge() without a counterpart in "
        "generate.py)" % (missing,))

    # The two that were actually lost, pinned by value so a silent fallback to
    # the default version cannot pass.
    assert seen["project_version"] == "9.8.7", (
        "the configured project.version did not survive the pipeline; the "
        "firmware would report the built-in default instead")
    assert seen["project_name"] == "ver_probe"


def test_project_block_unknown_keys_are_reported(caplog):
    """写在 task.yaml 的 project 块里、merge() 不认识的键必须出声。

    这里曾经静默丢弃 `project.heap_size`：几个示例都写了它（24576 / 16384），
    作者以为堆配上了，而生成的链接脚本一直是默认的 0x200、FreeRTOS 堆照旧走
    自动推算。配置写在错层却不报错是最难查的一类缺陷 —— 它既不出现在 diff 里，
    也不会在设备上留下任何痕迹。
    """
    import logging

    from generator.mapper import merge

    hardware = """
mcu:
  part: STM32G0B1RET6
pins:
  - id: PC0
    function: GPIO_Output
    label: LED
"""
    task = """
project:
  name: heap_probe
  version: "1.0.0"
  heap_size: 24576
"""

    with caplog.at_level(logging.WARNING, logger="hw2c.mapper"):
        merged = merge(hardware, task, "")

    assert merged.get("heap_size") is None, (
        "project.heap_size 不该被合并到顶层 —— 它是错层的配置，"
        "静默接受反而会让作者以为它生效了")
    assert any("heap_size" in r.getMessage() for r in caplog.records), (
        "merge() 对 project 块里的未知键保持沉默 —— 写错层的配置又被静默吞掉了")


def test_no_shipped_example_declares_a_dead_project_key(caplog):
    """仓库里随附的每个示例都不许在 project 块里放 merge() 不认的键。

    上面那条护栏只能证明「机制在」，证明不了「存量已清干净」。实测清扫第一遍时
    只处理了 4 个示例，剩下 6 个（mpu6050 / thermo_pid / spi_flash / modbus /
    pwm / solenoid_valve）依旧把 `heap_size` 写在 project 块里 —— 生成时会各刷
    一条 WARNING，长期淹没在日志里就没人看了。这条扫的是真实目录，不是构造
    YAML，所以能挡住「新示例又照着老示例抄」。
    """
    import logging
    from pathlib import Path

    from generator.mapper import merge

    repo_root = Path(__file__).resolve().parents[2]
    examples = sorted(repo_root.glob("examples/*/task.yaml"))
    assert len(examples) >= 8, (
        "只找到 %d 个示例 —— 路径推导错了，这条护栏会变成空转" % len(examples))

    offenders: list[str] = []
    with caplog.at_level(logging.WARNING, logger="hw2c.mapper"):
        for task_path in examples:
            hw_path = task_path.with_name("hardware.yaml")
            merge(
                hw_path.read_text(encoding="utf-8") if hw_path.is_file() else "",
                task_path.read_text(encoding="utf-8"),
                "",
            )
    offenders = [
        r.getMessage() for r in caplog.records
        if "project" in r.getMessage() and "忽略" in r.getMessage()
    ]
    assert not offenders, (
        "这些随仓库发布的示例在 project 块里写了不生效的键，示例本身就是文档，"
        "放死配置等于教用户写错层：\n  " + "\n  ".join(offenders))

    # 正控：确认这次扫描真的能看见告警，否则上面的断言可能只是没接上 logger。
    caplog.clear()
    with caplog.at_level(logging.WARNING, logger="hw2c.mapper"):
        merge("", 'project:\n  name: probe\n  heap_size: 16384\n', "")
    assert any("忽略" in r.getMessage() for r in caplog.records), (
        "正控失败：merge() 对错层键已经不出声了，本用例的断言失去意义")


if __name__ == "__main__":
    test_generate_with_mock_validator()
    test_generate_with_mock_context_builder()
    print("All tests passed.")
