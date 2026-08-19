import re
import os
import importlib.util

from .paths import MODELS_DIR, EXAMPLES_DIR

# Load generator/generator_types.py with explicit module name to avoid collision
# with Python's stdlib 'types' module which is frozen at interpreter startup.
_types_spec = importlib.util.spec_from_file_location(
    "hw2c_types",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "generator_types.py")
)
_types_module = importlib.util.module_from_spec(_types_spec)
_types_spec.loader.exec_module(_types_module)
ValidationError = _types_module.ValidationError

# ---------- Expression validation helpers ----------

_VALID_C_TYPES = {'uint8_t', 'uint16_t', 'uint32_t', 'int8_t', 'int16_t', 'int32_t', 'float', 'bool'}
_COMPARISON_OPS = {'>', '>=', '<', '<=', '==', '!='}
_C_IDENTIFIER = re.compile(r'^[a-zA-Z_][a-zA-Z0-9_]*$')


def _collect_all_variables(hw: dict) -> dict[str, str]:
    """
    Collect all declared variable names from behavior and regions.
    Returns dict: {var_name: var_type_str}
    """
    variables = {}
    bf = hw.get('behavior', {})
    if not bf:
        return variables

    for var in bf.get('variables', []):
        if 'name' not in var:
            continue
        variables[var['name']] = var.get('type', 'uint32_t')

    for region in bf.get('regions', []):
        prefix = region.get('name', '') + '_'
        for var in region.get('variables', []):
            variables[prefix + var['name']] = var.get('type', 'uint32_t')

    return variables


def _collect_custom_types(hw: dict) -> set[str]:
    """Collect all custom type names from behavior.types."""
    bf = hw.get('behavior', {})
    if not bf:
        return set()
    return {t['name'] for t in bf.get('types', []) if 'name' in t}


def _validate_custom_types(bf: dict) -> list[str]:
    """Validate behavior.types entries."""
    errors = []
    custom_types = set()
    for i, tdef in enumerate(bf.get('types', [])):
        name = tdef.get('name', f'#{i}')
        if not tdef.get('name'):
            errors.append(f"[ERROR] TypeDef #{i} has no 'name' field.")
            continue
        if tdef['name'] in custom_types:
            errors.append(f"[ERROR] Duplicate type name '{tdef['name']}'.")
        custom_types.add(tdef['name'])

        kind = None
        if 'struct' in tdef:
            kind = 'struct'
        elif 'enum' in tdef:
            kind = 'enum'
        elif 'union' in tdef:
            kind = 'union'
        elif 'bitfield' in tdef:
            kind = 'bitfield'

        if kind is None:
            errors.append(f"[ERROR] Type '{name}' must have one of: struct, enum, union, bitfield.")
            continue

        if kind == 'enum':
            values_seen = set()
            for j, ev in enumerate(tdef.get('enum', [])):
                if 'name' not in ev:
                    errors.append(f"[ERROR] Enum value #{j} in '{name}' has no 'name' field.")
                if 'value' in ev and ev['value'] in values_seen:
                    errors.append(f"[ERROR] Duplicate enum value {ev['value']} in '{name}'.")
                if 'value' in ev:
                    values_seen.add(ev['value'])

        if kind == 'bitfield':
            total_width = 0
            for j, bf_member in enumerate(tdef.get('bitfield', [])):
                if 'name' not in bf_member:
                    errors.append(f"[ERROR] Bitfield member #{j} in '{name}' has no 'name' field.")
                if 'width' in bf_member:
                    total_width += bf_member['width']
            if total_width > 64:
                errors.append(f"[WARNING] Bitfield '{name}' total width ({total_width}) exceeds 64 bits.")

        if kind in ('struct', 'union'):
            for j, field in enumerate(tdef.get(kind, [])):
                if 'name' not in field:
                    errors.append(f"[ERROR] {kind.capitalize()} field #{j} in '{name}' has no 'name' field.")
                if 'type' not in field and 'fields' not in field:
                    errors.append(f"[ERROR] {kind.capitalize()} field '{field.get('name', f'#{j}')}' in '{name}' has no 'type' field (required unless it has nested 'fields').")
                if kind == 'struct' and 'fields' in field:
                    for k, nested in enumerate(field.get('fields', [])):
                        if 'name' not in nested:
                            errors.append(f"[ERROR] Nested struct field #{k} in '{name}.{field['name']}' has no 'name' field.")
                        if 'type' not in nested:
                            errors.append(f"[ERROR] Nested struct field '{nested.get('name', f'#{k}')}' in '{name}.{field['name']}' has no 'type' field.")

    return errors


def _validate_guard(guard_str: str, variables: dict[str, str], location: str) -> list[str]:
    """
    Validate a guard condition string.
    Expected format: "var_name OP literal"
    Returns list of error strings.
    """
    errors = []
    if not guard_str or not guard_str.strip():
        return errors

    expr = guard_str.strip()

    # Try to match: IDENTIFIER OP (literal|IDENTIFIER)
    for op in sorted(_COMPARISON_OPS, key=len, reverse=True):
        if op in expr:
            parts = expr.split(op, 1)
            left = parts[0].strip()
            right = parts[1].strip()
            break
    else:
        # No operator found — might be a boolean variable reference like "flag_name"
        if _C_IDENTIFIER.match(expr):
            if expr not in variables:
                errors.append(f"[ERROR] Guard variable '{expr}' in {location} is not declared in variables list. Available: {sorted(variables.keys())}")
            return errors
        else:
            # Could be a complex C expression — warn but don't error
            errors.append(f"[WARNING] Guard expression '{guard_str}' in {location} could not be parsed as a simple comparison. It will be used as-is.")
            return errors

    # Check left side (must be a declared variable)
    if not _C_IDENTIFIER.match(left):
        errors.append(f"[WARNING] Guard left-hand side '{left}' in {location} does not look like a variable name. It will be used as-is.")
    elif left not in variables:
        errors.append(f"[ERROR] Guard variable '{left}' in {location} is not declared in variables list. Available: {sorted(variables.keys())}")

    # Check right side (can be a literal number, another variable, or a constant)
    if right.isdigit() or (right.startswith('0x') and all(c in '0123456789abcdefABCDEF' for c in right[2:])):
        pass  # Numeric literal — OK
    elif _C_IDENTIFIER.match(right):
        if right not in variables:
            errors.append(f"[INFO] Guard right-hand side '{right}' in {location} is not a declared variable. Assuming it is a C constant or macro.")
    else:
        errors.append(f"[INFO] Guard right-hand side '{right}' in {location} is a complex expression. It will be used as-is.")

    return errors


