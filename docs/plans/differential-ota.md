# 差分 OTA（Delta OTA）算法设计规划

> 状态：规划中（仅设计，未落实现） · 目标：为 hw2c 生成的固件设计一套**可在
> Cortex-M0+ / 144 KB RAM 上流式执行**的差分升级算法，并把它接进现有的
> A/B Bootloader 与代码生成管线。
>
> 本规划附带一次**现状核查**：FR-14 在 `docs/requirements.md` 中标为 ✅，
> 但实测该路径**从未被任何示例启用、从未编译、从未测试**，且存在多处
> 使算法在结构上不可能工作的缺陷。详见 §1.2。

> ⚠️ **路线修订（2026-09-16，依据 §16）**
>
> §3–§6 自研的 `H2CD` 补丁格式**不建议实现**。与 **HPatchLite v1.0.2**
> 逐条比对源码后确认：二者是**同一套执行模型**（补丁流严格单向、旧数据随机读、
> 新数据顺序写、操作码同构），而 HPatchLite 已有*实测* 662 B 的解码器。
> §3 提出的"保序"性质并非新发现 —— 它正是 HPatchLite 的设计本身。
>
> ⚠️ 但"HPatchLite *已实现*的压缩与原地更新、以及持续维护的差分器"这一说法
> **已在本轮核查中被推翻**：那三项分别落在 `HDiff/Diff`、调用方胶水、
> 与 `HPatch` 模块里，**都不在 HPatchLite 的 4 个文件内**（§16.3 / §16.8.1）。
> 实际能借到的只有**解码器** —— 这直接决定了下面的三项裁决有多重。
>
> **改为**：设备侧解码内核采用 HPatchLite（vendored、钉版本），本规划的
> **信封 / 暂存 / 引导决策 / 集成与测试计划全部保留**。
>
> §1（现状核查）、§7–§15 基本不受影响，继续有效；§3–§6、§11.1、§13、§14
> 的增量修订见 §16.6。

> ✅ **三项裁决已定（2026-09-16）** —— 详述见 §16.7、落地形态见 §16.8
>
> | # | 待裁决 | 裁决 | 直接后果 |
> |---|---|---|---|
> | 1 | 主机侧 `hdiffi` 怎么来 | **只借解码器，自研 Python 差分侧** | 写侧（差分 + 补丁流编码）全部自研；`hdiffi` 不进依赖树；C1 作废、新增 C1' |
> | 2 | `-c-tuz` 默认开还是关 | **默认开** | 需自研 Python 的 tinyuz 编码器 + vendored tinyuz 解码器 + 自写解压胶水 |
> | 3 | inplace 是否永久排除 | **永久排除** | 只编流式入口；`hpatchi_inplace*` 永不编入；C7 结案 |
>
> ⚠️ **第 1、2 条叠加后产生一个原问题未覆盖的工作项**：HPatchLite 只发布
> **解码器**（4 个文件，无写侧），且**其内不含任何压缩层** —— 所以补丁流的
> *编码器* 与 tinyuz 的 *编码器* 都要我们自己写。这不是"借一个库"的量级，
> 是本规划里最主要的自研工作量。理由与缓解见 §16.7 裁决 1 与 §16.8.1/§16.8.3。

---

## 1. 背景与现状核查

### 1.1 现有资产

| 层 | 位置 | 说明 |
|---|---|---|
| 引导器模板 | `templates/bootloader/`（9 个 `.j2`） | `boot_main` / `boot_crc` / `boot_nvm` / `boot_jump` / `boot_app` |
| 应用侧 FOTA | `templates/drivers/drv_fota.{c,h}.j2` | UART 分片接收 + 状态机 |
| 解码器（bspatch） | `templates/drivers/fota_bspatch.{c,h}.j2` | BSDIFF40 解码 |
| 主机侧差分生成 | `generator/bsdiff_tool.py` | 声称产出 BSDIFF40 |
| 主机侧头部回填 | `generator/patch_crc.py` | 填 `image_size` / CRC32 |
| 传输端工具 | `generator/fota_sender.py` | 分片发送 |
| 接线 | `generator/context/bootloader_context.py` | `enabled` 时注入 IWDG + FOTA 驱动 |
| 槽位计算 | 同上 `build_boot_config()` | 见 §1.3 |
| 镜像头部契约 | `templates/linker/app_slot_{a,b}.ld.j2` | `.app_header` 段 |
| 测试 | `templates/test/test_fota_{bspatch,protocol}.c.j2` | 存在但无有效断言 |

### 1.2 现状核查结论：算法不可用，路径未被验证

**先给结论：当前实现不是"差分升级有 bug"，而是"差分升级从未真正跑起来"。**

决定性证据：**`examples/` 下没有任何示例声明 `bootloader:`**

```
$ grep -rn "bootloader" examples/*/hardware.yaml
（无输出）
```

而 `bootloader_context.py:121` 的判定是 `has_fota = has_bootloader and has_uart`
→ 在全部 8 个示例中 `has_fota` 恒为 `False` → `drv_fota.c` / `fota_bspatch.c`
**从来没有被渲染进任何工程**，自然也从未编译、从未上板、从未测试。
唯一的测试 `generator/tests/test_bootloader_context.py` 只校验了配置字典的取值。

据此逐文件复核，确认以下缺陷（严重度按"是否使算法不可能工作"排序）：

| # | 位置 | 问题 | 后果 |
|---|---|---|---|
| A1 | `fota_bspatch.c.j2:188-190` | 解码 COPY 段时**完全不取用 diff 块**（注释原文 `For simplicity: skip diff`） | 即便补丁本身正确，重建出的镜像也必然错误 |
| A2 | `fota_bspatch.c.j2:29-30` | `patch_buf[8192]` 全量常驻 RAM；且 `static uint32_t patch_size = 0` **全仓无任何赋值点** | `fota_bspatch_apply()` 第 4 行即 `return -1`，函数恒失败 |
| A3 | `fota_bspatch.c.j2:214-227` | 头部写到 `new_base + new_size`（镜像**尾部**），字段序为 `[size][crc][version][magic]` | 与 `patch_crc.py` 约定的 `[size][crc][magic][version]` @ `slot+0xC0` 完全不符 → 引导器在前 0x200 字节内搜不到 magic，永远拒绝 FOTA 产物 |
| A4 | `bsdiff_tool.py:144-148` | 循环退出后 `if last_scan < new_len` 追加一次尾部，紧接的 `ctrl_entries.append(...)` + `extra_parts.append(...)` **又追加一次** | extra 块尾部重复 → 补丁内容错误 |
| A5 | `bsdiff_tool.py:135,155-156` | `diff_parts` 恒为空 | 产出的不是标准 BSDIFF40（无 diff 块），任何标准 `bspatch` 都会解错 |
| A6 | `drv_fota.c.j2:263-266` | 收到分片后**不写入任何存储**（注释写 "In production, write chunk data to flash or RAM buffer"），仅累加 `progress_bytes` | 补丁数据收完即丢，`FOTA_FLAG_PATCH_READY` 之后无物可应用 |
| A7 | `drv_fota.c.j2:128-138` | `fota_init()` 无条件 `bkp_clear_flags()` | 上电第一件事就抹掉 BKP4R —— 而 BKP4R 的全部意义就是"跨掉电保存"。`fota_process()` 的 IDLE 分支因此永远读不到 `FOTA_FLAG_PATCH_READY` |
| A8 | `boot_crc.c.j2:99-108` vs `app_slot_a.ld.j2:46-52` | `boot_read_fw_version()` 读 `magic+4`，但 `.app_header` 只放了 3 个 `LONG`（无 version 字段） | 读到的是 `.text` 首字（一条指令），不是版本号；`boot_main.c.j2:176` 的 `new_ver > current_ver` 判据失效 |
| A9 | `test_fota_bspatch.c.j2` | mock 下旧固件恒返回 `0xAA`、擦除/编程为 no-op | **结构上不可能**通过主机测试发现 A1/A2/A3 |

另有 3 项需独立复核（不影响本规划结论）：

| # | 位置 | 疑点 | 待办 |
|---|---|---|---|
| B1 | `patch_crc.py:89-95` | 在 `payload_offset`（= magic+4 = `.text` 起始）**插入** 4 字节版本号，使 0xCC 之后整体后移 4 字节；但链接期 `.text` 就放在 0xCC，绝对地址引用未同步 | 需实测：带 `.app_header` 构建 + `patch_crc.py` 回填后，向量表条目是否整体偏移 4 导致跳转失败 |
| B2 | `fota_bspatch.c.j2:209-212` | 尾部残字节按**整双字**刷写，未补齐 `0xFF` | 最多越界写 7 字节，且越界内容可能非 `0xFF` |
| B3 | `bsdiff_tool.py:24-34` | `suffix_array` 对 256 KB 输入做 Python 切片排序，中间量约 32 GB；`bsearch_sa` 每次比较也切片 | 规模上直接不可用，需换匹配算法（见 §10） |

> **对需求文档的影响**：`docs/requirements.md` 的 FR-14 与 §2.1「当前范围（已交付）」
> 中 `FOTA 差分升级`、`Bootloader A/B` 两项标 ✅ **与事实不符**，建议本轮改为
> ⏳ 并把本节作为状态说明。§8「待办」里已列的两项（`Bootloader 端到端测试`、
> `FOTA 完整集成测试`）应提升为 FR-14 的前置条件。

### 1.3 资源与布局现状（实测）

| 项 | 值 | 来源 |
|---|---|---|
| MCU | STM32G0B1RET6，Cortex-M0+，Flash 512 KB / RAM 144 KB，无 FPU | `hardware.yaml` |
| Flash 页 | 2 KB | `fota_bspatch.c.j2:14` |
| Flash 基址 | `0x08000000` | `bootloader_context.py:72` |
| Bootloader 默认 | `size_kb=8` | `bootloader_context.py:58` |
| Slot A 默认 | `app_a_offset=0x2000` → `0x08002000`，大小 `0x3E000`＝**248 KB** | 同上 `:59,:83` |
| Slot B 默认 | `app_b_offset=0x40000` → `0x08040000`，大小 **256 KB** | 同上 `:60,:84` |
| `max_retries` | 3 | 同上 `:63` |
| `wdg_timeout_ms` | 5000（IWDG 预分频 /256） | 同上 `:64-69` |
| NVM（TAMP BKP） | `BKP0R`=失败计数、`BKP1R`=活动槽、`BKP2R`=boot_ok 魔数、`BKP3R`=NVM 初始化魔数、`BKP4R`=FOTA 标志 | `boot_nvm.c.j2:10-16`、`drv_fota.h.j2:26-30` |
| 镜像头部 | `slot+0xC0`：`[image_size(4)][crc32(4)][magic"H2Ck"(4)]`，payload 紧随其后 | `app_slot_a.ld.j2:43-52`、`patch_crc.py:6-15`、`boot_crc.c.j2:48-64` |
| CRC 定义 | STM32 硬件 CRC32（反射式，`REV_IN` 字节反转 + `REV_OUT`），覆盖 `[magic+4, +image_size)` | `boot_crc.c.j2:66-89`、`patch_crc.py:33-46` |
| **实测固件体积** | `base.bin` = **89,172 B**；`solenoid` = **107,968 B**；`thermo` = 105,996 B；`mpu6050` = 103,680 B | `arm-none-eabi-size` |
| 实测内存占用 | `base`：text 88,084 / data 1,080 / bss 17,312 | 同上 |

**关键推论**：现有镜像 89–108 KB，而槽位 248/256 KB —— **槽位利用率仅 ~42%**。
这给了差分 OTA 两件此前没有的余地：镜像在槽内有充足余量，且**不需要重新分区**
就能腾出补丁暂存空间（见 §7.2 尾仓暂存）。

---

## 2. 目标与非目标

### 2.1 目标

| 编号 | 目标 | 量化判据 |
|---|---|---|
| G1 | 补丁在设备侧**流式**执行，RAM 占用与镜像/补丁大小无关 | 峰值 RAM ≤ 8 KB（页缓冲 2 KB + 字面量环 4 KB + 状态） |
| G2 | 补丁体积显著小于全量 | 单函数级改动 patch ≤ 新镜像的 15%；中等改动 ≤ 40% |
| G3 | 掉电任意时刻安全：**活动槽永不被触碰** | 掉电注入测试中活动槽 CRC 恒有效 |
| G4 | 升级失败/新镜像无效 → **自动回滚**到旧槽 | 引导决策表（§9.2）全部路径可测 |
| G5 | 解码器可在**主机侧编译并运行**，与设备侧同一份源码 | 同一 `delta_decode.c` 同时构建到 host 单测与固件 |
| G6 | 端到端流程**有示例、有 CI** | 新增 `examples/fota_demo/`，pytest + host test + SIL 全绿 |
| G7 | 不动供应商源码（HAL/CMSIS/FreeRTOS） | `git diff --stat -- static/` 为空 |

### 2.2 非目标（本期不做）

- 不做**固件签名/安全启动**：本期仍只有 CRC32（完整性，非真实性）。但补丁头部
  预留 `auth_len` 字段，为后续 Ed25519 留位（§12.3）。
- ~~不做压缩的**强制**启用：v1 字面量流不压缩，v2 以 flags 位可选启用（§5.5）。~~
  **已由裁决 2 修订**：压缩**默认开**（tinyuz），见 §16.7 第 7 问与 §16.8.1。
  不过它仍是可插拔层 —— `compress_type=0` 的未压缩路径必须始终可用，
  P1' 也先用它把 L2 跑绿（§16.6）。
- 不做 HTTP/云侧 OTA：传输仍是"主机 → 串口 → 设备"的直连模式。
- 不做多 MCU 后端：算法本身与 MCU 无关，但本期只落 STM32G0 的 Flash 后端。
- 不做原地（in-place）升级：始终是 A/B 异槽写入。**裁决 3 已将其由"本期不做"
  升为"永久排除"**，不作为配置项（理由见 C7）。

---

## 3. 算法总览

> ⚠️ **本节及 §4–§6 已被 §16 修订**：其中的"保序"性质 HPatchLite 早已实现并发布。
> 以下原文**保留作为设计记录与对照基线**（§16.2 的对照表逐条引用它），
> 但实施路线请按 §16.6。

差分 OTA 拆成三段，**每段的边界都是"可独立验证的数据"**：

```text
   主机侧（Python / 可随机访问）        设备侧（C / 只能顺序访问）
   ────────────────────────────        ────────────────────────────
   ①  差分生成                          ③  流式解码
   old.bin + new.bin                     补丁流 + 旧镜像（Flash 只读）
        ↓ 匹配 / 编码                          ↓ 逐操作消费
     patch.h2cd  ──────── ② 传输 ────────→  新镜像（写入目标槽）
                    （分片 + CRC16 + ACK）
```

设计上的核心约束来自设备侧：**Cortex-M0+ 上"顺序"是廉价的，"回退/随机"是昂贵的**。
Flash 可内存映射（旧镜像随机读免费），但 RAM 只有 144 KB 且要留给 RTOS 与业务。
因此算法必须满足：

> **性质 P1（保序）**：补丁被解码器严格单向消费 —— 控制流与数据流都只前进。
> **性质 P2（有界）**：任一时刻设备侧只需持有常数级状态 + 一个页缓冲。

标准 BSDIFF40 **不满足 P1**：其布局是 `[ctrl 块][diff 块][extra 块]`，而控制三元组
`(add, copy, seek)` 的第 1 个动作就要从**排在最后**的 extra 块取字节 —— 标准
`bspatch` 靠 `fseek` 解决，MCU 上没有这个能力（要么全量缓存，要么找可落脚的暂存区）。

**本规划的算法 = 把 BSDIFF40 重排成保序形式**：保留 bsdiff 的匹配模型与
`add / copy / seek` 语义，但把 `diff` 与 `extra` 合并成**一条按消费顺序排列的字面量流**，
控制块改为**变长整数编码**并单独前置。于是三块变成两块、随机访问变成顺序访问。

