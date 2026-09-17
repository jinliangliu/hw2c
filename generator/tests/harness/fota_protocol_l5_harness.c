/*
 * fota_protocol_l5_harness.c — L5：FOTA 接收侧传输状态机（drv_fota.c）的主机测试台
 *
 * 规划 §12 的 L5 层回答一个问题："主机发出去的字节流，设备到底收成了什么？"
 * 这一层不需要真板：`drv_fota` 的全部外部依赖只有三样 —— UART 字节流
 * （由 CLI 显式移交）、TAMP 备份寄存器、Flash。三样在主机上都有**语义正确**
 * 的模型（`mock_hal`：备份寄存器是真寄存器镜像，Flash 是 512 KB RAM 且
 * 擦/写/未擦写报错都按真实硬件语义）。所以这里没有任何替身逻辑，
 * 只有被替换的存储介质与串口。
 *
 * 与 L6 的关系
 * ------------
 *   L6（`test_fota_delta_l6.py`）测**应用层**的掉电安全：补丁已经在暂存区，
 *      在每一次持久化操作处断电，检查三条不变量。
 *   L5（本文件）测**传输层**：补丁还在路上，检查帧解析、应答、暂存、
 *      续传、准入、以及"收齐之后交给应用层"这一交棒动作。
 *
 * 两侧共用同一份差分解码器与同一份格式真源，所以本测试台会把**真实的**
 * `drv_fota.c` / `drv_fota_delta.c` / `hw2c_fault.c` 一起编进来 —— 不是
 * 替身实现。A9（mock 让测试失效）的根因就是"测的是另一份代码"。
 *
 * 输入从哪来
 * ----------
 * 全部帧字节由 **`generator/fota_sender.py::Framing`** 从真源构造（见
 * `test_fota_protocol_l5.py` 生成的 `fota_l5_vectors.h`）。这是刻意的：
 * 主机发送端算 CRC、设备接收端验 CRC，两侧是**不同实现**。若在 C 里再写
 * 一个 CRC16 去拼输入，就变成自己和自己对答案 —— 校验恒过，什么也证明不了。
 *
 * 喂字节的方式也与真机一致：按 7 字节一块，经 CLI 的 sink 指针进入
 * `fota_rx_bytes()`，而不是直接调它。于是"忘了 `cli_set_rx_sink()`"这类
 * 交接缺陷会直接表现为"字节根本进不去"，而不是被测试台绕过去。
 *
 * 输出：`RESULT: OK (...)` 或 `RESULT: FAILED (n failures)`；退出码 0/1。
 */

#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <stdint.h>

/* `l5_config.h` 由夹具生成，只放"与工程命名相关"的名字（如 CLI 头文件名）。
 * 它必须**最先**包含：下面的 CLI 头文件名是个宏，而其余生成的向量头
 * （fota_l5_vectors.h）要等 fota_delta.h 的类型可见之后才能用。 */
#include "l5_config.h"

#include "mock_hal.h"
#include L5_CLI_HEADER          /* cli_rx_sink_t + 三个交接原语（真头文件） */
#include "drv_fota.h"
#include "hw2c_fault.h"

#include "fota_l5_vectors.h"   /* 需要 FOTA_DELTA_E_* / FOTA_E_* 可见 */

/* ===========================================================================
 * 1. CLI 字节流的模型
 *
 * 真机上 `cli_task()` 从环形缓冲取出字节，若 sink 非空就整块交给它，
 * 否则喂行编辑器。这里只保留这个语义：一个函数指针 + 一个投递入口。
 * "FOTA 接管后 CLI 收不到字节"这件事本身就是被测行为（交接的入口与出口），
 * 所以模型里不做任何额外保护。
 * =========================================================================== */

static cli_rx_sink_t g_sink = NULL;
static uint8_t       g_resp[1024];
static uint32_t      g_resp_len = 0U;
static uint32_t      g_resp_drop = 0U;   /* 应答缓冲溢出（用例本身就不会发生） */
static uint32_t      g_feed_lost = 0U;   /* 未经接管就投递的字节数 */
static uint32_t      g_iwdg_calls = 0U;

void cli_set_rx_sink(cli_rx_sink_t sink) { g_sink = sink; }
uint8_t cli_rx_sink_active(void) { return (g_sink != NULL) ? 1U : 0U; }

void cli_write_raw(const uint8_t *data, uint16_t len)
{
    if ((uint32_t)len > (uint32_t)sizeof(g_resp) - g_resp_len) {
        g_resp_drop++;
        return;
    }
    memcpy(&g_resp[g_resp_len], data, len);
    g_resp_len += (uint32_t)len;
}

/* 真 API 是 `IWDG_Refresh()`（drv_iwdg.c）。这里只计数：喂狗是否发生
 * 在 L6 里已经按"每次 Flash 操作一次"验证过，L5 只确认调用不会缺失。 */
void IWDG_Refresh(void) { g_iwdg_calls++; }

/* ===========================================================================
 * 2. 断言与打印
 * =========================================================================== */

static int g_failures = 0;
static int g_case_failures = 0;

static void check(int cond, const char *what)
{
    if (!cond) {
        fprintf(stderr, "  [FAIL] %s\n", what);
        g_case_failures++;
    }
}

static void check_eq(long got, long want, const char *what)
{
    if (got != want) {
        fprintf(stderr, "  [FAIL] %s: got %ld, want %ld\n", what, got, want);
        g_case_failures++;
    }
}

