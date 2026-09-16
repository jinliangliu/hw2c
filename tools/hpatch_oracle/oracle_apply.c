/*
 * oracle_apply.c —— 主机侧 apply，**仅作测试判据**，不是产品代码。
 *
 * 为什么需要它：裁决 1 决定「只借解码器、自研 Python 写侧」，于是格式正确性
 * 的关注点从「第三方工具对不对」变成「我们写的编码器对不对」，而 HPatchLite
 * 没有格式规范文档、只有解码器源码。唯一可靠的判据就是：**用 vendored 的解码器
 * 去消费我们编码器产出的流，逐字节比对**。
 *
 * 因此本文件的目标是「忠实、无歧义地驱动 vendored 解码器」，不做任何优化、
 * 不做任何容错扩展。它引用的全部是上游源码，自己只提供 I/O 回调。
 *
 * 用法:
 *     oracle_apply <old.bin> <patch.bin> <out.bin>
 *
 * 退出码:
 *     0  应用成功（输出已写出）
 *     1  apply 失败（补丁或旧数据不合法）
 *     2  用法 / 文件 I/O 错误
 *
 * 标准输出末行是机器可读的统计（测试脚本据此断言）：
 *     ORACLE_STAT compress_type=0 new_size=123 uncompress_size=0 tuz_calls=0
 */

#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#include "hpatch_lite.h"
#ifdef _ORACLE_WITH_TUZ
#include "tuz_dec.h"
#endif

/* 与 hpatchi.c 的默认分配同量级；主机侧内存充裕，取大一点省去调参。 */
#ifndef ORACLE_PATCH_CACHE_SIZE
#define ORACLE_PATCH_CACHE_SIZE (64u * 1024u)
#endif

/* ------------------------------------------------------------------ */
/* 内存流：与 hpatchi.c 的 _read_diff 同语义                          */
/* ------------------------------------------------------------------ */

typedef struct {
    const uint8_t *data;
    size_t         size;
    size_t         pos;
} MemReader;

/* 约定同上游：永远返回 hpi_TRUE，*size = 实际取到的字节数，0 表示 EOF。
 * 返回 FALSE 会被解码器当作「读错误」而非「结束」，语义完全不同。 */
static hpi_BOOL mem_read(hpi_TInputStreamHandle handle, hpi_byte *out, hpi_size_t *size)
{
    MemReader *r = (MemReader *)handle;
    size_t want = (size_t)(*size);
    size_t avail = (r->pos < r->size) ? (r->size - r->pos) : 0u;
    size_t take = (want < avail) ? want : avail;

    if (take > 0u) {
        memcpy(out, r->data + r->pos, take);
        r->pos += take;
    }
    *size = (hpi_size_t)take;
    return hpi_TRUE;
}

/* ------------------------------------------------------------------ */
/* 应用器                                                              */
/* ------------------------------------------------------------------ */

typedef struct {
    hpatchi_listener_t base;
    const uint8_t     *old_data;
    size_t             old_size;
    uint8_t           *new_data;
    size_t             new_size;
    size_t             new_cap;
} Applier;

static hpi_BOOL oracle_read_old(hpatchi_listener_t *listener, hpi_pos_t read_from_pos,
                                hpi_byte *out_data, hpi_size_t data_size)
{
    Applier *self = (Applier *)listener;

    /* 越界一律失败：解码器不会替我们做边界检查，
     * 让「编码器产出了越界 cover」在这里立刻暴露，而不是静默读到垃圾。 */
    if ((size_t)read_from_pos + (size_t)data_size > self->old_size) {
        fprintf(stderr, "oracle: read_old 越界 (pos=%lu len=%lu old_size=%lu)\n",
                (unsigned long)read_from_pos, (unsigned long)data_size,
                (unsigned long)self->old_size);
        return hpi_FALSE;
    }
    memcpy(out_data, self->old_data + read_from_pos, (size_t)data_size);
    return hpi_TRUE;
}

