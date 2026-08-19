"""
Pin allocator module.

Automatically assigns MCU pins to peripheral functions using the
MCU pin database, avoiding conflicts with already-allocated pins
and respecting hardware constraints (SWD pins, same-port preference).
"""

import logging
import re
from typing import Any, Dict, List, Optional, Set, Tuple

from ..mcu_database import MCUDatabase

logger = logging.getLogger("hw2c.pin_allocator")

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Pins reserved for SWD/JTAG debug interface — never auto-allocate
_SWD_PINS: Set[str] = {"PA13", "PA14"}

# Mapping from peripheral type to its required signal list.
# Used by allocate_all() to know what signals a peripheral needs.
_PERIPHERAL_SIGNALS: Dict[str, List[str]] = {
    "UART_Serial":       ["TX", "RX"],
    "RS485":             ["TX", "RX", "DE"],
      "I2C_Sensor_MPU6050": ["SCL", "SDA"],
      "I2C_EEPROM":        ["SCL", "SDA"],
      "I2C_Pressure":      ["SCL", "SDA"],
"SPI_Flash_W25Q32":  ["SCK", "MISO", "MOSI", "NSS"],
"SPI_Flash_Generic": ["SCK", "MISO", "MOSI", "NSS"],
"SPI_Sensor_MPU6500": ["SCK", "MISO", "MOSI", "NSS"],
"Internal_PWM":      ["CH1"],
"FOC_Motor":         [],   # TIM1/ADC/encoder pins are declared manually
"Internal_ADC":      ["IN0"],
    "Cellular_4G":       ["TX", "RX", "PWR", "RST"],
    "Internal_IR":       ["OUT", "IN"],
}

# Pin priority hints: prefer these (peripheral, signal) -> pin mappings
# when multiple candidates are available. Used as tiebreaker in _select_best.
_PRIORITY_HINTS: Dict[Tuple[str, str], str] = {
    ("USART2", "TX"): "PA2",
    ("USART2", "RX"): "PA3",
    ("USART1", "TX"): "PA9",
    ("USART1", "RX"): "PA10",
    ("I2C1", "SCL"):  "PB6",
    ("I2C1", "SDA"):  "PB7",
}


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------

class AllocationError(Exception):
    """Raised when no suitable pin can be found for a peripheral function."""

    def __init__(self, peripheral: str, signal: str,
                 reason: str = "") -> None:
        self.peripheral = peripheral
        self.signal = signal
        self.reason = reason
        msg = f"No available pin for {peripheral}_{signal}"
        if reason:
            msg += f": {reason}"
        super().__init__(msg)


# ---------------------------------------------------------------------------
# PinAllocator
# ---------------------------------------------------------------------------