static void dump_hex(const char *tag, const uint8_t *p, uint32_t n)
{
    uint32_t i;
    fprintf(stderr, "    %s (%u B):", tag, (unsigned)n);
    for (i = 0U; i < n && i < 64U; i++) {
        fprintf(stderr, " %02X", p[i]);
    }
    if (n > 64U) {
        fprintf(stderr, " ...");
    }
    fprintf(stderr, "\n");
}

static void hex_of(char *out, size_t out_len,
                   const uint8_t *p, uint32_t n)
{
    /* 把前若干字节格式化成十六进制，便于把两串响应放进一条消息里比对 */
    uint32_t i;
    size_t   used = 0U;
    out[0] = '\0';
    for (i = 0U; i < n && i < 24U && used + 3U < out_len; i++) {
        used += (size_t)snprintf(out + used, out_len - used, "%02X ", p[i]);
    }
    if (n > 24U && used + 4U < out_len) {
        (void)snprintf(out + used, out_len - used, "...");
    }
}

/* ===========================================================================
 * 3. 夹具
 * =========================================================================== */

static uint8_t *flash_ptr(uint32_t addr)
{
    return mock_flash_base() + (addr - MOCK_FLASH_BASE);
}

/* 活动槽 A 内容是否原样未动 —— 与 L6 的不变量 ① 同一条，
 * 在 L5 里它的含义是"传输阶段不允许碰活动槽"。 */
static int active_slot_unchanged(void)
{
    return memcmp(flash_ptr((uint32_t)L5_SLOT_A_BASE), g_l5_old_image,
                  (size_t)L5_OLD_IMAGE_SIZE) == 0;
}

static int target_slot_equals_new(void)
{
    return memcmp(flash_ptr((uint32_t)L5_SLOT_B_BASE), g_l5_new_image,
                  (size_t)L5_NEW_IMAGE_SIZE) == 0;
}

/* 目标槽的镜像头 magic 是否仍是**擦除态**。擦除态 = 结构上不可引导，
 * 这是"失败/掉电后引导器不会去引导半成品"的依据。
 * 用谓词而不是比较数值：0xFFFFFFFF 转成有符号 long 的可移植性不值得讨论。 */
static int target_header_is_erased(void)
{
    uint8_t hdr[4];
    memcpy(hdr, flash_ptr((uint32_t)L5_SLOT_B_BASE + 0xC0U), sizeof(hdr));
    return (hdr[0] == 0xFFU && hdr[1] == 0xFFU
            && hdr[2] == 0xFFU && hdr[3] == 0xFFU);
}

static void seed_active_slot(void)
{
    uint32_t i;
    for (i = 0U; i < (uint32_t)L5_OLD_IMAGE_SIZE; i++) {
        mock_flash_poke((uint32_t)L5_SLOT_A_BASE + i, g_l5_old_image[i]);
    }
}

/* 每个用例开始时的完整前置：MCU 复位、Flash 空白、活动槽种入旧镜像、
 * UART 归还 CLI、FOTA 初始化。 */
static void l5_reset(void)
{
    mock_cmsis_reset();            /* TAMP 清零 ⇒ 元数据 magic 无效 */
    mock_flash_reset();            /* 整片 0xFF、上锁、DBANK 回默认 */
    mock_tick_reset();
    mock_NVIC_SystemReset_reset();
    hw2c_fault_clear();            /* 只清原因，保留 magic/reset_count */

    g_sink        = NULL;
    g_resp_len    = 0U;
    g_resp_drop   = 0U;
    g_feed_lost   = 0U;
    g_iwdg_calls  = 0U;

    seed_active_slot();
    fota_init();
}

/* 按真机的粒度投递：cli_task 是"取一批、投一批"。7 字节这个块长与所有帧长
 * 都互质，于是每一帧都会被切在非边界位置上 —— 增量解析器的状态推进被真正
 * 覆盖到（一次投一整帧的测试是覆盖不到这一层的）。 */
#define L5_FEED_BLOCK 7U

static void l5_feed(const uint8_t *data, uint32_t len)
{
    uint32_t i = 0U;

    while (i < len) {
        uint32_t take = len - i;
        if (take > L5_FEED_BLOCK) {
            take = L5_FEED_BLOCK;
        }
        if (g_sink == NULL) {
            g_feed_lost += (len - i);   /* 没人接管 ⇒ 字节进不去 */
            return;
        }
        g_sink(&data[i], (uint16_t)take);
        i += take;
    }
}

static void resp_reset(void) { g_resp_len = 0U; }

static int resp_is(const uint8_t *want, uint32_t len)
{
    if (g_resp_len != len) {
        return 0;
    }
    return memcmp(g_resp, want, len) == 0;
}

/* 第 k 个应答帧（都是 3 B：marker + next_seq）。应答流是"帧序"的，
 * 所以按下标取即可 —— 期望值由 Python 烘好，与设备实现无关。 */
static const uint8_t *resp_frame(uint32_t k)
{
    return &g_l5_resp[k * 3U];
}

/* 第 k 个请求帧（0 = START，1..n = DATA 0..n-1，n+1 = FINISH） */
static const uint8_t *frame_at(uint32_t k)
{
    return &g_l5_frames[g_l5_frame_off[k]];
}

static uint32_t frame_len(uint32_t k) { return g_l5_frame_len[k]; }

