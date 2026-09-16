"""把 HPatchLite 与 tinyuz 的解码器 vendored 进 static/third_party/。

只取解码侧，并记录可机械校验的来源清单（PROVENANCE.json）：
每个文件的 git blob sha1 与 sha256，以及上游仓库/提交/路径。
"逐字节等于上游"由 generator/tests/test_vendor_integrity.py 依该清单校验。

网络说明：本机 raw.githubusercontent.com 不可达（连接被重置/超时），
api.github.com 可达，故统一走 GitHub contents API（返回 base64）。
下载时会把 API 回给的 blob sha1 与本地重算值比对，不一致立即失败。

用法: python vendor_fetch.py <repo_root>
"""
import base64
import hashlib
import json
import os
import subprocess
import sys

HPATCHLITE_REPO = "sisong/HPatchLite"
HPATCHLITE_TAG = "v1.0.2"
HDIFFPATCH_REPO = "sisong/HDiffPatch"
# HPatchLite v1.0.2 的 .gitmodules 指向的 HDiffPatch 提交
HDIFFPATCH_COMMIT = "505eaa5f0bef2c4f70f02cc1bfe1448cf2bf54dc"

TINYUZ_REPO = "sisong/tinyuz"
TINYUZ_TAG = "v1.1.1"

SOURCES = [
    {
        "dest": "static/third_party/hpatch_lite",
        "upstream_repo": HDIFFPATCH_REPO,
        "upstream_ref": HDIFFPATCH_COMMIT,
        "upstream_ref_kind": "commit",
        "pinned_via": "%s %s 的子模块 HDiffPatch" % (HPATCHLITE_REPO, HPATCHLITE_TAG),
        "upstream_subdir": "libHDiffPatch/HPatchLite",
        "files": ["hpatch_lite.c", "hpatch_lite.h",
                  "hpatch_lite_types.h", "hpatch_lite_input_cache.h"],
        "license_repo": HPATCHLITE_REPO,
        "license_ref": HPATCHLITE_TAG,
        "note": "只发布解码器；写侧在 HDiff/Diff 模块，本项目不使用",
    },
    {
        "dest": "static/third_party/tinyuz/decompress",
        "upstream_repo": TINYUZ_REPO,
        "upstream_ref": TINYUZ_TAG,
        "upstream_ref_kind": "tag",
        "pinned_via": TINYUZ_TAG,
        "upstream_subdir": "decompress",
        "files": ["tuz_dec.c", "tuz_dec.h", "tuz_types.h", "tuz_types_private.h"],
        "license_repo": TINYUZ_REPO,
        "license_ref": TINYUZ_TAG,
        "note": "只取 decompress/，不取 compress/（编码器由本项目自研）",
    },
]


def api(path: str) -> dict:
    """调用 api.github.com 并解析 JSON。"""
    url = "https://api.github.com" + path
    res = subprocess.run(
        ["curl", "-sSL", "--max-time", "60", "--fail",
         "-H", "Accept: application/vnd.github+json", url],
        capture_output=True,
    )
    if res.returncode != 0:
        raise RuntimeError("API 失败 %s: %s"
                           % (url, res.stderr.decode("utf-8", "replace").strip()))
    return json.loads(res.stdout.decode("utf-8"))


def fetch_contents(repo: str, ref: str, path: str):
    """经 contents API 取一个文件，返回 (bytes, api_sha)。"""
    data = api("/repos/%s/contents/%s?ref=%s" % (repo, path, ref))
    if isinstance(data, list):
        raise RuntimeError("%s 是目录，不是文件" % path)
    if data.get("encoding") != "base64":
        raise RuntimeError("%s 的 encoding=%s，非预期" % (path, data.get("encoding")))
    blob = base64.b64decode(data["content"])
    if len(blob) != data["size"]:
        raise RuntimeError("%s 长度不符：API 声明 %d，解出 %d"
                           % (path, data["size"], len(blob)))
    return blob, data["sha"]


def blob_sha1(data: bytes) -> str:
    """git blob 的 sha1（与 GitHub contents/trees API 的 sha 同口径）。"""
    return hashlib.sha1(b"blob %d\0" % len(data) + data).hexdigest()


def main(repo_root: str) -> int:
    grand_total = 0
    for spec in SOURCES:
        dest_dir = os.path.join(repo_root, *spec["dest"].split("/"))
        os.makedirs(dest_dir, exist_ok=True)
        print("== %s ==" % spec["dest"])

        entries = {}
        for name in spec["files"]:
            path = "%s/%s" % (spec["upstream_subdir"], name)
            blob, api_sha = fetch_contents(spec["upstream_repo"], spec["upstream_ref"], path)
            local_sha = blob_sha1(blob)
            if local_sha != api_sha:
                raise RuntimeError("%s 的 blob sha1 不符：本地 %s vs 上游 %s"
                                   % (name, local_sha, api_sha))
            with open(os.path.join(dest_dir, name), "wb") as fh:
                fh.write(blob)
            entries[name] = {
                "size": len(blob),
                "blob_sha1": local_sha,
                "sha256": hashlib.sha256(blob).hexdigest(),
            }
            print("  %-28s %7d B  blob %s  (与上游一致)" % (name, len(blob), local_sha[:12]))

        lic, lic_sha = fetch_contents(spec["license_repo"], spec["license_ref"], "LICENSE")
        if blob_sha1(lic) != lic_sha:
            raise RuntimeError("LICENSE 的 blob sha1 不符")
        with open(os.path.join(dest_dir, "LICENSE.txt"), "wb") as fh:
            fh.write(lic)
        entries["LICENSE.txt"] = {
            "size": len(lic),
            "blob_sha1": lic_sha,
            "sha256": hashlib.sha256(lic).hexdigest(),
            "is_license": True,
        }
        print("  %-28s %7d B  (LICENSE)" % ("LICENSE.txt", len(lic)))

        manifest = {
            "_comment": [
                "vendored 第三方源码的来源清单。",
                "'逐字节等于上游'由 generator/tests/test_vendor_integrity.py 依本清单校验；",
                "校验口径 sha256，另记 blob_sha1（与 GitHub API 的 sha 同口径，便于比对）。",
                "更新上游时：改 upstream_ref，重跑 tools/vendor_fetch.py，再跑测试。",
                "⚠️ 本目录只读 —— 见 AGENTS.md 的供应商源码只读清单。",
            ],
            "name": os.path.basename(spec["dest"]),
            "upstream_repo": spec["upstream_repo"],
            "upstream_ref": spec["upstream_ref"],
            "upstream_ref_kind": spec["upstream_ref_kind"],
            "pinned_via": spec["pinned_via"],
            "upstream_subdir": spec["upstream_subdir"],
            "license": "MIT",
            "note": spec["note"],
            "readonly": True,
            "files": entries,
        }
        with open(os.path.join(dest_dir, "PROVENANCE.json"), "w", encoding="utf-8") as fh:
            json.dump(manifest, fh, indent=2, ensure_ascii=False)
            fh.write("\n")

        sub = sum(e["size"] for e in entries.values() if not e.get("is_license"))
        grand_total += sub
        print("  小计 %d B（不含 LICENSE）" % sub)
        print()

    print("源码合计 %d B" % grand_total)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1] if len(sys.argv) > 1 else "."))
