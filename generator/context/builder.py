"""
builder.py
Aggregation entry point for building the complete Jinja2 template context.
Delegates to sub-modules: pin_context, peripheral_context, hal_context,
bootloader_context.
"""

import os
import sys
import re
import yaml
import importlib.util

from ..paths import MODELS_DIR, EXAMPLES_DIR, STATIC_STM32_DIR

from .pin_context import process_pins
from .peripheral_context import detect_peripherals
from .bearer_context import associate_bearers
from .hal_context import compute_hal_sources
from .bootloader_context import build_boot_config, inject_bootloader_drivers, get_boot_led_pin

# Builder registry — auto-discovers all @register_builder classes
try:
    from ..builders.registry import get_builder
except ImportError:
    def get_builder(peripheral: dict):
        return None

# Load generator/generator_types.py with explicit module name to avoid collision
# with Python's stdlib 'types' module which is frozen at interpreter startup.
_types_spec = importlib.util.spec_from_file_location(
    "hw2c_types",
    os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "generator_types.py")
)
_types_module = importlib.util.module_from_spec(_types_spec)
_types_spec.loader.exec_module(_types_module)
BuildContext = _types_module.BuildContext


def load_model(model_type: str) -> dict:
    """从 models/ 目录加载外设模型 YAML"""
    model_path = os.path.join(MODELS_DIR, model_type + '.yaml')
    if os.path.exists(model_path):
        with open(model_path, 'r', encoding='utf-8') as f:
            return yaml.safe_load(f)
    else:
        print(f"Warning: model file for {model_type} not found.")
        return {}


