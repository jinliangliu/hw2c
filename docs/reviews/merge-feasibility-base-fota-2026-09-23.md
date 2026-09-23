# 评估：examples/base 与 examples/fota_demo 合并的可行性（2026-09-23）

> 只做评估，未改任何代码。文中数字均来自本机实测（生成 + 哈希比对 + 模板扫描）。

## 一句话结论

**不建议"合并成一个示例"**——真正的痛点不是两个目录的重复，而是 **bootloader 分支不在 CI**
（P1）。合并动的是目录结构，而病根在 `.github/workflows/build_and_test.yml`，合并治不好它。

推荐顺序：**先做 A（把 fota_demo 拉进 CI，修掉 P1）→ 再议 B/C（去重）→ 明确不做 D。**

> ### 状态：方案 A 已实施（2026-09-23）
>
> - `.github/workflows/build_and_test.yml`：build job 改为 **base / fota_demo 双矩阵**；
>   bootloader 侧显式构建 `bootloader` / `app` / `combined` 三个 custom target；
>   槽 B 单独一次 `cmake -B build_b -DHW2C_APP_SLOT=B`。
> - 新增 `tools/ci/verify_slot_images.py`：从**链接脚本解析**期望区间，断言两个槽的镜像
>   各自按自己的基址链接（判据 = 向量表第二字）。
> - 新增护栏：`generator/tests/test_ci_workflow.py`（workflow 结构，变异 6/6 全抓）、
>   `tests/test_ci_verify_slot_images.py`（判据脚本的反例，8 条）。
> - 本机已完整复现 CI 的 fota 构建链：生成 → `cmake --build build` → 三个 target →
>   槽 B → 判据，实测槽 A `0x0800504D` / 槽 B `0x0804304D`，`combined.bin` 119828 B。
> - 顺手修 `examples/fota_demo/task.yaml` 的 `project.name: base` → `fota_demo`。
>
> B / C 未做，D 不做。

---

## 一、事实基线（实测）

### 1.1 输入：两个示例的差异只有一处

| 层 | base | fota_demo | 结论 |
|---|---|---|---|
| `hardware.yaml` | `25003fdfd5bb` | `42de3d8f25a7` | **DIFF** |
| `task.yaml` | `3eae9fcc8596` | `3eae9fcc8596` | 逐字节相同 |
| `bind.yaml` | `9940c14947f8` | `9940c14947f8` | 逐字节相同 |
| `components.yaml` | `ba2fe3457d5e` | `ba2fe3457d5e` | 逐字节相同 |
| `params.yaml` | `a653e0eae2ee` | `a653e0eae2ee` | 逐字节相同 |
| `pubsub.yaml` | `1db9b40253d9` | `1db9b40253d9` | 逐字节相同 |

`hardware.yaml` 105 → 139 行的 diff 只有两块：头部注释改写 + 末尾新增 `bootloader:` 段
（9 行配置 + 注释）。**配置层面 fota_demo = base + `bootloader.enabled: true`。**

### 1.2 产物：fota 是 base 的严格超集

| 项 | 值 |
|---|---|
| base 产物文件 | 89 |
| fota 产物文件 | 120 |
| **仅 base 有** | **0** |
| 仅 fota 有 | **31**（`bootloader/` 9 个、`src/drivers/drv_fota*` 10 个、`linker/app_slot_{a,b}.ld` + `bootloader.ld`、`patch_crc.py`、`fota_format.json`、`src/boot_app.{c,h}`、`test/test_boot_{crc,jump,nvm}.c` + `test_fota_{protocol,ymodem}.c`、`drv_iwdg.{c,h}`） |
| 共有文件中内容不同 | **7 个** |

7 个"同源不同支"的文件（这一条决定是否可合并）：

| 文件 | base | fota | 差异规模 |
|---|---|---|---|
| `CMakeLists.txt` | 199 行 | 355 行 | 180 行（bootloader/app/combined 三个 custom target、槽选择、patch_crc 后处理） |
| `src/main.c` | 387 | 460 | 87 行（FOTA include、`fota_task`、读镜像头版本、App 心跳 2 闪） |
| `src/drivers/drv_shell.c` | 710 | 792 | 82 行（FOTA 命令族） |
| `src/event_mgr.c` | 104 | 109 | 5 行 |
| `src/telemetry.c` | 324 | 325 | 1 行（`{ "fota", 512 }` 任务栈条目） |
| `config/FreeRTOSConfig.h` | 堆 11264 | 堆 13312 | 1 行 |
| `.vscode/launch.json` | — | — | 2 行（elf 名） |

