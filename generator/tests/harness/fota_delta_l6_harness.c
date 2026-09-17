/*
 * fota_delta_l6_harness.c — L6 掉电注入主机测试台（规划 §12 的 L6）
 *
 * 它测的是**真实源码**：drv_fota_delta.c 与 vendored 解码器都被原样编译进来，
 * 只有 Flash 那一层换成内存实现。这正是规划 §5.1 分层设计的回报 ——
 * 掉电安全是差分 OTA 唯一真正危险的地方，而它可以在主机上被完整枚举。
 *
 * 为什么不能用 mock 替身（A9 的教训）
 * ---------------------------------
 * 本测试台**不替换** drv_fota_delta.c 的任何内容，也不用 no-op 顶掉 I/O。
 * 后端是一个"行为正确 + 可注入失败"的内存 Flash：擦除真的把页变 0xFF，
 * 编程真的做 1→0 的按位与；越界、非 8 字节对齐、长度非 8 的整数倍一律报错
 * （与 STM32G0 双字编程的前置条件一致）。宽容的后端会把真实的地址计算错误
 * 掩盖掉，那样主机上的结论对目标就没有预测力。
 *
 * 故障模型
 * --------
 * 每一个"持久化操作"（patch_read / erase / program）都可以在第 k 次被注入
 * 掉电。三类的物理含义不同，中断后的落盘状态也不同：
 *
 *   · patch_read → 补丁流被截断（传输中断）；语义是"到此为止，EOF"
 *   · erase      → 擦到一半断电：按 8 字节粒度，已擦的保持擦除态
 *   · program    → 写一半断电：STM32G0 双字编程是原子的，已写的双字落盘，
 *                  未写的保持擦除态
 *
 * 注入后立即 longjmp 出 apply()，模拟"CPU 没了"（不返回、不清理）。
 *
 * 断言（规划 §12 的 L6 三条）
 * --------------------------
 *   ① 活动槽内容**逐字节**不变；
 *   ② 目标槽镜像头 magic 恒为擦除态 0xFFFFFFFF ⇒ 结构上不可能被引导；
 *   ③ 掉电后**重放**同一补丁，最终目标槽逐字节等于新镜像。
 *
 * ② 为什么最关键：magic 由设备在 FLUSH 阶段独占写入（见 drv_fota_delta.h 的
 * FOTA_HDR_HOLE_* 说明），所以"掉电后目标槽不可引导"是**结构性**的，不依赖
 * "引导路径上每一处都必须先验 CRC"这个跨子系统假设。若有人把 magic 改回从
 * 补丁流里取，本测试会在第一次注入时就失败。
 */

#include <setjmp.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#include "drv_fota_delta.h"
#include "fota_l6_vectors.h"

/* ---------------------------------------------------------------------------
 * 内存 Flash
 * ------------------------------------------------------------------------- */

#define FLASH_BASE      0x08000000UL
#define FLASH_SIZE      (512UL * 1024UL)
#define FLASH_ERASED    0xFFU

static unsigned char g_flash[FLASH_SIZE];

static int in_flash(uint32_t addr, uint32_t len)
{
    if (addr < FLASH_BASE) {
        return 0;
    }
    return ((addr - FLASH_BASE) + len) <= FLASH_SIZE;
}

static unsigned char *flash_ptr(uint32_t addr)
{
    return &g_flash[addr - FLASH_BASE];
}

/* ---------------------------------------------------------------------------
 * 故障注入
 * ------------------------------------------------------------------------- */

typedef struct {
    long   fail_at;        /* 第几次持久化操作注入掉电；0 = 不注入 */
    long   persist_ops;    /* 已发生的持久化操作数 */
    long   stop_units;     /* 该操作**已完成**的 8 字节单元数（掉电点） */
    int    armed;          /* setjmp 就位后才允许 longjmp */
    int    record;         /* 只有基线枚举才写操作表（见下） */
    jmp_buf escape;
} fault_t;

static fault_t g_fault;