def build_context(hw: dict, project_name: str, hil_mode: bool = False) -> BuildContext:
    """
    将 YAML 中的硬件描述处理成模板渲染所需的完整上下文。
    """
    # ---------- 基础信息提取 ----------
    mcu = hw.get("mcu", {})
    mcu["core_clock_mhz"] = int(mcu.get("core_clock_mhz", 16))
    mcu["clock_source"] = mcu.get("clock_source", "HSI").upper()
    mcu["clock_freq_hz"] = int(mcu.get("clock_freq_hz", 16000000))
    mcu["hse_freq"] = int(mcu.get("hse_freq", 8000000))
    mcu["hclk_freq_hz"] = mcu["clock_freq_hz"]

    pins = hw.get("pins", [])
    sleep = hw.get("sleep", {})
    app_tasks = hw.get("app_tasks", [])

    # ---------- 引脚字段补充默认值 ----------
    process_pins(pins)

    # ---------- 外设处理 ----------
    peripherals = hw.get("peripherals", [])
    peri_result = detect_peripherals(peripherals, load_model)

    drivers = peri_result["drivers"]
    has_i2c = peri_result["has_i2c"]
    has_rtc = peri_result["has_rtc"]
    has_mpu6050 = peri_result["has_mpu6050"]
    has_pwm = peri_result["has_pwm"]
    has_spi = peri_result["has_spi"]
    has_spi_flash = peri_result["has_spi_flash"]
    has_adc = peri_result["has_adc"]
    has_uart = peri_result["has_uart"]
    has_rs485 = peri_result["has_rs485"]
    has_ir = peri_result["has_ir"]
    has_cellular = peri_result["has_cellular"]
    has_cli = peri_result["has_cli"]
    has_temp_sensor = peri_result["has_temp_sensor"]
    has_telemetry = True   # always available, auto-generated
    has_power_mgr = True   # always available, auto-generated
    uart_name = peri_result["uart_name"]
    rs485_name = peri_result["rs485_name"]
    cli_uart_name = peri_result["cli_uart_name"]
    cli_name = peri_result["cli_name"]

    # ---------- USART2 baudrate (from UART peripheral config) ----------
    usart2_baudrate = 115200  # default
    for p in peripherals:
        if p.get("name") == cli_uart_name and p.get("type") == "UART_Serial":
            extra = p.get("extra", {})
            usart2_baudrate = int(extra.get("baudrate", 115200))
            break

    # USART2 clock source is HSI16 (always 16 MHz in this design)
    usart2_clock_freq_hz = 16000000

    # ---------- Temperature sensor offset ----------
    temp_offset_deci = 0  # default: no offset
    for p in peripherals:
        if p.get("name") == "temp_sensor" and p.get("type") == "Internal_TempSensor":
            extra = p.get("extra", {})
            offset_c = float(extra.get("temp_offset", 0.0))
            temp_offset_deci = int(round(offset_c * 10.0))
            break

    # ---------- Bearer association (MQTT/Modbus) ----------
    bearer_result = associate_bearers(peripherals, drivers)
    has_modbus = bearer_result["has_modbus"]
    has_mqtt = bearer_result["has_mqtt"]
    modbus_name = bearer_result["modbus_name"]

    # ---------- HAL sources ----------
    hal_sources = compute_hal_sources(peri_result, hil_mode)

    # ---------- Bootloader ----------
    mcu_flash_kb = mcu.get('flash_kb', 512)
    boot_config, has_bootloader = build_boot_config(hw.get('bootloader', {}),
                                                     mcu_flash_kb)
    boot_led = get_boot_led_pin(pins) if has_bootloader else {}
    boot_result = inject_bootloader_drivers(has_bootloader, has_uart,
                                             boot_config, uart_name)
    drivers.extend(boot_result['drivers_additions'])
    has_fota = boot_result['has_fota']
    for hal_file in boot_result['hal_additions']:
        if hal_file not in hal_sources:
            hal_sources.append(hal_file)

    # IWDG HAL source is auto-injected when bootloader is enabled
    if has_bootloader:
        if 'stm32g0xx_hal_iwdg.c' not in hal_sources:
            hal_sources.append('stm32g0xx_hal_iwdg.c')

    # ---------- Builder registry: pre-calculate peripheral values ----------
    # Each peripheral's registered builder computes register values, timings,
    # and prescalers so that templates use simple {{ variable }} interpolation
    # instead of inline arithmetic.
    for p in peripherals:
        builder_cls = get_builder(p)
        if builder_cls is not None:
            try:
                builder = builder_cls()
                computed = builder.calculate(p, mcu, {})
                # Merge computed fields into the peripheral dict
                for key, value in computed.items():
                    if isinstance(value, dict):
                        p.setdefault(key, {})
                        if isinstance(p[key], dict):
                            p[key].update(value)
                    elif key not in p:
                        p[key] = value
            except Exception as e:
                print(f"Warning: builder {builder_cls.__name__} failed for "
                      f"'{p.get('name', '?')}': {e}")

    # ---------- 业务逻辑 DSL ----------
    behavior = hw.get('behavior', {})
    has_behavior = bool(behavior)
    periodic_events = hw.get('periodic_events', [])

    has_led = any(pin.get('label') == 'LED' for pin in pins)
    has_led_task = any(t.get('name') == 'led_task' for t in app_tasks)
    has_btn_task = any(t.get('name') == 'btn_task' for t in app_tasks)

    led_active_low = False
    for pin in pins:
        if pin.get('label') == 'LED':
            led_active_low = pin.get('active_level', '').lower() == 'low'
            break

    # ---------- Log subsystem: ring buffer size ----------
    log_config = hw.get('log', {})
    has_log = log_config.get('enable', False)
    log_ring_buf_size = int(log_config.get('ring_buf_size', 1024))

    # ---------- Log subsystem: UART pin / AF / IRQ derivation ----------
    # Derive macro names from the CLI UART peripheral and its pins.
    # This avoids hardcoding USART2/PA2/PA3/AF1 in the log driver template.
    log_uart = {}

    # Extract TX / RX pin info from the pins list
    for pin in pins:
        func = pin.get("function", "").upper()
        pin_id = pin.get("id", "")
        af = pin.get("af", 0)

        # Parse port letter and pin number from pin id e.g. "PA2" → port='A', num=2
        m = re.match(r'P([A-Z])(\d+)', pin_id.upper())
        if not m:
            continue
        port_letter = m.group(1)
        pin_number = int(m.group(2))

        # Match USARTx_TX or USARTx_RX
        if func == f"{cli_uart_name.upper()}_TX":
            log_uart["tx_port"] = f"GPIO{port_letter}"
            log_uart["tx_pin"] = f"GPIO_PIN_{pin_number}"
            log_uart["tx_af"] = f"GPIO_AF{af}_{cli_uart_name.upper()}"
        elif func == f"{cli_uart_name.upper()}_RX":
            log_uart["rx_port"] = f"GPIO{port_letter}"
            log_uart["rx_pin"] = f"GPIO_PIN_{pin_number}"
            log_uart["rx_af"] = f"GPIO_AF{af}_{cli_uart_name.upper()}"

    # Derive from USART instance (e.g., USART2)
    inst = cli_uart_name.upper()  # "USART2"
    log_uart["instance"] = inst
    log_uart["rcc_usart_clk"] = f"__HAL_RCC_{inst}_CLK_ENABLE"

    # STM32G0: USART2 shares IRQ with LPUART2, USART3 with LPUART1
    irq_map = {
        "USART1": "USART1_IRQn",
        "USART2": "USART2_LPUART2_IRQn",
        "USART3": "USART3_LPUART1_IRQn",
    }
    log_uart["irqn"] = irq_map.get(inst, f"{inst}_IRQn")

    # CCIPR selectors
    log_uart["ccipr_sel_msk"] = f"RCC_CCIPR_{inst}SEL_Msk"
    log_uart["ccipr_hsi_src"] = f"RCC_{inst}CLKSOURCE_HSI"

    # GPIO port clock enable (use TX port)
    tx_port = log_uart.get("tx_port", "GPIOA")
    log_uart["rcc_gpio_clk"] = f"__HAL_RCC_{tx_port}_CLK_ENABLE"

    # ---------- Tickless idle (requires FreeRTOS app_tasks + RTC) ----------
    # sleep.tickless field can explicitly override the auto-detection
    tickless_explicit = sleep.get("tickless", None)
    if tickless_explicit is not None:
        has_tickless = bool(tickless_explicit)
    else:
        has_tickless = bool(app_tasks) and has_rtc

    # ---------- 辅助函数：加载外部引用 ----------
    def load_external_flow(ref_path):
        full_path = os.path.join(EXAMPLES_DIR, ref_path)
        if not os.path.exists(full_path):
            full_path = ref_path
        try:
            with open(full_path, 'r', encoding='utf-8') as f:
                data = yaml.safe_load(f)
            if data and 'behavior' in data:
                return data['behavior']
        except (yaml.YAMLError, OSError) as e:
            print(f"Warning: cannot load ref {ref_path}: {e}")
        return None

    # ---------- 函数：为状态和变量添加命名空间前缀 ----------
    def apply_namespace(state, namespace, _parent_vars=None):
        """Apply namespace prefix to state names, variable names, and action
        references. Recursively walks substates, carrying parent variables so
        that calc/when/guard expressions in nested transitions are rewritten
        correctly."""
        # Collect all visible variables (current state + parent scope)
        all_vars = list(_parent_vars or [])
        if 'variables' in state:
            for var in state['variables']:
                var['name'] = f"{namespace}_{var['name']}"
            all_vars.extend(state['variables'])
        if 'initial_state' in state:
            state['initial_state'] = f"{namespace}_{state['initial_state']}"

        def replace_in_actions(actions):
            import re
            for i, act in enumerate(actions):
                for var in all_vars:
                    original_name = var['name'].replace(f"{namespace}_", "")
                    pattern = r'\b' + re.escape(original_name) + r'\b'
                    act = re.sub(pattern, var['name'], act)
                actions[i] = act
        for trans in state.get('transitions', []):
            replace_in_actions(trans.get('actions', []))
        if 'on_entry' in state:
            replace_in_actions(state['on_entry'])
        if 'on_exit' in state:
            replace_in_actions(state['on_exit'])

        if 'states' in state:
            for substate in state['states']:
                substate['name'] = f"{namespace}_{substate['name']}"
                for trans in substate.get('transitions', []):
                    trans['target'] = f"{namespace}_{trans['target']}"
                apply_namespace(substate, namespace, all_vars)

    # ---------- 函数：解析单个引用状态 ----------
    def resolve_ref(state, base_path=''):
        if state.get('type') != 'ref':
            return state
        ref_file = state.get('ref')
        if not ref_file:
            return state
        flow = load_external_flow(ref_file)
        if not flow:
            return state
        new_state = {
            'name': state['name'],
            'initial_state': flow.get('initial_state'),
            'states': flow.get('states', []),
            'variables': flow.get('variables', []),
            'transitions': state.get('transitions', []),
            'on_entry': state.get('on_entry', []),
            'on_exit': state.get('on_exit', []),
            'after': state.get('after', None),
            'history': state.get('history', False),
        }
        namespace = state.get('namespace')
        if namespace:
            apply_namespace(new_state, namespace)
        return new_state

    # ---------- 递归解析所有引用 ----------
    def resolve_all_refs(states, max_depth=5):
        for _ in range(max_depth):
            changed = False
            for i, s in enumerate(states):
                if s.get('type') == 'ref':
                    states[i] = resolve_ref(s)
                    changed = True
                if states[i].get('states'):
                    resolve_all_refs(states[i]['states'], max_depth - 1)
            if not changed:
                break

    if behavior:
        if behavior.get('states'):
            resolve_all_refs(behavior['states'])
        if behavior.get('regions'):
            for region in behavior['regions']:
                resolve_all_refs(region['states'])

    # ---------- 确保复合状态有 initial_state ----------
    def fix_initial_state(states):
        for s in states:
            if s.get('states') and not s.get('initial_state'):
                s['initial_state'] = s['states'][0]['name']
            if s.get('states'):
                fix_initial_state(s['states'])

    if behavior:
        if behavior.get('states'):
            fix_initial_state(behavior['states'])
        if behavior.get('regions'):
            for region in behavior['regions']:
                fix_initial_state(region['states'])

    # ---------- 检查是否有子状态 ----------
    def has_nested_states(states):
        for s in states:
            if s.get('states'):
                return True
        return False

    has_substate = False
    if behavior:
        if behavior.get('states'):
            has_substate = has_nested_states(behavior['states'])
        if behavior.get('regions'):
            for region in behavior['regions']:
                if has_nested_states(region['states']):
                    has_substate = True
                    break

    # ---------- HIL 配置 ----------
    hil_config = hw.get('hil', {})
    if not hil_config:
        hil_config = {
            'baudrate': 115200,
            'uart': 'UART2',
            'tx_pin': 'PA2',
            'rx_pin': 'PA3'
        }

    # 生成 HIL 测试用例列表
    hil_tests = []
    for p in peripherals:
        if p['type'] == 'Internal_RTC':
            hil_tests.append({
                'name': 'test_RTC_Init',
                'body': r"""
    RTC_HandleTypeDef hrtc;
    __HAL_RCC_RTC_ENABLE();
    HAL_PWR_EnableBkUpAccess();

    RCC_OscInitTypeDef RCC_OscInitStruct = {0};
    RCC_OscInitStruct.OscillatorType = RCC_OSCILLATORTYPE_LSE | RCC_OSCILLATORTYPE_LSI;
    RCC_OscInitStruct.LSEState = RCC_LSE_ON;
    RCC_OscInitStruct.LSIState = RCC_LSI_ON;
    RCC_OscInitStruct.PLL.PLLState = RCC_PLL_NONE;
    if (HAL_RCC_OscConfig(&RCC_OscInitStruct) != HAL_OK) {
        RCC_OscInitStruct.LSEState = RCC_LSE_OFF;
        HAL_RCC_OscConfig(&RCC_OscInitStruct);
    }

    RCC_PeriphCLKInitTypeDef PeriphClkInit = {0};
    PeriphClkInit.PeriphClockSelection = RCC_PERIPHCLK_RTC;
    PeriphClkInit.RTCClockSelection = (RCC_OscInitStruct.LSEState == RCC_LSE_ON) ?
                                       RCC_RTCCLKSOURCE_LSE : RCC_RTCCLKSOURCE_LSI;
    HAL_RCCEx_PeriphCLKConfig(&PeriphClkInit);

    __HAL_RCC_RTC_ENABLE();

    hrtc.Instance = RTC;
    hrtc.Init.HourFormat = RTC_HOURFORMAT_24;
    hrtc.Init.AsynchPrediv = 0;
    hrtc.Init.SynchPrediv = 32767;
    hrtc.Init.OutPut = RTC_OUTPUT_DISABLE;
    if (HAL_RTC_Init(&hrtc) != HAL_OK) {
        TEST_FAIL("HAL_RTC_Init failed");
    } else {
        TEST_PASS();
    }
"""
            })
    if not hil_tests:
        hil_tests.append({
            'name': 'test_dummy',
            'body': 'TEST_PASS();'
        })

    # ---------- Dict-format action normalization ----------
    # Convert new dict-format actions to legacy string format before processing.
    def normalize_dict_action(action):
        """
        Convert dict-format action to string format.
        E.g. {defer: {after: 3000, do: toggle_led}} -> "defer 3000 => toggle_led"
             {start_timer: {name: exit_timer, ms: 3000}} -> "start_timer exit_timer 3000"
             {timeline: [{ms: 1000, do: toggle_led}]} -> "timeline: 1000=>toggle_led"
        Returns the same string if already a string.
        """
        if isinstance(action, str):
            return action
        if not isinstance(action, dict):
            return str(action)

        keys = list(action.keys())
        if len(keys) != 1:
            return str(action)
        name = keys[0]
        params = action[name]

        # Simple actions (no params): toggle_led, return
        if name in ('toggle_led', 'return', 'EVENT_NONE'):
            return name

        if params is None:
            return name

        if name == 'defer':
            return f"defer {params.get('after', 0)} => {normalize_dict_action(params.get('do', ''))}"
        elif name == 'start_timer':
            return f"start_timer {params.get('name', '')} {params.get('ms', 0)}"
        elif name == 'stop_timer':
            return f"stop_timer {params.get('name', '')}"
        elif name == 'set':
            if 'op' in params:
                return f"set {params.get('var', '')} {params.get('op', 'inc')}"
            return f"set {params.get('var', '')} {params.get('value', 0)}"
        elif name == 'calc':
            return f"calc {params.get('var', '')} = {params.get('expr', '')}"
        elif name == 'publish':
            return f"publish {params.get('event', '')}"
        elif name == 'publish_async':
            return f"publish_async {params.get('event', '')}"
        elif name == 'when':
            # Support nested when: {when: {cond: "...", do: {when: {...}}}}
            # Recursively normalize the 'do' sub-action.
            sub_do = params.get('do', '')
            return f"when {params.get('cond', '')} => {normalize_dict_action(sub_do)}"
        elif name == 'send_to':
            return f"send_to {params.get('region', '')} {params.get('event', '')}"
        elif name == 'timeline':
            if isinstance(params, list):
                parts = [f"{item.get('ms', 0)}=>{normalize_dict_action(item.get('do', ''))}" for item in params]
                return "timeline: " + ", ".join(parts)
            return name
        else:
            return name

    def normalize_actions(action_list):
        """Normalize all actions in a list, converting dicts to strings."""
        if not action_list:
            return
        for i, act in enumerate(action_list):
            action_list[i] = normalize_dict_action(act)

    # Apply normalization to all action lists in behavior
    def traverse_and_normalize(states_or_regions):
        """Walk all states/regions and normalize action lists."""
        entries = states_or_regions if isinstance(states_or_regions, list) else [states_or_regions]
        for entry in entries:
            # States list
            for state in entry.get('states', []):
                if state.get('on_entry'):
                    normalize_actions(state['on_entry'])
                if state.get('on_exit'):
                    normalize_actions(state['on_exit'])
                for trans in state.get('transitions', []):
                    if trans.get('actions'):
                        normalize_actions(trans['actions'])
                if state.get('states'):
                    traverse_and_normalize(state)

    if behavior:
        if behavior.get('states'):
            traverse_and_normalize({'states': behavior['states']})
        if behavior.get('regions'):
            for region in behavior['regions']:
                traverse_and_normalize(region)

    # ---------- Defer / Timeline 动作处理 ----------
    defer_actions = []
    defer_counter = 0
    defer_timer_names = []

    def process_defer(action_list, defer_counter):
        new_actions = []
        for act in action_list:
            if act.startswith('timeline:'):
                content = act[len('timeline:'):].strip()
                items = [x.strip() for x in content.split(',')]
                for item in items:
                    parts = item.split('=>')
                    if len(parts) == 2:
                        time_ms = parts[0].strip()
                        sub_action = parts[1].strip()
                        timer_name = f"defer_{defer_counter}"
                        new_actions.append(f"start_timer {timer_name} {time_ms}")
                        defer_actions.append({
                            'timer_name': timer_name,
                            'sub_action': sub_action
                        })
                        defer_timer_names.append(timer_name)
                        defer_counter += 1
            elif act.startswith('defer '):
                parts = act.split('=>', 1)
                if len(parts) == 2:
                    time_part = parts[0].strip().split()
                    if len(time_part) >= 2:
                        time_ms = time_part[1]
                        sub_action = parts[1].strip()
                        timer_name = f"defer_{defer_counter}"
                        new_actions.append(f"start_timer {timer_name} {time_ms}")
                        defer_actions.append({
                            'timer_name': timer_name,
                            'sub_action': sub_action
                        })
                        defer_timer_names.append(timer_name)
                        defer_counter += 1
                        continue
            new_actions.append(act)
        return new_actions, defer_counter

    def traverse_states(states, defer_counter):
        for state in states:
            for trans in state.get('transitions', []):
                new_acts, defer_counter = process_defer(trans.get('actions', []), defer_counter)
                trans['actions'] = new_acts
            if 'on_entry' in state:
                new_acts, defer_counter = process_defer(state['on_entry'], defer_counter)
                state['on_entry'] = new_acts
            if 'on_exit' in state:
                new_acts, defer_counter = process_defer(state['on_exit'], defer_counter)
                state['on_exit'] = new_acts
            if 'states' in state:
                defer_counter = traverse_states(state['states'], defer_counter)
        return defer_counter

    if behavior:
        if behavior.get('states'):
            traverse_states(behavior['states'], 0)
        if behavior.get('regions'):
            for region in behavior['regions']:
                traverse_states(region['states'], 0)

    # ---------- 收集所有定时器事件名 ----------
    timer_events = set()
    # 收集用户显式声明的 start_timer / state.after 定时器
    user_timer_actions = []  # Fix 2: 替代模板 collect_timers 宏

    def collect_timer_events_from_states(states, prefix=''):
        for state in states:
            for trans in state.get('transitions', []):
                for action in trans.get('actions', []):
                    if action.startswith('start_timer '):
                        timer_name = action.split(' ')[1]
                        if not timer_name.startswith('defer_'):
                            period = action.split(' ')[2]
                            user_timer_actions.append({
                                'timer_name': timer_name,
                                'period': period,
                            })
                        timer_events.add(f"EVENT_TIMER_EXPIRED_{timer_name}")
            for action in state.get('on_entry', []):
                if action.startswith('start_timer '):
                    timer_name = action.split(' ')[1]
                    if not timer_name.startswith('defer_'):
                        period = action.split(' ')[2]
                        user_timer_actions.append({
                            'timer_name': timer_name,
                            'period': period,
                        })
                    timer_events.add(f"EVENT_TIMER_EXPIRED_{timer_name}")
            for action in state.get('on_exit', []):
                if action.startswith('start_timer '):
                    timer_name = action.split(' ')[1]
                    if not timer_name.startswith('defer_'):
                        period = action.split(' ')[2]
                        user_timer_actions.append({
                            'timer_name': timer_name,
                            'period': period,
                        })
                    timer_events.add(f"EVENT_TIMER_EXPIRED_{timer_name}")
            if state.get('after'):
                timeout_name = f"{prefix}{state['name']}_timeout"
                user_timer_actions.append({
                    'timer_name': timeout_name,
                    'period': state['after'],
                })
                timer_events.add(f"EVENT_TIMER_EXPIRED_{timeout_name}")
            if state.get('states'):
                collect_timer_events_from_states(state['states'], prefix)

    if behavior:
        if behavior.get('states'):
            collect_timer_events_from_states(behavior['states'])
        if behavior.get('regions'):
            for region in behavior['regions']:
                collect_timer_events_from_states(region['states'], region['name'] + '_')

    # ---------- 收集所有 publish / publish_async 事件 ----------
    published_events = set()

    def extract_publish_events(action_str: str):
        """从动作字符串中提取 publish / publish_async / send_to 的事件名"""
        events = set()
        # 直接发布动作
        if action_str.startswith('publish ') or action_str.startswith('publish_async '):
            events.add(action_str.split()[-1])
        # send_to 跨区域事件：send_to led_region LED_ON
        elif action_str.startswith('send_to '):
            parts = action_str.split(' ')
            if len(parts) >= 3:
                events.add(parts[2])
        # defer 内嵌发布：defer 1000 => publish FOO
        elif action_str.startswith('defer ') and '=>' in action_str:
            sub = action_str.split('=>', 1)[1].strip()
            if sub.startswith('publish ') or sub.startswith('publish_async '):
                events.add(sub.split()[-1])
        # timeline 内嵌发布：timeline: 1000=>publish FOO, 2000=>publish BAR
        elif action_str.startswith('timeline:') and '=>' in action_str:
            # 提取 "timeline: " 之后的部分
            content = action_str.split(':', 1)[1].strip() if ':' in action_str else action_str
            for part in content.split(','):
                if '=>' in part:
                    sub = part.split('=>', 1)[1].strip()
                    if sub.startswith('publish ') or sub.startswith('publish_async '):
                        events.add(sub.split()[-1])
        # when 条件发布：when condition => publish_async EVENT
        elif '=>' in action_str:
            sub = action_str.split('=>', 1)[1].strip()
            if sub.startswith('publish ') or sub.startswith('publish_async '):
                events.add(sub.split()[-1])
        return events

    def collect_published_events(states):
        for state in states:
            for trans in state.get('transitions', []):
                for action in trans.get('actions', []):
                    published_events.update(extract_publish_events(action))
            for action in state.get('on_entry', []):
                published_events.update(extract_publish_events(action))
            for action in state.get('on_exit', []):
                published_events.update(extract_publish_events(action))
            if state.get('states'):
                collect_published_events(state['states'])

    if behavior:
        if behavior.get('states'):
            collect_published_events(behavior['states'])
        if behavior.get('regions'):
            for region in behavior['regions']:
                collect_published_events(region['states'])

    # 同时从已经处理好的 defer_actions 中提取 publish 事件
    # （traverse_states 已将 "defer 1000 => publish FOO" 替换为 "start_timer defer_N 1000"，
    #   原始 publish 动作被移入 defer_actions[].sub_action）
    for d in defer_actions:
        sub = d.get('sub_action', '')
        if sub.startswith('publish ') or sub.startswith('publish_async '):
            published_events.add(sub.split()[-1])

    # ---------- 收集所有 transition event 名称（非内置事件、非定时器事件）----------
    BUILTIN_EVENTS = {
        'BUTTON_PRESS', 'RTC_TICK', 'RTC_ALARM', 'RETURN',
        'MINUTE_TICK', 'HOUR_TICK', 'MPU6050_ALERT',
    }
    transition_events = set()

    def collect_transition_events(states):
        for state in states:
            for trans in state.get('transitions', []):
                evt_name = trans.get('event', '')
                evt_upper = evt_name.replace(' ', '_').upper()
                if evt_name and evt_upper not in BUILTIN_EVENTS:
                    event_key = evt_name.replace(' ', '_')
                    # Skip timer expiry events (handled by timer_events separately)
                    if event_key.startswith('TIMER_EXPIRED_'):
                        continue
                    # Skip events already collected as published_events
                    if event_key not in published_events:
                        transition_events.add(event_key)
            if state.get('states'):
                collect_transition_events(state['states'])

    if behavior:
        if behavior.get('states'):
            collect_transition_events(behavior['states'])
        if behavior.get('regions'):
            for region in behavior['regions']:
                collect_transition_events(region['states'])

    # Button gesture events — always generated when btn_task is present
    if has_btn_task:
        for evt in ('BUTTON_SHORT_PRESS', 'BUTTON_DOUBLE_PRESS', 'BUTTON_LONG_PRESS'):
            transition_events.add(evt)

    # ---------- Auto-inject RTC driver when business flow needs timers ----------
    # Demos with timer_events or defer_actions need the software timer
    # infrastructure from drv_rtc, even if no explicit Internal_RTC peripheral
    # is declared.  Inject a synthetic driver entry so drv_rtc.c/h are generated.
    if (timer_events or defer_actions) and not has_rtc:
        has_rtc = True
        rtc_model = load_model('Internal_RTC')
        synth_peripheral = {
            'name': 'rtc',
            'type': 'Internal_RTC',
            'interface': 'internal',
            'model': rtc_model,
        }
        drivers.append({
            'name': 'rtc',
            'template': rtc_model.get('driver_template', ''),
            'header_template': rtc_model.get('header_template', ''),
            'model': rtc_model,
            'peripheral': synth_peripheral,
        })
        peripherals.append(synth_peripheral)
        # HAL sources for RTC and TIM (needed for drv_rtc.c)
        for rtc_hal in ['stm32g0xx_hal_rtc.c', 'stm32g0xx_hal_rtc_ex.c',
                         'stm32g0xx_hal_tim.c', 'stm32g0xx_hal_tim_ex.c']:
            if rtc_hal not in hal_sources:
                hal_sources.append(rtc_hal)

    # ---------- 预计算：MPU6050 缩放因子和寄存器配置值 ----------
    mpu6050_scale = {}
    for p in peripherals:
        if p.get("type") == "I2C_Sensor_MPU6050":
            extra = p.get("extra", {})
            # Accel scale: 2g→16384.0, 4g→8192.0, 8g→4096.0, 16g→2048.0
            accel_fs = extra.get("accel_fs", 2)
            accel_scale_map = {2: 16384.0, 4: 8192.0, 8: 4096.0, 16: 2048.0}
            accel_scale = accel_scale_map.get(accel_fs, 16384.0)
            # Accel register value: 2→0x00, 4→0x08, 8→0x10, 16→0x18
            accel_reg_map = {2: "0x00", 4: "0x08", 8: "0x10", 16: "0x18"}
            accel_fs_val = accel_reg_map.get(accel_fs, "0x00")
            # Gyro scale: 250→131.0, 500→65.5, 1000→32.8, 2000→16.4
            gyro_fs = extra.get("gyro_fs", 250)
            gyro_scale_map = {250: 131.0, 500: 65.5, 1000: 32.8, 2000: 16.4}
            gyro_scale = gyro_scale_map.get(gyro_fs, 131.0)
            # Gyro register value: 250→0x00, 500→0x08, 1000→0x10, 2000→0x18
            gyro_reg_map = {250: "0x00", 500: "0x08", 1000: "0x10", 2000: "0x18"}
            gyro_fs_val = gyro_reg_map.get(gyro_fs, "0x00")
            mpu6050_scale = {
                "accel_scale": accel_scale,
                "accel_fs_val": accel_fs_val,
                "accel_fs": accel_fs,
                "gyro_scale": gyro_scale,
                "gyro_fs_val": gyro_fs_val,
                "gyro_fs": gyro_fs,
            }
            # Inject into peripheral dict for template access
            p["_mpu6050"] = mpu6050_scale
            break

    # ---------- 预计算：EXTI handler 分组 ----------
    def _exti_handler_name(pin_id: str) -> str:
        num = int(pin_id[2:]) if len(pin_id) > 2 else 0
        if num <= 1:
            return "EXTI0_1_IRQHandler"
        elif num <= 3:
            return "EXTI2_3_IRQHandler"
        else:
            return "EXTI4_15_IRQHandler"

    exti_handler_groups = {}
    for pin in pins:
        if pin.get("exti", {}).get("enable"):
            handler = _exti_handler_name(pin["id"])
            exti_handler_groups.setdefault(handler, []).append(pin)

    # ---------- 预计算：PWM 预分频器和周期 ----------
    pwm_tim_prescaler = 15999   # PCLK/(prescaler+1) = timer tick rate
    pwm_tim_period = 999        # Timer period in ticks

    # ---------- 预计算：RTC 时钟源 ----------
    rtc_clock_source = "LSI"
    rtc_wakeup_interval_ms = 100  # default: 100 ms
    rtc_alarms = []               # list of {period_s: int, event: str}
    rtc_init_time = {"year": 0, "month": 1, "day": 1, "hour": 0, "min": 0, "sec": 0}
    for p in peripherals:
        if p.get("type") == "Internal_RTC":
            rtc_clock_source = p.get("extra", {}).get("clock_source", rtc_clock_source)
            p["_clock_source"] = rtc_clock_source  # inject into peripheral dict
            extra = p.get("extra", {})
            # Parse wakeup interval
            wkup = extra.get("wakeup_interval_ms", None)
            if wkup is not None:
                rtc_wakeup_interval_ms = int(wkup)
            # Parse alarms
            alarms_raw = extra.get("alarms", [])
            for alarm in alarms_raw:
                atype = alarm.get("type", "periodic_sec")
                period_s = int(alarm.get("period_s", 0))
                delay_ms = int(alarm.get("delay_ms", 0))
                rtc_alarms.append({
                    "type": atype,
                    "period_ms": (period_s * 1000) if atype != "one_shot_ms" else delay_ms,
                    "event": alarm.get("event", "").upper(),
                })
            # Parse initial_time from extra config
            init_str = extra.get("initial_time", "")
            if init_str:
                try:
                    # Format: "YYYY-MM-DD HH:MM:SS"
                    date_part, time_part = init_str.split(" ")
                    y, mo, d = date_part.split("-")
                    h, mi, s = time_part.split(":")
                    rtc_year = int(y) - 2000
                    rtc_init_time = {
                        "year": max(0, min(99, rtc_year)),
                        "month": int(mo),
                        "day": int(d),
                        "hour": int(h),
                        "min": int(mi),
                        "sec": int(s),
                    }
                except (ValueError, AttributeError):
                    pass  # malformed, use defaults

    # ---------- 预计算：Bootloader 字节大小 ----------
    boot_size_bytes = boot_config.get("size_kb", 8) * 1024 if has_bootloader else 0

    # ---------- 预计算：状态枚举稳定值（djb2 哈希） ----------
    def _djb2_hash(s: str) -> int:
        h = 5381
        for c in s:
            h = ((h << 5) + h) + ord(c)
        return h & 0x7FFFFFFF

    def compute_state_enums(flow):
        """为所有状态分配基于名称哈希的稳定枚举值，替代 loop.index0"""
        if not flow:
            return
        def walk(states, prefix=''):
            for state in states:
                full_name = f"{prefix}{state['name']}"
                state['_enum'] = _djb2_hash(full_name)
                if state.get('states'):
                    walk(state['states'], f"{full_name}_")
        if flow.get('states'):
            walk(flow['states'])
        if flow.get('regions'):
            for region in flow['regions']:
                walk(region['states'], f"{region['name']}_")
    compute_state_enums(behavior)

    # ---------- 预计算：FreeRTOS 堆大小 ----------
    def compute_heap_size(tasks, has_cli, has_fota, cli_stack=512):
        heap = 0
        for task in tasks:
            stack = task.get('stack_size', 128)
            heap += stack * 4 + 128   # stack bytes + TCB overhead
        # Event manager task
        heap += 512 * 4 + 128
        if has_cli:
            heap += cli_stack * 4 + 128
        if has_fota:
            heap += 512 * 4 + 128
        # Timer task (FreeRTOS configTIMER_TASK_STACK_DEPTH=256)
        heap += 256 * 4 + 128
        # Event queue: 100 entries × 8 bytes + overhead
        heap += 100 * 8 + 256
        # Driver / kernel overhead
        heap += 4096
        return ((heap + 1023) // 1024) * 1024   # round to KiB
    total_heap_size = compute_heap_size(app_tasks, has_cli, has_fota)

    # ---------- 查找 LED 任务名 ----------
    led_task_name = None
    for t in app_tasks:
        if t.get('name') == 'led_task':
            led_task_name = 'led_task'
            break

    # ---------- 静态库绝对路径 ----------
    static_dir_absolute = os.path.abspath(STATIC_STM32_DIR).replace("\\", "/")

    # ---------- 构建 IR (Intermediate Representation) ----------
    from ..ir import ProjectIR, McuIR, LogIR, RtcIR, RtcAlarmIR, RtcInitTimeIR
    from ..ir import BootIR, ExtiIR, HilIR

    mcu_ir = McuIR(
        part=mcu.get("part", ""),
        core=mcu.get("core", "Cortex-M0+"),
        core_clock_mhz=mcu.get("core_clock_mhz", 16),
        clock_source=mcu.get("clock_source", "HSI"),
        clock_freq_hz=mcu.get("clock_freq_hz", 16000000),
        hse_freq=mcu.get("hse_freq", 8000000),
        hclk_freq_hz=mcu.get("hclk_freq_hz", mcu.get("clock_freq_hz", 16000000)),
        flash_kb=mcu.get("flash_kb", 512),
    )

    log_ir = LogIR(
        enabled=has_log,
        ring_buf_size=log_ring_buf_size,
        uart_instance=log_uart.get("instance", ""),
        uart_irqn=log_uart.get("irqn", ""),
        uart_rcc_clk=log_uart.get("rcc_usart_clk", ""),
        uart_ccipr_sel_msk=log_uart.get("ccipr_sel_msk", ""),
        uart_ccipr_hsi_src=log_uart.get("ccipr_hsi_src", ""),
        tx_port=log_uart.get("tx_port", ""),
        tx_pin=log_uart.get("tx_pin", ""),
        tx_af=log_uart.get("tx_af", ""),
        rx_port=log_uart.get("rx_port", ""),
        rx_pin=log_uart.get("rx_pin", ""),
        rx_af=log_uart.get("rx_af", ""),
        rcc_gpio_clk=log_uart.get("rcc_gpio_clk", ""),
    )

    alarm_objs = [
        RtcAlarmIR(type=a["type"], period_ms=a["period_ms"], event=a["event"])
        for a in rtc_alarms
    ]
    init_t = rtc_init_time
    rtc_ir = RtcIR(
        has_rtc=has_rtc,
        clock_source=rtc_clock_source,
        async_prediv=0,
        sync_prediv=32767,
        wakeup_interval_ms=rtc_wakeup_interval_ms,
        alarms=alarm_objs,
        init_time=RtcInitTimeIR(
            year=init_t["year"], month=init_t["month"], day=init_t["day"],
            hour=init_t["hour"], min=init_t["min"], sec=init_t["sec"],
        ),
    )

    boot_ir = BootIR(
        enabled=has_bootloader,
        size_kb=boot_config.get("size_kb", 8),
        size_bytes=boot_size_bytes,
        app_a_offset=boot_config.get("app_a_offset", 0x2000),
        app_b_offset=boot_config.get("app_b_offset", 0x4000),
        crc_method=boot_config.get("crc_method", "CRC32"),
        boot_flag_src=boot_config.get("boot_flag_src", "RAM"),
        max_retries=boot_config.get("max_retries", 3),
        wdg_timeout_ms=boot_config.get("wdg_timeout_ms", 5000),
        iwdg_reload_value=boot_config.get("iwdg_reload_value", 625),
        led_port=boot_led.get("boot_led_port", "GPIOC"),
        led_pin_num=boot_led.get("boot_led_pin_num", 0),
        led_rcc_enable=boot_led.get("boot_led_rcc_enable", "RCC_IOPENR_GPIOCEN"),
        raw=boot_config,
    )

    exti_ir = ExtiIR(groups=exti_handler_groups)

    hil_ir = HilIR(
        baudrate=hil_config.get("baudrate", 115200),
        uart=hil_config.get("uart", "UART2"),
        tx_pin=hil_config.get("tx_pin", "PA2"),
        rx_pin=hil_config.get("rx_pin", "PA3"),
        tests=hil_tests,
    )

    project_ir = ProjectIR(
        mcu=mcu_ir,
        log=log_ir,
        project_name=project_name,
        pins=pins,
        sleep=sleep,
        app_tasks=app_tasks,
        hal_sources=hal_sources,
        peripherals=peripherals,
        drivers=drivers,
        has_i2c=has_i2c,
        has_rtc=has_rtc,
        has_mpu6050=has_mpu6050,
        has_pwm=has_pwm,
        has_spi=has_spi,
        has_spi_flash=has_spi_flash,
        has_adc=has_adc,
        has_uart=has_uart,
        has_rs485=has_rs485,
        has_ir=has_ir,
        has_cellular=has_cellular,
        has_cli=has_cli,
        has_temp_sensor=has_temp_sensor,
        has_telemetry=has_telemetry,
        has_power_mgr=has_power_mgr,
        has_modbus=has_modbus,
        has_mqtt=has_mqtt,
        has_led=has_led,
        has_led_task=has_led_task,
        has_btn_task=has_btn_task,
        has_behavior=has_behavior,
        has_substate=has_substate,
        has_bootloader=has_bootloader,
        has_fota=has_fota,
        has_iwdg=has_bootloader,
        has_event_mgr=True,
        has_tickless=has_tickless,
        has_log=has_log,
        uart_name=uart_name,
        rs485_name=rs485_name,
        modbus_name=modbus_name,
        cli_uart_name=cli_uart_name,
        cli_name=cli_name,
        led_active_low=led_active_low,
        led_task_name=led_task_name or "",
        rtc=rtc_ir,
        pwm_tim_prescaler=pwm_tim_prescaler,
        pwm_tim_period=pwm_tim_period,
        temp_offset_deci=temp_offset_deci,
        usart2_baudrate=usart2_baudrate,
        usart2_clock_freq_hz=usart2_clock_freq_hz,
        behavior=behavior,
        periodic_events=periodic_events,
        defer_actions=defer_actions,
        defer_timer_names=defer_timer_names,
        user_timer_actions=user_timer_actions,
        timer_events=sorted(timer_events),
        published_events=sorted(published_events),
        transition_events=sorted(transition_events),
        events=behavior.get('events', []),
        boot=boot_ir,
        exti=exti_ir,
        hil=hil_ir,
        hil_mode=hil_mode,
        hil_tests=hil_tests,
        heap_size=hw.get("heap_size", "0x200"),
        stack_size=hw.get("stack_size", "0x400"),
        total_heap_size=total_heap_size,
        flash_kb=mcu_flash_kb,
        test_mode=False,
        static_dir_absolute=static_dir_absolute,
    )

    return project_ir.to_dict()