static hpi_BOOL oracle_write_new(hpatchi_listener_t *listener, const hpi_byte *data,
                                 hpi_size_t data_size)
{
    Applier *self = (Applier *)listener;

    if (self->new_size + (size_t)data_size > self->new_cap) {
        size_t want = (self->new_size + (size_t)data_size) * 2u + 4096u;
        uint8_t *grown = (uint8_t *)realloc(self->new_data, want);
        if (!grown) {
            fprintf(stderr, "oracle: 输出缓冲扩容失败\n");
            return hpi_FALSE;
        }
        self->new_data = grown;
        self->new_cap = want;
    }
    memcpy(self->new_data + self->new_size, data, (size_t)data_size);
    self->new_size += (size_t)data_size;
    return hpi_TRUE;
}

/* ------------------------------------------------------------------ */
/* tinyuz 解压胶水（与官方 decompresser_demo.h 的 tuz 段等价）        */
/* ------------------------------------------------------------------ */

/* 解压回调的调用次数。它是「压缩路径真的被执行了」的直接证据 ——
 * 没有它，一个「compress_type 写了但没人读」的静默失效会看不出来。 */
static unsigned long g_tuz_decompress_calls = 0;

#ifdef _ORACLE_WITH_TUZ


static size_t tuz_get_reserved_mem_size(hpi_TInputStreamHandle codeStream,
                                        hpi_TInputStream_read readCode)
{
    const tuz_size_t dictSize = tuz_TStream_read_dict_size(codeStream, readCode);

    /* 上游写法：dictSize-1 超出上限即视为错误（dictSize==0 也在此被抓住） */
    if ((tuz_size_t)(dictSize - 1u) >= tuz_kMaxOfDictSize) {
        return 0u;
    }
    return (size_t)dictSize;
}

static hpi_BOOL tuz_decompress(hpi_TInputStreamHandle diffStream, hpi_byte *out_part_data,
                               hpi_size_t *data_size)
{
    ++g_tuz_decompress_calls;
    return (tuz_STREAM_END >= tuz_TStream_decompress_partial((tuz_TStream *)diffStream,
                                                             out_part_data, data_size))
               ? hpi_TRUE
               : hpi_FALSE;
}

#endif /* _ORACLE_WITH_TUZ */

/* ------------------------------------------------------------------ */
/* 文件工具                                                            */
/* ------------------------------------------------------------------ */

static int read_file(const char *path, uint8_t **out, size_t *out_size)
{
    FILE *fh = fopen(path, "rb");
    long  len;

    if (!fh) {
        fprintf(stderr, "oracle: 打不开 %s\n", path);
        return 0;
    }
    if (fseek(fh, 0, SEEK_END) != 0 || (len = ftell(fh)) < 0 || fseek(fh, 0, SEEK_SET) != 0) {
        fprintf(stderr, "oracle: 无法定位 %s 的长度\n", path);
        fclose(fh);
        return 0;
    }
    *out = (uint8_t *)malloc((size_t)len + 1u);
    if (!*out) {
        fclose(fh);
        return 0;
    }
    *out_size = fread(*out, 1u, (size_t)len, fh);
    fclose(fh);
    return 1;
}

static int write_file(const char *path, const uint8_t *data, size_t size)
{
    FILE *fh = fopen(path, "wb");
    if (!fh) {
        fprintf(stderr, "oracle: 无法写出 %s\n", path);
        return 0;
    }
    if (size > 0u && fwrite(data, 1u, size, fh) != size) {
        fprintf(stderr, "oracle: 写入 %s 不完整\n", path);
        fclose(fh);
        return 0;
    }
    fclose(fh);
    return 1;
}

/* ------------------------------------------------------------------ */

