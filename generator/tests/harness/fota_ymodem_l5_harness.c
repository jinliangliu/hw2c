/*
 * fota_ymodem_l5_harness.c — L5：YMODEM 接收通道（drv_fota_ymodem.c）的主机测试台
 *
 * 这一层回答的问题
 * ----------------
 * "主机用**任意终端软件**的 YMODEM 发送功能发出去的那串字节，设备收成了
 *  什么？" —— 它不需要真板：`drv_fota_ymodem` 的全部外部依赖只有四样
 * （CLI 的字节流、tick、Flash、会话层），前三样在 `mock_hal` 里都是**语义
 * 正确**的模型，第四样就是仓库自己的 `drv_fota.c`（一起编进来，不是替身）。
 *
 * 为什么要单开一个台架，而不是塞进 fota_protocol_l5_harness.c
 * ---------------------------------------------------------
 * 两者共享的东西（会话层、暂存、元数据、CRC32）已经由那一个覆盖了。这里要
 * 覆盖的是**只有 YMODEM 才有**的那些点，而它们恰好都是"错了也不报错"的类型：
 *
 *   · 块尾 CRC 用的是 **CRC-16/XMODEM（初值 0x0000）**，不是本仓库帧协议的
 *     CRC-16/CCITT-FALSE（初值 0xFFFF）。用错的那个方向"自研主机 ↔ 设备"
 *     完全互通，但与所有真实 YMODEM 软件都不通 —— 所以向量必须来自**另一份
 *     实现**（`generator/fota_ymodem_sender.py`，且它的 CRC 由 stdlib 的
 *     `binascii.crc_hqx` 独立验算）。
 *   · 末块用 0x1A 补齐到块长，**必须按块 0 声明的长度截断**。不截断的症状是
 *     "所有块 CRC 都对，但解出来的镜像最后一段是坏的"。
 *   · CAN（0x18）只在**块边界**才算中止。补丁正文是接近随机的字节流，块内也
 *     识别就会把正常传输随机打断。
 *   · 块号错乱但 CRC 合法的链路，必须能打穿重试上限（否则无限 NAK、UART 永远
 *     不还给 CLI）。
 *
 * 向量从哪来
 * ----------
 * 全部上行字节由 `generator/fota_ymodem_sender.py::batch_plan()` 与它的几个
 * 负例计划产出，烘成 `fota_ymodem_vectors.h`。每个"计划"是
 * `[(要发的字节, 期望的应答字节, 说明)]` —— 台架只负责"发出去、比对应答"，
 * 不自己拼块、不算 CRC。这是刻意的：台架里再写一遍组块，就等于把"跨实现
 * 比对"退化成"自己和自己对答案"。
 *
 * 输出：`RESULT: OK (...)` 或 `RESULT: FAILED (n failures)`；退出码 0/1。
 */

#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <stdint.h>

/* `ym_l5_config.h` 由夹具生成，只放"与工程命名相关"的名字（CLI 头文件名）。
 * 必须**最先**包含：下面的 CLI 头文件名是个宏。 */
#include "ym_l5_config.h"

#include "mock_hal.h"
#include L5_CLI_HEADER               /* cli_rx_sink_t + 三个交接原语（真头文件） */
#include "drv_fota.h"
#include "drv_fota_ymodem.h"
#include "hw2c_fault.h"

#include "fota_ymodem_vectors.h"      /* 需要 FOTA_E_* 可见 */

/* ===========================================================================
 * 1. CLI 字节流的模型（与帧协议台架同一份语义，故意重复：两个台架互不依赖，
 *    任何一个坏掉都不该让另一个编不过）
 * =========================================================================== */

static cli_rx_sink_t g_sink = NULL;
static uint8_t       g_resp[4096];
static uint32_t      g_resp_len  = 0U;
static uint32_t      g_resp_drop = 0U;
static uint32_t      g_feed_lost = 0U;

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

