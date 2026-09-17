# Architecture Overview

## Project Structure

```
hw2c
├── models/          # Peripheral model YAML definitions
├── templates/       # Jinja2 templates (C source, tests, config)
├── generator/       # Python code generation engine
│   ├── generate.py          # Main generator entry point
│   ├── validator.py         # YAML validation
│   ├── context_builder.py   # Template rendering context
│   ├── models.py            # Pydantic type models
│   ├── delta_tool.py        # H2CD delta build / repack / apply
│   ├── fota_sender.py       # FOTA patch delivery
│   ├── patch_crc.py         # CRC metadata post-processing
│   ├── allocators/          # Pin assignment engine
│   ├── backends/            # MCU-specific backends (stm32)
│   ├── builders/            # Peripheral driver builders
│   ├── context/             # Rendering context sub-modules
│   ├── merger/              # C code AST-based merger
│   ├── schemas/             # Pydantic validation models
│   ├── validators/          # Pin conflict and rule validators
│   └── tests/               # Generator unit tests
├── examples/        # Example hardware.yaml projects (21)
├── static/          # Static vendor files (Git submodules)
│   ├── stm32g0/     # HAL, CMSIS, FreeRTOS
│   └── unity/       # Unity test framework
├── parser/          # Netlist/BOM parsers
└── docs/            # Documentation (MkDocs Material)
```

## Generation Pipeline

```
hardware.yaml
    │
    ▼
┌─────────────────────┐
│ 1. YAML Parsing     │  → PyYAML + Pydantic models
├─────────────────────┤
│ 2. Validation       │  → validator.py + schemas/
├─────────────────────┤
│ 3. Context Building │  → context_builder.py + context/
│   - Pin extraction  │
│   - Peripheral map  │
│   - Driver list     │
│   - Condition flags │
├─────────────────────┤
│ 4. Pin Allocation   │  → allocators/pin_allocator.py
├─────────────────────┤
│ 5. Template Render  │  → Jinja2 engine, all .j2 → .c/.h
├─────────────────────┤
│ 6. CRC Post-process │  → patch_crc.py (Bootloader)
└─────────────────────┘
    │
    ▼
output/<project>/
```

## Key Design Principles

### 1. Model-Driven Generation

All peripheral knowledge lives in `models/*.yaml` files. Each model defines:

- Driver templates to render
- Required pins/peripherals
- Extra configuration schema
- Default parameters

### 2. Jinja2 Template System

Templates use condition flags (e.g., `has_rtc`, `has_bootloader`) to generate only what's needed. The template context is a simple dictionary built by `context_builder.py`.

### 3. Test-Driven from Day One

Every generated project includes:
- **Mock HAL** — `test/mock_hal.c` simulates all STM32 peripherals on PC
- **Unit tests** — Unity-based, compiled with `gcc -DTEST`
- **HIL tests** — Optional hardware-in-loop via UART

### 4. Layered C Code Architecture

Generated projects follow a consistent layered structure:

```
src/
├── main.c              # Application entry + task creation
├── gpio.c              # GPIO + EXTI (HAL)
├── sleep.c             # Low-power idle hook
├── stm32g0xx_it.c      # Interrupt handlers
├── event_mgr.c/h       # Event queue + dispatcher
├── statemachine.c/h    # Business flow state machine
├── boot_app.c/h        # App-side bootloader integration
└── drivers/            # Peripheral drivers
    ├── drv_rtc.c/h
    ├── drv_log.c/h
    ├── drv_i2c_mpu6050.c/h
    └── ...
```

## Extension Points

### Adding a New Peripheral

1. Create `models/New_Peripheral.yaml`
2. Create `templates/drivers/drv_new_peripheral.c.j2` and `.h.j2`
3. Create `templates/test/test_new_peripheral.c.j2`
4. Register in `generator/validator.py`
5. Add mock support in `templates/test/mock_hal.c.j2`

### Adding a New MCU

1. Create `generator/data/mcu/<part>.json` with pin/peripheral data
2. Implement `generator/backends/<vendor>/backend.py`
3. Register in `generator/registry.py`
