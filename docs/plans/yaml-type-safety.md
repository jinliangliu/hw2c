# YAML 类型安全增强规划（保留 YAML 声明式设计）

> 状态：规划中 · 目标：在保留六层 YAML 作为一等公民的前提下，把
> "运行时字符串解析 / 裸 uint32 载荷 / 手写参数读取"前移到
> **生成期静态校验 + 生成强类型代码**，不引入 TypeScript 替换。

## 1. 背景与目标

hw2c 的六层 YAML（hardware / task / components / bind / params / pubsub）
已通过 Pydantic + validator + 引脚冲突三层校验，表达力足够。类型安全的
短板集中在四处：

| # | 短板 | 后果 |
|---|------|------|
| A | `actions` 是字符串解析（`'led_pattern fast_blink'`） | 动作名/参数写错**运行时**才暴露 |
| B | `event_t` 只有 `id + uint32 param` | 事件载荷无语义，全靠注释，易误用 |
| C | 组件里手写 `param_get("name")` | 参数名/类型拼错运行时才暴露 |
| D | 事件/主题无跨组件契约校验 | 事件没人发、主题类型不匹配生成期不报 |

**原则**：YAML 保持声明式、直观、diff 友好；类型安全靠
**生成期校验 + 生成强类型 API** 实现，不改 YAML 语法风格。

## 2. P0 — 动作生成期校验（最优先）

### 设计

建立**动作注册表**（生成器侧，随模板扩展）：

| 动作 | 合法参数 | 校验规则 |
|------|----------|----------|
| `led_pattern` | off / fast_blink / slow_blink / fault | 枚举匹配 |
| `set(led,on)` | 组件 + 状态 | 组件存在 + 状态合法 |
| `publish <topic>` | pubsub 主题 | 主题已声明 |
| `start_timer` / `stop_timer` | 定时器名 | 存在于 behavior.timers |
| `shell_temp` / `telemetry_snapshot` / `power_status` | 无 | 内置动作表 |
| `log "..."` | 任意 | 常量字符串 |

validator 在生成期逐条校验 `actions`，未知动作/非法参数直接报错并
给出**可用动作列表**（类似引脚冲突的替代建议）。同时 statemachine
模板改为**直接生成对应 C 代码**，不再依赖运行时字符串解析。

### 验收

- `led_pattern fast_blik` → 生成期报错（含候选：fast_blink）
- `publish nonexistent_topic` → 生成期报错
- 全部 demo 重新生成通过，行为无回归

## 3. P1 — 事件载荷类型化

### 设计

task.yaml 增加事件契约声明（或独立 `events.yaml`）：

```yaml
events:
  - name: FALL_DETECTED
    payload: { type: float, unit: "g", range: [0, 4] }
  - name: KNOB_TURNED
    payload: { type: uint32 }
  - name: BUTTON_SHORT_PRESS
    payload: none
```

生成器产出：

```c
/* event_t 载荷：按声明生成（无载荷事件保持 param=0） */
typedef struct {
    event_id_t id;
    union {
        uint32_t raw;
        float    f;
    } payload;
} event_t;

/* 类型化投递：范围/类型在生成期与编译期约束 */
int event_post_fall_detected(float impact_g);
int event_post_knob_turned(uint32_t turns);
```

投递函数对 payload 做范围钳位/断言；消费侧（状态机）按事件读取
`evt.payload.f`，消除裸 uint32 魔法数字。

### 兼容

- 无 `events` 声明时沿用现有 `param` 语义，旧 YAML 零改动
- `param` 字段保留为兼容别名（映射到 `payload.raw`）

## 4. P2 — 参数访问器生成

### 设计

由 params.yaml 生成类型化访问器（param_registry 模板内）：

```c
float    param_knob_damping_get(void);
void     param_knob_damping_set(float v);
uint32_t param_fall_impact_get(void);
```

- 名称 = `param_<name>_get/set`，类型与缩放（如 uint32 x100）由
  params.yaml 决定
- 组件模板改用访问器，删除手写 `param_get("...")`
- validator 交叉校验：组件里引用的访问器必须对应 params.yaml 中
  已声明参数，不存在或类型不符生成期报错

### 收益

- 参数名/类型拼错 → 生成期报错（不再静默回默认值）
- 组件模板更短、更少样板

## 5. P3 — 跨组件契约校验

### 设计

生成期闭环检查：

1. **事件生产-消费闭环**：状态机 transition 消费的每个事件，必须存在
   投递源（RTC 定时器 / EXTI / 组件发布 / events.yaml 声明），
   否则警告（无生产者）；组件发布的每个事件若无消费者，提示
2. **pubsub 主题值类型**：topic 声明 `value: {type, unit}`，
   组件 publish 使用生成包装函数（同 P1 模式），类型/单位不匹配
   生成期报错
3. **bind.yaml 强化**：事件名、引脚名与 hardware/task 交叉校验
   （现有部分保留，补动作/事件侧）

## 6. 落地顺序与验证

1. **P0**：动作注册表 + validator 校验 + statemachine 模板去字符串解析
2. **P1**：事件契约 + event_t 载荷联合体 + 类型化投递函数
3. **P2**：参数访问器生成 + 组件模板迁移 + 交叉校验
4. **P3**：事件/主题闭环校验

每步验证：

- 新增负向用例（错误动作名 / 未知事件 / 错参数名）必须生成期报错
- 全部 demo（base / modbus / spi_flash / mpu6050 / pwm / knob）重新
  生成 + 主机测试 + SIL 无回归
- 生成器/解析器 pytest 全绿

## 7. 风险与开放问题

- **动作注册表维护**：新模板动作需同步注册表——用测试保证
  （动作表覆盖所有模板动作，缺失即失败，类似 pin signals coverage）
- **P1 兼容性**：`event_t` 结构变化影响全部生成代码与 mock——需要
  全量回归；保守方案是保留 `param` 字段并新增 `payload`（共存一个版本）
- **P3 警告策略**：无生产者/消费者默认 warning 而非 error，避免
  破坏现有合法（但松耦合）配置