def _validate_calc(calc_str: str, variables: dict[str, str], location: str) -> list[str]:
    """
    Validate a calc expression string.
    Expected format: "dest_var = expression"
    Returns list of error strings.
    """
    errors = []
    if not calc_str or not calc_str.strip():
        return errors

    expr = calc_str.strip()
    if '=' not in expr:
        errors.append(f"[ERROR] Calc expression '{calc_str}' in {location} missing '='. Format: 'dest = expression'.")
        return errors

    parts = expr.split('=', 1)
    dest = parts[0].strip()
    rhs = parts[1].strip()

    if not _C_IDENTIFIER.match(dest):
        errors.append(f"[ERROR] Calc destination '{dest}' in {location} is not a valid variable name.")
    elif dest not in variables:
        errors.append(f"[ERROR] Calc destination variable '{dest}' in {location} is not declared. Available: {sorted(variables.keys())}")

    # Extract identifiers from RHS and check them
    rhs_tokens = re.findall(r'[a-zA-Z_][a-zA-Z0-9_]*', rhs)
    for token in rhs_tokens:
        if token not in variables and token not in {'inc', 'dec'}:
            errors.append(f"[INFO] Calc references '{token}' in {location} which is not a declared variable. Assuming C constant/macro.")

    return errors


def _validate_when(when_str: str, variables: dict[str, str], location: str) -> list[str]:
    """
    Validate a when condition+action string.
    Expected format: "var OP literal => action"
    Returns list of error strings.
    """
    errors = []
    if not when_str or not when_str.strip():
        return errors

    expr = when_str.strip()

    if '=>' not in expr:
        errors.append(f"[ERROR] When expression '{when_str}' in {location} missing '=>' separator. Format: 'condition => action'.")
        return errors

    cond_part, action_part = expr.split('=>', 1)
    cond = cond_part.strip()
    action = action_part.strip()

    # Validate the condition part using the same logic as guards
    for op in sorted(_COMPARISON_OPS, key=len, reverse=True):
        if op in cond:
            parts = cond.split(op, 1)
            left = parts[0].strip()
            right = parts[1].strip()
            break
    else:
        # No operator — treat as boolean variable
        if _C_IDENTIFIER.match(cond) and cond not in variables:
            errors.append(f"[ERROR] When condition variable '{cond}' in {location} is not declared. Available: {sorted(variables.keys())}")
        if not _C_IDENTIFIER.match(cond):
            errors.append(f"[WARNING] When condition '{cond}' in {location} is complex. It will be used as-is.")
        return errors

    if not _C_IDENTIFIER.match(left):
        errors.append(f"[WARNING] When left-hand side '{left}' in {location} does not look like a variable name.")
    elif left not in variables:
        errors.append(f"[ERROR] When variable '{left}' in {location} is not declared. Available: {sorted(variables.keys())}")

    # Validate the action part (basic check — it should be a known action)
    return errors


# ---------- Main validator ----------


def _validate_extra_fields(peripheral: dict, model_path: str, errors: list[str]) -> None:
    """
    Validate peripheral extra fields against model's extra_schema.
    """
    import yaml
    try:
        with open(model_path, 'r', encoding='utf-8') as f:
            model = yaml.safe_load(f)
    except (yaml.YAMLError, OSError):
        return

    schema = model.get('extra_schema', {})
    if not schema:
        return

    extra = peripheral.get('extra', {})
    pname = peripheral.get('name', 'unknown')

    for field_name, field_schema in schema.items():
        field_type = field_schema.get('type', 'str')
        required = field_schema.get('required', False)
        default = field_schema.get('default')
        allowed_values = field_schema.get('values')

        # Check extra dict first, then fall back to top-level peripheral fields
        if field_name in extra:
            value = extra[field_name]
        elif field_name in peripheral:
            value = peripheral[field_name]
        else:
            if required:
                errors.append(f"[ERROR] Peripheral '{pname}' is missing required field '{field_name}'.")
            continue

        # Type check
        if field_type == 'int' and not isinstance(value, int):
            errors.append(f"[ERROR] Peripheral '{pname}' field '{field_name}' must be an integer, got '{value}'.")
        elif field_type == 'str' and not isinstance(value, str):
            errors.append(f"[ERROR] Peripheral '{pname}' field '{field_name}' must be a string, got '{value}'.")
        elif field_type == 'pin':
            if not isinstance(value, str) or not re.match(r'^P[A-F][0-9]{1,2}$', value):
                errors.append(f"[ERROR] Peripheral '{pname}' field '{field_name}' must be a valid pin ID (e.g. PA2), got '{value}'.")

        # Enum check
        if allowed_values and value not in allowed_values:
            errors.append(f"[WARNING] Peripheral '{pname}' field '{field_name}' value '{value}' is not in recommended list: {allowed_values}.")


# =========================================================================
# P3: Cross-component contract validation helpers
# =========================================================================

_VALID_TOPIC_VALUE_TYPES = {'float', 'int32', 'uint32', 'bool'}

# Templates' publish payload types per topic. Each entry is
# (payload_type, payload_unit). The generator checks that pubsub.yaml
# declares a matching value.type / value.unit so a component can never
# silently publish a scaled int32 to a topic declared as float (or the
# reverse) without failing generation.
_TEMPLATE_PUBLISH_CONTRACTS = {
    'att_roll':     ('int32', '0.01 deg'),
    'att_pitch':    ('int32', '0.01 deg'),
    'att_yaw':      ('int32', '0.01 deg'),
    'imu_temp':     ('int32', '0.1 C'),
    'knob_angle':   ('int32', '0.001 rad'),
    'fall_detected': ('int32', '0.01 g'),
}


def _collect_transition_event_names(bf: dict) -> set[str]:
    """Event names consumed by state machine transitions (states/regions)."""
    consumed = set()

    def walk(states):
        for st in states or []:
            for tr in st.get('transitions', []) or []:
                if isinstance(tr, dict) and tr.get('event'):
                    consumed.add(str(tr['event']).strip())
            walk(st.get('states'))

    walk((bf or {}).get('states'))
    for region in (bf or {}).get('regions', []):
        walk(region.get('states'))
    return consumed


