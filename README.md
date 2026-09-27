# 博物馆藏品来源与返还审查

标准库实现、SQLite 持久化的独立项目。它管理藏品、历史流转事件、来源引用、证据、权利主张和审查阶段，并提供面向公众、主张人、审查员和工作人员的分层视图。

## 运行

```bash
python3 app.py --init --seed
python3 app.py
```

访问 <http://127.0.0.1:8103>。数据库默认是 `provenance.db`。测试命令：

```bash
python3 -m unittest -v
```

演示身份通过 `X-User-Id` 传入：`staff`、`reviewer1`、`claimant1`、`public`。

## 主要接口

- `POST /api/objects`、`GET /api/objects`、`GET /api/objects/{id}`：藏品登记与分层查看。
- `POST /api/objects/{id}/update`：更新藏品并创建完整快照。
- `POST /api/sources`、`POST /api/objects/{id}/events`：来源与流转事件。
- `POST /api/objects/{id}/evidence`：上传证据，服务端计算 SHA-256。
- `POST /api/objects/{id}/claims`：提交权利主张。
- `POST /api/claims/{id}/transition`：按 `submitted → under_review → negotiating → resolved_return/rejected` 流转；补件期间主张处于 `awaiting_materials`。
- `POST /api/claims/{id}/supplements`：审查员对主张提出补件要求（`material_category` 材料类别 + `description` 说明），主张自动进入补件中。
- `POST /api/supplements/{id}/materials`：主张人上传一份或几份材料回应补件项（可多次补交，服务端计算 SHA-256）。
- `POST /api/supplements/{id}/close`：审查员查看材料并填写核查意见后结束补件项；主张人不能自行关闭。
- `GET /api/supplements/materials/{id}`：下载补件材料（审查员/工作人员/该主张的主张人可见，公众不可见）。
- `GET /api/objects/{id}/history` 与 `/history/{version}`：版本历史及历史快照。

补件规则：任一补件项未被审查员核查结束（状态非 `closed`）时，进入协商或完成返还会被拦截，接口返回 `409 supplement_pending` 并在 `details.missing_categories` 中列出缺少的材料类别；驳回不受影响。公众只能看到主张处于“补件中”及补件项的公开状态，看不到材料类别、说明、材料本身和内部核查意见；主张人可看到自己主张下的全部明细。每次提出、回应、核查补件都会生成新版本快照，快照保留补件项与材料元数据（不含文件二进制）。

`--seed` 会创建一个已进入“补件中”的演示主张，打开演示页后依次切换 `reviewer1 → claimant1 → reviewer1` 即可走完提出、回应、核查与拦截过程。

此外：公众看不到持有人和内部事件；主张人只能查看自己的主张；阶段不能跳跃或从终态重新打开；每次对象变化都会保存 JSON 快照和审计记录。