/* 基线运行里记录每个操作的元数据。
 * 覆盖头部的操作要**逐个前缀**枚举（头部的写入位置决定 magic 何时出现），
 * 其余操作抽样即可 —— 一个没写完的页无论断在哪里都不影响断言。
 *
 * `record` 为什么必须有：注入用例跑完之后会**重放**同一份补丁，那次重放同样
 * 会调用 op_begin。若不加门控，重放就会改写操作表 —— 而重放时 persist_ops
 * 是从"断电点"续着的，于是整张表被整体平移一格。结果是主循环在 k 处读到的
 * 永远是某次重放的第一个操作（patch_read，units=0），枚举退化成"每个操作只
 * 注入 1 次"，L6 的覆盖率静默归零。第一版就踩了这个坑。 */
#define MAX_OPS 4096
static unsigned char g_op_touches_header[MAX_OPS + 1];
static long          g_op_units[MAX_OPS + 1];

static uint32_t g_hdr_lo;
static uint32_t g_hdr_hi;

/* 每次持久化操作调用一次。返回 1 表示"这一次应被掉电中断"。 */
static int op_begin(int touches_header, long units)
{
    g_fault.persist_ops++;
    if (g_fault.record && g_fault.persist_ops <= MAX_OPS) {
        g_op_touches_header[g_fault.persist_ops] = (unsigned char)touches_header;
        g_op_units[g_fault.persist_ops]          = units;
    }
    return (g_fault.fail_at != 0L) && (g_fault.persist_ops == g_fault.fail_at);
}

static void power_loss(void)
{
    if (g_fault.armed) {
        longjmp(g_fault.escape, 1);
    }
    fprintf(stderr, "FATAL: power loss injected with no setjmp target\n");
    exit(2);
}

/* ---------------------------------------------------------------------------
 * 补丁输入流
 * ------------------------------------------------------------------------- */

typedef struct {
    uint32_t pos;
} stream_t;

static stream_t g_stream;

static uint32_t stream_read(void *ctx, uint8_t *dst, uint32_t len)
{
    stream_t *s = (stream_t *)ctx;
    uint32_t  left;

    /* units = 0：补丁读没有"完成度"，只能在"一个字节都没读到"处断（见
     * n_prefix_points / prefix_units），所以这里传 0 表示不可细分。 */
    if (op_begin(0, 0L)) {
        power_loss();                 /* 链路被掐断：不返回任何字节 */
    }
    if (s->pos >= (uint32_t)FOTA_L6_PATCH_SIZE) {
        return 0U;                    /* 正常 EOF */
    }
    left = (uint32_t)FOTA_L6_PATCH_SIZE - s->pos;
    if (len > left) {
        len = left;
    }
    memcpy(dst, &g_l6_patch[s->pos], len);
    s->pos += len;
    return len;
}

/* ---------------------------------------------------------------------------
 * 后端实现
 * ------------------------------------------------------------------------- */

static int in_header_span(uint32_t addr, uint32_t len)
{
    return (addr < g_hdr_hi) && ((addr + len) > g_hdr_lo);
}

static int be_erase(void *ctx, uint32_t addr, uint32_t len)
{
    uint32_t a;
    uint32_t stop;
    uint32_t units;
    int      interrupted;

    (void)ctx;
    if (!in_flash(addr, len)) {
        return -1;
    }
    units     = len / 8U;
    interrupted = op_begin(in_header_span(addr, len), (long)units);
    /* 掉电点由外部给定；0 表示"一个字节都没擦" */
    stop = addr + ((interrupted ? (uint32_t)g_fault.stop_units : units) * 8U);
    for (a = addr; a < stop; a += 8U) {
        memset(flash_ptr(a), FLASH_ERASED, 8U);
    }
    if (interrupted) {
        power_loss();
    }
    return 0;
}

static int be_program(void *ctx, uint32_t addr, const uint8_t *src, uint32_t len)
{
    uint32_t i;
    uint32_t stop;
    uint32_t units;
    int      interrupted;

    (void)ctx;
    /* 与 STM32G0 双字编程的前置条件一致，违反即报错（不宽容接受） */
    if (!in_flash(addr, len) || (addr % 8U) != 0U || (len % 8U) != 0U) {
        return -1;
    }
    units       = len / 8U;
    interrupted = op_begin(in_header_span(addr, len), (long)units);
    stop        = (interrupted ? (uint32_t)g_fault.stop_units : units) * 8U;
    for (i = 0U; i < stop; i += 8U) {
        uint32_t w;
        for (w = 0U; w < 8U; w++) {
            unsigned char *p = flash_ptr(addr + i + w);
            *p = (unsigned char)(*p & src[i + w]);      /* Flash 只能 1 -> 0 */
        }
    }
    if (interrupted) {
        power_loss();
    }
    return 0;
}