def _extract_publish_targets_from_action(action) -> set[str]:
    """Extract publish / publish_async / send_to event names from an action."""
    targets = set()
    if isinstance(action, str):
        s = action.strip()
        if s.startswith('publish ') or s.startswith('publish_async '):
            targets.add(s.split()[-1])
        elif s.startswith('send_to '):
            parts = s.split(' ')
            if len(parts) >= 3:
                targets.add(parts[2])
        elif '=>' in s:
            sub = s.split('=>', 1)[1].strip()
            if sub.startswith('publish ') or sub.startswith('publish_async '):
                targets.add(sub.split()[-1])
        if s.startswith('timeline:'):
            content = s.split(':', 1)[1].strip() if ':' in s else s
            for part in content.split(','):
                if '=>' in part:
                    sub = part.split('=>', 1)[1].strip()
                    if sub.startswith('publish ') or sub.startswith('publish_async '):
                        targets.add(sub.split()[-1])
    elif isinstance(action, dict):
        for key, params in action.items():
            if key in ('publish', 'publish_async'):
                if isinstance(params, str):
                    targets.add(params.strip())
                elif isinstance(params, dict) and params.get('topic'):
                    targets.add(str(params['topic']).strip())
            elif key == 'send_to':
                if isinstance(params, str):
                    targets.add(params.strip())
                elif isinstance(params, dict) and params.get('event'):
                    targets.add(str(params['event']).strip())
    return targets


def _collect_published_event_names(bf: dict) -> set[str]:
    """Event names produced by publish/publish_async/send_to actions."""
    produced = set()

    def walk(states):
        for st in states or []:
            for action in st.get('on_entry', []) or []:
                produced.update(_extract_publish_targets_from_action(action))
            for action in st.get('on_exit', []) or []:
                produced.update(_extract_publish_targets_from_action(action))
            for tr in st.get('transitions', []) or []:
                if isinstance(tr, dict):
                    for action in tr.get('actions', []) or []:
                        produced.update(_extract_publish_targets_from_action(action))
            walk(st.get('states'))

    walk((bf or {}).get('states'))
    for region in (bf or {}).get('regions', []):
        walk(region.get('states'))
    return produced


def _collect_timer_event_names(bf: dict) -> set[str]:
    """Timer-expiry events produced by start_timer / state.after actions."""
    names = set()

    def walk(states, prefix=''):
        for st in states or []:
            for action in st.get('on_entry', []) or []:
                if isinstance(action, str) and action.strip().startswith('start_timer '):
                    names.add(action.strip().split(' ')[1])
            for action in st.get('on_exit', []) or []:
                if isinstance(action, str) and action.strip().startswith('start_timer '):
                    names.add(action.strip().split(' ')[1])
            for tr in st.get('transitions', []) or []:
                if isinstance(tr, dict):
                    for action in tr.get('actions', []) or []:
                        if isinstance(action, str) and action.strip().startswith('start_timer '):
                            names.add(action.strip().split(' ')[1])
            if st.get('after'):
                names.add(f"{prefix}{st.get('name', 'state')}_timeout")
            walk(st.get('states'), prefix)

    walk((bf or {}).get('states'))
    for region in (bf or {}).get('regions', []):
        walk(region.get('states'), region.get('name', '') + '_')
    return {f"TIMER_EXPIRED_{n}" for n in names}


def _collect_event_producers(hw: dict, bf: dict) -> set[str]:
    """All event names that have a producer in this project."""
    produced = set()

    # 1) behavior.events typed contracts (components call event_post_<name>)
    for evt in (bf or {}).get('events', []) or []:
        if isinstance(evt, dict) and evt.get('name'):
            produced.add(str(evt['name']).strip())

    # 2) RTC periodic events
    for pe in hw.get('periodic_events', []) or []:
        if isinstance(pe, dict) and pe.get('event'):
            produced.add(str(pe['event']).strip())

    # 3) RTC peripheral alarms
    for p in hw.get('peripherals', []) or []:
        if isinstance(p, dict) and p.get('type') == 'Internal_RTC':
            for alarm in (p.get('extra', {}) or {}).get('alarms', []) or []:
                if isinstance(alarm, dict) and alarm.get('event'):
                    produced.add(str(alarm['event']).strip())

    # 4) Button gesture events — produced by the btn component from EXTI
    has_btn = any(
        isinstance(pin, dict)
        and ('BUTTON' in (pin.get('label') or '').upper()
             or 'BTN' in (pin.get('label') or '').upper())
        and (pin.get('exti') or {}).get('enable')
        for pin in hw.get('pins', [])
    )
    if has_btn:
        produced.update({
            'BUTTON_PRESS', 'BUTTON_RELEASE',
            'BUTTON_SHORT_PRESS', 'BUTTON_DOUBLE_PRESS', 'BUTTON_LONG_PRESS',
        })

    # 5) EXTI bind events (e.g. EXTI13)
    for pin in hw.get('pins', []) or []:
        if isinstance(pin, dict) and pin.get('bind_event'):
            produced.add(str(pin['bind_event']).strip())

    # 6) Published events (publish / publish_async / send_to)
    produced.update(_collect_published_event_names(bf))

    # 7) Timer expiry events
    produced.update(_collect_timer_event_names(bf))

    # 8) Platform built-ins
    has_rtc = any(
        isinstance(p, dict) and p.get('type') == 'Internal_RTC'
        for p in hw.get('peripherals', [])
    )
    if has_rtc:
        produced.update({'RTC_TICK', 'RTC_ALARM', 'MINUTE_TICK', 'HOUR_TICK'})
    if any(
        isinstance(p, dict) and p.get('type') in ('I2C_Sensor_MPU6050', 'SPI_Sensor_MPU6500')
        for p in hw.get('peripherals', [])
    ):
        produced.add('MPU6050_ALERT')
    produced.add('RETURN')

    return produced