```text
  标准 BSDIFF40（不保序）               H2CD v1（保序）
  ┌──────────────┐                     ┌──────────────────┐
  │ Header 32 B  │                     │ Header 32 B      │
  ├──────────────┤                     ├──────────────────┤
  │ ctrl  : 24B×N│──┐                 │ ctrl : varint×N  │──┐ 顺序读
  ├──────────────┤  │ add 要 extra     ├──────────────────┤  │
  │ diff : ...   │  │ copy 要 diff     │ literal : 按消费  │  │ 顺序读
  ├──────────────┤  │  ✗ 需 fseek      │           顺序排列│  │
  │ extra: ...   │──┘                 └──────────────────┘──┘
  └──────────────┘
  ctrl 24 B/三元组                      ctrl ≈ 3–6 B/三元组
```

**这套重排是可逆的、且可被自动化**：任何现存的标准 BSDIFF40 补丁都能由主机侧
一次性重打成 H2CD（见 §10.2 `repack`），因此不牺牲与既有 bsdiff 工具链的兼容性。

---

## 4. 补丁格式 H2CD v1

### 4.1 文件布局

```text
┌────────────────────────────────────────────────────────────┐
│ Header (32 B, 小端)                                        │
├────────────────────────────────────────────────────────────┤
│ Control stream  (ctrl_size 字节, 变长整数编码的操作序列)     │
├────────────────────────────────────────────────────────────┤
│ Literal stream  (literal_size 字节, 按被消费顺序排列)        │
└────────────────────────────────────────────────────────────┘
```

### 4.2 Header

| 偏移 | 类型 | 字段 | 说明 |
|---|---|---|---|
| 0x00 | `u32` | `magic` | `0x44324348` = ASCII `H2CD`（小端存 'H','2','C','D'） |
| 0x04 | `u16` | `format_ver` | = 1 |
| 0x06 | `u16` | `flags` | bit0=字面量经 LZSS；bit1=带签名；其余保留（须为 0） |
| 0x08 | `u32` | `old_size` | 期望的旧镜像 `.bin` 大小 |
| 0x0C | `u32` | `old_crc32` | 旧镜像 **payload** 的 CRC32（即 `boot_crc` 同一算法、同一区间） |
| 0x10 | `u32` | `new_size` | 新镜像 `.bin` 大小 |
| 0x14 | `u32` | `new_crc32` | 新镜像 **payload** 的 CRC32 —— 解码完成后设备侧必须复算出相同值 |
| 0x18 | `u32` | `ctrl_size` | 控制流字节数 |
| 0x1C | `u16` | `hdr_crc16` | 覆盖 `0x00..0x1D`（前 30 字节）的 CRC-16/CCITT |
| 0x1E | `u16` | `auth_len` | 签名字节数，0 = 无签名（为 §12.3 预留） |

**为什么头里要带 `old_size` / `old_crc32`**：设备侧在**烧写任何一页之前**就能判定
"这份补丁是不是给当前这版固件的"。不匹配立即拒绝，避免把目标槽擦成半成品。
这是差分升级最容易被忽略、代价却最高的一处前置校验。

### 4.3 控制流指令集

| 操作码 | 名称 | 编码 | 语义 |
|---|---|---|---|
| `0x00` | `END` | — | 结束；此后不得有指令 |
| `0x01` | `ADD` | `len:uvarint` | 从字面量流取 `len` 字节 → 输出 |
| `0x02` | `COPY` | `len:uvarint` | 从旧镜像 `src_pos` 起拷 `len` 字节 → 输出；`src_pos += len` |
| `0x03` | `CDIFF` | `len:uvarint` | 从字面量流取 `len` 字节作为差值：`out[i] = old[src_pos+i] + lit[i]`（模 256）；`src_pos += len` |
| `0x04` | `SEEK` | `delta:ivarint` | `src_pos += delta`（可负） |
| `0x05` | `FILL` | `len:uvarint, value:u8` | 输出 `len` 个 `value`（典型场景：`0xFF` 补齐、`0x00` 清零的 .bss 镜像区） |

编码规则：

- 无符号变长整数 `uvarint`：LEB128，每字节低 7 位有效，最高位为"续接"标志。
- 有符号变长整数 `ivarint`：先 zigzag 映射（`n ≥ 0 → 2n`，`n < 0 → -2n-1`）再 `uvarint`。

**变长编码是必须的，不是优化**：现有实现用固定 8 字节字段（`fota_bspatch.c.j2:148-151`
的 `read_le64` + `struct.pack('<QQQ')`）——一个 5,000 三元组的补丁光控制块就 **120 KB**，
比它要替代的全量镜像还大。改用 varint 后典型三元组为 `COPY(中长) + SEEK(小)` ≈ 3–6 字节。

`FILL` 增益虽小但几乎零成本，对固件里的空白/零区很有效（STM32G0B1 的 `.bin` 常见
大段 `0x00` 的 `.bss` 镜像区）。

### 4.4 与 BSDIFF40 的语义映射（保证可自动重打）

标准 bsdiff 的一个三元组 `(add_len, copy_len, seek_len)` 在本格式中展开为：

```text
  ADD(add_len)            ← 字面量取自原 extra 块的下 add_len 字节
  CDIFF(copy_len)         ← 字面量取自原 diff 块的下 copy_len 字节
  SEEK(seek_len)          ← 原语义
```

三条均为保序操作，且字面量的取用顺序恰好是 `extra[0..add₀] , diff[0..copy₀] ,
extra[add₀..add₁] , diff[copy₀..copy₁] , …` —— **恰好是从两个块各取一个前向游标、
交替消费**。因此 `repack` 是一个 O(补丁大小) 的单趟重排，无需重新做匹配。

> 注意：`add_len == 0` / `copy_len == 0` 的三元组在展开时须跳过对应操作；
> 原 `bsdiff_tool.py` 会产出大量 `add_len == 0` 的项（其匹配循环的副作用），
> 重打时应合并相邻同类操作以压掉冗余控制开销。

### 4.5 压缩（v2，可选）—— ⚠️ 已被 §16.8.1 取代

> **本节整体作废（2026-09-16）。** 裁决 2 改为"压缩**默认开**"，且不采用这里设想的
> 自研 LZSS，而是 vendored 的 **tinyuz** 解码器 + 我们自研的 Python 编码器。
> 落地形态（压缩从哪个偏移开始、`dict_size` 取多少、胶水写在哪）见 **§16.8.1**。
> 以下原文保留，仅为记录当时的取舍过程。

字面量流天然是**一条顺序消费的字节流**，因此可以在其上叠加任意顺序熵编码而
不破坏性质 P1——解码器只需一个窗口大小的环缓冲。

- 推荐 **LZSS，窗口 2 KB，前瞻 16–24 B**（解码器代码 < 200 行，RAM 4 KB）。
- 不推荐 LZ4/DEFLATE：前者窗口太小、后者解码器状态大且许可证/代码量不友好。
- 不推荐 bzip2（标准 BSDIFF40 用的）：代码量、RAM 与 CPU 都不适合 M0+ 无 FPU。
- flags bit0 置位即表示已压缩；解码器不支持该位时**必须直接拒绝**（不可静默忽略）。

v1 先不启用：小改动场景字面量本就很少，压缩收益微乎其微，而复杂度与风险
（一个只在真机暴露的解码 bug 就足以让升级失败）不成比例。

---

## 5. 设备侧解码算法

### 5.1 分层：纯算法与 Flash 后端解耦

这是让 G5（主机侧同源可测）成立的前提。**`delta_decode.c` 内不得出现任何 HAL 调用**，
所有 I/O 通过两个函数指针注入：

```c
typedef struct {
    /* 读旧镜像（Flash 内存映射，直接取值） */
    int  (*old_read) (void *ctx, uint32_t off, uint8_t *dst, uint32_t len);
    /* 顺序写新镜像：off 单调不减，实现方可按页缓冲 */
    int  (*new_write)(void *ctx, uint32_t off, const uint8_t *src, uint32_t len);
    /* 顺序取补丁字节 */
    int  (*patch_read)(void *ctx, uint8_t *dst, uint32_t len);
    void  *ctx;
} delta_io_t;

int delta_decode(const delta_hdr_t *hdr, const delta_io_t *io,
                 delta_progress_t *prog /* 可选，用于喂狗与续传点 */);
```

- 设备侧后端：`new_write` 收进 2 KB 页缓冲，满页调 `HAL_FLASHEx_Erase`/`HAL_FLASH_Program`；
  `old_read` 直接从 `SLOT_x_ADDR` 取值；`patch_read` 从 W25Q32 或接收环缓冲取值。
- 主机侧后端：旧镜像来自 `old.bin`，新镜像写进内存缓冲，补丁来自文件 → 直接在
  单测/SIL 里逐字节对比。

**这条约束把 A9 那一类"mock 让测试无法发现缺陷"的坑从结构上堵死了** ——
主机侧跑的是与设备侧**同一份**解码源码，而不是一份行为不同的替身。

### 5.2 解码状态机

```text
        ┌──────────────────────────────────────────────────────┐
        │ INIT                                                 │
        │  · 校验 hdr_crc16 / magic / format_ver / flags        │
        │  · 校验 old_size、old_crc32 与活动槽实况一致          │
        │  · 校验 new_size + patch_size 放得下目标槽            │
        └───────────────────────┬──────────────────────────────┘
                                │ 通过
        ┌───────────────────────▼──────────────────────────────┐
        │ ERASE  （按页擦除目标槽的 ceil(new_size/2K) 个页）     │
        │  · 逐页擦除，每页之间 IWDG_Refresh()                  │
        └───────────────────────┬──────────────────────────────┘
                                │
        ┌───────────────────────▼──────────────────────────────┐
        │ DECODE （循环取控制指令，见 §5.3）                     │
        │  · 输出游标 dst_pos 严格单调递增                       │
        │  · 旁路累积 CRC32（经过 0xCC 之后按字喂 CRC->DR）      │
        └───────────────────────┬──────────────────────────────┘
                                │ END
        ┌───────────────────────▼──────────────────────────────┐
        │ FLUSH                                                │
        │  · 补齐并写最后一页（余量填 0xFF）                     │
        │  · 回填镜像头部 @ slot+0xC0：[size][crc][magic][ver]   │
        └───────────────────────┬──────────────────────────────┘
                                │
        ┌───────────────────────▼──────────────────────────────┐
        │ VERIFY  · 回读 payload 复算 CRC32 == hdr.new_crc32     │
        └───────────────────────┬──────────────────────────────┘
                    ┌───────────┴───────────┐
                    │                       │
        ┌───────────▼──────────┐  ┌─────────▼──────────────────┐
        │ COMMIT               │  │ FAIL                       │
        │ · 提交元数据 pending │  │ · 目标槽标记为无效          │
        │ · 复位进 Bootloader  │  │ · 记录 FAIL 原因（.noinit） │
        └──────────────────────┘  │ · **不动活动槽**，可重试     │
                                  └────────────────────────────┘
```

### 5.3 主循环（要点）

- **输出游标单调**：`dst_pos` 从 0 线性增长到 `new_size`。任何指令都不会回退它。
  这是 P1 在输出侧的具体化，也是"删除不需要重放"结论的依据（§7.1）。
- **页缓冲**：2 KB。页内偏移越过 0xC0 的 12 字节时**先写 `0xFF` 跳过**，留到
  FLUSH 阶段单独编程 —— 因为 0xC0..0xCB 是镜像头部，其内容（CRC）只有在
  payload 全部生成后才能确定，而 Flash 只能单向 1→0，**不能先写后改**。
- **CRC 旁路累积**：指令把输出字节推过 0xCC 之后，按 4 字节攒成一个字写入
  `CRC->DR`，无需第二遍回读。`image_size` 尾部不足 4 字节的部分按 §1.3
  的既有约定处理（与 `patch_crc.py` 保持一致）。
- **喂狗**：每处理完一页（或每 N 条指令）调一次 `IWDG_Refresh()`。
  现有实现在 `fota_bspatch.c.j2:56` 只留了一句注释"Keep IWDG fed externally"，
  并无实际喂狗点 —— 5000 ms 超时下，擦 + 写 108 KB 很可能直接复位。
- **进度上报**：`prog.dst_pos / hdr.new_size`，供 CLI/遥测读取（复用现有
  `fota_get_progress()` 接口）。
- **中断与并行安全**：解码期间不应被业务中断打断关键区。设计上**不引入临界区**，
  而是在一个专用任务里同步跑完（见 §11.3），把并发问题消掉而不是加锁。

### 5.4 RAM 预算

| 项 | v1（不压缩） | v2（LZSS） |
|---|---|---|
| 页缓冲 | 2,048 B | 2,048 B |
| 字面量环缓冲 | 0 | 4,096 B |
| 解码状态（游标、累积字、指令暂存） | ~64 B | ~96 B |
| 补丁头 | 32 B | 32 B |
| **合计** | **≈ 2.2 KB** | **≈ 6.3 KB** |

对比现状：现有实现声明 `uint8_t patch_buf[8192]` + `chunk_buf[1028]`，
且**多出来的 8 KB 并不解决问题**（A2：全量在 RAM 反而限制了可升级的镜像规模）。

---

## 6. 主机侧差分生成算法

### 6.1 为什么换掉后缀数组

现有 `bsdiff_tool.py:24-34` 的 `suffix_array()`：

```python
sa = list(range(n))
sa.sort(key=lambda i: data[i:])     # n 个切片，平均长度 n/2
```

对 256 KB 输入，`data[i:]` 的中间量约 `n²/2 = 32 GB`，且比较是 Python 层的字节序比较。
**这不是"慢"，是不可用**（B3）。而 `bsearch_sa()` 每次二分又切片一次，雪上加霜。

标准 bsdiff 的正确做法（qsort + 倍增/SA-IS）在纯 Python 里依然过慢；
引 C 扩展又违背"生成器纯 Python、零编译依赖"的现状。

### 6.2 改用滚动哈希块匹配（rsync / zdelta 路线）

对**固件**这一类输入，逐字节最优匹配的收益远小于"块级匹配 + 局部编码"：

```text
  ① 对 old.bin 建索引：固定块长 B=16，滚动哈希（Rabin-Karp）→ 哈希表 (hash → [offsets])
  ② 在 new.bin 上滑窗，命中哈希即验证字面量、向前向后贪心扩展匹配
  ③ 匹配段 → COPY(n)；未命中段 → 逐字节与 old 的同位字节比较，
       若多数字节相同 → CDIFF（字面量只放差值），否则 → ADD
  ④ 相邻同类操作合并；输出 varint 控制流 + 保序字面量流
```

- 复杂度 O(n)，内存 O(n/B)，纯 Python 可在秒级完成 256 KB 输入。
- 对"同一份源码、不同槽基址"的 A→B 场景（§8.1）尤其合适：代码段几乎整段命中。
- 可用参数 `B`（块长）与 `-l`（最小匹配长度）调优；默认 `B=16` 在固件上足够。

> 若后续愿意引入编译依赖，可换成 `zstandard` 的 `zstd --patch-from` 或
> `divsufsort` 绑定以获得更优比率。本规划把接口留成
> `delta_build(old, new, opts) -> patch`，内部实现可替换。

### 6.3 必须提供的两个工具

| 工具 | 职责 |
|---|---|
| `hw2c-delta build old.bin new.bin -o p.h2cd` | 生成 H2CD 补丁（§6.2） |
| `hw2c-delta repack p.bsdiff -o p.h2cd` | 把标准 BSDIFF40 重打成 H2CD（§4.4） |
| `hw2c-delta apply old.bin p.h2cd -o out.bin` | **用设备侧同一份 `delta_decode.c` 编译出的主机程序解码**（G5） |
| `hw2c-delta info p.h2cd` | 打印头部与操作统计（补丁构成、比率） |

`apply` 的存在是这套设计最关键的工程保障：**CI 每次都能在主机上把补丁跑一遍并与
`new.bin` 逐字节比对**，而不必等上板。

