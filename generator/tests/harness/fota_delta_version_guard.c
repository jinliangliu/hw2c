/*
 * fota_delta_version_guard.c —— 把「信封版本号 ≠ 新镜像头版本号」这个组合
 * 直接喂给**设备侧真实代码**，断言它确实以 `FOTA_DELTA_E_CRC(-19)` 失败。
 *
 * 为什么这条要在设备代码上证一遍
 * ------------------------------
 * 保护措施（`delta_tool.build()` 拒绝不一致的 fw_version）是主机侧的。
 * 主机侧护栏只能证明"我们不再生成这种补丁"，证明不了"这种补丁在设备上会怎样"。
 * 而"设备上会怎样"才是决定它该不该在主机侧拦的依据：
 *
 *   设备侧 `fota_delta_apply()` 在 FLUSH 阶段把**信封里的** fw_version 写进
 *   目标槽镜像头，而镜像 CRC 覆盖区从 magic 起、包含 fw_version 那 4 字节
 *   ⇒ 版本号不一致时，算出的 CRC 与信封里的 new_crc32 必然不同
 *   ⇒ 返回 -19，而 -19 的字面含义是「新镜像 CRC 不符」，即"内容已错"。
 *
 * 2026-09-17 真机实测就是这个形态：设备报 `HW2C_FAULT_FOTA_APPLY`，活动槽
 * 毫发无损，而错误码把人引向"差分/Flash/传输"。本台架把这条因果链固定在
 * 测试里 —— 若哪天设备侧改成"版本号不符就报一个专门的码"，这个测试会红，
 * 那时就可以放心地把主机侧护栏放宽。
 *
 * 与 fota_delta_l6_harness.c 的关系：那个测掉电安全（同一份源码、注入失败），
 * 这个测版本号契约（同一份源码、只换输入）。两者共用同一套渲染与编译流程。
 *
 * 模型与真机一致的三个细节见 generator/tests/test_fota_delta_l6.py::_run_version_guard。
 */

#include <stdio.h>
#include <string.h>

#include "drv_fota_delta.h"
#include "fota_version_guard_vectors.h"

static unsigned char g_flash[VG_SLOT_B_BASE - VG_SLOT_A_BASE + VG_SLOT_B_SIZE];

static unsigned long align_up(unsigned long v, unsigned long a)
{
    return ((v + a - 1UL) / a) * a;
}

static unsigned char *at(unsigned int addr, unsigned int len)
{
    if (addr < VG_SLOT_A_BASE
        || (unsigned long)(addr - VG_SLOT_A_BASE) + len > sizeof(g_flash)) {
        fprintf(stderr, "!! 越界 addr=%#010x len=%u\n", addr, len);
        return NULL;
    }
    return g_flash + (addr - VG_SLOT_A_BASE);
}

/* ---- 后端 ---------------------------------------------------------------- */

typedef struct { unsigned int pos; unsigned int end; } cursor_t;

static cursor_t g_cur;

static unsigned int patch_read(void *ctx, unsigned char *dst, unsigned int len)
{
    cursor_t *c = (cursor_t *)ctx;
    unsigned char *p;
    if (c->pos >= c->end) { return 0u; }
    if (c->pos + len > c->end) { len = c->end - c->pos; }
    p = at(c->pos, len);
    if (p == NULL) { return 0u; }
    memcpy(dst, p, len);
    c->pos += len;
    return len;
}

static int slot_erase(void *ctx, unsigned int addr, unsigned int len)
{
    unsigned long n = align_up(len, VG_PAGE_SIZE);   /* 真 Flash 只按整页擦 */
    unsigned char *p = at(addr, (unsigned int)n);
    (void)ctx;
    if (p == NULL) { return -1; }
    memset(p, 0xFF, n);
    return 0;
}

static int slot_program(void *ctx, unsigned int addr, const unsigned char *src,
                        unsigned int len)
{
    unsigned char *p = at(addr, len);
    (void)ctx;
    if (p == NULL) { return -1; }
    memcpy(p, src, len);
    return 0;
}

static void slot_read(void *ctx, unsigned int addr, unsigned char *dst,
                      unsigned int len)
{
    unsigned char *p = at(addr, len);
    (void)ctx;
    if (p == NULL) { return; }
    memcpy(dst, p, len);
}