def _validate_event_producer_closure(hw: dict, bf: dict, errors: list[str]) -> None:
    """P3.1: every transition event must have a producer; orphan typed
    events / published events are reported as hints."""
    consumed = _collect_transition_event_names(bf)
    produced = _collect_event_producers(hw, bf)

    if consumed:
        missing = sorted(consumed - produced)
        for evt in missing:
            errors.append(
                f"[WARNING] Transition event '{evt}' has no producer: no "
                f"behavior.events contract, periodic RTC event, EXTI/button "
                f"source, publish action or timer declares it. Add an event "
                f"contract under 'events:' in task.yaml (or a producer action)."
            )

    # Hints only for typed contracts and published events (built-in events
    # such as RTC_TICK / RETURN are consumed internally by the platform).
    orphanable = (
        {str(e.get('name', '')).strip() for e in (bf or {}).get('events', []) if isinstance(e, dict)}
        | _collect_published_event_names(bf)
    )
    for evt in sorted(produced & orphanable - consumed):
        errors.append(
            f"[INFO] Event '{evt}' is produced but never consumed by a "
            f"state machine transition. Add a transition with "
            f"'event: {evt}' or remove the producer."
        )


def _validate_topic_contracts(hw: dict, errors: list[str]) -> None:
    """P3.2: pubsub topic value declarations and component publish contracts."""
    topics = hw.get('topics', []) or []
    if not topics:
        return

    declared: dict[str, dict] = {}
    topic_names = set()
    for i, topic in enumerate(topics):
        if not isinstance(topic, dict):
            continue
        name = topic.get('name', f'#{i}')
        topic_names.add(name)
        val = topic.get('value')
        if val is None:
            continue
        if not isinstance(val, dict):
            errors.append(
                f"[ERROR] Topic '{name}' value must be a mapping "
                f"{{type, unit, range}}, got {type(val).__name__}."
            )
            continue
        declared[name] = val
        vtype = val.get('type')
        if vtype and vtype not in _VALID_TOPIC_VALUE_TYPES:
            errors.append(
                f"[ERROR] Topic '{name}' value.type '{vtype}' is not "
                f"supported. Valid: {sorted(_VALID_TOPIC_VALUE_TYPES)}."
            )
        if 'unit' in val and not isinstance(val['unit'], str):
            errors.append(f"[ERROR] Topic '{name}' value.unit must be a string.")
        rng = val.get('range')
        if rng is not None and (not isinstance(rng, list) or len(rng) != 2):
            errors.append(f"[ERROR] Topic '{name}' value.range must be [min, max].")

    # Template publish contracts must match the declared topic value type/unit.
    for topic_name, (expected_type, expected_unit) in sorted(_TEMPLATE_PUBLISH_CONTRACTS.items()):
        if topic_name not in topic_names:
            continue  # topic not used in this project
        decl = declared.get(topic_name)
        if decl is None:
            errors.append(
                f"[INFO] Topic '{topic_name}' is published by a generated "
                f"component but has no 'value' declaration in pubsub.yaml. "
                f"Declare value: {{type: {expected_type}, unit: "
                f"\"{expected_unit}\"}} to enable the typed publish API."
            )
            continue
        if decl.get('type') and decl['type'] != expected_type:
            errors.append(
                f"[ERROR] Topic '{topic_name}' declares value.type "
                f"'{decl['type']}', but the generated component publishes "
                f"a {expected_type} payload. Set value.type: {expected_type} "
                f"to match the component contract."
            )
        if decl.get('unit') and decl['unit'] != expected_unit:
            errors.append(
                f"[WARNING] Topic '{topic_name}' declares value.unit "
                f"'{decl['unit']}', but the generated component publishes "
                f"'{expected_unit}'. Align the unit to avoid scaling bugs."
            )


_ERROR_PREFIX_RE = re.compile(r'\[(CRITICAL|ERROR|WARNING|INFO)\]\s*(.*)')


def _parse_error(raw: str) -> ValidationError:
    """
    Parse a raw error string like '[ERROR] message' into a ValidationError dict.
    Defaults to severity 'ERROR' if prefix is missing.
    """
    m = _ERROR_PREFIX_RE.match(raw)
    if m:
        return ValidationError(severity=m.group(1), message=m.group(2))
    return ValidationError(severity="ERROR", message=raw)