---

## 7. 掉电与失效语义

### 7.1 幂等重放（v1 基线）

不变式：**补丁应用期间，活动槽只读、目标槽写坏也无所谓**。

- 目标槽的镜像头部（`slot+0xC0` 的 magic）在 FLUSH 之前始终是 `0xFF` 或被擦除态
  → 引导器 `boot_crc_verify()` 必然判无效 → 不会引导半成品。
- 掉电后：目标槽无效 + `pending` 标志仍在 → 设备请求**重新传输补丁并从头应用**。
- 由于 P1（输出严格单调、无回退），**从头重放一定是安全的**，不会出现"半新半旧"
  被误认为新镜像的情况。

代价：掉电要重传。在 115200 bps（≈11.5 KB/s）下，20 KB 补丁约 1.7 s —— 可接受。

### 7.2 可恢复模式：尾仓暂存（推荐 v1.5）

若要避免重传，需要把补丁**先完整落 Flash 再应用**。基于 §1.3 的利用率实测，
**不需要重新分区**即可实现：

```text
  目标槽（Slot B，256 KB）
  ┌──────────────────────────────┬──────────────────────────────┐
  │ 新镜像（自槽首向前生长）      │ 补丁（自槽尾向后锚定）        │
  │ 0x08040000 →                 │ ← 0x0807F000 - patch_size    │
  └──────────────────────────────┴──────────────────────────────┘
            ↑ 擦除并写入                    ↑ 传输期写入，应用期只读
```

- 补丁在传输期即写入槽尾：`patch_addr = slot_b_end - patch_size`（`patch_size` 由
  START 报文给出，见 §8）。
- 应用期**只擦除镜像占用的页**（`ceil(new_size/2048)` 页），尾仓所在页不动。
- 准入条件（START 阶段即校验，不满足直接拒绝）：
  `ceil(new_size / 2048) * 2048 + patch_size + 0xC0 ≤ slot_size`
- 掉电后可**免重传**继续：补丁仍在尾仓，设备只需重跑应用阶段。

按实测 `solenoid` 镜像 108 KB、补丁按 15% 估 16 KB 计：`108 + 16 + 0.2 = 124 KB
≤ 256 KB`，余量充足。

### 7.3 更保守的备选：独立暂存分区

若期望补丁可能很大（例如跨版本大改，补丁 > 100 KB），可改为收缩槽位并划分独立暂存区：

| 区域 | 地址 | 大小 |
|---|---|---|
| Bootloader | `0x08000000` | 8 KB |
| Slot A | `0x08002000` | 192 KB |
| Slot B | `0x08032000` | 192 KB |
| 补丁暂存 | `0x08062000` | 116 KB |
| 元数据（双副本） | `0x0807F000` | 4 KB |

好处：槽位**对称**（对 §8.1 的差分友好）、有独立元数据页（§9.1）、不必依赖尾仓约束。
代价：改动 `app_a_offset`/`app_b_offset` 的默认值、需要重新验证链接与跳转。

**建议**：v1 用尾仓暂存（§7.2），暂不重分区；把本表作为补丁体积失控时的逃生路线。

---

## 8. 镜像与槽位布局对差分率的影响

### 8.1 跨槽差分的固有代价（必须显式设计）

A/B 双槽天然存在的问题：**Slot A 与 Slot B 的链接基址不同**，因此同一个源
链接两次会得到两份不同的镜像 —— 全部**绝对 Flash 地址**（字面量池里的函数指针、
字符串地址、向量表条目）都不同。

本节的结论：**这对差分率影响可控，但必须显式处理。**

- **可控**：ARM Thumb 的分支/跳转绝大多数是 PC 相对（`B`/`BL`/`Bcc`、字面量池
  也随 PC 相对），这些字节在两份镜像中**完全相同**；差异集中在
  `(a)` 向量表 192 B、`(b)` 承载绝对 Flash 地址的字面量池。改动是**稀疏**的
  4 字节字，块级匹配器能大量命中 COPY，差异字落入 CDIFF/ADD。
- **必须处理**：差分工具**要知道两个槽的基址**才能把 old/new 对齐；
  否则若把"为 Slot A 链接的新镜像"误当作 Slot B 目标，产物即使 CRC 正确
  也会在跳转后硬故障。

**设计要求**：

1. `hw2c-delta build` 必须接收槽位上下文（`--old-slot A --new-slot B` 或直接
   读生成目录的 `toolchain.cmake`/链接脚本），并在补丁头里固化 `new_size`/`new_crc32`。
2. 生成期应输出**两份** `.bin`（slot A 版与 slot B 版）供差分使用，命名如
   `base.slotA.bin` / `base.slotB.bin`。
3. **槽位对称化**（§7.3 表）会让基址差成为固定值 `0x30000`，差异更规整、
   元数据更简单 —— 这是对称槽除"好算账"之外的第二个理由。

### 8.2 头部与向量的处理

- 镜像头部（`slot+0xC0`，12 B）在 old/new 中都存在但内容不同（size/CRC 变），
  属于稀疏差异，交给 CDIFF 即可。
- `#B1` 的 `.text` 起始偏移疑点必须先复核（`patch_crc.py` 的 4 字节插入）——
  在它明确之前，**头部布局不得固化进补丁格式**。本规划的 H2CD 只依赖
  "`slot+0xC0` 处有 16 字节头部"这一约定，与 B1 的结论解耦。

---

## 9. 元数据与启动决策

### 9.1 元数据布局（**专用 Flash 页 + 日志式记录**）

> **本节已按 2026-09-17 的实测结论重写。原版（"扩展现有 TAMP BKP"）建立在一个
> 错误的前提上，整节作废。**

#### 原版错在哪

原文写"现有 `boot_nvm` 用了 `BKP0R..BKP3R`、FOTA 用了 `BKP4R`。G0 的 TAMP 有
`BKP0R..BKP31R`，空间充足"，并据此把元数据分配到 `BKP5R..BKP9R`。**"32 个"是错的**：

| 证据 | 内容 |
|---|---|
| RM0444 §31.1 | TAMP 章开头写明 "**5** backup registers" |
| RM0444 §31.6.8 | `TAMP_BKPxR` 偏移 `0x100 + 4*x`，**x = 0..4** |
| vendored `stm32g0b1xx.h` | `TAMP_TypeDef` 正好止于 `BKP4R`；位定义也只到 `TAMP_BKP4R` |

而 `BKP0R`（失败计数）、`BKP1R`（活动槽）、`BKP2R`（boot_ok 魔数）、`BKP3R`
（NVM 初始化魔数）归 `boot_nvm`，`BKP4R` 归 `boot_main` 的旧 FOTA 标志 ——
**一个不剩**。于是早期实现往 `BKP5R..BKP9R` 写元数据：目标上编译期就报
"no member named 'BKP5R'"，若绕过编译则写进保留地址、**静默不生效**，只有跨复位
才看得出来。

> 这个缺陷能在 P0/P2'/P3 的主机测试里全部通过，原因值得单独记一笔：
> 当时的 `mock_hal.h` 把 `TAMP_TypeDef` 开到了 `BKP31R`，**mock 凭空造出了硅片上
> 不存在的寄存器**。这不是"mock 太宽松"，而是 mock 与产品代码**共享同一个错误
> 前提** —— 测试既发现不了问题、也提示不了方向（A9 最贵的一种）。
> 现已截断到 5 个寄存器，并加了护栏
> `test_bootloader_host_tests.py::test_mock_tamp_backup_register_count_matches_silicon`。

#### 现在的方案

元数据放在**引导器区（`bootloader.size_kb`）的最后一页**，用**日志式追加记录**：

| 项 | 值 | 真源 |
|---|---|---|
| 页基址 | `0x08000000 + size_kb*1024 - page_size` | `bootloader_context.py` 由 YAML 算出 |
| 页大小 | `2048`（= 器件擦除粒度，与 `delta_page_size` 必须相等，生成期断言） | `fota_format.json:metadata.page_size` |
| 记录长度 | `24 B`（3 个双字，正好一次 8 B 编程粒度 ×3） | `fota_format.json:metadata.record_size` |
| 字段 | `magic(4) seq(4) state(4) slot(4) staged(4) crc16(2)` + `pad(2)` | `fota_format.json:metadata.fields` |
| 容量 | 85 条 + 8 B 页尾余数（**余数不参与日志**，扫描上界用 `RECORD_COUNT`） | 推导 |
| `magic` | `0x4D544F46`（内存里读出来是 `'FOTM'`） | `fota_format.json:metadata.fields.magic` |

**为什么是日志式而不是"两个槽乒乓"**：乒乓每轮都要擦一整页，而"擦掉旧槽"那段
时间里**唯一**的有效记录正好在被擦的页上，掉电即全丢。追加式把每次状态变更写成
一条 24 B 新记录，页满（85 条）才擦一次；掉电最多毁掉**正在写的那一条** ——
扫描时 CRC16 不过就被跳过，上一条仍然有效。这一条性质由生成出来的
`test_fota_protocol.c::test_torn_record_is_skipped_and_the_previous_one_still_holds`
正面钉住。

**提交点最后**：一条记录 = 3 个双字，含 CRC16 的第 3 个双字**最后落盘**。被打断
的记录一定校验不过、被跳过；已提交的上一条不受影响。反过来先写校验值，就会留下
"校验通过但内容是半截"的记录 —— 那是最坏的一种状态。

**记录里只放无法从别处重算的字段**：

* `state` / `slot` —— 决策本身，以及目标槽（暂存区地址由它算出，而信封本身就在
  暂存区里，是个鸡生蛋）；
* `staged` —— 已提交字节数。补丁正文里可以合法出现 `0xFF`，所以**扫描暂存区推不出**
  "写到哪了"；
* `new_crc32` / `patch_size` **不存**：它们就在暂存区开头那 48 B 信封里，而 START
  阶段本来就要逐字段比对来帧信封与暂存信封（判定续传）。再存一份只会在两边不一致
  时制造歧义。

**页擦除的时机**：接收期间每个 DATA 帧都要提交一次进度，而页擦除有一个"整页元数据
同时消失"的窗口。`fota_meta_reserve(slots_needed)` 把擦除**挪到传输开始之前**
（此时还没有值得保留的进度），于是传输途中不会再触发擦除。否则一次恰好落在擦除
窗口里的掉电，会把已经收了几百 KB 的断点信息整块丢掉。

#### 所有权：引导器只读，App 读写

| 角色 | 权限 | 说明 |
|---|---|---|
| 引导器 | **只读**（`FOTA_META_READ_ONLY`） | 读最新记录 → 决定启动哪个槽 → 跳转。不写 Flash ⇒ 不需要 HAL、不需要 tick 时基、不需要考虑"擦写中途掉电" |
| App | 读 + 写 | 传输期每帧提交进度；应用后提交 `DONE`；**启动后消费已生效的记录** |

**为什么"消费"必须由 App 做**：引导器没有清记录的能力，所以它只能在
**目标槽通过 CRC** 时才切过去 —— 没有这个闸门，"切过去 → CRC 失败 → 退回旧槽 →
再切过去"就是死循环。有了闸门，最坏情况只是每次复位多验一次目标槽的 CRC，设备
始终跑在能跑的那一版上。而"消费"这件事等 App 跑起来做刚刚好：App 能确认
"记录指向的槽 == 我正在运行的槽"，这才叫"升级真的生效了"。

反向的情形同样重要：若记录说 `DONE` 指向 B，而 App 跑在 A 上（说明新固件没能起来，
被 CRC 或 boot_nvm 的失败计数退了回来），App 必须把记录**改写成 `ERROR`**，
而不是留着 `DONE` —— 留 `DONE` 会让引导器每次复位都重新去试那个起不来的槽，
失效保护被反复推翻。

#### 跨复位的状态映射（`fota_init`）

记录里存的是**上一次掉电时的处境**，而运行期状态是**这一秒能做/该做什么**。
两者不是一一对应，`fota_init()` 负责翻译。这张表是设计的核心，也是最容易
"看起来没问题"的地方（改错了以后，症状是"某次掉电之后补丁被静默擦掉重传"，
而不是任何一条报错）：

| 记录状态 | 运行期状态 | 动作 | 依据 |
|---|---|---|---|
| 无（空页 / 整页校验不过） | `IDLE` | 不写 Flash | "没有记录"本身就是明确的初始状态；**不可信时不解释它** |
| `RECEIVING` | `IDLE` | 保留记录与暂存区 | 运行期上下文（`g_env`）要从 START 帧重建。主机重发 START 后，身份一致 ⇒ 续传（ACK 带断点序号） |
| `READY` | `READY` | **从暂存区头部读回信封重建上下文** | 记录的全部价值就是"整条补丁已通过 CRC32 并落盘"。退回 IDLE 会让下一次 START 走**完整重传**，把已校验通过的补丁擦掉重收 |
| `READY`（重建失败） | `IDLE` | 记录改写成 `ERROR` | 信封读不回来 / 与记录的 `staged` 对不上 ⇒ 暂存区身份不可信，**不能**报 READY（apply 会拿它去擦掉整个目标槽） |
| `DONE` 且 `slot == 活动槽` | `DONE` | 消费（追加一条 `IDLE`） | 升级真的生效了；引导器只读，消费只能由 App 做 |
| `DONE` 且 `slot != 活动槽` | `ERROR` | 记录改写成 `ERROR` | 新槽起不来、已回退。留 `DONE` 会让引导器每次复位重新去试那个坏槽 |
| `APPLYING` | `IDLE` | 不写 Flash | 落在 `default` 分支：**上电不解释半截状态**，否则设备刚上电就开始擦 Flash |
| `ERROR` | `ERROR` | 不写 Flash | 是"上一次尝试失败"的记录，不是"设备坏了"。`fota recv` 仍然可用 |

两条由此确定的性质：

* **恢复的是状态，不是动作**：`READY` 恢复后**不**自动开始应用
  （不置 `g_apply_requested`）。`fota_init()` 每次上电都会跑，而"刚上电就自己
  擦掉一个槽"是这张表反复避免的事 —— 何时应用由操作员决定（`fota apply`），
  状态本身是可见的（`fota status` 会显示 `READY` 与目标槽）。
* **"拒绝进入接收"的判据落在持久事实上**：`READY` / `DONE` 下 `fota_receive_begin`
  必须拒绝（允许进入 = 允许静默丢弃一条已校验通过的补丁），而要这么做得先显式
  `fota erase` 或 `fota apply`。这个判据只有真的把 `READY` 恢复起来才成立 ——
  若退回 `IDLE`，跨复位后就再也没有东西拦得住它。

覆盖：生成工程侧的
`test_fota_protocol.c::test_init_restores_ready_so_the_patch_can_still_be_applied`
（含"不得自动 apply"与"必须拒绝接收"）与两个保守分支
（`..._when_the_staging_envelope_is_unreadable` /
`..._when_the_record_does_not_match_the_envelope`）；端到端（**真**信封解析器、
收齐后只调 `fota_init()` 模拟复位）由 L5 台架的 `case 9` 覆盖，并有变异用例
`test_l5_bench_detects_dropping_the_ready_restore` 证明该用例真的会红。

#### 与链接脚本的契约

引导器代码区 = `[0x08000000, 页基址)`，链接脚本的 `FLASH` 区域 `LENGTH` 就是
"区域大小 - 一页"，并带一条可读的 `ASSERT`。**这条约束是必须的**：若 `LENGTH`
仍写 `size_kb*1024`，引导器代码涨到最后一页时链接器会痛快地把它放进去，而运行期
`fota_meta_append()` 擦页时擦掉的正是**引导器正在执行的代码** —— 症状是"升级到
一半设备再也不启动"，且只在代码恰好涨过一页时出现。
实测引导器约 1.6 KB / 8 KB，余量充足。


### 9.2 启动决策表（重写 `boot_main` 阶段 4/4.5/5/6）