static void slot_watchdog(void *ctx) { (void)ctx; }

/* ---- 一次实验：把某个补丁应用到全新的槽 A/槽 B 上 ------------------------ */

static int run_once(const unsigned char *patch, unsigned int patch_len,
                    unsigned int staging_off)
{
    fota_delta_env_t env;
    fota_delta_backend_t io;
    cursor_t c2;
    int derr = 0;
    int rc;

    /* 每次实验都从干净的 Flash 开始：槽 A 放真实旧镜像，槽 B 全擦除态 */
    memset(g_flash, 0xFF, sizeof(g_flash));
    memcpy(g_flash, g_vg_old, VG_OLD_SIZE);

    /* 描述符解析：流从暂存区第 0 字节（信封）开始 */
    memcpy(at(VG_SLOT_B_BASE + staging_off, patch_len), patch, patch_len);
    c2.pos = VG_SLOT_B_BASE + staging_off;
    c2.end = c2.pos + (unsigned int)VG_ENV_SIZE;
    rc = fota_delta_parse_env(patch_read, &c2, &env);
    if (rc != FOTA_DELTA_OK) {
        printf("    parse_env rc=%d\n", rc);
        return rc;
    }

    memset(&io, 0, sizeof(io));
    io.patch_read = patch_read;
    io.erase      = slot_erase;
    io.program    = slot_program;
    io.read       = slot_read;
    io.watchdog   = slot_watchdog;
    io.progress   = NULL;
    io.ctx        = &g_cur;

    /* apply 的约定：patch_read 第一次读就要返回 **lite 流**首字节 */
    g_cur.pos = VG_SLOT_B_BASE + staging_off + (unsigned int)VG_ENV_SIZE;
    g_cur.end = VG_SLOT_B_BASE + staging_off + patch_len;

    return fota_delta_apply(&env, &io, VG_SLOT_A_BASE, VG_SLOT_B_BASE,
                            VG_SLOT_B_SIZE, &derr);
}

int main(void)
{
    unsigned int staging_off;
    int rc_ok, rc_bad;

    /* 暂存区 = align_up(new_size, page)（与 drv_fota.c 的接线一致）。
     * 两个补丁的 new_size 相同，所以共用一个偏移。 */
    staging_off = (unsigned int)align_up(VG_NEW_SIZE, VG_PAGE_SIZE);
    if ((unsigned long)staging_off + VG_PATCH_BAD_SIZE > VG_SLOT_B_SIZE) {
        printf("RESULT: FAIL 暂存区放不下\n");
        return 2;
    }
    printf("staging_off=%u  new_size=%u  patch_ok=%u  patch_bad=%u\n",
           staging_off, (unsigned)VG_NEW_SIZE,
           (unsigned)VG_PATCH_OK_SIZE, (unsigned)VG_PATCH_BAD_SIZE);

    /* 1) 版本号一致的补丁：必须成功，且目标槽逐字节等于新镜像 */
    rc_ok = run_once(g_vg_patch_ok, VG_PATCH_OK_SIZE, staging_off);
    printf("consistent   rc=%d\n", rc_ok);
    if (rc_ok == FOTA_DELTA_OK) {
        const unsigned char *dst = at(VG_SLOT_B_BASE, VG_NEW_SIZE);
        if (dst == NULL || memcmp(dst, g_vg_new, VG_NEW_SIZE) != 0) {
            printf("RESULT: FAIL 应用成功但目标槽与新镜像不一致\n");
            return 2;
        }
    }

    /* 2) 只有信封版本号不同（其余字节完全一样）：必须 -19 */
    rc_bad = run_once(g_vg_patch_bad, VG_PATCH_BAD_SIZE, staging_off);
    printf("mismatched   rc=%d\n", rc_bad);

    printf("OK_RC=%d\n", rc_ok);
    printf("BAD_RC=%d\n", rc_bad);

    if (rc_ok == FOTA_DELTA_OK && rc_bad == FOTA_DELTA_E_CRC) {
        printf("RESULT: OK\n");
        return 0;
    }
    printf("RESULT: FAIL 期望 consistent=0 / mismatched=%d\n", FOTA_DELTA_E_CRC);
    return 1;
}
