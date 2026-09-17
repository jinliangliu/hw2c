"""Tests for generator/context/builder.py"""

from generator.context.builder import load_model, build_context


def test_load_model_internal_rtc():
    """load_model should return a dict for Internal_RTC"""
    model = load_model("Internal_RTC")
    assert isinstance(model, dict)
    assert model.get("type") == "Internal_RTC"


def test_load_model_internal_cli():
    """load_model should return a dict for Internal_CLI"""
    model = load_model("Internal_CLI")
    assert isinstance(model, dict)
    assert model.get("type") == "Internal_CLI"


def test_load_model_uart_serial():
    """load_model should return a dict for UART_Serial"""
    model = load_model("UART_Serial")
    assert isinstance(model, dict)
    assert model.get("type") == "UART_Serial"


def test_load_model_nonexistent():
    """load_model should return empty dict for nonexistent type"""
    model = load_model("NonExistent_XYZ_123")
    assert model == {}


def test_load_model_i2c_sensor():
    """load_model should return dict for I2C_Sensor_MPU6050"""
    model = load_model("I2C_Sensor_MPU6050")
    assert isinstance(model, dict)
    assert model.get("type") == "I2C_Sensor"


def test_build_context_minimal():
    """build_context with minimal config returns valid context"""
    hw = {
        "mcu": {"part": "STM32G0B1RET6"},
        "pins": [{"id": "PA5", "function": "GPIO_Output"}],
    }
    ctx = build_context(hw, "test_project")
    assert ctx["project_name"] == "test_project"
    assert ctx["mcu"]["part"] == "STM32G0B1RET6"
    assert ctx["mcu"]["core_clock_mhz"] == 16
    assert ctx["has_i2c"] == False
    assert ctx["has_rtc"] == False
    assert ctx["has_bootloader"] == False
    assert ctx["has_behavior"] == False
    assert ctx["hil_mode"] == False
    assert isinstance(ctx["hal_sources"], list)
    assert "stm32g0xx_hal.c" in ctx["hal_sources"]


def test_build_context_with_rtc():
    """build_context with RTC peripheral sets has_rtc and rtc_prediv"""
    hw = {
        "mcu": {"part": "STM32G0B1RET6"},
        "pins": [{"id": "PA5", "function": "GPIO_Output"}],
        "peripherals": [{"name": "rtc1", "type": "Internal_RTC"}],
    }
    ctx = build_context(hw, "test_rtc")
    assert ctx["has_rtc"] == True
    assert ctx["rtc_async_prediv"] == 0
    assert ctx["rtc_sync_prediv"] == 32767
    assert "stm32g0xx_hal_rtc.c" in ctx["hal_sources"]


def _rtc_hw(initial_time):
    """Minimal hardware description with an RTC carrying `initial_time`."""
    return {
        "mcu": {"part": "STM32G0B1RET6"},
        "pins": [{"id": "PA5", "function": "GPIO_Output"}],
        "peripherals": [{
            "name": "rtc1", "type": "Internal_RTC",
            "extra": {"clock_source": "LSE", "wakeup_interval_ms": 1000,
                      "initial_time": initial_time},
        }],
    }


def test_rtc_initial_time_is_parsed_strictly():
    """`initial_time` must be validated at generation time, never clamped.

    The parsed fields go straight into `HAL_RTC_SetTime/SetDate`, so a value
    that is silently defaulted or clamped leaves the device running on a wrong
    calendar with no later chance to notice it -- the only symptom is a wrong
    log timestamp, which gets misdiagnosed as a crystal / backup-domain fault.

    The old implementation split the string by hand (`y, mo, d =
    date_part.split("-")`), so field order was never checked:
    "17-09-2026 10:00:00" produced `year = 17-2000 = -1983` clamped to 0 and
    `day = 2026`, the latter written verbatim into `sDate.Date` whose legal
    range on the RTC is 1..31.
    """
    from generator.context.builder import _parse_rtc_initial_time

    default = {"year": 0, "month": 1, "day": 1, "hour": 0, "min": 0, "sec": 0}
    # Only an omitted value may fall back to a default.
    assert _parse_rtc_initial_time(None) == default
    assert _parse_rtc_initial_time("") == default
    assert _parse_rtc_initial_time("   ") == default

    assert _parse_rtc_initial_time("2026-09-17 10:00:00") == {
        "year": 26, "month": 9, "day": 17, "hour": 10, "min": 0, "sec": 0}
    # Unpadded month/day/time is still accepted (YAML is written by hand).
    assert _parse_rtc_initial_time("2026-9-7 1:2:3") == {
        "year": 26, "month": 9, "day": 7, "hour": 1, "min": 2, "sec": 3}

    for bad in ("2026/09/17 10:00:00", "2026-09-17 10:00", "17-09-2026 10:00:00",
                "2026-02-30 10:00:00", "2026-13-01 10:00:00",
                "2026-09-17 25:00:00", "2026-09-17 10:60:00", "2026-09-17",
                "2026-09-17T10:00:00", "garbage", "2026-09-17 10:00:00 extra"):
        try:
            _parse_rtc_initial_time(bad)
        except ValueError:
            continue
        raise AssertionError(
            "initial_time %r was accepted; malformed input must fail the "
            "generation instead of being clamped or replaced by a default"
            % (bad,))