def validate_hardware(hw: dict) -> list[ValidationError]:
    """
    Cross-field business logic validation.

    Type/shape/format validation is handled by Pydantic models (models.py).
    This function only validates cross-referencing and business rules that
    Pydantic cannot express alone.
    """
    errors: list[str] = []

    # MCU and pin shape checks are now handled by Pydantic (McuModel, PinModel).
    # Pin duplicates and LED-task consistency are also handled by HardwareModel.

    if 'pins' not in hw or not hw['pins']:
        errors.append("[WARNING] No pins defined in hardware YAML.")
    else:
        for i, pin in enumerate(hw['pins']):
            if 'function' not in pin or not pin['function']:
                errors.append(f"[ERROR] Pin #{i} ('{pin.get('id', 'unknown')}') has no 'function' field.")

            valid_functions = ['GPIO_Output', 'GPIO_Input', 'I2C_SCL', 'I2C_SDA', 'SPI_SCK', 'SPI_MISO', 'SPI_MOSI', 'SPI_NSS', 'UART_TX', 'UART_RX', 'USART_TX', 'USART_RX', 'LPUART_TX', 'LPUART_RX', 'RS485_DE', 'ADC_IN', 'IR_OUT', 'IR_IN', 'CELL_PWR', 'CELL_RST']
            valid_function_patterns = [
                r'^I2C\d+_SCL$', r'^I2C\d+_SDA$',
                r'^TIM\d+_CH[1-4]N?$',
                r'^SPI\d+_SCK$', r'^SPI\d+_MISO$', r'^SPI\d+_MOSI$', r'^SPI\d+_NSS$',
                r'^USART\d+_TX$', r'^USART\d+_RX$', r'^UART\d+_TX$', r'^UART\d+_RX$',
                r'^ADC_IN\d+$',
            ]
            if pin.get('function') and pin['function'] not in valid_functions:
                if not any(re.match(p, pin['function']) for p in valid_function_patterns):
                    errors.append(f"[ERROR] Pin #{i} ('{pin.get('id', 'unknown')}') has invalid function '{pin['function']}'. Valid options: {valid_functions} or numbered variants like I2C1_SCL, SPI1_SCK, USART2_TX, ADC_IN1.")

            # EXTI trigger check: Pydantic validates trigger enum values, but
            # the cross-field rule (enabled implies trigger required) remains.
            if pin.get('exti') and pin['exti'].get('enable'):
                if not pin.get('exti', {}).get('trigger'):
                    errors.append(f"[ERROR] Pin #{i} ('{pin.get('id', 'unknown')}') has EXTI enabled but no trigger specified.")

    # Task name/priority/stack_size shape is now handled by Pydantic (TaskModel).

    if 'peripherals' in hw:
        for i, p in enumerate(hw['peripherals']):
            # Peripheral name/type shape is handled by Pydantic (PeripheralModel).
            # Type enum validation is handled by Pydantic.

            if p.get('type') in ['I2C_Sensor_MPU6050', 'I2C_EEPROM', 'I2C_Pressure', 'I2C_TempSensor'] and 'bus' not in p:
                errors.append(f"[ERROR] I2C peripheral '{p.get('name', 'unknown')}' is missing 'bus' field (e.g., 'I2C1').")

            if p.get('type') in ['SPI_Flash_W25Q32', 'SPI_Flash_Generic', 'SPI_Sensor_MPU6500'] and 'bus' not in p:
                errors.append(f"[ERROR] SPI peripheral '{p.get('name', 'unknown')}' is missing 'bus' field (e.g., 'SPI1').")
            if p.get('type') == 'SPI_Sensor_MPU6500' and 'cs_pin' not in p:
                errors.append(f"[ERROR] SPI sensor '{p.get('name', 'unknown')}' is missing 'cs_pin' field (e.g., 'PC4').")
            if p.get('type') == 'NTC_TempSensor' and not (p.get('extra', {}) or {}).get('adc'):
                errors.append(f"[ERROR] NTC sensor '{p.get('name', 'unknown')}' is missing required extra.adc field (name of the Internal_ADC peripheral).")

            if p.get('type') in ['Protocol_MQTT']:
                extra = p.get('extra', {})
                bearer_val = p.get('bearer', extra.get('bearer'))
                broker_val = p.get('broker', extra.get('broker'))
                if not bearer_val:
                    errors.append(f"[ERROR] Protocol_MQTT peripheral '{p.get('name', 'unknown')}' is missing required 'bearer' field.")
                else:
                    bearer_found = any(
                        pp.get('name') == bearer_val and pp.get('type') == 'Cellular_4G'
                        for pp in hw.get('peripherals', [])
                    )
                    if not bearer_found:
                        errors.append(f"[ERROR] Protocol_MQTT peripheral '{p.get('name', 'unknown')}' bearer '{bearer_val}' refers to a non-existent Cellular_4G peripheral. Available Cellular_4G: {[pp.get('name') for pp in hw.get('peripherals', []) if pp.get('type') == 'Cellular_4G']}")
                if not broker_val:
                    errors.append(f"[ERROR] Protocol_MQTT peripheral '{p.get('name', 'unknown')}' is missing required 'broker' field.")

            if p.get('type') in ['Protocol_Modbus']:
                extra = p.get('extra', {})
                bearer_val = p.get('bearer', extra.get('bearer'))
                if not bearer_val:
                    errors.append(f"[ERROR] Protocol_Modbus peripheral '{p.get('name', 'unknown')}' is missing required 'bearer' field.")
                else:
                    bearer_found = any(
                        pp.get('name') == bearer_val and pp.get('type') in ['RS485', 'UART_Serial']
                        for pp in hw.get('peripherals', [])
                    )
                    if not bearer_found:
                        errors.append(f"[ERROR] Protocol_Modbus peripheral '{p.get('name', 'unknown')}' bearer '{bearer_val}' refers to a non-existent RS485 or UART peripheral. Available: {[pp.get('name') for pp in hw.get('peripherals', []) if pp.get('type') in ['RS485', 'UART_Serial']]}")

            model_path = os.path.join(MODELS_DIR, p['type'] + '.yaml')
            if not os.path.exists(model_path):
                errors.append(f"[WARNING] Model file '{model_path}' for peripheral type '{p['type']}' not found. Some features may not work.")
            else:
                # Validate extra fields against model's extra_schema
                _validate_extra_fields(p, model_path, errors)

    # Sleep mode enum is handled by Pydantic (SleepModel).
    # Bootloader size_kb, max_retries, and offset constraints are handled
    # by Pydantic (BootloaderModel).

    if 'behavior' in hw and hw['behavior']:
        bf = hw['behavior']

        # Collect all declared variables for expression validation
        _all_vars = _collect_all_variables(hw)

        # ---------- Optional events declaration ----------
        if 'events' in bf:
            valid_event_sources = {'exti', 'rtc', 'timer', 'custom'}
            valid_event_types = {'synchronous', 'asynchronous'}
            for i, evt in enumerate(bf['events']):
                if 'name' not in evt or not evt['name']:
                    errors.append(f"[ERROR] Event #{i} in behavior.events has no 'name' field.")
                if evt.get('source') and evt['source'] not in valid_event_sources:
                    errors.append(f"[WARNING] Event '{evt.get('name', 'unknown')}' has unknown source '{evt['source']}'. Valid: {sorted(valid_event_sources)}.")
                if evt.get('type') and evt['type'] not in valid_event_types:
                    errors.append(f"[WARNING] Event '{evt.get('name', 'unknown')}' has unknown type '{evt['type']}'. Valid: {sorted(valid_event_types)}.")

        if not ('states' in bf or 'regions' in bf):
            errors.append("[ERROR] behavior has neither 'states' nor 'regions' defined.")

        valid_actions = [
            'toggle_led', 'return', 'EVENT_NONE',
            'shell_temp', 'telemetry_snapshot', 'power_status',
        ]
        valid_action_prefixes = [
            'start_timer ', 'stop_timer ', 'set ', 'calc ',
            'publish ', 'publish_async ', 'when ', 'defer ',
            'timeline:', 'send_to ',
            'led_pattern ', 'log ',
        ]
        valid_led_patterns = ['off', 'fast_blink', 'slow_blink', 'fault']
        valid_topics = [t.get('name', '') for t in hw.get('topics', [])]
        state_names = []

        def validate_actions(action_list, location):
            for idx, action in enumerate(action_list):
                # Support new dict-format actions: {toggle_led: null}, {defer: {after: 3000, do: ...}}, etc.
                if isinstance(action, dict):
                    action_keys = list(action.keys())
                    if len(action_keys) != 1:
                        errors.append(f"[ERROR] Dict-format action #{idx} in {location} must have exactly one key. Got: {action_keys}")
                        continue
                    action_name = action_keys[0]
                    if action_name in valid_actions:
                        continue  # Simple actions like toggle_led
                    elif action_name == 'timeline':
                        # timeline: [{ms: N, do: ACTION}, ...]
                        continue
                    elif action_name == 'led_pattern':
                        pattern = (action[action_name] or {})
                        if isinstance(pattern, dict):
                            pattern = pattern.get('pattern', '')
                        if pattern not in valid_led_patterns:
                            errors.append(
                                f"[ERROR] Action #{idx} in {location}: unknown led_pattern "
                                f"'{pattern}'. Valid: {valid_led_patterns}."
                            )
                        continue
                    elif action_name in ('defer', 'start_timer', 'stop_timer', 'set', 'calc',
                                         'publish', 'publish_async', 'when', 'send_to', 'log'):
                        continue
                    else:
                        errors.append(f"[ERROR] Unknown dict-format action '{action_name}' in {location}.")
                    continue

                # String-format action (legacy)
                if not isinstance(action, str):
                    errors.append(f"[ERROR] Action #{idx} in {location} must be a string or dict, got {type(action).__name__}.")
                    continue

                is_valid = False
                if action in valid_actions:
                    is_valid = True
                elif action.startswith('led_pattern '):
                    is_valid = True
                    pattern = action[len('led_pattern '):].strip()
                    if pattern not in valid_led_patterns:
                        errors.append(
                            f"[ERROR] Action '{action}' in {location}: unknown led_pattern "
                            f"'{pattern}'. Valid: {valid_led_patterns}."
                        )
                elif action.startswith('publish ') or action.startswith('publish_async '):
                    is_valid = True
                    topic = action.split(' ', 1)[1].strip()
                    if valid_topics and topic not in valid_topics:
                        errors.append(
                            f"[ERROR] Action '{action}' in {location}: unknown topic "
                            f"'{topic}'. Declared topics: {valid_topics}."
                        )
                else:
                    for prefix in valid_action_prefixes:
                        if action.startswith(prefix):
                            is_valid = True
                            break
                if not is_valid:
                    errors.append(f"[ERROR] Unknown action '{action}' in {location}.")

        def validate_expressions_in_actions(action_list, location):
            """Validate guard/calc/when expressions within action strings or dicts."""
            for action in action_list:
                # Dict-format action: extract the action type and params
                if isinstance(action, dict):
                    action_key = list(action.keys())[0]
                    params = action[action_key] or {}
                    if action_key == 'when':
                        cond = params.get('cond', '')
                        if cond:
                            errors.extend(_validate_when(f"{cond} => _", _all_vars, location))
                    elif action_key == 'calc':
                        params_dict = params if isinstance(params, dict) else {}
                        expr = params_dict.get('expr', '')
                        dest = params_dict.get('var', '')
                        if expr and dest:
                            errors.extend(_validate_calc(f"{dest} = {expr}", _all_vars, location))
                    continue

                # String-format action
                if not isinstance(action, str):
                    continue
                if action.startswith('calc '):
                    calc_expr = action[5:].strip()
                    errors.extend(_validate_calc(calc_expr, _all_vars, location))
                elif action.startswith('when '):
                    when_expr = action[5:].strip()
                    errors.extend(_validate_when(when_expr, _all_vars, location))
                elif action.startswith('defer '):
                    # Check if defer's sub-action is a when/calc
                    if '=>' in action:
                        sub = action.split('=>', 1)[1].strip()
                        if sub.startswith('when '):
                            errors.extend(_validate_when(sub[5:].strip(), _all_vars, f"{location} (defer sub-action)"))
                        elif sub.startswith('calc '):
                            errors.extend(_validate_calc(sub[5:].strip(), _all_vars, f"{location} (defer sub-action)"))
                elif action.startswith('timeline:'):
                    content = action.split(':', 1)[1].strip() if ':' in action else action
                    for part in content.split(','):
                        if '=>' in part:
                            sub = part.split('=>', 1)[1].strip()
                            if sub.startswith('when '):
                                errors.extend(_validate_when(sub[5:].strip(), _all_vars, f"{location} (timeline sub-action)"))
                            elif sub.startswith('calc '):
                                errors.extend(_validate_calc(sub[5:].strip(), _all_vars, f"{location} (timeline sub-action)"))

        if 'states' in bf and bf['states']:
            for i, state in enumerate(bf['states']):
                if 'name' not in state or not state['name']:
                    errors.append(f"[ERROR] State #{i} in behavior has no 'name' field.")
                else:
                    state_names.append(state['name'])

                if state.get('states') and not state.get('initial_state'):
                    errors.append(f"[ERROR] Compound state '{state.get('name', 'unknown')}' has sub-states but no initial_state.")

                if state.get('on_entry'):
                    validate_actions(state['on_entry'], f"on_entry of state '{state.get('name', 'unknown')}'")
                    validate_expressions_in_actions(state['on_entry'], f"on_entry of state '{state.get('name', 'unknown')}'")
                if state.get('on_exit'):
                    validate_actions(state['on_exit'], f"on_exit of state '{state.get('name', 'unknown')}'")
                    validate_expressions_in_actions(state['on_exit'], f"on_exit of state '{state.get('name', 'unknown')}'")

                for j, trans in enumerate(state.get('transitions', [])):
                    if 'event' not in trans:
                        errors.append(f"[ERROR] Transition #{j} in state '{state.get('name', 'unknown')}' has no 'event' field.")
                    if 'target' not in trans:
                        errors.append(f"[ERROR] Transition #{j} in state '{state.get('name', 'unknown')}' has no 'target' field.")
                    if trans.get('actions'):
                        validate_actions(trans['actions'], f"transition #{j} of state '{state.get('name', 'unknown')}'")
                        validate_expressions_in_actions(trans['actions'], f"transition #{j} of state '{state.get('name', 'unknown')}'")
                    if trans.get('guard'):
                        errors.extend(_validate_guard(trans['guard'], _all_vars, f"guard of transition #{j} in state '{state.get('name', 'unknown')}'"))

                if state.get('type') == 'ref':
                    ref_file = state.get('ref')
                    if not ref_file:
                        errors.append(f"[ERROR] State '{state.get('name', 'unknown')}' is a 'ref' type but has no 'ref' field.")
                    else:
                        ref_path = os.path.join(EXAMPLES_DIR, ref_file)
                        if not os.path.exists(ref_path) and not os.path.exists(ref_file):
                            errors.append(f"[ERROR] Ref file '{ref_file}' not found for state '{state.get('name', 'unknown')}'. Searched in: '{ref_path}' and '{ref_file}'.")
                    if not state.get('namespace'):
                        errors.append(f"[WARNING] State '{state.get('name', 'unknown')}' is a 'ref' type but has no 'namespace'. Variable/state name conflicts may occur.")

                if state.get('states'):
                    for k, substate in enumerate(state['states']):
                        substate_full_name = f"{state['name']}.{substate.get('name', 'unknown')}"
                        state_names.append(substate_full_name)
                        if 'name' not in substate or not substate['name']:
                            errors.append(f"[ERROR] Substate #{k} in state '{state.get('name', 'unknown')}' has no 'name' field.")
                        if substate.get('on_entry'):
                            validate_actions(substate['on_entry'], f"on_entry of substate '{substate_full_name}'")
                            validate_expressions_in_actions(substate['on_entry'], f"on_entry of substate '{substate_full_name}'")
                        if substate.get('on_exit'):
                            validate_actions(substate['on_exit'], f"on_exit of substate '{substate_full_name}'")
                            validate_expressions_in_actions(substate['on_exit'], f"on_exit of substate '{substate_full_name}'")
                        for l, subtrans in enumerate(substate.get('transitions', [])):
                            if 'event' not in subtrans:
                                errors.append(f"[ERROR] Transition #{l} in substate '{substate_full_name}' has no 'event' field.")
                            if subtrans.get('actions'):
                                validate_actions(subtrans['actions'], f"transition #{l} of substate '{substate_full_name}'")
                                validate_expressions_in_actions(subtrans['actions'], f"transition #{l} of substate '{substate_full_name}'")

        if 'regions' in bf and bf['regions']:
            for i, region in enumerate(bf['regions']):
                if 'name' not in region or not region['name']:
                    errors.append(f"[ERROR] Region #{i} has no 'name' field.")
                if 'initial_state' not in region:
                    errors.append(f"[ERROR] Region '{region.get('name', 'unknown')}' has no 'initial_state' field.")
                if region.get('states'):
                    for j, state in enumerate(region['states']):
                        state_full_name = f"{region.get('name', 'unknown')}.{state.get('name', 'unknown')}"
                        state_names.append(state_full_name)
                        if 'name' not in state or not state['name']:
                            errors.append(f"[ERROR] State #{j} in region '{region.get('name', 'unknown')}' has no 'name' field.")
                        if state.get('on_entry'):
                            validate_actions(state['on_entry'], f"on_entry of state '{state_full_name}'")
                            validate_expressions_in_actions(state['on_entry'], f"on_entry of state '{state_full_name}'")
                        if state.get('on_exit'):
                            validate_actions(state['on_exit'], f"on_exit of state '{state_full_name}'")
                            validate_expressions_in_actions(state['on_exit'], f"on_exit of state '{state_full_name}'")
                        for k, trans in enumerate(state.get('transitions', [])):
                            if 'event' not in trans:
                                errors.append(f"[ERROR] Transition #{k} in state '{state_full_name}' has no 'event' field.")
                            if 'target' not in trans:
                                errors.append(f"[ERROR] Transition #{k} in state '{state_full_name}' has no 'target' field.")
                            if trans.get('actions'):
                                validate_actions(trans['actions'], f"transition #{k} of state '{state_full_name}'")
                                validate_expressions_in_actions(trans['actions'], f"transition #{k} of state '{state_full_name}'")

        # Validate custom type definitions
        errors += _validate_custom_types(bf)

        if 'variables' in bf:
            custom_type_names = _collect_custom_types(hw)
            for i, var in enumerate(bf['variables']):
                if 'name' not in var or not var['name']:
                    errors.append(f"[ERROR] Variable #{i} in behavior has no 'name' field.")
                if 'type' not in var or not var['type']:
                    errors.append(f"[ERROR] Variable '{var.get('name', 'unknown')}' has no 'type' field.")
                else:
                    if var['type'] not in _VALID_C_TYPES and var['type'] not in custom_type_names:
                        errors.append(f"[WARNING] Variable '{var.get('name', 'unknown')}' has type '{var['type']}' which is not in the recommended list: {sorted(_VALID_C_TYPES)} and not a custom type.")

        # ---------- P3: cross-component contract validation ----------
        _validate_event_producer_closure(hw, bf, errors)

    # ---------- P3: pubsub topic value contracts ----------
    _validate_topic_contracts(hw, errors)

    return [_parse_error(e) for e in errors]


