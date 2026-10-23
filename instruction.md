# 散射台轨迹复核

散射实验台刚换过采集卡，导出的轨迹片段只剩时间和端点。通道号还在，但同一粒子在碰撞前后的片段没有共同编号；台面上也没有现成的圆心、壁线或事件表可抄。现场人员需要一份能继续用于标定的实验册，把片段归并到粒子轨迹、恢复每次碰撞的接触位置与恢复系数，并从多次壁面反弹中拟合四面墙。数据来自一次静态导出，入口是 `/app/analyze.py`，日常由批次调度器直接调用。

## 导出的样子

待读的实验文件采用 UTF-8 编码，文件体量不超过 `262144` 字节。读入时先认版本、批次、约定、实验四块内容；除此之外没有第五块。版本号以 JSON 整数 `1` 表示，布尔 `true`/`false` 不能替代。`batch_id` 是大写字符串，格式为 `QS-` 后接三段四位大写十六进制，段间用连字符连接，例如 `QS-7A2C-91D0-4B6E`。

单位约定不许改：长度写 `"mm"`，时间写 `"s"`，对象只容纳 `length_unit` 与 `time_unit`。`experiments` 是长度 `1` 到 `6` 的数组。一次实验拆成实验号、壁面标签、传感器三槽，分别落在 `experiment_id`、`wall_labels`、`sensors`。`experiment_id` 是大写字符串，格式为 `EX-` 后接四位大写十六进制、一个连字符、四位大写十六进制。`wall_labels` 恰好是 `["W-A", "W-B", "W-C", "W-D"]`，顺序也不能变。

每个实验的 `sensors` 是长度 `8` 到 `48` 的偶数数组；实验之间可以重复使用通道号，同一实验内不能重复。一条传感器记录由四段数据组成：通道信息写 `channel`，时间窗写 `time_s`，两个端点写 `points_mm`，质量估计写 `mass_mg`。`channel` 是大写字符串，格式为 `CH-` 后接三位大写字母或数字、一个连字符、四位大写十六进制。`time_s` 是两个数的二元素数组，前后分别是片段开始与结束时刻，满足 `0 <= start < end <= 12` 且时长在 `0.010` 到 `0.400` 秒之间。`points_mm` 是两个二元素坐标数组，依次对应开始和结束位置；每个坐标分量在 `-800` 到 `800` 之间，片段端点距离在 `5` 到 `220` 毫米之间。`mass_mg` 是根据散射脉冲面积估计的粒子质量，取值在 `500` 到 `2500` 之间；同一粒子在碰撞两侧的片段质量应一致。这里说的“数”包括有限整数和有限浮点数，但不包括布尔值、字符串或 null。

`experiments` 内不能重复 `experiment_id`。所有对象都拒绝重复键，重叠发生在顶层、`conventions`、实验对象或传感器对象时都算输入损坏。空文件、非对象根值、缺键、额外键、错误类型、越界值、重复通道、重复实验编号、退化片段以及无法支持同一接触的片段集合都不能进入计算。

## 交回的实验册

调用形式是：

```text
python3 /app/analyze.py --input INPUT.json --ledger LEDGER.jsonl --output REPORT.json
```

正常退出不会在标准输出或标准错误留下文字。报告写成 UTF-8、按键名升序、无空格的单行 JSON，并在末尾保留一个换行；不得使用 NaN 或 Infinity。报告根层依次放版本字段 `schema_version`、批次字段 `batch_id`、输入摘要 `input_sha256`、实验清单 `experiments`、壁面模型 `wall_models` 和汇总 `summary`，共六段。

`schema_version` 为整数 `1`，`batch_id` 原样取自输入，`input_sha256` 是输入文件原始字节的 SHA-256 小写十六进制。`experiments` 按输入顺序排列。每份实验输出里，先放 `experiment_id`；其余三块分别叫 `sensor_to_track`、`tracks`、`events`。`sensor_to_track` 恰好覆盖该实验全部通道，值为恢复出的轨迹编号。

