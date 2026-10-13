# 一起到——多人异地会合旅行助手

朋友从不同城市出发，先比较去哪儿会合，再决定行程。一起到支持 2–4 位成员，按日期、交通偏好与预算上限查询候选城市，展示共同停留时间、已知费用和每个人的往返负担。

在线体验：https://yiqidao-meet-travel.mingtian008.chatgpt.site/

## 功能

- 填写成员、出发城市、日期、预算和候选城市；本地规则辅助识别城市与预算。
- 百炼 Managed Agent 调用 yiqidao Skill，使用 FlyAI 官方 CLI 查询真实交通及酒店/景点候选。
- 程序计算城市比较和逐人往返组合，展示查询来源、时间及待确认项。
- 查询进度与刷新恢复、保存方案、匿名分享、PNG 导出。
- 明确区分真实查询、缓存与虚构演示。

## 结构（建议参赛仓库采用）

```text
sites/yiqidao/    网站：页面、API、数据库结构和网站验证脚本
yiqidao/          Skill 与 Python 规划引擎、虚构样例和离线测试
提交材料/         参赛说明、截图与复现说明
```

团队：落梦飞花；主要提交人与领奖人：hope0719。

演示视频：https://github.com/hope0719/yiqidao/releases/tag/demo-v1

## 快速体验与离线验证

线上按表单确认条件后发起规划，选择城市查看详情并导出。真实请求可能受到额度和上游限流影响；已有结果是查询快照，预订前需要再次核实。

不调用付费服务的引擎验证：

```bash
cd yiqidao
python3 -m unittest discover -s tests -v
python3 scripts/yiqidao.py plan --request examples/offline_replay.json --mode fixture --cache-dir examples/fixtures --output outputs/demo.md --save-state outputs/demo-state.json
```

Python 3.9+；样例数据为虚构，不能预订。网站开发与真实服务配置见 `提交材料/03-技术说明与复现.md`。

## 数据与能力边界

已知费用是已确认项目的小计，酒店展示价不能作为整房报价。未确认住宿、库存、接驳及其他支出时，全程预算为未知；不承诺预算满足、全市场最优或当前可订。不自动下单或支付。

共同停留是重叠停留窗口扣除配置缓冲后的时长，包含睡眠休息，不等同于可游玩时长。换乘初筛不代表运输方的联程保障。

截图主案例为长沙＋上海：杭州共同停留 45.6 小时、已知交通 587 元；结果仅代表该次查询快照。

密钥只通过服务端秘密环境配置提供。公开仓库不得包含 `.env`、`.dev.vars`、凭证、私人缓存、控制台导出或用户个人记录。