现有逻辑（`boot_main.c.j2:144-233`）是"失败了就换槽"，缺少"换槽前先确认对面的
槽是有效的"这一步，也没有"版本更高才切"与"回滚"的清晰分层。改为决策表实现：

| A 有效 | B 有效 | pending | 其它条件 | 决策 |
|:---:|:---:|:---:|---|---|
| ✓ | ✗ | — | — | 启动 A |
| ✗ | ✓ | — | — | 启动 B |
| ✓ | ✓ | B | B.pending_attempts ≤ max_retries | 启动 B（尝试计数 +1，清 `boot_ok`） |
| ✓ | ✓ | B | B.pending_attempts > max_retries | **回滚**：清 pending、启动 A |
| ✓ | ✓ | — | — | 启动**版本号更高**者（相同则 A） |
| ✓ | ✓ | A | — | 对称处理（A 为待验证槽） |
| ✗ | ✗ | — | — | Recovery：LED SOS + 串口 DFU 等待（不无限复位） |
| 元数据非法（CRC 不过） | — | — | — | 视为 pending=无，走"两槽都校验"分支 |

要点：

1. **换槽前必须验证目标槽**（现有实现在阶段 6 无条件 `swap` 后复位，
   若对面也是空的就会来回切）。上表把这一步前置。
2. **回滚不依赖 `pending` 被清干净**：只要 `pending_attempts > max_retries`
   就回滚，即使元数据部分损坏也能通过 `BKP9R` 校验发现并走保守分支。
3. **进入 Recovery 而非死循环**：现有 `led_error_blink()` 是 `for(;;)` 死循环
   （`boot_main.c.j2:42`）。对现场设备，应改为"SOS 闪烁 + 串口等待 DFU"，
   否则唯一的恢复手段是 SWD。
4. `boot_read_fw_version()` 必须修（A8）：版本号应来自**镜像头部显式字段**，
   而不是 `magic+4` 的巧合。建议把头部扩为 16 字节：
   `[image_size][crc32][magic][fw_version]`，并让链接脚本 `app_slot_*.ld.j2`
   真的预留 16 字节（4 个 `LONG`），`patch_crc.py` 直接回填**不再插入**（顺带消除 B1）。
   这是修 A8 与 B1 的同一个动作。

---

## 10. 传输协议

### 10.1 报文

现有分片格式（`drv_fota.c.j2:229-247`）为 `seq(2) | len(2) | data | crc16(2)`，
设计基本合理，保留并补充三个阶段：

| 阶段 | 报文 | 携带 | 设备侧动作 |
|---|---|---|---|
| START | `[0xA5][magic][hdr32][hdr_crc16]` | 补丁头 32 B | 校验头、`old_crc32` 比对、容量准入、写元数据 `RECEIVING` |
| DATA | `seq(2) | len(2) | data | crc16(2)` | ≤ `FOTA_CHUNK_SIZE` | 写尾仓 / 喂解码器；按序 ACK、乱序重 ACK、CRC 错 NAK |
| FINISH | `[0xA6][patch_crc32]` | 全量 CRC | 比对 `patch_crc32`；通过 → `APPLYING` 并启动应用任务 |

补充要求：

- **丢包与重复**：`seq != expected_seq` 时重发上一次的 ACK（现有实现有此意，
  但 `expected_seq` 在 NAK 分支里算 `expected_seq - 1` 的语义要写进协议文档 + 单测）。
- **窗口**：115200 bps 下 1 KB 分片耗时 ≈ 89 ms，停等协议的往返开销尚可接受；
  若后续提高波特率或换 USB CDC，再加滑动窗口（协议已留 `flags` 位）。
- **超时**：`ACK_TIMEOUT_MS = 2000` 保留；超时后置 `ERROR` 但**不清元数据**，
  允许主机重发 FINISH 或从头重来。

### 10.2 与现有 `fota_sender.py` 的关系

`generator/fota_sender.py` 可作为主机侧发送端复用，但需：
1. 增加 START 帧（携带补丁头）与 FINISH 帧；
2. 增加 `--resume`（尾仓模式下从设备上报的偏移继续）；
3. 与 `hw2c-delta` 共用头部构造代码（避免两侧各写一份 32 字节打包逻辑 ——
   这正是 A3 类缺陷的温床）。

---

## 11. 生成器与模板集成

### 11.1 改动清单

| 文件 | 改动 | 对应问题 |
|---|---|---|
| `templates/linker/app_slot_{a,b}.ld.j2` | `.app_header` 扩为 16 B（4×`LONG`），含 `fw_version` 槽位 | A8 / B1 |
| `generator/patch_crc.py` | 改为**回填**而非插入；与 16 B 头部对齐 | A8 / B1 |
| `templates/bootloader/boot_crc.c.j2` | 头部按固定偏移解析；`boot_read_fw_version` 读显式字段 | A8 |
| `templates/bootloader/boot_main.c.j2` | 按 §9.2 决策表重写阶段 4/4.5/5/6；Recovery 改为等待 DFU | 决策完整性 |
| `templates/bootloader/boot_nvm.{c,h}.j2` | 新增 `BKP5R..BKP9R` 字段与自校验 | §9.1 |
| `templates/drivers/drv_fota.{c,h}.j2` | 接收落尾仓；START/FINISH 帧；状态机与元数据重写 | A6 / A7 |
| `templates/drivers/fota_delta.{c,h}.j2`（**新**，替代 `fota_bspatch.*`） | H2CD 流式解码器，纯算法 + 可注入 I/O | A1 / A2 / A3 / A5 |
| `generator/delta_tool.py`（**新**，替代 `bsdiff_tool.py`） | 滚动哈希差分 + H2CD 打包 + repack + 主机 apply | B3 / A4 / A5 |
| `templates/test/test_fota_delta.c.j2`（**新**） | 真实断言：多组 old/new 对逐字节比对 | A9 |
| `templates/test/test_fota_protocol.c.j2` | 补齐丢包/乱序/重复/CRC 错/超时用例 | A9 |
| `examples/fota_demo/`（**新**） | 唯一开启 `bootloader.enabled: true` 的示例 | 见 §1.2 |
| `generator/tests/test_delta_tool.py`（**新**） | 主机侧差分/解码的 pytest | G6 |
| `docs/requirements.md` | FR-14 状态由 ✅ 改 ⏳；补 FR-14.5..FR-14.9 | §1.2 |

### 11.2 模板化要点（hw2c 特有约束）

- **补丁格式常量必须由生成器统一产出**，而不是在 C 模板与 Python 工具里各写一份。
  建议放 `generator/data/fota_format.json`，C 头模板与 Python 工具都从它读 ——
  否则 A3 那类"两侧头部布局不一致"必然复发。
- **YAML 新增字段**（`hardware.yaml` 的 `bootloader:` 下）：
  `slot_size_kb`、`patch_staging`（`tail` / `partition`）、`delta_lzss`（bool）、
  `delta_block_size`。全部带默认值，保持"老 YAML 不改也能生成"（验收标准 §7.4）。
- 遵守既有约束：**供应商源码只读**。本规划不触碰 `static/`；Flash 操作全部经 HAL
  公开 API（`HAL_FLASHEx_Erase` / `HAL_FLASH_Program`），无生成期补丁。
- 解码器**不得**因 `TEST` 构建而被空桩化（A9 的根因）。做法是 §5.1 的 I/O 注入：
  主机侧提供一个**内存 Flash 后端**（可正确读写、可注入擦除失败/掉电），
  而不是把函数换成 no-op。

### 11.3 任务与接线

FOTA 目前没有独立任务，靠 `fota_process()` 被轮询（调用点需确认）。建议：

- 接收用 UART 中断/DMA，**应用阶段放进一个专用低优先级任务**并临时提升
  该任务的看门狗喂狗责任；
- 应用前**进入安全态**：调用 `hw2c_fault` 侧的"输出归零"原语，
  确保 PWM/继电器/电磁阀全部断电后再擦写 Flash。

  > 对 `solenoid_valve_pid_ctrl_demo` 与 `thermo_pid_ctrl_demo` 这类带执行器的
  > 工程，**"升级期间执行器保持通电"是安全事故，不只是功能缺陷**。
  > 引导前必须有明确的安全态约定，并在 SIL 中把它测出来。

---

## 12. 测试计划

分层设计，每层都要能在 CI 里跑（HIL 除外）：

| 层 | 对象 | 用例要点 |
|---|---|---|
| L1 纯算法 | `delta_decode.c`（主机编译） | old/new 对：完全相同 / 完全不同 / new 更小 / new 更大 / 空 old / 含长重复块 / 长度非 4 字节对齐 / 大量 `0xFF` / 大量 `0x00` |
| L2 端到端一致性 | `hw2c-delta build` + `apply` | 随机与真实镜像对，`apply(old, build(old,new)) == new` **逐字节** |
| L3 repack 相容 | `hw2c-delta repack` | 用参考 `bsdiff` 产出标准补丁 → repack → apply → 逐字节等于 new |
| L4 头部与准入 | 解码器 INIT 阶段 | `old_crc32` 不匹配 → 拒绝；`new_size` 超槽 → 拒绝；`hdr_crc16` 错 → 拒绝；`flags` 未知位 → 拒绝；**以上任一情况都不得产生任何 Flash 写** |
| L5 协议 | `drv_fota` 状态机 | 丢包 / 乱序 / 重复 / CRC 错 / 超时 / START 重发 / FINISH 在不完整时到达 |
| L6 掉电注入 | 内存后端 | 在**每条指令边界**模拟掉电（截断补丁 / 写一半），断言：① 活动槽内容恒不变；② 目标槽头部 magic 恒无效；③ 重放后结果正确 |
| L7 SIL | 组件级仿真 | 虚拟串口 + 内存 Flash 跑完整 START→FINISH→APPLY→VERIFY |
| L8 HIL | 真板（已有 pyOCD 通路） | `fota_demo`：A→B 升级 → 校验启动 → 注入无效新镜像 → 断言自动回滚；记录 patch 比率与 apply 耗时 |
| L9 体积/时间预算 | 统计 | patch/新镜像 比率、apply 耗时、峰值 RAM（读 map + `.noinit`/栈水位） |

**L6 是本规划里性价比最高的一层**：掉电安全是差分 OTA 唯一真正危险的地方，
而它恰好可以在主机上完整覆盖（只要解码器不依赖真 Flash）。这也是 §5.1 分层
设计的直接回报。

HIL 复用既有通路（`pyocd flash` + COM4 抓串口，见 `AGENTS.md`），
注意既有教训：**先开串口监听再复位**、`pyocd` 必须在沙箱外、烧录后补一次
`pyocd reset` 才能抓全启动日志。

---

## 13. 分阶段路线图

| 阶段 | 内容 | 出口判据 |
|---|---|---|
| **P0** 现状纠正 | `requirements.md` 的 FR-14 状态改为 ⏳；复核 B1（`patch_crc.py` 4 字节插入）；头部布局定稿 16 B | 文档与实现一致；B1 有明确结论 |
| **P1** 算法与主机工具 | H2CD 格式定稿；`delta_tool.py`（build/repack/apply/info）；L1–L4 主机测试 | `apply(build(old,new)) == new` 全绿；含真实镜像对 |
| **P2** 设备侧解码器 | `fota_delta.{c,h}.j2`（I/O 注入、页缓冲、CRC 旁路、喂狗）；内存后端；L6 掉电注入 | L6 全绿；峰值 RAM ≤ 8 KB |
| **P3** 传输与元数据 | START/FINISH 帧；尾仓暂存；`BKP5R..BKP9R`；L5 | L5 全绿；掉电后免重传可续 |
| **P4** 引导决策重写 | §9.2 决策表；16 B 头部；Recovery 等待 DFU；`boot_read_fw_version` 修复 | 决策表 8 行逐行有测试；L7 全绿 |
| **P5** 示例与端到端 | `examples/fota_demo/`；生成期输出 slotA/slotB 两份 bin；L8 HIL | 真板升级 + 回滚成功；CI 覆盖 L1–L7 |
| **P6** 可选增强 | ~~LZSS（flags bit0）；滑动窗口~~（**已被 §16.6 修订**：改为 vendored tinyuz 且默认开）；Ed25519 签名（`auth_len`） | 按需 |

**P1 与 P2 可以并行**：两者之间只有 H2CD 格式这一个接口，且主机侧 `apply`
本就用设备侧源码，接口一旦冻结即可并行。

---

## 14. 验收标准

1. `apply(build(old, new), old) == new` 对**全部 8 个示例的相邻版本对**逐字节成立。
2. 解码器峰值 RAM ≤ 8 KB（v1 ≤ 2.5 KB），且与镜像/补丁大小无关 —— 用
   10 KB 与 200 KB 两组输入验证 RAM 占用不变。
3. 掉电注入：在每条指令边界截断，活动槽 CRC 恒有效、目标槽头部恒无效、重放后正确。
4. 单函数级改动的 patch ≤ 新镜像的 15%；中等改动 ≤ 40%（用真实示例对度量并记录）。
5. 引导决策表 §9.2 的 8 行**逐行**有对应测试。
6. `examples/fota_demo` 在真板上完成一次 A→B 升级并成功启动，随后注入无效新镜像
   并观测到自动回滚。
7. `git diff --stat -- static/` 为空（供应商源码未被触碰）。
8. 新增/修改的 Python 测试与主机测试全部纳入既有 CI 三个 job。

---

## 15. 风险与开放问题

| # | 风险 / 问题 | 影响 | 处置 |
|---|---|---|---|
| R1 | `patch_crc.py` 的 4 字节插入（B1）若确为缺陷，则历史上所有"带 bootloader 的产物"都不成立 | 高 | P0 阶段先复核；16 B 头部改造顺带消除 |
| R2 | 跨槽基址差导致差分率不达预期 | 中 | P1 用真实 slotA/slotB 对**实测**比率，不靠估算；必要时改对称槽（§7.3） |
| R3 | 尾仓暂存与镜像生长的碰撞 | 中 | START 阶段准入校验 + 碰撞即拒绝；保留独立暂存分区作为逃生路线 |
| R4 | ~~LZSS 解码器引入只在真机暴露的 bug~~（**已修订**：自研 LZSS 取消，改用 vendored tinyuz 解码器；风险转为"**我们的编码器产出的流不可解**"，由 §16.8.3 的 oracle 逐字节判据覆盖） | 中 | 压缩**默认开**（裁决 2）⇒ L1/L6/L7 必须全部覆盖压缩路径，且须断言解压路径**真的被执行**（§14 增量第 6 条） |
| R5 | IWDG 5 s 超时 vs 大镜像擦写耗时 | 中 | 明确喂狗点 + HIL 实测 apply 耗时；必要时调整 `wdg_timeout_ms` |
| R6 | 升级期间执行器带电 | **高（安全）** | §11.3 强制安全态；SIL 用例覆盖 |
| R7 | 无签名 → 补丁可被伪造（CRC32 只防误码） | 中 | 明确列为非目标；格式预留 `auth_len` |
| R8 | 元数据依赖备份域，VDD 掉电即丢失 | 低 | 掉电语义本就假设"从头重放"，不依赖元数据存活 |

### 待确认的开放问题

1. **传输通道是否只用 UART**？若规划 USB CDC / CAN，协议的窗口与分片长度需重定。
2. **是否需要保留"标准 BSDIFF40"作为对外格式**？（决定是否必须长期维护 `repack`）
3. **是否接受 v1 的"掉电重传"**，还是直接做尾仓免重传？
4. **是否需要签名**、以及可接受的验证耗时预算（Ed25519 在 16 MHz M0+ 上约几十 ms）。
5. 差分生成是否需要支持**跨多个历史版本**（跳到任意版本），还是只支持相邻版本对？

---

## 16. 与 HPatchLite 的对比与路线选择

> 本节修订 §3–§6 的算法选型。结论：**自研 H2CD 格式不值得做，改用 HPatchLite
> 作为设备侧解码内核**；本规划的信封、暂存、引导决策与测试计划全部保留。

### 16.1 核查范围与方法