/* 用真源的标记 + 给定的信封与 CRC 拼一条 START 帧（负例用） */
static uint32_t build_start_frame(uint8_t *out, const uint8_t *env,
                                  uint16_t crc16)
{
    out[0] = (uint8_t)FOTA_FRAME_START;
    memcpy(&out[1], env, (size_t)L5_ENV_SIZE);
    out[1 + L5_ENV_SIZE] = (uint8_t)(crc16 & 0xFFU);
    out[2 + L5_ENV_SIZE] = (uint8_t)((crc16 >> 8) & 0xFFU);
    return (uint32_t)L5_ENV_SIZE + 3U;
}

/* 一次健康的"START → 收满"投递。返回收完所有 DATA 之后的状态。 */
static void feed_up_to_data(uint32_t n_data)
{
    uint32_t k;
    l5_feed(frame_at(0U), frame_len(0U));              /* START */
    for (k = 0U; k < n_data; k++) {
        l5_feed(frame_at(1U + k), frame_len(1U + k));  /* DATA k */
    }
}

/* ===========================================================================
 * 4. 用例
 * =========================================================================== */

/* ---- 4.1 交接契约与噪声免疫 ------------------------------------------------ */
static void case_handover_and_noise(void)
{
    static const uint8_t noise[] = "\r\nhw2c boot: System ready\r\n";
    char msg[48];

    printf("case 1: CLI 交接 / 噪声免疫\n");

    /* 夹具复位必须在**第一次投递之前**：mock 的 Flash 存储是静态零初始化的，
     * 不经过 mock_flash_reset() 它就是 0x00 而不是擦除态 0xFF ——
     * 那样"噪声不会写 Flash"这条断言会因为介质本身就写不进去而恒真。 */
    l5_reset();

    /* 没接管之前，投进去的字节不该有任何后果 */
    l5_feed((const uint8_t *)noise, (uint32_t)(sizeof(noise) - 1U));
    check_eq((long)g_feed_lost, (long)(sizeof(noise) - 1U),
             "未接管时字节应被记为投递失败（sink 为 NULL）");
    check_eq((long)fota_get_state(), (long)FOTA_STATE_IDLE, "初始应为 IDLE");
    check_eq((long)g_resp_len, 0L, "未接管时不应有任何应答");

    check_eq((long)fota_receive_begin(msg, sizeof(msg)), 0L, "fota_receive_begin");
    check(cli_rx_sink_active() != 0U, "接管后 sink 必须装好");
    check_eq((long)fota_get_state(), (long)FOTA_STATE_RECEIVING, "接管后应为 RECEIVING");

    /* 接管之后，日志文本混在流里也不该被当成帧 */
    resp_reset();
    l5_feed((const uint8_t *)noise, (uint32_t)(sizeof(noise) - 1U));
    check_eq((long)fota_get_state(), (long)FOTA_STATE_RECEIVING, "噪声不应改变状态");
    check_eq((long)fota_get_staged_bytes(), 0L, "噪声不应写进暂存区");
    check_eq((long)g_resp_len, 0L, "噪声不应产生应答");

    /* 半帧 + 噪声 + 完整帧：解析器必须能重新同步 */
    l5_feed(frame_at(0U), 10U);                        /* 半条 START */
    l5_feed((const uint8_t *)noise, (uint32_t)(sizeof(noise) - 1U));
    resp_reset();
    l5_feed(frame_at(0U), frame_len(0U));
    /* 半帧残留在缓冲区里，所以这一帧本身也可能被认成"半帧的第 2 段"。
     * 期望的最终结果只有一条：要么这一帧被接受（ACK），要么被丢弃后
     * 重新同步 —— 但**绝不能**进入一种既没收到也没报错的中间态。 */
    if (g_resp_len == 0U) {
        /* 被丢弃：再发一次必须成功（证明重新同步了） */
        l5_feed(frame_at(0U), frame_len(0U));
    }
    check(resp_is(resp_frame(0U), 3U), "重新同步后 START 应被接受并 ACK(0)");
    check_eq((long)fota_get_staged_bytes(), (long)L5_ENV_SIZE,
             "START 之后暂存区应恰有 48 B 信封");
    /* ⚠️ Flash 必须**仍是解锁的**：整段接收期都要往暂存区写，而元数据提交
     * （一条 24 B 记录）是在这段期间发生的。若元数据模块在写完自己的记录后
     * 顺手上了锁，后续每个 DATA 分片都会以"Flash 被锁"告终 ——
     * 症状只是一个 E_FLASH，离根因（有人动了锁状态）很远。
     * 这条断言把「元数据写路径必须恢复调用方的锁状态」变成可执行的性质。 */
    check(mock_flash_is_locked() == false,
          "START 之后 Flash 必须仍处于解锁态（接收期要持续写暂存区）");

    fota_receive_abort();
    check(cli_rx_sink_active() == 0U, "abort 后必须交还 UART");
    check_eq((long)fota_get_state(), (long)FOTA_STATE_IDLE, "abort 后应为 IDLE");
    check_eq((long)fota_get_staged_bytes(), (long)L5_ENV_SIZE,
             "abort 不应擦掉暂存区（续传的前提）");
    check(mock_flash_is_locked() == true, "abort 之后必须上锁（不再需要写）");
}

