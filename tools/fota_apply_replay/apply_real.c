/*
 * apply_real.c —— 把**真机那份** drv_fota_delta.c 编到主机上，用真实产物重放
 * 一次差分应用，打印真实的 rc / derr。
 *
 * 为什么需要它
 * ------------
 * 差分升级在真机上失败时，**几乎拿不到有用的错误信息**：
 *   · `fota status` 的 `last error` 是 RAM 变量，失败路径会复位 ⇒ 复位即失；
 *   · `.noinit` 的 `hw2c_fault` 记录只存错误**码**、不存细节；
 *   · 串口日志可能被转录工具截断（曾经把 119 B 里的后 71 B 丢掉）。
 * 于是"猜错误码"就成了默认路径 —— 而这次真机根因（信封 fw_version 与新镜像头
 * 不一致 ⇒ `FOTA_DELTA_E_CRC(-19)`）正是靠本工具一步定位的。
 *
 * 它可行的前提是设备侧应用层的设计：`drv_fota_delta.c` 是**纯逻辑 + 后端注入**
 * （无 HAL、无 `#ifdef TEST`），所以同一份源码在主机与目标上行为一致。
 * 生成工程里那份文件可以直接拿来编译，不需要重新渲染模板。
 *
 * 用法
 * ----
 *   gcc -std=c99 -O1 -Wall -Wextra \
 *       -I output/<demo>/src/drivers \
 *       -I static/third_party/hpatch_lite \
 *       -I static/third_party/tinyuz/decompress \
 *       apply_real.c output/<demo>/src/drivers/drv_fota_delta.c \
 *       static/third_party/hpatch_lite/hpatch_lite.c \
 *       static/third_party/tinyuz/decompress/tuz_dec.c -o apply_real
 *
 *   ./apply_real <槽A镜像.bin> <补丁.h2cd> [期望新镜像.bin|-] \
 *                [--a-base 0x08002000] [--b-base 0x08040000] \
 *                [--b-size 262144] [--page 2048]
 *
 * 退出码：0 = 应用成功；1 = 设备侧返回了错误码；2 = 用法/环境错误。
 *
 * ⚠️ 模型必须与真机一致的三个细节（做不到就失去预测力）
 * ----------------------------------------------------
 *   1. 活动槽 = 设备上**真实的字节**（`pyocd cmd -c "savemem <base> <len> f.bin"`），
 *      其余 0xFF。用别的镜像会先卡在 `old_crc32` 上，测不出后面的事。
 *   2. `erase` 按**整页**擦（真 Flash 只按页擦），不是只擦声明长度 ——
 *      否则"擦除是否吃到暂存区"这类边界问题永远暴露不出来。
 *   3. 补丁放在**目标槽尾部的暂存区**：`off = align_up(new_size, page_size)`，
 *      `patch_read` 从信封之后（+48）开始 —— 与 `drv_fota.c` 的接线一致。
 *      这样顺带验证了"擦目标槽不会毁掉暂存区"这条前提。
 */

#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#include "drv_fota_delta.h"

#define ENV_SIZE 48UL

static uint32_t g_a_base = 0x08002000UL;
static uint32_t g_b_base = 0x08040000UL;
static uint32_t g_b_size = 262144UL;
static uint32_t g_page   = 2048UL;

static unsigned char *g_slot_a;
static unsigned char *g_slot_b;
static uint32_t       g_a_size;
static uint32_t       g_env_size = ENV_SIZE;

static unsigned long align_up(unsigned long v, unsigned long a)
{
    return ((v + a - 1UL) / a) * a;
}

/* ---- 地址 -> 缓冲区（越界即失败：宁可报错，也不要"静默读到别的槽"）------- */

static unsigned char *at(uint32_t addr, uint32_t len)
{
    if (addr >= g_a_base && (unsigned long)(addr - g_a_base) + len <= g_a_size) {
        return g_slot_a + (addr - g_a_base);
    }
    if (addr >= g_b_base && (unsigned long)(addr - g_b_base) + len <= g_b_size) {
        return g_slot_b + (addr - g_b_base);
    }
    fprintf(stderr, "!! 越界访问 addr=%#010lx len=%lu（不在任一槽范围内）\n",
            (unsigned long)addr, (unsigned long)len);
    exit(2);
}

/* ---- 后端 ---------------------------------------------------------------- */

typedef struct { uint32_t pos; uint32_t end; } cursor_t;

static cursor_t g_patch_cur;