| 项 | 内容 |
|---|---|
| 版本 | HPatchLite **v1.0.2**（MIT，作者 HouSisong，即 HDiffPatch 作者） |
| 设备侧 | 通读 `HDiffPatch/libHDiffPatch/HPatchLite/` 全部 4 个文件 |
| 主机侧 | `hdiffi`（生成）/ `hpatchi`（应用）命令行 |
| 依据 | 上游 `master` 分支源码原文 + 官方 README，非二手描述 |

设备侧源码就是 **4 个文件、共 23,041 B**：

| 文件 | 大小 | git blob sha1（`master` 快照） |
|---|---|---|
| `hpatch_lite.c` | 14,884 B | `b8dfded5a45501301c2aec6a5a3126ce25b378d1` |
| `hpatch_lite.h` | 4,539 B | `65d3d18e8033b151ef94629a210312beb6e234a0` |
| `hpatch_lite_input_cache.h` | 808 B | `aaa382925715e89cc53d5399c0cc81197f42f98c` |
| `hpatch_lite_types.h` | 2,810 B | `536a1f8610878ff04de460f5a3eceb948c2e88cf` |

> ⚠️ 上表的哈希是**读取时 `master` 分支快照的 git blob sha1**，用于标识我实际读过的
> 那一版，**不等于 v1.0.2 标签的内容**。落地时的钉版本依据应当是 **release tag `v1.0.2`**，
> 并在纳入时重新记录该 tag 下的 blob sha1（§16.6 验收项第 3 条）。

### 16.2 机制对照：这是同一套执行模型

**必须先承认的结论**：§3 提出的"性质 P1（保序）"不是本规划的新发现 ——
HPatchLite 的源码版权年从 2020 起（`Copyright (c) 2020-2022 HouSisong`），
也就是说这套执行模型在 2020 年就已实现。逐条对照如下，右列均为源码事实。

| 维度 | 本规划 H2CD v1（§3–§5） | HPatchLite v1.0.2 | 判定 |
|---|---|---|---|
| 补丁流消费 | 严格单向，不回跳 | `_hpi_cache_update()` 只从 `cache_buf` 头部重填，`_cache_read_1byte()` 只递增 `cache_begin` | **等价** |
| 旧数据访问 | `old_read(ctx, off, dst, len)` 随机 | `read_old(listener, read_from_pos, …)` 随机 | **等价** |
| 新数据写出 | `new_write(ctx, off, src, len)` 顺序 | `write_new(listener, data, size)` 顺序 | **等价** |
| ADD | 显式 opcode | cover 之间的**间隙**：`_patch_copy_diff()` 直接从补丁流拷字节写出 | **等价** |
| COPY | 显式 opcode | cover 的 `tag` bit7 = 1（`isNotNeedSubDiff`），不消费差值字节 | **等价** |
| CDIFF | `out[i] = old[i] + lit[i]` | 同：`addData()` = `*dst++ += *src++`，由 `tag` bit7 = 0 触发 | **等价** |
| SEEK | `delta:ivarint` 可正可负 | `cover_oldPos` 相对上一 cover 末尾，`tag` bit6 选正向/回退 | **等价** |
| END | 显式终止指令 | `coverCount` 耗尽即止 | **等价** |
| 变长整数 | LEB128 | 同族 `v=(v<<7)\|(b&127)`，续接标志 `b>>7`（MSB 优先） | 同族 |
| 控制与字面量布局 | ctrl 独立前置流 + literal 流（两游标） | **交错**在同一条流内（单游标） | HPatchLite 更省 |
| 头部 | 32 B（magic / ver / flags / old_size / old_crc32 / new_size / new_crc32 / ctrl_size / hdr_crc16 / auth_len） | **5–7 B**（见 §16.4） | 见 §16.4 |
| 解码状态量 | 输出游标 + 旧游标 + 指令暂存 | `coverCount` / `newPosBack` / `oldPosBack` + 输入缓存游标 | **等价（均 O(1)）** |

**操作码集是子集关系**：H2CD 的 `ADD` / `COPY` / `CDIFF` / `SEEK` / `END`
在 HPatchLite 中都有一一对应物；只有 `FILL`（填充定长字节）是 H2CD 独有，
但它在 HPatchLite 里用字面量间隙就能表达，**不构成格式差异**。

由此，§4.3 中"变长编码是必须的，不是优化"这一判断**依然成立**（HPatchLite
同样是 varint），但它印证的是 HPatchLite 的正确性，而不是 H2CD 的独创性。
§4.4 的 `repack`（把标准 BSDIFF40 重打成保序格式）同样是多余的 ——
HPatchLite 的 `hdiffi` 直接产出保序补丁，不需要中转格式。

### 16.3 双方各自独有的能力

#### HPatchLite 有、本规划没有（或只是规划）

| 能力 | 事实 | 对本项目的意义 |
|---|---|---|
| **实测的解码器体积** | 流式 662 B / 原地更新 976 B / 按页原地 1,116 B（Mbed Studio 编译）。同基准下 HDiffPatch 自家 `patch_single_stream` 是 2,356 B、`patch_decompress_with_cache` 是 2,846 B | 本规划的 §5.4 只有 RAM 预算，**ROM 完全没有估算** |
| **压缩有现成方案 —— 但不在 HPatchLite 里** | `-c-tuz`(tinyuz) / `-c-tuzi` / `-c-zlib` / `-c-pzlib` / `-c-lzma` / `-c-lzma2`，压缩类型由补丁头的 `compress_type` 字节（`hpi_compressType_tuz = 1`）携带。⚠️ **但这一层不在 HPatchLite 内**：`hpatch_lite.c` 全文（14,884 B）**不含** tinyuz 引用、**不**按 `compress_type` 分派、**不**调用任何解压函数 —— `hpatch_lite_open()` 只把 `buf[2]` 原样填进 `*out_compress_type` 交还调用方。解压必须由调用方完成（HDiffPatch 侧是 `patch_decompress_with_cache()`，**不在** HPatchLite 的 4 文件内） | §4.5 的 LZSS 不必自研（tinyuz 更成熟），但**压缩这一层要我们自己接**（§16.8.1）—— 见 C9；且我"不推荐 DEFLATE"的论证在 tinyuz 这个专为 MCU 写的选项面前不成立 |
| **原地更新** | `hpatchi_inplaceB()` / `_by_page()`，只需 `extraSafeSize` 额外字节即可原地打补丁，**不需要第二个槽，也不需要尾仓暂存** | §2.2 已列为非目标，**裁决 3 进一步升为永久排除**（失败不可恢复，对带执行器的工程不可接受，见 C7）⇒ 这项能力**对我们不存在**，集成面里也不再出现 |
| **成熟的差分器** | 后缀数组匹配 + `matchScore` 调参 + `-cache` + `-p-4` 多线程；HPatchLite 自身已发到 v1.0.2，上游 HDiffPatch 主线在持续发版（二手材料提到 v4.12.x 系列，**未独立核实**） | ⚠️ 这项能力位于 `HDiff/Diff` 模块，**不在 HPatchLite 的 4 文件内**，**裁决 1 明确不使用** ⇒ 只借鉴思路、实现仍自研：§6.2 的滚动哈希块匹配从"可选方案"变成**必做项**，`bsdiff_tool.py` 的后缀数组（B3）问题也由我们自己收掉 |
| **交付与生态** | Win/Linux/macOS 预编译 release、Mbed Studio 集成、Android NDK 构建、CI 徽章 | **裁决 1 之后大部分用不上** —— 预编译 release 与构建系统针对的是 `hdiffi`（写侧），我们不再使用；对设备侧只剩"Mbed Studio 集成"这一条参考价值 |

#### 本规划有、HPatchLite 没有

| 能力 | 说明 |
|---|---|
| **完整性 + 版本信封** | §16.4 详述。HPatchLite 头部**没有任何** `old_size` / `old_crc32` / `new_crc32` / 头校验 |
| **A/B + 尾仓暂存的掉电语义** | §7。HPatchLite 对非原地模式的掉电语义**不表态**，对原地模式明确警告"失败可能损坏且无法恢复" |
| **hw2c 集成面** | §8 跨槽基址的差分对齐、§9 启动决策表、§11 模板接线、§12 测试计划（尤其 L6 掉电注入） |
| **§1.2 的 9 项现存缺陷** | **这才是本规划的主要产出**，且 A3/A6/A7/A8/A9 **与解码器选型完全无关** —— 换掉解码器一个也不会自动修好 |

### 16.4 HPatchLite 的三处具体缺口 —— 本规划信封的价值所在

以下三条都是从源码读出的，不是推测。它们共同解释了**为什么 §4.2 的 32 B 信封
必须存在**，也解释了社区实践里为什么人人都在它外面再套一层。

**(1) 头部不含任何校验字段。** 实际布局（`hpatch_lite.h` 与 `hpatch_lite_open()`）：

```text
偏移 0    'h'
偏移 1    'I'
偏移 2    compressType
偏移 3    [7:6] versionCode(=1 流式 / =2 原地) | [5:3] uncompressSize 字节宽 | [2:0] newSize 字节宽
偏移 4    newSize         (1–4 B, 小端，宽度由偏移 3 给出)
   …      uncompressSize  (0–4 B, 小端；未压缩时宽度为 0，不占字节)
```

`hpi_kHeadSize = 2+1+1 = 4`。对一份 108 KB 的固件：`newSize` 需 3 字节、
未压缩时 `uncompressSize` 宽 0 → **补丁头共 7 字节**。

**没有 old_size、没有 old_crc32、没有 new_crc32、没有头校验、没有 4 字节魔数。**
后果很具体：设备**无法在擦除任何一页之前**判定"这份补丁是不是给当前这一版的"，
也无法在写完后自证输出正确。HPatchLite 的立场是"patcher 不管 OTA 策略"，
只回传 `newSize`。这是设计上的分工选择，不是疏漏 —— **但这一层必须由使用方补上。**

**(2) 结束校验不足，发现不了截断与尾部垃圾。** `hpatch_lite_patch()` 的返回：

```c
return (newSize==newPosBack)&_cache_success_finish(&diff);
```

而 `_cache_success_finish()` 的定义是：

```c
static hpi_force_inline
hpi_BOOL _hpi_cache_success_finish(const _TInputCache* self){ return (self->cache_end!=0); }
```

它**只断言"输入缓存非空"**，并不校验补丁流被恰好消费完。也就是说：补丁被截断、
或在末尾多出一段垃圾字节，在解码器层面**察觉不到**。加上 (1) 里没有 `new_crc32`，
设备侧无法仅凭 HPatchLite 区分"一份好补丁"和"一份被篡改/截断的补丁"。
（另注：这里用的是位与 `&` 而非 `&&`，作者在 `hpatch_lite_open()` 的注释里说明
是有意为之，为性能放弃短路求值。）

**(3) 省字节的开关会连带关掉唯一的安全校验。** `_IS_RUN_MEM_SAFE_CHECK` 默认为 1；
置 0 可省 48–80 B，但它编译掉的是：

- `hpatch_lite_open()` 中的 `(lenn==4) & (buf[0]=='h') & (buf[1]=='I') & ((lenu>>6)==1)`
  —— **连 2 字节 `"hI"` 魔数与版本号校验一起消失**，以及宽度上限检查；
- `hpatch_lite_patch()` 中的 `temp_cache_size>=hpi_kMinCacheSize`、
  `cover_newPos>=newPosBack`、`cover_length>0`。

**结论：我们的构建必须保持 `_IS_RUN_MEM_SAFE_CHECK=1`**，并把它写进集成验收项
（§16.6 第 6 条）—— 因为信封在 HPatchLite 之外，里面这层校验是唯一的内层防线。

> 这三条合起来就是 §4.2 那句"设备侧在烧写任何一页之前就能判定这份补丁是不是
> 给当前这版的"的全部理由。它与 HPatchLite **不冲突、可叠加**：
> 信封打包在外层，`hpatch_lite_patch()` 在内层，两者职责正交。

### 16.5 集成代价与风险

| # | 事项 | 事实 / 处置 |
|---|---|---|
| C1 | ~~主机侧引入编译依赖~~ **（已因裁决 1 作废）** | 原方案要构建 `hdiffi`，其依赖是**嵌套 submodule**（HDiffPatch + tinyuz + lzma + zlib），与"生成器纯 Python、零编译依赖"的现状冲突。**裁决 1 选了"只借解码器、自研 Python 差分侧"，`hdiffi` 不进依赖树** → 本条消失，风险转移到 C1' |
| C1' | **写侧全自研，且无 C 参考实现** | HPatchLite **只发布解码器**（4 文件、23,041 B、纯 C、无子目录），**不含写侧**。因此下列三件都要自己写：① 差分（块匹配，§6.2）② 补丁流编码（coverCount / cover / tag 位 / varint）③ tinyuz 编码（裁决 2）。**唯一权威是解码器源码本身**，上游没有格式规范文档。缓解见 §16.8.3：主机侧用 vendored 解码器编一个 oracle，把"我们的流被逐字节还原 == new"当作硬判据 |
| C2 | **落地位置** | 仓库既有惯例是 **vendored 明文 + 保留 LICENSE**（`static/third_party/lwrb`、`static/unity/LICENSE.txt`），全仓**只有** `FreeRTOS-Kernel` 是 submodule。→ 放 `static/third_party/hpatch_lite/`（4 文件 + `LICENSE`），与惯例一致，无需新增 submodule |
| C3 | **供应商只读约束** | 需在 `AGENTS.md` 的只读清单里显式加入 `static/third_party/hpatch_lite/`，与 HAL/CMSIS/FreeRTOS 同级。好处：解码器不再是 `.j2`，**从结构上消除"A9 那类模板从未被渲染"的风险**，且它本就与 MCU 无关、不该被模板化 |
| C4 | **严格告警面** | `hpi_fast_uint8` 被定义为 `unsigned int`，`_cache_read_1byte()` 也返回它 —— 在本项目的 `-Wsign-conversion` / `-Wconversion` / `-Wundef` 严格集下**大概率有告警**。⚠️ 必须**实测**统计，不得凭猜。按既有基线做法（`^src/` 过滤）它落在 vendor 侧、**不计入 538 条基线**，但静态分析/MISRA 报告里会出现，需在报告中标注 vendor 边界 |
| C5 | `-fshort-enums` 与 `compressType` | 本目标默认 `-fshort-enums`。`hpi_compressType` 有 10 个成员（0–9），仍为 1 字节，`*out_compress_type = buf[2]` 是逐字节读写 → **已核对，安全**。之所以要写明，是因为这是该类 ABI 坑的高发点 |
| C6 | 整数宽度 | `hpi_pos_t` / `hpi_size_t` 均为 `unsigned int`（32 位）→ 对 248/256 KB 槽位毫无压力 |
| C7 | **原地更新 —— 已永久排除**（裁决 3） | 原地更新要一边读旧数据一边覆盖同一区域，而 NOR 写前必须按页擦除（`hpatchi_inplaceB_by_page()` 即为此设，`pageSize` 计入 `temp_cache`）。**失败不可恢复**（README 原文警告：「原地更新失败，旧文件可能会被损坏且无法恢复」），对 `solenoid` / `thermo` 这类带执行器的工程不可接受。**裁决 3 = 永久排除，不作为配置项**。落地后果：只编 `hpatch_lite_patch()` 一个入口，`hpatchi_inplace_open()` / `hpatchi_inplaceB*()` / `hpi_kInplaceHeadSize` / `extraSafeSize` / `pageSize` 全部不进集成面；§2.2 相应条目由"本期不做"升为"永不做"。附带收益：上游 `hpi_kMinInplaceCacheSize`（`types.h` 里拼作 `hpi_kMinInlpaceCacheSize`，与实际使用处不一致）这类问题永久不入我们的责任范围 |
| C8 | **压缩侧有两个"必须显式对齐"的开关** | ① 命令行默认值：README 顶层 `-c-compressType` 写"默认不压缩"，而 tinyuz 一节又写"`-c-tuz` 是默认的压缩器"（前者指命令行默认、后者指可选压缩器中的默认），极易误读。② **更危险的是编译期宏**：tinyuz README 明确写「如果编译了解压缩器的源代码，并设置 `tuz_isNeedLiteralLine=0`，那么必须使用 `-ci` 压缩器」—— 即**编码器形态必须与设备侧解码器的编译开关匹配**，配错不是"压缩率变差"而是**流根本无法解码**。⇒ 我们的 Python 编码器必须显式记录它按哪种开关生成，并在集成验收里断言 `compress_type` 与设备侧实际解压路径一致（§14 增量第 6 条） |
| C9 | **HPatchLite 不含压缩层** | `hpatch_lite.c` 全文（14,884 B）**没有** include tinyuz、**没有**按 `compress_type` 分派、**没有**任何解压调用；`hpatch_lite_open()` 只是把 `buf[2]` 原样填进 `*out_compress_type` 交还调用方。⇒ 压缩必须落在**我们自己的 `read_diff` 胶水**里（§16.8.1）。这既是负担也是自由度：`compress_type` 的语义解释与分派实现都在我们手里，所以**压缩是一个可插拔、可独立验证、也可一行退回的层** |
| C10 | tinyuz 的真实体积与 RAM（**修正 §16.7 第 7 问里的估算**） | ROM：tinyuz 解码器 **626 B**（流式）/ 424 B（内存式，Mbed Studio 基准）—— 不是原先估的"2–3 KB"；与 HPatchLite 流式 662 B 合计 **≈1.29 KB**。RAM：**`dict_size` + `cache_size`**，其中 `dict_size` 由**压缩时写进流内**（`tuz_TStream_read_dict_size()` 读取），**所以是我们的 Python 编码器说了算**，规范允许 ≥1 B。压 `aMCU.bin.diff` 时 dict 从 1 MB 降到 255 B，压缩率只从 5.75% 退到 6.89% ⇒ **差分流对 dict 不敏感，取 1–4 KB 即可把 RAM 压到与 §5.4 预算同量级**。这条实际是**支持**裁决 2 的新证据 |