# =========================================================================
# Cross-layer validation: bind.yaml → hardware.yaml / task.yaml
# =========================================================================

def validate_bind_cross_refs(
    hw_raw: dict,
    task_raw: dict | None,
    bind_raw: dict | None,
    components_raw: dict | None = None,
) -> list[ValidationError]:
    """Validate that bind.yaml references are consistent with hardware.yaml
    and task.yaml.

    Checks:
      - bind.interrupt[].pin exists in hw.pins
      - bind.interrupt[].task exists in task.app_tasks
      - bind.interrupt[].event matches a pin with EXTI enabled
      - bind.interrupt[].event is consumed by a state machine transition
        (or is a platform event) and EXTI<num> matches the pin number
      - bind.interrupt[].component exists in components.yaml
      - bind.peripheral_assign[].peripheral exists in hw.peripherals
      - bind.peripheral_assign[].task exists in task.app_tasks
      - bind.routing[].from / .to tasks exist in task.app_tasks
      - bind.routing has required 'signal' field

    Args:
        hw_raw:  Parsed hardware.yaml dict.
        task_raw: Parsed task.yaml dict (may be None).
        bind_raw: Parsed bind.yaml dict (may be None).
        components_raw: Parsed components.yaml dict (may be None).

    Returns:
        List of ValidationError objects.
    """
    errors: list[str] = []

    if not bind_raw or not isinstance(bind_raw, dict):
        return []

    # ---------- Collect validated name sets ----------
    pin_ids: set[str] = set()
    for p in hw_raw.get("pins", []):
        if isinstance(p, dict) and p.get("id"):
            pin_ids.add(p["id"].upper())

    task_names: set[str] = set()
    if task_raw and isinstance(task_raw, dict):
        # System event task name is also a valid task target
        et = task_raw.get("event_task", {})
        if isinstance(et, dict) and et.get("name"):
            task_names.add(et["name"])
        for t in task_raw.get("app_tasks", []):
            if isinstance(t, dict) and t.get("name"):
                task_names.add(t["name"])

    peri_names: set[str] = set()
    for p in hw_raw.get("peripherals", []):
        if isinstance(p, dict) and p.get("name"):
            peri_names.add(p["name"])

    component_names: set[str] = set()
    if components_raw and isinstance(components_raw, dict):
        for c in components_raw.get("components", []):
            if isinstance(c, dict) and c.get("name"):
                component_names.add(c["name"])

    # ---------- Collect behavior events for cross-validation ----------
    behavior = {}
    if task_raw and isinstance(task_raw, dict):
        behavior = task_raw.get("behavior", {}) or {}
    elif hw_raw and isinstance(hw_raw, dict):
        behavior = hw_raw.get("behavior", {}) or {}
    consumed_events = _collect_transition_event_names(behavior)
    produced_events = _collect_event_producers(hw_raw, behavior)

    # ---------- Validate interrupt bindings ----------
    for i, binding in enumerate(bind_raw.get("interrupt", [])):
        if not isinstance(binding, dict):
            continue
        pin_id = binding.get("pin", "")
        task_name = binding.get("task", "")
        event_name = binding.get("event", "")
        component_name = binding.get("component", "")
        loc = f"bind.yaml interrupt #{i + 1}"

        if pin_id:
            if pin_id.upper() not in pin_ids:
                errors.append(
                    f"[ERROR] {loc}: pin '{pin_id}' not found in hardware.yaml pins. "
                    f"Available: {sorted(pin_ids)}"
                )
            else:
                # Check EXTI is enabled for this pin
                pin_info = next(
                    (p for p in hw_raw.get("pins", [])
                     if isinstance(p, dict) and p.get("id", "").upper() == pin_id.upper()),
                    None,
                )
                if pin_info:
                    exti = pin_info.get("exti", {})
                    if isinstance(exti, dict) and not exti.get("enable"):
                        errors.append(
                            f"[WARNING] {loc}: pin '{pin_id}' has event "
                            f"'{event_name}' but EXTI is not enabled in hardware.yaml."
                        )
                    if event_name:
                        # EXTI<num> must match the pin number (e.g. PC13 -> EXTI13)
                        m_evt = re.match(r'^EXTI(\d+)$', event_name.strip().upper())
                        m_pin = re.match(r'^P[A-F](\d+)$', pin_id.strip().upper())
                        if m_evt and m_pin and m_evt.group(1) != m_pin.group(1):
                            errors.append(
                                f"[WARNING] {loc}: event '{event_name}' does not "
                                f"match pin '{pin_id}' (expected EXTI{m_pin.group(1)})."
                            )

        if task_name and task_names:
            if task_name not in task_names:
                errors.append(
                    f"[ERROR] {loc}: task '{task_name}' not found in task.yaml "
                    f"app_tasks. Available: {sorted(task_names)}"
                )

        if component_name and component_names and component_name not in component_names:
            errors.append(
                f"[ERROR] {loc}: component '{component_name}' not found in "
                f"components.yaml. Available: {sorted(component_names)}"
            )

        known_exti_events: set[str] = set()
        for pin in hw_raw.get("pins", []):
            if (isinstance(pin, dict) and pin.get("exti")
                    and isinstance(pin["exti"], dict) and pin["exti"].get("enable")
                    and pin.get("id")):
                m = re.match(r'^P[A-F](\d+)$', pin["id"].strip().upper())
                if m:
                    known_exti_events.add(f"EXTI{m.group(1)}")

        if event_name:
            event_upper = event_name.strip().upper()
            if not re.match(r'^(EXTI\d+|EVENT_\w+)$', event_upper):
                errors.append(
                    f"[WARNING] {loc}: event '{event_name}' does not look like "
                    f"an event name (expected 'EXTI<num>' or 'EVENT_<name>')."
                )
            if (event_upper not in consumed_events | produced_events | known_exti_events):
                errors.append(
                    f"[WARNING] {loc}: event '{event_name}' is not consumed by "
                    f"any state machine transition and has no producer. "
                    f"Consumed events: {sorted(consumed_events) or 'none'}."
                )

    # ---------- Validate peripheral_assign ----------
    for i, assign in enumerate(bind_raw.get("peripheral_assign", [])):
        if not isinstance(assign, dict):
            continue
        peri_name = assign.get("peripheral", "")
        task_name = assign.get("task", "")
        loc = f"bind.yaml peripheral_assign #{i + 1}"

        if peri_name and peri_name not in peri_names:
            # Case-insensitive check
            peri_lower = {p.lower(): p for p in peri_names}
            if peri_name.lower() in peri_lower:
                errors.append(
                    f"[WARNING] {loc}: peripheral '{peri_name}' case mismatch. "
                    f"Did you mean '{peri_lower[peri_name.lower()]}'?"
                )
            else:
                errors.append(
                    f"[ERROR] {loc}: peripheral '{peri_name}' not found in "
                    f"hardware.yaml peripherals. Available: {sorted(peri_names)}"
                )

        if task_name and task_names and task_name not in task_names:
            errors.append(
                f"[ERROR] {loc}: task '{task_name}' not found in task.yaml "
                f"app_tasks. Available: {sorted(task_names)}"
            )

    # ---------- Validate routing ----------
    for i, route in enumerate(bind_raw.get("routing", [])):
        if not isinstance(route, dict):
            continue
        from_task = route.get("from", "")
        to_task = route.get("to", "")
        signal = route.get("signal", "")
        loc = f"bind.yaml routing #{i + 1}"

        if not signal:
            errors.append(f"[ERROR] {loc}: missing required 'signal' field.")

        if from_task and task_names and from_task not in task_names:
            errors.append(
                f"[ERROR] {loc}: from_task '{from_task}' not found in "
                f"task.yaml app_tasks. Available: {sorted(task_names)}"
            )

        if to_task and task_names and to_task not in task_names:
            errors.append(
                f"[ERROR] {loc}: to_task '{to_task}' not found in "
                f"task.yaml app_tasks. Available: {sorted(task_names)}"
            )

    return [_parse_error(e) for e in errors]