/* ---- 4.2 正常流：收齐 → 应用 → DONE --------------------------------------- */
static void happy_path(void)
{
    check_eq((long)fota_receive_begin(NULL, 0U), 0L, "fota_receive_begin");

    resp_reset();
    l5_feed(g_l5_frames, (uint32_t)L5_FRAMES_SIZE);

    /* 一次性比完整条应答流：ACK(0) … ACK(n-1) 各一次 + FINISH 的 ACK(n)。
     * 这比逐个 assert 更强 —— 少发一个 ACK、多发一个 ACK、序号错位都会红。 */
    if (!resp_is(g_l5_resp, (uint32_t)L5_RESP_SIZE)) {
        char got[128], want[128];
        hex_of(got, sizeof(got), g_resp, g_resp_len);
        hex_of(want, sizeof(want), g_l5_resp, (uint32_t)L5_RESP_SIZE);
        fprintf(stderr, "  [FAIL] 应答流不符\n    got : %s\n    want: %s\n", got, want);
        g_case_failures++;
    }

    check_eq((long)fota_get_state(), (long)FOTA_STATE_READY, "收齐后应为 READY");
    check_eq((long)fota_get_last_error(), (long)FOTA_E_NONE, "不应有错误");
    check_eq((long)fota_get_staged_bytes(), (long)L5_PATCH_SIZE,
             "暂存字节数应为整条补丁记录");
    check_eq((long)fota_get_pending_slot(), (long)FOTA_META_SLOT_B, "待启动槽应为 B");
    check(cli_rx_sink_active() == 0U, "收齐后必须交还 UART（应用在任务上下文里跑）");
    check(active_slot_unchanged(), "传输阶段不允许碰活动槽");
    check(target_header_is_erased(),
          "应用之前目标槽头部必须仍是擦除态（结构上不可引导）");
    check(g_iwdg_calls > 0U, "擦/写期间必须喂狗");

    /* ---- 应用 ---- */
    fota_process();

    check_eq((long)fota_get_state(), (long)FOTA_STATE_DONE, "应用后应为 DONE");
    check_eq((long)fota_get_last_error(), (long)FOTA_E_NONE, "应用不应报错");
    check_eq((long)hw2c_fault_last(), (long)HW2C_FAULT_NONE,
             "成功路径不应触发故障陷阱");
    check_eq((long)fota_get_progress(), 100L, "应用完成后进度应为 100");
    check(active_slot_unchanged(), "应用不应改动活动槽（回滚底座）");
    check(target_slot_equals_new(), "目标槽内容应等于新镜像（含头部回填）");
    check_eq((long)TAMP->BKP0R, 0L, "提交时应把启动尝试计数归零");
    check(mock_NVIC_SystemReset_called(), "提交后必须复位进引导器");
    check_eq((long)mock_flash_get_program_fail(), 0L,
             "不应出现任何被硬件拒绝的编程（未擦写/非对齐）");
    check_eq((long)mock_flash_get_erase_fail(), 0L, "不应出现擦除失败");
}

static void case_happy_path(void)
{
    printf("case 2: 正常流（单 bank 页号语义）\n");
    l5_reset();
    happy_path();
}

static void case_happy_path_dbank2(void)
{
    /* 双 bank：页号是 bank 内的，Bank1/Bank2 由地址决定。drv_fota 的
     * `fota_flash_erase_range()` 读运行期 DBANK 位来换算 —— 这条分支只有
     * 真的把 DBANK 置起来才会被走到。 */
    printf("case 3: 正常流（双 bank 页号语义）\n");
    l5_reset();
    mock_flash_set_dbank(1U);
    check_eq((long)mock_flash_get_dbank(), 1L, "DBANK 应已置位");
    happy_path();
}

/* ---- 4.4 DATA 的重复 / 乱序 / 损坏 ---------------------------------------- */
static void case_data_dup_oOO_corrupt(void)
{
    uint8_t  bad[2048];
    uint8_t  nak[3];
    uint32_t n = frame_len(1U);      /* DATA 0 */

    printf("case 4: DATA 重复 / 乱序 / CRC 损坏\n");
    l5_reset();

    check_eq((long)fota_receive_begin(NULL, 0U), 0L, "fota_receive_begin");
    l5_feed(frame_at(0U), frame_len(0U));                       /* START */
    check(resp_is(resp_frame(0U), 3U), "START 应 ACK(0)");

    /* 乱序：先发 DATA 1（设备要的是 0）⇒ 必须重发 ACK(0)，且不写暂存区 */
    resp_reset();
    l5_feed(frame_at(2U), frame_len(2U));
    check(resp_is(resp_frame(0U), 3U), "乱序帧应重发 ACK(0)");
    check_eq((long)fota_get_staged_bytes(), (long)L5_ENV_SIZE,
             "乱序帧不得写进暂存区");

    /* 正常收下 DATA 0 ⇒ ACK(1) */
    resp_reset();
    l5_feed(frame_at(1U), n);
    check(resp_is(resp_frame(1U), 3U), "DATA 0 应 ACK(1)");
    check_eq((long)fota_get_staged_bytes(),
             (long)L5_ENV_SIZE + (long)L5_CHUNK_SIZE, "DATA 0 应完整落进暂存区");

    /* 重复：再发一次 DATA 0 ⇒ 仍应 ACK(1)，且暂存区不增长 */
    resp_reset();
    l5_feed(frame_at(1U), n);
    check(resp_is(resp_frame(1U), 3U), "重复帧应重发 ACK(1)");
    check_eq((long)fota_get_staged_bytes(),
             (long)L5_ENV_SIZE + (long)L5_CHUNK_SIZE, "重复帧不得重复写入");

    /* CRC 损坏：翻掉载荷里的一个 bit ⇒ NAK(want=1)，暂存区不变 */
    check(n <= (uint32_t)sizeof(bad), "DATA 帧长度超出本地缓冲");
    memcpy(bad, frame_at(1U), n);
    bad[5] ^= 0x01U;                    /* 载荷首字节；CRC 不再匹配 */
    resp_reset();
    l5_feed(bad, n);
    nak[0] = (uint8_t)FOTA_PROTOCOL_NAK;
    nak[1] = (uint8_t)(1U & 0xFFU);     /* want_seq = 1（设备的当前期望） */
    nak[2] = (uint8_t)((1U >> 8) & 0xFFU);
    check(resp_is(nak, 3U), "CRC 不符应 NAK(want_seq=1)");
    check_eq((long)fota_get_staged_bytes(),
             (long)L5_ENV_SIZE + (long)L5_CHUNK_SIZE, "损坏帧不得写入");
    check_eq((long)fota_get_state(), (long)FOTA_STATE_RECEIVING,
             "NAK 之后必须仍在接收态（主机要能继续补发）");
    check(cli_rx_sink_active() != 0U, "NAK 之后 UART 仍归 FOTA");
}