static void be_read(void *ctx, uint32_t addr, uint8_t *dst, uint32_t len)
{
    (void)ctx;
    if (!in_flash(addr, len)) {
        fprintf(stderr, "FATAL: read outside flash: 0x%08X + %u\n", addr, len);
        exit(2);
    }
    memcpy(dst, flash_ptr(addr), len);
}

static void be_watchdog(void *ctx)
{
    (void)ctx;
}

static void be_progress(void *ctx, uint32_t done, uint32_t total)
{
    (void)ctx;
    (void)done;
    (void)total;
}

/* 注意 ctx 必须指向 g_stream：驱动侧（drv_fota_delta.c 的 stream_read）会把
 * `io->ctx` 原样透传给我们。ctx 留 NULL 会在这里空指针解引用 —— 第一次跑
 * L6 时就踩到了。main() 里显式赋值。 */
static fota_delta_backend_t g_io = {
    stream_read,
    be_erase,
    be_program,
    be_read,
    be_watchdog,
    be_progress,
    NULL
};

/* ---------------------------------------------------------------------------
 * 辅助
 * ------------------------------------------------------------------------- */

static int g_failures;

static void fail(long k, const char *what)
{
    fprintf(stderr, "  [FAIL] k=%ld: %s\n", k, what);
    g_failures++;
}

static uint32_t rd32(const unsigned char *p)
{
    return (uint32_t)p[0]
         | ((uint32_t)p[1] << 8)
         | ((uint32_t)p[2] << 16)
         | ((uint32_t)p[3] << 24);
}

static int active_slot_unchanged(void)
{
    return memcmp(flash_ptr((uint32_t)FOTA_SLOT_A_BASE), g_l6_old,
                  (size_t)FOTA_L6_OLD_IMAGE_SIZE) == 0;
}

static int target_slot_equals_new(void)
{
    return memcmp(flash_ptr((uint32_t)FOTA_SLOT_B_BASE), g_l6_new,
                  (size_t)FOTA_L6_NEW_IMAGE_SIZE) == 0;
}

static uint32_t target_header_magic(void)
{
    unsigned char hdr[FOTA_IMG_HDR_SIZE];
    be_read(NULL, (uint32_t)FOTA_SLOT_B_BASE + FOTA_IMG_HDR_OFF,
            hdr, (uint32_t)FOTA_IMG_HDR_SIZE);
    return rd32(&hdr[FOTA_IMG_OFF_MAGIC]);
}

/* ---------------------------------------------------------------------------
 * 一次完整运行（解析信封 → 应用）
 * ------------------------------------------------------------------------- */

static int run_apply(int *out_err)
{
    fota_delta_env_t env;
    int              rc;

    g_stream.pos = 0U;
    rc = fota_delta_parse_env(stream_read, &g_stream, &env);
    if (rc != FOTA_DELTA_OK) {
        if (out_err != NULL) { *out_err = rc; }
        return rc;
    }
    return fota_delta_apply(&env, &g_io,
                            (uint32_t)FOTA_SLOT_A_BASE,
                            (uint32_t)FOTA_SLOT_B_BASE,
                            (uint32_t)FOTA_SLOT_B_SIZE,
                            out_err);
}

static void seed_active_slot(void)
{
    g_fault.fail_at = 0L;             /* 种入活动槽时绝不注入 */
    if (be_program(NULL, (uint32_t)FOTA_SLOT_A_BASE,
                   g_l6_old, (uint32_t)FOTA_L6_OLD_IMAGE_SIZE) != 0) {
        fprintf(stderr, "FATAL: cannot seed active slot\n");
        exit(2);
    }
}

/* ---------------------------------------------------------------------------
 * 用例
 * ------------------------------------------------------------------------- */