def test_rtc_initial_time_year_must_fit_the_twodigit_calendar():
    """The RTC keeps only a 2-digit year, so the window is 2000..2099.

    Refusing (rather than clamping) matters: a clamped year looks like a
    successful configuration, i.e. the generator makes a decision the YAML
    author never sees.
    """
    from generator.context.builder import _parse_rtc_initial_time

    assert _parse_rtc_initial_time("2000-01-01 00:00:00")["year"] == 0
    assert _parse_rtc_initial_time("2099-12-31 23:59:59")["year"] == 99

    for bad in ("1999-12-31 23:59:59", "2100-01-01 00:00:00",
                "0001-01-01 00:00:00"):
        try:
            _parse_rtc_initial_time(bad)
        except ValueError:
            continue
        raise AssertionError(
            "initial_time %r is outside the RTC calendar's 2000..2099 window "
            "but was accepted" % (bad,))


def test_build_context_carries_rtc_initial_time():
    """A well-formed `initial_time` reaches the template context."""
    ctx = build_context(_rtc_hw("2026-09-17 10:00:00"), "test_rtc_init")
    assert ctx["rtc_init_time"] == {
        "year": 26, "month": 9, "day": 17, "hour": 10, "min": 0, "sec": 0}


def test_bad_rtc_initial_time_fails_the_whole_generation():
    """The refusal must happen on the real path, not only in the helper.

    Guards against the parse being fixed while a later `except` swallows it
    again somewhere between `build_context` and the rendered driver.
    """
    try:
        build_context(_rtc_hw("17-09-2026 10:00:00"), "test_rtc_bad")
    except ValueError:
        return
    raise AssertionError(
        "build_context accepted a day/month/year-swapped initial_time; the "
        "device would have been flashed with `sDate.Date = 2026`")


def test_rtos_heap_size_is_parsed_strictly():
    """rtos_heap_size 是烤进固件的常量，不允许任何静默兜底。

    写小了这个值只会在运行时表现为 pvPortMalloc 返回 NULL（任务创建失败、
    OTA 收不下包），编译期毫无提示 —— 所以非法配置必须在**生成期**失败：
      · 非整数 / 布尔 / 浮点 / 容器        -> ValueError
      · <= 0                              -> ValueError
      · 不是 portBYTE_ALIGNMENT(8) 的整数倍 -> ValueError
        （heap_4 会留下一块永远分不出去的尾部碎片）
      · 超过 MCU 的 RAM 容量              -> ValueError
    """
    from generator.context.builder import _resolve_rtos_heap_size

    AUTO = 13312

    # 省略（None / 空串 / 纯空白）→ 推算值
    assert _resolve_rtos_heap_size(None, AUTO) == AUTO
    assert _resolve_rtos_heap_size("", AUTO) == AUTO
    assert _resolve_rtos_heap_size("   ", AUTO) == AUTO

    # 显式值优先；十进制与十六进制都认
    assert _resolve_rtos_heap_size(20480, AUTO) == 20480
    assert _resolve_rtos_heap_size("20480", AUTO) == 20480
    assert _resolve_rtos_heap_size("0x5000", AUTO) == 20480

    # 低于推算值：接受（作者可能有意省 RAM），但要出声
    assert _resolve_rtos_heap_size(4096, AUTO) == 4096

    for bad in (0, -1, 1, 20481, "abc", "20 KB", "0x1FFF",
                True, False, 3.5, [1], {"a": 1}, 300000, 144 * 1024 + 8):
        try:
            got = _resolve_rtos_heap_size(bad, AUTO)
        except ValueError:
            continue
        raise AssertionError(
            "rtos_heap_size=%r 被接受了（得到 %r）—— 非法值必须在生成期失败"
            % (bad, got))