/* ---- 4.5 FINISH 来得太早 -------------------------------------------------- */
static void case_finish_short(void)
{
    uint32_t n = frame_len(L5_FRAME_COUNT - 1U);    /* FINISH 帧的长度 */

    printf("case 5: FINISH 早到（收得不全）\n");
    l5_reset();
    check_eq((long)fota_receive_begin(NULL, 0U), 0L, "fota_receive_begin");

    feed_up_to_data(1U);                             /* START + 只收一片 */

    resp_reset();
    l5_feed(frame_at(L5_FRAME_COUNT - 1U), n);       /* 立刻 FINISH */
    check_eq((long)g_resp_len, 3L, "早到的 FINISH 应有唯一一个应答帧");
    check_eq((long)g_resp[0], (long)FOTA_PROTOCOL_NAK, "早到的 FINISH 应 NAK");
    check_eq((long)g_resp[1], 1L, "NAK 的 want_seq 应为 1（断点）");
    check_eq((long)fota_get_last_error(), (long)FOTA_E_SHORT, "错误码应为 E_SHORT");
    check_eq((long)fota_get_state(), (long)FOTA_STATE_RECEIVING,
             "补发缺口是可能的 ⇒ 必须留在接收态而不是进 ERROR");
    check(cli_rx_sink_active() != 0U,
             "仍在接收态 ⇒ UART 仍归 FOTA（否则主机补发不进来）");
    check(active_slot_unchanged(), "活动槽不得被碰");

    /* 补上缺口（DATA 1..n-1），这次应当收齐。
     * ⚠️ 帧下标：0 = START，1..FRAME_COUNT-2 = DATA，FRAME_COUNT-1 = FINISH。
     * 循环条件写成 `i + 1U < FRAME_COUNT` 才是"只发数据帧" —— 早先写成
     * `i + 2U` 之类的边界会把 FINISH 也发进去，于是用例在测别的东西。 */
    resp_reset();
    {
        uint32_t i;
        for (i = 2U; i + 1U < (uint32_t)L5_FRAME_COUNT; i++) {
            l5_feed(frame_at(i), frame_len(i));
        }
    }
    l5_feed(frame_at(L5_FRAME_COUNT - 1U), n);
    check_eq((long)fota_get_state(), (long)FOTA_STATE_READY, "补齐后应 READY");
    check_eq((long)fota_get_staged_bytes(), (long)L5_PATCH_SIZE, "应已收满");
}

/* ---- 4.6 FINISH 的 patch_crc32 不符 --------------------------------------- */
static void case_finish_bad_crc(void)
{
    uint8_t  finish[8];
    uint32_t n = frame_len(L5_FRAME_COUNT - 1U);

    printf("case 6: FINISH 的 patch_crc32 不符\n");
    l5_reset();
    check_eq((long)fota_receive_begin(NULL, 0U), 0L, "fota_receive_begin");

    feed_up_to_data((uint32_t)L5_NCHUNKS);           /* 收满，但不发 FINISH */

    memcpy(finish, frame_at(L5_FRAME_COUNT - 1U), n);
    finish[n - 1U] ^= 0x01U;                         /* 破坏 patch_crc32 最高字节 */

    resp_reset();
    l5_feed(finish, n);

    check_eq((long)g_resp_len, 3L, "应有唯一一个应答帧");
    check_eq((long)g_resp[0], (long)FOTA_PROTOCOL_NAK, "CRC 不符应 NAK");
    check_eq((long)fota_get_last_error(), (long)FOTA_E_PATCH_CRC, "错误码应为 E_PATCH_CRC");
    check_eq((long)fota_get_state(), (long)FOTA_STATE_ERROR,
             "暂存区内容已被证明不可信 ⇒ 终态 ERROR");
    check(cli_rx_sink_active() == 0U,
             "ERROR 是终态 ⇒ 必须交还 UART，否则 CLI 永久失聪");
    check(target_header_is_erased(),
          "失败之后目标槽必须仍然结构上不可引导");
    check(active_slot_unchanged(), "活动槽不得被碰");

    /* ERROR 之后操作员仍应能重新发起（否则一次串口噪声就要求 fota erase） */
    check_eq((long)fota_receive_begin(NULL, 0U), 0L, "ERROR 态应允许重新接收");
    check(cli_rx_sink_active() != 0U, "重新接收后 sink 应再次装好");
}