⇒ **两个示例不是"一个包含另一个"，而是同一批模板在 `has_bootloader` 两侧的不同渲染结果。**

### 1.3 模板分支面

`{% if has_bootloader %}` 共 **12 处**，集中在 3 个模板：

- `templates/project/CMakeLists.txt.j2`：7 处（行 24/48/142/203/241/274/339 + 1 处注释）
- `templates/src/main.c.j2`：4 处（行 34/371/428/593）
- `templates/drivers/drv_iwdg.c.j2`：1 处（守卫注释）

这些分支的**另一侧**（`{% else %}` / 不渲染）只有 base 会走到。

### 1.4 实证：交叉生成可行（零框架改动）

```
-i examples/fota_demo/hardware.yaml
--task/--bind/--components/--params/--pubsub  全部指向 examples/base/*.yaml
-o output/_merge_probe
```

产物与 `output/fota_demo` **逐字节一致**，仅 4 处工程名派生差异（`project()`、横幅、
`launch.json`、日志时间戳）—— 工程名取输出目录名（`generate.py:1187`）。
⇒ **"fota_demo 只留 hardware.yaml、其余五层引用 base" 在技术上今天就能用，不需要改生成器。**

### 1.5 CI 现状（这是病根）

`.github/workflows/build_and_test.yml`：

- `env.EXAMPLE_PATH: examples/base` / `OUTPUT_DIR: output/base`，**单矩阵项**；
- 生成用 `--force` ⇒ **跳过编译自检**（`generate.py --force` 语义）；
- 构建只跑 `cmake --build build` ⇒ **只构建 ALL**。
- 后续还有 `output/base/test/run_tests.py`（主机 C 测试）与 `test/sil`（组件 SIL）。

⚠️ 两个隐藏后果：

1. fota 的 `bootloader` / `app` / `combined` 都是 **`add_custom_target`，不带 `ALL`**
   （`output/fota_demo/CMakeLists.txt:297/311/328`）⇒ 即使把 fota_demo 放进矩阵，
   `cmake --build build` **也不会编译引导器**，等于没覆盖。
2. 槽 B 镜像需要第二次 configure：`cmake -B build_b -DHW2C_APP_SLOT=B`（同文件行 48–55）。
   不写这一句，槽 B 永不编译——而"给槽 B 种了槽 A 版本"正是 09-23 白查一轮的坑
   （判据：向量表第二字 A `0x08005DE5` / B `0x08043DE5`）。

### 1.6 顺带的两个事实

- **fota_demo 注释已过时**：`hardware.yaml` 头写着"仓库里没有任何示例开启 bootloader"，
  实际 `examples/mhde_mainboard/hardware.yaml:153` 也开了 `bootloader.enabled: true`。
  但 mhde 同样不在 CI ⇒ 两条 bootloader 路径**都没有构建闸门**。
- **护栏耦合**：`generator/tests/test_generate.py:202`
  `examples = sorted(repo_root.glob("examples/*/task.yaml"))`，断言 `len(examples) >= 8`。
  若删掉 fota_demo 的 `task.yaml`，示例数 10 → 9，**断言仍然通过但静默少扫一个示例**——
  典型的"护栏变空转"。

### 1.7 主机测试的额外收益

`output/*/test/run_tests.py:91` 用 `glob.glob("test_*.c")` 自动发现 ⇒ fota 进 CI 会把
主机测试从 **10 个套件**扩到 **15 个**（+ `test_boot_crc` / `test_boot_jump` /
`test_boot_nvm` / `test_fota_protocol` / `test_fota_ymodem`）。
其中 `test_boot_*` 正是历史教训 7 里"从未编译通过"的那批，把它们拉进 CI 有实打实的价值。

---

## 二、四种方案对比

