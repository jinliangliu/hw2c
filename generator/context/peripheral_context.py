"""
peripheral_context.py
Peripheral detection logic: loads models, detects hardware flags, and builds
the driver list from the peripheral configuration.
"""
import os
import yaml
from ..paths import MODELS_DIR

def detect_peripherals(peripherals: list, load_model_func) -> dict:
    """Process peripheral list, load models, detect capability flags.

    Args:
        peripherals: list of peripheral config dicts from hardware YAML.
        load_model_func: callable that loads a model YAML by type string.

    Returns:
        dict with drivers list, boolean flags, and name strings.
    """
    drivers = []
    has_i2c = has_rtc = has_mpu6050 = has_pwm = has_spi = False
    has_spi_flash = has_adc = has_uart = has_rs485 = False
    has_ir = has_cellular = has_cli = False
    has_temp_sensor = False
    uart_name = rs485_name = cli_uart_name = cli_name = ""

    for p in peripherals:
        model = load_model_func(p['type'])
        p['model'] = model

        drivers.append({
            'name': p['name'],
            'template': model.get('driver_template', ''),
            'header_template': model.get('header_template', ''),
            'model': model,
            'peripheral': p
        })

        iface = model.get('interface', '').upper()
        if 'I2C' in iface:
            has_i2c = True
        if p['type'] in ('I2C_Sensor_MPU6050', 'SPI_Sensor_MPU6500'):
            has_mpu6050 = True
        if model.get('type') == 'Internal_RTC':
            has_rtc = True
        if 'SPI' in iface:
            has_spi = True
            if p['type'] in ('SPI_Flash_W25Q32', 'SPI_Flash_Generic'):
                has_spi_flash = True
        if model.get('type') == 'Internal_PWM':
            has_pwm = True
        if model.get('type') == 'Internal_ADC':
            has_adc = True
        if model.get('type') == 'NTC_TempSensor':
            has_adc = True
        if model.get('type') == 'UART_Serial':
            has_uart = True
            uart_name = p['name']
        if model.get('type') == 'Internal_IR':
            has_ir = True
        if model.get('type') == 'RS485':
            has_rs485 = True
            has_uart = True
            rs485_name = p['name']
            uart_ref = p.get('uart', '')
            if uart_ref:
                uart_name = uart_ref
        if model.get('type') == 'Cellular_4G':
            has_cellular = True
            has_uart = True
            uart_ref = p.get('uart', p.get('extra', {}).get('uart', ''))
            if uart_ref and not uart_name:
                uart_name = uart_ref
        if model.get('type') == 'Internal_CLI':
            has_cli = True
            has_uart = True
            cli_name = p['name']
            uart_ref = p.get('uart', '')
            if uart_ref:
                cli_uart_name = uart_ref
                if not uart_name:
                    uart_name = uart_ref
        if model.get('type') == 'Internal_TempSensor':
            has_temp_sensor = True
            has_adc = True

    return {
        "drivers": drivers,
        "has_i2c": has_i2c,
        "has_rtc": has_rtc,
        "has_mpu6050": has_mpu6050,
        "has_pwm": has_pwm,
        "has_spi": has_spi,
        "has_spi_flash": has_spi_flash,
        "has_adc": has_adc,
        "has_uart": has_uart,
        "has_rs485": has_rs485,
        "has_ir": has_ir,
        "has_cellular": has_cellular,
        "has_cli": has_cli,
        "has_temp_sensor": has_temp_sensor,
        "uart_name": uart_name,
        "rs485_name": rs485_name,
        "cli_uart_name": cli_uart_name,
        "cli_name": cli_name,
    }