/* ---- 4.7 START 阶段的各种拒绝 --------------------------------------------- */
static void case_start_rejections(void)
{
    uint8_t  frame[64];
    uint8_t  bad_crc[64];
    uint32_t k;

    printf("case 7: START 阶段拒绝（每条负例只违反一条规则）\n");

    for (k = 0U; k < (uint32_t)L5_NEG_COUNT; k++) {
        const l5_negative_t *neg = &g_l5_negatives[k];
        uint32_t len = build_start_frame(frame, neg->env, neg->crc16);

        l5_reset();
        check_eq((long)fota_receive_begin(NULL, 0U), 0L, "fota_receive_begin");
        resp_reset();
        l5_feed(frame, len);

        if (g_resp_len != 0U) {
            fprintf(stderr, "  [FAIL] %s：被拒的 START 不应产生应答，却收到 %u B\n",
                    neg->why, (unsigned)g_resp_len);
            g_case_failures++;
        }
        if (fota_get_last_error() != neg->expect) {
            fprintf(stderr, "  [FAIL] %s：错误码 got %d, want %d\n",
                    neg->why, fota_get_last_error(), neg->expect);
            g_case_failures++;
        }
        if (fota_get_state() != FOTA_STATE_RECEIVING) {
            fprintf(stderr, "  [FAIL] %s：应留在 RECEIVING，实际 %d\n",
                    neg->why, (int)fota_get_state());
            g_case_failures++;
        }
        if (fota_get_staged_bytes() != 0U) {
            fprintf(stderr, "  [FAIL] %s：暂存区不该被写入（%u B）\n",
                    neg->why, (unsigned)fota_get_staged_bytes());
            g_case_failures++;
        }
        if (fota_get_pending_slot() != FOTA_META_SLOT_NONE) {
            fprintf(stderr, "  [FAIL] %s：不该写出待启动槽元数据\n", neg->why);
            g_case_failures++;
        }
        if (cli_rx_sink_active() == 0U) {
            fprintf(stderr, "  [FAIL] %s：被拒后仍应保持接管（等另一条 START）\n",
                    neg->why);
            g_case_failures++;
        }
    }

    /* 传输级 CRC16 不符：信封本身完全合法，只有尾校验被破坏。
     * 这一条与上面几条**必须**分开测 —— 它证明外层校验真的独立生效，
     * 而不是全都被信封内部的 hdr_crc16 兜住了。 */
    l5_reset();
    check_eq((long)fota_receive_begin(NULL, 0U), 0L, "fota_receive_begin");
    memcpy(bad_crc, frame_at(0U), frame_len(0U));
    bad_crc[frame_len(0U) - 1U] ^= 0x80U;            /* 破坏传输层 CRC16 */
    resp_reset();
    l5_feed(bad_crc, frame_len(0U));
    check_eq((long)g_resp_len, 0L, "传输层 CRC 不符不应产生应答");
    check_eq((long)fota_get_last_error(), (long)FOTA_E_IO, "错误码应为 E_IO");
    check_eq((long)fota_get_state(), (long)FOTA_STATE_RECEIVING, "应留在 RECEIVING");
    check_eq((long)fota_get_staged_bytes(), 0L, "不应写入暂存区");
}

/* ---- 4.8 掉电/复位后的续传 ------------------------------------------------ */
static void case_resume_after_reset(void)
{
    uint32_t k;

    printf("case 8: 复位后续传（元数据驱动）\n");
    l5_reset();
    check_eq((long)fota_receive_begin(NULL, 0U), 0L, "fota_receive_begin");

    feed_up_to_data(1U);                     /* START + DATA 0 落盘 */

    check_eq((long)fota_get_staged_bytes(),
             (long)L5_ENV_SIZE + (long)L5_CHUNK_SIZE, "DATA 0 应已提交");
    check_eq((long)fota_get_pending_slot(), (long)FOTA_META_SLOT_B, "元数据应已写");

    /* 模拟一次复位：UART 交还（CLI 重新初始化）、FOTA 重新初始化。
     * **Flash 与备份域不动** —— 这正是续传要依赖的两样。 */
    cli_set_rx_sink(NULL);
    fota_init();

    check_eq((long)fota_get_state(), (long)FOTA_STATE_IDLE,
             "复位后应回到 IDLE（不主动恢复内存态）");

    check_eq((long)fota_receive_begin(NULL, 0U), 0L, "重新进入接收");

    resp_reset();
    l5_feed(frame_at(0U), frame_len(0U));            /* 重发 START */
    /* 身份一致 ⇒ 续传：ACK 直接携带断点序号 1，而不是 0 */
    check(resp_is(resp_frame(1U), 3U), "续传时 ACK 应携带断点序号 1");
    check_eq((long)fota_get_staged_bytes(),
             (long)L5_ENV_SIZE + (long)L5_CHUNK_SIZE,
             "续传不得重擦暂存区（否则前面的数据白收）");
    check_eq((long)fota_get_last_error(), (long)FOTA_E_NONE, "续传不应有错误");

    /* 接着发剩下的分片（只发数据帧，FINISH 单独发）与 FINISH */
    for (k = 2U; k + 1U < (uint32_t)L5_FRAME_COUNT; k++) {
        l5_feed(frame_at(k), frame_len(k));
    }
    resp_reset();
    l5_feed(frame_at(L5_FRAME_COUNT - 1U), frame_len(L5_FRAME_COUNT - 1U));
    check_eq((long)fota_get_state(), (long)FOTA_STATE_READY, "续传后应 READY");
    check(resp_is(resp_frame((uint32_t)L5_RESP_COUNT - 1U), 3U),
          "FINISH 应被 ACK");
    check_eq((long)fota_get_staged_bytes(), (long)L5_PATCH_SIZE, "应已收满");

    /* 收满之后仍能应用 —— 续传路径与一次跑完的路径必须等价 */
    fota_process();
    check_eq((long)fota_get_state(), (long)FOTA_STATE_DONE, "续传后应用应成功");
    check(target_slot_equals_new(), "续传后目标槽内容应等同于一次跑完");
}

