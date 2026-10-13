# 一起到 · 多人异地会合旅行助手

版本 0.4.3。本地恢复并修复了原 0.1.0 项目。给 2–4 位旅伴比较“去哪会合”：通过飞猪查询往返交通和酒店，计算每人已知费用、共同停留时间及交通负担，输出可解释的城市方案。

目前是可执行的 Python Skill，包含输入校验、CLI、离线样例和测试，网页源码另见仓库 `sites/yiqidao/`。新智能体需要支持 Skill 文件和 Python/bash 工具。上传作品包不等于完成平台部署或大赛提交。

## 运行

需要 Python 3.9+（仅标准库），在解压出的 `yiqidao` 目录运行：

```bash
python3 -m unittest discover -s tests -v
python3 scripts/yiqidao.py plan --request examples/offline_replay.json --mode fixture --cache-dir examples/fixtures --output outputs/demo.md --save-state outputs/demo-state.json
```

样例中的航班、酒店、价格和日期均为虚构或固定测试条件，不能预订。期望：杭州方案、共同停留 54 小时、团队已知交通与住宿费用 3600 元，真实调用 0 次。该示例显式提供虚构的已确认整房报价，并将缓冲设为0以复现；正常请求默认使用60/90分钟缓冲。结果不含餐饮、市内交通等杂费。

真实调用需要安装官方 CLI（Node/npm 环境）：

```bash
npm install -g @fly-ai/flyai-cli
flyai --help
```

API Key 由官方 CLI/智能体环境管理，不写在提示词、代码或公开包里。本脚本不会自动加载 `.env`。具体凭证配置以安装版本的帮助为准。先复制请求样例并替换成员、候选、日期和预算，不使用演示作为真实请求。

```bash
python3 scripts/yiqidao.py validate --request outputs/request.json
python3 scripts/yiqidao.py plan --request outputs/request.json --cache-dir .cache --verbose --output outputs/result.md
```

默认每次请求超时 30 秒，一轮最多 300 秒和 80 次真实调用，串行执行。遇到鉴权错误/451/429停止该轮，成功结果已缓存。输出 JSON 可加 `--format json`，包含完整方案与留痕。退出码：0 有可行结果，2 需补问，3 已查询范围内没有可行结果，4 输入/配置/查询错误，5 有候选但仍待确认或搜索不完整，6 日期范围未查完且没有候选结果。

## 断档恢复

`plan` 开始时就保存结构化请求到 `outputs/latest-state.json`，结束更新完整结果。成功查询落盘 `.cache`；模型停止后重新运行可以复用有效缓存。

```bash
python3 scripts/yiqidao.py plan --resume-state outputs/latest-state.json --cache-dir .cache --output outputs/result.md
```

实时缓存有效期默认 900 秒，可用 `--cache-ttl` 调整，原查询时间始终保留。隔天继续通常需要重新查询。修改某人条件时，把状态文件中的 `request` 提取成新 JSON，只修改对应字段，再校验并运行；参数相同且未过期的查询可以复用。

跨会话必须把代码、request、状态、结果和工作说明下载备份，不能依赖模型记忆或云端临时目录。当前上传包只包含代码与虚构样例，不包含昨天的真实运行缓存/证据。需要回放私有缓存时使用 `--mode fixture`，该模式缺文件会报错，绝不会回落到联网。

## 当前边界

- 默认最多查询3组共同可行日期与用户提供的最多5个城市；可用--max-date-pairs调整，仍受调用次数与总时限约束。未搜索的范围不能判定无解。
- 去程与返程分别查询，再枚举逐人的往返组合、扫描共同时间窗口，在返回班次内优先完整交通价格并最小化已知费用。搜索范围和独立穷举对照见docs/logic-audit.md。
- 酒店搜索展示价不作为整房房价，不计入预算。仅明确确认的 `hotel_quote` 可按每房每晚计算，校验日期和房型人数；规则见 docs/hotel-quote-rules.md。费用按人数均摊，最终可订与结算仍待核实。
- 共同停留默认扣除到站后60分钟、离站前90分钟缓冲，仍包含睡眠休息；到家截止另预留home_buffer_minutes（默认60分钟）。跨日到达需确认夜间入住。
- 餐饮、门票、接驳和杂费不计入已知小计；预算状态不能解释为旅行总价承诺。
- 未知票价保留缺失，失败、空结果和确定条件冲突分别处理。历史缓存、虚构演示与真实查询来源在结果中留痕。
- 没有代付/自动预订/退款功能。现有云端智能体已完成单城与三城真实调用，见 docs/live-acceptance-20261012.md；住宿完整报价仍待确认；网站与参赛材料见仓库首页。

## 文件

`SKILL.md` 是智能体入口；`scripts/` 是查询、校验、计算、输出；`tests/` 是离线测试；`examples/fixtures/` 是虚构数据；`docs/upload-and-resume.md` 是新智能体操作说明；`docs/local-fixes.md` 说明修复和验证边界。

当前整体审计结论与仍未覆盖的边界见 [docs/logic-audit.md](docs/logic-audit.md)。

## 0.4.2 修复

默认整房住宿候选及备选均排除名称含青年公寓、青旅、床位、胶囊等共享住宿提示的结果，再按接口评分排列；全部被排除则提示补查。名称筛选不证明房型、容量、位置或可订性。用户核实的整房报价仍可明确采用。
所有文档使用 ASCII 文件名，并同步修复引用，避免平台解压改写中文路径。用项目根目录的 package_skill.py 生成并校验交付包。


### 0.4.3 补齐
逐段换乘间隔披露与风险优先排序：同站火车少于30分钟、同机场飞机少于120分钟、跨站或站点缺失需要确认。这是产品初筛规则，不代表铁路/航空公司最短换乘标准；时间充足也不证明联程保障、库存或实际衔接。先满足用户硬性限制，再优先无上述风险组合，最后比较已知费用；仅剩风险组合仍保留并请求确认。
同日22点后、6点前或跨日到达提示夜间入住；具体到店时间仍需考虑接驳。住宿展示价继续不计预算，酒店报价确认不等于库存确认。普通聊天追问、报价补充及风险摘要见SKILL.md。
本版本本地回归通过；0.4.2云端验收不能作为0.4.3云端实测。