static uint32_t patch_read(void *ctx, uint8_t *dst, uint32_t len)
{
    cursor_t *c = (cursor_t *)ctx;
    unsigned char *p;
    if (c->pos >= c->end) { return 0u; }
    if (c->pos + len > c->end) { len = c->end - c->pos; }
    p = at(c->pos, len);
    memcpy(dst, p, len);
    c->pos += len;
    return len;
}

static int slot_erase(void *ctx, uint32_t addr, uint32_t len)
{
    unsigned long n = align_up(len, g_page);
    unsigned char *p = at(addr, (uint32_t)n);
    (void)ctx;
    memset(p, 0xFF, n);
    return 0;
}

static int slot_program(void *ctx, uint32_t addr, const uint8_t *src, uint32_t len)
{
    unsigned char *p = at(addr, len);
    (void)ctx;
    memcpy(p, src, len);
    return 0;
}

static void slot_read(void *ctx, uint32_t addr, uint8_t *dst, uint32_t len)
{
    unsigned char *p = at(addr, len);
    (void)ctx;
    memcpy(dst, p, len);
}

static void slot_watchdog(void *ctx) { (void)ctx; }

/* ---- 文件与工具 ---------------------------------------------------------- */

static unsigned char *load(const char *path, unsigned long *out_len)
{
    FILE *f = fopen(path, "rb");
    long n;
    unsigned char *buf;
    if (f == NULL) { fprintf(stderr, "!! 打不开 %s\n", path); exit(2); }
    fseek(f, 0, SEEK_END);
    n = ftell(f);
    fseek(f, 0, SEEK_SET);
    buf = (unsigned char *)malloc((size_t)n);
    if (buf == NULL || fread(buf, 1, (size_t)n, f) != (size_t)n) {
        fprintf(stderr, "!! 读不满 %s\n", path);
        exit(2);
    }
    fclose(f);
    *out_len = (unsigned long)n;
    return buf;
}

static uint32_t rd_u32(const unsigned char *p)
{
    return (uint32_t)p[0] | ((uint32_t)p[1] << 8)
         | ((uint32_t)p[2] << 16) | ((uint32_t)p[3] << 24);
}

static const char *err_name(int rc)
{
    switch (rc) {
    case FOTA_DELTA_OK:             return "OK";
    case FOTA_DELTA_E_ARG:          return "E_ARG(-1)";
    case FOTA_DELTA_E_TOO_SHORT:    return "E_TOO_SHORT(-2)";
    case FOTA_DELTA_E_ENV_MAGIC:    return "E_ENV_MAGIC(-3)";
    case FOTA_DELTA_E_ENV_VERSION:  return "E_ENV_VERSION(-4)";
    case FOTA_DELTA_E_ENV_FLAGS:    return "E_ENV_FLAGS(-5)";
    case FOTA_DELTA_E_ENV_CRC16:    return "E_ENV_CRC16(-6)";
    case FOTA_DELTA_E_AUTH:         return "E_AUTH(-7)";
    case FOTA_DELTA_E_OLD_SIZE:     return "E_OLD_SIZE(-8)";
    case FOTA_DELTA_E_OLD_CRC:      return "E_OLD_CRC(-9)";
    case FOTA_DELTA_E_TOO_BIG:      return "E_TOO_BIG(-10)";
    case FOTA_DELTA_E_IMAGE_HEAD:   return "E_IMAGE_HEAD(-11)";
    case FOTA_DELTA_E_LITE_HEAD:    return "E_LITE_HEAD(-12)";
    case FOTA_DELTA_E_LITE_NEWSIZE: return "E_LITE_NEWSIZE(-13)";
    case FOTA_DELTA_E_PATCH_SIZE:   return "E_PATCH_SIZE(-14)";
    case FOTA_DELTA_E_UNSUPPORTED:  return "E_UNSUPPORTED(-15)";
    case FOTA_DELTA_E_DECOMP:       return "E_DECOMP(-16)";
    case FOTA_DELTA_E_APPLY:        return "E_APPLY(-17)";
    case FOTA_DELTA_E_IO:           return "E_IO(-18)";
    case FOTA_DELTA_E_CRC:          return "E_CRC(-19)";
    case FOTA_DELTA_E_VECTOR:       return "E_VECTOR(-20)";
    default:                        return "??";
    }
}