/* ---- 4.9 收齐后掉电：重启仍可应用（不重传） ------------------------------ */
static void case_ready_survives_reset(void)
{
    printf("case 9: 收齐后掉电 ⇒ 重启仍可 apply（不重传）\n");
    l5_reset();
    check_eq((long)fota_receive_begin(NULL, 0U), 0L, "fota_receive_begin");

    /* 一路收完（START + 全部分片 + FINISH）走到 READY，但**不**调用
     * `fota_process()` —— 也就是停在"已通过 CRC32、等着应用"这一刻。 */
    feed_up_to_data((uint32_t)L5_NCHUNKS);
    resp_reset();
    l5_feed(frame_at(L5_FRAME_COUNT - 1U), frame_len(L5_FRAME_COUNT - 1U));
    check_eq((long)fota_get_state(), (long)FOTA_STATE_READY, "应停在 READY");
    check_eq((long)fota_get_staged_bytes(), (long)L5_PATCH_SIZE, "应已收满");

    /* 模拟复位：UART 交还、FOTA 重新初始化，**Flash 与备份域不动**。
     * 用 `fota_init()` 而不是 `l5_reset()` 正是本用例的关键 ——
     * `l5_reset()` 会把整片 Flash 抹成 0xFF，那样测的就不是"掉电后重启"
     * 而是"换了台设备"。 */
    cli_set_rx_sink(NULL);
    fota_init();

    /* 记录里那条 READY 的意义就是"整条补丁已通过 CRC32 并落盘"，所以重启后
     * 必须把上下文重建起来（信封就在暂存区头部，落盘正是为了这条路径）。
     * 退回 IDLE 的代价不是"多等一会儿"：下一次 START 的续传判定只认
     * RECEIVING 记录，于是会走完整重传，把这份已校验通过的补丁擦掉重收。 */
    check_eq((long)fota_get_state(), (long)FOTA_STATE_READY,
             "复位后应恢复 READY（不得退回 IDLE）");
    check_eq((long)fota_get_staged_bytes(), (long)L5_PATCH_SIZE,
             "重启后暂存进度应完好");
    check_eq((long)fota_get_pending_slot(), (long)FOTA_META_SLOT_B,
             "重启后待启动槽应仍是 B");

    /* 有一条**已校验通过、等着启动**的补丁时拒绝进入接收：允许进入就等于
     * 允许静默丢弃它（要这么做得先显式 erase）。 */
    check_eq((long)fota_receive_begin(NULL, 0U), -1L, "有待应用补丁时应拒绝接收");
    check_eq((long)fota_get_last_error(), (long)FOTA_E_BUSY, "拒绝原因应为 BUSY");
    check_eq((long)fota_get_state(), (long)FOTA_STATE_READY, "拒绝后应留在 READY");

    /* 而这个拒绝**不是**死路：重启前 READY 能做的事，重启后同样能做。
     * 但**不是**自动就做 —— `fota_init` 在每次上电都会跑，而"刚上电就自己
     * 擦掉一个槽"是这条路径刻意避免的（见 fota_init 的 READY 分支注释）。 */
    fota_process();
    check_eq((long)fota_get_state(), (long)FOTA_STATE_READY,
             "fota_process 不应在重启后自动开始应用");

    check_eq((long)fota_apply_request(), 0L, "显式 fota apply 应被接受");
    fota_process();
    check_eq((long)fota_get_state(), (long)FOTA_STATE_DONE,
             "重启后 apply 应成功（无需主机重传）");
    check(target_slot_equals_new(), "重启后应用结果应与一次跑完等价");
    check_eq((long)fota_get_last_error(), (long)FOTA_E_NONE, "不应有错误");
}

/* ---- 4.10 空闲超时 -------------------------------------------------------- */
static void case_idle_timeout(void)
{
    printf("case 10: 空闲超时交还 UART\n");
    l5_reset();
    check_eq((long)fota_receive_begin(NULL, 0U), 0L, "fota_receive_begin");

    /* 这一步很关键：必须**已经收下一片数据**再静默，否则"超时不丢进度"
     * 这条断言会是空的（只有 48 B 信封时，续传与否都是 ACK(0)，看不出差别）。 */
    feed_up_to_data(1U);
    check_eq((long)fota_get_staged_bytes(),
             (long)L5_ENV_SIZE + (long)L5_CHUNK_SIZE, "DATA 0 应已提交");

    fota_process();                                   /* 还没超时：不应有任何动作 */
    check(cli_rx_sink_active() != 0U, "未超时不得交还 UART");
    check_eq((long)fota_get_state(), (long)FOTA_STATE_RECEIVING, "未超时应仍在接收");

    mock_tick_advance((uint32_t)FOTA_ACK_TIMEOUT_MS + 1U);
    fota_process();

    check(cli_rx_sink_active() == 0U, "超时后必须交还 UART");
    check_eq((long)fota_get_state(), (long)FOTA_STATE_IDLE, "超时后应回到 IDLE");
    check_eq((long)fota_get_staged_bytes(),
             (long)L5_ENV_SIZE + (long)L5_CHUNK_SIZE,
             "超时**不得**清暂存区：主机应当还能续传");

    /* 续传仍成立：重发 START 之后 ACK 直接携带断点序号 1。 */
    check_eq((long)fota_receive_begin(NULL, 0U), 0L, "超时后应能重新接收");
    resp_reset();
    l5_feed(frame_at(0U), frame_len(0U));
    check(resp_is(resp_frame(1U), 3U), "超时后重发 START 应续传（ACK 携带序号 1）");
}