int main(int argc, char **argv)
{
    uint8_t  *old_data = NULL, *patch_data = NULL;
    size_t    old_size = 0, patch_size = 0;
    Applier   applier;
    MemReader patch_reader;
    hpi_compressType compress_type = hpi_compressType_no;
    hpi_pos_t new_size = 0, uncompress_size = 0;
    uint8_t  *temp_cache = NULL;
    hpi_size_t temp_cache_size;
    int       ok = 0;

    if (argc != 4) {
        fprintf(stderr, "用法: %s <old.bin> <patch.bin> <out.bin>\n", argv[0]);
        return 2;
    }
    if (!read_file(argv[1], &old_data, &old_size) ||
        !read_file(argv[2], &patch_data, &patch_size)) {
        goto done;
    }

    memset(&applier, 0, sizeof(applier));
    applier.old_data = old_data;
    applier.old_size = old_size;
    applier.base.read_old = oracle_read_old;
    applier.base.write_new = oracle_write_new;

    patch_reader.data = patch_data;
    patch_reader.size = patch_size;
    patch_reader.pos = 0;
    applier.base.diff_data = &patch_reader;
    applier.base.read_diff = mem_read;

    /* 1) 读 lite 头（明文，未被压缩） */
    if (!hpatch_lite_open(&patch_reader, mem_read, &compress_type, &new_size, &uncompress_size)) {
        fprintf(stderr, "oracle: hpatch_lite_open() 失败（头不合法）\n");
        goto done;
    }

    /* 2) 按 compress_type 装配补丁输入流 */
    if (compress_type == hpi_compressType_no) {
        temp_cache_size = (hpi_size_t)ORACLE_PATCH_CACHE_SIZE;
    }
#ifdef _ORACLE_WITH_TUZ
    else if (compress_type == hpi_compressType_tuz) {
        size_t   patch_buf_size = (((size_t)ORACLE_PATCH_CACHE_SIZE + 1u) * 3u) / 4u;
        size_t   dec_buf_size = (size_t)ORACLE_PATCH_CACHE_SIZE - patch_buf_size;
        size_t   reserved = tuz_get_reserved_mem_size(&patch_reader, mem_read);
        size_t   decompress_mem_size = reserved + dec_buf_size;
        tuz_TStream *tuz = NULL;
        uint8_t *pmem;

        if (reserved == 0u) {
            fprintf(stderr, "oracle: tuz dict_size 读取失败\n");
            goto done;
        }
        pmem = (uint8_t *)malloc(decompress_mem_size);
        tuz = (tuz_TStream *)malloc(sizeof(tuz_TStream));
        if (!pmem || !tuz) {
            fprintf(stderr, "oracle: tuz 缓冲分配失败\n");
            free(pmem);
            free(tuz);
            goto done;
        }
        if (tuz_OK != tuz_TStream_open(tuz, &patch_reader, mem_read,
                                       pmem, (tuz_size_t)reserved, (tuz_size_t)dec_buf_size)) {
            fprintf(stderr, "oracle: tuz_TStream_open() 失败\n");
            free(pmem);
            free(tuz);
            goto done;
        }
        applier.base.diff_data = tuz;
        applier.base.read_diff = tuz_decompress;
        temp_cache_size = (hpi_size_t)patch_buf_size;
        /* pmem 与 tuz 故意不释放：进程随即退出，且保持代码路径可读 */
    }
#endif
    else {
        fprintf(stderr, "oracle: 不支持的 compress_type=%d\n", (int)compress_type);
        goto done;
    }

    /* 3) 分配 temp_cache 并应用 */
    temp_cache = (uint8_t *)malloc((size_t)temp_cache_size);
    if (!temp_cache) {
        fprintf(stderr, "oracle: temp_cache 分配失败\n");
        goto done;
    }
    if (!hpatch_lite_patch(&applier.base, new_size, temp_cache, temp_cache_size)) {
        fprintf(stderr, "oracle: hpatch_lite_patch() 失败\n");
        goto done;
    }
    if ((size_t)new_size != applier.new_size) {
        fprintf(stderr, "oracle: 输出长度 %lu 与头声明的 new_size %lu 不符\n",
                (unsigned long)applier.new_size, (unsigned long)new_size);
        goto done;
    }
    if (!write_file(argv[3], applier.new_data, applier.new_size)) {
        goto done;
    }

    printf("ORACLE_STAT compress_type=%d new_size=%lu uncompress_size=%lu tuz_calls=%lu\n",
           (int)compress_type, (unsigned long)new_size, (unsigned long)uncompress_size,
           g_tuz_decompress_calls);
    ok = 1;

done:
    free(old_data);
    free(patch_data);
    free(temp_cache);
    free(applier.new_data);
    return ok ? 0 : 1;
}