static void usage(const char *argv0)
{
    fprintf(stderr,
            "用法: %s <槽A镜像.bin> <补丁.h2cd> [期望新镜像.bin|-]\n"
            "          [--a-base 0x08002000] [--a-size <B>] [--b-base 0x08040000]\n"
            "          [--b-size 262144] [--page 2048] [--env-size 48]\n",
            argv0);
}

int main(int argc, char **argv)
{
    const char *paths[3] = { NULL, NULL, NULL };
    int npos = 0;
    unsigned char *old_img, *patch;
    unsigned long old_len, patch_len;
    unsigned long a_size_arg = 0;
    fota_delta_env_t env;
    fota_delta_backend_t io;
    cursor_t c2;
    int derr = 0;
    int rc;
    uint32_t staging_off, live_crc, hdr_img_size;
    int i;

    for (i = 1; i < argc; i++) {
        const char *a = argv[i];
        if (strcmp(a, "--a-base") == 0 && i + 1 < argc) {
            g_a_base = (uint32_t)strtoul(argv[++i], NULL, 0);
        } else if (strcmp(a, "--b-base") == 0 && i + 1 < argc) {
            g_b_base = (uint32_t)strtoul(argv[++i], NULL, 0);
        } else if (strcmp(a, "--a-size") == 0 && i + 1 < argc) {
            a_size_arg = strtoul(argv[++i], NULL, 0);
        } else if (strcmp(a, "--b-size") == 0 && i + 1 < argc) {
            g_b_size = (uint32_t)strtoul(argv[++i], NULL, 0);
        } else if (strcmp(a, "--page") == 0 && i + 1 < argc) {
            g_page = (uint32_t)strtoul(argv[++i], NULL, 0);
        } else if (strcmp(a, "--env-size") == 0 && i + 1 < argc) {
            g_env_size = (uint32_t)strtoul(argv[++i], NULL, 0);
        } else if (a[0] == '-' && a[1] == '-' && strcmp(a, "-") != 0) {
            fprintf(stderr, "!! 未知选项 %s\n", a);
            usage(argv[0]);
            return 2;
        } else if (npos < 3) {
            paths[npos++] = a;
        } else {
            usage(argv[0]);
            return 2;
        }
    }
    if (npos < 2) { usage(argv[0]); return 2; }
    if (g_a_base >= g_b_base) {
        fprintf(stderr, "!! 槽 A 基址必须低于槽 B 基址\n");
        return 2;
    }

    old_img = load(paths[0], &old_len);
    patch   = load(paths[1], &patch_len);

    /* 槽 A 缓冲区覆盖 [a_base, b_base) —— 真机上这两段之间就是整个槽 A */
    g_a_size = a_size_arg ? (uint32_t)a_size_arg : (g_b_base - g_a_base);
    if (old_len > g_a_size) {
        fprintf(stderr, "!! 旧镜像 %lu B 超出槽 A 容量 %lu B\n",
                old_len, (unsigned long)g_a_size);
        return 2;
    }
    g_slot_a = (unsigned char *)malloc(g_a_size);
    g_slot_b = (unsigned char *)malloc(g_b_size);
    if (g_slot_a == NULL || g_slot_b == NULL) { fprintf(stderr, "!! 内存不足\n"); return 2; }

    /* 细节 1：活动槽 = 设备上真实的字节，其余保持擦除态 */
    memset(g_slot_a, 0xFF, g_a_size);
    memset(g_slot_b, 0xFF, g_b_size);
    memcpy(g_slot_a, old_img, old_len);

    /* ---- 1) 解析信封（顺序读，暂存区几何未知前先摆在槽 B 开头）-------- */
    memcpy(g_slot_b, patch, g_env_size < patch_len ? g_env_size : patch_len);
    c2.pos = g_b_base;
    c2.end = g_b_base + g_env_size;
    rc = fota_delta_parse_env(patch_read, &c2, &env);
    printf("=== 1) fota_delta_parse_env rc=%d (%s) ===\n", rc, err_name(rc));
    if (rc != FOTA_DELTA_OK) {
        printf("RC=%d\n", rc);
        return 1;
    }
    printf("    flags=%#06x  old_size=%lu  old_crc32=%#010lx\n",
           (unsigned)env.flags, (unsigned long)env.old_size,
           (unsigned long)env.old_crc32);
    printf("    new_size=%lu  new_crc32=%#010lx  patch_size=%lu  fw_version=%#lx\n",
           (unsigned long)env.new_size, (unsigned long)env.new_crc32,
           (unsigned long)env.patch_size, (unsigned long)env.fw_version);
    printf("    实到文件: 旧=%lu B  补丁=%lu B（信封 %lu + 正文 %lu，%s）\n",
           old_len, patch_len, (unsigned long)g_env_size,
           (unsigned long)(patch_len > g_env_size ? patch_len - g_env_size : 0),
           (patch_len == (unsigned long)(g_env_size + env.patch_size))
               ? "相符" : "**与 patch_size 不符**");

    /* ---- 2) 细节 3：补丁放进目标槽尾部暂存区 ------------------------- */
    memset(g_slot_b, 0xFF, g_b_size);
    staging_off = (uint32_t)align_up(env.new_size, g_page);
    if ((unsigned long)staging_off + patch_len > g_b_size) {
        fprintf(stderr, "!! 暂存区放不下：off=%lu + %lu > %lu\n",
                (unsigned long)staging_off, patch_len, (unsigned long)g_b_size);
        return 2;
    }
    memcpy(g_slot_b + staging_off, patch, patch_len);
    printf("=== 2) 暂存区 off=%lu（= align_up(new_size, %lu)），占用 %lu B ===\n",
           (unsigned long)staging_off, (unsigned long)g_page, patch_len);

    /* ---- 3) 复算设备侧会算的 live_crc（活动槽代码区）----------------- */
    {
        unsigned char hdr[16];
        slot_read(NULL, g_a_base + 0xC0UL, hdr, sizeof(hdr));
        hdr_img_size = rd_u32(hdr);
        printf("=== 3) 槽 A 镜像头 image_size=%lu（+208 = %lu，信封 old_size=%lu）%s ===\n",
               (unsigned long)hdr_img_size, (unsigned long)(hdr_img_size + 208UL),
               (unsigned long)env.old_size,
               (hdr_img_size + 208UL == env.old_size) ? "一致" : "**不一致 ⇒ -8 E_OLD_SIZE**");
    }
    live_crc = FOTA_CRC32_INIT;
    {
        uint32_t len = (env.old_size - 208UL) + 8UL;
        uint32_t done = 0U;
        unsigned char buf[512];
        while (done < len) {
            uint32_t take = len - done;
            if (take > (uint32_t)sizeof(buf)) { take = (uint32_t)sizeof(buf); }
            slot_read(NULL, g_a_base + 0xC8UL + done, buf, take);
            live_crc = fota_delta_crc32_update(live_crc, buf, take);
            done += take;
        }
    }
    live_crc ^= FOTA_CRC32_FINAL_XOR;
    printf("    live_crc=%#010lx  env.old_crc32=%#010lx  %s\n",
           (unsigned long)live_crc, (unsigned long)env.old_crc32,
           (live_crc == env.old_crc32) ? "一致" : "**不一致 ⇒ -9 E_OLD_CRC**");

    /* ---- 4) 应用 ------------------------------------------------------ */
    memset(&io, 0, sizeof(io));
    io.patch_read = patch_read;
    io.erase      = slot_erase;
    io.program    = slot_program;
    io.read       = slot_read;
    io.watchdog   = slot_watchdog;
    io.ctx        = &g_patch_cur;

    g_patch_cur.pos = g_b_base + staging_off + g_env_size;   /* apply 要的是 lite 流起点 */
    g_patch_cur.end = g_b_base + staging_off + (uint32_t)patch_len;

    printf("=== 4) fota_delta_apply ===\n");
    rc = fota_delta_apply(&env, &io, g_a_base, g_b_base, g_b_size, &derr);
    printf("=== 结果 rc=%d (%s)  derr=%d ===\n", rc, err_name(rc), derr);

    if (rc == FOTA_DELTA_OK) {
        if (npos >= 3 && strcmp(paths[2], "-") != 0) {
            unsigned long exp_len;
            unsigned char *exp = load(paths[2], &exp_len);
            int same = (exp_len <= g_b_size
                        && memcmp(g_slot_b, exp, exp_len) == 0);
            printf("    目标槽 vs 期望新镜像: %s（期望 %lu B）\n",
                   same ? "逐字节一致" : "**不一致**", exp_len);
            if (!same) { printf("RC=%d\n", rc); return 1; }
        }
    } else {
        int untouched = (memcmp(g_slot_a, old_img, old_len) == 0);
        printf("    活动槽是否被改动: %s\n", untouched ? "未改动 ✓" : "**被改动了**");
        if (!untouched) { printf("RC=%d\n", rc); return 1; }
    }
    printf("RC=%d DERR=%d\n", rc, derr);
    return 0;
}