### 16.6 修订后的路线（对 §11.1 / §13 / §14 的增量）

**改动清单增量（§11.1）**

| 文件 | 原计划 | 修订后 |
|---|---|---|
| `static/third_party/hpatch_lite/` | — | **新增**：4 文件（`hpatch_lite.c` / `.h` / `hpatch_lite_types.h` / `hpatch_lite_input_cache.h`，23,041 B）+ `LICENSE`（MIT），钉 release tag，只读 |
| `static/third_party/tinyuz/decompress/` | — | **新增**（裁决 2）：4 文件（`tuz_dec.c` / `.h` / `tuz_types.h` / `tuz_types_private.h`，24,117 B）+ `LICENSE`（MIT），钉 release tag `v1.1.1`，只读。**只取 `decompress/`，不取 `compress/`** |
| `templates/drivers/fota_delta.{c,h}.j2` | 新写：完整 H2CD 解码器 | **缩为适配层**：信封解析 + Flash 后端（页缓冲 / 擦写 / 喂狗）+ 挂接 `hpatch_lite_patch()`。**新增**：`read_diff` 胶水（压缩透传 / tinyuz 流式解压分派，§16.8.1）—— 这段是**我们的代码**，不是 vendored 的 |
| `generator/delta_tool.py` | 新写：滚动哈希差分 + H2CD 打包 + repack + apply | **仍要自研，且范围比原计划更大**（裁决 1 + 2）：① 差分（块匹配，§6.2）② HPatchLite 补丁流编码器 ③ tinyuz 编码器 ④ 我方信封 ⑤ 主机 `apply`。§4.4 的 `repack` 与标准 BSDIFF40 兼容**取消** |
| `generator/bsdiff_tool.py` | 替换（B3 的后缀数组不可用） | **退役**。⚠️ 注意口径变化：原计划是"由 `hdiffi` 承接"，裁决 1 之后改为**由我们自己的块匹配承接** —— 即 B3 的问题**回归到我们头上**，§6.1 / §6.2 于是从"可选优化"变成**必做项** |
| `templates/drivers/fota_bspatch.{c,h}.j2` | 由 `fota_delta` 替代 | 退役（A1/A2/A5 由 HPatchLite 取代） |
| §4 的 H2CD 格式 | 定稿实现 | **不实现**；§4.2 的 32 B 信封**保留并复用**（去 `ctrl_size`，改为 `patch_size`） |

**验证专用（不属于"主机工具链"，必须单列）** —— 它是一个测试夹具，不是生成器依赖：

| 位置 | 原计划 | 修订后 |
|---|---|---|
| `test/hpatch_oracle/`（建议路径） | — | **新增**：用 vendored 的 8 个文件（HPatchLite 4 + tinyuz 4，纯 C、零第三方依赖）编一个几十行的主机 `apply`，**只做判据用**。生成器本体仍是纯 Python，无编译依赖；oracle 只在测试 / CI 里出现。**这是裁决 1（无 C 参考实现）唯一可靠的正确性锚点**，见 §16.8.3 |

**路线图增量（§13）**：P0 / P3 / P4 / P5 不变，P1 与 P2 重定义为：

| 阶段 | 修订后内容 | 出口判据 |
|---|---|---|
| **P1'** 写侧全自研（裁决 1 + 2） | ① 差分（块匹配，§6.2）② HPatchLite 流编码器 ③ tinyuz 编码器 ④ 32 B 信封 ⑤ 主机 `apply`（oracle）；L2/L3 测试 | `apply(build(old,new)) == new` 全绿（含真实 slotA/slotB 对），**且"不压缩"与"tinyuz"两条路径分别验证**。建议先让不压缩路径（`compress_type=0`）跑到 L2 全绿，再叠加 tinyuz —— 因为压缩是可插拔层（C9），叠加顺序可调、退回代价是一行 |
| **P2'** 设备侧接线 | vendored HPatchLite + vendored tinyuz-dec + `read_diff` 解压胶水 + Flash 后端 + 页缓冲 + CRC 旁路 + 喂狗；L1/L6 | L6 掉电注入全绿；**ROM 与 RAM 均为实测值**；并断言压缩路径**真的被执行**（§14 增量第 6 条 —— 防止"字段写了但没人读"的静默失效，正是 A9 的老毛病） |
| **P6** 可选增强 | ~~LZSS 改为「显式启用 `-c-tuz`」~~（已升为裁决 2 的默认项，从 P6 移除）；剩下：Ed25519 签名走 `auth_len` | 按需 |

**一个由此推出的设计简化（影响 §7.2）**：HPatchLite 的补丁数据是通过
`read_diff` 回调**增量取用**的（头文件原文即 "hpatch_lite **by stream**"），
因此可以**边收边解码**（UART 字节直接喂进环形缓冲，由回调供给），
**完全不需要把补丁先落 Flash**。这意味着：

- §7.1 的"幂等重放 + 掉电重传"直接就是 v1 的自然形态（补丁不持久化），
  且此时**不需要任何暂存分区**；
- §7.2 的尾仓暂存因此降级为**纯可选优化** —— 它的唯一收益是"掉电免重传"，
  代价是要在槽尾预留 `patch_size` 空间并承担碰撞校验（§7.2 的准入条件）。

按 §7.1 已算过的账（20 KB 补丁 @115200 bps ≈ 1.7 s），**建议 v1 就走流式解码、
不做尾仓暂存**，把 §7.2 留到"补丁体积显著变大"或"传输链路昂贵"（如 NB-IoT）时再启用。
集成时需注意：`read_diff` 是阻塞式接口，喂数据的环形缓冲与解码任务之间的
水位控制要做对，否则会死锁或丢字节。

**验收标准增量（§14）**：第 1、3、4、5、7、8 条**不变**。修订与新增：

1. 第 2 条改为：解码器 RAM = HPatchLite `temp_cache`（内部对半分为"旧数据读缓冲"与"补丁输入缓存"）+（裁决 2 生效时）tinyuz 的 `dict_size + cache_size`，**全部实测**，且与镜像/补丁大小无关。⚠️ `hpatch_lite_patch()` 内部做了 `temp_cache_size >>= 1`，两半用途不同，核算预算时必须按"一半"估有效输入缓存。
2. **新增**：解码器 ROM **实测**（文字段增量）。构成已可预估：HPatchLite 流式 662 B + tinyuz 流式 626 B ≈ 1.29 KB（均为上游 Mbed Studio 基准），再加信封解析与胶水 ⇒ **目标 ≤ 1.5 KB，以实测为准**。
3. **新增**：`static/third_party/hpatch_lite/` 与 `static/third_party/tinyuz/decompress/` 的 8 个文件**逐字节等于上游 release tag**（分别钉 HPatchLite tag 与 tinyuz `v1.1.1`），且 `_IS_RUN_MEM_SAFE_CHECK` 未被改为 0（§16.4 第 3 条）、`tuz_isNeedLiteralLine` 的取值与我们的编码器形态一致（C8）。
4. **新增**：`git diff --stat -- static/` 为空 —— 与既有 G7 合并适用。
5. **新增**：C4 的严格告警统计完成（`hpi_fast_uint8` 已确认为 `unsigned int`，在 `-Wsign-conversion` / `-Wconversion` 下大概率告警），vendor 边界在报告中标注。
6. **新增（裁决 2 的专属判据）**：压缩是"可插拔、但必须证明真的接上了"的一层 —— 对同一 old/new 对分别在 `compress_type=0` 与 `compress_type=tuz` 下跑通，并**在设备侧断言解压路径确实被执行**（计数 / 断点均可）。理由：HPatchLite 自己不分派压缩（C9），若我们漏接胶水，`compress_type` 会变成**只被写入、从不被读取**的字段 —— 现象与 A9「mock 让测试失效」同类：测试全绿、功能静默失效。
7. **新增（裁决 1 的专属判据）**：自研编码器产出的流，必须能被 **vendored 解码器组成的 oracle 逐字节还原**（`oracle_apply(old, our_patch) == new`）。这是在没有 C 参考实现的情况下唯一可信的格式正确性判据（§16.8.3）。

### 16.7 待裁决三问：已裁决（2026-09-16）

6. **主机侧 `hdiffi` 怎么来** → **裁决：只借解码器，自研 Python 差分侧**（原选项 c）。
   备选 (a) 官方 release 预编译二进制 / (b) CI 内构建 `hdiffi`，两者都能免掉"写侧自研"，
   但都引入外部制品或编译依赖，且补丁质量仍取决于第三方调参。
   选 (c) 的代价已如实登记为 C1'：**差分 + 补丁流编码 + tinyuz 编码都要自己写**。
   收益是生成器保持纯 Python 零编译依赖，补丁质量与参数完全可控、可回归。
   ⚠️ 一个此前没写明的连带后果：**HPatchLite 根本不发布写侧** ——
   所以"自研"不是"重复造轮子去替代现成的"，而是**没有现成的可用**。
   缓解手段只剩 oracle（§16.8.3）。
7. **`-c-tuz` 默认开还是关** → **裁决：默认开**。
   ⚠️ 与第 6 问**叠加**后，本条的含义从"传一个命令行参数"变成
   "**我们要实现一个 tinyuz 编码器**" —— 因为 `-c-tuz` 是 `hdiffi` 的写侧选项，而写侧没有了。
   另外，决策依据在核查中**被一手数据修正**：原估算"约 2–3 KB ROM + 约 1 KB RAM"偏高 ——
   实测 tinyuz 解码器流式 **626 B**，且 **RAM = `dict_size` + `cache_size`，其中
   `dict_size` 由我们的编码器写进流内、规范允许 ≥1 B**；对差分流而言 dict 取 1–4 KB
   几乎不损压缩率（`aMCU.bin.diff`：dict 1 MB → 5.75%，dict 255 B → 6.89%）。
   ⇒ 这条裁决在**真实**代价下依然成立，见 C10。
8. **inplace 是否永久排除** → **裁决：永久排除**，不留配置项。
   依据：对带执行器的工程失败不可恢复（README 原文警告），而本项目的差异恰恰在于
   "固件带执行器"；Flash 紧张时应优先走 §7.2 尾仓暂存或换更大的 part，
   而不是把一条不可恢复的写路径做成产品开关。落地后果见 C7。

**原 §15 的五个问题仍未裁决**（传输通道是否只走 UART；是否保留标准 BSDIFF40 对外格式；
v1 是否接受掉电重传；是否需要签名；是否需要跨多个历史版本）。
其中"是否保留标准 BSDIFF40 对外格式"在裁决 1 之后**已部分失去意义** ——
§4.4 的 `repack` 已取消（它的原本用途就是"把标准 bsdiff 补丁重打成我方格式"）；
若不再需要与外部 bsdiff 工具链互操作，该问可一并关闭。

### 16.8 三项裁决的落地形态

#### 16.8.1 压缩层的归属：它是**我们的胶水**，不是 vendored 的一部分

这是本轮核查里最容易被误判的一点。`hpatch_lite.c` 全文 14,884 B，**不含任何压缩代码**：
不 include tinyuz、不按 `compress_type` 分派、不调用解压函数。`hpatch_lite_open()` 读到
`buf[2]` 之后只是把它填进 `*out_compress_type` 返回给调用方。

所以"默认开 tinyuz" = **我们写这段解压胶水**，形态大致是：

```text
UART 字节 → 环形缓冲 → read_diff（我们实现）
                          ├─ 补丁头区（未压缩）→ 直通给 hpatch_lite_open()
                          └─ 补丁正文区（tinyuz 流）→ tuz_TStream_decompress_partial()
                                                       → 解压出的字节喂给 hpatch_lite_patch()
```

两个必须由我们决定、也因此**可以独立验证**的点：

- **压缩从哪个偏移开始**。`hpatch_lite_open()` 必须先读到 4 字节头才知道 `compress_type`，
  所以头**不可能**在压缩流里（否则鸡生蛋）。两种候选：
  **(i)** 头明文 + 正文 `uncompressSize` 字节压缩；
  **(ii)** 整个 lite 流（含头）压缩，压缩类型由**我们的 32 B 信封**携带。
  **建议 (i)** —— 代价相当，但 (i) 保留了"将来拿 `hdiffi` 产出的压缩补丁做交叉验证"
  这条后路，而 (ii) 会把这条路焊死。P1' 阶段读一遍 HDiffPatch 侧的
  `patch_decompress_with_cache()` 即可定稿（它不在 HPatchLite 的 4 文件内）。
- **`dict_size` 取多少**：它写在 tinyuz 流内，直接决定设备侧 RAM。
  差分流高度冗余 ⇒ 取 1–4 KB（见 C10）。

#### 16.8.2 收窄后的设备侧集成面（裁决 3 的收益）

| 编进固件 | 不编进固件 |
|---|---|
| `hpatch_lite_open()`、`hpatch_lite_patch()` | `hpatchi_inplace_open()`、`hpatchi_inplaceB()`、`hpatchi_inplaceB_by_page()` |
| `tuz_TStream_open` / `_read_dict_size` / `_decompress_partial`（裁决 2） | 整套 `hpatchi_listener_extra_*` 环形缓存与按页写出 |
| 我方信封解析 + `read_diff` 胶水 + Flash 后端 | `extraSafeSize` / `pageSize` 相关的一切 |

收益不只是 ROM 节省，更重要的是**测试面收窄**：§12 的 L6 掉电注入不必再考虑
"原地更新失败"这一类语义，断言集合可以保持简单
（活动槽不变 / 目标槽头部无效 / 重放后正确）。

#### 16.8.3 没有 C 参考实现时，怎么保证格式是对的

裁决 1 把"格式正确性"的关注点从"第三方工具对不对"换成了"**我们写的编码器对不对**"，
而 HPatchLite 没有格式规范文档、只有解码器源码。判据因此必须换一种方式建立：

1. **oracle 只能是解码器**。把 vendored 的 8 个文件（HPatchLite 4 + tinyuz 4，纯 C、
   零第三方依赖）编成一个主机 `apply`，它就是**事实上的规范**。
