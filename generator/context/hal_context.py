"""
hal_context.py
HAL source file management: base HAL sources and dynamic additions based on
which peripherals are enabled.
"""

# Default HAL sources always required for STM32G0B1 projects.
BASE_HAL_SOURCES = [
    'stm32g0xx_hal.c',
    'stm32g0xx_hal_cortex.c',
    'stm32g0xx_hal_gpio.c',
    'stm32g0xx_hal_rcc.c',
    'stm32g0xx_hal_rcc_ex.c',
    'stm32g0xx_hal_pwr.c',
    'stm32g0xx_hal_pwr_ex.c',
    'stm32g0xx_hal_exti.c',
    'stm32g0xx_hal_tim.c',
    'stm32g0xx_hal_tim_ex.c'
]


def compute_hal_sources(flags: dict, hil_mode: bool = False) -> list[str]:
    """
    Build the complete HAL source list from detected peripheral flags.

    Args:
        flags: dict with has_i2c, has_rtc, has_pwm, etc. boolean keys.
        hil_mode: whether HIL test mode is active (always adds UART HAL).

    Returns:
        List of HAL source filenames.
    """
    hal_sources = list(BASE_HAL_SOURCES)

    if flags.get('has_i2c'):
        if 'stm32g0xx_hal_i2c.c' not in hal_sources:
            hal_sources.append('stm32g0xx_hal_i2c.c')
    if flags.get('has_rtc'):
        for rtc_file in ['stm32g0xx_hal_rtc.c', 'stm32g0xx_hal_rtc_ex.c',
                          'stm32g0xx_hal_tim.c', 'stm32g0xx_hal_tim_ex.c']:
            if rtc_file not in hal_sources:
                hal_sources.append(rtc_file)
    if flags.get('has_spi'):
        if 'stm32g0xx_hal_spi.c' not in hal_sources:
            hal_sources.append('stm32g0xx_hal_spi.c')
    if flags.get('has_pwm'):
        if 'stm32g0xx_hal_tim.c' not in hal_sources:
            hal_sources.append('stm32g0xx_hal_tim.c')
            hal_sources.append('stm32g0xx_hal_tim_ex.c')
    if flags.get('has_adc'):
        if 'stm32g0xx_hal_adc.c' not in hal_sources:
            hal_sources.append('stm32g0xx_hal_adc.c')
        if 'stm32g0xx_hal_adc_ex.c' not in hal_sources:
            hal_sources.append('stm32g0xx_hal_adc_ex.c')
    if flags.get('has_uart') or hil_mode:
        if 'stm32g0xx_hal_uart.c' not in hal_sources:
            hal_sources.append('stm32g0xx_hal_uart.c')
    if flags.get('has_bootloader') or flags.get('has_iwdg'):
        if 'stm32g0xx_hal_iwdg.c' not in hal_sources:
            hal_sources.append('stm32g0xx_hal_iwdg.c')

    return hal_sources