def test_rtos_heap_size_overrides_the_computed_default():
    """task.yaml 顶层的 rtos_heap_size 必须真的走到 build_context 里。

    这条防的是"配置写了但不生效"：示例里曾经把堆大小写在 project 块下，
    而 merge() 只认 name / version —— 那份配置被静默丢弃，堆一路走自动推算，
    全程没有任何地方会报错。
    """
    from generator.mapper import merge

    hw_yaml = (
        "mcu: {part: STM32G0B1RET6}\n"
        "pins:\n"
        "  - {id: PA5, function: GPIO_Output}\n"
        "peripherals:\n"
        "  - name: usart2\n"
        "    type: UART_Serial\n"
        "    instance: USART2\n"
        "    interface: uart\n"
        "    extra: {baudrate: 115200}\n"
    )
    head = "project: {name: heap_probe, version: '1.0.0'}\n"

    configured = build_context(
        merge(hw_yaml, head + "rtos_heap_size: 20480\n"), "heap_probe")
    auto = build_context(merge(hw_yaml, head), "heap_probe")

    assert configured["total_heap_size"] == 20480, (
        "rtos_heap_size 没有走到上下文里 —— YAML 里配的堆大小又成了摆设")
    assert auto["total_heap_size"] > 0
    assert auto["total_heap_size"] != 20480, (
        "省略 rtos_heap_size 时应当走自动推算，而不是也得到 20480")


def test_build_context_with_bootloader():
    """build_context with bootloader sets has_bootloader"""
    hw = {
        "mcu": {"part": "STM32G0B1RET6"},
        "pins": [{"id": "PA5", "function": "GPIO_Output"}],
        "bootloader": {"enabled": True},
    }
    ctx = build_context(hw, "test_boot")
    assert ctx["has_bootloader"] == True
    assert "stm32g0xx_hal_iwdg.c" in ctx["hal_sources"]


def test_build_context_with_behavior():
    """build_context with behavior sets has_behavior"""
    hw = {
        "mcu": {"part": "STM32G0B1RET6"},
        "pins": [{"id": "PA5", "function": "GPIO_Output"}],
        "behavior": {
            "states": [
                {"name": "idle",
                 "transitions": [{"event": "TICK", "target": "idle"}]},
            ]
        },
    }
    ctx = build_context(hw, "test_bf")
    assert ctx["has_behavior"] == True
    assert ctx["has_event_mgr"] == True


def test_build_context_default_hil():
    """build_context creates default HIL config when none provided"""
    hw = {
        "mcu": {"part": "STM32G0B1RET6"},
        "pins": [{"id": "PA5", "function": "GPIO_Output"}],
    }
    ctx = build_context(hw, "test_hil")
    assert ctx["hil"]["baudrate"] == 115200
    assert ctx["hil"]["uart"] == "UART2"


def test_build_context_with_led():
    """build_context detects LED label on pin"""
    hw = {
        "mcu": {"part": "STM32G0B1RET6"},
        "pins": [
            {"id": "PA5", "function": "GPIO_Output", "label": "LED"},
        ],
        "app_tasks": [{"name": "led_task", "priority": 5}],
    }
    ctx = build_context(hw, "test_led")
    assert ctx["has_led"] == True
    assert ctx["has_led_task"] == True


def test_build_context_heap_stack():
    """build_context uses custom heap/stack sizes"""
    hw = {
        "mcu": {"part": "STM32G0B1RET6"},
        "pins": [{"id": "PA5", "function": "GPIO_Output"}],
        "heap_size": "0x800",
        "stack_size": "0x1000",
    }
    ctx = build_context(hw, "test_mem")
    assert ctx["heap_size"] == "0x800"
    assert ctx["stack_size"] == "0x1000"


def test_build_context_hil_mode():
    """build_context in hil_mode adds UART HAL"""
    hw = {
        "mcu": {"part": "STM32G0B1RET6"},
        "pins": [{"id": "PA5", "function": "GPIO_Output"}],
    }
    ctx = build_context(hw, "test_hil", hil_mode=True)
    assert ctx["hil_mode"] == True
    assert "stm32g0xx_hal_uart.c" in ctx["hal_sources"]