| 方案 | 消除重复 | bootloader 进 CI | 成本 | 主要风险 |
|---|---|---|---|---|
| **A. 只改 CI**：加 `fota_demo` 矩阵项（含显式 target + 槽 B） | ✗ | ✅ | 低（~40 行 workflow） | 忘记显式 target 就白加（见 1.5） |
| **B. 去重**：fota_demo 只留 `hardware.yaml`，其余五层指向 base | ✅ | ✗（需配 A） | 低（删 5 文件 + 改 CI/README） | 约定变隐式；触碰 1.6 的 glob 假设；用户照 README 生成会困惑 |
| **C. 框架级变体**：hardware 支持 `extends:` / CLI `--overlay` | ✅ | ✗（需配 A） | 中（schema + mapper + CLI + 测试 + 文档，0.5–1 天） | 新增机制本身要有护栏，否则又一处"写了不生效" |
| **D. 合成一个示例**：base 开 bootloader，删 fota_demo | ✅ | ✅（表面） | **高（隐性）** | ❌ 见下 |

### 为什么 D 是最危险的直觉选项

1. **无 bootloader 侧永久失去构建闸门**：12 处 `{% if has_bootloader %}` 的 else 分支
   （以及整条"单片无槽"链接脚本路径）将没有任何示例渲染它 ⇒ 重演 FR-14 被误标 ✅ 的
   同一类失效：**一个从不参与构建的模板等于不存在**。
2. **base 的定位被破坏**：它是 README/docs 里 20+ 处引用的"最小系统"，是用户第一行命令
   的模板。合并后每个新手都要面对双槽布局、0x08002000 起址、combined.bin 与单镜像的取舍。
3. **产物不再"可对比"**：现在 base/fota 的差集可以当成"bootloader 到底带来了什么"的
   现成答案（31 个文件 + 6 个被改的公共文件），合并后这份对照消失。
4. **mhde_mainboard 仍在外面**：即使合并，真板那条 bootloader 路径依旧无 CI。

---

## 三、推荐路线

### 第 1 步（必做，半天）：A —— 把 fota_demo 拉进 CI

workflow 改造要点，缺一不可：

1. `strategy.matrix` 加两组：`{example: examples/base, output: output/base}` 与
   `{example: examples/fota_demo, output: output/fota_demo}`。
2. 生成步骤：五层路径照旧从 `examples/<name>/` 取（**先不要**改指向 base，避免与 B 混在一起）。
3. 构建步骤不能只有 `cmake --build build`，需追加：
   - `cmake --build build --target bootloader`
   - `cmake --build build --target app`（生成 `<proj>_crc.bin`，会跑 `patch_crc.py`）
   - `cmake --build build --target combined`
   - `cmake -B build_b -DHW2C_APP_SLOT=B` + `cmake --build build_b`（槽 B 镜像）
   - 最好再断言：槽 B 镜像向量表第二字 == `0x08043DE5`（防"种了槽 A 版本"）。
4. 顺手修 `examples/fota_demo/task.yaml` 的 `project.name: base`（复制遗留，P4）。

> 代价：CI 时间增加（3 次 configure + 引导器 + 15 个主机测试套件）。本机量级参考：
> base 全量 ~22 s、fota_demo ~17 s，可接受。

### 第 2 步（可选）：B 或 C 去重

- 想要**今天就能落地** → B（已由 1.4 实证可行）。记得同步改
  `generator/tests/test_generate.py` 的发现方式（否则示例数变 9，护栏静默少扫），
  并在 `examples/fota_demo/README.md`（当前缺失）里写清"其余五层来自 examples/base"。
- 想要**关系被显式表达** → C。收益是"fota = base + 一段"这个事实进入配置本身，
  漂移不可能发生；代价是一套新机制及其护栏。

### 不做：D

---

## 四、待你决定的两个问题

1. **mhde_mainboard 要不要一并进 CI？** 它也开 bootloader（真板项目，FR-14 全量 +
   20 KB RTOS 堆）。进 CI 覆盖更全，但它的硬件假设（LSE 晶振等）与构建依赖更重。
2. **B 与 C 二选一，还是都不做？** 如果目标只是"少维护一份 YAML"，B 足够；
   如果目标是"让示例体系能表达变体"，才值得上 C。