`tracks` 按 `track_id` 升序排列；一条轨迹记录承载 `track_id`、`sensor_ids`、`mass_mg` 这三样信息。`track_id` 是 `TRK-` 加轨迹内通道名排序后拼接串的 SHA-256 前十个大写十六进制字符；`sensor_ids` 是升序通道名数组，`mass_mg` 保留三位小数。`events` 按接触时刻升序排列，时刻相同时按 `event_id` 升序；每项恰好包含 `event_id`、`kind`、`contact_time_s`、`contact_mm`、`normal`、`restitution`、`incoming_sensor_ids`、`outgoing_sensor_ids`、`pairs`。`kind` 只能是 `"particle"` 或 `"wall"`。`event_id` 是 `EV-` 加上事件内全部通道名排序后拼接串的 SHA-256 前十个大写十六进制字符。`incoming_sensor_ids` 与 `outgoing_sensor_ids` 都按通道名升序；`pairs` 是按入射通道名升序排列的二元通道数组，同一对两侧质量相差不得超过 `0.001` 毫克，粒子事件有两对，壁面事件有一对。

粒子碰撞的法向取碰撞冲量方向，并规范成 `nx > 0`，或在 `nx == 0` 时取 `ny >= 0`。壁面事件的 `normal` 是外法向，单位向量，指向台面外侧。`contact_mm` 是接触点坐标。`contact_time_s` 保留六位小数，`contact_mm` 与 `wall_models.offset_mm` 保留三位小数，`normal` 保留六位小数，`restitution` 保留四位小数；所有舍入都按十进制 `ROUND_HALF_UP`，不接受二进制浮点的银行家舍入。比较恢复后的几何时，法向方向按真实夹角计算，容差为 `0.35` 度；接触位置按欧氏距离计算，容差为 `0.35` 毫米；壁线有符号偏移按外法向点积计算，容差为 `0.45` 毫米；恢复系数容差为 `0.0015`。位置容差不能代替方向容差，反过来也不行。

`wall_models` 恰好四项，顺序为 `W-A`、`W-B`、`W-C`、`W-D`。每项恰好包含 `wall_id`、`normal`、`offset_mm`、`restitution`。墙编号按外法向极角从 `-pi` 到 `pi` 升序对应，`offset_mm` 是同一个外单位法向与墙线任一点的点积。同一壁面的 `restitution` 是该面全部接触事件法向恢复系数的算术平均值；单个事件的法向恢复系数为出射速度在外法向上的分量除以入射速度向内分量的绝对值。`summary` 恰好包含 `experiment_count`、`sensor_count`、`event_count`、`particle_event_count`、`wall_event_count`、`max_momentum_error`、`min_energy_loss`。前五项为非负整数；`max_momentum_error` 是全部粒子碰撞中 `|delta p_1 + delta p_2|` 的最大值，取 `mg * mm/s`，保留六位小数；`min_energy_loss` 是按 `1 - K_out/K_in` 计算的全部粒子与壁面事件最小值，保留六位小数且不得小于零。

## 账本和失败边界

每次分析先在 `--ledger` 指向的 JSONL 文件中查找同一条成功提交。账本每行恰好包含 `schema_version`、`entry_id`、`batch_id`、`input_sha256`、`report_sha256` 五个键，采用与报告相同的紧凑 JSON 和尾换行。`entry_id` 是 `LE-` 加 `input_sha256 + ":" + report_sha256` 的 SHA-256 前十二个大写十六进制字符；`report_sha256` 是报告原始字节的 SHA-256。相同输入和相同报告再次运行，报告字节不变，账本一个字节也不得增加；新的成功结果只追加一行，已有行不改写、不重排。

输入损坏、账本损坏或计算结果无法唯一闭合时，进程退出码为 `2`，标准输出为空。输入或结构损坏时标准错误只写 `分析未写入: input_invalid` 并以换行结束；账本损坏时只写 `分析未写入: ledger_invalid`；计算歧义时只写 `分析未写入: result_ambiguous`。报告和账本保持调用前的字节；不得残留暂存、备份或半写文件。运行时无法完成两个目标的替换时，错误分类为 `result_ambiguous`，退出码仍为 `2`，两个旧目标都要恢复。

整批最多六个实验、每个实验不超过四十八个片段；从入口启动到成功返回的公开上限是 `30` 秒。入口连续运行两次都应得到相同报告字节。账本、报告和输入都应使用镜像内自带的 Python 标准库处理，判分不依赖外部服务。