static void case_no_fault(void)
{
    int rc;

    printf("case 0: no fault (baseline)\n");
    memset(g_flash, FLASH_ERASED, sizeof(g_flash));
    seed_active_slot();

    g_fault.armed = 0;
    g_fault.record = 0;
    rc = run_apply(NULL);
    if (rc != FOTA_DELTA_OK) {
        printf("  [FAIL] clean apply returned %d\n", rc);
        g_failures++;
        return;
    }
    if (!target_slot_equals_new()) {
        printf("  [FAIL] target slot != new image after a clean apply\n");
        g_failures++;
    }
    if (!active_slot_unchanged()) {
        printf("  [FAIL] active slot was modified by a clean apply\n");
        g_failures++;
    }
    if (fota_delta_check_vectors(&g_io, (uint32_t)FOTA_SLOT_B_BASE,
                                 (uint32_t)FOTA_SLOT_B_SIZE) != FOTA_DELTA_OK) {
        printf("  [FAIL] check_vectors rejected a freshly applied image\n");
        g_failures++;
    }
}

static long count_persist_ops(void)
{
    int  rc;
    long n;

    memset(g_flash, FLASH_ERASED, sizeof(g_flash));
    seed_active_slot();
    g_fault.fail_at     = 0L;
    g_fault.persist_ops = 0L;
    g_fault.armed       = 0;
    g_fault.record      = 1;           /* 唯一写操作表的地方 */
    rc                  = run_apply(NULL);
    g_fault.record      = 0;
    n                   = g_fault.persist_ops;
    if (rc != FOTA_DELTA_OK) {
        fprintf(stderr, "FATAL: baseline run failed (%d)\n", rc);
        exit(2);
    }
    return n;
}

/* 在"第 k 次持久化操作、完成 stop_units 个 8 字节单元后"断电，
 * 然后检查三条不变量 + 重放。 */
static void case_inject(long k, long stop_units)
{
    int injected = 0;

    memset(g_flash, FLASH_ERASED, sizeof(g_flash));
    g_fault.persist_ops = 0L;
    g_fault.fail_at     = 0L;
    g_fault.armed       = 0;
    g_fault.record      = 0;           /* 注入与重放**都不许**改写操作表 */
    seed_active_slot();

    g_fault.persist_ops = 0L;
    g_fault.fail_at     = k;
    g_fault.stop_units  = stop_units;

    if (setjmp(g_fault.escape) == 0) {
        int rc;
        g_fault.armed = 1;
        rc = run_apply(NULL);
        g_fault.armed = 0;
        if (rc == FOTA_DELTA_OK) {
            return;                    /* k 未命中任何操作（k > 操作总数） */
        }
        injected = 1;                  /* 没断电但失败了（例如流被截断） */
    } else {
        injected = 1;                  /* 断电 */
    }
    g_fault.armed   = 0;
    g_fault.fail_at = 0L;

    if (!injected) {
        return;
    }

    /* ① 活动槽逐字节不变 */
    if (!active_slot_unchanged()) {
        fail(k, "active slot changed");
    }

    /* ② 目标槽头部 magic 恒为擦除态 ⇒ 结构上不可引导 */
    if (target_header_magic() != 0xFFFFFFFFUL) {
        fail(k, "target header magic is not erased after power loss");
    }

    /* ③ 重放同一补丁：必须成功，且逐字节等于新镜像 */
    {
        int rc2;
        g_fault.persist_ops = 0L;      /* 重放重新计数（操作表已由 record=0 保护） */
        rc2 = run_apply(NULL);
        if (rc2 != FOTA_DELTA_OK) {
            fail(k, "replay after power loss failed");
        } else if (!target_slot_equals_new()) {
            fail(k, "replay produced a wrong image");
        }
        if (!active_slot_unchanged()) {
            fail(k, "active slot changed during replay");
        }
    }
}

/* 该操作应枚举的掉电点数（以 8 字节单元计）。
 *
 * **小操作全枚举**：头部窗口只有 16 B（2 个双字），值得把每个前缀都试一遍 ——
 * "magic 恰好在哪一次写入之后变得合法"完全由它决定，而"magic 恒擦除"正是 L6
 * 的核心不变量。
 * **大操作抽样**：一次擦除是 544 个单元、一个页编程是 256 个；全枚举会让 CI
 * 慢下来而收益很低（没写完的页无论断在哪里都不会产生合法 magic）。
 * `units <= 0` 表示该操作没有"完成度"概念（补丁读），只试一次完全截断。 */