/* ---- 4.10 应用失败必须失效安全 ------------------------------------------- */
static void case_apply_failure_failsafe(void)
{
    uint32_t i;

    printf("case 11: 应用失败（活动槽实况与信封不符）⇒ 失效安全\n");
    l5_reset();
    check_eq((long)fota_receive_begin(NULL, 0U), 0L, "fota_receive_begin");

    feed_up_to_data((uint32_t)L5_NCHUNKS);
    resp_reset();
    l5_feed(frame_at(L5_FRAME_COUNT - 1U), frame_len(L5_FRAME_COUNT - 1U));
    check_eq((long)fota_get_state(), (long)FOTA_STATE_READY, "应已收齐待应用");

    /* 让活动槽"变心"：补丁针对的是另一版固件。
     * 用改一个字节的方式模拟 —— 它破坏的是 old_crc32 的实况校验。 */
    for (i = 0U; i < 8U; i++) {
        mock_flash_poke((uint32_t)L5_SLOT_A_BASE + 0x100U + i, (uint8_t)(0x5AU ^ i));
    }

    fota_process();

    check_eq((long)fota_get_state(), (long)FOTA_STATE_ERROR, "应用失败应进 ERROR");
    check(fota_get_last_error() < 0,
          "错误码应是 fota_delta 的（负数），表示是解码/校验环节失败");
    check_eq((long)hw2c_fault_last(), (long)HW2C_FAULT_FOTA_APPLY,
             "必须走故障陷阱（置安全态 → 记录 → 复位），而不是静默继续");
    check(mock_NVIC_SystemReset_called(), "陷阱必须请求复位");
    check(target_header_is_erased(),
          "半成品目标槽必须结构上不可引导（magic 从未写入）");
    check(cli_rx_sink_active() == 0U, "ERROR 之后 UART 应已交还");
}

/* ===========================================================================
 * 5. main
 * =========================================================================== */

int main(void)
{
    /* 关掉 stdout 缓冲。测试台一旦崩在某个用例里，被缓冲住的输出会全部丢掉，
     * 于是"最后打印到哪一条"这个最有用的线索就没了（本测试台第一次真的段错误
     * 时就是这样：只有退出码，什么也看不到）。 */
    setvbuf(stdout, NULL, _IONBF, 0);

    /* 夹具前置：本测试台假定"活动槽 = A"（BKP1R == 0 ⇒ 目标槽 = B）。
     * 若这个约定变了，下面所有用例的语义会**静默反转** —— 宁可在这里
     * 直接停下。 */
    if (TAMP->BKP1R != 0U) {
        fprintf(stderr, "FATAL: 夹具假定 BKP1R == 0（活动槽 A），实际 %u\n",
                (unsigned)TAMP->BKP1R);
        return 2;
    }
    if (L5_NCHUNKS < 2U) {
        fprintf(stderr,
                "FATAL: 向量只有 %u 个分片，续传/乱序用例需要 ≥ 2 个。"
                "补丁必须大于 env + chunk_size。\n", (unsigned)L5_NCHUNKS);
        return 2;
    }
    /* `drv_fota` 的读回路径按真机语义直接用 Flash 地址取内容，因此这个
     * 测试台**要求** mock 的存储确实位于 0x08000000。映射不可用时必须在这里
     * 明确报错 —— 否则表现会是一句没有上下文的段错误。 */
    if (mock_flash_map() == 0 || mock_flash_is_mapped() == 0) {
        fprintf(stderr,
                "FATAL: 无法把 mock Flash 映射到 0x%08X —— 本测试台依赖"
                "「Flash 可按地址直接取」这一真机语义。\n",
                (unsigned)MOCK_FLASH_BASE);
        return 2;
    }

    printf("L5 harness: drv_fota protocol (patch %u B, %u chunks, %u frames)\n",
           (unsigned)L5_PATCH_SIZE, (unsigned)L5_NCHUNKS,
           (unsigned)L5_FRAME_COUNT);

#define L5_CASE(fn)                                     \
    do {                                                \
        g_case_failures = 0;                            \
        fn;                                             \
        if (g_case_failures != 0) {                     \
            printf("  -> %d failure(s)\n", g_case_failures); \
            g_failures += g_case_failures;              \
        }                                               \
    } while (0)

    L5_CASE(case_handover_and_noise());
    L5_CASE(case_happy_path());
    L5_CASE(case_happy_path_dbank2());
    L5_CASE(case_data_dup_oOO_corrupt());
    L5_CASE(case_finish_short());
    L5_CASE(case_finish_bad_crc());
    L5_CASE(case_start_rejections());
    L5_CASE(case_resume_after_reset());
    L5_CASE(case_ready_survives_reset());
    L5_CASE(case_idle_timeout());
    L5_CASE(case_apply_failure_failsafe());

    if (g_resp_drop != 0U) {
        printf("  [FAIL] 应答缓冲溢出 %u 次\n", (unsigned)g_resp_drop);
        g_failures++;
    }

    if (g_failures != 0) {
        printf("RESULT: FAILED (%d failures)\n", g_failures);
        return 1;
    }
    printf("RESULT: OK (11 cases, %u response bytes checked)\n",
           (unsigned)L5_RESP_SIZE);
    return 0;
}
