"""CI 构建闸门的护栏：两个示例必须都在矩阵里，bootloader 侧必须真的被构建。

为什么需要它
------------
`.github/workflows/build_and_test.yml` 曾经只有一个矩阵项 `examples/base`，
后果是 bootloader / 差分 OTA 的整批模板**永不渲染、永不编译**（FR-14 被误标 ✅
的根因）。而修好之后还有两个更隐蔽的退化方式：

  · 把 `fota_demo` 加进矩阵、但构建步骤只有 `cmake --build build` ——
    bootloader / app / combined 都是 `add_custom_target`（**不带 ALL**），
    一句默认构建**一个都不会构建**，CI 全绿但什么也没覆盖；
  · 忘了槽 B 的第二次 configure（`-DHW2C_APP_SLOT=B`）—— 槽 B 永远是槽 A 版本。

这三条都不是"改坏了会报错"的类型，全是静默失效，所以必须由护栏盯住。
依据：docs/reviews/merge-feasibility-base-fota-2026-09-23.md
"""

from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
WORKFLOW = REPO_ROOT / ".github" / "workflows" / "build_and_test.yml"
VERIFY_SCRIPT = REPO_ROOT / "tools" / "ci" / "verify_slot_images.py"


def _load():
    yaml = pytest.importorskip("yaml")
    assert WORKFLOW.is_file(), "找不到 workflow：%s" % WORKFLOW
    return yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))


def _build_steps(wf):
    job = wf["jobs"]["build"]
    steps = job["steps"]
    # 正控：解析出来的东西得像真的 —— 否则下面的断言可能只是"没接上"
    assert len(steps) >= 8, "只解析到 %d 个步骤，路径/结构可能变了" % len(steps)
    return job, steps


def _runs(steps):
    return "\n".join((s.get("run") or "") for s in steps)


def test_both_examples_are_in_the_build_matrix():
    """无 bootloader 与有 bootloader 两条路径都必须有示例在编译。

    只留一个 ⇒ 另一侧的 `{% if has_bootloader %}` 分支永不渲染，
    一个从不参与构建的模板等于不存在。
    """
    job, _ = _build_steps(_load())
    matrix = job.get("strategy", {}).get("matrix", {})
    examples = [m.get("example") for m in matrix.get("include", [])]
    assert "examples/base" in examples, "base（无 bootloader 侧）掉出矩阵了"
    assert "examples/fota_demo" in examples, (
        "fota_demo（bootloader 侧）掉出矩阵了 —— bootloader / 差分 OTA 的模板"
        "会重新回到『从不参与构建』的状态")
    assert len(examples) == len(set(examples)), "矩阵里出现重复示例：%r" % examples


def test_example_paths_come_from_the_matrix_not_from_env():
    """示例路径不许回到顶层 env。

    单示例时代它就是 `env.EXAMPLE_PATH: examples/base`。写回 env 意味着
    矩阵形同虚设（某处仍在用它），所以这里直接禁止这个键存在。
    """
    wf = _load()
    env = wf.get("env") or {}
    for key in ("EXAMPLE_PATH", "OUTPUT_DIR"):
        assert key not in env, (
            "workflow 的顶层 env 里又出现了 %s —— 示例路径必须来自 matrix，"
            "否则两个示例里只有一个会被真正构建" % key)


@pytest.mark.parametrize("target", ["bootloader", "app", "combined"])
def test_bootloader_custom_targets_are_built_explicitly(target):
    """bootloader / app / combined 必须被显式构建。

    它们是 `add_custom_target`（不带 ALL），`cmake --build build` 不会碰它们。
    """
    _, steps = _build_steps(_load())
    runs = _runs(steps)
    assert "--target %s" % target in runs, (
        "workflow 里找不到 `--target %s` —— 它是 custom target，不在 ALL 里，"
        "默认构建不会碰它，bootloader 侧的模板等于没被编译" % target)


def test_slot_b_gets_its_own_cmake_configure():
    """槽 B 镜像需要单独一次 configure，否则永远是按槽 A 链接的版本。"""
    _, steps = _build_steps(_load())
    runs = _runs(steps)
    assert "HW2C_APP_SLOT=B" in runs, (
        "workflow 里找不到 `-DHW2C_APP_SLOT=B` —— 槽 B 镜像会一直用 "
        "app_slot_a.ld 链接，而 CRC 只覆盖文件本身、与基址无关，"
        "这种错误在校验时看不出来，只在跳过去之后崩")
    assert "build_b" in runs, "槽 B 应该有独立的构建目录（build_b）"


def test_slot_linkage_verdict_step_exists_and_its_script_is_shipped():
    """判据步骤要在，且它调用的脚本必须随仓库发布。"""
    _, steps = _build_steps(_load())
    hits = [s for s in steps
            if "verify_slot_images.py" in (s.get("run") or "")]
    assert hits, (
        "workflow 里没有槽链接判据步骤 —— 少了它，『给槽 B 种了按槽 A 链接的"
        "镜像』会一路校验通过，直到跳过去才崩")
    assert VERIFY_SCRIPT.is_file(), "判据脚本不在仓库里：%s" % VERIFY_SCRIPT

    # 参数必须齐全：少给一个，判据要么跑不起来、要么退化成只验一半。
    run = hits[0]["run"]
    for arg in ("--slot-a", "--slot-b", "--linker-a", "--linker-b",
                "--bootloader-ld", "--combined"):
        assert arg in run, (
            "判据步骤缺少 %s —— 期望区间是从链接脚本解析的，少一个参数判据就不完整"
            % arg)


def test_bootloader_only_steps_are_gated_on_the_matrix_flag():
    """bootloader 专属步骤必须带 `if: matrix.bootloader`。

    否则 base（没有这些 cmake target）会在 CI 里直接失败，
    而修的人很可能顺手把整条步骤删掉 —— 于是覆盖范围又退回去了。
    """
    _, steps = _build_steps(_load())
    keywords = ("bootloader", "slot b", "verify each slot")
    gated = 0
    for step in steps:
        name = (step.get("name") or "").lower()
        if not any(k in name for k in keywords):
            continue
        gated += 1
        assert "matrix.bootloader" in (step.get("if") or ""), (
            "步骤「%s」是 bootloader 专属的，却没有 `if: matrix.bootloader` —— "
            "它在 base 那条矩阵上会失败" % step.get("name"))
    assert gated >= 3, (
        "只找到 %d 个 bootloader 专属步骤（应该 ≥3：bootloader/app/combined、"
        "槽 B、判据）—— 关键字匹配失效会让这条护栏空转" % gated)


def test_fota_demo_task_yaml_does_not_claim_to_be_base():
    """示例里的 project.name 不许张冠李戴。

    `examples/fota_demo/task.yaml` 曾经写着 `name: base`。它不生效（工程名取
    输出目录名），但示例本身就是文档 —— 用户照抄会得到一份自相矛盾的配置。
    """
    task = REPO_ROOT / "examples" / "fota_demo" / "task.yaml"
    yaml = pytest.importorskip("yaml")
    data = yaml.safe_load(task.read_text(encoding="utf-8"))
    assert data.get("project", {}).get("name") == "fota_demo", (
        "examples/fota_demo/task.yaml 的 project.name 应该是 fota_demo，"
        "实际是 %r" % data.get("project", {}).get("name"))