#define EXHAUSTIVE_LIMIT 8L
#define SAMPLE_POINTS    16L

static long n_prefix_points(long units)
{
    if (units <= 0L) {
        return 1L;
    }
    return (units <= EXHAUSTIVE_LIMIT) ? units : SAMPLE_POINTS;
}

static long prefix_units(long units, long i)
{
    if (units <= 0L) {
        return 0L;                     /* 补丁读：直接截断 */
    }
    if (units <= EXHAUSTIVE_LIMIT) {
        return i;                      /* 全枚举：0 .. units-1 */
    }
    /* 抽样：开头 0..3 密集（覆盖窗口边界附近），再均匀铺开，末点取 units-1 */
    if (i < 4L) {
        return i;
    }
    {
        long span = units - 1L;
        long v = (span * i) / (SAMPLE_POINTS - 1L);
        return (v >= span) ? span : v;
    }
}

int main(void)
{
    long baseline_ops;
    long expect = 0L;
    long total  = 0L;
    long k;
    long i;
    long hdr_ops = 0L;

    printf("=== L6 power-loss injection: old=%d B, new=%d B, patch=%d B ===\n",
           (int)FOTA_L6_OLD_IMAGE_SIZE, (int)FOTA_L6_NEW_IMAGE_SIZE,
           (int)FOTA_L6_PATCH_SIZE);

    g_io.ctx = &g_stream;              /* 见 g_io 定义处的注释 */
    g_hdr_lo = (uint32_t)FOTA_SLOT_B_BASE + FOTA_IMG_HDR_OFF;
    g_hdr_hi = g_hdr_lo + FOTA_HDR_HOLE_LEN;

    case_no_fault();

    /* 基线：数出无故障运行里的每一个持久化操作，并记下它们的可细分程度。 */
    baseline_ops = count_persist_ops();
    if (baseline_ops <= 0L || baseline_ops > MAX_OPS) {
        fprintf(stderr, "FATAL: implausible op count %ld\n", baseline_ops);
        return 2;
    }
    for (k = 1L; k <= baseline_ops; k++) {
        if (g_op_touches_header[k]) {
            hdr_ops++;
        }
        expect += n_prefix_points(g_op_units[k]);
    }

    /* 结构性自检：枚举必须真的把操作细分下去。
     *
     * 若 expect == baseline_ops，说明每个操作只试了一个点 —— 这正是"操作表被
     * 重放改写"那次的症状，覆盖率会静默归零而测试照样打印 OK。宁可在这里硬
     * 失败，也不要交出一个看起来很绿、实际什么都没测的 L6。 */
    if (expect <= baseline_ops) {
        fprintf(stderr,
                "FATAL: enumeration degenerated to 1 point/op (%ld ops, %ld points)"
                " — op table was clobbered?\n", baseline_ops, expect);
        return 2;
    }

    printf("injecting at %ld persistent ops (%ld of them overlap the header span)\n",
           baseline_ops, hdr_ops);
#ifdef L6_DUMP_TABLE
    for (k = 1L; k <= baseline_ops; k++) {
        fprintf(stderr, "  op[%ld]: hdr=%d units=%ld npts=%ld\n",
                k, (int)g_op_touches_header[k], g_op_units[k],
                n_prefix_points(g_op_units[k]));
    }
#endif

    for (k = 1L; k <= baseline_ops; k++) {
        long units = g_op_units[k];
        long npts  = n_prefix_points(units);
        for (i = 0L; i < npts; i++) {
            case_inject(k, prefix_units(units, i));
            total++;
            if (g_failures != 0) {
                printf("RESULT: FAILED (%d failures)\n", g_failures);
                return 1;
            }
        }
    }

    /* 实际注入点数必须与预先算出的期望一致：不一致说明主循环读到的操作表和
     * 基线不是同一张，那么上面的"全绿"就没有意义。 */
    if (total != expect) {
        printf("RESULT: FAILED (injected %ld points, expected %ld)\n", total, expect);
        return 1;
    }
    printf("RESULT: OK (%ld injection points, all invariants held)\n", total);
    return 0;
}
