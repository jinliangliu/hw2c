# CLI Debug Shell Commands

When `Internal_CLI` is enabled in your hardware YAML, hw2c generates a UART-based interactive debug shell. Connect via serial terminal (115200 bps default) to access the command prompt.

## Enabling CLI

```yaml
peripherals:
  - name: "uart_debug"
    type: "UART_Serial"
    extra:
      baudrate: 115200

  - name: "cli"
    type: "Internal_CLI"
    uart: "uart_debug"
    extra:
      prompt: "h2c> "
      stack_size: 512
      priority: 4
```

## Command Reference

### System Commands

| Command | Description | Availability |
|---------|-------------|-------------|
| `help` | List all available commands | Always |
| `version` | Show firmware version and build time | Always |
| `uptime` | Show system uptime in seconds | Always |
| `free` | Show free heap memory (bytes) | Always |
| `tasks` | List all FreeRTOS tasks with stack info | Always |
| `reset` | Trigger software reset of the MCU | Always |

### GPIO Commands

| Command | Description | Availability |
|---------|-------------|-------------|
| `gpio read <pin>` | Read GPIO pin level (0 or 1) | When pins configured |
| `gpio write <pin> <0\|1>` | Set GPIO output level | When pins configured |

Example:

```
h2c> gpio read PA0
PA0: 1
h2c> gpio write PC0 0
PC0 set to 0
```

### LED Commands

| Command | Description | Availability |
|---------|-------------|-------------|
| `led on` | Turn LED on | When LED pin exists |
| `led off` | Turn LED off | When LED pin exists |
| `led toggle` | Toggle LED state | When LED pin exists |

### RTC Commands

| Command | Description | Availability |
|---------|-------------|-------------|
| `rtc time` | Show current RTC time (HH:MM:SS) | When RTC enabled |
| `rtc set <HH:MM:SS>` | Set RTC time | When RTC enabled |

Example:

```
h2c> rtc time
RTC Time: 14:30:00
h2c> rtc set 08:00:00
RTC time set to 08:00:00
```

### Power Commands

| Command | Description | Availability |
|---------|-------------|-------------|
| `power info` | Show allowed sleep depth + component compat | When power_mgr enabled |
| `power mode <RUN\|SLEEP\|STOP0\|STOP1>` | Select low-power mode | When power_mgr enabled |
| `power sleep` | Enter the selected mode now (wake by RTC / UART / button) | When power_mgr enabled |

Example:

```
h2c> power mode STOP1
Power mode set to STOP1
h2c> power sleep
Entering STOP1 (wake by RTC 1s / UART RX / button)...
Woke up after 240 ms, RTC keeps time, RAM intact.
```

### Modbus Commands

| Command | Description | Availability |
|---------|-------------|-------------|
| `modbus read <addr> <count>` | Read holding registers | When Modbus enabled |
| `modbus write <addr> <value>` | Write single holding register | When Modbus enabled |

### Cellular Commands

| Command | Description | Availability |
|---------|-------------|-------------|
| `cellular status` | Show cellular connection status | When Cellular enabled |
| `cellular imei` | Show modem IMEI | When Cellular enabled |
| `cellular csq` | Show signal quality (CSQ) | When Cellular enabled |

### MQTT Commands

| Command | Description | Availability |
|---------|-------------|-------------|
| `mqtt status` | Show MQTT connection status | When MQTT enabled |
| `mqtt publish <topic> <payload>` | Publish a message to a topic | When MQTT enabled |

### FOTA Commands

Available when the example enables the FOTA receiver (`fota_receive:` / bootloader with
differential OTA). Two **entry points**, one receive chain — both end up in the same staging
session and take the same payload (the whole `.h2cd` patch record: 48 B envelope + lite stream).

| Command | Description | Availability |
|---------|-------------|-------------|
| `fota status` | State (IDLE/RECEIVING/READY/APPLYING/DONE/ERROR), pending slot, staged bytes, last YMODEM file name, last error code | FOTA receive enabled |
| `fota progress` | Progress 0–100 % (receive progress, then `dst_pos/new_size` during apply) | FOTA receive enabled |
| `fota recv` | Enter receive mode for the **H2C frame protocol**; host runs `generator/fota_sender.py` | FOTA receive enabled |
| `fota ymodem` | Enter receive mode for **YMODEM**; send the file with any terminal app's built-in YMODEM send | FOTA receive enabled |
| `fota apply` | Apply the staged patch (auto-scheduled once a transfer completes) | FOTA receive enabled |
| `fota erase` | Erase the target slot incl. the staging tail, drop the upgrade, reset metadata to IDLE | FOTA receive enabled |

Both entry points must be typed explicitly by the operator — this is a security property, not a
convenience: "wait for START from power-on" would let any serial noise trigger a Flash erase.
While a transfer is in progress the line editor is bypassed, so there is no "type a command to
cancel": the only ways out are a host `CAN`, retry exhaustion on the device side, or a timeout
(all of them visible to the host).

#### Upgrading over YMODEM (no host tool required)

```
> fota ymodem
CCC...          <- device sends 'C' every 3 s, about 10 times
                <- now pick the .h2cd file and send it with YMODEM
> fota status   <- after the batch finishes: READY, then DONE
```

Steps in any terminal app (Tera Term / SecureCRT / ExtraPuTTY / `sb`):

1. Build the patch on the host: `python generator/delta_tool.py ...` → `xxx.h2cd`.
2. On the device, type `fota ymodem`. The device starts sending `C` (CRC handshake) every 3 s,
   for about 30 s — that is the window for you to start the send.
3. In the terminal, choose *Send file… → YMODEM* and pick the `.h2cd` file.
4. The device writes into the staging tail as the blocks arrive, then applies the patch
   synchronously, writes the image, fixes the image header CRC and schedules the reboot.

Notes and limitations:

- The protocol itself has no whole-file check, so the **device accumulates CRC-32/ISO-HDLC while
  receiving**; a corrupted patch is rejected at the end rather than silently accepted.
- YMODEM has no partial-resume, so the host always re-sends from byte 0. The device treats the
  already-committed prefix as a **replay to be re-verified byte by byte** and fails fast on the
  first mismatch — that is what makes resume work at all (the host does not need to know where
  the break was).
- A batch is exactly one patch. A second, non-empty block 0 mid-transfer is rejected with `CAN CAN`
  instead of being silently written into the same staging area.
- Cost: ~2.7 KB of code and a 1029 B block buffer (RAM), see
  `docs/plans/differential-ota.md` §19.

## Customization

CLI is generated via `templates/drivers/drv_cli.c.j2`. The following template variables control CLI behavior:

| Parameter | Default | Description |
|-----------|---------|-------------|
| `prompt` | `"> "` | Shell prompt string |
| `stack_size` | `512` | CLI task stack size in words |
| `priority` | `4` | CLI FreeRTOS task priority |
| `max_cmd_len` | `64` | Maximum command line length |