/* 真 API 是 `IWDG_Refresh()`（drv_iwdg.c）。这里只计数：喂狗的频次在 L6 里
 * 已按"每次 Flash 操作一次"验证过，这里只确认调用不会缺失。 */
void IWDG_Refresh(void) { }

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

static void hex_of(char *out, size_t out_len, const uint8_t *p, uint32_t n)
{
    uint32_t i;
    size_t   used = 0U;
    out[0] = '\0';
    for (i = 0U; i < n && i < 16U && used + 4U < out_len; i++) {
        used += (size_t)snprintf(out + used, out_len - used, "%02X ", p[i]);
    }
    if (n > 16U && used + 4U < out_len) {
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

static int active_slot_unchanged(void)
{
    return memcmp(flash_ptr((uint32_t)YM_SLOT_A_BASE), g_ym_old_image,
                  (size_t)YM_OLD_IMAGE_SIZE) == 0;
}

static int target_slot_equals_new(void)
{
    return memcmp(flash_ptr((uint32_t)YM_SLOT_B_BASE), g_ym_new_image,
                  (size_t)YM_NEW_IMAGE_SIZE) == 0;
}

/* 暂存区基址。与设备侧同一个式子（目标槽尾部、按 8 字节对齐倒推）——
 * 在这里重算是可接受的：它只用于**比对内容**，不参与任何判定。 */
static uint32_t staging_base(void)
{
    uint32_t total = ((uint32_t)YM_ENV_SIZE + (uint32_t)YM_RECORD_SIZE
                      - (uint32_t)YM_ENV_SIZE + 7U) & ~7U;
    /* total == align8(record_size) */
    return (uint32_t)YM_SLOT_B_BASE + (uint32_t)YM_SLOT_B_SIZE - total;
}

static int staging_equals_record(const unsigned char *record, uint32_t n)
{
    return memcmp(flash_ptr(staging_base()), record, (size_t)n) == 0;
}

static void seed_active_slot(void)
{
    uint32_t i;
    for (i = 0U; i < (uint32_t)YM_OLD_IMAGE_SIZE; i++) {
        mock_flash_poke((uint32_t)YM_SLOT_A_BASE + i, g_ym_old_image[i]);
    }
}

static void ym_reset(void)
{
    mock_cmsis_reset();
    mock_flash_reset();
    mock_tick_reset();
    mock_NVIC_SystemReset_reset();
    hw2c_fault_clear();

    g_sink        = NULL;
    g_resp_len    = 0U;
    g_resp_drop   = 0U;
    g_feed_lost   = 0U;

    seed_active_slot();
    fota_init();
}

/* 按真机的粒度投递：cli_task 是"取一批、投一批"。7 字节与所有块长（133/1029）
 * 都互质，于是每一块都会被切在非边界位置上 —— 块内累积的状态推进被真正覆盖
 * （一次投一整块的测试覆盖不到"半块"这一层）。 */
#define YM_FEED_BLOCK 7U

static void ym_feed(const uint8_t *data, uint32_t len)
{
    uint32_t i = 0U;

    while (i < len) {
        uint32_t take = len - i;
        if (take > YM_FEED_BLOCK) {
            take = YM_FEED_BLOCK;
        }
        if (g_sink == NULL) {
            g_feed_lost += (len - i);
            return;
        }
        g_sink(&data[i], (uint16_t)take);
        i += take;
    }
}

static void resp_reset(void) { g_resp_len = 0U; }

/* 一步 = 发一段上行字节、比对设备答的字节。期望值来自 Python 侧的计划。 */
static void run_step(const ym_plan_t *plan, uint32_t k)
{
    const uint8_t *snd = &plan->batch[plan->snd_off[k]];
    uint32_t       slen = (uint32_t)plan->snd_len[k];
    const uint8_t *exp = &plan->exp[plan->exp_off[k]];
    uint32_t       elen = (uint32_t)plan->exp_len[k];
    char got_hex[80];
    char exp_hex[80];
    char what[192];

    resp_reset();
    ym_feed(snd, slen);

    if (g_resp_len != elen || memcmp(g_resp, exp, elen) != 0) {
        hex_of(got_hex, sizeof(got_hex), g_resp, g_resp_len);
        hex_of(exp_hex, sizeof(exp_hex), exp, elen);
        (void)snprintf(what, sizeof(what),
                       "step %u (%s): device said [%s], protocol says [%s]",
                       (unsigned)k, plan->notes[k], got_hex, exp_hex);
        check(0, what);
    }
}

/* 一直跑到第 upto 步（不含）。返回失败的步数。 */
static int run_plan_prefix(const ym_plan_t *plan, uint32_t upto,
                           const char *what)
{
    uint32_t k;
    int before = g_case_failures;

    for (k = 0U; k < upto && k < plan->nsteps; k++) {
        run_step(plan, k);
    }
    if (g_case_failures != before) {
        fprintf(stderr, "  [FAIL] %s: plan 前 %u 步里有应答不符\n",
                what, (unsigned)upto);
    }
    return g_case_failures - before;
}

/* 整批跑完。 */
static void run_plan(const ym_plan_t *plan, const char *what)
{
    (void)run_plan_prefix(plan, plan->nsteps, what);
}

/* 进接收态：必须能装上 sink（否则字节根本进不来）。 */
static void ym_begin(void)
{
    char msg[64];

    msg[0] = '\0';
    check_eq((long)fota_ymodem_begin(msg, sizeof(msg)), 0L,
             "fota_ymodem_begin 应当成功");
    check(cli_rx_sink_active() != 0U, "接管后 sink 必须装好");
}

/* ===========================================================================
 * 4. 用例
 * =========================================================================== */

/* ---- 4.1 握手：进入接收就发 'C'，且日志文本不会被打断 ---------------------- */
static void case_handshake_and_noise(void)
{
    static const uint8_t noise[] = "\r\nhw2c boot: System ready\r\n";

    printf("case 1: 握手与噪声免疫\n");
    ym_reset();

    /* 没接管之前，投进去的字节不该有任何后果 */
    ym_feed((const uint8_t *)noise, (uint32_t)(sizeof(noise) - 1U));
    check_eq((long)g_feed_lost, (long)(sizeof(noise) - 1U),
             "未接管时字节应被记为投递失败（sink 为 NULL）");
    check_eq((long)g_resp_len, 0L, "未接管时不应有任何字节发出");

    resp_reset();
    ym_begin();

    /* ⚠️ `fota_ymodem_begin()` 必须**立刻**发一个 'C'，不能等一个握手周期 ——
     * 操作员刚敲完命令就在等这个信号，终端软件靠它才知道设备准备好了。 */
    check_eq((long)g_resp_len, 1L, "进入接收应当立刻发出 1 个字节");
    check_eq((long)g_resp[0], (long)YMODEM_CTL_CRC_REQUEST, "那一个字节必须是 'C'");

    /* 块边界上的噪声（终端软件的回显、日志）应当被无声丢掉 */
    resp_reset();
    ym_feed((const uint8_t *)noise, (uint32_t)(sizeof(noise) - 1U));
    check_eq((long)g_resp_len, 0L, "噪声不该换来任何应答");
    check_eq((long)fota_get_state(), (long)FOTA_STATE_IDLE,
             "还没收到数据块时不该进入 RECEIVING（会话在第一块才打开）");
}

/* ---- 4.2 正常批次（1 K 块）：逐字节比对应答流 ------------------------------- */
static void case_happy_path_1k(void)
{
    printf("case 2: 正常批次（1024 字节块）\n");
    ym_reset();
    resp_reset();
    ym_begin();

    /* 计划是 [块0, 数据块×N, EOT#1, EOT#2, 结束块]。逐块跑，并在关键节点
     * 断言设备侧的**可观测状态**（不只是应答字节）。 */
    run_step(&g_plan_1k, 0U);            /* 块 0 */
    check_eq((long)fota_get_state(), (long)FOTA_STATE_IDLE,
             "块 0 之后还不该打开会话（准入要等信封）");

    /* 第一个数据块：会话在这里打开 —— 此刻才允许擦暂存区 */
    run_step(&g_plan_1k, 1U);
    check_eq((long)fota_get_state(), (long)FOTA_STATE_RECEIVING,
             "第一个数据块之后应当进入 RECEIVING");
    check_eq((long)fota_get_pending_slot(), (long)FOTA_META_SLOT_B,
             "活动槽是 A ⇒ 目标槽应当是 B");

    /* 其余数据块（最后 4 步依次是 EOT#1 / EOT#2 / 结束块，还有块 0 与第一块） */
    {
        uint32_t k;
        for (k = 2U; k + 4U <= g_plan_1k.nsteps; k++) {
            run_step(&g_plan_1k, k);
        }
    }

    /* EOT 两拍 + 结束块 */
    run_step(&g_plan_1k, g_plan_1k.nsteps - 3U);
    run_step(&g_plan_1k, g_plan_1k.nsteps - 2U);
    check_eq((long)fota_get_state(), (long)FOTA_STATE_READY,
             "EOT 的第二拍 ACK 之后应当已经提交 READY");
    run_step(&g_plan_1k, g_plan_1k.nsteps - 1U);

    check_eq((long)fota_get_state(), (long)FOTA_STATE_READY, "批次结束应为 READY");
    check_eq((long)fota_get_last_error(), (long)FOTA_E_NONE, "不该有错误码");

    /* ⚠️ 这一条是"末块按声明长度截断"的判据。
     *
     * 记录长度不是块长的整数倍 ⇒ 末块被 0x1A 补齐。不截断的话，写进暂存区的
     * 字节数会比记录长，`fota_stage_verify()` 的回读 CRC32 就会不符 ——
     * 症状是"所有块 CRC 都对，但补丁被拒"。 */
    check_eq((long)fota_get_staged_bytes(), (long)YM_RECORD_SIZE,
             "写进暂存区的字节数必须等于块 0 声明的长度（0x1A 补齐必须被截掉）");

    check(staging_equals_record(g_ym_record, (uint32_t)YM_RECORD_SIZE),
          "暂存区内容必须与补丁**逐字节相同**");
    check(active_slot_unchanged(), "传输阶段不允许碰活动槽");

    /* 交还 UART：批次收工后 sink 必须被摘掉，CLI 要能再用 */
    check(cli_rx_sink_active() == 0U, "批次收工后必须把 UART 还给 CLI");
}

/* ---- 4.3 正常批次（128 字节块）：SOH 路径 + 更多块 -------------------------- */
static void case_happy_path_128(void)
{
    printf("case 3: 正常批次（128 字节块 / SOH 路径）\n");
    ym_reset();
    resp_reset();
    ym_begin();
    run_plan(&g_plan_128, "128 字节块批次");

    check_eq((long)fota_get_state(), (long)FOTA_STATE_READY,
             "128 字节块批次也应当收成 READY");
    check_eq((long)fota_get_staged_bytes(), (long)YM_RECORD_SIZE,
             "128 字节块批次同样必须按声明长度截断");
    check(staging_equals_record(g_ym_record, (uint32_t)YM_RECORD_SIZE),
          "暂存区内容必须与补丁逐字节相同");
    /* 128 字节块意味着块号要走更多圈 —— 这条断言保证向量真的覆盖了"多块"，
     * 而不是退化成"只有一块、什么也没测到"。 */
    check(g_plan_128.nsteps > 8U,
          "128 字节块的批次应当有很多步（否则这条用例没覆盖到多块）");
}

/* ---- 4.4 重复块：ACK 丢了，主机重发同一块 ---------------------------------- */
static void case_duplicate_block(void)
{
    printf("case 4: 重复块（ACK 丢失）\n");
    ym_reset();
    resp_reset();
    ym_begin();

    run_step(&g_plan_dup, 0U);   /* 块 0 */
    run_step(&g_plan_dup, 1U);   /* 数据块 1 */
    {
        long staged_after_first = (long)fota_get_staged_bytes();
        run_step(&g_plan_dup, 2U);   /* 数据块 1 的重复 */
        check_eq((long)fota_get_staged_bytes(), staged_after_first,
                 "重复块不得被再写一遍（写两遍会让暂存区里那一段成为拼接）");
    }
    {
        uint32_t k;
        for (k = 3U; k < g_plan_dup.nsteps; k++) {
            run_step(&g_plan_dup, k);
        }
    }
    check_eq((long)fota_get_state(), (long)FOTA_STATE_READY,
             "含重复块的批次仍应收成 READY");
    check(staging_equals_record(g_ym_record, (uint32_t)YM_RECORD_SIZE),
          "重复块不得改变暂存区内容");
}

/* ---- 4.5 块号错乱 ⇒ 重试耗尽 ⇒ 设备必须主动中止 ----------------------------- */
static void case_desync_retry_exhaustion(void)
{
    printf("case 5: 块号错乱 -> 重试耗尽 -> 主动中止\n");
    ym_reset();
    resp_reset();
    ym_begin();

    /* ⚠️ 这条用例是 `ym_nak()` 的重试计数与"CRC 通过之后不许复位计数"这两件事
     * 的**唯一**判据。
     *
     * 造的链路状态是"每一块 CRC 都合法、块号却始终不对"。若把 `g_retry = 0`
     * 写在 CRC 通过之后（一处看起来无害的清理），计数器每一轮都会被清零，
     * 上限永远打不穿 —— 设备无限 NAK，UART 永远不还给 CLI，而**所有应答
     * 都是 NAK，看起来完全正常**。 */
    run_plan(&g_plan_desync, "块号错乱的批次");

    check_eq((long)fota_get_state(), (long)FOTA_STATE_IDLE,
             "中止后状态应退回 IDLE（不是 ERROR：什么都没落盘）");
    check_eq((long)fota_get_last_error(), (long)FOTA_E_ABORTED,
             "错误码应当是『被中止』");
    check(cli_rx_sink_active() == 0U, "中止后必须把 UART 还给 CLI");
    check(active_slot_unchanged(), "中止不该碰活动槽");
    check_eq((long)fota_get_pending_slot(), (long)FOTA_META_SLOT_NONE,
             "会话都没打开过 ⇒ 不该有『待启动槽』");
}

/* ---- 4.6 主机 CAN CAN 中止 -------------------------------------------------- */
static void case_host_cancel(void)
{
    printf("case 6: 主机 CAN CAN 中止\n");
    ym_reset();
    resp_reset();
    ym_begin();
    run_plan(&g_plan_cancel, "主机在块边界取消");

    check_eq((long)fota_get_state(), (long)FOTA_STATE_IDLE, "取消后应为 IDLE");
    check_eq((long)fota_get_last_error(), (long)FOTA_E_ABORTED, "错误码应为 ABORTED");
    check(cli_rx_sink_active() == 0U, "取消后必须把 UART 还给 CLI");
    check(active_slot_unchanged(), "取消不该碰活动槽");
}

/* ---- 4.7 块 0 声明的长度与信封不符 ⇒ 拒绝 ----------------------------------- */
static void case_length_mismatch(void)
{
    printf("case 7: 块 0 声明长度与信封不符 -> 拒绝\n");
    ym_reset();
    resp_reset();
    ym_begin();
    run_plan(&g_plan_badlen, "块 0 长度与信封不符");

    check_eq((long)fota_get_last_error(), (long)FOTA_E_LENGTH,
             "错误码应当是『长度不符』");
    check(cli_rx_sink_active() == 0U, "拒绝后必须把 UART 还给 CLI");
    check(active_slot_unchanged(), "拒绝不该碰活动槽");
    /* 关键：会话被拒绝时**不该**留下一条 RECEIVING 记录指向一个没擦过的区域 */
    check_eq((long)fota_get_pending_slot(), (long)FOTA_META_SLOT_NONE,
             "长度不符的信封不该留下待启动槽");
}

/* ---- 4.8 容量准入不过 ⇒ 拒绝，且不擦活动槽 ---------------------------------- */
static void case_admission_rejected(void)
{
    printf("case 8: 容量准入不过 -> 拒绝\n");
    ym_reset();
    resp_reset();
    ym_begin();
    run_plan(&g_plan_overadm, "准入不过的补丁");

    check_eq((long)fota_get_last_error(), (long)YM_EXPECT_OVER_ADMISSION,
             "错误码应当是『暂存区装不下』");
    check_eq((long)fota_get_state(), (long)FOTA_STATE_IDLE,
             "准入不过不该进 RECEIVING");
    check(cli_rx_sink_active() == 0U, "拒绝后必须把 UART 还给 CLI");
    /* 准入发生在**任何擦除之前**：既没擦暂存区，也没写元数据。 */
    check_eq((long)fota_get_pending_slot(), (long)FOTA_META_SLOT_NONE,
             "准入不过不该留下待启动槽");
    /* 准入公式用的是"实际会擦掉的整页数"，而不是 new_size。少算一页就会在
     * 应用期擦掉暂存区首页 —— 这条断言保证拒绝发生在**任何擦除之前**。 */
    check(active_slot_unchanged(), "准入不过时活动槽必须原样未动");
}

/* ---- 4.9 传输途中又冒出一个文件头 ⇒ 拒绝整个批次 ---------------------------- */
static void case_multi_header_rejected(void)
{
    printf("case 9: 传输途中出现第二个文件头 -> 拒绝\n");
    ym_reset();
    resp_reset();
    ym_begin();
    run_plan(&g_plan_multi, "多文件批次");

    check(cli_rx_sink_active() == 0U, "拒绝后必须把 UART 还给 CLI");
    check(active_slot_unchanged(), "拒绝不该碰活动槽");
    check_eq((long)fota_get_state(), (long)FOTA_STATE_IDLE,
             "多文件批次被拒后应为 IDLE（补丁没有收齐）");
}

/* ---- 4.10 块超时 ⇒ NAK，之后仍能接着收 -------------------------------------- */
static void case_block_timeout(void)
{
    printf("case 10: 块超时 -> NAK -> 接着收\n");
    ym_reset();
    resp_reset();
    ym_begin();

    run_step(&g_plan_1k, 0U);                  /* 块 0 */
    check_eq((long)fota_get_state(), (long)FOTA_STATE_IDLE, "块 0 之后仍等数据");

    /* 发**半块**：起始字节 + 3 个字节，然后什么都不发。 */
    {
        const uint8_t *blk = &g_plan_1k.batch[g_plan_1k.snd_off[1]];
        resp_reset();
        ym_feed(blk, 4U);
        check_eq((long)g_resp_len, 0L, "半块不该有任何应答");

        mock_tick_advance((uint32_t)YM_BLOCK_TIMEOUT_MS + 1U);
        fota_ymodem_process();
        check_eq((long)g_resp_len, 1L, "块超时应当答 1 个字节");
        check_eq((long)g_resp[0], (long)YMODEM_CTL_NAK, "那个字节必须是 NAK");
    }

    /* 半块被丢掉，重发同一块应当被正常接收 */
    run_step(&g_plan_1k, 1U);
    check_eq((long)fota_get_state(), (long)FOTA_STATE_RECEIVING,
             "超时后重发的块应当被正常接收");
}

/* ---- 4.11 握手超时 ⇒ 交还 UART、退回 IDLE（不是 ERROR） --------------------- */
static void case_handshake_timeout(void)
{
    printf("case 11: 握手超时（主机一直没开始）\n");
    ym_reset();
    resp_reset();
    ym_begin();

    /* 每次推进一个握手周期，数设备一共发了几个 'C'。 */
    {
        uint32_t c_count = g_resp_len;      /* 进入接收时那一个 */
        uint32_t i;

        for (i = 0U; i < 32U && cli_rx_sink_active() != 0U; i++) {
            uint32_t before = g_resp_len;

            mock_tick_advance((uint32_t)YM_HS_INTERVAL_MS);
            fota_ymodem_process();
            c_count += (g_resp_len - before);
        }
        check_eq((long)c_count, (long)YM_HS_MAX_ATTEMPTS,
                 "发的 'C' 总数应当等于真源里的 max_attempts");
    }

    check_eq((long)fota_get_state(), (long)FOTA_STATE_IDLE,
             "握手超时应当退回 IDLE —— 什么都没发生，不该要求人工清理");
    check_eq((long)fota_get_last_error(), (long)FOTA_E_IO, "错误码应为 IO");
    check(cli_rx_sink_active() == 0U, "放弃后必须把 UART 还给 CLI");
    check_eq((long)fota_get_pending_slot(), (long)FOTA_META_SLOT_NONE,
             "什么都没收 ⇒ 不该有『待启动槽』");
}

/* ---- 4.12 主机不发结束块 ⇒ 仍算成功 ----------------------------------------- */
static void case_missing_terminator(void)
{
    printf("case 12: 主机不发结束块 -> 仍算成功\n");
    ym_reset();
    resp_reset();
    ym_begin();

    /* 跑到 EOT 的第二拍（含）为止，不发结束块。 */
    (void)run_plan_prefix(&g_plan_1k, g_plan_1k.nsteps - 1U, "缺结束块");
    check_eq((long)fota_get_state(), (long)FOTA_STATE_READY,
             "EOT 之后补丁就已经是 READY 了");

    /* ⚠️ 结束块只是"这一批结束了"的礼节，有些发送端根本不发它。因为少一个
     * ACK 就把一次**已经通过回读 CRC32 的升级**判成失败是错的。
     *
     * 这里直接驱动 YMODEM 的节拍（而不是 `fota_process()`）：后者在 READY 且
     * 传输收工后会立刻开始应用并复位，把这条断言吃掉。应用阶段由 case 13 覆盖。 */
    mock_tick_advance((uint32_t)YM_TERMINATOR_TIMEOUT_MS + 1U);
    fota_ymodem_process();

    check_eq((long)fota_get_state(), (long)FOTA_STATE_READY,
             "结束块超时不该把成功的批次判失败");
    check(cli_rx_sink_active() == 0U, "收工后必须把 UART 还给 CLI");
}

/* ---- 4.13 收齐之后交给应用层（与帧协议的同一个出口） ------------------------- */
static void case_apply_after_batch(void)
{
    printf("case 13: 收齐 -> fota_process 应用 -> 目标槽是新镜像\n");
    ym_reset();
    resp_reset();
    ym_begin();
    run_plan(&g_plan_1k, "完整批次");

    check_eq((long)fota_get_state(), (long)FOTA_STATE_READY, "先到 READY");

    /* 交棒：`fota_process()` 在传输收工（transport == NONE）之后才应用。 */
    fota_process();

    check(target_slot_equals_new(), "应用之后目标槽必须等于新镜像");
    check(mock_NVIC_SystemReset_called(), "应用之后应当请求复位");
    check_eq((long)fota_get_state(), (long)FOTA_STATE_DONE, "应用后应为 DONE");
}

/* ---- 4.14 续传：重放前缀与原记录不符 ⇒ fail-fast ----------------------------- */
static void case_resume_prefix_mismatch(void)
{
    printf("case 14: 续传前缀不符 -> fail-fast\n");
    ym_reset();

    /* 造出"上一次传到一半"的现场：用会话层按帧协议的方式提交进度。
     *
     * 这不是在测会话层 —— 而是在造一个 YMODEM 必然遇到的状态：操作员上次用
     * `fota recv` 传到一半掉线，这次改用终端软件的 YMODEM 重发**整份**文件。
     * YMODEM 没有"从偏移续传"，主机只会从字节 0 重发，所以设备必须逐字节
     * 复核已经落盘的前缀。 */
    {
        char msg[64];
        msg[0] = '\0';
        check_eq((long)fota_session_open(g_ym_record, (uint32_t)YM_RECORD_SIZE,
                                        FOTA_STREAM_FROM_COMMIT_POINT),
                 0L, "种子会话应当打开成功");
        check_eq((long)fota_session_feed(&g_ym_record[YM_ENV_SIZE],
                                        (uint32_t)YM_SEED_PREFIX - (uint32_t)YM_ENV_SIZE),
                 0L, "种子喂入应当成功");
        fota_session_commit_progress();
        fota_session_abort();
        check_eq((long)fota_get_state(), (long)FOTA_STATE_IDLE, "种子后应回 IDLE");
    }

    resp_reset();
    ym_begin();

    /* 发一份**信封相同、正文不同**的记录：信封一致 ⇒ 续传判定成立（不会重擦），
     * 正文在第 YM_RECORD_CORRUPT_AT 字节处与暂存区不符 ⇒ 必须立刻中止。
     *
     * 为什么这条断言值钱：回读 CRC32（在收尾处）**也能**发现这份数据是错的，
     * 但那要多等一整次传输。fail-fast 的意义就在于不必等 —— 没有它，这条用例
     * 会以"全部收完、最后报 E_PATCH_CRC"的形式通过，看起来还挺正常。 */
    run_plan(&g_plan_corrupt, "正文与已落盘前缀不符");

    check_eq((long)fota_get_last_error(), (long)FOTA_E_STAGING_LOST,
             "错误码应当是『已落盘的与主机重发的不是同一份』");
    check_eq((long)fota_get_state(), (long)FOTA_STATE_IDLE, "中止后应为 IDLE");
    check(cli_rx_sink_active() == 0U, "中止后必须把 UART 还给 CLI");
    check(active_slot_unchanged(), "中止不该碰活动槽");
}

/* ===========================================================================
 * 5. main
 * =========================================================================== */

typedef void (*case_fn)(void);

static const char *const g_case_names[] = {
    "handshake and noise",
    "happy path (1024 B blocks)",
    "happy path (128 B blocks)",
    "duplicate block",
    "desync -> retry exhaustion",
    "host cancel (CAN CAN)",
    "block-0 length mismatch",
    "admission rejected",
    "second header mid-transfer",
    "block timeout",
    "handshake timeout",
    "missing terminator block",
    "apply after batch",
    "resume prefix mismatch",
};

int main(void)
{
    uint32_t i;

    static const case_fn cases[] = {
        case_handshake_and_noise,
        case_happy_path_1k,
        case_happy_path_128,
        case_duplicate_block,
        case_desync_retry_exhaustion,
        case_host_cancel,
        case_length_mismatch,
        case_admission_rejected,
        case_multi_header_rejected,
        case_block_timeout,
        case_handshake_timeout,
        case_missing_terminator,
        case_apply_after_batch,
        case_resume_prefix_mismatch,
    };

    for (i = 0U; i < (uint32_t)(sizeof(cases) / sizeof(cases[0])); i++) {
        g_case_failures = 0;
        cases[i]();
        if (g_case_failures != 0) {
            g_failures += g_case_failures;
            printf("case %u: FAILED (%d)\n", (unsigned)(i + 1U), g_case_failures);
        } else {
            printf("case %u: ok  -- %s\n", (unsigned)(i + 1U), g_case_names[i]);
        }
    }

    if (g_failures != 0) {
        printf("RESULT: FAILED (%d failures)\n", g_failures);
        return 1;
    }
    printf("RESULT: OK (14 cases, %u response bytes checked)\n",
           (unsigned)g_resp_len);
    return 0;
}