2. **判据是逐字节等价，而不是"看起来对"**：`oracle_apply(old, our_patch) == new`。
   这与原 L2 的合同完全一致，只是 `apply` 的实现从"第三方二进制"换成了"我们 vendored 的解码器"。
3. **编码器的自由度必须显式约束**。压缩器的合法输出不唯一 —— 我们的编码器只需满足
   "可被解码器还原"，**不需要**与上游 `tuz_enc` 逐字节一致。这条必须写清楚，
   否则很容易滑进"与参考实现对齐每一个 bit"的无效工作。
4. **优先用最保守的构造**：varint 一律最短编码、tag 位严格按解码器的读取顺序排、
   cover 严格递增（解码器里有 `_SAFE_CHECK(cover_newPos>=newPosBack)`）。
   把编码器的选择空间压到最小，出错面就最小。

> 这是本规划里同一条教训的第三次应用：**"存在"不等于"被执行"**。
> 在没有参考实现时，唯一能替代人眼审查的办法，是**用另一份实现去消费它的输出**。


---

## 17. 实施记录：P0 与 P1'（2026-09-16）

本节只记**实测得到的事实**与**据此做的决定**。凡与前面各节的推测冲突，以本节为准。

### 17.1 已落地

| 文件 | 作用 |
|---|---|
| `generator/data/fota_format.json` | 格式唯一真源（镜像头 16 B / 信封 48 B），生成时复制进 `output/<demo>/` |
| `generator/patch_crc.py` | **重写**：只做原位回填、绝不插入字节（B1 修复） |
| `templates/linker/app_slot_{a,b}.ld.j2` | `.app_header` 12 B → **16 B**（新增 fw_version） |
| `templates/bootloader/boot_crc.c.j2` | 用 `slot_size` 做边界；版本号读显式字段（A8） |
| `static/third_party/hpatch_lite/` | vendored 4 文件 + LICENSE + PROVENANCE（blob sha1 已核） |
| `static/third_party/tinyuz/decompress/` | vendored 4 文件 + LICENSE + PROVENANCE（blob sha1 已核） |
| `tools/vendor_fetch.py` | 重取/校验 vendored 源码（走 api.github.com，raw 域名不可达） |
| `tools/hpatch_oracle/oracle_apply.c` | **测试夹具**：用 vendored 解码器做 `apply`，产出 `ORACLE_STAT` |
| `generator/delta_tool.py` | **自研写侧**：差分 + lite 流编码 + 信封 + CLI |
| `generator/tinyuz_enc.py` | **自研 tinyuz 编码器**（裁决 2 的主要自研量） |
| `generator/tests/test_fota_format.py` | 14 条：真源一致性 + B1 回归 |
| `generator/tests/test_delta_tool.py` | 45 条：L1 结构 + L2 oracle 逐字节 + 压缩路径 |

`generator/tests` 全绿 **311 条**（此前 295 条）。

### 17.2 实测确认的事实（推翻了若干此前的推测）

1. **HPatchLite 的魔数是 `'h'`（小写）+ `'I'`（大写）**，不对称。
   写成 `"HI"` 会被 `hpatch_lite_open()` 整体拒绝。已加断言并把理由写进代码。
2. **tinyuz 的流不是纯位流也不是纯字节流，而是两者按"写入顺序"交错的。**
   `outDictPos` 用 `push_back` 追加**裸字节**，而位累加器仍写回**更早的**那个
   字节下标；解码器先把位字节整字节读进累加器、需要时再顺序读裸字节。
   ⇒ **不能**用「先攒位、最后统一打包」的常规实现。`tinyuz_enc.py` 的
   `TuzCode` 因此严格镜像上游 `TTuzCode` 的写入顺序（含 `type_count` 归零处）。
3. **HPatchLite 的结束校验基本无效 —— 已实测复现静默损坏。**
   在**中段**翻转 1 字节后，`hpatch_lite_patch()` **返回成功**、输出长度也对，
   但**内容已错**。⇒ 信封里的 `new_crc32` 不是冗余，是**唯一**的内容完整性判据；
   设备必须在提交新镜像前复算。这条把 §16.4 的"建议"升级为**硬性要求**。
   尾部垃圾同样被无视（`tuz_calls`/rc 都正常）。
4. **`dict_size` 对差分流几乎不敏感 —— 4 KB 顶格。**
   真实固件对实测：`base` 1522 B（1K/4K/16K 全相同）；`modbus_demo`
   3546 / **3498** / 3498。⇒ 取 **4096**，设备侧 dict 缓冲从 16 KB 降到 4 KB。
5. **补丁很小时压缩会反超收益 —— 不能无条件压。**
   实测 `knob_demo`：不压 **70 B** vs 压 79 B。⇒ 新增 `compress="auto"`
   （两条都算、取更小者，默认值），信封 flags 与 lite 头由实际选中的那条决定。
   这是对裁决 2「默认开」的**修订而非否决**：压缩默认启用，但不会回退。
6. **真实固件的差分效果**（都是逐字节 oracle 验证过的）：

   | 对 | 新镜像 | 不压 | tinyuz(auto) | 占比 |
   |---|---|---|---|---|
   | base | 89,172 | 1,837 | **1,522** | 1.71% |
   | modbus_demo | 89,988 | 3,845 | **3,498** | 3.89% |
   | knob_demo | 88,876 | **70** | (79,未选) | 0.08% |

7. **CRC32 的算法名一度写错**：`patch_crc.py` / `boot_crc.c.j2` / 真源注释都写着
   "CRC-32/MPEG-2"，**实际是 CRC-32/ISO-HDLC（≡ `zlib.crc32`）**。
   已核对 `crc32(b"123456789") == 0xCBF43926`。照 MPEG-2 配 STM32 CRC 外设会让
   引导器拒绝**所有**镜像，且极易误判成"外设有问题"。三处注释已改正。
   ⚠️ **仍待上板复核**：外设配置（REV_IN=byte / REV_OUT=1）与 zlib 的等价性
   只有实测能证实 —— 探针当前未连接。

### 17.3 测试策略：为什么必须有两个实现

L1（Python 结构检查）跑得快但证明不了 cover 编码正确；L2 用 **vendored 的 C
解码器**消费我们编码器的输出、逐字节比对，才是权威判据。

关键的一条纪律：**oracle 编译失败必须让测试失败，不能静默 skip**。
「没有编译器」是环境问题（可跳过），「编译失败」是缺陷。二者由
`_build_oracle()` 抛异常 vs 返回 None 区分，另有
`test_oracle_builds_and_is_self_evidently_functional` 防止 skip 掩盖问题。

同理，压缩路径必须有 `tuz_calls >= 1` 的断言（§16.6 验收第 6 条）——
否则 `compress_type` 会退化成"只写不读"的字段。

### 17.4 下一步

按 §16.6：**P2'**（设备侧 `fota_delta.{c,h}` 适配层 + 掉电注入）、
**P3**（传输与元数据）、**P4**（启动决策表 / 16 B 头部落地）、
**P5**（`examples/fota_demo/`，必须开 `bootloader.enabled`，否则模板永不编译）。

三件未决事项的处置（2026-09-17 全部落地）：
① ✅ `requirements.md` FR-14 已由 ✅ 改为 ⏳，并写明缺口；
② ✅ 压缩自 `patch` 流第 0 字节起（本实现取 §16.8.1 的方案 (i)：**头明文 + 正文压缩**），已落地；
③ ✅ `static/third_party/` 已补进 `AGENTS.md` 第 1 条（供应商只读）清单。

---

## 18. 实施记录：P2' + 引导路径首次进入构建闸门（2026-09-16）

同 §17：只记**实测得到的事实**与**据此做的决定**。与前面各节冲突处，以本节为准。

### 18.1 已落地

| 文件 | 作用 |
|---|---|
| `templates/drivers/fota_delta.{c,h}.j2` | 设备侧差分应用层。纯算法 + `fota_delta_backend_t` 注入式 I/O，**同一份源码**编到目标与主机（不用 `#ifdef TEST`）——这是 §5.1 分层的落点，也是 L6 能在主机上跑起来的前提 |
| `generator/tests/harness/fota_delta_l6_harness.c` | L6 测试台：内存 Flash（真擦除、真双字 1→0 编程、越界/非对齐/长度非法一律报错）+ 掉电注入 + 三条不变量 |
| `generator/tests/test_fota_delta_l6.py` | L6 驱动：真补丁（走压缩路径）+ 2 条**变异测试**（18.3） |
| `generator/tests/test_bootloader_host_tests.py` | 3 类结构护栏（18.2 第 2、3 条） |
| `examples/fota_demo/` | 首个开 `bootloader.enabled` 的示例。**必须先有它**——否则整条引导/差分路径永远在构建闸门之外（这正是 FR-14 被误标 ✅ 的根因） |
| `templates/test/mock_hal.{h,c}.j2` | 补 `IWDG` 寄存器模型（此前是 `#define IWDG ((void *)0)`） |
| `generator/run_tests.py` | 修正 `-ICMSIS/Core/Include`（该目录不存在，真实位置是 `CMSIS/Core/`） |
| `templates/test/test_boot_nvm.c.j2` | 修掉一条空洞断言（18.2 第 3 条） |

`generator/tests` 全绿 **328 条**（§17 结束时 311 条）。四个编译自检目标
（默认 / `bootloader` / `app` / `combined`）全部通过。

### 18.2 实测确认的事实

1. **引导/差分路径第一次被真的生成 + 编译，一次性暴露 6 处潜伏缺陷。**
   镜像头落在 `0xBC` 而非 `0xC0`（STM32G0 的 `.isr_vector` 只有 47 项 = 0xBC，
   `.app_header` 没被钉住）⇒ 引导器拒绝**所有**镜像；`patch_crc.py` 当时是
   "跟着 magic 走"，于是**默默接受**了这个偏移。修法：链接脚本用
   `ASSERT(ADDR(.app_header) - ORIGIN(FLASH) == img_hdr_off)` 钉死，
   `patch_crc.py` 改为 **fail-closed**。另有 ASM 编译标志被当作单个 argv 传入、
   引导器因 `-u _printf_float` 溢出 8 KB、`build/` 被一起提交导致
   `CMakeCache.txt` 路径错、编译自检没覆盖三个自定义目标等。
   > 结论与 §16 一致：**"有模板"不等于"被编译过"**。FR-14 的 ✅ 就建立在这个错觉上。

2. **整头独占写入：把掉电安全从"依赖跨子系统假设"变成"结构性不可能"。**
   镜像头的 16 B **全部**由设备在 FLUSH 阶段写入（不是只留 `image_size`+`crc32`
   那 8 B）。差分流天然带着镜像头字节，若让 magic 从流里进来，"中途掉电"就可能留下
   一个 **magic 合法**的目标槽 —— 它能不能被引导，就取决于"引导路径上每一处都必须
   先验 CRC"这个跨子系统的隐含假设。改为整头独占后，掉电状态下目标槽头部恒为
   `0xFFFFFFFF` ⇒ magic 恒非法 ⇒ **与其它模块的内部检查无关**。
   `fota_format_for_templates()` 里有断言把这条不变量固化（退化不会有编译信号，
   只能靠断言）。

3. **bootloader 源码从未遵守 TEST/mock 约定 ⇒ 三个 boot 测试从来没编译通过过。**
   `boot_crc.c`/`boot_jump.c`/`boot_nvm.c` 无条件 `#include "stm32g0xx.h"`，
   而主机单测是"把被测源码 include 进测试 + 链接 `mock_hal.c`"，
   `mock_hal.h` 里 `typedef int32_t IRQn_Type;` 与 CMSIS 的枚举**必然冲突**：
   `error: conflicting types for 'IRQn_Type'` + `core_cm0plus.h: No such file`。
   而 `mock_hal.h` 里早就做好了 CRC/PWR/RCC/TAMP/SCB/NVIC/SysTick 的寄存器模型和
   `mock_cmsis_reset()` —— 缺的只是一处约定。修完后 boot 测试真的跑起来了
   （`test_boot_nvm` 12 条、`test_boot_crc` 4 条）。
   > 这是 A9 的**镜像版本**：A9 是"mock 太宽容所以查不出缺陷"，这里是
   > "测试根本编不过，于是没人知道它没跑"。两者都以"有测试"的形式提供虚假安全感。

4. **`test_boot_nvm` 里有一条空洞断言。** `test_swap_active_slot_a_to_b` 断言
   "切换后尝试计数器归零"，却**从未先把计数器顶起来** ⇒ `0 == 0` 恒成立，
   删掉实现里的归零语句它也照样 PASS。已补上前置自增（并加断言确认已自增）。
   实测：去掉 `TAMP->BKP0R = 0;` 后该用例现在报
   `Expected 0 Was 3`。

5. **L6 的实现形态与 §12 措辞不同（有意为之，记录以免误解）。**
   §12 写"在**每条指令边界**模拟掉电"，实现是**在每个持久化操作边界**
   （`patch_read` / `erase` / `program`）注入。理由：纯计算指令不落盘，掉电在
   其中间不会留下任何可观测的持久状态，"每条指令"只会把成本乘以几千倍而覆盖
   不到新东西。**真正需要细分的是"覆盖镜像头窗口的那些操作"**——magic 恰好在
   哪一步之后变得合法完全由它决定——因此对它们**逐个 8 字节前缀全枚举**；
   对大操作（一次擦除 544 个双字、一次页编程 256 个）抽样 16 点
   （一个没写完的页无论断在哪里都不会产生合法 magic）。
   当前向量：15 个持久化操作 → **76 个注入点**。
   三条不变量逐点成立：① 活动槽逐字节不变；② 目标槽头部 magic 恒为擦除态；
   ③ 掉电后**重放**同一补丁，目标槽逐字节等于新镜像。

6. **静态 RAM 实测 ≈ 8.5 KB，略超 §14 验收第 2 条的 8 KB。**
   `arm-none-eabi-size` 于 `drv_fota_delta.c.obj`：`text 4,636 B / bss 8,772 B`。
   bss 构成：`s_page_buf` 2,048 + `s_temp_cache` 2,048 + `s_tuz_mem` 4,616
   （= tinyuz 预留 4,104 + 解压输出缓冲 512），余下约 60 B 为其它静态量。
   超出的原因不是设计走偏，而是**预算函数只算了三条缓冲之和（8,192），
   没有算 tinyuz 自身的预留开销（`tuz_reserved_mem_size()` = dict_size + 8）
   与适配层其余静态量**。另有 512 B 是有意的松弛：不压缩路径下
   `s_temp_cache` 要 2,048 全用，压缩路径只用 1,536。
   ⇒ **已改判**（用户 2026-09-17 批准）：§14 第 2 条改为"**≤ 9 KB 且与镜像/补丁
   大小无关**"（后者是结构性的：所有缓冲都是编译期定长，`fota_delta_budget()`
   是唯一出处）。备选方案（把 `cache_size` 降到 1,024，≈7.5 KB，代价是吞吐）
   保留在案，若将来 RAM 吃紧可随时启用。

7. **差分适配层当前会被链接器整个丢掉 —— 这是预期的，不是缺陷。**
   `drv_fota_delta.c` 编译进了 app，但没有任何调用者（接收侧在 P3），
   在 `-ffunction-sections --gc-sections` 下被 GC，`fota_demo.elf` 里查不到
   `fota_delta_*` 符号。⇒ **在 P3 之前，L6 主机测试台是这条路径唯一的执行证据**，
   这也是 18.3 必须存在的理由。

### 18.3 测试台的可信度：变异测试

> 绿色结论只有在"它能红"的前提下才有意义。第一版 L6 就打印过 `RESULT: OK`，
> 而它实际只覆盖了 15 个点中的一个（见第 4 条的同源问题）。

因此 `test_fota_delta_l6.py` 除了主用例外，还**主动改坏模板**并断言 L6 必须报错：