def test_build_context_with_defer_timeline():
    """build_context processes defer and timeline actions"""
    hw = {
        "mcu": {"part": "STM32G0B1RET6"},
        "pins": [{"id": "PA5", "function": "GPIO_Output"}],
        "behavior": {
            "states": [
                {"name": "idle",
                 "on_entry": ["defer 3000 => toggle_led"],
                 "transitions": [
                     {"event": "TICK", "target": "active",
                      "actions": ["timeline: 1000=>toggle_led"]}
                 ]},
                {"name": "active"},
            ]
        },
    }
    ctx = build_context(hw, "test_defer")
    assert ctx["has_behavior"] == True
    assert len(ctx["defer_actions"]) >= 2
    assert len(ctx["defer_timer_names"]) >= 2


def test_build_context_with_publish():
    """build_context collects published events"""
    hw = {
        "mcu": {"part": "STM32G0B1RET6"},
        "pins": [{"id": "PA5", "function": "GPIO_Output"}],
        "behavior": {
            "states": [
                {"name": "idle",
                 "transitions": [
                     {"event": "TICK", "target": "active",
                      "actions": ["publish ALARM"]}
                 ]},
                {"name": "active"},
            ]
        },
    }
    ctx = build_context(hw, "test_pub")
    assert ctx["has_behavior"] == True
    assert "ALARM" in ctx["published_events"]


def test_build_context_with_dict_actions():
    """build_context normalizes dict-format actions"""
    hw = {
        "mcu": {"part": "STM32G0B1RET6"},
        "pins": [{"id": "PA5", "function": "GPIO_Output"}],
        "behavior": {
            "states": [
                {"name": "idle",
                 "on_entry": [
                     {"defer": {"after": 500, "do": "toggle_led"}},
                     {"timeline": [{"ms": 200, "do": "toggle_led"}]},
                     {"set": {"var": "counter", "value": 10}},
                     {"start_timer": {"name": "t1", "ms": 1000}},
                     {"stop_timer": {"name": "t1"}},
                 ],
                 "transitions": [
                     {"event": "TICK", "target": "idle"}
                 ]},
            ]
        },
    }
    ctx = build_context(hw, "test_dict")
    assert ctx["has_behavior"] == True


def test_build_context_compound_state():
    """build_context handles compound states (states within states)"""
    hw = {
        "mcu": {"part": "STM32G0B1RET6"},
        "pins": [{"id": "PA5", "function": "GPIO_Output"}],
        "behavior": {
            "states": [
                {"name": "parent", "initial_state": "child1",
                 "states": [
                     {"name": "child1",
                      "transitions": [{"event": "GO", "target": "child2"}]},
                     {"name": "child2"},
                 ]},
            ]
        },
    }
    ctx = build_context(hw, "test_cmpd")
    assert ctx["has_behavior"] == True
    assert ctx["has_substate"] == True


def test_build_context_with_fota():
    """build_context with bootloader + UART sets has_fota"""
    hw = {
        "mcu": {"part": "STM32G0B1RET6"},
        "pins": [{"id": "PA5", "function": "GPIO_Output"}],
        "bootloader": {"enabled": True},
        "peripherals": [{"name": "uart1", "type": "UART_Serial"}],
    }
    ctx = build_context(hw, "test_fota")
    assert ctx["has_bootloader"] == True
    assert ctx["has_uart"] == True
    assert ctx["has_fota"] == True


def test_build_context_with_regions():
    """build_context handles behavior with regions"""
    hw = {
        "mcu": {"part": "STM32G0B1RET6"},
        "pins": [{"id": "PA5", "function": "GPIO_Output"}],
        "behavior": {
            "regions": [
                {"name": "r1", "initial_state": "s1",
                 "states": [
                     {"name": "s1",
                      "transitions": [{"event": "E1", "target": "s2"}]},
                     {"name": "s2"},
                 ]},
            ]
        },
    }
    ctx = build_context(hw, "test_regions")
    assert ctx["has_behavior"] == True


if __name__ == "__main__":
    test_load_model_internal_rtc()
    test_load_model_internal_cli()
    test_load_model_uart_serial()
    test_load_model_nonexistent()
    test_load_model_i2c_sensor()
    test_build_context_minimal()
    test_build_context_with_rtc()
    test_rtc_initial_time_is_parsed_strictly()
    test_rtc_initial_time_year_must_fit_the_twodigit_calendar()
    test_build_context_carries_rtc_initial_time()
    test_bad_rtc_initial_time_fails_the_whole_generation()
    test_rtos_heap_size_is_parsed_strictly()
    test_rtos_heap_size_overrides_the_computed_default()
    test_build_context_with_bootloader()
    test_build_context_with_behavior()
    test_build_context_default_hil()
    test_build_context_with_led()
    test_build_context_heap_stack()
    test_build_context_hil_mode()
    print("All builder tests passed.")