class PinAllocator:
    """Allocates MCU pins to peripheral functions based on the MCU database.

    Tracks allocated pins to prevent conflicts between peripherals.
    Prefers pins on the same GPIO port for bus peripherals (I2C, SPI)
    and avoids debug/programming pins (PA13/PA14).

    Usage:
        db = MCUDatabase.from_mcu_name("STM32G0B1RET6")
        alloc = PinAllocator(db)

        # Reserve YAML-defined pins
        alloc.pre_allocate("PA2", "USART2", "TX")

        # Auto-allocate a single pin
        pin = alloc.allocate("I2C1", "SCL")   # -> "PB6"

        # Atomic bus allocation (rolls back on failure)
        pins = alloc.allocate_bus("SPI1", ["SCK", "MISO", "MOSI", "NSS"])
        # -> {"SCK": "PA5", "MISO": "PA6", "MOSI": "PA7", "NSS": "PA4"}
    """

    def __init__(self, mcu_db: MCUDatabase) -> None:
        """Initialize with an MCUDatabase instance.

        Args:
            mcu_db: MCUDatabase providing pin/peripheral lookup.
        """
        self.mcu_db: MCUDatabase = mcu_db
        self._allocated: Dict[str, Tuple[str, str, Optional[int]]] = {}
        # pin_name -> (peripheral, signal, af)

    # ------------------------------------------------------------------
    # Public API — query
    # ------------------------------------------------------------------

    @property
    def allocated_pins(self) -> Dict[str, Tuple[str, str, Optional[int]]]:
        """Return current allocations: pin -> (peripheral, signal, af)."""
        return dict(self._allocated)

    def is_allocated(self, pin: str) -> bool:
        """Check if a pin has already been allocated."""
        return pin.upper() in self._allocated

    # ------------------------------------------------------------------
    # Public API — single-pin allocation
    # ------------------------------------------------------------------

    def pre_allocate(self, pin: str, peripheral: str, signal: str,
                     af: Optional[int] = None) -> None:
        """Reserve a manually-specified pin before auto-allocation.

        Call this for pins already defined in the YAML so the allocator
        skips them during auto-assignment.

        Args:
            pin: Pin identifier (e.g. 'PA2').
            peripheral: Peripheral name (e.g. 'USART2').
            signal: Signal name (e.g. 'TX').
            af: Alternate function number (optional; resolved from DB if None).
        """
        pin = pin.upper()
        peripheral = peripheral.upper()
        signal = signal.upper()

        if af is None:
            af = self.mcu_db.resolve_pin_function(pin, peripheral, signal)

        self._allocated[pin] = (peripheral, signal, af)
        logger.debug("Pre-allocated %s -> %s_%s (AF=%s)",
                     pin, peripheral, signal, af)

    def allocate(self, peripheral: str, signal: str) -> str:
        """Find and reserve a pin for a single peripheral function.

        Args:
            peripheral: Peripheral name (e.g. 'USART2').
            signal: Signal name (e.g. 'TX').

        Returns:
            Pin identifier (e.g. 'PA2').

        Raises:
            AllocationError: If no suitable pin is available.
        """
        peripheral = peripheral.upper()
        signal = signal.upper()

        candidates = self._get_candidates(peripheral, signal)
        chosen = self._select_best(candidates, peripheral, signal)
        if chosen is None:
            raise AllocationError(peripheral, signal,
                                  f"all {len(candidates)} candidate(s) taken")

        self._allocated[chosen["pin"]] = (peripheral, signal, chosen["af"])
        logger.info("Allocated %s -> %s_%s (AF=%s)",
                    chosen["pin"], peripheral, signal, chosen["af"])
        return chosen["pin"]

    # ------------------------------------------------------------------
    # Public API — bus (atomic multi-pin) allocation
    # ------------------------------------------------------------------

    def allocate_bus(self, peripheral: str,
                     signals: List[str]) -> Dict[str, str]:
        """Atomically allocate all signals for a bus peripheral.

        If any signal cannot be allocated, rolls back ALL allocations
        made during this call. Guarantees the peripheral gets either
        a complete pin set or nothing.

        Args:
            peripheral: Peripheral name (e.g. 'USART2', 'I2C1').
            signals: List of signal names (e.g. ['TX', 'RX']).

        Returns:
            Dict mapping signal -> pin (e.g. {'TX': 'PA2', 'RX': 'PA3'}).

        Raises:
            AllocationError: If any signal cannot be allocated.
        """
        peripheral = peripheral.upper()

        # Snapshot current state for rollback on failure
        snapshot = dict(self._allocated)
        result: Dict[str, str] = {}

        # Reveal same-port preference by pre-selecting the first signal
        # before allocating the rest — so remaining signals prefer the
        # same port as the first allocated pin.
        signals = [s.upper() for s in signals]
        first = True

        try:
            for signal in signals:
                if first:
                    chosen = self._select_best(
                        self._get_candidates(peripheral, signal),
                        peripheral, signal,
                    )
                    first = False
                else:
                    chosen = self._select_best(
                        self._get_candidates(peripheral, signal),
                        peripheral, signal,
                    )
                if chosen is None:
                    raise AllocationError(peripheral, signal,
                                          "bus requires all signals")
                self._allocated[chosen["pin"]] = (peripheral, signal, chosen["af"])
                result[signal] = chosen["pin"]
        except AllocationError:
            self._allocated = snapshot
            raise

        logger.info("Allocated %s bus: %s", peripheral,
                    ", ".join(f"{sig}={pin}" for sig, pin in result.items()))
        return result

    # ------------------------------------------------------------------
    # Public API — batch allocation from HardwareModel
    # ------------------------------------------------------------------

    def allocate_all(self, hw_model: Any) -> List[Dict[str, str]]:
        """Auto-allocate pins for all peripherals in a HardwareModel.

        Pre-allocates already-defined pins from the YAML, then fills in
        missing assignments for peripherals that have no manual pins.
        Skips peripherals where the user has already specified pins
        (even with generic function names like 'I2C_SCL').

        Args:
            hw_model: HardwareModel instance with .pins and .peripherals.

        Returns:
            List of new PinConfig-compatible dicts (id, function) to
            append to hw_model.pins. Empty if nothing needs allocation.
        """
        # Step 1: Pre-allocate all parseable pins already defined in the YAML
        for pin_cfg in hw_model.pins:
            parsed = self._parse_function_str(pin_cfg.function)
            if parsed is not None:
                peri, sig = parsed
                self.pre_allocate(pin_cfg.id, peri, sig, getattr(pin_cfg, "af", 0))

        new_pins: List[Dict[str, str]] = []

        # Step 2: Auto-allocate missing pins for each peripheral
        for peri in hw_model.peripherals:
            peri_name = self._resolve_peripheral_name(peri)
            if peri_name is None:
                logger.debug("Skipping '%s': cannot resolve peripheral instance",
                             peri.name)
                continue

            sig_list = _PERIPHERAL_SIGNALS.get(peri.type)
            if not sig_list:
                continue

            # Skip if user already manually assigned pins for this peripheral
            # (detected via generic function names like 'I2C_SCL' matching 'I2C1')
            if self._has_manual_pins(hw_model.pins, peri_name):
                logger.info("Skipping '%s' [%s]: pins already defined in YAML",
                            peri.name, peri.type)
                continue

            # Determine which signals already have precise pin assignments
            already = self._find_assigned_signals(peri_name, sig_list)
            missing = [s for s in sig_list if s not in already]

            if not missing:
                continue

            try:
                allocated = self.allocate_bus(peri_name, missing)
            except AllocationError as e:
                logger.warning("Cannot auto-allocate %s [%s]: %s",
                               peri.name, peri.type, e)
                continue

            for signal, pin in allocated.items():
                new_pins.append({"id": pin, "function": f"{peri_name}_{signal}"})

        return new_pins

    # ------------------------------------------------------------------
    # Public API — release
    # ------------------------------------------------------------------

    def release(self, pin: str) -> None:
        """Release a previously allocated pin back to the pool."""
        pin = pin.upper()
        if pin in self._allocated:
            logger.debug("Released %s", pin)
            del self._allocated[pin]

    # ------------------------------------------------------------------
    # Internal: candidate lookup & selection
    # ------------------------------------------------------------------

    def _get_candidates(self, peripheral: str,
                        signal: str) -> List[Dict[str, Any]]:
        """Return all valid (pin, af) entries for a peripheral signal.

        Excludes reserved pins (PA13/PA14) and already-allocated pins.
        """
        all_signals = self.mcu_db.get_peripheral_signals(peripheral)
        candidates: List[Dict[str, Any]] = []
        for entry in all_signals:
            if entry["signal"] != signal:
                continue
            pin = entry["pin"]
            if pin in _SWD_PINS:
                continue
            if pin in self._allocated:
                continue
            candidates.append(entry)
        return candidates

    def _select_best(self, candidates: List[Dict[str, Any]],
                     peripheral: str,
                     signal: str) -> Optional[Dict[str, Any]]:
        """Select the best pin from a list of candidates.

        Scoring priorities (lower = better):
          1. Priority hints — match (peripheral, signal) prefered pin.
          2. Same port as already-allocated pins for the same peripheral.
          3. Lower GPIO bank letter (A < B < C).
          4. Lower pin number.
        """
        if not candidates:
            return None

        # Collect ports already used by this peripheral (for bus cohesion)
        used_ports: Set[str] = set()
        for pin_key, (peri, _, _) in self._allocated.items():
            if peri == peripheral and len(pin_key) >= 2:
                used_ports.add(pin_key[1])  # 'PA2' -> 'A'

        hint_pin = _PRIORITY_HINTS.get((peripheral, signal))

        def _score(entry: Dict[str, Any]) -> Tuple[int, int, int, int]:
            pin = entry["pin"]
            port = pin[1] if len(pin) >= 2 else "Z"
            num = int(pin[2:]) if pin[2:].isdigit() else 999

            # 0 if pin matches priority hint, 1 otherwise
            hint_match = 0 if hint_pin and pin == hint_pin else 1

            # 0 if port is same as already-used ports for this peripheral
            same_port = 0 if port in used_ports else 1

            # Port ordinal: A=0, B=1, ...
            port_order = ord(port) - ord("A") if "A" <= port <= "Z" else 26

            return (hint_match, same_port, port_order, num)

        candidates.sort(key=_score)
        return candidates[0]

    # ------------------------------------------------------------------
    # Internal: function string parsing
    # ------------------------------------------------------------------

    @staticmethod
    def _parse_function_str(func: str) -> Optional[Tuple[str, str]]:
        """Parse a function string into (peripheral, signal) pair.

        'USART2_TX'  -> ('USART2', 'TX')
        'GPIO_Output' -> None  (pure GPIO, no AF allocation needed)
        """
        if func.startswith("GPIO_"):
            return None
        m = re.match(r"^([A-Z][A-Z0-9]*)_([A-Z][A-Z0-9]*)$", func)
        if m:
            return m.group(1).upper(), m.group(2).upper()
        return None

    @staticmethod
    def _resolve_peripheral_name(peri: Any) -> Optional[str]:
        """Extract the hardware peripheral instance name from a PeripheralModel.

        Priority: interface > bus > uart > name (if it looks like a peripheral).
        """
        # Check fields in priority order
        for attr in ("interface", "bus", "uart"):
            val = getattr(peri, attr, None)
            if val:
                return val.upper()

        # Fallback: if name looks like a peripheral instance (e.g. "USART2")
        name = peri.name.upper()
        if re.match(r"^(USART|UART|LPUART|I2C|SPI|TIM|ADC|CAN|USB)\d+$", name):
            return name
        return None

    def _find_assigned_signals(self, peripheral: str,
                               signals: List[str]) -> Set[str]:
        """Return which of the given signals already have pins allocated
        for the specified peripheral."""
        assigned: Set[str] = set()
        peri_upper = peripheral.upper()
        signal_set = {s.upper() for s in signals}
        for _, (peri, sig, _) in self._allocated.items():
            if peri == peri_upper and sig in signal_set:
                assigned.add(sig)
        return assigned

    @staticmethod
    def _has_manual_pins(pins: List[Any], peri_name: str) -> bool:
        """Check if any pin in the YAML already belongs to this peripheral.

        Handles both precise names ('USART2_TX') and generic names
        ('I2C_SCL' for I2C1). Strips trailing digits from peri_name
        to match generic function prefixes in the YAML.

        Args:
            pins: List of PinConfig objects from hw_model.pins.
            peri_name: Resolved peripheral instance name (e.g. 'I2C1').

        Returns:
            True if the user has already assigned at least one pin
            for this peripheral.
        """
        peri_upper = peri_name.upper()

        for pin_cfg in pins:
            func = pin_cfg.function.upper()

            # Exact match: 'USART2_TX' contains 'USART2'
            if peri_upper in func:
                return True

            # Generic prefix match: 'I2C_SCL' matches 'I2C1'
            # Strip trailing digits to get family prefix, e.g. 'I2C1' -> 'I2C'
            prefix = re.sub(r"\d+$", "", peri_upper)
            if prefix and prefix in func and func.startswith(prefix):
                return True

        return False