| 变异 | 违反 | 期望信号 |
|---|---|---|
| 擦除后立刻把 magic 写进头部（而不是等 FLUSH） | 不变量 ② | `target header magic is not erased after power loss` |
| 提交前擅自动活动槽 | 不变量 ① | `active slot changed` / `active slot was modified` |

变异锚点是模板源码里的具体片段，**模板重构会让锚点断言先失败**，逼着人来更新 ——
刻意的：一个悄悄失效的变异测试比没有变异测试更糟。

`test_bootloader_host_tests.py` 同样按此思路写成"结构护栏"：
① bootloader 源码必须走 `#ifdef TEST` 三段式；
② **mock 必须建模 bootloader 解引用的每一个寄存器块**（正则抽取 `NAME->`，
   逐个核对 `mock_hal.h.j2` 的指针声明）——这条如果早存在，第 3 条那个缺陷
   在写的当天就会被抓住；③ `run_tests.py` 里 `static/` 下的 `-I` 路径必须真实存在
   （gcc 对不存在的 `-I` 目录**不报错**，这类笔误会一直潜伏）。
三者都做过"还原缺陷 ⇒ 必须变红"的验证。

### 18.4 下一步

按 §16.6 与 §13：

- **P3**（传输与元数据）：`drv_fota` 重写为接收侧状态机，调用 `fota_delta_apply()`；
  这一步做完，适配层才第一次被真正链接进镜像，§14 第 2 条的 RAM 也才有意义。
- **P4**（引导决策表 / 16 B 头部落地）。
- **P5**（HIL 端到端）：`examples/fota_demo/` 已就位，剩下真板 A→B 升级 + 回滚。

两件待裁决事项均已批准（用户 2026-09-17），并在本文件记录处置：

④ ✅ §14 第 2 条的 RAM 上限由 8 KB 改为 **9 KB**（依据见 18.2 第 6 条）；
⑤ ✅ 旧 BSDIFF 实现已退役：删 `generator/bsdiff_tool.py`、
  `templates/drivers/fota_bspatch.{c,h}.j2`、`templates/test/test_fota_bspatch.c.j2`，
  并同步 `docs/requirements.md` 与 `docs/developer-guide/{architecture-overview,
  template-development}.md`。
  > 原计划"单独一个 commit 做退役清理"在执行时**改为与 P3 同一提交**：
  > `templates/drivers/drv_fota.c.j2` 原先 `#include "drv_fota_bspatch.h"` 并调用
  > `fota_bspatch_apply()`，先删被依赖方会让仓库里存在一个"模板 include 了
  > 已删除的头"的悬空提交。P3 重写 `drv_fota` 后该依赖自然消失，
  > 于是两件事合并为一个原子提交。

## 19. 实施记录：P3 接收侧状态机 + P3' YMODEM 通道（2026-09-17）

§18.4 列的 P3 已完成，并在此基础上加了第二个传输通道。P4（引导决策表）与 P5（真板 HIL）
仍未做 —— §14 的验收标准 4/5 条即对应它们。

### 19.1 已落地

| 位置 | 内容 |
|---|---|
| `templates/drivers/drv_fota.{c,h}.j2` | 接收侧状态机重写；`fota_session_*` 暂存会话层（两条传输共用）；`fota_transport_t { NONE, FRAME, YMODEM }` |
| `templates/drivers/drv_fota_ymodem.{c,h}.j2` | YMODEM batch 接收侧（新） |
| `templates/drivers/fota_meta.{c,h}.j2` | Flash 页元数据日志（P3 时点已从 BKP 寄存器搬到专用页，见 §18） |
| `templates/drivers/drv_cli.c.j2` | `fota` 命令组：`status` / `progress` / `recv` / `ymodem` / `apply` / `erase` |
| `generator/data/ymodem_format.json` | YMODEM 格式**唯一真源**（新） |
| `generator/fota_ymodem_sender.py` | 测试侧发送器（新）：出字节计划，供 L5 台架与调试用 |
| `generator/tests/test_fota_ymodem.py` | 渲染层护栏（新） |
| `generator/tests/test_fota_ymodem_l5.py` + `tests/harness/fota_ymodem_l5_harness.c` | L5 跨实现台架（新，14 用例） |
| `templates/test/test_fota_ymodem.c.j2` | **生成工程内**的传输层单测（新，15 用例）；`test_fota_protocol.c.j2` 同步补上 `drv_fota_ymodem.c` 的 include（见 19.5） |
| `docs/user-guide/cli-commands.md` | FOTA 小节按实际子命令重写，含 YMODEM 操作步骤 |

### 19.2 关键设计：两条通道，一份会话

YMODEM 与帧协议的差别**只有"字节怎么进来"**：前者是定长分片 + 显式序号 + 停等 ACK，
后者是 128/1024 字节块 + 16 位 CRC。字节进来之后的一切必须逐字节相同 ——

- 容量准入公式：`align_up(new_size, page) + align_up(48 + patch_size, 8)`
- 暂存区几何：`staging_base = slot_base + slot_size - align_up(record, 8)`
- 续传判定：记录状态 + 槽 + 进度区间 + 信封逐字节相同
- 信封先落盘、双字对齐攒批、`0xFF` 补尾
- 收尾的"从 Flash 回读整条记录复算 CRC32"

所以这些收敛进 `fota_session_*`，两条传输都只经由它落盘。**各写一份的结局不是"代码重复"，
而是两份准入公式**，而"边界少算一页、应用期把暂存区首页擦掉"这类缺陷的难点恰恰在于它
只在一小段边界上出现 —— 两份实现里总有一份没被那条边界测到。同理，"哪些状态允许进入
接收"是一条安全策略（READY / DONE 下暂存区里已有一条校验通过的补丁），抽成唯一的
`fota_receive_ready()`，两个入口各判一次迟早会漏。

`fota_transport_t` 的存在理由是**节拍互斥**：`fota_process()` 里帧协议要做空闲超时
（超时即 `fota_receive_abort()`），YMODEM 要周期发 `'C'` 并处理块超时，两者的时基都由
`g_last_activity_ms` 折算。不区分传输方式的话，一次 YMODEM 传输会被帧协议判定为
"主机静默"而中途中止 —— 而且是在 2 s 之后，表现为"YMODEM 传到一半设备自己放弃了"。

⚠️ `fota_transport_t` 的定义必须在 `.h` 而不是 `.c`：`fota_transport_begin()` 的形参用它，
而 `drv_shell.c` / `main.c` / `event_mgr.c` 只 include 头文件，类型留在 `.c` 里这些翻译单元
会以 `unknown type name` 失败（实施时确实这样失败过一次）。

### 19.3 YMODEM 的四个必踩点（每一个都能"自测全绿、接真软件全挂"）

| # | 陷阱 | 后果 | 处置 |
|---|---|---|---|
| 1 | **CRC 是两个不同的算法** | 见下 | 设备自己实现 CRC-16/XMODEM，不复用 `fota_crc16()` |
| 2 | 末块 `0x1A` 填充未按声明长度截断 | 补丁尾部多一截 `0x1A` | 块 0 的十进制长度即整条记录长度，据此截断 |
| 3 | `CAN`(0x18) 在非块边界被当成中止 | 补丁里随机的 `0x18` 触发假中止 | 只在块起点判 CAN，且**连续两个**才算 |
| 4 | `(uint8_t)YMODEM_BLK_MODULUS` = `(uint8_t)256U` = `0` | 运行期除零 ⇒ 真机 HardFault | 取模先升 32 位，最后再转 `uint8_t` |

**第 1 条是这个特性最贵的坑**：本仓帧协议用 CRC-16/CCITT-FALSE（init `0xFFFF`，
`'123456789'` → `0x29B1`），YMODEM 规定 CRC-16/XMODEM（init `0x0000` → `0x31C3`）。
同一多项式、不同初值。图省事复用 `fota_crc16()` 的话，**自研主机 ↔ 设备之间完全互通**
（两侧错得一样，自测全绿），但 Tera Term / lrzsz `sb` / ExtraPuTTY **一个都连不上**。
这是 A3 类缺陷（两侧各自"正确"地实现了一份不同的约定）的教科书形态，而且它天然不会被
自测暴露 —— 因为自测的两侧来自同一份错代码。

判据因此是**标准**而不是"另一侧也这么算"：`ymodem_format.json` 写死 check 值
`0x31C3`，测试拿它当 KAT，独立验算方式一并写在真源里
（`python -c "import binascii; print(hex(binascii.crc_hqx(b'123456789', 0)))"`）。

### 19.4 载荷与续传

- 载荷是 `.h2cd` 差分补丁的**整条记录**（48 B 信封 + lite 流），与帧协议发的是同一串字节。
- **整文件校验由设备承担**：YMODEM 协议本身没有全文件校验，所以设备在接收过程中累加
  CRC-32/ISO-HDLC，收尾与帧协议走同一个 `fota_session_finish(expected_crc32)`。
- **YMODEM 没有部分续传** ⇒ 主机恒从文件字节 0 重发。设备的续传就是"把已提交的前缀当作
  要复核的重放"（`FOTA_STREAM_FROM_RECORD_START`）：逐字节与 Flash 里的内容比对，
  不一致立刻 fail-fast。跳过也能保证最终正确（收尾那次回读 CRC32 覆盖整条记录），
  但校验失败要等全部传完才报出来 —— 操作员会为一个 3 秒就能发现的问题多等一次完整传输。
- **块 0 的声明长度是"主机声明长度"与"信封声明长度"的唯一交叉核对点**
  （帧协议的 START 帧没有长度字段，只能由信封推出）。两者不符即拒绝。
- **一批 = 一个补丁**。传输途中出现第二个非空块 0 时明确拒绝（`CAN CAN`），
  而不是把它当成另一个补丁默默写进同一个暂存区。
- **传输层失败不升格成 ERROR**：握手超时 / 重试耗尽 / 主机 CAN 都没有破坏已落盘的内容，
  元数据里的 RECEIVING 记录仍然有效，主机重发即可续传。升格成 ERROR 会让 `fota status`
  显示一个需要人工介入的终态（操作员会先去敲 `fota erase`），而实际上再试一次就行。
  真正的 ERROR 只留给"已落盘内容不可信"那几种（Flash 编程失败、回读 CRC32 不符）。

### 19.5 测试台

**L5 跨实现台架**（`test_fota_ymodem_l5.py` + `harness/fota_ymodem_l5_harness.c`）：
渲染真模板 → 主机 gcc 编译（真驱动 + vendored 解码器 + `mock_hal`）→ 喂
`fota_ymodem_sender.py` 产生的字节 → 断言设备行为。编译带 `-Werror=div-by-zero`
（陷阱 4 就是它抓到的）。

14 个用例：

| # | 用例 | # | 用例 |
|---|---|---|---|
| 1 | 握手与噪声免疫 | 8 | 容量准入不过 → 拒绝 |
| 2 | 正常批次（1024 字节块） | 9 | 传输途中出现第二个文件头 → 拒绝 |
| 3 | 正常批次（128 字节块 / SOH 路径） | 10 | 块超时 → NAK → 接着收 |
| 4 | 重复块（ACK 丢失） | 11 | 握手超时（主机一直没开始）→ 退回 IDLE，**不是 ERROR** |
| 5 | 块号错乱 → 重试耗尽 → 主动中止 | 12 | 主机不发结束块 → 仍算成功 |
| 6 | 主机 `CAN CAN` 中止 | 13 | 收齐 → `fota_process` 应用 → 目标槽是新镜像 |
| 7 | 块 0 声明长度与信封不符 → 拒绝 | 14 | 续传前缀不符 → fail-fast |

**变异验证（收敛性是台架可信度的判据）**：M2（CRC 通过后重置重试计数）只红 case 5，
M4（去掉前缀复核）只红 case 14，M5（重复块不回 ACK）只红 case 4 ——
每条变异只打掉一个用例，说明用例彼此独立、没有靠"顺手覆盖"过关。

**渲染层护栏**（`test_fota_ymodem.py`）盯的是"真源 → 宏"这一段：

- 数值键 → 宏的**穷尽映射**（`_MACRO_MAP` + `_NOT_A_MACRO`）：真源里加一个键而模板没消费，
  测试就红。漏一个键的后果是 C 里出现一个手写的字面量 —— 而它看起来完全正常。
- 模板里**不许出现控制字节字面量**（只能经 `YMODEM_*` 宏）。
- **窄化溢出检测**：抓 `(uint8_t)YMODEM_BLK_MODULUS` 那一类。⚠️ 扫描前必须先剥 C 注释，
  否则模板注释里的反例（`(uint8_t)YMODEM_BLK_MODULUS`、`fota_crc16`）会把护栏自己绊倒。
- **设备侧不得复用帧协议的 CRC**，且 check 值必须是标准值 `0x31C3`。

**生成工程内的传输层单测**（`templates/test/test_fota_ymodem.c.j2` → `test/` 目录）：
不靠 Python、不靠 vendored 解码器，`python run_tests.py` 就能跑 15 个用例。它的
分工是"**字节怎么进来**的那一半" —— 握手 'C' 的节奏与上限、块号反码先于 CRC、
**块尾必须是 CRC-16/XMODEM**、块 0 的文件名/长度解析、CAN 只在块边界且要连续两个、
载荷里的 `0x18` 是数据、文件头之前来数据块/EOT/结束块各走哪条分支。
另有两条不显眼但值钱的用例：①测试**自己的**两个 CRC 实现先过标准 KAT
（`0x31C3` / `0x29B1`）—— 尺子先校准，否则"设备 ACK 了我造的块"只说明两边错得一样；
②断言 `(uint8_t)YMODEM_BLK_MODULUS == 0`，把"回绕写成窄化 cast 会得到 0"这件事
写在生成出来的工程里。

**这一节的一次实测回归值得记住**：`drv_fota.c` 的 `fota_process()` 会调用
`fota_ymodem_process()`，于是**已经存在的** `test_fota_protocol.c` 立刻
`undefined reference to 'fota_ymodem_process'` —— 编译失败，而它的 pytest /
L5 用例全绿（那些用例根本不生成、也不编译工程内的测试）。发现它的是
`output/fota_demo/test/run_tests.py`（CI 的第 157 行也会跑同样的一步）。
修法是让两个测试都 `#include` 进全部三个 FOTA 源文件，**不给替身**：
给一个空桩就等于让"drv_fota 调用了不存在的实现"继续静默。
⇒ 凡给某个生成源码**新增一个跨模块调用**，都要顺手跑一次受影响示例的
`run_tests.py`，别只看 pytest 的颜色。

### 19.6 实测数字（`output/fota_demo`）

| 项 | 值 |
|---|---|
| `fota_demo.elf` | text 109552 / data 1120 / bss 30472 |
| YMODEM 代码 | 2690 B（17 个函数，`nm` 逐个 text 求和） |
| YMODEM 静态 RAM | `g_blk` 1029 B（`total_1024`）+ 少量标志位 |
| `combined.bin` | 118868 B（bootloader + app）；`mhde_mainboard` 为 126192 B |

`/tmp` 级的 RAM 代价（约 1 KB）是**必须的**：块最长 1029 B，去掉它就只能逐字节接收再
拼块 —— 那样每来一个字节都要过一遍状态机，而 YMODEM 的块是原子的（校验在块尾，
块内无法边收边判）。§14 第 2 条的 RAM 上限仍是 9 KB（见 §18.2 第 6 条）。

### 19.7 下一步

- **P4**（引导决策表 / 16 B 头部落地）。
- **P5**（HIL 端到端）：`examples/fota_demo/` 就位，剩下真板 A→B 升级 + 回滚。
  ⚠️ 主机 L5 全绿**不能**替代它 —— §18.2 与 `test-writing-guardrails` 记录的
  A3/A9 类缺陷（两侧各自"正确"、mock 造出硅片上没有的东西）恰恰都是"主机全绿、真机没有"。
  真机上至少要走一次：`fota ymodem` + Tera Term 实际发送（这才真正验证陷阱 1）。

